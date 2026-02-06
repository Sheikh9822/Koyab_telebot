import os, asyncio, time, libtorrent as lt, PTN, humanize
from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

# --- CONFIGURATION ---
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

app = Client("TorrentBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
ses = lt.session({'listen_interfaces': '0.0.0.0:6881'})
tasks = {} # Stores handle and status

def get_prog_bar(percentage):
    p = int(percentage / 10)
    return "█" * p + "░" * (10 - p)

async def progress_callback(current, total, msg, filename, start_time):
    # Throttle updates to avoid FloodWait (every 5 seconds)
    if time.time() - progress_callback.last_update < 5: return
    progress_callback.last_update = time.time()
    
    pct = (current / total) * 100
    elapsed = time.time() - start_time
    speed = current / elapsed if elapsed > 0 else 0
    
    text = (f"📤 **Uploading:** `{filename}`\n"
            f"[{get_prog_bar(pct)}] {pct:.1f}%\n"
            f"🚀 Speed: {humanize.naturalsize(speed)}/s")
    try: await msg.edit_text(text)
    except: pass

progress_callback.last_update = 0

@app.on_message(filters.command("start"))
async def start(c, m):
    await m.reply_text("👋 Send me a **Magnet Link** to start downloading to Telegram Cloud!")

@app.on_message(filters.regex(r"magnet:\?xt=urn:btih:[a-zA-Z0-9]+"))
async def handle_magnet(c, m):
    magnet = m.text
    params = {'save_path': './downloads/', 'storage_mode': lt.storage_mode_t.storage_mode_sparse}
    handle = lt.add_magnet_uri(ses, magnet, params)
    
    status_msg = await m.reply_text("🧲 **Fetching Metadata...**")
    while not handle.has_metadata(): await asyncio.sleep(1)
    
    info = handle.get_torrent_info()
    hash_id = str(handle.info_hash())
    tasks[hash_id] = {"handle": handle, "cancel": False}
    
    # Logic: Set all files to priority 0 (Skip)
    handle.prioritize_files([0] * info.num_files())
    
    await status_msg.edit(f"📦 **Found {info.num_files()} files.** Starting sequential download...")

    for i in range(info.num_files()):
        if tasks[hash_id]["cancel"]: break
        
        file = info.file_at(i)
        if file.size < 5 * 1024 * 1024: continue # Skip files smaller than 5MB (nfo, txt)
        
        # Priority 1: Download ONLY this file
        handle.file_priority(i, 1)
        file_name = file.path.split('/')[-1]
        
        while True:
            if tasks[hash_id]["cancel"]: break
            s = handle.status()
            f_prog = handle.file_progress()[i]
            pct = (f_prog / file.size) * 100
            
            kb_speed = s.download_rate / 1024
            btn = InlineKeyboardMarkup([[InlineKeyboardButton("⏸ Pause", f"p_{hash_id}"), 
                                         InlineKeyboardButton("❌ Cancel", f"c_{hash_id}")]])
            
            try:
                await status_msg.edit(
                    f"📥 **Downloading {i+1}/{info.num_files()}**\n"
                    f"📝 `{file_name}`\n"
                    f"[{get_prog_bar(pct)}] {pct:.2f}%\n"
                    f"⚡ {kb_speed:.1f} KB/s | 📂 {humanize.naturalsize(file.size)}",
                    reply_markup=btn
                )
            except: pass
            
            if f_prog >= file.size: break
            await asyncio.sleep(5)

        # Upload & Clean
        if not tasks[hash_id]["cancel"]:
            file_path = os.path.join("./downloads/", file.path)
            meta = PTN.parse(file_name)
            caption = f"🎬 **{meta.get('title', file_name)}**\n💎 Size: {humanize.naturalsize(file.size)}"
            
            await status_msg.edit(f"📤 **Uploading:** `{file_name}`...")
            await c.send_document(m.chat.id, document=file_path, caption=caption, 
                                  progress=progress_callback, progress_args=(status_msg, file_name, time.time()))
            
            if os.path.exists(file_path): os.remove(file_path)
            handle.file_priority(i, 0) # Free disk space

    await status_msg.edit("✅ **All tasks completed!**")
    tasks.pop(hash_id, None)

@app.on_callback_query(filters.regex(r"^(p|r|c)_"))
async def controls(c, q: CallbackQuery):
    action, h_id = q.data.split("_")
    if h_id not in tasks: return await q.answer("Task not found.")
    
    handle = tasks[h_id]["handle"]
    if action == "p":
        handle.pause(); await q.edit_message_reply_markup(InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Resume", f"r_{h_id}"), InlineKeyboardButton("❌ Cancel", f"c_{h_id}")]]))
    elif action == "r":
        handle.resume(); await q.answer("Resumed")
    elif action == "c":
        tasks[h_id]["cancel"] = True; ses.remove_torrent(handle); await q.message.edit("❌ Task Cancelled."); tasks.pop(h_id, None)

if __name__ == "__main__":
    app.run()
