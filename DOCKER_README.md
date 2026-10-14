# Запуск в Docker

Образ собирается на `python:3.12-slim` и работает от непривилегированного пользователя. В образ попадают только `main.py` и `config.py`. Секреты передаются при запуске.

## Сборка

```bash
docker build -t email-to-telegram-bot .
```

## Запуск

```bash
docker run -d \
  --name email-to-telegram-bot \
  --restart unless-stopped \
  --env-file .env \
  email-to-telegram-bot
```

`--restart unless-stopped` поднимает контейнер после перезагрузки сервера или падения.

## Логи и остановка

```bash
docker logs -f email-to-telegram-bot
docker stop email-to-telegram-bot
```

При остановке бот корректно закрывает соединение с почтой.

## Обновление

```bash
docker build -t email-to-telegram-bot .
docker rm -f email-to-telegram-bot
# затем снова команда из раздела «Запуск»
```
