# Harmonist design

Why Harmonist is built the way it is: the model, the principles, the major
decisions and the alternatives turned down. It does not describe what the code
does; the code does that. If a question can be answered by reading a module,
the answer doesn't belong here. If the answer is "because otherwise…", it does.

For what Harmonist is and how to use it, start with the [README](../../README.md)
and the [user docs](../how-it-works.md).

## Contents

- [Principles](principles.md): the rules every change is held to, and why each
  one exists.
- [The model](model.md): an album is a release, not a folder; state is derived;
  how albums are identified.
- [What is stored where](storage.md): the sidecar, `activity.db`, `ignores.txt`,
  and what is deliberately kept only in memory.
- [Matching](matching.md): deciding which release an album is, and which file is
  which track.
- [Bandcamp](bandcamp.md): linking purchases to albums already on disk, and the
  sync's safety rules.
- [MusicBrainz and the Cover Art Archive](external-services.md): the request
  budget, caching, merges and deletions.
- [Tagging](tagging.md): what Harmonist writes, what it leaves alone, and how it
  judges a change.
- [Artwork](artwork.md): how an album's images are chosen and written.
- [History and undo](history.md): what is recorded, and how a change is reversed.
- [Staying current](staying-current.md): noticing changes on disk and on
  MusicBrainz.
- [Contributions](contributions.md): possible mismatches and data MusicBrainz is
  missing.
- [Security](security.md): the threat model.
- [Rejected directions](rejected.md): things Harmonist deliberately doesn't do.

## Keeping this honest

A design doc rots in two ways: it describes code that has since changed, or it
grows into a description of the code. Both are avoided the same way. Record the
*reason* for a decision and the alternative it beat, in a few sentences, and
leave the mechanics to the code. Link an issue only when its history changes how
the current design should be read.
