import os, sys, time, asyncio, re, subprocess, requests, html, json
import pyrogram.utils
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
RENAME = os.getenv("RENAME")
FONT_LINK = os.getenv("FONT_LINK")
TRIGGER_MSG_ID = os.getenv("TRIGGER_MSG_ID")
RUN_ID = (os.getenv("GITHUB_RUN_ID") or "").strip()   # set automatically by GitHub Actions

_user_id_raw = os.getenv("USER_ID")
USER_ID = int(_user_id_raw) if _user_id_raw and _user_id_raw.strip().lower() != "none" else CHAT_ID

# Button data carries the run id, so "Cancel" stops ONLY this task (not everyone's).
CANCEL_DATA = f"cancel_{RUN_ID}_{USER_ID}" if RUN_ID else "cancel_active_run"

DESK_CHANNEL_ID = -1003700822969
TRANSFER_TIMEOUT = 2400

last_time = 0
start_time = 0
status_msg_id = None
os.makedirs("fonts", exist_ok=True)


def is_set(v):
    return bool(v) and str(v).strip().lower() not in ("none", "")


# =========================================================
# WATERMARK (hardcoded, injected as plain text into the .ass)
# =========================================================
WATERMARK_STYLE_NAME = "ASI_Watermark"


def watermark_text(k):
    """k = PlayResY / 1080, so the watermark keeps the same relative size on any script."""
    return (f"{{\\an9\\bord{8*k:.1f}\\blur{5*k:.1f}\\shad{3*k:.1f}}} "
            "{\\c&HFF00FF&}\U0001D670{\\c&HFFFFFF&}\U0001D682{\\c&H00A0FF&}\U0001D678\u2620")


