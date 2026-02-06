import os
import asyncio
import time
import json
import libtorrent as lt
import humanize
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from googleapiclient.discovery import build
from google.oauth2 import service_account
from googleapiclient.http import MediaFileUpload

# --- 1. CONFIGURATION ---
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID", "")

# Extract GDrive Credentials
SERVICE_ACCOUNT_JSON = os.environ.get("SERVICE_ACCOUNT_JSON")
if SERVICE_ACCOUNT_JSON:
    with open('credentials.json', 'w') as f:
        f.write(SERVICE_ACCOUNT_JSON)

SCOPES = ['https://www.googleapis.com/auth/drive']
creds = service_account.Credentials.from_service_account_file('credentials.json', scopes=SCOPES)
drive_service = build('drive', 'v3', credentials=creds)

app = Client("GDriveTorrentBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- 2. TORRENT ENGINE & MASSIVE TRACKERS ---
ses = lt.session()
settings = {
    'listen_interfaces': '0.0.0.0:6881',
    'announce_to_all_trackers': True,
    'announce_to_all_tiers': True,
    'enable_dht': True,
    'dht_announce_interval': 60,
}
ses.apply_settings(settings)

# Massive Tracker List
TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce", "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce", "udp://exodus.desync.com:6969/announce",
    "udp://tracker.openbittorrent.com:6969/announce", "udp://9.rarbg.com:2810/announce",
    "udp://explodie.org:6969/announce", "udp://ipv4.tracker.harry.lu:80/announce",
    "udp://p4p.arenabg.com:1337/announce", "udp://tracker.tiny-vps.com:6969/announce",
    "udp://open.demonii.com:1337/announce", "http://tracker.openbittorrent.com:80/announce",
    "udp://tracker.coppersurfer.tk:6969/announce", "udp://tracker.cyberia.is:6969/announce"
]

# Task Storage: { message_id: { "handle": h, "selected": [0, 1, ...], "files": [...] } }
active_tasks = {}

# --- 3. HELPER FUNCTIONS ---

def upload_to_gdrive(file_path, file_name):
    file_metadata = {'name': file_name, 'parents': [GDRIVE_FOLDER_ID]}
    media = MediaFileUpload(file_path, resumable=True)
    request = drive_service.files().create(body=file_metadata, media_body=media, fields='id, webViewLink')
    response = None
    while response is None:
        status, response = request.next_chunk()
    return response.get('webViewLink')

def get_prog_bar(pct):
    p = int(pct / 10)
    return "█" * p + "░" * (10 - p)

def generate_file_selection(h_hash):
    task = active_tasks[h_hash]
    files = task["files"]
    selected = task["selected"]
    
    buttons = []
    # Show first 15 files to avoid Telegram button limits
    for i, file in enumerate(files[:15]):
        icon = "✅" if i in selected else "⬜"
        buttons.append([InlineKeyboardButton(f"{icon} {file['name'][:40]}", callback_data=f"tog_{h_hash}_{i}")])
    
    buttons.append([InlineKeyboardButton("🚀 START DOWNLOAD", callback_data=f"startdl_{h_hash}")])
    buttons.append([InlineKeyboardButton("❌ CANCEL", callback_data=f"ca_{h_hash}")])
    return InlineKeyboardMarkup(buttons)

# --- 4. HANDLERS ---

@app.on_message(filters.command("start"))
async def start(c, m):
    await m.reply_text("👋 Send a Magnet link. You can then **select which files** to upload to GDrive.")

