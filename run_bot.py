import asyncio
import logging

from dotenv import load_dotenv

from app.bot import BotApp
from app.handlers import VideoBot
from app.queue_manager import QueueManager


async def main() -> None:
    load_dotenv()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    bot_app = BotApp()
    queue_manager = QueueManager()
    video_bot = VideoBot(queue_manager, bot_app.bot)
    bot_app.add_handlers(video_bot.build_handlers())

    application = bot_app.application

    await application.initialize()
    queue_manager.start()
    await application.start()
    await application.updater.start_polling()

    try:
        await asyncio.Event().wait()
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
