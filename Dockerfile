FROM python:3.11-slim-bookworm

# Install system dependencies
RUN apt-get update && apt-get install -y \
    python3-libtorrent \
    && rm -rf /var/lib/apt/lists/*

# Link system libraries to Python
ENV PYTHONPATH="/usr/lib/python3/dist-packages"
ENV PYTHONUNBUFFERED=1

WORKDIR /app
COPY . .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Use a shell script to start both processes properly
CMD gunicorn app:app --bind 0.0.0.0:$PORT --daemon && python3 bot.py
