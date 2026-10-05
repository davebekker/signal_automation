import json
import asyncio
import aiohttp
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()


class BinBot:
    def __init__(self):
        self.base_url = os.getenv("WASTE_URL")
        self.data_url = f"{self.base_url}?page_loading=1" if self.base_url else None
        self.cache_file = os.getenv("BIN_CACHE_FILE", "bins.json")
        self.state_file = os.getenv("BIN_STATE_FILE", "bins_state.json")

        # Keep the scheduler low-wake: it sleeps until the next meaningful milestone.
        self.night_before_hour = int(os.getenv("BIN_NIGHT_BEFORE_HOUR", "18"))
        self.night_before_minute = int(os.getenv("BIN_NIGHT_BEFORE_MINUTE", "0"))
        self.morning_hour = int(os.getenv("BIN_MORNING_HOUR", "7"))
        self.morning_minute = int(os.getenv("BIN_MORNING_MINUTE", "0"))
        self.refresh_hour = int(os.getenv("BIN_REFRESH_HOUR", "9"))
        self.refresh_minute = int(os.getenv("BIN_REFRESH_MINUTE", "0"))

        # If the master bot was down/restarted after the scheduled time, these windows
        # allow the missed reminder to be sent once, instead of being skipped forever.
        self.night_before_grace_hours = float(os.getenv("BIN_NIGHT_BEFORE_GRACE_HOURS", "12"))
        self.morning_grace_hours = float(os.getenv("BIN_MORNING_GRACE_HOURS", "5"))

        # Safety cap so a bad date calculation never sleeps for weeks/months without logs.
        self.max_sleep_seconds = int(os.getenv("BIN_MAX_SLEEP_SECONDS", str(24 * 3600)))

        self.headers = {
            "User-Agent": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:146.0) Gecko/20100101 Firefox/146.0",
            "Accept": "text/html, */*",
            "x-requested-with": "fetch",
            "Referer": self.base_url or "",
        }

    def load_cache(self):
        try:
            with open(self.cache_file, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def save_cache(self, data):
        with open(self.cache_file, "w") as f:
            json.dump(data, f)

    def load_state(self):
        try:
            with open(self.state_file, "r") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data.setdefault("sent", {})
                return data
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        return {"sent": {}}

    def save_state(self, state):
        path = Path(self.state_file)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        with open(tmp_path, "w") as f:
            json.dump(state, f, indent=2, sort_keys=True)
        tmp_path.replace(path)

    def _sent_key(self, collection_date, milestone):
        return f"{collection_date.isoformat()}|{milestone}"

    def was_sent(self, state, collection_date, milestone):
        return self._sent_key(collection_date, milestone) in state.get("sent", {})

    def mark_sent(self, state, collection_date, milestone):
        state.setdefault("sent", {})[self._sent_key(collection_date, milestone)] = datetime.now().isoformat(timespec="seconds")
        self.save_state(state)

    def prune_state(self, state, keep_days=120):
        cutoff = (datetime.now() - timedelta(days=keep_days)).date()
        sent = state.setdefault("sent", {})
        for key in list(sent.keys()):
            try:
                date_part = key.split("|", 1)[0]
                if datetime.fromisoformat(date_part).date() < cutoff:
                    sent.pop(key, None)
            except Exception:
                # Remove malformed legacy keys.
                sent.pop(key, None)
        self.save_state(state)

    async def fetch_bin_data(self):
        """Scrapes the council site with retries for the loading fragment."""
        if not self.base_url or not self.data_url:
            logging.error("BinBot: WASTE_URL is not configured")
            return None

        async with aiohttp.ClientSession(headers=self.headers) as session:
            try:
                # Step 1: Establish Session (Crucial for cookies)
                await session.get(self.base_url)

                for attempt in range(15):
                    logging.info(f"BinBot Fetch attempt: {attempt + 1}")
                    async with session.get(self.data_url) as resp:
                        html = await resp.text()
                        soup = BeautifulSoup(html, "html.parser")

                        services = soup.find_all("h3", class_="waste-service-name")

                        if services:
                            collections = []
                            logging.info("BinBot: Data received on attempt %s", attempt + 1)
                            for service in services:
                                bin_name = service.get_text(strip=True)
                                if "Bulky" in bin_name:
                                    continue

                                summary_list = service.find_next("dl", class_="govuk-summary-list")

                                if summary_list:
                                    rows = summary_list.find_all("div", class_="govuk-summary-list__row")
                                    for row in rows:
                                        key = row.find("dt", class_="govuk-summary-list__key")
                                        if key and "Next collection" in key.get_text():
                                            value = row.find("dd", class_="govuk-summary-list__value")
                                            if value:
                                                raw_date = value.get_text(" ", strip=True).split("(")[0].strip()
                                                collections.append({"type": bin_name, "date": raw_date})
                                                logging.info("BinBot: %-22s : %s", bin_name, raw_date)
                                                break
                            if collections:
                                logging.info("BinBot data retrieved successfully on attempt %s", attempt + 1)
                                return collections

                    await asyncio.sleep(2)
                return None
            except Exception as e:
                logging.error(f"BinBot Fetch Error: {e}")
                return None

    async def get_next_run_delay(self, collections):
        """Compatibility helper retained for older callers."""
        try:
            now = datetime.now()
            future_dates = self.parse_future_dates(collections, now)
            if not future_dates:
                return 3600
            next_event = future_dates[0][0]
            target = (next_event + timedelta(days=1)).replace(hour=self.refresh_hour, minute=self.refresh_minute, second=0, microsecond=0)
            return max((target - now).total_seconds(), 3600)
        except Exception:
            return 3600

    def clean_kingston_date(self, date_str):
        """Removes ordinal suffixes and commas for parsing."""
        date_str = date_str.replace(",", "")
        import re
        return re.sub(r"(\d+)(st|nd|rd|th)", r"\1", date_str)

    def parse_future_dates(self, data, now=None):
        now = now or datetime.now()
        parsed_dates = []
        for c in data or []:
            clean_date = self.clean_kingston_date(c["date"])
            dt = datetime.strptime(f"{clean_date} {now.year}", "%A %d %B %Y")

            # If the date is more than 6 months in the past, it is almost certainly next year.
            if dt < now - timedelta(days=180):
                dt = dt.replace(year=now.year + 1)

            parsed_dates.append((dt, c["type"]))

        future_dates = [d for d in parsed_dates if d[0] > now]
        future_dates.sort(key=lambda x: x[0])
        return future_dates

    def _milestones_for(self, collection_dt):
        night_before = (collection_dt - timedelta(days=1)).replace(
            hour=self.night_before_hour,
            minute=self.night_before_minute,
            second=0,
            microsecond=0,
        )
        morning_of = collection_dt.replace(
            hour=self.morning_hour,
            minute=self.morning_minute,
            second=0,
            microsecond=0,
        )
        refresh_time = (collection_dt + timedelta(days=1)).replace(
            hour=self.refresh_hour,
            minute=self.refresh_minute,
            second=0,
            microsecond=0,
        )
        return night_before, morning_of, refresh_time

    async def _sleep_until(self, target, label):
        while True:
            now = datetime.now()
            delay = (target - now).total_seconds()
            if delay <= 0:
                return
            sleep_for = min(delay, self.max_sleep_seconds)
            logging.info("BinBot: Sleeping %.1fh until %s.", sleep_for / 3600, label)
            await asyncio.sleep(sleep_for)

    async def bin_scheduler(self, alert_callback):
        """Low-wake scheduler with restart-safe reminder state.

        This intentionally sleeps for hours between meaningful milestones. On startup
        or master-bot restart, it checks bins_state.json so missed reminders inside a
        grace window are sent once, and previously sent reminders are not duplicated.
        """
        while True:
            try:
                state = self.load_state()
                self.prune_state(state)

                data = self.load_cache()
                if not data:
                    logging.info("BinBot: Cache empty, performing first-run fetch...")
                    data = await self.fetch_bin_data()
                    if data:
                        self.save_cache(data)

                if not data:
                    logging.warning("BinBot: No bin data available. Retrying in 1 hour.")
                    await asyncio.sleep(3600)
                    continue

                now = datetime.now()
                future_dates = self.parse_future_dates(data, now)

                if not future_dates:
                    logging.warning("BinBot: No future bin collections found in cache. Refreshing...")
                    refreshed = await self.fetch_bin_data()
                    if refreshed:
                        self.save_cache(refreshed)
                    await asyncio.sleep(3600)
                    continue

                next_date, _ = future_dates[0]
                due_types = [t for d, t in future_dates if d.date() == next_date.date()]
                items_str = ", ".join(due_types)
                collection_date = next_date.date()

                night_before, morning_of, refresh_time = self._milestones_for(next_date)
                night_grace_until = night_before + timedelta(hours=self.night_before_grace_hours)
                morning_grace_until = morning_of + timedelta(hours=self.morning_grace_hours)

                logging.info(
                    "BinBot: Next collection %s items=%s; night=%s morning=%s refresh=%s",
                    collection_date,
                    items_str,
                    night_before.isoformat(timespec="minutes"),
                    morning_of.isoformat(timespec="minutes"),
                    refresh_time.isoformat(timespec="minutes"),
                )

                # Night-before reminder. If the process starts after 18:00 but within
                # the grace window and no state says it was sent, send it now.
                if not self.was_sent(state, collection_date, "night_before"):
                    now = datetime.now()
                    if now < night_before:
                        await self._sleep_until(night_before, "Night Before reminder")
                        now = datetime.now()
                    if night_before <= now <= night_grace_until:
                        logging.info("BinBot: Sending Night Before reminder for %s.", collection_date)
                        await alert_callback(f"🌙 *Night Before* Bin Reminder:\nItems: **{items_str}**")
                        state = self.load_state()
                        self.mark_sent(state, collection_date, "night_before")
                    elif now > night_grace_until:
                        logging.info("BinBot: Night Before reminder window passed for %s.", collection_date)

                # Morning-of reminder. Same restart-safe logic.
                if not self.was_sent(state, collection_date, "morning_of"):
                    now = datetime.now()
                    if now < morning_of:
                        await self._sleep_until(morning_of, "Morning Of reminder")
                        now = datetime.now()
                    if morning_of <= now <= morning_grace_until:
                        logging.info("BinBot: Sending Morning Of reminder for %s.", collection_date)
                        await alert_callback(f"☀️ *Morning Of* Bin Reminder:\nItems: **{items_str}**")
                        state = self.load_state()
                        self.mark_sent(state, collection_date, "morning_of")
                    elif now > morning_grace_until:
                        logging.info("BinBot: Morning Of reminder window passed for %s.", collection_date)

                # Refresh after collection so the next loop gets next week's dates.
                now = datetime.now()
                if now < refresh_time:
                    await self._sleep_until(refresh_time, "post-collection refresh")

                logging.info("BinBot: Refreshing collection schedule...")
                refreshed = await self.fetch_bin_data()
                if refreshed:
                    self.save_cache(refreshed)
                else:
                    logging.warning("BinBot: Refresh failed; keeping existing cache and retrying in 1 hour.")
                    await asyncio.sleep(3600)

            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("BinBot Scheduler Error")
                await asyncio.sleep(3600)

    async def handle_command(self, text):
        if "/bins" in text.lower():
            data = self.load_cache() or await self.fetch_bin_data()
            if not data:
                return "⚠️ Council site is slow. No cached data available."
            self.save_cache(data)
            msg = "🚛 **Upcoming Kingston Collections:**\n\n"
            for item in data:
                msg += f"• **{item['type']}**: {item['date']}\n"
            return msg
        return None
