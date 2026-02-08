import os, asyncio, time, json, libtorrent as lt, humanize, PTN
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.errors import FloodWait
from googleapiclient.discovery import build
from google.oauth2 import service_account
from googleapiclient.http import MediaFileUpload

# --- 1. SETUP & CONFIG ---
SERVICE_ACCOUNT_JSON = os.environ.get("SERVICE_ACCOUNT_JSON")
if SERVICE_ACCOUNT_JSON:
    with open('credentials.json', 'w') as f: f.write(SERVICE_ACCOUNT_JSON)

API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID", "")
INDEX_URL = os.environ.get("INDEX_URL", "").rstrip('/') # e.g., https://myindex.workers.dev

# GDrive Auth
creds = service_account.Credentials.from_service_account_file('credentials.json', scopes=['https://www.googleapis.com/auth/drive'])
drive_service = build('drive', 'v3', credentials=creds)

app = Client("AdvancedLeechBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
ses = lt.session({'listen_interfaces': '0.0.0.0:6881'})

# Global Queue and Task Storage
DOWNLOAD_QUEUE = asyncio.Queue()
active_tasks = {} # h_hash -> data
is_processing = False

# --- 2. UTILS ---

def get_eta(remaining_bytes, speed):
    if speed <= 0: return "Unknown"
    seconds = remaining_bytes / speed
    return time.strftime("%Hh %Mm %Ss", time.gmtime(seconds))

def smart_rename(original_name):
    info = PTN.parse(original_name)
    title = info.get('title', original_name)
    season = info.get('season')
    episode = info.get('episode')
    quality = info.get('quality', '')
    
    if season and episode:
        return f"[S{season:02d}E{episode:02d}] {title} [{quality}].mkv"
    return f"{title} [{quality}].mkv"

def upload_to_gdrive(file_path, file_name):
    meta = {'name': file_name, 'parents': [GDRIVE_FOLDER_ID]}
    media = MediaFileUpload(file_path, resumable=True)
    request = drive_service.files().create(body=meta, media_body=media, fields='id, webViewLink')
    response = None
    while response is None:
        status, response = request.next_chunk()
    return response.get('webViewLink')

# --- 3. QUEUE WORKER ---

async def queue_worker():
    global is_processing
    while True:
        h_hash = await DOWNLOAD_QUEUE.get()
        is_processing = True
        try:
            await run_download_logic(h_hash)
        except Exception as e:
            print(f"Queue Error: {e}")
        finally:
            is_processing = False
            DOWNLOAD_QUEUE.task_done()

# --- 4. DOWNLOAD & UPLOAD LOGIC ---

async def run_download_logic(h_hash):
    task = active_tasks[h_hash]
    handle = task["handle"]
    info = handle.get_torrent_info()
    
    for idx in sorted(task["selected"]):
        if task["cancel"]: break
        file = info.file_at(idx)
        original_name = file.path.split('/')[-1]
        
        # Applying Auto-Rename Logic
        final_name = smart_rename(original_name) if task["do_rename"] else original_name
        
        handle.file_priority(idx, 4)
        
        while True:
            if task["cancel"]: break
            s = handle.status()
            f_prog = handle.file_progress()[idx]
            done = f_prog >= file.size
            
            if not done:
                pct = (f_prog / file.size) * 100
                speed = s.download_rate
                eta = get_eta(file.size - f_prog, speed)
                
                status_text = (
                    f"📥 **Downloading:** `{final_name}`\n"
                    f"📊 **Progress:** {pct:.1f}%\n"
                    f"🚀 **Speed:** {humanize.naturalsize(speed)}/s\n"
                    f"⏳ **ETA:** {eta}\n"
                    f"👥 **Peers:** {s.num_peers} | **Seeds:** {s.num_seeds}"
                )
                try:
                    await app.edit_message_text(task["chat_id"], task["msg_id"], status_text,
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", f"ca_{h_hash}")]]))
                except: pass
            
            if done: break
            await asyncio.sleep(5)

        if not task["cancel"]:
            await app.edit_message_text(task["chat_id"], task["msg_id"], f"☁️ **Uploading:** `{final_name}`")
            f_path = os.path.join("./downloads/", file.path)
            try:
                loop = asyncio.get_event_loop()
                glink = await loop.run_in_executor(None, upload_to_gdrive, f_path, final_name)
                
                # Index Link Generation
                idx_link = f"{INDEX_URL}/{final_name.replace(' ', '%20')}" if INDEX_URL else None
                
                out = f"✅ **Finished:** `{final_name}`\n🔗 [GDrive Link]({glink})"
                if idx_link: out += f"\n⚡ [Direct Index Link]({idx_link})"
                
                await app.send_message(task["chat_id"], out, disable_web_page_preview=True)
            except Exception as e:
                await app.send_message(task["chat_id"], f"❌ Upload Error: {e}")
            finally:
                if os.path.exists(f_path): os.remove(f_path)
                handle.file_priority(idx, 0)

    await app.send_message(task["chat_id"], f"🏁 Torrent `{info.name()}` complete.")
    active_tasks.pop(h_hash, None)

# --- 5. INTERFACE HANDLERS ---

@app.on_message(filters.command("start"))
async def cmd_start(c, m):
    await m.reply_text("👋 **Ultimate Leech Bot**\nSend Magnet or .torrent file.")

@app.on_message(filters.regex(r"magnet:\?xt=urn:btih:[a-zA-Z0-9]+") | filters.document)
async def handle_input(c, m):
    if m.document and not m.document.file_name.endswith(".torrent"): return
    
    msg = await m.reply_text("🧲 **Processing Input...**")
    
    if m.document:
        path = await m.download()
        handle = lt.add_torrent(ses, {'ti': lt.torrent_info(path), 'save_path': './downloads/'})
        os.remove(path)
    else:
        handle = lt.add_magnet_uri(ses, m.text, {'save_path': './downloads/'})

    while not handle.has_metadata(): await asyncio.sleep(1)
    
    info = handle.get_torrent_info()
    h_hash = str(handle.info_hash())
    files = [{"name": info.file_at(i).path.split('/')[-1], "size": info.file_at(i).size} for i in range(info.num_files())]
    
    active_tasks[h_hash] = {
        "handle": handle, "selected": [], "files": files, "chat_id": m.chat.id, 
        "msg_id": msg.id, "cancel": False, "do_rename": True 
    }
    handle.prioritize_files([0] * info.num_files())
    
    kb = [
        [InlineKeyboardButton("✅ Auto-Rename ON", callback_data=f"arn_on_{h_hash}")],
        [InlineKeyboardButton("Select Files", callback_data=f"page_{h_hash}_0")]
    ]
    await msg.edit(f"📂 **Metadata Found:** `{info.name()}`\nSelect options below:", reply_markup=InlineKeyboardMarkup(kb))

@app.on_callback_query()
async def cb_handler(c, q: CallbackQuery):
    data = q.data.split("_")
    action, h_hash = data[0], data[2] if data[0] == "arn" else data[1]
    task = active_tasks.get(h_hash)
    if not task: return await q.answer("Expired.")

    if action == "arn":
        task["do_rename"] = not task["do_rename"]
        status = "ON" if task["do_rename"] else "OFF"
        await q.answer(f"Auto-Rename {status}")
        # Update buttons... (simplified for brevity)

    elif action == "page":
        # (Insert the pagination logic from the previous prompt here)
        pass

    elif action == "startdl":
        await DOWNLOAD_QUEUE.put(h_hash)
        pos = DOWNLOAD_QUEUE.qsize()
        await q.message.edit(f"⏳ **Task Queued.**\nPosition in line: {pos}\nDownload will start automatically.")

# --- 6. RUN ---
if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.create_task(queue_worker()) # Start background queue processor
    app.run()
