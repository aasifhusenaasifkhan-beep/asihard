import os, re, time, asyncio, threading, requests, psutil
from pyrogram import Client, filters, idle
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.enums import ChatType
from http.server import HTTPServer, BaseHTTPRequestHandler

# Safe Environment Variables
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
REPO_NAME = os.getenv("REPO_NAME", "").strip()  
PORT = int(os.getenv("PORT", "8080"))

CHAT_ID = int(os.getenv("CHAT_ID", "0")) if os.getenv("CHAT_ID") else 0

OWNER_ID = 5344078567
ALLOWED_USER = 5351848105
GROUP_ID = -1003899919015
DESK_CHANNEL_ID = -1003700822969

app = Client("HarsubBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, workers=16)

users_data = {}
wm_positions = {} 
task_queue = asyncio.Queue()

# --- NEW: dedup lock to prevent the same video being dispatched twice ---
active_video_locks = set()
_lock_guard = asyncio.Lock()

def is_authorized(m: Message):
    if not m.from_user: return False
    u_id = m.from_user.id
    if u_id in [OWNER_ID, ALLOWED_USER]: return True
    if m.chat and m.chat.id == GROUP_ID: return True
    return False

async def check_command_privacy(c, m: Message):
    is_pm = m.chat.type == ChatType.PRIVATE
    if is_pm and m.from_user.id in [OWNER_ID, ALLOWED_USER]: return True
    if is_pm:
        try: 
            chat_info = await c.get_chat(GROUP_ID)
            invite_link = chat_info.invite_link or "https://t.me/Mangajii"
        except: 
            invite_link = "https://t.me/Mangajii"
        await m.reply(f"❌ **Aap is Bot ko Private mein use nahi kar sakte!**\n\n👉 Humara [Official Group]({invite_link}) join karein.", disable_web_page_preview=True)
        return False
    return is_authorized(m)

# Check if 2 server slots are full
async def is_server_busy():
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    url_in_progress = f"https://api.github.com/repos/{REPO_NAME}/actions/runs?status=in_progress"
    url_queued = f"https://api.github.com/repos/{REPO_NAME}/actions/runs?status=queued"
    try:
        r_in = await asyncio.to_thread(requests.get, url_in_progress, headers=headers)
        r_qu = await asyncio.to_thread(requests.get, url_queued, headers=headers)
        if r_in.status_code == 200 and r_qu.status_code == 200:
            count = r_in.json().get("total_count", 0) + r_qu.json().get("total_count", 0)
            return count >= 2
    except Exception as e:
        print(f"API Error: {e}")
    return False

def _dispatch_worker(task):
    url = f"https://api.github.com/repos/{REPO_NAME}/actions/workflows/encode.yml/dispatches"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    payload = {"ref": "main", "inputs": task}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=10)
        return (True, "Success") if r.status_code == 204 else (False, f"Code {r.status_code}: {r.text}")
    except Exception as e: 
        return False, str(e)

async def trigger_dispatch(task):
    return await asyncio.to_thread(_dispatch_worker, task)

async def queue_worker():
    while True:
        payload = await task_queue.get()
        dedup_key = payload.get("_dedup_key")
        try:
            while await is_server_busy():
                await asyncio.sleep(12)
            ok, msg = await trigger_dispatch(payload)
            if not ok:
                print(f"Dispatch Error: {msg}")
        finally:
            # Always release the lock once dispatch attempt is done (success or fail),
            # so a genuinely new request for the same video can go through later.
            if dedup_key:
                async with _lock_guard:
                    active_video_locks.discard(dedup_key)
            task_queue.task_done()

async def enqueue_task(payload, dedup_key=None):
    """Adds a task to the queue. If dedup_key is given and a task with the same
    key is already queued/dispatching, the new one is silently rejected instead
    of being queued a second time — this is what was causing double-encodes."""
    if dedup_key:
        async with _lock_guard:
            if dedup_key in active_video_locks:
                return False
            active_video_locks.add(dedup_key)
        payload["_dedup_key"] = dedup_key
    await task_queue.put(payload)
    return True

