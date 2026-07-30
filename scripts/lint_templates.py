#!/usr/bin/env python3
"""Template lint: forbid `onclick` alongside `hx-*` on the same element.

Why: an inline onclick runs before htmx's *delegated* click handler. If the
onclick mutates the DOM so the element is detached (e.g. closing a modal by
wiping innerHTML), htmx never fires its confirm or request — the button
silently does nothing except whatever the onclick did. This bit us in #40
(and a second latent instance on the verify-dialog Link button). The safe
pattern is to let htmx own the click and sequence side effects declaratively
(`hx-on::after-request`, `hx-on::before-request`, ...).

Mechanical rule, so it lives here (Makefile `check`) rather than in the
review-gate skill — see CLAUDE.md "Working conventions".

Exit 0 when clean; exit 1 listing template:line for each offending element.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"

# An open tag, possibly spanning lines. Jinja `{{ ... }}` inside attribute
# values can contain `>` only via filters we don't use; good enough for lint.
OPEN_TAG = re.compile(r"<[a-zA-Z][^>]*>", re.DOTALL)
ONCLICK = re.compile(r"\bonclick\s*=", re.IGNORECASE)
HX_ATTR = re.compile(r"\bhx-[a-z:.-]+\s*=", re.IGNORECASE)


def main() -> int:
    failures: list[str] = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        for match in OPEN_TAG.finditer(text):
            tag = match.group(0)
            if ONCLICK.search(tag) and HX_ATTR.search(tag):
                line = text.count("\n", 0, match.start()) + 1
                rel = path.relative_to(TEMPLATES.parent)
                failures.append(f"{rel}:{line}: element mixes onclick with hx-*")
    if failures:
        print("template-lint: onclick must not share an element with hx-* attributes")
        print("(onclick runs before htmx's delegated handler — see #40)\n")
        print("\n".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
