import asyncio

from aiogram import Bot, Dispatcher, F
from aiogram.types import BusinessConnection, Message

BOT_TOKEN = "8280076573:AAHiJHpTiFKMT3hBXKs9YBv4jJUWTcxw7V4"

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# id подключения -> id владельца аккаунта
owners: dict[str, int] = {}


# Срабатывает, когда ты подключаешь бота в Настройки -> Telegram Business -> Чат-боты
@dp.business_connection()
async def on_connection(conn: BusinessConnection):
    owners[conn.id] = conn.user.id
    if conn.is_enabled:
        await bot.send_message(conn.user_chat_id, "бот успешно подключен")


# Твоё сообщение "/help" в любом личном чате -> заменяется на текст
@dp.business_message(F.text == "/help")
async def on_help(message: Message):
    conn_id = message.business_connection_id

    if conn_id not in owners:  # например, после перезапуска бота
        conn = await bot.get_business_connection(conn_id)
        owners[conn_id] = conn.user.id

    # реагируем только на СВОИ сообщения, а не на сообщения собеседника
    if message.from_user.id != owners[conn_id]:
        return

    text = "проверка, работает"
    try:
        await bot.edit_message_text(
            text,
            business_connection_id=conn_id,
            chat_id=message.chat.id,
            message_id=message.message_id,
        )
    except Exception:
        # если Telegram не даёт отредактировать твоё сообщение:
        # удаляем "/help" и отправляем новое сообщение с текстом
        await bot.delete_business_messages(conn_id, [message.message_id])
        await bot.send_message(text=text, chat_id=message.chat.id, business_connection_id=conn_id)


async def main():
    print("бот запущен")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


asyncio.run(main())
