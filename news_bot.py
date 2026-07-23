#!/usr/bin/env python3
"""Лёгкий дайджест ИТ-новостей из трёх источников.

Формат каждой новости: заголовок + одна фраза сути + ссылка на источник.
Отправляет в тот же Telegram-канал через того же бота, что и раньше.
Помнит уже отправленное (seen.json), поэтому в канал попадают только новые новости.
"""
import argparse
import html
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from time import mktime
from typing import List, Dict, Optional

import feedparser
import requests
from bs4 import BeautifulSoup


# --- Источники --------------------------------------------------------------
FEEDS: List[Dict[str, str]] = [
    {"name": "CNews", "url": "https://www.cnews.ru/inc/rss/news.xml"},
    {"name": "TAdviser", "url": "https://www.tadviser.ru/xml/tadviser.xml"},
    {"name": "НовостиИТ-канала", "url": "https://www.novostiitkanala.ru/rss"},
]

STATE_FILE = "seen.json"          # список уже отправленных новостей (коммитится экшеном)
SEEN_KEEP = 1500                  # сколько id хранить в памяти, чтобы файл не рос бесконечно
RECENCY_DAYS = 3                  # игнорируем новости старше N дней (защита от потопа)
MAX_ITEMS_PER_RUN = 60            # предохранитель на один запуск
TG_LIMIT = 3800                   # безопасный лимит длины одного сообщения Telegram (макс. 4096)
PHRASE_MAX = 240                  # максимальная длина «одной фразы»
MSK = timezone(timedelta(hours=3))

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"


# --- Загрузка (requests, при сбое — curl) -----------------------------------
def fetch_url(url: str, timeout: int = 20) -> Optional[bytes]:
    """Возвращает содержимое URL. Сначала requests, при ошибке — системный curl.

    Фолбэк на curl нужен на локальном macOS: системный Python собран со старым
    LibreSSL и не может открыть некоторые сайты (TAdviser). В CI работает requests.
    """
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
        resp.raise_for_status()
        return resp.content
    except Exception as exc:
        print(f"  requests не смог ({url}): {exc}; пробую curl", file=sys.stderr)
    try:
        out = subprocess.run(
            ["curl", "-sSL", "--max-time", str(timeout), "-A", USER_AGENT, url],
            capture_output=True, timeout=timeout + 5,
        )
        if out.returncode == 0 and out.stdout:
            return out.stdout
        print(f"  curl не смог ({url}): rc={out.returncode}", file=sys.stderr)
    except Exception as exc:
        print(f"  curl упал ({url}): {exc}", file=sys.stderr)
    return None


# --- Текстовые помощники -----------------------------------------------------
def clean_text(text: Optional[str]) -> str:
    if not text:
        return ""
    plain = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    plain = html.unescape(plain)
    return re.sub(r"\s+", " ", plain).strip()


def first_sentence(text: str, max_len: int = PHRASE_MAX) -> str:
    """Первое предложение — «одна фраза сути»."""
    text = clean_text(text)
    if not text:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", text)
    phrase = parts[0].strip() if parts else text
    # если первое «предложение» подозрительно короткое (обрезалось на инициале и т.п.) —
    # добавляем следующее
    if len(phrase) < 30 and len(parts) > 1:
        phrase = (phrase + " " + parts[1]).strip()
    if len(phrase) > max_len:
        phrase = phrase[: max_len - 1].rstrip() + "…"
    return phrase


# --- Модель новости ----------------------------------------------------------
def entry_id(entry, link: str) -> str:
    return (getattr(entry, "id", "") or getattr(entry, "guid", "") or link).strip()


def entry_timestamp(entry) -> int:
    for attr in ("published_parsed", "updated_parsed"):
        value = getattr(entry, attr, None)
        if value:
            try:
                return int(mktime(value))
            except Exception:
                continue
    return 0


JUNK_PHRASE_PREFIXES = ("основная статья", "см.", "см ", "смотрите также", "содержание")


def _norm(text: str) -> str:
    return re.sub(r"\W+", "", text.lower())


def make_phrase(entry, title: str) -> str:
    """Одна фраза сути — из описания RSS. Если описания нет или оно просто
    повторяет заголовок, фразу не показываем (заголовок говорит сам за себя)."""
    desc = getattr(entry, "summary", "") or getattr(entry, "description", "")
    phrase = first_sentence(desc)
    if len(phrase) < 20:
        return ""
    low = phrase.lower()
    if any(low.startswith(prefix) for prefix in JUNK_PHRASE_PREFIXES):
        return ""  # вики-ссылки TAdviser вроде «Основная статья: …»
    if _norm(phrase) == _norm(title) or _norm(phrase) in _norm(title):
        return ""
    return phrase


def should_include(item: Dict) -> bool:
    """Точка расширения для будущего фильтра.

    Сейчас пропускаем все новые новости. Когда понадобится отсекать по темам/
    ключевым словам — вся логика фильтра живёт здесь (одно место).
    """
    return True


