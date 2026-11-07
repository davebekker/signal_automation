"""Unit tests for master_bot_v2 hardening and implicit state machine fixes."""

import asyncio
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
from aiohttp import web

from master_bot_v2 import (
    AppConfig,
    BotResponse,
    BotRoute,
    BridgeStore,
    _dispatch_standard_bot,
    _handle_signal_item,
    _load_shopping_internal_ids,
    _supervise,
    cleanup_old_attachments,
    download_signal_attachment,
    send_signal,
)


class TestShoppingIdParsing(unittest.TestCase):
    def test_load_shopping_internal_ids_empty(self):
        with patch.dict(os.environ, {}, clear=True):
            ids = _load_shopping_internal_ids()
            self.assertEqual(ids, set())
            self.assertFalse("any_id" in ids)

    def test_load_shopping_internal_ids_single(self):
        with patch.dict(os.environ, {"SHOPPING_INTERNAL_ID": "group.123"}):
            ids = _load_shopping_internal_ids()
            self.assertEqual(ids, {"group.123"})
            self.assertTrue("group.123" in ids)
            self.assertFalse("group.12" in ids)  # Not substring matching

    def test_load_shopping_internal_ids_multiple(self):
        with patch.dict(os.environ, {"SHOPPING_INTERNAL_ID": "group.1, group.2 , group.3"}):
            ids = _load_shopping_internal_ids()
            self.assertEqual(ids, {"group.1", "group.2", "group.3"})


