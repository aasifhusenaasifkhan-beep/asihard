import os, sys, time, asyncio, re, subprocess, requests, html, shutil
import pyrogram.utils, pysubs2
from pyrogram import Client
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.enums import ParseMode
from fontTools.ttLib import TTFont

pyrogram.utils.get_peer_type = lambda p: "channel" if str(p).startswith("-100") else "chat" if str(p).startswith("-") else "user"

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
STRING_SESSION = os.getenv("STRING_SESSION")
TASK_TYPE = os.getenv("TASK_TYPE")
VIDEO_ID = os.getenv("VIDEO_ID")
SUB_ID = os.getenv("SUB_ID")
CHAT_ID = int(os.getenv("CHAT_ID"))
RESOLUTION = os.getenv("RESOLUTION")
WM_POS = os.getenv("WM_POS")
RENAME = os.getenv("RENAME")
FONT_LINK = os.getenv("FONT_LINK")
TRIGGER_MSG_ID = os.getenv("TRIGGER_MSG_ID")

# --- FIX: USER_ID used to crash on default 'none'. Now falls back to CHAT_ID safely. ---
_user_id_raw = os.getenv("USER_ID")
USER_ID = int(_user_id_raw) if _user_id_raw and _user_id_raw.strip().lower() != "none" else CHAT_ID

DESK_CHANNEL_ID = -1003700822969

last_time = 0
start_time = 0
status_msg_id = None
os.makedirs("fonts", exist_ok=True)

# ---------------- HARDCODED WATERMARK (ASS-based, no PNG overlay needed) ----------------
WATERMARK_STYLE_NAME = "ASI_Watermark"
# Stylized "ASI" (mathematical monospace letters) + skull, matching the reference screenshot.
WATERMARK_TEXT = "{\\bord8\\blur5\\shad3} {\\c&HFF00FF&}\U0001D670{\\c&HFFFFFF&}\U0001D682{\\c&H00A0FF&}\U0001D678\u2620"

def sec_to_ass_time(seconds):
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"

def add_hardcoded_watermark(ass_path, duration):
    """Injects the ASI watermark style + dialogue line into an existing .ass file,
    with the End time matched to the real video duration (never over/under-runs)."""
    try:
        try:
            subs = pysubs2.load(ass_path, encoding="utf-8")
        except Exception:
            subs = pysubs2.load(ass_path, encoding="latin-1")

        subs.styles[WATERMARK_STYLE_NAME] = pysubs2.SSAStyle(
            fontname="Arial",
            fontsize=140,
            primarycolor=pysubs2.Color(255, 255, 255),
            secondarycolor=pysubs2.Color(255, 0, 0),
            outlinecolor=pysubs2.Color(0, 0, 0),
            backcolor=pysubs2.Color(0, 0, 0),
            bold=True,
            italic=False,
            scalex=100, scaley=100,
            spacing=0, angle=0,
            borderstyle=1,
            outline=5, shadow=2,
            alignment=9,       # top-right (numpad alignment)
            marginl=10, marginr=40, marginv=40,
            encoding=1,
        )

        end_ms = int(round(duration * 1000))
        watermark_event = pysubs2.SSAEvent(
            start=0,
            end=end_ms,
            style=WATERMARK_STYLE_NAME,
            text=WATERMARK_TEXT,
            layer=10,
        )
        subs.append(watermark_event)
        subs.save(ass_path)
    except Exception as e:
        print(f"Watermark injection failed (continuing without it): {e}")

def reset_prog():
    global last_time, start_time
    last_time = time.time()
    start_time = time.time()

def get_download_bar(percent):
    filled = int(percent / 100 * 20)
    return f"[{'>' * filled}{'-' * (20 - filled)}]"

def get_process_bar(percent):
    filled = int(percent / 100 * 20)
    seq = ["•", "°", ":", "°", "•", ":"]
    bar = "".join(seq[i % len(seq)] for i in range(filled))
    return f"[{bar}{'-' * (20 - filled)}]"

def get_send_bar(percent):
    filled = int(percent / 100 * 20)
    return f"[{'▓' * filled}{'▒' * (20 - filled)}]"

