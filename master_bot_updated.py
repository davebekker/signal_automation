
import asyncio
import aiohttp
import logging
import os
import json
import sqlite3
from dotenv import load_dotenv
import base64
from pathlib import Path
from typing import Optional, Dict, Any

# Import existing bot logic
from budget_bot import BudgetBot
from train_bot import TrainBot
from bin_bot import BinBot
from nest_bot import NestBot
from bots.reminder_bot import ReminderBot
from bots.bluesky_bot import BlueskyBot

load_dotenv()

# --- Global Configuration ---
SIGNAL_API_BASE = "http://localhost:8080"
SIGNAL_NUMBER = os.getenv("SIGNAL_NUMBER")
POLL_INTERVAL = 2
BLUESKY_NOTIFICATION_POLL_INTERVAL = int(os.getenv("BLUESKY_NOTIFICATION_POLL_INTERVAL", "60"))
BLUESKY_FOLLOWED_ONLY = os.getenv("BLUESKY_FOLLOWED_ONLY", "true").lower() in ("1", "true", "yes", "on")
BLUESKY_ACTIVE_POLL_MINUTES = int(os.getenv("BLUESKY_ACTIVE_POLL_MINUTES", "30"))
BLUESKY_REFRESH_FOLLOWS_EVERY = int(os.getenv("BLUESKY_REFRESH_FOLLOWS_EVERY", "20"))
BRIDGE_DB_PATH = os.getenv("BLUESKY_BRIDGE_DB", "bridge.db")

# Mapping Internal IDs (what we see) to External IDs (where we send)
BOT_ROUTING = {
    os.getenv("BUDGET_INTERNAL_ID"): os.getenv("BUDGET_RECIPIENT"),
    os.getenv("TRAIN_INTERNAL_ID"): os.getenv("TRAIN_RECIPIENT"),
    os.getenv("BIN_INTERNAL_ID"): os.getenv("BIN_RECIPIENT"),
    os.getenv("TESTING_INTERNAL_ID"): os.getenv("TESTING_RECIPIENT"),
    os.getenv("NEST_INTERNAL_ID"): os.getenv("NEST_RECIPIENT"),
    os.getenv("REMINDER_INTERNAL_ID"): os.getenv("REMINDER_RECIPIENT"),
    os.getenv("BLUESKY_INTERNAL_ID"): os.getenv("BLUESKY_RECIPIENT"),
}

ATTACHMENTS_DIR = Path("/tmp/signal_attachments")
ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO)