def fetch_feed(feed: Dict[str, str]) -> List[Dict]:
    content = fetch_url(feed["url"])
    if not content:
        print(f"Пропускаю {feed['name']}: не удалось загрузить ленту", file=sys.stderr)
        return []
    parsed = feedparser.parse(content)
    items: List[Dict] = []
    for entry in parsed.entries:
        title = clean_text(getattr(entry, "title", ""))
        if not title:
            continue
        link = (getattr(entry, "link", "") or feed["url"]).strip()
        items.append(
            {
                "id": entry_id(entry, link),
                "title": title,
                "link": link,
                "source": feed["name"],
                "ts": entry_timestamp(entry),
                "_entry": entry,  # временно, для ленивого расчёта фразы
            }
        )
    return items


# --- Память отправленного ----------------------------------------------------
def load_seen(path: str = STATE_FILE) -> List[str]:
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return list(data.get("seen", []))
    except Exception as exc:
        print(f"Не смог прочитать {path}: {exc}", file=sys.stderr)
        return []


def save_seen(seen: List[str], path: str = STATE_FILE) -> None:
    seen = seen[-SEEN_KEEP:]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"seen": seen}, fh, ensure_ascii=False, indent=0)


def collect_new(seen_ids: List[str], limit: int = MAX_ITEMS_PER_RUN) -> List[Dict]:
    seen = set(seen_ids)
    now = time.time()
    window = RECENCY_DAYS * 24 * 3600

    raw: List[Dict] = []
    for feed in FEEDS:
        try:
            raw.extend(fetch_feed(feed))
        except Exception as exc:
            print(f"Пропускаю {feed['name']}: {exc}", file=sys.stderr)

    fresh: List[Dict] = []
    seen_now = set()
    for item in sorted(raw, key=lambda x: x["ts"], reverse=True):
        if item["id"] in seen or item["id"] in seen_now:
            continue
        if item["ts"] and (now - item["ts"] > window):
            continue
        if not should_include(item):
            continue
        seen_now.add(item["id"])
        fresh.append(item)

    return fresh[:limit]


# --- Форматирование и отправка ----------------------------------------------
def part_of_day() -> str:
    hour = datetime.now(MSK).hour
    return "утро" if 4 <= hour < 15 else "вечер"


def format_item(item: Dict) -> str:
    title = html.escape(item["title"])
    phrase = html.escape(make_phrase(item["_entry"], item["title"]))
    src = html.escape(item["source"])
    link = html.escape(item["link"], quote=True)
    block = f"🔹 <b>{title}</b>"
    if phrase:
        block += f"\n{phrase}"
    block += f'\n<a href="{link}">{src} →</a>'
    return block


def pack_messages(items: List[Dict]) -> List[str]:
    now = datetime.now(MSK).strftime("%d.%m")
    header = f"📰 <b>Дайджест ИТ-новостей</b> · {part_of_day()} · {now}"

    messages: List[str] = []
    current = header
    for item in items:
        block = format_item(item)
        if len(current) + len(block) + 2 > TG_LIMIT:
            messages.append(current)
            current = block
        else:
            current += "\n\n" + block
    if current.strip():
        messages.append(current)
    return messages


def send_telegram_message(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    resp = requests.post(url, data=payload, timeout=30)
    resp.raise_for_status()


# --- Окружение и запуск ------------------------------------------------------
def load_env_file(filepath: str = ".env") -> None:
    if not os.path.isfile(filepath):
        return
    with open(filepath, "r", encoding="utf-8") as env_file:
        for line in env_file:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and value and key not in os.environ:
                os.environ[key] = value


def parse_args() -> argparse.Namespace:
    load_env_file()
    parser = argparse.ArgumentParser(description="Дайджест ИТ-новостей из трёх источников")
    parser.add_argument("--dry-run", action="store_true", help="Показать дайджест, не отправляя в Telegram")
    parser.add_argument("--seed", action="store_true", help="Отметить все текущие новости как отправленные (без отправки) — запустить один раз при старте")
    parser.add_argument("--limit", type=int, default=MAX_ITEMS_PER_RUN, help="Максимум новостей за запуск")
    parser.add_argument("--token", default=os.getenv("TELEGRAM_BOT_TOKEN", ""), help="Telegram bot token")
    parser.add_argument("--chat-id", default=os.getenv("TELEGRAM_CHAT_ID", ""), help="Telegram chat id")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    seen_ids = load_seen()

    if args.seed:
        ids: List[str] = list(seen_ids)
        for feed in FEEDS:
            for item in fetch_feed(feed):
                if item["id"] not in ids:
                    ids.append(item["id"])
        save_seen(ids)
        print(f"Готово: отмечено как отправленное {len(ids)} новостей. Теперь бот будет слать только новые.")
        return 0

    new_items = collect_new(seen_ids, limit=args.limit)
    print(f"Новых новостей: {len(new_items)}")

    if not new_items:
        print("Новых новостей нет — ничего не отправляю.")
        return 0

    messages = pack_messages(new_items)

    for msg in messages:
        print("\n" + "=" * 60)
        print(msg)

    if args.dry_run:
        return 0

    if not args.token or not args.chat_id:
        print("TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID должны быть заданы для отправки.", file=sys.stderr)
        return 2

    for msg in messages:
        send_telegram_message(args.token, args.chat_id, msg)
        time.sleep(1)

    # запоминаем отправленное только после успешной отправки
    save_seen(seen_ids + [item["id"] for item in new_items])
    print(f"Отправлено сообщений: {len(messages)}, новостей: {len(new_items)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
