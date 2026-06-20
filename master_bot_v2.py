"""Robust master process for the Signal bot bridge.

This is intentionally conservative: it keeps the existing bot modules and public
behaviour, but hardens orchestration, routing, Signal IO, attachment handling,
SQLite persistence, and task supervision.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, Mapping, Optional

import aiohttp
from dotenv import load_dotenv

# Import existing bot logic. These modules are intentionally left unchanged.
from budget_bot import BudgetBot
from train_bot import TrainBot
from bin_bot import BinBot
from nest_bot import NestBot
from bots.reminder_bot import ReminderBot
from bots.bluesky_bot import BlueskyBot

load_dotenv()

LOGGER = logging.getLogger("master_bot")

BOT_ENV = {
    "budget": ("BUDGET_INTERNAL_ID", "BUDGET_RECIPIENT"),
    "train": ("TRAIN_INTERNAL_ID", "TRAIN_RECIPIENT"),
    "bin": ("BIN_INTERNAL_ID", "BIN_RECIPIENT"),
    "testing": ("TESTING_INTERNAL_ID", "TESTING_RECIPIENT"),
    "nest": ("NEST_INTERNAL_ID", "NEST_RECIPIENT"),
    "reminder": ("REMINDER_INTERNAL_ID", "REMINDER_RECIPIENT"),
    "bluesky": ("BLUESKY_INTERNAL_ID", "BLUESKY_RECIPIENT"),
}


class ConfigError(RuntimeError):
    """Raised when required configuration is invalid or incomplete."""


def _truthy(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int, minimum: Optional[int] = None) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        value = default
    else:
        try:
            value = int(raw)
        except ValueError as exc:
            raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


@dataclass(frozen=True)
class BotRoute:
    kind: str
    internal_id: str
    recipient: str


@dataclass(frozen=True)
class AppConfig:
    signal_api_base: str
    signal_number: str
    bridge_db_path: str
    attachments_dir: Path
    max_attachment_bytes: int
    bluesky_notification_poll_interval: int
    bluesky_followed_only: bool
    bluesky_active_poll_minutes: int
    bluesky_refresh_follows_every: int
    bluesky_auto_sync_enabled: bool
    routes_by_internal_id: Mapping[str, BotRoute]
    route_by_kind: Mapping[str, BotRoute]

    @property
    def ws_url(self) -> str:
        return f"{self.signal_api_base}/v1/receive/{self.signal_number}"

    @property
    def send_url(self) -> str:
        return f"{self.signal_api_base}/v2/send"

    def route_for_kind(self, kind: str) -> Optional[BotRoute]:
        return self.route_by_kind.get(kind)

    def route_for_internal_id(self, internal_id: str) -> Optional[BotRoute]:
        return self.routes_by_internal_id.get(internal_id)


def load_config() -> AppConfig:
    signal_number = os.getenv("SIGNAL_NUMBER")
    if not signal_number:
        raise ConfigError("SIGNAL_NUMBER is required")

    signal_api_base = os.getenv("SIGNAL_API_BASE", "http://localhost:8080").rstrip("/")
    bridge_db_path = os.getenv("BLUESKY_BRIDGE_DB", "bridge.db")
    attachments_dir = Path(os.getenv("SIGNAL_ATTACHMENTS_DIR", "/tmp/signal_attachments"))
    max_attachment_bytes = _env_int("SIGNAL_MAX_ATTACHMENT_BYTES", 25 * 1024 * 1024, minimum=1)

    routes_by_internal_id: Dict[str, BotRoute] = {}
    route_by_kind: Dict[str, BotRoute] = {}
    incomplete_routes = []

    for kind, (internal_env, recipient_env) in BOT_ENV.items():
        internal_id = os.getenv(internal_env)
        recipient = os.getenv(recipient_env)
        if not internal_id and not recipient:
            LOGGER.warning("Route %s disabled because %s and %s are not set", kind, internal_env, recipient_env)
            continue
        if not internal_id or not recipient:
            incomplete_routes.append(f"{kind}: requires both {internal_env} and {recipient_env}")
            continue
        route = BotRoute(kind=kind, internal_id=internal_id, recipient=recipient)
        if internal_id in routes_by_internal_id:
            other = routes_by_internal_id[internal_id]
            raise ConfigError(f"Duplicate internal route id {internal_id!r} for {other.kind!r} and {kind!r}")
        routes_by_internal_id[internal_id] = route
        route_by_kind[kind] = route

    if incomplete_routes:
        raise ConfigError("Incomplete bot route configuration:\n- " + "\n- ".join(incomplete_routes))

    attachments_dir.mkdir(parents=True, exist_ok=True)

    return AppConfig(
        signal_api_base=signal_api_base,
        signal_number=signal_number,
        bridge_db_path=bridge_db_path,
        attachments_dir=attachments_dir,
        max_attachment_bytes=max_attachment_bytes,
        bluesky_notification_poll_interval=_env_int("BLUESKY_NOTIFICATION_POLL_INTERVAL", 60, minimum=5),
        bluesky_followed_only=_truthy(os.getenv("BLUESKY_FOLLOWED_ONLY"), default=True),
        bluesky_active_poll_minutes=_env_int("BLUESKY_ACTIVE_POLL_MINUTES", 30, minimum=1),
        bluesky_refresh_follows_every=_env_int("BLUESKY_REFRESH_FOLLOWS_EVERY", 20, minimum=1),
        bluesky_auto_sync_enabled=_truthy(os.getenv("BLUESKY_AUTO_SYNC_ENABLED"), default=False),
        routes_by_internal_id=routes_by_internal_id,
        route_by_kind=route_by_kind,
    )


CONFIG: Optional[AppConfig] = None


def require_config() -> AppConfig:
    if CONFIG is None:
        raise ConfigError("Application configuration has not been loaded")
    return CONFIG


class BridgeStore:
    ALLOWED_MAPPING_COLUMNS = {
        "signal_group_id",
        "signal_ts",
        "signal_sender",
        "bluesky_uri",
        "bluesky_cid",
        "bluesky_root_uri",
        "bluesky_root_cid",
        "bluesky_parent_uri",
        "bluesky_parent_cid",
        "bluesky_author_did",
        "direction",
        "text",
    }

    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, tuple(params))

    def _init_db(self) -> None:
        self._execute("PRAGMA journal_mode=WAL")
        self._execute("PRAGMA busy_timeout=30000")
        self._execute("PRAGMA foreign_keys=ON")
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS message_map (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_group_id TEXT,
                signal_ts TEXT,
                signal_sender TEXT,
                bluesky_uri TEXT UNIQUE,
                bluesky_cid TEXT,
                bluesky_root_uri TEXT,
                bluesky_root_cid TEXT,
                bluesky_parent_uri TEXT,
                bluesky_parent_cid TEXT,
                bluesky_author_did TEXT,
                direction TEXT,
                text TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self._execute(
            """
            CREATE INDEX IF NOT EXISTS idx_message_map_signal
            ON message_map(signal_group_id, signal_ts)
            """
        )
        self._execute(
            """
            CREATE INDEX IF NOT EXISTS idx_message_map_bsky
            ON message_map(bluesky_uri)
            """
        )
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS processed_notifications (
                uri TEXT PRIMARY KEY,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS processed_signal_messages (
                dedupe_key TEXT PRIMARY KEY,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS bluesky_watch_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                active_until_ts INTEGER,
                last_manual_check_ts INTEGER
            )
            """
        )
        self._execute(
            """
            INSERT OR IGNORE INTO bluesky_watch_state (id, active_until_ts, last_manual_check_ts)
            VALUES (1, NULL, NULL)
            """
        )
        self.conn.commit()

    def save_mapping(self, **kwargs: Any) -> None:
        unknown = set(kwargs) - self.ALLOWED_MAPPING_COLUMNS
        if unknown:
            raise ValueError(f"Unexpected message_map columns: {sorted(unknown)}")
        if not kwargs:
            raise ValueError("save_mapping called without columns")
        cols = list(kwargs.keys())
        quoted_cols = ", ".join(cols)
        placeholders = ", ".join("?" for _ in cols)
        values = [kwargs[col] for col in cols]
        with self.conn:
            self._execute(f"INSERT OR REPLACE INTO message_map ({quoted_cols}) VALUES ({placeholders})", values)

    def by_signal_ts(self, group_id: str, signal_ts: str) -> Optional[sqlite3.Row]:
        cur = self._execute(
            "SELECT * FROM message_map WHERE signal_group_id = ? AND signal_ts = ? ORDER BY id DESC LIMIT 1",
            (group_id, str(signal_ts)),
        )
        return cur.fetchone()

    def by_bluesky_uri(self, uri: str) -> Optional[sqlite3.Row]:
        cur = self._execute("SELECT * FROM message_map WHERE bluesky_uri = ? LIMIT 1", (uri,))
        return cur.fetchone()

    def notification_seen(self, uri: str) -> bool:
        cur = self._execute("SELECT 1 FROM processed_notifications WHERE uri = ? LIMIT 1", (uri,))
        return cur.fetchone() is not None

    def mark_notification_seen(self, uri: str) -> None:
        with self.conn:
            self._execute("INSERT OR IGNORE INTO processed_notifications (uri) VALUES (?)", (uri,))

    def signal_message_seen(self, dedupe_key: str) -> bool:
        cur = self._execute("SELECT 1 FROM processed_signal_messages WHERE dedupe_key = ? LIMIT 1", (dedupe_key,))
        return cur.fetchone() is not None

    def mark_signal_message_seen(self, dedupe_key: str) -> None:
        with self.conn:
            self._execute("INSERT OR IGNORE INTO processed_signal_messages (dedupe_key) VALUES (?)", (dedupe_key,))

    def activate_watch_window(self, minutes: int) -> int:
        now = int(asyncio.get_running_loop().time())
        active_until = now + max(1, minutes) * 60
        with self.conn:
            self._execute("UPDATE bluesky_watch_state SET active_until_ts = ? WHERE id = 1", (active_until,))
        return active_until

    def deactivate_watch_window(self) -> None:
        with self.conn:
            self._execute("UPDATE bluesky_watch_state SET active_until_ts = NULL WHERE id = 1")

    def is_watch_active(self) -> bool:
        cur = self._execute("SELECT active_until_ts FROM bluesky_watch_state WHERE id = 1")
        row = cur.fetchone()
        if not row or row["active_until_ts"] is None:
            return False
        now = int(asyncio.get_running_loop().time())
        return row["active_until_ts"] > now

    def watch_status(self) -> Dict[str, Any]:
        cur = self._execute("SELECT active_until_ts, last_manual_check_ts FROM bluesky_watch_state WHERE id = 1")
        row = cur.fetchone()
        now = int(asyncio.get_running_loop().time())
        active_until = row["active_until_ts"] if row else None
        remaining = max(0, active_until - now) if active_until else 0
        return {
            "active": bool(active_until and active_until > now),
            "active_until_ts": active_until,
            "remaining_seconds": remaining,
            "last_manual_check_ts": row["last_manual_check_ts"] if row else None,
        }

    def mark_manual_check(self) -> None:
        now = int(asyncio.get_running_loop().time())
        with self.conn:
            self._execute("UPDATE bluesky_watch_state SET last_manual_check_ts = ? WHERE id = 1", (now,))


