FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем только код. Секреты передаются при запуске через --env-file.
COPY main.py config.py ./

RUN useradd --create-home --uid 10001 bot
USER bot

CMD ["python", "main.py"]
