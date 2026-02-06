FROM python:3.9-slim-bullseye

# Install system dependencies for libtorrent
RUN apt-get update && apt-get install -y \
    python3-libtorrent \
    && rm -rf /var/lib/apt/lists/*

# Set the PYTHONPATH so Python can find the libtorrent installed by apt
ENV PYTHONPATH="/usr/lib/python3/dist-packages"

WORKDIR /app
COPY . .

# Install other requirements
RUN pip install --no-cache-dir -r requirements.txt

# Start Flask (for health check) and the Bot
CMD gunicorn app:app --bind 0.0.0.0:$PORT --daemon && python3 bot.py
