# Образ для проверок wshub: ruff, shellcheck, pytest. Проверки запускаются только в контейнере — не на хосте.
# Сборка и запуск: scripts/check.sh (локально и в CI).
ARG PYTHON=3.13

FROM ghcr.io/astral-sh/uv:0.12.23 AS uv

FROM python:${PYTHON}-slim
# ripgrep — для веток grep через rg, shellcheck — для install.sh и scripts/*.sh
# hadolint ignore=DL3008
RUN if ! command -v rg >/dev/null || ! command -v shellcheck >/dev/null; then \
        apt-get update && apt-get install -y --no-install-recommends ripgrep shellcheck \
        && rm -rf /var/lib/apt/lists/*; \
    fi
COPY --from=uv /uv /uvx /usr/local/bin/

# не root: тесты с меткой nonroot (chmod) иначе пропускаются
RUN useradd --create-home --uid 1000 tester
USER 1000:1000
WORKDIR /home/tester/wshub
ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=python3 \
    UV_PROJECT_ENVIRONMENT=/home/tester/venv

# зависимости — отдельным слоем: меняются реже кода, слой кэшируется
COPY --chown=tester:tester pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/home/tester/.cache/uv,uid=1000 uv sync --frozen --no-install-project
COPY --chown=tester:tester . .
RUN --mount=type=cache,target=/home/tester/.cache/uv,uid=1000 uv sync --frozen
# окружение готово при сборке: запуск без сети и без пересинхронизации
ENV UV_NO_SYNC=1

CMD ["sh", "-c", "uv run ruff check . && shellcheck install.sh scripts/*.sh && uv run pytest -rs"]
