"""
Selecting video clips based on music beat intervals

For each beat interval, we find the window in the video with the highest motion score
Avoiding overlaps between clips
Optionally, we use an ML model for re-ranking
"""

from __future__ import annotations

import glob
from dataclasses import dataclass, field
from typing import List, Optional

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

# Optional ML re-scoring

def _try_load_model():
    """Loads the last trained model"""
    try:
        import joblib
        files = sorted(glob.glob("model_output/model_*.pkl"))
        if files:
            model = joblib.load(files[-1])
            print(f"[selector] ML model: {files[-1]}")
            return model
    except ImportError:
        pass
    return None

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
    beat_times = audio.beat_times
    n_beats = len(beat_times) - 1

    if max_clips is None:
        max_clips = n_beats
    else:
        max_clips = min(max_clips, n_beats)

    model = _try_load_model()

    clips: List[Clip] = []
    # The number of occupied seconds in the video (including overlap_seconds)
    used_ranges: List[tuple] = []

    for i in range(max_clips):
        beat_start = beat_times[i]
        beat_dur = beat_times[i + 1] - beat_start

        if beat_dur < min_clip_duration:
            continue

        clip = _find_best_window(
            video=video,
            duration=beat_dur,
            used_ranges=used_ranges,
            overlap_seconds=overlap_seconds,
        )
        if clip is None:
            continue

        clip.beat_index = i
        clips.append(clip)

        # Mark as busy
        used_ranges.append((clip.start - overlap_seconds,
                             clip.end + overlap_seconds))

    if model is not None:
        clips = _ml_rerank(clips, video, model)

    print(f"[selector] {len(clips)} clips selected")
    if clips:
        avg = sum(c.score for c in clips) / len(clips)
        total = sum(c.duration for c in clips)
        print(f"[selector] avg_score={avg:.3f}, total_duration={total:.1f}s")

    return clips

# Internal helpers

def _find_best_window(
    video: VideoFeatures,
    duration: float,
    used_ranges: list,
    overlap_seconds: float,
) -> Optional[Clip]:
    """Sliding window over video, returns the clip with the best motion score"""
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

    best_score = -1.0
    best_start = 0.0

    for fi in range(0, max_start_frame, step):
        start_sec = fi / fps
        end_sec = (fi + win_frames) / fps

        # overlap rewiew
        if _overlaps(start_sec, end_sec, used_ranges, overlap_seconds):
            continue

        score = float(scores[fi: fi + win_frames].mean())
        if score > best_score:
            best_score = score
            best_start = start_sec

    return Clip(
        start=best_start,
        end=best_start + duration,
        score=best_score,
    )


def _overlaps(start: float, end: float, used: list, margin: float) -> bool:
    for u_start, u_end in used:
        if start < u_end + margin and end > u_start - margin:
            return True
    return False


def _ml_rerank(clips: List[Clip], video: VideoFeatures, model) -> List[Clip]:
    """optional ml_rerank"""
    try:
        features = np.array([
            [c.score, c.duration, c.start / video.duration]
            for c in clips
        ], dtype=np.float32)

        if hasattr(model, "predict_proba"):
            ml_scores = model.predict_proba(features)[:, 1]
        else:
            ml_scores = model.predict(features)

        for clip, ml_score in zip(clips, ml_scores):
            clip.score = float(ml_score)

        print("[selector] ML scores applied")
    except Exception as e:
        print(f"[selector] ML re-scoring failed: {e} — using heuristic scores")

    return clips