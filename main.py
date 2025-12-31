import os
import asyncio
import random
import logging
import string
import time as _time
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import asyncpg

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)
from telegram.error import RetryAfter, TimedOut, NetworkError, Forbidden, BadRequest

logging.basicConfig(level=logging.INFO)

# ================== CONFIG ==================
TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

TZ = ZoneInfo("Europe/Amsterdam")
RESET_AT = time(5, 0)

CHAT_ID = -1003328329377
DAILY_THREAD_ID = None
VERIFY_THREAD_ID = 4

PHOTO_PATH = "banner.jpg"

DAILY_SECONDS = 17
VERIFY_SECONDS = 15
JOIN_DELAY_SECONDS = 5 * 60
ACTIVITY_SECONDS = 15

DELETE_DAILY_SECONDS = 34
DELETE_LEAVE_SECONDS = 10000 * 6000

ENABLE_DAILY = os.getenv("ENABLE_DAILY", "1") == "1"
ENABLE_VERIFY = os.getenv("ENABLE_VERIFY", "1") == "1"
ENABLE_ACTIVITY = os.getenv("ENABLE_ACTIVITY", "1") == "1"
ENABLE_CLEANUP = os.getenv("ENABLE_CLEANUP", "1") == "1"

TELEGRAM_PAUSE_UNTIL = 0.0

DB_POOL: asyncpg.Pool | None = None
JOINED_NAMES: list[str] = []

# ================== CONTENT ==================
WELCOME_TEXT = (
    "Welcome to THE 18+ HUB Telegram group 😈\n\n"
    "⚠️ To be admitted to the group, please share the link!\n"
    "Also, confirm you're not a bot by clicking the \"Open group✅\" button\n"
    "below, and invite 5 people by sharing the link with them."
)

SHARE_URL = (
    "https://t.me/share/url?url=%20all%20exclusive%E2%80%94content%20"
    "https%3A%2F%2Ft.me%2F%2BAiDsi2LccXJmMjlh"
)

def build_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 0/3", url=SHARE_URL)],
        [InlineKeyboardButton("Open group✅", callback_data="open_group")]
    ])

def unlocked_text(name: str) -> str:
    return f"{name} Successfully unlocked the group✅"

# ================== HELPERS ==================
def safe_create_task(coro, name: str):
    task = asyncio.create_task(coro)

    def _done(t: asyncio.Task):
        try:
            t.result()
        except Exception:
            logging.exception("Task crashed: %s", name)

    task.add_done_callback(_done)
    return task

# ================== CYCLE FIX (BELANGRIJK) ==================
def current_cycle_id(now: datetime):
    local = now.astimezone(TZ)
    if local.time() < RESET_AT:
        return local.date() - timedelta(days=1)
    return local.date()

# ================== DB ==================
async def db_init():
    global DB_POOL
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt")

    DB_POOL = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)

    async with DB_POOL.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS joined_names (
            name TEXT PRIMARY KEY,
            first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS used_names (
            cycle_id DATE NOT NULL,
            name TEXT NOT NULL,
            PRIMARY KEY (cycle_id, name)
        );
        """)

    logging.info("DB initialized ok")

async def db_load_joined_names_into_memory():
    global JOINED_NAMES
    async with DB_POOL.acquire() as conn:
        rows = await conn.fetch("SELECT name FROM joined_names;")
    JOINED_NAMES = [r["name"] for r in rows]

async def db_remember_joined_name(name: str):
    if not DB_POOL:
        return
    name = name.strip()
    async with DB_POOL.acquire() as conn:
        await conn.execute(
            "INSERT INTO joined_names(name) VALUES($1) ON CONFLICT DO NOTHING;",
            name
        )
    if name not in JOINED_NAMES:
        JOINED_NAMES.append(name)

async def db_is_used(name: str) -> bool:
    cid = current_cycle_id(datetime.now(TZ))
    async with DB_POOL.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM used_names WHERE cycle_id=$1 AND name=$2;",
            cid, name
        )
    return row is not None

async def db_mark_used(name: str):
    cid = current_cycle_id(datetime.now(TZ))
    async with DB_POOL.acquire() as conn:
        await conn.execute(
            "INSERT INTO used_names(cycle_id, name) VALUES($1,$2) ON CONFLICT DO NOTHING;",
            cid, name
        )

# ================== TELEGRAM SAFE SEND ==================
async def safe_send(coro_factory):
    global TELEGRAM_PAUSE_UNTIL
    if _time.time() < TELEGRAM_PAUSE_UNTIL:
        return None

    try:
        return await coro_factory()
    except (RetryAfter, TimedOut, NetworkError):
        TELEGRAM_PAUSE_UNTIL = _time.time() + 60
    except (Forbidden, BadRequest):
        return None

# ================== LOOPS ==================
async def verify_random_joiner_loop(app: Application):
    while True:
        if JOINED_NAMES:
            name = random.choice(JOINED_NAMES)
            if not await db_is_used(name):
                await safe_send(
                    lambda: app.bot.send_message(
                        chat_id=CHAT_ID,
                        message_thread_id=VERIFY_THREAD_ID,
                        text=unlocked_text(name)
                    )
                )
                await db_mark_used(name)
        await asyncio.sleep(VERIFY_SECONDS)

# ================== HANDLERS ==================
async def on_open_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer(
        "You need to share the group first.",
        show_alert=True
    )

async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        return
    for m in update.message.new_chat_members:
        await db_remember_joined_name(m.full_name)

# ================== INIT ==================
async def post_init(app: Application):
    await db_init()
    await db_load_joined_names_into_memory()

    if ENABLE_VERIFY:
        safe_create_task(verify_random_joiner_loop(app), "verify_loop")

def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CallbackQueryHandler(on_open_group, pattern="^open_group$"))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members))
    app.run_polling()

if __name__ == "__main__":
    main()