#!/bin/zsh
set -e
cd /Users/alexandr/telegram-news-bot

if [ -z "$1" ]; then
  echo "Usage: ./deploy.sh <github-repo-url>"
  echo "Example: ./deploy.sh https://github.com/yourname/news-bot.git"
  exit 1
fi

git init

git add .
git commit -m "Prepare autonomous Telegram news bot" 2>/dev/null || true

git branch -M main
git remote add origin "$1" 2>/dev/null || git remote set-url origin "$1"
git push -u origin main
