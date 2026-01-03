import asyncio
import aiohttp
import logging
import os
import json
from dotenv import load_dotenv
import httpx
import base64

# Import existing bot logic
from budget_bot import BudgetBot
from train_bot import TrainBot
from bin_bot import BinBot
from nest_bot import NestBot

load_dotenv()

# --- Global Configuration ---
SIGNAL_API_BASE = "http://localhost:8080"
SIGNAL_NUMBER = os.getenv("SIGNAL_NUMBER")
POLL_INTERVAL = 2

# Mapping Internal IDs (what we see) to External IDs (where we send)
# This handles both Group IDs and direct phone numbers
BOT_ROUTING = {
    os.getenv("BUDGET_INTERNAL_ID"): os.getenv("BUDGET_RECIPIENT"),
    os.getenv("TRAIN_INTERNAL_ID"): os.getenv("TRAIN_RECIPIENT"),
    os.getenv("BIN_INTERNAL_ID"): os.getenv("BIN_RECIPIENT"),
    os.getenv("TESTING_INTERNAL_ID"): os.getenv("TESTING_RECIPIENT"),
    os.getenv("NEST_INTERNAL_ID"): os.getenv("NEST_RECIPIENT")
}

logging.basicConfig(level=logging.INFO)

async def send_signal(session, message, external_id, filepath=None):
    """Centralized sending function."""
    payload = {
        "message": message,
        "number": SIGNAL_NUMBER,
        "recipients": [external_id],
        "text_mode": "styled",
        "base64_attachments": []
    }
    if filepath:
        with open(filepath, "rb") as f:
            b64 = base64.b64encode(f.read()).decode('utf-8')
            payload["base64_attachments"].append(
                f"data:video/mp4;filename={os.path.basename(filepath)};base64,{b64}"
            )
    try:
        async with session.post(f"{SIGNAL_API_BASE}/v2/send", json=payload) as resp:
            if resp.status not in [200, 201]:
                logging.error(f"Send failed: {await resp.text()}")
    except Exception as e:
        logging.error(f"Send error: {e}")

async def master_listener(budget_bot, train_bot, bin_bot, nest_bot):
    """The single loop that polls for all messages."""
    async with aiohttp.ClientSession() as session:
        logging.info("Master Listener online. Routing messages...")
        
        while True:
            try:
                receive_url = f"{SIGNAL_API_BASE}/v1/receive/{SIGNAL_NUMBER}"
                async with session.get(receive_url) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        if data and data != "null":
                            for msg in data:
                                envelope = msg.get("envelope", {})
                                
                                # Extract content from normal or sync messages
                                data_msg = envelope.get("dataMessage")
                                sync_msg = envelope.get("syncMessage", {}).get("sentMessage")
                                target_msg = data_msg or sync_msg

                                if not target_msg:
                                    continue

                                # Identify the sender/group (Internal ID)
                                # For private chats, groupInfo is missing; we use 'source'
                                internal_id = target_msg.get("groupInfo", {}).get("groupId") or envelope.get("source")
                                incoming_text = target_msg.get("message")

                                if not incoming_text or not incoming_text.startswith("/"):
                                    continue
                                print(f"incoming message received from {internal_id}")
                                # --- ROUTING LOGIC ---
                                if internal_id == os.getenv("BUDGET_INTERNAL_ID"):
                                    reply = await budget_bot.handle_command(incoming_text)
                                    if reply:
                                        await send_signal(session, reply, BOT_ROUTING[internal_id])

                                elif internal_id == os.getenv("TRAIN_INTERNAL_ID"):
                                # elif internal_id == os.getenv("TESTING_INTERNAL_ID"):
                                    reply = await train_bot.handle_command(incoming_text)
                                    if reply:
                                        await send_signal(session, reply, BOT_ROUTING[internal_id])

                                elif internal_id == os.getenv("BIN_INTERNAL_ID"):
                                    reply = await bin_bot.handle_command(incoming_text)
                                    if reply:
                                        await send_signal(session, reply, BOT_ROUTING[internal_id])
                                elif internal_id == os.getenv("NEST_INTERNAL_ID"):
                                    reply = await nest_bot.handle_command(incoming_text)
                                    if reply:
                                        if isinstance(reply, tuple) and reply[0] == "FILE":
                                            # reply = ("FILE", "Message text", "filepath")
                                            await send_signal(session, reply[1], BOT_ROUTING[internal_id], reply[2])
                                        else:
                                            await send_signal(session, reply, BOT_ROUTING[internal_id])
                                
                                else:
                                    logging.info(f"Ignored command from unknown source: {internal_id}")

            except Exception as e:
                logging.error(f"Polling error: {e}")
            
            await asyncio.sleep(POLL_INTERVAL)

async def train_alert_monitor(train_bot, session):
    """Specific wrapper to handle train alerts while they are yielded."""
    async for alert_msg in train_bot.monitor_subscriptions(session):
        await send_signal(session, alert_msg, os.getenv("TRAIN_RECIPIENT"))
        # await send_signal(session, alert_msg, os.getenv("TESTING_RECIPIENT"))

async def main():
    budget_bot = BudgetBot()
    train_bot = TrainBot()
    bin_bot = BinBot()
    nest_bot = NestBot()

    async with aiohttp.ClientSession() as session:
        # Define a small helper to bridge the TrainBot alert to the MasterBot sender
        async def train_alert_handler(message):
            await send_signal(session, message, os.getenv("TRAIN_RECIPIENT"))
            # await send_signal(session, message, os.getenv("TESTING_RECIPIENT"))

        async def bin_alert_handler(message):
            await send_signal(session, message, os.getenv("BIN_RECIPIENT"))

        async def budget_alert_handler(message):
            await send_signal(session, message, os.getenv("BUDGET_RECIPIENT"))

        async def nest_alert_handler(message, filepath=None):
            await send_signal(session, message, os.getenv("NEST_RECIPIENT"), filepath)

        await asyncio.gather(
            master_listener(budget_bot, train_bot, bin_bot, nest_bot),
            nest_bot.sync_task(nest_alert_handler),
            budget_bot.weekly_task(budget_alert_handler),
            train_bot.monitor_subscriptions(train_alert_handler),
            bin_bot.bin_scheduler(bin_alert_handler)
        )

if __name__ == "__main__":
    asyncio.run(main())