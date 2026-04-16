import asyncio
import logging

logger = logging.getLogger(__name__)

# Таймаут на одну ffmpeg-команду (секунды)
FFMPEG_TIMEOUT = 300


class FFmpegService:
    @staticmethod
    async def run(*args: str) -> None:
        cmd = ["ffmpeg", "-y", *args]
        logger.debug(f"ffmpeg: {' '.join(cmd)}")

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=FFMPEG_TIMEOUT,
            )
        except asyncio.TimeoutError:
            try:
                process.kill()
            except Exception:
                pass
            raise RuntimeError(f"ffmpeg завис (>{FFMPEG_TIMEOUT}с). Попробуй файл поменьше.")

        if process.returncode != 0:
            err_text = stderr.decode(errors="replace").strip()
            # Показываем только последние 3 строки ошибки
            lines = [l for l in err_text.splitlines() if l.strip()]
            short_err = "\n".join(lines[-3:]) if lines else "неизвестная ошибка"
            raise RuntimeError(f"ffmpeg завершился с ошибкой:\n{short_err}")

    async def to_mp3(self, input_path: str, output_path: str) -> None:
        await self.run(
            "-i", input_path,
            "-vn",            # только аудио
            "-q:a", "0",      # лучшее качество
            output_path,
        )

    async def to_mp4(self, input_path: str, output_path: str) -> None:
        await self.run(
            "-i", input_path,
            "-c", "copy",     # без перекодирования
            "-movflags", "+faststart",
            output_path,
        )