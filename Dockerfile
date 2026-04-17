FROM python:3.11-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libsndfile1-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.11-slim

LABEL maintainer="clipmaker"
LABEL description="Telegram bot for automatic video editing"

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libsndfile1 \
    libglib2.0-0 \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local

COPY app/ ./app/
COPY clipmaker/ ./clipmaker/
COPY run_bot.py ./
COPY requirements.txt ./
COPY pyproject.toml ./

RUN mkdir -p /app/tmp /app/output /app/data

RUN useradd -m -u 1000 clipuser && chown -R clipuser:clipuser /app
USER clipuser

CMD ["python", "run_bot.py"]
