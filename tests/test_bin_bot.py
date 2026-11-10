"""Unit tests for BinBot last collection parsing and reminder formatting."""

import unittest
from bin_bot import BinBot


class TestBinBotFormatting(unittest.TestCase):
    def setUp(self):
        self.bot = BinBot()

    def test_recycling_notes(self):
        not_put_out = {
            "type": "Mixed recycling",
            "date": "Thursday, 19th November",
            "last_collection": "Thursday, 5th November, at 9:25am",
            "last_collected": False,
        }
        put_out = {
            "type": "Mixed recycling",
            "date": "Thursday, 19th November",
            "last_collection": "Thursday, 5th November, at 9:25am",
            "last_collected": True,
        }
        self.assertEqual(self.bot.format_last_collection_note(not_put_out), "not put out last time")
        self.assertEqual(
            self.bot.format_last_collection_note(put_out),
            "put out last time — can skip if needed",
        )

    def test_paper_and_general_notes(self):
        paper_skipped = {"type": "Paper and card", "last_collected": False}
        paper_put_out = {"type": "Paper and card", "last_collected": True}
        refuse_put_out = {"type": "Non-recyclable Refuse", "last_collected": True}
        food_put_out = {"type": "Food waste", "last_collected": True}

        self.assertEqual(self.bot.format_last_collection_note(paper_skipped), "not put out last time")
        self.assertEqual(self.bot.format_last_collection_note(paper_put_out), "put out last time")
        self.assertEqual(self.bot.format_last_collection_note(refuse_put_out), "put out last time")
        self.assertEqual(self.bot.format_last_collection_note(food_put_out), "")

    def test_format_reminder_lines(self):
        data = [
            {"type": "Mixed recycling", "date": "Thursday, 19th November", "last_collected": True},
            {"type": "Food waste", "date": "Thursday, 12th November", "last_collected": True},
            {"type": "Paper and card", "date": "Thursday, 12th November", "last_collected": False},
            {"type": "Non-recyclable Refuse", "date": "Thursday, 12th November", "last_collected": True},
        ]
        lines_week_1 = self.bot.format_reminder_lines(
            data, ["Food waste", "Paper and card", "Non-recyclable Refuse"]
        )
        self.assertEqual(
            lines_week_1,
            "• **Food waste**\n"
            "• **Paper and card** *(not put out last time)*\n"
            "• **Non-recyclable Refuse** *(put out last time)*",
        )

        lines_week_2 = self.bot.format_reminder_lines(data, ["Mixed recycling", "Food waste"])
        self.assertEqual(
            lines_week_2,
            "• **Mixed recycling** *(put out last time — can skip if needed)*\n"
            "• **Food waste**",
        )


if __name__ == "__main__":
    unittest.main()
