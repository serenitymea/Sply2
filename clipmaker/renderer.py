"""
renderer.py — builds ffmpeg filter_complex and renders the final video

one or more source video files
basic visual effects (optional)
automatic resolution selection
clip playback speed (speed)
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List, Optional, Union

from .selector import Clip

# Constants

MIN_SPEED = 0.5
MAX_SPEED = 3.0

# Standard resolutions, top-down priority
_RESOLUTIONS = [
    (16 / 9, "1920:1080"),
    (9 / 16, "1080:1920"),
    (1 / 1,  "1080:1080"),
    (4 / 3,  "1440:1080"),
]

# Public API

def render(
    clips: List[Clip],
    video_source: Union[str, List[str]],
    music_path: str,
    output_path: str,
    *,
    fps: int = 30,
    speed: float = 1.0,
    resolution: Optional[str] = None,
    effects: bool = True,
    clip_sources: Optional[List[str]] = None,
) -> str:
    """
    Renders the final video

    Args:
    clips: List of clips from selector.select_clips()
    video_source: Path to video or list of paths
    music_path: Path to audio file
    output_path: Where to save the result
    fps: Output video FPS
    speed: Clip playback speed (1.0 = original)
    resolution: "1920:1080" or None (auto)
    effects: Whether to apply basic color correction
    clip_sources: For multi-video: Path to the source of each clip

    Returns:
    Path to the output file
    """
    if not clips:
        raise ValueError("[renderer] clips list is empty")

    # Normalizing sources
    if isinstance(video_source, str):
        video_paths = [video_source]
    else:
        video_paths = list(video_source)

    if clip_sources is None:
        clip_sources = [video_paths[0]] * len(clips)

    if len(clip_sources) != len(clips):
        raise ValueError(
            f"[renderer] clip_sources ({len(clip_sources)}) != clips ({len(clips)})"
        )

    # Resolution
    if resolution is None:
        resolution = _detect_resolution(video_paths[0])

    speed = max(MIN_SPEED, min(MAX_SPEED, speed))

    # Building a unique mapping source_path - input_index
    path_to_idx: dict[str, int] = {}
    unique_paths: List[str] = []
    for p in clip_sources:
        if p not in path_to_idx:
            path_to_idx[p] = len(unique_paths)
            unique_paths.append(p)

    filters = _build_filters(clips, clip_sources, path_to_idx,
                              resolution, speed, effects)
    music_idx = len(unique_paths)

    cmd = ["ffmpeg", "-y"]
    for p in unique_paths:
        cmd += ["-i", p]
    cmd += ["-i", music_path]
    cmd += [
        "-filter_complex", ";".join(filters),
        "-map", "[outv]",
        "-map", f"{music_idx}:a",
        "-r", str(fps),
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        output_path,
    ]

    print(f"[renderer] {len(clips)} clips, {len(unique_paths)} source(s), {resolution}")
    print(f"[renderer] output: {output_path}")

    subprocess.run(cmd, check=True)
    print("[renderer] done")
    return output_path

# Filter building

def _build_filters(
    clips: List[Clip],
    clip_sources: List[str],
    path_to_idx: dict,
    resolution: str,
    speed: float,
    effects: bool,
) -> List[str]:
    filters = []

    for i, (clip, src) in enumerate(zip(clips, clip_sources)):
        src_idx = path_to_idx[src]
        effect_str = _color_grade() if effects else ""

        f = (
            f"[{src_idx}:v]"
            f"trim=start={clip.start:.3f}:end={clip.end:.3f},"
            f"setpts=PTS-STARTPTS,"
            f"setpts=PTS/{speed:.4f},"
            f"{effect_str}"
            f"scale={resolution},"
            f"setsar=1,"
            f"format=yuv420p"
            f"[v{i}]"
        )
        filters.append(f)

    concat = "".join(f"[v{i}]" for i in range(len(clips)))
    filters.append(f"{concat}concat=n={len(clips)}:v=1:a=0[outv]")

    return filters


def _color_grade() -> str:
    """Light cinematic color correction"""
    return "eq=contrast=1.08:brightness=-0.03:saturation=1.1,"

# Resolution detection

def _detect_resolution(video_path: str) -> str:
    """Determines the resolution of the source and selects the standard output"""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "csv=p=0",
                video_path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        w, h = (int(x) for x in result.stdout.strip().split(","))
        aspect = w / h

        best = min(_RESOLUTIONS, key=lambda r: abs(r[0] - aspect))
        res = best[1]
        print(f"[renderer] source {w}x{h} -> output {res}")
        return res

    except Exception as e:
        print(f"[renderer] resolution detection failed ({e}), using 1920:1080")
        return "1920:1080"
