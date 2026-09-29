---
name: web-ui
description: UI design, copy, and front-end conventions for Harmonist's HTMX + Jinja2 + Tailwind UI. Consult when reviewing a screen or changing user-facing wording, actions, templates/, or static/input.css. Covers terminology, useful evidence, visual consistency, review flows, and browser verification as well as HTMX mechanics.
---

# Web UI: HTMX + Jinja2 + Tailwind

Harmonist's front end is server-rendered Jinja2 fragments swapped by HTMX, styled
with a committed Tailwind bundle. There is no framework, no client-side router,
and deliberately very little JavaScript. That buys simplicity, and it costs you
the safety net a framework would provide — these are the rules that replace it.

## Review the user's decision before the markup

Read `docs/how-it-works.md` for the workflow and `docs/design/` for the meaning of
states, matching, and tagging. Review the **whole rendered panel**, including
parent templates and asynchronously loaded fragments. Individually plausible
sentences and controls can contradict one another when assembled.

### Use one vocabulary

- MusicBrainz has **releases** and **release groups**. Don't call releases
  "editions" or interchange those two entities. Use **album** for the user's
  library item where that distinction matters.
- Reuse the same label for the same action, source, or status across Inbox,
  Library, searches, and review. Check existing shared components before
  introducing a new label or presentation. A terminology review includes
  tooltips, accessible names, loading/error messages, and related user docs.

### Every sentence must help the current decision

- **Show only the problem domain or actionable analysis.** Every line is either
  a fact about the user's music and its releases, or analysis they can act on.
  Never reassurance that the software did its job: "Nothing to add to
  MusicBrainz for this release", "No issues found", "Checked just now" beside a
  result that already says it. If a line admits it isn't actionable, delete it;
  a section with nothing to say either keeps only its controls or isn't
  rendered.
- Lead with the observed finding and the available action. State what is known
  now: "Another release may already link…" is unhelpful after the check has
  completed, and contradicts a following "No release … links to the download."
  While checking, show a loading state; afterward, replace it with the result.
- Remove prose that repeats a heading, button, visible evidence, or another
  paragraph. Keep background explanations in the usage guide. Don't add a
  disclaimer merely because an edge case exists; show relevant constraints
  when they affect this user's action.
- Don't narrate internal bookkeeping or every empty collection. "No other
  confirmed digital releases found" adds nothing beside an actionable missing
  store-link finding. Keep an empty-state explanation when it explains a missing
  result or gives the user a way forward, such as an explicit search with no hits.
- Brevity must preserve meaning: a failed or incomplete check is **not** evidence
  of absence. Retain a concise limitation and recovery action where needed;
  don't hide errors or turn uncertainty into a definitive claim.

### Actions look like actions

- Actions use the existing button styles, including workflow links such as
  **Edit store link** and **Add release**. An external action is still an
  `<a href>` styled as a button, with the established external-link cue; local
  operations use `<button>`. Navigation such as **Browse all releases** and
  ordinary inline references stay text links. Distinguish taking an action from
  browsing information; being in a workflow does not make every link an action.
  Viewing an entity is navigation: link its name (purple, with the ↗ cue) rather
  than adding a button beside it, as release rows link their titles to MusicBrainz.
- A persisted yes/no decision — *Don't warn me about this* — is a native checkbox,
  muted and placed in the heading of the statement it governs, not a prominent
  button. It shows the state as well as changing it.
- Use a short verb-led label that describes the next step. Distinguish opening
  a review or external editor from applying a change. Put the action beside
  the entity or evidence it acts on, with consistent spacing and sizing.
- Give the main next step appropriate emphasis and alternatives secondary
  styling. Subtle actions must still be recognizable and keyboard reachable;
  don't make users discover an action by hovering over ordinary-looking prose.

### Reuse presentation and show evidence where it helps

- The same choice should look and behave the same wherever it appears. Release
  pickers share candidate rows, metadata, mismatch cues, and selection controls
  (`_release_candidate.html`), rather than inventing a table for each entry point.
- Put positive evidence such as **Store URL matches** or **Barcode matches**
  beside the selection control. Prefer the reason to a vague "Suggested" badge.
  Leave absent evidence unlabelled unless it materially affects the decision;
  avoid filling every row with "Unknown" / "Doesn't match" noise. A matching
  barcode is evidence for review, not proof of release identity.
- Show the current decision once. During a selected-release review, hide
  competing search/edit/import controls and duplicate artwork sections. Preserve
  a clear Cancel/Dismiss path back to the choices, and preserve drafts on errors
  and background refreshes. Don't stack another confirmation of the same
  changes after the user has already reviewed and explicitly confirmed them.
