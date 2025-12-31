import os
import asyncio
import logging
from datetime import date, datetime, timezone

import asyncpg
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# ---------------------------
# Logging
# ---------------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")


# ---------------------------
# Config (Railway env vars)
# ---------------------------
def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("Missing env var BOT_TOKEN")

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()  # Railway geeft deze
if not DATABASE_URL:
    raise RuntimeError("Missing env var DATABASE_URL")

ENABLE_CLEANUP = env_bool("ENABLE_CLEANUP", False)
ENABLE_DAILY = env_bool("ENABLE_DAILY", False)
ENABLE_ACTIVITY = env_bool("ENABLE_ACTIVITY", False)
ENABLE_VERIFY = env_bool("ENABLE_VERIFY", True)

# Polling (veiligere defaults)
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "1.0"))
READ_TIMEOUT = float(os.getenv("READ_TIMEOUT", "30"))
CONNECT_TIMEOUT = float(os.getenv("CONNECT_TIMEOUT", "15"))
WRITE_TIMEOUT = float(os.getenv("WRITE_TIMEOUT", "30"))
POOL_MIN = int(os.getenv("DB_POOL_MIN", "1"))
POOL_MAX = int(os.getenv("DB_POOL_MAX", "5"))

# Let op: die 409 Conflict komt meestal door 2 instanties tegelijk.
# Dit helpt een beetje, maar de échte fix is "maar 1 instance" draaien.
DROP_PENDING_UPDATES = env_bool("DROP_PENDING_UPDATES", True)

log.info("ENABLE_CLEANUP=%s", int(ENABLE_CLEANUP))
log.info("ENABLE_DAILY=%s", int(ENABLE_DAILY))
log.info("ENABLE_ACTIVITY=%s", int(ENABLE_ACTIVITY))
log.info("ENABLE_VERIFY=%s", int(ENABLE_VERIFY))


# ---------------------------
# Database helper
# ---------------------------
class Storage:
    def __init__(self) -> None:
        self.pool: asyncpg.Pool | None = None

    async def init_db(self) -> None:
        # asyncpg gebruikt DATABASE_URL direct
        self.pool = await asyncpg.create_pool(
            dsn=DATABASE_URL,
            min_size=POOL_MIN,
            max_size=POOL_MAX,
            command_timeout=30,
        )
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS used_names (
                    name TEXT PRIMARY KEY,
                    used_on DATE NOT NULL
                );
                """
            )
        log.info("DB initialized ok")

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()

    async def is_used_today(self, name: str) -> bool:
        """
        CHECK: is deze naam al gebruikt vandaag?
        FIX voor jouw crash:
        - asyncpg verwacht DATE type voor date-kolom
        - dus NIET '2025-12-31' als string geven, maar date.today()
        """
        assert self.pool is not None
        today = date.today()  # <-- dit is een echte date, geen string
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM used_names WHERE name=$1 AND used_on=$2;",
                name,
                today,
            )
        return row is not None

    async def mark_used_today(self, name: str) -> None:
        assert self.pool is not None
        today = date.today()
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO used_names(name, used_on)
                VALUES ($1, $2)
                ON CONFLICT (name) DO UPDATE SET used_on=EXCLUDED.used_on;
                """,
                name,
                today,
            )


st = Storage()


# ---------------------------
# Telegram handlers
# ---------------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("✅ Bot draait. Gebruik /ping of /used <naam>.")


async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    await update.message.reply_text(f"pong 🟢 {now}")


async def used_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Gebruik: /used <naam>")
        return
    name = " ".join(context.args).strip()
    used = await st.is_used_today(name)
    if used:
        await update.message.reply_text(f"⚠️ '{name}' is AL gebruikt vandaag.")
    else:
        await st.mark_used_today(name)
        await update.message.reply_text(f"✅ '{name}' gemarkeerd als gebruikt voor vandaag.")


# ---------------------------
# Background task (voorbeeld)
# ---------------------------
async def verify_random_joiner_loop(app: Application) -> None:
    """
    Alleen als ENABLE_VERIFY=1.
    Dit is een voorbeeldloop. Vul hier je echte logic in.
    """
    log.info("verify_random_joiner_loop started")
    while True:
        try:
            # voorbeeld: elke 60 sec iets doen
            await asyncio.sleep(60)
            # ... jouw verify logic ...
        except asyncio.CancelledError:
            log.info("verify_random_joiner_loop cancelled")
            raise
        except Exception:
            log.exception("verify_random_joiner_loop crashed (will continue)")
            await asyncio.sleep(5)


# ---------------------------
# Main
# ---------------------------
async def main_async() -> None:
    await st.init_db()

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        # PTB timeouts (helpt tegen timeouts in logs)
        .read_timeout(READ_TIMEOUT)
        .write_timeout(WRITE_TIMEOUT)
        .connect_timeout(CONNECT_TIMEOUT)
        .build()
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("ping", ping_cmd))
    app.add_handler(CommandHandler("used", used_cmd))

    # background tasks starten nadat app gestart is
    async def on_startup(_: Application) -> None:
        if ENABLE_VERIFY:
            _.create_task(verify_random_joiner_loop(_))

    async def on_shutdown(_: Application) -> None:
        await st.close()

    app.post_init = on_startup
    app.post_shutdown = on_shutdown

    # Polling starten
    # drop_pending_updates helpt “oude updates” weggooien na redeploy.
    await app.run_polling(
        poll_interval=POLL_INTERVAL,
        drop_pending_updates=DROP_PENDING_UPDATES,
        allowed_updates=Update.ALL_TYPES,
        close_loop=False,
    )


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
