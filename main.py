"""Пересылка одноразовых кодов из почты (IMAP) в Telegram."""

import email
import html
import imaplib
import logging
import re
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

import requests

import config

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s - %(levelname)s - %(message)s",
)
log = logging.getLogger("email_to_telegram")

_DIGITS = rf"(?<![#\w&])(\d{{{config.CODE_LENGTH}}})(?!\w)"
# Код рядом с ключевым словом: «Ваш код: 123456», «code is 123456».
CODE_NEAR_KEYWORD = (
    re.compile(
        r"(?:" + "|".join(re.escape(k) for k in config.CODE_KEYWORDS) + r")"
        + r"\D{0,100}?" + _DIGITS,
        re.IGNORECASE,
    )
    if config.CODE_KEYWORDS
    else None
)
# Запасной вариант: любое отдельно стоящее число нужной длины,
# но не HTML-цвет (#123456) и не часть более длинного числа или слова.
CODE_ANYWHERE = re.compile(_DIGITS)

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _redact(text):
    """Убирает токен бота из текста ошибок перед записью в лог."""
    return str(text).replace(config.TELEGRAM_BOT_TOKEN, "***")


def _mask(code):
    return code[:2] + "****"


# ---------------------------------------------------------------- Telegram


_http = requests.Session()  # переиспользует TCP/TLS-соединение с Telegram


def send_to_telegram(code, prefix="", attempts=3):
    """Отправляет код в Telegram. Возвращает True при успехе.

    Повторы короткие: если Telegram недоступен, письмо останется
    непрочитанным и будет отправлено в следующем цикле проверки.
    """
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    prefix = f"{html.escape(prefix)}: " if prefix else ""
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        # <code> позволяет скопировать код одним нажатием
        "text": f"{prefix}<code>{code}</code>",
        "parse_mode": "HTML",
    }

    for attempt in range(1, attempts + 1):
        try:
            response = _http.post(
                url, json=payload, timeout=(5, config.HTTP_TIMEOUT)
            )
        except requests.RequestException as exc:
            log.warning("Telegram недоступен (попытка %d/%d): %s",
                        attempt, attempts, _redact(exc))
            if attempt < attempts:
                time.sleep(1)
            continue

        if response.ok:
            return True

        if response.status_code == 429:
            try:
                retry_after = int(response.json()["parameters"]["retry_after"])
            except (ValueError, KeyError, TypeError):
                retry_after = 5
            retry_after = min(retry_after, 15)
            log.warning("Telegram ограничил частоту, ждём %d с", retry_after)
            time.sleep(retry_after)
            continue

        log.error("Telegram вернул %d: %s",
                  response.status_code, _redact(response.text[:300]))
        if 400 <= response.status_code < 500:
            return False  # ошибка в запросе или настройках, повтор не поможет
        if attempt < attempts:
            time.sleep(1)

    return False


# ------------------------------------------------------------ Разбор письма


def _decode_header_value(value):
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def _decode_part(part):
    payload = part.get_payload(decode=True)
    if not payload:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:  # неизвестная кодировка
        return payload.decode("utf-8", errors="replace")


def _html_to_text(raw_html):
    text = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", raw_html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return html.unescape(text)


def get_body_texts(msg):
    """Возвращает (plain, html_как_текст) из всех неприкреплённых частей."""
    plain, html_parts = [], []
    for part in msg.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type()
        if content_type == "text/plain":
            plain.append(_decode_part(part))
        elif content_type == "text/html":
            html_parts.append(_html_to_text(_decode_part(part)))
    return "\n".join(plain), "\n".join(html_parts)


def extract_verification_code(*texts):
    """Ищет код: сначала рядом с ключевым словом, затем любой."""
    texts = [t for t in texts if t]
    patterns = [p for p in (CODE_NEAR_KEYWORD, CODE_ANYWHERE) if p is not None]
    for pattern in patterns:
        for text in texts:
            match = pattern.search(text)
            if match:
                return match.group(1)
    return None


def get_sender(msg):
    return parseaddr(_decode_header_value(msg.get("From")))[1].strip().lower()


_RECIPIENT_HEADERS = ("To", "Cc", "Delivered-To", "X-Original-To", "Envelope-To")


def get_recipients(msg):
    values = []
    for header in _RECIPIENT_HEADERS:
        values.extend(_decode_header_value(v) for v in msg.get_all(header, []))
    return {addr.strip().lower() for _, addr in getaddresses(values) if addr}


