import os
import asyncio
import time
import json
import libtorrent as lt
import humanize
import logging
from pathlib import Path
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.errors import FloodWait
from googleapiclient.discovery import build
from google.oauth2 import service_account
from googleapiclient.http import MediaFileUpload

# Logging
logging.basicConfig(level=logging.INFO)

# --- 1. CONFIGURATION ---
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID", "")
INDEX_URL = os.environ.get("INDEX_URL", "").rstrip('/')
DOWNLOAD_DIR = "./downloads/"

# Ensure download directory exists
Path(DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)

# Service Account Setup
drive_service = None
SERVICE_ACCOUNT_JSON = os.environ.get("SERVICE_ACCOUNT_JSON")

if SERVICE_ACCOUNT_JSON:
    try:
        info = json.loads(SERVICE_ACCOUNT_JSON)
        creds = service_account.Credentials.from_service_account_info(info, scopes=['https://www.googleapis.com/auth/drive'])
        drive_service = build('drive', 'v3', credentials=creds)
        logging.info("GDrive Service Account Authenticated.")
    except Exception as e:
        logging.error(f"GDrive Auth Error: {e}")

# --- 2. TORRENT ENGINE SETUP ---
ses = lt.session()
ses.listen_on(6881, 6891)

TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce", 
    "udp://open.stealth.si:80/announce",
    "udp://exodus.desync.com:6969/announce"
]

app = Client("GDriveTorrentBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
active_tasks = {}
FILES_PER_PAGE = 8

# --- 3. HELPERS ---

def upload_to_gdrive(file_path, file_name):
    if not drive_service:
        return "Error: GDrive Service not initialized."
    try:
        file_metadata = {'name': file_name, 'parents': [GDRIVE_FOLDER_ID]}
        media = MediaFileUpload(file_path, resumable=True)
        request = drive_service.files().create(body=file_metadata, media_body=media, fields='id, webViewLink')
        
        response = None
        while response is None:
            status, response = request.next_chunk()
        return response.get('webViewLink')
    except Exception as e:
        return f"Error: {str(e)}"

def get_prog_bar(pct):
    pct = float(pct)
    p = int(pct / 10)
    return "█" * p + "░" * (10 - p)

def gen_selection_kb(h_hash, page=0):
    task = active_tasks[h_hash]
    files = task["files"]
    selected = task["selected"]
    start = page * FILES_PER_PAGE
    end = start + FILES_PER_PAGE
    btns = []
    for i, file in enumerate(files[start:end]):
        idx = start + i
        icon = "✅" if idx in selected else "⬜"
        btns.append([InlineKeyboardButton(f"{icon} {file['name'][:35]}", callback_data=f"tog_{h_hash}_{idx}_{page}")])
    
    nav = []
    if page > 0: nav.append(InlineKeyboardButton("⬅️ Back", callback_data=f"page_{h_hash}_{page-1}"))
    if end < len(files): nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"page_{h_hash}_{page+1}"))
    if nav: btns.append(nav)
    
    btns.append([InlineKeyboardButton("🚀 START DOWNLOAD", callback_data=f"startdl_{h_hash}")])
    btns.append([InlineKeyboardButton("❌ CANCEL", callback_data=f"ca_{h_hash}")])
    return InlineKeyboardMarkup(btns)

# --- 4. BOT HANDLERS ---

@app.on_message(filters.command("start"))
async def start(c, m):
    await m.reply_text("👋 **Torrent to GDrive Bot**\nSend a magnet link to begin.")

@app.on_message(filters.regex(r"magnet:\?xt=urn:btih:[a-zA-Z0-9]+"))
async def handle_magnet(c, m):
    try:
        params = lt.parse_magnet_uri(m.text)
        params.save_path = DOWNLOAD_DIR
        handle = ses.add_torrent(params)
        for t in TRACKERS: handle.add_tracker({'url': t})
    except Exception as e:
        return await m.reply_text(f"❌ Error: {e}")

    msg = await m.reply_text("🧲 **Fetching Metadata...**")
    
    # Wait for metadata (Libtorrent 2.0 compatible)
    while not handle.has_metadata():
        await asyncio.sleep(2)
    
    info = handle.get_torrent_info()
    h_hash = str(handle.info_hash())
    
    files = []
    for i in range(info.num_files()):
        files.append({
            "name": info.file_at(i).path, # Keeping full path
            "size": info.file_at(i).size
        })

    active_tasks[h_hash] = {
        "handle": handle, "selected": [], "files": files,
        "chat_id": m.chat.id, "msg_id": msg.id, "cancel": False
    }
    
    # Pause all files initially
    handle.prioritize_files([0] * info.num_files())
    await msg.edit("✅ Metadata found! Select files to download:", reply_markup=gen_selection_kb(h_hash))

