import os, re, time, asyncio, subprocess, signal
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
BASE_DIR = "/app/downloads"
ARIA2_CONF = "/app/aria2.conf"
TG_LIMIT = 2 * 1024**3

os.makedirs(BASE_DIR, exist_ok=True)

STATE = {
    "process": None,
    "paused": False,
    "cancel": False
}

# ---------- UI ----------

def bar(p, w=14):
    return "█" * int(w*p/100) + "░" * (w - int(w*p/100))

def buttons(paused=False):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "▶ Resume" if paused else "⏸ Pause",
                callback_data="resume" if paused else "pause"
            ),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel")
        ]
    ])

# ---------- Anime helpers ----------

def clean_name(name):
    name = re.sub(r"\[.*?\]", "", name)
    name = re.sub(r"\(.*?\)", "", name)
    return name.strip()

def episode_name(fname):
    m = re.search(r"(\d{1,2})", fname)
    return f"EP{int(m.group(1)):02d}" if m else "EP00"

def sort_anime(path):
    fname = os.path.basename(path)
    anime = clean_name(fname.split("-")[0])
    ep = episode_name(fname)

    target = f"{BASE_DIR}/{anime}/Season 01"
    os.makedirs(target, exist_ok=True)

    new = f"{target}/{ep}.mkv"
    os.rename(path, new)
    return new

# ---------- Controls ----------

async def control(update, context):
    q = update.callback_query
    await q.answer()

    if q.data == "pause" and STATE["process"]:
        STATE["paused"] = True
        STATE["process"].send_signal(signal.SIGSTOP)
        await q.edit_message_reply_markup(buttons(True))

    elif q.data == "resume" and STATE["process"]:
        STATE["paused"] = False
        STATE["process"].send_signal(signal.SIGCONT)
        await q.edit_message_reply_markup(buttons(False))

    elif q.data == "cancel":
        STATE["cancel"] = True
        if STATE["process"]:
            STATE["process"].kill()
        await q.edit_message_text("❌ Cancelled")
        cleanup()

# ---------- Download ----------

async def download(update, link):
    cmd = [
        "aria2c",
        "--conf-path", ARIA2_CONF,
        "--show-console-readout=true",
        "-d", BASE_DIR,
        link
    ]

    p = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )

    STATE["process"] = p
    msg = await update.message.reply_text("📥 Starting…", reply_markup=buttons())
    last = 0

    for line in p.stdout:
        if STATE["cancel"]:
            return

        if time.time() - last < 1.5:
            continue

        m = re.search(
            r"\[(.*?)\].*\((\d+)%\).*DL:([^\s]+).*ETA:([^\]]+)",
            line
        )

        if m:
            file = m.group(1)
            percent = int(m.group(2))
            speed = m.group(3)
            eta = m.group(4)

            text = (
                f"📦 {file}\n"
                f"{bar(percent)} {percent}%\n"
                f"🚀 {speed}/s | ⏳ {eta}"
            )

            try:
                await msg.edit_text(text, reply_markup=buttons(STATE["paused"]))
                last = time.time()
            except:
                pass

    p.wait()
    STATE["process"] = None
    await msg.edit_text("✅ Download complete")

# ---------- Upload ----------

async def upload(update, path):
    size = os.path.getsize(path)
    msg = await update.message.reply_text("📤 Uploading…")

    async def progress(cur, total):
        p = int(cur * 100 / total)
        await msg.edit_text(
            f"📤 Uploading\n{bar(p)} {p}%\n"
            f"{cur//1024//1024}MB / {total//1024//1024}MB"
        )

    await update.message.reply_document(
        document=open(path, "rb"),
        caption=os.path.basename(path),
        progress=progress
    )

    await msg.edit_text("✅ Uploaded")

# ---------- Cleanup ----------

def cleanup():
    subprocess.run(["rm", "-rf", BASE_DIR])
    os.makedirs(BASE_DIR, exist_ok=True)
    STATE.update({"process": None, "paused": False, "cancel": False})

# ---------- Handler ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send magnet / torrent\n"
        "• Episode-wise progress\n"
        "• Pause / Resume / Cancel\n"
        "• Auto cleanup"
    )

async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    link = update.message.text
    await download(update, link)

    for root, _, files in os.walk(BASE_DIR):
        for f in files:
            path = sort_anime(os.path.join(root, f))
            if os.path.getsize(path) <= TG_LIMIT:
                await upload(update, path)

    cleanup()
    await update.message.reply_text("🎉 All done")

# ---------- Run ----------

app = ApplicationBuilder().token(BOT_TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CallbackQueryHandler(control))
app.add_handler(MessageHandler(filters.TEXT, handle))
app.run_polling()