# Railway Deploy

This project is ready for `single-instance` deployment on Railway as a worker-style Telegram bot.

## Required Settings

Set these Railway environment variables:

- `TELEGRAM_BOT_TOKEN`
- `ADMIN_TELEGRAM_IDS`

Optional variables:

- `LOG_LEVEL=INFO`
- `YT_DLP_PROXY`
- `YT_DLP_COOKIES`

## Important Limits

- Run exactly `1 replica`.
- Do not enable horizontal scaling.
- Local `data/`, `tmp/`, and `output/` live inside the container filesystem.
- Queue and runtime state are safe only for a single active Railway instance.

## Deploy Configuration

The repo already contains:

- [Dockerfile](/d:/Sply2/Dockerfile)
- [railway.toml](/d:/Sply2/railway.toml)
- [run_bot.py](/d:/Sply2/run_bot.py)

Railway should build using the Dockerfile and start with:

```bash
python run_bot.py
```

## Before First Deploy

1. Rotate the current Telegram bot token if it was ever committed or shared locally.
2. Add the fresh token to Railway variables.
3. Confirm the service uses one replica only.
4. Deploy.

## Operational Notes

- The bot works only in private chats.
- `ffmpeg` and `ffprobe` are checked at startup.
- If Railway restarts the container, in-memory queue items are lost.
- If you need durable queueing or multiple replicas, move runtime state to Redis or Postgres first.
