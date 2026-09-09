---
name: screenshots
description: How screenshots get into the docs — where they live, what to crop them down to, how to crop them on this machine, and how to write their alt text. Consult BEFORE adding or replacing any image in `docs/` or `README.md`, and before cropping, resizing or renaming one. The user takes the screenshots; this covers everything that happens to them afterwards.
---

# Screenshots in the docs

`docs/usage.md` describes a UI in prose, and some of its states are ones the
reader will never see for themselves — a MusicBrainz release deleted out from
under an album they own is not something you can arrange to look at. Those are
the states worth a picture. Expect more of them.

**The user takes the screenshots.** Don't mock up a UI in ASCII, don't describe
one as though it were captured, and don't generate a stand-in. Ask for the shot,
then do the rest of this.

## 1. Crop to the subject, not the page

**A screenshot that ages badly is worse than no screenshot** — it teaches the
reader something that is no longer true, and nothing about the file announces
that it went stale. So keep what the surrounding prose is about and cut what is
merely nearby, especially the chrome most likely to be redesigned: the album
header, the nav, the surrounding panels.

The `release-gone.png` shot started as the whole album page — banner, header,
cover art, path, format, both buttons, and the Tags/Tracks sections below. Only
the banner survived. Everything else was either volatile (the header) or already
covered by the paragraph in words ("Re-tag is disabled meanwhile").

Ask what the reader is meant to take away, and crop to exactly that. If the
answer is "two things", that is usually two screenshots.

## 2. Where they live

```
docs/images/<state-or-feature>.png     referenced from docs/usage.md as
                                       ![…](images/release-gone.png)
```

Name it for the **state**, not the album or the fixture that produced it —
`release-gone.png`, not `voices-from-the-lake.png`. The album is incidental and
may not be there next year.

These are committed binaries, so keep them tight. Cropping usually does it on
its own: `release-gone.png` went from 359KB to 63KB by losing the cover art.

## 3. How to crop, on this machine

There is **no ImageMagick and no Pillow in the project venv**, and installing
one into `.venv` for a one-off crop is not worth it. Use an ephemeral `uv`
environment, which touches neither the venv nor `pyproject.toml`:

```bash
uv run --with pillow --no-project python3 -c "
from PIL import Image
im = Image.open('orig.png').convert('RGB')
im.crop((left, top, right, bottom)).save('out.png')"
```

**Don't reach for `sips` for this.** It is built in and it looks like the
obvious tool, but `-c` crops from the **centre** and its `--cropOffset` is
measured from the centre too, not the top-left. `--cropOffset 0 0` silently
returns the middle of the image, and guessing the sign of the offset burns
attempts on output you then have to look at to evaluate. Two goes were wasted
this way before switching to Pillow.

**Find the bounds by measuring, not by eye.** Scan for a colour distinctive to
the subject and let it tell you the box:

```python
# the amber banner: border and fill are warm, so red leads blue by a wide margin
im = Image.open('orig.png').convert('RGB')
w, h = im.size
rows = []
for y in range(h):
    if sum(1 for x in range(0, w, 4)
           if (px := im.getpixel((x, y)))[0] > 230 and px[1] > 190
           and px[2] < 200 and px[0] - px[2] > 40) > 3:
        rows.append(y)
print(rows[0], rows[-1])          # -> 14 125
```

That gave `y=14..125, x=20..1972` in one pass. Then take an **even margin** on
all four sides — 14px there, matching the gap the page already left above the
banner — so the crop looks deliberate rather than clipped.

Always **look at the result** (read the image back) before installing it. The
measurement can be right and the crop still wrong.

## 4. Fix the file mode

Images arriving from the conversation's image cache are `-rw-------`. Committing
one at 0600 makes it unreadable to everyone else:

```bash
chmod 644 docs/images/<name>.png
```

## 5. Alt text carries the words, not a description of them

If the screenshot contains prose — a banner, an error, a label — put **that
prose in the alt text**, so a screen reader gets what a sighted reader gets
rather than a summary of it. Describe layout only for what isn't words ("a Find
a new release button sits to its right").

```markdown
![The banner shown on such an album: "This release is gone from MusicBrainz — it
was deleted there, usually because it was a duplicate. …" A Find a new release
button sits to its right.](images/release-gone.png)
```

The cost is real and worth naming: this is a **copy of the UI's copy**, so it
has to be updated when that copy changes. Grep `docs/images` usages when you
touch user-facing wording in a template.

## Done when

- [ ] cropped to the subject, with the volatile chrome gone
- [ ] bounds measured rather than eyeballed, margins even, result looked at
- [ ] `docs/images/<state>.png`, named for the state, mode 644
- [ ] alt text quotes the wording the image contains
- [ ] the prose still stands on its own if the image doesn't load
