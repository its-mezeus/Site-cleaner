import os
import re
import threading
import asyncio
from io import BytesIO
from urllib.parse import urlparse

from flask import Flask, request, abort

import tldextract
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
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
# Modes
# ----------------------------
DEFAULT_MODE = "apex"  # "apex" collapses to registrable; "host" keeps subdomains

def _get_mode(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> str:
    modes = context.application.bot_data.setdefault("modes", {})
    return modes.get(chat_id, DEFAULT_MODE)

def _set_mode(context: ContextTypes.DEFAULT_TYPE, chat_id: int, mode: str):
    modes = context.application.bot_data.setdefault("modes", {})
    modes[chat_id] = mode

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

def to_host_site(u: str) -> str:
    """Return https://<full-hostname> (keeps subdomains) and strips path/query."""
    u = normalize_input_url(u)
    p = urlparse(u)
    host = p.hostname or ""
    if not host:
        return ""
    return f"https://{host}"

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

def clean_sites(text: str, mode: str):
    """
    Returns a de-duplicated list of sites as https://<domain>.
    mode = "apex" (registrable) or "host" (keep subdomain).
    """
    sites, seen = [], set()
    for u in extract_urls(text):
        site = to_apex_site(u) if mode == "apex" else to_host_site(u)
        if site and site not in seen:
            seen.add(site)
            sites.append(site)
    return sites

# ----------------------------
# Keyboards
# ----------------------------
def settings_keyboard(curr_mode: str) -> InlineKeyboardMarkup:
    is_apex = (curr_mode == "apex")
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(("✅ Apex (current)" if is_apex else "Apex"), callback_data="mode:apex"),
            InlineKeyboardButton(("✅ Host (current)" if not is_apex else "Host"), callback_data="mode:host"),
        ]
    ])

# ----------------------------
# Handlers (async)
# ----------------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    mode = _get_mode(context, chat_id)
    await update.message.reply_text(
        "👋 *Welcome to the Site Cleaner Bot!*\n\n"
        "Send me:\n"
        "• Any text containing URLs\n"
        "• Or upload a `.txt` file with URLs\n\n"
        "I will:\n"
        "✅ Add `https://` if missing\n"
        f"✅ Clean links to *{'apex domain' if mode=='apex' else 'full host (with subdomain)'}*\n"
        "✅ Remove duplicates\n"
        "✅ Return a `urls.txt` with one site per line\n\n"
        f"Current mode: *{mode.upper()}*\n"
        "Change it anytime with /mode or /settings",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=settings_keyboard(mode)
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    mode = _get_mode(context, chat_id)
    await update.message.reply_text(
        "📖 *How to Use*\n\n"
        "1) Send text with links OR upload a `.txt` file (≤10MB)\n"
        "2) I will normalize with `https://` and clean each link to a site\n"
        "3) Output is a neat `urls.txt` (one per line)\n\n"
        "*Modes:*\n"
        "• `apex` → `https://shop.amazon.co.uk/abc` → `https://amazon.co.uk`\n"
        "• `host` → `https://shop.amazon.co.uk/abc` → `https://shop.amazon.co.uk`\n\n"
        f"Current mode: *{mode.upper()}*\n"
        "Use `/mode apex` or `/mode host`, or tap below:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=settings_keyboard(mode)
    )

async def mode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    arg = (context.args[0].lower() if context.args else "").strip()
    if arg not in {"apex", "host"}:
        await update.message.reply_text(
            "Usage: `/mode apex` or `/mode host`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    _set_mode(context, chat_id, arg)
    await update.message.reply_text(
        f"✅ Mode set to *{arg.upper()}*.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=settings_keyboard(arg)
    )

async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    mode = _get_mode(context, chat_id)
    await update.message.reply_text(
        f"Choose output mode (current: *{mode.upper()}*):",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=settings_keyboard(mode)
    )

async def settings_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    data = query.data or ""
    if data.startswith("mode:"):
        new_mode = data.split(":", 1)[1]
        if new_mode in {"apex", "host"}:
            _set_mode(context, chat_id, new_mode)
            await query.edit_message_text(
                f"✅ Mode set to *{new_mode.upper()}*.\n\n"
                "Send text or a `.txt` file and I’ll return a clean list.",
                parse_mode=ParseMode.MARKDOWN,
            )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    mode = _get_mode(context, chat_id)
    sites = clean_sites(update.message.text or "", mode)
    if not sites:
        await update.message.reply_text("No site URLs found.")
        return
    buf = BytesIO(("\n".join(sites) + "\n").encode("utf-8"))
    buf.seek(0)
    await update.message.reply_document(
        document=buf, filename="urls.txt",
        caption=f"✅ Extracted {len(sites)} {'apex' if mode=='apex' else 'host'} site(s)."
    )

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    mode = _get_mode(context, chat_id)
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

    sites = clean_sites(content, mode)
    if not sites:
        await update.message.reply_text("No site URLs found in your file.")
        return

    buf = BytesIO(("\n".join(sites) + "\n").encode("utf-8"))
    buf.seek(0)
    await update.message.reply_document(
        document=buf, filename="urls.txt",
        caption=f"✅ Extracted {len(sites)} {'apex' if mode=='apex' else 'host'} site(s) from your file."
    )

# Register handlers
application.add_handler(CommandHandler("start", start_cmd))
application.add_handler(CommandHandler("help", help_cmd))
application.add_handler(CommandHandler("mode", mode_cmd))  # <-- FIXED: removed extra ')'
application.add_handler(CommandHandler("settings", settings_cmd))
application.add_handler(CallbackQueryHandler(settings_cb, pattern=r"^mode:(apex|host)$"))
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
# Start PTB loop thread + auto set webhook
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