def _sync_http_edit(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
    payload = {
        "chat_id": CHAT_ID,
        "message_id": status_msg_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": {
            "inline_keyboard": [[{"text": "🛑 Cancel Task", "callback_data": "cancel_active_run"}]]
        }
    }
    try: requests.post(url, json=payload, timeout=6)
    except: pass

async def update_http_status(text):
    await asyncio.to_thread(_sync_http_edit, text)

async def prog(c, t, app_instance, step_name):
    global last_time, start_time
    now = time.time()
    if start_time == 0:
        start_time = now
        last_time = now
        return

    if now - last_time > 8 or c == t:
        elapsed = now - start_time
        speed = c / elapsed if elapsed > 0 else 0
        speed_mb = (speed / 1024) / 1024
        percent = (c / t) * 100 if t > 0 else 0

        if step_name in ["hardsub_download", "compress_download"]:
            text = f"📥 <b>Downloading Video</b>\n<code>{get_download_bar(percent)}</code> [{percent:.1f}%]\n🚀 Speed: <b>{speed_mb:.2f} MB/s</b>\n📦 {c/1048576:.1f}MB / {t/1048576:.1f}MB"
        else:
            text = f"📤 <b>Sending Video</b>\n<code>{get_send_bar(percent)}</code> [{percent:.1f}%]\n🚀 Speed: <b>{speed_mb:.2f} MB/s</b>\n📦 {c/1048576:.1f}MB / {t/1048576:.1f}MB"

        asyncio.create_task(update_http_status(text))
        last_time = now

def convert_to_clean_ass(input_sub, output_ass):
    try:
        subs = pysubs2.load(input_sub)
        subs.styles["Default"] = pysubs2.SSAStyle(fontname="Arial", fontsize=24, primarycolor=pysubs2.Color(255, 255, 255), outlinecolor=pysubs2.Color(0, 0, 0), outline=2, shadow=1, marginl=20, marginr=20, marginv=15)
        for line in subs:
            line.style = "Default"
            line.text = re.sub(r'<[^>]+>', '', re.sub(r'\{[^}]+\}', '', line.text)).replace('\r', '').replace('\n', '\\N').strip()
        subs.save(output_ass)
    except Exception: pass

def is_ass_format(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            head = f.read(4000)
        return bool(re.search(r'\[Script Info\]|\[V4\+?\s*Styles\]|\[Events\]', head, re.IGNORECASE))
    except Exception: return False

def get_font_name(font_path):
    try:
        font = TTFont(font_path)
        for record in font['name'].names:
            if record.nameID == 4: return record.toUnicode()
    except: pass
    return "Arial"

def get_video_dimensions_and_duration(video_path):
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream=width,height:format=duration",
           "-of", "default=noprint_wrappers=1", video_path]
    width, height, duration = 1280, 720, 0.0
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        for line in res.stdout.strip().split("\n"):
            if "=" not in line: continue
            k, v = line.split("=", 1)
            if k == "width": width = int(v)
            elif k == "height": height = int(v)
            elif k == "duration": duration = float(v)
    except: pass
    return width, height, duration

async def download_tg_link(app_instance, link, output_path, step_name):
    if not link or link == "none": return None
    try:
        msg_id = int(link.split("/")[-1])
        msg = await app_instance.get_messages(CHAT_ID, msg_id)
        if msg and (msg.document or msg.video or msg.photo or msg.animation):
            reset_prog()
            downloaded = await asyncio.wait_for(
                app_instance.download_media(msg, file_name=output_path, progress=prog, progress_args=(app_instance, step_name)),
                timeout=2400
            )
            if downloaded and os.path.exists(downloaded) and os.path.getsize(downloaded) > 1024:
                return downloaded
    except Exception as e:
        print(f"Download Exception: {e}")
    return None