def get_email_age(msg):
    """Возраст письма по заголовку Date или None, если даты нет."""
    try:
        sent = parsedate_to_datetime(msg.get("Date"))
    except (TypeError, ValueError, IndexError):
        return None
    if sent is None:
        return None
    if sent.tzinfo is None:
        sent = sent.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - sent


# -------------------------------------------------------------------- IMAP


class AuthError(Exception):
    """Сервер отклонил логин или пароль."""


class MailClient:
    """Держит одно IMAP-соединение и переподключается при обрыве."""

    def __init__(self):
        self.conn = None

    def connect(self):
        log.info("Подключение к %s:%d (%s)",
                 config.EMAIL_SERVER, config.IMAP_PORT, config.IMAP_SECURITY)
        if config.IMAP_SECURITY == "ssl":
            self.conn = imaplib.IMAP4_SSL(
                config.EMAIL_SERVER, config.IMAP_PORT, timeout=config.IMAP_TIMEOUT
            )
        else:
            self.conn = imaplib.IMAP4(
                config.EMAIL_SERVER, config.IMAP_PORT, timeout=config.IMAP_TIMEOUT
            )
            self.conn.starttls()
        try:
            self.conn.login(config.EMAIL, config.PASSWORD)
        except imaplib.IMAP4.error as exc:
            self.close()
            raise AuthError(exc) from exc
        folder = config.IMAP_FOLDER.replace("\\", "\\\\").replace('"', '\\"')
        status, _ = self.conn.select(f'"{folder}"')
        if status != "OK":
            raise imaplib.IMAP4.error(f"Не удалось открыть папку {config.IMAP_FOLDER}")

    def ensure_connected(self):
        if self.conn is not None:
            try:
                status, _ = self.conn.noop()
                if status == "OK":
                    return
            except (imaplib.IMAP4.error, OSError):
                pass
            log.warning("Соединение с почтой потеряно, переподключаемся")
            self.close()
        self.connect()

    def close(self):
        if self.conn is None:
            return
        try:
            self.conn.close()
        except Exception:
            pass
        try:
            self.conn.logout()
        except Exception:
            pass
        self.conn = None

    def search_unseen_from(self, sender, since):
        """UID непрочитанных писем от отправителя начиная с даты."""
        since_str = f"{since.day:02d}-{_MONTHS[since.month - 1]}-{since.year}"
        status, data = self.conn.uid(
            "SEARCH", None, "UNSEEN", "FROM", f'"{sender}"', "SINCE", since_str
        )
        if status != "OK":
            raise imaplib.IMAP4.error(f"Ошибка поиска писем: {data}")
        return data[0].split() if data and data[0] else []

    def fetch_message(self, uid):
        """Загружает письмо, не помечая его прочитанным (BODY.PEEK)."""
        status, data = self.conn.uid("FETCH", uid, "(BODY.PEEK[])")
        if status != "OK":
            raise imaplib.IMAP4.error(f"Ошибка загрузки письма {uid!r}: {data}")
        for item in data:
            if isinstance(item, tuple) and len(item) > 1:
                return email.message_from_bytes(item[1])
        raise imaplib.IMAP4.error(f"Пустой ответ при загрузке письма {uid!r}")

    def mark_seen(self, uid):
        self.conn.uid("STORE", uid, "+FLAGS", "(\\Seen)")


# ---------------------------------------------------------------- Логика


