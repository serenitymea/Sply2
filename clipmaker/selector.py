"""
Selecting video clips based on music beat intervals.

The selector keeps the runtime cheap by working with precomputed motion scores,
but tries to avoid the obvious MVP problems:
- choosing only the noisiest / shakiest windows
- cutting on every single beat for fast tracks
- repeating the same source too aggressively in multi-video mode
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import List, Optional, Sequence, Tuple

import numpy as np

from .analyzer import AudioFeatures, VideoFeatures

# Data classes

@dataclass
class Clip:
    start: float           # start second in the original video
    end: float             # end sec
    score: float           # motion score (0-1)
    beat_index: int = 0    # index of beat from music

    @property
    def duration(self) -> float:
        return self.end - self.start

# Core selection

def select_clips(
    video: VideoFeatures,
    audio: AudioFeatures,
    max_clips: Optional[int] = None,
    min_clip_duration: float = 0.2,
    overlap_seconds: float = 0.0,
) -> List[Clip]:
    """
    Selects clips synchronized to the beats of the music

    Args:
    video: Result of analyze_video()
    audio: Result of analyze_audio()
    max_clips: Maximum number of clips (default: all beats)
    min_clip_duration: Minimum clip length in seconds
    overlap_seconds: Allowed overlap between clips (0 = none)

    Returns:
    Clip list sorted by beat order
    """
    segments = _build_segments(
        beat_times=audio.beat_times,
        tempo=audio.tempo,
        min_clip_duration=min_clip_duration,
    )
    if max_clips is not None:
        segments = segments[:max_clips]

    clips: List[Clip] = []
    used_ranges: List[tuple[float, float]] = []
    used_centers: List[float] = []

    for beat_index, _, beat_dur in segments:
        clip = _find_best_window(
            video=video,
            duration=beat_dur,
            used_ranges=used_ranges,
            overlap_seconds=overlap_seconds,
            used_centers=used_centers,
        )
        if clip is None:
            continue

        clip.beat_index = beat_index
        clips.append(clip)
        used_ranges.append((clip.start - overlap_seconds,
                             clip.end + overlap_seconds))
        used_centers.append((clip.start + clip.end) * 0.5)

    print(f"[selector] {len(clips)} clips selected")
    if clips:
        avg = sum(c.score for c in clips) / len(clips)
        total = sum(c.duration for c in clips)
        print(f"[selector] avg_score={avg:.3f}, total_duration={total:.1f}s")

    return clips


def select_clips_multi(
    videos: Sequence[VideoFeatures],
    audio: AudioFeatures,
    video_paths: Sequence[str],
    max_clips: Optional[int] = None,
    min_clip_duration: float = 0.2,
    overlap_seconds: float = 0.0,
) -> tuple[List[Clip], List[str]]:
    """
    Global multi-video selection.

    For every music segment, we score the best candidate in each source and then
    pick the strongest one with a small penalty for long same-source streaks.
    """
    if len(videos) != len(video_paths):
        raise ValueError("videos/video_paths length mismatch")

    segments = _build_segments(
        beat_times=audio.beat_times,
        tempo=audio.tempo,
        min_clip_duration=min_clip_duration,
    )
    if max_clips is not None:
        segments = segments[:max_clips]

    used_ranges = [[] for _ in videos]
    used_centers = [[] for _ in videos]

    clips: List[Clip] = []
    clip_sources: List[str] = []
    last_source: str | None = None
    same_source_run = 0

    for beat_index, _, beat_dur in segments:
        best_choice: tuple[Clip, str, int, float] | None = None

        for video_idx, (video, path) in enumerate(zip(videos, video_paths)):
            clip = _find_best_window(
                video=video,
                duration=beat_dur,
                used_ranges=used_ranges[video_idx],
                overlap_seconds=overlap_seconds,
                used_centers=used_centers[video_idx],
            )
            if clip is None:
                continue

            score = clip.score
            if path == last_source:
                score -= 0.08 + 0.04 * same_source_run

            if best_choice is None or score > best_choice[3]:
                best_choice = (clip, path, video_idx, score)

        if best_choice is None:
            continue

        clip, path, video_idx, _ = best_choice
        clip.beat_index = beat_index
        clips.append(clip)
        clip_sources.append(path)
        used_ranges[video_idx].append((clip.start - overlap_seconds,
                                       clip.end + overlap_seconds))
        used_centers[video_idx].append((clip.start + clip.end) * 0.5)

        if path == last_source:
            same_source_run += 1
        else:
            last_source = path
            same_source_run = 0

    print(f"[selector] {len(clips)} multi-source clips selected")
    return clips, clip_sources

# Internal helpers


def _build_segments(
    beat_times: np.ndarray,
    tempo: float,
    min_clip_duration: float,
) -> List[Tuple[int, float, float]]:
    if len(beat_times) < 2:
        return []

    beat_intervals = np.diff(beat_times)
    median_beat = float(median(beat_intervals)) if len(beat_intervals) else 0.5

    beats_per_clip = 1
    if tempo >= 150 or median_beat <= 0.28:
        beats_per_clip = 2
    if tempo >= 185 or median_beat <= 0.18:
        beats_per_clip = 4

    segments: List[Tuple[int, float, float]] = []
    i = 0
    last_index = len(beat_times) - 1

    while i < last_index:
        end_index = min(i + beats_per_clip, last_index)
        duration = float(beat_times[end_index] - beat_times[i])
        if duration >= min_clip_duration:
            segments.append((i, float(beat_times[i]), duration))
        i = end_index

    return segments

def _find_best_window(
    video: VideoFeatures,
    duration: float,
    used_ranges: list,
    overlap_seconds: float,
    used_centers: list[float],
) -> Optional[Clip]:
    """Sliding window over video, returns the best balanced clip."""
    fps = video.fps
    scores = video.motion_scores
    total = video.frame_count

    win_frames = int(duration * fps)
    if win_frames < 1:
        return None

    max_start_frame = total - win_frames
    if max_start_frame <= 0:
        return None

    # step — 0.25 sec
    step = max(1, int(fps * 0.25))

    best_score = float("-inf")
    best_start = 0.0

    for fi in range(0, max_start_frame, step):
        start_sec = fi / fps
        end_sec = (fi + win_frames) / fps

        if _overlaps(start_sec, end_sec, used_ranges, overlap_seconds):
            continue

        window = scores[fi: fi + win_frames]
        mean_score = float(window.mean())
        peak_score = float(window.max())
        volatility = float(np.abs(np.diff(window)).mean()) if len(window) > 1 else 0.0
        similarity_penalty = _reuse_penalty(
            start_sec=start_sec,
            end_sec=end_sec,
            used_centers=used_centers,
        )
        score = (
            mean_score * 0.60
            + peak_score * 0.32
            - volatility * 0.22
            - similarity_penalty
        )
        if score > best_score:
            best_score = score
            best_start = start_sec

    if best_score == float("-inf"):
        return None

    return Clip(
        start=best_start,
        end=best_start + duration,
        score=max(0.0, best_score),
    )


def _overlaps(start: float, end: float, used: list, margin: float) -> bool:
    for u_start, u_end in used:
        if start < u_end + margin and end > u_start - margin:
            return True
    return False


def _reuse_penalty(start_sec: float, end_sec: float, used_centers: list[float]) -> float:
    if not used_centers:
        return 0.0

    center = (start_sec + end_sec) * 0.5
    nearest = min(abs(center - other) for other in used_centers)
    if nearest >= 2.5:
        return 0.0
    return (2.5 - nearest) / 2.5 * 0.18
