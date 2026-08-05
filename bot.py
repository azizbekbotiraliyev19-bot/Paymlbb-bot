import asyncio
import hashlib
import hmac
import json
import logging
import os
from datetime import datetime
from urllib.parse import parse_qsl

from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiohttp import web
from sqlalchemy import BigInteger, ForeignKey, String, Numeric, DateTime, func, select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "8080"))

raw_db_url = os.getenv("DATABASE_URL", "").strip()

if not raw_db_url:
    logger.error(
        "DATABASE_URL topilmadi! Railway > Bot xizmati > Variables "
        "bo'limida DATABASE_URL borligini tekshiring."
    )

DATABASE_URL = raw_db_url.replace("postgresql://", "postgresql+asyncpg://", 1)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    first_name: Mapped[str] = mapped_column(String(100), nullable=True)
    language: Mapped[str] = mapped_column(String(5), default="uz")
    wallet_balance: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    referred_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


BOT_USERNAME = "paymlbbaibot"


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    referral_code = None
    parts = message.text.split(maxsplit=1)
    if len(parts) > 1 and parts[1].startswith("ref_"):
        referral_code = parts[1].replace("ref_", "")

    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == message.from_user.id)
        )
        user = result.scalar_one_or_none()

        is_new = user is None
        if is_new:
            user = User(
                telegram_id=message.from_user.id,
                first_name=message.from_user.first_name,
                language=message.from_user.language_code or "uz",
            )

            if referral_code:
                try:
                    referrer_tid = int(referral_code)
                    ref_result = await session.execute(
                        select(User).where(User.telegram_id == referrer_tid)
                    )
                    referrer = ref_result.scalar_one_or_none()
                    if referrer and referrer.telegram_id != message.from_user.id:
                        user.referred_by_id = referrer.id
                except ValueError:
                    pass

            session.add(user)
            await session.commit()
            logger.info(f"Yangi foydalanuvchi saqlandi: {message.from_user.id}")

    holat = "Ro'yxatdan o'tdingiz" if is_new else "Qaytib kelibsiz"
    await message.answer(
        f"Salom, {message.from_user.first_name}! 👋\n\n"
        f"{holat}. Bu ma'lumot endi bazada saqlanmoqda.\n"
        f"Balansingiz: {user.wallet_balance} so'm"
    )


# ---------------------------------------------------------------------------
# Mini App uchun veb-API — Profil sahifasi shu yerdan haqiqiy ma'lumot oladi
# ---------------------------------------------------------------------------

def verify_telegram_init_data(init_data: str) -> dict | None:
    """Mini App'dan kelgan initData haqiqatan Telegramdan ekanini tekshiradi."""
    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None

        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(
            secret_key, data_check_string.encode(), hashlib.sha256
        ).hexdigest()

        if calculated_hash != received_hash:
            return None
        return parsed
    except Exception:
        return None


async def api_profile(request: web.Request) -> web.Response:
    if request.method == "OPTIONS":
        return web.Response()

    init_data = request.headers.get("X-Telegram-Init-Data", "")
    parsed = verify_telegram_init_data(init_data)
    if not parsed:
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        user_info = json.loads(parsed.get("user", "{}"))
        telegram_id = user_info["id"]
    except (KeyError, json.JSONDecodeError):
        return web.json_response({"error": "invalid_user"}, status=400)

    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        user = result.scalar_one_or_none()

    if user is None:
        return web.json_response({"error": "not_found"}, status=404)

    return web.json_response(
        {
            "first_name": user.first_name,
            "balance": float(user.wallet_balance),
            "language": user.language,
            "joined": user.created_at.isoformat(),
        }
    )


async def api_referrals(request: web.Request) -> web.Response:
    if request.method == "OPTIONS":
        return web.Response()

    init_data = request.headers.get("X-Telegram-Init-Data", "")
    parsed = verify_telegram_init_data(init_data)
    if not parsed:
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        user_info = json.loads(parsed.get("user", "{}"))
        telegram_id = user_info["id"]
    except (KeyError, json.JSONDecodeError):
        return web.json_response({"error": "invalid_user"}, status=400)

    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        user = result.scalar_one_or_none()

        if user is None:
            return web.json_response({"error": "not_found"}, status=404)

        count_result = await session.execute(
            select(func.count()).select_from(User).where(User.referred_by_id == user.id)
        )
        referred_count = count_result.scalar_one()

    return web.json_response(
        {
            "referral_link": f"https://t.me/{BOT_USERNAME}?start=ref_{telegram_id}",
            "referred_count": referred_count,
        }
    )


async def cors_middleware(app, handler):
    async def middleware_handler(request):
        if request.method == "OPTIONS":
            response = web.Response()
        else:
            response = await handler(request)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Headers"] = "X-Telegram-Init-Data, Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        return response

    return middleware_handler


def create_web_app() -> web.Application:
    app = web.Application(middlewares=[cors_middleware])
    app.router.add_route("GET", "/api/profile", api_profile)
    app.router.add_route("OPTIONS", "/api/profile", api_profile)
    app.router.add_route("GET", "/api/referrals", api_referrals)
    app.router.add_route("OPTIONS", "/api/referrals", api_referrals)
    return app


async def main():
    logger.info("Bazani tekshirmoqda...")
    await init_db()

    web_app = create_web_app()
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Veb-server {PORT}-portda ishga tushdi (Mini App uchun API)...")

    logger.info("Bot ishga tushmoqda...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
