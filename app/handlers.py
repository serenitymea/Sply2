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
from .runtime_control import RuntimeControl

logger = logging.getLogger(__name__)

WAIT_AUDIO, WAIT_VIDEO = range(2)

PROCESSING_TIMEOUT = 500
TELEGRAM_SEND_VIDEO_WRITE_TIMEOUT = 180
TELEGRAM_SEND_VIDEO_READ_TIMEOUT = 60
TELEGRAM_SEND_VIDEO_CONNECT_TIMEOUT = 20
TELEGRAM_SEND_VIDEO_POOL_TIMEOUT = 20
SESSION_TTL_SEC = 10 * 60
SESSION_CLEANUP_INTERVAL_SEC = 60
MAX_ACTIVE_SESSIONS = 20
SESSION_RUNTIME_TTL_SEC = SESSION_TTL_SEC + 120
PIPELINE_SLOT_TTL_SEC = PROCESSING_TIMEOUT + 120


@dataclass
class UserSession:
    tmp_dir: Path
    chat_id: int
    audio_path: Path = None
    video_files: list[str] = field(default_factory=list)
    total_video_duration: float = 0.0
    done_called: bool = False
    last_activity: float = field(default_factory=time.time)


class VideoBot:
    def __init__(self, queue_manager: QueueManager, bot: Bot):
        self._queue = queue_manager
        self._bot = bot
        self._sessions: dict[int, UserSession] = {}
        self._admin_ids = load_admin_ids()
        self._usage_limiter = DailyUsageLimiter()
        self._runtime = RuntimeControl()
        self._queue.set_process_callback(self._process_user)
        self._cleanup_stale_tmp()
        self._session_cleanup_task = asyncio.create_task(self._cleanup_expired_sessions_loop())

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

    def _touch_session(self, user_id: int, session: UserSession | None) -> None:
        if session:
            session.last_activity = time.time()
            self._runtime.touch_session(user_id, SESSION_RUNTIME_TTL_SEC)

    def _create_session(self, user_id: int, chat_id: int) -> UserSession:
        tmp_dir = ensure_tmp_root() / f"vbot_{user_id}_{int(time.time() * 1000)}"
        tmp_dir.mkdir(parents=True, exist_ok=False)
        session = UserSession(tmp_dir=tmp_dir, chat_id=chat_id)
        self._sessions[user_id] = session
        return session

    def _drop_session(self, user_id: int) -> None:
        session = self._sessions.pop(user_id, None)
        self._runtime.unregister_session(user_id)
        if session:
            shutil.rmtree(session.tmp_dir, ignore_errors=True)

    @staticmethod
    def _is_private_chat(update: Update) -> bool:
        chat = update.effective_chat
        return bool(chat and chat.type == "private")

    async def _require_private_chat(self, update: Update) -> bool:
        if self._is_private_chat(update):
            return True
        if update.effective_message:
            await update.effective_message.reply_text(
                "This bot only works in private chats. Message it directly."
            )
        return False

    async def close(self) -> None:
        if self._session_cleanup_task and not self._session_cleanup_task.done():
            self._session_cleanup_task.cancel()
            try:
                await self._session_cleanup_task
            except asyncio.CancelledError:
                pass

    async def _cleanup_expired_sessions_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(SESSION_CLEANUP_INTERVAL_SEC)
                now = time.time()
                expired_users = [
                    user_id
                    for user_id, session in self._sessions.items()
                    if not session.done_called and now - session.last_activity > SESSION_TTL_SEC
                ]
                for user_id in expired_users:
                    session = self._get_session(user_id)
                    if not session:
                        continue
                    logger.info(f"[Session] EXPIRED user={user_id}")
                    await self._notify(
                        session.chat_id,
                        "Your session expired due to inactivity. Start again with /run."
                    )
                    self._drop_session(user_id)
        except asyncio.CancelledError:
            logger.info("[Session] Cleanup loop stopped")
            raise


    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._require_private_chat(update):
            return ConversationHandler.END
        await update.message.reply_text(
            "👋 Hi! I create video edits for you.\n\n"
            "What I can do:\n"
            "• Receive video and audio files\n"
            "• Sync music with video\n"
            "• Create a video edit\n\n"
            "▶️ Press /run to start"
        )

    async def cmd_run(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._require_private_chat(update):
            return ConversationHandler.END
        user_id = update.effective_user.id

        remaining = await self._usage_limiter.get_remaining(
            user_id,
            is_admin=self._is_admin(user_id),
        )
        if remaining == 0:
            await update.message.reply_text(
                f"Today's limit is used up: {DAILY_GENERATION_LIMIT} generations.\n"
                "Try again tomorrow."
            )
            return ConversationHandler.END

        remaining_hint = (
            ""
            if remaining is None
            else f"\n\nGenerations left today: {remaining} of {DAILY_GENERATION_LIMIT}"
        )

        position = await self._queue.get_position(user_id)
        if position is not None:
            if position == 0:
                await update.message.reply_text("⚙️ Your job is already being processed. Hang tight, the result is coming soon!")
            else:
                await update.message.reply_text(
                    f"⏳ You are already in the queue at position {position}.\n"
                    "I will let you know when it is your turn!"
                )
            return ConversationHandler.END

        if not self._runtime.try_register_session(user_id, MAX_ACTIVE_SESSIONS, SESSION_RUNTIME_TTL_SEC):
            await update.message.reply_text(
                "There are too many active sessions right now. Try again a bit later."
            )
            return ConversationHandler.END

        self._drop_session(user_id)
        try:
            session = self._create_session(user_id, update.effective_chat.id)
        except Exception:
            self._runtime.unregister_session(user_id)
            raise
        self._touch_session(user_id, session)

        await update.message.reply_text(
            "🎬 Let's start!\n\n"
            "🎵 Step 1 of 2: Send an audio file or a link to a video with music\n"
            "(TikTok)\n\n"
            "❌ To cancel — /cancel"
            f"{remaining_hint}"
        )
        return WAIT_AUDIO

    async def cmd_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._require_private_chat(update):
            return ConversationHandler.END
        user_id = update.effective_user.id
        session = self._get_session(user_id)
        self._touch_session(user_id, session)

        if not session:
            await update.message.reply_text("Run /run first")
            return ConversationHandler.END

        if session.done_called:
            await update.message.reply_text("⏳ Already added to the queue, please wait...")
            return WAIT_VIDEO

        if not session.video_files:
            await update.message.reply_text(
                "❌ You need at least one video!\n"
                f"Send a video file (maximum {MAX_VIDEOS}), then /done"
            )
            return WAIT_VIDEO

        allowed, remaining = await self._usage_limiter.consume(
            user_id,
            is_admin=self._is_admin(user_id),
        )
        if not allowed:
            await update.message.reply_text(
                f"Today's limit is used up: {DAILY_GENERATION_LIMIT} generations.\n"
                "Try again tomorrow."
            )
            return ConversationHandler.END

        remaining_hint = (
            ""
            if remaining is None
            else f"\n\nGenerations left today: {remaining} of {DAILY_GENERATION_LIMIT}"
        )
        session.done_called = True
        position = await self._queue.enqueue(user_id)
        if position is None:
            await self._usage_limiter.refund(
                user_id,
                is_admin=self._is_admin(user_id),
            )
            session.done_called = False

        if position is None:
            await update.message.reply_text("⏳ Already in the queue, please wait...")
        elif position == 1:
            await update.message.reply_text(
                f"✅ Got it! {len(session.video_files)} video(s) + audio\n\n"
                "🚀 The queue is free, starting processing right now!\n"
                "This may take a few minutes..."
                f"{remaining_hint}"
            )
        else:
            await update.message.reply_text(
                f"✅ Got it! {len(session.video_files)} video(s) + audio\n\n"
                f"⏳ Your position in the queue: {position}\n"
                "I will let you know and start processing when it is your turn."
                f"{remaining_hint}"
            )
        return ConversationHandler.END

    async def cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._require_private_chat(update):
            return ConversationHandler.END
        user_id = update.effective_user.id
        session = self._get_session(user_id)
        self._touch_session(user_id, session)

        if not session:
            await update.message.reply_text("Nothing to cancel. Start with /run")
            return ConversationHandler.END

        cancel_result = await self._queue.cancel(user_id)
        if cancel_result == "active":
            await update.message.reply_text("Cancellation requested. Stopping the current processing job.")
            return ConversationHandler.END

        self._drop_session(user_id)
        await update.message.reply_text(
            "❌ Operation cancelled.\n"
            "Start again with /run when you are ready."
        )
        return ConversationHandler.END


    async def receive_audio(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._require_private_chat(update):
            return ConversationHandler.END
        user_id = update.effective_user.id
        session = self._get_session(user_id)
        self._touch_session(user_id, session)

        if not session:
            await update.message.reply_text("Session not found. Start with /run")
            return ConversationHandler.END

        svc = VideoService(session.tmp_dir, user_id=user_id)

        try:
            audio_path = await svc.acquire_audio(update.message)
        except asyncio.TimeoutError:
            await update.message.reply_text(
                "Could not download the audio — the 5-minute timeout was exceeded.\n\n"
                "Possible reasons:\n"
                "• The link is unavailable or blocked\n"
                "• The connection is very slow\n\n"
                "Try another link or send the file directly."
            )
            return WAIT_AUDIO
        except ValueError as e:
            await update.message.reply_text(
                f"❌ {e}\n\n"
                "Try again or send another file/link."
            )
            return WAIT_AUDIO
        except Exception as e:
            logger.exception(f"Audio error user={user_id}: {e}")
            await update.message.reply_text(
                "❌ Could not get the audio. Try again or send the file another way."
            )
            return WAIT_AUDIO

        session.audio_path = audio_path
        await update.message.reply_text(
            "✅ Audio received!\n\n"
            "🎬 Step 2 of 2: Send your video files\n"
            f"Maximum: {MAX_VIDEOS} videos, up to 20 MB each\n"
            "Total duration of all videos must be no more than 10 minutes.\n\n"
            "When you have sent everything, type /done\n"
            "❌ To cancel — /cancel"
        )
        return WAIT_VIDEO

    async def receive_video(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._require_private_chat(update):
            return ConversationHandler.END
        user_id = update.effective_user.id
        session = self._get_session(user_id)
        self._touch_session(user_id, session)

        if not session:
            await update.message.reply_text("Session not found. Start with /run")
            return ConversationHandler.END

        if len(session.video_files) >= MAX_VIDEOS:
            await update.message.reply_text(
                f"❌ The maximum number of videos has already been added ({MAX_VIDEOS}).\n"
                "Type /done to start processing."
            )
            return WAIT_VIDEO

        svc = VideoService(session.tmp_dir, user_id=user_id)
        idx = len(session.video_files)

        try:
            video_path, duration = await svc.acquire_video(
                update.message, idx, session.total_video_duration
            )
        except asyncio.TimeoutError:
            await update.message.reply_text(
                "Could not upload the video — it took too long.\n"
                "Try again or send a smaller file."
            )
            return WAIT_VIDEO
        except ValueError as e:
            await update.message.reply_text(
                f"❌ {e}\n\n"
                "Supported formats: mp4, mov, mkv"
            )
            return WAIT_VIDEO
        except RuntimeError as e:
            await update.message.reply_text(f"❌ {e}")
            return WAIT_VIDEO
        except Exception as e:
            logger.exception(f"Video error user={user_id}: {e}")
            await update.message.reply_text(
                "❌ Error while uploading the video. Try again."
            )
            return WAIT_VIDEO

        session.video_files.append(str(video_path))
        session.total_video_duration += duration
        count = len(session.video_files)
        remaining = MAX_VIDEOS - count

        total_min = int(session.total_video_duration) // 60
        total_sec = int(session.total_video_duration) % 60
        remaining_dur = MAX_TOTAL_DURATION_SEC - session.total_video_duration
        duration_info = f"Total duration: {total_min}m {total_sec}s / 10m"

        if remaining == 0 or remaining_dur < 10:
            await update.message.reply_text(
                f"✅ Video #{count} received.\n"
                f"{duration_info}\n\n"
                f"Limit reached ({MAX_VIDEOS} videos or 10 minutes).\n"
                "Type /done to start processing."
            )
        else:
            rem_min = int(remaining_dur) // 60
            rem_sec = int(remaining_dur) % 60
            await update.message.reply_text(
                f"✅ Video #{count} received.\n"
                f"{duration_info}\n"
                f"You can add {remaining} more video(s) (~{rem_min}m {rem_sec}s left).\n\n"
                "Send another video or /done to process."
            )
        return WAIT_VIDEO


    async def fallback_start_hint(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.message:
            return
        if not self._is_private_chat(update):
            return
        await update.message.reply_text(
            "Hi! 👋 Press /start to see how I work, or /run to start right away."
        )


    async def _process_user(self, user_id: int) -> None:
        session = self._sessions.get(user_id)
        if not session:
            logger.warning(f"No session for user={user_id}")
            return

        output_dir = ensure_output_root()
        output_path = output_dir / f"final_{user_id}_{int(time.time())}.mp4"
        start_time = time.monotonic()
        delivered = False

        logger.info(f"[Process] START user={user_id} videos={len(session.video_files)}")
        await self._notify(
            session.chat_id,
            f"🚀 Starting processing!\n"
            f"Videos: {len(session.video_files)}\n\n"
            "This will take a few minutes. I will send the result as soon as it is ready."
        )

        try:
            pipeline_token = await self._runtime.acquire_pipeline_slot(
                user_id,
                limit=1,
                ttl_sec=PIPELINE_SLOT_TTL_SEC,
            )
            pipeline = PipelineService(
                video_files=session.video_files,
                audio_path=session.audio_path,
                tmp_dir=session.tmp_dir,
                output_path=output_path,
            )
            try:
                await pipeline.run()
            finally:
                self._runtime.release_pipeline_slot(pipeline_token)

            if not output_path.exists() or output_path.stat().st_size == 0:
                raise RuntimeError("The output file was not created or is empty")

            elapsed = time.monotonic() - start_time
            logger.info(f"[Process] DONE user={user_id} elapsed={elapsed:.1f}s")
            await self._notify(session.chat_id, "📤 Processing finished! Sending the file...")

            with open(output_path, "rb") as f:
                await self._bot.send_video(
                    chat_id=session.chat_id,
                    video=f,
                    caption=f"🎬 Done! Processing time: {elapsed:.0f} sec.",
                    supports_streaming=True,
                    write_timeout=TELEGRAM_SEND_VIDEO_WRITE_TIMEOUT,
                    read_timeout=TELEGRAM_SEND_VIDEO_READ_TIMEOUT,
                    connect_timeout=TELEGRAM_SEND_VIDEO_CONNECT_TIMEOUT,
                    pool_timeout=TELEGRAM_SEND_VIDEO_POOL_TIMEOUT,
                )
            delivered = True

        except asyncio.CancelledError:
            reason = await self._queue.pop_stop_reason(user_id)
            if reason == "timeout":
                elapsed = time.monotonic() - start_time
                logger.error(f"[Process] TIMEOUT user={user_id} after {elapsed:.0f}s")
                await self._notify(
                    session.chat_id,
                    "Processing timed out (8 minutes).\n\n"
                    "Possible reasons:\n"
                    "• The videos are too large or too long\n"
                    "• High server load\n\n"
                    "Try with shorter clips or try again later. /run"
                )
                raise
            logger.info(f"[Process] CANCELLED user={user_id}")
            await self._notify(
                session.chat_id,
                "Processing cancelled. You can start again with /run."
            )
            raise
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - start_time
            logger.error(f"[Process] TIMEOUT user={user_id} after {elapsed:.0f}s")
            await self._notify(
                session.chat_id,
                "Processing timed out (8 minutes).\n\n"
                "Possible reasons:\n"
                "• The videos are too large or too long\n"
                "• High server load\n\n"
                "Try with shorter clips or try again later. /run"
            )
        except telegram.error.TimedOut as e:
            logger.exception(f"[Process] TELEGRAM TIMEOUT user={user_id}: {e}")
            await self._notify(
                session.chat_id,
                "Video was prepared, but Telegram timed out while uploading it.\n\n"
                "Please try again with /run. If it keeps happening, reduce the final video size or clip length."
            )
        except Exception as e:
            logger.exception(f"[Process] ERROR user={user_id}: {e}")
            await self._notify(
                session.chat_id,
                "❌ An error occurred while processing the video.\n\n"
                "Try starting again with /run\n"
                "If the error repeats, try different files."
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
