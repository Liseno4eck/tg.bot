import logging
import uuid

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    ContextTypes,
    TypeHandler,
    CallbackQueryHandler,
)


BOT_TOKEN = "8280076573:AAFknR_2qMrJb89lYr0vMbLi48aruYWNx3Q"


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)


games = {}


def create_board():
    return [""] * 9


def make_keyboard(game_id):
    game = games[game_id]
    board = game["board"]

    keyboard = []

    for row in range(3):
        line = []

        for col in range(3):
            index = row * 3 + col

            if board[index] == "X":
                symbol = "❌"
            elif board[index] == "O":
                symbol = "⭕️"
            else:
                symbol = "⬜️"

            line.append(
                InlineKeyboardButton(
                    symbol,
                    callback_data=f"xox:{game_id}:{index}"
                )
            )

        keyboard.append(line)

    if game["finished"]:
        keyboard.append([
            InlineKeyboardButton(
                "🔄 Новая игра",
                callback_data=f"newxox:{game_id}"
            )
        ])

    return InlineKeyboardMarkup(keyboard)


def check_winner(board):
    combinations = [
        (0, 1, 2),
        (3, 4, 5),
        (6, 7, 8),
        (0, 3, 6),
        (1, 4, 7),
        (2, 5, 8),
        (0, 4, 8),
        (2, 4, 6),
    ]

    for a, b, c in combinations:
        if board[a] and board[a] == board[b] == board[c]:
            return board[a]

    if all(board):
        return "draw"

    return None


def get_user_name(user):
    if user.username:
        return f"@{user.username}"

    if user.first_name:
        return user.first_name

    return str(user.id)


def game_text(game):
    if len(game["players"]) == 0:
        return (
            "❌⭕️ КРЕСТИКИ-НОЛИКИ\n\n"
            "Нажмите на любую клетку, чтобы присоединиться.\n\n"
            "❌ Первый игрок\n"
            "⭕️ Второй игрок"
        )

    if len(game["players"]) == 1:
        player = next(iter(game["players"].values()))

        return (
            "❌⭕️ КРЕСТИКИ-НОЛИКИ\n\n"
            f"Игрок {player} подключился.\n"
            "Ждём второго игрока..."
        )

    if game["finished"]:
        result = game["result"]

        return (
            "❌⭕️ КРЕСТИКИ-НОЛИКИ\n\n"
            f"{result}"
        )

    if game["turn_symbol"] == "X":
        return (
            "❌⭕️ КРЕСТИКИ-НОЛИКИ\n\n"
            "Сейчас ходит ❌"
        )

    return (
        "❌⭕️ КРЕСТИКИ-НОЛИКИ\n\n"
        "Сейчас ходит ⭕️"
    )


async def handle_business_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.business_message

    if message is None:
        return

    if not message.text:
        return

    text = message.text.strip().lower()

    logging.info(
        "Получено Business-сообщение: %r | chat_id=%s | business_id=%s",
        message.text,
        message.chat_id,
        message.business_connection_id,
    )

    # =========================
    # КОМАНДА /help
    # =========================
    if text == "/help":
        try:
            await context.bot.delete_business_messages(
                business_connection_id=message.business_connection_id,
                message_ids=[message.message_id],
            )

            await context.bot.send_message(
                chat_id=message.chat_id,
                business_connection_id=message.business_connection_id,
                text="помощи покачто нету",
            )

            logging.info("Команда /help успешно обработана.")

        except Exception:
            logging.exception("Ошибка при обработке команды /help")

        return
    # =========================
    # КОМАНДА /xox
    # =========================
    if text != "/xox":
        return

    game_id = uuid.uuid4().hex[:12]

    games[game_id] = {
        "board": create_board(),
        "players": {},
        "turn": None,
        "turn_symbol": "X",
        "finished": False,
        "result": "",
        "chat_id": message.chat_id,
        "message_id": None,  # заполним после отправки
        "business_connection_id": message.business_connection_id,
    }

    game = games[game_id]

    try:
        # ИСПРАВЛЕНО: отправляем новое сообщение, а не редактируем
        # входящее от пользователя (его редактировать нельзя)
        sent = await context.bot.send_message(
            chat_id=game["chat_id"],
            business_connection_id=game["business_connection_id"],
            text=game_text(game),
            reply_markup=make_keyboard(game_id),
        )

        game["message_id"] = sent.message_id

        # Удаляем сообщение пользователя с /xox
        try:
            await context.bot.delete_business_messages(
                business_connection_id=message.business_connection_id,
                message_ids=[message.message_id],
            )
        except Exception:
            logging.exception("Не удалось удалить сообщение /xox")

        logging.info("Команда /xox успешно обработана: %s", game_id)

    except Exception:
        logging.exception("Ошибка при отправке игрового сообщения /xox")


