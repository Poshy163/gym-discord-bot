FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

RUN useradd --create-home --uid 1000 gymbot \
 && mkdir -p /data && chown -R gymbot:gymbot /data /app
USER gymbot

VOLUME ["/data"]

CMD ["python", "-m", "app.bot"]
