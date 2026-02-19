import json
import asyncio
import aiohttp
import logging
import os
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

class BinBot:
    def __init__(self):
        self.base_url = os.getenv("WASTE_URL")
        self.data_url = f"{self.base_url}?page_loading=1"
        self.cache_file = "bins.json"
        self.headers = {
            "User-Agent": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:146.0) Gecko/20100101 Firefox/146.0",
            "Accept": "text/html, */*",
            "x-requested-with": "fetch",
            "Referer": self.base_url
        }

    def load_cache(self):
        try:
            with open(self.cache_file, 'r') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def save_cache(self, data):
        with open(self.cache_file, 'w') as f:
            json.dump(data, f)

    async def fetch_bin_data(self):
        """Scrapes the council site with retries for the loading fragment."""
        async with aiohttp.ClientSession(headers=self.headers) as session:
            try:
                # Step 1: Establish Session (Crucial for cookies)
                await session.get(self.base_url)
                
                for attempt in range(15): # Increased to match your 3rd-attempt success
                    logging.info(f'BinBot Fetch attempt: {attempt + 1}')
                    async with session.get(self.data_url) as resp:
                        html = await resp.text()
                        soup = BeautifulSoup(html, 'html.parser')
                        
                        # Look for the headers that indicate data has loaded
                        services = soup.find_all('h3', class_='waste-service-name')
                        
                        if services:
                            collections = []
                            print(f"✅ Data received on attempt {attempt + 1}!")
                            for service in services:
                                bin_name = service.get_text(strip=True)
                                if "Bulky" in bin_name: continue

                                # 1. Find the summary list associated with this service
                                # Kingston usually puts the data in a <dl> (description list) after the h3
                                summary_list = service.find_next('dl', class_='govuk-summary-list')
                                
                                if summary_list:
                                    # 2. Find the row where the key (dt) is "Next collection"
                                    rows = summary_list.find_all('div', class_='govuk-summary-list__row')
                                    for row in rows:
                                        key = row.find('dt', class_='govuk-summary-list__key')
                                        if key and "Next collection" in key.get_text():
                                            value = row.find('dd', class_='govuk-summary-list__value')
                                            if value:
                                                # Strip the ordinal suffixes (st, nd, rd, th) and brackets
                                                raw_date = value.get_text(" ", strip=True).split('(')[0].strip()
                                                collections.append({"type": bin_name, "date": raw_date})
                                                print(f"• {bin_name.ljust(22)} : {raw_date}")
                                                break
                            if collections:
                                logging.info(f"✅ BinBot data retrieved successfully on attempt {attempt + 1}")
                                return collections
                                
                    await asyncio.sleep(2)
                return None
            except Exception as e:
                logging.error(f"BinBot Fetch Error: {e}")
                return None

    async def get_next_run_delay(self, collections):
        """Calculates delay until 9am the day after the closest collection."""
        try:
            # Kingston format: "Friday 2 Jan" -> We need to handle year logic
            now = datetime.now()
            dates = []
            for c in collections:
                # Basic parser: adds current year; handles wrap-around if date is in past
                dt = datetime.strptime(f"{c['date']} {now.year}", "%A, %d %b %Y")
                if dt < now - timedelta(days=1): dt = dt.replace(year=now.year + 1)
                dates.append(dt)
            
            next_event = min(dates)
            target = (next_event + timedelta(days=1)).replace(hour=9, minute=0)
            return max((target - now).total_seconds(), 3600)
        except:
            return 3600 # Fallback to 1 hour

    def clean_kingston_date(self, date_str):
        """Removes ordinal suffixes (st, nd, rd, th) and commas for parsing."""
        # Remove commas
        date_str = date_str.replace(",", "")
        # Remove suffixes: find 'st', 'nd', 'rd', 'th' preceded by a digit and remove them
        import re
        date_str = re.sub(r'(\d+)(st|nd|rd|th)', r'\1', date_str)
        return date_str
    
    async def bin_scheduler(self, alert_callback):
        """Sequential scheduler: Sleeps until Night Before, Morning Of, then Refreshes."""
        while True:
            try:
                # 1. Ensure we have data
                data = self.load_cache()
                if not data:
                    logging.info("BinBot: Cache empty, performing first-run fetch...")
                    data = await self.fetch_bin_data()
                    if data: self.save_cache(data)

                if not data:
                    await asyncio.sleep(3600) # Retry in 1 hour if fetch failed
                    continue

                # 2. Parse dates using the cleaner
                now = datetime.now()
                parsed_dates = []
                for c in data:
                    clean_date = self.clean_kingston_date(c['date'])
                    # Format: 'Thursday 15 January 2026'
                    dt = datetime.strptime(f"{clean_date} {now.year}", "%A %d %B %Y")
                    
                    # --- FIXED YEAR WRAP LOGIC ---
                    # If the date is more than 6 months in the past, it's almost certainly for NEXT year.
                    # (e.g. It's December and the JSON says "January")
                    if dt < now - timedelta(days=180):
                        dt = dt.replace(year=now.year + 1)
                        
                    # If the date is in the past but LESS than 6 months ago, it's just "stale" data.
                    # We should keep the current year but it will be filtered out by 'dt > now' later.
                    
                    parsed_dates.append((dt, c['type']))

                # Filter for only future events so we don't sleep until a date in the past
                future_dates = [d for d in parsed_dates if d[0] > now]

                if not future_dates:
                    logger.warning("No future bin collections found in data. JSON might be stale.")
                    return 3600 # Check again in 1 hour
                
                # Sort to find the nearest collection day
                parsed_dates.sort(key=lambda x: x[0])
                next_date, _ = parsed_dates[0]
                
                # Identify all bins due on that same day
                due_types = [t for d, t in parsed_dates if d.date() == next_date.date()]
                items_str = ", ".join(due_types)

                # 3. MILESTONE SEQUENCE
                # Milestone 1: Night Before (18:00)
                night_before = next_date - timedelta(days=1)
                night_before = night_before.replace(hour=18, minute=0, second=0)
                
                if now < night_before:
                    delay = (night_before - now).total_seconds()
                    logging.info(f"BinBot: Sleeping {delay/3600:.1f}h until Night Before reminder.")
                    await asyncio.sleep(delay)
                    await alert_callback(f"🌙 *Night Before* Bin Reminder:\nItems: **{items_str}**")
                    now = datetime.now()

                # Milestone 2: Morning Of (07:00)
                morning_of = next_date.replace(hour=7, minute=0, second=0)
                if now < morning_of:
                    delay = (morning_of - now).total_seconds()
                    logging.info(f"BinBot: Sleeping {delay/3600:.1f}h until Morning Of reminder.")
                    await asyncio.sleep(delay)
                    await alert_callback(f"☀️ *Morning Of* Bin Reminder:\nItems: **{items_str}**")
                    now = datetime.now()

                # Milestone 3: Refresh Data (09:00 the day AFTER collection)
                refresh_time = (next_date + timedelta(days=1)).replace(hour=9, minute=0, second=0)
                if now < refresh_time:
                    delay = (refresh_time - now).total_seconds()
                    logging.info(f"BinBot: Collection passed. Sleeping {delay/3600:.1f}h until refresh.")
                    await asyncio.sleep(delay)
                
                # Perform the refresh to get next week's dates
                logging.info("BinBot: Refreshing collection schedule...")
                data = await self.fetch_bin_data()
                if data: self.save_cache(data)

            except Exception as e:
                logging.error(f"BinBot Scheduler Error: {e}")
                await asyncio.sleep(3600)

    async def handle_command(self, text):
        if "/bins" in text.lower():
            data = self.load_cache() or await self.fetch_bin_data()
            if not data: return "⚠️ Council site is slow. No cached data available."
            self.save_cache(data)
            msg = "🚛 **Upcoming Kingston Collections:**\n\n"
            for item in data:
                msg += f"• **{item['type']}**: {item['date']}\n"
            return msg
        return None