async def get_pinned_file_link(chat_id, target_name):
    try:
        chat = await app.get_chat(chat_id)
        if chat.pinned_message and chat.pinned_message.text and f"Name – {target_name}" in chat.pinned_message.text:
            match = re.search(r"Link – (https://\S+)", chat.pinned_message.text)
            if match: return match.group(1)
        async for msg in app.get_chat_history(chat_id, limit=50):
            if msg.text and f"Name – {target_name}" in msg.text:
                match = re.search(r"Link – (https://\S+)", msg.text)
                if match: return match.group(1)
    except: 
        pass
    return "none"

@app.on_message(filters.command(["start", "help", "cancel", "stats", "addposition", "admark", "deletmark", "addfont", "removefont"]))
async def general_cmds(c, m: Message):
    cmd = m.command[0]
    if cmd == "start" and m.chat.type == ChatType.PRIVATE:
        if m.from_user.id in [OWNER_ID, ALLOWED_USER]: 
            return await m.reply("🙋‍♂️ Welcome Master!")
        return await check_command_privacy(c, m)
    if not await check_command_privacy(c, m): return

    if cmd == "help":
        help_text = (
            "🤖 **Bot Usage Guide:**\n\n"
            "1️⃣ **Compress Video:** Video par reply karein `/1080g`, `/720g`, ya `/480g`\n"
            "2️⃣ **Hardsub Video:** Video par reply karein `/sub` aur subtitle send karein.\n"
            "3️⃣ **Set Watermark Position:** `/addposition left` ya `/addposition right`\n"
            "4️⃣ **Cancel Setup State:** `/cancel` type karein."
        )
        await m.reply(help_text)

    elif cmd == "cancel":
        if m.from_user.id in users_data:
            users_data.pop(m.from_user.id)
            await m.reply("✅ Active setup cancel kar diya gaya.")
        else:
            await m.reply("❌ Koi active process nahi chal raha tha.")

    elif cmd == "stats":
        ram = psutil.virtual_memory()
        cpu = psutil.cpu_percent()
        await m.reply(f"📊 **System Status:**\n🖥️ CPU: `{cpu}%`\n💾 RAM: `{ram.percent}%`\n📂 Queue Size: `{task_queue.qsize()}`")

    elif cmd == "addposition":
        if len(m.command) < 2 or m.command[1].lower() not in ["left", "right"]: 
            return await m.reply("❌ Usage: `/addposition left` ya `/addposition right`")
        wm_positions[m.chat.id] = m.command[1].lower()
        await m.reply(f"✅ Watermark position set to: **{m.command[1].upper()}**")

    elif cmd in ["admark", "addfont"]:
        if not m.reply_to_message or not (m.reply_to_message.photo or m.reply_to_message.document): 
            return await m.reply("❌ File par reply karein.")
        msg_link = f"https://t.me/c/{str(m.chat.id)[4:]}/{m.reply_to_message.id}"
        t_name = "watermark" if cmd == "admark" else "file"
        pinned = await m.reply(f"ID – {m.from_user.id}\nLink – {msg_link}\nName – {t_name}")
        await pinned.pin()
        await m.reply("✅ Configuration saved successfully.")

    elif cmd in ["deletmark", "removefont"]:
        chat = await c.get_chat(m.chat.id)
        t_name = "watermark" if cmd == "deletmark" else "file"
        if chat.pinned_message and f"Name – {t_name}" in chat.pinned_message.text:
            await chat.pinned_message.unpin()
            await m.reply("🗑️ Config removed.")
        else: 
            await m.reply("❌ Config nahi mila.")

RES_CMD_MAP = {"1080g": "1080p", "720g": "720p", "480g": "480p"}

