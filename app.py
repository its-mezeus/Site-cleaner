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

# ----------------------------
# ENV VARIABLES
# ----------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "")

if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN env var")

if not RENDER_URL:
    raise RuntimeError("Missing RENDER_EXTERNAL_URL env var (Render adds this automatically)")

# ----------------------------
# FLASK
# ----------------------------
app = Flask(__name__)

# ----------------------------
# TELEGRAM BOT
# ----------------------------
bot = Bot(token=BOT_TOKEN)
dispatcher = Dispatcher(bot=bot, update_queue=None, workers=0, use_context=True)

WEBHOOK_URL = f"{RENDER_URL}/webhook/{BOT_TOKEN}"

# ----------------------------
# URL HELPERS
# ----------------------------
URL_REGEX = re.compile(
    r'(?:(?:https?://)|(?:www\.))?(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?::\d{2,5})?(?:/[^\s]*)?',
    re.IGNORECASE
)

def normalize_input_url(u: str) -> str:
    """Ensure https:// exists (but leave path/query intact)."""
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
    # keep IP/localhost as-is
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

def clean_pairs(text: str):
    """
    Returns list of (normalized_url, apex_site) pairs.
    - normalized_url: original with https:// added if missing
    - apex_site: https://<registrable-domain>
    De-duplicated by normalized_url.
    """
    pairs, seen = [], set()
    for u in extract_urls(text):
        norm = normalize_input_url(u)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        pairs.append((norm, to_apex_site(norm)))
    return pairs

# ----------------------------
# COMMAND HANDLERS
# ----------------------------
def start(update: Update, context: CallbackContext):
    update.message.reply_text(
        "👋 *Welcome to the Site Cleaner Bot!*\n\n"
        "Send me:\n"
        "• Any text containing URLs\n"
        "• Or upload a `.txt` file with URLs\n\n"
        "I will:\n"
        "✅ Add `https://` if missing\n"
        "✅ Extract the clean apex domain\n"
        "✅ Remove duplicates\n"
        "✅ Return a `urls.txt` mapping:\n"
        "`<normalized>` -> `<site>`\n\n"
        "*Example:*\n"
        "`shop.amazon.co.uk/deal` → `https://amazon.co.uk`\n"
        "`example.com/path` → `https://example.com`",
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

# ----------------------------
# MESSAGE HANDLERS
# ----------------------------
def handle_text(update: Update, context: CallbackContext):
    pairs = clean_pairs(update.message.text or "")
    if not pairs:
        update.message.reply_text("No URLs found.")
        return

    lines = [f"{norm} -> {site}" for norm, site in pairs]
    buf = BytesIO(("\n".join(lines) + "\n").encode("utf-8"))
    buf.seek(0)
    update.message.reply_document(
        document=buf, filename="urls.txt",
        caption=f"✅ Processed {len(pairs)} URL(s)."
    )

def handle_document(update: Update, context: CallbackContext):
    doc = update.message.document
    if not doc:
        update.message.reply_text("Please upload a `.txt` file.", parse_mode="Markdown")
        return

    is_txt_mime = (doc.mime_type or "").lower() == "text/plain"
    is_txt_name = (doc.file_name or "").lower().endswith(".txt")
    if not (is_txt_mime or is_txt_name):
        update.message.reply_text("Unsupported file type. Please upload a `.txt` file.", parse_mode="Markdown")
        return

    if doc.file_size and doc.file_size > 10 * 1024 * 1024:
        update.message.reply_text("File too large. Please upload a .txt under 10 MB.")
        return

    try:
        file_obj = context.bot.get_file(doc.file_id)
        inbuf = BytesIO()
        file_obj.download(out=inbuf)
        content = inbuf.getvalue().decode("utf-8", errors="ignore")
    except Exception:
        update.message.reply_text("Couldn't read that file. Please try again.")
        return

    pairs = clean_pairs(content)
    if not pairs:
        update.message.reply_text("No URLs found in your file.")
        return

    lines = [f"{norm} -> {site}" for (norm, site) in pairs]
    outbuf = BytesIO(("\n".join(lines) + "\n").encode("utf-8"))
    outbuf.seek(0)
    update.message.reply_document(
        document=outbuf, filename="urls.txt",
        caption=f"✅ Processed {len(pairs)} URL(s) from your file."
    )

# ----------------------------
# REGISTER HANDLERS
# ----------------------------
dispatcher.add_handler(CommandHandler("start", start))
dispatcher.add_handler(CommandHandler("help", help_command))
dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_text))
dispatcher.add_handler(MessageHandler(Filters.document, handle_document))

# ----------------------------
# FLASK ROUTES
# ----------------------------
@app.route("/", methods=["GET"])
def health():
    return "OK", 200

@app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
def webhook():
    update_data = request.get_json(force=True, silent=True)
    if not update_data:
        abort(400)
    update = Update.de_json(update_data, bot)
    dispatcher.process_update(update)
    return "OK", 200

# ----------------------------
# AUTO SET WEBHOOK ON STARTUP
# ----------------------------
with app.app_context():
    try:
        bot.set_webhook(url=WEBHOOK_URL, drop_pending_updates=True)
        print("✅ Webhook set to:", WEBHOOK_URL)
    except Exception as e:
        print("❌ Failed to set webhook:", e)

# ----------------------------
# LOCAL RUN
# ----------------------------
if __name__ == "__main__":
    app.run(port=8000, host="0.0.0.0")
