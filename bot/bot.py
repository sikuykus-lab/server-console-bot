#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from telegram.error import BadRequest
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    filters,
)

CONFIG_PATH = Path(__file__).resolve().parent / "scripts.json"
ENV_PATH = Path(__file__).resolve().parent / ".env"
DB_PATH = Path(__file__).resolve().parent / "access.db"

BTN_SCHEDULE = "Расписание + crontab"
BTN_CONSTRUCTOR = "Конструктор"
BTN_SETTINGS = "Настройки"
BTN_REQUEST_MORE = "Запросить кнопку"
BTN_CLOSE = "Закрыть"
MSK_UTC_OFFSET_HOURS = 3

# Псевдо-ID в user_button_visibility для кнопок панели (не скрипты). Конструктор в reply-клавиатуре всегда остаётся.
UI_KEY_SCHEDULE = "__ui_schedule__"
UI_KEY_CLOSE = "__ui_close__"
UI_KEY_SETTINGS = "__ui_settings__"
UI_VISIBILITY_KEYS = frozenset({UI_KEY_SCHEDULE, UI_KEY_CLOSE, UI_KEY_SETTINGS})

load_dotenv(ENV_PATH)
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ALLOWED_RAW = os.environ.get("ALLOWED_USER_ID", "").strip()

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s %(message)s", level=logging.INFO)
log = logging.getLogger("server_console_bot")

if not BOT_TOKEN:
    raise SystemExit("В .env нужен BOT_TOKEN")
if not ALLOWED_RAW:
    raise SystemExit("В .env нужен ALLOWED_USER_ID")
OWNER_ID = int(ALLOWED_RAW)

OWNER_FILTER = filters.User(user_id=OWNER_ID) & filters.ChatType.PRIVATE
running_scripts: dict[str, dict[str, Any]] = {}
open_inline_panels: dict[int, tuple[int, int]] = {}


def utc_now_ts() -> int:
    return int(time.time())


