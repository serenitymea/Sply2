import asyncio
import logging
import os
import signal
import subprocess
import sys
from pathlib import Path

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
        effects: bool = True,
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
        self._process: asyncio.subprocess.Process | None = None

    def _build_cmd(self) -> list[str]:
        cmd = [
            sys.executable,
            "-m",
            "clipmaker.main",
            *self._video_files,
            "--music",
            str(self._audio_path),
            "--output",
            str(self._output_path),
            "--fps",
            str(self._fps),
            "--speed",
            str(self._speed),
            "--sample-fps",
            str(self._sample_fps),
        ]
        if self._resolution:
            cmd.extend(["--resolution", self._resolution])
        if self._max_clips is not None:
            cmd.extend(["--max-clips", str(self._max_clips)])
        if self._effects:
            cmd.append("--effects")
        return cmd

    async def _terminate_process_tree(self) -> None:
        process = self._process
        if not process or process.returncode is not None:
            return

        try:
            if os.name == "nt":
                kill_proc = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(kill_proc.communicate(), timeout=10)
            else:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), timeout=10)
                except asyncio.TimeoutError:
                    os.killpg(process.pid, signal.SIGKILL)
                    await asyncio.wait_for(process.wait(), timeout=10)
        except ProcessLookupError:
            pass
        except Exception as e:
            logger.warning("[Pipeline] Failed to terminate process tree: %s", e)
            process.kill()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except Exception:
                pass

    async def run(self) -> None:
        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        self._output_path.parent.mkdir(parents=True, exist_ok=True)
        self._output_path.unlink(missing_ok=True)

        cmd = self._build_cmd()
        logger.info("[Pipeline] Starting subprocess: %s", " ".join(cmd))

        creationflags = 0
        start_new_session = False
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            start_new_session = True

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )

        try:
            stdout, stderr = await self._process.communicate()
        except asyncio.CancelledError:
            logger.info("[Pipeline] Cancel requested, terminating subprocess")
            await self._terminate_process_tree()
            raise

        if self._process.returncode != 0:
            err_text = stderr.decode(errors="replace").strip()
            out_text = stdout.decode(errors="replace").strip()
            details = err_text or out_text or "clipmaker exited with an unknown error"
            short_details = "\n".join([line for line in details.splitlines() if line.strip()][-10:])
            raise RuntimeError(f"Pipeline failed:\n{short_details}")

        if not self._output_path.exists() or self._output_path.stat().st_size == 0:
            raise RuntimeError("Pipeline finished but the output file was not created.")