def _safe_filename(filename: Optional[str], fallback: str) -> str:
    name = Path(filename or fallback).name
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return name or fallback


def _guess_mime_type(filepath: str) -> str:
    guessed, _ = mimetypes.guess_type(filepath)
    return guessed or "application/octet-stream"


async def download_signal_attachment(
    session: aiohttp.ClientSession,
    attachment_id: str,
    filename: Optional[str] = None,
    config: Optional[AppConfig] = None,
) -> Optional[str]:
    config = config or require_config()
    if not attachment_id:
        return None

    url = f"{config.signal_api_base}/v1/attachments/{attachment_id}"
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                body = await resp.text()
                LOGGER.error("Failed to download attachment %s: HTTP %s %s", attachment_id, resp.status, body[:300])
                return None
            data = await resp.read()
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        LOGGER.error("Failed to download attachment %s: %s", attachment_id, exc)
        return None

    if len(data) > config.max_attachment_bytes:
        LOGGER.error(
            "Rejecting attachment %s because it is %s bytes; limit is %s bytes",
            attachment_id,
            len(data),
            config.max_attachment_bytes,
        )
        return None

    safe_name = _safe_filename(filename, attachment_id)
    unique_name = f"{uuid.uuid4().hex}_{safe_name}"
    out_path = config.attachments_dir / unique_name
    out_path.write_bytes(data)
    LOGGER.info("Saved attachment %s to %s", attachment_id, out_path)
    return str(out_path)


