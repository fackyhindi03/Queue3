import logging
logging.basicConfig(level=logging.DEBUG,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

logger = logging.getLogger(__name__)
logging.getLogger("pyrogram").setLevel(logging.WARNING)

import os
import time
import re
import uuid
import requests
import aiohttp
from urllib.parse import unquote, urlparse
from chat import Chat
from config import Config
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from helper_func.progress_bar import progress_bar
from helper_func.dbhelper import Database as Db

db = Db()

async def _check_user(filt, c, m):
    chat_id = str(m.from_user.id)
    return chat_id in Config.ALLOWED_USERS

check_user = filters.create(_check_user)


# ================================
# Helpers for URL Downloads
# ================================
FILENAME_RE = re.compile(r'filename\*?=(?:UTF-8\'\')?("?)([^";]+)\1', re.IGNORECASE)

def _safe_filename(name: str) -> str:
    name = os.path.basename(name)
    name = name.replace('\r', '').replace('\n', '').strip()
    return re.sub(r'[\\/:*?"<>|]+', '_', name)

def _pick_name_from_url(url: str) -> str:
    path = urlparse(url).path
    tail = os.path.basename(path) or 'download.bin'
    return _safe_filename(unquote(tail))

def _maybe_add_ext(name: str, content_type: str) -> str:
    if (not os.path.splitext(name)[1]) and content_type and content_type.startswith('video/'):
        return name + '.mp4'
    return name

async def _download_http_with_progress(url: str, dest_dir: str, status_msg, start_time: float, job_id: str | None):
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=60, sock_read=600)
    headers = {"User-Agent": "Mozilla/5.0 (QueueBot/1.0)"}

    async with aiohttp.ClientSession(timeout=timeout, headers=headers, raise_for_status=True) as session:
        async with session.get(url, allow_redirects=True) as resp:
            total = int(resp.headers.get('Content-Length', '0') or 0)

            # Filename detection
            filename = None
            cd = resp.headers.get('Content-Disposition')
            if cd:
                m = FILENAME_RE.search(cd)
                if m:
                    filename = _safe_filename(m.group(2))

            if not filename:
                filename = _pick_name_from_url(str(resp.url))

            filename = _maybe_add_ext(filename, resp.headers.get('Content-Type', ''))

            # Ensure uniqueness
            base, ext = os.path.splitext(filename)
            unique_name = f"{base}_{uuid.uuid4().hex[:6]}{ext}"
            full_path = os.path.join(dest_dir, unique_name)

            downloaded = 0
            chunk_size = 1024 * 1024  # 1MB
            with open(full_path, 'wb') as f:
                async for chunk in resp.content.iter_chunked(chunk_size):
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded += len(chunk)

                    await progress_bar(
                        downloaded,
                        total if total > 0 else downloaded,
                        "Downloading from link…",
                        status_msg,
                        start_time,
                        job_id=job_id
                    )

    return unique_name


# ================================
# Handlers
# ================================

