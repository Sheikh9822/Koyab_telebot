import os
import re
import time
import asyncio
import subprocess
import signal
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)

# ================= CONFIG =================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
BASE_DIR = "/app/downloads"
ARIA2_CONF = "/app/aria2.conf"
TG_LIMIT = 2 * 1024**3  # 2GB telegram limit

os.makedirs(BASE_DIR, exist_ok=True)

STATE = {
    "process": None,
    "paused": False,
    "cancel": False
}

# ================= HEALTH SERVER =================

def start_health_server():

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

        def log_message(self, *args):
            return  # silence logs

    server = HTTPServer(("0.0.0.0", 8000), Handler)
    server.serve_forever()


# ================= UI HELPERS =================

def progress_bar(percent, width=14):
    filled = int(width * percent / 100)
    return "█" * filled + "░" * (width - filled)


def control_buttons(paused=False):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "▶ Resume" if paused else "⏸ Pause",
                callback_data="resume" if paused else "pause"
            ),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel")
        ]
    ])


# ================= ANIME HELPERS =================

def clean_name(name):
    name = re.sub(r"\[.*?\]", "", name)
    name = re.sub(r"\(.*?\)", "", name)
    return name.strip()


def detect_episode(filename):
    match = re.search(r"\b(\d{1,2})\b", filename)
    if match:
        return f"EP{int(match.group(1)):02d}"
    return "EP00"


def sort_anime(path):
    filename = os.path.basename(path)
    anime = clean_name(filename.split("-")[0])
    episode = detect_episode(filename)

    target_dir = f"{BASE_DIR}/{anime}/Season 01"
    os.makedirs(target_dir, exist_ok=True)

    new_path = f"{target_dir}/{episode}.mkv"

    if not os.path.exists(new_path):
        os.rename(path, new_path)

    return new_path


# ================= CONTROLS =================

async def control_handler(update, context):

    query = update.callback_query
    await query.answer()

    if query.data == "pause" and STATE["process"]:
        STATE["paused"] = True
        STATE["process"].send_signal(signal.SIGSTOP)
        await query.edit_message_reply_markup(control_buttons(True))

    elif query.data == "resume" and STATE["process"]:
        STATE["paused"] = False
        STATE["process"].send_signal(signal.SIGCONT)
        await query.edit_message_reply_markup(control_buttons(False))

    elif query.data == "cancel":
        STATE["cancel"] = True
        if STATE["process"]:
            STATE["process"].kill()
        await query.edit_message_text("❌ Download cancelled")
        cleanup()


# ================= DOWNLOAD =================

async def download_torrent(update, link):

    cmd = [
        "aria2c",
        "--conf-path", ARIA2_CONF,
        "--show-console-readout=true",
        "-d", BASE_DIR,
        link
    ]

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )

    STATE["process"] = process

    msg = await update.message.reply_text(
        "📥 Starting download...",
        reply_markup=control_buttons()
    )

    last_update = 0

    for line in process.stdout:

        if STATE["cancel"]:
            return

        if time.time() - last_update < 1.5:
            continue

        match = re.search(
            r"\[(.*?)\].*\((\d+)%\).*DL:([^\s]+).*ETA:([^\]]+)",
            line
        )

        if match:
            file_name = match.group(1)
            percent = int(match.group(2))
            speed = match.group(3)
            eta = match.group(4)

            text = (
                f"📦 {file_name}\n"
                f"{progress_bar(percent)} {percent}%\n"
                f"🚀 {speed}/s | ⏳ {eta}"
            )

            try:
                await msg.edit_text(text, reply_markup=control_buttons(STATE["paused"]))
                last_update = time.time()
            except:
                pass

    process.wait()
    STATE["process"] = None

    await msg.edit_text("✅ Download complete")


# ================= UPLOAD =================

async def upload_file(update, file_path):

    size = os.path.getsize(file_path)

    msg = await update.message.reply_text("📤 Uploading...")

    async def progress(current, total):
        percent = int(current * 100 / total)

        try:
            await msg.edit_text(
                f"📤 Uploading\n"
                f"{progress_bar(percent)} {percent}%\n"
                f"{current//1024//1024}MB / {total//1024//1024}MB"
            )
        except:
            pass

    await update.message.reply_document(
        document=open(file_path, "rb"),
        caption=os.path.basename(file_path),
        progress=progress
    )

    await msg.edit_text("✅ Uploaded")


# ================= CLEANUP =================

def cleanup():
    subprocess.run(["rm", "-rf", BASE_DIR])
    os.makedirs(BASE_DIR, exist_ok=True)

    STATE.update({
        "process": None,
        "paused": False,
        "cancel": False
    })


# ================= HANDLERS =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "Send magnet or torrent link\n"
        "• Episode progress\n"
        "• Pause / Resume / Cancel\n"
        "• Auto cleanup"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):

    link = update.message.text.strip()

    await download_torrent(update, link)

    for root, _, files in os.walk(BASE_DIR):
        for file in files:

            file_path = os.path.join(root, file)
            file_path = sort_anime(file_path)

            if os.path.getsize(file_path) <= TG_LIMIT:
                await upload_file(update, file_path)

    cleanup()

    await update.message.reply_text("🎉 All uploads completed")


# ================= MAIN =================

def main():

    threading.Thread(target=start_health_server, daemon=True).start()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(control_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.run_polling()


if __name__ == "__main__":
    main()