@app.on_message(filters.command(["1080g", "720g", "480g"]))
async def compress_cmd(c, m: Message):
    if not await check_command_privacy(c, m): return
    media = m.reply_to_message.video or m.reply_to_message.document or m.reply_to_message.animation if m.reply_to_message else None
    if not media: 
        return await m.reply("❌ Kisi valid video par reply karein.")
    
    cmd = RES_CMD_MAP[m.command[0].lower()]
    orig_name = getattr(media, "file_name", "output.mp4")
    video_link = f"https://t.me/c/{str(m.chat.id)[4:]}/{m.reply_to_message.id}"

    # --- NEW: dedup key = same video + same target resolution ---
    dedup_key = f"compress:{video_link}:{cmd}"

    font_link = await get_pinned_file_link(m.chat.id, "file")
    payload = {
        "task_type": "compress", "video_id": video_link,
        "sub_id": "none", "chat_id": str(m.chat.id), "user_id": str(m.from_user.id),
        "resolution": cmd, "wm_id": "none", "wm_pos": "none", "rename": orig_name, 
        "font_link": font_link, "trigger_msg_id": "none"
    }

    is_busy = await is_server_busy()
    status_text = "⏳ **Task Queued!**\nServer busy hain, aapka task queue me lag gaya hai aur turn aane par automatic start hoga." if is_busy else "⏳ **Task Dispatched to Cloud Processing Node...**"
    st = await m.reply(status_text)
    payload["trigger_msg_id"] = str(st.id)

    accepted = await enqueue_task(payload, dedup_key=dedup_key)
    if not accepted:
        await st.edit("⚠️ **Ye video already process ho rahi hai (ya queue me hai).** Dobara request bhejne ki zaroorat nahi.")

@app.on_message(filters.command("sub"))
async def hsub_cmd(c, m: Message):
    if not await check_command_privacy(c, m): return
    media = m.reply_to_message.video or m.reply_to_message.document or m.reply_to_message.animation if m.reply_to_message else None
    if not media: 
        return await m.reply("❌ Hardsub ke liye video par reply karein.")

    # --- NEW: if this exact video is already mid-setup or mid-dispatch, block re-entry ---
    video_link = f"https://t.me/c/{str(m.chat.id)[4:]}/{m.reply_to_message.id}"
    existing = next((u for u, s in users_data.items() if s.get("video_msg_id") == m.reply_to_message.id and s.get("chat_id") == m.chat.id), None)
    if existing is not None or f"hardsub:{video_link}" in active_video_locks:
        return await m.reply("⚠️ **Is video ka hardsub setup already chal raha hai / queue me hai.**")

    orig_name = getattr(media, "file_name", "output.mp4")
    await m.reply("Send subtitle file (.srt, .ass, .vtt) ya skip karne ke liye `S` type karein.")
    users_data[m.from_user.id] = {"video_msg_id": m.reply_to_message.id, "chat_id": m.chat.id, "state": "WAIT_SUB", "rename": "none", "orig_name": orig_name, "video_link": video_link}

async def prompt_watermark_or_execute(c, m, user_id, session):
    wm_link = await get_pinned_file_link(session["chat_id"], "watermark")
    if wm_link != "none":
        session["state"] = "WAIT_WM_CHOICE"
        await m.reply("Add watermark? Type `A` for Add ya `S` for Skip.")
    else:
        session["watermark"] = "no"
        await execute_dispatch_hardsub(user_id, m)

@app.on_message(filters.text | filters.document)
async def replies_controller(c, m: Message):
    if not m.from_user or (m.text and m.text.startswith("/")): return
    user_id = m.from_user.id
    if user_id not in users_data: return
    session = users_data[user_id]
    if session["chat_id"] != m.chat.id: return
    
    state, text = session.get("state"), m.text.strip().upper() if m.text else ""
    
    if state == "WAIT_SUB":
        if m.document and m.document.file_name and m.document.file_name.lower().endswith(('.srt', '.ass', '.vtt', '.txt')):
            session["sub_msg_link"] = f"https://t.me/c/{str(m.chat.id)[4:]}/{m.id}"
            session["state"] = "WAIT_RENAME_CHOICE"
            await m.reply("Rename ke liye `R` / Same name ke liye `S` type karein.")
        elif text == "S":
            session["sub_msg_link"] = "none"
            session["state"] = "WAIT_RENAME_CHOICE"
            await m.reply("Rename ke liye `R` / Same name ke liye `S` type karein.")
        else: 
            await m.reply("❌ Invalid format! Please send a valid subtitle file ya type `S`.")
        return

    if state == "WAIT_RENAME_CHOICE":
        if text == "R": 
            session["state"] = "WAIT_RENAME_VALUE"
            await m.reply("Send new file name:")
        elif text == "S": 
            session["rename"] = session["orig_name"]
            await prompt_watermark_or_execute(c, m, user_id, session)
        else: 
            await m.reply("❌ Type `R` to rename ya `S` to skip.")
        return
            
    elif state == "WAIT_RENAME_VALUE":
        if not text: 
            return await m.reply("❌ Invalid name.")
        raw_name = m.text.strip()
        if raw_name.lower().endswith(".mp4"): 
            raw_name = raw_name[:-4]
        clean_name = re.sub(r'[\\/:*?"<>|]', '', raw_name).strip()
        session["rename"] = clean_name + ".mp4"
        await prompt_watermark_or_execute(c, m, user_id, session)
        return
        
    elif state == "WAIT_WM_CHOICE":
        if text == "A": 
            session["watermark"] = "yes"
        elif text == "S": 
            session["watermark"] = "no"
        else: 
            return await m.reply("❌ Type `A` to add watermark ya `S` to skip.")
        await execute_dispatch_hardsub(user_id, m)

