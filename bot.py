import logging

from telegram import Update
from telegram.ext import Application, ContextTypes, TypeHandler

BOT_TOKEN = "8280076573:AAFknR_2qMrJb89lYr0vMbLi48aruYWNx3Q"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

async def business_connection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    connection = update.business_connection

    if connection is None:
        return

    if connection.is_enabled:
        try:
            await context.bot.send_message(
                chat_id=connection.user_chat_id,
                text="бот успешно подключен"
            )
        except Exception:
            logging.exception("Не удалось отправить уведомление владельцу.")

def main():
    if not BOT_TOKEN or BOT_TOKEN == "ВСТАВЬ_СЮДА_ТОКЕН_БОТА":
        raise RuntimeError("Вставь токен бота в переменную BOT_TOKEN")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(TypeHandler(Update, business_connection))

    logging.info("Ожидание подключения через Telegram Business...")
    app.run_polling(allowed_updates=["business_connection"])

if __name__ == "__main__":
    main()