class Processor:
    def __init__(self, client):
        self.client = client
        # Письма, которые уже обработаны или сознательно пропущены,
        # чтобы не скачивать их повторно каждые несколько секунд.
        self.handled = set()

    def run_once(self):
        self.client.ensure_connected()
        # SINCE работает по дням, берём запас в сутки, точный фильтр по Date ниже
        since = datetime.now(timezone.utc) - timedelta(days=1)

        uids = []
        for sender in sorted(config.ALLOWED_SENDERS):
            uids.extend(self.client.search_unseen_from(sender, since))

        log.debug("Проверка: непрочитанных писем от отправителей %d", len(uids))
        for uid in uids:
            if uid in self.handled:
                continue
            try:
                self.process_message(uid)
            except (imaplib.IMAP4.abort, OSError):
                raise  # проблема с соединением, переподключимся в основном цикле
            except Exception:
                log.exception("Не удалось обработать письмо %s, пропускаем", uid)
                self.handled.add(uid)

    def process_message(self, uid):
        msg = self.client.fetch_message(uid)

        # IMAP ищет FROM по подстроке, поэтому проверяем адрес точно
        sender = get_sender(msg)
        if sender not in config.ALLOWED_SENDERS:
            log.debug("Письмо %s от %r не из списка отправителей", uid, sender)
            self.handled.add(uid)
            return

        if config.ALLOWED_RECIPIENTS and not (
            get_recipients(msg) & config.ALLOWED_RECIPIENTS
        ):
            log.debug("Письмо %s адресовано не указанным получателям", uid)
            self.handled.add(uid)
            return

        age = get_email_age(msg)
        max_age = timedelta(minutes=config.MAX_EMAIL_AGE_MINUTES)
        if age is not None and age > max_age:
            log.info("Письмо %s старше %d мин, код уже неактуален, пропускаем",
                     uid, config.MAX_EMAIL_AGE_MINUTES)
            self.handled.add(uid)
            return

        subject = _decode_header_value(msg.get("Subject"))
        plain, html_text = get_body_texts(msg)
        code = extract_verification_code(subject, plain, html_text)
        if not code:
            log.warning("В письме %s от %s код не найден", uid, sender)
            self.handled.add(uid)
            return

        age_text = f"{age.total_seconds():.0f} с" if age is not None else "неизвестно"
        log.info("Найден код %s в письме %s, письмо отправлено %s назад",
                 _mask(code), uid, age_text)
        started = time.monotonic()
        if send_to_telegram(code, config.prefix_for(sender)):
            took = time.monotonic() - started
            log.info("Код отправлен в Telegram за %.1f с", took)
            if took > 5:
                log.warning("Отправка в Telegram заняла %.1f с, проверьте сеть сервера", took)
            self.handled.add(uid)
            self.client.mark_seen(uid)
        else:
            # Письмо остаётся непрочитанным и будет обработано в следующем цикле
            log.error("Не удалось отправить код, повторим позже")


def _handle_sigterm(signum, frame):
    sys.exit(0)


def main():
    signal.signal(signal.SIGTERM, _handle_sigterm)
    log.info("Запуск. Ящик %s, папка %s, отправители: %s",
             config.EMAIL, config.IMAP_FOLDER,
             ", ".join(
                 f"{s} ({config.prefix_for(s) or 'без префикса'})"
                 for s in sorted(config.ALLOWED_SENDERS)
             ))
    if config.ALLOWED_RECIPIENTS:
        log.info("Получатели: %s", ", ".join(sorted(config.ALLOWED_RECIPIENTS)))

    client = MailClient()
    processor = Processor(client)
    auth_backoff = 0
    stats = {"cycles": 0, "errors": 0, "slowest": 0.0}
    last_report = time.monotonic()

    try:
        while True:
            started = time.monotonic()
            delay = None
            try:
                processor.run_once()
                auth_backoff = 0
            except AuthError as exc:
                # Неверный пароль или блокировка входа: частые попытки
                # только продлят блокировку, поэтому пауза растёт до 5 минут
                auth_backoff = min(max(auth_backoff * 2, 30), 300)
                log.error("Сервер отклонил вход: %s. Повтор через %d с", exc, auth_backoff)
                delay = auth_backoff
                stats["errors"] += 1
            except Exception as exc:
                # Обрыв соединения, таймаут и т. п.: переподключаемся быстро,
                # чтобы не пропустить код
                log.error("Ошибка: %s: %s. Переподключение",
                          type(exc).__name__, _redact(exc))
                client.close()
                delay = min(5, config.POLL_INTERVAL)
                stats["errors"] += 1

            elapsed = time.monotonic() - started
            stats["cycles"] += 1
            stats["slowest"] = max(stats["slowest"], elapsed)
            if elapsed > config.POLL_INTERVAL:
                log.warning("Проверка почты заняла %.1f с, дольше интервала %d с",
                            elapsed, config.POLL_INTERVAL)

            if time.monotonic() - last_report >= 900:
                log.info("Работаю: проверок %d, ошибок %d, самая долгая %.1f с",
                         stats["cycles"], stats["errors"], stats["slowest"])
                stats = {"cycles": 0, "errors": 0, "slowest": 0.0}
                last_report = time.monotonic()

            if delay is None:
                # Проверки начинаются каждые POLL_INTERVAL секунд
                delay = max(0.0, config.POLL_INTERVAL - elapsed)
            time.sleep(delay)
    except (KeyboardInterrupt, SystemExit):
        log.info("Остановка")
    finally:
        client.close()


if __name__ == "__main__":
    main()
