import asyncio
import logging
from pathlib import Path

from editor import VideoPipeline
from tools import PromptConfigGenerator

logger = logging.getLogger(__name__)


class PipelineService:
    def __init__(
        self,
        video_files: list[str],
        audio_path: Path,
        prompt_text: str,
        tmp_dir: Path,
        output_path: Path,
    ):
        self._video_files = video_files
        self._audio_path = audio_path
        self._prompt_text = prompt_text
        self._tmp_dir = tmp_dir
        self._output_path = output_path

    def _run_blocking(self) -> None:
        import shutil

        input_dir = Path("input")
        input_dir.mkdir(parents=True, exist_ok=True)

        audio_path = input_dir / "m1.mp3"
        shutil.copy(str(self._audio_path), str(audio_path))

        pipeline_output = self._tmp_dir / "pipeline.mp4"

        logger.info(f"[Pipeline] Generating prompt config")
        PromptConfigGenerator().generate(self._prompt_text)

        logger.info(f"[Pipeline] Running video pipeline with {len(self._video_files)} video(s)")
        VideoPipeline(
            input_video=self._video_files,
            output_video=str(pipeline_output),
            music_file=str(audio_path),
            bpm=120,
            beats_per_clip=16,
        ).run()

        if not pipeline_output.exists() or pipeline_output.stat().st_size == 0:
            raise RuntimeError("Ошибка в pipeline — выходной файл не создан.")

        self._output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(pipeline_output), str(self._output_path))

        # Чистим промежуточные файлы
        for path in (audio_path,):
            if path.exists():
                path.unlink(missing_ok=True)

        if not self._output_path.exists() or self._output_path.stat().st_size == 0:
            raise RuntimeError("Финальный файл не был создан. Проверь исходные видео и аудио.")

        logger.info(f"[Pipeline] Done → {self._output_path} ({self._output_path.stat().st_size // (1024*1024)} MB)")

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._run_blocking)