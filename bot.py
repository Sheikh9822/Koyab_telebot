import os
import asyncio
import time
import json
import logging
import shutil
import libtorrent as lt
import humanize
from pathlib import Path
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.errors import MessageNotModified, FloodWait
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from googleapiclient.http import MediaFileUpload

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID", "")
INDEX_URL = os.environ.get("INDEX_URL", "").rstrip('/')
DOWNLOAD_DIR = "/app/downloads/"

Path(DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)

# --- GDRIVE AUTHENTICATION ---
drive_service = None
TOKEN_JSON = os.environ.get("TOKEN_JSON")

try:
    if TOKEN_JSON:
        info = json.loads(TOKEN_JSON)
        creds = Credentials.from_authorized_user_info(info, ['https://www.googleapis.com/auth/drive'])
        drive_service = build('drive', 'v3', credentials=creds, cache_discovery=False)
        logger.info("✅ Google Drive Authenticated")
except Exception as e:
    logger.error(f"❌ GDrive Auth Failed: {e}")

# --- LIBTORRENT SETUP ---
ses = lt.session({'listen_interfaces': '0.0.0.0:6881'})

TRACKERS = ["udp://tracker.opentrackr.org:1337/announce", "udp://open.stealth.si:80/announce", "udp://exodus.desync.com:6969/announce"]

app = Client("KoyebBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
active_tasks = {}
FILES_PER_PAGE = 8

def upload_to_gdrive(file_path, file_name):
    if not drive_service: return "Error: Drive not configured."
    try:
        meta = {'name': file_name, 'parents': [GDRIVE_FOLDER_ID]}
        media = MediaFileUpload(file_path, resumable=True)
        request = drive_service.files().create(body=meta, media_body=media, fields='id, webViewLink', supportsAllDrives=True)
        response = None
        while response is None:
            _, response = request.next_chunk()
        return response.get('webViewLink')
    except Exception as e:
        return f"Upload Error: {str(e)}"

def get_prog_bar(pct):
    p = int(pct / 10)
    return "█" * p + "░" * (10 - p)

def gen_keyboard(h_hash, page=0):
    task = active_tasks.get(h_hash)
    if not task: return None
    files, selected = task["files"], task["selected"]
    start, end = page * FILES_PER_PAGE, (page + 1) * FILES_PER_PAGE
    btns = []
    for i, file in enumerate(files[start:end]):
        idx = start + i
        icon = "✅" if idx in selected else "⬜"
        name = file['name']
        display_name = (name[:15] + "..." + name[-10:]) if len(name) > 30 else name
        btns.append([InlineKeyboardButton(f"{icon} {display_name}", callback_data=f"tog_{h_hash}_{idx}_{page}")])
    nav = []
    if page > 0: nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"page_{h_hash}_{page-1}"))
    if end < len(files): nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"page_{h_hash}_{page+1}"))
    if nav: btns.append(nav)
    btns.append([InlineKeyboardButton("🚀 START DOWNLOAD", callback_data=f"start_{h_hash}")])
    btns.append([InlineKeyboardButton("❌ CANCEL", callback_data=f"cancel_{h_hash}")])
    return InlineKeyboardMarkup(btns)

@app.on_message(filters.command("start"))
async def start_cmd(c, m):
    await m.reply_text("👋 **Torrent to GDrive Bot**\nSend a Magnet Link.")

