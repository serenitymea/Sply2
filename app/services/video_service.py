import asyncio
import logging
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from ..runtime_control import RuntimeControl
from .downloader import MediaDownloader
from .ffmpeg_service import FFmpegService

logger = logging.getLogger(__name__)

MAX_FILE_SIZE_BYTES = 20 * 1024 * 1024
MAX_VIDEOS = 5
MAX_TOTAL_DURATION_SEC = 10 * 60
MAX_PREPROCESS_JOBS = 2
PREPROCESS_SLOT_TTL_SEC = 10 * 60

SUPPORTED_AUDIO_MIME = {
    "audio/mpeg", "audio/mp3", "audio/ogg", "audio/wav",
    "audio/flac", "audio/mp4", "audio/x-m4a",
}
SUPPORTED_VIDEO_MIME = {
    "video/mp4", "video/quicktime", "video/x-matroska",
    "video/webm", "video/avi", "video/x-msvideo",
}

MIME_SUFFIXES = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/flac": ".flac",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/x-matroska": ".mkv",
    "video/webm": ".webm",
    "video/avi": ".avi",
    "video/x-msvideo": ".avi",
}

TG_DOWNLOAD_TIMEOUT = 120


def _is_url(text: str) -> bool:
    return text.strip().lower().startswith(("http://", "https://", "www."))


def _check_size(file_obj) -> None:
    size = getattr(file_obj, "file_size", None)
    if size and size > MAX_FILE_SIZE_BYTES:
        mb = size // (1024 * 1024)
        raise ValueError(f"Р¤Р°Р№Р» СЃР»РёС€РєРѕРј Р±РѕР»СЊС€РѕР№ ({mb} MB). РњР°РєСЃРёРјСѓРј - 20 MB.")


def _check_downloaded_size(path: Path) -> None:
    size = path.stat().st_size
    if size > MAX_FILE_SIZE_BYTES:
        mb = size // (1024 * 1024)
        path.unlink(missing_ok=True)
        raise ValueError(f"Р В¤Р В°Р в„–Р В» РЎРѓР В»Р С‘РЎв‚¬Р С”Р С•Р С Р В±Р С•Р В»РЎРЉРЎв‚¬Р С•Р в„– ({mb} MB). Р СљР В°Р С”РЎРѓР С‘Р СРЎС“Р С - 20 MB.")


def _guess_suffix(file_obj, default: str) -> str:
    file_name = getattr(file_obj, "file_name", "") or ""
    suffix = Path(file_name).suffix.lower()
    if suffix:
        return suffix

    mime_type = (getattr(file_obj, "mime_type", "") or "").lower()
    return MIME_SUFFIXES.get(mime_type, default)


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
            raise RuntimeError("РќРµ СѓРґР°Р»РѕСЃСЊ РѕРїСЂРµРґРµР»РёС‚СЊ РґР»РёС‚РµР»СЊРЅРѕСЃС‚СЊ РІРёРґРµРѕ. РџРѕРїСЂРѕР±СѓР№ РґСЂСѓРіРѕР№ С„Р°Р№Р».")
        return float(raw)
    except (asyncio.TimeoutError, FileNotFoundError) as e:
        logger.error(f"ffprobe failed for {video_path}: {e}")
        raise RuntimeError("РќРµ СѓРґР°Р»РѕСЃСЊ РїСЂРѕРІРµСЂРёС‚СЊ РґР»РёС‚РµР»СЊРЅРѕСЃС‚СЊ РІРёРґРµРѕ. РЈР±РµРґРёСЃСЊ, С‡С‚Рѕ ffprobe СѓСЃС‚Р°РЅРѕРІР»РµРЅ.")
    except RuntimeError:
        raise
    except Exception as e:
        logger.error(f"ffprobe unexpected error for {video_path}: {e}")
        raise RuntimeError("РќРµ СѓРґР°Р»РѕСЃСЊ РѕРїСЂРµРґРµР»РёС‚СЊ РґР»РёС‚РµР»СЊРЅРѕСЃС‚СЊ РІРёРґРµРѕ. РџРѕРїСЂРѕР±СѓР№ РґСЂСѓРіРѕР№ С„Р°Р№Р».")


