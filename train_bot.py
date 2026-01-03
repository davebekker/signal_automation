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

    async def fetch_trains(self, crs, filter_crs=None, with_details=False):
        """Official Thales Source with Detailed Calling Points support."""
        url = 'https://lite.realtime.nationalrail.co.uk/OpenLDBWS/ldb12.asmx'
        headers = {'Content-Type': 'text/xml; charset=utf-8'}
        
        req_type = "GetDepBoardWithDetailsRequest" if with_details else "GetDepartureBoardRequest"
        filter_tag = f"<ldb:filterCrs>{filter_crs}</ldb:filterCrs><ldb:filterType>to</ldb:filterType>" if filter_crs else ""
        
        payload = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope" 
               xmlns:typ="http://thalesgroup.com/RTTI/2013-11-28/Token/types" 
               xmlns:ldb="http://thalesgroup.com/RTTI/2021-11-01/ldb/">
    <soap:Header><typ:AccessToken><typ:TokenValue>{self.ldb_token}</typ:TokenValue></typ:AccessToken></soap:Header>
    <soap:Body>
        <ldb:{req_type}>
            <ldb:numRows>10</ldb:numRows><ldb:crs>{crs}</ldb:crs>{filter_tag}
        </ldb:{req_type}>
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
                        dest_node = service.find('.//{*}destination/{*}location/{*}locationName')
                        dest_name = dest_node.text if dest_node is not None else "Unknown"
                        
                        eta = "N/A"
                        if with_details and filter_crs:
                            # Search calling points for the destination CRS
                            for cp in service.findall('.//{*}callingPoint'):
                                if cp.findtext('.//{*}crs') == filter_crs:
                                    # If 'et' is 'On time', the time is actually in 'st'
                                    est = cp.findtext('.//{*}et')
                                    sch = cp.findtext('.//{*}st')
                                    eta = sch if est == "On time" else est
                                    break

                        services.append({'std': std, 'etd': etd, 'dest': dest_name, 'plat': plat, 'eta': eta})
                    return services
            except Exception as e:
                logging.error(f"Fetch Error: {e}")
                return []

    async def monitor_subscriptions(self, alert_callback):
        """Fixed: Unpacks 4 values (origin, status, plat, dest)."""
        while True:
            try:
                to_remove = []
                # Group by origin station
                stations = {s for s, _, _, _ in self.subscriptions.values()}
                for station in stations:
                    # Monitor doesn't need 'with_details' to keep it fast
                    trains = await self.fetch_trains(station)
                    for time_t, (sub_origin, last_status, last_plat, dest) in list(self.subscriptions.items()):
                        if sub_origin != station: continue
                        
                        match = next((t for t in trains if t['std'] == time_t), None)
                        if match:
                            cur_status, cur_plat = match['etd'], match['plat']
                            if cur_status != last_status or cur_plat != last_plat:
                                self.subscriptions[time_t] = (station, cur_status, cur_plat, dest)
                                await alert_callback(f"⚠️ **UPDATE**: {time_t} from **{station}** is **{cur_status}** [P{cur_plat}]")
                            if "Departed" in cur_status: to_remove.append(time_t)
                        else:
                            to_remove.append(time_t)
                for t in to_remove: self.subscriptions.pop(t, None)
            except Exception as e:
                logging.error(f"Monitor: {e}")
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
            origin, dest = self.default_crs, None
            if len(parts) == 2:
                origin = self.stations.get(parts[1].lower(), parts[1].upper())
            elif len(parts) >= 3:
                origin = self.stations.get(parts[1].lower(), parts[1].upper())
                dest = self.stations.get(parts[2].lower(), parts[2].upper())

            self.current_context_crs = origin
            self.current_context_filter = dest
            
            trains = await self.fetch_trains(origin, dest)
            if not trains: return f"⚠️ No trains found for {origin}."
            
            msg = f"🚆 **{origin} Departures**" + (f" to **{dest}**" if dest else "") + ":\n"
            for t in trains:
                plat = f" [P{t['plat']}]" if t['plat'] != "TBC" else ""
                msg += f"• {t['std']} to {t['dest']}: **{t['etd']}**{plat}\n"
            return msg

        if cmd == "/watch" and len(parts) > 1:
            time_target = parts[1]
            origin, dest = self.current_context_crs, self.current_context_filter
            
            # Fetch details once to get arrival time AND current status/platform
            details = await self.fetch_trains(origin, filter_crs=dest, with_details=True)
            match = next((t for t in details if t['std'] == time_target), None)
            
            # Extract initial data or defaults
            status = match['etd'] if match else "Unknown"
            plat = match['plat'] if match else "TBC"
            eta = match['eta'] if match else "N/A"
            
            eta_str = ""
            if eta != "N/A":
                eta_str = f" (ETA @ {dest}: {eta})" if dest else f" (Arrives: {eta})"
            
            # Save the REAL status and platform immediately
            self.subscriptions[time_target] = (origin, status, plat, dest)
            return f"🔔 Watching the **{time_target}** from **{origin}**{eta_str}."

        if cmd == "/watching":
            if not self.subscriptions: return "🔕 No active watches."
            msg = "👀 **Currently Watching:**\n"
            for time_t, (station, status, plat, dest) in self.subscriptions.items():
                dest_str = f" to **{dest}**" if dest else ""
                p_str = f"P{plat}" if plat != "TBC" else "Plat TBC"
                msg += f"• **{time_t}** from {station}{dest_str} ({status}, {p_str})\n"
            return msg

        if cmd == "/unwatch":
            if len(parts) > 1:
                target = parts[1]
                if self.subscriptions.pop(target, None):
                    return f"✅ Stopped watching **{target}**."
                return f"❓ No watch for **{target}**."
            self.subscriptions.clear()
            return "🔕 All watches cleared."

        if cmd == "/list":
            return "📋 **Saved Stations:**\n" + "\n".join([f"• {k.title()}: {v}" for k,v in self.stations.items()])

        if cmd == "/add" and len(parts) >= 3:
            name, crs = parts[1].lower(), parts[2].upper()
            self.stations[name] = crs
            self.save_stations()
            return f"✅ Added shortcut: **{name}** → **{crs}**"