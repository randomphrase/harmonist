# Harmonist → bandcampsync — potential upstream PRs

_Last updated: 2026‑06‑29 · DRAFT_

> Running list of bandcampsync limitations we work around (monkeypatch, subclass,
> or log‑level tuning). **Each workaround is a candidate upstream PR** — if it
> lands upstream, we delete our workaround. Heuristic: anything we monkeypatch or
> override is here.

| # | Limitation | Our workaround | PR idea |
|---|---|---|---|
| U1 | `ignores.template.txt` is hard‑coded at `/ignores.template.txt` and only exists inside bandcampsync's own Docker image, so first run **outside** Docker crashes (`[Errno 2] … '/ignores.template.txt'`). | Vendor the template (`_IGNORES_TEMPLATE`) and pre‑seed the ignores file. | Ship the template as **package data**, resolve via `importlib.resources`; or, when missing, create a blank ignores file (their own code says blank is valid). _(task #2)_ |
| U2 | Over‑eager **WARNING** logging: "Skipping item … present in the ignore file" is logged at WARNING for a perfectly **normal** already‑downloaded item (one per purchase — ~400 WARNINGs per sync). "Syncing item N of M" floods at INFO too. | We raise the `sync`/`ignores` loggers to WARNING/ERROR in `main.py` (which then hides useful move/extract INFO lines). | Lower these to **DEBUG/INFO** — already‑downloaded is not a warning. Lets consumers keep INFO without the flood. |
| U3 | De‑dup signal depends on flat side files: **with** an ignores file (our case) it's `ignores.txt` (item_id list); **without** one it's per‑album `bandcamp_item_id.txt`. Both are fragile/loseable, and `is_locally_downloaded`/`index()` key on bandcampsync's own `media_dir/band/album` path + `(band_name, item_title)`, so a differently‑organised/cased library isn't recognised. (Note: with an ignores file, bandcampsync writes **no** `bandcamp_item_id.txt` at all — `sync.py:399`.) | Considering: seed bandcampsync's in‑memory `ignores` from our `.harmonist.json` sidecars at sync start (sidecar‑backed, folder‑independent, self‑heals a lost `ignores.txt`). _(LocalMedia subclass idea dropped — premise was wrong.)_ | Allow **injecting a dedup source / `LocalMedia`** (a pluggable "is‑downloaded?(item)" callback) so consumers can answer from their own durable store instead of bandcampsync's side files. |
| U4 | File moves/overwrites during extract aren't surfaced at a usable level for auditing (they ride the silenced `sync` logger). | Planned: monkeypatch `bandcampsync.sync.move_file` to audit each `src → dst`. | Expose a **file‑operation hook/callback**, or log each move destination at a predictable, separable level. |
| U5 | `Syncer` runs the **entire sync eagerly inside `__init__`** under `auto_run=True`, so injecting collaborators (a custom `LocalMedia`) requires patching the module symbol *before* construction. | We swap `bandcampsync.sync.LocalMedia` around `super().__init__`, restoring after. | Make `auto_run=False` the clean dependency‑injection path, and/or accept injected collaborators (local_media, ignores) as constructor args. |

## Notes
- Repo is private for now; these are tracked here until we either upstream them
  or the project goes public. When we open issues/PRs upstream, link them here.
- If we end up **re‑implementing** the thin slice we use (see refactor Phase A4),
  several of these become moot — revisit before investing in PRs.
