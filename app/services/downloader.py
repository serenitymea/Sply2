import asyncio
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

import yt_dlp

logger = logging.getLogger(__name__)

DOWNLOAD_TIMEOUT = 300

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')


def _clean(text: str) -> str:

    text = _ANSI_RE.sub('', text)
    text = re.sub(r'^(ERROR|WARNING):\s*', '', text, flags=re.IGNORECASE).strip()
    return text


class _SilentLogger:
    def warning(self, msg): logger.debug(f"[yt-dlp] {msg}")
    def error(self, msg): logger.error(f"[yt-dlp] {msg}")
    def debug(self, msg): pass


class MediaDownloader:
    def __init__(self, output_dir: Path):
        self._output_dir = output_dir
        self._output_dir.mkdir(parents=True, exist_ok=True)

    def _build_opts(self) -> dict:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "logger": _SilentLogger(),
            "paths": {
                "home": str(self._output_dir),
                "temp": str(self._output_dir),
            },
            "outtmpl": {"default": "%(id)s.%(ext)s"},
            "restrictfilenames": False,
            "noplaylist": True,
            "ignoreerrors": False,
            "continuedl": False,
            "nopart": True,
            "overwrites": True,
            "retries": 10,
            "fragment_retries": 10,
            "retry_sleep": 5,
            "socket_timeout": 30,
            "concurrent_fragment_downloads": 1,
            "http_headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.tiktok.com/",
            },
            "geo_bypass": True,
            "geo_bypass_country": "US",
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "0",
            }],
            "keepvideo": False,
            "extractor_args": {
                "tiktok": {"webpage_download": ["1"]},
            },
        }

        proxy = os.environ.get("YT_DLP_PROXY", "")
        if proxy:
            opts["proxy"] = proxy

        # Cookies
        for candidate in [
            os.environ.get("YT_DLP_COOKIES", ""),
            Path.home() / ".config" / "yt-dlp" / "cookies.txt",
            Path("/etc/yt-dlp/cookies.txt"),
        ]:
            if not candidate:
                continue
            p = Path(str(candidate))
            if p.exists() and p.stat().st_size > 0:
                opts["cookiefile"] = str(p)
                break

        return opts

    @staticmethod
    def _kill_external() -> None:
        try:
            if sys.platform.startswith("win"):
                for proc in ("ffmpeg.exe", "yt-dlp.exe"):
                    subprocess.run(["taskkill", "/F", "/IM", proc],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                for proc in ("ffmpeg", "yt-dlp"):
                    subprocess.run(["pkill", "-f", proc],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def _download_blocking(self, url: str) -> Path:
        try:
            with yt_dlp.YoutubeDL(self._build_opts()) as ydl:
                info = ydl.extract_info(url, download=True)
                if not info:
                    raise RuntimeError("empty info")
                filename = Path(ydl.prepare_filename(info)).with_suffix(".mp3")

        except yt_dlp.utils.DownloadError as e:
            self._kill_external()
            err = _clean(str(e)).lower()

            if "private" in err or "members only" in err:
                raise ValueError("Видео приватное или только для подписчиков.")
            if "unavailable" in err or "not available" in err:
                raise ValueError("Видео недоступно или удалено.")
            if "copyright" in err:
                raise ValueError("Видео заблокировано по авторским правам.")
            if "sign in" in err or "login" in err:
                raise ValueError("Для этого видео требуется авторизация. Попробуй другую ссылку.")
            if "no video formats" in err or "no media" in err:
                raise ValueError("Не удалось найти медиафайл по этой ссылке.")
            if "permission denied" in err:
                raise ValueError("Ошибка доступа на сервере. Попробуй позже.")

            raise ValueError("Не удалось скачать. Попробуй другую ссылку или отправь файл напрямую.")

        except Exception as e:
            self._kill_external()
            raise ValueError("Не удалось скачать. Попробуй другую ссылку или отправь файл напрямую.")

        if not filename.exists():
            files = list(self._output_dir.glob("*.mp3"))
            if not files:
                raise ValueError("Файл не был создан. Попробуй другую ссылку.")
            filename = max(files, key=lambda p: p.stat().st_mtime)

        if filename.stat().st_size == 0:
            filename.unlink(missing_ok=True)
            raise ValueError("Скачанный файл оказался пустым. Попробуй другую ссылку.")

        logger.info(f"Downloaded: {filename} ({filename.stat().st_size // 1024} KB)")
        return filename

    async def download(self, url: str) -> Path:
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(None, self._download_blocking, url)
        try:
            return await asyncio.wait_for(future, timeout=DOWNLOAD_TIMEOUT)
        except asyncio.TimeoutError:
            self._kill_external()
            raise asyncio.TimeoutError()
        except asyncio.CancelledError:
            self._kill_external()
            raise
        except Exception:
            self._kill_external()
            raise