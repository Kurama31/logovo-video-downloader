# Logovo Video Downloader

Загрузчик видео по ссылке с веб-интерфейсом. Запуск в Docker, сборка видео на устройстве пользователя.

Интерфейс: русский и английский. Язык выбирается автоматически по настройкам браузера; переключатель сверху позволяет изменить его вручную.

## Запуск

1. Установите [Git](https://git-scm.com/downloads/) и [Docker Desktop](https://docs.docker.com/desktop/). Запустите Docker Desktop в режиме Linux-контейнеров. На Linux можно использовать Docker Engine с Compose.
2. Скачайте проект и создайте файл настроек:

```sh
git clone https://github.com/Kurama31/logovo-video-downloader.git
cd logovo-video-downloader
cp .env.example .env
```

3. Откройте `.env` и замените `POSTGRES_PASSWORD` длинным случайным паролем из латинских букв и цифр. Не публикуйте `.env`.
4. Запустите приложение:

```sh
docker compose up -d --build
```

После запуска откройте [http://localhost:8080](http://localhost:8080). Порт по умолчанию — **8080**, доступ только с вашего компьютера. Python и Node.js отдельно устанавливать не нужно.

Остановка: `docker compose down`. Не добавляйте `-v`, если хотите сохранить историю и сессии.

Для доступа с других устройств нужен HTTPS и настройка `.env`: `PUBLIC_ORIGIN`, `COOKIE_SECURE`, `BIND_ADDRESS`. При смене порта измените и `PORT`, и `PUBLIC_ORIGIN`.

## Лицензия

Собственный код проекта — MIT. Условия сторонних компонентов: [THIRD_PARTY.md](THIRD_PARTY.md).

Скачивайте только материалы, на сохранение которых у вас есть права или иное законное основание. DRM не поддерживается.