async def deliver_video_asset(app_instance, chat_id, target_user, file_path, caption, progress_callback):
    if not os.path.exists(file_path) or os.path.getsize(file_path) < 1000:
        raise Exception("Output video file is missing or invalid.")

    width, height, duration = get_video_dimensions_and_duration(file_path)

    thumb_path = "thumb.jpg"
    try: subprocess.run(["ffmpeg", "-y", "-i", file_path, "-ss", "00:00:01", "-vframes", "1", thumb_path], capture_output=True, timeout=15)
    except: pass
    if not os.path.exists(thumb_path): thumb_path = None

    reset_prog()

    try:
        pm_msg = await asyncio.wait_for(
            app_instance.send_video(chat_id=target_user, video=file_path, width=width, height=height, duration=int(duration), supports_streaming=True, caption=caption, thumb=thumb_path, progress=progress_callback, progress_args=(app_instance, "sending_video")),
            timeout=2400
        )
        if pm_msg and pm_msg.video:
            try: await app_instance.send_video(chat_id=DESK_CHANNEL_ID, video=pm_msg.video.file_id, caption=f"🎬 Logs: {caption}\nUser: `{target_user}`")
            except: pass
        return pm_msg
    except Exception:
        pm_msg = await asyncio.wait_for(
            app_instance.send_video(chat_id=chat_id, video=file_path, width=width, height=height, duration=int(duration), supports_streaming=True, caption=f"⚠️ <a href='tg://user?id={target_user}'>User</a>, Video Ready:\n\n{caption}", thumb=thumb_path, progress=progress_callback, progress_args=(app_instance, "sending_video"), parse_mode=ParseMode.HTML),
            timeout=2400
        )
        return pm_msg

