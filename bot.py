import os
import asyncio
import time
import json
import libtorrent as lt
import humanize
import PTN
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.errors import FloodWait
from googleapiclient.discovery import build
from google.oauth2 import service_account
from googleapiclient.http import MediaFileUpload

# --- 1. CONFIGURATION ---
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID", "")
INDEX_URL = os.environ.get("INDEX_URL", "").rstrip('/')

# Service Account Setup
SERVICE_ACCOUNT_JSON = os.environ.get("SERVICE_ACCOUNT_JSON")
if SERVICE_ACCOUNT_JSON:
    with open('credentials.json', 'w') as f:
        f.write(SERVICE_ACCOUNT_JSON)

try:
    creds = service_account.Credentials.from_service_account_file('credentials.json', scopes=['https://www.googleapis.com/auth/drive'])
    drive_service = build('drive', 'v3', credentials=creds)
except Exception as e:
    print(f"GDrive Auth Error: {e}")

# --- 2. TORRENT ENGINE SETUP ---
# Standard session setup for Libtorrent 1.2.x
ses = lt.session()
ses.listen_on(6881, 6891)
ses.apply_settings({
    'announce_to_all_trackers': True,
    'enable_dht': True,
    'download_rate_limit': 0,
    'connections_limit': 200
})

TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce", 
    "udp://open.stealth.si:80/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://9.rarbg.com:2810/announce"
]

app = Client("GDriveTorrentBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
active_tasks = {}
FILES_PER_PAGE = 8

# --- 3. HELPERS ---

def upload_to_gdrive(file_path, file_name):
    try:
        meta = {'name': file_name, 'parents': [GDRIVE_FOLDER_ID]}
        media = MediaFileUpload(file_path, mimetype='application/octet-stream', resumable=True)
        request = drive_service.files().create(body=meta, media_body=media, fields='id, webViewLink')
        
        response = None
        while response is None:
            status, response = request.next_chunk()
        return response.get('webViewLink')
    except Exception as e:
        raise Exception(f"Google Drive API Error: {str(e)}")

def get_prog_bar(pct):
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
    await m.reply_text("👋 **Torrent to GDrive Bot**\nSend a magnet link to start.")

@app.on_message(filters.regex(r"magnet:\?xt=urn:btih:[a-zA-Z0-9]+"))
async def handle_magnet(c, m):
    try:
        handle = lt.add_magnet_uri(ses, m.text, {'save_path': './downloads/'})
        for t in TRACKERS: handle.add_tracker({'url': t, 'tier': 0})
    except Exception as e:
        return await m.reply_text(f"❌ Error: {e}")

    msg = await m.reply_text("🧲 **Fetching Metadata...**")
    
    while not handle.has_metadata(): 
        await asyncio.sleep(1)
    
    # Use get_torrent_info() for 1.2.x compatibility
    info = handle.get_torrent_info()
    h_hash = str(handle.info_hash())
    
    files = []
    for i in range(info.num_files()):
        files.append({
            "name": info.file_at(i).path.split('/')[-1], 
            "size": info.file_at(i).size
        })

    active_tasks[h_hash] = {
        "handle": handle, "selected": [], "files": files,
        "chat_id": m.chat.id, "msg_id": msg.id, "cancel": False
    }
    
    handle.prioritize_files([0] * info.num_files())
    await msg.edit("✅ Metadata found! Select files:", reply_markup=gen_selection_kb(h_hash))

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
        asyncio.create_task(run_download(c, h_hash))
    elif action == "pa":
        task["handle"].pause(); await q.answer("Paused")
    elif action == "re":
        task["handle"].resume(); await q.answer("Resumed")
    elif action == "ca":
        task["cancel"] = True; ses.remove_torrent(task["handle"])
        await q.message.edit("❌ Cancelled."); active_tasks.pop(h_hash, None)

async def run_download(c, h_hash):
    task = active_tasks[h_hash]
    handle = task["handle"]
    info = handle.get_torrent_info()
    
    for idx in sorted(task["selected"]):
        if task["cancel"]: break
        
        handle.file_priority(idx, 4)
        file = info.file_at(idx)
        f_name = file.path.split('/')[-1]
        
        while True:
            if task["cancel"]: break
            s = handle.status()
            f_prog = handle.file_progress()[idx]
            pct = (f_prog / file.size) * 100 if file.size > 0 else 100
            
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("⏸ Pause" if not s.paused else "▶️ Resume", callback_data=f"{'pa' if not s.paused else 're'}_{h_hash}")],
                [InlineKeyboardButton("❌ Cancel", callback_data=f"ca_{h_hash}")]
            ])
            
            try:
                await c.edit_message_text(task["chat_id"], task["msg_id"],
                    f"**{'⏸ Paused' if s.paused else '📥 Downloading'}:**\n`{f_name}`\n"
                    f"[{get_prog_bar(pct)}] {pct:.1f}%\n"
                    f"🚀 {humanize.naturalsize(s.download_rate)}/s | 👥 P: {s.num_peers} S: {s.num_seeds}",
                    reply_markup=kb)
            except: pass
            if f_prog >= file.size: break
            await asyncio.sleep(5)

        if not task["cancel"]:
            await c.edit_message_text(task["chat_id"], task["msg_id"], f"☁️ **Uploading to GDrive:** `{f_name}`")
            f_path = os.path.join("./downloads/", file.path)
            
            try:
                loop = asyncio.get_event_loop()
                glink = await loop.run_in_executor(None, upload_to_gdrive, f_path, f_name)
                
                out = f"✅ **Uploaded:** `{f_name}`\n🔗 [GDrive Link]({glink})"
                if INDEX_URL:
                    clean_name = f_name.replace(" ", "%20")
                    out += f"\n⚡ [Direct Index Link]({INDEX_URL}/{clean_name})"
                
                await c.send_message(task["chat_id"], out, disable_web_page_preview=True)
            except Exception as e:
                await c.send_message(task["chat_id"], f"❌ UPLOAD FAILED: `{f_name}`\nReason: {e}")
            finally:
                if os.path.exists(f_path): os.remove(f_path)
                handle.file_priority(idx, 0)

    await c.send_message(task["chat_id"], "🏁 Task Finished.")
    active_tasks.pop(h_hash, None)

if __name__ == "__main__":
    while True:
        try:
            print("Bot starting...")
            app.run()
        except FloodWait as e:
            time.sleep(e.value + 5)
        except Exception:
            time.sleep(10)
