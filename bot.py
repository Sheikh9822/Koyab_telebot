import os
import asyncio
import time
import libtorrent as lt
import PTN
import humanize
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

# --- CONFIG ---
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

app = Client("TorrentBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
ses = lt.session({'listen_interfaces': '0.0.0.0:6881'})
active_tasks = {}

def get_prog_bar(pct):
    p = int(pct / 10)
    return "█" * p + "░" * (10 - p)

async def up_progress(current, total, msg, filename, start_time):
    # Update every 5 seconds to avoid Telegram flood limits
    if time.time() - up_progress.last_up < 5: return
    up_progress.last_up = time.time()
    
    pct = (current / total) * 100
    speed = current / (time.time() - start_time)
    text = (f"📤 **Uploading:** `{filename}`\n"
            f"[{get_prog_bar(pct)}] {pct:.1f}%\n"
            f"⚡ Speed: {humanize.naturalsize(speed)}/s")
    try: await msg.edit_text(text)
    except: pass
up_progress.last_up = 0

@app.on_message(filters.command("start"))
async def start_cmd(c, m):
    await m.reply_text("🚀 **Torrent to Telegram Cloud**\nSend me a magnet link to begin.")

@app.on_message(filters.regex(r"magnet:\?xt=urn:btih:[a-zA-Z0-9]+"))
async def handle_magnet(c, m):
    magnet = m.text
    params = {'save_path': './downloads/', 'storage_mode': lt.storage_mode_t.storage_mode_sparse}
    handle = lt.add_magnet_uri(ses, magnet, params)
    
    status_msg = await m.reply_text("🧲 **Fetching Metadata...**")
    while not handle.has_metadata(): await asyncio.sleep(1)
    
    info = handle.get_torrent_info()
    h_hash = str(handle.info_hash())
    active_tasks[h_hash] = {"handle": handle, "cancel": False}
    
    # 🛑 Crucial: Skip all files initially to save disk
    handle.prioritize_files([0] * info.num_files())
    
    await status_msg.edit(f"📂 **Found {info.num_files()} files.** Starting one-by-one download...")

    for i in range(info.num_files()):
        if active_tasks[h_hash]["cancel"]: break
        
        file = info.file_at(i)
        if file.size < 2 * 1024 * 1024: continue # Skip files < 2MB (junk/ads)
        
        # ✅ Prioritize only THIS file
        handle.file_priority(i, 1)
        f_name = file.path.split('/')[-1]
        
        while True:
            if active_tasks[h_hash]["cancel"]: break
            s = handle.status()
            f_prog = handle.file_progress()[i]
            pct = (f_prog / file.size) * 100
            
            ctrl_btn = InlineKeyboardMarkup([[
                InlineKeyboardButton("⏸ Pause", f"pa_{h_hash}"),
                InlineKeyboardButton("❌ Cancel", f"ca_{h_hash}")
            ]])
            
            try:
                await status_msg.edit(
                    f"📥 **Downloading Part {i+1}/{info.num_files()}**\n"
                    f"📝 `{f_name}`\n"
                    f"[{get_prog_bar(pct)}] {pct:.1f}%\n"
                    f"🚀 {humanize.naturalsize(s.download_rate)}/s",
                    reply_markup=ctrl_btn
                )
            except: pass
            
            if f_prog >= file.size: break
            await asyncio.sleep(5)

        # 📤 Upload and then Delete (to free disk for next file)
        if not active_tasks[h_hash]["cancel"]:
            f_path = os.path.join("./downloads/", file.path)
            meta = PTN.parse(f_name)
            cap = f"🎬 **{meta.get('title', f_name)}**\n📦 Size: {humanize.naturalsize(file.size)}"
            
            await status_msg.edit(f"📤 **Uploading:** `{f_name}`")
            await c.send_document(m.chat.id, document=f_path, caption=cap, 
                                  progress=up_progress, progress_args=(status_msg, f_name, time.time()))
            
            if os.path.exists(f_path): os.remove(f_path)
            handle.file_priority(i, 0) # Free disk space

    await status_msg.edit("✅ **All files uploaded successfully!**")
    active_tasks.pop(h_hash, None)

@app.on_callback_query(filters.regex(r"^(pa|re|ca)_"))
async def btn_controls(c, q: CallbackQuery):
    act, h_id = q.data.split("_")
    if h_id not in active_tasks: return await q.answer("Task not found.")
    
    h = active_tasks[h_id]["handle"]
    if act == "pa":
        h.pause(); await q.edit_message_reply_markup(InlineKeyboardMarkup([[
            InlineKeyboardButton("▶️ Resume", f"re_{h_id}"),
            InlineKeyboardButton("❌ Cancel", f"ca_{h_id}")
        ]]))
    elif act == "re":
        h.resume(); await q.answer("Resumed")
    elif act == "ca":
        active_tasks[h_id]["cancel"] = True; ses.remove_torrent(h); await q.message.edit("❌ Cancelled."); active_tasks.pop(h_id, None)

if __name__ == "__main__":
    print("Bot is starting...")
    app.run()
