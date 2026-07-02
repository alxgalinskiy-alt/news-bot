#!/bin/zsh
set -e
cd /Users/alexandr/telegram-news-bot

if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

source .venv/bin/activate 2>/dev/null || true
python3 news_bot.py --limit 8
