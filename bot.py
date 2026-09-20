import asyncio
import hashlib
import html
import io
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    BusinessConnection, CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, Message,
)
from PIL import Image

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("Не задана переменная окружения BOT_TOKEN")

# Папка для сохранения настроек (на bothost это /app/data, она переживает перезапуск)
DATA_DIR = os.getenv("DATA_DIR") or ("/app/data" if os.path.isdir("/app") else "data")
os.makedirs(DATA_DIR, exist_ok=True)
STATE_FILE = os.path.join(DATA_DIR, "state.json")

TEST_TEXT = "проверка, работает"
MAX_REPEAT = 10  # максимум сообщений для .spam и .haha
LAUGHS = ["ахахах", "хахаха", "АХАХАХ", "ахах", "хех", "ахаха))", "😂", "🤣"]
ACTIONS = {
    "typing": "typing", "voice": "record_voice", "video": "record_video",
    "circle": "record_video_note", "photo": "upload_photo",
    "file": "upload_document", "sticker": "choose_sticker",
}
TOGGLES = {
    "clone": "🪞 Клонирование сообщений",
    "wsag": "🚫 Анти стикер/гиф",
    "wbl": "🧼 Фильтр мата",
    "bw": "🔤 Бан-слова",
}
KNOWN = {
    "help", "spam", "haha", "mute", "unmute", "warn", "unwarn", "bchat", "unbchat",
    "burn", "type", "imit", "unimit", "time", "pinf", "spinf", "bwadd", "bwdel", "bwlist",
    "flip", "rps", "xox", "revo", "duel", "streak",
} | set(TOGGLES) | {"un" + k for k in TOGGLES}

PROFANITY = re.compile(
    r"(?<!\w)(?:за|на|вы|по|у|съ|от|до|при|раз|об|под|про|с)?"
    r"(?:хуй|хуе|хуя|хуи|пизд|[её]ба|[её]бл|[её]бн|бляд|блят|сучк|сука(?!\w)|"
    r"мудак|мудил|пидор|пидар|залуп|гандон)",
    re.IGNORECASE,
)

HELP = """🤖 Команды (работают только от твоего имени, в личных чатах)

.spam [кол-во] [текст] — повторить сообщение (максимум 10)
.haha [кол-во] — случайный смех (максимум 10)
.mute [30s|5m|2h] / .unmute — удалять сообщения собеседника (в чате статус с кнопкой «Снять мут»)
.warn [число] / .unwarn — лимит сообщений, потом мут навсегда
.clone / .unclone — отправлять собеседнику его же текст обратно
.wsag / .unwsag — удалять стикеры и гифки собеседника
.wbl / .unwbl — фильтр мата
.bw / .unbw — удалять сообщения с бан-словами
.bwadd слово / .bwdel слово / .bwlist — список бан-слов
.imit typing|voice|video|circle|photo|file|sticker / .unimit — имитация действия
.time [часовой пояс|off] — время в фамилии профиля, например .time Europe/Kyiv
.burn 30s текст — сообщение, которое исчезнет через время
.bchat 30s / .unbchat — автоудаление всех новых сообщений в чате
.pinf — метаданные фото (ответом на фото-файл), результат виден в чате
.spinf — то же, но результат приходит только тебе сюда
.type текст — отправить текст по словам
.revo / .rps / .flip / .xox / .duel — игры с собеседником
.streak — серия с собеседником (как в Snapchat)
.help — меню команд с кнопками

Время: число + s/m/h (максимум 24 часа)."""

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# ---------------------------------------------------------------- состояние
state = {"chats": {}, "queue": [], "bad_words": [], "owners": {}, "time": {}, "names": {}}
dirty = False
sent_ids: set[int] = set()  # id сообщений, отправленных самим ботом
imit_tasks: dict[str, asyncio.Task] = {}


def load():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state.update(json.load(f))
    except FileNotFoundError:
        pass
    except Exception:
        log.exception("не удалось прочитать state.json")


def save():
    global dirty
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)
    dirty = False


def touch():
    global dirty
    dirty = True


def chat_state(conn_id: str, chat_id: int) -> dict:
    return state["chats"].setdefault(f"{conn_id}:{chat_id}", {})


async def get_owner(conn_id: str) -> int:
    if conn_id not in state["owners"]:
        conn = await bot.get_business_connection(conn_id)
        state["owners"][conn_id] = conn.user.id
        touch()
    return state["owners"][conn_id]


def parse_dur(s: str):
    m = re.fullmatch(r"(\d+)([smh])", (s or "").strip().lower())
    if not m:
        return None
    sec = int(m[1]) * {"s": 1, "m": 60, "h": 3600}[m[2]]
    return min(sec, 86400) if sec > 0 else None


# ---------------------------------------------------------------- отправка / удаление
async def notify(owner_id: int, text: str):
    """Сообщение владельцу в чат с ботом (собеседник его не видит)."""
    try:
        await bot.send_message(owner_id, text, disable_web_page_preview=True)
    except Exception as e:
        log.warning("не удалось написать владельцу: %s", e)


async def delete_msgs(conn_id: str, ids: list[int]):
    try:
        await bot.delete_business_messages(conn_id, ids)
    except Exception as e:
        log.warning("удаление не удалось: %s", e)


def schedule_delete(conn_id: str, msg_id: int, seconds: int):
    state["queue"].append([conn_id, msg_id, time.time() + seconds])
    touch()


async def send(conn_id: str, chat_id: int, text: str, reply_markup=None, keep: bool = False) -> Message:
    m = await bot.send_message(
        chat_id=chat_id, text=text, business_connection_id=conn_id, reply_markup=reply_markup
    )
    if len(sent_ids) > 5000:
        sent_ids.clear()
    sent_ids.add(m.message_id)
    ttl = chat_state(conn_id, chat_id).get("bchat")
    if ttl and not keep:
        schedule_delete(conn_id, m.message_id, ttl)
    return m


async def replace_text(message: Message, text: str):
    """Заменить своё сообщение текстом: редактирование, а если нельзя — удалить и отправить заново."""
    conn_id = message.business_connection_id
    try:
        await bot.edit_message_text(
            text, business_connection_id=conn_id,
            chat_id=message.chat.id, message_id=message.message_id,
        )
    except Exception as e:
        log.warning("редактирование не удалось (%s), удаляю и отправляю заново", e)
        await bot.delete_business_messages(conn_id, [message.message_id])
        await send(conn_id, message.chat.id, text)


async def done(message: Message, owner_id: int, note: str):
    """Убрать команду из чата и тихо сообщить владельцу результат."""
    await delete_msgs(message.business_connection_id, [message.message_id])
    await notify(owner_id, f"{note}\nЧат: {message.chat.full_name or message.chat.id}")


# ---------------------------------------------------------------- статусы с кнопками
# режимы, о которых пишем прямо в чат с собеседником; остальные (розыгрыши) — только тебе в чат с ботом
PUBLIC = {"mute", "warn", "wsag", "wbl", "bw", "bchat"}
BUTTONS = {
    "mute": "🔊 Снять мут", "warn": "✅ Снять warn", "wsag": "❌ Выключить", "wbl": "❌ Выключить",
    "bw": "❌ Выключить", "bchat": "❌ Выключить", "clone": "❌ Выключить",
    "imit": "⏹ Остановить", "time": "❌ Выключить",
}


