FROM denoland/deno:bin-2.1.4 AS deno

FROM python:3.12-slim
# ffmpeg merges video+audio; deno lets yt-dlp solve YouTube JS challenges.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY --from=deno /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py texts.yaml tones.yaml ./
COPY webapp ./webapp

CMD ["python", "bot.py"]
