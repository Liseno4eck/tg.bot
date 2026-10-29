import asyncio
import html
import json
import logging
import os
import re
import time
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BotCommand, BusinessConnection, CallbackQuery,
                           InlineKeyboardButton, InlineKeyboardMarkup, Message)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from cryptography.fernet import Fernet

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

TOKEN = os.environ["BOT_TOKEN"]
DEV_ID = int(os.getenv("DEV_ID", "0") or 0)  # ваш Telegram ID (куда летят предложения/обращения)
STATE_KEY = os.getenv("STATE_KEY")  # ключ шифрования состояния (для GitHub Actions)

# ───────────────────────── хранилище ─────────────────────────
STATE_FILE = Path("state.enc" if STATE_KEY else "state.json")
fernet = Fernet(STATE_KEY.encode()) if STATE_KEY else None


def load_state() -> dict:
    try:
        raw = STATE_FILE.read_bytes()
        return json.loads(fernet.decrypt(raw) if fernet else raw)
    except Exception:
        return {}


def save_state() -> None:
    raw = json.dumps(S, ensure_ascii=False).encode()
    STATE_FILE.write_bytes(fernet.encrypt(raw) if fernet else raw)


S: dict = load_state()


def U(owner: int) -> dict:
    u = S.setdefault("users", {}).setdefault(str(owner), {})
    u.setdefault("mutes", {})      # chat_id -> until (0 = навсегда)
    u.setdefault("blocked", [])    # chat_id
    u.setdefault("voices", {})     # name -> {"id":..., "type":...}
    u.setdefault("auto", {"on": False, "text": "", "replied": []})
    u.setdefault("unread", {})     # chat_id -> last message_id
    u.setdefault("conn", None)
    return u


def esc(s) -> str:
    return html.escape(str(s or ""))


# ───────────────────────── тексты и меню ─────────────────────────
START_TEXT = (
    "✨ <b>Добро пожаловать!</b> ✨\n\n"
    "Я бот-помощник для вашего аккаунта: сохраняю исчезающие медиа, ставлю мут и ЧС, "
    "отвечаю автоответчиком, храню голосовые и многое другое.\n\n"
    "🔌 <b>Подключение:</b> Настройки → Telegram для бизнеса → Чат-боты → укажите этого бота.\n\n"
    "Выберите раздел ниже 👇"
)

COMMANDS = [
    ("/look", "👁 <b>/look</b>\n\nОтветьте этой командой на фото или видео (в т.ч. исчезающее) — "
              "бот сохранит его и пришлёт вам сюда, в чат с ботом."),
    ("/read", "📖 <b>/read</b>\n\nПомечает прочитанными все чаты, в которые писали, пока бот был подключён."),
    ("/block", "⛔ <b>/block</b>\n\nНапишите в нужном чате — собеседник попадёт в чёрный список: "
               "все его сообщения будут автоматически удаляться у вас."),
    ("/unblock", "✅ <b>/unblock</b>\n\nНапишите в чате — собеседник будет убран из чёрного списка."),
    ("/mute", "🔇 <b>/mute время</b>\n\nМутит собеседника в этом чате: его сообщения удаляются. "
              "Время: <code>30s</code>, <code>10m</code>, <code>2h</code>, <code>1d</code>. "
              "Без времени — навсегда. Пример: <code>/mute 1h</code>"),
    ("/unmute", "🔊 <b>/unmute</b>\n\nСнимает мут в чате. Можно также нажать кнопку «Снять мут» "
                "под уведомлением, которое бот присылает после выдачи мута."),
    ("/+гс", "🎙 <b>/+гс название</b>\n\nОтветьте командой на голосовое — бот сохранит его под этим названием."),
    ("/гс", "📤 <b>/гс название</b> (или <code>н гс название</code>)\n\n"
            "Отправляет сохранённое голосовое в текущий чат."),
    ("/гсы", "🗂 <b>/гсы</b>\n\nПоказывает все сохранённые голосовые."),
    ("/погода", "🌤 <b>/погода город</b>\n\nПрисылает текущую погоду в городе. Пример: <code>/погода Рига</code>"),
    ("/рестарт", "♻️ <b>/рестарт</b>\n\nПолностью перезагружает бота."),
    ("/хелп", "❓ <b>/хелп</b>\n\nПоказывает список всех команд."),
    ("/авто", "🤖 <b>/авто текст</b>\n\nВключает автоответчик: на первое сообщение от каждого "
              "нового собеседника бот отправит ваш текст один раз. "
              "<code>/авто выкл</code> — выключить, <code>/авто</code> — статус."),
]


