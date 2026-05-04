import os
import time
import threading
import requests
import feedparser
import logging
from flask import Flask
from pymongo import MongoClient
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from bs4 import BeautifulSoup

# --- CONFIGURATION ---
TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
MONGO_URI = os.getenv("MONGO_URI")
APP_URL = os.getenv("APP_URL") # Used for keep-alive

# Setup Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# MongoDB Setup
client = MongoClient(MONGO_URI)
db = client['movie_news_bot']
sent_collection = db['sent_posts']
feeds_collection = db['rss_feeds']

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
        time.sleep(600) # Ping every 10 minutes

# --- CORE LOGIC ---
async def fetch_and_post(context: ContextTypes.DEFAULT_TYPE):
    feeds = feeds_collection.find()
    for f in feeds:
        url = f['url']
        feed = feedparser.parse(url)
        for entry in feed.entries[:3]: # Check last 3 items
            if not sent_collection.find_one({"link": entry.link}):
                # Extract Image from content or link
                soup = BeautifulSoup(getattr(entry, 'summary', ''), 'html.parser')
                img_tag = soup.find('img')
                photo_url = img_tag['src'] if img_tag else None
                
                # Get Description
                description = soup.get_text()[:150] + "..."
                
                caption = f"<b>{entry.title}</b>\n\n{description}\n\n<a href='{entry.link}'>Read More</a>"
                
                try:
                    if photo_url:
                        await context.bot.send_photo(chat_id=CHANNEL_ID, photo=photo_url, caption=caption, parse_mode='HTML')
                    else:
                        await context.bot.send_message(chat_id=CHANNEL_ID, text=caption, parse_mode='HTML')
                    
                    sent_collection.insert_one({"link": entry.link, "title": entry.title})
                    logging.info(f"Posted: {entry.title}")
                except Exception as e:
                    logging.error(f"Error posting: {e}")

# --- COMMANDS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Movie News Bot is Active!")

async def add_feed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Usage: /addfeed <url>")
    url = context.args[0]
    if not feeds_collection.find_one({"url": url}):
        feeds_collection.insert_one({"url": url})
        await update.message.reply_text(f"Added: {url}")
    else:
        await update.message.reply_text("Feed already exists.")

def run_flask():
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))

if __name__ == "__main__":
    # Start Flask in a separate thread
    threading.Thread(target=run_flask, daemon=True).start()
    # Start Keep-alive in a separate thread
    if APP_URL:
        threading.Thread(target=keep_alive, daemon=True).start()

    application = Application.builder().token(TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("addfeed", add_feed))
    
    # Run fetcher every 15 minutes
    job_queue = application.job_queue
    job_queue.run_repeating(fetch_and_post, interval=900, first=10)
    
    application.run_polling()
