import os, time, re, uuid, asyncio, math, logging
from config import Config
from urllib.parse import urlparse
from helper_func.settings_manager import SettingsManager
from pyrogram.enums import ParseMode

logger = logging.getLogger("mux.ffmpeg")
# Track running jobs so /cancel can kill ffmpeg
running_jobs: dict[str, dict] = {}

# Parse both classic ffmpeg stats AND -progress key/value output
progress_pattern = re.compile(
    r'(frame|fps|size|time|bitrate|speed|total_size|out_time_ms|progress)\s*=\s*(\S+)'
)

def _humanbytes(n: int) -> str:
    if not n:
        return "0 B"
    units = ["B","KB","MB","GB","TB","PB"]
    i = int(math.floor(math.log(n, 1024))) if n > 0 else 0
    p = math.pow(1024, i)
    s = round(n / p, 2)
    return f"{s} {units[i]}"

def _humanrate(bps: float) -> str:
    # bytes/sec -> "2.10 MB/s"
    if bps <= 0:
        return "N/A"
    units = ["B/s","KB/s","MB/s","GB/s","TB/s"]
    i = int(math.floor(math.log(bps, 1024))) if bps > 0 else 0
    p = math.pow(1024, i)
    s = round(bps / p, 2)
    return f"{s} {units[i]}"

def _fmt_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h: return f"{h}h {m}m {s}s"
    if m: return f"{m}m {s}s"
    return f"{s}s"

