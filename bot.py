import asyncio
import logging
import os
from datetime import datetime

from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from sqlalchemy import BigInteger, String, Numeric, DateTime, func, select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")

# Railway "postgresql://" beradi, bizga "postgresql+asyncpg://" kerak —
# shuning uchun avtomatik almashtiramiz, qo'lda tuzatish shart emas
raw_db_url = os.getenv("DATABASE_URL", "")
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
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
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
            session.add(user)
            await session.commit()
            logger.info(f"Yangi foydalanuvchi saqlandi: {message.from_user.id}")

    holat = "Ro'yxatdan o'tdingiz" if is_new else "Qaytib kelibsiz"
    await message.answer(
        f"Salom, {message.from_user.first_name}! 👋\n\n"
        f"{holat}. Bu ma'lumot endi bazada saqlanmoqda.\n"
        f"Balansingiz: {user.wallet_balance} so'm"
    )


async def main():
    logger.info("Bazani tekshirmoqda...")
    await init_db()
    logger.info("Bot ishga tushmoqda...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