def who_of(chat) -> str:
    username = getattr(chat, "username", None)
    return f"@{username}" if username else (chat.full_name or str(chat.id))


async def announce(message: Message, owner_id: int, key: str, text: str):
    """Убрать команду и показать статус режима с кнопкой отмены."""
    conn_id = message.business_connection_id
    chat_id = message.chat.id
    st = chat_state(conn_id, chat_id)
    st["who"] = who_of(message.chat)
    touch()
    await delete_msgs(conn_id, [message.message_id])
    kb = markup([[(BUTTONS[key], f"u:{key}:{chat_id}")]])
    if key in PUBLIC:
        try:
            m = await send(conn_id, chat_id, text, reply_markup=kb, keep=True)
            st.setdefault("status", {})[key] = m.message_id
            return
        except Exception as e:
            # Telegram не принял кнопку в бизнес-чате: пишем в чат текст, а кнопку даём в чате с ботом
            log.warning("кнопку в чат отправить не удалось (%s)", e)
            await send(conn_id, chat_id, text, keep=True)
    await bot.send_message(owner_id, f"{text}\nЧат: {st['who']}", reply_markup=kb)


async def apply_undo(conn_id: str, chat_id: int, key: str) -> str:
    """Выключить режим и, если в чате висит его статус, заменить статус на итоговый текст."""
    st = chat_state(conn_id, chat_id)
    who = st.get("who", "собеседник")
    if key == "mute":
        st.pop("mute_until", None)
        st["warn_count"] = 0
        text = f"🔊 {who} снят с мута"
    elif key == "warn":
        st.pop("warn_limit", None)
        st.pop("warn_count", None)
        text = f"✅ Warn снят с {who}"
    elif key in TOGGLES:
        st.pop(key, None)
        text = f"{TOGGLES[key]}: выключено"
    elif key == "bchat":
        st.pop("bchat", None)
        text = "⏳ Автоудаление выключено"
    elif key == "imit":
        task = imit_tasks.pop(f"{conn_id}:{chat_id}", None)
        if task:
            task.cancel()
        text = "🎭 Имитация выключена"
    elif key == "time":
        cfg = state["time"].pop(conn_id, None)
        if cfg:
            try:
                await bot.set_business_account_name(
                    conn_id, first_name=cfg["first"], last_name=cfg["last"] or None
                )
            except Exception as e:
                log.warning("не удалось вернуть фамилию: %s", e)
        text = "🕒 Время в профиле выключено"
    else:
        return ""
    touch()
    mid = st.get("status", {}).pop(key, None)
    if mid:
        try:
            await bot.edit_message_text(
                text, business_connection_id=conn_id, chat_id=chat_id, message_id=mid
            )
        except Exception as e:
            log.warning("не удалось обновить статус в чате: %s", e)
    return text


# ---------------------------------------------------------------- фоновые задачи
async def worker():
    """Удаляет сообщения по расписанию (.burn, .bchat) и сохраняет состояние."""
    while True:
        await asyncio.sleep(3)
        try:
            now = time.time()
            due: dict[str, set[int]] = {}
            rest = []
            for conn_id, msg_id, ts in state["queue"]:
                if ts <= now:
                    due.setdefault(conn_id, set()).add(msg_id)
                else:
                    rest.append([conn_id, msg_id, ts])
            if due:
                state["queue"] = rest
                touch()
                for conn_id, ids in due.items():
                    ids = sorted(ids)
                    for i in range(0, len(ids), 100):
                        await delete_msgs(conn_id, ids[i:i + 100])
            if dirty:
                save()
        except Exception:
            log.exception("ошибка в worker")


async def update_clock(conn_id: str):
    cfg = state["time"].get(conn_id)
    if not cfg:
        return
    t = datetime.now(ZoneInfo(cfg["tz"])).strftime("%H:%M")
    name = f"{cfg['last']} {t}".strip()[:64]
    await bot.set_business_account_name(conn_id, first_name=cfg["first"], last_name=name)


async def clock_loop():
    while True:
        await asyncio.sleep(60 - datetime.now().second + 1)
        for conn_id in list(state["time"]):
            try:
                await update_clock(conn_id)
            except Exception as e:
                log.warning("не удалось обновить время в профиле: %s", e)


async def imit_loop(conn_id: str, chat_id: int, action: str):
    try:
        while True:
            await bot.send_chat_action(chat_id, action, business_connection_id=conn_id)
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.warning("имитация остановлена: %s", e)


# ---------------------------------------------------------------- метаданные фото
def _dms(value, ref) -> float:
    d, m, s = (float(x) for x in value)
    res = d + m / 60 + s / 3600
    return -res if ref in ("S", "W") else res


def _ifd(exif, tag):
    try:
        return dict(exif.get_ifd(tag))
    except Exception:
        return {}


def photo_report(data: bytes, name: str) -> str:
    lines = [
        f"📄 Файл: {name}",
        f"📦 Размер: {len(data) / 1024:.1f} КБ ({len(data)} байт)",
        f"MD5: {hashlib.md5(data).hexdigest()}",
        f"SHA-256: {hashlib.sha256(data).hexdigest()}",
    ]
    try:
        img = Image.open(io.BytesIO(data))
        lines.append(f"🖼 Формат: {img.format}, {img.width}×{img.height}")
        exif = img.getexif()
    except Exception:
        return "\n".join(lines + ["Не удалось прочитать изображение."])

    main, sub, gps = dict(exif), _ifd(exif, 0x8769), _ifd(exif, 0x8825)
    device = " ".join(str(x).strip() for x in (main.get(271), main.get(272)) if x)
    if device:
        lines.append(f"📱 Устройство: {device}")
    if sub.get(42036):
        lines.append(f"🔍 Объектив: {sub[42036]}")
    if main.get(305):
        lines.append(f"🛠 ПО: {main[305]}")
    taken = sub.get(36867) or main.get(306)
    if taken:
        lines.append(f"🕒 Дата съёмки: {taken}")
    try:
        if gps.get(2) and gps.get(4):
            lat, lon = _dms(gps[2], gps.get(1)), _dms(gps[4], gps.get(3))
            lines.append(f"📍 Координаты: {lat:.6f}, {lon:.6f}")
            lines.append(f"https://maps.google.com/?q={lat:.6f},{lon:.6f}")
        else:
            lines.append("📍 Геолокации в файле нет")
    except Exception:
        lines.append("📍 Не удалось разобрать геолокацию")
    if not main and not sub:
        lines.append("ℹ️ EXIF-данных в файле нет")
    return "\n".join(lines)


async def get_replied_image(message: Message):
    r = message.reply_to_message
    doc = r.document if r else None
    if not doc or not (doc.mime_type or "").startswith("image/"):
        return None, None
    buf = io.BytesIO()
    await bot.download(doc.file_id, destination=buf)
    return buf.getvalue(), doc.file_name or "файл"