def sec_to_ass_time(seconds):
    cs = int(round(max(0.0, float(seconds)) * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def has_watermark_style(text):
    return bool(re.search(r"^\s*Style:\s*[^,\n]*(watermark|logo|credit)", text, re.I | re.M))


def inject_watermark(text, duration):
    """Adds the ASI watermark straight into the ass text. Nothing else in the file
    is touched (styles, [Fonts], attachments, override tags all stay as they were)."""
    lines = text.split("\n")
    sec = None
    play_y = None
    style_fmt = ev_fmt = None
    last_style = last_event = ev_fmt_idx = None

    for i, raw in enumerate(lines):
        s = raw.strip()
        if s.startswith("[") and s.endswith("]"):
            sec = s.lower()
            continue
        low = s.lower()
        if sec == "[script info]":
            m = re.match(r"playresy:\s*(\d+)", low)
            if m:
                play_y = int(m.group(1))
        elif sec and "styles" in sec:
            if low.startswith("format:"):
                style_fmt = [x.strip() for x in s[7:].split(",")]
            elif low.startswith("style:"):
                last_style = i
        elif sec == "[events]":
            if low.startswith("format:"):
                ev_fmt = [x.strip() for x in s[7:].split(",")]
                ev_fmt_idx = i
            elif low.startswith(("dialogue:", "comment:")):
                last_event = i

    if style_fmt is None or last_style is None or ev_fmt is None:
        print("Watermark skipped: sub file has no usable Styles/Events section")
        return text

    k = (play_y or 288) / 1080.0   # libass assumes 288 when PlayResY is missing

    style_vals = {
        "name": WATERMARK_STYLE_NAME, "fontname": "Arial", "fontsize": f"{140*k:.1f}",
        "primarycolour": "&H00FFFFFF", "secondarycolour": "&H000000FF",
        "outlinecolour": "&H00000000", "backcolour": "&H00000000",
        "bold": "-1", "italic": "0", "underline": "0", "strikeout": "0",
        "scalex": "100", "scaley": "100", "spacing": "0", "angle": "0",
        "borderstyle": "1", "outline": f"{5*k:.1f}", "shadow": f"{2*k:.1f}",
        "alignment": "9", "marginl": f"{10*k:.0f}", "marginr": f"{40*k:.0f}",
        "marginv": f"{40*k:.0f}", "encoding": "1",
    }
    style_line = "Style: " + ",".join(style_vals.get(n.lower(), "0") for n in style_fmt)

    ev_vals = {
        "layer": "10", "marked": "Marked=0", "start": sec_to_ass_time(0),
        "end": sec_to_ass_time(duration), "style": WATERMARK_STYLE_NAME, "name": "",
        "marginl": "0", "marginr": "0", "marginv": "0", "effect": "",
        "text": watermark_text(k),
    }
    event_line = "Dialogue: " + ",".join(ev_vals.get(n.lower(), "0") for n in ev_fmt)

    ev_pos = last_event if last_event is not None else ev_fmt_idx
    # insert the later position first so the earlier index stays valid
    for pos, line in sorted([(ev_pos, event_line), (last_style, style_line)], reverse=True):
        lines.insert(pos + 1, line)
    return "\n".join(lines)


# =========================================================
# SUBTITLE HELPERS (file ka apna style bina chhede use hota hai)
# =========================================================
def read_text_any(path):
    raw = open(path, "rb").read()
    if raw[:3] == b"\xef\xbb\xbf":
        return raw[3:].decode("utf-8", "replace")
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", "replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", "replace")


def is_ass_text(text):
    return bool(re.search(r"\[Script Info\]|\[V4\+?\s*Styles\]|\[Events\]", text[:6000], re.I))


def apply_font_to_styles(text, font_name):
    """Only when a custom font is pinned: swap the Fontname column, keep every other style value."""
    out, sec, idx = [], None, None
    for line in text.split("\n"):
        s = line.strip()
        low = s.lower()
        if s.startswith("[") and s.endswith("]"):
            sec, idx = low, None
        elif sec and "styles" in sec:
            if low.startswith("format:"):
                fmt = [x.strip().lower() for x in s[7:].split(",")]
                idx = fmt.index("fontname") if "fontname" in fmt else None
            elif low.startswith("style:") and idx is not None:
                head, _, body = line.partition(":")
                parts = body.split(",")
                if len(parts) > idx:
                    parts[idx] = font_name
                    line = head + ":" + ",".join(parts)
        out.append(line)
    return "\n".join(out)


_COLOR_NAMES = {
    "white": "ffffff", "black": "000000", "red": "ff0000", "green": "008000", "lime": "00ff00",
    "blue": "0000ff", "yellow": "ffff00", "cyan": "00ffff", "aqua": "00ffff", "magenta": "ff00ff",
    "orange": "ffa500", "gray": "808080", "grey": "808080", "pink": "ffc0cb", "purple": "800080",
}


def _html_color_to_bgr(c):
    c = c.strip().strip("\"'").lower()
    c = _COLOR_NAMES.get(c, c)
    m = re.fullmatch(r"#?([0-9a-f]{6})", c)
    if not m:
        m3 = re.fullmatch(r"#([0-9a-f]{3})", c)
        if not m3:
            return None
        h = "".join(ch * 2 for ch in m3.group(1))
    else:
        h = m.group(1)
    return (h[4:6] + h[2:4] + h[0:2]).upper()


def _convert_cue_text(t):
    """SRT / VTT cue text -> ASS text, keeping italics / bold / underline / colour / font."""
    t = t.replace("\r", "")

    def font_open(m):
        attrs, out = m.group(1), ""
        cm = re.search(r"color\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", attrs, re.I)
        if cm:
            bgr = _html_color_to_bgr(cm.group(1))
            if bgr:
                out += f"\\c&H{bgr}&"
        fm = re.search(r"face\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", attrs, re.I)
        if fm:
            out += "\\fn" + fm.group(1).strip("\"'")
        return "{" + out + "}" if out else ""

    t = re.sub(r"<font\b([^>]*)>", font_open, t, flags=re.I)
    t = re.sub(r"</font\s*>", r"{\\c\\fn}", t, flags=re.I)
    for tag, code in (("i", "i"), ("b", "b"), ("u", "u"), ("s", "s")):
        t = re.sub(rf"<{tag}>", "{\\\\" + code + "1}", t, flags=re.I)
        t = re.sub(rf"</{tag}>", "{\\\\" + code + "0}", t, flags=re.I)
    t = re.sub(r"</?[A-Za-z][^>]*>", "", t)        # leftover html / vtt tags (<c.x>, <v Name>, <ruby> ...)
    t = re.sub(r"<\d{1,2}:\d{2}[^>]*>", "", t)     # vtt karaoke timestamps
    t = html.unescape(t)
    return "\\N".join(x.strip() for x in t.split("\n")).strip()


_TIME_RE = re.compile(
    r"(?:(\d+):)?(\d{1,2}):(\d{2})[,.](\d{1,3})\s*-->\s*(?:(\d+):)?(\d{1,2}):(\d{2})[,.](\d{1,3})")


def _to_ms(h, m, s, ms):
    return ((int(h or 0) * 60 + int(m)) * 60 + int(s)) * 1000 + int(ms.ljust(3, "0"))


def _ms_to_ass(ms):
    return sec_to_ass_time(ms / 1000.0)


def text_sub_to_ass(text, font_name):
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    events = []
    for block in re.split(r"\n\s*\n", text):
        lines = block.strip("\n").split("\n")
        if lines and lines[0].strip().upper().startswith(("NOTE", "STYLE", "REGION", "WEBVTT")) and \
                not any("-->" in l for l in lines):
            continue
        ti = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if ti is None:
            continue
        m = _TIME_RE.search(lines[ti])
        if not m:
            continue
        g = m.groups()
        start, end = _to_ms(*g[0:4]), _to_ms(*g[4:8])
        body = _convert_cue_text("\n".join(lines[ti + 1:]))
        if body:
            events.append((start, end, body))
    if not events:
        raise Exception("Subtitle file me koi valid line nahi mili.")

    # PlayRes 1920x1080; sizes below equal the old 24pt@288 look, so nothing changes visually.
    head = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\nWrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\nYCbCr Matrix: None\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font_name},90,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,"
        "7,4,2,75,75,56,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    body = "\n".join(f"Dialogue: 0,{_ms_to_ass(s)},{_ms_to_ass(e)},Default,,0,0,0,,{tx}" for s, e, tx in events)
    return head + body + "\n"


def prepare_subtitle(sub_file, font_name, custom_font, duration, out_path="ready_sub.ass"):
    text = read_text_any(sub_file).replace("\r\n", "\n").replace("\r", "\n")
    if sub_file.lower().endswith((".ass", ".ssa")) or is_ass_text(text):
        if custom_font:
            text = apply_font_to_styles(text, font_name)
    else:
        text = text_sub_to_ass(text, font_name)
    if not has_watermark_style(text):
        text = inject_watermark(text, duration)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(text)
    return out_path


def get_font_name(font_path):
    try:
        font = TTFont(font_path, fontNumber=0)
        for record in font["name"].names:
            if record.nameID == 4:
                return record.toUnicode()
    except Exception:
        pass
    return "Arial"


# =========================================================
# PROGRESS / STATUS
# =========================================================
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


def _sync_http_edit(text, cancel=True):
    if not status_msg_id:
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
    payload = {
        "chat_id": CHAT_ID,
        "message_id": status_msg_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": {"inline_keyboard": [[{"text": "🛑 Cancel Task", "callback_data": CANCEL_DATA}]] if cancel else []},
    }
    try:
        r = requests.post(url, json=payload, timeout=8)
        return r.ok or "not modified" in r.text
    except Exception:
        return False


async def update_http_status(text, cancel=True):
    await asyncio.to_thread(_sync_http_edit, text, cancel)


async def prog(c, t, app_instance, step_name):
    global last_time, start_time
    now = time.time()
    if start_time == 0:
        start_time = last_time = now
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

        print(f"[{step_name}] {percent:.1f}%  {speed_mb:.2f} MB/s", flush=True)
        asyncio.create_task(update_http_status(text))
        last_time = now


# =========================================================
# TELEGRAM DOWNLOAD / UPLOAD
# =========================================================
def get_video_dimensions_and_duration(video_path):
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream=width,height:format=duration",
           "-of", "default=noprint_wrappers=1", video_path]
    width, height, duration = 1280, 720, 0.0
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        for line in res.stdout.strip().split("\n"):
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            try:
                if k == "width": width = int(v)
                elif k == "height": height = int(v)
                elif k == "duration": duration = float(v)
            except ValueError:
                pass
    except Exception:
        pass
    return width, height, duration


