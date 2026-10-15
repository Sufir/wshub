"""GitLab: PRIVATE-TOKEN, /api/v4, адрес API задаётся в реестре (self-hosted)."""
from __future__ import annotations

from . import Preset, floor, writes

PRESET = Preset(
    name="gitlab",
    default_url=None,
    headers=lambda token: {"PRIVATE-TOKEN": token},
    prefix="/api/v4",
    floor=floor(
        # мерж и одобрение — действие оператора
        (r"/merge_requests/[^/]+/merge(/|$)", "мерж MR"),
        (r"/merge_requests/[^/]+/(approve|unapprove)(/|$)", "одобрение MR"),
        (r"/merge_requests/[^/]+/rebase(/|$)", "rebase MR пишет в ветку"),  # сверх ТЗ
        # запись в репозиторий — только wshub push по push_protect
        (r"/repository/(commits|files|branches|tags)(/|$)", "запись в репозиторий"),
        (r"/repository(/|$)", "запись в репозиторий"),  # сверх ТЗ: submodules, changelog и прочее под /repository
        (r"/protected_(branches|tags)(/|$)", "защита веток и тегов"),
        # сверх ТЗ, ревью WSH-3.4: мерж поездом, правила одобрения, тег через релиз, выпуск учётных данных
        (r"/merge_trains(/|$)", "мерж через merge train"),
        (r"/(approvals|approval_rules)(/|$)", "правила одобрения"),
        (r"/releases(/|$)", "релиз создаёт тег"),
        (r"/(personal_access_tokens|access_tokens|deploy_tokens|deploy_keys)(/|$)", "выпуск учётных данных"),
        (r"^/api/v4/user/(keys|gpg_keys)(/|$)", "выпуск учётных данных"),
    ),
    # проект — сырым сегментом: числом или закодированным путём group%2Frepo
    writes=writes(
        r"/api/v4/projects/[^/]+/",
        ("POST", "merge_requests"),
        ("PUT", "merge_requests/<n>"),
        ("POST", "merge_requests/<n>/notes"),
        ("POST", "merge_requests/<n>/discussions"),
        ("POST", "merge_requests/<n>/discussions/[0-9a-f]+/notes"),
        ("POST", "issues"),
        ("PUT", "issues/<n>"),
        ("POST", "issues/<n>/notes"),
    ),
    secret_reads=floor(
        (r"/(variables|hooks|access_tokens|deploy_tokens)(/|$)", "секреты: CI-переменные, webhooks, токены"),
    ),
)
