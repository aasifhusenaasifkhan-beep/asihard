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
# SUBTITLE HELPERS
# Dialogue ALWAYS uses the bot's own style (subtitle file ka style / tags ignore),
# same look as the Colab bot: Arial Bold, white, black outline, bottom centre,
# and never more than 2 lines (long lines get a slightly smaller font instead of a 3rd line).
# =========================================================
PLAY_W, PLAY_H = 1920, 1080
DLG_FONT_SIZE = 90
DLG_OUTLINE = 4.5
DLG_SHADOW = 3.5
DLG_MARGIN_V = 70
DLG_MARGIN_LR = 120


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


def _find_measure_font(custom_path=None):
    """Font file used only to MEASURE text width (so we know when a line needs 2 lines / smaller size)."""
    if custom_path and os.path.exists(custom_path):
        return custom_path
    try:
        r = subprocess.run(["fc-match", "-f", "%{file}", "Arial:bold"], capture_output=True, text=True, timeout=10)
        p = r.stdout.strip()
        if p and os.path.exists(p):
            return p
    except Exception:
        pass
    for p in ("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
              "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"):
        if os.path.exists(p):
            return p
    return None


class TextMeter:
    def __init__(self, font_path):
        self.ok = False
        try:
            f = TTFont(font_path, fontNumber=0)
            self.cmap, self.hmtx = f.getBestCmap(), f["hmtx"]
            upem = f["head"].unitsPerEm
            os2, hh = f["OS/2"], f["hhea"]
            # ASS Fontsize = line cell height (win ascent + descent), so px per font unit = fs / cell
            self.cell = (os2.usWinAscent + os2.usWinDescent) or (hh.ascent - hh.descent) or upem
            self.missing = int(0.62 * upem)     # glyphs the font lacks (e.g. Devanagari) -> libass falls back
            self.ok = True
        except Exception:
            pass

    def width(self, text, fs):
        if not self.ok:
            return len(text) * 0.47 * fs
        total = 0
        for ch in text:
            g = self.cmap.get(ord(ch))
            total += self.hmtx[g][0] if g is not None else self.missing
        return total * fs / self.cell


def layout_dialogue(lines, meter):
    """lines -> ASS text with at most 2 lines. Keeps the author's own 1-2 line split when it fits,
    otherwise re-wraps into 2 balanced lines, and shrinks the font for that cue only if 2 lines are not enough."""
    lines = [l for l in lines if l.strip()]
    if not lines:
        return ""
    fs = DLG_FONT_SIZE
    limit = (PLAY_W - 2 * DLG_MARGIN_LR - 2 * DLG_OUTLINE) * 0.97

    if len(lines) <= 2 and all(meter.width(l, fs) <= limit for l in lines):
        return "\\N".join(lines)
    flat = " ".join(lines)
    if meter.width(flat, fs) <= limit:
        return flat

    words = flat.split(" ")
    best = None
    for i in range(1, len(words)):
        a, b = " ".join(words[:i]), " ".join(words[i:])
        m = max(meter.width(a, fs), meter.width(b, fs))
        if best is None or m < best[0]:
            best = (m, a + "\\N" + b)
    if best is None:                       # one single very long word
        best = (meter.width(flat, fs), flat)
    worst, text = best
    if worst <= limit:
        return text
    return "{\\fs%d}%s" % (max(30, int(fs * limit / worst)), text)


def _plain_lines(body):
    """Cue text -> list of plain lines (all tags / styling removed)."""
    body = re.sub(r"\{[^}]*\}", "", body)              # ass override tags
    body = re.sub(r"<\d{1,2}:\d{2}[^>]*>", "", body)   # vtt karaoke timestamps
    body = re.sub(r"</?[A-Za-z][^>]*>", "", body)      # <i> <b> <font ..> <c.x> <v ..>
    body = html.unescape(body).replace("\\N", "\n").replace("\\n", "\n").replace("\\h", " ")
    lines = [re.sub(r"\s+", " ", l).strip() for l in body.replace("\r", "").split("\n")]
    return [l for l in lines if l]


