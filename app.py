# app.py (Option A: Manual /clean mode)
import os
import re
import threading
import asyncio
from io import BytesIO
from urllib.parse import urlparse, splitext

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
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "")
WEB_BASE_URL = RENDER_URL or os.environ.get("WEB_BASE_URL", "")

if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN env var")

# ----------------------------
# Flask
# ----------------------------
app = Flask(__name__)

# ----------------------------
# PTB v21 (async)
# ----------------------------
application = Application.builder().token(BOT_TOKEN).build()
_loop = asyncio.new_event_loop()

# ----------------------------
# Config & state helpers
# ----------------------------
DEFAULT_MODE = "apex"  # apex -> registrable domain | host -> keep subdomain

def _get_mode(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> str:
    modes = context.application.bot_data.setdefault("modes", {})
    return modes.get(chat_id, DEFAULT_MODE)

def _set_mode(context: ContextTypes.DEFAULT_TYPE, chat_id: int, mode: str):
    modes = context.application.bot_data.setdefault("modes", {})
    modes[chat_id] = mode

# Pending storage for Option A (per chat)
# bot_data["pending"] = { chat_id: {"texts":[...], "files":[{"file_id","file_name","file_size"}, ...]} }
def _get_pending(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    pendings = context.application.bot_data.setdefault("pending", {})
    return pendings.setdefault(chat_id, {"texts": [], "files": []})

def _clear_pending(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    pendings = context.application.bot_data.setdefault("pending", {})
    pendings.pop(chat_id, None)

# merge helpers (unchanged)
def _start_merge(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    merges = context.application.bot_data.setdefault("merge", {})
    merges[chat_id] = {"files": [], "total_bytes": 0}

def _cancel_merge(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    merges = context.application.bot_data.setdefault("merge", {})
    merges.pop(chat_id, None)

def _is_merging(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> bool:
    merges = context.application.bot_data.setdefault("merge", {})
    return chat_id in merges

def _add_merge_file(context: ContextTypes.DEFAULT_TYPE, chat_id: int, file_id: str, file_size: int, file_name: str):
    merges = context.application.bot_data.setdefault("merge", {})
    entry = merges.setdefault(chat_id, {"files": [], "total_bytes": 0})
    entry["files"].append({"file_id": file_id, "file_size": file_size, "file_name": file_name})
    entry["total_bytes"] += (file_size or 0)

def _get_merge_files(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    merges = context.application.bot_data.setdefault("merge", {})
    return merges.get(chat_id, {"files": [], "total_bytes": 0}).get("files", [])

# ----------------------------
# Config limits for merge & pending
# ----------------------------
MAX_MERGE_FILES = 30
MAX_TOTAL_MERGE_BYTES = 25 * 1024 * 1024
MAX_SINGLE_FILE_BYTES = 10 * 1024 * 1024
# For pending accumulation: cap total pending bytes to avoid abuse
MAX_PENDING_TOTAL_BYTES = 25 * 1024 * 1024

# ----------------------------
# URL Helpers
# ----------------------------
URL_REGEX = re.compile(
    r'(?:(?:https?://)|(?:www\.))?(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?::\d{2,5})?(?:/[^\s]*)?',
    re.IGNORECASE
)

def normalize_input_url(u: str) -> str:
    u = (u or "").strip()
    if not u:
        return ""
    if not re.match(r'^[a-zA-Z][a-zA-Z0-9+\-.]*://', u):
        u = "https://" + u
    return u

def to_apex_site(u: str) -> str:
    u = normalize_input_url(u)
    p = urlparse(u)
    host = p.hostname or ""
    if re.match(r"^(\d{1,3}\.){3}\d{1,3}$", host) or host == "localhost":
        site = host
    else:
        ext = tldextract.extract(host)
        site = f"{ext.domain}.{ext.suffix}" if (ext.domain and ext.suffix) else host
    return f"https://{site}"

def to_host_site(u: str) -> str:
    u = normalize_input_url(u)
    p = urlparse(u)
    host = p.hostname or ""
    if not host:
        return ""
    return f"https://{host}"

def extract_urls(text: str):
    matches = URL_REGEX.findall(text or "")
    seen, out = set(), []
    for m in matches:
        s = m.strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out

def clean_sites_list_from_text(text: str, mode: str):
    sites, seen = [], set()
    for u in extract_urls(text):
        site = to_apex_site(u) if mode == "apex" else to_host_site(u)
        if site and site not in seen:
            seen.add(site)
            sites.append(site)
    return sites

# ----------------------------
# Credit Card extraction & validation
# ----------------------------
CC_CANDIDATE_RE = re.compile(r'(?:\d[ -]*){13,19}')

def digits_only(s: str) -> str:
    return re.sub(r'[^0-9]', '', s)

def luhn_check(card_number: str) -> bool:
    total = 0
    reverse_digits = card_number[::-1]
    for i, ch in enumerate(reverse_digits):
        d = ord(ch) - 48
        if i % 2 == 1:
            d = d * 2
            if d > 9:
                d -= 9
        total += d
    return (total % 10) == 0

def detect_card_type(card_number: str) -> str:
    n = card_number
    ln = len(n)
    if ln in (13,16,19) and n.startswith('4'):
        return 'Visa'
    two = int(n[:2]) if len(n) >= 2 else 0
    three = int(n[:3]) if len(n) >= 3 else 0
    four = int(n[:4]) if len(n) >= 4 else 0
    six = int(n[:6]) if len(n) >= 6 else 0

    if ln == 15 and (n.startswith('34') or n.startswith('37')):
        return 'American Express'
    if ln == 16 and (51 <= two <= 55 or 2221 <= int(n[:4]) <= 2720):
        return 'MasterCard'
    if ln in (16,19) and (n.startswith('6011') or n.startswith('65') or 644 <= three <= 649 or 622126 <= six <= 622925):
        return 'Discover'
    if 16 <= ln <= 19 and 3528 <= three <= 3589:
        return 'JCB'
    if ln == 14 and (300 <= three <= 305 or n.startswith('36') or n.startswith('38')):
        return 'Diners Club'
    if 12 <= ln <= 19 and (n.startswith('50') or n.startswith('56') or n.startswith('57') or n.startswith('58') or n.startswith('6')):
        return 'Maestro/Other'
    return 'Unknown'

def extract_credit_cards_from_text(text: str):
    found = []
    seen = set()
    for m in CC_CANDIDATE_RE.findall(text or ""):
        digits = digits_only(m)
        if len(digits) < 13 or len(digits) > 19:
            continue
        if digits in seen:
            continue
        if luhn_check(digits):
            ctype = detect_card_type(digits)
            seen.add(digits)
            found.append({'number': digits, 'type': ctype})
    return found

# ----------------------------
# Filenames helpers
# ----------------------------
def cleaned_filename_for(original_name: str, prefix: str = "cleaned_") -> str:
    if not original_name or "." not in original_name:
        return f"{prefix}urls.txt"
    base, ext = splitext(original_name)
    ext = ext or ".txt"
    safe_base = base.strip() or "file"
    return f"{prefix}{safe_base}{ext}"

def merged_filename_for(first_filename: str) -> str:
    if first_filename:
        base, _ = splitext(first_filename)
        base = base.strip() or "files"
        return f"merged_{base}_cleaned.txt"
    return "merged_files_cleaned.txt"

def merged_cards_filename_for(first_filename: str) -> str:
    if first_filename:
        base, _ = splitext(first_filename)
        base = base.strip() or "files"
        return f"merged_cards_{base}_cleaned.txt"
    return "merged_cards_files_cleaned.txt"

# ----------------------------
# Inline Keyboard
# ----------------------------
def settings_keyboard(curr_mode: str):
    is_apex = curr_mode == "apex"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Apex" if is_apex else "Apex", callback_data="mode:apex"),
            InlineKeyboardButton("✅ Host" if not is_apex else "Host", callback_data="mode:host"),
        ]
    ])

# ----------------------------
# Commands
# ----------------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    mode = _get_mode(context, chat_id)
    await update.message.reply_text(
        "👋 *Welcome to the Site Cleaner Bot (Manual Clean Mode)!*\n\n"
        "Upload text or `.txt` files — I will store them. When you're ready send `/clean` to process all stored items.\n\n"
        "*Modes:*\n"
        "🔵 *Apex* → Removes subdomain (default)\n"
        "🟣 *Host* → Keeps subdomain\n\n"
        f"Current mode: *{mode.upper()}*\n\n"
        "Use `/settings` to change mode or `/help` for more.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=settings_keyboard(mode)
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    mode = _get_mode(context, chat_id)
    await update.message.reply_text(
        "📖 *Manual Clean Mode — How to use*\n\n"
        "1) Send any text message(s) containing URLs.\n"
        "2) Or upload one or more `.txt` files (as documents).\n"
        "3) When ready, send `/clean` — I will process everything you uploaded/sent and return results.\n\n"
        "*Results behavior:*\n"
        "• If only URLs found → I send cleaned URLs file.\n"
        "• If only credit cards found → I send cards file.\n"
        "• If both found → I send URLs first, then cards (cards contain full numbers).\n\n"
        "Commands:\n"
        "• `/clean` — Process all pending stored items now.\n"
        "• `/pending` — Show how many pending texts/files you have.\n"
        "• `/clear_pending` — Discard stored items.\n"
        "• `/merge` + upload files + `/done` — separate merge workflow (unchanged).\n\n"
        "⚠️ Security: card files are sensitive. Keep them private.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=settings_keyboard(mode)
    )

async def mode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    arg = (context.args[0].lower() if context.args else "").strip()
    if arg not in {"apex", "host"}:
        await update.message.reply_text("Usage: `/mode apex` or `/mode host`", parse_mode=ParseMode.MARKDOWN)
        return
    _set_mode(context, chat_id, arg)
    await update.message.reply_text(f"✅ Mode set to *{arg.upper()}*.", parse_mode=ParseMode.MARKDOWN, reply_markup=settings_keyboard(arg))

async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    mode = _get_mode(context, chat_id)
    await update.message.reply_text(f"⚙️ Current mode: *{mode.upper()}*\nChoose below:", parse_mode=ParseMode.MARKDOWN, reply_markup=settings_keyboard(mode))

async def settings_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    data = query.data or ""
    if data.startswith("mode:"):
        new_mode = data.split(":", 1)[1]
        if new_mode in {"apex", "host"}:
            _set_mode(context, chat_id, new_mode)
            await query.edit_message_text(f"✅ Mode set to *{new_mode.upper()}*.\n\nUpload files or send text, then `/clean`.", parse_mode=ParseMode.MARKDOWN, reply_markup=settings_keyboard(new_mode))

# ----------------------------
# Pending management commands
# ----------------------------
async def pending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    pending = _get_pending(context, chat_id)
    texts = len(pending.get("texts", []))
    files = len(pending.get("files", []))
    await update.message.reply_text(f"Pending stored items: {texts} text(s), {files} file(s). Send `/clean` to process or `/clear_pending` to discard.")

async def clear_pending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    _clear_pending(context, chat_id)
    await update.message.reply_text("Pending stored items cleared.")

# ----------------------------
# Merge commands (unchanged)
# ----------------------------
async def merge_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if _is_merging(context, chat_id):
        await update.message.reply_text("You already have an active merge session. Send more `.txt` files or `/done` to finish, `/merge_cancel` to cancel.")
        return
    _start_merge(context, chat_id)
    await update.message.reply_text(
        "🔀 *Merge mode started*\n\n"
        "Upload two or more `.txt` files (documents). When finished send `/done` to merge and process.\n"
        "To cancel, send `/merge_cancel`.",
        parse_mode=ParseMode.MARKDOWN
    )

async def merge_cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _is_merging(context, chat_id):
        await update.message.reply_text("No active merge session to cancel.")
        return
    _cancel_merge(context, chat_id)
    await update.message.reply_text("Merge session cancelled.")

# /done: unchanged merge processing (sends files immediately)
async def done_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _is_merging(context, chat_id):
        await update.message.reply_text("No active merge session. Start one with `/merge`.")
        return

    files = _get_merge_files(context, chat_id)
    if not files:
        _cancel_merge(context, chat_id)
        await update.message.reply_text("No files were uploaded. Merge cancelled.")
        return

    total_bytes = sum(f.get("file_size") or 0 for f in files)
    if total_bytes > MAX_TOTAL_MERGE_BYTES:
        _cancel_merge(context, chat_id)
        await update.message.reply_text("Total uploaded files exceed allowed size. Merge cancelled.")
        return

    mode = _get_mode(context, chat_id)

    merged_texts = []
    first_filename = None
    for fentry in files:
        file_id = fentry["file_id"]
        fname = fentry.get("file_name")
        if not first_filename and fname:
            first_filename = fname
        try:
            file = await context.bot.get_file(file_id)
            data = await file.download_as_bytearray()
            merged_texts.append(data.decode("utf-8", errors="ignore"))
        except Exception:
            continue

    if not merged_texts:
        _cancel_merge(context, chat_id)
        await update.message.reply_text("Couldn't download any files. Merge cancelled.")
        return

    combined = "\n".join(merged_texts)
    sites = clean_sites_list_from_text(combined, mode)
    cards = extract_credit_cards_from_text(combined)

    sent_any = False
    if sites:
        out_filename = merged_filename_for(first_filename)
        buf = BytesIO(("\n".join(sites) + "\n").encode("utf-8"))
        buf.seek(0)
        await update.message.reply_document(document=buf, filename=out_filename, caption=f"✅ Merged and extracted {len(sites)} site(s) in *{mode.upper()}* mode.", parse_mode=ParseMode.MARKDOWN, reply_markup=settings_keyboard(mode))
        sent_any = True

    if cards:
        card_lines = [f"{c['type']}:{c['number']}" for c in cards]
        cards_filename = merged_cards_filename_for(first_filename)
        buf_cards = BytesIO(("\n".join(card_lines) + "\n").encode("utf-8"))
        buf_cards.seek(0)
        await update.message.reply_document(document=buf_cards, filename=cards_filename, caption=f"🔒 Extracted {len(cards)} validated credit card(s). Keep them secure.", parse_mode=ParseMode.MARKDOWN)
        sent_any = True

    if not sent_any:
        await update.message.reply_text("No site URLs or credit cards found in merged files.")

    _cancel_merge(context, chat_id)

# ----------------------------
# /clean command - processes pending stored items (Option A)
# ----------------------------
async def clean_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    pending = _get_pending(context, chat_id)
    texts = pending.get("texts", [])
    files = pending.get("files", [])

    if not texts and not files:
        await update.message.reply_text("No pending items to clean. Upload text or files first.")
        return

    mode = _get_mode(context, chat_id)

    # Download files content
    combined_parts = []
    first_filename = None
    total_pending_bytes = 0

    for f in files:
        if not first_filename and f.get("file_name"):
            first_filename = f.get("file_name")
        try:
            file_obj = await context.bot.get_file(f["file_id"])
            data = await file_obj.download_as_bytearray()
            total_pending_bytes += len(data)
            combined_parts.append(data.decode("utf-8", errors="ignore"))
        except Exception:
            continue

    # include texts
    combined_parts.extend(texts)

    combined_text = "\n".join(combined_parts)

    # Clean
    sites = clean_sites_list_from_text(combined_text, mode)
    cards = extract_credit_cards_from_text(combined_text)

    sent_any = False
    # Send URLs first if present
    if sites:
        # Choose filename:
        if len(files) == 1 and not texts:
            # single uploaded file -> keep original name cleaned_<original>
            out_filename = cleaned_filename_for(files[0].get("file_name", "urls.txt"), prefix="cleaned_")
        else:
            # multiple files or texts -> merged name if file exists else urls.txt
            out_filename = merged_filename_for(first_filename) if first_filename else "urls.txt"
        buf = BytesIO(("\n".join(sites) + "\n").encode("utf-8"))
        buf.seek(0)
        await update.message.reply_document(document=buf, filename=out_filename, caption=f"✅ Extracted {len(sites)} site(s) in *{mode.upper()}* mode.", parse_mode=ParseMode.MARKDOWN, reply_markup=settings_keyboard(mode))
        sent_any = True

    if cards:
        # Choose cards filename:
        if len(files) == 1 and not texts:
            cards_filename = f"cards_{files[0].get('file_name','file.txt')}"
        else:
            cards_filename = merged_cards_filename_for(first_filename) if first_filename else "cards.txt"
        card_lines = [f"{c['type']}:{c['number']}" for c in cards]
        buf_cards = BytesIO(("\n".join(card_lines) + "\n").encode("utf-8"))
        buf_cards.seek(0)
        await update.message.reply_document(document=buf_cards, filename=cards_filename, caption=f"🔒 Extracted {len(cards)} validated credit card(s). Keep them secure.", parse_mode=ParseMode.MARKDOWN)
        sent_any = True

    if not sent_any:
        await update.message.reply_text("No valid URLs or credit card numbers found in your pending items.")

    # Clear pending after processing
    _clear_pending(context, chat_id)

# ----------------------------
# Text & Document handlers (now store pending instead of processing)
# ----------------------------
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    # If merging active, instruct to use merge flow
    if _is_merging(context, chat_id):
        await update.message.reply_text("You're in merge mode. Upload `.txt` files and use `/done` to process merged output.")
        return

    pending = _get_pending(context, chat_id)
    pending_texts = pending["texts"]
    # Simple pending bytes check: approximate by length
    if sum(len(t.encode("utf-8")) for t in pending_texts) > MAX_PENDING_TOTAL_BYTES:
        await update.message.reply_text("Pending text storage limit reached. Please send `/clean` or `/clear_pending`.")
        return

    pending_texts.append(update.message.text or "")
    await update.message.reply_text("Saved your text. Send `/clean` when you're ready to process all stored items.")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    doc = update.message.document
    if not doc:
        await update.message.reply_text("Please upload a `.txt` file.")
        return

    # If merging active, collect into merge instead (unchanged)
    if _is_merging(context, chat_id):
        is_txt_mime = (doc.mime_type or "").lower() == "text/plain"
        is_txt_name = (doc.file_name or "").lower().endswith(".txt")
        if not (is_txt_mime or is_txt_name):
            await update.message.reply_text("Unsupported file type for merging. Please upload `.txt` files only.")
            return

        if doc.file_size and doc.file_size > MAX_SINGLE_FILE_BYTES:
            await update.message.reply_text(f"File too large (limit {MAX_SINGLE_FILE_BYTES//(1024*1024)} MB). Skipping this file.")
            return

        files = _get_merge_files(context, chat_id)
        if len(files) >= MAX_MERGE_FILES:
            await update.message.reply_text(f"Reached maximum number of merge files ({MAX_MERGE_FILES}). Send `/done` to finish or `/merge_cancel` to cancel.")
            return

        total_bytes = sum(f.get("file_size") or 0 for f in files) + (doc.file_size or 0)
        if total_bytes > MAX_TOTAL_MERGE_BYTES:
            await update.message.reply_text("Adding this file would exceed total merge size limit. Send `/done` or `/merge_cancel`.", parse_mode=ParseMode.MARKDOWN)
            return

        _add_merge_file(context, chat_id, doc.file_id, doc.file_size or 0, doc.file_name or "file.txt")
        files_count = len(_get_merge_files(context, chat_id))
        await update.message.reply_text(f"✅ File added to merge ({files_count} file(s) collected). Send more `.txt` files or `/done` to finish.", parse_mode=ParseMode.MARKDOWN)
        return

    # Not merging: store to pending
    is_txt_mime = (doc.mime_type or "").lower() == "text/plain"
    is_txt_name = (doc.file_name or "").lower().endswith(".txt")
    if not (is_txt_mime or is_txt_name):
        await update.message.reply_text("Unsupported file type. Please upload a `.txt` file.")
        return

    if doc.file_size and doc.file_size > MAX_SINGLE_FILE_BYTES:
        await update.message.reply_text("File too large. Please upload a `.txt` under 10 MB.")
        return

    pending = _get_pending(context, chat_id)
    total_pending_bytes = sum(f.get("file_size") or 0 for f in pending["files"]) + (doc.file_size or 0)
    if total_pending_bytes > MAX_PENDING_TOTAL_BYTES:
        await update.message.reply_text("Adding this file would exceed pending storage limit. Send `/clean` or `/clear_pending` first.")
        return

    # store reference (we'll download on /clean)
    pending["files"].append({"file_id": doc.file_id, "file_name": doc.file_name or "file.txt", "file_size": doc.file_size or 0})
    files_count = len(pending["files"])
    await update.message.reply_text(f"Saved file to pending ({files_count} file(s)). Send `/clean` when ready or upload more files.")

# ----------------------------
# Register handlers
# ----------------------------
application.add_handler(CommandHandler("start", start_cmd))
application.add_handler(CommandHandler("help", help_cmd))
application.add_handler(CommandHandler("mode", mode_cmd))
application.add_handler(CommandHandler("settings", settings_cmd))
application.add_handler(CallbackQueryHandler(settings_cb, pattern=r"^mode:(apex|host)$"))
application.add_handler(CommandHandler("merge", merge_cmd))
application.add_handler(CommandHandler("merge_cancel", merge_cancel_cmd))
application.add_handler(CommandHandler("done", done_cmd))
application.add_handler(CommandHandler("clean", clean_cmd))
application.add_handler(CommandHandler("pending", pending_cmd))
application.add_handler(CommandHandler("clear_pending", clear_pending_cmd))
application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

# ----------------------------
# Flask Webhook
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
# Run PTB in thread + auto webhook
# ----------------------------
def _run_bot():
    asyncio.set_event_loop(_loop)
    _loop.run_until_complete(application.initialize())
    _loop.run_until_complete(application.start())

    if WEB_BASE_URL:
        webhook_url = f"{WEB_BASE_URL}/webhook/{BOT_TOKEN}"
        async def set_hook():
            try:
                await application.bot.delete_webhook(drop_pending_updates=True)
                await application.bot.set_webhook(url=webhook_url, drop_pending_updates=True)
                print("✅ Webhook set to:", webhook_url, flush=True)
            except Exception as e:
                print("❌ Failed to set webhook:", e, flush=True)
        _loop.run_until_complete(set_hook())

    _loop.run_forever()

threading.Thread(target=_run_bot, daemon=True).start()

# ----------------------------
# Local
# ----------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
