import os
import datetime as dt
import asyncio
from utils.tools import logger
from utils.google_auth_wrapper import GoogleConnection
from dotenv import load_dotenv
import json 
import time

load_dotenv()

class NestBot:
    def __init__(self):
        self.sync_interval = 30  # Default minutes
        self.messaging_enabled = True
        self.state_file = "nest_state.json"
        
        # Initialize Google Connection
        self.username = os.getenv("GOOGLE_USERNAME")
        self.token = os.getenv("GOOGLE_MASTER_TOKEN")
        self.monitored = ["Backyard", "Nest Doorbell (battery)"]
        
        self.conn = GoogleConnection(self.token, self.username)
        self.devices = self.conn.get_nest_camera_devices()
        self.state = self.load_state()
        self.download_path = os.getenv("DOWNLOAD_PATH", "./downloads")
        self.max_folder_gb = 10  # Max storage limit
        self.max_age_days = 30   # Delete older than a mont

    def load_state(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r") as f:
                    content = f.read().strip()
                    if not content:  # Handle empty file
                        return {}
                    return json.loads(content)
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"Error loading state file: {e}. Resetting state.")
                return {}
        return {}

    def save_state(self):
        with open(self.state_file, "w") as f:
            json.dump(self.state, f)

    def cleanup_storage(self):
        """Deletes files older than N days or if folder exceeds GB limit"""
        files = [os.path.join(self.download_path, f) for f in os.listdir(self.download_path)]
        files.sort(key=os.path.getmtime) # Oldest first

        # Delete by age
        now = time.time()
        for f in files[:]:
            if os.path.getmtime(f) < now - (self.max_age_days * 86400):
                os.remove(f)
                files.remove(f)

        # Delete by size
        total_size = sum(os.path.getsize(f) for f in files)
        while total_size > (self.max_folder_gb * 1024**3) and files:
            oldest = files.pop(0)
            total_size -= os.path.getsize(oldest)
            os.remove(oldest)

    async def handle_command(self, text):
        parts = text.split()
        cmd = parts[0].lower()

        if cmd == "/sync" and len(parts) > 1:
            try:
                self.sync_interval = int(parts[1])
                return f"⏳ Nest Sync interval updated to **{self.sync_interval} minutes**."
            except ValueError:
                return "❌ Please provide a valid number of minutes."

        elif cmd == "/message":
            if len(parts) > 1:
                sub = parts[1].lower()
                if sub == "on":
                    self.messaging_enabled = True
                    return "🔔 Nest Signal alerts: **ON**."
                if sub == "off":
                    self.messaging_enabled = False
                    return "🔕 Nest Signal alerts: **OFF** (Background backup active)."
            return f"Nest alerts are currently {'ON' if self.messaging_enabled else 'OFF'}."
        elif cmd in ["/usage", "/help"]:
            return (
                "🚆 **Nest Bot**\n"
                "• `/sync [minutes]` - Set camera download interval\n"
                "• `/message [on/off]` - receive video alerts\n"
            )

    async def sync_task(self, alert_callback):
        """Modified sync loop to use the dynamic interval and callback."""
        while True:
            try:
                now = dt.datetime.now(dt.timezone.utc)
                for device in self.devices:
                    d_id = getattr(device, 'device_id', device.device_name)
                    last_ts_str = self.state.get(d_id)
                    if last_ts_str:
                        last_ts = dt.datetime.fromisoformat(last_ts_str)
                        # Calculate minutes since last successful sync
                        delta = int((now - last_ts).total_seconds() / 60) + 2 
                    else:
                        delta = 180 # Default fallback

                    logger.info(f"Syncing {device.device_name} (Lookback: {delta}m)")
                    events = device.get_events(end_time=now, duration_minutes=delta)
                    
                    latest_event_time = last_ts if last_ts_str else None

                    for event in (events or []):
                        # Avoid duplicates
                        if latest_event_time and event.start_time <= latest_event_time:
                            continue

                        filename = f"{device.device_name}_{event.start_time.strftime('%Y%m%d_%H%M%S')}.mp4"
                        filepath = os.path.join(self.download_path, filename)

                        if not os.path.exists(filepath):
                            video_bytes = device.download_camera_event(event)
                            if video_bytes:
                                with open(filepath, "wb") as f:
                                    f.write(video_bytes)
                                
                                # 2. Filter Alerts by Camera Name
                                if device.device_name in self.monitored:
                                    await alert_callback(f"Alert: {device.device_name} - {event.start_time.strftime('%d-%m-%Y_%H:%M:%S')}", filepath)

                        if not latest_event_time or event.start_time > latest_event_time:
                            latest_event_time = event.start_time

                    # 3. Update State
                    if latest_event_time:
                        self.state[d_id] = latest_event_time.isoformat()
                        self.save_state()
                
                logger.info(f"Nest Syncing {len(self.devices)} cameras...")
            except Exception as e:
                logger.error(f"Nest Sync Error: {e}")
            
            await asyncio.sleep(self.sync_interval * 60)