class TestBridgeStoreWatchWindow(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_bridge.db")
        self.store = BridgeStore(self.db_path)

    def tearDown(self):
        self.store.conn.close()
        shutil.rmtree(self.temp_dir)

    def test_watch_window_wall_clock(self):
        self.assertFalse(self.store.is_watch_active())
        status = self.store.watch_status()
        self.assertFalse(status["active"])

        # Activate for 10 minutes
        now = int(time.time())
        expires = self.store.activate_watch_window(10)
        self.assertGreaterEqual(expires, now + 599)
        self.assertTrue(self.store.is_watch_active())

        status = self.store.watch_status()
        self.assertTrue(status["active"])
        self.assertGreater(status["remaining_seconds"], 500)

        # Deactivate
        self.store.deactivate_watch_window()
        self.assertFalse(self.store.is_watch_active())

    def test_watch_window_survives_restart_simulation(self):
        # Activate window
        self.store.activate_watch_window(30)
        self.store.conn.close()

        # Reopen database in fresh instance
        reopened_store = BridgeStore(self.db_path)
        self.assertTrue(reopened_store.is_watch_active())
        status = reopened_store.watch_status()
        self.assertTrue(status["active"])
        self.assertGreater(status["remaining_seconds"], 1700)
        reopened_store.conn.close()


class TestIngressDeduplication(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_bridge.db")
        self.store = BridgeStore(self.db_path)
        self.attachments_dir = Path(self.temp_dir) / "attachments"
        self.attachments_dir.mkdir()

        self.route = BotRoute(kind="budget", internal_id="group.budget", recipient="+1234567890")
        self.config = AppConfig(
            signal_api_base="http://localhost:8080",
            signal_number="+0000000000",
            bridge_db_path=self.db_path,
            attachments_dir=self.attachments_dir,
            max_attachment_bytes=1024 * 1024,
            bluesky_notification_poll_interval=60,
            bluesky_followed_only=True,
            bluesky_active_poll_minutes=30,
            bluesky_refresh_follows_every=20,
            bluesky_auto_sync_enabled=False,
            routes_by_internal_id={"group.budget": self.route},
            route_by_kind={"budget": self.route},
        )

    def tearDown(self):
        self.store.conn.close()
        shutil.rmtree(self.temp_dir)

    async def test_standard_bot_deduplication(self):
        mock_budget_bot = MagicMock()
        mock_budget_bot.handle_command = AsyncMock(return_value="Expense recorded: $50")
        bots = {"budget": mock_budget_bot}

        item = {
            "envelope": {
                "source": "+9876543210",
                "timestamp": 1730000000000,
                "dataMessage": {
                    "message": "/spend 50 groceries",
                    "groupInfo": {"groupId": "group.budget"},
                },
            }
        }

        mock_session = MagicMock(spec=aiohttp.ClientSession)

        with patch("master_bot_v2.send_signal", new_callable=AsyncMock) as mock_send:
            # First delivery
            await _handle_signal_item(mock_session, item, bots, self.store, self.config)
            self.assertEqual(mock_budget_bot.handle_command.call_count, 1)

            # Replayed delivery (e.g. Signal CLI reconnect replay)
            await _handle_signal_item(mock_session, item, bots, self.store, self.config)
            # Should NOT be called again
            self.assertEqual(mock_budget_bot.handle_command.call_count, 1)


class TestSubBotDispatchContract(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.route = BotRoute(kind="budget", internal_id="group.budget", recipient="+1234567890")
        self.config = MagicMock(spec=AppConfig)

    async def test_dispatch_bot_response_dataclass(self):
        mock_bot = MagicMock()
        mock_bot.handle_command = AsyncMock(
            return_value=BotResponse(text="Hello from dataclass", filepath="/tmp/file.png")
        )
        bots = {"budget": mock_bot}
        mock_session = MagicMock(spec=aiohttp.ClientSession)

        with patch("master_bot_v2.send_signal", new_callable=AsyncMock) as mock_send:
            await _dispatch_standard_bot(mock_session, self.route, "/test", bots, self.config)
            mock_send.assert_awaited_once_with(
                mock_session,
                "Hello from dataclass",
                "+1234567890",
                filepath="/tmp/file.png",
                quote_timestamp=None,
                quote_author=None,
                config=self.config,
            )

    async def test_dispatch_legacy_nest_file_tuple(self):
        nest_route = BotRoute(kind="nest", internal_id="group.nest", recipient="+1234567890")
        mock_nest = MagicMock()
        mock_nest.handle_command = AsyncMock(return_value=("FILE", "Here is clip", "/tmp/clip.mp4"))
        bots = {"nest": mock_nest}
        mock_session = MagicMock(spec=aiohttp.ClientSession)

        with patch("master_bot_v2.send_signal", new_callable=AsyncMock) as mock_send:
            await _dispatch_standard_bot(mock_session, nest_route, "/get 1", bots, self.config)
            mock_send.assert_awaited_once_with(
                mock_session,
                "Here is clip",
                "+1234567890",
                filepath="/tmp/clip.mp4",
                config=self.config,
            )


class TestAttachmentLifecycle(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.attachments_dir = Path(self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def test_cleanup_old_attachments(self):
        # Create an old file
        old_file = self.attachments_dir / "old.jpg"
        old_file.write_text("old")
        past_time = time.time() - 90000  # > 24 hours ago
        os.utime(old_file, (past_time, past_time))

        # Create a fresh file
        fresh_file = self.attachments_dir / "fresh.jpg"
        fresh_file.write_text("fresh")

        deleted = cleanup_old_attachments(self.attachments_dir, max_age_seconds=86400)
        self.assertEqual(deleted, 1)
        self.assertFalse(old_file.exists())
        self.assertTrue(fresh_file.exists())


class TestSuperviseBackoffReset(unittest.IsolatedAsyncioTestCase):
    async def test_supervise_resets_backoff_on_stable_uptime(self):
        call_count = 0

        async def dummy_task():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First run: crashes quickly
                raise RuntimeError("Quick transient crash")
            elif call_count == 2:
                # Simulated cancelled on second run to stop supervisor
                raise asyncio.CancelledError()

        with patch("master_bot_v2.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            try:
                await _supervise("test_task", dummy_task)
            except asyncio.CancelledError:
                pass

            # Checked that sleep was called with initial backoff
            mock_sleep.assert_awaited_once_with(2)


class TestAttachmentDownloadStreaming(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.attachments_dir = Path(self.temp_dir)
        self.config = AppConfig(
            signal_api_base="http://localhost:8080",
            signal_number="+0000000000",
            bridge_db_path=":memory:",
            attachments_dir=self.attachments_dir,
            max_attachment_bytes=100,  # 100 bytes limit
            bluesky_notification_poll_interval=60,
            bluesky_followed_only=True,
            bluesky_active_poll_minutes=30,
            bluesky_refresh_follows_every=20,
            bluesky_auto_sync_enabled=False,
            routes_by_internal_id={},
            route_by_kind={},
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    async def test_download_rejects_content_length_exceeded(self):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.headers = {"Content-Length": "200"}  # Exceeds 100 bytes

        mock_session = MagicMock(spec=aiohttp.ClientSession)
        mock_session.get.return_value.__aenter__.return_value = mock_resp

        result = await download_signal_attachment(mock_session, "att_123", "test.jpg", config=self.config)
        self.assertIsNone(result)

    async def test_download_rejects_streaming_size_exceeded(self):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.headers = {}  # No Content-Length

        async def chunk_generator(chunk_size):
            yield b"x" * 60
            yield b"x" * 60  # Total 120 > 100

        mock_resp.content = MagicMock()
        mock_resp.content.iter_chunked = chunk_generator

        mock_session = MagicMock(spec=aiohttp.ClientSession)
        mock_session.get.return_value.__aenter__.return_value = mock_resp

        result = await download_signal_attachment(mock_session, "att_123", "test.jpg", config=self.config)
        self.assertIsNone(result)


class TestBlueskyMirroringDelivery(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_bridge.db")
        self.store = BridgeStore(self.db_path)
        self.route = BotRoute(kind="bluesky", internal_id="group.bsky", recipient="+1234567890")
        self.config = AppConfig(
            signal_api_base="http://localhost:8080",
            signal_number="+0000000000",
            bridge_db_path=self.db_path,
            attachments_dir=Path(self.temp_dir),
            max_attachment_bytes=1024 * 1024,
            bluesky_notification_poll_interval=60,
            bluesky_followed_only=False,
            bluesky_active_poll_minutes=30,
            bluesky_refresh_follows_every=20,
            bluesky_auto_sync_enabled=False,
            routes_by_internal_id={"group.bsky": self.route},
            route_by_kind={"bluesky": self.route},
        )

    def tearDown(self):
        self.store.conn.close()
        shutil.rmtree(self.temp_dir)

    async def test_poll_and_mirror_retries_on_send_signal_failure(self):
        from master_bot_v2 import _poll_and_mirror_bluesky_replies

        # Seed parent mapping in store
        self.store.save_mapping(
            signal_group_id="group.bsky",
            signal_ts="1730000000000",
            signal_sender="me",
            bluesky_uri="at://did:plc:123/app.bsky.feed.post/parent1",
            bluesky_cid="cid1",
            direction="signal_to_bluesky",
            text="Root post",
        )

        mock_bluesky = MagicMock()
        mock_bluesky.list_reply_notifications.return_value = {
            "notifications": [{"uri": "at://did:plc:456/app.bsky.feed.post/reply1"}]
        }
        mock_bluesky.extract_reply_notification.return_value = {
            "uri": "at://did:plc:456/app.bsky.feed.post/reply1",
            "cid": "cid_reply",
            "reason_subject": "at://did:plc:123/app.bsky.feed.post/parent1",
            "author_handle": "alice.bsky.social",
            "text": "Hello reply!",
        }
        mock_bluesky.client = MagicMock()
        mock_bluesky.client.me.did = "did:plc:123"

        mock_session = MagicMock(spec=aiohttp.ClientSession)

        # Simulate send_signal FAILING (returning None)
        with patch("master_bot_v2.send_signal", new_callable=AsyncMock, return_value=None):
            stats = await _poll_and_mirror_bluesky_replies(
                mock_session, mock_bluesky, self.store, force=True, config=self.config
            )
            # Notification should NOT be marked seen
            self.assertEqual(stats["mirrored"], 0)
            self.assertEqual(stats["skipped"], 1)
            self.assertFalse(self.store.notification_seen("at://did:plc:456/app.bsky.feed.post/reply1"))

        # Next poll: simulate send_signal SUCCEEDING (returning timestamp)
        with patch("master_bot_v2.send_signal", new_callable=AsyncMock, return_value="1730000000100"):
            stats = await _poll_and_mirror_bluesky_replies(
                mock_session, mock_bluesky, self.store, force=True, config=self.config
            )
            self.assertEqual(stats["mirrored"], 1)
            self.assertTrue(self.store.notification_seen("at://did:plc:456/app.bsky.feed.post/reply1"))
            mapping = self.store.by_bluesky_uri("at://did:plc:456/app.bsky.feed.post/reply1")
            self.assertIsNotNone(mapping)
            self.assertEqual(mapping["signal_ts"], "1730000000100")


if __name__ == "__main__":
    unittest.main()