class BridgeStore:
    def __init__(self, path: str = BRIDGE_DB_PATH):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self):
        self.conn.execute(
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
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_message_map_signal
            ON message_map(signal_group_id, signal_ts)
            """
        )
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_message_map_bsky
            ON message_map(bluesky_uri)
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_notifications (
                uri TEXT PRIMARY KEY,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_signal_messages (
                dedupe_key TEXT PRIMARY KEY,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bluesky_watch_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                active_until_ts INTEGER,
                last_manual_check_ts INTEGER
            )
            """
        )
        self.conn.execute(
            """
            INSERT OR IGNORE INTO bluesky_watch_state (id, active_until_ts, last_manual_check_ts)
            VALUES (1, NULL, NULL)
            """
        )
        self.conn.commit()

    def save_mapping(self, **kwargs):
        cols = ", ".join(kwargs.keys())
        placeholders = ", ".join("?" for _ in kwargs)
        self.conn.execute(
            f"INSERT OR REPLACE INTO message_map ({cols}) VALUES ({placeholders})",
            list(kwargs.values()),
        )
        self.conn.commit()

    def by_signal_ts(self, group_id: str, signal_ts: str):
        cur = self.conn.execute(
            "SELECT * FROM message_map WHERE signal_group_id = ? AND signal_ts = ? ORDER BY id DESC LIMIT 1",
            (group_id, str(signal_ts)),
        )
        return cur.fetchone()

    def by_bluesky_uri(self, uri: str):
        cur = self.conn.execute(
            "SELECT * FROM message_map WHERE bluesky_uri = ? LIMIT 1",
            (uri,),
        )
        return cur.fetchone()

    def notification_seen(self, uri: str) -> bool:
        cur = self.conn.execute(
            "SELECT 1 FROM processed_notifications WHERE uri = ? LIMIT 1",
            (uri,),
        )
        return cur.fetchone() is not None

    def mark_notification_seen(self, uri: str):
        self.conn.execute(
            "INSERT OR IGNORE INTO processed_notifications (uri) VALUES (?)",
            (uri,),
        )
        self.conn.commit()

    def signal_message_seen(self, dedupe_key: str) -> bool:
        cur = self.conn.execute(
            "SELECT 1 FROM processed_signal_messages WHERE dedupe_key = ? LIMIT 1",
            (dedupe_key,),
        )
        return cur.fetchone() is not None

    def mark_signal_message_seen(self, dedupe_key: str):
        self.conn.execute(
            "INSERT OR IGNORE INTO processed_signal_messages (dedupe_key) VALUES (?)",
            (dedupe_key,),
        )
        self.conn.commit()

    def activate_watch_window(self, minutes: int):
        now = int(asyncio.get_running_loop().time())
        active_until = now + max(1, minutes) * 60
        self.conn.execute(
            "UPDATE bluesky_watch_state SET active_until_ts = ? WHERE id = 1",
            (active_until,),
        )
        self.conn.commit()
        return active_until

    def deactivate_watch_window(self):
        self.conn.execute(
            "UPDATE bluesky_watch_state SET active_until_ts = NULL WHERE id = 1"
        )
        self.conn.commit()

    def is_watch_active(self) -> bool:
        cur = self.conn.execute(
            "SELECT active_until_ts FROM bluesky_watch_state WHERE id = 1"
        )
        row = cur.fetchone()
        if not row or row["active_until_ts"] is None:
            return False
        now = int(asyncio.get_running_loop().time())
        return row["active_until_ts"] > now

    def watch_status(self) -> Dict[str, Any]:
        cur = self.conn.execute(
            "SELECT active_until_ts, last_manual_check_ts FROM bluesky_watch_state WHERE id = 1"
        )
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

    def mark_manual_check(self):
        now = int(asyncio.get_running_loop().time())
        self.conn.execute(
            "UPDATE bluesky_watch_state SET last_manual_check_ts = ? WHERE id = 1",
            (now,),
        )
        self.conn.commit()


async def download_signal_attachment(session, attachment_id, filename=None):
    url = f"{SIGNAL_API_BASE}/v1/attachments/{attachment_id}"

    async with session.get(url) as resp:
        if resp.status != 200:
            logging.error("Failed to download attachment %s: %s", attachment_id, resp.status)
            return None

        data = await resp.read()

    safe_name = filename or attachment_id
    out_path = ATTACHMENTS_DIR / safe_name

    with open(out_path, "wb") as f:
        f.write(data)

    logging.info("Saved attachment %s to %s", attachment_id, out_path)
    return str(out_path)


async def send_signal(
    session,
    message,
    external_id,
    filepath=None,
    quote_timestamp=None,
    quote_author=None,
):
    """Centralized sending function, with optional Signal quote/reply support."""
    payload = {
        "message": message,
        "number": SIGNAL_NUMBER,
        "recipients": [external_id],
        "text_mode": "styled",
        "base64_attachments": []
    }

    if quote_timestamp is not None:
        try:
            payload["quote_timestamp"] = int(quote_timestamp)
            if quote_author:
                payload["quote_author"] = str(quote_author)
        except (TypeError, ValueError):
            logging.warning("Invalid quote_timestamp %r; sending without quote", quote_timestamp)

    if filepath:
        with open(filepath, "rb") as f:
            b64 = base64.b64encode(f.read()).decode('utf-8')
            payload["base64_attachments"].append(
                f"data:video/mp4;filename={os.path.basename(filepath)};base64,{b64}"
            )
    try:
        async with session.post(f"{SIGNAL_API_BASE}/v2/send", json=payload) as resp:
            body = await resp.text()
            if resp.status not in [200, 201]:
                logging.error("Send failed (%s): %s", resp.status, body)
                return None

            try:
                result = json.loads(body) if body else None
            except Exception:
                result = None

            sent_ts = None
            if isinstance(result, dict):
                if isinstance(result.get("timestamp"), (int, str)):
                    sent_ts = str(result.get("timestamp"))
                elif isinstance(result.get("results"), list) and result["results"]:
                    first = result["results"][0]
                    if isinstance(first, dict) and first.get("timestamp") is not None:
                        sent_ts = str(first.get("timestamp"))
            return sent_ts
    except Exception as e:
        logging.error(f"Send error: {e}")
        return None


