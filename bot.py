import logging
import uuid

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
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


def empty_board():
    return [""] * 9


def board_keyboard(game_id):
    game = games[game_id]
    board = game["board"]

    keyboard = []

    for row in range(3):
        buttons = []

        for col in range(3):
            index = row * 3 + col
            value = board[index]

            if value == "X":
                text = "❌"
            elif value == "O":
                text = "⭕"
            else:
                text = "　"

            buttons.append(
                InlineKeyboardButton(
                    text,
                    callback_data=f"xox:{game_id}:{index}"
                )
            )

        keyboard.append(buttons)

    if game["finished"]:
        keyboard.append([
            InlineKeyboardButton(
                "🔄 Новая игра",
                callback_data=f"xoxnew:{game_id}"
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


def player_name(user):
    if user.username:
        return f"@{user.username}"

    if user.first_name:
        return user.first_name

    return str(user.id)


async def send_xox_game(
    bot,
    chat_id,
    business_connection_id=None,
):
    game_id = uuid.uuid4().hex[:12]

    games[game_id] = {
        "board": empty_board(),
        "players": {},
        "turn": None,
        "finished": False,
        "chat_id": chat_id,
        "business_connection_id": business_connection_id,
        "message_id": None,
    }

    text = (
        "❌⭕ КРЕСТИКИ-НОЛИКИ\n\n"
        "Нажмите на любую клетку, чтобы присоединиться к игре.\n\n"
        "❌ — первый игрок\n"
        "⭕ — второй игрок"
    )

    message = await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=board_keyboard(game_id),
        business_connection_id=business_connection_id,
    )

    games[game_id]["message_id"] = message.message_id


async def start_xox_from_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

    if message is None:
        return

    text = message.text or ""

    if text.strip().lower() != "/xox":
        return

    business_connection_id = None

    if update.business_message:
        business_connection_id = update.business_message.business_connection_id

    await send_xox_game(
        bot=context.bot,
        chat_id=message.chat_id,
        business_connection_id=business_connection_id,
    )


async def xox_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if query is None:
        return

    await query.answer()

    data = query.data

    if not data.startswith("xox:"):
        return

    _, game_id, position_text = data.split(":")
    position = int(position_text)

    game = games.get(game_id)

    if game is None:
        await query.answer(
            "Эта игра уже закончилась.",
            show_alert=True
        )
        return

    if game["finished"]:
        await query.answer(
            "Игра уже закончилась.",
            show_alert=True
        )
        return

    user = query.from_user
    user_id = user.id

    if user_id not in game["players"]:
        if len(game["players"]) >= 2:
            await query.answer(
                "В игре уже участвуют два игрока.",
                show_alert=True
            )
            return

        if len(game["players"]) == 0:
            game["players"][user_id] = "X"
            game["turn"] = user_id

            await query.answer(
                "Ты играешь ❌"
            )

        else:
            game["players"][user_id] = "O"

            await query.answer(
                "Ты играешь ⭕"
            )

    symbol = game["players"][user_id]

    if len(game["players"]) < 2:
        await query.answer(
            "Ждём второго игрока.",
            show_alert=True
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
        result_text = (
            "❌⭕ КРЕСТИКИ-НОЛИКИ\n\n"
            f"🏆 Победил ❌ — {player_name(user)}!"
        )

    elif winner == "O":
        game["finished"] = True
        result_text = (
            "❌⭕ КРЕСТИКИ-НОЛИКИ\n\n"
            f"🏆 Победил ⭕ — {player_name(user)}!"
        )

    elif winner == "draw":
        game["finished"] = True
        result_text = (
            "❌⭕ КРЕСТИКИ-НОЛИКИ\n\n"
            "🤝 Ничья!"
        )

    else:
        if symbol == "X":
            next_symbol = "⭕"
        else:
            next_symbol = "❌"

        next_player = None

        for player_id, player_symbol in game["players"].items():
            if player_symbol == next_symbol:
                next_player = player_id
                break

        game["turn"] = next_player

        result_text = (
            "❌⭕ КРЕСТИКИ-НОЛИКИ\n\n"
            f"Сейчас ход: {next_symbol}"
        )

    try:
        await context.bot.edit_message_text(
            chat_id=game["chat_id"],
            message_id=game["message_id"],
            text=result_text,
            reply_markup=board_keyboard(game_id),
            business_connection_id=game["business_connection_id"],
        )
    except Exception:
        logging.exception(
            "Не удалось обновить поле игры."
        )


async def new_xox_game(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if query is None:
        return

    await query.answer()

    data = query.data

    if not data.startswith("xoxnew:"):
        return

    _, old_game_id = data.split(":", 1)

    old_game = games.get(old_game_id)

    if old_game is None:
        return

    chat_id = old_game["chat_id"]
    business_connection_id = old_game["business_connection_id"]

    game_id = uuid.uuid4().hex[:12]

    games[game_id] = {
        "board": empty_board(),
        "players": {},
        "turn": None,
        "finished": False,
        "chat_id": chat_id,
        "business_connection_id": business_connection_id,
        "message_id": query.message.message_id,
    }

    games.pop(old_game_id, None)

    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=query.message.message_id,
            text=(
                "❌⭕ КРЕСТИКИ-НОЛИКИ\n\n"
                "Нажмите на любую клетку, чтобы присоединиться к игре.\n\n"
                "❌ — первый игрок\n"
                "⭕ — второй игрок"
            ),
            reply_markup=board_keyboard(game_id),
            business_connection_id=business_connection_id,
        )
    except Exception:
        logging.exception(
            "Не удалось начать новую игру."
        )


async def business_connection(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    connection = update.business_connection

    if connection is None:
        return

    if connection.is_enabled:
        try:
            await context.bot.send_message(
                chat_id=connection.user_chat_id,
                text="бот успешно подключен"
            )

            logging.info(
                "Telegram Business подключен: %s",
                connection.id
            )

        except Exception:
            logging.exception(
                "Не удалось отправить уведомление владельцу."
            )

    else:
        logging.info(
            "Telegram Business отключен: %s",
            connection.id
        )


async def all_updates(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await start_xox_from_message(update, context)


def main():
    if not BOT_TOKEN or BOT_TOKEN == "ВСТАВЬ_НОВЫЙ_ТОКЕН_БОТА":
        raise RuntimeError(
            "Вставь токен бота в переменную BOT_TOKEN"
        )

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(
        TypeHandler(Update, business_connection)
    )

    app.add_handler(
        TypeHandler(Update, all_updates)
    )

    app.add_handler(
        CallbackQueryHandler(
            xox_button,
            pattern=r"^xox:"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            new_xox_game,
            pattern=r"^xoxnew:"
        )
    )

    logging.info(
        "Бот запущен. Ожидание Telegram Business..."
    )

    app.run_polling(
        allowed_updates=[
            "business_connection",
            "business_message",
            "callback_query",
        ]
    )


if __name__ == "__main__":
    main()
