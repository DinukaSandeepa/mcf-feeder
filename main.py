import os
import sys
import asyncio
import time
import calendar
import threading
import functools
import requests
import feedparser
import logging
from flask import Flask
from pymongo import MongoClient
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
)
from bs4 import BeautifulSoup

# Load .env if python-dotenv is installed
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# --- CONFIGURATION ---
TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
MONGO_URI = os.getenv("MONGO_URI")
APP_URL = os.getenv("APP_URL")  # Used for keep-alive
OWNER_ID = int(os.getenv("OWNER_ID", "0"))  # Telegram user ID of the bot owner

# Setup Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# Validation
if not TOKEN:
    logging.error("Missing BOT_TOKEN. Set it in your environment or .env file.")
    sys.exit(1)
if not MONGO_URI:
    logging.error("Missing MONGO_URI. Set it in your environment or .env file.")
    sys.exit(1)
if not CHANNEL_ID:
    logging.error("Missing CHANNEL_ID. Set it in your environment or .env file.")
    sys.exit(1)
if not OWNER_ID:
    logging.error("Missing OWNER_ID. Set it in your environment or .env file.")
    sys.exit(1)

# MongoDB Setup
client = MongoClient(MONGO_URI)
db = client['movie_news_bot']
feed_state_collection = db['feed_state']   # one doc per feed, rolling seen_links[]
feeds_collection = db['rss_feeds']

# Max number of link URLs retained per feed document (rolling window)
MAX_SEEN_PER_FEED = 100

# ConversationHandler states
SELECT_FEED, SELECT_POST = range(2)

# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

def owner_only(func):
    """Decorator: silently ignore commands from anyone who isn't OWNER_ID."""
    @functools.wraps(func)
    async def wrapper(update: Update, context, *args, **kwargs):
        user_id = update.effective_user.id if update.effective_user else None
        if user_id != OWNER_ID:
            logging.warning(f"Blocked unauthorized access from user_id={user_id}")
            return  # silent reject — no message to the stranger
        return await func(update, context, *args, **kwargs)
    return wrapper

# Flask for Render Web Service
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running!", 200

def keep_alive():
    while True:
        try:
            requests.get(APP_URL)
            logging.info("Keep-alive ping sent.")
        except Exception as e:
            logging.error(f"Keep-alive failed: {e}")
        time.sleep(600)  # Ping every 10 minutes

# ---------------------------------------------------------------------------
# Feed-state helpers  (one MongoDB doc per feed, rolling seen_links array)
# ---------------------------------------------------------------------------

def _get_state(feed_url: str) -> list[str]:
    """Return the list of seen link URLs for a feed (empty list if not found)."""
    doc = feed_state_collection.find_one({"feed_url": feed_url}, {"seen_links": 1})
    return doc["seen_links"] if doc else []

def get_last_pub_ts(feed_url: str) -> int:
    """Return the last published timestamp (epoch seconds) for a feed."""
    doc = feed_state_collection.find_one({"feed_url": feed_url}, {"last_pub_ts": 1})
    return int(doc.get("last_pub_ts", 0)) if doc else 0

def set_last_pub_ts(feed_url: str, ts: int) -> None:
    """Persist the last published timestamp for a feed."""
    feed_state_collection.update_one(
        {"feed_url": feed_url},
        {"$set": {"last_pub_ts": int(ts)}},
        upsert=True,
    )

def get_entry_timestamp(entry) -> int | None:
    """Return the entry's timestamp (epoch seconds) or None if missing."""
    parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not parsed:
        return None
    try:
        return int(calendar.timegm(parsed))
    except Exception:
        return None

def is_new_entry(feed_url: str, entry, last_pub_ts: int) -> bool:
    """Return True if entry is newer than last_pub_ts (fallback to seen_links)."""
    entry_ts = get_entry_timestamp(entry)
    if entry_ts is not None:
        return entry_ts > last_pub_ts
    return not is_seen(feed_url, entry.link)

def dedupe_seen_links(links: list[str]) -> list[str]:
    """De-dupe while keeping the latest occurrence and limiting to MAX_SEEN_PER_FEED."""
    seen = set()
    deduped_rev = []
    for link in reversed(links):
        if link not in seen:
            seen.add(link)
            deduped_rev.append(link)
    deduped = list(reversed(deduped_rev))
    if len(deduped) > MAX_SEEN_PER_FEED:
        deduped = deduped[-MAX_SEEN_PER_FEED:]
    return deduped

def is_seen(feed_url: str, link: str) -> bool:
    """True if link was already seen/posted for this feed."""
    doc = feed_state_collection.find_one(
        {"feed_url": feed_url, "seen_links": link},
        {"_id": 1},
    )
    return doc is not None

