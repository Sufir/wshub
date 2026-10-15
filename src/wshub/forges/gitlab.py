"""GitLab: PRIVATE-TOKEN, /api/v4, адрес API задаётся в реестре (self-hosted)."""
from __future__ import annotations

from . import Preset, floor

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
)
