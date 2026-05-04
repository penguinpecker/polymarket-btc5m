FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x /app/railway-start.sh /app/live-railway-start.sh

# Default CMD = paper service entrypoint. The live service overrides
# startCommand in Railway to /app/live-railway-start.sh, so the same
# image runs both with no per-service Dockerfile fork.
CMD ["/app/railway-start.sh"]
