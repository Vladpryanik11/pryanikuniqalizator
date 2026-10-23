FROM python:3.12-slim-trixie AS base
# lets deploy.ps1 prune only our own dangling images on a shared host; the test and runtime stages inherit it
LABEL app=uniqbot
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 LANG=C.UTF-8 TZ=Europe/Moscow
# git: the engine is installed from a pinned commit on GitHub. The build fails if Debian's ffmpeg lacks a filter or encoder the engine needs
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg git ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && ffmpeg -hide_banner -filters | grep -q zscale \
    && ffmpeg -hide_banner -encoders | grep -q libx264 \
    && ffmpeg -hide_banner -encoders | grep -q libx265
WORKDIR /app
COPY pyproject.toml ./
# Dependencies from pyproject get their own layer: a change in src/ does not reinstall them
RUN python -c "import tomllib; print(*tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies'], sep=chr(10))" \
        > /tmp/requirements.txt \
    && pip install -r /tmp/requirements.txt && rm /tmp/requirements.txt
COPY config ./config
COPY src ./src
# Editable: PROJECT_ROOT stays /app, so config/uniq.yaml is found there. uid/gid 101 are the telegram-bot-api image's:
# the files its server writes to the shared volume must be readable and deletable by the bot
RUN pip install --no-deps -e . \
    && groupadd --gid 101 uniqbot \
    && useradd --uid 101 --gid 101 --create-home --shell /usr/sbin/nologin uniqbot \
    && mkdir -p /app/data && chown 101:101 /app/data

# The full suite, engine tests included, runs as the runtime user: the POSIX branches (nice, process-group kill) run here
FROM base AS test
RUN pip install -e ".[dev]"
COPY .env.example ./
COPY tests ./tests
USER 101:101
RUN python -m pytest -q -p no:cacheprovider

FROM base AS runtime
USER 101:101
CMD ["python", "-m", "uniqbot", "bot"]
