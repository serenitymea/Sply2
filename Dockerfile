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
LABEL description="Automatic video editing"

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local

COPY clipmaker/ ./clipmaker/

RUN mkdir -p /data/input /data/output

RUN useradd -m -u 1000 clipuser && chown -R clipuser:clipuser /app /data
USER clipuser

ENTRYPOINT ["python", "-m", "clipmaker.main"]

CMD ["--help"]