async def execute_dispatch_hardsub(user_id, msg: Message):
    data = users_data.pop(user_id)
    video_link = data.get("video_link", f"https://t.me/c/{str(data['chat_id'])[4:]}/{data['video_msg_id']}")
    dedup_key = f"hardsub:{video_link}"

    is_busy = await is_server_busy()
    status_text = "⏳ **Task Queued!**\nServer busy hain, aapka task queue me lag gaya hai aur turn aane par automatic start hoga." if is_busy else "⏳ **Task Dispatched to Cloud Processing Node...**"
    
    st = await msg.reply(status_text)
    wm_link = "none"
    wm_pos = "right"
    if data.get("watermark") == "yes":
        wm_link = await get_pinned_file_link(data["chat_id"], "watermark")
        wm_pos = wm_positions.get(data["chat_id"], "right")

    payload = {
        "task_type": "hardsub", "video_id": video_link,
        "sub_id": data.get("sub_msg_link", "none"), "chat_id": str(data["chat_id"]), "user_id": str(user_id),
        "resolution": "none", "wm_id": wm_link, "wm_pos": wm_pos, "rename": data.get("rename", "none"),
        "font_link": await get_pinned_file_link(data["chat_id"], "file"), "trigger_msg_id": str(st.id)
    }
    accepted = await enqueue_task(payload, dedup_key=dedup_key)
    if not accepted:
        await st.edit("⚠️ **Ye video already process ho rahi hai (ya queue me hai).** Dobara request bhejne ki zaroorat nahi.")

@app.on_callback_query(filters.regex("cancel_active_run"))
async def cancel_run_callback(c, q: CallbackQuery):
    if q.from_user.id not in [OWNER_ID, ALLOWED_USER]:
        return await q.answer("❌ You are not authorized to cancel this task.", show_alert=True)

    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    try:
        cancelled = False
        for status in ["in_progress", "queued"]:
            url = f"https://api.github.com/repos/{REPO_NAME}/actions/runs?status={status}"
            r = await asyncio.to_thread(requests.get, url, headers=headers)
            if r.status_code == 200:
                runs = r.json().get("workflow_runs", [])
                for run in runs:
                    cancel_url = f"https://api.github.com/repos/{REPO_NAME}/actions/runs/{run.get('id')}/cancel"
                    await asyncio.to_thread(requests.post, cancel_url, headers=headers)
                    cancelled = True
        
        if cancelled:
            await q.message.edit("🛑 **Process Cancelled Successfully!**")
            await q.answer("Task Aborted", show_alert=True)
        else: 
            await q.answer("Koi running task nahi mila.", show_alert=True)
    except Exception as e: 
        await q.answer(f"Abort Exception: {e}", show_alert=True)

class Health(BaseHTTPRequestHandler):
    def do_GET(self): 
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Server Active & Running")

async def main():
    threading.Thread(target=lambda: HTTPServer(("0.0.0.0", PORT), Health).serve_forever(), daemon=True).start()
    print(f"📡 Web server running on port {PORT}")
    
    await app.start()
    print("🚀 Bot Client Connected Successfully!")
    asyncio.create_task(queue_worker())
    await idle()
    await app.stop()

if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main()) 