async def main():
    global status_msg_id

    client_params = {
        "name": "worker_single_session",
        "api_id": API_ID,
        "api_hash": API_HASH,
        "workers": 16,
        "max_concurrent_transmissions": 10,
        "no_updates": True
    }
    if STRING_SESSION and STRING_SESSION.strip() != "":
        client_params["session_string"] = STRING_SESSION.strip()
    else:
        client_params["bot_token"] = BOT_TOKEN

    app = Client(**client_params)
    await app.start()

    try: await app.get_chat(CHAT_ID)
    except: pass

    if TRIGGER_MSG_ID and TRIGGER_MSG_ID != "none":
        try: await app.delete_messages(CHAT_ID, int(TRIGGER_MSG_ID))
        except: pass

    init_msg = await app.send_message(
        CHAT_ID,
        "⚙️ Initializing Cloud Processing Node...",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Cancel Task", callback_data="cancel_active_run")]])
    )
    status_msg_id = init_msg.id

    try:
        step_dl = "hardsub_download" if TASK_TYPE == "hardsub" else "compress_download"
        video_file = await download_tg_link(app, VIDEO_ID, "video.mkv", step_dl)

        if not video_file or not os.path.exists(video_file) or os.path.getsize(video_file) < 10000:
            raise Exception("Video download failed or file is 0 bytes.")

        vid_width, vid_height, duration = get_video_dimensions_and_duration(video_file)
        if duration <= 0: duration = 1.0

        base_name = "output"
        if RENAME and RENAME != "none":
            base_name = RENAME.rsplit('.', 1)[0]
        out_name = f"{base_name}.mp4"

        font_name = "Arial"
        if FONT_LINK and FONT_LINK != "none":
            r = requests.get(FONT_LINK, timeout=15)
            if r.status_code == 200:
                with open("fonts/custom_font.ttf", "wb") as f: f.write(r.content)
                font_name = get_font_name("fonts/custom_font.ttf")

        sub_file, has_watermark = None, False
        extracted_subs = []

        if TASK_TYPE == "hardsub":
            if SUB_ID and SUB_ID != "none":
                sub_file = await download_tg_link(app, SUB_ID, "sub_raw", "hardsub_download")
            if not sub_file or not os.path.exists(sub_file):
                raise Exception("Subtitle file not found or download failed.")

            if sub_file.lower().endswith('.ass') or is_ass_format(sub_file):
                try:
                    with open(sub_file, 'r', encoding='utf-8', errors='ignore') as f: ass_content = f.read()
                except Exception:
                    with open(sub_file, 'r', encoding='latin-1', errors='ignore') as f: ass_content = f.read()

                if any(word in ass_content.lower() for word in ["logo", "watermark", "cr", "credit"]):
                    has_watermark = True

                if FONT_LINK and FONT_LINK != "none":
                    lines = ass_content.splitlines()
                    new_lines = []
                    for line in lines:
                        if line.strip().startswith("Style:"):
                            parts = line.split(",", 2)
                            if len(parts) >= 3: line = f"{parts[0]},{font_name},{parts[2]}"
                        new_lines.append(line)
                    with open("ready_sub.ass", "w", encoding="utf-8") as f: f.write("\n".join(new_lines))
                else:
                    shutil.copy(sub_file, "ready_sub.ass")
            else:
                try: subs = pysubs2.load(sub_file, encoding="utf-8")
                except: subs = pysubs2.load(sub_file, encoding="latin-1")
                new_subs = pysubs2.SSAFile()
                new_subs.styles["Default"] = pysubs2.SSAStyle(fontname=font_name, fontsize=24, primarycolor=pysubs2.Color(255, 255, 255), outlinecolor=pysubs2.Color(0, 0, 0), outline=2, shadow=1, marginl=20, marginr=20, marginv=15)
                for line in subs:
                    clean_text = re.sub(r'<[^>]+>', '', re.sub(r'\{[^}]+\}', '', line.text)).replace('\r', '').replace('\n', '\\N').strip()
                    if clean_text: new_subs.append(pysubs2.SSAEvent(start=line.start, end=line.end, text=clean_text, style="Default"))
                new_subs.save("ready_sub.ass")

            # ---- Hardcoded ASI watermark, always injected for hardsub, duration-matched ----
            if not has_watermark:
                add_hardcoded_watermark("ready_sub.ass", duration)

        # ---------------- ENCODE STAGE ----------------
        process_title = "Compressing Video" if TASK_TYPE == "compress" else "Encoding Hardsub"

        # Resolution-aware rate control: bucket is chosen from the video's REAL output
        # height (after any requested resize), not blindly from the raw input string.
        # This fixes the "flat 1200k cap on a 1080p video" quality problem.
        reso_clean = str(RESOLUTION).replace("p", "").replace("P", "").strip() if RESOLUTION else ""
        if reso_clean and reso_clean.lower() != "none" and reso_clean.isdigit():
            effective_height = int(reso_clean)
        else:
            effective_height = vid_height

        if effective_height >= 1080:
            max_rate, buf_size = "2200k", "4400k"
        elif effective_height >= 720:
            max_rate, buf_size = "1600k", "3200k"
        elif effective_height >= 480:
            max_rate, buf_size = "1000k", "2000k"
        else:
            max_rate, buf_size = "700k", "1400k"

        if TASK_TYPE == "compress":
            await update_http_status("⚙️ <b>Extracting internal subtitles...</b>")
            cmd_probe = ["ffprobe", "-v", "error", "-select_streams", "s", "-show_entries", "stream=index,codec_name", "-of", "csv=p=0", video_file]
            res_probe = subprocess.run(cmd_probe, capture_output=True, text=True)
            if res_probe.stdout.strip():
                streams = res_probe.stdout.strip().split('\n')
                for i, st in enumerate(streams):
                    if not st: continue
                    parts = st.split(',')
                    s_idx = parts[0]
                    s_codec = parts[1].strip()
                    if s_codec in ['ass', 'ssa']:
                        ass_out = f"{base_name}_track_{i+1}.ass"
                        subprocess.run(["ffmpeg", "-y", "-i", video_file, "-map", f"0:{s_idx}", ass_out])
                        if os.path.exists(ass_out) and os.path.getsize(ass_out) > 0: extracted_subs.append(ass_out)
                    elif s_codec in ['subrip', 'srt', 'webvtt']:
                        temp_ext = ".srt" if s_codec == 'subrip' else ".vtt"
                        temp_sub = f"temp_{i+1}{temp_ext}"
                        subprocess.run(["ffmpeg", "-y", "-i", video_file, "-map", f"0:{s_idx}", temp_sub])
                        if os.path.exists(temp_sub) and os.path.getsize(temp_sub) > 0:
                            ass_out = f"{base_name}_track_{i+1}.ass"
                            convert_to_clean_ass(temp_sub, ass_out)
                            if os.path.exists(ass_out): extracted_subs.append(ass_out)

            if reso_clean and reso_clean.lower() != "none":
                scale_filter = f"scale=-2:min({reso_clean}\\,ih)"
            else:
                scale_filter = "scale='trunc(iw/2)*2:trunc(ih/2)*2'"

            await update_http_status(f"⚙️ <b>{process_title}</b>\n<code>{get_process_bar(0)}</code> [0.0%]")

            # CRF 26 (was 28) + resolution-aware maxrate: better quality per-bit at the
            # same "ultrafast" speed. Preset kept as ultrafast per requirement.
            cmd = [
                "ffmpeg", "-y", "-progress", "pipe:1", "-i", video_file, "-vf", scale_filter,
                "-map", "0:v", "-map", "0:a?",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
                "-maxrate", max_rate, "-bufsize", buf_size,
                "-pix_fmt", "yuv420p", "-threads", "0",
                "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", out_name
            ]
        else:
            vf_filter = "subtitles='ready_sub.ass':charenc=UTF-8"
            if FONT_LINK and FONT_LINK != "none": vf_filter += ":fontsdir=fonts"

            if reso_clean and reso_clean.lower() != "none":
                scale_stage = f"scale=-2:min({reso_clean}\\,ih)"
            else:
                scale_stage = "scale='trunc(iw/2)*2:trunc(ih/2)*2'"
            v_filter = f"{scale_stage},{vf_filter}"

            await update_http_status(f"⚙️ <b>{process_title}</b>\n<code>{get_process_bar(0)}</code> [0.0%]")

            # Watermark is now baked directly into ready_sub.ass (see add_hardcoded_watermark),
            # so there is no separate image-overlay input/filter step anymore -> faster encode.
            cmd = [
                "ffmpeg", "-y", "-progress", "pipe:1", "-i", video_file, "-vf", v_filter,
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
                "-maxrate", max_rate, "-bufsize", buf_size,
                "-pix_fmt", "yuv420p", "-threads", "0",
                "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", out_name
            ]

        process = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        last_edit = time.time()
        log_tail = []

        while True:
            line = await process.stdout.readline()
            if not line: break
            line_str = line.decode('utf-8', errors='ignore').strip()
            if line_str and "out_time_us=" not in line_str and "frame=" not in line_str:
                log_tail.append(line_str)
                if len(log_tail) > 15: log_tail.pop(0)
            if "out_time_us=" in line_str:
                now = time.time()
                if now - last_edit > 8:
                    try:
                        percent = min((int(line_str.split("=")[1]) / 1000000.0 / duration) * 100, 100.0)
                        asyncio.create_task(update_http_status(f"⚙️ <b>{process_title}</b>\n<code>{get_process_bar(percent)}</code> [{percent:.1f}%]"))
                    except: pass
                    last_edit = now

        await process.wait()
        if process.returncode != 0:
            raise Exception("FFmpeg processing failure:\n" + "\n".join(log_tail[-6:]))

        # ---------------- UPLOAD STAGE ----------------
        await update_http_status(f"📤 <b>Sending Video</b>\n<code>{get_send_bar(0)}</code> [0.0%]")

        await deliver_video_asset(app, CHAT_ID, USER_ID, out_name, f"✅ <b>Process Completed!</b>\n<code>{out_name}</code>", prog)

        if TASK_TYPE == "compress" and extracted_subs:
            for sub_f in extracted_subs:
                try: await app.send_document(chat_id=USER_ID, document=sub_f, caption="📄 Extracted Subtitles (.ass)")
                except:
                    try: await app.send_document(chat_id=CHAT_ID, document=sub_f, caption="📄 Extracted Subtitles (.ass)")
                    except: pass

        try: await app.delete_messages(CHAT_ID, status_msg_id)
        except: pass

    except Exception as e:
        try: _sync_http_edit(f"❌ <b>Execution Error:</b>\n<code>{html.escape(str(e))}</code>")
        except: pass
    finally:
        await app.stop()

if __name__ == "__main__":
    asyncio.run(main())
