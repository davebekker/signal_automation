import asyncio
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv

from utils.google_auth_wrapper import GoogleConnection
from utils.tools import logger

load_dotenv()


CONFIG_CANDIDATES = (
    Path("config/nest_bot.json"),
    Path("data/nest_bot_config.json"),
    Path("nest_bot_config.json"),
)

DEFAULT_CONFIG: Dict[str, Any] = {
    "sync_interval_minutes": 30,
    "messaging_enabled": False,
    "state_file": "nest_state.json",
    "download_path": "data/nest_clips",
    "pending_archive_dir": "data/nest_pending_archive",
    "drive_base": None,
    "drive_archive_path": None,
    "mount_drive": None,
    "monitored_cameras": ["Backyard", "Nest Doorbell (battery)"],
    "ignored_cameras": ["Rookery"],
    "lookback_cap_minutes": 150,
    "first_sync_lookback_minutes": 180,
    "sync_overlap_minutes": 2,
    "max_folder_gb": 3,
    "max_age_days": 30,
    "recent_events_limit": 50,
    "pending_retry_limit": 25,
    "delete_local_after_drive_copy": True,
    "google_username": None,
    "google_master_token": None,
}


def _deep_merge(base: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load_json_config() -> Dict[str, Any]:
    for path in CONFIG_CANDIDATES:
        if path.exists():
            try:
                with path.open("r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if not isinstance(loaded, dict):
                    logger.warning("Nest config %s is not a JSON object; ignoring it", path)
                    return {}
                logger.info("Loaded Nest config from %s", path)
                return loaded
            except Exception as exc:
                logger.error("Failed to read Nest config %s: %s", path, exc)
                return {}
    return {}


def _env_bool(name: str, default: Optional[bool] = None) -> Optional[bool]:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _load_config() -> Dict[str, Any]:
    config = _deep_merge(DEFAULT_CONFIG, _load_json_config())

    # Environment variables remain supported as optional overrides for existing deployments.
    env_overrides = {
        "google_username": os.getenv("GOOGLE_USERNAME"),
        "google_master_token": os.getenv("GOOGLE_MASTER_TOKEN"),
        "drive_base": os.getenv("DRIVE_BASE"),
        "mount_drive": os.getenv("MOUNT_DRIVE"),
        "download_path": os.getenv("DOWNLOAD_PATH"),
    }
    for key, value in env_overrides.items():
        if value:
            config[key] = value

    if os.getenv("NEST_SYNC_INTERVAL_MINUTES"):
        config["sync_interval_minutes"] = int(os.getenv("NEST_SYNC_INTERVAL_MINUTES", "30"))
    if os.getenv("NEST_MESSAGING_ENABLED") is not None:
        config["messaging_enabled"] = bool(_env_bool("NEST_MESSAGING_ENABLED"))

    return config


def _atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp_path, path)


def _safe_filename(value: str) -> str:
    value = value.strip().replace("/", "-").replace("\\", "-")
    value = re.sub(r"[^A-Za-z0-9._() -]+", "_", value)
    value = re.sub(r"\s+", "_", value)
    return value[:180] or "nest_event"


def _format_bytes(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def _format_dt(value: Optional[dt.datetime]) -> str:
    if not value:
        return "never"
    local = value.astimezone() if value.tzinfo else value
    return local.strftime("%d %b %Y %H:%M")


class NestBot:
    def __init__(self):
        self.config = _load_config()
        self.sync_interval = int(self.config.get("sync_interval_minutes") or 30)
        self.messaging_enabled = bool(self.config.get("messaging_enabled"))

        self.state_file = Path(str(self.config.get("state_file") or "nest_state.json"))
        self.download_path = Path(str(self.config.get("download_path") or "data/nest_clips"))
        self.pending_archive_dir = Path(str(self.config.get("pending_archive_dir") or "data/nest_pending_archive"))
        self.download_path.mkdir(parents=True, exist_ok=True)
        self.pending_archive_dir.mkdir(parents=True, exist_ok=True)

        self.drive_base = self.config.get("drive_base")
        self.drive_archive_path = self.config.get("drive_archive_path")
        self.mount_drive = self.config.get("mount_drive")
        self.monitored = list(self.config.get("monitored_cameras") or [])
        self.ignored_cameras = list(self.config.get("ignored_cameras") or [])
        self.lookback_cap_minutes = int(self.config.get("lookback_cap_minutes") or 150)
        self.first_sync_lookback_minutes = int(self.config.get("first_sync_lookback_minutes") or 180)
        self.sync_overlap_minutes = int(self.config.get("sync_overlap_minutes") or 2)
        self.max_folder_gb = float(self.config.get("max_folder_gb") or 3)
        self.max_age_days = int(self.config.get("max_age_days") or 30)
        self.recent_events_limit = int(self.config.get("recent_events_limit") or 50)
        self.pending_retry_limit = int(self.config.get("pending_retry_limit") or 25)
        self.delete_local_after_drive_copy = bool(self.config.get("delete_local_after_drive_copy", True))

        self.username = self.config.get("google_username")
        self.token = self.config.get("google_master_token")
        if not self.username or not self.token:
            logger.warning("Nest Google credentials are missing; Nest sync may fail until configured")

        self.conn = GoogleConnection(self.token, self.username)
        self.devices = self.conn.get_nest_camera_devices()
        self.state = self.load_state()
        self.recent_events: List[Tuple[dt.datetime, str, str]] = []
        self.manual_sync_trigger = asyncio.Event()

    def load_state(self) -> Dict[str, Any]:
        if self.state_file.exists():
            try:
                content = self.state_file.read_text(encoding="utf-8").strip()
                if not content:
                    return {}
                loaded = json.loads(content)
                return loaded if isinstance(loaded, dict) else {}
            except (json.JSONDecodeError, OSError) as exc:
                logger.error("Error loading Nest state file %s: %s. Resetting state.", self.state_file, exc)
                return {}
        return {}

    def save_state(self) -> None:
        _atomic_write_json(self.state_file, self.state)

    def _drive_path(self) -> Optional[Path]:
        if self.drive_archive_path:
            return Path(str(self.drive_archive_path))
        if self.drive_base:
            return Path(str(self.drive_base))
        return None

    def _drive_available(self) -> bool:
        path = self._drive_path()
        return bool(path and path.exists() and path.is_dir())

    async def _try_wake_drive(self) -> bool:
        if self._drive_available():
            return True
        if not self.mount_drive:
            return False
        logger.warning("⚠️ Google Drive mount not found. Attempting to wake it up...")
        try:
            proc = await asyncio.create_subprocess_exec(
                "gio",
                "mount",
                str(self.mount_drive),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=10)
        except Exception as exc:
            logger.warning("Could not wake Google Drive mount: %s", exc)
        await asyncio.sleep(2)
        return self._drive_available()

    def _local_clip_path(self, device_name: str, event_time: dt.datetime) -> Path:
        filename = _safe_filename(f"{device_name}_{event_time.strftime('%Y%m%d_%H%M%S')}.mp4")
        return self.download_path / filename

    def _pending_path_for(self, filename: str) -> Path:
        return self.pending_archive_dir / _safe_filename(filename)

    def _archive_path_for(self, filename: str) -> Optional[Path]:
        drive_path = self._drive_path()
        if not drive_path:
            return None
        return drive_path / _safe_filename(filename)

    def _copy_to_drive(self, source: Path, destination: Path) -> bool:
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            tmp_destination = destination.with_suffix(destination.suffix + ".tmp")
            shutil.copyfile(source, tmp_destination)
            os.replace(tmp_destination, destination)
            if destination.exists() and destination.stat().st_size == source.stat().st_size:
                logger.info("📁 Archived %s to Google Drive", destination.name)
                return True
            logger.error("Drive archive verification failed for %s", destination)
            return False
        except Exception as exc:
            logger.error("Error copying %s to Google Drive: %s", source.name, exc)
            return False

    async def flush_pending_archives(self) -> Tuple[int, int]:
        """Copy locally staged clips to Drive when the GVFS mount is available."""
        pending_files = sorted(
            [p for p in self.pending_archive_dir.iterdir() if p.is_file() and not p.name.endswith(".tmp")],
            key=lambda p: p.stat().st_mtime,
        )
        if not pending_files:
            return (0, 0)
        if not await self._try_wake_drive():
            logger.info("Drive unavailable; %s pending Nest clip(s) remain staged locally", len(pending_files))
            return (0, len(pending_files))

        copied = 0
        for source in pending_files[: self.pending_retry_limit]:
            destination = self._archive_path_for(source.name)
            if not destination:
                break
            if self._copy_to_drive(source, destination):
                try:
                    source.unlink()
                except OSError as exc:
                    logger.warning("Could not remove staged Nest clip %s: %s", source, exc)
                copied += 1
        remaining = len([p for p in self.pending_archive_dir.iterdir() if p.is_file() and not p.name.endswith(".tmp")])
        if copied:
            logger.info("✅ Flushed %s staged Nest clip(s) to Google Drive; %s pending", copied, remaining)
        return (copied, remaining)

    def _stage_for_later(self, source: Path) -> Path:
        destination = self._pending_path_for(source.name)
        if source.resolve() == destination.resolve():
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        return destination

    def _record_recent_event(self, event_time: dt.datetime, camera: str, filepath: Path) -> None:
        self.recent_events.append((event_time, camera, str(filepath)))
        if len(self.recent_events) > self.recent_events_limit:
            self.recent_events = self.recent_events[-self.recent_events_limit :]

    async def _archive_or_stage(self, local_path: Path) -> Path:
        """Archive to Drive when possible. Otherwise stage locally and return the usable clip path."""
        if await self._try_wake_drive():
            destination = self._archive_path_for(local_path.name)
            if destination and self._copy_to_drive(local_path, destination):
                logger.info("✅ Successfully archived to Drive: %s", local_path.name)
                if self.delete_local_after_drive_copy:
                    try:
                        local_path.unlink(missing_ok=True)
                    except OSError as exc:
                        logger.warning("Could not remove local Nest clip %s: %s", local_path, exc)
                return destination

        staged = self._stage_for_later(local_path)
        logger.warning("Drive unavailable; staged Nest clip locally for retry: %s", staged)
        if self.delete_local_after_drive_copy and local_path.exists() and local_path != staged:
            try:
                local_path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("Could not remove local Nest clip %s: %s", local_path, exc)
        return staged

    def cleanup_storage(self) -> None:
        """Deletes local working/staged files older than N days or if folders exceed GB limit."""
        for folder in (self.download_path, self.pending_archive_dir):
            if not folder.exists():
                continue
            files = [p for p in folder.iterdir() if p.is_file()]
            files.sort(key=lambda p: p.stat().st_mtime)
            now = time.time()
            for path in files[:]:
                try:
                    if path.stat().st_mtime < now - (self.max_age_days * 86400):
                        path.unlink()
                        files.remove(path)
                except OSError as exc:
                    logger.warning("Could not clean old Nest file %s: %s", path, exc)

            total_size = sum(p.stat().st_size for p in files if p.exists())
            while total_size > (self.max_folder_gb * 1024**3) and files:
                oldest = files.pop(0)
                try:
                    size = oldest.stat().st_size
                    oldest.unlink()
                    total_size -= size
                    logger.info("Removed old Nest local clip to stay within storage cap: %s", oldest)
                except OSError as exc:
                    logger.warning("Could not remove old Nest file %s: %s", oldest, exc)

    def _pending_summary(self) -> Tuple[int, int]:
        files = [p for p in self.pending_archive_dir.iterdir() if p.is_file() and not p.name.endswith(".tmp")]
        return len(files), sum(p.stat().st_size for p in files)

    def _last_sync_time(self) -> Optional[dt.datetime]:
        values = []
        for value in self.state.values():
            if isinstance(value, str):
                try:
                    values.append(dt.datetime.fromisoformat(value))
                except ValueError:
                    continue
        return max(values) if values else None

    def _status_text(self) -> str:
        pending_count, pending_size = self._pending_summary()
        drive_path = self._drive_path()
        drive_status = "available" if self._drive_available() else "unavailable"
        recent = self.recent_events[-1] if self.recent_events else None
        recent_text = f"{recent[1]} at {_format_dt(recent[0])}" if recent else "none this run"
        devices = len(self.devices) if self.devices else 0
        monitored = ", ".join(self.monitored) if self.monitored else "none"
        return (
            "🏠 Nest status\n\n"
            f"Sync interval: {self.sync_interval} min\n"
            f"Signal alerts: {'on' if self.messaging_enabled else 'off'}\n"
            f"Cameras discovered: {devices}\n"
            f"Monitored for alerts: {monitored}\n"
            f"Drive archive: {drive_status}\n"
            f"Drive path: {drive_path or 'not configured'}\n"
            f"Pending local clips: {pending_count} ({_format_bytes(pending_size)})\n"
            f"Last sync marker: {_format_dt(self._last_sync_time())}\n"
            f"Last event seen: {recent_text}\n\n"
            "Commands: `/sync now`, `/nest status`, `/nest flush`, `/events`, `/get [number]`."
        )

    async def handle_command(self, text):
        parts = text.split()
        if not parts:
            return self._status_text()
        cmd = parts[0].lower()

        if cmd == "/sync":
            if len(parts) > 1:
                if parts[1].lower() == "now":
                    self.manual_sync_trigger.set()
                    return "🔄 Manual Nest sync triggered! Checking cameras now..."
                try:
                    self.sync_interval = int(parts[1])
                    return f"⏳ Nest sync interval updated to {self.sync_interval} minutes."
                except ValueError:
                    return "❌ Please provide a valid number of minutes."
            return f"Nest sync interval is currently {self.sync_interval} minutes."

        if cmd == "/message":
            if len(parts) > 1:
                sub = parts[1].lower()
                if sub == "on":
                    self.messaging_enabled = True
                    return "🔔 Nest Signal alerts: ON."
                if sub == "off":
                    self.messaging_enabled = False
                    return "🔕 Nest Signal alerts: OFF. Background backup remains active."
            return f"Nest alerts are currently {'ON' if self.messaging_enabled else 'OFF'}."

        if cmd in ["/status", "/nest"]:
            if len(parts) == 1 or (len(parts) > 1 and parts[1].lower() == "status"):
                return self._status_text()
            if len(parts) > 1 and parts[1].lower() == "flush":
                copied, remaining = await self.flush_pending_archives()
                return f"📦 Nest archive flush complete. Copied: {copied}. Pending: {remaining}."

        if cmd == "/flush":
            copied, remaining = await self.flush_pending_archives()
            return f"📦 Nest archive flush complete. Copied: {copied}. Pending: {remaining}."

        if cmd in ["/usage", "/help"]:
            return (
                "🏠 Nest Bot\n\n"
                "• `/sync [minutes]` - Show or set camera download interval\n"
                "• `/sync now` - Trigger a sync immediately\n"
                "• `/message [on/off]` - Toggle Signal video alerts\n"
                "• `/events` - Show last 10 events seen during this run\n"
                "• `/get [event]` - Get full video for an event\n"
                "• `/nest status` or `/status` - Show Drive/archive health\n"
                "• `/nest flush` or `/flush` - Retry pending local clips to Drive\n\n"
                "If Google Drive is unavailable after reboot, clips are staged locally and retried later."
            )

        if cmd == "/events":
            if not self.recent_events:
                return "📭 No recent events recorded in this bot run."
            msg = "📹 Recent Nest events\n\n"
            shown = self.recent_events[-10:]
            for i, (ts, cam, filepath) in enumerate(shown, start=1):
                exists = "✅" if Path(filepath).exists() else "⚠️"
                msg += f"{i}. {exists} [{cam}] {_format_dt(ts)}\n"
            msg += "\nUse `/get [number]` for the full clip. ✅ means the bot can currently see the file."
            return msg

        if cmd == "/get" and len(parts) > 1:
            try:
                idx = int(parts[1]) - 1
                shown = self.recent_events[-10:]
                target = shown[idx]
                filepath = Path(target[2])
                if not filepath.exists():
                    return "❌ I no longer have local access to that clip. Try `/nest status` to check Drive/pending archive state."
                return ("FILE", f"Sending full clip for {target[1]}...", str(filepath))
            except (ValueError, IndexError):
                return "❌ Invalid event number."

        return "❓ Unknown Nest command. Try `/help`."

    async def sync_task(self, alert_callback):
        """Background sync loop with local staging when Google Drive is unavailable."""
        while True:
            try:
                await self.flush_pending_archives()
                now = dt.datetime.now(dt.timezone.utc)
                for device in self.devices:
                    d_id = getattr(device, "device_id", device.device_name)
                    if any(ignored in device.device_name for ignored in self.ignored_cameras):
                        continue

                    last_ts_str = self.state.get(d_id)
                    if last_ts_str:
                        last_ts = dt.datetime.fromisoformat(last_ts_str)
                        diff_minutes = int((now - last_ts).total_seconds() / 60) + self.sync_overlap_minutes
                        delta = min(max(diff_minutes, 1), self.lookback_cap_minutes)
                    else:
                        delta = self.first_sync_lookback_minutes
                        last_ts = now - dt.timedelta(minutes=min(delta, self.lookback_cap_minutes))

                    logger.info("Syncing %s (Lookback: %sm)", device.device_name, delta)
                    events = device.get_events(end_time=now, duration_minutes=delta)
                    latest_event_time = last_ts

                    for event in (events or []):
                        if latest_event_time and event.start_time <= latest_event_time:
                            continue

                        local_path = self._local_clip_path(device.device_name, event.start_time)
                        if not local_path.exists():
                            video_bytes = device.download_camera_event(event)
                            if video_bytes:
                                local_path.parent.mkdir(parents=True, exist_ok=True)
                                with local_path.open("wb") as f:
                                    f.write(video_bytes)

                                usable_path = local_path
                                if device.device_name in self.monitored and self.messaging_enabled:
                                    await alert_callback(
                                        f"Alert: {device.device_name} - {event.start_time.strftime('%d-%m-%Y_%H:%M:%S')}",
                                        str(local_path),
                                    )
                                usable_path = await self._archive_or_stage(local_path)
                                self._record_recent_event(event.start_time, device.device_name, usable_path)

                        if not latest_event_time or event.start_time > latest_event_time:
                            latest_event_time = event.start_time

                    if events:
                        self.cleanup_storage()
                    if latest_event_time:
                        self.state[d_id] = latest_event_time.isoformat()
                        self.save_state()

                logger.info("Nest Syncing %s cameras...", len(self.devices))
            except Exception as exc:
                logger.exception("Nest Sync Error: %s", exc)

            try:
                await asyncio.wait_for(self.manual_sync_trigger.wait(), timeout=self.sync_interval * 60)
            except asyncio.TimeoutError:
                pass
            finally:
                self.manual_sync_trigger.clear()