async def send_signal(
    session: aiohttp.ClientSession,
    message: str,
    external_id: Optional[str],
    filepath: Optional[str] = None,
    quote_timestamp: Optional[Any] = None,
    quote_author: Optional[Any] = None,
    config: Optional[AppConfig] = None,
) -> Optional[str]:
    """Centralized Signal send, with optional quote/reply and attachment support."""
    config = config or require_config()
    if not external_id:
        LOGGER.error("Cannot send Signal message because recipient is missing. Message was: %r", message)
        return None

    payload: Dict[str, Any] = {
        "message": message or "",
        "number": config.signal_number,
        "recipients": [external_id],
        "text_mode": "styled",
        "base64_attachments": [],
    }

    if quote_timestamp is not None:
        try:
            payload["quote_timestamp"] = int(quote_timestamp)
            if quote_author:
                payload["quote_author"] = str(quote_author)
        except (TypeError, ValueError):
            LOGGER.warning("Invalid quote_timestamp %r; sending without quote", quote_timestamp)

    if filepath:
        try:
            path = Path(filepath)
            if not path.is_file():
                LOGGER.error("Attachment path does not exist or is not a file: %s", filepath)
            elif path.stat().st_size > config.max_attachment_bytes:
                LOGGER.error("Attachment %s exceeds limit of %s bytes", filepath, config.max_attachment_bytes)
            else:
                b64 = base64.b64encode(path.read_bytes()).decode("utf-8")
                mime_type = _guess_mime_type(str(path))
                filename = _safe_filename(path.name, "attachment")
                payload["base64_attachments"].append(f"data:{mime_type};filename={filename};base64,{b64}")
        except OSError as exc:
            LOGGER.error("Could not attach file %s: %s", filepath, exc)

    try:
        async with session.post(config.send_url, json=payload) as resp:
            body = await resp.text()
            if resp.status not in {200, 201}:
                LOGGER.error("Signal send failed: HTTP %s %s", resp.status, body[:500])
                return None
            try:
                result = json.loads(body) if body else None
            except json.JSONDecodeError:
                result = None
            return _extract_sent_timestamp(result)
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        LOGGER.error("Signal send error: %s", exc)
        return None