def kb_main() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📋 Команды", callback_data="cmds")
    b.button(text="💡 Предложения", callback_data="idea")
    b.button(text="🛠 Связь с разработчиком", callback_data="support")
    b.adjust(2, 1)
    return b.as_markup()


def kb_cmds() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for i, (name, _) in enumerate(COMMANDS):
        b.button(text=name, callback_data=f"cmd:{i}")
    b.adjust(2)
    b.row(InlineKeyboardButton(text="◀️ В меню", callback_data="menu"))
    return b.as_markup()


def kb_single(text: str, data: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=text, callback_data=data)
    return b.as_markup()


# ───────────────────────── бот-меню (личка с ботом) ─────────────────────────
router = Router()


class Form(StatesGroup):
    idea = State()
    support = State()


@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(START_TEXT, reply_markup=kb_main())


@router.callback_query(F.data == "menu")
async def cb_menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text(START_TEXT, reply_markup=kb_main())
    await c.answer()


@router.callback_query(F.data == "cmds")
async def cb_cmds(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text("📋 <b>Все команды бота</b>\n\nНажмите на команду, чтобы узнать, что она делает 👇",
                              reply_markup=kb_cmds())
    await c.answer()


@router.callback_query(F.data.startswith("cmd:"))
async def cb_cmd(c: CallbackQuery):
    i = int(c.data.split(":")[1])
    await c.message.edit_text(COMMANDS[i][1], reply_markup=kb_single("◀️ Назад к командам", "cmds"))
    await c.answer()


@router.callback_query(F.data == "idea")
async def cb_idea(c: CallbackQuery, state: FSMContext):
    await state.set_state(Form.idea)
    await c.message.edit_text(
        "💡 <b>Предложения</b>\n\nПришлите свои предложения — за хорошую идею вы будете вознаграждены 🎁\n\n"
        "⏳ Жду ваше сообщение...",
        reply_markup=kb_single("◀️ Назад", "menu"))
    await c.answer()


@router.callback_query(F.data == "support")
async def cb_support(c: CallbackQuery, state: FSMContext):
    await state.set_state(Form.support)
    await c.message.edit_text(
        "🛠 <b>Связь с разработчиком</b>\n\nСвязь исключительно по багам, проблемам с подключением "
        "или проблемам в работе бота.\n\nОпишите проблему одним сообщением.\n\n⏳ Жду ваше сообщение...",
        reply_markup=kb_single("◀️ Назад", "menu"))
    await c.answer()


async def to_dev(m: Message, bot: Bot, title: str):
    u = m.from_user
    head = (f"{title}\n👤 {esc(u.full_name)}" + (f" (@{esc(u.username)})" if u.username else "") +
            f"\nID: <code>{u.id}</code>\n\n")
    if not DEV_ID:
        log.warning("DEV_ID не задан, сообщение не доставлено")
        return
    if m.text:
        await bot.send_message(DEV_ID, head + esc(m.text))
    else:
        await m.copy_to(DEV_ID, caption=head + esc(m.caption), parse_mode=ParseMode.HTML)


@router.message(F.chat.id == DEV_ID, F.reply_to_message)
async def dev_reply(m: Message, bot: Bot):
    src = m.reply_to_message.text or m.reply_to_message.caption or ""
    found = re.search(r"ID:\s*(\d+)", src)
    if not found:
        return
    uid = int(found.group(1))
    await bot.send_message(uid, "✉️ <b>Ответ от разработчика:</b>")
    await m.copy_to(uid)
    await m.answer("✅ Отправлено")


@router.message(Form.idea)
async def got_idea(m: Message, state: FSMContext, bot: Bot):
    await to_dev(m, bot, "💡 <b>Новое предложение</b>")
    await state.clear()
    await m.answer("✅ <b>Предложение успешно отправлено!</b>\n\nСпасибо за идею 💙",
                   reply_markup=kb_single("🏠 В меню", "menu"))


@router.message(Form.support)
async def got_support(m: Message, state: FSMContext, bot: Bot):
    await to_dev(m, bot, "🛠 <b>Обращение к разработчику</b>")
    await state.clear()
    await m.answer("📨 <b>Обращение отправлено!</b>\n\nОжидайте — вам напишут.",
                   reply_markup=kb_single("🏠 В меню", "menu"))


# ───────────────────────── кнопки снятия мута / ЧС ─────────────────────────
@router.callback_query(F.data.startswith("unmute:"))
async def cb_unmute(c: CallbackQuery):
    cid = c.data.split(":")[1]
    U(c.from_user.id)["mutes"].pop(cid, None)
    save_state()
    await c.message.edit_text("🔊 Мут снят.")
    await c.answer()


@router.callback_query(F.data.startswith("unblock:"))
async def cb_unblock(c: CallbackQuery):
    cid = c.data.split(":")[1]
    u = U(c.from_user.id)
    if cid in u["blocked"]:
        u["blocked"].remove(cid)
    save_state()
    await c.message.edit_text("✅ Убран из чёрного списка.")
    await c.answer()


# ───────────────────────── Business: подключение ─────────────────────────
@router.business_connection()
async def on_connection(bc: BusinessConnection, bot: Bot):
    known = bc.id in S.get("conns", {})
    if bc.is_enabled:
        S.setdefault("conns", {})[bc.id] = {"owner": bc.user.id, "chat": bc.user_chat_id}
        U(bc.user.id)["conn"] = bc.id
        save_state()
        if not known:
            await bot.send_message(
                bc.user_chat_id,
                "✅ <b>Вы успешно подключили бота, удачного пользования!</b>\n\n"
                "Список команд — /хелп (пишите в любом чате) или кнопка «Команды» в /start.")
    else:
        S.get("conns", {}).pop(bc.id, None)
        save_state()
        try:
            await bot.send_message(bc.user_chat_id, "🔌 Бот отключён от вашего аккаунта.")
        except Exception:
            pass


# ───────────────────────── Business: сообщения ─────────────────────────
def is_muted(u: dict, cid: str) -> bool:
    until = u["mutes"].get(cid)
    if until is None:
        return False
    if until == 0 or time.time() < until:
        return True
    u["mutes"].pop(cid, None)
    return False


@router.business_message()
async def on_business(m: Message, bot: Bot):
    conn = m.business_connection_id
    info = S.get("conns", {}).get(conn)
    if not info:
        bc = await bot.get_business_connection(conn)
        info = {"owner": bc.user.id, "chat": bc.user_chat_id}
        S.setdefault("conns", {})[conn] = info
        U(info["owner"])["conn"] = conn
        save_state()
    u = U(info["owner"])
    if m.from_user and m.from_user.id == info["owner"]:
        await owner_command(m, bot, conn, info, u)
    else:
        await incoming(m, bot, conn, u)


async def incoming(m: Message, bot: Bot, conn: str, u: dict):
    if m.from_user is None or m.from_user.is_bot:
        return
    cid = str(m.chat.id)
    if cid in u["blocked"] or is_muted(u, cid):
        try:
            await bot.delete_business_messages(business_connection_id=conn, message_ids=[m.message_id])
        except Exception as e:
            log.warning("не удалось удалить сообщение: %s", e)
        return
    u["unread"][cid] = m.message_id
    a = u["auto"]
    if a["on"] and a["text"] and cid not in a["replied"]:
        a["replied"].append(cid)
        try:
            await bot.send_message(m.chat.id, a["text"], business_connection_id=conn)
        except Exception as e:
            log.warning("автоответ не отправлен: %s", e)
    save_state()


def parse_duration(s: str):
    """'' -> 0 (навсегда), '10m' -> 600, мусор -> None"""
    s = s.strip().lower()
    if not s:
        return 0
    mt = re.fullmatch(r"(\d+)\s*([a-zа-я]*)", s)
    if not mt:
        return None
    n, unit = int(mt.group(1)), mt.group(2)[:1]
    mult = {"s": 1, "с": 1, "m": 60, "м": 60, "h": 3600, "ч": 3600, "d": 86400, "д": 86400, "": 60}.get(unit)
    return n * mult if mult and n > 0 else None


def fmt_dur(sec: int) -> str:
    if sec == 0:
        return "навсегда"
    for unit, size in (("д", 86400), ("ч", 3600), ("мин", 60), ("сек", 1)):
        if sec >= size:
            return f"{sec // size} {unit}"
    return f"{sec} сек"


WEATHER = {0: "☀️ Ясно", 1: "🌤 Малооблачно", 2: "⛅ Переменная облачность", 3: "☁️ Пасмурно",
           45: "🌫 Туман", 48: "🌫 Изморозь", 51: "🌦 Лёгкая морось", 53: "🌦 Морось", 55: "🌦 Сильная морось",
           61: "🌧 Небольшой дождь", 63: "🌧 Дождь", 65: "🌧 Сильный дождь", 71: "🌨 Небольшой снег",
           73: "🌨 Снег", 75: "❄️ Сильный снег", 80: "🌦 Ливни", 81: "🌧 Сильные ливни", 82: "⛈ Очень сильные ливни",
           95: "⛈ Гроза", 96: "⛈ Гроза с градом", 99: "⛈ Сильная гроза с градом"}


async def weather_text(city: str) -> str:
    async with aiohttp.ClientSession() as s:
        async with s.get("https://geocoding-api.open-meteo.com/v1/search",
                         params={"name": city, "count": 1, "language": "ru"}) as r:
            geo = await r.json()
        if not geo.get("results"):
            return f"❌ Город «{esc(city)}» не найден."
        g = geo["results"][0]
        async with s.get("https://api.open-meteo.com/v1/forecast", params={
                "latitude": g["latitude"], "longitude": g["longitude"], "timezone": "auto",
                "current": "temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,weather_code"}) as r:
            w = (await r.json())["current"]
    return (f"🌍 <b>{esc(g['name'])}</b>, {esc(g.get('country', ''))}\n"
            f"{WEATHER.get(w['weather_code'], '🌡')}\n"
            f"🌡 {w['temperature_2m']}°C (ощущается как {w['apparent_temperature']}°C)\n"
            f"💧 Влажность: {w['relative_humidity_2m']}%\n"
            f"💨 Ветер: {w['wind_speed_10m']} км/ч")


async def send_media(bot: Bot, to: int, rm: Message, cap: str) -> bool:
    if rm.photo:
        await bot.send_photo(to, rm.photo[-1].file_id, caption=cap)
    elif rm.video:
        await bot.send_video(to, rm.video.file_id, caption=cap)
    elif rm.video_note:
        await bot.send_message(to, cap)
        await bot.send_video_note(to, rm.video_note.file_id)
    elif rm.voice:
        await bot.send_voice(to, rm.voice.file_id, caption=cap)
    elif rm.animation:
        await bot.send_animation(to, rm.animation.file_id, caption=cap)
    elif rm.audio:
        await bot.send_audio(to, rm.audio.file_id, caption=cap)
    elif rm.document:
        await bot.send_document(to, rm.document.file_id, caption=cap)
    else:
        return False
    return True


RESTART = {"flag": False}


async def owner_command(m: Message, bot: Bot, conn: str, info: dict, u: dict):
    text = (m.text or m.caption or "").strip()
    low = text.lower()
    if low.startswith("н гс "):
        cmd, arg = "/гс", text[5:].strip()
    elif low.startswith("/"):
        parts = text.split(maxsplit=1)
        cmd, arg = parts[0].split("@")[0].lower(), (parts[1].strip() if len(parts) > 1 else "")
    else:
        return

    me = info["chat"]
    cid = str(m.chat.id)
    name = esc(m.chat.full_name)

    async def say(t: str, kb: InlineKeyboardMarkup | None = None):
        await bot.send_message(me, t, reply_markup=kb)

    known = True
    if cmd == "/look":
        rm = m.reply_to_message
        if not rm:
            await say("👁 Ответьте командой /look на фото или видео.")
        else:
            ok = await send_media(bot, me, rm, f"👁 Из чата с {name}")
            if not ok:
                await say("😕 Не удалось получить медиа: Telegram не передал его боту "
                          "(часть исчезающих медиа боты не получают).")

    elif cmd == "/read":
        n = 0
        for c, mid in list(u["unread"].items()):
            try:
                await bot.read_business_message(business_connection_id=conn, chat_id=int(c), message_id=mid)
                n += 1
            except Exception as e:
                log.warning("read %s: %s", c, e)
        u["unread"].clear()
        await say(f"📖 Прочитано чатов: <b>{n}</b>")

    elif cmd == "/block":
        if cid not in u["blocked"]:
            u["blocked"].append(cid)
        await say(f"⛔ <b>{name}</b> в чёрном списке — его сообщения будут удаляться.",
                  kb_single("✅ Убрать из ЧС", f"unblock:{cid}"))

    elif cmd == "/unblock":
        if cid in u["blocked"]:
            u["blocked"].remove(cid)
        await say(f"✅ <b>{name}</b> убран из чёрного списка.")

    elif cmd == "/mute":
        sec = parse_duration(arg)
        if sec is None:
            await say("❌ Неверное время. Пример: <code>/mute 10m</code>, <code>/mute 2h</code>, <code>/mute 1d</code>")
        else:
            u["mutes"][cid] = int(time.time()) + sec if sec else 0
            await say(f"🔇 <b>{name}</b> замучен: <b>{fmt_dur(sec)}</b>.\nЕго сообщения удаляются.",
                      kb_single("🔊 Снять мут", f"unmute:{cid}"))

    elif cmd == "/unmute":
        u["mutes"].pop(cid, None)
        await say(f"🔊 Мут с <b>{name}</b> снят.")

    elif cmd == "/+гс":
        rm = m.reply_to_message
        v = rm and (rm.voice or rm.audio)
        if not arg or not v:
            await say("🎙 Ответьте на голосовое: <code>/+гс название</code>")
        else:
            u["voices"][arg.lower()] = {"id": v.file_id, "type": "voice" if rm.voice else "audio"}
            await say(f"🎙 Голосовое сохранено как <b>{esc(arg)}</b>.\nОтправка: <code>/гс {esc(arg)}</code>")

    elif cmd == "/гс":
        v = u["voices"].get(arg.lower())
        if not v:
            await say(f"❌ Голосового «{esc(arg)}» нет. Список: /гсы")
        elif v["type"] == "voice":
            await bot.send_voice(m.chat.id, v["id"], business_connection_id=conn)
        else:
            await bot.send_audio(m.chat.id, v["id"], business_connection_id=conn)

    elif cmd == "/гсы":
        if u["voices"]:
            lst = "\n".join(f"• <code>{esc(k)}</code>" for k in u["voices"])
            await say(f"🗂 <b>Сохранённые голосовые:</b>\n{lst}\n\nОтправить: <code>/гс название</code>")
        else:
            await say("🗂 Сохранённых голосовых пока нет. Сохранить: ответьте на гс командой <code>/+гс название</code>")

    elif cmd == "/погода":
        if not arg:
            await say("🌤 Укажите город: <code>/погода Рига</code>")
        else:
            try:
                await say(await weather_text(arg))
            except Exception as e:
                log.warning("weather: %s", e)
                await say("❌ Не удалось получить погоду, попробуйте позже.")

    elif cmd == "/авто":
        a = u["auto"]
        if not arg:
            await say(f"🤖 Автоответчик: <b>{'включён' if a['on'] else 'выключен'}</b>\n"
                      f"Текст: {esc(a['text']) or '—'}\n\nЗадать: <code>/авто текст</code>, выключить: <code>/авто выкл</code>")
        elif arg.lower() in ("выкл", "off"):
            a["on"] = False
            await say("🤖 Автоответчик выключен.")
        else:
            a.update(on=True, text=arg, replied=[])
            await say(f"🤖 Автоответчик включён.\nТекст: {esc(arg)}")

    elif cmd == "/хелп":
        lst = "\n\n".join(d for _, d in COMMANDS)
        await say(f"❓ <b>Команды бота</b>\n\n{lst}")

    elif cmd == "/рестарт":
        await say("♻️ Перезагружаюсь... вернусь через минуту.")
        RESTART["flag"] = True
        save_state()
        await dp.stop_polling()

    else:
        known = False

    if known:
        save_state()
        try:  # убираем сообщение с командой из чата (нужно право на удаление)
            await bot.delete_business_messages(business_connection_id=conn, message_ids=[m.message_id])
        except Exception:
            pass


dp = Dispatcher()
dp.include_router(router)


async def main():
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await bot.set_my_commands([BotCommand(command="start", description="Открыть меню")])
    await bot.delete_webhook(drop_pending_updates=False)
    log.info("Бот запущен")
    try:
        await dp.start_polling(bot, allowed_updates=[
            "message", "callback_query", "business_connection", "business_message"])
    finally:
        save_state()
        await bot.session.close()
        log.info("Бот остановлен")


if __name__ == "__main__":
    asyncio.run(main())
