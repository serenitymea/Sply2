import asyncio
import ipaddress
import logging
import os
import re
import socket
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

import yt_dlp

logger = logging.getLogger(__name__)

DOWNLOAD_TIMEOUT = 300
ALLOWED_URL_ROOT = "tiktok.com"
_TIKTOK_MUSIC_PATH_RE = re.compile(r"^/music/[\w\.-]+-\d+/?$", re.IGNORECASE)
_TIKTOK_VIDEO_PATH_RE = re.compile(r"^/@[^/]+/video/\d+/?$", re.IGNORECASE)
_TIKTOK_SHORTLINK_HOSTS = {"vt.tiktok.com", "vm.tiktok.com"}

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')

_ERROR_MAP = [
    (["private", "members only"], "The video is private or subscribers-only."),
    (["unavailable", "not available"], "The video is unavailable or has been removed."),
    (["copyright"],                    "The video is blocked due to copyright."),
    (["sign in", "login"],             "This video requires authorization. Try another link."),
    (["no video formats", "no media"], "Could not find a media file for this link."),
    (["permission denied"],            "Server access error. Try again later."),
]

_DEFAULT_ERROR = "Could not download it. Try another link or send the file directly."


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
    def debug(self, msg):   logger.debug("[yt-dlp] %s", msg)


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
            # Prefer TikTok's dedicated music track when it is exposed.
            # If it is unavailable, fall back to the best available audio.
            "format": "bestaudio[format_note*=Music]/bestaudio/best",
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

    def _apply_runtime_overrides(self, opts: dict) -> dict:
        merged = dict(opts)
        if proxy := os.environ.get("YT_DLP_PROXY", ""):
            merged["proxy"] = proxy
        if cookie_file := self._find_cookies():
            merged["cookiefile"] = cookie_file
        return merged

    def _build_probe_opts(self) -> dict:
        return self._apply_runtime_overrides({
            **self._base_opts(),
            **self._extractor_opts(),
            "skip_download": True,
            "noplaylist": False,
            "extract_flat": True,
        })

    def _build_opts(self) -> dict:
        return self._apply_runtime_overrides({
            **self._base_opts(),
            **self._audio_opts(),
            **self._extractor_opts(),
        })

    def _build_music_page_opts(self) -> dict:
        return {
            **self._build_opts(),
            "noplaylist": False,
            "playlist_items": "1",
            "lazy_playlist": True,
        }

    @staticmethod
    def _validate_url(url: str) -> str:
        normalized = url.strip()
        if normalized.lower().startswith("www."):
            normalized = f"https://{normalized}"

        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("Only http/https TikTok links are supported.")

        host = (parsed.hostname or "").rstrip(".").lower()
        if not host:
            raise ValueError("Could not recognize the link address.")

        if host != ALLOWED_URL_ROOT and not host.endswith(f".{ALLOWED_URL_ROOT}"):
            raise ValueError("Only TikTok links are supported right now.")

        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None

        if ip is not None:
            raise ValueError("Links with direct IP addresses are not allowed.")

        try:
            addrinfo = socket.getaddrinfo(host, None)
        except socket.gaierror:
            raise ValueError("Could not verify the link address. Try another link.")

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
                raise ValueError("Unsafe link address blocked.")

        return normalized

    @staticmethod
    def _is_music_url(url: str) -> bool:
        parsed = urlparse(url)
        return bool(parsed.hostname) and _TIKTOK_MUSIC_PATH_RE.match(parsed.path or "") is not None

    @staticmethod
    def _is_video_url(url: str) -> bool:
        parsed = urlparse(url)
        return bool(parsed.hostname) and _TIKTOK_VIDEO_PATH_RE.match(parsed.path or "") is not None

    @staticmethod
    def _needs_resolution(url: str) -> bool:
        parsed = urlparse(url)
        return (
            parsed.hostname in _TIKTOK_SHORTLINK_HOSTS
            or MediaDownloader._is_music_url(url)
            or not MediaDownloader._is_video_url(url)
        )

    @staticmethod
    def _pick_entry_url(info: dict) -> str | None:
        candidates = [
            info.get("webpage_url"),
            info.get("original_url"),
            info.get("url") if isinstance(info.get("url"), str) and info.get("url", "").startswith(("http://", "https://")) else None,
        ]
        for candidate in candidates:
            if candidate:
                return candidate
        return None

    def _probe_info(self, url: str) -> dict:
        with yt_dlp.YoutubeDL(self._build_probe_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            raise ValueError("Could not get TikTok data from the link.")
        return info

    def _expand_short_url(self, url: str) -> str:
        parsed = urlparse(url)
        if parsed.hostname not in _TIKTOK_SHORTLINK_HOSTS:
            return url

        headers = self._base_opts()["http_headers"]
        handlers = [HTTPRedirectHandler()]
        if proxy := os.environ.get("YT_DLP_PROXY", ""):
            handlers.append(ProxyHandler({"http": proxy, "https": proxy}))
        opener = build_opener(*handlers)

        for method in ("HEAD", "GET"):
            try:
                request = Request(url, headers=headers, method=method)
                with opener.open(request, timeout=15) as response:
                    final_url = response.geturl() or url
                if final_url and final_url != url:
                    logger.info("Expanded TikTok short URL: %s -> %s", url, final_url)
                    return final_url
            except Exception as exc:
                logger.debug("Failed to expand short TikTok URL via %s: %s", method, exc)

        return url

    @staticmethod
    def _select_download_target(info: dict) -> dict:
        requested = info.get("requested_downloads") or []
        for item in requested:
            if item:
                return item

        entries = info.get("entries") or []
        for entry in entries:
            if entry:
                nested_requested = entry.get("requested_downloads") or []
                for item in nested_requested:
                    if item:
                        return item
                return entry

        return info

    def _resolve_music_target_url(self, url: str, max_depth: int = 3) -> str:
        if max_depth <= 0:
            raise ValueError("Could not determine the direct TikTok track link.")

        info = self._probe_info(url)

        direct_url = self._pick_entry_url(info)
        if direct_url:
            if self._needs_resolution(direct_url) and direct_url != url:
                return self._resolve_music_target_url(direct_url, max_depth=max_depth - 1)
            return direct_url

        entries = info.get("entries") or []
        for entry in entries:
            if not entry:
                continue
            target_url = self._pick_entry_url(entry)
            if not target_url:
                continue
            if self._needs_resolution(target_url) and target_url != url:
                return self._resolve_music_target_url(target_url, max_depth=max_depth - 1)
            if target_url:
                return target_url

        raise ValueError("Could not find a video for this TikTok track.")

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
        target_info = self._select_download_target(info)
        filename = Path(ydl.prepare_filename(target_info)).with_suffix(".mp3")
        if filename.exists():
            return filename

        files = list(self._output_dir.glob("*.mp3"))
        if not files:
            raise ValueError("The file was not created. Try another link.")
        return max(files, key=lambda p: p.stat().st_mtime)

    def _download_with_opts(self, url: str, opts: dict) -> tuple[dict, Path]:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                raise RuntimeError("empty info")
            filename = self._resolve_filename(info, ydl)
        return info, filename

    def _download_blocking(self, url: str) -> Path:
        try:
            safe_url = self._validate_url(url)
            safe_url = self._expand_short_url(safe_url)
            safe_url = self._validate_url(safe_url)
            if self._is_music_url(safe_url):
                try:
                    _, filename = self._download_with_opts(safe_url, self._build_music_page_opts())
                except (yt_dlp.utils.DownloadError, ValueError, RuntimeError):
                    resolved_url = self._resolve_music_target_url(safe_url)
                    _, filename = self._download_with_opts(resolved_url, self._build_opts())
            else:
                if self._needs_resolution(safe_url):
                    safe_url = self._resolve_music_target_url(safe_url)
                _, filename = self._download_with_opts(safe_url, self._build_opts())

        except yt_dlp.utils.DownloadError as e:
            raw_error = _clean(str(e))
            logger.error("yt-dlp download failed for %s: %s", url, raw_error)
            raise ValueError(_classify_error(raw_error.lower()))

        except ValueError:
            raise

        except Exception as e:
            logger.exception("Unexpected downloader error for %s: %s", url, e)
            raise ValueError(_DEFAULT_ERROR)

        if filename.stat().st_size == 0:
            filename.unlink(missing_ok=True)
            raise ValueError("The downloaded file is empty. Try another link.")

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
