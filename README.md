# Site Cleaner Bot ❄️

A Telegram bot for cleaning, merging, splitting URL lists and extracting/scraping credit cards.

## Features

- **/clean** — Reply to a `.txt` file to clean & deduplicate URLs (Apex or Host mode)
- **/cclean** — Reply to a `.txt` file to extract valid CCs (`CC|MM|YY|CVV` format)
- **/merge** — Combine multiple `.txt` files into one deduplicated file
- **/split** — Split a `.txt` file into chunks by line count
- **/scr** — Scrape CCs from Telegram groups/channels using a userbot
- **/mode** — Switch between Apex (root domain) and Host (keep subdomain) modes

## Scraper Features

- Supports invite links, message links, usernames, and numeric IDs
- Auto-joins groups via invite link if not already a member
- Live progress updates with cancel button
- Luhn validation, expiry check, brand detection (Visa, Mastercard, Amex, Discover, JCB, etc.)

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | ✅ | Telegram bot token from BotFather |
| `WEB_BASE_URL` | ✅ | Public URL for webhook (e.g. Cloudflare tunnel) |
| `PORT` | ❌ | Port to run on (default: `8000`) |
| `SCRAPER_API_ID` | ❌ | Telegram API ID (for scraper) |
| `SCRAPER_API_HASH` | ❌ | Telegram API Hash (for scraper) |
| `SCRAPER_SESSION` | ❌ | Pyrogram session string (for scraper) |
| `ADMIN_IDS` | ❌ | Comma-separated Telegram user IDs for admin-only commands |

## Deploy

```bash
pip install -r requirements.txt

BOT_TOKEN="your_token" \
WEB_BASE_URL="https://your-url.com" \
SCRAPER_API_ID=12345 \
SCRAPER_API_HASH="your_hash" \
SCRAPER_SESSION="your_session" \
ADMIN_IDS="123456789" \
python app.py
```

## Owner

[@ZEUS_IS_HERE2](https://t.me/ZEUS_IS_HERE2)
