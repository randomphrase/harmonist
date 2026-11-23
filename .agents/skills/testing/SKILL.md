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

## 6. e2e: the browser rung

`test/e2e/` holds the Playwright tests, each gated on `RUN_E2E=1` so `make
check` stays browser-free. Use the checked-in devcontainer and its `/opt/venv`
environment; [development setup](../../../docs/development.md) covers rebuilding
it and installing the locked browser assets. After dependency changes, reinstall
`.[dev,e2e]` and run `playwright install --with-deps chromium webkit` inside that
container. Verify the imported checkout and installed versions before comparing
runs: an old browser build or missing `static/vendor/` is a different environment.
Run with `make e2e`. Use `browser_factory` and a fresh `browser.new_page(...)`
context for each test. The fixture shares processes, closes contexts after failures,
and preserves traces; opening another `sync_playwright()` session conflicts with
the worker's existing driver. Its browsers are offline: a request for anything but
`127.0.0.1` fails at once (#1028), so a page waits only on the server under test.
A test that needs an outside answer fulfils it with `page.route`. The web fonts
are Harmonist's own (#1029), so text is drawn in Inter, as users see it.

CI runs them in its own `e2e` job, four workers wide (`-n 4 --dist
loadscope`), so a red browser test there is a real failure, not noise to rerun
past — and a test that only fails in parallel is usually a race the slower
machine would hit too. The job keeps a Playwright trace of each
failing test in its `e2e-traces` artifact; `E2E_TRACE_DIR=test-results make e2e`
does the same locally, and `playwright show-trace` opens one. Still run `make e2e` yourself after any change
under `templates/` that alters *behaviour* (a trigger, a handler, a control's
lifecycle) rather than wording or styling: CI tells you after the push, and the
full run takes minutes you'd rather spend before it.

**A local green says nothing about a race.** A dev machine answers a fragment
in a few milliseconds; CI takes 100–400ms. A test that acts before the previous
response has landed — opens the next menu, makes the next choice, counts a held
request — passes locally every time and fails on CI now and then. Four such
failures in a row (#823, #824, #828, #832) were each "fixed" for the one
interleaving their trace showed, verified green locally, and failed again on CI
somewhere else. `E2E_LATENCY_MS=300 make e2e` holds every htmx request on the
e2e servers for 300ms first (`test/e2e/latency_app.py`, #834); run the suite
that way to reproduce a CI race, and again to call one fixed. It found three
of these tests in one run. Some races also need the parallel load, so run it
four wide as CI does (`pytest test/e2e -n 4 --dist loadscope`), and more than
once.

Keep it a smoke layer. It exists to prove the wiring is live — that a click
produces a request, that a dialog closes, that focus lands — not to re-assert
what the Python suite already covers about the response.

## 7. No sleeps for synchronization

**Do not use `time.sleep`, `asyncio.sleep`, or `page.wait_for_timeout` to make a
test ready to continue.** A fixed delay proves only that time passed. Shortening
it makes the race easier to hit; lengthening it makes every passing run pay for
one machine's worst case. Existing sleeps are debt, not examples to copy.

Choose the evidence the next assertion actually needs:

- **Requests and navigation:** arm `expect_request`, `expect_response`, or
  `expect_navigation` before the action that triggers it. A response arriving
  does not mean HTMX has swapped or wired the fragment. Follow it with a
  retrying assertion on the resulting DOM or an observed, scoped HTMX lifecycle
  event. If the expected value was already present, it cannot prove completion;
  track the specific replacement or event instead. `htmx_ready` scopes control
  readiness after a swap; unrelated polling must not hold up an action.
- **UI state:** use Playwright's retrying `expect(locator)` assertions or a
  bounded `wait_for_function` for the actual condition. Wait for image loading,
  layout, or focus where that is what the assertion needs. A blanket
  `networkidle` wait is a poor readiness signal on a page that polls continuously.
- **Races:** hold a request or response with routing, observe that it reached
  the barrier, perform the competing action, then release it. Exercise the
  relevant response orders explicitly. Do not guess when a request is in flight.
- **Timers and polling:** install Playwright's clock before navigation and
  advance it deliberately. Use `run_for` when intermediate callbacks matter;
  `fast_forward` can coalesce timers. Clock advancement does not complete HTTP
  requests: wait for the resulting request and DOM effect as well. A test that
  asserts a poll did not destroy something needs a control proving that the
  poll was armed and could otherwise destroy it.
- **Python workers and fixtures:** use an event, barrier, future, or a bounded
  readiness-condition wait. Every wait needs a deadline and a useful failure;
  an unbounded loop or an unsignalled `Event.wait(delay)` merely disguises the
  sleep. Poll a read-only condition, not a mutation: even a rejected POST can
  schedule work and keep the fixture busy forever. A readiness check must observe
  the state being waited for, not assume it after a grace period.

For a negative assertion (no navigation, duplicate write, or DOM replacement),
first observe the completion of the action that could cause it. An immediately
passing absence assertion is not a substitute for the old sleep. Mutation-check
these replacements so a missing event or unchanged DOM cannot make them vacuous.

Deliberate latency in `test/e2e/latency_app.py` is a simulated service condition,
not synchronization. Keep that fault injection; the test still waits on observable
completion, including when the response takes longer than the injected delay.

## 8. When not to add a test

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
7. Waits observe a condition or event, with a deadline; timer tests use a controlled
   clock. No fixed sleeps stand in for completion.
8. `make check` green — necessary, never sufficient.

## Why this exists

Because the suite is not the point. Four times now the suite has been green while
the thing it was meant to protect was broken, and each time the gap was the same
shape: a test that could not have failed. This skill is the set of questions that
make it possible for one to.
