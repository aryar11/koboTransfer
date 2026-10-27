FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

# 568 is the TrueNAS SCALE "apps" user/group.
ARG APP_UID=568
ARG APP_GID=568
RUN groupadd -g ${APP_GID} app \
    && useradd -u ${APP_UID} -g app -M -d /app -s /usr/sbin/nologin app

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY templates ./templates
COPY static ./static

# Build contexts on TrueNAS datasets are often owner-only (ACL/umask); the app runs as non-root.
RUN chmod -R a+rX /app \
    && mkdir -p /data && chown app:app /data
USER app
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)" || exit 1

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8080 --proxy-headers --forwarded-allow-ips \"${FORWARDED_ALLOW_IPS:-127.0.0.1}\""]