def _extract_sent_timestamp(result: Any) -> Optional[str]:
    if not isinstance(result, dict):
        return None
    if isinstance(result.get("timestamp"), (int, str)):
        return str(result["timestamp"])
    results = result.get("results")
    if isinstance(results, list) and results:
        first = results[0]
        if isinstance(first, dict) and first.get("timestamp") is not None:
            return str(first["timestamp"])
    return None


def _extract_signal_quote_id(target_msg: Mapping[str, Any]) -> Optional[str]:
    quote = target_msg.get("quote") or {}
    if not isinstance(quote, Mapping):
        return None
    quote_id = quote.get("id") or quote.get("timestamp")
    return str(quote_id) if quote_id is not None else None


def _format_watch_status(store: BridgeStore) -> str:
    status = store.watch_status()
    if status["active"]:
        mins = max(1, int((status["remaining_seconds"] + 59) // 60))
        return f"🦋 Notification watch is on for about {mins} more minute(s)."
    return "🦋 Notification watch is off."


async def _poll_and_mirror_bluesky_replies(
    session: aiohttp.ClientSession,
    bluesky_bot: BlueskyBot,
    store: BridgeStore,
    force: bool = False,
    config: Optional[AppConfig] = None,
) -> Dict[str, int]:
    config = config or require_config()
    followed_refresh_counter = getattr(_poll_and_mirror_bluesky_replies, "_followed_refresh_counter", 0)

    refresh_follows = False
    if config.bluesky_followed_only:
        followed_refresh_counter += 1
        if followed_refresh_counter >= config.bluesky_refresh_follows_every or force:
            refresh_follows = True
            followed_refresh_counter = 0
    _poll_and_mirror_bluesky_replies._followed_refresh_counter = followed_refresh_counter

    result = bluesky_bot.list_reply_notifications(
        limit=50,
        followed_only=config.bluesky_followed_only,
        refresh_follows=refresh_follows,
    )

    notifications = result.get("notifications", []) if isinstance(result, dict) else []
    my_did = getattr(getattr(bluesky_bot.client, "me", None), "did", None)
    mirrored = 0
    skipped = 0
    bluesky_route = config.route_for_kind("bluesky")

    for notif in notifications:
        parsed = bluesky_bot.extract_reply_notification(notif)
        if not parsed:
            skipped += 1
            continue

        uri = parsed.get("uri")
        if not uri or store.notification_seen(uri):
            skipped += 1
            continue

        if my_did and parsed.get("author_did") == my_did:
            store.mark_notification_seen(uri)
            skipped += 1
            continue

        reason_subject = parsed.get("reason_subject")
        if not reason_subject:
            store.mark_notification_seen(uri)
            skipped += 1
            continue

        mapped_parent = store.by_bluesky_uri(reason_subject)
        if not mapped_parent:
            store.mark_notification_seen(uri)
            skipped += 1
            continue

        author = parsed.get("author_handle") or parsed.get("author_did") or "unknown"
        text = (parsed.get("text") or "").strip() or "[no text]"
        signal_text = f"🦋 @{author} replied:\n{text}"

        quoted_signal_ts = mapped_parent["signal_ts"]
        quoted_signal_author = mapped_parent["signal_sender"]

        if quoted_signal_ts and str(quoted_signal_ts).isdigit():
            sent_ts = await send_signal(
                session,
                signal_text,
                bluesky_route.recipient if bluesky_route else None,
                quote_timestamp=quoted_signal_ts,
                quote_author=quoted_signal_author,
                config=config,
            )
        else:
            LOGGER.warning("Parent mapping found but no numeric Signal timestamp to quote: %r", quoted_signal_ts)
            sent_ts = await send_signal(
                session,
                signal_text,
                bluesky_route.recipient if bluesky_route else None,
                config=config,
            )

        store.save_mapping(
            signal_group_id=bluesky_route.internal_id if bluesky_route else None,
            signal_ts=str(sent_ts or uri),
            signal_sender=author,
            bluesky_uri=parsed.get("uri"),
            bluesky_cid=parsed.get("cid"),
            bluesky_root_uri=parsed.get("root_uri") or mapped_parent["bluesky_root_uri"] or mapped_parent["bluesky_uri"],
            bluesky_root_cid=parsed.get("root_cid") or mapped_parent["bluesky_root_cid"] or mapped_parent["bluesky_cid"],
            bluesky_parent_uri=parsed.get("parent_uri"),
            bluesky_parent_cid=parsed.get("parent_cid"),
            bluesky_author_did=parsed.get("author_did"),
            direction="bluesky_to_signal",
            text=text,
        )
        store.mark_notification_seen(uri)
        mirrored += 1

    return {"mirrored": mirrored, "skipped": skipped}


async def _handle_bluesky_notif_command(
    session: aiohttp.ClientSession,
    bluesky_bot: BlueskyBot,
    store: BridgeStore,
    incoming_text: str,
    route: BotRoute,
    config: Optional[AppConfig] = None,
) -> None:
    config = config or require_config()
    parts = incoming_text.strip().split()
    subcommand = parts[1].lower() if len(parts) > 1 else ""

    if subcommand in {"", "check", "now"}:
        store.mark_manual_check()
        stats = await _poll_and_mirror_bluesky_replies(session, bluesky_bot, store, force=True, config=config)
        if stats["mirrored"]:
            reply = f"🦋 Checked notifications and mirrored {stats['mirrored']} repl{'y' if stats['mirrored'] == 1 else 'ies'}."
        else:
            reply = "🦋 Checked notifications. Nothing new to mirror."
        await send_signal(session, reply, route.recipient, config=config)
        return

    if subcommand == "status":
        await send_signal(session, _format_watch_status(store), route.recipient, config=config)
        return

    if subcommand == "off":
        store.deactivate_watch_window()
        await send_signal(session, "🦋 Notification watch turned off.", route.recipient, config=config)
        return

    if subcommand == "on":
        minutes = config.bluesky_active_poll_minutes
        if len(parts) > 2:
            try:
                minutes = max(1, int(parts[2]))
            except ValueError:
                pass
        store.activate_watch_window(minutes)
        await send_signal(session, f"🦋 Notification watch turned on for {minutes} minute(s).", route.recipient, config=config)
        return

    help_text = (
        "🦋 /notif commands:\n"
        "/notif - check now\n"
        "/notif status - show watcher status\n"
        "/notif on [minutes] - enable temporary polling\n"
        "/notif off - disable temporary polling"
    )
    await send_signal(session, help_text, route.recipient, config=config)


async def _handle_bluesky_message(
    session: aiohttp.ClientSession,
    bluesky_bot: BlueskyBot,
    store: BridgeStore,
    envelope: Mapping[str, Any],
    target_msg: Mapping[str, Any],
    route: BotRoute,
    config: Optional[AppConfig] = None,
) -> None:
    config = config or require_config()
    incoming_text = target_msg.get("message") or ""

    if incoming_text.strip().lower().startswith("/notif"):
        await _handle_bluesky_notif_command(session, bluesky_bot, store, incoming_text, route, config=config)
        return

    attachments = target_msg.get("attachments", [])
    attachment_id = None
    attachment_filename = None
    if isinstance(attachments, list) and attachments:
        attachment = attachments[0]
        if isinstance(attachment, Mapping):
            attachment_id = attachment.get("id")
            attachment_filename = attachment.get("filename")

    LOGGER.info(
        "Bluesky Signal message text=%r attachment_id=%r filename=%r",
        incoming_text,
        attachment_id,
        attachment_filename,
    )

    if not incoming_text and not attachment_id:
        return

    image_path = None
    if attachment_id:
        image_path = await download_signal_attachment(session, attachment_id, attachment_filename, config=config)

    clean_text = incoming_text.replace("/bs", "", 1).strip() if incoming_text else ""

    quoted_signal_ts = _extract_signal_quote_id(target_msg)
    reply_ref = None
    if quoted_signal_ts:
        mapped = store.by_signal_ts(route.internal_id, quoted_signal_ts)
        if mapped:
            reply_ref = {
                "root_uri": mapped["bluesky_root_uri"] or mapped["bluesky_uri"],
                "root_cid": mapped["bluesky_root_cid"] or mapped["bluesky_cid"],
                "parent_uri": mapped["bluesky_uri"],
                "parent_cid": mapped["bluesky_cid"],
                "parent_author_did": mapped["bluesky_author_did"],
            }
            LOGGER.info("Resolved Signal reply %s to Bluesky URI %s", quoted_signal_ts, mapped["bluesky_uri"])
        else:
            LOGGER.info("Signal reply had quote id %s but no Bluesky mapping found", quoted_signal_ts)

    signal_ts = str(envelope.get("timestamp") or target_msg.get("timestamp") or "")
    dedupe_key = f"{route.internal_id}:{signal_ts}:{clean_text}:{attachment_id or ''}"
    if dedupe_key and store.signal_message_seen(dedupe_key):
        LOGGER.info("Skipping already processed Bluesky Signal message %s", dedupe_key)
        return
    if dedupe_key:
        store.mark_signal_message_seen(dedupe_key)

    status = await bluesky_bot.handle_command(text=clean_text, image_path=image_path, reply_ref=reply_ref)

    if isinstance(status, str):
        LOGGER.info("BlueskyBot returned legacy status string: %s", status)
        return

    if not status or not status.get("ok"):
        LOGGER.error("Bluesky post failed: %s", status)
        await send_signal(
            session,
            f"❌ Failed to post to Bluesky: {(status or {}).get('message', 'unknown error')}",
            route.recipient,
            config=config,
        )
        return

    sender = envelope.get("source")
    for post in status.get("posts", []):
        if not isinstance(post, Mapping):
            continue
        store.save_mapping(
            signal_group_id=route.internal_id,
            signal_ts=signal_ts,
            signal_sender=sender,
            bluesky_uri=post.get("uri"),
            bluesky_cid=post.get("cid"),
            bluesky_root_uri=post.get("root_uri"),
            bluesky_root_cid=post.get("root_cid"),
            bluesky_parent_uri=post.get("parent_uri"),
            bluesky_parent_cid=post.get("parent_cid"),
            bluesky_author_did=getattr(getattr(bluesky_bot.client, "me", None), "did", None),
            direction="signal_to_bluesky",
            text=post.get("text"),
        )

    store.activate_watch_window(config.bluesky_active_poll_minutes)
    await send_signal(
        session,
        f"🦋 Posted to Bluesky. Notification watch active for {config.bluesky_active_poll_minutes} minute(s).",
        route.recipient,
        config=config,
    )


async def sync_bluesky_replies(bluesky_bot: BlueskyBot, store: BridgeStore, config: Optional[AppConfig] = None) -> None:
    config = config or require_config()
    """Poll Bluesky notifications only while the temporary watch window is active."""
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            try:
                if store.is_watch_active():
                    await _poll_and_mirror_bluesky_replies(session, bluesky_bot, store, config=config)
            except Exception:
                LOGGER.exception("Bluesky notification sync error")
            await asyncio.sleep(config.bluesky_notification_poll_interval)


def _parse_signal_items(raw_data: str) -> list[Mapping[str, Any]]:
    try:
        payload = json.loads(raw_data)
    except json.JSONDecodeError:
        LOGGER.warning("Ignoring non-JSON WebSocket message: %r", raw_data[:500])
        return []

    data_list = payload if isinstance(payload, list) else [payload]
    items: list[Mapping[str, Any]] = []
    for item in data_list:
        if isinstance(item, Mapping):
            items.append(item)
        else:
            LOGGER.warning("Ignoring unexpected Signal payload item type: %s", type(item).__name__)
    return items


def _extract_target_message(item: Mapping[str, Any]) -> tuple[Mapping[str, Any], Optional[Mapping[str, Any]]]:
    envelope = item.get("envelope", {})
    if not isinstance(envelope, Mapping):
        return {}, None
    data_msg = envelope.get("dataMessage")
    sync_msg = envelope.get("syncMessage", {})
    if isinstance(sync_msg, Mapping):
        sync_msg = sync_msg.get("sentMessage")
    target_msg = data_msg or sync_msg
    if not isinstance(target_msg, Mapping):
        return envelope, None
    return envelope, target_msg


def _internal_id_for(envelope: Mapping[str, Any], target_msg: Mapping[str, Any]) -> Optional[str]:
    group_info = target_msg.get("groupInfo", {})
    if isinstance(group_info, Mapping) and group_info.get("groupId"):
        return str(group_info["groupId"])
    source = envelope.get("source")
    return str(source) if source else None


async def _dispatch_standard_bot(
    session: aiohttp.ClientSession,
    route: BotRoute,
    text: str,
    bots: Mapping[str, Any],
    config: AppConfig,
) -> None:
    bot = bots.get(route.kind)
    if bot is None:
        LOGGER.info("No command handler configured for route kind %s", route.kind)
        return

    reply = await bot.handle_command(text)
    if not reply:
        return

    if route.kind == "nest" and isinstance(reply, tuple) and len(reply) >= 3 and reply[0] == "FILE":
        await send_signal(session, reply[1], route.recipient, reply[2], config=config)
    else:
        await send_signal(session, reply, route.recipient, config=config)


async def _handle_signal_item(
    session: aiohttp.ClientSession,
    item: Mapping[str, Any],
    bots: Mapping[str, Any],
    store: BridgeStore,
    config: AppConfig,
) -> None:
    envelope, target_msg = _extract_target_message(item)
    if not target_msg:
        return

    internal_id = _internal_id_for(envelope, target_msg)
    if not internal_id:
        LOGGER.debug("Ignoring Signal message without source/group id")
        return

    route = config.route_for_internal_id(internal_id)
    if not route:
        LOGGER.info("Ignored message from unknown source: %s", internal_id)
        return

    incoming_text = target_msg.get("message") or ""
    LOGGER.info("Incoming message received from %s route=%s", internal_id, route.kind)

    if route.kind == "bluesky":
        await _handle_bluesky_message(session, bots["bluesky"], store, envelope, target_msg, route, config=config)
        return

    if not incoming_text or not incoming_text.startswith("/"):
        return

    await _dispatch_standard_bot(session, route, incoming_text, bots, config)


async def master_listener(bots: Mapping[str, Any], store: BridgeStore, config: Optional[AppConfig] = None) -> None:
    config = config or require_config()
    """Stream Signal messages over WebSocket and dispatch them to the right bot."""
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=None)
    backoff_seconds = 2

    async with aiohttp.ClientSession(timeout=timeout) as session:
        LOGGER.info("Master listener online. Opening WebSocket connection to %s", config.ws_url)
        while True:
            try:
                async with session.ws_connect(config.ws_url, heartbeat=30) as ws:
                    LOGGER.info("WebSocket connected successfully")
                    backoff_seconds = 2

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            for item in _parse_signal_items(msg.data):
                                try:
                                    await _handle_signal_item(session, item, bots, store, config)
                                except Exception:
                                    LOGGER.exception("Error while processing one Signal payload item")
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            LOGGER.error("WebSocket error: %s", ws.exception())
                            break
                        elif msg.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE}:
                            LOGGER.warning("WebSocket closed by server")
                            break
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                LOGGER.error("WebSocket connection dropped/error: %s", exc)
            except Exception:
                LOGGER.exception("Unexpected WebSocket listener error")

            LOGGER.info("Reconnecting WebSocket after %s second(s)", backoff_seconds)
            await asyncio.sleep(backoff_seconds)
            backoff_seconds = min(backoff_seconds * 2, 60)


