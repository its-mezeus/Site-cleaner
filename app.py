import os
import re
import threading
import asyncio
from io import BytesIO
from urllib.parse import urlparse

from flask import Flask, request, abort

import tldextract
from pyrogram import Client as PyroClient
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

SCRAPER_API_ID = int(os.environ.get("SCRAPER_API_ID", "0"))
SCRAPER_API_HASH = os.environ.get("SCRAPER_API_HASH", "")
SCRAPER_SESSION = os.environ.get("SCRAPER_SESSION", "")
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().isdigit()]

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
# Merge sessions: chat_id -> list of raw text contents
# ----------------------------
merge_sessions = {}    # chat_id -> [str, str, ...]
merge_status_msg = {}  # chat_id -> message_id of the status message
merge_locks = {}       # chat_id -> asyncio.Lock

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
    await update.message.reply_text(
        "⚡ *Site Cleaner Bot*\n\n"
        "Clean, merge & deduplicate URL lists in seconds.\n\n"
        "📌 *Commands:*\n"
        "/clean - Clean URLs from a file\n"
        "/cclean - Extract CCs from a file\n"
        "/scr - Scrape CCs from group/channel\n"
        "/merge - Combine multiple files\n"
        "/split - Split a file by lines\n"
        "/mode - Switch Apex / Host mode\n"
        "/help - How to use this bot",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("❓ Help", callback_data="help"),
                InlineKeyboardButton("⚙️ Settings", callback_data="settings"),
            ],
            [
                InlineKeyboardButton("👤 Owner", url="https://t.me/ZEUS_IS_HERE2"),
            ]
        ])
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    mode = _get_mode(context, chat_id)
    await update.message.reply_text(
        "📖 *How to Use*\n\n"
        "*🧹 Clean URLs:*\n"
        "1. Send a `.txt` file to the bot\n"
        "2. Reply to it with /clean\n"
        "3. Get a cleaned, deduplicated `urls.txt`\n\n"
        "*💳 Clean CCs:*\n"
        "1. Send a `.txt` file to the bot\n"
        "2. Reply to it with /cclean\n"
        "3. Get valid CCs in `CC|MM|YY|CVV` format\n\n"
        "*📂 Merge Files:*\n"
        "1. Send /merge to start\n"
        "2. Send multiple `.txt` files\n"
        "3. Tap ✅ Done to get one merged file\n\n"
        "*⚙️ Modes:*\n"
        "• *Apex* → `shop.amazon.co.uk/abc` → `amazon.co.uk`\n"
        "• *Host* → `shop.amazon.co.uk/abc` → `shop.amazon.co.uk`\n\n"
        f"Current mode: *{mode.upper()}*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⚙️ Change Mode", callback_data="settings")],
            [InlineKeyboardButton("🏠 Back", callback_data="back_start")],
        ])
    )

import calendar as _calendar
from datetime import datetime as _datetime

# ----------------------------
# Credit Card helpers
# ----------------------------
# Regex to find card-like patterns: 13-19 digits followed by separators and expiry/cvv
CC_PATTERN = re.compile(
    r'(\d{13,19})\s*[\|/\\:;\-,\s]+\s*(\d{1,2})\s*[\|/\\:;\-,\s]+\s*(\d{2,4})\s*[\|/\\:;\-,\s]+\s*(\d{3,4})'
)

def luhn_check(card_number: str) -> bool:
    """Validate card number using Luhn algorithm."""
    digits = [int(d) for d in card_number]
    digits.reverse()
    total = 0
    for i, d in enumerate(digits):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0

def identify_card_brand(card_number: str) -> str:
    """Identify card brand from number."""
    n = card_number
    length = len(n)

    # Visa: starts with 4, length 13 or 16 or 19
    if n[0] == '4' and length in (13, 16, 19):
        return "VISA"

    # Mastercard: 2221-2720 or 51-55, length 16
    if length == 16:
        prefix2 = int(n[:2])
        prefix4 = int(n[:4])
        if 51 <= prefix2 <= 55 or 2221 <= prefix4 <= 2720:
            return "MASTERCARD"

    # Amex: 34 or 37, length 15
    if length == 15 and n[:2] in ('34', '37'):
        return "AMEX"

    # Discover: 6011, 622126-622925, 644-649, 65
    if length == 16:
        if n[:4] == '6011' or n[:2] == '65' or (644 <= int(n[:3]) <= 649):
            return "DISCOVER"
        if 622126 <= int(n[:6]) <= 622925:
            return "DISCOVER"

    # JCB: 3528-3589, length 16-19
    if 16 <= length <= 19:
        if 3528 <= int(n[:4]) <= 3589:
            return "JCB"

    # Diners Club: 300-305, 36, 38, length 14-19
    if 14 <= length <= 19:
        if n[:2] == '36' or n[:2] == '38' or (300 <= int(n[:3]) <= 305):
            return "DINERS"

    # Maestro: 5018, 5020, 5038, 6304, 6759, 6761, 6762, 6763
    if length >= 12:
        if n[:4] in ('5018', '5020', '5038', '6304', '6759', '6761', '6762', '6763'):
            return "MAESTRO"

    # UnionPay: 62, length 16-19
    if 16 <= length <= 19 and n[:2] == '62':
        return "UNIONPAY"

    return ""

