import os, asyncio, time, json, libtorrent as lt, humanize, PTN
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.errors import FloodWait
from googleapiclient.discovery import build
from google.oauth2 import service_account
from googleapiclient.http import MediaFileUpload

# --- 1. SETUP & REPAIR CREDENTIALS ---
SERVICE_ACCOUNT_JSON = os.environ.get("SERVICE_ACCOUNT_JSON")
if SERVICE_ACCOUNT_JSON:
    try:
        # Repairing Koyeb newline issues
        parsed_json = json.loads(SERVICE_ACCOUNT_JSON.replace("\\n", "\n"))
        with open('credentials.json', 'w') as f:
            json.dump(parsed_json, f)
    except Exception as e:
        print(f"JSON Error: {e}")

# --- 2. CONFIG ---
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID", "")
INDEX_URL = os.environ.get("INDEX_URL", "").rstrip('/')

try:
    creds = service_account.Credentials.from_service_account_file('credentials.json', scopes=['https://www.googleapis.com/auth/drive'])
    drive_service = build('drive', 'v3', credentials=creds)
except Exception as e:
    print(f"Auth Init Error: {e}")

app = Client("LeechBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
ses = lt.session()
ses.listen_on(6881, 6891)

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
        # This will now capture the specific JSON error from Google
        raise Exception(str(e))

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

# --- 4. HANDLERS ---

@app.on_message(filters.command("start"))
async def start(c, m):
    await m.reply_text("👋 Send a Magnet Link.")

@app.on_message(filters.regex(r"magnet:\?xt=urn:btih:[a-zA-Z0-9]+"))
async def handle_magnet(c, m):
    handle = lt.add_magnet_uri(ses, m.text, {'save_path': './downloads/'})
    msg = await m.reply_text("🧲 **Fetching Metadata...**")
    while not handle.has_metadata(): await asyncio.sleep(1)
    
    info = handle.get_torrent_info()
    h_hash = str(handle.info_hash())
    files = [{"name": info.file_at(i).path.split('/')[-1], "size": info.file_at(i).size} for i in range(info.num_files())]
    
    active_tasks[h_hash] = {"handle": handle, "selected": [], "files": files, "chat_id": m.chat.id, "msg_id": msg.id, "cancel": False}
    handle.prioritize_files([0] * info.num_files())
    await msg.edit("✅ Select files:", reply_markup=gen_selection_kb(h_hash))

@app.on_callback_query(filters.regex(r"^(tog|page|startdl|ca|pa|re)_"))
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
        task["cancel"] = True; ses.remove_torrent(task["handle"])
        await q.message.edit("❌ Cancelled."); active_tasks.pop(h_hash, None)

async def run_download(c, h_hash):
    task = active_tasks[h_hash]
    handle = task["handle"]
    info = handle.get_torrent_info()
    
    for idx in sorted(task["selected"]):
        if task["cancel"]: break
        file = info.file_at(idx)
        handle.file_priority(idx, 4)
        f_name = file.path.split('/')[-1]
        
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
            await c.edit_message_text(task["chat_id"], task["msg_id"], f"☁️ **Uploading to GDrive:** `{f_name}`")
            f_path = os.path.join("./downloads/", file.path)
            try:
                loop = asyncio.get_event_loop()
                glink = await loop.run_in_executor(None, upload_to_gdrive, f_path, f_name)
                
                out = f"✅ **Uploaded:** `{f_name}`\n🔗 [GDrive Link]({glink})"
                if INDEX_URL:
                    out += f"\n⚡ [Direct Link]({INDEX_URL}/{f_name.replace(' ', '%20')})"
                
                await c.send_message(task["chat_id"], out, disable_web_page_preview=True)
            except Exception as e:
                # This will now show the REAL error reason in Telegram
                await c.send_message(task["chat_id"], f"❌ UPLOAD FAILED: `{f_name}`\nReason: {e}")
            finally:
                if os.path.exists(f_path): os.remove(f_path)
                handle.file_priority(idx, 0)

    await c.send_message(task["chat_id"], "🏁 Task Finished.")
    active_tasks.pop(h_hash, None)

if __name__ == "__main__":
    app.run()
