import json
import asyncio
import aiohttp
import os
import logging
import xml.etree.ElementTree as ET
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

class TrainBot:
    def __init__(self):
        self.ldb_token = os.getenv("LDB_TOKEN")
        self.default_crs = os.getenv("DEFAULT_CRS", "NEM")
        self.current_context_crs = self.default_crs
        # Subscriptions store (time, station, last_status, last_platform)
        self.subscriptions = {} 
        self.stations_file = "stations.json"
        self.stations = self.load_stations()
        
        if not self.ldb_token:
            logging.error("TrainBot: LDB_TOKEN not found.")

    def load_stations(self):
        """Loads shortcuts from JSON. survive restarts."""
        if os.path.exists(self.stations_file):
            try:
                with open(self.stations_file, 'r') as f:
                    return json.load(f)
            except: pass
        return {"home": "NEM", "work": "WAT"}

    def save_stations(self):
        with open(self.stations_file, 'w') as f:
            json.dump(self.stations, f, indent=4)

    async def fetch_trains(self, crs, filter_crs=None):
        """Official Thales Source with filtering and wildcard XML parsing."""
        url = 'https://lite.realtime.nationalrail.co.uk/OpenLDBWS/ldb12.asmx'
        headers = {'Content-Type': 'text/xml; charset=utf-8'}
        
        filter_tag = f"<ldb:filterCrs>{filter_crs}</ldb:filterCrs><ldb:filterType>to</ldb:filterType>" if filter_crs else ""
        
        payload = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope" 
               xmlns:typ="http://thalesgroup.com/RTTI/2013-11-28/Token/types" 
               xmlns:ldb="http://thalesgroup.com/RTTI/2021-11-01/ldb/">
    <soap:Header><typ:AccessToken><typ:TokenValue>{self.ldb_token}</typ:TokenValue></typ:AccessToken></soap:Header>
    <soap:Body>
        <ldb:GetDepartureBoardRequest>
            <ldb:numRows>10</ldb:numRows><ldb:crs>{crs}</ldb:crs>{filter_tag}
        </ldb:GetDepartureBoardRequest>
    </soap:Body>
</soap:Envelope>"""

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, data=payload, headers=headers) as resp:
                    if resp.status != 200: return []
                    root = ET.fromstring(await resp.text())
                    services = []
                    for service in root.findall('.//{*}service'):
                        std = service.findtext('.//{*}std')
                        etd = service.findtext('.//{*}etd')
                        plat = service.findtext('.//{*}platform') or "TBC"
                        # Specific path for 'True' destination at terminus stations
                        dest_node = service.find('.//{*}destination/{*}location/{*}locationName')
                        dest = dest_node.text if dest_node is not None else "Unknown"
                        services.append({'std': std, 'etd': etd, 'dest': dest, 'plat': plat})
                    return services
            except Exception as e:
                logging.error(f"Fetch Error: {e}")
                return []

    async def monitor_subscriptions(self, alert_callback):
        """Background loop monitoring multiple stations at once."""
        while True:
            try:
                to_remove = []
                # Group by station to be efficient
                stations_to_check = {s for s, _, _ in self.subscriptions.values()}
                for station in stations_to_check:
                    trains = await self.fetch_trains(station)
                    for time_t, (sub_station, last_status, last_plat) in list(self.subscriptions.items()):
                        if sub_station != station: continue
                        match = next((t for t in trains if t['std'] == time_t), None)
                        if match:
                            cur_status, cur_plat = match['etd'], match['plat']
                            if cur_status != last_status or cur_plat != last_plat:
                                self.subscriptions[time_t] = (station, cur_status, cur_plat)
                                await alert_callback(f"⚠️ **UPDATE**: {time_t} from **{station}** is **{cur_status}** [P{cur_plat}]")
                            if "Departed" in cur_status: to_remove.append(time_t)
                        else: to_remove.append(time_t)
                for t in to_remove: self.subscriptions.pop(t, None)
            except Exception as e: logging.error(f"Monitor: {e}")
            await asyncio.sleep(120)

    async def handle_command(self, text):
        parts = text.split()
        if not parts: return None
        cmd = parts[0].lower()

        if cmd in ["/usage", "/help"]:
            return (
                "🚆 **Train Bot**\n"
                "• `/trains [from] [to]` - Board (shortcuts ok)\n"
                "• `/watch [time]` - Watch last queried station\n"
                "• `/unwatch` - Clear alerts\n"
                "• `/list` - Show shortcuts\n"
                "• `/add [name] [CRS]` - Add shortcut"
            )

        if cmd == "/trains":
            origin = self.default_crs
            dest = None
            if len(parts) == 2:
                origin = self.stations.get(parts[1].lower(), parts[1].upper())
            elif len(parts) >= 3:
                origin = self.stations.get(parts[1].lower(), parts[1].upper())
                dest = self.stations.get(parts[2].lower(), parts[2].upper())

            self.current_context_crs = origin # 'Sticky' for subsequent /watch
            trains = await self.fetch_trains(origin, dest)
            if not trains: return f"⚠️ No trains for {origin}" + (f" to {dest}" if dest else ".")
            
            msg = f"🚆 **{origin} Departures**" + (f" to **{dest}**" if dest else "") + ":\n"
            for t in trains:
                msg += f"• {t['std']} to {t['dest']}: **{t['etd']}** [P{t['plat']}]\n"
            return msg

        if cmd == "/watch" and len(parts) > 1:
            time_target = parts[1]
            # Uses the origin station from the last successful /trains call
            self.subscriptions[time_target] = (self.current_context_crs, "Unknown", "TBC")
            return f"🔔 Watching the **{time_target}** from **{self.current_context_crs}**."

        if cmd == "/unwatch":
            self.subscriptions.clear()
            return "🔕 All watches cleared."

        if cmd == "/list":
            return "📋 **Saved Stations:**\n" + "\n".join([f"• {k.title()}: {v}" for k,v in self.stations.items()])

        if cmd == "/add" and len(parts) >= 3:
            name, crs = parts[1].lower(), parts[2].upper()
            self.stations[name] = crs
            self.save_stations()
            return f"✅ Added shortcut: **{name}** → **{crs}**"