# ---------------------------------------------------------------- команды владельца
async def bad_usage(message: Message, owner_id: int, usage: str):
    await done(message, owner_id, f"⚠️ Использование: {usage}")


async def handle_command(message: Message, owner_id: int):
    conn_id = message.business_connection_id
    chat_id = message.chat.id
    st = chat_state(conn_id, chat_id)
    text = (message.text or message.caption or "").strip()
    cmd, _, arg = text[1:].partition(" ")
    cmd, arg = cmd.lower(), arg.strip()

    if cmd not in KNOWN:
        if st.get("bchat"):
            schedule_delete(conn_id, message.message_id, st["bchat"])
        return

    if cmd == "help":
        await delete_msgs(conn_id, [message.message_id])
        await send_menu(owner_id)
        return

    # --- удаление сообщений собеседника
    if cmd == "mute":
        dur = parse_dur(arg) if arg else None
        if arg and dur is None:
            return await bad_usage(message, owner_id, ".mute [30s|5m|2h]")
        st["mute_until"] = time.time() + dur if dur else 0
        touch()
        return await announce(
            message, owner_id, "mute",
            f"🔇 {who_of(message.chat)} в муте " + (f"на {arg}" if dur else "навсегда"),
        )
    if cmd == "unmute":
        return await done(message, owner_id, await apply_undo(conn_id, chat_id, "mute"))
    if cmd == "warn":
        if not arg.isdigit() or int(arg) < 1:
            return await bad_usage(message, owner_id, ".warn [число]")
        st["warn_limit"], st["warn_count"] = int(arg), 0
        touch()
        return await announce(
            message, owner_id, "warn",
            f"⚠️ {who_of(message.chat)}: warn, можно написать {arg} сообщений",
        )
    if cmd == "unwarn":
        return await done(message, owner_id, await apply_undo(conn_id, chat_id, "warn"))
    if cmd in TOGGLES:
        st[cmd] = True
        touch()
        return await announce(message, owner_id, cmd, f"{TOGGLES[cmd]}: включено")
    if cmd.startswith("un") and cmd[2:] in TOGGLES:
        return await done(message, owner_id, await apply_undo(conn_id, chat_id, cmd[2:]))
    if cmd == "bwadd" and arg:
        word = arg.lower()
        if word not in state["bad_words"]:
            state["bad_words"].append(word)
            touch()
        return await done(message, owner_id, f"➕ Бан-слово добавлено: {word}")
    if cmd == "bwdel" and arg:
        word = arg.lower()
        if word in state["bad_words"]:
            state["bad_words"].remove(word)
            touch()
        return await done(message, owner_id, f"➖ Бан-слово удалено: {word}")
    if cmd == "bwlist":
        return await done(message, owner_id, "🔤 Бан-слова: " + (", ".join(state["bad_words"]) or "список пуст"))

    # --- автоудаление
    if cmd == "bchat":
        dur = parse_dur(arg)
        if not dur:
            return await bad_usage(message, owner_id, ".bchat 30s | 5m | 2h")
        st["bchat"] = dur
        touch()
        return await announce(message, owner_id, "bchat", f"⏳ Автоудаление сообщений через {arg}")
    if cmd == "unbchat":
        return await done(message, owner_id, await apply_undo(conn_id, chat_id, "bchat"))
    if cmd == "burn":
        first, _, body = arg.partition(" ")
        dur = parse_dur(first)
        if not dur or not body.strip():
            return await bad_usage(message, owner_id, ".burn 30s текст")
        await delete_msgs(conn_id, [message.message_id])
        m = await send(conn_id, chat_id, body.strip())
        schedule_delete(conn_id, m.message_id, dur)
        return

    # --- текст
    if cmd == "type":
        if not arg:
            return await bad_usage(message, owner_id, ".type текст")
        await delete_msgs(conn_id, [message.message_id])
        for word in arg.split()[:50]:
            await send(conn_id, chat_id, word)
            await asyncio.sleep(0.7)
        return
    if cmd == "spam":
        first, _, body = arg.partition(" ")
        if not first.isdigit() or int(first) < 1:
            return await bad_usage(message, owner_id, ".spam [кол-во] [текст]")
        await delete_msgs(conn_id, [message.message_id])
        for _ in range(min(int(first), MAX_REPEAT)):
            await send(conn_id, chat_id, body.strip() or "спам")
            await asyncio.sleep(0.5)
        return
    if cmd == "haha":
        count = int(arg) if arg.isdigit() and int(arg) > 0 else 3
        await delete_msgs(conn_id, [message.message_id])
        for _ in range(min(count, MAX_REPEAT)):
            await send(conn_id, chat_id, random.choice(LAUGHS))
            await asyncio.sleep(0.5)
        return

    # --- имитация действия
    key = f"{conn_id}:{chat_id}"
    if cmd == "imit":
        action = ACTIONS.get(arg.lower())
        if not action:
            return await bad_usage(message, owner_id, ".imit " + "|".join(ACTIONS))
        if key in imit_tasks:
            imit_tasks[key].cancel()
        imit_tasks[key] = asyncio.create_task(imit_loop(conn_id, chat_id, action))
        return await announce(message, owner_id, "imit", f"🎭 Имитация включена: {arg.lower()}")
    if cmd == "unimit":
        return await done(message, owner_id, await apply_undo(conn_id, chat_id, "imit"))

    # --- время в фамилии профиля
    if cmd == "time":
        if arg.lower() in ("off", "выкл"):
            return await done(message, owner_id, await apply_undo(conn_id, chat_id, "time"))
        tz = arg or "UTC"
        try:
            ZoneInfo(tz)
        except Exception:
            return await bad_usage(message, owner_id, ".time Europe/Kyiv (или .time off)")
        old = state["time"].get(conn_id)
        if old:
            first, last = old["first"], old["last"]
        else:
            conn = await bot.get_business_connection(conn_id)
            first, last = conn.user.first_name, conn.user.last_name or ""
        state["time"][conn_id] = {"tz": tz, "first": first, "last": last}
        touch()
        try:
            await update_clock(conn_id)
        except Exception as e:
            state["time"].pop(conn_id, None)
            return await done(message, owner_id, f"⚠️ Не удалось изменить имя ({e}). Проверь, что у бота есть право на изменение имени.")
        return await announce(message, owner_id, "time", f"🕒 Время в профиле включено ({tz})")

    # --- игры
    if cmd in GAME_CMDS:
        return await g_start(message, owner_id, cmd)
    if cmd == "streak":
        return await streak_menu(message, owner_id)

    # --- метаданные фото
    if cmd in ("pinf", "spinf"):
        data, name = await get_replied_image(message)
        if data is None:
            return await done(message, owner_id, "⚠️ Ответь командой на фото, отправленное как файл (без сжатия).")
        report = photo_report(data, name)
        if cmd == "pinf":
            return await replace_text(message, report)
        await delete_msgs(conn_id, [message.message_id])
        await notify(owner_id, "🕵️ Secret photo information\n\n" + report)


