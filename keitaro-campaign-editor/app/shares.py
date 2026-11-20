"""Чистая логика пересчёта процентов (share) офферов с учётом закрепления (pin)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class OfferShareItem:
    offer_id: int
    share: int
    is_pinned: bool = False
    is_removed: bool = False


def recalculate_shares(items: list[OfferShareItem]) -> list[OfferShareItem]:
    """Пересчитывает доли офферов в потоке:
    - у удалённых (is_removed=True) всегда share=0 и is_pinned=False;
    - у закреплённых активных (is_pinned=True) доля не меняется;
    - остаток (100 - сумма закреплённых) равномерно делится между незакреплёнными
      активными офферами; остаток от деления (+1%) добавляется первым элементам.
    """
    result = [
        OfferShareItem(
            offer_id=it.offer_id,
            share=0 if it.is_removed else max(0, int(it.share)),
            is_pinned=False if it.is_removed else bool(it.is_pinned),
            is_removed=bool(it.is_removed),
        )
        for it in items
    ]

    active = [it for it in result if not it.is_removed]
    pinned = [it for it in active if it.is_pinned]
    unpinned = [it for it in active if not it.is_pinned]

    pinned_sum = sum(it.share for it in pinned)
    remaining = max(0, 100 - pinned_sum)

    if unpinned:
        base, rem = divmod(remaining, len(unpinned))
        for idx, it in enumerate(unpinned):
            it.share = base + (1 if idx < rem else 0)

    return result


def validate_active_shares(items: list[OfferShareItem]) -> None:
    """Проверяет, что при наличии активных офферов сумма их долей равна 100%."""
    active = [it for it in items if not it.is_removed]
    if not active:
        return
    for it in active:
        if it.share < 0 or it.share > 100:
            raise ValueError(f"Некорректная доля {it.share}% для оффера #{it.offer_id}")
    total = sum(it.share for it in active)
    if total != 100:
        raise ValueError(f"Сумма долей активных офферов должна быть 100% (сейчас {total}%)")
