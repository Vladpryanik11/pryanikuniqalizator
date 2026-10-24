# Бот-уникализатор для подписчиков

Бесплатный Telegram-бот для подписчиков канала: присылаешь видео — получаешь одну уникализированную копию
файлом. Движок — [video-uniquifier](https://github.com/Hostlife22/video-uniquifier) (MIT, см. `NOTICE`),
профиль диапазонов — `config/uniq.yaml`. Бот ведёт базу пользователей: статистика, выгрузка, рассылки.
Дизайн: `docs/superpowers/specs/2026-10-23-subscriber-uniq-bot-design.md`.

## Разработка и тесты

```bash
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"    # на Linux: .venv/bin/python
.venv\Scripts\python -m pytest
```

Для тестов движка нужны `ffmpeg` и `ffprobe` в PATH (фильтр `zscale`, кодеки `libx264` и `libx265`);
без них эти тесты пропускаются. Тесты никогда не читают настоящий `.env`.

Те же тесты под Linux, как в проде (пользователь 101, POSIX-ветки `nice` и остановки группы процессов):
`docker build --target test .` или `scripts\deploy.ps1 -Test`.

Команды: `python -m uniqbot bot | render IN OUT [--seed N] | logout-cloud`.

## Подбор диапазонов

```bash
.venv\Scripts\python -m uniqbot render in.mp4 out.mp4 --seed 42
```

Один рендер по профилю `config/uniq.yaml`, в конце печатает параметры копии (JSON). Тот же `--seed` даёт
те же параметры. Команда не читает `.env`, ограничение времени у неё фиксированное (900 с).

## Настройки (`.env`)

Скопировать `.env.example` в `.env` и заполнить (файл в `.gitignore`):

| Ключ | Что это |
| --- | --- |
| `BOT_TOKEN` | токен бота от @BotFather |
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` | с my.telegram.org; нужны локальному серверу Bot API |
| `CHANNEL_ID` | канал, на который надо быть подписанным: `-100…` или `@username` |
| `CHANNEL_URL` | ссылка на канал для кнопки «Подписаться» |
| `ADMIN_IDS` | Telegram id администраторов через запятую (узнать у @userinfobot) |
| `MAX_FILE_MB`, `MAX_DURATION_SEC`, `RENDER_TIMEOUT_SEC` | пределы; пусто — 200 МБ, 180 с, 900 с |

`DATA_DIR` (база `bot.db` и рабочие каталоги `work/`) и `BOT_API_URL` задаёт `docker-compose.yml`. Без
`BOT_API_URL` бот ходит в облачный Bot API с его лимитами на размер файлов: так можно только отлаживаться.

## Первый запуск на сервере

Сервер: каталог `/opt/uniqbot`, свой compose-проект `uniqbot` (службы `bot-api` и `bot`), соседние проекты
на этом сервере не затрагиваются. Нужен SSH под root с ключом.

1. Подготовить: токен нового бота, `api_id` и `api_hash`, канал, бота в администраторах канала без прав на
   публикацию, свой Telegram id для `ADMIN_IDS`. Заполнить локальный `.env`.
2. Один раз вывести бота из облачного Bot API (после этого он работает только через локальный сервер):
   `.venv\Scripts\python -m uniqbot logout-cloud` — при успехе печатает «Logged out of the cloud Bot API. The bot
   can start on the local server now; it cannot go back to the cloud for 10 minutes.» и завершается с кодом 0; при
   ошибке печатает её в stderr и завершается с кодом 1. После выхода из облака для локальной отладки в облачном
   режиме нужен отдельный dev-токен (другой бот), а не боевой.
3. Скопировать `scripts/server.env.example` в `scripts/server.env` (в `.gitignore`), указать `UNIQBOT_HOST`,
   `UNIQBOT_KEY` и при необходимости `UNIQBOT_DIR`. Затем
   `powershell -ExecutionPolicy Bypass -File scripts\setup-server.ps1` — создаёт `data/` (владелец 101:101) и
   кладёт `.env` на сервер с правами 600. Повторно `.env` заменяется только с `-Force`.
4. `powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1 -Test` — собирает тестовую цель, запускает
   контейнеры, проверяет, что `uniqbot` работает и не перезапускался; в логах должно быть «Start polling».
5. Зафиксировать образ Bot API: на сервере `docker inspect --format '{{index .RepoDigests 0}}'
   aiogram/telegram-bot-api:latest`, подставить `aiogram/telegram-bot-api@sha256:…` вместо `:latest` в
   `docker-compose.yml`, закоммитить и снова `scripts\deploy.ps1`.
6. Приёмка по разделу 9 спеки.

## Деплой

```
powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1 [-Test]
```

На сервер уходит только закоммиченный код (`git archive HEAD`); при незакоммиченных изменениях скрипт
отказывает. Код распаковывается в `app.new`, с `-Test` в нём собирается тестовая цель образа (упала —
деплой остановлен до подмены), затем `app.new` становится `app`, и `docker compose up -d --build
--remove-orphans`. Через 20 секунд скрипт проверяет, что `uniqbot` запущен и его `RestartCount` не вырос,
и печатает хвост логов. Данные и `.env` лежат рядом с `app/` и деплоем не заменяются.

Запускать из Windows PowerShell без перенаправления вывода (`2>&1`, `*>`): предупреждения ssh на stderr
иначе считаются ошибкой.
