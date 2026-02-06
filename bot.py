import os, asyncio, time, json, libtorrent as lt, humanize
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from googleapiclient.discovery import build
from google.oauth2 import service_account
from googleapiclient.http import MediaFileUpload

# --- CONFIG ---
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID", "")
SERVICE_ACCOUNT_JSON = os.environ.get("SERVICE_ACCOUNT_JSON")

# GDrive Auth
drive_service = None
if SERVICE_ACCOUNT_JSON:
    with open('credentials.json', 'w') as f: f.write(SERVICE_ACCOUNT_JSON)
    try:
        creds = service_account.Credentials.from_service_account_file('credentials.json', scopes=['https://www.googleapis.com/auth/drive'])
        drive_service = build('drive', 'v3', credentials=creds)
    except Exception as e: print(f"GDrive Error: {e}")

app = Client("TorrentBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
ses = lt.session({'listen_interfaces': '0.0.0.0:6881', 'enable_dht': True})

TRACKERS = ["udp://tracker.opentrackr.org:1337/announce", "udp://open.stealth.si:80/announce", "udp://exodus.desync.com:6969/announce"]
active_tasks = {}
FILES_PER_PAGE = 7

# --- HELPERS ---
def get_prog_bar(pct):
    return "█" * int(pct/10) + "░" * (10-int(pct/10))

def upload_to_gdrive(file_path, file_name):
    meta = {'name': file_name, 'parents': [GDRIVE_FOLDER_ID]}
    media = MediaFileUpload(file_path, resumable=True)
    request = drive_service.files().create(body=meta, media_body=media, fields='id, webViewLink')
    resp = None
    while resp is None: status, resp = request.next_chunk()
    return resp.get('webViewLink')

async def tg_prog(current, total, msg, filename, start_time):
    if time.time() - getattr(tg_prog, "last", 0) < 5: return
    tg_prog.last = time.time()
    pct = (current/total)*100
    try: await msg.edit(f"📤 **Telegram Uploading:** `{filename}`\n[{get_prog_bar(pct)}] {pct:.1f}%")
    except: pass

def gen_selection_kb(h_hash, page=0):
    task = active_tasks[h_hash]
    start, end = page * FILES_PER_PAGE, (page + 1) * FILES_PER_PAGE
    btns = []
    for i, f in enumerate(task['files'][start:end], start):
        icon = "✅" if i in task['selected'] else "⬜"
        btns.append([InlineKeyboardButton(f"{icon} {f['name'][:35]}", callback_data=f"tog_{h_hash}_{i}_{page}")])
    
    nav = []
    if page > 0: nav.append(InlineKeyboardButton("⬅️ Back", callback_data=f"page_{h_hash}_{page-1}"))
    if end < len(task['files']): nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"page_{h_hash}_{page+1}"))
    if nav: btns.append(nav)
    
    btns.append([InlineKeyboardButton("☁️ DRIVE", callback_data=f"start_gdrive_{h_hash}"), InlineKeyboardButton("📱 TELEGRAM", callback_data=f"start_tg_{h_hash}")])
    btns.append([InlineKeyboardButton("❌ CANCEL", callback_data=f"ca_{h_hash}")])
    return InlineKeyboardMarkup(btns)

# --- HANDLERS ---
@app.on_message(filters.command("start"))
async def start_msg(c, m): await m.reply_text("👋 Send a Magnet Link to begin.")

@app.on_message(filters.regex(r"magnet:\?xt=urn:btih:[a-zA-Z0-9]+"))
async def magnet_handler(c, m):
    handle = lt.add_magnet_uri(ses, m.text, {'save_path': './downloads/'})
    for t in TRACKERS: handle.add_tracker({'url': t, 'tier': 0})
    msg = await m.reply_text("🧲 **Fetching Metadata...**")
    while not handle.has_metadata(): await asyncio.sleep(1)
    
    info, h_hash = handle.get_torrent_info(), str(handle.info_hash())
    active_tasks[h_hash] = {
        "handle": handle, "selected": [], "chat_id": m.chat.id, "msg_id": msg.id, "cancel": False,
        "files": [{"name": info.file_at(i).path.split('/')[-1], "size": info.file_at(i).size} for i in range(info.num_files())]
    }
    handle.prioritize_files([0] * info.num_files())
    await msg.edit("✅ Select files and Target:", reply_markup=gen_selection_kb(h_hash))

