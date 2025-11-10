import logging, os, sys
from logging.handlers import RotatingFileHandler
from config import Config
from helper_func.dbhelper import Database as Db
from plugins.muxer import queue_worker

os.makedirs("logs", exist_ok=True)

log_fmt = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
logger = logging.getLogger()
logger.setLevel(logging.INFO)

from logging.handlers import RotatingFileHandler
fh = RotatingFileHandler("logs/bot.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
fh.setFormatter(logging.Formatter(log_fmt))
fh.setLevel(logging.INFO)

ch = logging.StreamHandler()
ch.setFormatter(logging.Formatter(log_fmt))
ch.setLevel(logging.INFO)

logger.handlers.clear()
logger.addHandler(fh)
logger.addHandler(ch)

logging.getLogger("pyrogram").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)

def _uncaught(exc_type, exc, tb):
    logging.getLogger("bot").error("Uncaught exception", exc_info=(exc_type, exc, tb))
sys.excepthook = _uncaught

db = Db().setup()
if not os.path.isdir(Config.DOWNLOAD_DIR):
    os.mkdir(Config.DOWNLOAD_DIR)

from pyrogram import Client
class QueueBot(Client):
    async def start(self):
        await super().start()
        # launch our single background worker
        self.loop.create_task(queue_worker(self))

app = QueueBot(
    "SubtitleMuxer",
    bot_token=Config.BOT_TOKEN,
    api_id=Config.APP_ID,
    api_hash=Config.API_HASH,
    plugins=dict(root="plugins")
)

if __name__ == "__main__":
    app.run()
