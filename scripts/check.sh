#!/bin/sh
# Все проверки wshub — только в Docker: ruff, shellcheck, pytest. На хосте ничего не ставится и не запускается.
#   scripts/check.sh                   все проверки, Python 3.13
#   scripts/check.sh --python 3.10     другая версия Python (3.10–3.14)
#   scripts/check.sh -- -k setup -x    только pytest со своими аргументами
#   scripts/check.sh --wsl             WSL-тесты: профили Windows (/mnt/c/Users) подключаются только для чтения
#   --no-build                         не собирать образ (CI собирает его сам, с кэшем)
set -eu
cd "$(dirname "$0")/.."

py=3.13
wsl=0
build=1
while [ $# -gt 0 ]; do
    case "$1" in
        --python) [ $# -ge 2 ] || { echo "check.sh: после --python нужна версия" >&2; exit 2; }
                  py="$2"; shift 2 ;;
        --wsl) wsl=1; shift ;;
        --no-build) build=0; shift ;;
        --) shift; break ;;
        -h|--help) sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "check.sh: неизвестный параметр $1 (см. --help)" >&2; exit 2 ;;
    esac
done

command -v docker >/dev/null 2>&1 || {
    echo "check.sh: нужен Docker (Docker Desktop с интеграцией WSL или docker-ce в WSL)" >&2
    exit 1
}
image="wshub-check:py$py"
if [ "$build" = 1 ]; then
    echo "→ образ $image (первая сборка — несколько минут, дальше — из кэша)" >&2
    docker build --quiet --file docker/check.Dockerfile --build-arg PYTHON="$py" --tag "$image" . >/dev/null
fi

# сеть контейнеру не нужна: зависимости уже в образе
if [ "$wsl" = 1 ]; then
    if [ -z "${WSL_DISTRO_NAME:-}" ] || [ ! -d /mnt/c/Users ]; then
        echo "check.sh --wsl: запускай в WSL (нужны WSL_DISTRO_NAME и /mnt/c/Users)" >&2
        exit 2
    fi
    exec docker run --rm --network none -e WSL_DISTRO_NAME="$WSL_DISTRO_NAME" \
        -v /mnt/c/Users:/mnt/c/Users:ro "$image" uv run pytest -rs -m wsl "$@"
fi
if [ $# -gt 0 ]; then
    exec docker run --rm --network none "$image" uv run pytest "$@"
fi
exec docker run --rm --network none "$image"
