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
            try:
                with open(self.reminders_file, "r") as f:
                    return json.load(f)
            except: return []
        return []

    def save_reminders(self):
        with open(self.reminders_file, "w") as f:
            json.dump(self.reminders, f, indent=4)

    async def handle_command(self, text):
        parts = text.split()
        if not parts: return None
        cmd = parts[0].lower()

        # 1. LIST COMMAND
        if cmd == "/list":
            if not self.reminders:
                return "📭 No pending reminders."
            
            # Sort by time so the soonest is at the top
            sorted_r = sorted(self.reminders, key=lambda x: x['time'])
            msg = "🗓 **Pending Reminders:**\n"
            for i, r in enumerate(sorted_r):
                t = dt.datetime.fromisoformat(r['time']).strftime('%d %b, %H:%M')
                msg += f"{i+1}. **{t}**: {r['task']}\n"
            return msg

        # 2. DELETE COMMAND (Crucial for mobile typos)
        if cmd == "/del" and len(parts) > 1:
            try:
                idx = int(parts[1]) - 1
                sorted_r = sorted(self.reminders, key=lambda x: x['time'])
                removed = sorted_r.pop(idx)
                # Sync back to main list
                self.reminders = sorted_r
                self.save_reminders()
                return f"✅ Deleted: {removed['task']}"
            except:
                return "❌ Use `/del [number]` from the `/list`."

        # 3. REMIND COMMAND
        if cmd == "/remind":
            raw_content = text.replace("/remind", "").strip()
            if "|" not in raw_content:
                return "💡 Format: `/remind time | message`"

            time_phrase, task = raw_content.split("|", 1)
            target_time = dateparser.parse(
                time_phrase.strip(), 
                settings={'PREFER_DATES_FROM': 'future', 'PREFER_DAY_OF_MONTH': 'first'}
            )

            if not target_time:
                return f"❓ Unsure when '{time_phrase.strip()}' is."

            self.reminders.append({
                "time": target_time.isoformat(),
                "task": task.strip()
            })
            self.save_reminders()
            return f"✅ Set for {target_time.strftime('%H:%M (%a)')}: {task.strip()}"

        if cmd == '/usage' or cmd == '/help':
            return ("⏳ **Reminder Bot**\n"
            "• `/remind [time] | [message]`: Creates a new reminder.\n"
            "• `/list`: Shows all pending reminders with an ID number.\n"
            "• `/del [ID]`: Deletes a specific reminder by its number.\n"
            "• `/usage`: Shows this quick-start guide.")

    async def check_reminders(self, alert_callback):
        """The Polling Loop: Computatially cheap and survives restarts."""
        while True:
            try:
                now = dt.datetime.now()
                due = []
                remaining = []

                for r in self.reminders:
                    if dt.datetime.fromisoformat(r["time"]) <= now:
                        due.append(r)
                    else:
                        remaining.append(r)

                for r in due:
                    await alert_callback(f"🔔 **REMINDER**: {r['task']}")
                
                if due:
                    self.reminders = remaining
                    self.save_reminders()

            except Exception as e:
                logger.error(f"Reminder Loop Error: {e}")
            
            # Sleeping for 60 seconds uses near-zero CPU
            await asyncio.sleep(60)