async def download_tg_link(app_instance, link, output_path, step_name, min_size=1, show_progress=True):
    if not is_set(link):
        return None
    for attempt in (1, 2):
        try:
            msg_id = int(link.split("/")[-1])
            msg = await app_instance.get_messages(CHAT_ID, msg_id)
            if msg and (msg.document or msg.video or msg.photo or msg.animation):
                if show_progress:
                    reset_prog()
                kw = dict(progress=prog, progress_args=(app_instance, step_name)) if show_progress else {}
                downloaded = await asyncio.wait_for(
                    app_instance.download_media(msg, file_name=output_path, **kw), timeout=TRANSFER_TIMEOUT)
                if downloaded and os.path.exists(downloaded) and os.path.getsize(downloaded) >= min_size:
                    return downloaded
            else:
                print(f"Download: message {msg_id} has no media")
                return None
        except Exception as e:
            print(f"Download Exception (try {attempt}): {e}")
    return None


def make_thumb(file_path, duration):
    thumb = "thumb.jpg"
    try:
        if os.path.exists(thumb):
            os.remove(thumb)
        ts = "1" if duration > 2 else "0"
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", ts, "-i", file_path,
                        "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "6", thumb],
                       capture_output=True, timeout=30)
    except Exception:
        pass
    return thumb if os.path.exists(thumb) and os.path.getsize(thumb) > 0 else None


