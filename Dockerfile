FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

# /data/run holds the supervisor lock and the worker control socket.
RUN useradd --create-home --uid 1000 gymbot \
 && mkdir -p /data/run && chown -R gymbot:gymbot /data /app
USER gymbot

VOLUME ["/data"]
EXPOSE 8080 8081

# Health is "is the supervisor serving the dashboard?" — deliberately NOT "is
# the Discord bot connected". The supervisor staying up while the bot is down is
# the normal, intended state during setup and reconfiguration, and it is exactly
# when the container must keep running so the operator can fix it. Alerting that
# wants the stricter test can use /healthz?require_worker=1.
#
# This replaces the old compose-level check, which opened DB_PATH in a separate
# process — and sqlite3.connect() CREATES a missing file, so it reported healthy
# while probing a stray empty database.
HEALTHCHECK --interval=1m --timeout=10s --retries=3 --start-period=20s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8081/healthz',timeout=5)"

# The supervisor owns the database and the dashboard and spawns `python -m
# app.bot` as a child. Running the bot module directly still works for
# development; it only serves the control socket when GYMBOT_ROLE=worker.
CMD ["python", "-m", "app.supervisor"]