# ---------------------------------------------------------------- сообщения собеседника
async def handle_incoming(message: Message):
    conn_id = message.business_connection_id
    st = chat_state(conn_id, message.chat.id)
    text = message.text or message.caption or ""
    delete = False

    mu = st.get("mute_until")
    if mu is not None:
        if mu == 0 or mu > time.time():
            delete = True
        else:
            st.pop("mute_until", None)
            touch()
    if not delete and st.get("warn_limit"):
        st["warn_count"] = st.get("warn_count", 0) + 1
        touch()
        if st["warn_count"] > st["warn_limit"]:
            st["mute_until"] = 0
            delete = True
    if not delete and st.get("wsag") and (message.sticker or message.animation):
        delete = True
    if not delete and st.get("wbl") and PROFANITY.search(text):
        delete = True
    if not delete and st.get("bw") and any(w in text.lower() for w in state["bad_words"]):
        delete = True

    if delete:
        await delete_msgs(conn_id, [message.message_id])
        return
    if st.get("bchat"):
        schedule_delete(conn_id, message.message_id, st["bchat"])
    if st.get("clone") and message.text:
        await send(conn_id, message.chat.id, message.text)
    await streak_event(conn_id, message.chat.id, "them")


# ---------------------------------------------------------------- обработчики Telegram
@dp.business_connection()
async def on_connection(conn: BusinessConnection):
    log.info("business_connection: enabled=%s rights=%s", conn.is_enabled, conn.rights)
    state["owners"][conn.id] = conn.user.id
    state["names"][conn.id] = conn.user.first_name or "Я"
    touch()
    if conn.is_enabled:
        await bot.send_message(conn.user_chat_id, "бот успешно подключен")


@dp.business_message()
async def on_business_message(message: Message):
    if message.message_id in sent_ids:  # сообщения, которые отправил сам бот, игнорируем
        return
    conn_id = message.business_connection_id
    owner_id = await get_owner(conn_id)
    sender = message.from_user.id if message.from_user else None

    if sender != owner_id:
        try:
            await handle_incoming(message)
        except Exception:
            log.exception("ошибка при обработке сообщения собеседника")
        return

    # дальше только сообщения ВЛАДЕЛЬЦА аккаунта
    text = (message.text or message.caption or "").strip()
    try:
        if text == "/help":
            await replace_text(message, TEST_TEXT)
        elif text.startswith("."):
            await handle_command(message, owner_id)
        else:
            ttl = chat_state(conn_id, message.chat.id).get("bchat")
            if ttl:
                schedule_delete(conn_id, message.message_id, ttl)
            await streak_event(conn_id, message.chat.id, "me")
    except Exception as e:
        log.exception("ошибка команды")
        await notify(owner_id, f"⚠️ Ошибка команды: {e}")


# ---------------------------------------------------------------- меню с кнопками
def _card(cmd: str, body: str) -> str:
    return f"<b>Команда:</b> <code>.{cmd}</code>\n\n<blockquote>{body}</blockquote>"


DESC = {
    "spam": (
        "Спам в чате с собеседником (максимум 10 сообщений)\n\n"
        "<b>Использование:</b>\n<code>.spam [кол-во] [текст]</code>\n\n"
        "<b>Пример:</b>\n<code>.spam 5 Привет</code>"
    ),
    "haha": (
        "Отправляет в чат случайный смех отдельными сообщениями (максимум 10)\n\n"
        "<b>Использование:</b>\n<code>.haha [кол-во]</code>\n\n"
        "<b>Пример:</b>\n<code>.haha 5</code>"
    ),
    "mute": (
        "После команды <code>.mute</code> все сообщения собеседника автоматически удаляются. "
        "После <code>.unmute</code> сообщения перестают удаляться.\n\n"
        "1. <code>.mute 30s</code> — мутит на 30 секунд\n"
        "2. <code>.mute 5m</code> — мутит на 5 минут\n"
        "3. <code>.mute 2h</code> — мутит на 2 часа\n"
        "4. <code>.mute</code> — мутит навсегда\n"
        "5. <code>.unmute</code> — снимает мут досрочно\n\n"
        "<b>Использование:</b>\n<code>.mute [число][s/m/h]</code> — на время\n"
        "<code>.mute</code> — навсегда\n\nМаксимум для времени — 24 часа."
    ),
    "warn": (
        "Warn режим — собеседник может написать указанное количество сообщений, "
        "после чего он замутится навсегда\n\n"
        "<b>Использование:</b>\n<code>.warn [число]</code> — включить с лимитом\n"
        "<code>.unwarn</code> — снять warn\n<code>.unmute</code> — снять мут, выданный после warn\n\n"
        "<b>Пример:</b>\n<code>.warn 5</code> — собеседник может написать 5 сообщений"
    ),
    "clone": (
        "Режим клонирования — все текстовые сообщения собеседника автоматически "
        "отправляются ему обратно\n\n"
        "<b>Использование:</b>\n<code>.clone</code> — включить режим\n"
        "<code>.unclone</code> — выключить режим\n\n"
        "<b>Пример:</b>\nСобеседник пишет: <code>кегля</code>\nБот отправляет ему: <code>кегля</code>"
    ),
    "wsag": (
        "Анти стикер/гифка — стикеры и гифки собеседника автоматически удаляются\n\n"
        "<b>Использование:</b>\n<code>.wsag</code> — включить режим\n"
        "<code>.unwsag</code> — выключить режим"
    ),
    "wbl": (
        "Фильтр мата — удаляет все сообщения с матом от собеседника в текущем чате\n\n"
        "<b>Использование:</b>\n<code>.wbl</code> — включить фильтр\n"
        "<code>.unwbl</code> — выключить фильтр"
    ),
    "bw": (
        "Удаляет сообщения собеседника, в которых есть слова из списка бан-слов\n\n"
        "<b>Использование:</b>\n<code>.bw</code> — включить режим\n"
        "<code>.unbw</code> — выключить режим\n\n"
        "<b>Список слов:</b>\n<code>.bwadd слово</code> — добавить\n"
        "<code>.bwdel слово</code> — удалить\n<code>.bwlist</code> — показать список\n\n"
        "<b>Пример:</b>\nДобавляешь слово <code>спам</code> — собеседник пишет «это спам», "
        "бот удаляет сообщение"
    ),
    "imit": (
        "Имитация действия — бот бесконечно показывает собеседнику, что ты печатаешь, "
        "записываешь голосовое и т.д.\n\n"
        "<b>Использование:</b>\n"
        "<code>.imit typing</code> — печатает\n<code>.imit voice</code> — записывает голосовое\n"
        "<code>.imit video</code> — записывает видео\n<code>.imit circle</code> — записывает кружок\n"
        "<code>.imit photo</code> — отправляет фото\n<code>.imit file</code> — отправляет файл\n"
        "<code>.imit sticker</code> — выбирает стикер\n<code>.unimit</code> — остановить"
    ),
    "time": (
        "Показывает текущее время в фамилии профиля, обновляется каждую минуту.\n\n"
        "<b>Использование:</b>\n<code>.time Europe/Kyiv</code> — включить (укажи свой часовой пояс)\n"
        "<code>.time off</code> — выключить и вернуть прежнюю фамилию\n\n"
        "<b>Информация:</b>\n• Часовой пояс в формате Континент/Город\n"
        "• Требует разрешение на изменение имени"
    ),
    "burn": (
        "Отправляет сообщение, которое исчезает через заданное время.\n\n"
        "1. <code>.burn 30s текст</code> — исчезнет через 30 секунд\n"
        "2. <code>.burn 5m текст</code> — исчезнет через 5 минут\n"
        "3. <code>.burn 2h текст</code> — исчезнет через 2 часа\n\n"
        "<b>Использование:</b>\n<code>.burn [число][s/m/h] текст</code>\n\n"
        "<b>Пример:</b>\n<code>.burn 30s привет</code>\n\nМаксимум — 24 часа."
    ),
    "bchat": (
        "После команды <code>.bchat</code> все новые сообщения в чате (и твои, и собеседника, "
        "любого типа) автоматически удаляются через заданное время. "
        "После <code>.unbchat</code> сообщения перестают удаляться.\n\n"
        "1. <code>.bchat 30s</code> — удаляются через 30 секунд\n"
        "2. <code>.bchat 5m</code> — удаляются через 5 минут\n"
        "3. <code>.bchat 2h</code> — удаляются через 2 часа\n"
        "4. <code>.unbchat</code> — выключает режим досрочно\n\n"
        "<b>Использование:</b>\n<code>.bchat [число][s/m/h]</code> — включить\n"
        "<code>.unbchat</code> — выключить\n\nМаксимум — 24 часа."
    ),
    "pinf": (
        "Показывает информацию о фото прямо в чате с собеседником: устройство, дату съёмки, "
        "геолокацию, размер, хэши и другие метаданные.\n\n"
        "<b>Примечание:</b>\nЕсли на устройстве при съёмке была отключена геолокация, информация "
        "о местоположении отображаться не будет. Также бот может получить информацию только "
        "из фото, отправленного как файл.\n\n"
        "<b>Использование:</b>\nОтветь на фото (отправленное файлом) и напиши <code>.pinf</code>"
    ),
    "spinf": (
        "Показывает информацию о фото тайно: результат приходит только тебе в чат с ботом, "
        "собеседник ничего не увидит и не узнает. Бот покажет устройство, дату съёмки, "
        "геолокацию, размер, хэши и другие метаданные.\n\n"
        "<b>Примечание:</b>\nЕсли на устройстве при съёмке была отключена геолокация, информация "
        "о местоположении отображаться не будет. Также бот может получить информацию только "
        "из фото, отправленного как файл.\n\n"
        "<b>Использование:</b>\nОтветь на фото (отправленное файлом) и напиши <code>.spinf</code>"
    ),
    "type": (
        "Отправляет текст по словам отдельными сообщениями (максимум 50 слов).\n\n"
        "<b>Использование:</b>\nНапиши <code>.type [текст]</code>\n\n"
        "<b>Пример:</b>\n<code>.type всем привет как дела</code>"
    ),
}