def utc_str(ts: int | None = None) -> str:
    dt = datetime.fromtimestamp(ts or utc_now_ts(), tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def load_config() -> dict[str, Any]:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("scripts", [])
    return cfg


def scripts_map() -> dict[str, dict[str, Any]]:
    return {s["id"]: s for s in load_config().get("scripts", [])}


def script_title(script_id: str) -> str:
    s = scripts_map().get(script_id)
    return s.get("title", script_id) if s else script_id


def script_owner_only(script_id: str) -> bool:
    s = scripts_map().get(script_id)
    return bool(s and s.get("owner_only"))


def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def db_init() -> None:
    with db_conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                role TEXT NOT NULL DEFAULT 'user',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at INTEGER NOT NULL,
                approved_at INTEGER,
                approved_by INTEGER
            );
            CREATE TABLE IF NOT EXISTS user_script_permissions(
                user_id INTEGER NOT NULL,
                script_id TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(user_id, script_id)
            );
            CREATE TABLE IF NOT EXISTS user_button_visibility(
                user_id INTEGER NOT NULL,
                script_id TEXT NOT NULL,
                visible INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(user_id, script_id)
            );
            CREATE TABLE IF NOT EXISTS access_requests(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                payload_json TEXT,
                created_at INTEGER NOT NULL,
                resolved_at INTEGER,
                resolved_by INTEGER
            );
            CREATE TABLE IF NOT EXISTS script_runs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                script_id TEXT NOT NULL,
                started_at INTEGER NOT NULL,
                finished_at INTEGER,
                exit_code INTEGER,
                status TEXT NOT NULL,
                duration_ms INTEGER
            );
            CREATE TABLE IF NOT EXISTS cooldowns(
                user_id INTEGER NOT NULL,
                script_id TEXT NOT NULL,
                next_allowed_at INTEGER NOT NULL,
                PRIMARY KEY(user_id, script_id)
            );
            CREATE TABLE IF NOT EXISTS rate_windows(
                user_id INTEGER NOT NULL,
                ts INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS bot_settings(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_log(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                action TEXT NOT NULL,
                payload_json TEXT,
                ts INTEGER NOT NULL
            );
            """
        )
        c.execute(
            "INSERT OR IGNORE INTO bot_settings(key, value) VALUES('limits_json', ?)",
            (json.dumps({"cooldown_sec": 600, "rate_max": 3, "rate_window_sec": 900}),),
        )
        c.execute(
            """
            INSERT OR IGNORE INTO users(user_id, username, full_name, role, status, created_at, approved_at, approved_by)
            VALUES (?, '', 'Owner', 'admin', 'approved', ?, ?, ?)
            """,
            (OWNER_ID, utc_now_ts(), utc_now_ts(), OWNER_ID),
        )


def audit(user_id: int | None, action: str, payload: dict[str, Any] | None = None) -> None:
    with db_conn() as c:
        c.execute(
            "INSERT INTO audit_log(user_id, action, payload_json, ts) VALUES(?, ?, ?, ?)",
            (user_id, action, json.dumps(payload or {}, ensure_ascii=False), utc_now_ts()),
        )


def user_row(user_id: int) -> sqlite3.Row | None:
    with db_conn() as c:
        return c.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


def upsert_user_from_update(update: Update) -> None:
    u = update.effective_user
    if not u:
        return
    full_name = " ".join([x for x in [u.first_name, u.last_name] if x]).strip() or "Unknown"
    with db_conn() as c:
        c.execute(
            """
            INSERT INTO users(user_id, username, full_name, role, status, created_at)
            VALUES (?, ?, ?, 'user', 'pending', ?)
            ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name
            """,
            (u.id, u.username or "", full_name, utc_now_ts()),
        )


def get_limits() -> dict[str, int]:
    with db_conn() as c:
        raw = c.execute("SELECT value FROM bot_settings WHERE key='limits_json'").fetchone()
    if not raw:
        return {"cooldown_sec": 600, "rate_max": 3, "rate_window_sec": 900}
    val = json.loads(raw["value"])
    return {
        "cooldown_sec": int(val.get("cooldown_sec", 600)),
        "rate_max": int(val.get("rate_max", 3)),
        "rate_window_sec": int(val.get("rate_window_sec", 900)),
    }


def set_limits(cooldown_sec: int, rate_max: int, rate_window_sec: int) -> None:
    value = json.dumps(
        {
            "cooldown_sec": cooldown_sec,
            "rate_max": rate_max,
            "rate_window_sec": rate_window_sec,
        }
    )
    with db_conn() as c:
        c.execute("REPLACE INTO bot_settings(key, value) VALUES('limits_json', ?)", (value,))
    audit(OWNER_ID, "settings_limits_updated", json.loads(value))


def perm_stored(user_id: int, script_id: str) -> bool:
    with db_conn() as c:
        got = c.execute(
            "SELECT enabled FROM user_script_permissions WHERE user_id=? AND script_id=?",
            (user_id, script_id),
        ).fetchone()
    return bool(got and got["enabled"] == 1)


def has_perm(user_id: int, script_id: str) -> bool:
    if script_owner_only(script_id):
        return user_id == OWNER_ID
    row = user_row(user_id)
    if not row or row["status"] != "approved":
        return False
    if row["role"] == "admin":
        return True
    return perm_stored(user_id, script_id)


def _visibility_flag(user_id: int, key: str) -> bool:
    with db_conn() as c:
        v = c.execute(
            "SELECT visible FROM user_button_visibility WHERE user_id=? AND script_id=?",
            (user_id, key),
        ).fetchone()
    return v is None or v["visible"] == 1


def visible_meta_ui(user_id: int, ui_key: str) -> bool:
    if ui_key not in UI_VISIBILITY_KEYS:
        return False
    if ui_key == UI_KEY_SETTINGS and user_id != OWNER_ID:
        return False
    return _visibility_flag(user_id, ui_key)


def visible_script_ids(user_id: int) -> list[str]:
    row = user_row(user_id)
    ids: list[str] = []
    for sid in scripts_map().keys():
        if script_owner_only(sid):
            allowed = user_id == OWNER_ID
        elif row and row["role"] == "admin":
            allowed = True
        else:
            allowed = has_perm(user_id, sid)
        if not allowed:
            continue
        if _visibility_flag(user_id, sid):
            ids.append(sid)
    return ids


def allowed_script_ids(user_id: int) -> list[str]:
    row = user_row(user_id)
    ids: list[str] = []
    for sid in scripts_map().keys():
        if script_owner_only(sid):
            if user_id == OWNER_ID:
                ids.append(sid)
            continue
        if row and row["role"] == "admin":
            ids.append(sid)
            continue
        if has_perm(user_id, sid):
            ids.append(sid)
    return ids


def set_user_role(user_id: int, role: str, approved_by: int) -> None:
    with db_conn() as c:
        c.execute(
            "UPDATE users SET role=?, status='approved', approved_at=?, approved_by=? WHERE user_id=?",
            (role, utc_now_ts(), approved_by, user_id),
        )
    audit(approved_by, "user_approved_role", {"target_user": user_id, "role": role})


def toggle_permission(user_id: int, script_id: str, by_user: int | None = None) -> int:
    with db_conn() as c:
        row = c.execute(
            "SELECT enabled FROM user_script_permissions WHERE user_id=? AND script_id=?",
            (user_id, script_id),
        ).fetchone()
        new_val = 0 if row and row["enabled"] == 1 else 1
        c.execute(
            "REPLACE INTO user_script_permissions(user_id, script_id, enabled) VALUES(?, ?, ?)",
            (user_id, script_id, new_val),
        )
    audit(by_user or OWNER_ID, "perm_toggle", {"target_user": user_id, "script_id": script_id, "enabled": new_val})
    return new_val


def toggle_visibility(user_id: int, script_id: str) -> int:
    with db_conn() as c:
        row = c.execute(
            "SELECT visible FROM user_button_visibility WHERE user_id=? AND script_id=?",
            (user_id, script_id),
        ).fetchone()
        current = 1 if row is None or row["visible"] == 1 else 0
        new_val = 0 if current == 1 else 1
        c.execute(
            "REPLACE INTO user_button_visibility(user_id, script_id, visible) VALUES(?, ?, ?)",
            (user_id, script_id, new_val),
        )
    audit(user_id, "constructor_toggle_visibility", {"script_id": script_id, "visible": new_val})
    return new_val


def open_access_request(user_id: int, kind: str, payload: dict[str, Any] | None = None) -> int:
    with db_conn() as c:
        pending = c.execute(
            "SELECT id FROM access_requests WHERE user_id=? AND kind=? AND status='pending' ORDER BY id DESC LIMIT 1",
            (user_id, kind),
        ).fetchone()
        if pending:
            return int(pending["id"])
        cur = c.execute(
            "INSERT INTO access_requests(user_id, kind, payload_json, created_at) VALUES(?, ?, ?, ?)",
            (user_id, kind, json.dumps(payload or {}, ensure_ascii=False), utc_now_ts()),
        )
    rid = int(cur.lastrowid)
    audit(user_id, "access_request_opened", {"request_id": rid, "kind": kind, "payload": payload or {}})
    return rid


def resolve_request(request_id: int, by_user: int, accepted: bool) -> sqlite3.Row | None:
    with db_conn() as c:
        row = c.execute("SELECT * FROM access_requests WHERE id=?", (request_id,)).fetchone()
        if not row or row["status"] != "pending":
            return None
        new_status = "approved" if accepted else "rejected"
        c.execute(
            "UPDATE access_requests SET status=?, resolved_at=?, resolved_by=? WHERE id=?",
            (new_status, utc_now_ts(), by_user, request_id),
        )
    audit(by_user, "request_resolved", {"request_id": request_id, "accepted": accepted, "kind": row["kind"]})
    return row


def user_state(user_id: int) -> str:
    row = user_row(user_id)
    if not row:
        return "pending"
    return row["status"]


def build_user_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = []
    row: list[KeyboardButton] = []
    for sid in visible_script_ids(user_id):
        row.append(KeyboardButton(script_title(sid)))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    sched_ctor: list[KeyboardButton] = []
    if visible_meta_ui(user_id, UI_KEY_SCHEDULE):
        sched_ctor.append(KeyboardButton(BTN_SCHEDULE))
    sched_ctor.append(KeyboardButton(BTN_CONSTRUCTOR))
    rows.append(sched_ctor)
    if user_id == OWNER_ID and visible_meta_ui(user_id, UI_KEY_SETTINGS):
        rows.append([KeyboardButton(BTN_SETTINGS)])
    if visible_meta_ui(user_id, UI_KEY_CLOSE):
        rows.append([KeyboardButton(BTN_CLOSE)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True, selective=False)


def script_id_by_title(title: str, user_id: int) -> str | None:
    for sid in visible_script_ids(user_id):
        if script_title(sid) == title:
            return sid
    return None


def subprocess_env() -> dict[str, str]:
    from dotenv import dotenv_values

    merged = {**os.environ, **{k: v for k, v in dotenv_values(ENV_PATH).items() if v is not None and v != ""}}
    merged["PYTHONUNBUFFERED"] = "1"
    return merged


def run_subprocess(cwd: str, argv: list[str], timeout: int) -> tuple[int, str, str]:
    p = subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=subprocess_env(),
    )
    return p.returncode, p.stdout or "", p.stderr or ""


def rate_limit_check(user_id: int) -> tuple[bool, int]:
    lim = get_limits()
    now = utc_now_ts()
    from_ts = now - lim["rate_window_sec"]
    with db_conn() as c:
        c.execute("DELETE FROM rate_windows WHERE ts < ?", (from_ts,))
        cnt = c.execute("SELECT COUNT(*) AS c FROM rate_windows WHERE user_id=? AND ts>=?", (user_id, from_ts)).fetchone()["c"]
        if cnt >= lim["rate_max"]:
            oldest = c.execute(
                "SELECT ts FROM rate_windows WHERE user_id=? AND ts>=? ORDER BY ts ASC LIMIT 1",
                (user_id, from_ts),
            ).fetchone()
            retry = (oldest["ts"] + lim["rate_window_sec"] - now) if oldest else lim["rate_window_sec"]
            return False, max(1, retry)
        c.execute("INSERT INTO rate_windows(user_id, ts) VALUES(?, ?)", (user_id, now))
    return True, 0


def cooldown_check_and_set(user_id: int, script_id: str, skip: bool) -> tuple[bool, int]:
    if skip:
        return True, 0
    lim = get_limits()
    now = utc_now_ts()
    with db_conn() as c:
        row = c.execute("SELECT next_allowed_at FROM cooldowns WHERE user_id=? AND script_id=?", (user_id, script_id)).fetchone()
        if row and row["next_allowed_at"] > now:
            return False, row["next_allowed_at"] - now
        c.execute(
            "REPLACE INTO cooldowns(user_id, script_id, next_allowed_at) VALUES(?, ?, ?)",
            (user_id, script_id, now + lim["cooldown_sec"]),
        )
    return True, 0


def get_crontab_text() -> str:
    p = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=15)
    if p.returncode != 0:
        return p.stderr or "(crontab недоступен)"
    return p.stdout or "(пусто)"


def _cron_job_payload(line: str) -> str:
    s = line.strip()
    if s.startswith("#"):
        s = s.lstrip("#").strip()
    return s


def is_cron_job_line(line: str) -> bool:
    s = _cron_job_payload(line)
    parts = s.split()
    if len(parts) < 6:
        return False
    for p in parts[:5]:
        if not re.match(r"^[\d*,/\-]+$", p):
            return False
    return True


def find_script_cron_job_indices(lines: list[str], hint: str) -> list[int]:
    out: list[int] = []
    for i, line in enumerate(lines):
        if hint not in line or not is_cron_job_line(line):
            continue
        out.append(i)
    return out


def script_cron_schedule_label(line: str) -> str:
    s = _cron_job_payload(line)
    parts = s.split()
    if len(parts) < 6:
        return ""
    try:
        mm, hh = int(parts[0]), int(parts[1])
    except ValueError:
        return ""
    msk_h = (hh + MSK_UTC_OFFSET_HOURS) % 24
    return f"{msk_h:02d}:{mm:02d} МСК"


def script_cron_state(script_id: str) -> tuple[str, str]:
    """Состояние: enabled | disabled | missing; подпись расписания."""
    script = scripts_map().get(script_id)
    if not script:
        return "missing", ""
    hint = str(script.get("cron_hint") or "").strip()
    if not hint:
        return "missing", ""

    current = get_crontab_text()
    if current.startswith("("):
        return "missing", ""

    lines = current.splitlines()
    indices = find_script_cron_job_indices(lines, hint)
    if not indices:
        return "missing", ""

    active_idx = None
    for i in indices:
        if not lines[i].strip().startswith("#"):
            active_idx = i
            break
    if active_idx is not None:
        return "enabled", script_cron_schedule_label(lines[active_idx])
    return "disabled", script_cron_schedule_label(lines[indices[0]])


def set_script_cron_enabled(script_id: str, enabled: bool) -> tuple[bool, str]:
    script = scripts_map().get(script_id)
    if not script:
        return False, "Скрипт не найден."
    hint = str(script.get("cron_hint") or "").strip()
    if not hint:
        return False, "У скрипта нет cron_hint."

    current = get_crontab_text()
    if current.startswith("("):
        return False, f"Не удалось прочитать crontab: {current}"

    lines = current.splitlines()
    indices = find_script_cron_job_indices(lines, hint)
    if not indices:
        return False, "В crontab нет строки задания для этого скрипта."

    changed = 0
    if enabled:
        for i in indices:
            raw = lines[i].strip()
            if raw.startswith("#"):
                lines[i] = _cron_job_payload(lines[i])
                changed += 1
        if changed == 0:
            return True, f"{script_title(script_id)} уже включён в cron."
    else:
        for i in indices:
            raw = lines[i].strip()
            if raw and not raw.startswith("#"):
                lines[i] = "# " + raw
                changed += 1
        if changed == 0:
            return True, f"{script_title(script_id)} уже выключен в cron."

    new_text = "\n".join(lines) + "\n"
    p = subprocess.run(["crontab", "-"], input=new_text, text=True, capture_output=True, timeout=15)
    if p.returncode != 0:
        return False, p.stderr.strip() or "crontab завершился с ошибкой"

    state, sched = script_cron_state(script_id)
    if enabled:
        extra = f" ({sched})" if sched else ""
        return True, f"{script_title(script_id)}: автообновление включено{extra}."
    return True, f"{script_title(script_id)}: автообновление выключено."


def cron_auto_menu_markup() -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for sid, s in scripts_map().items():
        state, sched = script_cron_state(sid)
        if state == "enabled":
            mark = "🟢"
            tail = f" {sched}" if sched else ""
            action = "выкл"
        elif state == "disabled":
            mark = "⏸"
            tail = f" ({sched})" if sched else " (выкл)"
            action = "вкл"
        else:
            mark = "❌"
            tail = " (нет в crontab)"
            action = "—"
        label = f"{mark} {s['title']}{tail}"
        if len(label) > 64:
            label = label[:61] + "…"
        if state == "missing":
            buttons.append([InlineKeyboardButton(label, callback_data="noop")])
        else:
            buttons.append(
                [InlineKeyboardButton(label, callback_data=f"cronauto:toggle:{sid}")]
            )
    buttons.append([InlineKeyboardButton("← Настройки", callback_data="menu:settings")])
    buttons.append([InlineKeyboardButton(BTN_CLOSE, callback_data="menu:close")])
    return InlineKeyboardMarkup(buttons)


def set_cron_time_for_script(script_id: str, hour: int, minute: int) -> tuple[bool, str]:
    smap = scripts_map()
    script = smap.get(script_id)
    if not script:
        return False, "Скрипт не найден."
    hint = str(script.get("cron_hint") or "").strip()
    if not hint:
        return False, "У скрипта нет cron_hint."

    current = get_crontab_text()
    if current.startswith("("):
        return False, f"Не удалось прочитать crontab: {current}"

    lines = current.splitlines()
    changed = False
    for i, line in enumerate(lines):
        if hint not in line or not is_cron_job_line(line):
            continue
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        parts = stripped.split()
        parts[0] = str(minute)
        parts[1] = str(hour)
        lines[i] = " ".join(parts)
        changed = True
        break

    if not changed:
        return False, f"Строка с «{hint}» в crontab не найдена."

    new_text = "\n".join(lines) + "\n"
    p = subprocess.run(["crontab", "-"], input=new_text, text=True, capture_output=True, timeout=15)
    if p.returncode != 0:
        return False, p.stderr.strip() or "crontab - завершился с ошибкой"
    msk_hour = (hour + MSK_UTC_OFFSET_HOURS) % 24
    return True, f"{script_title(script_id)} -> {msk_hour:02d}:{minute:02d} МСК ({hour:02d}:{minute:02d} UTC)"


def chunk_text(s: str, size: int = 3800) -> list[str]:
    return [s[i : i + size] for i in range(0, len(s), size)]


async def post_long_text_html(send, text: str) -> None:
    for part in chunk_text(text if text.strip() else "(пусто)"):
        await send(f"<pre>{html.escape(part)}</pre>", parse_mode="HTML")


async def notify_owner_access_request(app: Application, user_id: int, request_id: int) -> None:
    u = user_row(user_id)
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Одобрить", callback_data=f"adm:reqok:{request_id}"),
                InlineKeyboardButton("Отклонить", callback_data=f"adm:reqno:{request_id}"),
            ]
        ]
    )
    txt = (
        "<b>Новая заявка на доступ</b>\n"
        f"user_id=<code>{user_id}</code>\n"
        f"username=@{html.escape((u['username'] or '').strip())}\n"
        f"name={html.escape((u['full_name'] or '').strip())}\n"
        f"request_id=<code>{request_id}</code>"
    )
    await app.bot.send_message(chat_id=OWNER_ID, text=txt, parse_mode="HTML", reply_markup=kb)


async def edit_inline_kb(q, reply_markup: InlineKeyboardMarkup, *, text: str | None = None) -> None:
    try:
        if text is not None:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=reply_markup)
        else:
            await q.edit_message_reply_markup(reply_markup=reply_markup)
    except BadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise


def remember_open_inline_panel(user_id: int, chat_id: int, message_id: int) -> None:
    open_inline_panels[user_id] = (chat_id, message_id)


async def dismiss_open_inline_panel(bot, user_id: int, *, keep_message_id: int | None = None) -> None:
    rec = open_inline_panels.pop(user_id, None)
    if not rec:
        return
    chat_id, msg_id = rec
    if keep_message_id is not None and msg_id == keep_message_id:
        open_inline_panels[user_id] = rec
        return
    try:
        await bot.edit_message_reply_markup(chat_id=chat_id, message_id=msg_id, reply_markup=None)
    except BadRequest:
        pass
    except Exception as e:
        log.warning("dismiss_open_inline_panel uid=%s: %s", user_id, e)


async def reply_open_inline_panel(message, user_id: int, text: str, reply_markup: InlineKeyboardMarkup, **kwargs):
    bot = message.get_bot()
    await dismiss_open_inline_panel(bot, user_id)
    sent = await message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML", **kwargs)
    remember_open_inline_panel(user_id, sent.chat_id, sent.message_id)
    return sent


async def callback_open_inline_panel(q, user_id: int, text: str, reply_markup: InlineKeyboardMarkup, **kwargs):
    bot = q.get_bot()
    await dismiss_open_inline_panel(bot, user_id)
    sent = await q.message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML", **kwargs)
    remember_open_inline_panel(user_id, sent.chat_id, sent.message_id)
    return sent


async def edit_open_inline_panel(q, user_id: int, text: str, reply_markup: InlineKeyboardMarkup) -> None:
    try:
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=reply_markup)
    except BadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise
    remember_open_inline_panel(user_id, q.message.chat_id, q.message.message_id)


async def push_panel_keyboard(
    bot,
    target_uid: int,
    *,
    note: str | None = None,
    silent: bool = False,
) -> None:
    text = note if note is not None else ("·" if silent else "Панель обновлена.")
    try:
        await bot.send_message(
            chat_id=target_uid,
            text=text,
            reply_markup=build_user_keyboard(target_uid),
            disable_notification=silent,
        )
    except Exception as e:
        log.warning("push_panel_keyboard uid=%s: %s", target_uid, e)


def constructor_kb(user_id: int) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for sid in allowed_script_ids(user_id):
        mark = "👁" if _visibility_flag(user_id, sid) else "🚫"
        buttons.append([InlineKeyboardButton(f"{mark} {script_title(sid)}", callback_data=f"ctor:vis:{sid}")])
    panel_rows: list[tuple[str, str]] = [
        (UI_KEY_SCHEDULE, BTN_SCHEDULE),
        (UI_KEY_CLOSE, BTN_CLOSE),
    ]
    if user_id == OWNER_ID:
        panel_rows.append((UI_KEY_SETTINGS, BTN_SETTINGS))
    for key, title in panel_rows:
        mark = "👁" if _visibility_flag(user_id, key) else "🚫"
        buttons.append([InlineKeyboardButton(f"{mark} {title}", callback_data=f"ctor:vis:{key}")])
    buttons.append([InlineKeyboardButton(BTN_REQUEST_MORE, callback_data="ctor:reqmenu")])
    buttons.append([InlineKeyboardButton(BTN_CLOSE, callback_data="menu:close")])
    return InlineKeyboardMarkup(buttons)


def users_menu_markup() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    with db_conn() as c:
        found = c.execute(
            """
            SELECT user_id, username, full_name, role
            FROM users
            WHERE status='approved' AND user_id != ?
            ORDER BY user_id
            """,
            (OWNER_ID,),
        ).fetchall()
    if not found:
        rows.append([InlineKeyboardButton("Нет других одобренных пользователей", callback_data="noop")])
    else:
        for r in found:
            uid = int(r["user_id"])
            label = str(uid)
            un = (r["username"] or "").strip()
            if un:
                label += f" @{un}"
            nm = (r["full_name"] or "").strip()
            if nm:
                label += " — " + nm[:20]
            if len(label) > 64:
                label = label[:61] + "…"
            rows.append([InlineKeyboardButton(label, callback_data=f"adm:pickuser:{uid}")])
    rows.append([InlineKeyboardButton(BTN_CLOSE, callback_data="menu:close")])
    return InlineKeyboardMarkup(rows)


def owner_perm_kb(target_uid: int) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    target = user_row(target_uid)
    for sid, s in scripts_map().items():
        if script_owner_only(sid):
            continue
        en = perm_stored(target_uid, sid)
        mark = "✅" if en else "⬜"
        buttons.append([InlineKeyboardButton(f"{mark} {s['title']}", callback_data=f"adm:pt:{target_uid}:{sid}")])
    if target and target["role"] == "admin":
        buttons.insert(
            0,
            [InlineKeyboardButton("ℹ️ Роль admin — запуск всех скриптов; галочки для учёта", callback_data="noop")],
        )
    buttons.append([InlineKeyboardButton("← К списку пользователей", callback_data="adm:userlist")])
    buttons.append([InlineKeyboardButton("Готово", callback_data=f"adm:pdone:{target_uid}")])
    buttons.append([InlineKeyboardButton(BTN_CLOSE, callback_data="menu:close")])
    return InlineKeyboardMarkup(buttons)


def request_more_kb(user_id: int) -> InlineKeyboardMarkup:
    buttons = []
    for sid, s in scripts_map().items():
        if script_owner_only(sid) or has_perm(user_id, sid):
            continue
        buttons.append([InlineKeyboardButton(f"Запросить: {s['title']}", callback_data=f"ctor:req:{sid}")])
    if not buttons:
        buttons = [[InlineKeyboardButton("Нет доступных запросов", callback_data="noop")]]
    buttons.append([InlineKeyboardButton(BTN_CLOSE, callback_data="menu:close")])
    return InlineKeyboardMarkup(buttons)


async def ensure_access_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    upsert_user_from_update(update)
    u = update.effective_user
    if not u:
        return False
    if u.id == OWNER_ID:
        return True
    state = user_state(u.id)
    if state == "approved":
        return True
    if state == "blocked":
        await update.effective_message.reply_text("Нет доступа.")
        return False
    rid = open_access_request(u.id, "access")
    await update.effective_message.reply_text("Ожидайте решения администратора.")
    await notify_owner_access_request(context.application, u.id, rid)
    return False


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_access_flow(update, context):
        return
    uid = update.effective_user.id
    await dismiss_open_inline_panel(context.application.bot, uid)
    await update.effective_message.reply_text("Панель активна.", reply_markup=build_user_keyboard(uid))
    audit(uid, "cmd_start")


async def cmd_audit_last(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != OWNER_ID:
        return
    n = 30
    if context.args and context.args[0].isdigit():
        n = max(1, min(200, int(context.args[0])))
    with db_conn() as c:
        rows = c.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    if not rows:
        await update.effective_message.reply_text("Аудит пуст.")
        return
    lines = []
    for r in rows:
        lines.append(f"{utc_str(r['ts'])} uid={r['user_id']} action={r['action']} payload={r['payload_json']}")
    for part in chunk_text("\n".join(lines), 3500):
        await update.effective_message.reply_text(f"<pre>{html.escape(part)}</pre>", parse_mode="HTML")


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    await q.answer()
    uid = update.effective_user.id
    data = q.data or ""

    if data == "noop":
        return
    if data == "menu:close":
        await q.message.edit_reply_markup(reply_markup=None)
        open_inline_panels.pop(uid, None)
        return

    if data.startswith("adm:") and uid != OWNER_ID:
        await q.message.reply_text("Нет доступа.")
        return

    if data == "adm:usersmenu":
        await callback_open_inline_panel(
            q,
            uid,
            "Выберите пользователя, для которого настраиваются скрипты (одобренные, кроме вас):",
            users_menu_markup(),
        )
        return

    if data == "adm:userlist":
        await edit_open_inline_panel(
            q,
            uid,
            "Выберите пользователя, для которого настраиваются скрипты (одобренные, кроме вас):",
            users_menu_markup(),
        )
        return

    if data.startswith("adm:pickuser:"):
        target = int(data.removeprefix("adm:pickuser:"))
        await edit_open_inline_panel(
            q,
            uid,
            f"Скрипты для пользователя <code>{target}</code> (галочка — видит и может запускать):",
            owner_perm_kb(target),
        )
        return

    if data.startswith("adm:reqok:"):
        req_id = int(data.split(":")[-1])
        req = resolve_request(req_id, OWNER_ID, True)
        if not req:
            await q.message.reply_text("Заявка уже обработана.")
            return
        target = req["user_id"]
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Админ", callback_data=f"adm:role:{target}:admin")],
                [InlineKeyboardButton("Пользователь", callback_data=f"adm:role:{target}:user")],
                [InlineKeyboardButton(BTN_CLOSE, callback_data="menu:close")],
            ]
        )
        await callback_open_inline_panel(
            q,
            uid,
            f"Заявка #{req_id}: выбери роль для <code>{target}</code>",
            kb,
        )
        return

    if data.startswith("adm:reqno:"):
        req_id = int(data.split(":")[-1])
        req = resolve_request(req_id, OWNER_ID, False)
        if not req:
            await q.message.reply_text("Заявка уже обработана.")
            return
        with db_conn() as c:
            c.execute("UPDATE users SET status='blocked' WHERE user_id=?", (req["user_id"],))
        await context.application.bot.send_message(chat_id=req["user_id"], text="Нет доступа.")
        await q.message.reply_text("Пользователь отклонён.")
        return

    if data.startswith("adm:role:"):
        _, _, target_s, role = data.split(":")
        target = int(target_s)
        set_user_role(target, role, OWNER_ID)
        await callback_open_inline_panel(
            q,
            uid,
            f"Роль {role} назначена. Теперь отметь доступные кнопки для <code>{target}</code>.",
            owner_perm_kb(target),
        )
        return

    if data.startswith("adm:pt:"):
        rest = data.removeprefix("adm:pt:")
        if ":" not in rest:
            return
        target_s, sid = rest.split(":", 1)
        target = int(target_s)
        toggle_permission(target, sid, uid)
        await edit_inline_kb(q, owner_perm_kb(target))
        remember_open_inline_panel(uid, q.message.chat_id, q.message.message_id)
        if user_row(target) and user_row(target)["role"] != "admin":
            await push_panel_keyboard(context.application.bot, target, note="Права на скрипты обновлены.")
        return

    if data.startswith("adm:pdone:"):
        target = int(data.split(":")[-1])
        await context.application.bot.send_message(
            chat_id=target,
            text="Доступ активирован.",
            reply_markup=build_user_keyboard(target),
        )
        await q.message.reply_text("Готово: доступ выдан.")
        return

    if data.startswith("ctor:vis:"):
        sid = data.removeprefix("ctor:vis:")
        if sid in UI_VISIBILITY_KEYS:
            if sid == UI_KEY_SETTINGS and uid != OWNER_ID:
                await q.message.reply_text("Нет доступа.")
                return
        elif not has_perm(uid, sid):
            await q.message.reply_text("Нет прав на эту кнопку.")
            return
        toggle_visibility(uid, sid)
        await edit_inline_kb(q, constructor_kb(uid))
        remember_open_inline_panel(uid, q.message.chat_id, q.message.message_id)
        await push_panel_keyboard(context.application.bot, uid, silent=True)
        return

    if data == "ctor:reqmenu":
        await callback_open_inline_panel(q, uid, "Запрос дополнительных кнопок:", request_more_kb(uid))
        return

    if data.startswith("ctor:req:"):
        sid = data.split(":")[-1]
        rid = open_access_request(uid, "button", {"script_id": sid})
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Одобрить", callback_data=f"adm:btnok:{rid}"),
                    InlineKeyboardButton("Отклонить", callback_data=f"adm:btnno:{rid}"),
                ],
                [InlineKeyboardButton(BTN_CLOSE, callback_data="menu:close")],
            ]
        )
        await context.application.bot.send_message(
            chat_id=OWNER_ID,
            text=f"Запрос кнопки: user=<code>{uid}</code> script=<code>{sid}</code> request=<code>{rid}</code>",
            parse_mode="HTML",
            reply_markup=kb,
        )
        await q.message.reply_text("Принято.")
        return

    if data.startswith("adm:btnok:"):
        rid = int(data.split(":")[-1])
        req = resolve_request(rid, OWNER_ID, True)
        if not req:
            await q.message.reply_text("Заявка уже обработана.")
            return
        payload = json.loads(req["payload_json"] or "{}")
        sid = payload.get("script_id", "")
        with db_conn() as c:
            c.execute(
                "REPLACE INTO user_script_permissions(user_id, script_id, enabled) VALUES(?, ?, 1)",
                (req["user_id"], sid),
            )
            c.execute(
                "REPLACE INTO user_button_visibility(user_id, script_id, visible) VALUES(?, ?, 1)",
                (req["user_id"], sid),
            )
        await context.application.bot.send_message(
            chat_id=req["user_id"],
            text="Доступ обновлён.",
            reply_markup=build_user_keyboard(req["user_id"]),
        )
        await q.message.reply_text("Запрос кнопки одобрен.")
        return

    if data.startswith("adm:btnno:"):
        rid = int(data.split(":")[-1])
        req = resolve_request(rid, OWNER_ID, False)
        if not req:
            await q.message.reply_text("Заявка уже обработана.")
            return
        await context.application.bot.send_message(chat_id=req["user_id"], text="Доступ не изменён.")
        await q.message.reply_text("Отклонено.")
        return

    if data.startswith("setlim:"):
        if uid != OWNER_ID:
            return
        mode = data.split(":")[-1]
        if mode == "strict":
            set_limits(600, 3, 900)
        elif mode == "balanced":
            set_limits(300, 4, 600)
        elif mode == "soft":
            set_limits(120, 6, 600)
        await q.message.reply_text("Лимиты обновлены.")
        return

    if data == "setcron:menu":
        if uid != OWNER_ID:
            return
        buttons = []
        for sid, s in scripts_map().items():
            buttons.append([InlineKeyboardButton(s["title"], callback_data=f"setcron:pick:{sid}")])
        buttons.append([InlineKeyboardButton(BTN_CLOSE, callback_data="menu:close")])
        await callback_open_inline_panel(
            q,
            uid,
            "Выберите скрипт для изменения времени (UTC):",
            InlineKeyboardMarkup(buttons),
        )
        return

    if data.startswith("setcron:pick:"):
        if uid != OWNER_ID:
            return
        sid = data.split(":")[-1]
        context.user_data["await_cron_sid"] = sid
        await q.message.reply_text(
            f"Отправьте время для {script_title(sid)} в формате HH:MM (МСК), например 19:35"
        )
        return

    if data == "cronauto:menu":
        if uid != OWNER_ID:
            return
        await callback_open_inline_panel(
            q,
            uid,
            "<b>Автообновление (cron)</b>\n"
            "Нажмите скрипт, чтобы включить или выключить задание в crontab.\n"
            "🟢 — включено, ⏸ — выключено (строка закомментирована), ❌ — нет строки в crontab.",
            cron_auto_menu_markup(),
        )
        return

    if data.startswith("cronauto:toggle:"):
        if uid != OWNER_ID:
            return
        sid = data.split(":")[-1]
        state, _ = script_cron_state(sid)
        if state == "missing":
            await q.message.reply_text(
                f"Для {script_title(sid)} нет строки задания в crontab. "
                "Добавьте её на сервере или сначала задайте время в «Изменить расписание»."
            )
            return
        enable = state != "enabled"
        ok, result = set_script_cron_enabled(sid, enable)
        if ok:
            await q.message.reply_text(result)
            audit(
                uid,
                "cron_auto_toggled",
                {"script_id": sid, "enabled": enable},
            )
            await edit_open_inline_panel(
                q,
                uid,
                "<b>Автообновление (cron)</b>\nНажмите скрипт, чтобы переключить.",
                cron_auto_menu_markup(),
            )
        else:
            await q.message.reply_text(f"Ошибка: {result}")
            audit(uid, "cron_auto_toggle_failed", {"script_id": sid, "enabled": enable, "error": result})
        return

    if data == "menu:settings":
        if uid != OWNER_ID:
            return
        lim = get_limits()
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Strict", callback_data="setlim:strict"),
                    InlineKeyboardButton("Balanced", callback_data="setlim:balanced"),
                    InlineKeyboardButton("Soft", callback_data="setlim:soft"),
                ],
                [InlineKeyboardButton("Автообновление скриптов", callback_data="cronauto:menu")],
                [InlineKeyboardButton("Изменить расписание скриптов", callback_data="setcron:menu")],
                [InlineKeyboardButton("Права пользователей (скрипты)", callback_data="adm:usersmenu")],
                [InlineKeyboardButton(BTN_CLOSE, callback_data="menu:close")],
            ]
        )
        await edit_open_inline_panel(
            q,
            uid,
            f"Текущие лимиты:\ncd={lim['cooldown_sec']}s\nrate={lim['rate_max']} / {lim['rate_window_sec']}s",
            kb,
        )
        return


async def execute_script(message, script_id: str, user_id: int) -> None:
    cfg = load_config()
    s = scripts_map().get(script_id)
    if not s:
        await message.reply_text("Неизвестный скрипт.")
        return
    row = user_row(user_id)
    is_admin = bool(row and row["role"] == "admin")

    if script_id in running_scripts:
        who = running_scripts[script_id]["user_id"]
        await message.reply_text(f"Скрипт уже выполняется пользователем {who}.")
        audit(user_id, "script_denied_busy", {"script_id": script_id, "owner": who})
        return

    if not is_admin:
        ok_cd, wait_cd = cooldown_check_and_set(user_id, script_id, skip=False)
        if not ok_cd:
            await message.reply_text(f"Откат активен. Подождите {wait_cd} сек.")
            audit(user_id, "script_denied_cooldown", {"script_id": script_id, "wait_sec": wait_cd})
            return
        ok_rate, wait_rate = rate_limit_check(user_id)
        if not ok_rate:
            await message.reply_text(f"Лимит запросов. Повторите через {wait_rate} сек.")
            audit(user_id, "script_denied_rate", {"script_id": script_id, "wait_sec": wait_rate})
            return

    cwd = cfg.get("scripts_dir", "/opt/scripts")
    argv = s.get("argv", [])
    timeout = int(s.get("timeout_sec", 7200))

    with db_conn() as c:
        run_id = c.execute(
            "INSERT INTO script_runs(user_id, script_id, started_at, status) VALUES(?, ?, ?, 'running')",
            (user_id, script_id, utc_now_ts()),
        ).lastrowid

    running_scripts[script_id] = {"user_id": user_id, "run_id": run_id}
    audit(user_id, "script_started", {"script_id": script_id, "run_id": run_id})
    await message.reply_text(f"Отчёт {script_title(script_id)} выполняется.")

    loop = asyncio.get_running_loop()
    code = -999
    out = ""
    err = ""
    status = "failed"
    start_ts = utc_now_ts()
    try:
        code, out, err = await loop.run_in_executor(None, lambda: run_subprocess(cwd, argv, timeout))
        status = "ok" if code == 0 else "failed"
    except subprocess.TimeoutExpired:
        err = f"Timeout {timeout}s"
        status = "timeout"
    except Exception as e:
        err = str(e)
        status = "failed"
    finally:
        running_scripts.pop(script_id, None)

    dur_ms = (utc_now_ts() - start_ts) * 1000
    with db_conn() as c:
        c.execute(
            "UPDATE script_runs SET finished_at=?, exit_code=?, status=?, duration_ms=? WHERE id=?",
            (utc_now_ts(), code, status, dur_ms, run_id),
        )
    audit(user_id, "script_finished", {"script_id": script_id, "run_id": run_id, "status": status, "exit": code})

    if status == "ok":
        await message.reply_text(f"Отчёт {script_title(script_id)} выполнился.")
    else:
        await message.reply_text(f"Отчёт {script_title(script_id)} не выполнился.")
    owner_head = (
        f"<b>Отчёт выполнения</b>\n"
        f"user=<code>{user_id}</code>\nscript=<code>{script_title(script_id)}</code>\n"
        f"status=<code>{status}</code> exit=<code>{code}</code> run_id=<code>{run_id}</code>\n"
        f"started={utc_str(start_ts)}"
    )
    await message.get_bot().send_message(chat_id=OWNER_ID, text=owner_head, parse_mode="HTML")
    await post_long_text_html(lambda t, parse_mode="HTML": message.get_bot().send_message(chat_id=OWNER_ID, text=t, parse_mode=parse_mode), "— STDOUT —\n" + out)
    await post_long_text_html(lambda t, parse_mode="HTML": message.get_bot().send_message(chat_id=OWNER_ID, text=t, parse_mode=parse_mode), "— STDERR —\n" + err)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_access_flow(update, context):
        return
    msg = update.effective_message
    uid = update.effective_user.id
    text = (msg.text or "").strip()

    if text == BTN_CLOSE:
        if not visible_meta_ui(uid, UI_KEY_CLOSE):
            await msg.reply_text(
                "Кнопка «Закрыть» скрыта в конструкторе. Откройте конструктор и включите её, либо отправьте /start.",
                reply_markup=build_user_keyboard(uid),
            )
            return
        await dismiss_open_inline_panel(msg.get_bot(), uid)
        await msg.reply_text("Панель скрыта. Для возврата отправьте /start", reply_markup=ReplyKeyboardRemove())
        audit(uid, "panel_closed")
        return

    pending_sid = context.user_data.get("await_cron_sid")
    if uid == OWNER_ID and pending_sid:
        m = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", text)
        if not m:
            await msg.reply_text("Неверный формат. Нужен HH:MM по МСК, например 19:35")
            return
        hh_msk = int(m.group(1))
        mm = int(m.group(2))
        hh_utc = (hh_msk - MSK_UTC_OFFSET_HOURS) % 24
        ok, result = set_cron_time_for_script(pending_sid, hh_utc, mm)
        context.user_data.pop("await_cron_sid", None)
        if ok:
            await msg.reply_text(f"Готово: {result}")
            audit(
                uid,
                "cron_time_changed",
                {
                    "script_id": pending_sid,
                    "hour_msk": hh_msk,
                    "hour_utc": hh_utc,
                    "minute": mm,
                },
            )
        else:
            await msg.reply_text(f"Ошибка: {result}")
            audit(
                uid,
                "cron_time_change_failed",
                {
                    "script_id": pending_sid,
                    "hour_msk": hh_msk,
                    "hour_utc": hh_utc,
                    "minute": mm,
                    "error": result,
                },
            )
        return

    if text == BTN_SCHEDULE:
        if not visible_meta_ui(uid, UI_KEY_SCHEDULE):
            await msg.reply_text(
                "Кнопка расписания скрыта. Включите её в конструкторе или отправьте /start.",
                reply_markup=build_user_keyboard(uid),
            )
            return
        await msg.reply_text(f"<pre>{html.escape(get_crontab_text()[:12000])}</pre>", parse_mode="HTML")
        audit(uid, "show_schedule")
        return

    if text == BTN_CONSTRUCTOR:
        await reply_open_inline_panel(msg, uid, "Конструктор кнопок:", constructor_kb(uid))
        audit(uid, "constructor_open")
        return

    if text == BTN_SETTINGS and uid == OWNER_ID:
        if not visible_meta_ui(uid, UI_KEY_SETTINGS):
            await msg.reply_text(
                "Кнопка «Настройки» скрыта. Включите её в конструкторе или отправьте /start.",
                reply_markup=build_user_keyboard(uid),
            )
            return
        lim = get_limits()
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Strict", callback_data="setlim:strict"),
                    InlineKeyboardButton("Balanced", callback_data="setlim:balanced"),
                    InlineKeyboardButton("Soft", callback_data="setlim:soft"),
                ],
                [InlineKeyboardButton("Автообновление скриптов", callback_data="cronauto:menu")],
                [InlineKeyboardButton("Изменить расписание скриптов", callback_data="setcron:menu")],
                [InlineKeyboardButton("Права пользователей (скрипты)", callback_data="adm:usersmenu")],
                [InlineKeyboardButton(BTN_CLOSE, callback_data="menu:close")],
            ]
        )
        await reply_open_inline_panel(
            msg,
            uid,
            f"Текущие лимиты:\ncd={lim['cooldown_sec']}s\nrate={lim['rate_max']} / {lim['rate_window_sec']}s",
            kb,
        )
        return

    sid = script_id_by_title(text, uid)
    if sid:
        if not has_perm(uid, sid):
            await msg.reply_text("Нет прав на этот скрипт.")
            audit(uid, "script_denied_perm", {"script_id": sid})
            return
        await execute_script(msg, sid, uid)
        return

    await msg.reply_text("Неизвестная кнопка. Нажмите /start для обновления панели.", reply_markup=build_user_keyboard(uid))


async def post_init(application: Application) -> None:
    db_init()
    await application.bot.set_my_commands([], scope=BotCommandScopeDefault())
    await application.bot.set_my_commands(
        [BotCommand("start", "Открыть панель"), BotCommand("audit_last", "Последние действия (owner)")],
        scope=BotCommandScopeChat(chat_id=OWNER_ID),
    )
    try:
        await application.bot.set_my_name("ServerConsoleBot", language_code="ru")
    except Exception as e:
        log.warning("set_my_name: %s", e)
    log.info("Started with owner=%s", OWNER_ID)


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).defaults(Defaults(parse_mode="HTML")).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("audit_last", cmd_audit_last))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
