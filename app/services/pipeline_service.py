import asyncio
import logging
import shutil
from pathlib import Path

from clipmaker.analyzer import analyze_audio, analyze_video
from clipmaker.selector import select_clips
from clipmaker.renderer import render

logger = logging.getLogger(__name__)


class PipelineService:
    def __init__(
        self,
        video_files: list[str],
        audio_path: Path,
        tmp_dir: Path,
        output_path: Path,
        fps: int = 30,
        speed: float = 1.0,
        resolution: str | None = None,
        max_clips: int | None = None,
        effects: bool = False,
        sample_fps: float = 4.0,
    ):
        self._video_files = video_files
        self._audio_path = audio_path
        self._tmp_dir = tmp_dir
        self._output_path = output_path
        self._fps = fps
        self._speed = speed
        self._resolution = resolution
        self._max_clips = max_clips
        self._effects = effects
        self._sample_fps = sample_fps

    def _select_clips(self, audio):
        if len(self._video_files) == 1:
            video = analyze_video(self._video_files[0], sample_fps=self._sample_fps)
            clips = select_clips(video, audio, max_clips=self._max_clips)
            return clips, None
        else:
            return self._run_multi_video(audio)

    def _run_multi_video(self, audio):
        n = len(self._video_files)
        per_video = (self._max_clips or 40) // n + 1

        all_clips: list = []
        all_sources: list[str] = []

        for i, vpath in enumerate(self._video_files):
            logger.info(f"[Pipeline] Video {i+1}/{n}: {vpath}")
            video = analyze_video(vpath, sample_fps=self._sample_fps)
            clips = select_clips(video, audio, max_clips=per_video)
            all_clips.extend(clips)
            all_sources.extend([vpath] * len(clips))

        merged_clips: list = []
        merged_sources: list[str] = []
        groups = [
            [(c, s) for c, s in zip(all_clips, all_sources) if s == vp]
            for vp in self._video_files
        ]
        iters = [iter(g) for g in groups]
        active = list(range(n))
        while active:
            next_active = []
            for i in active:
                try:
                    c, s = next(iters[i])
                    merged_clips.append(c)
                    merged_sources.append(s)
                    next_active.append(i)
                except StopIteration:
                    pass
            active = next_active

        cap = self._max_clips or len(merged_clips)
        return merged_clips[:cap], merged_sources[:cap]

    def _run_blocking(self) -> None:
        input_dir = Path("input")
        input_dir.mkdir(parents=True, exist_ok=True)

        audio_path = input_dir / "m1.mp3"
        shutil.copy(str(self._audio_path), str(audio_path))

        pipeline_output = self._tmp_dir / "pipeline.mp4"

        logger.info("[Pipeline] Analyzing audio & video")
        audio = analyze_audio(str(audio_path))
        clips, clip_sources = self._select_clips(audio)

        logger.info(f"[Pipeline] Rendering {len(clips)} clip(s)")
        render(
            clips=clips,
            video_source=self._video_files,
            music_path=str(audio_path),
            output_path=str(pipeline_output),
            fps=self._fps,
            speed=self._speed,
            resolution=self._resolution,
            effects=self._effects,
            clip_sources=clip_sources,
        )

        if not pipeline_output.exists() or pipeline_output.stat().st_size == 0:
            raise RuntimeError("Ошибка в pipeline — выходной файл не создан.")

        self._output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(pipeline_output), str(self._output_path))

        for path in (audio_path,):
            if path.exists():
                path.unlink(missing_ok=True)

        if not self._output_path.exists() or self._output_path.stat().st_size == 0:
            raise RuntimeError("Финальный файл не был создан. Проверь исходные видео и аудио.")

        logger.info(
            f"[Pipeline] Done → {self._output_path} "
            f"({self._output_path.stat().st_size // (1024 * 1024)} MB)"
        )

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._run_blocking)