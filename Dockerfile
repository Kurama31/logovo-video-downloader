FROM node:22-bookworm-slim AS javascript
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY app/vendor.js ./app/vendor.js
RUN npm run build \
    && npm pack ./node_modules/mediabunny --pack-destination app/web --silent \
    && cp node_modules/mediabunny/LICENSE app/web/MEDIABUNNY-LICENSE.txt
FROM python:3.12-slim-bookworm
COPY --from=javascript /usr/local/bin/node /usr/local/bin/node
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home downloader
WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt
COPY app ./app
COPY --from=javascript /build/app/web/ ./app/web/
COPY tests ./tests
RUN mkdir /data && chown downloader:downloader /data
USER downloader
ENV DATA_DIR=/data
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
