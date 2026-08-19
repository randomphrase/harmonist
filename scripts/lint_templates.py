#!/usr/bin/env python3
"""Template lint: mechanical rules the Python suite structurally cannot see.

1. **No `onclick` alongside `hx-*` on the same element.** An inline onclick runs
   before htmx's *delegated* click handler. If the onclick mutates the DOM so the
   element is detached (e.g. closing a modal by wiping innerHTML), htmx never
   fires its confirm or request — the button silently does nothing except
   whatever the onclick did. This bit us in #40 (and a second latent instance on
   the verify-dialog Link button). The safe pattern is to let htmx own the click
   and sequence side effects declaratively (`hx-on::after-request`,
   `hx-on::before-request`, ...).

2. **Every `library_query()` call passes all four arguments.** That macro builds
   the query string naming one Library view, in seventeen places, and Jinja
   renders a missing argument as an empty Undefined rather than raising — so a
   call site left behind when the view gained a parameter drops it on that one
   control and renders perfectly. The macro exists because that failure had
   already happened by hand; this makes it impossible to reintroduce (#180).

Mechanical rules, so they live here (Makefile `check`) rather than in the
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

# A `library_query(...)` call. The arguments are plain names, `none`, and dotted
# lookups — no nested calls and no literal commas — so splitting the captured text
# on commas counts them exactly. The macro's own definition is skipped by name.
LIBRARY_QUERY_CALL = re.compile(r"\blibrary_query\(([^()]*)\)")
LIBRARY_QUERY_ARGS = 4


def _onclick_failures(rel: str, text: str) -> list[str]:
    out = []
    for match in OPEN_TAG.finditer(text):
        tag = match.group(0)
        if ONCLICK.search(tag) and HX_ATTR.search(tag):
            line = text.count("\n", 0, match.start()) + 1
            out.append(f"{rel}:{line}: element mixes onclick with hx-*")
    return out


def _library_query_failures(rel: str, text: str) -> list[str]:
    out = []
    for match in LIBRARY_QUERY_CALL.finditer(text):
        # The `{% macro library_query(...) %}` definition names the parameters; it
        # is the one call-shaped thing here that is not a call.
        if text[: match.start()].rstrip().endswith("macro"):
            continue
        count = len(match.group(1).split(","))
        if count != LIBRARY_QUERY_ARGS:
            line = text.count("\n", 0, match.start()) + 1
            out.append(f"{rel}:{line}: library_query() takes {LIBRARY_QUERY_ARGS} arguments, got {count}")
    return out


def main() -> int:
    failures: list[str] = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        rel = str(path.relative_to(TEMPLATES.parent))
        failures += _onclick_failures(rel, text)
        failures += _library_query_failures(rel, text)
    if failures:
        print("template-lint: failures")
        print("(onclick must not share an element with hx-* — it runs before htmx's")
        print(" delegated handler, see #40; library_query() must name every parameter")
        print(" of the Library view, since Jinja silently drops a missing one, see #180)\n")
        print("\n".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
