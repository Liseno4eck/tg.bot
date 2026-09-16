import logging
import os

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, ContextTypes, TypeHandler

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

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
    if not BOT_TOKEN:
        raise RuntimeError("В .env не указан BOT_TOKEN")

    app = Application.builder().token(BOT_TOKEN).build()

    # Единственное событие, которое обрабатывает этот бот:
    # подключение Telegram Business.
    app.add_handler(TypeHandler(Update, business_connection))

    logging.info("Ожидание подключения через Telegram Business...")
    app.run_polling(allowed_updates=["business_connection"])


if __name__ == "__main__":
    main()
