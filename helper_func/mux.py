import os, time, re, uuid, asyncio, math, logging, shlex, json, sys
from config import Config
from urllib.parse import urlparse
from helper_func.settings_manager import SettingsManager
from pyrogram.enums import ParseMode

logger = logging.getLogger("mux.ffmpeg")

# Get the directory of the current python executable (e.g., /home/giveaway/venv/bin)
VENV_BIN_DIR = os.path.dirname(sys.executable)
# Define the full path to the yt-dlp executable
YT_DLP_PATH = os.path.join(VENV_BIN_DIR, 'yt-dlp')
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
    """Return total duration (seconds) using ffprobe (for files) or yt-dlp (for URLs). 0.0 if unknown."""
    
    is_url = vid_path.startswith(("http://", "https://"))
    
    if not is_url:
        # --- Original ffprobe logic for local files ---
        proc = await asyncio.create_subprocess_exec(
            'ffprobe',
            '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'format=duration',
            '-of', 'default=nw=1:nk=1',
            vid_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        out, _ = await proc.communicate()
        try:
            return float(out.decode().strip())
        except Exception:
            logger.warning("ffprobe failed to get duration for local file: %s", vid_path)
            return 0.0
    
    else:
        # --- New yt-dlp logic for URLs ---
        logger.info("Probing duration for URL with yt-dlp: %s", vid_path)
        
        host = urlparse(vid_path).hostname or ""
        referer_url = f"https://{host}/" # Guess root domain as referer

        yt_dlp_cmd_parts = [
            YT_DLP_PATH,
            '--dump-json',
            '--no-warnings',
            '--user-agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        ]

        if "dmcdn.net" in host or "dailymotion.com" in host:
            logger.info("Applying Dailymotion-specific referer")
            referer_url = "https://www.dailymotion.com"
        elif "topchineseanime.store" in host:
            logger.info("Applying TopChineseAnime referer and cookies")
            referer_url = "https://topchineseanime.xyz/"
            yt_dlp_cmd_parts += ['--cookies-from-domain', 'topchineseanime.xyz'] # <--- ADDED
        
        yt_dlp_cmd_parts += ['--referer', referer_url]
        yt_dlp_cmd_parts.append(vid_path)
        
        proc = await asyncio.create_subprocess_exec(
            *yt_dlp_cmd_parts,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        out, err = await proc.communicate()
        
        if proc.returncode != 0:
            logger.error("yt-dlp probe failed: %s", err.decode(errors='ignore'))
            return 0.0
            
        try:
            # yt-dlp might output multiple JSON objects (e.g. for playlists)
            # We'll take the first valid one.
            for line in out.decode(errors='ignore').splitlines():
                if line.strip().startswith("{"):
                    data = json.loads(line)
                    duration = data.get('duration')
                    if duration:
                        logger.info("Found duration via yt-dlp: %s seconds", duration)
                        return float(duration)
            
            logger.warning("yt-dlp probe output did not contain duration.")
            return 0.0
        except Exception as e:
            logger.error("Failed to parse yt-dlp JSON: %s", e, exc_info=True)
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
    
    # This list will capture all output
    captured_lines: list[str] = []

    os.makedirs("logs", exist_ok=True)
    ff_log_path = os.path.join("logs", f"ffmpeg_{job_id}.log")
    ff_log = open(ff_log_path, "a", encoding="utf-8", errors="ignore")
    _wrote = 0
    
    async for raw in readlines(proc.stderr):
        line = raw.decode(errors='ignore')
        
        # Add every line to our capture list
        captured_lines.append(line)
        
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

    # Return the captured output
    return captured_lines
# ============ SOFT-MUX ============

async def softmux_vid(vid_filename: str, sub_filename: str, msg, job_id: str):
    start    = time.time()
    
    is_url  = vid_filename.startswith(("http://","https://"))
    vid_path = vid_filename if is_url else os.path.join(Config.DOWNLOAD_DIR, vid_filename)
    sub_path = os.path.join(Config.DOWNLOAD_DIR, sub_filename)
    
    base     = os.path.splitext(vid_filename)[0]
    output   = f"{base}_soft.mkv"
    out_path = os.path.join(Config.DOWNLOAD_DIR, output)
    sub_ext  = os.path.splitext(sub_filename)[1].lstrip('.')

    temp_vid_to_delete = None

    if is_url:
        # --- NEW: Download the video file first ---
        await msg.edit(
            f"Downloading video for soft-mux (<code>{job_id}</code>)…\n"
            "This is required for -c copy.",
            parse_mode=ParseMode.HTML
        )
        
        base = uuid.uuid4().hex[:8]
        temp_vid_file = f"{base}_temp_video.mp4"
        temp_vid_path = os.path.join(Config.DOWNLOAD_DIR, temp_vid_file)
        temp_vid_to_delete = temp_vid_file # Mark for deletion
        
        host = urlparse(vid_path).hostname or ""
        referer_url = f"https://{host}/" # Guess root domain as referer

        yt_dlp_cmd_parts = [
            YT_DLP_PATH,
            '--user-agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        ]

        if "dmcdn.net" in host or "dailymotion.com" in host:
            logger.info("Applying Dailymotion-specific referer")
            referer_url = "https://www.dailymotion.com"
        elif "topchineseanime.store" in host:
            logger.info("Applying TopChineseAnime referer and cookies")
            referer_url = "https://topchineseanime.xyz/"
            yt_dlp_cmd_parts += ['--cookies-from-domain', 'topchineseanime.xyz'] # <--- ADDED
        
        yt_dlp_cmd_parts += ['--referer', referer_url]
        # Add output format and URL
        yt_dlp_cmd_parts += ['-o', temp_vid_path, vid_path]
        
        proc = await asyncio.create_subprocess_exec(
            *yt_dlp_cmd_parts,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        # We can't use read_stderr here as it's not ffmpeg progress
        # So we just wait for the download to finish
        stdout, stderr = await proc.communicate()
        
        if proc.returncode != 0:
            logger.error("yt-dlp download for softmux failed: %s", stderr.decode(errors='ignore'))
            await msg.edit(
                f"❌ yt-dlp download failed for job <code>{job_id}</code>:\n"
                f"<pre>{stderr.decode(errors='ignore')[-1000:]}</pre>",
                parse_mode=ParseMode.HTML
            )
            return False
        
        # Success! Now set vid_path to our new local file
        vid_path = temp_vid_path
        logger.info("Download complete. Proceeding to soft-mux.")


    # --- Original ffmpeg -c copy logic (runs on local file) ---
    
    total_dur  = await _probe_duration(vid_path) # Probe the local file
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
    
    full_stderr_lines = reader.result() or []
    running_jobs.pop(job_id, None)

    # Delete the temporary downloaded video if one exists
    if temp_vid_to_delete:
        try:
            os.remove(os.path.join(Config.DOWNLOAD_DIR, temp_vid_to_delete))
        except Exception as e:
            logger.warning("Could not delete temp softmux file: %s", e)

    if proc.returncode == 0:
        await msg.edit(
            f"✅ Soft-Mux `<code>{job_id}</code>` completed in {round(time.time()-start)}s",
            parse_mode=ParseMode.HTML
        )
        await asyncio.sleep(2)
        return output
    else:
        full_stderr_text = "".join(full_stderr_lines)

        try:
            os.makedirs("logs", exist_ok=True)
            with open(os.path.join("logs", f"ffmpeg_{job_id}.log"), "a", encoding="utf-8", errors="ignore") as _f:
                _f.write(f"\n\n=== PROCESS EXITED WITH CODE {proc.returncode} ===")
        except:
            pass
        
        logger.error("Soft-mux failed for job %s — tail: %s", job_id, full_stderr_text[-1500:])
        err_preview = full_stderr_text[-1000:]
        
        await msg.edit(
            f"❌ Error during soft-mux! (Job: <code>{job_id}</code>)\n\n"
            f"<pre>{err_preview}</pre>",
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

    is_url  = vid_filename.startswith(("http://","https://"))
    vid_path = vid_filename if is_url else os.path.join(Config.DOWNLOAD_DIR, vid_filename)
    sub_path = os.path.join(Config.DOWNLOAD_DIR, sub_filename)

    total_dur  = await _probe_duration(vid_path)
    input_size = 0 if is_url else (os.path.getsize(vid_path) if os.path.exists(vid_path) else 0)

    vf = [f"subtitles={sub_path}:fontsdir={Config.FONTS_DIR}"]
    if res != 'original':
        vf.append(f"scale={res}")
    if fps != 'original':
        vf.append(f"fps={fps}")
    vf_arg = ",".join(vf)

    if is_url:
        base = uuid.uuid4().hex[:8]
    else:
        base = os.path.splitext(vid_filename)[0]
        
    output   = f"{base}_hard.mp4"
    out_path = os.path.join(Config.DOWNLOAD_DIR, output)

    if is_url:
        # ---- NEW: Build yt-dlp + ffmpeg shell command ----
        host = urlparse(vid_path).hostname or ""
        referer_url = f"https://{host}/" # Guess root domain as referer

        yt_dlp_cmd_parts = [
            YT_DLP_PATH, '-o', '-', '--no-progress',
            '--user-agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        ]

        if "dmcdn.net" in host or "dailymotion.com" in host:
            logger.info("Applying Dailymotion-specific referer")
            referer_url = "https://www.dailymotion.com"
        elif "topchineseanime.store" in host:
            logger.info("Applying TopChineseAnime referer and cookies")
            referer_url = "https://topchineseanime.xyz/"
            yt_dlp_cmd_parts += ['--cookies-from-domain', 'topchineseanime.xyz'] # <--- ADDED
        
        yt_dlp_cmd_parts += ['--referer', referer_url]
        yt_dlp_cmd_parts.append(vid_path)
        
        yt_dlp_cmd_str = " ".join([shlex.quote(p) for p in yt_dlp_cmd_parts])
        
        ffmpeg_cmd_parts = [
            'ffmpeg','-hide_banner',
            '-progress', 'pipe:2', '-nostats',
            '-i', '-',         # Read video from stdin (yt-dlp)
            '-i', sub_path,    # Read subtitle from file
            '-vf', vf_arg,
            '-c:v', codec, '-preset', preset, '-crf', crf,
            '-map','0:v:0','-map','0:a:0?',
            '-map', '1:s:0?',
            '-c:a','copy',
            '-c:s', 'mov_text',  # <---- THIS IS THE FIX
            '-y', out_path
        ]
        ffmpeg_cmd_str = " ".join([shlex.quote(p) for p in ffmpeg_cmd_parts])

        full_command = f"{yt_dlp_cmd_str} | {ffmpeg_cmd_str}"
        logger.info(f"Starting shell pipe for job {job_id}: yt-dlp | ffmpeg (hardmux)")
        
        proc = await asyncio.create_subprocess_shell(
            full_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
    else:
        # ---- OLD: Original logic for local files ----
        proc = await asyncio.create_subprocess_exec(
            'ffmpeg','-hide_banner',
            '-progress', 'pipe:2', '-nostats',
            '-i', vid_path,
            '-i', sub_path,
            '-vf', vf_arg,
            '-c:v', codec, '-preset', preset, '-crf', crf,
            '-map','0:v:0','-map','0:a:0?',
            '-map', '1:s:0?',
            '-c:a','copy',
            '-c:s', 'mov_text',  # <---- THIS IS THE FIX
            '-y', out_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

    # --- THIS PART IS THE SAME AS THE NEW nosub_encode (with error capture) ---
    reader = asyncio.create_task(read_stderr(start, msg, proc, job_id, total_dur, input_size))
    waiter = asyncio.create_task(proc.wait())
    running_jobs[job_id] = {'proc': proc, 'tasks': [reader, waiter]}

    await msg.edit(
        f"🔄 Hard-Mux job started: <code>{job_id}</code>\n"
        f"Send <code>/cancel {job_id}</code> to abort",
        parse_mode=ParseMode.HTML
    )

    await asyncio.wait([reader, waiter])
    
    full_stderr_lines = reader.result() or []
    running_jobs.pop(job_id, None)

    if proc.returncode == 0:
        await msg.edit(
            f"✅ Hard-Mux `<code>{job_id}</code>` completed in {round(time.time()-start)}s",
            parse_mode=ParseMode.HTML
        )
        await asyncio.sleep(2)
        return output
    else:
        full_stderr_text = "".join(full_stderr_lines)

        try:
            os.makedirs("logs", exist_ok=True)
            with open(os.path.join("logs", f"ffmpeg_{job_id}.log"), "a", encoding="utf-8", errors="ignore") as _f:
                _f.write(f"\n\n=== PROCESS EXITED WITH CODE {proc.returncode} ===")
        except:
            pass
        
        logger.error("Hard-mux failed for job %s — tail: %s", job_id, full_stderr_text[-1500:])
        err_preview = full_stderr_text[-1000:] 
        
        await msg.edit(
            f"❌ Error during hard-mux! (Job: <code>{job_id}</code>)\n\n"
            f"<pre>{err_preview}</pre>",
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
    
    # ALWAYS try to probe the duration. 
    # _probe_duration will work for URLs and files.
    # If it fails on a stream, it will correctly return 0.0.
    total_dur = await _probe_duration(vid_path)
    
    if is_url:
        input_size = 0
    else:
        input_size = os.path.getsize(vid_path) if os.path.exists(vid_path) else 0

    vf = []
    if res != 'original':
        vf.append(f"scale={res}")
    if fps != 'original':
        vf.append(f"fps={fps}")
    vf_args = ['-vf', ",".join(vf)] if vf else []

    if is_url:
        # Create a simple, unique name for URL encodes
        base = uuid.uuid4().hex[:8] 
    else:
        # Use the original filename for local files
        base = os.path.splitext(vid_filename)[0]

    output   = f"{base}_enc.mp4"
    out_path = os.path.join(Config.DOWNLOAD_DIR, output)

    if is_url:
        # ---- NEW: Build yt-dlp + ffmpeg shell command ----
        
        # Build the yt-dlp part
        host = urlparse(vid_path).hostname or ""
        referer_url = f"https://{host}/" # Guess root domain as referer

        yt_dlp_cmd_parts = [
            YT_DLP_PATH, '-o', '-', '--no-progress',
            '--user-agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        ]

        if "dmcdn.net" in host or "dailymotion.com" in host:
            logger.info("Applying Dailymotion-specific referer")
            referer_url = "https://www.dailymotion.com"
        elif "topchineseanime.store" in host:
            logger.info("Applying TopChineseAnime referer and cookies")
            referer_url = "https://topchineseanime.xyz/"
            yt_dlp_cmd_parts += ['--cookies-from-domain', 'topchineseanime.xyz'] # <--- ADDED
        
        yt_dlp_cmd_parts += ['--referer', referer_url]
        yt_dlp_cmd_parts.append(vid_path)
        
        # Quote each part for shell safety
        yt_dlp_cmd_str = " ".join([shlex.quote(p) for p in yt_dlp_cmd_parts])
        
        # Build the ffmpeg part
        ffmpeg_cmd_parts = [
            'ffmpeg', '-hide_banner', '-progress', 'pipe:2', '-nostats',
            '-i', '-', # Read from stdin
            *vf_args,
            '-c:v', codec, '-preset', preset, '-crf', crf,
            '-map', '0:v:0', '-map', '0:a:0?', '-c:a', 'copy',
            '-y', out_path
        ]
        ffmpeg_cmd_str = " ".join([shlex.quote(p) for p in ffmpeg_cmd_parts])

        # Create the full shell command
        full_command = f"{yt_dlp_cmd_str} | {ffmpeg_cmd_str}"
        
        logger.info(f"Starting shell pipe for job {job_id}: yt-dlp | ffmpeg")
        
        proc = await asyncio.create_subprocess_shell(
            full_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE  # read_stderr will capture output from both
        )
        
    else:
        # ---- OLD: Original logic for local files ----
        args = ['ffmpeg', '-hide_banner', '-progress', 'pipe:2', '-nostats']
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

    # --- THIS PART IS THE SAME AS BEFORE (and uses my previous log-capturing fix) ---
    reader = asyncio.create_task(read_stderr(start, msg, proc, job_id, total_dur, input_size))
    waiter = asyncio.create_task(proc.wait())
    running_jobs[job_id] = {'proc': proc, 'tasks': [reader, waiter]}

    await msg.edit(
        f"🔄 Encode (no-sub) job started: <code>{job_id}</code>\n"
        f"Send <code>/cancel {job_id}</code> to abort",
        parse_mode=ParseMode.HTML
    )

    await asyncio.wait([reader, waiter])
    
    # Get the captured lines from the reader task
    full_stderr_lines = reader.result() or [] # Ensure it's a list
    
    running_jobs.pop(job_id, None)

    if proc.returncode == 0:
        await msg.edit(
            f"✅ Encode `<code>{job_id}</code>` completed in {round(time.time()-start)}s",
            parse_mode=ParseMode.HTML
        )
        await asyncio.sleep(2)
        return output
    else:
        # Join the captured lines into a single string
        full_stderr_text = "".join(full_stderr_lines)

        try:
            os.makedirs("logs", exist_ok=True)
            with open(os.path.join("logs", f"ffmpeg_{job_id}.log"), "a", encoding="utf-8", errors="ignore") as _f:
                _f.write(f"\n\n=== PROCESS EXITED WITH CODE {proc.returncode} ===")
        except:
            pass
        
        # Log the tail of the error (now from yt-dlp OR ffmpeg)
        logger.error("No-sub encode failed for job %s — tail: %s", job_id, full_stderr_text[-1500:])
        
        err_preview = full_stderr_text[-1000:] 
        
        await msg.edit(
            f"❌ Error during encode! (Job: <code>{job_id}</code>)\n\n"
            f"<pre>{err_preview}</pre>",
            parse_mode=ParseMode.HTML
        )
        return False