@Client.on_message(filters.document & check_user & filters.private)
async def save_doc(client, message):
    chat_id = message.from_user.id
    start_time = time.time()
    downloading = await client.send_message(chat_id, 'Downloading your File!')
    download_location = await client.download_media(
        message=message,
        file_name=Config.DOWNLOAD_DIR+'/',
        progress=progress_bar,
        progress_args=('Initializing', downloading, start_time)
    )

    if download_location is None:
        return client.edit_message_text(
            text='Downloading Failed!',
            chat_id=chat_id,
            message_id=downloading.id
        )

    await client.edit_message_text(
        text=Chat.DOWNLOAD_SUCCESS.format(round(time.time()-start_time)),
        chat_id=chat_id,
        message_id=downloading.id
    )

    tg_filename = os.path.basename(download_location)
    try:
        og_filename = message.document.filename
    except:
        og_filename = False

    save_filename = og_filename if og_filename else tg_filename
    ext = save_filename.split('.').pop()
    filename = str(round(start_time))+'.'+ext

    if ext in ['srt', 'ass']:
        os.rename(Config.DOWNLOAD_DIR+'/'+tg_filename, Config.DOWNLOAD_DIR+'/'+filename)
        db.put_sub(chat_id, filename)
        if db.check_video(chat_id):
            text = 'Subtitle file downloaded successfully.\nChoose : [ /softmux , /hardmux , /nosub ]'
        else:
            text = 'Subtitle file downloaded.\nNow send Video File!'
        await client.edit_message_text(text=text, chat_id=chat_id, message_id=downloading.id)

    elif ext in ['mp4', 'mkv']:
        os.rename(Config.DOWNLOAD_DIR+'/'+tg_filename, Config.DOWNLOAD_DIR+'/'+filename)
        db.put_video(chat_id, filename, save_filename)
        if db.check_sub(chat_id):
            text = 'Video file downloaded successfully.\nChoose : [ /softmux , /hardmux , /nosub ]'
        else:
            text = 'Video file downloaded successfully.\nChoose[ /softmux , /hardmux , /nosub ].'
        await client.edit_message_text(text=text, chat_id=chat_id, message_id=downloading.id)

    else:
        text = Chat.UNSUPPORTED_FORMAT.format(ext)+f'\nFile = {tg_filename}'
        await client.edit_message_text(text=text, chat_id=chat_id, message_id=downloading.id)
        os.remove(Config.DOWNLOAD_DIR+'/'+tg_filename)


@Client.on_message(filters.video & check_user & filters.private)
async def save_video(client, message):
    chat_id = message.from_user.id
    start_time = time.time()
    downloading = await client.send_message(chat_id, 'Downloading your File!')
    download_location = await client.download_media(
        message=message,
        file_name=Config.DOWNLOAD_DIR+'/',
        progress=progress_bar,
        progress_args=('Initializing', downloading, start_time)
    )

    if download_location is None:
        return client.edit_message_text(
            text='Downloading Failed!',
            chat_id=chat_id,
            message_id=downloading.id
        )

    await client.edit_message_text(
        text=Chat.DOWNLOAD_SUCCESS.format(round(time.time()-start_time)),
        chat_id=chat_id,
        message_id=downloading.id
    )

    tg_filename = os.path.basename(download_location)
    try:
        og_filename = message.document.filename
    except:
        og_filename = False

    save_filename = og_filename if og_filename else tg_filename
    ext = save_filename.split('.').pop()
    filename = str(round(start_time))+'.'+ext
    os.rename(Config.DOWNLOAD_DIR+'/'+tg_filename, Config.DOWNLOAD_DIR+'/'+filename)

    db.put_video(chat_id, filename, save_filename)
    if db.check_sub(chat_id):
        text = 'Video file downloaded successfully.\nChoose : [ /softmux , /hardmux , /nosub ]'
    else:
        text = 'Video file downloaded successfully.\nChoose[ /softmux , /hardmux , /nosub ].'
    await client.edit_message_text(text=text, chat_id=chat_id, message_id=downloading.id)


# ================================
# Direct Link Downloader
# ================================
@Client.on_message(filters.text & filters.regex('^http') & check_user & filters.private)
async def save_url(client, message):
    chat_id = message.from_user.id
    url = message.text.strip()
    sent = await client.send_message(chat_id, "Fetching link…")
    t0 = time.time()

    try:
        os.makedirs(Config.DOWNLOAD_DIR, exist_ok=True)
        job_id = uuid.uuid4().hex[:8]

        saved_name = await _download_http_with_progress(
            url=url,
            dest_dir=Config.DOWNLOAD_DIR,
            status_msg=sent,
            start_time=t0,
            job_id=job_id
        )

        db.put_video(chat_id, saved_name, saved_name)
        if db.check_sub(chat_id):
            text = 'Video File Downloaded.\nChoose : [ /softmux , /hardmux , /nosub ]'
        else:
            text = 'Video file downloaded successfully.\nChoose[ /softmux , /hardmux , /nosub ].'
        await sent.edit_text(text)

    except Exception as e:
        try:
            await sent.edit_text(f"❌ Failed to download from link.\n<code>{str(e)}</code>", parse_mode=ParseMode.HTML)
        except:
            pass
