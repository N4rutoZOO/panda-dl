FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV PORT=8080
ENV PYTHONUNBUFFERED=1
ENV PANDA_JOB_TTL=3600
ENV PANDA_YT_WORKER_POLL=0.8

CMD ["sh","-c","python -m uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
