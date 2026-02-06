FROM python:3.11-slim

RUN apt update && apt install -y \
    aria2 \
    coreutils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .

RUN pip install --no-cache-dir -r requirements.txt

CMD ["python", "bot.py"]