def _extract_signal_quote_id(target_msg: Dict[str, Any]) -> Optional[str]:
    quote = target_msg.get("quote") or {}
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
    bluesky_bot,
    store: BridgeStore,
    force: bool = False,
) -> Dict[str, int]:
    followed_refresh_counter = getattr(_poll_and_mirror_bluesky_replies, "_followed_refresh_counter", 0)

    refresh_follows = False
    if BLUESKY_FOLLOWED_ONLY:
        followed_refresh_counter += 1
        if followed_refresh_counter >= BLUESKY_REFRESH_FOLLOWS_EVERY or force:
            refresh_follows = True
            followed_refresh_counter = 0
    _poll_and_mirror_bluesky_replies._followed_refresh_counter = followed_refresh_counter

    result = bluesky_bot.list_reply_notifications(
        limit=50,
        followed_only=BLUESKY_FOLLOWED_ONLY,
        refresh_follows=refresh_follows,
    )

    notifications = result.get("notifications", []) if isinstance(result, dict) else []
    my_did = getattr(getattr(bluesky_bot.client, "me", None), "did", None)
    mirrored = 0
    skipped = 0

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
        text = (parsed.get("text") or "").strip()
        if not text:
            text = "[no text]"

        signal_text = f"🦋 @{author} replied:\n{text}"

        quoted_signal_ts = mapped_parent["signal_ts"]
        quoted_signal_author = mapped_parent["signal_sender"]

        if quoted_signal_ts and str(quoted_signal_ts).isdigit():
            sent_ts = await send_signal(
                session,
                signal_text,
                os.getenv("BLUESKY_RECIPIENT"),
                quote_timestamp=quoted_signal_ts,
                quote_author=quoted_signal_author,
            )
        else:
            logging.warning(
                "Parent mapping found but no numeric Signal timestamp to quote: %r",
                quoted_signal_ts,
            )
            sent_ts = await send_signal(
                session,
                signal_text,
                os.getenv("BLUESKY_RECIPIENT"),
            )

        store.save_mapping(
            signal_group_id=os.getenv("BLUESKY_INTERNAL_ID"),
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
    bluesky_bot,
    store: BridgeStore,
    incoming_text: str,
    internal_id: str,
):
    parts = incoming_text.strip().split()
    subcommand = parts[1].lower() if len(parts) > 1 else ""

    if subcommand in ("", "check", "now"):
        store.mark_manual_check()
        stats = await _poll_and_mirror_bluesky_replies(session, bluesky_bot, store, force=True)
        if stats["mirrored"]:
            reply = f"🦋 Checked notifications and mirrored {stats['mirrored']} repl{'y' if stats['mirrored'] == 1 else 'ies'}."
        else:
            reply = "🦋 Checked notifications. Nothing new to mirror."
        await send_signal(session, reply, BOT_ROUTING[internal_id])
        return

    if subcommand == "status":
        await send_signal(session, _format_watch_status(store), BOT_ROUTING[internal_id])
        return

    if subcommand == "off":
        store.deactivate_watch_window()
        await send_signal(session, "🦋 Notification watch turned off.", BOT_ROUTING[internal_id])
        return

    if subcommand == "on":
        minutes = BLUESKY_ACTIVE_POLL_MINUTES
        if len(parts) > 2:
            try:
                minutes = int(parts[2])
            except ValueError:
                pass
        store.activate_watch_window(minutes)
        await send_signal(
            session,
            f"🦋 Notification watch turned on for {minutes} minute(s).",
            BOT_ROUTING[internal_id],
        )
        return

    help_text = (
        "🦋 /notif commands:\n"
        "/notif - check now\n"
        "/notif status - show watcher status\n"
        "/notif on [minutes] - enable temporary polling\n"
        "/notif off - disable temporary polling"
    )
    await send_signal(session, help_text, BOT_ROUTING[internal_id])


