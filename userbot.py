import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.types import BusinessConnection, Message

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("Не задана переменная окружения BOT_TOKEN")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# id подключения -> id владельца аккаунта
owners: dict[str, int] = {}
TEXT = "проверка, работает"


# Подключение бота в Настройки -> Telegram Business -> Чат-боты
@dp.business_connection()
async def on_connection(conn: BusinessConnection):
    logging.info("business_connection: enabled=%s rights=%s", conn.is_enabled, conn.rights)
    owners[conn.id] = conn.user.id
    if conn.is_enabled:
        await bot.send_message(conn.user_chat_id, "бот успешно подключен")


# /help в личных чатах с другими людьми (через Telegram Business)
@dp.business_message(F.text == "/help")
async def on_help(message: Message):
    conn_id = message.business_connection_id
    logging.info("business_message /help из чата %s от %s", message.chat.id, message.from_user.id)

    if conn_id not in owners:  # например, после перезапуска бота
        conn = await bot.get_business_connection(conn_id)
        owners[conn_id] = conn.user.id

    # реагируем только на СВОИ сообщения, а не на сообщения собеседника
    if message.from_user.id != owners[conn_id]:
        return

    try:
        await bot.edit_message_text(
            TEXT,
            business_connection_id=conn_id,
            chat_id=message.chat.id,
            message_id=message.message_id,
        )
    except Exception as e:
        logging.warning("редактирование не удалось (%s), удаляю и отправляю заново", e)
        await bot.delete_business_messages(conn_id, [message.message_id])
        await bot.send_message(text=TEXT, chat_id=message.chat.id, business_connection_id=conn_id)


# /help или /start прямо в чате с самим ботом (обычный чат, не Business)
@dp.message(F.text.in_({"/help", "/start"}))
async def on_direct(message: Message):
    await message.answer(TEXT)


async def main():
    logging.info("бот запущен")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


asyncio.run(main())