def is_expired(month: int, year: int) -> bool:
    """Check if card is expired."""
    now = _datetime.utcnow()
    # Get last day of expiry month
    _, last_day = _calendar.monthrange(year, month)
    from datetime import datetime as dt
    expiry_date = dt(year, month, last_day, 23, 59, 59)
    return now > expiry_date

def extract_and_validate_ccs(text: str):
    """Extract, validate, deduplicate, and filter expired CCs from text.
    Returns (valid_ccs, stats_dict)."""
    matches = CC_PATTERN.findall(text)
    valid_ccs = []
    seen = set()
    total = len(matches)
    duplicates = 0
    expired = 0
    invalid = 0

    for card_num, mm, yy, cvv in matches:
        # Normalize month
        month = int(mm)
        if month < 1 or month > 12:
            invalid += 1
            continue

        # Normalize year
        year = int(yy)
        if year < 100:
            year += 2000

        # Validate year range
        if year < 2024 or year > 2099:
            invalid += 1
            continue

        # Check expiry
        if is_expired(month, year):
            expired += 1
            continue

        # Validate card length and brand
        brand = identify_card_brand(card_num)
        if not brand:
            invalid += 1
            continue

        # Luhn check
        if not luhn_check(card_num):
            invalid += 1
            continue

        # Validate CVV length (Amex = 4 digits, others = 3 digits)
        if brand == "AMEX" and len(cvv) != 4:
            invalid += 1
            continue
        if brand != "AMEX" and len(cvv) != 3:
            invalid += 1
            continue

        # Format: CC|MM|YY|CVV
        formatted_mm = f"{month:02d}"
        formatted_yy = f"{year % 100:02d}"
        cc_line = f"{card_num}|{formatted_mm}|{formatted_yy}|{cvv}"

        # Deduplicate by card number
        if card_num in seen:
            duplicates += 1
            continue
        seen.add(card_num)
        valid_ccs.append(cc_line)

    stats = {
        'total': total,
        'valid': len(valid_ccs),
        'duplicates': duplicates,
        'expired': expired,
        'invalid': invalid,
    }
    return valid_ccs, stats

