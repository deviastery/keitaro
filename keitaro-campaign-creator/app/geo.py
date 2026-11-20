"""Работа с гео-кодами (без внешних зависимостей, легко тестируется)."""
from __future__ import annotations

import re

_SPLIT = re.compile(r"[,\s;]+")
_CODE = re.compile(r"[A-Z]{2}")


def parse_geo(value, *, strict: bool = True) -> list[str]:
    """'mx, AU;ro' или ['mx', 'au'] -> ['MX', 'AU', 'RO'].

    strict=True  — для создания кампании: мусор и пустой ввод дают ValueError.
    strict=False — мягкий режим: некорректные коды молча пропускаются.
    """
    if isinstance(value, str):
        parts = _SPLIT.split(value)
    elif isinstance(value, (list, tuple)):
        parts = [str(v) for v in value]
    elif strict:
        raise ValueError("Гео-коды укажите через запятую, например: MX,AU,RO")
    else:
        return []

    codes: list[str] = []
    for part in parts:
        code = part.strip().upper()
        if not code:
            continue
        if not _CODE.fullmatch(code):
            if strict:
                raise ValueError(f"Некорректный гео-код '{part.strip()}': нужен код из двух букв, например MX")
            continue
        if code not in codes:
            codes.append(code)

    if strict and not codes:
        raise ValueError("Укажите хотя бы один гео-код, например: MX,AU,RO")
    return codes