DESC.update({
    "xox": (
        "Запускает игру Крестики-нолики прямо в чате с собеседником.\n\n"
        "<b>Использование:</b>\nНапиши <code>.xox</code> — собеседник нажимает «Принять вызов» "
        "и игра начинается!"
    ),
    "flip": (
        "Орёл или Решка — игра с собеседником\n\n"
        "<b>Использование:</b>\n<code>.flip</code> — сыграть в монетку"
    ),
    "rps": (
        "Камень, ножницы, бумага — игра с собеседником\n\n"
        "<b>Использование:</b>\n<code>.rps</code> — начать игру"
    ),
    "revo": (
        "Русская рулетка — игра с собеседником\n\n"
        "<b>Использование:</b>\n<code>.revo</code> — бросить вызов собеседнику\n\n"
        "После принятия вызова начинается игра. Игроки стреляют в самих себя по очереди — "
        "в барабане один заряженный патрон. Кому не повезёт — тот проиграл."
    ),
    "duel": (
        "Дуэль на реакцию с собеседником.\n\n"
        "<b>Использование:</b>\nНапиши <code>.duel</code> — собеседник принимает вызов. "
        "Когда кнопка станет зелёной — жми первым.\n\n"
        "Кто выстрелит раньше времени — рискует застрелиться сам."
    ),
    "streak": (
        "Серия с собеседником — как в Snapchat.\n"
        "Пишите друг другу каждый день, чтобы серия росла.\n\n"
        "<b>Использование:</b>\n<code>.streak</code> — открыть меню серии\n\n"
        "Можно создать глазика — питомца серии.\n"
        "Если не писать день — серия умирает.\n"
        "Восстановить можно 5 раз в месяц.\n\n"
        "День считается по часовому поясу из <code>.time</code> (по умолчанию UTC)."
    ),
})
DESC["mute"] += "\n\nВ чате появится сообщение «@user в муте» с кнопкой «Снять мут»."
for _k in ("warn", "wsag", "wbl", "bw", "bchat"):
    DESC[_k] += "\n\nВ чате появится сообщение о включении с кнопкой «Выключить»."
for _k in ("clone", "imit", "time"):
    DESC[_k] += "\n\nСообщение с кнопкой «Выключить» придёт тебе в чат с ботом."

CATEGORIES = {
    "actions": "Действия с собеседником",
    "games": "Игры",
    "utils": "Утилиты",
    "text": "Текст",
    "media": "Медиа",
}
# какие команды лежат в каждой категории (пустые разделы можно наполнить позже)
CAT_COMMANDS = {
    "actions": [
        "spam", "haha", "mute", "warn", "clone", "wsag", "wbl", "bw",
        "imit", "time", "burn", "bchat", "pinf", "spinf", "type",
    ],
    "games": ["revo", "rps", "flip", "xox", "duel", "streak"],
}

MAIN_TEXT = (
    "📖 <b>Описание команд</b>\n\nСписок команд, доступных в чате с собеседником.\n"
    "<blockquote>Выбери категорию ниже, чтобы посмотреть команды.</blockquote>"
)


def markup(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows]
    )


def screen(path: str):
    """Возвращает (текст, клавиатура) для экрана меню."""
    kind, _, arg = path.partition(":")
    if kind == "cat" and arg in CATEGORIES:
        cmds = CAT_COMMANDS.get(arg, [])
        rows = [[(f".{c}", f"m:cmd:{c}") for c in cmds[i:i + 3]] for i in range(0, len(cmds), 3)]
        rows.append([("👈 Назад", "m:main")])
        hint = ("Выбери команду ниже, чтобы ознакомиться с её функционалом."
                if cmds else "В этом разделе пока нет команд.")
        return f"<b>{CATEGORIES[arg]}</b>\n\n<blockquote>{hint}</blockquote>", markup(rows)
    if kind == "cmd" and arg in DESC:
        return _card(arg, DESC[arg]), markup([[("👈 Назад", "m:cat:actions")]])
    if kind == "brief":
        text = "📋 <b>Краткое описание</b>\n\n<blockquote>" + html.escape(HELP) + "</blockquote>"
        return text, markup([[("👈 Назад", "m:main")]])
    rows = [
        [("Действия с собеседником", "m:cat:actions")],
        [("Игры", "m:cat:games"), ("Утилиты", "m:cat:utils")],
        [("Текст", "m:cat:text"), ("Медиа", "m:cat:media")],
        [("Краткое описание", "m:brief")],
        [("✖️ Закрыть", "m:close")],
    ]
    return MAIN_TEXT, markup(rows)