async def cclean_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Extract and clean credit cards from a replied .txt file"""
    chat_id = update.effective_chat.id

    reply = update.message.reply_to_message
    if not reply or not reply.document:
        await update.message.reply_text(
            "↩️ Reply to a `.txt` file with /cclean",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    doc = reply.document
    is_txt_mime = (doc.mime_type or "").lower() == "text/plain"
    is_txt_name = (doc.file_name or "").lower().endswith(".txt")
    if not (is_txt_mime or is_txt_name):
        await update.message.reply_text("❌ That's not a `.txt` file.", parse_mode=ParseMode.MARKDOWN)
        return

    if doc.file_size and doc.file_size > 10 * 1024 * 1024:
        await update.message.reply_text("❌ File too large. Max 10 MB.")
        return

    try:
        file = await context.bot.get_file(doc.file_id)
        data = await file.download_as_bytearray()
        content = data.decode("utf-8", errors="ignore")
    except Exception:
        await update.message.reply_text("❌ Couldn't read that file. Try again.")
        return

    valid_ccs, stats = extract_and_validate_ccs(content)

    if not valid_ccs:
        await update.message.reply_text("❌ No valid credit cards found in that file.")
        return

    buf = BytesIO(("\n".join(valid_ccs) + "\n").encode("utf-8"))
    buf.seek(0)
    await update.message.reply_document(
        document=buf, filename="cleaned_ccs.txt",
        caption=(
            f"Total: *{stats['total']}* | "
            f"Valid: *{stats['valid']}* | "
            f"Duplicates: *{stats['duplicates']}* | "
            f"Expired: *{stats['expired']}* | "
            f"Invalid: *{stats['invalid']}*"
        ),
        parse_mode=ParseMode.MARKDOWN,
    )


# ----------------------------
# Pyrogram userbot for scraping
# ----------------------------
_pyro_client = None
_pyro_lock = asyncio.Lock()
scrape_cancelled = {}  # chat_id -> True when cancelled

async def _get_pyro():
    """Get or create Pyrogram client (lazy init, runs in _loop)."""
    global _pyro_client
    if _pyro_client and _pyro_client.is_connected:
        return _pyro_client
    if not all([SCRAPER_API_ID, SCRAPER_API_HASH, SCRAPER_SESSION]):
        return None
    _pyro_client = PyroClient(
        "scraper_user",
        api_id=SCRAPER_API_ID,
        api_hash=SCRAPER_API_HASH,
        session_string=SCRAPER_SESSION,
        no_updates=True,
    )
    await _pyro_client.start()
    me = await _pyro_client.get_me()
    print(f"✅ Scraper userbot: {me.first_name}", flush=True)
    return _pyro_client

# CC extraction regex for scraping (same pattern)
SCRAPE_CC_PATTERN = re.compile(
    r'(\d{13,19})\s*[\|/\\:;\-,\s]+\s*(\d{1,2})\s*[\|/\\:;\-,\s]+\s*(\d{2,4})\s*[\|/\\:;\-,\s]+\s*(\d{3,4})'
)

async def scr_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Scrape CCs from a Telegram group/channel."""
    user_id = update.effective_user.id

    # Admin only
    if ADMIN_IDS and user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only.")
        return

    # Parse args: /scr <link_or_id> <count>
    args = context.args or []
    if len(args) < 1:
        await update.message.reply_text(
            "📌 *Usage:*\n`/scr <link or ID> <max_ccs>`\n\n"
            "*Examples:*\n"
            "`/scr @channelname 5000`\n"
            "`/scr https://t.me/groupname 10000`\n"
            "`/scr https://t.me/c/123456/89420 500`\n"
            "`/scr -1001234567890 5000`\n\n"
            "Copy any message link from a channel to identify it.\n"
            "If no count given, defaults to all available.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    target = args[0]
    max_ccs = int(args[1]) if len(args) > 1 and args[1].isdigit() else 0  # 0 = no limit

    # Get pyrogram client
    pyro = await _get_pyro()
    if not pyro:
        await update.message.reply_text("❌ Scraper not configured. Set SCRAPER env vars.")
        return

    # Resolve target
    try:
        if "t.me/" in target:
            slug = target.split("t.me/")[-1].split("?")[0].rstrip("/")

            # Handle t.me/c/CHANNEL_ID/MSG_ID links (private channel message links)
            if slug.startswith("c/"):
                parts = slug.split("/")  # ['c', 'channel_id', 'msg_id', ...]
                if len(parts) >= 2 and parts[1].isdigit():
                    raw_id = int(parts[1])
                    chat_id_resolved = int(f"-100{raw_id}")
                    try:
                        chat = await pyro.get_chat(chat_id_resolved)
                    except Exception:
                        # Not in storage — use GetChannels to get access_hash
                        try:
                            from pyrogram.raw.functions.channels import GetChannels
                            from pyrogram.raw.types import InputChannel
                            result = await pyro.invoke(GetChannels(id=[InputChannel(channel_id=raw_id, access_hash=0)]))
                            if result.chats:
                                raw_chat = result.chats[0]
                                pyro.storage.conn.execute(
                                    'INSERT OR REPLACE INTO peers (id, access_hash, type, username, phone_number) VALUES (?, ?, ?, ?, ?)',
                                    (chat_id_resolved, raw_chat.access_hash, 'channel', '', '')
                                )
                                pyro.storage.conn.commit()
                                chat = await pyro.get_chat(chat_id_resolved)
                            else:
                                await update.message.reply_text("❌ Channel not found.")
                                return
                        except Exception as e2:
                            await update.message.reply_text(
                                f"❌ Can't access channel `{raw_id}`\n\nMake sure the userbot is a member.",
                                parse_mode=ParseMode.MARKDOWN
                            )
                            return
                    chat_title = chat.title or str(raw_id)
                    pass
                else:
                    await update.message.reply_text("❌ Invalid message link format.")
                    return
            elif slug.startswith("+") or slug.startswith("joinchat/"):
                # Private invite link — resolve via invite link
                invite_hash = slug.replace("joinchat/", "").lstrip("+")
                try:
                    from pyrogram.raw.functions.messages import CheckChatInvite
                    from pyrogram.raw.types import ChatInviteAlready, ChatInvitePeek, ChatInvite as RawChatInvite
                    from pyrogram.raw.types import Channel as RawChannel, Chat as RawChat
                    invite_info = await pyro.invoke(CheckChatInvite(hash=invite_hash))

                    if isinstance(invite_info, (ChatInviteAlready, ChatInvitePeek)):
                        raw_chat = invite_info.chat

                        # Save peer to Pyrogram's sqlite storage with correct prefixed ID
                        if isinstance(raw_chat, RawChannel):
                            chat_id_resolved = int(f"-100{raw_chat.id}")
                            pyro.storage.conn.execute(
                                'INSERT OR REPLACE INTO peers (id, access_hash, type, username, phone_number) VALUES (?, ?, ?, ?, ?)',
                                (chat_id_resolved, raw_chat.access_hash, 'channel', '', '')
                            )
                            pyro.storage.conn.commit()
                        elif isinstance(raw_chat, RawChat):
                            chat_id_resolved = -raw_chat.id
                            pyro.storage.conn.execute(
                                'INSERT OR REPLACE INTO peers (id, access_hash, type, username, phone_number) VALUES (?, ?, ?, ?, ?)',
                                (chat_id_resolved, 0, 'group', '', '')
                            )
                            pyro.storage.conn.commit()
                        else:
                            chat_id_resolved = raw_chat.id

                        chat = await pyro.get_chat(chat_id_resolved)
                    elif isinstance(invite_info, RawChatInvite):
                        # Not joined yet — auto-join via invite link
                        try:
                            from pyrogram.raw.functions.messages import ImportChatInvite
                            join_result = await pyro.invoke(ImportChatInvite(hash=invite_hash))
                            # After joining, resolve the chat
                            if hasattr(join_result, 'chats') and join_result.chats:
                                joined_chat = join_result.chats[0]
                                if isinstance(joined_chat, RawChannel):
                                    chat_id_resolved = int(f"-100{joined_chat.id}")
                                    pyro.storage.conn.execute(
                                        'INSERT OR REPLACE INTO peers (id, access_hash, type, username, phone_number) VALUES (?, ?, ?, ?, ?)',
                                        (chat_id_resolved, joined_chat.access_hash, 'channel', '', '')
                                    )
                                    pyro.storage.conn.commit()
                                else:
                                    chat_id_resolved = -joined_chat.id
                                    pyro.storage.conn.execute(
                                        'INSERT OR REPLACE INTO peers (id, access_hash, type, username, phone_number) VALUES (?, ?, ?, ?, ?)',
                                        (chat_id_resolved, 0, 'group', '', '')
                                    )
                                    pyro.storage.conn.commit()
                                chat = await pyro.get_chat(chat_id_resolved)
                            else:
                                await update.message.reply_text("❌ Joined but couldn't resolve the chat.")
                                return
                        except Exception as je:
                            jerr = str(je)
                            if "USER_ALREADY_PARTICIPANT" in jerr:
                                await update.message.reply_text("❌ Already joined but can't resolve. Try using the group/channel ID instead.")
                            elif "INVITE_REQUEST_SENT" in jerr:
                                await update.message.reply_text("⏳ Join request sent. Admin needs to approve. Try again after approval.")
                            else:
                                await update.message.reply_text(f"❌ Couldn't join\n\n`{jerr[:150]}`", parse_mode=ParseMode.MARKDOWN)
                            return
                    else:
                        await update.message.reply_text("❌ Could not resolve invite link.")
                        return
                except Exception as e:
                    err = str(e)
                    if "INVITE_HASH_EXPIRED" in err:
                        await update.message.reply_text("❌ Invite link expired.")
                    else:
                        await update.message.reply_text(f"❌ Can't resolve invite link\n\n`{err[:150]}`", parse_mode=ParseMode.MARKDOWN)
                    return
            else:
                # Public username (may have /msg_id suffix, just grab the username)
                username = slug.split("/")[0]
                if not username.startswith("@"):
                    username = "@" + username
                chat = await pyro.get_chat(username)
        elif target.lstrip("-").isdigit():
            chat = await pyro.get_chat(int(target))
        else:
            if not target.startswith("@"):
                target = "@" + target
            chat = await pyro.get_chat(target)

        chat_title = chat.title or str(target)
    except Exception as e:
        await update.message.reply_text(f"❌ Can't access `{target}`\n\n`{str(e)[:150]}`", parse_mode=ParseMode.MARKDOWN)
        return

    # Send progress message with cancel button
    user_chat_id = update.effective_chat.id
    cancel_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data=f"scr_cancel:{user_chat_id}")]
    ])
    scrape_cancelled.pop(user_chat_id, None)  # reset

    progress = await update.message.reply_text(
        f"🔍 Scraping *{chat_title}*...\n\n"
        f"⏳ This may take a while for large groups.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=cancel_kb,
    )

    # Scrape messages
    valid_ccs = []
    seen = set()
    messages_scanned = 0
    last_update = 0
    was_cancelled = False

    try:
        async for msg in pyro.get_chat_history(chat.id):
            # Check cancel
            if scrape_cancelled.get(user_chat_id):
                was_cancelled = True
                break

            if max_ccs and len(valid_ccs) >= max_ccs:
                break

            messages_scanned += 1

            # Update progress every 2000 messages
            if messages_scanned - last_update >= 2000:
                last_update = messages_scanned
                try:
                    await progress.edit_text(
                        f"🔍 Scraping *{chat_title}*...\n\n"
                        f"📨 Messages scanned: *{messages_scanned}*\n"
                        f"💳 CCs found: *{len(valid_ccs)}*",
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=cancel_kb,
                    )
                except Exception:
                    pass

            text = msg.text or msg.caption or ""
            if not text:
                continue

            matches = SCRAPE_CC_PATTERN.findall(text)
            for card_num, mm, yy, cvv in matches:
                if max_ccs and len(valid_ccs) >= max_ccs:
                    break

                month = int(mm)
                if month < 1 or month > 12:
                    continue

                year = int(yy)
                if year < 100:
                    year += 2000
                if year < 2024 or year > 2099:
                    continue

                if is_expired(month, year):
                    continue

                brand = identify_card_brand(card_num)
                if not brand:
                    continue

                if not luhn_check(card_num):
                    continue

                if brand == "AMEX" and len(cvv) != 4:
                    continue
                if brand != "AMEX" and len(cvv) != 3:
                    continue

                if card_num in seen:
                    continue
                seen.add(card_num)

                formatted_mm = f"{month:02d}"
                formatted_yy = f"{year % 100:02d}"
                valid_ccs.append(f"{card_num}|{formatted_mm}|{formatted_yy}|{cvv}")

    except Exception as e:
        await progress.edit_text(f"❌ Error while scraping: {str(e)[:200]}")
        return
    finally:
        scrape_cancelled.pop(user_chat_id, None)

    # Delete progress message
    try:
        await progress.delete()
    except Exception:
        pass

    if not valid_ccs:
        await update.message.reply_text(
            f"❌ No valid CCs found in *{chat_title}*\n\n"
            f"📨 Messages scanned: *{messages_scanned}*",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    status = "⚠️ *Scrape Cancelled*" if was_cancelled else "✅ *Scrape Complete!*"
    buf = BytesIO(("\n".join(valid_ccs) + "\n").encode("utf-8"))
    buf.seek(0)
    await update.message.reply_document(
        document=buf, filename=f"scraped_{chat.id}.txt",
        caption=(
            f"{status}\n\n"
            f"📢 Source: *{chat_title}*\n"
            f"📨 Messages scanned: *{messages_scanned}*\n"
            f"💳 Valid CCs: *{len(valid_ccs)}*"
        ),
        parse_mode=ParseMode.MARKDOWN,
    )


async def split_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Split a .txt file into chunks of N lines"""
    chat_id = update.effective_chat.id

    # Parse line count from args
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(
            "↩️ Reply to a `.txt` file with:\n`/split 200`\n\nReplace 200 with lines per file.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    chunk_size = int(context.args[0])
    if chunk_size < 1:
        await update.message.reply_text("❌ Line count must be at least 1.")
        return

    reply = update.message.reply_to_message
    if not reply or not reply.document:
        await update.message.reply_text(
            "↩️ Reply to a `.txt` file with /split",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    doc = reply.document
    is_txt_mime = (doc.mime_type or "").lower() == "text/plain"
    is_txt_name = (doc.file_name or "").lower().endswith(".txt")
    if not (is_txt_mime or is_txt_name):
        await update.message.reply_text("❌ That's not a `.txt` file.", parse_mode=ParseMode.MARKDOWN)
        return

    if doc.file_size and doc.file_size > 10 * 1024 * 1024:
        await update.message.reply_text("❌ File too large. Max 10 MB.")
        return

    try:
        file = await context.bot.get_file(doc.file_id)
        data = await file.download_as_bytearray()
        content = data.decode("utf-8", errors="ignore")
    except Exception:
        await update.message.reply_text("❌ Couldn't read that file. Try again.")
        return

    lines = [l for l in content.splitlines() if l.strip()]

    if not lines:
        await update.message.reply_text("❌ File is empty.")
        return

    # Split into chunks
    chunks = [lines[i:i + chunk_size] for i in range(0, len(lines), chunk_size)]

    if len(chunks) == 1:
        await update.message.reply_text(
            f"File only has *{len(lines)}* lines - no split needed.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Get base filename
    base_name = (doc.file_name or "file.txt").rsplit(".", 1)[0]

    for idx, chunk in enumerate(chunks, 1):
        buf = BytesIO(("\n".join(chunk) + "\n").encode("utf-8"))
        buf.seek(0)
        await context.bot.send_document(
            chat_id=chat_id,
            document=buf,
            filename=f"{base_name}_part{idx}.txt",
            caption=f"Part *{idx}/{len(chunks)}* - *{len(chunk)}* lines" if idx < len(chunks) else f"Part *{idx}/{len(chunks)}* - *{len(chunk)}* lines\n\n✅ Split complete! Total: *{len(lines)}* lines → *{len(chunks)}* files",
            parse_mode=ParseMode.MARKDOWN,
        )

    return


async def clean_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clean URLs from a replied .txt file"""
    chat_id = update.effective_chat.id
    mode = _get_mode(context, chat_id)

    reply = update.message.reply_to_message
    if not reply or not reply.document:
        await update.message.reply_text(
            "↩️ Reply to a `.txt` file with /clean",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    doc = reply.document
    is_txt_mime = (doc.mime_type or "").lower() == "text/plain"
    is_txt_name = (doc.file_name or "").lower().endswith(".txt")
    if not (is_txt_mime or is_txt_name):
        await update.message.reply_text("❌ That's not a `.txt` file.", parse_mode=ParseMode.MARKDOWN)
        return

    if doc.file_size and doc.file_size > 10 * 1024 * 1024:
        await update.message.reply_text("❌ File too large. Max 10 MB.")
        return

    try:
        file = await context.bot.get_file(doc.file_id)
        data = await file.download_as_bytearray()
        content = data.decode("utf-8", errors="ignore")
    except Exception:
        await update.message.reply_text("❌ Couldn't read that file. Try again.")
        return

    sites = clean_sites(content, mode)
    if not sites:
        await update.message.reply_text("No URLs found in that file.")
        return

    buf = BytesIO(("\n".join(sites) + "\n").encode("utf-8"))
    buf.seek(0)
    await update.message.reply_document(
        document=buf, filename="urls.txt",
        caption=f"✅ Cleaned *{len(sites)}* {mode} site(s).",
        parse_mode=ParseMode.MARKDOWN,
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
        f"⚙️ *Settings*\n\nCurrent mode: *{mode.upper()}*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=settings_keyboard(mode)
    )

async def settings_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    data = query.data or ""

    if data == "help":
        mode = _get_mode(context, chat_id)
        await query.edit_message_text(
            "📖 *How to Use*\n\n"
            "*🧹 Clean URLs:*\n"
            "1. Send a `.txt` file to the bot\n"
            "2. Reply to it with /clean\n"
            "3. Get a cleaned, deduplicated `urls.txt`\n\n"
            "*💳 Clean CCs:*\n"
            "1. Send a `.txt` file to the bot\n"
            "2. Reply to it with /cclean\n"
            "3. Get valid CCs in `CC|MM|YY|CVV` format\n\n"
            "*📂 Merge Files:*\n"
            "1. Send /merge to start\n"
            "2. Send multiple `.txt` files\n"
            "3. Tap ✅ Done to get one merged file\n\n"
            "*⚙️ Modes:*\n"
            "• *Apex* → `shop.amazon.co.uk/abc` → `amazon.co.uk`\n"
            "• *Host* → `shop.amazon.co.uk/abc` → `shop.amazon.co.uk`\n\n"
            f"Current mode: *{mode.upper()}*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⚙️ Change Mode", callback_data="settings")],
                [InlineKeyboardButton("🏠 Back", callback_data="back_start")],
            ])
        )
        return

    if data == "settings":
        mode = _get_mode(context, chat_id)
        await query.edit_message_text(
            f"⚙️ *Settings*\n\nCurrent mode: *{mode.upper()}*\n\n"
            "• *Apex* - strips to root domain\n"
            "• *Host* - keeps subdomains",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(("✅ Apex" if mode == "apex" else "Apex"), callback_data="mode:apex"),
                    InlineKeyboardButton(("✅ Host" if mode == "host" else "Host"), callback_data="mode:host"),
                ],
                [InlineKeyboardButton("🏠 Back", callback_data="back_start")],
            ])
        )
        return

    if data == "back_start":
        await query.edit_message_text(
            "⚡ *Site Cleaner Bot*\n\n"
            "Clean, merge & deduplicate URL lists in seconds.\n\n"
            "📌 *Commands:*\n"
            "/clean — Clean URLs from a file\n"
            "/cclean — Extract CCs from a file\n"
            "/scr — Scrape CCs from group/channel\n"
            "/merge — Combine multiple files\n"
            "/split — Split a file by lines\n"
            "/mode — Switch Apex / Host mode\n"
            "/help — How to use this bot",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("❓ Help", callback_data="help"),
                    InlineKeyboardButton("⚙️ Settings", callback_data="settings"),
                ],
                [
                    InlineKeyboardButton("👤 Owner", url="https://t.me/ZEUS_IS_HERE2"),
                ]
            ])
        )
        return

    if data.startswith("mode:"):
        new_mode = data.split(":", 1)[1]
        if new_mode in {"apex", "host"}:
            _set_mode(context, chat_id, new_mode)
            await query.edit_message_text(
                f"✅ Mode set to *{new_mode.upper()}*",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(("✅ Apex" if new_mode == "apex" else "Apex"), callback_data="mode:apex"),
                        InlineKeyboardButton(("✅ Host" if new_mode == "host" else "Host"), callback_data="mode:host"),
                    ],
                    [InlineKeyboardButton("🏠 Back", callback_data="back_start")],
                ])
            )

