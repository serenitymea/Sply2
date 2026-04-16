import asyncio
import logging
import shutil
import time
from pathlib import Path
from dataclasses import dataclass, field

import telegram
from telegram import Update, Bot
from telegram.ext import (
    CommandHandler, MessageHandler, ContextTypes,
    ConversationHandler, filters,
)

from .services.video_service import VideoService, MAX_VIDEOS, MAX_TOTAL_DURATION_SEC
from .services.pipeline_service import PipelineService
from .access_control import DAILY_GENERATION_LIMIT, DailyUsageLimiter, load_admin_ids
from .paths import ensure_output_root, ensure_tmp_root
from .queue_manager import QueueManager

logger = logging.getLogger(__name__)

WAIT_AUDIO, WAIT_VIDEO = range(2)

PROCESSING_TIMEOUT = 500


@dataclass
class UserSession:
    tmp_dir: Path
    chat_id: int
    audio_path: Path = None
    video_files: list[str] = field(default_factory=list)
    total_video_duration: float = 0.0
    done_called: bool = False


class VideoBot:
    def __init__(self, queue_manager: QueueManager, bot: Bot):
        self._queue = queue_manager
        self._bot = bot
        self._sessions: dict[int, UserSession] = {}
        self._admin_ids = load_admin_ids()
        self._usage_limiter = DailyUsageLimiter()
        self._queue.set_process_callback(self._process_user)
        self._cleanup_stale_tmp()

    @staticmethod
    def _cleanup_stale_tmp() -> None:
        stale = list(ensure_tmp_root().glob("vbot_*"))
        for path in stale:
            try:
                shutil.rmtree(path, ignore_errors=True)
                logger.info(f"Cleaned up stale tmp dir: {path}")
            except Exception as e:
                logger.warning(f"Failed to clean up {path}: {e}")
        if stale:
            logger.info(f"Cleaned up {len(stale)} stale tmp dir(s) on startup")

    async def _notify(self, chat_id: int, text: str) -> None:
        try:
            await self._bot.send_message(chat_id=chat_id, text=text)
        except Exception as e:
            logger.warning(f"Notify failed chat={chat_id}: {e}")

    def _get_session(self, user_id: int) -> UserSession | None:
        return self._sessions.get(user_id)

    def _is_admin(self, user_id: int) -> bool:
        return user_id in self._admin_ids

    def _create_session(self, user_id: int, chat_id: int) -> UserSession:
        tmp_dir = ensure_tmp_root() / f"vbot_{user_id}_{int(time.time() * 1000)}"
        tmp_dir.mkdir(parents=True, exist_ok=False)
        session = UserSession(tmp_dir=tmp_dir, chat_id=chat_id)
        self._sessions[user_id] = session
        return session

    def _drop_session(self, user_id: int) -> None:
        session = self._sessions.pop(user_id, None)
        if session:
            shutil.rmtree(session.tmp_dir, ignore_errors=True)


    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "👋 Привет! Я бот для создания видео-эдитов.\n\n"
            "Что я умею:\n"
            "• Принимать видео и аудио\n"
            "• Синхронизировать музыку с видео\n"
            "• Делать видео-эдит\n\n"
            "▶️ Нажми /run чтобы начать"
        )

    async def cmd_run(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id

        remaining = await self._usage_limiter.get_remaining(
            user_id,
            is_admin=self._is_admin(user_id),
        )
        if remaining == 0:
            await update.message.reply_text(
                f"Лимит на сегодня исчерпан: {DAILY_GENERATION_LIMIT} генераций.\n"
                "Попробуй снова завтра."
            )
            return ConversationHandler.END

        position = await self._queue.get_position(user_id)
        if position is not None:
            if position == 0:
                await update.message.reply_text("⚙️ Твоё задание уже обрабатывается. Жди, скоро будет результат!")
            else:
                await update.message.reply_text(
                    f"⏳ Ты уже в очереди на позиции {position}.\n"
                    "Когда подойдёт твоя очередь — сообщу!"
                )
            return ConversationHandler.END

        self._drop_session(user_id)
        self._create_session(user_id, update.effective_chat.id)

        await update.message.reply_text(
            "🎬 Начинаем!\n\n"
            "🎵 Шаг 1 из 2: Отправь аудио файл или ссылку на музыку\n"
            "(TikTok)\n\n"
            "❌ Для отмены — /cancel"
        )
        return WAIT_AUDIO

    async def cmd_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        session = self._get_session(user_id)

        if not session:
            await update.message.reply_text("Сначала запусти /run")
            return ConversationHandler.END

        if session.done_called:
            await update.message.reply_text("⏳ Уже добавлено в очередь, ожидай...")
            return WAIT_VIDEO

        if not session.video_files:
            await update.message.reply_text(
                "❌ Нужно хотя бы одно видео!\n"
                f"Отправь видео файл (максимум {MAX_VIDEOS} штук), затем /done"
            )
            return WAIT_VIDEO

        allowed, remaining = await self._usage_limiter.consume(
            user_id,
            is_admin=self._is_admin(user_id),
        )
        if not allowed:
            await update.message.reply_text(
                f"Лимит на сегодня исчерпан: {DAILY_GENERATION_LIMIT} генераций.\n"
                "Попробуй снова завтра."
            )
            return ConversationHandler.END

        remaining_hint = "" if remaining is None else f"\nОсталось генераций сегодня: {remaining}"
        session.done_called = True
        position = await self._queue.enqueue(user_id)

        if position is None:
            await update.message.reply_text("⏳ Уже в очереди, ожидай...")
        elif position == 1:
            await update.message.reply_text(
                f"✅ Принято! {len(session.video_files)} видео + аудио\n\n"
                "🚀 Очередь свободна, начинаю обработку прямо сейчас!\n"
                "Это может занять несколько минут..."
            )
        else:
            await update.message.reply_text(
                f"✅ Принято! {len(session.video_files)} видео + аудио\n\n"
                f"⏳ Твоя позиция в очереди: {position}\n"
                "Как только подойдёт очередь — сообщу и начну обработку."
            )
        return ConversationHandler.END

    async def cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        session = self._get_session(user_id)

        if not session:
            await update.message.reply_text("Нечего отменять. Начни с /run")
            return ConversationHandler.END

        cancel_result = await self._queue.cancel(user_id)
        if cancel_result == "active":
            await update.message.reply_text("Отмена запрошена. Останавливаю текущую обработку.")
            return ConversationHandler.END

        self._drop_session(user_id)
        await update.message.reply_text(
            "❌ Операция отменена.\n"
            "Начни заново с /run когда будешь готов."
        )
        return ConversationHandler.END


    async def receive_audio(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        session = self._get_session(user_id)

        if not session:
            await update.message.reply_text("Сессия не найдена. Начни с /run")
            return ConversationHandler.END

        svc = VideoService(session.tmp_dir)

        try:
            audio_path = await svc.acquire_audio(update.message)
        except asyncio.TimeoutError:
            await update.message.reply_text(
                "Не удалось скачать аудио — превышено время ожидания (5 минут).\n\n"
                "Возможные причины:\n"
                "• Ссылка недоступна или заблокирована\n"
                "• Очень медленное соединение\n\n"
                "Попробуй другую ссылку или отправь файл напрямую."
            )
            return WAIT_AUDIO
        except ValueError as e:
            await update.message.reply_text(
                f"❌ {e}\n\n"
                "Попробуй ещё раз или отправь другой файл/ссылку."
            )
            return WAIT_AUDIO
        except Exception as e:
            logger.exception(f"Audio error user={user_id}: {e}")
            await update.message.reply_text(
                "❌ Не удалось получить аудио. Попробуй ещё раз или отправь файл другим способом."
            )
            return WAIT_AUDIO

        session.audio_path = audio_path
        await update.message.reply_text(
            "✅ Аудио получено!\n\n"
            "🎬 Шаг 2 из 2: Отправляй видео файлы\n"
            f"Максимум: {MAX_VIDEOS} видео, каждое до 20 MB\n"
            "Суммарная длительность всех видео — не более 10 минут.\n\n"
            "Когда отправишь все — напиши /done\n"
            "❌ Для отмены — /cancel"
        )
        return WAIT_VIDEO

    async def receive_video(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        session = self._get_session(user_id)

        if not session:
            await update.message.reply_text("Сессия не найдена. Начни с /run")
            return ConversationHandler.END

        if len(session.video_files) >= MAX_VIDEOS:
            await update.message.reply_text(
                f"❌ Уже добавлено максимум видео ({MAX_VIDEOS}).\n"
                "Напиши /done для начала обработки."
            )
            return WAIT_VIDEO

        svc = VideoService(session.tmp_dir)
        idx = len(session.video_files)

        try:
            video_path, duration = await svc.acquire_video(
                update.message, idx, session.total_video_duration
            )
        except asyncio.TimeoutError:
            await update.message.reply_text(
                "Не удалось загрузить видео — слишком долго.\n"
                "Попробуй ещё раз или отправь файл меньшего размера."
            )
            return WAIT_VIDEO
        except ValueError as e:
            await update.message.reply_text(
                f"❌ {e}\n\n"
                "Поддерживаемые форматы: mp4, mov, mkv"
            )
            return WAIT_VIDEO
        except RuntimeError as e:
            await update.message.reply_text(f"❌ {e}")
            return WAIT_VIDEO
        except Exception as e:
            logger.exception(f"Video error user={user_id}: {e}")
            await update.message.reply_text(
                "❌ Ошибка при загрузке видео. Попробуй ещё раз."
            )
            return WAIT_VIDEO

        session.video_files.append(str(video_path))
        session.total_video_duration += duration
        count = len(session.video_files)
        remaining = MAX_VIDEOS - count

        total_min = int(session.total_video_duration) // 60
        total_sec = int(session.total_video_duration) % 60
        remaining_dur = MAX_TOTAL_DURATION_SEC - session.total_video_duration
        duration_info = f"Суммарная длительность: {total_min}м {total_sec}с / 10м"

        if remaining == 0 or remaining_dur < 10:
            await update.message.reply_text(
                f"✅ Видео #{count} получено.\n"
                f"{duration_info}\n\n"
                f"Достигнут лимит ({MAX_VIDEOS} видео или 10 минут).\n"
                "Напиши /done для начала обработки."
            )
        else:
            rem_min = int(remaining_dur) // 60
            rem_sec = int(remaining_dur) % 60
            await update.message.reply_text(
                f"✅ Видео #{count} получено.\n"
                f"{duration_info}\n"
                f"Можно добавить ещё {remaining} видео (осталось ~{rem_min}м {rem_sec}с).\n\n"
                "Отправь ещё видео или /done для обработки."
            )
        return WAIT_VIDEO


    async def fallback_start_hint(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.message:
            return
        await update.message.reply_text(
            "Привет! 👋 Нажми /start чтобы узнать как я работаю, или /run чтобы сразу начать."
        )


    async def _process_user(self, user_id: int) -> None:
        session = self._sessions.get(user_id)
        if not session:
            logger.warning(f"No session for user={user_id}")
            return

        output_dir = ensure_output_root()
        output_path = output_dir / f"final_{user_id}_{int(time.time())}.mp4"
        start_time = time.monotonic()

        logger.info(f"[Process] START user={user_id} videos={len(session.video_files)}")
        await self._notify(
            session.chat_id,
            f"🚀 Начинаю обработку!\n"
            f"Видео: {len(session.video_files)} шт.\n\n"
            "Это займёт несколько минут. Пришлю результат как только будет готово."
        )

        try:
            pipeline = PipelineService(
                video_files=session.video_files,
                audio_path=session.audio_path,
                tmp_dir=session.tmp_dir,
                output_path=output_path,
            )
            await pipeline.run()

            if not output_path.exists() or output_path.stat().st_size == 0:
                raise RuntimeError("Выходной файл не был создан или пустой")

            elapsed = time.monotonic() - start_time
            logger.info(f"[Process] DONE user={user_id} elapsed={elapsed:.1f}s")
            await self._notify(session.chat_id, "📤 Обработка завершена! Отправляю файл...")

            with open(output_path, "rb") as f:
                await self._bot.send_video(
                    chat_id=session.chat_id,
                    video=f,
                    caption=f"🎬 Готово! Время обработки: {elapsed:.0f} сек.",
                    supports_streaming=True,
                )

        except asyncio.CancelledError:
            logger.info(f"[Process] CANCELLED user={user_id}")
            await self._notify(
                session.chat_id,
                "Обработка отменена. Можешь начать заново с /run."
            )
            raise
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - start_time
            logger.error(f"[Process] TIMEOUT user={user_id} after {elapsed:.0f}s")
            await self._notify(
                session.chat_id,
                "Превышено время обработки (8 минут).\n\n"
                "Возможные причины:\n"
                "• Слишком большие или длинные видео\n"
                "• Высокая нагрузка на сервер\n\n"
                "Попробуй с более короткими клипами или позже. /run"
            )
        except Exception as e:
            logger.exception(f"[Process] ERROR user={user_id}: {e}")
            await self._notify(
                session.chat_id,
                "❌ Произошла ошибка при обработке видео.\n\n"
                "Попробуй начать заново через /run\n"
                "Если ошибка повторяется — попробуй другие файлы."
            )
        finally:
            self._drop_session(user_id)
            if output_path.exists():
                output_path.unlink(missing_ok=True)


    def build_handlers(self) -> list:
        conv = ConversationHandler(
            entry_points=[CommandHandler("run", self.cmd_run)],
            states={
                WAIT_AUDIO: [
                    MessageHandler(
                        (filters.AUDIO | filters.VOICE | filters.Document.ALL | filters.TEXT) & ~filters.COMMAND,
                        self.receive_audio,
                    )
                ],
                WAIT_VIDEO: [
                    CommandHandler("done", self.cmd_done),
                    MessageHandler(
                        (filters.VIDEO | filters.Document.ALL) & ~filters.COMMAND,
                        self.receive_video,
                    ),
                ],
            },
            fallbacks=[CommandHandler("cancel", self.cmd_cancel)],
            per_user=True,
            per_chat=False,
            allow_reentry=True,
        )

        return [
            CommandHandler("start", self.cmd_start),
            conv,
            MessageHandler(filters.ALL, self.fallback_start_hint),
        ]
