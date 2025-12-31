import os
import asyncio
import logging
import signal
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional

import asyncpg
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")


def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None:
        return default
    try:
        return int(v)
    except ValueError:
        return default


@dataclass
class Config:
    bot_token: str
    database_url: str

    enable_cleanup: bool
    enable_daily: bool
    enable_activity: bool
    enable_verify: bool

    verify_interval_s: int
    telegram_timeout_s: int


def load_config() -> Config:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    db_url = os.getenv("DATABASE_URL", "").strip()

    if not bot_token:
        raise RuntimeError("Missing BOT_TOKEN env var")
    if not db_url:
        raise RuntimeError("Missing DATABASE_URL env var")

    return Config(
        bot_token=bot_token,
        database_url=db_url,
        enable_cleanup=env_bool("ENABLE_CLEANUP", False),
        enable_daily=env_bool("ENABLE_DAILY", False),
        enable_activity=env_bool("ENABLE_ACTIVITY", False),
        enable_verify=env_bool("ENABLE_VERIFY", True),  # <— zet op 0 als je hem uit wilt
        verify_interval_s=env_int("VERIFY_INTERVAL_S", 30),
        telegram_timeout_s=env_int("TELEGRAM_TIMEOUT_S", 20),
    )


class State:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.pool: Optional[asyncpg.Pool] = None
        self.stop_event = asyncio.Event()

    async def init_db(self) -> None:
        # Pool is meestal stabieler dan losse connects
        self.pool = await asyncpg.create_pool(
            dsn=self.cfg.database_url,
            min_size=1,
            max_size=5,
            command_timeout=30,
        )
        log.info("DB initialized ok")

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()
            self.pool = None


# ---------- DB helpers (FIX voor jouw error) ----------

SQL_IS_USED = """
SELECT 1
FROM joined_names
WHERE name = $1
  AND used_on = $2
LIMIT 1;
"""

SQL_MARK_USED = """
INSERT INTO joined_names (name, used_on, created_at)
VALUES ($1, $2, $3)
ON CONFLICT DO NOTHING;
"""


async def db_is_used(pool: asyncpg.Pool, name: str, used_on: date) -> bool:
    # LET OP: used_on is een datetime.date object, geen string!
    async with pool.acquire() as conn:
        row = await conn.fetchrow(SQL_IS_USED, name, used_on)
        return row is not None


async def db_mark_used(pool: asyncpg.Pool, name: str, used_on: date) -> None:
    async with pool.acquire() as conn:
        await conn.execute(SQL_MARK_USED, name, used_on, datetime.now(timezone.utc))


# ---------- Telegram handlers ----------

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    await update.message.reply_text("✅ Bot draait.")


# ---------- Background loops ----------

async def verify_random_joiner_loop(app: Application, st: State) -> None:
    """
    Voorbeeld loop die checkt of 'name' vandaag al gebruikt is.
    Hier zat bij jou de crash omdat je 'YYYY-MM-DD' string meegaf.
    """
    assert st.pool is not None

    log.info("verify_random_joiner_loop started (ENABLE_VERIFY=1)")

    # voorbeeld data; vervang met je echte bron (db list, cache, etc.)
    candidates = ["alice", "bob", "charlie"]

    backoff = 1.0
    while not st.stop_event.is_set():
        try:
            today = date.today()  # <-- date object (FIX)
            for name in candidates:
                used = await db_is_used(st.pool, name, today)
                if not used:
                    # doe iets (bijv. iemand verifiëren / bericht sturen / markeren)
                    await db_mark_used(st.pool, name, today)
                    log.info("Marked used: name=%s date=%s", name, today.isoformat())

            backoff = 1.0
            await asyncio.wait_for(st.stop_event.wait(), timeout=st.cfg.verify_interval_s)

        except asyncio.TimeoutError:
            continue
        except Exception as e:
            log.exception("verify_random_joiner_loop crashed iteration: %s", e)
            # simpele retry/backoff
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


async def safe_create_task(coro, name: str) -> asyncio.Task:
    task = asyncio.create_task(coro, name=name)

    def _done(t: asyncio.Task) -> None:
        try:
            t.result()
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("Task crashed: %s", t.get_name())

    task.add_done_callback(_done)
    return task


# ---------- Main ----------

async def main_async() -> None:
    cfg = load_config()

    log.info("ENABLE_CLEANUP=%s", int(cfg.enable_cleanup))
    log.info("ENABLE_DAILY=%s", int(cfg.enable_daily))
    log.info("ENABLE_ACTIVITY=%s", int(cfg.enable_activity))
    log.info("ENABLE_VERIFY=%s", int(cfg.enable_verify))

    st = State(cfg)
    await st.init_db()

    # Zet timeouts iets hoger om transient issues minder pijnlijk te maken
    app = (
        ApplicationBuilder()
        .token(cfg.bot_token)
        .read_timeout(cfg.telegram_timeout_s)
        .write_timeout(cfg.telegram_timeout_s)
        .connect_timeout(cfg.telegram_timeout_s)
        .pool_timeout(cfg.telegram_timeout_s)
        .build()
    )

    app.add_handler(MessageHandler(filters.ALL, on_message))

    # Background tasks (alleen starten als flag aan is)
    if cfg.enable_verify:
        await safe_create_task(verify_random_joiner_loop(app, st), "verify_random_joiner_loop")

    # Graceful stop (SIGTERM is wat Railway meestal stuurt)
    loop = asyncio.get_running_loop()

    def _stop(*_args):
        log.info("Stop signal received")
        st.stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            pass

    try:
        # BELANGRIJK: run_polling = long polling.
        # Zorg dat Railway echt maar 1 replica draait, anders krijg je 409 Conflict.
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)

        log.info("Bot started")

        await st.stop_event.wait()

    finally:
        log.info("Shutting down...")
        try:
            await app.updater.stop()
        except Exception:
            pass
        try:
            await app.stop()
        except Exception:
            pass
        try:
            await app.shutdown()
        except Exception:
            pass
        await st.close()
        log.info("Shutdown complete")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
