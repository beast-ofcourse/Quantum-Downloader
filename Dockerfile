FROM python:3.11-slim

# ffmpeg is required to merge separate video+audio streams and for --audio-only.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir .

ENTRYPOINT ["ytchannel"]
