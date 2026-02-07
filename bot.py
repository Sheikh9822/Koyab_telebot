import os
import asyncio
import time
import json
import libtorrent as lt
import humanize
import PTN
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from googleapiclient.discovery import build
from google.oauth2 import service_account
from googleapiclient.http import MediaFileUpload

# --- 1. GLOBALS ---
drive_service = None
active_tasks = {}
FILES_PER_PAGE = 8

# --- 2. CONFIGURATION ---
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID", "")
INDEX_URL = os.environ.get("INDEX_URL", "").rstrip('/')

# --- 3. GOOGLE DRIVE AUTHENTICATION (REPAIRED) ---
def init_gdrive():
    global drive_service
    json_raw = os.environ.get("SERVICE_ACCOUNT_JSON")
    if not json_raw:
        print("❌ CRITICAL: SERVICE_ACCOUNT_JSON is missing from environment variables!")
        return

    try:
        # Fix Koyeb escaped newlines and quotes
        json_fix = json_raw.replace("\\n", "\n").replace('\\"', '"')
        creds_data = json.loads(json_fix)
        
        with open('credentials.json', 'w') as f:
            json.dump(creds_data, f)
        
        creds = service_account.Credentials.from_service_account_file(
            'credentials.json', 
            scopes=['https://www.googleapis.com/auth/drive']
        )
        drive_service = build('drive', 'v3', credentials=creds, cache_discovery=False)
        print("✅ Google Drive Service Initialized Successfully")
    except Exception as e:
        print(f"❌ GDrive Auth Initialization Failed: {e}")

init_gdrive()

# --- 4. TORRENT ENGINE ---
ses = lt.session()
ses.listen_on(6881, 6891)

# --- 5. HELPERS ---

def upload_to_gdrive(file_path, file_name):
    if not drive_service:
        raise Exception("GDrive Service not initialized. Check logs for Auth errors.")
    
    if not os.path.exists(file_path):
        raise Exception(f"File not found on disk: {file_path}")
        
    if os.path.getsize(file_path) == 0:
        raise Exception("File is empty (0 bytes). Torrent not flushed to disk.")

    meta = {'name': file_name, 'parents': [GDRIVE_FOLDER_ID]}
    media = MediaFileUpload(file_path, mimetype='application/octet-stream', resumable=True)
    request = drive_service.files().create(body=meta, media_body=media, fields='id, webViewLink')
    
    response = None
    while response is None:
        status, response = request.next_chunk()
    return response.get('webViewLink')

def get_prog_bar(pct):
    p = int(pct / 10)
    return "█" * p + "░" * (10 - p)

def gen_selection_kb(h_hash, page=0):
    task = active_tasks[h_hash]
    files = task["files"]
    selected = task["selected"]
    start, end = page * FILES_PER_PAGE, (page + 1) * FILES_PER_PAGE
    btns = []
    for i, f in enumerate(files[start:end]):
        idx = start + i
        icon = "✅" if idx in selected else "⬜"
        btns.append([InlineKeyboardButton(f"{icon} {f['name'][:35]}", callback_data=f"tog_{h_hash}_{idx}_{page}")])
    nav = []
    if page > 0: nav.append(InlineKeyboardButton("⬅️ Back", callback_data=f"page_{h_hash}_{page-1}"))
    if end < len(files): nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"page_{h_hash}_{page+1}"))
    if nav: btns.append(nav)
    btns.append([InlineKeyboardButton("🚀 START DOWNLOAD", callback_data=f"startdl_{h_hash}")])
    btns.append([InlineKeyboardButton("❌ CANCEL", callback_data=f"ca_{h_hash}")])
    return InlineKeyboardMarkup(btns)

# --- 6. HANDLERS ---