@app.on_callback_query(filters.regex(r"^(tog|page|start|ca|pa|re)_"))
async def cb_handler(c, q: CallbackQuery):
    data = q.data.split("_")
    action, h_hash = data[0], data[1]
    task = active_tasks.get(h_hash)
    if not task: return await q.answer("Task Expired.")

    if action == "tog":
        idx, pg = int(data[2]), int(data[3])
        if idx in task['selected']: task['selected'].remove(idx)
        else: task['selected'].append(idx)
        await q.message.edit_reply_markup(gen_selection_kb(h_hash, pg))
    elif action == "page":
        await q.message.edit_reply_markup(gen_selection_kb(h_hash, int(data[2])))
    elif action == "start":
        if not task["selected"]: return await q.answer("Select at least one file!")
        asyncio.create_task(download_loop(c, h_hash, data[1])) # data[1] is 'tg' or 'gdrive'
    elif action == "pa": task["handle"].pause(); await q.answer("Paused")
    elif action == "re": task["handle"].resume(); await q.answer("Resumed")
    elif action == "ca":
        task["cancel"] = True
        ses.remove_torrent(task["handle"])
        await q.message.edit("❌ Cancelled."); active_tasks.pop(h_hash, None)

async def download_loop(c, h_hash, target):
    task = active_tasks[h_hash]
    handle, info = task["handle"], task["handle"].get_torrent_info()
    
    for idx in sorted(task["selected"]):
        if task["cancel"]: break
        file = info.file_at(idx)
        handle.file_priority(idx, 4)
        f_name = file.path.split('/')[-1]
        
        while True:
            if task["cancel"]: break
            s, prog = handle.status(), handle.file_progress()[idx]
            pct = (prog/file.size)*100 if file.size > 0 else 100
            btns = InlineKeyboardMarkup([[InlineKeyboardButton("⏸ Pause" if not s.paused else "▶️ Resume", callback_data=f"{'pa' if not s.paused else 're'}_{h_hash}"), InlineKeyboardButton("❌ Cancel", callback_data=f"ca_{h_hash}")]])
            try: await c.edit_message_text(task["chat_id"], task["msg_id"], f"**{'⏸ Paused' if s.paused else '📥 Downloading'}:** `{f_name}`\n[{get_prog_bar(pct)}] {pct:.1f}%\n🚀 {humanize.naturalsize(s.download_rate)}/s | 👥 P:{s.num_peers} S:{s.num_seeds}", reply_markup=btns)
            except: pass
            if prog >= file.size: break
            await asyncio.sleep(5)

        if not task["cancel"]:
            f_path = os.path.join("./downloads/", file.path)
            try:
                if target == "gdrive":
                    await c.edit_message_text(task["chat_id"], task["msg_id"], f"☁️ **Uploading to GDrive:** `{f_name}`")
                    link = await asyncio.get_event_loop().run_in_executor(None, upload_to_gdrive, f_path, f_name)
                    await c.send_message(task["chat_id"], f"✅ **Drive:** `{f_name}`\n🔗 {link}")
                else:
                    await c.send_document(task["chat_id"], document=f_path, caption=f"✅ `{f_name}`", progress=tg_prog, progress_args=(task["msg_id"], f_name, time.time()))
            except Exception as e: await c.send_message(task["chat_id"], f"❌ Error: {e}")
            finally:
                if os.path.exists(f_path): os.remove(f_path)
                handle.file_priority(idx, 0)
    await c.send_message(task["chat_id"], "🏁 Task Finished.")
    active_tasks.pop(h_hash, None)

if __name__ == "__main__":
    app.run()
