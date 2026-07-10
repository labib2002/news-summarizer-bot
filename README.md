# News Summarizer Bot

A hands-off daily news briefing: a GitHub Actions cron job fetches a curated
set of tech/AI RSS feeds every morning, deduplicates the articles in SQLite,
asks Gemini to write a structured summary, and delivers it to a Telegram chat.

## How it works

```
GitHub Actions (daily cron, 07:00 UTC)
│
├─ 1. gather_news.py
│     • reads the committed feed list (feeds.ini)
│     • fetches all feeds concurrently (httpx + asyncio, 60s timeout,
│       browser user-agent, follows redirects)
│     • parses entries with feedparser
│     • inserts into SQLite (news_articles.db); the article URL is the
│       PRIMARY KEY, so re-runs and cross-feed duplicates are dropped
│
└─ 2. summarize_and_send.py
      • loads all stored articles grouped by category
      • builds a structured prompt and calls Gemini (gemini-3.5-flash,
        falling back to gemini-2.5-flash if the model id is unavailable)
      • sanitizes the result for Telegram MarkdownV2 (with 4096-char
        message splitting)
      • sends the briefing via the Telegram Bot API
      • clears the database only after a successful send, so a failed
        delivery is retried with the same articles next run
```

## Configuration

Feeds and secrets are deliberately kept apart:

| File / source | Committed? | Contains |
|---|---|---|
| `feeds.ini` | yes | RSS feed list only — no secrets |
| GitHub Actions secrets | n/a | `GEMINI_API_KEY`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` |
| `config.ini` (optional, gitignored) | no | local overrides: secrets and/or extra feeds for development |

### Feed list (`feeds.ini`)

Every section named `[feeds_<category>]` is a feed group; the suffix becomes
the category heading in the briefing (`feeds_tech_news` → "Tech News"):

```ini
[feeds_tech_news]
hacker_news = https://news.ycombinator.com/rss
ars_technica = https://feeds.arstechnica.com/arstechnica/index
```

The committed list currently holds 11 verified tech/AI feeds (Hacker News,
Ars Technica, The Verge, TechCrunch, The Register, Wired, IEEE Spectrum,
MIT Technology Review AI, Google AI Blog, VentureBeat AI, Simon Willison).

## Setup

1. **Fork/clone** the repo and enable GitHub Actions.
2. **Create a Telegram bot** with [@BotFather](https://t.me/BotFather), grab
   the bot token, then get your chat id (e.g. message the bot and read
   `https://api.telegram.org/bot<TOKEN>/getUpdates`).
3. **Get a Gemini API key** from [Google AI Studio](https://aistudio.google.com/).
4. **Add repository secrets** (Settings → Secrets and variables → Actions):
   - `GEMINI_API_KEY`
   - `TELEGRAM_TOKEN`
   - `TELEGRAM_CHAT_ID`
5. Adjust `feeds.ini` and the cron schedule in
   `.github/workflows/daily_briefing.yml` to taste.

That's it — the workflow also has a `workflow_dispatch` trigger so you can
fire a test run from the Actions tab.

### Running locally

```bash
python -m venv venv
venv/Scripts/activate        # Windows (or: source venv/bin/activate)
pip install -r requirements.txt

# Stage 1 needs no secrets — it just fills news_articles.db:
python gather_news.py

# Stage 2 needs the secrets, via env vars…
set GEMINI_API_KEY=... TELEGRAM_TOKEN=... TELEGRAM_CHAT_ID=...
python summarize_and_send.py
```

…or via a local, gitignored `config.ini`:

```ini
[telegram]
token = 123456:ABC...
chat_id = 123456789

[gemini]
api_key = AIza...

[database]
path = news_articles.db
```

Environment variables take precedence over `config.ini`.

## Limitations

- **Delivery requires secrets.** Without a Gemini API key and a Telegram bot
  token/chat id, only the gather stage works; the summarize/send stage exits
  with a clear error. There is no keyless demo mode.
- Summary quality is whatever Gemini produces from feed titles/summaries
  (article bodies are not scraped); the prompt enforces structure, not facts.
- Uses the deprecated-but-functional `google-generativeai` SDK; a future
  cleanup would migrate to `google-genai`.

## Stack

Python 3.11 · httpx + asyncio · feedparser · SQLite · google-generativeai
(gemini-3.5-flash → gemini-2.5-flash fallback) · python-telegram-bot ·
GitHub Actions cron
