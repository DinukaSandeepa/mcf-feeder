# Movie Series News Bot (RSS to Telegram)

An automation bot that pulls movie series news from RSS feeds, prevents duplicates with MongoDB, and posts rich updates (image + title + summary + link) to a private Telegram channel. It can run locally or as a Render Web Service.

## Features
- RSS ingestion from multiple feeds
- MongoDB-backed de-duplication
- Rich Telegram posts with images
- Telegram commands to manage feeds
- Health-check server for hosting platforms

## Tech Stack
- Python 3.10+
- MongoDB (Atlas or local)
- python-telegram-bot, feedparser, pymongo, flask, beautifulsoup4

## Prerequisites
1. Telegram bot token from [@BotFather](https://t.me/BotFather)
2. Private channel ID (bot must be an admin)
3. MongoDB connection string (Atlas or local)

## Local Deployment (venv)

### 1) Clone and create a virtual environment
```bash
git clone <your-repo-url>
cd mcf-feeder
python -m venv .venv
```

### 2) Activate the virtual environment
```bash
# macOS / Linux
source .venv/bin/activate

# Windows (PowerShell)
.venv\Scripts\Activate.ps1
```

### 3) Install dependencies
```bash
pip install -r requirements.txt
```

### 4) Set environment variables
Create a local env file or export variables in your shell.

```bash
export BOT_TOKEN="<telegram-bot-token>"
export CHANNEL_ID="-100xxxxxxxxxx"
export MONGO_URI="mongodb+srv://..."
export APP_URL="http://localhost:5000"
```

### 5) Run the bot
```bash
python main.py
```

## Environment Variables
| Name | Description | Example |
| --- | --- | --- |
| BOT_TOKEN | Telegram Bot API token | `123456:ABC...` |
| CHANNEL_ID | Private channel ID | `-1001234567890` |
| MONGO_URI | MongoDB connection string | `mongodb+srv://...` |
| APP_URL | Public URL for keep-alive | `https://my-bot.onrender.com` |

## Bot Commands
- `/start` - Check if the bot is running
- `/addfeed <url>` - Add a new RSS feed
- `/listfeeds` - List active RSS feeds
- `/removefeed <url>` - Remove a feed

## Deployment on Render
1. Create a new Web Service and connect this repo
2. Set environment variables: `BOT_TOKEN`, `CHANNEL_ID`, `MONGO_URI`, `APP_URL`
3. Build command: `pip install -r requirements.txt`
4. Start command: `python main.py`

## Troubleshooting
- If `pip` is not found, ensure your venv is activated
- If no messages are posted, confirm the bot is an admin in the channel
- If duplicate posts appear, verify MongoDB connectivity and permissions

## Notes
- RSS images are best-effort; some feeds may not include media
- Keep-alive assumes a reachable URL for the health-check endpoint