def mark_seen(feed_url: str, link: str) -> None:
    """Add link to the feed's rolling seen_links array (max MAX_SEEN_PER_FEED).
    The feed_state document is created on first call and updated in-place thereafter.
    """
    feed_state_collection.update_one(
        {"feed_url": feed_url},
        [
            {
                "$set": {
                    "feed_url": feed_url,
                    "seen_links": {
                        "$slice": [
                            {
                                "$setUnion": [
                                    {"$ifNull": ["$seen_links", []]},
                                    [link],
                                ]
                            },
                            -MAX_SEEN_PER_FEED,
                        ]
                    },
                }
            }
        ],
        upsert=True,
    )

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def build_post_caption(entry) -> tuple[str, str | None]:
    """Return (caption_html, photo_url | None) for a feed entry."""
    soup = BeautifulSoup(getattr(entry, 'summary', ''), 'html.parser')
    img_tag = soup.find('img')
    photo_url = img_tag['src'] if img_tag else None
    description = soup.get_text()[:200].strip()
    if description:
        description += "..."

    # Title as headline — always at the very top so it's visible before "Show more"
    title_line = f"📰 <b>{entry.title}</b>"
    divider = "─" * 20
    body = description if description else ""
    link_line = f'🔗 <a href="{entry.link}">Read Full Article</a>'

    parts = [title_line, divider]
    if body:
        parts.append(body)
    parts.append(link_line)

    caption = "\n\n".join(parts)
    return caption, photo_url

async def publish_entry(bot: Bot, feed_url: str, entry) -> None:
    """Post a feed entry to the channel and update the feed's seen_links."""
    caption, photo_url = build_post_caption(entry)
    if photo_url:
        await bot.send_photo(chat_id=CHANNEL_ID, photo=photo_url, caption=caption, parse_mode='HTML')
    else:
        await bot.send_message(chat_id=CHANNEL_ID, text=caption, parse_mode='HTML')
    mark_seen(feed_url, entry.link)   # update in-place, no new document
    logging.info(f"Posted: {entry.title}")

# ---------------------------------------------------------------------------
# Auto-fetch (scheduled job)
# ---------------------------------------------------------------------------

async def fetch_and_post(bot: Bot):
    feeds = feeds_collection.find()
    for f in feeds:
        feed_url = f['url']
        feed = feedparser.parse(feed_url)
        last_pub_ts = get_last_pub_ts(feed_url)

        new_entries = [
            entry for entry in feed.entries
            if is_new_entry(feed_url, entry, last_pub_ts)
        ]
        if not new_entries:
            continue

        # Post oldest-to-newest to preserve timeline order.
        for entry in reversed(new_entries):
            try:
                await publish_entry(bot, feed_url, entry)
                entry_ts = get_entry_timestamp(entry)
                if entry_ts is not None:
                    set_last_pub_ts(feed_url, entry_ts)
            except Exception as e:
                logging.error(f"Error posting: {e}")
                # Avoid skipping timestamped entries on failure.
                if get_entry_timestamp(entry) is not None:
                    break

async def fetch_and_post_job(context: ContextTypes.DEFAULT_TYPE):
    await fetch_and_post(context.bot)

async def fetch_and_post_loop(application: Application):
    while True:
        await fetch_and_post(application.bot)
        await asyncio.sleep(300)

# ---------------------------------------------------------------------------
# /fetchpost — interactive manual post picker
# ---------------------------------------------------------------------------

