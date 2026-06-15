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
- file (`mp3/wav/ogg/flac/m4a`, Telegram voice messages are also accepted) or
- TikTok link (downloaded via `yt-dlp`).
3. Sends video files (`mp4/mov/mkv/avi/webm`, up to 5 files, up to 20 MB each, total up to 10 minutes).
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
- `app/bot.py` - Telegram application setup and client timeouts.
- `app/handlers.py` - Telegram states, commands, orchestration.
- `app/queue_manager.py` - queue and processing timeouts.
- `app/access_control.py` - daily usage limits and admin IDs.
- `app/runtime_control.py` - file-backed session/preprocess/pipeline slot state.
- `app/shared_state.py` - JSON state files with file locks.
- `app/services/video_service.py` - media intake and validation.
- `app/services/downloader.py` - TikTok audio download through `yt-dlp`.
- `app/services/ffmpeg_service.py` - media conversion helpers.
- `app/services/pipeline_service.py` - runs `clipmaker.main` as subprocess.
- `clipmaker/analyzer.py` - audio/video analysis.
- `clipmaker/selector.py` - beat-aligned clip selection.
- `clipmaker/renderer.py` - final render via `ffmpeg`.

## Карта проекта для преподавателя

### Как части связаны между собой

1. `run_bot.py` запускает Telegram-бота, проверяет `.env`, `ffmpeg` и `ffprobe`, затем создает `BotApp`, `VideoBot` и `QueueManager`.
2. `app/handlers.py` принимает команды и файлы из Telegram. Он хранит временную сессию пользователя, проверяет загрузки через `VideoService`, затем ставит пользователя в очередь.
3. `QueueManager` выполняет только одну задачу обработки за раз. Когда очередь доходит до пользователя, `VideoBot._process_user()` создает `PipelineService`.
4. `PipelineService` запускает `python -m clipmaker.main` отдельным subprocess. Так тяжелая обработка видео не блокирует Telegram-бота.
5. `clipmaker.main` вызывает основные этапы: `analyze_audio()`, `analyze_video()`, `select_clips()` и `render()`.
6. `renderer.py` собирает `ffmpeg` filter graph: вырезает выбранные фрагменты, склеивает их, добавляет музыку и сохраняет итоговый `.mp4`.
7. Бот отправляет готовое видео пользователю и удаляет временные файлы сессии.

### Файлы и основные функции

- `run_bot.py` - точка входа приложения.
  `_configure_logging()` настраивает логирование; `_require_env()` проверяет обязательные переменные; `_run_preflight_checks()` проверяет токен, `ffmpeg`, `ffprobe` и папки данных; `_install_signal_handlers()` обрабатывает завершение процесса; `main()` запускает polling Telegram-бота и worker очереди.

- `app/bot.py` - обертка над `python-telegram-bot`.
  `BotApp` создает Telegram `Application`, хранит `application` и `bot`, а также подключает handlers.

- `app/handlers.py` - основная логика диалога в Telegram.
  `UserSession` хранит временные файлы и состояние пользователя. `VideoBot` обрабатывает `/start`, `/run`, `/done`, `/cancel`, прием аудио, прием видео, уведомления очереди и отправку результата. `_process_user()` связывает Telegram-часть с pipeline обработки.

- `app/queue_manager.py` - очередь задач.
  `QueueManager.enqueue()` добавляет пользователя в очередь; `get_position()` возвращает позицию; `cancel()` отменяет задачу; `_worker()` выполняет задачи по одной с таймаутом `500s`.

- `app/access_control.py` - дневные лимиты.
  `load_admin_ids()` читает `ADMIN_TELEGRAM_IDS`. `DailyUsageLimiter` хранит использование в JSON и дает методы `get_remaining()`, `consume()` и `refund()`. Обычные пользователи ограничены дневным лимитом, админы не ограничены.

- `app/runtime_control.py` - контроль активных сессий и слотов.
  `RuntimeControl` ограничивает количество активных сессий, задач предобработки и задач pipeline. Состояние хранится в JSON с временем жизни, поэтому устаревшие записи очищаются.

- `app/shared_state.py` - безопасная работа с JSON-состоянием.
  `FileMutex` создает lock-файл для защиты от одновременной записи. `locked_json_state()` читает JSON, дает к нему доступ и атомарно сохраняет изменения.

- `app/paths.py` - общие пути проекта.
  `ensure_tmp_root()`, `ensure_output_root()` и `ensure_data_root()` создают и возвращают папки `tmp/`, `output/` и `data/`.

- `app/services/video_service.py` - прием и проверка медиа из Telegram.
  Вспомогательные функции проверяют URL, размер файла, расширение, скачивают Telegram-файл и получают длительность через `ffprobe`. `VideoService.acquire_audio()` принимает аудиофайл, voice message или TikTok-ссылку. `acquire_video()` принимает видео, конвертирует его при необходимости, проверяет длительность и возвращает готовый путь к файлу.

