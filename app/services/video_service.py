import asyncio
import logging
import shutil
from pathlib import Path

from .downloader import MediaDownloader
from .ffmpeg_service import FFmpegService

logger = logging.getLogger(__name__)

MAX_FILE_SIZE_BYTES = 200 * 1024 * 1024   # 200 MB
MAX_VIDEOS = 5
MAX_TOTAL_DURATION_SEC = 10 * 60  # 10 min

SUPPORTED_AUDIO_MIME = {
    "audio/mpeg", "audio/mp3", "audio/ogg", "audio/wav",
    "audio/flac", "audio/mp4", "audio/x-m4a",
}
SUPPORTED_VIDEO_MIME = {
    "video/mp4", "video/quicktime", "video/x-matroska",
    "video/webm", "video/avi", "video/x-msvideo",
}

TG_DOWNLOAD_TIMEOUT = 120


def _is_url(text: str) -> bool:
    t = text.strip().lower()
    return t.startswith(("http://", "https://", "www."))


def _check_size(file_obj) -> None:
    size = getattr(file_obj, "file_size", None)
    if size and size > MAX_FILE_SIZE_BYTES:
        mb = size // (1024 * 1024)
        raise ValueError(f"Файл слишком большой ({mb} MB). Максимум — 200 MB.")


async def _download_tg_file(file_obj, dest: Path) -> None:
    tg_file = await asyncio.wait_for(file_obj.get_file(), timeout=30)
    await asyncio.wait_for(tg_file.download_to_drive(str(dest)), timeout=TG_DOWNLOAD_TIMEOUT)


async def get_video_duration(video_path: Path) -> float:

    try:
        process = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=15)
        raw = stdout.decode().strip()
        if not raw:
            err = stderr.decode(errors="replace").strip()
            logger.error(f"ffprobe returned empty output for {video_path}: {err}")
            raise RuntimeError("Не удалось определить длительность видео. Попробуй другой файл.")
        return float(raw)
    except (asyncio.TimeoutError, FileNotFoundError) as e:
        logger.error(f"ffprobe failed for {video_path}: {e}")
        raise RuntimeError("Не удалось проверить длительность видео. Убедись, что ffprobe установлен.")
    except RuntimeError:
        raise
    except Exception as e:
        logger.error(f"ffprobe unexpected error for {video_path}: {e}")
        raise RuntimeError("Не удалось определить длительность видео. Попробуй другой файл.")


class VideoService:
    def __init__(self, tmp_dir: Path):
        self._tmp_dir = tmp_dir
        self._ffmpeg = FFmpegService()
        self._downloader = MediaDownloader(tmp_dir)

    async def acquire_audio(self, msg) -> Path:
        audio_path = self._tmp_dir / "m1.mp3"

        if msg.text and _is_url(msg.text):
            await msg.reply_text("⏬ Скачиваю аудио по ссылке... Это может занять до нескольких минут.")
            downloaded = await self._downloader.download(msg.text.strip())
            shutil.copy(downloaded, audio_path)

        elif msg.audio or msg.voice or msg.document:
            file_obj = None
            if msg.audio:
                file_obj = msg.audio
            elif msg.voice:
                file_obj = msg.voice
            elif msg.document and msg.document.mime_type in SUPPORTED_AUDIO_MIME:
                file_obj = msg.document

            if not file_obj:
                raise ValueError(
                    "Неподдерживаемый формат документа.\n"
                    "Поддерживаются: mp3, wav, ogg, flac, m4a\n"
                )

            _check_size(file_obj)
            await msg.reply_text("⏬ Получаю аудио файл...")

            raw_path = self._tmp_dir / "audio_raw"
            try:
                await _download_tg_file(file_obj, raw_path)
            except asyncio.TimeoutError:
                raise asyncio.TimeoutError("Загрузка файла заняла слишком долго.")

            if not raw_path.exists() or raw_path.stat().st_size == 0:
                raise ValueError("Не удалось получить файл от Telegram. Попробуй ещё раз.")

            try:
                await self._ffmpeg.to_mp3(str(raw_path), str(audio_path))
            except Exception:
                
                logger.warning("ffmpeg conversion failed, using raw file")
                shutil.copy(raw_path, audio_path)

            raw_path.unlink(missing_ok=True)

        else:
            raise ValueError(
                "Отправь аудио файл (mp3, wav, ogg) или ссылку на музыку"
            )

        if not audio_path.exists() or audio_path.stat().st_size == 0:
            raise ValueError("Не удалось обработать аудио файл. Попробуй другой.")

        logger.info(f"Audio ready: {audio_path} ({audio_path.stat().st_size // 1024} KB)")
        return audio_path


    async def acquire_video(self, msg, idx: int, current_total_duration: float = 0.0) -> Path:
        video_path = self._tmp_dir / f"video_{idx}.mp4"

        file_obj = None
        if msg.video:
            file_obj = msg.video
        elif msg.document and msg.document.mime_type in SUPPORTED_VIDEO_MIME:
            file_obj = msg.document
        elif msg.document:
            fname = getattr(msg.document, "file_name", "") or ""
            if any(fname.lower().endswith(ext) for ext in (".mp4", ".mov", ".mkv", ".avi", ".webm")):
                file_obj = msg.document

        if not file_obj:
            raise ValueError(
                "Неподдерживаемый формат.\n"
                "Поддерживаются: mp4, mov, mkv, avi, webm"
            )

        _check_size(file_obj)
        await msg.reply_text(f"⏬ Получаю видео #{idx + 1}...")

        raw_path = self._tmp_dir / f"video_raw_{idx}"
        try:
            await _download_tg_file(file_obj, raw_path)
        except asyncio.TimeoutError:
            raise asyncio.TimeoutError("Загрузка видео заняла слишком долго. Попробуй файл поменьше.")

        if not raw_path.exists() or raw_path.stat().st_size == 0:
            raise ValueError("Не удалось получить файл от Telegram. Попробуй ещё раз.")

        try:
            await self._ffmpeg.to_mp4(str(raw_path), str(video_path))
        except Exception:
            logger.warning(f"ffmpeg video conversion failed for video_{idx}, using raw")
            shutil.copy(raw_path, video_path)

        raw_path.unlink(missing_ok=True)

        if not video_path.exists() or video_path.stat().st_size == 0:
            raise ValueError("Не удалось обработать видео файл. Попробуй другой.")

        duration = await get_video_duration(video_path)
        new_total = current_total_duration + duration
        if new_total > MAX_TOTAL_DURATION_SEC:
            video_path.unlink(missing_ok=True)
            current_min = int(current_total_duration) // 60
            current_sec = int(current_total_duration) % 60
            clip_min = int(duration) // 60
            clip_sec = int(duration) % 60
            raise ValueError(
                f"Суммарная длительность видео превысит лимит 10 минут.\n"
                f"Уже добавлено: {current_min}м {current_sec}с, "
                f"этот клип: {clip_min}м {clip_sec}с.\n"
                f"Отправь видео покороче или начни обработку с /done."
            )

        logger.info(f"Video ready: {video_path} ({video_path.stat().st_size // (1024*1024)} MB, {duration:.1f}s)")
        return video_path, duration