async def deliver_video_asset(app_instance, chat_id, target_user, file_path, caption, progress_callback):
    """Sends the result as a DOCUMENT (not media)."""
    if not os.path.exists(file_path) or os.path.getsize(file_path) < 1000:
        raise Exception("Output video file is missing or invalid.")

    _, _, duration = get_video_dimensions_and_duration(file_path)
    thumb_path = make_thumb(file_path, duration)
    file_name = os.path.basename(file_path)

    async def _send(dest, cap):
        reset_prog()
        return await asyncio.wait_for(
            app_instance.send_document(
                chat_id=dest, document=file_path, file_name=file_name, thumb=thumb_path,
                caption=cap, parse_mode=ParseMode.HTML,
                progress=progress_callback, progress_args=(app_instance, "sending_video")),
            timeout=TRANSFER_TIMEOUT)

    try:
        sent = await _send(target_user, caption)
    except Exception as e:
        print(f"PM delivery failed ({e}); sending in chat instead")
        sent = await _send(chat_id, f"⚠️ <a href='tg://user?id={target_user}'>User</a>, Video Ready:\n\n{caption}")

    media = getattr(sent, "document", None) or getattr(sent, "video", None)
    if media:
        try:
            await app_instance.send_document(
                chat_id=DESK_CHANNEL_ID, document=media.file_id,
                caption=f"🎬 Logs: {caption}\nUser: <code>{target_user}</code>", parse_mode=ParseMode.HTML)
        except Exception:
            pass
    return sent


# =========================================================
# FFMPEG
# =========================================================
TEXT_SUB_CODECS = {"ass", "ssa", "subrip", "srt", "webvtt", "mov_text", "text"}


