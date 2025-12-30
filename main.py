import os
import asyncio
import random
import logging
import string
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
RESET_AT = time(5, 0)  # 05:00 Amsterdam boundary

CHAT_ID = -1003328329377  # <-- FIX: alleen cijfers
DAILY_THREAD_ID = None
VERIFY_THREAD_ID = 4

PHOTO_PATH = "banner.jpg"

# TEST intervals (seconds) - jij gebruikt test tijden
DAILY_SECONDS = 17
VERIFY_SECONDS = 15
JOIN_DELAY_SECONDS = 5 * 60
ACTIVITY_SECONDS = 15

DELETE_DAILY_SECONDS = 34
DELETE_LEAVE_SECONDS = 10000 * 6000

# ===== OPTIE B: prune verify-message logging =====
BOT_MSG_RETENTION_DAYS = 2
BOT_MSG_MAX_ROWS = 20000
BOT_MSG_PRUNE_EVERY = 200
BOT_MSG_PRUNE_COUNTER = 0

# ================== DB GLOBALS ==================
DB_POOL: asyncpg.Pool | None = None
JOINED_NAMES: list[str] = []  # in-memory cache

# ================== CONTENT ==================
WELCOME_TEXT = (
    "Welcome to THE 18+ HUB Telegram group 😈\n\n"
    "⚠️ To be admitted to the group, please share the link!\n"
    "Also, confirm you're not a bot by clicking the \"Open group✅\" button\n"
    "below, and invite 5 people by sharing the link with them – via TELEGRAM "
    "REDDIT.COM or X.COM"
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

# ================== SAFETY: TASK CRASH LOGGING ==================
def safe_create_task(coro, name: str):
    task = asyncio.create_task(coro)

    def _done(t: asyncio.Task):
        try:
            t.result()
        except Exception:
            logging.exception("Task crashed: %s", name)

    task.add_done_callback(_done)
    return task

# ================== CYCLE HELPERS ==================
def current_cycle_id(now: datetime) -> str:
    local = now.astimezone(TZ)
    if local.time() < RESET_AT:
        cycle_date = (local.date() - timedelta(days=1))
    else:
        cycle_date = local.date()
    return cycle_date.isoformat()

# ================== DB ==================
async def db_init():
    global DB_POOL
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt. Zet DATABASE_URL in je BOT service variables.")

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

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS bot_verify_messages (
            message_id BIGINT PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)

        await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_bot_verify_messages_created_at
        ON bot_verify_messages(created_at);
        """)

    logging.info("DB initialized ok")

async def db_load_joined_names_into_memory():
    global JOINED_NAMES
    async with DB_POOL.acquire() as conn:
        rows = await conn.fetch("SELECT name FROM joined_names ORDER BY first_seen ASC;")
    JOINED_NAMES = [r["name"] for r in rows]
    logging.info("Loaded %d joined names from DB", len(JOINED_NAMES))

async def db_remember_joined_name(name: str):
    name = (name or "").strip()
    if not name:
        return

    # retry 3x
    for attempt in range(3):
        try:
            async with DB_POOL.acquire() as conn:
                await conn.execute(
                    "INSERT INTO joined_names(name) VALUES($1) ON CONFLICT (name) DO NOTHING;",
                    name
                )
            break
        except Exception:
            logging.exception("DB remember name failed attempt=%s", attempt + 1)
            await asyncio.sleep(1 + attempt)

    if name not in JOINED_NAMES:
        JOINED_NAMES.append(name)

async def db_is_used(name: str) -> bool:
    cid = current_cycle_id(datetime.now(TZ))
    async with DB_POOL.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM used_names WHERE cycle_id = $1::date AND name = $2 LIMIT 1;",
            cid, name
        )
    return row is not None

async def db_mark_used(name: str):
    cid = current_cycle_id(datetime.now(TZ))
    for attempt in range(3):
        try:
            async with DB_POOL.acquire() as conn:
                await conn.execute(
                    "INSERT INTO used_names(cycle_id, name) VALUES($1::date, $2) ON CONFLICT DO NOTHING;",
                    cid, name
                )
            return
        except Exception:
            logging.exception("DB mark used failed attempt=%s", attempt + 1)
            await asyncio.sleep(1 + attempt)

async def db_track_bot_verify_message_id(message_id: int):
    """
    Optie B: log message ids, maar prune zodat DB niet vol loopt.
    """
    global BOT_MSG_PRUNE_COUNTER

    for attempt in range(3):
        try:
            async with DB_POOL.acquire() as conn:
                await conn.execute(
                    "INSERT INTO bot_verify_messages(message_id) VALUES($1) ON CONFLICT DO NOTHING;",
                    int(message_id)
                )

                BOT_MSG_PRUNE_COUNTER += 1
                if BOT_MSG_PRUNE_COUNTER % BOT_MSG_PRUNE_EVERY != 0:
                    return

                # 1) retention
                await conn.execute(
                    f"DELETE FROM bot_verify_messages "
                    f"WHERE created_at < NOW() - INTERVAL '{BOT_MSG_RETENTION_DAYS} days';"
                )

                # 2) cap max rows (delete oldest beyond cap)
                await conn.execute(
                    """
                    DELETE FROM bot_verify_messages
                    WHERE message_id IN (
                        SELECT message_id
                        FROM bot_verify_messages
                        ORDER BY created_at DESC
                        OFFSET $1
                    );
                    """,
                    BOT_MSG_MAX_ROWS
                )
            return
        except Exception:
            logging.exception("DB track bot message failed attempt=%s", attempt + 1)
            await asyncio.sleep(1 + attempt)

# ================== TELEGRAM SEND WRAPPERS ==================
async def safe_send(coro_factory, what: str):
    """
    coro_factory: lambda -> awaitable Telegram API call
    Handles RetryAfter + transient errors so loops don't die.
    """
    while True:
        try:
            return await coro_factory()
        except RetryAfter as e:
            logging.warning("%s rate limited. Sleep %s sec", what, e.retry_after)
            await asyncio.sleep(e.retry_after + 1)
        except (TimedOut, NetworkError) as e:
            logging.warning("%s transient network error: %s. retry in 3s", what, e)
            await asyncio.sleep(3)
        except Forbidden as e:
            logging.exception("%s forbidden (rights/bot kicked?): %s", what, e)
            raise
        except BadRequest as e:
            logging.exception("%s bad request: %s", what, e)
            raise
        except Exception as e:
            logging.exception("%s unexpected error: %s", what, e)
            await asyncio.sleep(2)

async def delete_later(bot, chat_id, message_id, delay_seconds: int):
    await asyncio.sleep(delay_seconds)
    try:
        await safe_send(lambda: bot.delete_message(chat_id=chat_id, message_id=message_id), "delete_message")
    except Exception:
        pass

async def send_text(bot, chat_id, thread_id, text):
    if thread_id is None:
        msg = await safe_send(lambda: bot.send_message(chat_id=chat_id, text=text), "send_message(main)")
        return msg

    msg = await safe_send(
        lambda: bot.send_message(chat_id=chat_id, message_thread_id=thread_id, text=text),
        f"send_message(thread={thread_id})"
    )

    if thread_id == VERIFY_THREAD_ID:
        await db_track_bot_verify_message_id(msg.message_id)

    return msg

async def send_photo(bot, chat_id, thread_id, photo_path, caption, reply_markup):
    # Let op: als banner.jpg mist, task crasht — safe_create_task laat het zien.
    def _factory(photo_file):
        if thread_id is None:
            return lambda: bot.send_photo(
                chat_id=chat_id,
                photo=photo_file,
                caption=caption,
                reply_markup=reply_markup,
                has_spoiler=True
            )
        return lambda: bot.send_photo(
            chat_id=chat_id,
            message_thread_id=thread_id,
            photo=photo_file,
            caption=caption,
            reply_markup=reply_markup,
            has_spoiler=True
        )

    with open(photo_path, "rb") as photo:
        msg = await safe_send(_factory(photo), "send_photo")

    if thread_id == VERIFY_THREAD_ID:
        await db_track_bot_verify_message_id(msg.message_id)

    return msg

# ================== RESET LOOP (05:00) ==================
async def reset_loop():
    while True:
        now = datetime.now(TZ)
        target = datetime.combine(now.date(), RESET_AT, tzinfo=TZ)
        if now >= target:
            target = target + timedelta(days=1)

        await asyncio.sleep(max(1, int((target - now).total_seconds())))
        logging.info("Cycle boundary reached at 05:00 (used_names are naturally per cycle_id)")

# ================== 05:00 CLEANUP VERIFY TOPIC (BOT MESSAGES) ==================
async def cleanup_verify_topic_loop(app: Application):
    while True:
        now = datetime.now(TZ)
        target = datetime.combine(now.date(), RESET_AT, tzinfo=TZ)
        if now >= target:
            target = target + timedelta(days=1)

        await asyncio.sleep(max(1, int((target - now).total_seconds())))

        async with DB_POOL.acquire() as conn:
            rows = await conn.fetch("SELECT message_id FROM bot_verify_messages;")

        ids = [int(r["message_id"]) for r in rows]
        kept = []

        for mid in ids:
            try:
                await safe_send(
                    lambda: app.bot.delete_message(chat_id=CHAT_ID, message_id=mid),
                    "cleanup_delete_message"
                )
            except Exception:
                kept.append(mid)

        # Rewrite table with kept only
        async with DB_POOL.acquire() as conn:
            await conn.execute("TRUNCATE TABLE bot_verify_messages;")
            if kept:
                await conn.executemany(
                    "INSERT INTO bot_verify_messages(message_id) VALUES($1) ON CONFLICT DO NOTHING;",
                    [(m,) for m in kept]
                )

        logging.info("Cleanup verify-topic bot messages at 05:00 done. kept=%d", len(kept))

# ================== BUTTON POPUP ==================
async def on_open_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer(
        "Can’t acces the group, because unfortunately you haven’t shared the group 3 times yet.",
        show_alert=True
    )

# ================== JOIN -> AFTER DELAY ==================
async def announce_join_after_delay(context: ContextTypes.DEFAULT_TYPE, name: str):
    await asyncio.sleep(JOIN_DELAY_SECONDS)
    name = (name or "").strip()
    if not name:
        return

    if await db_is_used(name):
        return

    await send_text(context.bot, CHAT_ID, VERIFY_THREAD_ID, unlocked_text(name))
    await db_mark_used(name)

async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.new_chat_members:
        return
    if not update.effective_chat or update.effective_chat.id != CHAT_ID:
        return

    for member in update.message.new_chat_members:
        name = (member.full_name or "").strip()
        if name:
            await db_remember_joined_name(name)
            safe_create_task(announce_join_after_delay(context, name), f"announce_join_after_delay({name})")

# ================== DAILY POST ==================
async def daily_post_loop(app: Application):
    last_msg_id = None

    while True:
        msg = await send_photo(
            app.bot, CHAT_ID, DAILY_THREAD_ID,
            PHOTO_PATH, WELCOME_TEXT, build_keyboard()
        )

        if last_msg_id:
            safe_create_task(delete_later(app.bot, CHAT_ID, last_msg_id, DELETE_DAILY_SECONDS), "delete_old_daily")
        last_msg_id = msg.message_id

        try:
            await safe_send(lambda: app.bot.pin_chat_message(chat_id=CHAT_ID, message_id=msg.message_id), "pin_chat_message")
        except Exception:
            pass

        await asyncio.sleep(DAILY_SECONDS)

# ================== VERIFY LOOP ==================
async def verify_random_joiner_loop(app: Application):
    while True:
        if JOINED_NAMES:
            name = random.choice([n for n in JOINED_NAMES if n])
            if name and (not await db_is_used(name)):
                await send_text(app.bot, CHAT_ID, VERIFY_THREAD_ID, unlocked_text(name))
                await db_mark_used(name)

        await asyncio.sleep(VERIFY_SECONDS)

# ================== ALIAS GENERATOR (jouw originele) ==================
NL_CITY_CODES = [
    "010","020","030","040","050","070","073","076","079","071","072","074","075","078"
]
SEPARATORS = ["_", ".", "-"]
PREFIXES = ["x", "mr", "its", "real", "official", "the", "iam", "nl", "dm", "vip", "urban", "city", "only"]
EMOJIS = ["🔥", "💎", "👻", "⚡", "🚀", "✅"]

def _name_fragments_from_joined():
    frags = []
    for n in JOINED_NAMES:
        n = (n or "").strip().lower()
        n = "".join(ch for ch in n if ch.isalpha())
        if len(n) >= 4:
            for _ in range(2):
                start = random.randint(0, max(0, len(n) - 3))
                frag_len = random.randint(2, 4)
                frag = n[start:start+frag_len]
                if frag and frag not in frags:
                    frags.append(frag)
    return frags

def random_alias_from_joined():
    frags = _name_fragments_from_joined()
    if not frags:
        frags = ["nova", "sky", "dex", "luna", "vex", "rio", "mira", "zen"]

    code = random.choice(NL_CITY_CODES)
    sep = random.choice(SEPARATORS)
    prefix = random.choice(PREFIXES)
    frag1 = random.choice(frags)
    frag2 = random.choice(frags)
    while frag2 == frag1:
        frag2 = random.choice(frags)

    digits = "".join(random.choices(string.digits, k=random.randint(2, 4)))

    style = random.choice([
        "prefix_frag_digits",
        "frag_code_digits",
        "frag_frag_digits",
        "prefix_frag_code",
        "prefix_frag_sep_frag_digits",
        "frag_digits_emoji",
    ])

    if style == "prefix_frag_digits":
        base = f"{prefix}{sep}{frag1}{digits}"
    elif style == "frag_code_digits":
        base = f"{frag1}{sep}{code}{sep}{digits}"
    elif style == "frag_frag_digits":
        base = f"{frag1}{sep}{frag2}{digits}"
    elif style == "prefix_frag_code":
        base = f"{prefix}{sep}{frag1}{sep}{code}"
    elif style == "frag_digits_emoji":
        base = f"{frag1}{digits}{random.choice(EMOJIS)}"
    else:
        base = f"{prefix}{sep}{frag1}{sep}{frag2}{sep}{digits}"

    base = base.strip()
    if not base or base.isdigit():
        base = f"user{sep}{digits}"
    return base

# ================== ACTIVITY LOOP ==================
async def activity_loop(app: Application):
    while True:
        alias = random_alias_from_joined()
        text = f"{alias} Successfully unlocked the group✅"

        msg = await send_text(app.bot, CHAT_ID, VERIFY_THREAD_ID, text)

        safe_create_task(delete_later(app.bot, CHAT_ID, msg.message_id, DELETE_LEAVE_SECONDS), "delete_activity_msg")
        await asyncio.sleep(ACTIVITY_SECONDS)

# ================== INIT ==================
async def post_init(app: Application):
    me = await app.bot.get_me()
    logging.info("Bot started: @%s", me.username)

    await db_init()
    await db_load_joined_names_into_memory()

    # quick startup test to main chat (helps debug rights/chat_id)
    try:
        await safe_send(lambda: app.bot.send_message(chat_id=CHAT_ID, text="✅ bot gestart (startup test)"), "startup_test")
        logging.info("Startup test message sent ok to CHAT_ID=%s", CHAT_ID)
    except Exception:
        logging.exception("Startup test FAILED. Check CHAT_ID, bot membership, rights.")

    safe_create_task(reset_loop(), "reset_loop")
    safe_create_task(cleanup_verify_topic_loop(app), "cleanup_verify_topic_loop")
    safe_create_task(daily_post_loop(app), "daily_post_loop")
    safe_create_task(verify_random_joiner_loop(app), "verify_random_joiner_loop")
    safe_create_task(activity_loop(app), "activity_loop")

def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    app.add_handler(CallbackQueryHandler(on_open_group, pattern="^open_group$"))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members))

    app.run_polling()

if __name__ == "__main__":
    main()
