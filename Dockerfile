FROM python:3.11-slim

RUN apt update && apt install -y \
    aria2 \
    coreutils \
    curl \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# install rclone
RUN curl https://rclone.org/install.sh | bash

WORKDIR /app
COPY . .

RUN mkdir -p /app/downloads
RUN pip install --no-cache-dir python-telegram-bot==20.7

CMD ["python", "bot.py"]