@app.on_callback_query(filters.regex(r"^(tog|page|startdl|ca|pa|re)_"))
async def callbacks(c, q: CallbackQuery):
    data = q.data.split("_")
    action, h_hash = data[0], data[1]
    task = active_tasks.get(h_hash)
    if not task: return await q.answer("Task Expired.", show_alert=True)

    if action == "tog":
        idx, p = int(data[2]), int(data[3])
        if idx in task["selected"]: task["selected"].remove(idx)
        else: task["selected"].append(idx)
        await q.message.edit_reply_markup(gen_selection_kb(h_hash, p))
    
    elif action == "page":
        await q.message.edit_reply_markup(gen_selection_kb(h_hash, int(data[2])))
    
    elif action == "startdl":
        if not task["selected"]: return await q.answer("Select at least one file!")
        await q.answer("Starting...")
        asyncio.create_task(run_download(c, h_hash))
    
    elif action == "ca":
        task["cancel"] = True
        ses.remove_torrent(task["handle"])
        await q.message.edit("❌ Task Cancelled and Deleted.")
        active_tasks.pop(h_hash, None)

async def run_download(c, h_hash):
    task = active_tasks[h_hash]
    handle = task["handle"]
    info = handle.get_torrent_info()
    
    # Set priorities for selected files
    for idx in task["selected"]:
        handle.file_priority(idx, 4)

    while not handle.is_seed():
        if task["cancel"]: return
        s = handle.status()
        
        # Calculate progress of selected files only
        total_selected_size = sum(task["files"][i]["size"] for i in task["selected"])
        file_progress = handle.file_progress()
        downloaded_selected = sum(file_progress[i] for i in task["selected"])
        
        pct = (downloaded_selected / total_selected_size) * 100 if total_selected_size > 0 else 0
        
        status_text = f"**📥 Downloading:**\n" \
                      f"[{get_prog_bar(pct)}] {pct:.1f}%\n" \
                      f"🚀 Speed: {humanize.naturalsize(s.download_rate)}/s\n" \
                      f"👥 Peers: {s.num_peers}"
        
        try:
            await c.edit_message_text(task["chat_id"], task["msg_id"], status_text, 
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"ca_{h_hash}")]]))
        except: pass
        
        if downloaded_selected >= total_selected_size: break
        await asyncio.sleep(5)

    # UPLOAD PHASE
    await c.edit_message_text(task["chat_id"], task["msg_id"], "📤 **Download complete. Starting upload...**")
    
    for idx in task["selected"]:
        if task["cancel"]: break
        file_info = info.file_at(idx)
        f_name = os.path.basename(file_info.path)
        f_path = os.path.join(DOWNLOAD_DIR, file_info.path)
        
        if not os.path.exists(f_path):
            await c.send_message(task["chat_id"], f"⚠️ File missing: {f_name}")
            continue

        await c.edit_message_text(task["chat_id"], task["msg_id"], f"☁️ **Uploading:** `{f_name}`")
        
        loop = asyncio.get_event_loop()
        glink = await loop.run_in_executor(None, upload_to_gdrive, f_path, f_name)
        
        if "webViewLink" in str(glink) or "http" in str(glink):
            out = f"✅ **Uploaded:** `{f_name}`\n🔗 [GDrive Link]({glink})"
            if INDEX_URL:
                clean_name = f_name.replace(" ", "%20")
                out += f"\n⚡ [Direct Index]({INDEX_URL}/{clean_name})"
            await c.send_message(task["chat_id"], out, disable_web_page_preview=True)
        else:
            await c.send_message(task["chat_id"], f"❌ **Upload Failed:** `{f_name}`\nReason: {glink}")
        
        # Cleanup
        if os.path.exists(f_path):
            try: os.remove(f_path)
            except: pass

    await c.send_message(task["chat_id"], "🏁 **All tasks finished.**")
    ses.remove_torrent(handle)
    active_tasks.pop(h_hash, None)

if __name__ == "__main__":
    app.run()
