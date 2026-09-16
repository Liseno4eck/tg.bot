Telegram Business бот

Этот бот предназначен только для подключения через Telegram Business / «Автоматизация чатов».

После подключения бот получает событие BusinessConnection и отправляет владельцу:
«бот успешно подключен»

Обычных команд (/start и других) в коде нет.

Установка:
1. Установить Python 3.11+.
2. Выполнить:
   pip install -r requirements.txt
3. Создать файл .env на основе .env.example:
   BOT_TOKEN=токен_бота
4. Запустить:
   python bot.py

В BotFather для бота должен быть включён режим Telegram Business/Business Mode, иначе Telegram не позволит подключить бота к бизнес-аккаунту.

Официальная документация Telegram:
https://core.telegram.org/bots/features
