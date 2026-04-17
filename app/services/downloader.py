import asyncio
import ipaddress
import logging
import os
import re
import socket
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp

logger = logging.getLogger(__name__)

DOWNLOAD_TIMEOUT = 300
ALLOWED_URL_HOSTS = {
    "tiktok.com",
    "www.tiktok.com",
    "m.tiktok.com",
    "vm.tiktok.com",
    "vt.tiktok.com",
}

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')

_ERROR_MAP = [
    (["private", "members only"], "Видео приватное или только для подписчиков."),
    (["unavailable", "not available"], "Видео недоступно или удалено."),
    (["copyright"],                    "Видео заблокировано по авторским правам."),
    (["sign in", "login"],             "Для этого видео требуется авторизация. Попробуй другую ссылку."),
    (["no video formats", "no media"], "Не удалось найти медиафайл по этой ссылке."),
    (["permission denied"],            "Ошибка доступа на сервере. Попробуй позже."),
]

_DEFAULT_ERROR = "Не удалось скачать. Попробуй другую ссылку или отправь файл напрямую."


def _clean(text: str) -> str:
    text = _ANSI_RE.sub('', text)
    text = re.sub(r'^(ERROR|WARNING):\s*', '', text, flags=re.IGNORECASE).strip()
    return text


def _classify_error(err: str) -> str:
    for keywords, message in _ERROR_MAP:
        if any(k in err for k in keywords):
            return message
    return _DEFAULT_ERROR


class _YtdlpLogger:
    def warning(self, msg): logger.debug("[yt-dlp] %s", msg)
    def error(self, msg):   logger.error("[yt-dlp] %s", msg)
    def debug(self, msg):   pass


class MediaDownloader:
    def __init__(self, output_dir: Path):
        self._output_dir = output_dir
        self._output_dir.mkdir(parents=True, exist_ok=True)

    # Options builders

    def _base_opts(self) -> dict:
        return {
            "quiet": True,
            "no_warnings": True,
            "logger": _YtdlpLogger(),
            "paths": {"home": str(self._output_dir), "temp": str(self._output_dir)},
            "outtmpl": {"default": "%(id)s.%(ext)s"},
            "restrictfilenames": False,
            "noplaylist": True,
            "ignoreerrors": False,
            "nopart": True,
            "overwrites": True,
            "socket_timeout": 30,
            "http_headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            },
        }

    @staticmethod
    def _audio_opts() -> dict:
        return {
            # TikTok often exposes only muxed formats, so we fall back to `best`
            # and let FFmpegExtractAudio pull out the audio track.
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "0",
            }],
        }

    @staticmethod
    def _extractor_opts() -> dict:
        return {
            "extractor_args": {
                "tiktok": {"webpage_download": ["1"]},
            },
        }

    def _build_opts(self) -> dict:
        opts = {
            **self._base_opts(),
            **self._audio_opts(),
            **self._extractor_opts(),
        }
        if proxy := os.environ.get("YT_DLP_PROXY", ""):
            opts["proxy"] = proxy
        if cookie_file := self._find_cookies():
            opts["cookiefile"] = cookie_file
        return opts

    @staticmethod
    def _validate_url(url: str) -> str:
        normalized = url.strip()
        if normalized.lower().startswith("www."):
            normalized = f"https://{normalized}"

        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("Поддерживаются только http/https ссылки на TikTok.")

        host = (parsed.hostname or "").rstrip(".").lower()
        if not host:
            raise ValueError("Не удалось распознать адрес ссылки.")

        if host not in ALLOWED_URL_HOSTS:
            raise ValueError("Сейчас поддерживаются только ссылки TikTok.")

        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None

        if ip is not None:
            raise ValueError("Ссылки по прямому IP-адресу запрещены.")

        try:
            addrinfo = socket.getaddrinfo(host, None)
        except socket.gaierror:
            raise ValueError("Не удалось проверить адрес ссылки. Попробуй другую ссылку.")

        for entry in addrinfo:
            resolved_ip = ipaddress.ip_address(entry[4][0])
            if (
                resolved_ip.is_private
                or resolved_ip.is_loopback
                or resolved_ip.is_link_local
                or resolved_ip.is_multicast
                or resolved_ip.is_reserved
                or resolved_ip.is_unspecified
            ):
                raise ValueError("Небезопасный адрес ссылки заблокирован.")

        return normalized

    # ------------------------------------------------------------------
    # Cookie discovery
    # ------------------------------------------------------------------

    @staticmethod
    def _find_cookies() -> str | None:
        candidates = [
            os.environ.get("YT_DLP_COOKIES", ""),
            Path.home() / ".config" / "yt-dlp" / "cookies.txt",
            Path("/etc/yt-dlp/cookies.txt"),
        ]
        for candidate in candidates:
            if not candidate:
                continue
            p = Path(str(candidate))
            if p.exists() and p.stat().st_size > 0:
                return str(p)
        return None

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    def _resolve_filename(self, info: dict, ydl: yt_dlp.YoutubeDL) -> Path:
        """Return the path of the downloaded mp3, falling back to the newest file."""
        filename = Path(ydl.prepare_filename(info)).with_suffix(".mp3")
        if filename.exists():
            return filename

        files = list(self._output_dir.glob("*.mp3"))
        if not files:
            raise ValueError("Файл не был создан. Попробуй другую ссылку.")
        return max(files, key=lambda p: p.stat().st_mtime)

    def _download_blocking(self, url: str) -> Path:
        try:
            safe_url = self._validate_url(url)
            with yt_dlp.YoutubeDL(self._build_opts()) as ydl:
                info = ydl.extract_info(safe_url, download=True)
                if not info:
                    raise RuntimeError("empty info")
                filename = self._resolve_filename(info, ydl)

        except yt_dlp.utils.DownloadError as e:
            raise ValueError(_classify_error(_clean(str(e)).lower()))

        except Exception:
            raise ValueError(_DEFAULT_ERROR)

        if filename.stat().st_size == 0:
            filename.unlink(missing_ok=True)
            raise ValueError("Скачанный файл оказался пустым. Попробуй другую ссылку.")

        logger.info("Downloaded: %s (%d KB)", filename, filename.stat().st_size // 1024)
        return filename

    async def download(self, url: str) -> Path:
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, self._download_blocking, url),
                timeout=DOWNLOAD_TIMEOUT,
            )
        except asyncio.TimeoutError:
            raise asyncio.TimeoutError()
