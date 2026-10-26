"""Конфигурация приложения.

Все параметры читаются из переменных окружения. Для локального запуска
их можно положить в файл .env (см. .env.example).
"""

import os

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv необязателен, если переменные заданы иначе
    pass


def _required(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Не задана переменная окружения {name}. См. .env.example")
    return value


def _int(name, default, minimum=1):
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SystemExit(f"Переменная {name} должна быть целым числом, получено: {raw!r}")
    if value < minimum:
        raise SystemExit(f"Переменная {name} должна быть не меньше {minimum}")
    return value


def _list(name, default="", lower=True):
    raw = os.getenv(name, default)
    items = [s.strip() for s in raw.split(",") if s.strip()]
    return [s.lower() for s in items] if lower else items


# --- Почтовый сервер
EMAIL_SERVER = _required("EMAIL_SERVER")
IMAP_SECURITY = os.getenv("IMAP_SECURITY", "ssl").strip().lower()
if IMAP_SECURITY not in ("ssl", "starttls"):
    raise SystemExit("IMAP_SECURITY должна быть ssl или starttls")
IMAP_PORT = _int("IMAP_PORT", 993 if IMAP_SECURITY == "ssl" else 143)
IMAP_FOLDER = os.getenv("IMAP_FOLDER", "INBOX").strip() or "INBOX"

# --- Учётная запись
EMAIL = _required("EMAIL")
PASSWORD = _required("PASSWORD")

# --- Какие письма обрабатывать
# Адреса отправителей писем с кодами, через запятую.
# У каждого адреса может быть свой префикс: адрес=Префикс.
# Пример: support@kino.watch=Kinopub,noreply@bank.ru=Банк
def _parse_senders(name):
    senders, prefixes = set(), {}
    for item in _list(name, lower=False):
        address, has_prefix, prefix = item.partition("=")
        address = address.strip().lower()
        if not address:
            raise SystemExit(f"В {name} пустой адрес в элементе {item!r}")
        senders.add(address)
        if has_prefix:
            prefixes[address] = prefix.strip()
    return frozenset(senders), prefixes


ALLOWED_SENDERS, SENDER_PREFIXES = _parse_senders("ALLOWED_SENDERS")
if not ALLOWED_SENDERS:
    raise SystemExit("Не задана переменная окружения ALLOWED_SENDERS. См. .env.example")
# Необязательно: адреса получателей (например, алиасы ящика), через запятую.
# Если пусто, получатель не проверяется.
ALLOWED_RECIPIENTS = frozenset(_list("ALLOWED_RECIPIENTS"))

# --- Поиск кода
CODE_LENGTH = _int("CODE_LENGTH", 6, minimum=3)
CODE_KEYWORDS = _list("CODE_KEYWORDS", "код,code,пароль,password,otp")

# --- Telegram
TELEGRAM_BOT_TOKEN = _required("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _required("TELEGRAM_CHAT_ID")
# Префикс по умолчанию для отправителей, у которых в ALLOWED_SENDERS
# не указан свой. Пусто — только код.
MESSAGE_PREFIX = os.getenv("MESSAGE_PREFIX", "").strip()


def prefix_for(sender):
    """Префикс сообщения для отправителя: свой или по умолчанию."""
    return SENDER_PREFIXES.get(sender, MESSAGE_PREFIX)

# --- Интервалы и таймауты
POLL_INTERVAL = _int("POLL_INTERVAL", 10)
MAX_EMAIL_AGE_MINUTES = _int("MAX_EMAIL_AGE_MINUTES", 15)
IMAP_TIMEOUT = _int("IMAP_TIMEOUT", 30)
HTTP_TIMEOUT = _int("HTTP_TIMEOUT", 10)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper()