@app.on_message(filters.regex(r"magnet:\?xt=urn:btih:[a-zA-Z0-9]+"))
async def handle_magnet(c, m):
    magnet = m.text
    params = {'save_path': './downloads/', 'storage_mode': lt.storage_mode_t.storage_mode_sparse}
    handle = lt.add_magnet_uri(ses, magnet, params)
    
    for t in TRACKERS: handle.add_tracker({'url': t, 'tier': 0})

    status_msg = await m.reply_text("🧲 **Fetching Metadata...**")
    
    st_time = time.time()
    while not handle.has_metadata():
        if time.time() - st_time > 300: return await status_msg.edit("❌ Metadata Timeout.")
        await asyncio.sleep(1)
    
    info = handle.get_torrent_info()
    h_hash = str(handle.info_hash())
    
    # Store file info
    files_list = []
    for i in range(info.num_files()):
        files_list.append({"name": info.file_at(i).path.split('/')[-1], "size": info.file_at(i).size})

    active_tasks[h_hash] = {
        "handle": handle,
        "selected": [], 
        "files": files_list,
        "status_msg_id": status_msg.id,
        "chat_id": m.chat.id,
        "cancel": False
    }
    
    handle.prioritize_files([0] * info.num_files()) # Stop all initially
    await status_msg.edit("✅ Metadata found! Select files to download:", reply_markup=generate_file_selection(h_hash))

@app.on_callback_query(filters.regex(r"^(tog|startdl|ca|pa|re)_"))
async def handle_callbacks(c, q: CallbackQuery):
    data = q.data.split("_")
    action = data[0]
    h_hash = data[1]
    
    if h_hash not in active_tasks:
        return await q.answer("Task Expired.", show_alert=True)
    
    task = active_tasks[h_hash]
    handle = task["handle"]

    if action == "tog":
        file_idx = int(data[2])
        if file_idx in task["selected"]:
            task["selected"].remove(file_idx)
        else:
            task["selected"].append(file_idx)
        await q.message.edit_reply_markup(reply_markup=generate_file_selection(h_hash))

    elif action == "startdl":
        if not task["selected"]:
            return await q.answer("Please select at least one file!", show_alert=True)
        
        await q.message.edit("🚀 Starting Download for selected files...")
        asyncio.create_task(run_download_logic(c, h_hash))

    elif action == "ca":
        task["cancel"] = True
        ses.remove_torrent(handle)
        await q.message.edit("❌ Task Cancelled.")
        active_tasks.pop(h_hash, None)

# --- 5. CORE DOWNLOAD LOGIC ---

async def run_download_logic(c, h_hash):
    task = active_tasks[h_hash]
    handle = task["handle"]
    selected_indices = sorted(task["selected"])
    info = handle.get_torrent_info()
    
    for idx in selected_indices:
        if task["cancel"]: break
        
        file = info.file_at(idx)
        f_name = file.path.split('/')[-1]
        handle.file_priority(idx, 4) # Priority: Normal
        
        while True:
            if task["cancel"]: break
            s = handle.status()
            f_prog = handle.file_progress()[idx]
            pct = (f_prog / file.size) * 100 if file.size > 0 else 100
            
            try:
                await c.edit_message_text(
                    task["chat_id"], task["status_msg_id"],
                    f"📥 **Downloading:** `{f_name}`\n"
                    f"[{get_prog_bar(pct)}] {pct:.1f}%\n"
                    f"🚀 Speed: {humanize.naturalsize(s.download_rate)}/s | 👥 Peers: {s.num_peers}"
                )
            except: pass
            
            if f_prog >= file.size: break
            await asyncio.sleep(5)

        if not task["cancel"]:
            await c.edit_message_text(task["chat_id"], task["status_msg_id"], f"☁️ **Uploading to GDrive:** `{f_name}`")
            f_path = os.path.join("./downloads/", file.path)
            try:
                loop = asyncio.get_event_loop()
                link = await loop.run_in_executor(None, upload_to_gdrive, f_path, f_name)
                await c.send_message(task["chat_id"], f"✅ **Uploaded:** `{f_name}`\n🔗 [GDrive Link]({link})")
            except Exception as e:
                await c.send_message(task["chat_id"], f"❌ Error: {e}")
            finally:
                if os.path.exists(f_path): os.remove(f_path)
                handle.file_priority(idx, 0)

    await c.send_message(task["chat_id"], "🏁 All selected files processed.")
    active_tasks.pop(h_hash, None)

if __name__ == "__main__":
    app.run()
