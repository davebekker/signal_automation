import os
import re
import json
import time
import random
import secrets
import sqlite3
import asyncio
import calendar
import datetime as dt
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import dateparser
from dateparser.search import search_dates

try:
    from google import genai
    from google.genai import types
except Exception:  # pragma: no cover - optional dependency
    genai = None
    types = None

from utils.tools import logger

WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

TIME_WORDS = {
    "morning": "09:00",
    "afternoon": "14:00",
    "evening": "18:00",
    "tonight": "20:00",
    "noon": "12:00",
    "midday": "12:00",
    "midnight": "00:00",
}



DEFAULT_REMINDER_CONFIG_PATHS = (
    Path("config/reminder_bot.json"),
    Path("data/reminder_bot_config.json"),
    Path("reminder_bot_config.json"),
)


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return default


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _read_json_config(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            logger.warning("Ignoring %s because it does not contain a JSON object", path)
            return {}
        return raw
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        logger.exception("Ignoring invalid JSON in %s", path)
        return {}
    except OSError:
        logger.exception("Unable to read reminder config from %s", path)
        return {}


def _load_reminder_config() -> Dict[str, Any]:
    """
    Load ReminderBot settings without requiring shell exports.

    Precedence, lowest to highest:
      1. built-in defaults
      2. first existing JSON config file from config/reminder_bot.json,
         data/reminder_bot_config.json, or reminder_bot_config.json
      3. environment variables, if present, for backwards compatibility

    Example config/reminder_bot.json:
      {
        "llm_enabled": true,
        "gemini_api_key": "...",
        "llm_model": "gemini-2.5-flash",
        "llm_timeout_seconds": 8,
        "llm_auto_confirm_threshold": 0.75,
        "llm_confirmation_threshold": 0.45,
        "confirmation_expiry_minutes": 30,
        "db_path": "data/reminders.sqlite"
      }
    """
    cfg: Dict[str, Any] = {
        "llm_enabled": False,
        "llm_model": "gemini-2.5-flash",
        "llm_timeout_seconds": 8.0,
        "llm_auto_confirm_threshold": 0.75,
        "llm_confirmation_threshold": 0.45,
        "confirmation_expiry_minutes": 30,
        "gemini_api_key": None,
        "db_path": os.path.join("data", "reminders.sqlite"),
    }

    explicit_path = os.getenv("REMINDER_CONFIG_PATH")
    candidate_paths = [Path(explicit_path)] if explicit_path else list(DEFAULT_REMINDER_CONFIG_PATHS)
    loaded_from: Optional[Path] = None
    for path in candidate_paths:
        file_cfg = _read_json_config(path)
        if file_cfg:
            cfg.update(file_cfg)
            loaded_from = path
            break
    if loaded_from:
        logger.info("Loaded ReminderBot config from %s", loaded_from)

    env_map = {
        "REMINDER_DB_PATH": "db_path",
        "REMINDER_LLM_ENABLED": "llm_enabled",
        "REMINDER_LLM_MODEL": "llm_model",
        "REMINDER_LLM_TIMEOUT_SECONDS": "llm_timeout_seconds",
        "REMINDER_LLM_AUTO_CONFIRM_THRESHOLD": "llm_auto_confirm_threshold",
        "REMINDER_LLM_CONFIRMATION_THRESHOLD": "llm_confirmation_threshold",
        "REMINDER_CONFIRMATION_EXPIRY_MINUTES": "confirmation_expiry_minutes",
        "GEMINI_API_KEY": "gemini_api_key",
    }
    for env_name, cfg_name in env_map.items():
        if env_name in os.environ:
            cfg[cfg_name] = os.environ[env_name]

    cfg["llm_enabled"] = _coerce_bool(cfg.get("llm_enabled"), default=False)
    cfg["llm_timeout_seconds"] = _coerce_float(cfg.get("llm_timeout_seconds"), default=8.0)
    cfg["llm_auto_confirm_threshold"] = _coerce_float(cfg.get("llm_auto_confirm_threshold"), default=0.75)
    cfg["llm_confirmation_threshold"] = _coerce_float(cfg.get("llm_confirmation_threshold"), default=0.45)
    cfg["confirmation_expiry_minutes"] = _coerce_float(cfg.get("confirmation_expiry_minutes"), default=30.0)
    cfg["llm_model"] = str(cfg.get("llm_model") or "gemini-2.5-flash")
    cfg["db_path"] = str(cfg.get("db_path") or os.path.join("data", "reminders.sqlite"))
    return cfg


ORDINALS = {
    "1st": 1, "first": 1,
    "2nd": 2, "second": 2,
    "3rd": 3, "third": 3,
    "4th": 4, "fourth": 4,
    "5th": 5, "fifth": 5,
    "6th": 6, "sixth": 6,
    "7th": 7, "seventh": 7,
    "8th": 8, "eighth": 8,
    "9th": 9, "ninth": 9,
    "10th": 10, "tenth": 10,
    "11th": 11, "eleventh": 11,
    "12th": 12, "twelfth": 12,
    "13th": 13, "thirteenth": 13,
    "14th": 14, "fourteenth": 14,
    "15th": 15, "fifteenth": 15,
    "16th": 16, "sixteenth": 16,
    "17th": 17, "seventeenth": 17,
    "18th": 18, "eighteenth": 18,
    "19th": 19, "nineteenth": 19,
    "20th": 20, "twentieth": 20,
    "21st": 21, "twenty first": 21,
    "22nd": 22, "twenty second": 22,
    "23rd": 23, "twenty third": 23,
    "24th": 24, "twenty fourth": 24,
    "25th": 25, "twenty fifth": 25,
    "26th": 26, "twenty sixth": 26,
    "27th": 27, "twenty seventh": 27,
    "28th": 28, "twenty eighth": 28,
    "29th": 29, "twenty ninth": 29,
    "30th": 30, "thirtieth": 30,
    "31st": 31, "thirty first": 31,
}


class ReminderBot:
    """
    SQLite-backed reminder bot.

    Existing commands kept:
      /remind [time] | [message]
      /list
      /del [number]
      /usage or /help

    New commands:
      /remind me to [task] [time phrase]
      /birthday [person] | [date] [| remind 1 week before]
      /recur [recurrence phrase] | [message]
      /checkin [person] | [cadence]
      /flowers [person] | [cadence]
      /plans
      /renew [plan id or person]
      /stop [plan id or person]

    Data is stored in data/reminders.sqlite. If an old data/reminders.json file is
    found and the SQLite DB is empty, one-off reminders are imported automatically.
    """

    def __init__(self):
        os.makedirs("data", exist_ok=True)
        self.config = _load_reminder_config()
        self.db_file = self.config["db_path"]
        self.legacy_json_file = os.path.join("data", "reminders.json")
        self.conn = sqlite3.connect(self.db_file, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

        self.llm_enabled = bool(self.config.get("llm_enabled"))
        self.llm_model = self.config["llm_model"]
        self.llm_timeout_seconds = self.config["llm_timeout_seconds"]
        self.llm_auto_confirm_threshold = float(self.config.get("llm_auto_confirm_threshold", 0.75))
        self.llm_confirmation_threshold = float(self.config.get("llm_confirmation_threshold", 0.45))
        self.confirmation_expiry_minutes = float(self.config.get("confirmation_expiry_minutes", 30.0))
        self.gemini_api_key = self.config.get("gemini_api_key")
        self.gemini_client = None
        if self.llm_enabled and not self.gemini_api_key:
            logger.warning("ReminderBot LLM parser is enabled but no Gemini API key was found; deterministic parser will be used")
        if self.llm_enabled and self.gemini_api_key and genai is not None:
            try:
                self.gemini_client = genai.Client(api_key=self.gemini_api_key)
                logger.info("ReminderBot natural-language parser enabled with %s", self.llm_model)
            except Exception:
                logger.exception("Failed to initialise Gemini reminder parser; deterministic parser will be used")
                self.gemini_client = None
        elif self.llm_enabled and genai is None:
            logger.warning("ReminderBot LLM parser requested, but google-genai is not installed; deterministic parser will be used")

        self._init_db()
        self._migrate_legacy_json_if_needed()

    # ---------------------------------------------------------------------
    # DB setup and helpers
    # ---------------------------------------------------------------------

    def _init_db(self) -> None:
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self.conn.execute("PRAGMA foreign_keys=ON;")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                due_at TEXT NOT NULL,
                task TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'once',
                status TEXT NOT NULL DEFAULT 'pending',
                plan_id INTEGER,
                source_text TEXT,
                payload_json TEXT,
                created_at TEXT NOT NULL,
                sent_at TEXT,
                FOREIGN KEY(plan_id) REFERENCES plans(id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_reminders_due
                ON reminders(status, due_at);

            CREATE INDEX IF NOT EXISTS idx_reminders_plan
                ON reminders(plan_id, status, due_at);

            CREATE TABLE IF NOT EXISTS plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                name TEXT,
                action TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                config_json TEXT,
                generated_until TEXT,
                renewal_prompted_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_plans_kind_active
                ON plans(kind, active);

            CREATE TABLE IF NOT EXISTS pending_confirmations (
                token TEXT PRIMARY KEY,
                intent TEXT NOT NULL,
                parsed_json TEXT NOT NULL,
                source_text TEXT NOT NULL,
                preview TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_pending_confirmations_expires
                ON pending_confirmations(expires_at);
            """
        )
        self.conn.commit()

    def _migrate_legacy_json_if_needed(self) -> None:
        existing = self.conn.execute("SELECT COUNT(1) AS n FROM reminders").fetchone()["n"]
        if existing or not os.path.exists(self.legacy_json_file):
            return
        try:
            with open(self.legacy_json_file, "r", encoding="utf-8") as f:
                legacy = json.load(f)
        except Exception:
            logger.exception("Failed to read legacy reminders.json")
            return
        if not isinstance(legacy, list):
            return
        imported = 0
        for item in legacy:
            if not isinstance(item, dict):
                continue
            due_at = item.get("time")
            task = item.get("task")
            if not due_at or not task:
                continue
            self._insert_reminder(
                due_at=due_at,
                task=str(task),
                kind="once",
                source_text="migrated from reminders.json",
            )
            imported += 1
        if imported:
            logger.info("Imported %s legacy reminders from reminders.json into SQLite", imported)

    def _now(self) -> dt.datetime:
        return dt.datetime.now().replace(microsecond=0)

    def _now_iso(self) -> str:
        return self._now().isoformat()

    def _json_dump(self, data: Optional[Dict[str, Any]]) -> Optional[str]:
        if data is None:
            return None
        return json.dumps(data, ensure_ascii=False, sort_keys=True)

    def _json_load(self, value: Optional[str]) -> Dict[str, Any]:
        if not value:
            return {}
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    def _insert_reminder(
        self,
        *,
        due_at: str,
        task: str,
        kind: str = "once",
        plan_id: Optional[int] = None,
        source_text: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO reminders (
                due_at, task, kind, status, plan_id, source_text, payload_json, created_at
            )
            VALUES (?, ?, ?, 'pending', ?, ?, ?, ?)
            """,
            (
                due_at,
                task.strip(),
                kind,
                plan_id,
                source_text,
                self._json_dump(payload),
                self._now_iso(),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def _insert_plan(
        self,
        *,
        kind: str,
        name: str,
        action: str,
        config: Dict[str, Any],
        generated_until: Optional[str] = None,
    ) -> int:
        now = self._now_iso()
        cur = self.conn.execute(
            """
            INSERT INTO plans (
                kind, name, action, active, config_json, generated_until,
                renewal_prompted_at, created_at, updated_at
            )
            VALUES (?, ?, ?, 1, ?, ?, NULL, ?, ?)
            """,
            (kind, name.strip(), action.strip(), self._json_dump(config), generated_until, now, now),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def _update_plan(self, plan_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = self._now_iso()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values()) + [plan_id]
        self.conn.execute(f"UPDATE plans SET {assignments} WHERE id = ?", values)
        self.conn.commit()

    # ---------------------------------------------------------------------
    # Formatting
    # ---------------------------------------------------------------------

    def _parse_iso(self, value: str) -> dt.datetime:
        return dt.datetime.fromisoformat(value)

    def _format_due(self, value: str) -> str:
        d = self._parse_iso(value)
        now = self._now()
        if d.date() == now.date():
            return d.strftime("today %H:%M")
        if d.date() == (now.date() + dt.timedelta(days=1)):
            return d.strftime("tomorrow %H:%M")
        return d.strftime("%d %b %Y, %H:%M")

    def _format_short_due(self, value: str) -> str:
        return self._parse_iso(value).strftime("%d %b, %H:%M")

    def _normalize_text(self, value: str) -> str:
        return " ".join((value or "").strip().split())

    # ---------------------------------------------------------------------
    # Date/time parsing
    # ---------------------------------------------------------------------

    def _dateparser_settings(self) -> Dict[str, Any]:
        return {
            "PREFER_DATES_FROM": "future",
            "PREFER_DAY_OF_MONTH": "first",
            "RELATIVE_BASE": self._now(),
        }

    def _preprocess_time_phrase(self, phrase: str) -> str:
        out = phrase.strip()
        lower = out.lower()
        for word, replacement in TIME_WORDS.items():
            pattern = r"\b" + re.escape(word) + r"\b"
            if re.search(pattern, lower):
                out = re.sub(pattern, replacement, out, flags=re.IGNORECASE)
                lower = out.lower()
        out = re.sub(r"\bafter work\b", "18:00", out, flags=re.IGNORECASE)
        out = re.sub(r"\bbefore work\b", "08:00", out, flags=re.IGNORECASE)
        out = re.sub(r"\blunchtime\b", "12:30", out, flags=re.IGNORECASE)
        return out

    def _parse_datetime(self, phrase: str) -> Optional[dt.datetime]:
        cleaned = self._preprocess_time_phrase(phrase)
        parsed = dateparser.parse(cleaned, settings=self._dateparser_settings())
        if not parsed:
            return None
        parsed = parsed.replace(microsecond=0)
        if parsed <= self._now():
            if re.search(r"\b\d{1,2}(:\d{2})?\s*(am|pm)?\b", cleaned, flags=re.IGNORECASE):
                parsed = parsed + dt.timedelta(days=1)
        return parsed

    def _parse_time_of_day(self, text: str, default: str = "09:00") -> Tuple[int, int]:
        text = text.strip().lower()
        for word, replacement in TIME_WORDS.items():
            if re.search(r"\b" + re.escape(word) + r"\b", text):
                hour, minute = replacement.split(":")
                return int(hour), int(minute)
        after_at = re.search(r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", text, flags=re.IGNORECASE)
        direct = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", text, flags=re.IGNORECASE)
        m = after_at or direct
        if m:
            hour = int(m.group(1))
            minute = int(m.group(2) or 0)
            suffix = (m.group(3) or "").lower()
            if suffix == "pm" and hour != 12:
                hour += 12
            if suffix == "am" and hour == 12:
                hour = 0
            return hour, minute
        hour, minute = default.split(":")
        return int(hour), int(minute)

    def _extract_natural_reminder(self, raw: str) -> Tuple[Optional[dt.datetime], Optional[str], Optional[str]]:
        body = raw.replace("/remind", "", 1).strip()
        if "|" in body:
            time_phrase, task = body.split("|", 1)
            target = self._parse_datetime(time_phrase.strip())
            task = self._normalize_text(task)
            if not target:
                return None, None, f"❓ Unsure when '{time_phrase.strip()}' is."
            if not task:
                return None, None, "❓ I found the time, but not the reminder text."
            return target, task, None

        colon_match = re.match(r"(.+?)\s*:\s*(.+)$", body)
        if colon_match:
            maybe_time = colon_match.group(1).strip()
            maybe_task = colon_match.group(2).strip()
            target = self._parse_datetime(maybe_time)
            if target and maybe_task:
                return target, self._normalize_text(maybe_task), None

        cleaned = re.sub(r"^me\s+", "", body, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"^to\s+", "", cleaned, flags=re.IGNORECASE).strip()
        found = search_dates(
            self._preprocess_time_phrase(cleaned),
            settings=self._dateparser_settings(),
            languages=["en"],
        )
        if not found:
            return None, None, (
                "💡 Try `/remind tomorrow 8pm | water plants` or "
                "`/remind me to water plants tomorrow at 8pm`."
            )
        date_phrase, target = max(found, key=lambda pair: len(pair[0]))
        target = target.replace(microsecond=0)
        task = cleaned
        task = re.sub(re.escape(date_phrase), "", task, count=1, flags=re.IGNORECASE)
        task = re.sub(r"\b(remind|me|to)\b", "", task, flags=re.IGNORECASE)
        task = re.sub(r"\s+", " ", task)
        task = task.strip(" -:,")
        task = self._normalize_text(task)
        if not task:
            return None, None, "❓ I found the time, but not the reminder text."
        return target, task, None

    # ---------------------------------------------------------------------
    # Birthday plans
    # ---------------------------------------------------------------------

    def _parse_lead_offsets(self, phrase: Optional[str]) -> List[int]:
        offsets = {0}
        if not phrase:
            return sorted(offsets, reverse=True)
        text = phrase.lower()
        for n, unit in re.findall(r"(\d+)\s*(day|days|week|weeks|month|months)\s*before", text):
            value = int(n)
            if unit.startswith("week"):
                offsets.add(value * 7)
            elif unit.startswith("month"):
                offsets.add(value * 30)
            else:
                offsets.add(value)
        if "week before" in text:
            offsets.add(7)
        if "day before" in text:
            offsets.add(1)
        if "month before" in text:
            offsets.add(30)
        return sorted(offsets, reverse=True)

    def _schedule_birthday_occurrences(
        self,
        *,
        plan_id: int,
        person: str,
        month: int,
        day: int,
        offsets: List[int],
        years_ahead: int = 2,
    ) -> int:
        created = 0
        now = self._now()
        for year in range(now.year, now.year + years_ahead + 1):
            last_day = calendar.monthrange(year, month)[1]
            safe_day = min(day, last_day)
            birthday = dt.datetime(year, month, safe_day, 9, 0)
            for offset in offsets:
                due = birthday - dt.timedelta(days=offset)
                if due <= now:
                    continue
                if offset == 0:
                    task = f"{person}'s birthday"
                elif offset == 1:
                    task = f"{person}'s birthday is tomorrow"
                else:
                    task = f"{person}'s birthday is in {offset} days"
                self._insert_reminder(
                    due_at=due.isoformat(),
                    task=task,
                    kind="birthday",
                    plan_id=plan_id,
                    payload={"person": person, "month": month, "day": day, "offset_days": offset},
                )
                created += 1
        return created

    async def _cmd_birthday(self, text: str) -> str:
        body = text.replace("/birthday", "", 1).strip()
        if "|" not in body:
            return "💡 Format: `/birthday Person | 12 June` or `/birthday Person | 12 June | 1 week before`"
        bits = [b.strip() for b in body.split("|")]
        person = self._normalize_text(bits[0])
        date_phrase = bits[1] if len(bits) > 1 else ""
        lead_phrase = bits[2] if len(bits) > 2 else None
        if not person or not date_phrase:
            return "💡 Format: `/birthday Person | 12 June`"
        parsed = self._parse_datetime(date_phrase)
        if not parsed:
            return f"❓ Unsure what birthday date '{date_phrase}' is."
        offsets = self._parse_lead_offsets(lead_phrase)
        config = {"person": person, "month": parsed.month, "day": parsed.day, "offset_days": offsets}
        plan_id = self._insert_plan(kind="birthday", name=person, action=f"{person}'s birthday", config=config)
        created = self._schedule_birthday_occurrences(
            plan_id=plan_id,
            person=person,
            month=parsed.month,
            day=parsed.day,
            offsets=offsets,
            years_ahead=2,
        )
        offset_label = ", ".join("on the day" if o == 0 else f"{o} days before" for o in offsets)
        return f"🎂 Birthday reminder set for {person} ({parsed.strftime('%d %b')}); scheduled {created} reminder(s), {offset_label}."

    # ---------------------------------------------------------------------
    # Recurring reminders
    # ---------------------------------------------------------------------

    def _parse_month_day_number(self, text: str) -> Optional[int]:
        text = text.lower()
        for key, value in ORDINALS.items():
            if re.search(r"\b" + re.escape(key) + r"\b", text):
                return value
        m = re.search(r"\b([1-9]|[12]\d|3[01])(?:st|nd|rd|th)?\b", text)
        if m:
            return int(m.group(1))
        return None

    def _parse_recurrence(self, phrase: str) -> Optional[Dict[str, Any]]:
        p = phrase.lower().strip()
        hour, minute = self._parse_time_of_day(p, default="09:00")
        interval = 1
        m_interval = re.search(r"\bevery\s+(\d+)\s+(day|days|week|weeks|month|months|year|years)\b", p)
        if m_interval:
            interval = int(m_interval.group(1))
        if "daily" in p or re.search(r"\bevery\s+day\b", p):
            return {"freq": "daily", "interval": interval, "hour": hour, "minute": minute}
        weekdays = [num for name, num in WEEKDAYS.items() if re.search(r"\b" + re.escape(name) + r"\b", p)]
        if weekdays or "weekly" in p or "week" in p:
            return {
                "freq": "weekly",
                "interval": interval,
                "weekdays": sorted(set(weekdays)) or [self._now().weekday()],
                "hour": hour,
                "minute": minute,
            }
        if "monthly" in p or "month" in p:
            day = self._parse_month_day_number(p) or self._now().day
            return {"freq": "monthly", "interval": interval, "day": day, "hour": hour, "minute": minute}
        if "yearly" in p or "annually" in p or "every year" in p or "year" in p:
            after_on = p.split(" on ", 1)[1] if " on " in p else p
            parsed = self._parse_datetime(after_on)
            if parsed:
                return {"freq": "yearly", "interval": interval, "month": parsed.month, "day": parsed.day, "hour": hour, "minute": minute}
        return None

    def _next_due_for_recurrence(self, config: Dict[str, Any], after: Optional[dt.datetime] = None) -> dt.datetime:
        after = after or self._now()
        freq = config.get("freq")
        hour = int(config.get("hour", 9))
        minute = int(config.get("minute", 0))
        interval = max(1, int(config.get("interval", 1)))
        if freq == "daily":
            candidate = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate <= after:
                candidate += dt.timedelta(days=interval)
            return candidate
        if freq == "weekly":
            weekdays = sorted(set(int(x) for x in (config.get("weekdays") or [after.weekday()])))
            anchor_raw = config.get("anchor_date")
            anchor_date = None
            if anchor_raw:
                try:
                    anchor_date = dt.date.fromisoformat(str(anchor_raw))
                except ValueError:
                    anchor_date = None
            search_days = max(15, 7 * interval * 3 + 8)
            for days_ahead in range(0, search_days):
                candidate_date = after.date() + dt.timedelta(days=days_ahead)
                if candidate_date.weekday() not in weekdays:
                    continue
                if anchor_date and interval > 1:
                    weeks_since_anchor = max(0, (candidate_date - anchor_date).days // 7)
                    if weeks_since_anchor % interval != 0:
                        continue
                candidate = dt.datetime.combine(candidate_date, dt.time(hour, minute))
                if candidate > after:
                    return candidate
            return after + dt.timedelta(days=7 * interval)
        if freq == "monthly":
            day = int(config.get("day", 1))
            year = after.year
            month = after.month
            for _ in range(0, 36):
                last_day = calendar.monthrange(year, month)[1]
                candidate = dt.datetime(year, month, min(day, last_day), hour, minute)
                if candidate > after:
                    return candidate
                month += interval
                while month > 12:
                    month -= 12
                    year += 1
        if freq == "yearly":
            month = int(config.get("month", after.month))
            day = int(config.get("day", after.day))
            year = after.year
            while True:
                last_day = calendar.monthrange(year, month)[1]
                candidate = dt.datetime(year, month, min(day, last_day), hour, minute)
                if candidate > after:
                    return candidate
                year += interval
        return (after + dt.timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)

    async def _cmd_recur(self, text: str) -> str:
        body = text.replace("/recur", "", 1).strip()
        if "|" not in body:
            return "💡 Format: `/recur every Monday at 9am | submit timesheet`"
        recurrence_phrase, task = body.split("|", 1)
        recurrence_phrase = recurrence_phrase.strip()
        task = self._normalize_text(task)
        if not task:
            return "❓ I found the recurrence, but not the reminder text."
        config = self._parse_recurrence(recurrence_phrase)
        if not config:
            return f"❓ I couldn't understand the recurrence '{recurrence_phrase}'. Try `every Monday at 9am`, `monthly on the 1st`, or `daily at 8am`."
        config.setdefault("anchor_date", self._now().date().isoformat())
        next_due = self._next_due_for_recurrence(config)
        plan_id = self._insert_plan(kind="recurring", name=task, action=task, config=config)
        self._insert_reminder(due_at=next_due.isoformat(), task=task, kind="recurring", plan_id=plan_id, source_text=text, payload={"recurrence": config})
        return f"🔁 Recurring reminder set: {task}\nNext: {self._format_due(next_due.isoformat())}"

    def _advance_recurring_plan(self, reminder: sqlite3.Row) -> None:
        plan_id = reminder["plan_id"]
        if not plan_id:
            return
        plan = self.conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
        if not plan or not plan["active"]:
            return
        config = self._json_load(plan["config_json"])
        after = max(self._now(), self._parse_iso(reminder["due_at"])) + dt.timedelta(seconds=1)
        next_due = self._next_due_for_recurrence(config, after=after)
        self._insert_reminder(due_at=next_due.isoformat(), task=plan["action"], kind="recurring", plan_id=plan_id, payload={"recurrence": config})

    # ---------------------------------------------------------------------
    # Interaction plans
    # ---------------------------------------------------------------------

    def _parse_cadence(self, phrase: str) -> Dict[str, Any]:
        p = phrase.lower().strip()
        if not p:
            return {"count": 6, "period_days": 365, "label": "6 per year"}
        if "monthly" in p or "monthly-ish" in p or "once a month" in p:
            return {"count": 12, "period_days": 365, "label": phrase}
        if "quarterly" in p:
            return {"count": 4, "period_days": 365, "label": phrase}
        m = re.search(r"(\d+)\s*(?:x|times)?\s*(?:per|/|a)\s*(year|yr|month|mo)", p)
        if m:
            count = int(m.group(1))
            unit = m.group(2)
            if unit in ("month", "mo"):
                return {"count": count * 12, "period_days": 365, "label": phrase}
            return {"count": count, "period_days": 365, "label": phrase}
        m = re.search(r"every\s+(\d+)\s+(weeks|week|months|month)", p)
        if m:
            n = max(1, int(m.group(1)))
            unit = m.group(2)
            count = max(1, round(52 / n)) if unit.startswith("week") else max(1, round(12 / n))
            return {"count": count, "period_days": 365, "label": phrase}
        m = re.search(r"\b(\d+)\b", p)
        if m:
            return {"count": int(m.group(1)), "period_days": 365, "label": phrase}
        return {"count": 6, "period_days": 365, "label": phrase or "6 per year"}

    def _generate_interaction_dates(self, *, count: int, period_days: int, start: Optional[dt.datetime] = None, seed: Optional[int] = None) -> List[dt.datetime]:
        start = start or (self._now() + dt.timedelta(days=3))
        count = max(1, min(52, int(count)))
        period_days = max(count, int(period_days))
        rnd = random.Random(seed if seed is not None else time.time_ns())
        dates: List[dt.datetime] = []
        window = period_days / count
        for i in range(count):
            window_start = int(round(i * window))
            window_end = int(round((i + 1) * window)) - 1
            if window_end < window_start:
                window_end = window_start
            if window_end - window_start > 8:
                window_start += 2
                window_end -= 2
            day_offset = rnd.randint(window_start, window_end)
            hour = rnd.randint(9, 18)
            minute = rnd.choice([0, 15, 30, 45])
            candidate = (start + dt.timedelta(days=day_offset)).replace(hour=hour, minute=minute, second=0, microsecond=0)
            dates.append(candidate)
        dates.sort()
        cleaned: List[dt.datetime] = []
        used_dates: set[dt.date] = set()
        for candidate in dates:
            while candidate.date() in used_dates:
                candidate += dt.timedelta(days=1)
            used_dates.add(candidate.date())
            cleaned.append(candidate)
        return cleaned

    def _schedule_interaction_batch(self, plan_id: int, *, from_date: Optional[dt.datetime] = None) -> int:
        plan = self.conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
        if not plan:
            return 0
        config = self._json_load(plan["config_json"])
        count = int(config.get("count", 6))
        period_days = int(config.get("period_days", 365))
        action = plan["action"]
        name = plan["name"]
        seed = random.randint(0, 2**31 - 1)
        dates = self._generate_interaction_dates(count=count, period_days=period_days, start=from_date or (self._now() + dt.timedelta(days=3)), seed=seed)
        for due in dates:
            self._insert_reminder(due_at=due.isoformat(), task=action, kind="interaction", plan_id=plan_id, payload={"person": name, "batch_seed": seed})
        generated_until = max(dates).isoformat() if dates else None
        self._update_plan(plan_id, generated_until=generated_until, renewal_prompted_at=None)
        return len(dates)

    async def _cmd_interaction(self, text: str, *, command: str) -> str:
        body = text.replace(command, "", 1).strip()
        if "|" in body:
            name_part, cadence_part = body.split("|", 1)
        else:
            name_part, cadence_part = body, "6 per year"
        person = self._normalize_text(name_part)
        cadence = self._parse_cadence(cadence_part)
        if not person:
            return f"💡 Format: `{command} Person | 6 per year`"
        action = f"Buy flowers for {person}" if command == "/flowers" else f"Check in with {person}"
        config = {"person": person, "action": action, "count": cadence["count"], "period_days": cadence["period_days"], "cadence_label": cadence["label"]}
        plan_id = self._insert_plan(kind="interaction", name=person, action=action, config=config)
        created = self._schedule_interaction_batch(plan_id)
        return f"🌱 Set up {created} reminder(s) for {person}: {action} ({cadence['label']})."

    def _find_plan(self, query: str) -> Optional[sqlite3.Row]:
        q = query.strip()
        if not q:
            return None
        if q.isdigit():
            row = self.conn.execute("SELECT * FROM plans WHERE id = ?", (int(q),)).fetchone()
            if row:
                return row
        like = f"%{q.lower()}%"
        return self.conn.execute(
            """
            SELECT * FROM plans
            WHERE active = 1
              AND (lower(name) LIKE ? OR lower(action) LIKE ?)
            ORDER BY id DESC
            LIMIT 1
            """,
            (like, like),
        ).fetchone()

    async def _cmd_renew(self, text: str) -> str:
        query = self._normalize_text(text.replace("/renew", "", 1))
        plan = self._find_plan(query)
        if not plan:
            return "❓ I couldn't find that active plan. Try `/plans`."
        if plan["kind"] != "interaction":
            return "ℹ️ Only interaction plans need manual renewal. Recurring and birthday reminders renew themselves."
        generated_until = plan["generated_until"]
        if generated_until:
            try:
                start = max(self._now() + dt.timedelta(days=3), self._parse_iso(generated_until) + dt.timedelta(days=1))
            except Exception:
                start = self._now() + dt.timedelta(days=3)
        else:
            start = self._now() + dt.timedelta(days=3)
        created = self._schedule_interaction_batch(int(plan["id"]), from_date=start)
        return f"🔁 Renewed {plan['name']}: generated {created} more reminder(s)."

    async def _cmd_stop(self, text: str) -> str:
        query = self._normalize_text(text.replace("/stop", "", 1))
        plan = self._find_plan(query)
        if not plan:
            return "❓ I couldn't find that active plan. Try `/plans`."
        self._update_plan(int(plan["id"]), active=0)
        self.conn.execute("UPDATE reminders SET status = 'dismissed' WHERE plan_id = ? AND status = 'pending'", (int(plan["id"]),))
        self.conn.commit()
        return f"🛑 Stopped plan #{plan['id']}: {plan['action']}"

    async def _cmd_plans(self) -> str:
        rows = self.conn.execute(
            """
            SELECT p.*,
                (SELECT COUNT(1) FROM reminders r WHERE r.plan_id = p.id AND r.status = 'pending') AS pending_count,
                (SELECT MIN(r.due_at) FROM reminders r WHERE r.plan_id = p.id AND r.status = 'pending') AS next_due
            FROM plans p
            WHERE p.active = 1
            ORDER BY p.kind, p.name
            """
        ).fetchall()
        if not rows:
            return "📭 No active recurring plans."
        lines = ["🧭 **Active Plans:**"]
        for row in rows:
            next_due = f", next {self._format_short_due(row['next_due'])}" if row["next_due"] else ", no pending dates"
            lines.append(f"{row['id']}. **{row['kind']}** — {row['action']} ({row['pending_count']} pending{next_due})")
        lines.append("\nUse `/renew [id/name]` for interaction plans or `/stop [id/name]` to disable one.")
        return "\n".join(lines)


    # ---------------------------------------------------------------------
    # Optional Gemini natural-language parsing
    # ---------------------------------------------------------------------

    def _llm_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "enum": ["once", "recurring", "birthday", "checkin", "flowers", "list", "plans", "delete", "renew", "stop", "help", "unknown"],
                },
                "task": {"type": "string"},
                "due_at": {"type": "string"},
                "recurrence_phrase": {"type": "string"},
                "person": {"type": "string"},
                "date_phrase": {"type": "string"},
                "lead_phrase": {"type": "string"},
                "cadence_phrase": {"type": "string"},
                "target": {"type": "string"},
                "delete_number": {"type": "integer"},
                "confidence": {"type": "number"},
                "clarification": {"type": "string"},
            },
            "required": ["intent", "confidence"],
        }

    def _llm_prompt(self, text: str) -> str:
        now = self._now()
        return f"""
You parse reminder-bot messages into a small JSON object.

Current local datetime: {now.isoformat()}
Current local weekday: {now.strftime('%A')}

Rules:
- Preserve the user's reminder wording in task; remove only scheduling words.
- Prefer a single one-off reminder when the user gives one due date/time.
- For relative dates, calculate due_at as a local ISO datetime without timezone, e.g. 2026-06-21T09:00:00.
- If the user says only a date without a time, use 09:00.
- If the user says morning use 09:00, afternoon 14:00, evening 18:00, tonight 20:00, noon 12:00.
- If the request is recurring, put the schedule in recurrence_phrase using simple wording like "every Monday at 9am", "every 2 weeks on Tuesday at 18:00", "monthly on the 1st at 9am", or "daily at 8am".
- If it is a birthday, set person, date_phrase, and optional lead_phrase. Do not set due_at.
- If it is a check-in or flowers plan, set person and cadence_phrase.
- If the request is ambiguous, use intent "unknown" and put a short clarification.
- Do not invent missing people, tasks, or dates.

User message: {text!r}
""".strip()

    def _parse_llm_json_text(self, text: str) -> Optional[Dict[str, Any]]:
        if not text:
            return None
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start < 0 or end < start:
                return None
            try:
                parsed = json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                return None
        return parsed if isinstance(parsed, dict) else None

    async def _parse_with_llm(self, text: str) -> Optional[Dict[str, Any]]:
        if not self.gemini_client:
            return None

        def call_model():
            kwargs: Dict[str, Any] = {
                "model": self.llm_model,
                "contents": self._llm_prompt(text),
            }
            if types is not None:
                kwargs["config"] = types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=self._llm_schema(),
                    temperature=0,
                )
            return self.gemini_client.models.generate_content(**kwargs)

        try:
            response = await asyncio.wait_for(asyncio.to_thread(call_model), timeout=self.llm_timeout_seconds)
            parsed = self._parse_llm_json_text(getattr(response, "text", "") or "")
            if not parsed:
                logger.warning("Gemini reminder parser returned non-JSON response: %r", getattr(response, "text", None))
                return None
            return parsed
        except asyncio.TimeoutError:
            logger.warning("Gemini reminder parser timed out after %ss", self.llm_timeout_seconds)
            return None
        except Exception:
            logger.exception("Gemini reminder parser failed; falling back to deterministic parser")
            return None

    def _llm_confidence(self, parsed: Dict[str, Any]) -> float:
        try:
            return float(parsed.get("confidence", 0))
        except (TypeError, ValueError):
            return 0.0

    def _delete_expired_confirmations(self) -> None:
        self.conn.execute("DELETE FROM pending_confirmations WHERE expires_at <= ?", (self._now_iso(),))
        self.conn.commit()

    def _new_confirmation_token(self) -> str:
        for _ in range(10):
            token = secrets.token_hex(3)
            existing = self.conn.execute("SELECT 1 FROM pending_confirmations WHERE token = ?", (token,)).fetchone()
            if not existing:
                return token
        return secrets.token_hex(4)

    def _preview_llm_parse(self, parsed: Dict[str, Any]) -> Optional[str]:
        intent = str(parsed.get("intent") or "unknown").lower()
        if intent == "once":
            task = self._normalize_text(str(parsed.get("task") or ""))
            due_at = self._normalize_text(str(parsed.get("due_at") or ""))
            target_time = None
            if due_at:
                try:
                    target_time = dt.datetime.fromisoformat(due_at).replace(microsecond=0)
                except ValueError:
                    target_time = self._parse_datetime(due_at)
            if task and target_time:
                return f"{self._format_due(target_time.isoformat())} - {task}"
        if intent == "recurring":
            task = self._normalize_text(str(parsed.get("task") or ""))
            recurrence_phrase = self._normalize_text(str(parsed.get("recurrence_phrase") or ""))
            if task and recurrence_phrase:
                return f"Recurring: {recurrence_phrase} - {task}"
        if intent == "birthday":
            person = self._normalize_text(str(parsed.get("person") or ""))
            date_phrase = self._normalize_text(str(parsed.get("date_phrase") or ""))
            lead_phrase = self._normalize_text(str(parsed.get("lead_phrase") or ""))
            if person and date_phrase:
                suffix = f"; remind {lead_phrase}" if lead_phrase else ""
                return f"Birthday: {person} on {date_phrase}{suffix}"
        if intent in {"checkin", "flowers"}:
            person = self._normalize_text(str(parsed.get("person") or ""))
            cadence_phrase = self._normalize_text(str(parsed.get("cadence_phrase") or "6 per year")) or "6 per year"
            if person:
                label = "Flowers" if intent == "flowers" else "Check-in"
                return f"{label}: {person}, {cadence_phrase}"
        return None

    def _store_pending_confirmation(self, *, parsed: Dict[str, Any], source_text: str, preview: str) -> str:
        self._delete_expired_confirmations()
        token = self._new_confirmation_token()
        expires_at = (self._now() + dt.timedelta(minutes=self.confirmation_expiry_minutes)).isoformat()
        self.conn.execute(
            """
            INSERT INTO pending_confirmations (
                token, intent, parsed_json, source_text, preview, created_at, expires_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                token,
                str(parsed.get("intent") or "unknown").lower(),
                json.dumps(parsed, ensure_ascii=False, sort_keys=True),
                source_text,
                preview,
                self._now_iso(),
                expires_at,
            ),
        )
        self.conn.commit()
        return token

    def _confirmation_prompt(self, token: str, preview: str) -> str:
        return (
            "🤔 I think you mean:\n"
            f"{preview}\n\n"
            f"Reply `/confirm {token}` to save, or `/cancel {token}` to discard. "
            f"This expires in {int(self.confirmation_expiry_minutes)} minutes."
        )

    async def _execute_llm_parse(self, parsed: Dict[str, Any], source_text: str) -> Optional[str]:
        intent = str(parsed.get("intent") or "unknown").lower()
        if intent == "unknown":
            clarification = self._normalize_text(str(parsed.get("clarification") or ""))
            return f"❓ {clarification}" if clarification else None

        if intent == "once":
            task = self._normalize_text(str(parsed.get("task") or ""))
            due_at = self._normalize_text(str(parsed.get("due_at") or ""))
            if not task or not due_at:
                return None
            try:
                target_time = dt.datetime.fromisoformat(due_at).replace(microsecond=0)
            except ValueError:
                target_time = self._parse_datetime(due_at)
            if not target_time:
                return None
            if target_time <= self._now():
                return "❓ That looks like it is in the past. Please include a future time."
            reminder_id = self._insert_reminder(
                due_at=target_time.isoformat(),
                task=task,
                kind="once",
                source_text=source_text,
                payload={"parser": "gemini", "raw": parsed},
            )
            return f"✅ Set #{reminder_id} for {self._format_due(target_time.isoformat())}: {task}"

        if intent == "recurring":
            task = self._normalize_text(str(parsed.get("task") or ""))
            recurrence_phrase = self._normalize_text(str(parsed.get("recurrence_phrase") or ""))
            if not task or not recurrence_phrase:
                return None
            config = self._parse_recurrence(recurrence_phrase)
            if not config:
                return None
            config.setdefault("anchor_date", self._now().date().isoformat())
            next_due = self._next_due_for_recurrence(config)
            plan_id = self._insert_plan(kind="recurring", name=task, action=task, config=config)
            self._insert_reminder(
                due_at=next_due.isoformat(),
                task=task,
                kind="recurring",
                plan_id=plan_id,
                source_text=source_text,
                payload={"parser": "gemini", "recurrence": config, "raw": parsed},
            )
            return f"🔁 Recurring reminder set: {task}\nNext: {self._format_due(next_due.isoformat())}"

        if intent == "birthday":
            person = self._normalize_text(str(parsed.get("person") or ""))
            date_phrase = self._normalize_text(str(parsed.get("date_phrase") or ""))
            lead_phrase = self._normalize_text(str(parsed.get("lead_phrase") or "")) or None
            if not person or not date_phrase:
                return None
            parsed_date = self._parse_datetime(date_phrase)
            if not parsed_date:
                return None
            offsets = self._parse_lead_offsets(lead_phrase)
            config = {"person": person, "month": parsed_date.month, "day": parsed_date.day, "offset_days": offsets}
            plan_id = self._insert_plan(kind="birthday", name=person, action=f"{person}'s birthday", config=config)
            created = self._schedule_birthday_occurrences(
                plan_id=plan_id,
                person=person,
                month=parsed_date.month,
                day=parsed_date.day,
                offsets=offsets,
                years_ahead=2,
            )
            offset_label = ", ".join("on the day" if o == 0 else f"{o} days before" for o in offsets)
            return f"🎂 Birthday reminder set for {person} ({parsed_date.strftime('%d %b')}); scheduled {created} reminder(s), {offset_label}."

        if intent in {"checkin", "flowers"}:
            person = self._normalize_text(str(parsed.get("person") or ""))
            cadence_phrase = self._normalize_text(str(parsed.get("cadence_phrase") or "6 per year")) or "6 per year"
            if not person:
                return None
            command = "/flowers" if intent == "flowers" else "/checkin"
            return await self._cmd_interaction(f"{command} {person} | {cadence_phrase}", command=command)

        if intent == "list":
            return await self._cmd_list()
        if intent == "plans":
            return await self._cmd_plans()
        if intent == "help":
            return self._usage()
        if intent == "delete":
            n = parsed.get("delete_number")
            return await self._cmd_delete(f"/del {n}") if n else None
        if intent == "renew":
            target = self._normalize_text(str(parsed.get("target") or parsed.get("person") or ""))
            return await self._cmd_renew(f"/renew {target}") if target else None
        if intent == "stop":
            target = self._normalize_text(str(parsed.get("target") or parsed.get("person") or ""))
            return await self._cmd_stop(f"/stop {target}") if target else None
        return None

    async def _cmd_remind_with_llm(self, text: str) -> Optional[str]:
        parsed = await self._parse_with_llm(text)
        if not parsed:
            return None

        confidence = self._llm_confidence(parsed)
        intent = str(parsed.get("intent") or "unknown").lower()
        if intent == "unknown":
            clarification = self._normalize_text(str(parsed.get("clarification") or ""))
            return f"❓ {clarification}" if clarification else None

        if confidence >= self.llm_auto_confirm_threshold:
            return await self._execute_llm_parse(parsed, text)

        if confidence >= self.llm_confirmation_threshold:
            preview = self._preview_llm_parse(parsed)
            if preview:
                token = self._store_pending_confirmation(parsed=parsed, source_text=text, preview=preview)
                return self._confirmation_prompt(token, preview)

        return None

    # ---------------------------------------------------------------------
    # Core commands
    # ---------------------------------------------------------------------

    def _list_section(self, due_at: str) -> str:
        due = self._parse_iso(due_at)
        today = self._now().date()
        if due.date() < today:
            return "Overdue"
        if due.date() == today:
            return "Today"
        if due.date() == today + dt.timedelta(days=1):
            return "Tomorrow"
        if due.date() <= today + dt.timedelta(days=7):
            return "Next 7 days"
        if due.date() <= today + dt.timedelta(days=30):
            return "Next 30 days"
        return "Later"

    def _list_line(self, display_number: int, row: sqlite3.Row) -> str:
        due = self._parse_iso(row["due_at"])
        section = self._list_section(row["due_at"])
        if section in {"Today", "Tomorrow", "Overdue"}:
            when = due.strftime("%H:%M")
        elif section in {"Next 7 days", "Next 30 days"}:
            when = due.strftime("%a %d %b %H:%M")
        else:
            when = due.strftime("%d %b %Y %H:%M")

        kind = str(row["kind"] or "once")
        kind_icon = {
            "once": "",
            "recurring": " 🔁",
            "birthday": " 🎂",
            "checkin": " 👋",
            "flowers": " 🌷",
            "renewal": " 🔄",
        }.get(kind, f" {kind}")
        plan = f" p{row['plan_id']}" if row["plan_id"] else ""
        return f"{display_number}. {when} - {row['task']}{kind_icon}{plan}"

    async def _cmd_list(self) -> str:
        rows = self.conn.execute(
            """
            SELECT r.*, p.kind AS plan_kind, p.name AS plan_name
            FROM reminders r
            LEFT JOIN plans p ON p.id = r.plan_id
            WHERE r.status = 'pending'
            ORDER BY r.due_at ASC
            LIMIT 50
            """
        ).fetchall()
        if not rows:
            return "📭 No pending reminders."

        total = self.conn.execute("SELECT COUNT(1) AS n FROM reminders WHERE status = 'pending'").fetchone()["n"]
        lines = [f"🗓 Pending reminders ({total})"]
        current_section = None
        for i, r in enumerate(rows, start=1):
            section = self._list_section(r["due_at"])
            if section != current_section:
                current_section = section
                lines.append(f"\n{section}")
            lines.append(self._list_line(i, r))

        if total > len(rows):
            lines.append(f"\nShowing first {len(rows)} of {total} pending reminders.")
        lines.append("\nDelete with `/del [number]`. Recurring plans: `/plans`.")
        return "\n".join(lines)


    async def _cmd_delete(self, text: str) -> str:
        parts = text.split()
        if len(parts) < 2:
            return "❌ Use `/del [number]` from the `/list`."
        try:
            idx = int(parts[1]) - 1
        except ValueError:
            return "❌ Use `/del [number]` from the `/list`."
        rows = self.conn.execute("SELECT * FROM reminders WHERE status = 'pending' ORDER BY due_at ASC LIMIT 50").fetchall()
        if idx < 0 or idx >= len(rows):
            return "❌ That number is not in `/list`."
        removed = rows[idx]
        self.conn.execute("UPDATE reminders SET status = 'dismissed' WHERE id = ?", (removed["id"],))
        self.conn.commit()
        return f"✅ Deleted: {removed['task']}"


    async def _cmd_confirm(self, text: str) -> str:
        parts = text.split()
        if len(parts) < 2:
            return "❌ Use `/confirm [token]` from the confirmation message."
        token = parts[1].strip().lower()
        self._delete_expired_confirmations()
        row = self.conn.execute("SELECT * FROM pending_confirmations WHERE token = ?", (token,)).fetchone()
        if not row:
            return "❌ That confirmation was not found or has expired. Try the reminder again."
        try:
            parsed = json.loads(row["parsed_json"])
        except Exception:
            self.conn.execute("DELETE FROM pending_confirmations WHERE token = ?", (token,))
            self.conn.commit()
            logger.exception("Failed to decode pending reminder confirmation %s", token)
            return "❌ I couldn't read that saved confirmation. Please try the reminder again."

        response = await self._execute_llm_parse(parsed, row["source_text"])
        self.conn.execute("DELETE FROM pending_confirmations WHERE token = ?", (token,))
        self.conn.commit()
        return response or "❌ I couldn't save that confirmation. Please try the reminder again."

    async def _cmd_cancel_confirmation(self, text: str) -> str:
        parts = text.split()
        if len(parts) < 2:
            return "❌ Use `/cancel [token]` from the confirmation message."
        token = parts[1].strip().lower()
        self._delete_expired_confirmations()
        row = self.conn.execute("SELECT preview FROM pending_confirmations WHERE token = ?", (token,)).fetchone()
        if not row:
            return "❌ That confirmation was not found or has already expired."
        self.conn.execute("DELETE FROM pending_confirmations WHERE token = ?", (token,))
        self.conn.commit()
        return f"🗑 Cancelled: {row['preview']}"

    async def _cmd_remind(self, text: str) -> str:
        llm_response = await self._cmd_remind_with_llm(text)
        if llm_response:
            return llm_response

        target_time, task, err = self._extract_natural_reminder(text)
        if err:
            return err
        if not target_time or not task:
            return "💡 Format: `/remind tomorrow 8pm | water plants`"
        reminder_id = self._insert_reminder(due_at=target_time.isoformat(), task=task, kind="once", source_text=text)
        return f"✅ Set #{reminder_id} for {self._format_due(target_time.isoformat())}: {task}"

    def _usage(self) -> str:
        return (
            "⏳ **Reminder Bot**\n"
            "• `/remind [time] | [message]`: one-off reminder.\n"
            "• `/remind me to [message] [time]`: natural reminder.\n"
            "• `/remind me to [message] every Friday at 9am`: LLM-assisted natural recurring reminder, if enabled.\n"
            "• `/recur every Monday at 9am | [message]`: recurring reminder.\n"
            "• `/birthday [person] | [date] [| 1 week before]`: annual birthday reminder.\n"
            "• `/checkin [person] | 6 per year`: semi-random check-in reminders.\n"
            "• `/flowers [person] | 4 per year`: semi-random flower reminders.\n"
            "• `/list`: pending reminders.\n"
            "• `/plans`: active recurring plans.\n"
            "• `/renew [id/name]`: generate more interaction reminders.\n"
            "• `/stop [id/name]`: stop a plan.\n"
            "• `/del [number]`: delete a pending reminder from `/list`."
        )

    async def handle_command(self, text: str) -> Optional[str]:
        text = (text or "").strip()
        if not text:
            return None
        cmd = text.split()[0].lower()
        if cmd == "/list":
            return await self._cmd_list()
        if cmd == "/del":
            return await self._cmd_delete(text)
        if cmd == "/confirm":
            return await self._cmd_confirm(text)
        if cmd == "/cancel":
            return await self._cmd_cancel_confirmation(text)
        if cmd == "/remind":
            return await self._cmd_remind(text)
        if cmd == "/birthday":
            return await self._cmd_birthday(text)
        if cmd == "/recur":
            return await self._cmd_recur(text)
        if cmd == "/checkin":
            return await self._cmd_interaction(text, command="/checkin")
        if cmd == "/flowers":
            return await self._cmd_interaction(text, command="/flowers")
        if cmd == "/plans":
            return await self._cmd_plans()
        if cmd == "/renew":
            return await self._cmd_renew(text)
        if cmd == "/stop":
            return await self._cmd_stop(text)
        if cmd in ("/usage", "/help"):
            return self._usage()
        return None

    # ---------------------------------------------------------------------
    # Polling loop
    # ---------------------------------------------------------------------

    async def _send_due_reminder(self, reminder: sqlite3.Row, alert_callback) -> None:
        prefix = "🔔 **REMINDER**"
        if reminder["kind"] == "birthday":
            prefix = "🎂 **BIRTHDAY**"
        elif reminder["kind"] == "recurring":
            prefix = "🔁 **RECURRING REMINDER**"
        elif reminder["kind"] == "interaction":
            prefix = "🌱 **INTERACTION REMINDER**"
        await alert_callback(f"{prefix}: {reminder['task']}")

    def _mark_sent(self, reminder_id: int) -> None:
        self.conn.execute("UPDATE reminders SET status = 'sent', sent_at = ? WHERE id = ?", (self._now_iso(), reminder_id))
        self.conn.commit()

    def _ensure_birthday_future_events(self, plan_id: int) -> None:
        plan = self.conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
        if not plan or not plan["active"] or plan["kind"] != "birthday":
            return
        pending = self.conn.execute("SELECT COUNT(1) AS n FROM reminders WHERE plan_id = ? AND status = 'pending'", (plan_id,)).fetchone()["n"]
        if int(pending) >= 2:
            return
        config = self._json_load(plan["config_json"])
        self._schedule_birthday_occurrences(
            plan_id=plan_id,
            person=config.get("person") or plan["name"],
            month=int(config["month"]),
            day=int(config["day"]),
            offsets=[int(x) for x in config.get("offset_days", [0])],
            years_ahead=3,
        )

    async def _prompt_depleted_interaction_plans(self, alert_callback) -> None:
        rows = self.conn.execute(
            """
            SELECT p.*
            FROM plans p
            WHERE p.active = 1
              AND p.kind = 'interaction'
              AND p.renewal_prompted_at IS NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM reminders r
                  WHERE r.plan_id = p.id
                    AND r.status = 'pending'
              )
            """
        ).fetchall()
        for plan in rows:
            await alert_callback(
                "🔁 Interaction schedule has run out for "
                f"**{plan['name']}** ({plan['action']}).\n"
                f"Reply `/renew {plan['id']}` to generate more, or `/stop {plan['id']}` to disable it."
            )
            self._update_plan(int(plan["id"]), renewal_prompted_at=self._now_iso())

    async def check_reminders(self, alert_callback):
        """Polling loop: sends due reminders and advances recurring plans."""
        while True:
            try:
                now_iso = self._now_iso()
                due = self.conn.execute(
                    """
                    SELECT *
                    FROM reminders
                    WHERE status = 'pending'
                      AND due_at <= ?
                    ORDER BY due_at ASC
                    LIMIT 20
                    """,
                    (now_iso,),
                ).fetchall()
                for r in due:
                    await self._send_due_reminder(r, alert_callback)
                    self._mark_sent(int(r["id"]))
                    if r["kind"] == "recurring":
                        self._advance_recurring_plan(r)
                    elif r["kind"] == "birthday" and r["plan_id"]:
                        self._ensure_birthday_future_events(int(r["plan_id"]))
                await self._prompt_depleted_interaction_plans(alert_callback)
            except Exception as e:
                logger.exception("Reminder Loop Error: %s", e)
            await asyncio.sleep(60)