async def _supervise(name: str, task_factory: Callable[[], Awaitable[None]]) -> None:
    """Run a long-lived task and restart it after unexpected crashes."""
    backoff_seconds = 2
    while True:
        try:
            LOGGER.info("Starting task: %s", name)
            await task_factory()
            LOGGER.warning("Task %s exited; restarting after %s second(s)", name, backoff_seconds)
        except asyncio.CancelledError:
            LOGGER.info("Task %s cancelled", name)
            raise
        except Exception:
            LOGGER.exception("Task %s crashed; restarting after %s second(s)", name, backoff_seconds)
        await asyncio.sleep(backoff_seconds)
        backoff_seconds = min(backoff_seconds * 2, 60)


async def main(config: Optional[AppConfig] = None) -> None:
    global CONFIG
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if config is None:
        config = load_config()
    CONFIG = config

    budget_bot = BudgetBot()
    train_bot = TrainBot()
    bin_bot = BinBot()
    nest_bot = NestBot()
    reminder_bot = ReminderBot()
    bluesky_bot = BlueskyBot()
    store = BridgeStore(config.bridge_db_path)

    bots: Dict[str, Any] = {
        "budget": budget_bot,
        "train": train_bot,
        "bin": bin_bot,
        "nest": nest_bot,
        "reminder": reminder_bot,
        "bluesky": bluesky_bot,
    }

    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=60)
    async with aiohttp.ClientSession(timeout=timeout) as alert_session:
        async def send_to_kind(kind: str, message: str, filepath: Optional[str] = None) -> None:
            route = config.route_for_kind(kind)
            if not route:
                LOGGER.error("Cannot send %s alert because its route is not configured", kind)
                return
            await send_signal(alert_session, message, route.recipient, filepath, config=config)

        background_tasks = [
            _supervise("master_listener", lambda: master_listener(bots, store, config)),
            _supervise("nest_sync", lambda: nest_bot.sync_task(lambda message, filepath=None: send_to_kind("nest", message, filepath))),
            _supervise("budget_weekly", lambda: budget_bot.weekly_task(lambda message: send_to_kind("budget", message))),
            _supervise("train_monitor", lambda: train_bot.monitor_subscriptions(lambda message: send_to_kind("train", message))),
            _supervise("bin_scheduler", lambda: bin_bot.bin_scheduler(lambda message: send_to_kind("bin", message))),
            _supervise("reminder_checker", lambda: reminder_bot.check_reminders(lambda message: send_to_kind("reminder", message))),
        ]

        if config.bluesky_auto_sync_enabled:
            background_tasks.append(
                _supervise("sync_bluesky_replies", lambda: sync_bluesky_replies(bluesky_bot, store, config))
            )
            LOGGER.info("Bluesky background notification sync enabled")
        else:
            LOGGER.info("Bluesky background notification sync disabled; use /notif check or enable BLUESKY_AUTO_SYNC_ENABLED")

        await asyncio.gather(*background_tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except ConfigError as exc:
        logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(name)s: %(message)s")
        LOGGER.error("Configuration error: %s", exc)
        raise SystemExit(2)
    except KeyboardInterrupt:
        LOGGER.info("Shutdown requested")
