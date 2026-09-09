# INFRA-24 (SEC-024): pin the base image by full sha256 digest, not the mutable
# python:3.11-slim tag, so repeated builds resolve to the same bytes (no silent
# supply-chain drift). Roll forward deliberately with:
#   docker pull python:3.11-slim
#   docker inspect --format '{{index .RepoDigests 0}}' python:3.11-slim
# python:3.11-slim
FROM python:3.11-slim@sha256:f9fa7f851e38bfb19c9de3afbc4b86ae7176ea7aaf94535c31df5458d5849457

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WEATHER_GFS_DATA_DIR=/data \
    WEATHER_GFS_TMP_DIR=/tmp/weather-gfs

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl libeccodes0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY services/weather-gfs/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY services/weather-gfs/weather_gfs /app/weather_gfs

RUN mkdir -p /data /tmp/weather-gfs

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl --fail --silent --show-error -o /dev/null http://localhost:8080/health || exit 1

CMD ["uvicorn", "weather_gfs.app:app", "--host", "0.0.0.0", "--port", "8080"]