_TIME_RE = re.compile(
    r"(?:(\d+):)?(\d{1,2}):(\d{2})[,.](\d{1,3})\s*-->\s*(?:(\d+):)?(\d{1,2}):(\d{2})[,.](\d{1,3})")


def _to_ms(h, m, s, ms):
    return ((int(h or 0) * 60 + int(m)) * 60 + int(s)) * 1000 + int(ms.ljust(3, "0"))


def _srt_vtt_cues(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    cues = []
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
        plain = _plain_lines("\n".join(lines[ti + 1:]))
        if plain:
            cues.append((sec_to_ass_time(_to_ms(*g[0:4]) / 1000.0), sec_to_ass_time(_to_ms(*g[4:8]) / 1000.0), plain))
    return cues


def _ass_cues(text):
    """Only the dialogue text + timing is taken from an .ass; its styles / positions / effects are ignored.
    Vector drawings and watermark/logo/credit styled lines are dropped (they are not dialogue)."""
    cues, sec, fmt = [], None, None
    for raw in text.split("\n"):
        s = raw.strip()
        low = s.lower()
        if s.startswith("[") and s.endswith("]"):
            sec, fmt = low, None
        elif sec == "[events]":
            if low.startswith("format:"):
                fmt = [x.strip().lower() for x in s[7:].split(",")]
            elif low.startswith("dialogue:") and fmt:
                parts = s.split(":", 1)[1].lstrip().split(",", len(fmt) - 1)
                if len(parts) < len(fmt):
                    continue
                d = dict(zip(fmt, parts))
                txt = d.get("text", "")
                if re.search(r"(watermark|logo|credit)", d.get("style", ""), re.I):
                    continue
                if re.search(r"\{[^}]*\\p[1-9]", txt):
                    continue
                plain = _plain_lines(txt)
                if plain:
                    cues.append((d.get("start", "0:00:00.00").strip(), d.get("end", "0:00:00.00").strip(), plain))
    return cues


def build_dialogue_ass(cues, font_name, bold, meter):
    font_name = (font_name or "Arial").replace(",", " ")
    head = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {PLAY_W}\nPlayResY: {PLAY_H}\n"
        "WrapStyle: 0\nScaledBorderAndShadow: yes\nYCbCr Matrix: TV.601\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font_name},90,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,4.5,3.5,2,120,120,70,1\n"
        f"Style: Italic,{font_name},90,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,-1,-1,0,0,100,100,0,0,1,4.5,3.5,2,120,120,70,1\n"
        f"Style: Flashback,{font_name},90,&H00FFFFFF,&H000000FF,&H00505050,&H00505050,-1,0,0,0,100,100,0,0,1,4.5,3.5,2,120,120,70,1\n"
        f"Style: Signs,{font_name},70,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,3,0,8,10,10,20,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    events = []
    for start, end, lines in cues:
        t = layout_dialogue(lines, meter)
        if t:
            events.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{t}")
    if not events:
        raise Exception("Subtitle file me koi valid dialogue nahi mila.")
    return head + "\n".join(events) + "\n"


def prepare_subtitle(sub_file, font_name, custom_font, duration, out_path="ready_sub.ass", font_path=None):
    text = read_text_any(sub_file).replace("\r\n", "\n").replace("\r", "\n")
    if sub_file.lower().endswith((".ass", ".ssa")) or is_ass_text(text):
        cues = _ass_cues(text)
    else:
        cues = _srt_vtt_cues(text)
    meter = TextMeter(_find_measure_font(font_path if custom_font else None))
    ass = build_dialogue_ass(cues, font_name, bold=not custom_font, meter=meter)
    ass = inject_watermark(ass, duration)     # watermark code untouched
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(ass)
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
            prepare_subtitle(sub_file, font_name, custom_font, duration, font_path=font_path)
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
