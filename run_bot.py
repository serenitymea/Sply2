import asyncio
import logging
import os
import shutil
import signal

from dotenv import load_dotenv

from app.bot import BotApp
from app.handlers import VideoBot
from app.paths import ensure_data_root, ensure_output_root, ensure_tmp_root
from app.queue_manager import QueueManager


logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _run_preflight_checks() -> None:
    _require_env("TELEGRAM_BOT_TOKEN")

    for binary in ("ffmpeg", "ffprobe"):
        if shutil.which(binary) is None:
            raise RuntimeError(f"Required binary is not available in PATH: {binary}")

    ensure_tmp_root()
    ensure_output_root()
    ensure_data_root()

    if os.environ.get("RAILWAY_REPLICA_ID"):
        logger.info("Railway replica id: %s", os.environ["RAILWAY_REPLICA_ID"])

    if os.environ.get("YT_DLP_COOKIES"):
        logger.warning("YT_DLP_COOKIES is configured. This may grant access to private content.")

    logger.info("Preflight checks passed")


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def _request_shutdown(sig_name: str) -> None:
        logger.info("Received %s, shutting down", sig_name)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_shutdown, sig.name)
        except NotImplementedError:
            signal.signal(sig, lambda *_: stop_event.set())


async def main() -> None:
    load_dotenv()
    _configure_logging()
    _run_preflight_checks()

    bot_app = BotApp()
    queue_manager = QueueManager()
    video_bot = VideoBot(queue_manager, bot_app.bot)
    bot_app.add_handlers(video_bot.build_handlers())
    application = bot_app.application
    stop_event = asyncio.Event()

    _install_signal_handlers(stop_event)

    await application.initialize()
    queue_manager.start()
    await application.start()
    await application.updater.start_polling()
    logger.info("Bot polling started")

    try:
        await stop_event.wait()
    finally:
        await video_bot.close()
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        logger.info("Bot shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
