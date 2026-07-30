---
name: web-ui
description: Front-end conventions for Harmonist's HTMX + Jinja2 + Tailwind UI. Consult BEFORE editing anything under templates/ or static/input.css — adding a button or action, building a modal/popover/disclosure, wiring hx-* attributes, or changing how a control behaves. The Python suite cannot see browser event ordering, focus, or the top layer, so the rules that keep this layer honest are collected here rather than discovered one outage at a time.
---

# Web UI: HTMX + Jinja2 + Tailwind

Harmonist's front end is server-rendered Jinja2 fragments swapped by HTMX, styled
with a committed Tailwind bundle. There is no framework, no client-side router,
and deliberately very little JavaScript. That buys simplicity, and it costs you
the safety net a framework would provide — these are the rules that replace it.

## 1. Prefer the platform to a hand-rolled equivalent

Before writing markup + JS for an interactive widget, check whether an HTML
element already *is* that widget. The native element brings focus management,
keyboard handling, the top layer, and accessibility semantics for free — all of
which a hand-rolled version has to reimplement, and usually reimplements wrong.

| Want | Use | Not |
|---|---|---|
| Modal | `<dialog>` + `showModal()` | `div.fixed.inset-0` overlay |
| Backdrop | `dialog::backdrop` | `bg-black/40` on a wrapper div |
| Dismiss on Esc | native `<dialog>` behavior | a `keydown` listener |
| Disclosure / accordion | `<details>` / `<summary>` | a click handler toggling `hidden` |
| Anchored menu / popover | the `popover` attribute | a hover-tracked absolutely-positioned div |
| Required / pattern checks | native form validation | JS validators |

This isn't only about elegance. The `<dialog>` migration didn't merely tidy the
modal — it made an entire *class* of bug unrepresentable (see rule 2), because
closing became `dialog.close()` instead of destroying the subtree. When a native
element removes a failure mode structurally, that's the strongest reason to adopt
it.

Known remaining candidate: the sync-options popover in `header.html` is still a
hover/focus-tracked div. Raise an issue before converting it — it's functional
work.

## 2. Let HTMX own the click — never `onclick` alongside `hx-*`

An inline `onclick` runs **before** HTMX's delegated click handler. If the
`onclick` detaches the element (closing a modal, re-swapping the container),
HTMX never fires its `hx-confirm` and never sends the request. The control
silently does nothing but the `onclick`. That is issue #40, and it recurred on a
second button before anyone noticed.

**The rule is mechanical and enforced:** `make template-lint` (part of
`make check`) fails on any element carrying both `onclick` and an `hx-*`
attribute. Don't work around it — restructure.

```html
<!-- WRONG: closes without ever POSTing -->
<button hx-post="/x" onclick="harmonistCloseModal()">…</button>

<!-- RIGHT: HTMX owns the click; the close is sequenced off the response -->
<button hx-post="/x" hx-disabled-elt="this"
        hx-on::after-request="if (event.detail.successful) harmonistCloseModal()">…</button>
```

`onclick` on an element with **no** `hx-*` (a pure close ×, a backdrop-click
handler on the `<dialog>` itself) is fine — that's the lint's dividing line.

Always gate the side effect on `event.detail.successful`. Closing the dialog on a
failed request throws away the error the user needed to see.

## 3. Rebuild the CSS bundle after every template edit

`static/harmonist.css` is a committed build artifact. Run `make css` and commit
the result in the same commit as the template change. CI diffs a fresh build
against the committed bundle and fails on any drift — this is the single most
common CI break in this repo.

Two traps:

- Tailwind scans the whole working tree. An **untracked directory at the repo
  root** containing HTML (a review dump, a scratch copy, an unpacked patch) will
  silently inject classes into your bundle that CI can't reproduce. If `make css`
  produces classes you didn't write, look for stray files before suspecting the
  toolchain.
- Removing the last use of a class removes its custom property too. Dropping
  `bg-black/40` for `dialog::backdrop` also dropped `--color-black` — a real diff,
  not noise.

## 4. Destructive and rare actions

- **Placement disambiguates.** A control that acts on one entity belongs beside
  that entity's badge, not in a shared button row. The old "Wrong match" button
  was ambiguous purely because of where it sat (#37/#38).
- Rare actions should be **subtle** (muted until hover), not prominent.
- Anything destructive or hard to undo gets `hx-confirm`, plus both a `title`
  and an `aria-label` — the tooltip explains, the label makes it reachable.
- Long-running actions get `hx-disabled-elt="this"` and an `hx-indicator`, so a
  multi-second re-tag can't be double-fired and doesn't look hung (#34).

## 5. Render the outcome once

A mutation that changes one album should resolve in a **single** render. Don't
mutate and then rely on the background rescan to reflect it: the rescan flips
status to `scanning`, which dims and reloads the inbox — the #11 flicker. Pair
`runner.refresh_now()` with `request.state.skip_rescan = True` instead.

## 6. Prove it in a browser

Unit tests here render templates and assert on HTML. They structurally **cannot**
see event ordering, focus, the top layer, or whether a click actually produced a
request — every one of which is where this layer's bugs live. #40 shipped with a
green suite and 91% coverage.

- Exercise the change in **demo mode** (`HARMONIST_DEMO_MODE=1`) before calling
  it done. "The template looks right" is not verification.
- If the change concerns **event ordering or dialog lifecycle**, add or extend
  the Playwright smoke test in `test/e2e/`. It's opt-in (`make e2e`, needs
  `uv pip install -e '.[e2e]'` + `playwright install chromium`), so `make check`
  stays browser-free.
- A new browser test must be **mutation-checked**: reintroduce the bug, confirm
  the test fails, restore. A smoke test that cannot fail is worse than none.

## 7. Test-client CSRF

The middleware requires `HX-Request: true` on every state-changing request. HTMX
sends it in a browser; `TestClient` does not. New web fixtures must be built as
`TestClient(app, headers={"HX-Request": "true"})` or every POST 403s.

## Before committing a template change

1. `make css` run and the regenerated bundle staged.
2. `make check` green (includes `template-lint`).
3. No `onclick` on an element that also has `hx-*`.
4. Side effects gated on `event.detail.successful`.
5. Exercised in demo mode; browser-layer behavior covered by `test/e2e/` if the
   bug class would be invisible to pytest.
6. User-visible? → `CHANGELOG.md` entry (see the `changelog` skill).
