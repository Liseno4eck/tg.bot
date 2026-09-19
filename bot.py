import asyncio
import hashlib
import io
import json
import logging
import os
import random
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.types import BusinessConnection, Message
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
.mute [30s|5m|2h] / .unmute — удалять сообщения собеседника
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
.help — этот список

Время: число + s/m/h (максимум 24 часа)."""

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# ---------------------------------------------------------------- состояние
state = {"chats": {}, "queue": [], "bad_words": [], "owners": {}, "time": {}}
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


async def send(conn_id: str, chat_id: int, text: str) -> Message:
    m = await bot.send_message(chat_id=chat_id, text=text, business_connection_id=conn_id)
    if len(sent_ids) > 5000:
        sent_ids.clear()
    sent_ids.add(m.message_id)
    ttl = chat_state(conn_id, chat_id).get("bchat")
    if ttl:
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
        await notify(owner_id, HELP)
        return

    # --- удаление сообщений собеседника
    if cmd == "mute":
        dur = parse_dur(arg) if arg else None
        if arg and dur is None:
            return await bad_usage(message, owner_id, ".mute [30s|5m|2h]")
        st["mute_until"] = time.time() + dur if dur else 0
        touch()
        return await done(message, owner_id, f"🔇 Мут собеседника: {arg if dur else 'навсегда'}")
    if cmd == "unmute":
        st.pop("mute_until", None)
        st["warn_count"] = 0
        touch()
        return await done(message, owner_id, "🔊 Мут снят")
    if cmd == "warn":
        if not arg.isdigit() or int(arg) < 1:
            return await bad_usage(message, owner_id, ".warn [число]")
        st["warn_limit"], st["warn_count"] = int(arg), 0
        touch()
        return await done(message, owner_id, f"⚠️ Warn: собеседник может написать {arg} сообщений")
    if cmd == "unwarn":
        st.pop("warn_limit", None)
        st.pop("warn_count", None)
        touch()
        return await done(message, owner_id, "✅ Warn снят")
    if cmd in TOGGLES:
        st[cmd] = True
        touch()
        return await done(message, owner_id, f"{TOGGLES[cmd]}: включено")
    if cmd.startswith("un") and cmd[2:] in TOGGLES:
        st.pop(cmd[2:], None)
        touch()
        return await done(message, owner_id, f"{TOGGLES[cmd[2:]]}: выключено")
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
        return await done(message, owner_id, f"⏳ Автоудаление всех новых сообщений через {arg}")
    if cmd == "unbchat":
        st.pop("bchat", None)
        touch()
        return await done(message, owner_id, "⏳ Автоудаление выключено")
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
        return await done(message, owner_id, f"🎭 Имитация включена: {arg.lower()}")
    if cmd == "unimit":
        task = imit_tasks.pop(key, None)
        if task:
            task.cancel()
        return await done(message, owner_id, "🎭 Имитация выключена")

    # --- время в фамилии профиля
    if cmd == "time":
        if arg.lower() in ("off", "выкл"):
            cfg = state["time"].pop(conn_id, None)
            touch()
            if cfg:
                await bot.set_business_account_name(
                    conn_id, first_name=cfg["first"], last_name=cfg["last"] or None
                )
            return await done(message, owner_id, "🕒 Время в профиле выключено")
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
        return await done(message, owner_id, f"🕒 Время в профиле включено ({tz})")

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


# ---------------------------------------------------------------- обработчики Telegram
@dp.business_connection()
async def on_connection(conn: BusinessConnection):
    log.info("business_connection: enabled=%s rights=%s", conn.is_enabled, conn.rights)
    state["owners"][conn.id] = conn.user.id
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
    except Exception as e:
        log.exception("ошибка команды")
        await notify(owner_id, f"⚠️ Ошибка команды: {e}")


# /help и /start прямо в чате с самим ботом
@dp.message(F.text.in_({"/help", "/start"}))
async def on_direct(message: Message):
    await message.answer(TEST_TEXT)


@dp.message(F.text == "/commands")
async def on_commands(message: Message):
    await message.answer(HELP)


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