app = Client("LeechBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

@app.on_message(filters.command("start"))
async def start(c, m):
    await m.reply_text("🚀 Bot is ready. Send a magnet link to begin.")

@app.on_message(filters.regex(r"magnet:\?xt=urn:btih:[a-zA-Z0-9]+"))
async def handle_magnet(c, m):
    try:
        handle = lt.add_magnet_uri(ses, m.text, {'save_path': './downloads/'})
        msg = await m.reply_text("🧲 **Fetching Metadata...**")
        while not handle.has_metadata(): 
            await asyncio.sleep(1)
        
        info = handle.get_torrent_info()
        h_hash = str(handle.info_hash())
        files = [{"name": info.file_at(i).path.split('/')[-1], "size": info.file_at(i).size} for i in range(info.num_files())]
        
        active_tasks[h_hash] = {"handle": handle, "selected": [], "files": files, "chat_id": m.chat.id, "msg_id": msg.id, "cancel": False}
        handle.prioritize_files([0] * info.num_files())
        await msg.edit(f"📂 **{info.name()}**\nSelect files:", reply_markup=gen_selection_kb(h_hash))
    except Exception as e:
        await m.reply_text(f"❌ Error: {e}")

@app.on_callback_query(filters.regex(r"^(tog|page|startdl|ca)_"))
async def callbacks(c, q: CallbackQuery):
    d = q.data.split("_")
    action, h_hash = d[0], d[1]
    task = active_tasks.get(h_hash)
    if not task: return await q.answer("Expired.")

    if action == "tog":
        idx, p = int(d[2]), int(d[3])
        if idx in task["selected"]: task["selected"].remove(idx)
        else: task["selected"].append(idx)
        await q.message.edit_reply_markup(gen_selection_kb(h_hash, p))
    elif action == "page":
        await q.message.edit_reply_markup(gen_selection_kb(h_hash, int(d[2])))
    elif action == "startdl":
        if not task["selected"]: return await q.answer("Select a file!")
        asyncio.create_task(run_download(c, h_hash))
    elif action == "ca":
        task["cancel"] = True
        ses.remove_torrent(task["handle"])
        await q.message.edit("❌ Cancelled.")

async def run_download(c, h_hash):
    task = active_tasks[h_hash]
    handle = task["handle"]
    info = handle.get_torrent_info()
    
    for idx in sorted(task["selected"]):
        if task["cancel"]: break
        file = info.file_at(idx)
        handle.file_priority(idx, 4)
        f_name = file.path.split('/')[-1]
        
        # Download Progress
        while True:
            if task["cancel"]: break
            s = handle.status()
            f_p = handle.file_progress()[idx]
            pct = (f_p / file.size) * 100 if file.size > 0 else 100
            
            try:
                await c.edit_message_text(task["chat_id"], task["msg_id"],
                    f"📥 **Downloading:** `{f_name}`\n"
                    f"[{get_prog_bar(pct)}] {pct:.1f}%\n"
                    f"🚀 {humanize.naturalsize(s.download_rate)}/s")
            except: pass
            if f_p >= file.size: break
            await asyncio.sleep(5)

        if not task["cancel"]:
            # Ensure file is saved to disk
            handle.save_resume_data()
            await asyncio.sleep(5) # Delay to allow disk write

            # Robust Path Logic: check both root and subfolder
            f_path_root = os.path.join("./downloads/", file.path)
            f_path_sub = os.path.join("./downloads/", info.name(), file.path)
            
            f_path = f_path_root if os.path.exists(f_path_root) else f_path_sub

            await c.edit_message_text(task["chat_id"], task["msg_id"], f"☁️ **Uploading to GDrive:** `{f_name}`")
            
            try:
                loop = asyncio.get_event_loop()
                glink = await loop.run_in_executor(None, upload_to_gdrive, f_path, f_name)
                
                out = f"✅ **Uploaded:** `{f_name}`\n🔗 [GDrive Link]({glink})"
                if INDEX_URL:
                    out += f"\n⚡ [Direct Link]({INDEX_URL}/{f_name.replace(' ', '%20')})"
                await c.send_message(task["chat_id"], out, disable_web_page_preview=True)
            except Exception as e:
                await c.send_message(task["chat_id"], f"❌ UPLOAD FAILED: `{f_name}`\nReason: {e}")
            finally:
                if os.path.exists(f_path): os.remove(f_path)
                handle.file_priority(idx, 0)

    await c.send_message(task["chat_id"], "🏁 Task Finished.")
    active_tasks.pop(h_hash, None)

if __name__ == "__main__":
    app.run()
