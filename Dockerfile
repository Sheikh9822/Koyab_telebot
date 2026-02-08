FROM python:3.11-slim-bookworm

# 1. Install system dependencies for libtorrent (Bookworm version)
RUN apt-get update && apt-get install -y \
    python3-libtorrent \
    && rm -rf /var/lib/apt/lists/*

# 2. Link system libraries to Python 3.11
ENV PYTHONPATH="/usr/lib/python3/dist-packages"

WORKDIR /app
COPY . .

# 3. Install other requirements
RUN pip install --no-cache-dir -r requirements.txt

# 4. Start Health Check and Bot
CMD gunicorn app:app --bind 0.0.0.0:$PORT --daemon && python3 bot.py