async def extract_embedded_subs(video_file, base_name):
    """One ffmpeg pass for ALL text subtitle tracks (old code re-read the whole file per track).
    Runs in the background while the main encode is going on."""
    try:
        res = await asyncio.to_thread(
            subprocess.run,
            ["ffprobe", "-v", "error", "-select_streams", "s", "-show_entries",
             "stream=index,codec_name", "-of", "json", video_file],
            capture_output=True, text=True, timeout=60)
        streams = json.loads(res.stdout or "{}").get("streams", [])
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", video_file]
        outs = []
        for st in streams:
            if st.get("codec_name") in TEXT_SUB_CODECS:
                out = f"{base_name}_track_{len(outs) + 1}.ass"
                cmd += ["-map", f"0:{st['index']}", "-c:s", "ass", out]
                outs.append(out)
        if not outs:
            return []
        p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL,
                                                 stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(p.wait(), timeout=900)
        return [o for o in outs if os.path.exists(o) and os.path.getsize(o) > 0]
    except Exception as e:
        print(f"Subtitle extraction failed: {e}")
        return []


def pick_rate(effective_height):
    if effective_height >= 1080: return "2200k", "4400k"
    if effective_height >= 720: return "1600k", "3200k"
    if effective_height >= 480: return "1000k", "2000k"
    return "700k", "1400k"


def build_ffmpeg_cmd(video_file, vf, out_name, max_rate, buf_size):
    return [
        "ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "error", "-progress", "pipe:1",
        "-i", video_file, "-vf", vf,
        "-map", "0:v:0", "-map", "0:a?", "-sn", "-dn",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
        "-maxrate", max_rate, "-bufsize", buf_size,
        "-pix_fmt", "yuv420p", "-threads", "0",
        # keyframe every 2s (IDR) -> seeking anywhere in the player starts instantly
        "-force_key_frames", "expr:gte(t,n_forced*2)", "-forced-idr", "1",
        "-c:a", "aac", "-b:a", "96k", "-ac", "2",
        "-max_muxing_queue_size", "1024",
        "-movflags", "+faststart", out_name,
    ]


async def run_ffmpeg(cmd, duration, title):
    process = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    last_edit = time.time()
    log_tail = []
    while True:
        line = await process.stdout.readline()
        if not line:
            break
        line_str = line.decode("utf-8", errors="ignore").strip()
        if not line_str:
            continue
        if "out_time_us=" in line_str:
            now = time.time()
            if now - last_edit > 8:
                try:
                    percent = min((int(line_str.split("=")[1]) / 1000000.0 / duration) * 100, 100.0)
                    print(f"[encode] {percent:.1f}%", flush=True)
                    asyncio.create_task(update_http_status(
                        f"⚙️ <b>{title}</b>\n<code>{get_process_bar(percent)}</code> [{percent:.1f}%]"))
                except Exception:
                    pass
                last_edit = now
        elif "=" not in line_str or line_str.startswith(("Error", "[")):
            log_tail.append(line_str)
            if len(log_tail) > 15:
                log_tail.pop(0)
    await process.wait()
    if process.returncode != 0:
        raise Exception("FFmpeg processing failure:\n" + "\n".join(log_tail[-6:]))


