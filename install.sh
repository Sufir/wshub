#!/bin/sh
# Установка wshub в WSL (Ubuntu): uv и ripgrep, если их нет, затем сам wshub из GitHub.
#   curl -LsSf https://raw.githubusercontent.com/Sufir/wshub/main/install.sh | sh
# Дальше: ~/.local/bin/wshub setup
#
# Переменные: WSHUB_REF — тег или ветка (по умолчанию main), WSHUB_REPO — другой репозиторий,
# WSHUB_FORCE=1 — заменить установку из рабочей копии (editable).
set -eu

REPO="${WSHUB_REPO:-https://github.com/Sufir/wshub}"
REF="${WSHUB_REF:-}"
BIN="${UV_TOOL_BIN_DIR:-${XDG_BIN_HOME:-$HOME/.local/bin}}"

say() { printf '%s\n' "$*"; }
die() { printf 'wshub install: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -ne 0 ] || die "не запускай от root (и через sudo): wshub ставится твоему пользователю"
if [ -z "${WSL_DISTRO_NAME:-}" ] && ! grep -qi microsoft /proc/sys/kernel/osrelease 2>/dev/null; then
    say "Это не WSL: поставлю, но перевалка publish работает только в WSL."
fi

# --- системные пакеты: git (нужен uv для установки из GitHub) и ripgrep (быстрый grep) ---
need=""
command -v git >/dev/null 2>&1 || need="$need git"
command -v rg >/dev/null 2>&1 || need="$need ripgrep"
command -v curl >/dev/null 2>&1 || need="$need curl"
if [ -n "$need" ]; then
    if command -v apt-get >/dev/null 2>&1; then
        say "→ ставлю:$need (sudo спросит пароль)"
        # shellcheck disable=SC2086  # список пакетов намеренно разбивается на слова
        if ! { sudo apt-get update -qq && sudo apt-get install -y -qq $need >/dev/null; }; then
            say "  не получилось — поставь сам: sudo apt install$need"
        fi
    else
        say "→ нет:$need — поставь их менеджером пакетов своей системы"
    fi
fi
command -v git >/dev/null 2>&1 || die "нужен git: sudo apt install git"

# --- uv ---
UV="$(command -v uv 2>/dev/null || true)"
[ -n "$UV" ] || { [ -x "$HOME/.local/bin/uv" ] && UV="$HOME/.local/bin/uv"; } || true
if [ -z "$UV" ]; then
    say "→ ставлю uv (установщик Python-программ, в ~/.local/bin)"
    curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
    UV="$HOME/.local/bin/uv"
    [ -x "$UV" ] || die "uv не установился — см. https://docs.astral.sh/uv/getting-started/installation/"
fi

# --- wshub ---
receipt="${UV_TOOL_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/uv/tools}/wshub/uv-receipt.toml"
if [ -f "$receipt" ] && grep -q 'editable' "$receipt" && [ "${WSHUB_FORCE:-}" != "1" ]; then
    say "wshub уже установлен из рабочей копии (editable) — не трогаю."
    say "Обновление: wshub update. Заменить установкой из GitHub: WSHUB_FORCE=1 и запусти снова."
    exit 0
fi
src="git+$REPO${REF:+@$REF}"
say "→ ставлю wshub из $src"
"$UV" tool install --force --quiet "$src" || die "uv tool install не удался"

case ":$PATH:" in
    *":$BIN:"*) next="wshub setup" ;;
    *) "$UV" tool update-shell >/dev/null 2>&1 || true
       next="$BIN/wshub setup" ;;
esac
say ""
say "Готово: $("$BIN/wshub" --version 2>/dev/null || echo wshub)"
say "Дальше: $next"