def is_owner(user_id: int) -> bool:
    owners = set(state["owners"].values())
    return not owners or user_id in owners


async def send_menu(chat_id: int):
    text, kb = screen("main")
    await bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data.startswith("m:"))
async def on_menu(cb: CallbackQuery):
    if not is_owner(cb.from_user.id):
        return await cb.answer("Меню доступно только владельцу бота", show_alert=True)
    if not cb.message:
        return await cb.answer()
    path = cb.data[2:]
    if path == "close":
        try:
            await cb.message.delete()
        except Exception:
            pass
        return await cb.answer()
    text, kb = screen(path)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramBadRequest:
        pass  # например, "message is not modified"
    await cb.answer()


# кнопки «Снять мут» / «Выключить» под статусами
@dp.callback_query(F.data.startswith("u:"))
async def on_undo(cb: CallbackQuery):
    try:
        _, key, raw_chat = cb.data.split(":")
        chat_id = int(raw_chat)
    except ValueError:
        return await cb.answer()
    msg = cb.message
    conn_id = getattr(msg, "business_connection_id", None) if msg else None
    if not conn_id:  # кнопка нажата в чате с ботом: ищем подключение по чату
        conn_id = next(
            (k.rsplit(":", 1)[0] for k in state["chats"] if k.endswith(f":{chat_id}")), None
        )
    if not conn_id:
        return await cb.answer("Не нашёл этот чат", show_alert=True)
    if cb.from_user.id != await get_owner(conn_id):
        return await cb.answer("Это может сделать только владелец", show_alert=True)

    text = await apply_undo(conn_id, chat_id, key)
    if msg and text:
        try:
            if getattr(msg, "business_connection_id", None):
                await bot.edit_message_text(
                    text, business_connection_id=conn_id, chat_id=msg.chat.id, message_id=msg.message_id
                )
            else:
                await msg.edit_text(text)
        except Exception as e:  # например, сообщение уже обновлено
            log.info("статус не изменён: %s", e)
    await cb.answer()


# ---------------------------------------------------------------- игры с собеседником
GAME_CMDS = {"flip", "rps", "xox", "revo", "duel"}
games: dict[int, dict] = {}  # chat_id -> текущая игра (одна на чат)
SYM = {"o": "❌", "p": "⭕"}
XOX_LINES = [(0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6)]
RPS_NAMES = {"r": "🪨 камень", "s": "✌️ ножницы", "p": "📄 бумага"}
RPS_BTN = {"r": "🪨 Камень", "s": "✌️ Ножницы", "p": "📄 Бумага"}
RPS_BEATS = {"r": "s", "s": "p", "p": "r"}
COIN = {"h": "🦅 Орёл", "t": "🪙 Решка"}


def gb(chat_id: int, action: str, arg: str = "") -> str:
    return f"g:{chat_id}:{action}" + (f":{arg}" if arg else "")


def other(role: str) -> str:
    return "p" if role == "o" else "o"


def g_name(g: dict, role: str) -> str:
    return g["o"] if role == "o" else g["p"]


async def owner_name(conn_id: str) -> str:
    if conn_id not in state["names"]:
        conn = await bot.get_business_connection(conn_id)
        state["names"][conn_id] = conn.user.first_name or "Я"
        touch()
    return state["names"][conn_id]


def g_render(g: dict):
    """Текст и кнопки игры в её текущем состоянии."""
    k, c, o, p = g["kind"], g["chat"], g["o"], g["p"]
    invite = [[("✅ Принять вызов", gb(c, "accept"))]]
    if k == "flip":
        return (f"🪙 Орёл или Решка\n{o} бросает монету, {p} угадывает.\n\n{p}, выбирай сторону!",
                [[(COIN["h"], gb(c, "pick", "h")), (COIN["t"], gb(c, "pick", "t"))]])
    if k == "rps":
        mark = lambda r: "✅" if g["c"][r] else "⏳"
        return (f"✊ Камень, ножницы, бумага\n{o} {mark('o')}  vs  {p} {mark('p')}\n\n"
                "Результат откроется, когда выберут оба.",
                [[(RPS_BTN[x], gb(c, "pick", x)) for x in "rsp"]])
    if k == "xox":
        if g["state"] == "invite":
            return f"❌⭕ Крестики-нолики\n{o} бросает вызов {p}.\n\n{p}, нажми «Принять вызов»!", invite
        rows = [[(g["board"][r * 3 + i] or "▫️", gb(c, "cell", str(r * 3 + i))) for i in range(3)]
                for r in range(3)]
        return (f"❌⭕ Крестики-нолики\n❌ {o}   ⭕ {p}\n\nХод: {SYM[g['turn']]} {g_name(g, g['turn'])}", rows)
    if k == "revo":
        if g["state"] == "invite":
            return (f"🔫 Русская рулетка\n{o} бросает вызов {p}.\n"
                    "В барабане 1 патрон из 6, стреляете в себя по очереди.\n\n"
                    f"{p}, нажми «Принять вызов»!", invite)
        chambers = "⚪" * g["shots"] + "🔘" * (6 - g["shots"])
        return (f"🔫 Русская рулетка\n{o} vs {p}\n\n{g['log']}\n\nБарабан: {chambers}\n"
                f"Ход: {g_name(g, g['turn'])}", [[("🔫 Выстрелить", gb(c, "shoot"))]])
    if g["state"] == "invite":  # duel
        return (f"🤠 Дуэль на реакцию\n{o} вызывает {p}.\n\nКогда кнопка станет зелёной — жми первым. "
                f"Кто выстрелит раньше времени — рискует застрелиться сам.\n\n{p}, нажми «Принять вызов»!",
                invite)
    if g["state"] == "wait":
        return (f"🤠 Дуэль: {o} vs {p}\n\n🔴 Ждите сигнала… Не стреляйте раньше времени!",
                [[("🔴 Ждём…", gb(c, "fire"))]])
    return f"🤠 Дуэль: {o} vs {p}\n\n🟢 СТРЕЛЯЙТЕ!", [[("🟢 ОГОНЬ!", gb(c, "fire"))]]


async def g_edit(g: dict, text: str, rows=None):
    await bot.edit_message_text(
        text, business_connection_id=g["conn"], chat_id=g["chat"], message_id=g["mid"],
        reply_markup=markup(rows) if rows else None,
    )


async def g_finish(g: dict, text: str, rows=None):
    g["over"] = True
    if games.get(g["chat"]) is g:
        del games[g["chat"]]
    if g.get("task"):
        g["task"].cancel()
    await g_edit(g, text, rows)


