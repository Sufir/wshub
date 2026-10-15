"""GitHub: Bearer-токен, путь от адреса API (по умолчанию https://api.github.com)."""
from __future__ import annotations

from . import Preset, floor

PRESET = Preset(
    name="github",
    default_url="https://api.github.com",
    headers=lambda token: {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    prefix="",
    floor=floor(
        # мерж и одобрение — действие оператора; комментарии к строкам — pulls/<n>/comments, issues/<n>/comments
        (r"/pulls/[^/]+/merge(/|$)", "мерж PR"),
        (r"/merges(/|$)", "мерж веток"),
        (r"/pulls/[^/]+/reviews(/|$)", "одобрение PR (review)"),
        (r"/pulls/[^/]+/update-branch(/|$)", "update-branch пишет в ветку PR"),  # сверх ТЗ
        # запись в репозиторий — только wshub push по push_protect
        (r"/contents(/|$)", "запись в репозиторий"),
        (r"/git(/|$)", "запись в репозиторий"),
        (r"/branches/.+", "защита и переименование веток"),  # сверх ТЗ
        (r"/rulesets(/|$)", "правила веток"),  # сверх ТЗ
        (r"^/graphql(/|$)", "GraphQL: мутации мимо пола"),  # сверх ТЗ
        # сверх ТЗ, ревью WSH-3.4: запись в ветку форка, тег через релиз, выпуск учётных данных
        (r"/merge-upstream(/|$)", "merge-upstream пишет в ветку"),
        (r"/releases(/|$)", "релиз создаёт тег"),
        (r"/keys(/|$)", "выпуск учётных данных"),
        (r"/gpg_keys(/|$)", "выпуск учётных данных"),
    ),
)
