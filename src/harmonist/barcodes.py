"""Exact barcode evidence from existing tags; no network or persistence."""

from collections.abc import Sequence
from dataclasses import dataclass

from .formats.types import ScanFields


def normalise(value: str) -> str | None:
    """Validate a GTIN and compare its zero-padded 14-digit representation."""
    value = value.strip()
    if len(value) not in (8, 12, 13, 14) or not value.isascii() or not value.isdigit():
        return None
    if sum(int(n) * (1 if i % 2 == 0 else 3) for i, n in enumerate(reversed(value))) % 10:
        return None
    return value.zfill(14)


def variants(value: str) -> tuple[str, ...]:
    """All equivalent GTIN spellings, so a leading zero cannot hide a match."""
    canonical = normalise(value)
    if canonical is None:
        return ()
    return tuple(
        canonical[-length:]
        for length in (8, 12, 13, 14)
        if normalise(canonical[-length:]) == canonical
    )


@dataclass(frozen=True)
class Evidence:
    barcode: str


def evidence(fields: Sequence[ScanFields]) -> Evidence | None:
    """Require a consistent valid barcode on every readable file.

    Keep all barcode aliases until this decision: a BARCODE/UPC conflict within
    one file is as disqualifying as disagreement between two files.
    """
    if not fields or any(f.unreadable or not f.barcodes for f in fields):
        return None
    codes = {normalise(value) for f in fields for value in f.barcodes}
    if None in codes or len(codes) != 1:
        return None
    # Keep the original spelling for Harmony: Qobuz's literal barcode search
    # does not find a 13-digit UPC when given its equivalent padded GTIN-14.
    return Evidence(fields[0].barcodes[0].strip())
