"""
main.py — entry point

Usage:
python -m clipmaker video.mp4 music.mp3
python -m clipmaker video.mp4 music.mp3 -o result.mp4 --speed 1.1
python -m clipmaker v1.mp4 v2.mp4 --music music.mp3 -o result.mp4
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import List

from .analyzer import analyze_audio, analyze_video
from .selector import select_clips, select_clips_multi
from .renderer import render


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="clipmaker",
        description="Автоматический видеомонтаж под музыку",
    )

    p.add_argument(
        "videos",
        nargs="+",
        metavar="VIDEO",
        help="Один или несколько видеофайлов",
    )
    p.add_argument(
        "--music", "-m",
        required=True,
        metavar="MUSIC",
        help="Аудиофайл (mp3, wav, m4a, ...)",
    )
    p.add_argument(
        "--output", "-o",
        default="output.mp4",
        metavar="OUTPUT",
        help="Путь к выходному файлу (default: output.mp4)",
    )
    p.add_argument(
        "--fps",
        type=int,
        default=30,
        help="FPS выходного видео (default: 30)",
    )
    p.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="Скорость клипов (0.5–3.0, default: 1.0)",
    )
    p.add_argument(
        "--resolution",
        default=None,
        metavar="WxH",
        help='Разрешение выхода, например "1920:1080". Авто если не указано.',
    )
    p.add_argument(
        "--max-clips",
        type=int,
        default=None,
        metavar="N",
        help="Максимальное количество клипов",
    )
    p.add_argument(
        "--effects",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Включить лёгкую цветокоррекцию на этапе рендера",
    )
    p.add_argument(
        "--sample-fps",
        type=float,
        default=4.0,
        metavar="FPS",
        help="С какой частотой сэмплировать видео при анализе (default: 4)",
    )

    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    t0 = time.perf_counter()

    # check files
    for v in args.videos:
        if not Path(v).exists():
            print(f"[error] файл не найден: {v}", file=sys.stderr)
            sys.exit(1)
    if not Path(args.music).exists():
        print(f"[error] музыка не найдена: {args.music}", file=sys.stderr)
        sys.exit(1)

    print("=" * 50)
    print(f"  videos : {args.videos}")
    print(f"  music  : {args.music}")
    print(f"  output : {args.output}")
    print("=" * 50)

    # music analyze
    audio = analyze_audio(args.music)

    # video analyze
    if len(args.videos) == 1:
        video = analyze_video(args.videos[0], sample_fps=args.sample_fps)
        clips = select_clips(video, audio, max_clips=args.max_clips)
        clip_sources = None
    else:
        clips, clip_sources = _run_multi_video(
            args.videos, audio,
            max_clips=args.max_clips,
            sample_fps=args.sample_fps,
        )

    # render
    render(
        clips=clips,
        video_source=args.videos,
        music_path=args.music,
        output_path=args.output,
        fps=args.fps,
        speed=args.speed,
        resolution=args.resolution,
        effects=args.effects,
        clip_sources=clip_sources,
    )

    elapsed = time.perf_counter() - t0
    print(f"\n[ok] Ready in {elapsed:.1f}s -> {args.output}")


def _run_multi_video(
    video_paths: List[str],
    audio,
    max_clips: int | None,
    sample_fps: float,
):
    """Analyze all sources and select clips globally across videos."""
    videos = []

    for i, vpath in enumerate(video_paths):
        print(f"\n[main] === Video {i+1}/{len(video_paths)}: {vpath} ===")
        videos.append(analyze_video(vpath, sample_fps=sample_fps))

    return select_clips_multi(
        videos=videos,
        audio=audio,
        video_paths=video_paths,
        max_clips=max_clips,
    )


if __name__ == "__main__":
    main()
