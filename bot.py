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
from googleapiclient.discovery import build
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.http import MediaFileUpload

# --- CONFIGURATION ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID", "")
INDEX_URL = os.environ.get("INDEX_URL", "").rstrip('/')
DOWNLOAD_DIR = "/app/downloads/"

# Ensure download directory exists
Path(DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)

# --- GDRIVE AUTHENTICATION ---
drive_service = None
TOKEN_JSON = os.environ.get("TOKEN_JSON")             # Method 1: Personal Account (The fix)
SERVICE_ACCOUNT_JSON = os.environ.get("SERVICE_ACCOUNT_JSON") # Method 2: Shared Drive

try:
    if TOKEN_JSON:
        # Load credentials from the Token JSON you generated
        # This logs in as YOU, solving the quota issue
        info = json.loads(TOKEN_JSON)
        creds = Credentials.from_authorized_user_info(info, ['https://www.googleapis.com/auth/drive'])
        drive_service = build('drive', 'v3', credentials=creds)
        logger.info("✅ Google Drive Authenticated (User Mode)")
        
    elif SERVICE_ACCOUNT_JSON:
        # Fallback for Shared Drive users
        cred_dict = json.loads(SERVICE_ACCOUNT_JSON)
        creds = service_account.Credentials.from_service_account_info(
            cred_dict, scopes=['https://www.googleapis.com/auth/drive'])
        drive_service = build('drive', 'v3', credentials=creds)
        logger.info("✅ Google Drive Authenticated (Service Account)")
    else:
        logger.warning("⚠️ No Google Drive Credentials found! Uploads will fail.")
        
except Exception as e:
    logger.error(f"❌ GDrive Auth Failed: {e}")

# --- LIBTORRENT SETUP ---
ses = lt.session()
ses.listen_on(6881, 6891)
ses.apply_settings({'connection_speed': 100})

TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://9.rarbg.me:2960/announce",
    "udp://tracker.tiny-vps.com:6969/announce"
]

app = Client("KoyebBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
active_tasks = {}
FILES_PER_PAGE = 8

# --- HELPERS ---

def upload_to_gdrive(file_path, file_name):
    if not drive_service:
        return "Error: GDrive not configured."
    
    try:
        file_metadata = {'name': file_name, 'parents': [GDRIVE_FOLDER_ID]}
        media = MediaFileUpload(file_path, resumable=True)
        
        request = drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id, webViewLink',
            supportsAllDrives=True
        )
        
        response = None
        while response is None:
            status, response = request.next_chunk()
        
        return response.get('webViewLink')
    except Exception as e:
        return f"Upload Error: {str(e)}"

def get_prog_bar(pct):
    p = int(pct / 10)
    return "█" * p + "░" * (10 - p)

def gen_keyboard(h_hash, page=0):
    task = active_tasks.get(h_hash)
    if not task: return None
    
    files = task["files"]
    selected = task["selected"]
    start = page * FILES_PER_PAGE
    end = start + FILES_PER_PAGE
    
    btns = []
    for i, file in enumerate(files[start:end]):
        idx = start + i
        icon = "✅" if idx in selected else "⬜"
        # Truncate long names for button labels
        name = file['name']
        if len(name) > 30: name = name[:15] + "..." + name[-10:]
        btns.append([InlineKeyboardButton(f"{icon} {name}", callback_data=f"tog_{h_hash}_{idx}_{page}")])
    
    nav = []
    if page > 0: nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"page_{h_hash}_{page-1}"))
    if end < len(files): nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"page_{h_hash}_{page+1}"))
    if nav: btns.append(nav)
    
    btns.append([InlineKeyboardButton("🚀 START DOWNLOAD", callback_data=f"start_{h_hash}")])
    btns.append([InlineKeyboardButton("❌ CANCEL", callback_data=f"cancel_{h_hash}")])
    return InlineKeyboardMarkup(btns)

# --- HANDLERS ---

@app.on_message(filters.command("start"))
async def start(c, m):
    await m.reply_text("👋 **Torrent to GDrive Bot**\nSend a Magnet Link to start.")

