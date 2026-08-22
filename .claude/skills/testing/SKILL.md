---
name: testing
description: How Harmonist proves a change works — which rung of the ladder can actually see the bug, writing the failing test before the fix, mutation-checking the tests you wrote after it, and what makes a test worth keeping. Consult BEFORE writing or changing any test, before fixing a bug (the reproduction comes first), and before calling an implementation proven. Every escape this repo has paid for shipped with a green suite; the rules that make a suite mean something are collected here.
---

# Testing: what would actually prove this?

Harmonist has twelve hundred-odd tests. Every significant defect it has shipped
went out with that suite green:

- **#40** — a button carrying both `onclick` and `hx-*` silently stopped POSTing.
  Coverage was 91%. Unit tests render templates and assert on HTML; they cannot
  see that a click produced no request. It recurred on a second button before
  anyone noticed.
- **#144** — the fix's first browser test drove the bug with a keyboard `Enter`
  and stayed green **with the bug reintroduced**. The test was wrong, not the fix.
- **#47** — a bulk regex swapped the severity of four events. All 623 tests
  passed; the user caught it reading the diff.
- **KNOWN_GAPS** — a test asserted for months that Harmonist doesn't write
  `DISCSUBTITLE` while the tagger was writing it, because the fixture release
  happened not to exercise the field.

None of that is a coverage problem. The question a test has to answer is not
*"did this line run?"* but *"could this have failed?"*

## 1. Name the rung that can see the bug, before writing the test

Four rungs, and they are not interchangeable:

| Rung | Where | Sees | Blind to |
|---|---|---|---|
| Pure logic | `test_scanner.py`, `test_tagger*.py`, `test_reconcile.py`, `test_picard_spec.py` … | states, tag writes, sidecar shape, matching | anything HTTP or browser |
| Web routes | `test_web.py` via `TestClient` | status codes, rendered HTML, side effects on disk | event ordering, focus, the top layer, whether a control fired |
| Browser | `test/e2e/` (Playwright) | the above blind spots, for real | anything not scripted; it's a smoke layer |
| By hand | demo mode (`HARMONIST_DEMO_MODE=1`) | a whole flow, as a user meets it | nothing — but it proves nothing tomorrow |

The failure mode is testing one rung *below* the bug, because that rung is the
easy one to reach. #40's dead button had tests — of the rendered markup, which was
correct. If your change is to a template, an `hx-*` attribute, or anything about
*when* a request happens, the web-route rung cannot prove it and a passing test
there is not evidence.

Going up a rung is expensive, so going up is a decision, not a default: state
which rung proves this change and why the one below it can't.

## 2. The red test is the reproduction

`issue-first` already requires reproducing a bug before filing it — "a good issue
names the cause, not just the symptom". Promote that reproduction into a test and
the work orders itself:

1. Write the test that fails because the bug exists.
2. **Run it, and read the failure.** It must fail on its assertion, for the
   reason you predicted. A test that fails on an import error, a typo'd fixture,
   or a 404 is not a reproduction — it's a broken test that will go green for the
   wrong reason.
3. Fix. The same test going green is the evidence.

For a bug fix on the pure-logic or web-route rung, this is the expected order and
it is cheap. Skipping it and writing the test afterwards is how you get a test
that asserts what the code now does rather than what was wrong with it — those
pass identically before and after the fix.

Where red-first is genuinely not available — the bug class pytest structurally
cannot see, most template work — the obligation does not disappear. It becomes
rule 3.

## 3. Mutation-check anything written after the fix

Reintroduce the bug, run the test, confirm it fails, restore. Every browser test
must be mutation-checked; so must any test written after the code it covers.

**A mutation check that passes means your test is wrong, not that the fix was
unnecessary.** That is #144's lesson and it is counter-intuitive enough to be
worth restating each time: the check's job is to make the test fail. If it can't,
the test is not testing the thing.

Two ways the check itself lies:

- **You drove an event the engine didn't synthesise.** #144's `Enter` on a
  `<select>` did nothing at all in Chrome. Prefer raising the event directly
  (`requestSubmit()`, `element.click()`) over depending on a browser's rules for
  turning an input into one.
