import asyncio
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

LOGGER = logging.getLogger(__name__)

DEFAULT_CONFIG: Dict[str, Any] = {
    "state_file": "budget_state.json",
    "currency_symbol": "£",
    "weekly_amount": 1.0,
    "max_history": 100,
    "history_display_limit": 20,
    "allow_negative_balance": True,
}

CONFIG_PATHS = [
    Path("config/budget_bot.json"),
    Path("data/budget_bot_config.json"),
    Path("budget_bot_config.json"),
]

DEFAULT_STATE: Dict[str, Any] = {
    "balance": 0.0,
    "weekly_amount": DEFAULT_CONFIG["weekly_amount"],
    "last_weekly_update": datetime.now().strftime("%Y-%m-%d"),
    "history": [],
}


class BudgetConfigError(ValueError):
    pass


def _load_json_file(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        loaded = json.load(f)
    if not isinstance(loaded, dict):
        raise BudgetConfigError(f"{path} must contain a JSON object")
    return loaded


def load_config() -> Dict[str, Any]:
    """Load optional budget config from disk, with environment overrides as a fallback."""
    config = dict(DEFAULT_CONFIG)

    for path in CONFIG_PATHS:
        if path.exists():
            try:
                config.update(_load_json_file(path))
                LOGGER.info("BudgetBot: loaded config from %s", path)
                break
            except Exception as exc:
                LOGGER.warning("BudgetBot: failed to load config from %s: %s", path, exc)
                break

    # Optional compatibility with service-level env vars. No shell exports are required.
    env_state_file = os.getenv("BUDGET_STATE_FILE")
    if env_state_file:
        config["state_file"] = env_state_file

    env_weekly = os.getenv("BUDGET_WEEKLY_AMOUNT")
    if env_weekly:
        config["weekly_amount"] = env_weekly

    env_currency = os.getenv("BUDGET_CURRENCY_SYMBOL")
    if env_currency:
        config["currency_symbol"] = env_currency

    config["state_file"] = str(config.get("state_file") or DEFAULT_CONFIG["state_file"])
    config["currency_symbol"] = str(config.get("currency_symbol") or DEFAULT_CONFIG["currency_symbol"])
    config["weekly_amount"] = float(_parse_money_value(config.get("weekly_amount", 0)))
    config["max_history"] = max(1, int(config.get("max_history", DEFAULT_CONFIG["max_history"])))
    config["history_display_limit"] = max(1, int(config.get("history_display_limit", DEFAULT_CONFIG["history_display_limit"])))
    config["allow_negative_balance"] = bool(config.get("allow_negative_balance", DEFAULT_CONFIG["allow_negative_balance"]))
    return config


def save_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), delete=False) as tmp:
        json.dump(data, tmp, indent=4, ensure_ascii=False)
        tmp.write("\n")
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def _parse_money_value(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if value is None:
        raise ValueError("Missing amount")

    raw = str(value).strip()
    if not raw:
        raise ValueError("Missing amount")

    # Accept values such as £5, +5.50, -3.20, 1,234.56.
    cleaned = raw.replace(",", "")
    cleaned = re.sub(r"^[£$€]\s*", "", cleaned)
    cleaned = re.sub(r"\s*[£$€]$", "", cleaned)

    if not re.fullmatch(r"[+-]?\d+(?:\.\d{1,2})?", cleaned):
        raise ValueError(f"Invalid amount: {raw}")

    try:
        return Decimal(cleaned).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid amount: {raw}") from exc


def _money_to_float(amount: Decimal) -> float:
    return float(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _format_money(amount: Any, symbol: str = "£") -> str:
    dec = _parse_money_value(amount)
    sign = "-" if dec < 0 else ""
    return f"{sign}{symbol}{abs(dec):,.2f}"


def _parse_stored_datetime(value: str) -> datetime:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    return datetime.now()


class BudgetBot:
    def __init__(self):
        self.config = load_config()
        self.state_file = Path(self.config["state_file"])
        self.currency = self.config["currency_symbol"]
        self.state = self.load_state()

    def load_state(self) -> Dict[str, Any]:
        try:
            with self.state_file.open("r", encoding="utf-8") as f:
                loaded = json.load(f)
            if not isinstance(loaded, dict):
                raise ValueError("Budget state must be a JSON object")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            LOGGER.warning("BudgetBot: using default state because %s could not be loaded: %s", self.state_file, exc)
            loaded = dict(DEFAULT_STATE)

        state = dict(DEFAULT_STATE)
        state.update(loaded)

        try:
            state["balance"] = _money_to_float(_parse_money_value(state.get("balance", 0)))
        except ValueError:
            state["balance"] = 0.0

        try:
            state["weekly_amount"] = _money_to_float(_parse_money_value(state.get("weekly_amount", self.config["weekly_amount"])))
        except ValueError:
            state["weekly_amount"] = _money_to_float(_parse_money_value(self.config["weekly_amount"]))

        try:
            datetime.strptime(str(state.get("last_weekly_update")), "%Y-%m-%d")
        except ValueError:
            state["last_weekly_update"] = datetime.now().strftime("%Y-%m-%d")

        if not isinstance(state.get("history"), list):
            state["history"] = []

        state["history"] = state["history"][-self.config["max_history"] :]
        return state

    def save_state(self) -> None:
        self.state["history"] = self.state.get("history", [])[-self.config["max_history"] :]
        save_json_atomic(self.state_file, self.state)

    async def weekly_task(self, alert_callback):
        while True:
            try:
                now = datetime.now()
                last_date = datetime.strptime(self.state["last_weekly_update"], "%Y-%m-%d")
                next_update = last_date + timedelta(days=7)

                if now >= next_update:
                    days_passed = (now - last_date).days
                    weeks = days_passed // 7

                    if weeks > 0:
                        total = Decimal(str(weeks)) * _parse_money_value(self.state["weekly_amount"])
                        self.add_transaction(total, f"Auto-allowance ({weeks} wks)", tx_type="auto_allowance")

                        new_last_update = last_date + timedelta(weeks=weeks)
                        self.state["last_weekly_update"] = new_last_update.strftime("%Y-%m-%d")
                        self.save_state()

                        LOGGER.info("BudgetBot: automatically added %s", _format_money(total, self.currency))
                        await alert_callback(
                            f"💰 **Weekly Allowance**: Added {_format_money(total, self.currency)}. "
                            f"Balance now: {_format_money(self.state['balance'], self.currency)}"
                        )
                        next_update = new_last_update + timedelta(days=7)

                wait_seconds = (next_update - datetime.now()).total_seconds()
                if wait_seconds > 0:
                    LOGGER.info("BudgetBot: sleeping for %.1f hours until next allowance", wait_seconds / 3600)
                    await asyncio.sleep(wait_seconds)
                else:
                    await asyncio.sleep(3600)

            except Exception:
                LOGGER.exception("BudgetBot: error in weekly_task")
                await asyncio.sleep(3600)

    def add_transaction(self, amount: Any, comment: str = "", tx_type: str = "manual") -> float:
        dec_amount = _parse_money_value(amount)
        current = _parse_money_value(self.state.get("balance", 0))
        new_balance = current + dec_amount

        if not self.config["allow_negative_balance"] and new_balance < 0:
            raise ValueError("That would take the budget below zero")

        self.state["balance"] = _money_to_float(new_balance)
        self.state.setdefault("history", []).append(
            {
                "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "amount": _money_to_float(dec_amount),
                "comment": comment.strip() or "Manual entry",
                "type": tx_type,
            }
        )
        self.save_state()
        return self.state["balance"]

    def undo_last_transaction(self) -> Tuple[Dict[str, Any], float]:
        history = self.state.get("history", [])
        if not history:
            raise ValueError("No transactions to undo")

        last = history.pop()
        amount = _parse_money_value(last.get("amount", 0))
        self.state["balance"] = _money_to_float(_parse_money_value(self.state.get("balance", 0)) - amount)
        self.save_state()
        return last, self.state["balance"]

    def set_balance(self, amount: Any, comment: str = "Balance reset") -> float:
        new_balance = _parse_money_value(amount)
        old_balance = _parse_money_value(self.state.get("balance", 0))
        delta = new_balance - old_balance
        self.state["balance"] = _money_to_float(new_balance)
        self.state.setdefault("history", []).append(
            {
                "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "amount": _money_to_float(delta),
                "comment": comment.strip() or "Balance reset",
                "type": "balance_adjustment",
                "balance_after": _money_to_float(new_balance),
            }
        )
        self.save_state()
        return self.state["balance"]

    def _format_history(self, limit: Optional[int] = None) -> str:
        history: List[Dict[str, Any]] = self.state.get("history", [])
        if not history:
            return "📜 No transactions yet."

        display_limit = limit or self.config["history_display_limit"]
        recent = history[-display_limit:]
        total_count = len(history)
        rows: List[Tuple[datetime, Dict[str, Any]]] = [(_parse_stored_datetime(str(item.get("date", ""))), item) for item in recent]
        rows.sort(key=lambda pair: pair[0], reverse=True)

        today = datetime.now().date()
        yesterday = today - timedelta(days=1)
        sections: Dict[str, List[str]] = {}

        for idx, (when, item) in enumerate(rows, start=1):
            amount = _parse_money_value(item.get("amount", 0))
            sign = "+" if amount >= 0 else "-"
            amount_text = f"{sign}{self.currency}{abs(amount):,.2f}"
            comment = str(item.get("comment") or "Manual entry")
            line = f"{idx}. {when.strftime('%H:%M')}  {amount_text}  {comment}"

            if when.date() == today:
                section = "Today"
            elif when.date() == yesterday:
                section = "Yesterday"
            else:
                section = when.strftime("%a %d %b")
            sections.setdefault(section, []).append(line)

        output = [f"📜 Recent budget activity ({min(display_limit, total_count)} of {total_count})"]
        for section, lines in sections.items():
            output.append(f"\n{section}")
            output.extend(lines)

        output.append(f"\nBalance: {_format_money(self.state['balance'], self.currency)}")
        output.append("Undo last entry: /undo")
        return "\n".join(output)

    def _help_text(self) -> str:
        return (
            "📖 *Budget Bot Usage*\n"
            "• /balance - Show current balance\n"
            "• /add [amount] [reason] - Add funds, e.g. /add £10 refund\n"
            "• /sub [amount] [reason] - Spend/withdraw, e.g. /sub 4.50 coffee\n"
            "• /undo - Undo the most recent transaction\n"
            "• /history [n] - Show recent transactions\n"
            "• /set [amount] - Change weekly allowance\n"
            "• /setbalance [amount] - Reset the current balance\n"
            "• /help - Show this menu"
        )

    async def handle_command(self, text):
        parts = text.split()
        if not parts:
            return None
        cmd = parts[0].lower()

        try:
            if cmd in ["/usage", "/help"]:
                return self._help_text()

            if cmd == "/balance":
                return (
                    f"💰 Balance: {_format_money(self.state['balance'], self.currency)}\n"
                    f"Weekly allowance: {_format_money(self.state['weekly_amount'], self.currency)}"
                )

            if cmd == "/history":
                limit = None
                if len(parts) > 1:
                    try:
                        limit = max(1, min(int(parts[1]), self.config["max_history"]))
                    except ValueError:
                        return "⚠️ Invalid history limit. Use: /history 20"
                return self._format_history(limit)

            if cmd == "/undo":
                last, new_balance = self.undo_last_transaction()
                return (
                    f"↩️ Undid { _format_money(last.get('amount', 0), self.currency) } "
                    f"({last.get('comment', 'Manual entry')}).\n"
                    f"Balance now: {_format_money(new_balance, self.currency)}"
                )

            if cmd in ["/add", "/sub", "/withdraw"]:
                if len(parts) < 2:
                    return f"⚠️ Missing amount. Use: {cmd} 5.00 coffee"
                amount = _parse_money_value(parts[1])
                comment = " ".join(parts[2:]) if len(parts) > 2 else ""

                if cmd in ["/sub", "/withdraw"]:
                    amount = -abs(amount)
                    action = "Subtracted"
                else:
                    amount = abs(amount)
                    action = "Added"

                self.add_transaction(amount, comment)
                return f"✅ {action} {_format_money(abs(amount), self.currency)}. New balance: {_format_money(self.state['balance'], self.currency)}"

            if cmd in ["/set", "/setweekly"]:
                if len(parts) < 2:
                    return "⚠️ Missing amount. Use: /set 25"
                weekly = _parse_money_value(parts[1])
                if weekly < 0:
                    return "⚠️ Weekly allowance cannot be negative."
                self.state["weekly_amount"] = _money_to_float(weekly)
                self.save_state()
                return f"⚙️ Weekly amount set to {_format_money(weekly, self.currency)}"

            if cmd in ["/setbalance", "/resetbalance"]:
                if len(parts) < 2:
                    return "⚠️ Missing amount. Use: /setbalance 50"
                comment = " ".join(parts[2:]) if len(parts) > 2 else "Balance reset"
                new_balance = self.set_balance(parts[1], comment)
                return f"⚙️ Balance set to {_format_money(new_balance, self.currency)}"

        except ValueError as exc:
            return f"⚠️ {exc}"
        except Exception as exc:
            LOGGER.exception("BudgetBot: command failed")
            return f"⚠️ Error: {str(exc)}"

        return None