- **The release match is settled before MB contributions (#618).** Every missing
  or conflicting datum, including a barcode-only finding, checks for a possible
  release mismatch first. **Possible mismatch** says *why* — each reason is
  evidence the download has and the matched release lacks, stated precisely
  ("your download came from X; the matched release has no link to that store"),
  never a bare "another release links a different page". **MB contributions**
  then shows one finding at a time. Once the user dismisses the warning, take
  them at their word: the contribution makes no further claims about other
  releases, and needs no browse link either. Pending, failed, or incomplete
  checks must not invite edits or a new release. Decide both sections in one
  place (`contributions.panel`), render both, and let the dismissal checkbox
  reveal one and fold the other — changing the whole panel on a click is
  disorienting. Once resolved, refresh to reveal the next applicable finding.
- Make consequences visible at the choice: for optional artwork, say what will
  be kept or replaced and which files/images are affected. Don't repeat an
  identical image just to fill both comparison columns.

### Layout should make relationships obvious

Align comparison headings, images, and their facts; place the choice beside the
image or value it controls. Check wrapping labels, long URLs/titles, missing
images, and multiple rows at wide and narrow widths. Avoid blank space left by
hidden sections or idle progress indicators. Check focus visibility, keyboard
operation, readable contrast, and accessible names; color alone must not carry
the distinction between a suggestion, warning, and ordinary candidate.

These checks come from the contribution-panel iterations (`13620a4`, `03c43c5`,
`164a515`, `5e0f607`, `8e3bb2b`), review simplification (`d329a6f`, `0b2910a`),
and artwork outcome/alignment fixes (`65662ed`, `56f7e1d`, `b90820c`, `20be68a`).
Use those as examples of recurring problems, not specifications to copy blindly.

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

## 2. Let HTMX own the event

Two different ways the browser takes an event back off HTMX. Both leave markup
that reads correctly and renders correctly, and both have shipped here.

### 2a. Never `onclick` alongside `hx-*`

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

### 2b. `hx-trigger` on a form REPLACES its default, it doesn't extend it

A `<form>` carrying `hx-get`/`hx-post` triggers on `submit` by default. Write
`hx-trigger="change"` on it and you have not *added* change — you have **dropped
submit**. Every submit event then sails past HTMX into a native navigation to the
form's own `action`.

This is nastier than it sounds, because the fallback usually *works*. The Library
page-size control (#144) is a real `<form>` so it degrades without JS, so its
`action` renders the right page anyway — the reader got the albums they asked
for, via a full page load, with the transient `?anchor=3` stranded in the address
bar where the resolved `?page=` belonged. Nothing errored. Mouse selection was
flawless. Only the keyboard path hit it.

```html
<!-- WRONG: keyboard commit escapes to a native GET on `action` -->
<form method="get" action="/" hx-get="/library" hx-trigger="change">

<!-- RIGHT: HTMX owns both ways the control can be committed -->
<form method="get" action="/" hx-get="/library" hx-trigger="change, submit">
```

Generally: any `hx-trigger` you write replaces the element's default trigger
(`submit` for forms, `change` for inputs/selects/textareas, `click` for
everything else). If you name a trigger on an element that already had a useful
one, name **both**.

`make template-lint` does not catch this — the attribute is well-formed and the
element is legal. Neither does pytest, which sees a correct-looking string. Only
a browser does.

## 3. Rebuild the CSS bundle after every template edit

`static/harmonist.css` is a committed build artifact. Run `make css` and commit
the result in the same commit as the template change. CI diffs a fresh build
against the committed bundle and fails on any drift — this is the single most
common CI break in this repo.

Two traps:

- **The bundle is a function of the `@source` globs in `static/input.css`, and
  nothing else.** That file uses `@import "tailwindcss" source(none)`, which turns
  Tailwind's automatic content detection *off* — so if you add a new place that
  emits class names (a second `.py` building HTML, a JS file, a template outside
  `templates/`), its classes are **silently not generated** and the UI renders
  unstyled with a green `make check`. Add an `@source` line for it in the same
  commit.

  This replaced the older failure mode, worth knowing because the symptom is the
  same drift error: with auto-detection on, Tailwind scanned the *whole repo* and
  minted utilities out of anything that merely read like a class name — a
  `bg-black/40` written in prose in **this very file**, the word "now-fixed" in a
  template comment, a bare `<table>` tag. Editing a `.md` could fail the CSS drift
  check (#61). If you ever see drift you can't explain from a template edit,
  check whether `source(none)` is still there before hunting further.
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
  An explicit review-and-confirm flow already supplies that confirmation;
  don't add a second prompt for the same reviewed operation.
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

So a template change is not proven by pytest. Exercise it in **demo mode**
(`HARMONIST_DEMO_MODE=1`) — "the template looks right" is not verification — and
where the change concerns event ordering or dialog lifecycle, add to the
Playwright smoke tests in `test/e2e/`.

Also inspect the rendered screen for the decision-review items above. Exercise
the relevant loading, populated, empty, incomplete, error, and selected-review
states, including Cancel and retry. Inspect both initial page load and HTMX
updates: a fragment can be correct alone and misleading inside its parent.
Check affected layouts at wide and narrow widths, and use keyboard interaction;
for browser-specific changes, verify in the affected browser. Passing request
assertions does not establish that the result is clear, aligned, or uncluttered.

The **`testing`** skill owns the rest: which rung of the ladder can see which
bug, why a browser test must be mutation-checked, and why a mutation check that
*passes* means the test is wrong (#144). Read it before writing the test.

## 7. Test-client CSRF

The middleware requires `HX-Request: true` on every state-changing request. HTMX
sends it in a browser; `TestClient` does not. New web fixtures must be built as
`TestClient(app, headers={"HX-Request": "true"})` or every POST 403s.

## Before committing a template change

Review the assembled screen first: consistent terminology; useful, nonduplicated
copy that agrees with the loaded result; recognizable and consistently styled
actions; evidence beside choices; one active review; clear consequences and a
way back; usable layout and keyboard controls at the affected widths/states.

1. `make css` run and the regenerated bundle staged.
2. `make check` green (includes `template-lint`).
3. No `onclick` on an element that also has `hx-*`.
4. Any `hx-trigger` you wrote names **every** event that must reach HTMX, not just
   the new one — it replaced the element's default rather than adding to it.
5. Side effects gated on `event.detail.successful`.
6. Exercised in demo mode; browser-layer behavior covered by `test/e2e/` if the
   bug class would be invisible to pytest.
7. User-visible? → `CHANGELOG.md` entry (see the `changelog` skill).