async def g_start(message: Message, owner_id: int, kind: str):
    conn_id, chat_id = message.business_connection_id, message.chat.id
    await delete_msgs(conn_id, [message.message_id])
    old = games.pop(chat_id, None)
    if old:
        old["over"] = True
        if old.get("task"):
            old["task"].cancel()
    g = {"kind": kind, "conn": conn_id, "chat": chat_id, "owner": owner_id,
         "o": await owner_name(conn_id), "p": who_of(message.chat),
         "state": "invite", "over": False, "lock": asyncio.Lock()}
    if kind == "rps":
        g["c"] = {"o": None, "p": None}
    if kind == "xox":
        g["board"], g["turn"] = [""] * 9, "o"
    text, rows = g_render(g)
    try:
        m = await send(conn_id, chat_id, text, reply_markup=markup(rows), keep=True)
    except Exception as e:
        log.warning("игра: кнопки не отправились: %s", e)
        if kind == "flip":  # монетке кнопки не обязательны
            await send(conn_id, chat_id, f"🪙 Выпало: {COIN[random.choice('ht')]}", keep=True)
        else:
            await notify(owner_id, f"⚠️ Telegram не принял кнопки в этом чате, игра .{kind} не запущена: {e}")
        return
    g["mid"] = m.message_id
    games[chat_id] = g


async def duel_arm(g: dict):
    """Через случайное время кнопка дуэли «зеленеет»."""
    try:
        await asyncio.sleep(random.uniform(3, 8))
        async with g["lock"]:
            if g["over"]:
                return
            g["state"], g["t0"] = "fire", time.monotonic()
            text, rows = g_render(g)
            await g_edit(g, text, rows)
    except asyncio.CancelledError:
        pass
    except Exception:
        log.exception("ошибка дуэли")


async def g_action(cb: CallbackQuery, g: dict, role: str, action: str, arg: str):
    k, name = g["kind"], g_name(g, role)

    if action == "accept":
        if role != "p":
            return await cb.answer("Ждём, пока соперник примет вызов")
        if k == "xox":
            g["state"] = "play"
        elif k == "revo":
            g.update(state="play", bullet=random.randint(0, 5), shots=0,
                     turn=random.choice("op"), log="Барабан прокручен.")
        elif k == "duel":
            g["state"] = "wait"
            g["task"] = asyncio.create_task(duel_arm(g))
        text, rows = g_render(g)
        await g_edit(g, text, rows)
        return await cb.answer()

    if k == "flip" and action == "pick":
        if role != "p":
            return await cb.answer(f"Выбирает {g['p']}")
        coin = random.choice("ht")
        verdict = "угадал(а) 🎉" if coin == arg else "не угадал(а) 😅"
        await g_finish(g, f"🪙 Выпало: {COIN[coin]}\n{g['p']} выбрал(а) {COIN[arg]} — {verdict}")
        return await cb.answer()

    if k == "rps" and action == "pick" and arg in RPS_NAMES:
        if g["c"][role]:
            return await cb.answer("Ты уже сделал выбор")
        g["c"][role] = arg
        await cb.answer(f"Ты выбрал: {RPS_NAMES[arg]}")
        co, cp = g["c"]["o"], g["c"]["p"]
        if not (co and cp):
            text, rows = g_render(g)
            return await g_edit(g, text, rows)
        if co == cp:
            res = "🤝 Ничья!"
        else:
            res = f"🏆 Победил {g['o'] if RPS_BEATS[co] == cp else g['p']}!"
        return await g_finish(
            g, f"✊ Камень, ножницы, бумага\n{g['o']}: {RPS_NAMES[co]}\n{g['p']}: {RPS_NAMES[cp]}\n\n{res}")

    if k == "xox" and action == "cell" and g["state"] == "play":
        i = int(arg)
        if role != g["turn"]:
            return await cb.answer("Сейчас не твой ход")
        if g["board"][i]:
            return await cb.answer("Клетка занята")
        g["board"][i] = SYM[role]
        b = g["board"]
        win = any(b[x] and b[x] == b[y] == b[z] for x, y, z in XOX_LINES)
        if win or all(b):
            head = f"🏆 Победил {SYM[role]} {name}!" if win else "🤝 Ничья!"
            rows = [[(b[r * 3 + j] or "▫️", "g:0:noop") for j in range(3)] for r in range(3)]
            await g_finish(g, f"❌⭕ Крестики-нолики\n{head}", rows)
        else:
            g["turn"] = other(role)
            text, rows = g_render(g)
            await g_edit(g, text, rows)
        return await cb.answer()

    if k == "revo" and action == "shoot" and g["state"] == "play":
        if role != g["turn"]:
            return await cb.answer("Сейчас не твой ход")
        if g["shots"] == g["bullet"]:
            await g_finish(g, f"🔫 Русская рулетка\n\n💥 БАХ! {name} не повезло.\n"
                              f"🏆 Победил {g_name(g, other(role))}!")
        else:
            g["shots"] += 1
            g["turn"] = other(role)
            g["log"] = f"🔫 {name}: щёлк… пусто. Ход переходит к {g_name(g, g['turn'])}."
            text, rows = g_render(g)
            await g_edit(g, text, rows)
        return await cb.answer()

    if k == "duel" and action == "fire":
        if g["state"] == "wait":
            if random.random() < 0.5:
                await g_finish(g, f"🤠 Дуэль\n\n💥 {name} выстрелил раньше времени и застрелился!\n"
                                  f"🏆 Победил {g_name(g, other(role))}!")
                return await cb.answer()
            return await cb.answer("Рано! Тебе повезло — осечка. Жди зелёного!", show_alert=True)
        if g["state"] == "fire":
            ms = int((time.monotonic() - g["t0"]) * 1000)
            await g_finish(g, f"🤠 Дуэль\n\n🏆 {name} выстрелил первым!\n⏱ ≈ {ms} мс")
        return await cb.answer()

    await cb.answer()


@dp.callback_query(F.data.startswith("g:"))
async def on_game(cb: CallbackQuery):
    try:
        _, raw_chat, action, *rest = cb.data.split(":")
        chat_id = int(raw_chat)
    except ValueError:
        return await cb.answer()
    if action == "noop":
        return await cb.answer()
    g = games.get(chat_id)
    if not g or g["over"]:
        return await cb.answer("Эта игра уже закончилась", show_alert=True)
    uid = cb.from_user.id
    if uid not in (g["owner"], chat_id):
        return await cb.answer()
    role = "o" if uid == g["owner"] else "p"
    async with g["lock"]:
        if g["over"]:
            return await cb.answer("Эта игра уже закончилась", show_alert=True)
        try:
            await g_action(cb, g, role, action, rest[0] if rest else "")
        except Exception:
            log.exception("ошибка в игре")
            await cb.answer("Что-то пошло не так")


# ---------------------------------------------------------------- серия (streak)
def _today(conn_id: str):
    cfg = state["time"].get(conn_id)
    return datetime.now(ZoneInfo(cfg["tz"] if cfg else os.getenv("STREAK_TZ", "UTC"))).date()


