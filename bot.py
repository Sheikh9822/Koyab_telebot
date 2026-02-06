import os
import asyncio
import time
import json
import libtorrent as lt
import PTN
import humanize
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from googleapiclient.discovery import build
from google.oauth2 import service_account
from googleapiclient.http import MediaFileUpload

# --- 1. EXTRACT SERVICE ACCOUNT JSON ---
SERVICE_ACCOUNT_JSON = os.environ.get("SERVICE_ACCOUNT_JSON")
with open('credentials.json', 'w') as f:
    f.write(SERVICE_ACCOUNT_JSON)

# --- 2. CONFIGURATION ---
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID", "")

# GDrive Setup
SCOPES = ['https://www.googleapis.com/auth/drive']
creds = service_account.Credentials.from_service_account_file('credentials.json', scopes=SCOPES)
drive_service = build('drive', 'v3', credentials=creds)

# Torrent Setup
app = Client("GDriveTorrentBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
ses = lt.session()
ses.listen_on(6881, 6891)
settings = {'announce_to_all_trackers': True, 'enable_dht': True, 'download_rate_limit': 0}
ses.apply_settings(settings)

TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce", "udp://9.rarbg.com:2810/announce",
    "udp://tracker.openbittorrent.com:6969/announce", "udp://exodus.desync.com:6969/announce",
    "udp://open.stealth.si:80/announce"
]

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

# --- 4. BOT COMMANDS ---

@app.on_message(filters.command("start"))
async def start(c, m):
    await m.reply_text("👋 **Torrent to GDrive Bot**\nSend a magnet link to start.")

@app.on_message(filters.regex(r"magnet:\?xt=urn:btih:[a-zA-Z0-9]+"))
async def handle_magnet(c, m):
    magnet = m.text
    params = {'save_path': './downloads/', 'storage_mode': lt.storage_mode_t.storage_mode_sparse}
    handle = lt.add_magnet_uri(ses, magnet, params)
    
    for t in TRACKERS: handle.add_tracker({'url': t, 'tier': 0})

    status_msg = await m.reply_text("🧲 **Fetching Metadata...**")
    while not handle.has_metadata(): await asyncio.sleep(1)
    
    info = handle.get_torrent_info()
    handle.prioritize_files([0] * info.num_files()) # Skip all files initially

    await status_msg.edit(f"📂 **Torrent:** `{info.name()}`\nProcessing {info.num_files()} files sequentially...")

    for i in range(info.num_files()):
        file = info.file_at(i)
        if file.size < 5 * 1024 * 1024: continue # Skip files < 5MB
        
        handle.file_priority(i, 4) # Start downloading this file
        f_name = file.path.split('/')[-1]
        
        # Download Progress Loop
        while True:
            s = handle.status()
            f_prog = handle.file_progress()[i]
            pct = (f_prog / file.size) * 100
            
            try:
                await status_msg.edit(
                    f"📥 **Downloading:** `{f_name}`\n"
                    f"[{get_prog_bar(pct)}] {pct:.1f}%\n"
                    f"🚀 Speed: {humanize.naturalsize(s.download_rate)}/s"
                )
            except: pass
            
            if f_prog >= file.size: break
            await asyncio.sleep(5)

        # Upload to Google Drive
        await status_msg.edit(f"☁️ **Uploading to GDrive:** `{f_name}`")
        f_path = os.path.join("./downloads/", file.path)
        
        try:
            loop = asyncio.get_event_loop()
            link = await loop.run_in_executor(None, upload_to_gdrive, f_path, f_name)
            await m.reply_text(f"✅ **Uploaded:** `{f_name}`\n🔗 [View in GDrive]({link})", disable_web_page_preview=True)
        except Exception as e:
            await m.reply_text(f"❌ Upload Failed: {e}")
        finally:
            if os.path.exists(f_path): os.remove(f_path)
            handle.file_priority(i, 0) # Clear from disk and stop seeding

    await status_msg.edit("🏁 **Torrent processing finished.**")

if __name__ == "__main__":
    print("Bot is starting...")
    app.run()
