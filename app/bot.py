import os
import logging
from telegram.ext import Application, ApplicationBuilder

logger = logging.getLogger(__name__)

TELEGRAM_CONNECT_TIMEOUT = 15.0
TELEGRAM_READ_TIMEOUT = 30.0
TELEGRAM_WRITE_TIMEOUT = 30.0
TELEGRAM_MEDIA_WRITE_TIMEOUT = 120.0
TELEGRAM_POOL_TIMEOUT = 15.0


class BotApp:
    def __init__(self):
        token = os.environ["TELEGRAM_BOT_TOKEN"]
        self._app: Application = (
            ApplicationBuilder()
            .token(token)
            .connect_timeout(TELEGRAM_CONNECT_TIMEOUT)
            .read_timeout(TELEGRAM_READ_TIMEOUT)
            .write_timeout(TELEGRAM_WRITE_TIMEOUT)
            .media_write_timeout(TELEGRAM_MEDIA_WRITE_TIMEOUT)
            .pool_timeout(TELEGRAM_POOL_TIMEOUT)
            .build()
        )

    @property
    def application(self) -> Application:
        return self._app

    @property
    def bot(self):
        return self._app.bot

    def add_handlers(self, handlers: list) -> None:
        for handler in handlers:
            self._app.add_handler(handler)
