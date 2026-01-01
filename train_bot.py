import asyncio
import aiohttp
import os
import logging
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()
# --- Configuration ---
LDB_TOKEN = os.getenv("LDB_TOKEN")
SOAP_URL = 'https://lite.realtime.nationalrail.co.uk/OpenLDBWS/ldb12.asmx'

class TrainBot:
    def __init__(self):
        # Format: {"17:45": "On time"}
        self.ldb_token = os.getenv("LDB_TOKEN")
        self.crs = os.getenv("DEFAULT_CRS")
        #print(self.ldb_token)
        self.subscriptions = {}
        if not self.ldb_token:
            logging.error("TrainBot: LDB_TOKEN not found in environment variables.")

    def get_soap_payload(self, crs='NEM'):
        print(self.ldb_token)
        return f"""<?xml version="1.0" encoding="utf-8"?>
<soap12:Envelope xmlns:soap12="http://www.w3.org/2003/05/soap-envelope"
               xmlns:typ="http://thalesgroup.com/RTTI/2013-11-28/Token/types"
               xmlns:ldb="http://thalesgroup.com/RTTI/2021-11-01/ldb/">
    <soap12:Header><typ:AccessToken><typ:TokenValue>{self.ldb_token}</typ:TokenValue></typ:AccessToken></soap12:Header>
    <soap12:Body>
        <ldb:GetDepartureBoardRequest>
            <ldb:numRows>10</ldb:numRows>
            <ldb:crs>{crs}</ldb:crs>
        </ldb:GetDepartureBoardRequest>
    </soap12:Body>
</soap12:Envelope>"""

    def extract_text(self, xml, start_tag, end_tag):
        try:
            start = xml.find(start_tag) + len(start_tag)
            end = xml.find(end_tag, start)
            return xml[start:end].strip()
        except:
            return None

    async def fetch_trains(self, crs='NEM'):
        headers = {
            'Content-Type': 'application/soap+xml; charset=utf-8',
            'SOAPAction': 'http://thalesgroup.com/RTTI/2021-11-01/ldb/GetDepartureBoard'
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(SOAP_URL, data=self.get_soap_payload(crs), headers=headers) as resp:
                text = await resp.text()
                #print(text)
                services = []
                cursor = 0
                while True:
                    start = text.find("<lt8:service", cursor)
                    if start == -1: break
                    end = text.find("</lt8:service>", start) + 14
                    services.append(text[start:end])
                    cursor = end
                
                results = []
                for s in services:
                    results.append({
                        "std": self.extract_text(s, "<lt4:std>", "</lt4:std>"),
                        "etd": self.extract_text(s, "<lt4:etd>", "</lt4:etd>"),
                        "dest": self.extract_text(s, "<lt4:locationName>", "</lt4:locationName>")
                    })
                return results

    # FIXED: Now a method using 'self' and accepting a callback for alerts
    async def monitor_subscriptions(self, alert_callback):
        """Background loop to check watched trains and auto-clean departed ones."""
        while True:
            try:
                # Create a list of keys to remove to avoid 'dictionary changed size during iteration' error
                to_remove = []

                if self.subscriptions:
                    trains = await self.fetch_trains(self.crs)
                    
                    for time_target, last_status in self.subscriptions.items():
                        match = next((t for t in trains if t['std'] == time_target), None)
                        
                        if match:
                            current_status = match['etd']
                            
                            # 1. Check for status changes (e.g., 'On time' -> '17:46')
                            if current_status != last_status:
                                self.subscriptions[time_target] = current_status
                                alert = f"⚠️ **TRAIN UPDATE**: The **{time_target}** to **{match['dest']}** is now: **{current_status}**"
                                await alert_callback(alert)

                            # 2. Auto-unwatch if it has departed
                            if "Departed" in current_status:
                                to_remove.append(time_target)
                                logging.info(f"Auto-unwatching {time_target} (Departed)")
                        
                        else:
                            # 3. Auto-unwatch if the train is no longer on the board
                            to_remove.append(time_target)
                            logging.info(f"Auto-unwatching {time_target} (No longer on board)")

                # Clean up the subscriptions
                for time_target in to_remove:
                    if time_target in self.subscriptions:
                        del self.subscriptions[time_target]

            except Exception as e:
                logging.error(f"Error in monitor_subscriptions: {e}")
                
            await asyncio.sleep(120)

    async def handle_command(self, text):
        parts = text.split()
        if not parts: return None
        cmd = parts[0].lower()

        if cmd in ["/usage", "/help"]:
            return (
                "🚆 *Train Bot Usage*\n"
                "• `/trains [CRS]` - List next 10 departures (default: NEM)\n"
                "• `/watch [time]` - Alert if status changes. Ex: `/watch 08:15`\n"
                "• `/unwatch` - Clear current subscriptions\n"
                "• `/usage` - Show this menu"
            )

        if cmd == "/trains":
            crs = parts[1].upper() if len(parts) > 1 else 'NEM'
            trains = await self.fetch_trains(crs)
            if not trains: return f"⚠️ No trains found for {crs}."
            msg = f"🚆 Departures for {crs}:\n"
            for t in trains:
                msg += f"• {t['std']} to {t['dest']}: {t['etd']}\n"
            return msg

        if cmd == "/watch" and len(parts) > 1:
            time_target = parts[1]
            self.subscriptions[time_target] = "Unknown"
            return f"🔔 Watching the {time_target} departure from {self.crs} for updates."

        if cmd == "/unwatch":
            self.subscriptions.clear()
            return "🔕 Subscriptions cleared."

        return None