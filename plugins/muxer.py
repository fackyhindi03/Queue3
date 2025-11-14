from chat import Chat  
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from helper_func.queue import Job, job_queue
from helper_func.mux   import softmux_vid, hardmux_vid, nosub_encode, running_jobs
from helper_func.progress_bar import progress_bar
from helper_func.dbhelper       import Database as Db
from config import Config
import uuid, time, os, asyncio, sys, sqlite3
import logging
logger = logging.getLogger("muxer")

db = Db()
_PENDING_RENAME = {} 

async def _check_user(filt, client, message):
    # First, check if message.from_user even exists.
    if not message.from_user:
        return False
    
    # If it exists, then check the ID.
    return str(message.from_user.id) in Config.ALLOWED_USERS
check_user = filters.create(_check_user)

# ADD THIS NEW FILTER
async def _is_pending_rename(filt, c, m):
    # 1. Check if it's a text message
    if not m.text or m.text.startswith("/"):
        return False
    
    # 2. Check if this user is actually in the rename-pending dictionary
    return m.from_user.id in _PENDING_RENAME

is_pending_rename_filter = filters.create(_is_pending_rename)

# THIS IS THE NEW FUNCTION
async def _ask_for_name(client, chat_id, mode, vid, sub, default_name):
    status = await client.send_message(
        chat_id,
        text=Chat.RENAME_PROMPT.format(default_name), # <--- THE CHANGE IS HERE
        parse_mode=ParseMode.HTML
    )
    _PENDING_RENAME[chat_id] = dict(
        mode=mode, vid=vid, sub=sub, default_name=default_name, status_msg=status
    )

# --------------------- COMMANDS ---------------------

@Client.on_message(filters.command('softmux') & check_user & filters.private)
async def enqueue_soft(client, message):
    logger.info("/softmux by %s", message.from_user.id)
    chat_id = message.from_user.id
    vid     = db.get_vid_filename(chat_id)
    sub     = db.get_sub_filename(chat_id)
    if not vid or not sub:
        text = ''
        if not vid: text += 'First send a Video File\n'
        if not sub: text += 'Send a Subtitle File!'
        return await client.send_message(chat_id, text, parse_mode=ParseMode.HTML)

    # Get the default name
    final_name = db.get_filename(chat_id)
    # Ask the user for a new name
    await _ask_for_name(client, chat_id, 'soft', vid, sub, final_name)

@Client.on_message(filters.command('hardmux') & check_user & filters.private)
async def enqueue_hard(client, message):
    logger.info("/hardmux by %s", message.from_user.id)
    chat_id = message.from_user.id
    vid     = db.get_vid_filename(chat_id)
    sub     = db.get_sub_filename(chat_id)
    if not vid or not sub:
        text = ''
        if not vid: text += 'First send a Video File\n'
        if not sub: text += 'Send a Subtitle File!'
        return await client.send_message(chat_id, text, parse_mode=ParseMode.HTML)

    # Get the default name
    final_name = db.get_filename(chat_id)
    # Ask the user for a new name
    await _ask_for_name(client, chat_id, 'hard', vid, sub, final_name)
    
@Client.on_message(filters.command('nosub') & check_user & filters.private)
async def enqueue_nosub(client, message):
    logger.info("/nosub by %s", message.from_user.id)
    chat_id = message.from_user.id
    vid     = db.get_vid_filename(chat_id)
    if not vid:
        return await client.send_message(chat_id, 'First send a Video File', parse_mode=ParseMode.HTML)

    # Get the default name
    final_name = db.get_filename(chat_id)
    # Ask the user for a new name
    await _ask_for_name(client, chat_id, 'nosub', vid, None, final_name)