async def handle_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if query is None:
        return

    data = query.data or ""

    if data.startswith("newxox:"):
        await new_game(query, context)
        return

    if not data.startswith("xox:"):
        return

    parts = data.split(":")

    if len(parts) != 3:
        return

    game_id = parts[1]
    position = int(parts[2])

    game = games.get(game_id)

    if game is None:
        await query.answer(
            "Игра уже не существует.",
            show_alert=True
        )
        return

    if game["finished"]:
        await query.answer(
            "Игра уже закончилась.",
            show_alert=True
        )
        return

    user_id = query.from_user.id

    if user_id not in game["players"]:

        if len(game["players"]) >= 2:
            await query.answer(
                "В игре уже два игрока.",
                show_alert=True
            )
            return

        if len(game["players"]) == 0:
            symbol = "X"
            game["turn"] = user_id
        else:
            symbol = "O"

        game["players"][user_id] = symbol

    symbol = game["players"][user_id]

    if len(game["players"]) < 2:
        await update_game_message(game_id, context)
        await query.answer(
            f"Ты играешь {'❌' if symbol == 'X' else '⭕️'}"
        )
        return

    if game["turn"] != user_id:
        await query.answer(
            "Сейчас ход другого игрока.",
            show_alert=True
        )
        return

    if game["board"][position]:
        await query.answer(
            "Эта клетка уже занята.",
            show_alert=True
        )
        return

    game["board"][position] = symbol

    winner = check_winner(game["board"])

    if winner == "X":
        game["finished"] = True
        game["result"] = "🏆 Победили ❌!"
    elif winner == "O":
        game["finished"] = True
        game["result"] = "🏆 Победили ⭕️!"
    elif winner == "draw":
        game["finished"] = True
        game["result"] = "🤝 Ничья!"
    else:
        next_symbol = "O" if symbol == "X" else "X"
        game["turn_symbol"] = next_symbol

        for player_id, player_symbol in game["players"].items():
            if player_symbol == next_symbol:
                game["turn"] = player_id
                break

    await query.answer()
    await update_game_message(game_id, context)
[19.09.2026 10:25] TG Ai Chat: async def update_game_message(game_id, context):
    game = games.get(game_id)

    if game is None:
        return

    if game["message_id"] is None:
        return

    try:
        await context.bot.edit_message_text(
            chat_id=game["chat_id"],
            message_id=game["message_id"],
            business_connection_id=game["business_connection_id"],
            text=game_text(game),
            reply_markup=make_keyboard(game_id),
        )
    except Exception:
        logging.exception("Ошибка обновления игрового поля")


async def new_game(query, context):
    old_game_id = query.data.split(":", 1)[1]

    old_game = games.get(old_game_id)

    if old_game is None:
        await query.answer(
            "Игра уже удалена.",
            show_alert=True
        )
        return

    await query.answer()

    new_id = uuid.uuid4().hex[:12]

    games[new_id] = {
        "board": create_board(),
        "players": {},
        "turn": None,
        "turn_symbol": "X",
        "finished": False,
        "result": "",
        "chat_id": old_game["chat_id"],
        "message_id": old_game["message_id"],
        "business_connection_id": old_game["business_connection_id"],
    }

    del games[old_game_id]

    await update_game_message(new_id, context)


async def handle_business_connection(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    connection = update.business_connection

    if connection is None:
        return

    logging.info(
        "Business connection: id=%s enabled=%s",
        connection.id,
        connection.is_enabled
    )

    if connection.is_enabled:
        try:
            await context.bot.send_message(
                chat_id=connection.user_chat_id,
                text="бот успешно подключен"
            )
        except Exception:
            logging.exception(
                "Не удалось отправить сообщение о подключении."
            )


def main():
    if not BOT_TOKEN or BOT_TOKEN == "ВСТАВЬ_НОВЫЙ_ТОКЕН_БОТА":
        raise RuntimeError("Вставь новый токен бота в BOT_TOKEN")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(
        TypeHandler(Update, handle_business_connection)
    )
    app.add_handler(
        TypeHandler(Update, handle_business_message)
    )
    app.add_handler(
        CallbackQueryHandler(
            handle_callback,
            pattern=r"^(xox|newxox):"
        )
    )

    logging.info("Бот запущен.")

    app.run_polling(
        allowed_updates=[
            "business_connection",
            "business_message",
            "callback_query",
        ]
    )
