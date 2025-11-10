import logging, os, sys
from logging.handlers import RotatingFileHandler
from config import Config
from helper_func.dbhelper import Database as Db
from plugins.muxer import queue_worker

logging.basicConfig(level=logging.DEBUG,
                    format="%(asctime)s - %(name)s - %(message)s")
logging.getLogger('pyrogram').setLevel(logging.WARNING)

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
