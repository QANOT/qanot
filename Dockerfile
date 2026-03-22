FROM python:3.12-slim

LABEL maintainer="Sirli AI <hello@sirli.ai>"
LABEL description="Qanot AI — lightweight Python agent framework for Telegram bots"
LABEL version="2.0.3"

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl git zip && rm -rf /var/lib/apt/lists/*

RUN useradd -m -s /bin/bash qanot

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY qanot/ ./qanot/
COPY templates/ ./templates/
COPY plugins/ ./plugins/

RUN mkdir -p /data/workspace /data/sessions /data/cron /data/plugins \
    && chown -R qanot:qanot /app /data

USER qanot

EXPOSE 8765 8443

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -sf http://127.0.0.1:8765/api/status || exit 1

CMD ["python", "-m", "qanot.main"]