@app.on_message(filters.regex(r"magnet:\?xt=urn:btih:[a-zA-Z0-9]+"))
async def handle_magnet(c, m):
    try:
        params = lt.parse_magnet_uri(m.text)
        params.save_path = DOWNLOAD_DIR
        handle = ses.add_torrent(params)
        for t in TRACKERS: handle.add_tracker({'url': t})
    except Exception as e:
        return await m.reply_text(f"❌ Error: {e}")

    msg = await m.reply_text("⏳ **Fetching Metadata...**")
    while not handle.status().has_metadata:
        await asyncio.sleep(2)
    
    t_info = handle.status().torrent_file
    h_hash = str(handle.info_hash())
    files = [{"name": t_info.files().file_name(i), "size": t_info.files().file_size(i)} for i in range(t_info.num_files())]

    active_tasks[h_hash] = {"handle": handle, "files": files, "selected": [], "chat_id": m.chat.id, "msg_id": msg.id, "cancel": False}
    handle.prioritize_files([0] * t_info.num_files())
    try: await msg.edit(f"📂 **Metadata Found!**\nFiles: {len(files)}", reply_markup=gen_keyboard(h_hash))
    except MessageNotModified: pass

@app.on_callback_query()
async def cb_handler(c, q: CallbackQuery):
    data = q.data.split("_")
    action, h_hash = data[0], data[1]
    task = active_tasks.get(h_hash)
    if not task: return await q.answer("Task expired.", show_alert=True)
    if action == "tog":
        idx, page = int(data[2]), int(data[3])
        if idx in task["selected"]: task["selected"].remove(idx)
        else: task["selected"].append(idx)
        try: await q.message.edit_reply_markup(gen_keyboard(h_hash, page))
        except MessageNotModified: pass
    elif action == "page":
        try: await q.message.edit_reply_markup(gen_keyboard(h_hash, int(data[2])))
        except MessageNotModified: pass
    elif action == "cancel":
        task["cancel"] = True
        ses.remove_torrent(task["handle"])
        active_tasks.pop(h_hash, None)
        await q.message.edit("❌ Cancelled.")
    elif action == "start":
        if not task["selected"]: return await q.answer("Select a file!", show_alert=True)
        await q.answer("Starting...")
        asyncio.create_task(downloader(c, h_hash))

async def downloader(c, h_hash):
    task = active_tasks[h_hash]
    handle = task["handle"]
    for idx in task["selected"]: handle.file_priority(idx, 4)
    
    while not handle.status().is_seeding:
        if task["cancel"]: return
        s = handle.status()
        total = sum(task["files"][i]["size"] for i in task["selected"])
        done = sum(handle.file_progress()[i] for i in task["selected"])
        pct = (done / total * 100) if total > 0 else 0
        try:
            await c.edit_message_text(task["chat_id"], task["msg_id"], f"📥 **Downloading...**\n[{get_prog_bar(pct)}] {pct:.1f}%\n⚡ {humanize.naturalsize(s.download_rate)}/s", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{h_hash}")]]))
        except (MessageNotModified, FloodWait): pass
        if done >= total: break
        await asyncio.sleep(5)
        
    await c.edit_message_text(task["chat_id"], task["msg_id"], "📤 **Uploading to Drive...**")
    t_file = handle.status().torrent_file
    for idx in task["selected"]:
        if task["cancel"]: break
        name, path = t_file.files().file_name(idx), os.path.join(DOWNLOAD_DIR, t_file.files().file_path(idx))
        if os.path.exists(path):
            try: await c.edit_message_text(task["chat_id"], task["msg_id"], f"☁️ **Uploading:** `{name}`")
            except MessageNotModified: pass
            link = await asyncio.get_event_loop().run_in_executor(None, upload_to_gdrive, path, name)
            if "http" in str(link):
                msg = f"✅ **{name}**\n🔗 [GDrive Link]({link})"
                if INDEX_URL: msg += f"\n⚡ [Direct Link]({INDEX_URL}/{name.replace(' ', '%20')})"
                await c.send_message(task["chat_id"], msg, disable_web_page_preview=True)
            else: await c.send_message(task["chat_id"], f"❌ Upload Failed: {link}")

    ses.remove_torrent(handle)
    active_tasks.pop(h_hash, None)
    if os.path.exists(DOWNLOAD_DIR): shutil.rmtree(DOWNLOAD_DIR)
    await c.send_message(task["chat_id"], "🏁 **Task Finished.**")

if __name__ == "__main__":
    app.run()
