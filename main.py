import os, re, time, asyncio, threading, requests, psutil
from pyrogram import Client, filters, idle
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.enums import ChatType
from http.server import HTTPServer, BaseHTTPRequestHandler

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
REPO_NAME = os.getenv("REPO_NAME", "").strip()  

# ZEABUR PORT FIX
PORT = int(os.getenv("PORT", 8080))

OWNER_ID = 5344078567
ALLOWED_USER = 5351848105
GROUP_ID = -1003899919015
DESK_CHANNEL_ID = -1003700822969

app = Client("HarsubBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, workers=16)

users_data = {}
wm_positions = {} 

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
        try: chat_info = await c.get_chat(GROUP_ID); invite_link = chat_info.invite_link or "https://t.me/Mangajii"
        except: invite_link = "https://t.me/Mangajii"
        await m.reply(f"❌ **Aap is Bot ko Private mein use nahi kar sakte!**\n\n👉 Humara [Official Group]({invite_link}) join karein.", disable_web_page_preview=True)
        return False
    return is_authorized(m)

async def is_github_busy():
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    url_in_progress = f"https://api.github.com/repos/{REPO_NAME}/actions/runs?status=in_progress"
    url_queued = f"https://api.github.com/repos/{REPO_NAME}/actions/runs?status=queued"
    try:
        r_in = await asyncio.to_thread(requests.get, url_in_progress, headers=headers)
        r_qu = await asyncio.to_thread(requests.get, url_queued, headers=headers)
        if r_in.status_code == 200 and r_qu.status_code == 200:
            count = r_in.json().get("total_count", 0) + r_qu.json().get("total_count", 0)
            return count > 0
    except Exception as e:
        print(f"GH API Error: {e}")
    return False

def _send_to_github(task):
    url = f"https://api.github.com/repos/{REPO_NAME}/actions/workflows/encode.yml/dispatches"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    payload = {"ref": "main", "inputs": task}
    try:
        r = requests.post(url, headers=headers, json=payload)
        return (True, "Success") if r.status_code == 204 else (False, f"Code {r.status_code}: {r.text}")
    except Exception as e: return False, str(e)

async def trigger_github(task):
    return await asyncio.to_thread(_send_to_github, task)

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
    except: pass
    return "none"

@app.on_message(filters.command(["start", "stats", "addposition", "admark", "deletmark", "addfont", "removefont"]))
async def general_cmds(c, m: Message):
    cmd = m.command[0]
    if cmd == "start" and m.chat.type == ChatType.PRIVATE:
        if m.from_user.id in [OWNER_ID, ALLOWED_USER]: return await m.reply("🙋‍♂️ Welcome Owner!")
        return await check_command_privacy(c, m)
    if not await check_command_privacy(c, m): return

    if cmd == "stats":
        ram = psutil.virtual_memory(); cpu = psutil.cpu_percent()
        await m.reply(f"📊 **Bot diagnostics:**\n🖥️ CPU: `{cpu}%`\n💾 RAM: `{ram.percent}%`")
    elif cmd == "addposition":
        if len(m.command) < 2 or m.command[1].lower() not in ["left", "right"]: return await m.reply("❌ Usage: /addposition left|right")
        wm_positions[m.chat.id] = m.command[1].lower()
        await m.reply(f"✅ Watermark position updated: **{m.command[1].upper()}**")
    elif cmd in ["admark", "addfont"]:
        if not m.reply_to_message or not (m.reply_to_message.photo or m.reply_to_message.document): return await m.reply("❌ Reply to a file.")
        msg_link = f"https://t.me/c/{str(m.chat.id)[4:]}/{m.reply_to_message.id}"
        t_name = "watermark" if cmd == "admark" else "file"
        pinned = await m.reply(f"ID – {m.from_user.id}\nLink – {msg_link}\nName – {t_name}")
        await pinned.pin()
        await m.reply(f"✅ Configuration saved.")
    elif cmd in ["deletmark", "removefont"]:
        chat = await c.get_chat(m.chat.id)
        t_name = "watermark" if cmd == "deletmark" else "file"
        if chat.pinned_message and f"Name – {t_name}" in chat.pinned_message.text:
            await chat.pinned_message.unpin()
            await m.reply("🗑️ Registry removed.")
        else: await m.reply("❌ Registry not found.")

RES_CMD_MAP = {"1080t": "1080p", "720t": "720p", "480t": "480p"}

@app.on_message(filters.command(["1080t", "720t", "480t"]))
async def compress_cmd(c, m: Message):
    if not await check_command_privacy(c, m): return
    media = m.reply_to_message.video or m.reply_to_message.document or m.reply_to_message.animation if m.reply_to_message else None
    if not media: return await m.reply("❌ Compression task ke liye kisi valid video/document par reply karein.")
    
    if await is_github_busy(): return await m.reply("⏳ **Bot is currently BUSY!**\nPichla task chal raha hai. Ek complete hone dein.")

    cmd = RES_CMD_MAP[m.command[0].lower()]
    orig_name = getattr(media, "file_name", "output.mp4")
    st = await m.reply("⏳ **Task Dispatched to Server!**\n*Note: GitHub server start hone me 1-2 min lagte hain.*")
    font_link = await get_pinned_file_link(m.chat.id, "file")

    payload = {
        "task_type": "compress", "video_id": f"https://t.me/c/{str(m.chat.id)[4:]}/{m.reply_to_message.id}",
        "sub_id": "none", "chat_id": str(m.chat.id), "user_id": str(m.from_user.id),
        "resolution": cmd, "wm_id": "none", "wm_pos": "none", "rename": orig_name, 
        "font_link": font_link, "trigger_msg_id": str(st.id)
    }
    await trigger_github(payload)

@app.on_message(filters.command("hp"))
async def hsub_cmd(c, m: Message):
    if not await check_command_privacy(c, m): return
    media = m.reply_to_message.video or m.reply_to_message.document or m.reply_to_message.animation if m.reply_to_message else None
    if not media: return await m.reply("❌ Hardsub ke liye kisi forwarded video par reply karein.")
    
    if await is_github_busy(): return await m.reply("⏳ **Bot is currently BUSY!**\nPichla task chal raha hai. Ek complete hone dein.")

    orig_name = getattr(media, "file_name", "output.mp4")
    await m.reply("send file (vtt/srt/ass)")
    users_data[m.from_user.id] = {"video_msg_id": m.reply_to_message.id, "chat_id": m.chat.id, "state": "WAIT_SUB", "rename": "none", "orig_name": orig_name}

async def prompt_watermark_or_execute(c, m, user_id, session):
    wm_link = await get_pinned_file_link(session["chat_id"], "watermark")
    if wm_link != "none":
        session["state"] = "WAIT_WM_CHOICE"
        await m.reply("Add watermark\nSend A skip type s")
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
            await m.reply("Rename type R\nSame name type s")
        elif text == "S":
            session["sub_msg_link"] = "none"
            session["state"] = "WAIT_RENAME_CHOICE"
            await m.reply("Rename type R\nSame name type s")
        else: await m.reply("❌ Invalid format! Please send a valid subtitle file (.srt, .ass, .vtt, .txt) or type `S` to skip.")
        return

    if state == "WAIT_RENAME_CHOICE":
        if text == "R": session["state"] = "WAIT_RENAME_VALUE"; await m.reply("Send name")
        elif text == "S": session["rename"] = session["orig_name"]; await prompt_watermark_or_execute(c, m, user_id, session)
        else: await m.reply("❌ Invalid! Type `R` to rename or `S` to skip.")
        return
            
    elif state == "WAIT_RENAME_VALUE":
        if not text: return await m.reply("❌ Please send a valid text name.")
        raw_name = m.text.strip()
        if raw_name.lower().endswith(".mp4"): raw_name = raw_name[:-4]
        session["rename"] = re.sub(r'[^\w\-_]', '_', raw_name) + ".mp4"
        await prompt_watermark_or_execute(c, m, user_id, session)
        return
        
    elif state == "WAIT_WM_CHOICE":
        if text == "A": session["watermark"] = "yes"
        elif text == "S": session["watermark"] = "no"
        else: return await m.reply("❌ Invalid! Type `A` to add watermark or `S` to skip.")
        await execute_dispatch_hardsub(user_id, m)

async def execute_dispatch_hardsub(user_id, msg: Message):
    data = users_data.pop(user_id)
    if await is_github_busy(): return await msg.reply("⏳ **Bot is currently BUSY!**\nPichla task chal raha hai.")
    st = await msg.reply(f"⏳ **Task Dispatched to Server!**\n*Note: GitHub server start hone me 1-2 min lagte hain.*")
    wm_link = "none"
    wm_pos = "right"
    if data.get("watermark") == "yes":
        wm_link = await get_pinned_file_link(data["chat_id"], "watermark")
        wm_pos = wm_positions.get(data["chat_id"], "right")

    payload = {
        "task_type": "hardsub", "video_id": f"https://t.me/c/{str(data['chat_id'])[4:]}/{data['video_msg_id']}",
        "sub_id": data.get("sub_msg_link", "none"), "chat_id": str(data["chat_id"]), "user_id": str(user_id),
        "resolution": "none", "wm_id": wm_link, "wm_pos": wm_pos, "rename": data.get("rename", "none"),
        "font_link": await get_pinned_file_link(data["chat_id"], "file"), "trigger_msg_id": str(st.id)
    }
    await trigger_github(payload)

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
            await q.message.edit("🛑 **Task Cancelled Successfully!**")
            await q.answer("Task Aborted", show_alert=True)
        else: await q.answer("Active status par koi task nahi mila.", show_alert=True)
    except Exception as e: await q.answer(f"Abort Exception: {e}", show_alert=True)

class Health(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"Bot Operational")

async def main():
    await app.start()
    print("🚀 Zeabur Controller Bot Started Successfully!")
    threading.Thread(target=lambda: HTTPServer(("0.0.0.0", PORT), Health).serve_forever(), daemon=True).start()
    await idle()
    await app.stop()

if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
