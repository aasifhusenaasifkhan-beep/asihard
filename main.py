import os, re, time, asyncio, threading, requests, psutil
from pyrogram import Client, filters, idle
from pyrogram.types import Message, CallbackQuery
from pyrogram.enums import ChatType
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------- Environment ----------------
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
REPO_NAME = os.getenv("REPO_NAME", "").strip()                 # "owner/repo"
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main").strip()     # branch that has .github/workflows/encode.yml
MAX_RUNS = int(os.getenv("MAX_RUNS", "2"))                     # how many encodes may run at the same time
PORT = int(os.getenv("PORT", "8080"))

OWNER_ID = 5344078567
ALLOWED_USER = 5351848105
GROUP_ID = -1003899919015

WORKFLOW_FILE = "encode.yml"
GH_API = "https://api.github.com"

app = Client("HarsubBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, workers=16)

users_data = {}
task_queue = asyncio.Queue()


def gh_headers():
    return {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}


def file_name_of(media):
    # Telegram videos often have file_name = None -> GitHub rejects null inputs (HTTP 422)
    return getattr(media, "file_name", None) or "output.mp4"


def is_authorized(m: Message):
    if not m.from_user:
        return False
    if m.from_user.id in [OWNER_ID, ALLOWED_USER]:
        return True
    return bool(m.chat and m.chat.id == GROUP_ID)


async def check_command_privacy(c, m: Message):
    is_pm = m.chat.type == ChatType.PRIVATE
    if is_pm and m.from_user.id in [OWNER_ID, ALLOWED_USER]:
        return True
    if is_pm:
        try:
            chat_info = await c.get_chat(GROUP_ID)
            invite_link = chat_info.invite_link or "https://t.me/Mangajii"
        except Exception:
            invite_link = "https://t.me/Mangajii"
        await m.reply(f"❌ **Aap is Bot ko Private mein use nahi kar sakte!**\n\n👉 Humara [Official Group]({invite_link}) join karein.", disable_web_page_preview=True)
        return False
    return is_authorized(m)


# ---------------- GitHub Actions ----------------
def _count_runs():
    """Runs of encode.yml that are running or waiting. None if GitHub API failed."""
    total = 0
    for status in ("in_progress", "queued"):
        url = f"{GH_API}/repos/{REPO_NAME}/actions/workflows/{WORKFLOW_FILE}/runs?status={status}&per_page=1"
        r = requests.get(url, headers=gh_headers(), timeout=15)
        if r.status_code != 200:
            print(f"GitHub runs API {r.status_code}: {r.text[:200]}")
            return None
        total += r.json().get("total_count", 0)
    return total


async def active_runs():
    try:
        return await asyncio.to_thread(_count_runs)
    except Exception as e:
        print(f"API Error: {e}")
        return None


async def is_server_busy():
    n = await active_runs()
    return n is not None and n >= MAX_RUNS


def _dispatch_worker(task):
    url = f"{GH_API}/repos/{REPO_NAME}/actions/workflows/{WORKFLOW_FILE}/dispatches"
    inputs = {k: str(v) for k, v in task.items()}   # workflow_dispatch inputs must all be strings
    try:
        r = requests.post(url, headers=gh_headers(), json={"ref": GITHUB_BRANCH, "inputs": inputs}, timeout=15)
        return (True, "Success") if r.status_code == 204 else (False, f"Code {r.status_code}: {r.text}")
    except Exception as e:
        return False, str(e)


async def trigger_dispatch(task):
    return await asyncio.to_thread(_dispatch_worker, task)


async def queue_worker():
    while True:
        payload = await task_queue.get()
        try:
            while await is_server_busy():
                await asyncio.sleep(10)
            ok, msg = await trigger_dispatch(payload)
            if ok:
                # give GitHub a moment to register the run, otherwise the next
                # check would not see it and we could start too many at once
                await asyncio.sleep(6)
            else:
                print(f"Dispatch Error: {msg}")
                try:
                    await app.edit_message_text(
                        int(payload["chat_id"]), int(payload["trigger_msg_id"]),
                        f"❌ **Task start nahi ho paya.**\n`{msg[:300]}`")
                except Exception:
                    pass
        except Exception as e:
            print(f"Worker error: {e}")
        finally:
            task_queue.task_done()


async def enqueue_task(payload):
    await task_queue.put(payload)


async def get_pinned_file_link(chat_id, target_name):
    try:
        chat = await app.get_chat(chat_id)
        if chat.pinned_message and chat.pinned_message.text and f"Name – {target_name}" in chat.pinned_message.text:
            match = re.search(r"Link – (https://\S+)", chat.pinned_message.text)
            if match:
                return match.group(1)
        async for msg in app.get_chat_history(chat_id, limit=50):
            if msg.text and f"Name – {target_name}" in msg.text:
                match = re.search(r"Link – (https://\S+)", msg.text)
                if match:
                    return match.group(1)
    except Exception:
        pass
    return "none"


# ---------------- Commands ----------------
@app.on_message(filters.command(["start", "help", "cancel", "stats", "addfont", "removefont"]))
async def general_cmds(c, m: Message):
    cmd = m.command[0]
    if cmd == "start" and m.chat.type == ChatType.PRIVATE:
        if m.from_user.id in [OWNER_ID, ALLOWED_USER]:
            return await m.reply("🙋‍♂️ Welcome Master!")
        return await check_command_privacy(c, m)
    if not await check_command_privacy(c, m):
        return

    if cmd == "help":
        await m.reply(
            "🤖 **Bot Usage Guide:**\n\n"
            "1️⃣ **Compress Video:** Video par reply karein `/1080g`, `/720g`, ya `/480g`\n"
            "2️⃣ **Hardsub Video:** Video par reply karein `/sub` aur subtitle send karein.\n"
            "3️⃣ **Custom Font:** Font file par reply karke `/addfont` (hatane ke liye `/removefont`)\n"
            "4️⃣ **Cancel Setup State:** `/cancel` type karein."
        )

    elif cmd == "cancel":
        if users_data.pop(m.from_user.id, None) is not None:
            await m.reply("✅ Active setup cancel kar diya gaya.")
        else:
            await m.reply("❌ Koi active process nahi chal raha tha.")

    elif cmd == "stats":
        ram = psutil.virtual_memory()
        runs = await active_runs()
        await m.reply(
            "📊 **System Status:**\n"
            f"🖥️ CPU: `{psutil.cpu_percent()}%`  💾 RAM: `{ram.percent}%`\n"
            f"⚙️ Encoding now: `{'?' if runs is None else runs}` / `{MAX_RUNS}`\n"
            f"📂 Waiting in queue: `{task_queue.qsize()}`")

    elif cmd == "addfont":
        if not m.reply_to_message or not (m.reply_to_message.photo or m.reply_to_message.document):
            return await m.reply("❌ File par reply karein.")
        msg_link = f"https://t.me/c/{str(m.chat.id)[4:]}/{m.reply_to_message.id}"
        pinned = await m.reply(f"ID – {m.from_user.id}\nLink – {msg_link}\nName – file")
        try:
            await pinned.pin()
        except Exception:
            pass
        await m.reply("✅ Configuration saved successfully.")

    elif cmd == "removefont":
        chat = await c.get_chat(m.chat.id)
        if chat.pinned_message and "Name – file" in (chat.pinned_message.text or ""):
            await chat.pinned_message.unpin()
            await m.reply("🗑️ Config removed.")
        else:
            await m.reply("❌ Config nahi mila.")


RES_CMD_MAP = {"1080g": "1080p", "720g": "720p", "480g": "480p"}


def _replied_media(m: Message):
    r = m.reply_to_message
    return (r.video or r.document or r.animation) if r else None


@app.on_message(filters.command(["1080g", "720g", "480g"]))
async def compress_cmd(c, m: Message):
    if not await check_command_privacy(c, m):
        return
    media = _replied_media(m)
    if not media:
        return await m.reply("❌ Kisi valid video par reply karein.")

    res = RES_CMD_MAP[m.command[0].lower()]
    busy = await is_server_busy()
    st = await m.reply("⏳ **Task Queued!**\nServer busy hain, aapka task queue me lag gaya hai aur turn aane par automatic start hoga."
                       if busy else "⏳ **Task Dispatched to Cloud Processing Node...**")
    font_link = await get_pinned_file_link(m.chat.id, "file")

    await enqueue_task({
        "task_type": "compress", "video_id": f"https://t.me/c/{str(m.chat.id)[4:]}/{m.reply_to_message.id}",
        "sub_id": "none", "chat_id": str(m.chat.id), "user_id": str(m.from_user.id),
        "resolution": res, "rename": file_name_of(media),
        "font_link": font_link, "trigger_msg_id": str(st.id),
    })


@app.on_message(filters.command("sub"))
async def hsub_cmd(c, m: Message):
    if not await check_command_privacy(c, m):
        return
    media = _replied_media(m)
    if not media:
        return await m.reply("❌ Hardsub ke liye video par reply karein.")

    await m.reply("Send subtitle file (.srt, .ass, .vtt) ya skip karne ke liye `S` type karein.")
    users_data[m.from_user.id] = {"video_msg_id": m.reply_to_message.id, "chat_id": m.chat.id,
                                  "state": "WAIT_SUB", "rename": "none", "orig_name": file_name_of(media)}


@app.on_message(filters.text | filters.document)
async def replies_controller(c, m: Message):
    if not m.from_user or (m.text and m.text.startswith("/")):
        return
    user_id = m.from_user.id
    session = users_data.get(user_id)
    if not session or session["chat_id"] != m.chat.id:
        return

    state = session.get("state")
    text = m.text.strip().upper() if m.text else ""

    if state == "WAIT_SUB":
        if m.document and m.document.file_name and m.document.file_name.lower().endswith(('.srt', '.ass', '.ssa', '.vtt', '.txt')):
            session["sub_msg_link"] = f"https://t.me/c/{str(m.chat.id)[4:]}/{m.id}"
            session["state"] = "WAIT_RENAME_CHOICE"
            await m.reply("Rename ke liye `R` / Same name ke liye `S` type karein.")
        elif text == "S":
            session["sub_msg_link"] = "none"
            session["state"] = "WAIT_RENAME_CHOICE"
            await m.reply("Rename ke liye `R` / Same name ke liye `S` type karein.")
        else:
            await m.reply("❌ Invalid format! Please send a valid subtitle file ya type `S`.")

    elif state == "WAIT_RENAME_CHOICE":
        if text == "R":
            session["state"] = "WAIT_RENAME_VALUE"
            await m.reply("Send new file name:")
        elif text == "S":
            session["rename"] = session["orig_name"]
            await execute_dispatch_hardsub(user_id, m)
        else:
            await m.reply("❌ Type `R` to rename ya `S` to skip.")

    elif state == "WAIT_RENAME_VALUE":
        if not m.text or not m.text.strip():
            return await m.reply("❌ Invalid name.")
        raw_name = m.text.strip()
        if raw_name.lower().endswith(".mp4"):
            raw_name = raw_name[:-4]
        clean_name = re.sub(r'[\\/:*?"<>|]', '', raw_name).strip()
        if not clean_name:
            return await m.reply("❌ Invalid name.")
        session["rename"] = clean_name + ".mp4"
        await execute_dispatch_hardsub(user_id, m)


async def execute_dispatch_hardsub(user_id, msg: Message):
    data = users_data.pop(user_id, None)
    if not data:
        return
    busy = await is_server_busy()
    st = await msg.reply("⏳ **Task Queued!**\nServer busy hain, aapka task queue me lag gaya hai aur turn aane par automatic start hoga."
                         if busy else "⏳ **Task Dispatched to Cloud Processing Node...**")
    await enqueue_task({
        "task_type": "hardsub", "video_id": f"https://t.me/c/{str(data['chat_id'])[4:]}/{data['video_msg_id']}",
        "sub_id": data.get("sub_msg_link", "none"), "chat_id": str(data["chat_id"]), "user_id": str(user_id),
        "resolution": "none", "rename": data.get("rename", "none"),
        "font_link": await get_pinned_file_link(data["chat_id"], "file"), "trigger_msg_id": str(st.id),
    })


# ---------------- Cancel button (cancels ONLY that task's run) ----------------
@app.on_callback_query(filters.regex(r"^cancel_(\d+)_(\d+)$"))
async def cancel_run_callback(c, q: CallbackQuery):
    run_id, owner_id = q.matches[0].group(1), q.matches[0].group(2)
    if q.from_user.id not in [OWNER_ID, ALLOWED_USER] and str(q.from_user.id) != owner_id:
        return await q.answer("❌ Ye task aapka nahi hai.", show_alert=True)
    try:
        r = await asyncio.to_thread(
            requests.post, f"{GH_API}/repos/{REPO_NAME}/actions/runs/{run_id}/cancel",
            headers=gh_headers(), timeout=15)
        if r.status_code == 202:
            await q.message.edit("🛑 **Process Cancelled Successfully!**")
            await q.answer("Task Aborted", show_alert=True)
        else:
            await q.answer("Task pehle hi khatam ho chuka hai.", show_alert=True)
    except Exception as e:
        await q.answer(f"Abort Exception: {e}", show_alert=True)


@app.on_callback_query(filters.regex(r"^cancel_active_run$"))
async def old_cancel_button(c, q: CallbackQuery):
    await q.answer("Ye purana button hai. Naya task chalayein to cancel button kaam karega.", show_alert=True)


# ---------------- Render: health server + keep-alive ----------------
class Health(BaseHTTPRequestHandler):
    def _ok(self, body=True):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        if body:
            self.wfile.write(b"Server Active & Running")

    def do_GET(self):
        self._ok()

    def do_HEAD(self):        # UptimeRobot & co. often use HEAD
        self._ok(body=False)

    def log_message(self, *a):
        pass


async def keep_alive():
    """Render's free plan sleeps a web service after ~15 min without web traffic.
    RENDER_EXTERNAL_URL is set by Render itself; we ping it every 10 minutes."""
    url = os.getenv("RENDER_EXTERNAL_URL", "").strip()
    if not url:
        return
    while True:
        await asyncio.sleep(600)
        try:
            await asyncio.to_thread(requests.get, url, timeout=20)
        except Exception:
            pass


async def main():
    threading.Thread(target=lambda: ThreadingHTTPServer(("0.0.0.0", PORT), Health).serve_forever(), daemon=True).start()
    print(f"📡 Web server running on port {PORT}")

    await app.start()
    print("🚀 Bot Client Connected Successfully!")
    asyncio.create_task(queue_worker())
    asyncio.create_task(keep_alive())
    await idle()
    await app.stop()


if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
