import asyncio
import json
import logging
import os
from pathlib import Path
from datetime import datetime, timezone

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)

DATA_FILE = Path("chats.json")
LOG_FILE = Path("bot.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ],
)
logger = logging.getLogger(__name__)


def load_chats():
    if not DATA_FILE.exists():
        return {}

    try:
        with DATA_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        logger.exception("Не удалось прочитать chats.json")
        return {}


def save_chats(chats):
    temp = DATA_FILE.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(chats, f, ensure_ascii=False, indent=2)
    temp.replace(DATA_FILE)


chats = load_chats()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat:
        return

    chat = update.effective_chat
    chat_id = str(chat.id)

    chats[chat_id] = {
        "chat_id": chat.id,
        "type": chat.type,
        "title": chat.title or "",
        "username": getattr(chat, "username", "") or "",
        "first_name": getattr(chat, "first_name", "") or "",
        "last_name": getattr(chat, "last_name", "") or "",
        "connected_at": chats.get(chat_id, {}).get(
            "connected_at",
            datetime.now(timezone.utc).isoformat()
        ),
        "last_seen": datetime.now(timezone.utc).isoformat(),
    }
    save_chats(chats)

    await update.message.reply_text("я работаю")

    if OWNER_ID:
        try:
            await context.bot.send_message(
                chat_id=OWNER_ID,
                text=(
                    "Новый чат подключен.\n"
                    f"ID: {chat.id}\n"
                    f"Тип: {chat.type}\n"
                    f"Имя: {chat.title or chat.first_name or 'без имени'}"
                ),
            )
        except Exception:
            logger.exception("Не удалось отправить уведомление владельцу")



def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "Не указан BOT_TOKEN в .env"
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start))

    logger.info("Запуск Telegram-бота...")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
