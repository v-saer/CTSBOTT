"""
Telegram bot that downloads video/audio from TikTok, Facebook, Instagram,
and YouTube links. Khmer (ខ្មែរ) interface only.

How it works:
- User sends a link from TikTok / Facebook / Instagram / YouTube.
- Bot shows inline buttons: "វីដេអូ" (Video) or "សំឡេង" (Audio).
- Video downloads normally via yt-dlp and gets sent back via Telegram.
- Audio is not live yet — tapping it shows a "coming soon" popup alert
  instead of downloading anything.
- Every user who talks to the bot is logged (name + Telegram ID) to a
  local JSON file (users.json) so you can see who has used the bot.
- After choosing Video, the bot stays silent (no status text) and just
  sends the file when it's done. It only sends a message if something
  actually goes wrong (private link, file too large, etc).
- Video downloads grab the highest quality the source will serve — no
  resolution cap by default — so you get the biggest/best version
  available from TikTok, Facebook, Instagram, or YouTube. Fragments
  download in parallel, multiple users are served concurrently instead of
  one-at-a-time, and aria2c is used automatically for multi-connection
  downloads if it's installed.

Requirements:
    pip install -r requirements.txt
    (ffmpeg is OPTIONAL. Audio downloads no longer need it — the bot grabs
    an already-encoded audio-only stream (m4a preferred) straight from the
    source and sends it as-is, no conversion step. ffmpeg is still used
    only for merging separate video+audio streams on some YouTube videos;
    if it's missing, video downloads just fall back to the best single
    progressive (already-merged) format instead of the highest possible
    resolution.)
    (aria2c is OPTIONAL but recommended for speed: if installed, yt-dlp
    uses it to download with 16 parallel connections per file instead of
    one. Install with e.g. `apt install aria2` / `brew install aria2`.
    Nothing breaks if it's absent — yt-dlp just uses its own downloader.)

Setup:
    1. Create a bot with @BotFather on Telegram, get the token.
    2. Open this file and paste your token into the BOT_TOKEN variable
       right below (in the "Config" section), replacing the empty string.
       No .env file or extra config file is needed — this script is fully
       self-contained.
    3. (Optional) Install ffmpeg and make sure it's on your PATH: run
       `ffmpeg -version` to confirm. Only needed for merging some high-res
       YouTube video downloads — audio downloads work fine without it.
    4. Run:
           python bot.py

Troubleshooting "downloads don't work":
    - Audio downloads do NOT need ffmpeg anymore — they grab a ready-made
      audio stream and send it unchanged. If audio still fails, it's a
      yt-dlp/site issue, not ffmpeg.
    - Video downloads can still benefit from ffmpeg (for merging separate
      high-res video+audio streams). Run `ffmpeg -version` to check; if
      it's missing, the bot just falls back to a lower, already-merged
      format automatically instead of failing.
    - Run `pip install -U yt-dlp` regularly. YouTube/Instagram/TikTok/
      Facebook change their internal APIs often, and an outdated yt-dlp is
      the most common cause of sudden breakage.
    - YouTube sometimes shows a "Sign in to confirm you're not a bot" style
      block for server IPs. This bot already asks yt-dlp to try mimicking
      the Android app client first, which avoids this in most cases. If it
      still happens for you, see the "Cookies" section in README.md.
    - Audio downloads specifically: the old code guessed the final file
      name by swapping the extension to ".mp3" after download. That guess
      could be wrong (e.g. if the mp3 postprocessor didn't run, or if the
      source was already an audio-only container), which made the bot
      report a successful download but then fail to find/send the file.
      This version instead reads the *actual* output path that yt-dlp
      reports back (via `requested_downloads`), which is accurate for both
      audio and video and fixes that failure.

Notes:
    - Telegram bots can only send files up to 50 MB via the normal Bot API.
      Larger videos will be rejected by Telegram; the bot will tell the user.
      Since video downloads now grab the highest quality available (no
      resolution cap), large/long videos can easily exceed 50 MB — set
      TELEGRAM_LOCAL_API_URL (see the config section) to raise this to
      2000 MB via a self-hosted Bot API server, or set MAX_VIDEO_HEIGHT
      (e.g. "1080") to trade max quality for smaller, more deliverable
      files on the public API.
    - Only download content you have the right to download / share. Respect
      each platform's Terms of Service and copyright law.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yt_dlp
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Paste your bot token from @BotFather directly here (between the quotes).
# No .env file is used — this single file is all you need to run the bot.
# You can still override it with an environment variable named
# TELEGRAM_BOT_TOKEN if you prefer that instead of editing this file.
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN",)

# Optional: point this at a self-hosted Telegram Bot API server
# (https://github.com/tdlib/telegram-bot-api) to raise the upload limit
# from 50 MB to 2000 MB (2 GB). Leave unset to use Telegram's public,
# cloud-hosted Bot API (50 MB limit). See README "Large files" section.
LOCAL_BOT_API_URL = os.environ.get("TELEGRAM_LOCAL_API_URL", "").rstrip("/")

# Effective per-file upload limit. Only raise this above 50 if
# TELEGRAM_LOCAL_API_URL is actually set and running — otherwise every
# "video" download over 50 MB will fail to send.
MAX_TELEGRAM_FILE_MB = int(
    os.environ.get("TELEGRAM_MAX_FILE_MB", "2000" if LOCAL_BOT_API_URL else "50")
)

# Optional: path to a Netscape-format cookies.txt file, used to help yt-dlp
# access age-restricted YouTube videos or dodge bot-detection walls.
COOKIES_FILE = os.environ.get("YTDLP_COOKIES_FILE", "")

# File that keeps a record of every user who has talked to the bot
# (their Telegram ID, name, username, and first/last seen timestamps).
USERS_FILE = Path(os.environ.get("BOT_USERS_FILE", "users.json"))
_USERS_LOCK = threading.Lock()

URL_PATTERN = re.compile(
    r"(https?://)?(www\.)?"
    r"(tiktok\.com|vt\.tiktok\.com|vm\.tiktok\.com|"
    r"instagram\.com|instagr\.am|"
    r"facebook\.com|fb\.watch|fb\.com|"
    r"youtube\.com|youtu\.be|music\.youtube\.com)"
    r"/\S+",
    re.IGNORECASE,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# In-memory store: {request_id: url}
# Keeps the callback_data short (Telegram limits it to 64 bytes).
PENDING_URLS: dict[str, str] = {}

FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None

# aria2c downloads with multiple parallel connections per file, which is
# noticeably faster than yt-dlp's default single-connection downloader,
# especially on TikTok/Facebook/Instagram CDNs. Fully optional — falls
# back to yt-dlp's built-in downloader automatically if not installed.
ARIA2C_AVAILABLE = shutil.which("aria2c") is not None

# Optional cap on video resolution. By default this is UNSET, meaning the
# bot always grabs the highest quality available from TikTok, Facebook,
# Instagram, and YouTube — no resolution ceiling. Set MAX_VIDEO_HEIGHT
# (e.g. "1080") if you'd rather trade max quality for faster/smaller
# downloads.
_max_height_env = os.environ.get("MAX_VIDEO_HEIGHT", "").strip()
MAX_VIDEO_HEIGHT = int(_max_height_env) if _max_height_env else None

# How many fragments (HLS/DASH chunks) yt-dlp downloads in parallel per
# file. Higher = faster on fragmented sources (common on FB/IG/YouTube),
# at the cost of more simultaneous connections.
CONCURRENT_FRAGMENTS = int(os.environ.get("CONCURRENT_FRAGMENTS", "8"))


# ---------------------------------------------------------------------------
# User logging (name + Telegram ID -> users.json)
# ---------------------------------------------------------------------------

def log_user(update: Update) -> None:
    """Record/refresh this user's name + Telegram ID in USERS_FILE.

    Keeps one JSON object per user, keyed by their Telegram user ID, with
    first_seen / last_seen timestamps. Safe to call on every incoming
    update; existing users just get their last_seen and name refreshed.
    """
    user = update.effective_user
    if user is None:
        return

    now = datetime.now(timezone.utc).isoformat()
    user_id = str(user.id)

    with _USERS_LOCK:
        data: dict = {}
        if USERS_FILE.exists():
            try:
                data = json.loads(USERS_FILE.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                logger.warning("Could not read %s, starting a fresh user log.", USERS_FILE)
                data = {}

        existing = data.get(user_id, {})
        data[user_id] = {
            "id": user.id,
            "name": (user.full_name or user.first_name or user.username or "Unknown"),
            "username": user.username,
            "first_seen": existing.get("first_seen", now),
            "last_seen": now,
        }

        try:
            USERS_FILE.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            logger.exception("Could not write %s", USERS_FILE)


# ---------------------------------------------------------------------------
# Translations
# ---------------------------------------------------------------------------

TEXT = {
    "km": {
        "welcome": (
            "សួស្តី {name}! លេខសម្គាល់ Telegram របស់អ្នក: {id}\n\n"
            "សូមផ្ញើតំណភ្ជាប់វីដេអូពី TikTok, Instagram, Facebook ឬ "
            "YouTube មកខ្ញុំ ហើយខ្ញុំនឹងទាញយកវីដេអូ ឬសំឡេងជូនអ្នក។\n\n"
            "គ្រាន់តែផ្ញើតំណភ្ជាប់មក មិនចាំបាច់ប្រើពាក្យបញ្ជាទេ។"
        ),
        "help": (
            "របៀបប្រើប្រាស់៖\n"
            "១. ចម្លងតំណភ្ជាប់វីដេអូសាធារណៈពី TikTok, Instagram, Facebook ឬ YouTube។\n"
            "២. ផ្ញើវាមកខ្ញុំនៅទីនេះ។\n"
            "៣. ជ្រើសរើស វីដេអូ ឬ សំឡេង នៅពេលខ្ញុំសួរ។\n\n"
            "ចំណាំ៖\n"
            f"• Telegram កំណត់ទំហំឯកសារបូតត្រឹម {MAX_TELEGRAM_FILE_MB} MB ក្នុងមួយឯកសារ។\n"
            "• មាតិកាឯកជន កំណត់អាយុ ឬត្រូវការចូលគណនីមិនអាចទាញយកបានទេ។\n"
            f"• បូតទាញយកគុណភាពល្អបំផុតដែលមាន ប៉ុន្តែឯកសារធំពេកអាចនឹងមិនអាចផ្ញើបាន ប្រសិនបើលើសកម្រិត {MAX_TELEGRAM_FILE_MB} MB។\n"
            "• សូមទាញយកតែមាតិកាដែលអ្នកមានសិទ្ធិប្រើប្រាស់ប៉ុណ្ណោះ។"
        ),
        "invalid_url": (
            "នេះមិនមែនជាតំណភ្ជាប់ពី TikTok, Instagram, Facebook ឬ YouTube ទេ។ "
            "សូមផ្ញើតំណភ្ជាប់វីដេអូសាធារណៈដែលត្រឹមត្រូវ។"
        ),
        "choose_format": "តើអ្នកចង់ទាញយកអ្វី?",
        "btn_video": "🎬 វីដេអូ",
        "btn_audio": "🎵 សំឡេង",
        "button_expired": "សំណើនេះបានផុតកំណត់ហើយ។ សូមផ្ញើតំណភ្ជាប់ម្តងទៀត។",
        "button_error": "មានបញ្ហាជាមួយប៊ូតុងនោះ។ សូមផ្ញើតំណភ្ជាប់ម្តងទៀត។",
        "coming_soon": "សូមរង់ចាំ... មុខងារទាញយកសំឡេងនឹងមកដល់ឆាប់ៗនេះ។",
        "mode_video": "វីដេអូ",
        "mode_audio": "សំឡេង",
        "error_private": (
            "សូមអភ័យទោស ខ្ញុំមិនអាចទាញយកបានទេ។ តំណនេះអាចជាមាតិកាឯកជន "
            "កំណត់អាយុ តំបន់ដែលមិនអនុញ្ញាត បានលុបចោល ឬវេទិកាកំពុងទប់ស្កាត់"
            "ការទាញយកស្វ័យប្រវត្តិជាបណ្តោះអាសន្ន។"
        ),
        "error_unexpected": "មានបញ្ហាមិនរំពឹងទុកកើតឡើង។ សូមព្យាយាមម្តងទៀតបន្តិចទៀត។",
        "error_ffmpeg_missing": (
            "ម៉ាស៊ីនមេខ្វះកម្មវិធី ffmpeg ដែលត្រូវការសម្រាប់ដំណើរការសំឡេង "
            "និងវីដេអូមួយចំនួន។ សូមស្នើឲ្យអ្នកគ្រប់គ្រងបូតដំឡើង ffmpeg។"
        ),
        "error_too_large": (
            "ឯកសារមានទំហំ {size} MB ដែលលើសកម្រិត "
            f"{MAX_TELEGRAM_FILE_MB} MB របស់ Telegram សម្រាប់បូត ដូច្នេះ"
            "ខ្ញុំមិនអាចផ្ញើវាបានទេ។ សូមសាកល្បងជាមួយ សំឡេងតែប៉ុណ្ណោះ ឬវីដេអូខ្លីជាង។"
        ),
        "error_send_failed": (
            "បានទាញយកឯកសារហើយ ប៉ុន្តែផ្ញើទៅ Telegram មិនជោគជ័យទេ។ "
            "សូមរងចាំបន្តិច"
        ),
    },
}


def get_lang(update: Update) -> str:
    """Bot is Khmer-only now — always returns 'km'."""
    return "km"


def t(lang: str, key: str, **kwargs) -> str:
    template = TEXT["km"][key]
    return template.format(**kwargs) if kwargs else template


# Slash-command menu (shown when the user types "/" in the chat).
COMMANDS_KM = [
    BotCommand("start", "ចាប់ផ្តើមប្រើប្រាស់ Bot"),
    BotCommand("help", "មើលជំនួយ"),
]


# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------

def build_ydl_opts(out_template: str, audio_only: bool) -> dict:
    """Build yt-dlp options for either a video or audio-only download."""
    opts = {
        "outtmpl": out_template,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "restrictfilenames": True,
        "geo_bypass": True,
        "retries": 5,
        "fragment_retries": 5,
        # Fail fast on a stalled/dead connection instead of hanging, and
        # retry immediately rather than waiting a growing back-off delay.
        "socket_timeout": 15,
        "retry_sleep_functions": {"http": lambda n: 1, "fragment": lambda n: 1},
        # Download multiple fragments of a video at once (HLS/DASH — very
        # common on Facebook/Instagram/YouTube). Big speed win on those.
        "concurrent_fragment_downloads": CONCURRENT_FRAGMENTS,
        "extractor_args": {
            "youtube": {
                # Trying the Android client first avoids most of the
                # "Sign in to confirm you're not a bot" throttling that
                # datacenter IPs hit with the default web client.
                "player_client": ["android", "web"],
            },
        },
    }

    if ARIA2C_AVAILABLE:
        # aria2c opens several parallel connections per file, which is
        # substantially faster than yt-dlp's single-connection downloader
        # for plain (non-fragmented) HTTP files — the common case for
        # TikTok and single-file Instagram/Facebook videos.
        opts["external_downloader"] = "aria2c"
        opts["external_downloader_args"] = {
            "aria2c": ["-x", "16", "-s", "16", "-k", "1M"]
        }

    if COOKIES_FILE and Path(COOKIES_FILE).exists():
        opts["cookiefile"] = COOKIES_FILE

    if audio_only:
        # No ffmpeg, no re-encoding: grab an audio-only stream that's
        # already in a playable container and send it exactly as-is.
        # Preference order: a source that's already .mp3 (some sites do
        # offer this natively) > m4a (Telegram's player handles it
        # cleanly) > whatever best audio-only stream exists (e.g.
        # webm/opus) as a last resort.
        #
        # Note: true "always mp3" output for every source is only
        # possible by re-encoding, which requires ffmpeg. Without ffmpeg
        # we can only send whatever container the source already has —
        # this is that best-effort version.
        opts["format"] = (
            "bestaudio[ext=mp3]/bestaudio[ext=m4a]/"
            "bestaudio[acodec^=mp4a]/bestaudio/best"
        )
    else:
        # Grab the absolute best quality available from the source (no
        # resolution ceiling by default) — the biggest/highest-quality
        # video these 4 platforms will actually serve. Prefer a single
        # progressive file when one exists (no merge needed). If ffmpeg is
        # available, allow merging separate best video+audio streams
        # (needed for the very highest qualities on YouTube, which often
        # serves top resolutions as separate video/audio); without
        # ffmpeg, stick to the best single already-merged (progressive)
        # format so nothing breaks.
        h = MAX_VIDEO_HEIGHT
        height_filter = f"[height<={h}]" if h else ""
        if FFMPEG_AVAILABLE:
            opts["format"] = (
                f"bestvideo{height_filter}+bestaudio/best{height_filter}/best"
            )
            opts["merge_output_format"] = "mp4"
        else:
            opts["format"] = f"best{height_filter}/best"

    return opts


def download_media(url: str, audio_only: bool, workdir: Path) -> Path:
    """Download the given URL with yt-dlp and return the resulting file path.

    Previously this guessed the final audio filename by swapping the
    extension to ".mp3" after download, which could be wrong (e.g. the
    postprocessor writing a different container, or being skipped) and was
    the main reason audio downloads sometimes silently failed to send.
    Instead, we now read the real output path(s) that yt-dlp itself reports
    via info["requested_downloads"], which reflects any postprocessing
    (audio extraction, video+audio merging) that actually happened.
    """
    out_template = str(workdir / "%(id)s.%(ext)s")
    ydl_opts = build_ydl_opts(out_template, audio_only)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

        # yt-dlp records the true final path(s) here, after any
        # postprocessing (mp3 extraction, mp4 merging, etc).
        requested = info.get("requested_downloads") or []
        for entry in requested:
            filepath = entry.get("filepath")
            if filepath and Path(filepath).exists():
                return Path(filepath)

        # Fallback for older yt-dlp versions / edge cases where
        # requested_downloads isn't populated as expected.
        filename = ydl.prepare_filename(info)
        result_path = Path(filename)
        if result_path.exists():
            return result_path

        # Last resort: find the actual output file by matching the video id
        # in the working directory (handles any extension yt-dlp settled on).
        video_id = info.get("id", "")
        matches = sorted(workdir.glob(f"{video_id}.*"))
        # Ignore leftover intermediate files (e.g. the pre-extraction audio
        # source that FFmpegExtractAudio keeps only if keepvideo is True).
        matches = [m for m in matches if m.suffix.lower() not in {".part", ".ytdl"}]
        if matches:
            return matches[-1]

    raise FileNotFoundError(f"Downloaded file not found for {url!r}")


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log_user(update)
    lang = get_lang(update)
    user = update.effective_user
    name = (user.first_name or user.username or "there") if user else "there"
    user_id = user.id if user else "unknown"
    await update.message.reply_text(t(lang, "welcome", name=name, id=user_id))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log_user(update)
    lang = get_lang(update)
    await update.message.reply_text(t(lang, "help"))


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log_user(update)
    lang = get_lang(update)
    text = update.message.text or ""
    match = URL_PATTERN.search(text)

    if not match:
        await update.message.reply_text(t(lang, "invalid_url"))
        return

    url = match.group(0)
    request_id = uuid.uuid4().hex[:12]
    PENDING_URLS[request_id] = url

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(t(lang, "btn_video"), callback_data=f"v:{request_id}"),
                InlineKeyboardButton(t(lang, "btn_audio"), callback_data=f"a:{request_id}"),
            ]
        ]
    )
    await update.message.reply_text(t(lang, "choose_format"), reply_markup=keyboard)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log_user(update)
    query = update.callback_query
    lang = get_lang(update)

    try:
        mode, request_id = query.data.split(":", 1)
    except ValueError:
        await query.answer()
        await query.edit_message_text(t(lang, "button_error"))
        return

    audio_only = mode == "a"

    # Audio download isn't live yet — show a native popup alert instead of
    # touching the URL/queue, and leave the buttons in place.
    if audio_only:
        await query.answer(t(lang, "coming_soon"), show_alert=True)
        return

    await query.answer()

    url = PENDING_URLS.pop(request_id, None)
    if not url:
        await query.edit_message_text(t(lang, "button_expired"))
        return

    mode_label = t(lang, "mode_video")

    # No status text — just clear the "choose format" buttons and go
    # straight to downloading, then send the file when it's ready.
    try:
        await query.delete_message()
    except Exception:
        pass

    chat_id = query.message.chat_id

    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        try:
            # Run the blocking yt-dlp download off the event loop, so the
            # bot can keep answering other users/buttons while this
            # download is in progress instead of freezing for everyone.
            filepath = await asyncio.to_thread(download_media, url, audio_only, workdir)
        except RuntimeError as e:
            if str(e) == "ffmpeg_missing":
                logger.error("ffmpeg is not installed/available on PATH.")
                await context.bot.send_message(chat_id=chat_id, text=t(lang, "error_ffmpeg_missing"))
            else:
                logger.exception("Runtime error downloading %s", url)
                await context.bot.send_message(chat_id=chat_id, text=t(lang, "error_unexpected"))
            return
        except yt_dlp.utils.DownloadError as e:
            logger.warning("Download failed for %s: %s", url, e)
            await context.bot.send_message(chat_id=chat_id, text=t(lang, "error_private"))
            return
        except Exception:
            logger.exception("Unexpected error downloading %s", url)
            await context.bot.send_message(chat_id=chat_id, text=t(lang, "error_unexpected"))
            return

        size_mb = filepath.stat().st_size / (1024 * 1024)
        if size_mb > MAX_TELEGRAM_FILE_MB:
            await context.bot.send_message(
                chat_id=chat_id,
                text=t(lang, "error_too_large", size=f"{size_mb:.1f}"),
            )
            return

        # No "ready" text — just show Telegram's native uploading
        # indicator (not a chat message) and send the file straight away.
        await context.bot.send_chat_action(
            chat_id=chat_id,
            action=ChatAction.UPLOAD_AUDIO if audio_only else ChatAction.UPLOAD_VIDEO,
        )

        try:
            with open(filepath, "rb") as f:
                if audio_only:
                    await context.bot.send_audio(chat_id=chat_id, audio=f)
                else:
                    await context.bot.send_video(chat_id=chat_id, video=f, supports_streaming=True)
        except Exception:
            logger.exception("Failed to send file for %s", url)
            await context.bot.send_message(chat_id=chat_id, text=t(lang, "error_send_failed"))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Update %s caused error: %s", update, context.error)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def post_init(app: Application) -> None:
    """Register the "/" command menu (Khmer only)."""
    await app.bot.set_my_commands(COMMANDS_KM)


def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE":
        raise SystemExit(
            "Missing bot token. Open bot.py and paste your token from "
            "@BotFather into the BOT_TOKEN variable near the top of the "
            "file (in the Config section), e.g.\n"
            '  BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "123456:ABC-your-token")'
        )

    if not FFMPEG_AVAILABLE:
        logger.warning(
            "ffmpeg was not found on PATH. Audio downloads work fine "
            "without it. Video downloads will fall back to a single "
            "already-merged format instead of the highest possible "
            "resolution. Run `ffmpeg -version` to check."
        )

    builder = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        # Handle multiple users' messages/downloads concurrently instead
        # of processing updates one at a time.
        .concurrent_updates(True)
    )

    if LOCAL_BOT_API_URL:
        # Point at a self-hosted Telegram Bot API server so uploads up to
        # 2 GB are allowed (the public cloud API caps bots at 50 MB).
        # See README "Large files" section for how to run one.
        builder = builder.base_url(f"{LOCAL_BOT_API_URL}/bot").base_file_url(
            f"{LOCAL_BOT_API_URL}/file/bot"
        )
        logger.info("Using local Bot API server at %s (limit: %s MB)", LOCAL_BOT_API_URL, MAX_TELEGRAM_FILE_MB)
    else:
        logger.info("Using Telegram's public Bot API (limit: %s MB)", MAX_TELEGRAM_FILE_MB)

    app = builder.build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_error_handler(error_handler)

    logger.info("Bot starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()