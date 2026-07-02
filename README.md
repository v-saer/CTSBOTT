# Telegram TikTok / Instagram / Facebook / YouTube Downloader Bot

A Python Telegram bot that downloads video or audio from TikTok, Instagram,
Facebook, and YouTube links, using [yt-dlp](https://github.com/yt-dlp/yt-dlp).

## Features
- Send any public TikTok / Instagram / Facebook / YouTube video link.
- Bot asks: **Video** or **Audio only** (MP3).
- Handles errors gracefully (private posts, oversized files, dead links).
- YouTube videos are automatically capped at 720p to help fit Telegram's
  50 MB file-size limit for bots.

## Requirements
- Python 3.10+
- [ffmpeg](https://ffmpeg.org/download.html) installed and on your `PATH`
  (needed for MP3 audio extraction)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

## Setup

```bash
# 1. Create and activate a virtual environment (recommended)
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Install ffmpeg
#    macOS:   brew install ffmpeg
#    Ubuntu:  sudo apt install ffmpeg
#    Windows: https://ffmpeg.org/download.html (add to PATH)

# 4. Set your bot token — either option works:
#
#    Option A: .env file (recommended)
cp .env.example .env
#    then open .env and paste your real token from @BotFather
#
#    Option B: environment variable
export TELEGRAM_BOT_TOKEN="123456:ABC-your-token-here"   # Windows: set TELEGRAM_BOT_TOKEN=...

# 5. Run the bot
python bot.py
```

The bot reads `TELEGRAM_BOT_TOKEN` from a `.env` file automatically (via
`python-dotenv`) if one exists, so you don't have to re-export it every time.
**Never commit your `.env` file or share your real token** — anyone with it
can control your bot.

Then open Telegram, find your bot, and send `/start`.

## Usage
1. Paste a public video link from TikTok, Instagram, Facebook, or YouTube.
2. Tap **🎬 Video** or **🎵 Audio only**.
3. Wait for the bot to send the file back.

## Limitations
- Telegram's Bot API caps file uploads from bots at **50 MB**. Larger videos
  can't be delivered this way (you'd need a self-hosted Telegram server /
  MTProto client library like Telethon/Pyrogram to lift that limit). YouTube
  videos are downloaded at up to 720p to help stay under this limit — very
  long videos may still exceed it.
- Private accounts, login-gated posts, age-restricted YouTube videos, or
  region-locked content generally can't be downloaded — this bot only
  accesses what's publicly available, same as visiting the link in a browser.
- Platforms change their internal APIs often. If downloads suddenly start
  failing, update yt-dlp: `pip install -U yt-dlp`.

## Legal note
Only download and redistribute content you own or have permission to use.
Respect each platform's Terms of Service and applicable copyright law —
this tool is provided for personal, legitimate use (e.g., saving your own
posts, or content you have explicit rights to download).

## Deploying long-term
For 24/7 uptime, run this on a small VPS or server with a process manager,
e.g.:
```bash
pip install supervisor   # or use systemd / pm2 / Docker
```
A minimal `systemd` service or Docker container both work well — ask if
you'd like a sample config for either.