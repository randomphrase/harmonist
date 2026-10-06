"""Read disk usage without counting nested stores or hiding unreadable paths."""

from __future__ import annotations

import logging
import stat
from pathlib import Path

log = logging.getLogger(__name__)


def file_bytes(root: Path | None) -> int | None:
    """Regular-file bytes directly in a store; None means usage is unknown."""
    if root is None:
        return 0
    try:
        entries = list(root.iterdir())
    except FileNotFoundError:
        return 0  # Stores are created on the first write.
    except OSError:
        log.exception("could not measure storage usage at %s", root)
        return None
    total = 0
    for entry in entries:
        try:
            info = entry.stat()
        except FileNotFoundError:
            continue  # Another request retired or evicted it after the listing.
        except OSError:
            log.exception("could not measure storage usage at %s", entry)
            return None  # A partial total would understate disk use.
        if stat.S_ISREG(info.st_mode):
            total += info.st_size
    return total