async def merge_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /merge, /merge done, /merge cancel"""
    chat_id = update.effective_chat.id
    arg = (context.args[0].lower() if context.args else "").strip()

    if arg == "cancel":
        if chat_id in merge_sessions:
            count = len(merge_sessions.pop(chat_id))
            merge_locks.pop(chat_id, None)
            # Delete the old status message
            old_msg_id = merge_status_msg.pop(chat_id, None)
            if old_msg_id:
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=old_msg_id)
                except Exception:
                    pass
            await update.message.reply_text(
                f"❌ Merge cancelled. {count} file(s) discarded.",
            )
        else:
            await update.message.reply_text("No active merge session.")
        return

    if arg == "done":
        if chat_id not in merge_sessions or not merge_sessions[chat_id]:
            await update.message.reply_text(
                "⚠️ No files collected yet.\n\n"
                "Send `/merge` first, then upload your `.txt` files, then `/merge done`.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        files_data = merge_sessions.pop(chat_id)
        merge_locks.pop(chat_id, None)
        # Delete the old status message
        old_msg_id = merge_status_msg.pop(chat_id, None)
        if old_msg_id:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=old_msg_id)
            except Exception:
                pass
        # Combine all lines, deduplicate, preserve order
        seen = set()
        merged_lines = []
        for content in files_data:
            for line in content.splitlines():
                line = line.strip()
                if line and line not in seen:
                    seen.add(line)
                    merged_lines.append(line)

        if not merged_lines:
            await update.message.reply_text("No content found across all files.")
            return

        buf = BytesIO(("\n".join(merged_lines) + "\n").encode("utf-8"))
        buf.seek(0)
        await update.message.reply_document(
            document=buf, filename="merged.txt",
            caption=(
                f"✅ *Merge Complete!*\n\n"
                f"📁 Files merged: *{len(files_data)}*\n"
                f"📝 Total lines: *{len(merged_lines)}*\n"
                f"🗑 Duplicates removed"
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # /merge with no args - start a new session
    merge_sessions[chat_id] = []
    merge_status_msg.pop(chat_id, None)
    status = await update.message.reply_text(
        "📂 *Merge Mode Started!*\n\n"
        "Now send me your `.txt` files one by one.\n"
        "I'll collect them all.\n\n"
        "When you're done, tap ✅ Done\n"
        "To cancel, tap ❌ Cancel",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Done", callback_data="merge:done"),
                InlineKeyboardButton("❌ Cancel", callback_data="merge:cancel"),
            ]
        ])
    )
    merge_status_msg[chat_id] = status.message_id

async def merge_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle merge inline button callbacks"""
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    data = query.data or ""

    if data == "merge:cancel":
        if chat_id in merge_sessions:
            count = len(merge_sessions.pop(chat_id))
            merge_status_msg.pop(chat_id, None)
            await query.edit_message_text(f"❌ Merge cancelled. {count} file(s) discarded.")
        else:
            await query.edit_message_text("No active merge session.")
        return

    if data == "merge:done":
        if chat_id not in merge_sessions or not merge_sessions[chat_id]:
            await query.edit_message_text(
                "⚠️ No files collected yet. Send some `.txt` files first, then tap Done."
            )
            return

        files_data = merge_sessions.pop(chat_id)
        merge_status_msg.pop(chat_id, None)
        merge_locks.pop(chat_id, None)

        seen = set()
        merged_lines = []
        for content in files_data:
            for line in content.splitlines():
                line = line.strip()
                if line and line not in seen:
                    seen.add(line)
                    merged_lines.append(line)

        if not merged_lines:
            await query.edit_message_text("No content found across all files.")
            return

        # Delete the status/button message
        try:
            await query.message.delete()
        except Exception:
            pass

        buf = BytesIO(("\n".join(merged_lines) + "\n").encode("utf-8"))
        buf.seek(0)
        await context.bot.send_document(
            chat_id=chat_id,
            document=buf, filename="merged.txt",
            caption=(
                f"✅ *Merge Complete!*\n\n"
                f"📁 Files merged: *{len(files_data)}*\n"
                f"📝 Total lines: *{len(merged_lines)}*\n"
                f"🗑 Duplicates removed"
            ),
            parse_mode=ParseMode.MARKDOWN,
        )

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle document uploads - only used for merge mode collection"""
    chat_id = update.effective_chat.id
    doc = update.message.document
    if not doc:
        return

    is_txt_mime = (doc.mime_type or "").lower() == "text/plain"
    is_txt_name = (doc.file_name or "").lower().endswith(".txt")
    if not (is_txt_mime or is_txt_name):
        # Not in merge mode? Just ignore non-txt files
        if chat_id not in merge_sessions:
            return
        await update.message.reply_text("❌ Only `.txt` files supported.", parse_mode=ParseMode.MARKDOWN)
        return

    if doc.file_size and doc.file_size > 10 * 1024 * 1024:
        await update.message.reply_text("❌ File too large. Max 10 MB.")
        return

    # If not in merge mode, ignore (user should use /clean)
    if chat_id not in merge_sessions:
        return

    try:
        file = await context.bot.get_file(doc.file_id)
        data = await file.download_as_bytearray()
        content = data.decode("utf-8", errors="ignore")
    except Exception:
        await update.message.reply_text("❌ Couldn't read that file. Try again.")
        return

    # If merge session is active, collect the file instead of processing
    if chat_id in merge_sessions:
        # Use a per-chat lock to handle simultaneous files (media groups)
        if chat_id not in merge_locks:
            merge_locks[chat_id] = asyncio.Lock()

        async with merge_locks[chat_id]:
            merge_sessions[chat_id].append(content)
            count = len(merge_sessions[chat_id])

            # Delete the old status message
            old_msg_id = merge_status_msg.pop(chat_id, None)
            if old_msg_id:
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=old_msg_id)
                except Exception:
                    pass

            # Small delay so media group files batch together before we send status
            await asyncio.sleep(0.5)

            # Re-check count after delay (more files may have arrived)
            count = len(merge_sessions.get(chat_id, []))

            # Send a fresh status message
            status_text = (
                f"📂 *Merge Mode* - {count} file(s) collected\n\n"
                f"📎 Latest: `{doc.file_name}`\n\n"
                f"Send more files or tap ✅ Done"
            )
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(f"✅ Done ({count} files)", callback_data="merge:done"),
                    InlineKeyboardButton("❌ Cancel", callback_data="merge:cancel"),
                ]
            ])
            status = await update.message.reply_text(
                status_text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb
            )
            merge_status_msg[chat_id] = status.message_id
        return

# Register handlers
application.add_handler(CommandHandler("start", start_cmd))
application.add_handler(CommandHandler("help", help_cmd))
application.add_handler(CommandHandler("clean", clean_cmd))
application.add_handler(CommandHandler("cclean", cclean_cmd))
application.add_handler(CommandHandler("split", split_cmd))
application.add_handler(CommandHandler("scr", scr_cmd))
application.add_handler(CommandHandler("mode", mode_cmd))
application.add_handler(CommandHandler("merge", merge_cmd))
application.add_handler(CommandHandler("settings", settings_cmd))
async def scr_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle scrape cancel button"""
    query = update.callback_query
    await query.answer("Cancelling... sending collected CCs")
    chat_id = query.message.chat_id
    scrape_cancelled[chat_id] = True

application.add_handler(CallbackQueryHandler(settings_cb, pattern=r"^(mode:(apex|host)|help|settings|back_start)$"))
application.add_handler(CallbackQueryHandler(merge_cb, pattern=r"^merge:(done|cancel)$"))
application.add_handler(CallbackQueryHandler(scr_cancel_cb, pattern=r"^scr_cancel:"))
application.add_handler(MessageHandler(filters.Document.ALL, handle_document))

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