def _fmt_hhmmss(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"

def parse_progress(line: str):
    items = {k: v for k, v in progress_pattern.findall(line)}
    return items or None

async def readlines(stream):
    """Yield complete lines from an asyncio stream (handles CR/LF splits)."""
    pattern = re.compile(br'[\r\n]+')
    data = bytearray()
    while not stream.at_eof():
        parts = pattern.split(data)
        data[:] = parts.pop(-1)
        for line in parts:
            yield line
        data.extend(await stream.read(1024))

async def _probe_duration(vid_path: str) -> float:
    """Return total duration (seconds) using ffprobe. 0.0 if unknown."""
    proc = await asyncio.create_subprocess_exec(
        'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1', '-i', vid_path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, _ = await proc.communicate()
    try:
        return float(out.decode().strip())
    except Exception:
        return 0.0

async def read_stderr(start: float, msg, proc, job_id: str, total_dur: float, input_size: int):
    """
    Tail ffmpeg stderr and render a rich progress card (Size / Speed / Elapsed / ETA / %)
    with the Job ID visible.
    """
    last_edit = 0.0
    curr_time = 0.0   # seconds processed
    curr_size = 0     # bytes written (from total_size)
    speed_x   = 0.0

    os.makedirs("logs", exist_ok=True)
    ff_log_path = os.path.join("logs", f"ffmpeg_{job_id}.log")
    ff_log = open(ff_log_path, "a", encoding="utf-8", errors="ignore")
    _wrote = 0
    
    async for raw in readlines(proc.stderr):
        line = raw.decode(errors='ignore')
        try:
            ff_log.write(line)
            _wrote += 1
            if _wrote % 50 == 0:
                ff_log.flush()
        except:
            pass
        prog = parse_progress(line)
        if not prog:
            continue

        # Pull fields
        if 'out_time_ms' in prog:
            try:
                curr_time = int(prog['out_time_ms']) / 1_000_000.0
            except Exception:
                pass
        elif 'time' in prog:
            # fallback: 00:00:12.34 -> seconds
            t = prog['time']
            try:
                h, m, s = t.split(':')
                curr_time = int(h) * 3600 + int(m) * 60 + float(s)
            except Exception:
                pass

        if 'total_size' in prog:
            try:
                curr_size = int(prog['total_size'])
            except Exception:
                pass
        elif 'size' in prog and prog['size'].endswith('kB'):
            try:
                kb = float(prog['size'].replace('kB',''))
                curr_size = int(kb * 1024)
            except Exception:
                pass

        if 'speed' in prog and prog['speed'] not in ('N/A', '0x'):
            # '1.23x'
            try:
                speed_x = float(prog['speed'].rstrip('x'))
            except Exception:
                speed_x = 0.0

        # Throttle UI updates (~once every 2s)
        now = time.time()
        if now - last_edit < 5:
            continue
        last_edit = now

        # Percent and ETA
        pct = 0.0
        eta_sec = 0
        if total_dur > 0:
            pct = min(100.0, (curr_time / total_dur) * 100.0)
            if speed_x > 0:
                eta_sec = max(0, int((total_dur - curr_time) / speed_x))
            elif curr_time > 0:
                # fallback ETA from avg processing rate
                speed_factor = curr_time / (now - start)  # (sec encoded) per wall sec
                if speed_factor > 0:
                    eta_sec = max(0, int((total_dur - curr_time) / speed_factor))

        elapsed = now - start
        avg_bps = curr_size / elapsed if elapsed > 0 else 0.0

        card = (
            f"📽️ <b>Encoding</b> [<code>{job_id}</code>]\n\n"
            f"📊 <b>Size:</b> {_humanbytes(curr_size)}\n"
            f"⏱️ <b>Time:</b> {_fmt_hhmmss(curr_time)}\n"
            f"⚡ <b>Speed:</b> {f'{speed_x:.2f}x' if speed_x else 'N/A'}\n"
            f"📈 <b>Progress:</b> {pct:.1f}%\n"
            f"⏳ <b>ETA:</b> {_fmt_time(eta_sec)}\n"
        )
        try:
            await msg.edit(card, parse_mode=ParseMode.HTML)
        except:
            pass

    try:
        ff_log.flush()
        ff_log.close()
    except:
        pass
# ============ SOFT-MUX ============

async def softmux_vid(vid_filename: str, sub_filename: str, msg, job_id: str):
    start    = time.time()
    vid_path = os.path.join(Config.DOWNLOAD_DIR, vid_filename)
    sub_path = os.path.join(Config.DOWNLOAD_DIR, sub_filename)
    base     = os.path.splitext(vid_filename)[0]
    output   = f"{base}_soft.mkv"
    out_path = os.path.join(Config.DOWNLOAD_DIR, output)
    sub_ext  = os.path.splitext(sub_filename)[1].lstrip('.')

    total_dur  = await _probe_duration(vid_path)
    input_size = os.path.getsize(vid_path) if os.path.exists(vid_path) else 0

    proc = await asyncio.create_subprocess_exec(
        'ffmpeg', '-hide_banner',
        '-progress', 'pipe:2', '-nostats',
        '-i', vid_path, '-i', sub_path,
        '-map', '1:0', '-map', '0',
        '-disposition:s:0', 'default',
        '-c:v', 'copy', '-c:a', 'copy',
        f'-c:s', sub_ext,
        '-y', out_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    reader = asyncio.create_task(read_stderr(start, msg, proc, job_id, total_dur, input_size))
    waiter = asyncio.create_task(proc.wait())
    running_jobs[job_id] = {'proc': proc, 'tasks': [reader, waiter]}

    await msg.edit(
        f"🔄 Soft-Mux job started: <code>{job_id}</code>\n"
        f"Send <code>/cancel {job_id}</code> to abort",
        parse_mode=ParseMode.HTML
    )

    await asyncio.wait([reader, waiter])
    running_jobs.pop(job_id, None)

    if proc.returncode == 0:
        await msg.edit(
            f"✅ Soft-Mux `<code>{job_id}</code>` completed in {round(time.time()-start)}s",
            parse_mode=ParseMode.HTML
        )
        await asyncio.sleep(2)
        return output
    else:
        err = await proc.stderr.read()

        try:
            os.makedirs("logs", exist_ok=True)
            with open(os.path.join("logs", f"ffmpeg_{job_id}.log"), "a", encoding="utf-8", errors="ignore") as _f:
                _f.write("\n\n=== FINAL STDERR ===\n")
                _f.write(err.decode(errors='ignore'))
        except:
            pass
        logger.error("Soft-mux failed for job %s — tail: %s", job_id, err.decode(errors="ignore")[-1200:])
        
        await msg.edit(
            "❌ Error during soft-mux!\n\n"
            f"<pre>{err.decode(errors='ignore')}</pre>",
            parse_mode=ParseMode.HTML
        )
        return False


# ============ HARD-MUX ============

async def hardmux_vid(vid_filename: str, sub_filename: str, msg, job_id: str):

    start    = time.time()
    cfg      = SettingsManager.get(msg.chat.id)

    res    = cfg.get('resolution','1920:1080')
    fps    = cfg.get('fps','original')
    codec  = cfg.get('codec','libx264')
    crf    = cfg.get('crf','25')
    preset = cfg.get('preset','faster')

    vid_path = os.path.join(Config.DOWNLOAD_DIR, vid_filename)
    sub_path = os.path.join(Config.DOWNLOAD_DIR, sub_filename)

    total_dur  = await _probe_duration(vid_path)
    input_size = os.path.getsize(vid_path) if os.path.exists(vid_path) else 0

    vf = [f"subtitles={sub_path}:fontsdir={Config.FONTS_DIR}"]
    if res != 'original':
        vf.append(f"scale={res}")
    if fps != 'original':
        vf.append(f"fps={fps}")
    vf_arg = ",".join(vf)

    base     = os.path.splitext(vid_filename)[0]
    output   = f"{base}_hard.mp4"
    out_path = os.path.join(Config.DOWNLOAD_DIR, output)

    proc = await asyncio.create_subprocess_exec(
        'ffmpeg','-hide_banner',
        '-progress', 'pipe:2', '-nostats',
        '-i', vid_path,
        '-vf', vf_arg,
        '-c:v', codec, '-preset', preset, '-crf', crf,
        '-map','0:v:0','-map','0:a:0?',
        '-c:a','copy',
        '-y', out_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    reader = asyncio.create_task(read_stderr(start, msg, proc, job_id, total_dur, input_size))
    waiter = asyncio.create_task(proc.wait())
    running_jobs[job_id] = {'proc': proc, 'tasks': [reader, waiter]}

    await msg.edit(
        f"🔄 Hard-Mux job started: <code>{job_id}</code>\n"
        f"Send <code>/cancel {job_id}</code> to abort",
        parse_mode=ParseMode.HTML
    )

    await asyncio.wait([reader, waiter])
    running_jobs.pop(job_id, None)

    if proc.returncode == 0:
        await msg.edit(
            f"✅ Hard-Mux `<code>{job_id}</code>` completed in {round(time.time()-start)}s",
            parse_mode=ParseMode.HTML
        )
        await asyncio.sleep(2)
        return output
    else:
        err = await proc.stderr.read()

        try:
            os.makedirs("logs", exist_ok=True)
            with open(os.path.join("logs", f"ffmpeg_{job_id}.log"), "a", encoding="utf-8", errors="ignore") as _f:
                _f.write("\n\n=== FINAL STDERR ===\n")
                _f.write(err.decode(errors='ignore'))
        except:
            pass
        logger.error("Hard-mux failed for job %s — tail: %s", job_id, err.decode(errors="ignore")[-1200:])
        
        await msg.edit(
            "❌ Error during hard-mux!\n\n"
            f"<pre>{err.decode(errors='ignore')}</pre>",
            parse_mode=ParseMode.HTML
        )
        return False


# ============ NO-SUB (encode only) ============

async def nosub_encode(vid_filename: str, msg, job_id: str):
    start    = time.time()
    cfg      = SettingsManager.get(msg.chat.id)

    res    = cfg.get('resolution','1920:1080')
    fps    = cfg.get('fps','original')
    codec  = cfg.get('codec','libx264')
    crf    = cfg.get('crf','25')
    preset = cfg.get('preset','faster')

    is_url  = vid_filename.startswith(("http://","https://"))
    vid_path = vid_filename if is_url else os.path.join(Config.DOWNLOAD_DIR, vid_filename)
    total_dur  = await _probe_duration(vid_path)
    input_size = os.path.getsize(vid_path) if os.path.exists(vid_path) else 0

    vf = []
    if res != 'original':
        vf.append(f"scale={res}")
    if fps != 'original':
        vf.append(f"fps={fps}")
    vf_args = ['-vf', ",".join(vf)] if vf else []

    base     = os.path.splitext(vid_filename)[0]
    output   = f"{base}_enc.mp4"
    out_path = os.path.join(Config.DOWNLOAD_DIR, output)

    # Build ffmpeg args so we can inject headers for Dailymotion HLS
    args = ['ffmpeg', '-hide_banner', '-progress', 'pipe:2', '-nostats']
    
    # If input is a URL and host is Dailymotion/CDN, add headers
    is_url = vid_path.startswith(("http://", "https://"))
    host = urlparse(vid_path).hostname or ""
    if is_url and ("dmcdn.net" in host or "dailymotion.com" in host):
        # Add UA + Referer; add Cookie here if your log shows 403 requiring it
        args += ['-headers', 'User-Agent: Mozilla/5.0\r\nReferer: https://www.dailymotion.com\r\n']
    
    # Rest of your options (video filters, codec, mapping)
    args += [
        '-i', vid_path, *vf_args,
        '-c:v', codec, '-preset', preset, '-crf', crf,
        '-map', '0:v:0', '-map', '0:a:0?', '-c:a', 'copy',
        '-y', out_path
    ]
    
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    reader = asyncio.create_task(read_stderr(start, msg, proc, job_id, total_dur, input_size))
    waiter = asyncio.create_task(proc.wait())
    running_jobs[job_id] = {'proc': proc, 'tasks': [reader, waiter]}

    await msg.edit(
        f"🔄 Encode (no-sub) job started: <code>{job_id}</code>\n"
        f"Send <code>/cancel {job_id}</code> to abort",
        parse_mode=ParseMode.HTML
    )

    await asyncio.wait([reader, waiter])
    running_jobs.pop(job_id, None)

    if proc.returncode == 0:
        await msg.edit(
            f"✅ Encode `<code>{job_id}</code>` completed in {round(time.time()-start)}s",
            parse_mode=ParseMode.HTML
        )
        await asyncio.sleep(2)
        return output
    else:
        err = await proc.stderr.read()

        try:
            os.makedirs("logs", exist_ok=True)
            with open(os.path.join("logs", f"ffmpeg_{job_id}.log"), "a", encoding="utf-8", errors="ignore") as _f:
                _f.write("\n\n=== FINAL STDERR ===\n")
                _f.write(err.decode(errors='ignore'))
        except:
            pass
        logger.error("No-sub encode failed for job %s — tail: %s", job_id, err.decode(errors="ignore")[-1200:])
        
        await msg.edit(
            "❌ Error during encode!\n\n"
            f"<pre>{err.decode(errors='ignore')}</pre>",
            parse_mode=ParseMode.HTML
        )
        return False