- `app/services/downloader.py` - скачивание аудио из TikTok.
  `_validate_url()` разрешает только безопасные TikTok HTTP/HTTPS-ссылки; `_expand_short_url()` раскрывает короткие ссылки; `_resolve_music_target_url()` находит реальный источник аудио; `MediaDownloader.download()` запускает `yt-dlp` с таймаутом, proxy и cookies.

- `app/services/ffmpeg_service.py` - асинхронная конвертация.
  `FFmpegService.run()` запускает `ffmpeg` с таймаутом. `to_mp3()` конвертирует аудио в MP3; `to_mp4()` конвертирует видео в MP4/H.264/AAC.

- `app/services/pipeline_service.py` - запуск pipeline в subprocess.
  `PipelineService._build_cmd()` собирает команду `python -m clipmaker.main ...`. `run()` запускает процесс, ждет завершения, обрабатывает отмену и проверяет, что выходной файл создан.

- `clipmaker/main.py` - CLI-точка входа движка монтажа.
  `parse_args()` читает аргументы командной строки: видео, музыку, output, FPS, speed, resolution и параметры анализа. `main()` проверяет файлы и запускает pipeline. `_run_multi_video()` анализирует несколько видео и чередует выбранные фрагменты из разных источников.

- `clipmaker/analyzer.py` - анализ аудио и видео.
  `AudioFeatures` хранит tempo и времена битов. `VideoFeatures` хранит motion scores, FPS, число кадров и длительность. `analyze_audio()` через `librosa` находит BPM и биты. `analyze_video()` через OpenCV optical flow оценивает движение в видео.

- `clipmaker/selector.py` - выбор фрагментов.
  `Clip` описывает один выбранный фрагмент. `select_clips()` подбирает фрагменты видео под интервалы битов музыки. `_find_best_window()` ищет лучший участок по движению; `_overlaps()` не дает выбранным клипам пересекаться.

- `clipmaker/renderer.py` - финальный рендер.
  `render()` проверяет список клипов, выбирает разрешение, собирает команду `ffmpeg` и сохраняет результат. `_build_filters()` создает фильтры trim/scale/concat. `_detect_resolution()` через `ffprobe` определяет размер исходника и выбирает стандартное выходное разрешение.

- `clipmaker/gui.py` - дополнительный desktop GUI-прототип.
  `RenderWorker` запускает тот же pipeline в Qt-потоке. `FileDropLabel` поддерживает drag-and-drop файлов. `ClipMakerWindow` строит окно приложения и запускает рендер из выбранных файлов. В основном Telegram-сценарии этот файл не используется.

- `tests/` - автотесты.
  `tests/test_clipmaker.py` проверяет analyzer, downloader, selector, renderer, CLI и GUI через mocks. `tests/api/test_job_flow.py` проверяет API-схемы, если установлены API-зависимости. `tests/worker/test_refund_policy.py` проверяет наличие документации по refund policy.

- `Dockerfile` - production Docker-образ для Telegram-бота.
  Устанавливает Python-зависимости, `ffmpeg`, runtime-библиотеки, копирует `app/`, `clipmaker/` и запускает `python run_bot.py`.

- `docker-compose.yml` - локальный Docker-пример.
  Собирает образ, монтирует input/output папки и задает лимиты ресурсов. Сейчас command равен `["--help"]`, поэтому это пример, а не обычный запуск бота.

- `railway.toml` и `RAILWAY.md` - файлы деплоя Railway.
  Описывают Docker-based запуск в одном экземпляре и нужные переменные окружения.

- `.env.example` - минимальный шаблон окружения.
  Содержит `TELEGRAM_BOT_TOKEN` и опциональный `ADMIN_TELEGRAM_IDS`.

- `requirements.txt` и `pyproject.toml` - зависимости и метаданные пакета.
  Указывают Python 3.11+, основные библиотеки и console entrypoint `clipmaker`.

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
```

Optional:

- `ADMIN_TELEGRAM_IDS` - comma-separated Telegram user IDs with no daily limit
- `LOG_LEVEL` - Python logging level (default: `INFO`)
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

`docker-compose.yml` is included as a resource-limited sample with input/output volume mounts and `command: ["--help"]`.

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

The CLI parser also accepts `--effects`, but the current renderer has no separate effects switch.

## Current Limits

- Bot works in private chats only.
- Regular users can generate up to 5 videos per day; admins from `ADMIN_TELEGRAM_IDS` are unlimited.
- Max 5 videos per generation.
- Up to 20 MB per uploaded file.
- Total uploaded video duration up to 10 minutes.
- User upload sessions expire after 10 minutes of inactivity.
- Pipeline timeout is about 8m 20s (`500s`).
- Queue/runtime state is designed for one active instance.

## Railway Deployment

Already included in the repository:

- `Dockerfile`
- `railway.toml`
- `RAILWAY.md`

Required environment variables:

- `TELEGRAM_BOT_TOKEN`

Optional environment variables:

- `ADMIN_TELEGRAM_IDS`
- `LOG_LEVEL`
- `YT_DLP_PROXY`
- `YT_DLP_COOKIES`

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
