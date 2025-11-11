import os
import re
from io import BytesIO
from urllib.parse import urlparse

from flask import Flask, request, abort
from telegram import Bot, Update
from telegram.ext import (
    Dispatcher,
    MessageHandler,
    Filters,
    CallbackContext,
    CommandHandler,
)
import tldextract

# --- Config ---
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN env var")

# --- Flask app ---
app = Flask(__name__)

# --- Telegram bot + dispatcher ---
bot = Bot(token=BOT_TOKEN)
dispatcher = Dispatcher(bot=bot, update_queue=None, workers=0, use_context=True)

# --- URL helpers ---
URL_REGEX = re.compile(
    r'(?:(?:https?://)|(?:www\.))?(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?::\d{2,5})?(?:/[^\s]*)?',
    re.IGNORECASE
)

def normalize_input_url(u: str) -> str:
    """Ensure the URL has a scheme. If missing, add https://"""
    u = u.strip()
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

    # handle localhost/IPs
    if re.match(r"^(\d{1,3}\.){3}\d{1,3}$", host) or host == "localhost":
        site = host
    else:
        ext = tldextract.extract(host)
        site = f"{ext.domain}.{ext.suffix}" if (ext.domain and ext.suffix) else host
    return f"https://{site}"

def extract_urls(text: str):
    """Find URL-like strings and de-dup preserving order."""
    if not text:
        return []
    matches = URL_REGEX.findall(text)
    seen, out = set(), []
    for m in matches:
        s = m.strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out

def clean_pairs(text: str):
    """
    Returns list of (normalized_url, apex_site) pairs.
    - normalized_url: original with https:// added if missing
    - apex_site: https://<registrable-domain>
    De-duplicated by normalized_url.
    """
    pairs, seen_norm = [], set()
    for u in extract_urls(text):
        norm = normalize_input_url(u)
        if not norm or norm in seen_norm:
            continue
        seen_norm.add(norm)
        pairs.append((norm, to_apex_site(norm)))
    return pairs

# --- Command handlers ---
def start(update: Update, context: CallbackContext):
    update.message.reply_text(
        "👋 *Welcome to the Site Cleaner Bot!*\n\n"
        "Send me:\n"
        "• Any text containing URLs\n"
        "• Or upload a `.txt` file with URLs\n\n"
        "I will automatically:\n"
        "✅ Detect all links\n"
        "✅ Add `https://` if missing\n"
        "✅ Convert each to its clean site (apex domain)\n"
        "✅ Return a tidy `urls.txt` mapping:\n"
        "`<normalized>` -> `<site>`\n\n"
        "*Example:*\n"
        "`shop.amazon.co.uk/deal` → `https://amazon.co.uk`\n"
        "`example.com/path` → `https://example.com`\n\n"
        "Just send your text or `.txt` now!",
        parse_mode="Markdown"
    )

def help_command(update: Update, context: CallbackContext):
    update.message.reply_text(
        "📖 *How to Use*\n\n"
        "1) Send any message with links, or upload a `.txt` file (≤10MB)\n"
        "2) I will:\n"
        "   • Add `https://` if missing\n"
        "   • Clean to main domain (apex)\n"
        "   • Remove duplicates\n"
        "   • Reply with `urls.txt`\n\n"
        "*Example:*\n"
        "`https://bsteessublimationink.com/product/123` → `https://bsteessublimationink.com`\n",
        parse_mode="Markdown"
    )

# --- Message handlers ---
def handle_text(update: Update, context: CallbackContext):
    msg = update.effective_message
    pairs = clean_pairs(msg.text or "")
    if not pairs:
        msg.reply_text("No URLs found.")
        return
    lines = [f"{norm} -> {site}" for (norm, site) in pairs]
    buf = BytesIO(("\n".join(lines) + "\n").encode("utf-8"))
    buf.seek(0)
    msg.reply_document(document=buf, filename="urls.txt",
                       caption=f"✅ Processed {len(pairs)} URL(s).")

def handle_document(update: Update, context: CallbackContext):
    msg = update.effective_message
    doc = msg.document
    if not doc:
        msg.reply_text("Please upload a `.txt` file.", parse_mode="Markdown")
        return

    is_txt_mime = (doc.mime_type or "").lower() == "text/plain"
    is_txt_name = (doc.file_name or "").lower().endswith(".txt")
    if not (is_txt_mime or is_txt_name):
        msg.reply_text("Unsupported file type. Please upload a `.txt` file.", parse_mode="Markdown")
        return

    if doc.file_size and doc.file_size > 10 * 1024 * 1024:
        msg.reply_text("File too large. Please upload a .txt under 10 MB.")
        return

    try:
        file_obj = context.bot.get_file(doc.file_id)
        inbuf = BytesIO()
        file_obj.download(out=inbuf)
        content = inbuf.getvalue().decode("utf-8", errors="ignore")
    except Exception:
        msg.reply_text("Couldn't read that file. Please try again.")
        return

    pairs = clean_pairs(content)
    if not pairs:
        msg.reply_text("No URLs found in your file.")
        return

    lines = [f"{norm} -> {site}" for (norm, site) in pairs]
    outbuf = BytesIO(("\n".join(lines) + "\n").encode("utf-8"))
    outbuf.seek(0)
    msg.reply_document(document=outbuf, filename="urls.txt",
                       caption=f"✅ Processed {len(pairs)} URL(s) from your file.")

# --- Register handlers ---
dispatcher.add_handler(CommandHandler("start", start))
dispatcher.add_handler(CommandHandler("help", help_command))
dispatcher.add_handler(MessageHandler(Filters.document, handle_document))
dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_text))

# --- Webhook endpoints (no auto setWebhook here) ---
@app.route("/", methods=["GET"])
def health():
    return "OK", 200

@app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
def telegram_webhook():
    update_json = request.get_json(force=True, silent=True)
    if not update_json:
        abort(400)
    update = Update.de_json(update_json, bot)
    dispatcher.process_update(update)
    return "OK", 200

# Local dev (optional)
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