- **You asserted on a string that appears somewhere else in the markup.** The
  `checked` assertion for #227 nearly passed for free, because the checkbox
  carries `hx-on::after-request="… this.checked = !this.checked"` and the word
  was in the attribute *value*. Blank the values before matching:
  `re.sub(r'"[^"]*"', '""', tag)`. Assert on structure, not on a substring that
  could occur in a comment, a handler, or a tooltip.

## 4. Assert what the code does, not what it stopped doing

A test that certifies a removal — "the old button's label is absent" — can never
fail for a good reason. It pins a diff, and it goes stale silently.

Assert an absence only when a **live code path could produce the thing**. The
reliable tell: the same string is asserted *present* somewhere else under
different conditions.

```python
# Earns its run: the same control IS asserted present on a suggestion card.
assert "Confirm &amp; Tag" not in surrender_card_html

# Doesn't: nothing in the codebase can emit this any more.
assert "Recover store URL" not in r.text  # URL recovery is automatic now
```

Corollary for "we don't support X yet" lists: they either duplicate the
exhaustive positive check (and so fail second, never first) or drift away from
the code, as `KNOWN_GAPS` did. A roadmap goes in a **comment beside the check
that would actually notice**, never in an assertion.

## 5. Fixtures: self-contained, and never the real library

- `tmp_path` plus `test/fixtures/sine.m4a`; build state through the helpers
  (`test/helpers.py`'s `write_track_totals`, the `_make_*_album` builders in
  `test_web.py`) rather than by hand, so a fixture means the same thing
  everywhere.
- **Never read the dogfood library** (`/Volumes/media/music`). It is read-only,
  slow, and mounted on one machine — a test that touches it is not reproducible
  and cannot run in CI.
- `TestClient` must be built as `TestClient(app, headers={"HX-Request": "true"})`
  or every state-changing request 403s on the CSRF middleware.
- Demo mode sandboxes a seeded library under `$TMPDIR`; the configured
  `music_dir` is never touched. Use it for by-hand verification, and remember
  a stale `uvicorn` on the port will serve you yesterday's code — check the log
  says `Application startup complete`, not `address already in use`.

## 6. e2e: the rung nothing runs for you

`test/e2e/` holds ~20 Playwright tests across six files, each gated on
`RUN_E2E=1`. Setup is `uv pip install -e '.[e2e]'` + `playwright install
chromium`; run them with `make e2e`.

**CI does not run them.** The workflow runs `pytest test/ -q`, which collects
them and skips every one — so the only layer that can see the #40 bug class is
the only layer with nothing running it automatically. Until that changes, running
`make e2e` yourself is the whole of the coverage:

- after any change under `templates/` that alters *behaviour* (a trigger, a
  handler, a control's lifecycle) rather than wording or styling;
- before cutting a release.

Keep it a smoke layer. It exists to prove the wiring is live — that a click
produces a request, that a dialog closes, that focus lands — not to re-assert
what the Python suite already covers about the response.

## 7. When not to add a test

- To reach a coverage number. Coverage measures what ran, and every escape above
  ran fine.
- To restate a literal from the source (`AlbumState.NEW.value == "new"`, one
  entry of a mapping dict). If the constant is a contract, assert the **whole
  set**, so a rename can't slip through the gap between the items you listed.
- To cover a branch a neighbouring test already covers with a stricter assertion.
  Two tests of one path is one test and one maintenance cost.

A test that cannot fail is worse than no test: it costs a run forever, and it
tells the next reader the behaviour is covered when it isn't.

## Before you call it proven

1. Which rung sees this change, and can the one below it really not?
2. If it's a bug fix: did a test fail first, for the right reason?
3. If the test came after the code: has the mutation check been run, and did it
   go **red**?
4. Every new assertion states what the code does; any absence has a live path
   that could produce it.
5. Fixtures are self-contained — `tmp_path`, no real library, `HX-Request` on the
   test client.
6. Template behaviour changed? `make e2e` run locally, and the change exercised
   by hand in demo mode.
7. `make check` green — necessary, never sufficient.

## Why this exists

Because the suite is not the point. Four times now the suite has been green while
the thing it was meant to protect was broken, and each time the gap was the same
shape: a test that could not have failed. This skill is the set of questions that
make it possible for one to.
