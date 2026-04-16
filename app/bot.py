import os
import logging
from telegram.ext import Application, ApplicationBuilder

logger = logging.getLogger(__name__)


class BotApp:
    def __init__(self):
        token = os.environ["TELEGRAM_BOT_TOKEN"]
        self._app: Application = ApplicationBuilder().token(token).build()

    @property
    def application(self) -> Application:
        return self._app

    @property
    def bot(self):
        return self._app.bot

    def add_handlers(self, handlers: list) -> None:
        for handler in handlers:
            self._app.add_handler(handler)