@Client.on_message(filters.text & check_user & filters.private & is_pending_rename_filter)
async def handle_rename_reply(client, message):
    chat_id = message.from_user.id
    
    # Check if this user has a pending rename operation
    pending = _PENDING_RENAME.pop(chat_id, None)
    if not pending:
        # If not, it's a regular message, so do nothing
        return

    # User has replied, get the desired filename
    user_text = message.text.strip()
    
    if user_text.lower() == "default":
        final_name = pending["default_name"]
    else:
        # You might want to add more validation here (e.g., check for .mkv/.mp4)
        final_name = user_text

    # --- Now we do what the command handlers used to do ---
    
    # Delete the "Please send a name" message
    try:
        await pending["status_msg"].delete()
    except:
        pass

    job_id = uuid.uuid4().hex[:8]
    status = await client.send_message(
        chat_id,
        f"🧾 Job <code>{job_id}</code> (<code>{final_name}</code>) enqueued at position {job_queue.qsize() + 1}",
        parse_mode=ParseMode.HTML
    )

    # Enqueue the job with all the saved details and the new final_name
    await job_queue.put(Job(
        job_id=job_id,
        mode=pending["mode"],
        chat_id=chat_id,
        vid=pending["vid"],
        sub=pending["sub"],
        final_name=final_name,
        status_msg=status
    ))
    
    # Finally, erase the DB entry
    db.erase(chat_id)


@Client.on_message(filters.command('m3u8') & check_user & filters.private)
async def enqueue_m3u8(client, message):
    logger.info("/m3u8 by %s", message.from_user.id)
    # Usage: /m3u8 <m3u8_url> [output_name.mp4]
    parts = message.text.split(maxsplit=2)
    if len(parts) < 2:
        return await message.reply_text(
            "Usage:\n/m3u8 <m3u8_url> [output_name.mp4]",
            parse_mode=ParseMode.HTML
        )

    url = parts[1].strip()
    # Simple validation
    if not (url.startswith("http://") or url.startswith("https://")) or ".m3u8" not in url:
        return await message.reply_text("Please provide a valid .m3u8 URL.", parse_mode=ParseMode.HTML)

    # Optional custom name
    final_name = parts[2].strip() if len(parts) == 3 else f"{uuid.uuid4().hex[:6]}_enc.mp4"

    chat_id = message.from_user.id
    # Reset any previous staged files for this user
    db.erase(chat_id)
    # Store the URL as 'vid_name' so nosub_encode picks it up
    db.set_vid_filename(chat_id, url)
    db.set_filename(chat_id, final_name)

    job_id  = uuid.uuid4().hex[:8]
    status  = await client.send_message(
        chat_id,
        f"🧾 Job <code>{job_id}</code> enqueued at position {job_queue.qsize() + 1}",
        parse_mode=ParseMode.HTML
    )
    await job_queue.put(Job(job_id, 'nosub', chat_id, url, None, final_name, status))


@Client.on_message(filters.command('cancel') & check_user & filters.private)
async def cancel_job(client, message):
    logger.info("/cancel by %s", message.from_user.id)
    if len(message.command) != 2:
        return await message.reply_text("Usage: /cancel <job_id>", parse_mode=ParseMode.HTML)
    target = message.command[1]

    # Remove from pending queue if not started
    removed = False
    temp_q  = asyncio.Queue()
    while not job_queue.empty():
        job = await job_queue.get()
        if job.job_id == target:
            removed = True
            await job.status_msg.edit(f"❌ Job <code>{target}</code> cancelled before start.", parse_mode=ParseMode.HTML)
        else:
            await temp_q.put(job)
        job_queue.task_done()
    while not temp_q.empty():
        await job_queue.put(await temp_q.get())

    if removed:
        return

    # If running, kill ffmpeg
    entry = running_jobs.get(target)
    if not entry:
        return await message.reply_text(f"No job `<code>{target}</code>` found.", parse_mode=ParseMode.HTML)

    entry['proc'].kill()
    for t in entry['tasks']:
        t.cancel()
    running_jobs.pop(target, None)
    await message.reply_text(f"🛑 Job `<code>{target}</code>` aborted.", parse_mode=ParseMode.HTML)

