import os, asyncio, time, libtorrent as lt, PTN, humanize
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from pyrogram import Client as PyroClient

# --- CONFIG ---
TOKEN = os.environ.get("BOT_TOKEN")
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")

# Initialize Libtorrent Session
ses = lt.session({'listen_interfaces': '0.0.0.0:6881'})
# Initialize Pyrogram for Large Uploads (Bypasses 50MB limit)
pyro = PyroClient("uploader", api_id=API_ID, api_hash=API_HASH, bot_token=TOKEN)

def get_prog_bar(percentage):
    p = int(percentage / 10)
    return "█" * p + "░" * (10 - p)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚀 I am your PTB Torrent Bot. Send me a Magnet link!")

async def handle_magnet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    magnet = update.message.text
    params = {'save_path': './downloads/', 'storage_mode': lt.storage_mode_t.storage_mode_sparse}
    handle = lt.add_magnet_uri(ses, magnet, params)
    
    status_msg = await update.message.reply_text("🧲 Fetching Metadata...")
    
    while not handle.has_metadata():
        await asyncio.sleep(1)
    
    info = handle.get_torrent_info()
    handle.prioritize_files([0] * info.num_files()) # Stop all files
    
    await status_msg.edit_text(f"📦 Found {info.num_files()} files. Downloading one-by-one to save disk...")

    for i in range(info.num_files()):
        file = info.file_at(i)
        if file.size < 2000000: continue # Skip small files
        
        handle.file_priority(i, 1) # Start this file
        file_name = file.path.split('/')[-1]
        
        # Download Loop
        while True:
            s = handle.status()
            f_prog = handle.file_progress()[i]
            pct = (f_prog / file.size) * 100
            try:
                await status_msg.edit_text(
                    f"📥 **Downloading:** `{file_name}`\n"
                    f"[{get_prog_bar(pct)}] {pct:.2f}%\n"
                    f"🚀 Speed: {humanize.naturalsize(s.download_rate)}/s"
                )
            except: pass
            if f_prog >= file.size: break
            await asyncio.sleep(5)

        # Upload using Pyrogram (To bypass 50MB limit)
        await status_msg.edit_text(f"📤 **Uploading to Cloud:** `{file_name}`")
        file_path = os.path.join("./downloads/", file.path)
        
        async with pyro:
            await pyro.send_document(
                chat_id=update.effective_chat.id,
                document=file_path,
                caption=f"✅ `{file_name}`"
            )
        
        # Cleanup
        if os.path.exists(file_path):
            os.remove(file_path)
        handle.file_priority(i, 0)

    await status_msg.edit_text("✅ All files uploaded successfully!")

if __name__ == '__main__':
    app = ApplicationBuilder().token(TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex(r"magnet:\?xt=urn:btih:"), handle_magnet))
    
    print("Bot is running...")
    app.run_polling()