@app.on_message(filters.regex(r"magnet:\?xt=urn:btih:[a-zA-Z0-9]+"))
async def add_magnet(c, m):
    try:
        # Clean up old downloads
        if os.path.exists(DOWNLOAD_DIR): 
            shutil.rmtree(DOWNLOAD_DIR)
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)

        params = lt.parse_magnet_uri(m.text)
        params.save_path = DOWNLOAD_DIR
        handle = ses.add_torrent(params)
        for t in TRACKERS: handle.add_tracker({'url': t})
        
    except Exception as e:
        return await m.reply_text(f"❌ Error: {e}")

    msg = await m.reply_text("⏳ **Fetching Metadata...**")
    
    start_time = time.time()
    while not handle.has_metadata():
        if time.time() - start_time > 60:
            ses.remove_torrent(handle)
            return await msg.edit("❌ Metadata timeout. Try a better magnet link.")
        await asyncio.sleep(2)
    
    info = handle.get_torrent_info()
    h_hash = str(handle.info_hash())
    
    files = []
    for i in range(info.num_files()):
        files.append({"name": os.path.basename(info.file_at(i).path), "size": info.file_at(i).size})

    active_tasks[h_hash] = {
        "handle": handle, "files": files, "selected": [],
        "chat_id": m.chat.id, "msg_id": msg.id, "cancel": False
    }
    
    # Pause all initially
    handle.prioritize_files([0] * info.num_files())
    await msg.edit(f"📂 **Metadata Found!**\nFiles: {len(files)}", reply_markup=gen_keyboard(h_hash))

@app.on_callback_query()
async def cb_handler(c, q):
    data = q.data.split("_")
    action, h_hash = data[0], data[1]
    task = active_tasks.get(h_hash)
    
    if not task: return await q.answer("Task expired.", show_alert=True)
    
    if action == "tog":
        idx, page = int(data[2]), int(data[3])
        if idx in task["selected"]: task["selected"].remove(idx)
        else: task["selected"].append(idx)
        await q.message.edit_reply_markup(gen_keyboard(h_hash, page))
    
    elif action == "page":
        await q.message.edit_reply_markup(gen_keyboard(h_hash, int(data[2])))
    
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
    info = handle.get_torrent_info()
    
    # Set priority to 4 (Download)
    for idx in task["selected"]: handle.file_priority(idx, 4)
    
    while not handle.is_seed():
        if task["cancel"]: return
        s = handle.status()
        
        # Calculate specific progress
        total = sum(task["files"][i]["size"] for i in task["selected"])
        done = sum(handle.file_progress()[i] for i in task["selected"])
        pct = (done / total * 100) if total > 0 else 0
        
        try:
            await c.edit_message_text(
                task["chat_id"], task["msg_id"],
                f"📥 **Downloading...**\n[{get_prog_bar(pct)}] {pct:.1f}%\n⚡ {humanize.naturalsize(s.download_rate)}/s",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{h_hash}")]]))
            )
        except: pass
        
        if done >= total: break
        await asyncio.sleep(4)
        
    await c.edit_message_text(task["chat_id"], task["msg_id"], "📤 **Uploading to Drive...**")
    
    for idx in task["selected"]:
        if task["cancel"]: break
        f_info = info.file_at(idx)
        name = os.path.basename(f_info.path)
        path = os.path.join(DOWNLOAD_DIR, f_info.path)
        
        if os.path.exists(path):
            await c.edit_message_text(task["chat_id"], task["msg_id"], f"☁️ **Uploading:** `{name}`")
            loop = asyncio.get_event_loop()
            link = await loop.run_in_executor(None, upload_to_gdrive, path, name)
            
            if "http" in str(link):
                msg = f"✅ **{name}**\n🔗 [GDrive Link]({link})"
                if INDEX_URL:
                    msg += f"\n⚡ [Direct Link]({INDEX_URL}/{name.replace(' ', '%20')})"
                await c.send_message(task["chat_id"], msg, disable_web_page_preview=True)
            else:
                await c.send_message(task["chat_id"], f"❌ Upload Failed: {link}")

    ses.remove_torrent(handle)
    active_tasks.pop(h_hash, None)
    if os.path.exists(DOWNLOAD_DIR): shutil.rmtree(DOWNLOAD_DIR)
    await c.send_message(task["chat_id"], "🏁 **Task Finished.**")

if __name__ == "__main__":
    app.run()