# --------------------- WORKER ---------------------

async def queue_worker(client: Client):
    while True:
        job = await job_queue.get()

        try:
            await job.status_msg.edit(
                f"▶️ Starting <code>{job.job_id}</code> ({job.mode})…  "
                f"Use <code>/cancel {job.job_id}</code> to abort.",
                parse_mode=ParseMode.HTML
            )

            if job.mode == 'soft':
                out_file = await softmux_vid(job.vid, job.sub, msg=job.status_msg, job_id=job.job_id)
            elif job.mode == 'hard':
                out_file = await hardmux_vid(job.vid, job.sub, msg=job.status_msg, job_id=job.job_id)
            else:  # nosub
                out_file = await nosub_encode(job.vid, msg=job.status_msg, job_id=job.job_id)

            if not out_file:
                logp = os.path.join("logs", f"ffmpeg_{job.job_id}.log")
                sent = False
                if os.path.exists(logp) and os.path.getsize(logp) > 0:
                    try:
                        await client.send_document(
                            job.chat_id,
                            document=logp,
                            caption=f"FFmpeg log for job {job.job_id}",
                            file_name=os.path.basename(logp)
                        )
                        sent = True
                    except:
                        sent = False

                await job.status_msg.edit(
                    "❌ Job <code>{}</code> failed. {}{}".format(
                        job.job_id,
                        "Log sent above. " if sent else "",
                        f"Check <code>{logp}</code>" if not sent else ""
                    ),
                    parse_mode=ParseMode.HTML
                )
            else:
                # rename to desired final name
                src = os.path.join(Config.DOWNLOAD_DIR, out_file)
                dst = os.path.join(Config.DOWNLOAD_DIR, job.final_name)
                try:
                    os.rename(src, dst)
                except Exception:
                    dst = src

                # upload with progress UI
                t0 = time.time()
                await client.send_document(
                    job.chat_id,
                    document=dst,
                    caption=job.final_name,
                    file_name=job.final_name,
                    force_document=True,
                    progress=progress_bar,
                    progress_args=('Uploading…', job.status_msg, t0, job.job_id)
                )

                await job.status_msg.edit(
                    f"✅ Job <code>{job.job_id}</code> done.",
                    parse_mode=ParseMode.HTML
                )

                # cleanup best-effort
                for fn in (job.vid, job.sub, job.final_name):
                    try:
                        if fn:
                            os.remove(os.path.join(Config.DOWNLOAD_DIR, fn))
                    except:
                        pass

        except Exception:
            logger.exception("Unhandled error in queue_worker for job %s", job.job_id)
            try:
                await job.status_msg.edit(
                    f"💥 Unexpected error in <code>{job.job_id}</code>. See <code>logs/bot.log</code>.",
                    parse_mode=ParseMode.HTML
                )
            except:
                pass
        finally:
            job_queue.task_done()


@Client.on_message(filters.command('restart') & check_user & filters.private)
async def restart_bot(client, message):
    logger.warning("/restart by %s", message.from_user.id)
    await message.reply_text("♻️ Your All tasks and settings are now reset ✅")

    # 1) Stop any running ffmpeg processes from our tracked jobs
    try:
        for jid, entry in list(running_jobs.items()):
            try:
                entry['proc'].kill()
            except:
                pass
            for t in entry.get('tasks', []):
                try:
                    t.cancel()
                except:
                    pass
            running_jobs.pop(jid, None)
    except:
        pass

    # 2) Clear the DB table to reset staged files/filenames for everyone
    try:
        conn = sqlite3.connect('muxdb.sqlite', check_same_thread=False)
        conn.execute('DELETE FROM muxbot;')
        conn.commit()
        conn.close()
    except:
        pass

    # 3) Restart the current Python process (works on most hosts)
    await asyncio.sleep(1)
    os.execv(sys.executable, [sys.executable] + sys.argv)
