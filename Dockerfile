FROM python:3.12-slim

# ffmpeg is needed to merge 1080p+ video+audio and to make MP3s
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

ENV PORT=7860
EXPOSE 7860
CMD ["sh", "-c", "python3 app.py"]
