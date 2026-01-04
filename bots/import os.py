import os
import asyncio
import datetime as dt
import json
import dateparser
from utils.tools import logger

class ReminderBot:
    def __init__(self):
        self.reminders_file = os.path.join("data", "reminders.json")
        self.reminders = self.load_reminders()

    def load_reminders(self):
        if os.path.exists(self.reminders_file):
            with open(self.reminders_file, "r") as f:
                return json.load(f)
        return []

    def save_reminders(self):
        with open(self.reminders_file, "w") as f:
            json.dump(self.reminders, f)

    async def handle_command(self, text):
        """
        Syntax: /remind [time phrase] | [task]
        Example: /remind in 20 mins | take the bins out
        Example: /remind tomorrow 9am | call Dave
        """
        if not text.startswith("/remind"):
            return None

        try:
            # Split by the pipe character for clean parsing
            parts = text.replace("/remind", "").strip().split("|")
            if len(parts) < 2:
                return "💡 Format: `/remind time | message` (e.g. `/remind in 1h | tea`)"

            time_phrase = parts[0].strip()
            task_content = parts[1].strip()

            # The Magic: parse the messy phone text into a real datetime
            target_time = dateparser.parse(
                time_phrase, 
                settings={'PREFER_DATES_FROM': 'future', 'RETURN_AS_TIMEZONE_AWARE': False}
            )

            if not target_time:
                return f"❓ Couldn't figure out when '{time_phrase}' is."

            # Add to list
            new_reminder = {
                "time": target_time.isoformat(),
                "task": task_content,
                "done": False
            }
            self.reminders.append(new_reminder)
            self.save_reminders()

            return f"✅ OK! I'll remind you at {target_time.strftime('%H:%M (%d %b)')}: {task_content}"

        except Exception as e:
            logger.error(f"Reminder Error: {e}")
            return "❌ Something went wrong setting that reminder."

    async def check_reminders(self, alert_callback):
        """Background task to check for due reminders."""
        while True:
            now = dt.datetime.now()
            pending = []
            
            for r in self.reminders:
                remind_time = dt.datetime.fromisoformat(r["time"])
                if remind_time <= now and not r.get("done"):
                    await alert_callback(f"🔔 REMINDER: {r['task']}")
                    r["done"] = True
                else:
                    pending.append(r)
            
            # Clean up finished reminders and save
            if len(pending) != len(self.reminders):
                self.reminders = pending
                self.save_reminders()

            await asyncio.sleep(60) # Check every minute