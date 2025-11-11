import os
import re
import threading
import asyncio
from io import BytesIO
from urllib.parse import urlparse

from flask import Flask, request, abort

import tldextract
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

# ----------------------------
# ENV
# ----------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "")  # Render sets this automatically
WEB_BASE_URL = RENDER_URL or os.environ.get("WEB_BASE_URL", "")

if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN env var")

# ----------------------------
# Flask
# ----------------------------
app = Flask(__name__)

# ----------------------------
# PTB v21 Application (async)
# ----------------------------
application = Application.builder().token(BOT_TOKEN).build()
_loop = asyncio.new_event_loop()

# ----------------------------
# URL helpers
# ----------------------------
URL_REGEX = re.compile(
    r'(?:(?:https?://)|(?:www\.))?(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?::\d{2,5})?(?:/[^\s]*)?',
    re.IGNORECASE
)

def normalize_input_url(u: str) -> str:
    """Ensure https:// exists (leave path/query intact)."""
    u = (u or "").strip()
    if not u:
        return ""
    if not re.match(r'^[a-zA-Z][a-zA-Z0-9+\-.]*://', u):
        u = "https://" + u
    return u

def to_apex_site(u: str) -> str:
    """Return https://<registrable-domain> for any URL or bare domain."""
    u = normalize_input_url(u)
    p = urlparse(u)
    host = p.hostname or ""
    # Keep IP/localhost as-is
    if re.match(r"^(\d{1,3}\.){3}\d{1,3}$", host) or host == "localhost":
        site = host
    else:
        ext = tldextract.extract(host)  # (subdomain, domain, suffix)
        site = f"{ext.domain}.{ext.suffix}" if (ext.domain and ext.suffix) else host
    return f"https://{site}"

def extract_urls(text: str):
    """Find URL-like strings and de-dup preserving order."""
    matches = URL_REGEX.findall(text or "")
    seen, out = set(), []
    for m in matches:
        s = m.strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out

def clean_sites(text: str):
    """
    Returns a de-duplicated list of apex sites as https://<domain>.
    Example output lines:
      https://example.com
      https://amazon.co.uk
    """
    sites, seen = [], set()
    for u in extract_urls(text):
        site = to_apex_site(u)
        if site and site not in seen:
            seen.add(site)
            sites.append(site)
    return sites

# ----------------------------
# Handlers (async)
# ----------------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Welcome to the Site Cleaner Bot!*\n\n"
        "Send me:\n"
        "• Any text containing URLs\n"
        "• Or upload a `.txt` file with URLs\n\n"
        "I will:\n"
        "✅ Add `https://` if missing\n"
        "✅ Trim each link to the clean site (apex domain)\n"
        "✅ Remove duplicates\n"
        "✅ Return a `urls.txt` with *one clean site per line*.\n\n"
        "*Example:*\n"
        "`https://zero936.com/abc` → `https://zero936.com`",
        parse_mode=ParseMode.MARKDOWN
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *How to Use*\n\n"
        "1) Send text with links OR upload a `.txt` file (≤10MB)\n"
        "2) I will:\n"
        "   • Normalize with `https://` if missing\n"
        "   • Collapse to the apex domain\n"
        "   • De-duplicate\n"
        "   • Reply with `urls.txt` (one domain per line)\n\n"
        "*Example output:*\n"
        "`https://zero936.com`",
        parse_mode=ParseMode.MARKDOWN
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sites = clean_sites(update.message.text or "")
    if not sites:
        await update.message.reply_text("No site URLs found.")
        return

    buf = BytesIO(("\n".join(sites) + "\n").encode("utf-8"))
    buf.seek(0)
    await update.message.reply_document(
        document=buf, filename="urls.txt",
        caption=f"✅ Extracted {len(sites)} clean site(s)."
    )

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc:
        await update.message.reply_text("Please upload a `.txt` file.", parse_mode=ParseMode.MARKDOWN)
        return

    is_txt_mime = (doc.mime_type or "").lower() == "text/plain"
    is_txt_name = (doc.file_name or "").lower().endswith(".txt")
    if not (is_txt_mime or is_txt_name):
        await update.message.reply_text("Unsupported file type. Please upload a `.txt` file.", parse_mode=ParseMode.MARKDOWN)
        return

    if doc.file_size and doc.file_size > 10 * 1024 * 1024:
        await update.message.reply_text("File too large. Please upload a .txt under 10 MB.")
        return

    try:
        file = await context.bot.get_file(doc.file_id)
        data = await file.download_as_bytearray()
        content = data.decode("utf-8", errors="ignore")
    except Exception:
        await update.message.reply_text("Couldn't read that file. Please try again.")
        return

    sites = clean_sites(content)
    if not sites:
        await update.message.reply_text("No site URLs found in your file.")
        return

    buf = BytesIO(("\n".join(sites) + "\n").encode("utf-8"))
    buf.seek(0)
    await update.message.reply_document(
        document=buf, filename="urls.txt",
        caption=f"✅ Extracted {len(sites)} clean site(s) from your file."
    )

# Register handlers
application.add_handler(CommandHandler("start", start_cmd))
application.add_handler(CommandHandler("help", help_cmd))
application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

# ----------------------------
# Flask routes
# ----------------------------
@app.route("/", methods=["GET"])
def health():
    return "OK", 200

@app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
def telegram_webhook():
    update_json = request.get_json(force=True, silent=True)
    if not update_json:
        abort(400)
    update = Update.de_json(update_json, application.bot)
    asyncio.run_coroutine_threadsafe(application.process_update(update), _loop)
    return "OK", 200

# ----------------------------
# Start PTB event loop thread + auto set webhook
# ----------------------------
def _run_bot():
    asyncio.set_event_loop(_loop)
    _loop.run_until_complete(application.initialize())
    _loop.run_until_complete(application.start())
    if WEB_BASE_URL:
        webhook_url = f"{WEB_BASE_URL}/webhook/{BOT_TOKEN}"
        async def _set_hook():
            try:
                await application.bot.delete_webhook(drop_pending_updates=True)
                await application.bot.set_webhook(url=webhook_url, drop_pending_updates=True)
                print("✅ Webhook set to:", webhook_url, flush=True)
            except Exception as e:
                print("❌ Failed to set webhook:", e, flush=True)
        _loop.run_until_complete(_set_hook())
    _loop.run_forever()

threading.Thread(target=_run_bot, daemon=True).start()

# ----------------------------
# Local run
# ----------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
