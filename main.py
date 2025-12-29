import os
import json
import asyncio
import random
import logging
import string
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO)

# ================== CONFIG ==================
TOKEN = os.getenv("BOT_TOKEN")

TZ = ZoneInfo("Europe/Amsterdam")
RESET_AT = time(5, 0)  # 05:00 NL-tijd reset

CHAT_ID = -1003328329377
DAILY_THREAD_ID = None   # General topic (None = main chat)
VERIFY_THREAD_ID = 4     # Topic 4

PHOTO_PATH = "image (6).png"

DAILY_SECONDS = 17
VERIFY_SECONDS = 15          # elke 2 uur (echte joiners)
JOIN_DELAY_SECONDS = 5 * 60
ACTIVITY_SECONDS = 15       # elke 12 uur activity update

# ✅ Delete regels zoals jij wilt
DELETE_DAILY_SECONDS = 34              # alleen oude daily post
DELETE_LEAVE_SECONDS = 10000 * 6000              # alleen activity/leave berichten

NAMES_FILE = "joined_names.json"
USED_FILE = "used_names_cycle.json"

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

# ================== STORAGE: JOINED NAMES ==================
def load_joined_names():
    try:
        with open(NAMES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []

def save_joined_names(names):
    with open(NAMES_FILE, "w", encoding="utf-8") as f:
        json.dump(names, f, ensure_ascii=False, indent=2)

JOINED_NAMES = load_joined_names()

def remember_joined_name(name: str):
    name = (name or "").strip()
    if name and name not in JOINED_NAMES:
        JOINED_NAMES.append(name)
        save_joined_names(JOINED_NAMES)

# ================== STORAGE: USED NAMES PER CYCLE (reset 05:00) ==================
def current_cycle_id(now: datetime) -> str:
    local = now.astimezone(TZ)
    if local.time() < RESET_AT:
        cycle_date = (local.date() - timedelta(days=1))
    else:
        cycle_date = local.date()
    return cycle_date.isoformat()

def load_used_state():
    try:
        with open(USED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            cycle = data.get("cycle_id")
            used = set(data.get("used", []))
            return cycle, used
    except Exception:
        return None, set()

def save_used_state(cycle_id: str, used_set: set[str]):
    with open(USED_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {"cycle_id": cycle_id, "used": sorted(list(used_set))},
            f, ensure_ascii=False, indent=2
        )

USED_CYCLE_ID, USED_NAMES = load_used_state()

def ensure_cycle_is_current():
    global USED_CYCLE_ID, USED_NAMES
    now = datetime.now(TZ)
    cid = current_cycle_id(now)
    if USED_CYCLE_ID != cid:
        USED_CYCLE_ID = cid
        USED_NAMES = set()
        save_used_state(USED_CYCLE_ID, USED_NAMES)

def mark_used(name: str):
    ensure_cycle_is_current()
    USED_NAMES.add(name)
    save_used_state(USED_CYCLE_ID, USED_NAMES)

def is_used(name: str) -> bool:
    ensure_cycle_is_current()
    return name in USED_NAMES

# ================== HELPERS ==================
async def delete_later(bot, chat_id, message_id, delay_seconds: int):
    await asyncio.sleep(delay_seconds)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass

async def send_text(bot, chat_id, thread_id, text):
    if thread_id is None:
        return await bot.send_message(chat_id=chat_id, text=text)
    return await bot.send_message(chat_id=chat_id, message_thread_id=thread_id, text=text)

async def send_photo(bot, chat_id, thread_id, photo_path, caption, reply_markup):
    with open(photo_path, "rb") as photo:
        if thread_id is None:
            return await bot.send_photo(
                chat_id=chat_id,
                photo=photo,
                caption=caption,
                reply_markup=reply_markup,
                has_spoiler=True
            )
        return await bot.send_photo(
            chat_id=chat_id,
            message_thread_id=thread_id,
            photo=photo,
            caption=caption,
            reply_markup=reply_markup,
            has_spoiler=True
        )

# ================== RESET LOOP (05:00) ==================
async def reset_loop():
    global USED_CYCLE_ID, USED_NAMES
    while True:
        now = datetime.now(TZ)
        target = datetime.combine(now.date(), RESET_AT, tzinfo=TZ)
        if now >= target:
            target = target + timedelta(days=1)

        await asyncio.sleep(max(1, int((target - now).total_seconds())))
        USED_CYCLE_ID = current_cycle_id(datetime.now(TZ))
        USED_NAMES = set()
        save_used_state(USED_CYCLE_ID, USED_NAMES)
        logging.info("Reset used names at 05:00")

# ================== BUTTON POPUP (CHANGED) ==================
async def on_open_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer(
        "Can’t acces the group, because unfortunately you haven’t shared the group 3 times yet.",
        show_alert=True
    )

# ================== JOIN -> NA 15 MIN (blijft staan) ==================
async def announce_join_after_delay(context: ContextTypes.DEFAULT_TYPE, name: str):
    await asyncio.sleep(JOIN_DELAY_SECONDS)
    name = (name or "").strip()
    if not name or is_used(name):
        return

    await send_text(context.bot, CHAT_ID, VERIFY_THREAD_ID, unlocked_text(name))
    mark_used(name)

async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.new_chat_members:
        return
    if not update.effective_chat or update.effective_chat.id != CHAT_ID:
        return

    for member in update.message.new_chat_members:
        name = (member.full_name or "").strip()
        if name:
            remember_joined_name(name)
            asyncio.create_task(announce_join_after_delay(context, name))

# ================== DAILY POST (oude daily na 10 sec weg) ==================
async def daily_post_loop(app: Application):
    last_msg_id = None

    while True:
        msg = await send_photo(
            app.bot, CHAT_ID, DAILY_THREAD_ID,
            PHOTO_PATH, WELCOME_TEXT, build_keyboard()
        )

        # ✅ alleen het vorige daily bericht verwijderen na 10 sec
        if last_msg_id:
            asyncio.create_task(
                delete_later(app.bot, CHAT_ID, last_msg_id, DELETE_DAILY_SECONDS)
            )
        last_msg_id = msg.message_id

        # pinnen
        try:
            await app.bot.pin_chat_message(chat_id=CHAT_ID, message_id=msg.message_id)
        except Exception:
            pass

        await asyncio.sleep(DAILY_SECONDS)

# ================== VERIFY EVERY 2 HOURS (blijft staan) ==================
async def verify_random_joiner_loop(app: Application):
    while True:
        ensure_cycle_is_current()

        available = [n for n in JOINED_NAMES if n and (n not in USED_NAMES)]
        if available:
            name = random.choice(available)
            await send_text(app.bot, CHAT_ID, VERIFY_THREAD_ID, unlocked_text(name))
            mark_used(name)

        await asyncio.sleep(VERIFY_SECONDS)

# ================== IMPROVED RANDOM ALIAS GENERATOR ==================
NL_CITY_CODES = [
    "010",  # Rotterdam
    "020",  # Amsterdam
    "030",  # Utrecht
    "040",  # Eindhoven
    "050",  # Groningen
    "070",  # Den Haag
    "073",  # Den Bosch
    "076",  # Breda
    "079",  # Zoetermeer
    "071",  # Leiden
    "072",  # Alkmaar
    "074",  # Hengelo
    "075",  # Zaandam
    "078",  # Dordrecht
]

SEPARATORS = ["_", ".", "-"]
PREFIXES = ["x", "mr", "its", "real", "official", "the", "iam", "nl", "dm", "vip", "urban", "city", "only"]
EMOJIS = ["🔥", "💎", "👻", "⚡", "🚀", "✅"]

def _name_fragments_from_joined():
    frags = []
    for n in JOINED_NAMES:
        n = (n or "").strip().lower()
        # alleen letters
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
    """
    Maakt echt random aliassen:
    - NL netnummer / stads-code erin
    - prefix erin
    - 2 fragments uit opgeslagen namen (niet 1 naam copy/paste)
    - cijfers 2-4
    - separator mix
    - soms emoji achteraan
    """
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

# ================== ACTIVITY LOOP (delete na 15 sec) ==================
async def activity_loop(app: Application):
    while True:
        alias = random_alias_from_joined()
        text = f"{alias} Successfully unlocked the group✅"   # <- pas dit aan als je wil

        msg = await send_text(app.bot, CHAT_ID, VERIFY_THREAD_ID, text)

        # ✅ alleen deze messages na 15 sec verwijderen
        asyncio.create_task(
            delete_later(app.bot, CHAT_ID, msg.message_id, DELETE_LEAVE_SECONDS)
        )

        await asyncio.sleep(ACTIVITY_SECONDS)

# ================== INIT ==================
async def post_init(app: Application):
    ensure_cycle_is_current()
    asyncio.create_task(reset_loop())
    asyncio.create_task(daily_post_loop(app))
    asyncio.create_task(verify_random_joiner_loop(app))
    asyncio.create_task(activity_loop(app))

def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    app.add_handler(CallbackQueryHandler(on_open_group, pattern="^open_group$"))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members))

    app.run_polling()

if __name__ == "__main__":
    main()
