import os, asyncio, time, libtorrent as lt, PTN, humanize
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

# --- CONFIG ---
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

app = Client("TorrentBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- OPTIMIZED SESSION SETTINGS ---
ses = lt.session()
ses.listen_on(6881, 6891)
settings = {
    'user_agent': 'python-libtorrent/1.2.9',
    'announce_to_all_trackers': True,
    'announce_to_all_tiers': True,
    'enable_dht': True,
    'enable_lsd': True,
    'enable_upnp': True,
    'enable_natpmp': True,
    'download_rate_limit': 0, # Unlimited
    'upload_rate_limit': 100 * 1024, # Limit upload to save bandwidth
    'connections_limit': 200,
}
ses.apply_settings(settings)

# High-Performance Public Trackers
TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://9.rarbg.com:2810/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "http://tracker.openbittorrent.com:80/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://open.stealth.si:80/announce",
    "udp://www.torrent.eu.org:451/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://retracker.lanta-net.ru:2710/announce",
    "udp://tracker.tiny-vps.com:6969/announce"
]

active_tasks = {}

def get_prog_bar(pct):
    p = int(pct / 10)
    return "█" * p + "░" * (10 - p)

async def up_progress(current, total, msg, filename, start_time):
    if time.time() - up_progress.last_up < 5: return
    up_progress.last_up = time.time()
    pct = (current / total) * 100
    speed = current / (time.time() - start_time) if (time.time() - start_time) > 0 else 0
    text = (f"📤 **Uploading:** `{filename}`\n"
            f"[{get_prog_bar(pct)}] {pct:.1f}%\n"
            f"🚀 Speed: {humanize.naturalsize(speed)}/s")
    try: await msg.edit_text(text)
    except: pass
up_progress.last_up = 0

@app.on_message(filters.command("start"))
async def start_cmd(c, m):
    await m.reply_text("🚀 **Torrent to Telegram Cloud (Optimized)**\nSend me a magnet link.")

@app.on_message(filters.regex(r"magnet:\?xt=urn:btih:[a-zA-Z0-9]+"))
async def handle_magnet(c, m):
    magnet = m.text
    params = {
        'save_path': './downloads/',
        'storage_mode': lt.storage_mode_t.storage_mode_sparse,
    }
    
    handle = lt.add_magnet_uri(ses, magnet, params)
    
    # Manually add trackers to the handle to boost peer discovery
    for tracker in TRACKERS:
        handle.add_tracker({'url': tracker, 'tier': 0})

    status_msg = await m.reply_text("🧲 **Fetching Metadata...**\n(Finding peers may take 1-2 minutes)")
    
    # Wait for metadata
    timeout = 0
    while not handle.has_metadata():
        timeout += 1
        if timeout > 300: # 5 minute timeout
            return await status_msg.edit("❌ Metadata Timeout. Magnet might be dead or has 0 seeders.")
        await asyncio.sleep(1)
    
    info = handle.get_torrent_info()
    h_hash = str(handle.info_hash())
    active_tasks[h_hash] = {"handle": handle, "cancel": False}
    
    handle.prioritize_files([0] * info.num_files())
    await status_msg.edit(f"📂 **Found {info.num_files()} files.** Starting high-speed download...")

    for i in range(info.num_files()):
        if active_tasks[h_hash]["cancel"]: break
        file = info.file_at(i)
        if file.size < 2 * 1024 * 1024: continue 
        
        handle.file_priority(i, 4) # Priority 4 is "Normal"
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
            
            # Show Peers and Seeders count in status
            try:
                await status_msg.edit(
                    f"📥 **Downloading Part {i+1}/{info.num_files()}**\n"
                    f"📝 `{f_name}`\n"
                    f"[{get_prog_bar(pct)}] {pct:.1f}%\n"
                    f"🚀 {humanize.naturalsize(s.download_rate)}/s\n"
                    f"👥 Peers: {s.num_peers} | Seeds: {s.num_seeds}",
                    reply_markup=ctrl_btn
                )
            except: pass
            
            if f_prog >= file.size: break
            await asyncio.sleep(5)

        if not active_tasks[h_hash]["cancel"]:
            f_path = os.path.join("./downloads/", file.path)
            meta = PTN.parse(f_name)
            cap = f"🎬 **{meta.get('title', f_name)}**\n📦 Size: {humanize.naturalsize(file.size)}"
            
            await status_msg.edit(f"📤 **Uploading:** `{f_name}`")
            await c.send_document(m.chat.id, document=f_path, caption=cap, 
                                  progress=up_progress, progress_args=(status_msg, f_name, time.time()))
            
            if os.path.exists(f_path): os.remove(f_path)
            handle.file_priority(i, 0)

    await status_msg.edit("✅ **All files uploaded!**")
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
    app.run()