@owner_only
async def fetchpost_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Step 1: show all registered feeds as inline buttons."""
    feeds = list(feeds_collection.find())
    if not feeds:
        await update.message.reply_text("⚠️ No feeds registered yet. Use /addfeed <url> to add one.")
        return ConversationHandler.END

    keyboard = []
    for i, f in enumerate(feeds):
        label = f['url'].replace("https://", "").replace("http://", "")[:45]
        keyboard.append([InlineKeyboardButton(f"📰 {label}", callback_data=f"feed:{i}")])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])

    # Store feeds list in user_data so we can look them up in the next step
    context.user_data['fetchpost_feeds'] = feeds

    await update.message.reply_text(
        "📡 <b>Select a feed to browse:</b>",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return SELECT_FEED


async def fetchpost_select_feed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Step 2: user picked a feed — show its latest 5 entries."""
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        await query.edit_message_text("❌ Cancelled.")
        return ConversationHandler.END

    feed_index = int(query.data.split(":")[1])
    feeds = context.user_data.get('fetchpost_feeds', [])
    feed_url = feeds[feed_index]['url']

    await query.edit_message_text(f"⏳ Fetching latest posts from:\n<code>{feed_url}</code>", parse_mode='HTML')

    parsed = feedparser.parse(feed_url)
    entries = parsed.entries[:5]

    if not entries:
        await query.edit_message_text("⚠️ No posts found in that feed.")
        return ConversationHandler.END

    # Store entries + feed_url for the next step
    context.user_data['fetchpost_entries'] = entries
    context.user_data['fetchpost_feed_url'] = feed_url

    last_pub_ts = get_last_pub_ts(feed_url)
    keyboard = []
    for i, entry in enumerate(entries):
        title = entry.title[:55] + ("…" if len(entry.title) > 55 else "")
        prefix = "🆕" if is_new_entry(feed_url, entry, last_pub_ts) else "✅"
        keyboard.append([InlineKeyboardButton(f"{prefix} {title}", callback_data=f"post:{i}")])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back")])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])

    await query.edit_message_text(
        "📝 <b>Select a post to publish to the channel:</b>\n"
        "<i>(✅ = already sent, 🆕 = new)</i>",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return SELECT_POST


async def fetchpost_select_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Step 3: user picked a post — publish it."""
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        await query.edit_message_text("❌ Cancelled.")
        return ConversationHandler.END

    if query.data == "back":
        # Re-show feed list
        feeds = context.user_data.get('fetchpost_feeds', [])
        keyboard = []
        for i, f in enumerate(feeds):
            label = f['url'].replace("https://", "").replace("http://", "")[:45]
            keyboard.append([InlineKeyboardButton(f"📰 {label}", callback_data=f"feed:{i}")])
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
        await query.edit_message_text(
            "📡 <b>Select a feed to browse:</b>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return SELECT_FEED

    post_index = int(query.data.split(":")[1])
    entries = context.user_data.get('fetchpost_entries', [])
    feed_url = context.user_data.get('fetchpost_feed_url', '')
    entry = entries[post_index]

    await query.edit_message_text(f"⏳ Publishing <b>{entry.title}</b>…", parse_mode='HTML')

    try:
        await publish_entry(query.message.get_bot(), feed_url, entry)
        await query.edit_message_text(f"✅ Published to channel:\n<b>{entry.title}</b>", parse_mode='HTML')
    except Exception as e:
        logging.error(f"fetchpost publish error: {e}")
        await query.edit_message_text(f"❌ Failed to publish:\n<code>{e}</code>", parse_mode='HTML')

    return ConversationHandler.END


@owner_only
async def fetchpost_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END

# ---------------------------------------------------------------------------
# Other commands
# ---------------------------------------------------------------------------

@owner_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 <b>Movie News Bot is Active!</b>\n\n"
        "Commands:\n"
        "• /fetchpost — manually pick &amp; publish a post\n"
        "• /seedfeeds — mark current feed entries as seen (no post) — run on fresh DB\n"
        "• /migratefeedstate — one-time dedupe + backfill pubDate state\n"
        "• /addfeed &lt;url&gt; — add an RSS feed\n"
        "• /listfeeds — list registered feeds\n"
        "• /removefeed &lt;url&gt; — remove a feed",
        parse_mode='HTML',
    )

@owner_only
async def add_feed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Usage: /addfeed <url>")
    url = context.args[0]
    if not feeds_collection.find_one({"url": url}):
        feeds_collection.insert_one({"url": url})
        await update.message.reply_text(f"✅ Added feed:\n<code>{url}</code>", parse_mode='HTML')
    else:
        await update.message.reply_text("⚠️ Feed already exists.")

@owner_only
async def list_feeds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    feeds = list(feeds_collection.find())
    if not feeds:
        return await update.message.reply_text("No feeds registered yet.")
    lines = [f"{i+1}. <code>{f['url']}</code>" for i, f in enumerate(feeds)]
    await update.message.reply_text("📋 <b>Registered feeds:</b>\n\n" + "\n".join(lines), parse_mode='HTML')

@owner_only
async def remove_feed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Usage: /removefeed <url>")
    url = context.args[0]
    result = feeds_collection.delete_one({"url": url})
    if result.deleted_count:
        await update.message.reply_text(f"🗑️ Removed feed:\n<code>{url}</code>", parse_mode='HTML')
    else:
        await update.message.reply_text("⚠️ Feed not found.")

@owner_only
async def seed_feeds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mark the latest entries from every feed as seen WITHOUT posting.
    Run this once on a fresh DB to avoid re-posting existing channel articles."""
    feeds = list(feeds_collection.find())
    if not feeds:
        return await update.message.reply_text("⚠️ No feeds registered yet.")

    status_msg = await update.message.reply_text("⏳ Seeding feeds — marking current entries as seen…")

    total_new = 0
    total_skip = 0
    total_last_ts = 0
    for f in feeds:
        feed_url = f['url']
        parsed = feedparser.parse(feed_url)
        max_ts = None
        for entry in parsed.entries:
            entry_ts = get_entry_timestamp(entry)
            if entry_ts is not None:
                if max_ts is None or entry_ts > max_ts:
                    max_ts = entry_ts
                continue

            if not is_seen(feed_url, entry.link):
                mark_seen(feed_url, entry.link)  # update in-place, no new document
                total_new += 1
            else:
                total_skip += 1

        if max_ts is not None:
            set_last_pub_ts(feed_url, max_ts)
            total_last_ts += 1

    await status_msg.edit_text(
        f"✅ <b>Seeding complete!</b>\n\n"
        f"• <b>{total_new}</b> entries marked as seen (will not be auto-posted)\n"
        f"• <b>{total_skip}</b> already recorded\n"
        f"• <b>{total_last_ts}</b> feeds stored last_pub_ts\n\n"
        f"The auto-scheduler will now only post <i>new</i> articles.",
        parse_mode='HTML',
    )

@owner_only
async def migrate_feed_state(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """One-time migration: dedupe seen_links and backfill last_pub_ts."""
    feeds = list(feeds_collection.find())
    if not feeds:
        return await update.message.reply_text("⚠️ No feeds registered yet.")

    status_msg = await update.message.reply_text("⏳ Migrating feed state…")

    deduped_docs = 0
    for doc in feed_state_collection.find({}, {"seen_links": 1}):
        seen_links = doc.get("seen_links", [])
        deduped = dedupe_seen_links(seen_links)
        if deduped != seen_links:
            feed_state_collection.update_one(
                {"_id": doc["_id"]},
                {"$set": {"seen_links": deduped}},
            )
            deduped_docs += 1

    ts_set = 0
    ts_missing = 0
    for f in feeds:
        feed_url = f['url']
        parsed = feedparser.parse(feed_url)
        max_ts = None
        for entry in parsed.entries:
            entry_ts = get_entry_timestamp(entry)
            if entry_ts is not None:
                if max_ts is None or entry_ts > max_ts:
                    max_ts = entry_ts
        if max_ts is not None:
            set_last_pub_ts(feed_url, max_ts)
            ts_set += 1
        else:
            ts_missing += 1

    await status_msg.edit_text(
        f"✅ <b>Migration complete!</b>\n\n"
        f"• <b>{deduped_docs}</b> feed_state docs deduped\n"
        f"• <b>{ts_set}</b> feeds stored last_pub_ts\n"
        f"• <b>{ts_missing}</b> feeds missing pubDate\n",
        parse_mode='HTML',
    )

# ---------------------------------------------------------------------------
# Flask + scheduler helpers
# ---------------------------------------------------------------------------

def run_flask():
    port = int(os.environ.get("PORT", os.environ.get("FLASK_PORT", "5000")))
    try:
        app.run(host="0.0.0.0", port=port)
    except OSError as exc:
        logging.error("Flask failed to start on port %s: %s", port, exc)

async def post_init(application: Application) -> None:
    """Called by PTB after the app is initialised, inside the running event loop."""
    job_queue = application.job_queue
    # Use a longer first-run delay so the operator has time to /seedfeeds
    # if the DB is empty and the channel already has existing posts.
    first_run_delay = 300
    if job_queue:
        job_queue.run_repeating(fetch_and_post_job, interval=300, first=first_run_delay)
        logging.info("Scheduled fetch_and_post via JobQueue every 5 minutes (first run in 5 min).")
    else:
        logging.warning("JobQueue not available. Using manual asyncio loop.")
        asyncio.get_event_loop().create_task(fetch_and_post_loop(application))

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Start Flask in a separate thread
    threading.Thread(target=run_flask, daemon=True).start()
    # Start Keep-alive in a separate thread
    if APP_URL:
        threading.Thread(target=keep_alive, daemon=True).start()

    application = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    # /fetchpost conversation
    fetchpost_handler = ConversationHandler(
        entry_points=[CommandHandler("fetchpost", fetchpost_start)],
        states={
            SELECT_FEED: [CallbackQueryHandler(fetchpost_select_feed)],
            SELECT_POST: [CallbackQueryHandler(fetchpost_select_post)],
        },
        fallbacks=[CommandHandler("cancel", fetchpost_cancel)],
        per_message=False,  # track per chat+user, not per message
    )

    application.add_handler(fetchpost_handler)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("addfeed", add_feed))
    application.add_handler(CommandHandler("listfeeds", list_feeds))
    application.add_handler(CommandHandler("removefeed", remove_feed))
    application.add_handler(CommandHandler("seedfeeds", seed_feeds))
    application.add_handler(CommandHandler("migratefeedstate", migrate_feed_state))

    application.run_polling()
