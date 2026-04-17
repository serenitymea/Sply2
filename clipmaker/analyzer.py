"""
analyzer.py — audio beats + video motion scoring

two public calls:
    audio = analyze_audio("music.mp3")
    video = analyze_video("video.mp4")
"""

import cv2
import librosa
import numpy as np
from dataclasses import dataclass, field

MAX_ANALYSIS_DURATION_SEC = 10 * 60 + 5
MAX_ANALYSIS_FRAMES = 300_000
MAX_ANALYSIS_FPS = 240.0

# Data classes

@dataclass
class AudioFeatures:
    beat_times: np.ndarray   # secs of every beat
    tempo: float             # BPM


@dataclass
class VideoFeatures:
    motion_scores: np.ndarray   # [N] float32, normalized 0-1 (N = count of frames)
    fps: float
    frame_count: int
    duration: float


# Audio

def analyze_audio(music_path: str) -> AudioFeatures:
    """load audio, find BPM and beat times"""
    print(f"[analyzer] audio: {music_path}")
    y, sr = librosa.load(music_path, sr=22050, mono=True)

    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr, units="frames")
    beat_times = librosa.frames_to_time(beat_frames, sr=sr)
    tempo_val = float(np.atleast_1d(tempo)[0])

    print(f"[analyzer] BPM={tempo_val:.1f}, beats={len(beat_times)}")
    return AudioFeatures(beat_times=beat_times, tempo=tempo_val)

# Video

def analyze_video(video_path: str, sample_fps: float = 4.0) -> VideoFeatures:
    """
    motion score for every frame throuth optical flow.
    sempling video sample_fps (default 4 frames/sec),
    all else file pick interpolation
    """
    print(f"[analyzer] video: {video_path}")
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise IOError(f"Cant open video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = total / fps if fps > 0 else 0.0

    if fps <= 0 or fps > MAX_ANALYSIS_FPS:
        cap.release()
        raise ValueError(f"Suspicious video FPS: {fps}")
    if total <= 0 or total > MAX_ANALYSIS_FRAMES:
        cap.release()
        raise ValueError(f"Suspicious frame count: {total}")
    if duration <= 0 or duration > MAX_ANALYSIS_DURATION_SEC:
        cap.release()
        raise ValueError(f"Video duration is too large for analysis: {duration:.1f}s")

    print(f"[analyzer] fps={fps:.2f}, frames={total}, duration={duration:.1f}s")

    step = max(1, int(fps / sample_fps))
    scores = np.zeros(total, dtype=np.float32)
    prev_gray: np.ndarray | None = None
    sampled_indices = []

    for i in range(0, total, step):
        cap.set(cv2.CAP_PROP_POS_FRAMES, i)
        ret, frame = cap.read()
        if not ret or frame is None:
            break

        # l size enough for flow
        small = cv2.resize(frame, (320, 180))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

        if prev_gray is not None:
            flow = cv2.calcOpticalFlowFarneback(
                prev_gray, gray, None,
                pyr_scale=0.5, levels=3, winsize=15,
                iterations=3, poly_n=5, poly_sigma=1.2, flags=0,
            )
            mag = np.hypot(flow[..., 0], flow[..., 1])
            # p90
            scores[i] = float(np.percentile(mag, 90))
            sampled_indices.append(i)

        prev_gray = gray

        if i % (step * 100) == 0:
            pct = int(i / total * 100)
            print(f"[analyzer] video scan {pct}%", end="\r")

    cap.release()
    print()

    # interpolize skiped frames
    if len(sampled_indices) > 1:
        scores = np.interp(
            np.arange(total),
            sampled_indices,
            scores[sampled_indices],
        ).astype(np.float32)

    # Normalize
    mx = scores.max()
    if mx > 0:
        scores /= mx

    return VideoFeatures(
        motion_scores=scores,
        fps=fps,
        frame_count=total,
        duration=duration,
    )