async def _handle_bluesky_message(session, bluesky_bot, store: BridgeStore, envelope: Dict[str, Any], target_msg: Dict[str, Any], internal_id: str):
    incoming_text = target_msg.get("message") or ""

    if incoming_text.strip().lower().startswith("/notif"):
        await _handle_bluesky_notif_command(session, bluesky_bot, store, incoming_text, internal_id)
        return

    attachments = target_msg.get("attachments", [])
    attachment_id = None
    attachment_filename = None

    if attachments:
        attachment = attachments[0]
        attachment_id = attachment.get("id")
        attachment_filename = attachment.get("filename")

    logging.info(
        "Bluesky message text=%r attachments=%r attachment_id=%r filename=%r",
        incoming_text,
        attachments,
        attachment_id,
        attachment_filename,
    )

    if not incoming_text and not attachment_id:
        return

    image_path = None
    if attachment_id:
        image_path = await download_signal_attachment(session, attachment_id, attachment_filename)

    clean_text = incoming_text.replace("/bs", "", 1).strip() if incoming_text else ""

    quoted_signal_ts = _extract_signal_quote_id(target_msg)
    reply_ref = None
    if quoted_signal_ts:
        mapped = store.by_signal_ts(internal_id, quoted_signal_ts)
        if mapped:
            reply_ref = {
                "root_uri": mapped["bluesky_root_uri"] or mapped["bluesky_uri"],
                "root_cid": mapped["bluesky_root_cid"] or mapped["bluesky_cid"],
                "parent_uri": mapped["bluesky_uri"],
                "parent_cid": mapped["bluesky_cid"],
                "parent_author_did": mapped["bluesky_author_did"],
            }
            logging.info("Resolved Signal reply %s to Bluesky URI %s", quoted_signal_ts, mapped["bluesky_uri"])
        else:
            logging.info("Signal reply had quote id %s but no Bluesky mapping found", quoted_signal_ts)

    signal_ts = str(envelope.get("timestamp") or target_msg.get("timestamp") or "")
    dedupe_key = f"{internal_id}:{signal_ts}:{clean_text}:{attachment_id or ''}"
    if dedupe_key and store.signal_message_seen(dedupe_key):
        logging.info("Skipping already processed Bluesky Signal message %s", dedupe_key)
        return
    if dedupe_key:
        store.mark_signal_message_seen(dedupe_key)

    status = await bluesky_bot.handle_command(
        text=clean_text,
        image_path=image_path,
        reply_ref=reply_ref,
    )

    if isinstance(status, str):
        logging.info("BlueskyBot returned legacy status string: %s", status)
        return

    if not status or not status.get("ok"):
        logging.error("Bluesky post failed: %s", status)
        await send_signal(
            session,
            f"❌ Failed to post to Bluesky: {(status or {}).get('message', 'unknown error')}",
            BOT_ROUTING[internal_id],
        )
        return

    sender = envelope.get("source")
    for post in status.get("posts", []):
        store.save_mapping(
            signal_group_id=internal_id,
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

    store.activate_watch_window(BLUESKY_ACTIVE_POLL_MINUTES)
    await send_signal(
        session,
        f"🦋 Posted to Bluesky. Notification watch active for {BLUESKY_ACTIVE_POLL_MINUTES} minute(s).",
        BOT_ROUTING[internal_id],
    )


async def sync_bluesky_replies(bluesky_bot, store: BridgeStore):
    """Poll Bluesky notifications only while watch window is active."""
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                if store.is_watch_active():
                    await _poll_and_mirror_bluesky_replies(session, bluesky_bot, store)
            except Exception as e:
                logging.error(f"Bluesky notification sync error: {e}")

            await asyncio.sleep(BLUESKY_NOTIFICATION_POLL_INTERVAL)


async def master_listener(budget_bot, train_bot, bin_bot, nest_bot, reminder_bot, bluesky_bot, store: BridgeStore):
    """The single loop that streams all messages via WebSocket in json-rpc mode."""
    async with aiohttp.ClientSession() as session:
        logging.info("Master Listener online. Opening WebSocket connection...")
        
        # Connect to the WebSocket instead of standard HTTP GET
        ws_url = f"{SIGNAL_API_BASE}/v1/receive/{SIGNAL_NUMBER}"
        
        while True:
            try:
                async with session.ws_connect(ws_url) as ws:
                    logging.info("WebSocket connected successfully!")
                    
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            # The WebSocket streams messages individually or as arrays
                            payload = json.loads(msg.data)
                            
                            # Standardize single dictionary payloads into a iterable list
                            data_list = payload if isinstance(payload, list) else [payload]
                            
                            for item in data_list:
                                envelope = item.get("envelope", {})
                                data_msg = envelope.get("dataMessage")
                                sync_msg = envelope.get("syncMessage", {}).get("sentMessage")
                                target_msg = data_msg or sync_msg

                                if not target_msg:
                                    continue

                                internal_id = target_msg.get("groupInfo", {}).get("groupId") or envelope.get("source")
                                incoming_text = target_msg.get("message") or ""

                                if internal_id == os.getenv("BLUESKY_INTERNAL_ID"):
                                    print(f"incoming message received from {internal_id}")
                                    await _handle_bluesky_message(session, bluesky_bot, store, envelope, target_msg, internal_id)
                                    continue

                                if not incoming_text or not incoming_text.startswith("/"):
                                    continue

                                print(f"incoming message received from {internal_id}")
                                if internal_id == os.getenv("BUDGET_INTERNAL_ID"):
                                    reply = await budget_bot.handle_command(incoming_text)
                                    if reply:
                                        await send_signal(session, reply, BOT_ROUTING[internal_id])

                                elif internal_id == os.getenv("TRAIN_INTERNAL_ID"):
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
                                            await send_signal(session, reply[1], BOT_ROUTING[internal_id], reply[2])
                                        else:
                                            await send_signal(session, reply, BOT_ROUTING[internal_id])

                                elif internal_id == os.getenv("REMINDER_INTERNAL_ID"):
                                    reply = await reminder_bot.handle_command(incoming_text)
                                    if reply:
                                        await send_signal(session, reply, BOT_ROUTING[internal_id])
                                else:
                                    print("unknown source")
                                    logging.info(f"Ignored command from unknown source: {internal_id}")
                                    
            except Exception as e:
                logging.error(f"WebSocket connection dropped/error: {e}")
                logging.info("Reconnecting in 5 seconds...")
                await asyncio.sleep(5)


async def main():
    budget_bot = BudgetBot()
    train_bot = TrainBot()
    bin_bot = BinBot()
    nest_bot = NestBot()
    reminder_bot = ReminderBot()
    bluesky_bot = BlueskyBot()
    store = BridgeStore()

    async with aiohttp.ClientSession() as session:
        async def train_alert_handler(message):
            await send_signal(session, message, os.getenv("TRAIN_RECIPIENT"))

        async def bin_alert_handler(message):
            await send_signal(session, message, os.getenv("BIN_RECIPIENT"))

        async def budget_alert_handler(message):
            await send_signal(session, message, os.getenv("BUDGET_RECIPIENT"))

        async def nest_alert_handler(message, filepath=None):
            await send_signal(session, message, os.getenv("NEST_RECIPIENT"), filepath)

        async def remind_alert_handler(message):
            await send_signal(session, message, os.getenv("REMINDER_RECIPIENT"))

        await asyncio.gather(
            master_listener(budget_bot, train_bot, bin_bot, nest_bot, reminder_bot, bluesky_bot, store),
            sync_bluesky_replies(bluesky_bot, store),
            nest_bot.sync_task(nest_alert_handler),
            budget_bot.weekly_task(budget_alert_handler),
            train_bot.monitor_subscriptions(train_alert_handler),
            bin_bot.bin_scheduler(bin_alert_handler),
            reminder_bot.check_reminders(remind_alert_handler)
        )


if __name__ == "__main__":
    asyncio.run(main())