def _streak(st: dict) -> dict:
    s = st.setdefault("streak", {})
    for key, val in (("on", False), ("count", 0), ("best", 0), ("dead", False), ("lost", 0),
                     ("pet", False), ("me", False), ("them", False), ("restores", {})):
        s.setdefault(key, val)
    return s


def sb(chat_id: int, action: str) -> str:
    return f"s:{chat_id}:{action}"


def streak_tick(conn_id: str, chat_id: int, who: str):
    """Учитывает сообщение («me» — твоё, «them» — собеседника). Возвращает текст события или None."""
    s = chat_state(conn_id, chat_id).get("streak")
    if not s or not s.get("on"):
        return None
    today = _today(conn_id)
    d, event = today.isoformat(), None
    if s.get("day") != d:
        if s["count"] > 0 and s.get("last") not in (d, (today - timedelta(days=1)).isoformat()):
            s["lost"], s["count"], s["dead"] = s["count"], 0, True
            event = f"💀 Серия {s['lost']} дн. умерла. Открой .streak, чтобы восстановить."
        s["day"], s["me"], s["them"] = d, False, False
    s[who] = True
    if s["me"] and s["them"] and s.get("last") != d:
        if s["dead"]:  # серию не восстановили — начинается новая
            s["dead"], s["count"] = False, 0
        s["count"] += 1
        s["last"] = d
        s["best"] = max(s["best"], s["count"])
        event = f"🔥 Серия: {s['count']} дн." + (" 👁" if s["pet"] else "")
    touch()
    return event


async def streak_event(conn_id: str, chat_id: int, who: str):
    ev = streak_tick(conn_id, chat_id, who)
    if ev:
        try:
            await send(conn_id, chat_id, ev, keep=True)
        except Exception as e:
            log.warning("серия: не удалось написать в чат: %s", e)


def streak_render(conn_id: str, chat_id: int, who: str):
    s = _streak(chat_state(conn_id, chat_id))
    today = _today(conn_id)
    left = max(0, 5 - s["restores"].get(today.strftime("%Y-%m"), 0))
    if s["dead"]:
        line = f"💀 Серия умерла (было {s['lost']} дн.)"
    elif s["on"]:
        line = f"🔥 {s['count']} дн."
    else:
        line = "⏸ выключена"
    fresh = s.get("day") == today.isoformat()
    me, them = ("✅" if fresh and s["me"] else "⏳"), ("✅" if fresh and s["them"] else "⏳")
    if not s["pet"]:
        pet = "нет"
    elif s["dead"]:
        pet = "💀 Глазик умер — восстанови серию"
    else:
        pet = f"👁 Глазик, уровень {1 + s['count'] // 7}"
    text = (f"🔥 Серия с {who}\n\n{line}\nРекорд: {s['best']} дн.\n"
            f"Сегодня: я {me}   {who} {them}\n\n🐣 Питомец: {pet}\n"
            f"♻️ Восстановлений в этом месяце: {left} из 5")
    rows = [[("🔴 Выключить серию" if s["on"] else "🟢 Включить серию", sb(chat_id, "toggle"))]]
    if not s["pet"]:
        rows.append([("👁 Создать глазика", sb(chat_id, "pet"))])
    if s["dead"] and left > 0:
        rows.append([("♻️ Восстановить серию", sb(chat_id, "restore"))])
    rows.append([("🔄 Обновить", sb(chat_id, "refresh"))])
    return text, rows


async def streak_menu(message: Message, owner_id: int):
    conn_id, chat_id = message.business_connection_id, message.chat.id
    st = chat_state(conn_id, chat_id)
    st["who"] = who_of(message.chat)
    touch()
    await delete_msgs(conn_id, [message.message_id])
    text, rows = streak_render(conn_id, chat_id, st["who"])
    try:
        await send(conn_id, chat_id, text, reply_markup=markup(rows), keep=True)
    except Exception as e:
        log.warning("серия: кнопки в чат не отправились (%s)", e)
        await bot.send_message(owner_id, text, reply_markup=markup(rows))


@dp.callback_query(F.data.startswith("s:"))
async def on_streak(cb: CallbackQuery):
    try:
        _, raw_chat, action = cb.data.split(":")
        chat_id = int(raw_chat)
    except ValueError:
        return await cb.answer()
    msg = cb.message
    conn_id = getattr(msg, "business_connection_id", None) if msg else None
    if not conn_id:
        conn_id = next((k.rsplit(":", 1)[0] for k in state["chats"] if k.endswith(f":{chat_id}")), None)
    if not conn_id:
        return await cb.answer("Не нашёл этот чат", show_alert=True)
    if cb.from_user.id != await get_owner(conn_id):
        return await cb.answer("Меню серии доступно только владельцу", show_alert=True)

    st = chat_state(conn_id, chat_id)
    s = _streak(st)
    today = _today(conn_id)
    note = None
    if action == "toggle":
        s["on"] = not s["on"]
        if s["on"]:
            s["day"] = None
        note = "Серия включена" if s["on"] else "Серия выключена"
    elif action == "pet":
        s["pet"] = True
        note = "👁 Глазик создан!"
    elif action == "restore":
        month = today.strftime("%Y-%m")
        used = s["restores"].get(month, 0)
        if not s["dead"]:
            note = "Серия и так жива"
        elif used >= 5:
            return await cb.answer("В этом месяце восстановления закончились", show_alert=True)
        else:
            s["restores"][month] = used + 1
            both = s.get("day") == today.isoformat() and s["me"] and s["them"]
            s["dead"] = False
            s["count"] = s["lost"] + (1 if both else 0)
            s["last"] = today.isoformat() if both else (today - timedelta(days=1)).isoformat()
            s["best"] = max(s["best"], s["count"])
            note = "♻️ Серия восстановлена!"
    touch()

    text, rows = streak_render(conn_id, chat_id, st.get("who", "собеседник"))
    if msg:
        try:
            if getattr(msg, "business_connection_id", None):
                await bot.edit_message_text(
                    text, business_connection_id=conn_id, chat_id=msg.chat.id,
                    message_id=msg.message_id, reply_markup=markup(rows))
            else:
                await msg.edit_text(text, reply_markup=markup(rows))
        except Exception as e:
            log.info("меню серии не обновлено: %s", e)
    await cb.answer(note)


# /help прямо в чате с самим ботом
@dp.message(F.text == "/help")
async def on_direct_help(message: Message):
    await message.answer(TEST_TEXT)


# меню с кнопками: /start, /menu или /commands в чате с ботом
@dp.message(F.text.in_({"/start", "/menu", "/commands"}))
async def on_direct_menu(message: Message):
    if not is_owner(message.from_user.id):
        return await message.answer("Это личный бот.")
    await send_menu(message.chat.id)


async def main():
    load()
    tasks = [asyncio.create_task(worker()), asyncio.create_task(clock_loop())]
    log.info("бот запущен")
    try:
        # если у бота включён вебхук, polling не работает: удаляем его
        # (старые необработанные сообщения отбрасываем, чтобы не выполнить их задним числом)
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        for t in tasks:
            t.cancel()
        save()


asyncio.run(main())
