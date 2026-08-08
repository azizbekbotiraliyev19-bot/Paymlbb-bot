import asyncio
import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone

import aiohttp
from urllib.parse import parse_qsl

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, CommandStart
from aiohttp import web
from sqlalchemy import BigInteger, ForeignKey, Integer, String, Numeric, DateTime, func, select, text
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
    loyalty_points: Mapped[int] = mapped_column(Integer, default=0)
    ai_questions_used: Mapped[int] = mapped_column(Integer, default=0)
    ai_reset_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    referred_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Tournament(Base):
    __tablename__ = "tournaments"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(150))
    entry_fee: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    prize_pool: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    max_participants: Mapped[int] = mapped_column(Integer, default=64)
    status: Mapped[str] = mapped_column(String(20), default="upcoming")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class TournamentParticipant(Base):
    __tablename__ = "tournament_participants"

    id: Mapped[int] = mapped_column(primary_key=True)
    tournament_id: Mapped[int] = mapped_column(ForeignKey("tournaments.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Squad(Base):
    __tablename__ = "squads"

    id: Mapped[int] = mapped_column(primary_key=True)
    lane: Mapped[str] = mapped_column(String(30))
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    max_members: Mapped[int] = mapped_column(Integer, default=5)
    status: Mapped[str] = mapped_column(String(20), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class SquadMember(Base):
    __tablename__ = "squad_members"

    id: Mapped[int] = mapped_column(primary_key=True)
    squad_id: Mapped[int] = mapped_column(ForeignKey("squads.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


BOT_USERNAME = "paymlbbaibot"

SUPER_ADMIN_IDS = {
    int(x) for x in os.getenv("SUPER_ADMIN_IDS", "").split(",") if x.strip()
}


def is_admin(telegram_id: int) -> bool:
    return telegram_id in SUPER_ADMIN_IDS


# Loyalty rank — bot ichidagi faollik balliga asoslangan, o'yin rankiga
# BOG'LIQ EMAS (Moonton rasmiy statistika API bermaydi).
LOYALTY_RANKS = [
    (0, "warrior"),
    (100, "elite"),
    (300, "master"),
    (700, "grandmaster"),
    (1500, "epic"),
    (3000, "legend"),
    (6000, "mythic"),
]


def calculate_rank(points: int) -> str:
    rank = LOYALTY_RANKS[0][1]
    for threshold, name in LOYALTY_RANKS:
        if points >= threshold:
            rank = name
        else:
            break
    return rank


async def add_loyalty_points(session, user: "User", amount: int, reason: str):
    user.loyalty_points += amount
    logger.info(f"+{amount} ball ({reason}) — foydalanuvchi {user.telegram_id}")


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _ensure_new_columns(conn)


async def _ensure_new_columns(conn):
    """Mavjud jadvalga model'da bor-u, bazada yo'q ustunlarni avtomatik
    qo'shadi. create_all() faqat YO'Q jadvalni yaratadi, mavjud jadvalni
    o'zgartirmaydi — shuning uchun har safar shu tekshiruv kerak."""
    from sqlalchemy import inspect, text

    def get_existing_columns(sync_conn):
        inspector = inspect(sync_conn)
        if "users" not in inspector.get_table_names():
            return set()
        return {col["name"] for col in inspector.get_columns("users")}

    existing = await conn.run_sync(get_existing_columns)
    if not existing:
        return

    if "referred_by_id" not in existing:
        await conn.execute(text(
            "ALTER TABLE users ADD COLUMN referred_by_id INTEGER REFERENCES users(id)"
        ))
        logger.info("Baza yangilandi: users.referred_by_id ustuni qo'shildi")

    if "loyalty_points" not in existing:
        await conn.execute(text(
            "ALTER TABLE users ADD COLUMN loyalty_points INTEGER DEFAULT 0"
        ))
        logger.info("Baza yangilandi: users.loyalty_points ustuni qo'shildi")

    if "ai_questions_used" not in existing:
        await conn.execute(text(
            "ALTER TABLE users ADD COLUMN ai_questions_used INTEGER DEFAULT 0"
        ))
        logger.info("Baza yangilandi: users.ai_questions_used ustuni qo'shildi")

    if "ai_reset_date" not in existing:
        await conn.execute(text(
            "ALTER TABLE users ADD COLUMN ai_reset_date VARCHAR(10)"
        ))
        logger.info("Baza yangilandi: users.ai_reset_date ustuni qo'shildi")


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    try:
        await _handle_start(message)
    except Exception:
        logger.exception(f"/start da xato, foydalanuvchi {message.from_user.id}")
        await message.answer(
            "Kechirasiz, texnik xato yuz berdi. Biroz vaqtdan so'ng "
            "qayta urinib ko'ring — bu haqida ma'lumot loglarga yozildi."
        )


async def _handle_start(message: types.Message):
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
            "loyalty_points": user.loyalty_points,
            "loyalty_rank": calculate_rank(user.loyalty_points),
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


# ---------------------------------------------------------------------------
# Turnir moduli — admin /yangi_turnir orqali yaratadi, Mini App orqali
# foydalanuvchilar ro'yxatdan o'tadi
# ---------------------------------------------------------------------------

@dp.message(Command("yangi_turnir"))
async def cmd_new_tournament(message: types.Message):
    """Format: /yangi_turnir Nomi | kirish_tolovi | mukofot | max_ishtirokchi
    Masalan: /yangi_turnir Hafta kubogi | 15000 | 500000 | 64"""
    if not is_admin(message.from_user.id):
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "Format: /yangi_turnir Nomi | kirish_tolovi | mukofot | max_ishtirokchi\n"
            "Masalan: /yangi_turnir Hafta kubogi | 15000 | 500000 | 64"
        )
        return

    parts = [p.strip() for p in args[1].split("|")]
    if len(parts) != 4:
        await message.answer("4 ta qism kerak: Nomi | kirish | mukofot | max_ishtirokchi")
        return

    try:
        name, entry_fee, prize_pool, max_participants = parts
        entry_fee = float(entry_fee)
        prize_pool = float(prize_pool)
        max_participants = int(max_participants)
    except ValueError:
        await message.answer("Raqamlar noto'g'ri kiritildi.")
        return

    async with async_session() as session:
        tournament = Tournament(
            name=name,
            entry_fee=entry_fee,
            prize_pool=prize_pool,
            max_participants=max_participants,
            status="upcoming",
        )
        session.add(tournament)
        await session.commit()
        tournament_id = tournament.id

    await message.answer(f"✅ Turnir yaratildi: \"{name}\" (ID: {tournament_id})")


async def api_tournaments(request: web.Request) -> web.Response:
    if request.method == "OPTIONS":
        return web.Response()

    async with async_session() as session:
        result = await session.execute(
            select(Tournament).where(Tournament.status != "finished")
        )
        tournaments = result.scalars().all()

        data = []
        for t in tournaments:
            count_result = await session.execute(
                select(func.count()).select_from(TournamentParticipant)
                .where(TournamentParticipant.tournament_id == t.id)
            )
            participant_count = count_result.scalar_one()
            data.append({
                "id": t.id,
                "name": t.name,
                "entry_fee": float(t.entry_fee),
                "prize_pool": float(t.prize_pool),
                "max_participants": t.max_participants,
                "participant_count": participant_count,
                "status": t.status,
            })

    return web.json_response({"tournaments": data})


async def api_join_tournament(request: web.Request) -> web.Response:
    if request.method == "OPTIONS":
        return web.Response()

    init_data = request.headers.get("X-Telegram-Init-Data", "")
    parsed = verify_telegram_init_data(init_data)
    if not parsed:
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        user_info = json.loads(parsed.get("user", "{}"))
        telegram_id = user_info["id"]
        tournament_id = int(request.match_info["tournament_id"])
    except (KeyError, json.JSONDecodeError, ValueError):
        return web.json_response({"error": "invalid_request"}, status=400)

    async with async_session() as session:
        user_result = await session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        user = user_result.scalar_one_or_none()
        if user is None:
            return web.json_response({"error": "user_not_found"}, status=404)

        tournament = await session.get(Tournament, tournament_id)
        if tournament is None:
            return web.json_response({"error": "tournament_not_found"}, status=404)

        existing = await session.execute(
            select(TournamentParticipant).where(
                TournamentParticipant.tournament_id == tournament_id,
                TournamentParticipant.user_id == user.id,
            )
        )
        if existing.scalar_one_or_none():
            return web.json_response({"error": "already_joined"}, status=409)

        participant = TournamentParticipant(tournament_id=tournament_id, user_id=user.id)
        session.add(participant)
        await add_loyalty_points(session, user, 20, "turnirga qatnashish")
        await session.commit()

    return web.json_response({"success": True})


# ---------------------------------------------------------------------------
# Jamoa tuzish (Squad finder) — liniya bo'yicha jamoa yaratish va qo'shilish
# ---------------------------------------------------------------------------

async def api_squads(request: web.Request) -> web.Response:
    if request.method == "OPTIONS":
        return web.Response()

    lane_filter = request.query.get("lane")

    async with async_session() as session:
        query = select(Squad).where(Squad.status == "open")
        if lane_filter:
            query = query.where(Squad.lane == lane_filter)
        result = await session.execute(query)
        squads = result.scalars().all()

        data = []
        for s in squads:
            count_result = await session.execute(
                select(func.count()).select_from(SquadMember)
                .where(SquadMember.squad_id == s.id)
            )
            member_count = count_result.scalar_one()
            creator = await session.get(User, s.created_by_id)
            data.append({
                "id": s.id,
                "lane": s.lane,
                "member_count": member_count,
                "max_members": s.max_members,
                "created_by": creator.first_name if creator else "?",
            })

    return web.json_response({"squads": data})


async def api_create_squad(request: web.Request) -> web.Response:
    if request.method == "OPTIONS":
        return web.Response()

    init_data = request.headers.get("X-Telegram-Init-Data", "")
    parsed = verify_telegram_init_data(init_data)
    if not parsed:
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        user_info = json.loads(parsed.get("user", "{}"))
        telegram_id = user_info["id"]
        body = await request.json()
        lane = body["lane"]
    except (KeyError, json.JSONDecodeError):
        return web.json_response({"error": "invalid_request"}, status=400)

    async with async_session() as session:
        user_result = await session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        user = user_result.scalar_one_or_none()
        if user is None:
            return web.json_response({"error": "user_not_found"}, status=404)

        squad = Squad(lane=lane, created_by_id=user.id)
        session.add(squad)
        await session.flush()

        member = SquadMember(squad_id=squad.id, user_id=user.id)
        session.add(member)
        await session.commit()
        squad_id = squad.id

    return web.json_response({"success": True, "squad_id": squad_id})


async def api_join_squad(request: web.Request) -> web.Response:
    if request.method == "OPTIONS":
        return web.Response()

    init_data = request.headers.get("X-Telegram-Init-Data", "")
    parsed = verify_telegram_init_data(init_data)
    if not parsed:
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        user_info = json.loads(parsed.get("user", "{}"))
        telegram_id = user_info["id"]
        squad_id = int(request.match_info["squad_id"])
    except (KeyError, json.JSONDecodeError, ValueError):
        return web.json_response({"error": "invalid_request"}, status=400)

    async with async_session() as session:
        user_result = await session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        user = user_result.scalar_one_or_none()
        if user is None:
            return web.json_response({"error": "user_not_found"}, status=404)

        squad = await session.get(Squad, squad_id)
        if squad is None or squad.status != "open":
            return web.json_response({"error": "squad_not_available"}, status=404)

        count_result = await session.execute(
            select(func.count()).select_from(SquadMember)
            .where(SquadMember.squad_id == squad_id)
        )
        member_count = count_result.scalar_one()
        if member_count >= squad.max_members:
            return web.json_response({"error": "squad_full"}, status=409)

        existing = await session.execute(
            select(SquadMember).where(
                SquadMember.squad_id == squad_id,
                SquadMember.user_id == user.id,
            )
        )
        if existing.scalar_one_or_none():
            return web.json_response({"error": "already_joined"}, status=409)

        member = SquadMember(squad_id=squad_id, user_id=user.id)
        session.add(member)

        if member_count + 1 >= squad.max_members:
            squad.status = "full"

        await session.commit()

    return web.json_response({"success": True})


# ---------------------------------------------------------------------------
# AI Yordamchi — Anthropic Claude API orqali, real-vaqt veb-qidiruv bilan
# ---------------------------------------------------------------------------

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
AI_DAILY_FREE_LIMIT = 5
AI_MODEL = "claude-sonnet-4-6"

AI_SYSTEM_PROMPT = (
    "Siz PayMLBB.ai botining Mobile Legends: Bang Bang bo'yicha AI "
    "yordamchisiz. Hero'lar, item build, counter-pick, meta-tier va "
    "so'nggi yangiliklar/patch haqida savollarga javob berasiz. "
    "Joriy meta yoki yangiliklar so'ralsa, veb-qidiruvdan foydalaning. "
    "Javoblaringiz o'zbek tilida, qisqa va aniq bo'lsin (3-5 gap atrofida), "
    "agar savol boshqa tilda yozilgan bo'lsa, o'sha tilda javob bering."
)


async def api_ai_ask(request: web.Request) -> web.Response:
    if request.method == "OPTIONS":
        return web.Response()

    if not ANTHROPIC_API_KEY:
        return web.json_response({"error": "ai_not_configured"}, status=503)

    init_data = request.headers.get("X-Telegram-Init-Data", "")
    parsed = verify_telegram_init_data(init_data)
    if not parsed:
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        user_info = json.loads(parsed.get("user", "{}"))
        telegram_id = user_info["id"]
        body = await request.json()
        question = (body.get("question") or "").strip()
    except (KeyError, json.JSONDecodeError):
        return web.json_response({"error": "invalid_request"}, status=400)

    if not question:
        return web.json_response({"error": "empty_question"}, status=400)
    if len(question) > 500:
        return web.json_response({"error": "question_too_long"}, status=400)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        user = result.scalar_one_or_none()
        if user is None:
            return web.json_response({"error": "user_not_found"}, status=404)

        if user.ai_reset_date != today:
            user.ai_questions_used = 0
            user.ai_reset_date = today

        if user.ai_questions_used >= AI_DAILY_FREE_LIMIT:
            await session.commit()
            return web.json_response(
                {"error": "daily_limit_reached", "limit": AI_DAILY_FREE_LIMIT},
                status=429,
            )

        user.ai_questions_used += 1
        remaining = AI_DAILY_FREE_LIMIT - user.ai_questions_used
        await session.commit()

    try:
        async with aiohttp.ClientSession() as client:
            async with client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": AI_MODEL,
                    "max_tokens": 700,
                    "system": AI_SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": question}],
                    "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json()
                if resp.status != 200:
                    logger.error(f"Anthropic API xatosi: {resp.status} {data}")
                    return web.json_response({"error": "ai_request_failed"}, status=502)
    except Exception:
        logger.exception("AI so'roviga ulanishda xato")
        return web.json_response({"error": "ai_request_failed"}, status=502)

    answer_parts = [
        block.get("text", "") for block in data.get("content", [])
        if block.get("type") == "text"
    ]
    answer = "\n".join(p for p in answer_parts if p).strip()
    if not answer:
        answer = "Kechirasiz, javob topilmadi. Boshqacha savol bering."

    return web.json_response({"answer": answer, "remaining_today": remaining})


# ---------------------------------------------------------------------------
# TODO — quyidagilar tashqi ma'lumot/qaror kutmoqda, hali ULANMAGAN:
#
# 1. TO'LOV (Payme/Click): merchant ID va maxfiy kalitlar kerak.
#    Olganingizdan keyin shu yerga /api/payment/webhook qo'shiladi.
#
# 2. DIAMOND TOP-UP: faqat rasmiy Moonton/distribyutor hamkorligi
#    hujjat bilan tasdiqlangandan keyin qurilishi kerak.
# ---------------------------------------------------------------------------


@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        response = web.Response()
    else:
        response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "X-Telegram-Init-Data, Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    return response


def create_web_app() -> web.Application:
    app = web.Application(middlewares=[cors_middleware])
    app.router.add_route("GET", "/api/profile", api_profile)
    app.router.add_route("OPTIONS", "/api/profile", api_profile)
    app.router.add_route("GET", "/api/referrals", api_referrals)
    app.router.add_route("OPTIONS", "/api/referrals", api_referrals)
    app.router.add_route("GET", "/api/tournaments", api_tournaments)
    app.router.add_route("OPTIONS", "/api/tournaments", api_tournaments)
    app.router.add_route("POST", "/api/tournaments/{tournament_id}/join", api_join_tournament)
    app.router.add_route("OPTIONS", "/api/tournaments/{tournament_id}/join", api_join_tournament)
    app.router.add_route("GET", "/api/squads", api_squads)
    app.router.add_route("OPTIONS", "/api/squads", api_squads)
    app.router.add_route("POST", "/api/squads", api_create_squad)
    app.router.add_route("POST", "/api/squads/{squad_id}/join", api_join_squad)
    app.router.add_route("OPTIONS", "/api/squads/{squad_id}/join", api_join_squad)
    app.router.add_route("POST", "/api/ai/ask", api_ai_ask)
    app.router.add_route("OPTIONS", "/api/ai/ask", api_ai_ask)
    return app


async def main():
    try:
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
    except Exception:
        logger.exception("HALOKATLI XATO — bot ishga tusholmadi yoki tuxtadi")
        raise


if __name__ == "__main__":
    asyncio.run(main())
