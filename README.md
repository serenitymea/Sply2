# Clipmaker

Clipmaker is a Telegram bot and Python pipeline for automatic music-synced video editing.

The project accepts audio (file or TikTok link), collects up to 5 user videos, analyzes music rhythm and video motion, selects the best fragments, and renders a final clip with `ffmpeg`.

## What The Product Does

- Automatic beat-synced editing without manual timeline work.
- User task queue processing (single worker).
- Daily generation limits for regular users.
- Safer temp/state handling with file-based locks.
- Ready-to-deploy setup for Railway.

## User Flow In Telegram

1. User sends `/run` in a private chat with the bot.
2. Sends audio:
- file (`mp3/wav/ogg/flac/m4a`) or
- TikTok link (downloaded via `yt-dlp`).
3. Sends video files (up to 5 files, up to 20 MB each, total up to 10 minutes).
4. Sends `/done`.
5. Receives the rendered video.

Commands:

- `/start` - welcome and short guide
- `/run` - start a new session
- `/done` - finish uploads and enqueue processing
- `/cancel` - cancel current operation/session

## Tech Stack

- Python 3.11
- `python-telegram-bot`
- `librosa` (tempo/beat analysis)
- `opencv-python-headless` (frame motion scoring)
- `ffmpeg` / `ffprobe` (conversion and rendering)
- `yt-dlp` (audio download from TikTok links)

## Architecture

- `run_bot.py` - bot entrypoint, preflight checks, lifecycle.
- `app/handlers.py` - Telegram states, commands, orchestration.
- `app/queue_manager.py` - queue and processing timeouts.
- `app/services/video_service.py` - media intake and validation.
- `app/services/pipeline_service.py` - runs `clipmaker.main` as subprocess.
- `clipmaker/analyzer.py` - audio/video analysis.
- `clipmaker/selector.py` - beat-aligned clip selection.
- `clipmaker/renderer.py` - final render via `ffmpeg`.

## Quick Start (Local)

### 1) Install System Dependencies

`ffmpeg` and `ffprobe` must be available in `PATH`.

### 2) Prepare Environment

```bash
python -m venv .venv
# Windows (PowerShell)
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 3) Configure Variables

Create `.env` (you can use `.env.example`):

```env
TELEGRAM_BOT_TOKEN=your_bot_token_here
ADMIN_TELEGRAM_IDS=123456789
LOG_LEVEL=INFO
```

Optional:

- `YT_DLP_PROXY` - proxy for `yt-dlp`
- `YT_DLP_COOKIES` - cookie file path (for restricted content access)

### 4) Start Bot

```bash
python run_bot.py
```

## Docker Run

```bash
docker build -t clipmaker:latest .
docker run --rm --env-file .env clipmaker:latest
```

`docker-compose.yml` is also included for local runs.

## CLI Mode (Without Telegram)

You can run the editing engine directly:

```bash
python -m clipmaker.main video1.mp4 video2.mp4 --music music.mp3 --output result.mp4
```

Useful options:

- `--fps 30`
- `--speed 1.0` (clamped to `0.5..3.0`)
- `--resolution 1920:1080`
- `--max-clips 40`
- `--sample-fps 4.0`
- `--effects`

## Current Limits

- Bot works in private chats only.
- Max 5 videos per generation.
- Up to 20 MB per uploaded file.
- Total uploaded video duration up to 10 minutes.
- Pipeline timeout is about 8m 20s (`500s`).
- Queue/runtime state is designed for one active instance.

## Railway Deployment

Already included in the repository:

- `Dockerfile`
- `railway.toml`
- `RAILWAY.md`

Required environment variables:

- `TELEGRAM_BOT_TOKEN`
- `ADMIN_TELEGRAM_IDS`

Important: run with **1 replica** and no horizontal scaling.

## Data Storage

- `tmp/` - temporary session files.
- `output/` - rendered results.
- `data/` - service JSON state (`usage`, `runtime`).

## Tests

```bash
pytest
```

## Project Status

MVP in a production-ready form for a single-instance Telegram workflow.
