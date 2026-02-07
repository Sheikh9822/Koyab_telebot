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

# --- GDRIVE SETUP ---
drive_service = None
SERVICE_ACCOUNT_JSON = os.environ.get("SERVICE_ACCOUNT_JSON")

if SERVICE_ACCOUNT_JSON:
    try:
        if os.path.exists("credentials.json"):
            os.remove("credentials.json")
        
        # Save JSON to file for libraries that need file path, or load dict directly
        cred_dict = json.loads(SERVICE_ACCOUNT_JSON)
        creds = service_account.Credentials.from_service_account_info(
            cred_dict, 
            scopes=['https://www.googleapis.com/auth/drive']
        )
        drive_service = build('drive', 'v3', credentials=creds)
        logger.info("✅ Google Drive Authenticated")
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

# --- BOT SETUP ---
app = Client("KoyebBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
active_tasks = {}
FILES_PER_PAGE = 8

# --- FUNCTIONS ---

def upload_to_gdrive(file_path, file_name):
    """Uploads a file to Google Drive (Supports Shared Drives)."""
    if not drive_service:
        return "Error: Service Account not configured."
    
    try:
        file_metadata = {
            'name': file_name,
            'parents': [GDRIVE_FOLDER_ID]
        }
        
        media = MediaFileUpload(file_path, resumable=True)
        
        # supportsAllDrives=True is CRITICAL for Shared Drives
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
        btns.append([InlineKeyboardButton(f"{icon} {file['name'][:30]}...", callback_data=f"tog_{h_hash}_{idx}_{page}")])
    
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
    await m.reply_text("👋 **Torrent to GDrive Bot**\n\n1. Send a Magnet Link.\n2. Select files.\n3. Bot downloads & uploads to Shared Drive.")

@app.on_message(filters.regex(r"magnet:\?xt=urn:btih:[a-zA-Z0-9]+"))
async def add_magnet(c, m):
    try:
        # Clean temp folder before starting new
        if os.path.exists(DOWNLOAD_DIR):
            shutil.rmtree(DOWNLOAD_DIR)
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)

        params = lt.parse_magnet_uri(m.text)
        params.save_path = DOWNLOAD_DIR
        
        handle = ses.add_torrent(params)
        for t in TRACKERS: handle.add_tracker({'url': t})
        
    except Exception as e:
        return await m.reply_text(f"❌ Error adding torrent: {e}")

    msg = await m.reply_text("⏳ **Fetching Metadata...**")
    
    # Wait for metadata
    start_time = time.time()
    while not handle.has_metadata():
        if time.time() - start_time > 60:
            ses.remove_torrent(handle)
            return await msg.edit("❌ Timeout fetching metadata. Try a better magnet.")
        await asyncio.sleep(2)
    
    info = handle.get_torrent_info()
    h_hash = str(handle.info_hash())
    
    files = []
    for i in range(info.num_files()):
        files.append({"name": os.path.basename(info.file_at(i).path), "size": info.file_at(i).size})

    # Initialize task
    active_tasks[h_hash] = {
        "handle": handle,
        "files": files,
        "selected": [],
        "chat_id": m.chat.id,
        "msg_id": msg.id,
        "cancel": False
    }
    
    # Pause all files initially (priority 0 = do not download)
    handle.prioritize_files([0] * info.num_files())
    
    await msg.edit(f"📂 **Metadata Found!**\nFiles: {len(files)}", reply_markup=gen_keyboard(h_hash))

@app.on_callback_query()
async def cb_handler(c, q):
    data = q.data.split("_")
    action = data[0]
    h_hash = data[1]
    
    task = active_tasks.get(h_hash)
    if not task:
        return await q.answer("Task expired or cancelled.", show_alert=True)
    
    if action == "tog":
        idx = int(data[2])
        page = int(data[3])
        if idx in task["selected"]:
            task["selected"].remove(idx)
        else:
            task["selected"].append(idx)
        await q.message.edit_reply_markup(gen_keyboard(h_hash, page))
    
    elif action == "page":
        await q.message.edit_reply_markup(gen_keyboard(h_hash, int(data[2])))
    
    elif action == "cancel":
        task["cancel"] = True
        ses.remove_torrent(task["handle"])
        active_tasks.pop(h_hash, None)
        await q.message.edit("❌ **Cancelled.**")
    
    elif action == "start":
        if not task["selected"]:
            return await q.answer("Select at least one file!", show_alert=True)
        
        await q.answer("Starting Download...")
        asyncio.create_task(downloader(c, h_hash))

async def downloader(c, h_hash):
    task = active_tasks[h_hash]
    handle = task["handle"]
    info = handle.get_torrent_info()
    
    # Prioritize selected files
    for idx in task["selected"]:
        handle.file_priority(idx, 4) # 4 = top priority
    
    # Download Loop
    while not handle.is_seed():
        if task["cancel"]: return
        
        s = handle.status()
        
        # Calculate progress specifically for selected files
        total_needed = sum(task["files"][i]["size"] for i in task["selected"])
        file_progs = handle.file_progress()
        downloaded = sum(file_progs[i] for i in task["selected"])
        
        pct = (downloaded / total_needed * 100) if total_needed > 0 else 0
        
        txt = (f"📥 **Downloading...**\n"
               f"[{get_prog_bar(pct)}] {pct:.1f}%\n"
               f"⚡ Speed: {humanize.naturalsize(s.download_rate)}/s\n"
               f"👥 Peers: {s.num_peers}")
        
        try:
            await c.edit_message_text(task["chat_id"], task["msg_id"], txt, 
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{h_hash}")]]))
        except: pass
        
        if downloaded >= total_needed:
            break
            
        await asyncio.sleep(4)
        
    # Upload Loop
    await c.edit_message_text(task["chat_id"], task["msg_id"], "📤 **Download done. Starting Upload...**")
    
    uploaded_count = 0
    
    for idx in task["selected"]:
        if task["cancel"]: break
        
        file_info = info.file_at(idx)
        # Handle subfolders in torrents by taking just the filename for display
        display_name = os.path.basename(file_info.path) 
        # But use full path to locate file on disk
        local_path = os.path.join(DOWNLOAD_DIR, file_info.path)
        
        if not os.path.exists(local_path):
            await c.send_message(task["chat_id"], f"⚠️ Error: File not found on disk: `{display_name}`")
            continue

        await c.edit_message_text(task["chat_id"], task["msg_id"], f"☁️ **Uploading:** `{display_name}`")
        
        # Run blocking upload in executor
        loop = asyncio.get_event_loop()
        link = await loop.run_in_executor(None, upload_to_gdrive, local_path, display_name)
        
        if "http" in str(link):
            msg = f"✅ **{display_name}**\n🔗 [GDrive Link]({link})"
            if INDEX_URL:
                url_name = display_name.replace(" ", "%20")
                msg += f"\n⚡ [Direct Link]({INDEX_URL}/{url_name})"
            await c.send_message(task["chat_id"], msg, disable_web_page_preview=True)
            uploaded_count += 1
        else:
            await c.send_message(task["chat_id"], f"❌ Upload Failed for `{display_name}`: {link}")
    
    # Cleanup
    ses.remove_torrent(handle)
    active_tasks.pop(h_hash, None)
    
    if os.path.exists(DOWNLOAD_DIR):
        shutil.rmtree(DOWNLOAD_DIR)
        
    await c.send_message(task["chat_id"], f"🏁 **Task Finished.** Uploaded {uploaded_count} files.")

if __name__ == "__main__":
    app.run()