# =========================================================
# MAIN
# =========================================================
async def main():
    global status_msg_id

    client_params = {
        "name": "worker_single_session",
        "api_id": API_ID,
        "api_hash": API_HASH,
        "workers": 16,
        "max_concurrent_transmissions": 10,
        "no_updates": True,
    }
    if STRING_SESSION and STRING_SESSION.strip() != "":
        client_params["session_string"] = STRING_SESSION.strip()
    else:
        client_params["bot_token"] = BOT_TOKEN

    app = Client(**client_params)
    await app.start()

    try: await app.get_chat(CHAT_ID)
    except Exception: pass

    # Re-use the bot's "Task Dispatched..." message as the status message (saves a delete + a send).
    if is_set(TRIGGER_MSG_ID):
        try:
            status_msg_id = int(TRIGGER_MSG_ID)
            if not await asyncio.to_thread(_sync_http_edit, "⚙️ Initializing Cloud Processing Node..."):
                status_msg_id = None
        except Exception:
            status_msg_id = None
    if status_msg_id is None:
        init_msg = await app.send_message(
            CHAT_ID, "⚙️ Initializing Cloud Processing Node...",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Cancel Task", callback_data=CANCEL_DATA)]]))
        status_msg_id = init_msg.id

    try:
        is_hardsub = TASK_TYPE == "hardsub"
        step_dl = "hardsub_download" if is_hardsub else "compress_download"

        # ---- downloads: video starts right away, subtitle + font (tiny) fetched alongside ----
        video_task = asyncio.create_task(download_tg_link(app, VIDEO_ID, "video.mkv", step_dl, min_size=10000))
        sub_file = font_path = None
        try:
            if is_hardsub:
                sub_file = await download_tg_link(app, SUB_ID, "sub_raw", "sub", show_progress=False)
                if not sub_file:
                    raise Exception("Subtitle file not found or download failed.")
            font_path = await download_tg_link(app, FONT_LINK, "fonts/", "font", show_progress=False)
        except Exception:
            video_task.cancel()
            raise
        video_file = await video_task
        if not video_file:
            raise Exception("Video download failed or file is 0 bytes.")

        vid_width, vid_height, duration = get_video_dimensions_and_duration(video_file)
        if duration <= 0:
            duration = 1.0

        base_name = "output"
        if is_set(RENAME):
            base_name = RENAME.rsplit(".", 1)[0] if "." in RENAME else RENAME
        base_name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", base_name).strip()[:120] or "output"
        out_name = f"{base_name}.mp4"

        custom_font = bool(font_path) and font_path.lower().endswith((".ttf", ".otf", ".ttc"))
        font_name = get_font_name(font_path) if custom_font else "Arial"

        # ---- resolution / bitrate bucket ----
        reso_clean = str(RESOLUTION or "").replace("p", "").replace("P", "").strip()
        has_reso = reso_clean.isdigit()
        effective_height = int(reso_clean) if has_reso else vid_height
        max_rate, buf_size = pick_rate(effective_height)
        # (-2 keeps width even; min(...) never upscales; trunc keeps height even)
        scale_stage = f"scale=-2:'min({reso_clean},trunc(ih/2)*2)'" if has_reso else "scale=trunc(iw/2)*2:trunc(ih/2)*2"

        extract_task = None
        if is_hardsub:
            prepare_subtitle(sub_file, font_name, custom_font, duration)
            vf = f"{scale_stage},subtitles='ready_sub.ass':charenc=UTF-8"
            if custom_font:
                vf += ":fontsdir=fonts"
            title = "Encoding Hardsub"
        else:
            vf = scale_stage
            title = "Compressing Video"
            extract_task = asyncio.create_task(extract_embedded_subs(video_file, base_name))

        await update_http_status(f"⚙️ <b>{title}</b>\n<code>{get_process_bar(0)}</code> [0.0%]")
        await run_ffmpeg(build_ffmpeg_cmd(video_file, vf, out_name, max_rate, buf_size), duration, title)

        # ---- upload (as document) ----
        await update_http_status(f"📤 <b>Sending Video</b>\n<code>{get_send_bar(0)}</code> [0.0%]")
        await deliver_video_asset(app, CHAT_ID, USER_ID, out_name,
                                  f"✅ <b>Process Completed!</b>\n<code>{html.escape(out_name)}</code>", prog)

        if extract_task:
            for sub_f in await extract_task:
                try:
                    await app.send_document(chat_id=USER_ID, document=sub_f, caption="📄 Extracted Subtitles (.ass)")
                except Exception:
                    try: await app.send_document(chat_id=CHAT_ID, document=sub_f, caption="📄 Extracted Subtitles (.ass)")
                    except Exception: pass

        try: await app.delete_messages(CHAT_ID, status_msg_id)
        except Exception: pass

    except Exception as e:
        print(f"ERROR: {e}", flush=True)
        try: _sync_http_edit(f"❌ <b>Execution Error:</b>\n<code>{html.escape(str(e))[:3000]}</code>", cancel=False)
        except Exception: pass
        sys.exit(1)   # marks the Actions run as failed so it is visible in the Actions tab
    finally:
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
