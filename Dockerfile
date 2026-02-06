FROM python:3.9-slim-bullseye

# Install libtorrent system dependencies
RUN apt-get update && apt-get install -y \
    python3-libtorrent \
    && rm -rf /var/lib/apt/lists/*

# Link system libraries to Python
ENV PYTHONPATH="/usr/lib/python3/dist-packages"

WORKDIR /app
COPY . .

RUN pip install --no-cache-dir -r requirements.txt

# Start Flask (background) and Bot (foreground)
CMD gunicorn app:app --bind 0.0.0.0:$PORT --daemon && python3 bot.py