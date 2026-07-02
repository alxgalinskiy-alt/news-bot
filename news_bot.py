#!/usr/bin/env python3
import argparse
import os
import re
import sys
from datetime import datetime, timezone
from typing import List, Dict, Optional

import feedparser
import requests
from bs4 import BeautifulSoup


FEEDS: List[Dict[str, str]] = [
    {"name": "Reuters Business", "url": "https://feeds.feedburner.com/Reuters/businessNews", "category": "Бизнес"},
    {"name": "TechCrunch", "url": "https://techcrunch.com/feed/", "category": "ИТ"},
    {"name": "Hacker News", "url": "https://hnrss.org/frontpage", "category": "ИТ"},
    {"name": "Search Engine Land", "url": "https://searchengineland.com/feed", "category": "Маркетинг"},
    {"name": "Marketing Brew", "url": "https://www.marketingbrew.com/rss.xml", "category": "Маркетинг"},
    {"name": "CNews", "url": "https://www.cnews.ru/inc/rss/news.xml", "category": "РФ ИТ"},
    {"name": "TAdviser", "url": "https://www.tadviser.ru/rss.xml", "category": "РФ ИТ"},
    {"name": "РБК", "url": "https://www.rbc.ru/rss/v2/asia/?utm_source=rss", "category": "РФ Бизнес"},
]

SAMPLE_ITEMS: List[Dict[str, str]] = [
    {
        "title": "Microsoft и OpenAI усиливают давление на рынок корпоративного ИИ",
        "summary": "Крупные вендоры расширяют корпоративные предложения и меняют правила закупок для предприятий.",
        "link": "https://example.com/ai-enterprise",
        "category": "ИТ",
    },
    {
        "title": "Новые правила регулирования данных влияют на модели монетизации SaaS",
        "summary": "Изменения в регулировании подталкивают компании к пересмотру тарифов и архитектуры данных.",
        "link": "https://example.com/regulation-saas",
        "category": "Бизнес",
    },
    {
        "title": "Маркетинг в ИТ снова выходит на короткие форматы и AI-ассистентов",
        "summary": "Команды используют генеративный ИИ для контента и персонализации в реальном времени.",
        "link": "https://example.com/it-marketing",
        "category": "Маркетинг",
    },
]


def strip_tags(text: Optional[str]) -> str:
    if not text:
        return ""
    plain = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", plain).strip()


def clean_title(title: Optional[str]) -> str:
    title = strip_tags(title)
    title = re.sub(r"\s+", " ", title).strip()
    return title


def make_relevance_note(item: Dict[str, str]) -> str:
    category = item.get("category", "")
    if "Маркетинг" in category:
        return "Это влияет на каналы привлечения, контент-стратегию и расходы на рост."
    if "ИТ" in category or "РФ ИТ" in category:
        return "Это влияет на инфраструктуру, закупки, скорость развития продуктов и конкуренцию."
    if "Бизнес" in category or "РФ Бизнес" in category:
        return "Это влияет на расходы, регулирование и устойчивость бизнеса."
    return "Это может изменить приоритеты, риски и планы развития в ближайшие месяцы."


def make_thesis(item: Dict[str, str]) -> str:
    title = clean_title(item.get("title"))
    summary = clean_title(item.get("summary"))

    if summary:
        thesis = summary
    else:
        thesis = title

    if len(thesis) > 220:
        thesis = thesis[:217].rstrip() + "..."

    return f"Ключевая мысль: {thesis}\nПочему это важно: {make_relevance_note(item)}\nИсточник: {item.get('link', '')}"


def fetch_feed_items(feed: Dict[str, str], limit: int = 5) -> List[Dict[str, str]]:
    parsed = feedparser.parse(feed["url"])
    items: List[Dict[str, str]] = []

    for entry in parsed.entries[:limit]:
        title = clean_title(getattr(entry, "title", ""))
        summary = clean_title(getattr(entry, "summary", "") or getattr(entry, "description", ""))
        link = getattr(entry, "link", "") or feed["url"]

        if not title:
            continue

        items.append(
            {
                "title": title,
                "summary": summary,
                "link": link,
                "category": feed["category"],
            }
        )

    return items


def collect_news(limit: int = 12) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    for feed in FEEDS:
        try:
            items.extend(fetch_feed_items(feed, limit=3))
        except Exception as exc:
            print(f"Skipping {feed['name']}: {exc}", file=sys.stderr)

    seen = set()
    unique: List[Dict[str, str]] = []
    for item in sorted(items, key=lambda x: x.get("title", "").lower()):
        key = (item.get("title", "").lower(), item.get("link", ""))
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)

    return unique[:limit]


def build_digest(items: List[Dict[str, str]]) -> str:
    today = datetime.now(timezone.utc).strftime("%d.%m.%Y")
    lines = [f"📰 Дайджест новостей за {today}", ""]
    for item in items:
        title = clean_title(item.get("title"))
        lines.append(title)
        lines.append(make_thesis(item))
        lines.append("")
    return "\n".join(lines).strip()


def send_telegram_message(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    response = requests.post(url, data=payload, timeout=30)
    response.raise_for_status()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Daily news digest bot")
    parser.add_argument("--limit", type=int, default=10, help="How many items to include")
    parser.add_argument("--dry-run", action="store_true", help="Print digest without sending to Telegram")
    parser.add_argument("--token", default=os.getenv("TELEGRAM_BOT_TOKEN", ""), help="Telegram bot token")
    parser.add_argument("--chat-id", default=os.getenv("TELEGRAM_CHAT_ID", ""), help="Telegram chat id")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.dry_run:
        items = SAMPLE_ITEMS
    else:
        items = collect_news(limit=args.limit)

    if not items:
        print("No news items found.", file=sys.stderr)
        return 1

    digest = build_digest(items)
    print(digest)

    if args.dry_run:
        return 0

    if not args.token or not args.chat_id:
        print("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set to send messages.", file=sys.stderr)
        return 2

    send_telegram_message(args.token, args.chat_id, digest)
    print("Sent digest to Telegram.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