class VideoService:
    def __init__(self, tmp_dir: Path, user_id: int):
        self._tmp_dir = tmp_dir
        self._user_id = user_id
        self._ffmpeg = FFmpegService()
        self._downloader = MediaDownloader(tmp_dir)
        self._runtime = RuntimeControl()

    @asynccontextmanager
    async def _preprocess_slot(self):
        token = await self._runtime.acquire_preprocess_slot(
            self._user_id,
            MAX_PREPROCESS_JOBS,
            PREPROCESS_SLOT_TTL_SEC,
        )
        try:
            yield
        finally:
            self._runtime.release_preprocess_slot(token)

    async def acquire_audio(self, msg) -> Path:
        audio_path = self._tmp_dir / "m1.mp3"

        if msg.text and _is_url(msg.text):
            await msg.reply_text("вЏ¬ РЎРєР°С‡РёРІР°СЋ Р°СѓРґРёРѕ РїРѕ СЃСЃС‹Р»РєРµ... Р­С‚Рѕ РјРѕР¶РµС‚ Р·Р°РЅСЏС‚СЊ РґРѕ РЅРµСЃРєРѕР»СЊРєРёС… РјРёРЅСѓС‚.")
            async with self._preprocess_slot():
                downloaded = await self._downloader.download(msg.text.strip())
                _check_downloaded_size(downloaded)
                audio_path.unlink(missing_ok=True)
                shutil.copy(downloaded, audio_path)
                final_path = audio_path
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
                    "РќРµРїРѕРґРґРµСЂР¶РёРІР°РµРјС‹Р№ С„РѕСЂРјР°С‚ РґРѕРєСѓРјРµРЅС‚Р°.\n"
                    "РџРѕРґРґРµСЂР¶РёРІР°СЋС‚СЃСЏ: mp3, wav, ogg, flac, m4a\n"
                )

            _check_size(file_obj)
            await msg.reply_text("вЏ¬ РџРѕР»СѓС‡Р°СЋ Р°СѓРґРёРѕ С„Р°Р№Р»...")

            raw_path = self._tmp_dir / f"audio_raw{_guess_suffix(file_obj, '.bin')}"
            raw_path.unlink(missing_ok=True)
            audio_path.unlink(missing_ok=True)

            async with self._preprocess_slot():
                try:
                    await _download_tg_file(file_obj, raw_path)
                except asyncio.TimeoutError:
                    raise asyncio.TimeoutError("Р—Р°РіСЂСѓР·РєР° С„Р°Р№Р»Р° Р·Р°РЅСЏР»Р° СЃР»РёС€РєРѕРј РґРѕР»РіРѕ.")

                if not raw_path.exists() or raw_path.stat().st_size == 0:
                    raise ValueError("РќРµ СѓРґР°Р»РѕСЃСЊ РїРѕР»СѓС‡РёС‚СЊ С„Р°Р№Р» РѕС‚ Telegram. РџРѕРїСЂРѕР±СѓР№ РµС‰С‘ СЂР°Р·.")

                _check_downloaded_size(raw_path)

                try:
                    await self._ffmpeg.to_mp3(str(raw_path), str(audio_path))
                    raw_path.unlink(missing_ok=True)
                    final_path = audio_path
                except Exception as e:
                    logger.warning("ffmpeg audio conversion failed, using original file: %s", e)
                    final_path = raw_path
        else:
            raise ValueError(
                "РћС‚РїСЂР°РІСЊ Р°СѓРґРёРѕ С„Р°Р№Р» (mp3, wav, ogg) РёР»Рё СЃСЃС‹Р»РєСѓ РЅР° РјСѓР·С‹РєСѓ"
            )

        if not final_path.exists() or final_path.stat().st_size == 0:
            raise ValueError("РќРµ СѓРґР°Р»РѕСЃСЊ РѕР±СЂР°Р±РѕС‚Р°С‚СЊ Р°СѓРґРёРѕ С„Р°Р№Р». РџРѕРїСЂРѕР±СѓР№ РґСЂСѓРіРѕР№.")

        logger.info(f"Audio ready: {final_path} ({final_path.stat().st_size // 1024} KB)")
        return final_path

    async def acquire_video(self, msg, idx: int, current_total_duration: float = 0.0) -> tuple[Path, float]:
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
                "РќРµРїРѕРґРґРµСЂР¶РёРІР°РµРјС‹Р№ С„РѕСЂРјР°С‚.\n"
                "РџРѕРґРґРµСЂР¶РёРІР°СЋС‚СЃСЏ: mp4, mov, mkv, avi, webm"
            )

        _check_size(file_obj)
        await msg.reply_text(f"вЏ¬ РџРѕР»СѓС‡Р°СЋ РІРёРґРµРѕ #{idx + 1}...")

        raw_path = self._tmp_dir / f"video_raw_{idx}{_guess_suffix(file_obj, '.bin')}"
        raw_path.unlink(missing_ok=True)
        video_path.unlink(missing_ok=True)

        async with self._preprocess_slot():
            try:
                await _download_tg_file(file_obj, raw_path)
            except asyncio.TimeoutError:
                raise asyncio.TimeoutError("Р—Р°РіСЂСѓР·РєР° РІРёРґРµРѕ Р·Р°РЅСЏР»Р° СЃР»РёС€РєРѕРј РґРѕР»РіРѕ. РџРѕРїСЂРѕР±СѓР№ С„Р°Р№Р» РїРѕРјРµРЅСЊС€Рµ.")

            if not raw_path.exists() or raw_path.stat().st_size == 0:
                raise ValueError("РќРµ СѓРґР°Р»РѕСЃСЊ РїРѕР»СѓС‡РёС‚СЊ С„Р°Р№Р» РѕС‚ Telegram. РџРѕРїСЂРѕР±СѓР№ РµС‰С‘ СЂР°Р·.")

            _check_downloaded_size(raw_path)

            try:
                await self._ffmpeg.to_mp4(str(raw_path), str(video_path))
                raw_path.unlink(missing_ok=True)
                final_path = video_path
            except Exception as e:
                logger.warning("ffmpeg video conversion failed for video_%s, using original file: %s", idx, e)
                final_path = raw_path

            if not final_path.exists() or final_path.stat().st_size == 0:
                raise ValueError("РќРµ СѓРґР°Р»РѕСЃСЊ РѕР±СЂР°Р±РѕС‚Р°С‚СЊ РІРёРґРµРѕ С„Р°Р№Р». РџРѕРїСЂРѕР±СѓР№ РґСЂСѓРіРѕР№.")

            duration = await get_video_duration(final_path)

        new_total = current_total_duration + duration
        if new_total > MAX_TOTAL_DURATION_SEC:
            final_path.unlink(missing_ok=True)
            current_min = int(current_total_duration) // 60
            current_sec = int(current_total_duration) % 60
            clip_min = int(duration) // 60
            clip_sec = int(duration) % 60
            raise ValueError(
                f"РЎСѓРјРјР°СЂРЅР°СЏ РґР»РёС‚РµР»СЊРЅРѕСЃС‚СЊ РІРёРґРµРѕ РїСЂРµРІС‹СЃРёС‚ Р»РёРјРёС‚ 10 РјРёРЅСѓС‚.\n"
                f"РЈР¶Рµ РґРѕР±Р°РІР»РµРЅРѕ: {current_min}Рј {current_sec}СЃ, "
                f"СЌС‚РѕС‚ РєР»РёРї: {clip_min}Рј {clip_sec}СЃ.\n"
                f"РћС‚РїСЂР°РІСЊ РІРёРґРµРѕ РїРѕРєРѕСЂРѕС‡Рµ РёР»Рё РЅР°С‡РЅРё РѕР±СЂР°Р±РѕС‚РєСѓ СЃ /done."
            )

        logger.info(f"Video ready: {final_path} ({final_path.stat().st_size // (1024 * 1024)} MB, {duration:.1f}s)")
        return final_path, duration
