# PR #862: artist browsing and local NFO metadata

This document responds to the review of [PR #862](https://github.com/randomphrase/harmonist/pull/862) and summarizes the changes prepared for the updated contribution. The feature remains part of [issue #813](https://github.com/randomphrase/harmonist/issues/813).

## Responses to the review

### 1. A standalone Artists page and artist-specific filters

Artists now has its own `/artists` page, search, pagination and filters. It no longer shares the Library's album filters or lives inside its tab panel. The former `/?tab=artists` address redirects to the standalone page.

The available filters are:

| Filter | Choices or meaning |
| --- | --- |
| NFO | With NFO, without NFO, or conflicting NFO files. |
| Artwork | With or without a resolved local artist picture. |
| New | At least one of the artist's Library albums was added during the last seven days. |

Search and filters compose, survive reloads, and travel through the artist-to-album return links. The main navigation also shows the global artist count, alongside the Inbox and Library counts. This count comes from the existing consolidated status response and remains global while the visible results are filtered.

Artist detail pages continue to use the existing Library album tiles, including their warnings, artwork indicators and pagination.

### 2. Stable MBID identity and albums without artist IDs

Artist pages are addressed as `/artist/{mbid}`. Album-scoped `local-…` identities and the “Unidentified credit” badge have been removed. Names and directory spelling cannot establish identity or create an artist page.

The Artists page lists albums with missing artist IDs, including a count and links to their album pages. This also includes albums where only some tracks are missing the IDs. Existing aligned IDs may still establish the artist for those partially tagged albums when every track names the same release and the artist names agree. Conflicting IDs do not qualify for that association.

Collaborations appear under each identified artist. Homonymous artists remain separate because their MBIDs differ. Matching an album therefore does not replace a temporary artist bookmark with a different identity.

### 3. Discover NFOs throughout the library and associate them by MBID

The album scanner now collects `artist.nfo` files during the same filesystem walk, including directories that contain no audio. An NFO may be in an artist directory, an album directory, or a separate metadata directory anywhere within `music_dir`.

The MusicBrainz artist ID inside the XML establishes its association with a scanned artist. File location and the NFO's display name do not override the tag-derived identity. The old Artist Information Folder setting, environment override and settings display have been removed from this read-only feature.

The in-memory `Profile` retains the NFO path, MBID, declared name, biography and other metadata. The artist detail model carries its matching NFO paths alongside the tag-derived artist name and MBID. No new artist database, sidecar fields or on-disk identity mappings are introduced.

### 4. Orphans, missing MBIDs and incomplete scans remain visible

The Artists page reports NFOs with no artist MBID and NFOs whose MBID is not credited on any scanned album. It provides a count and the file paths, rather than silently dropping them.

Orphan checks use every scanned album, including the Inbox. An NFO is consequently not declared orphaned just because that artist's albums have not reached the Library yet.

Malformed or unreadable NFOs are reported separately as read errors. An incomplete directory walk cannot establish absence or uniqueness, so associations are withheld and the scan failure is reported instead of producing misleading missing-file results.

### 5. Duplicate MBIDs are conflicts, without a source-selection URL

Two or more NFOs containing the same artist MBID are presented as a conflict, with all matching file paths. Harmonist does not silently choose a file or expose a selected source in the page URL. Conflicted sources do not supply a chosen biography or artist picture.

Removing or correcting a duplicate file resolves the conflict through the normal watcher/rescan flow.

### 6. Automatic refresh and image revalidation

The artist “Read again” action has been removed. NFO and image changes refresh the in-memory inventory after filesystem activity settles, and the hourly full rescan covers missed notifications and directory changes. Open Artists and artist detail pages follow the resulting status changes automatically.

Image additions, replacements and deletions can change the selected picture without editing or reparsing an unchanged NFO. A late targeted refresh cannot overwrite a newer completed scan. Changes to audio credits also refresh the directory evidence used by image selection.

The image route uses the existing revalidation pattern: it sends a file-derived ETag with revalidation headers, and a matching conditional request receives `304 Not Modified`. Rendered image URLs include a stable version of the observed picture. This changes only when the observed file or selection changes, preventing a browser from keeping an old decoded image after a page refresh without issuing an HTTP request.

### 7. Prefer the declared thumbnail and avoid album-cover fallbacks

`Profile.image_ref` preserves the NFO's declared `<thumb>` value, while `Profile.image_path` stores the resolved local picture path in memory. The observed file fingerprint is retained alongside it so image changes are detectable independently of XML changes.

Selection proceeds in this order:

1. A valid local image explicitly referenced by `<thumb>`.
2. Explicit `artist.jpg`, `artist.png`, `artist.jpeg` or `artist.webp` files beside the NFO.
3. Generic `thumb.*` and then `folder.*` files only in a verified artist directory.

Relative thumbnail references are resolved from the NFO directory. Absolute paths and parent-relative paths are accepted only when the resolved image remains inside the music library. Remote URLs and paths escaping the library, including symlink escapes, are not served or downloaded.

Generic filenames require positive evidence: all descendant albums must credit the NFO's artist MBID, and the NFO directory must not be an audio directory, an album-metadata directory or an identified disc container. Merely finding no audio directly in a directory is insufficient. Ambiguous layouts therefore do not acquire an automatic `folder.jpg`/`folder.png` artist picture. An explicit `<thumb>` remains the owner's declared choice and may deliberately refer to such a file.

### 8. Exclude MusicBrainz's special-purpose artists

The index excludes the MBIDs of MusicBrainz's [special-purpose artists](https://musicbrainz.org/doc/Style/Unknown_and_untitled/Special_purpose_artist), including Various Artists, unknown, anonymous, data, dialogue, no artist, traditional and the test artist. They do not get artist pages aggregating compilations. Identified collaborators on the same albums remain independently browsable.

## Additional navigation and presentation changes

Artist names in album-page headings and Library cards now link directly to their MBID-based artist pages. Collaborators have separate links; credits without a usable artist page remain text. Library cards use separate native links for the album and artist, avoiding nested anchors. Artist names remain gray, with dotted underlines, and the Library links use the same orange hover color as “Back to library”. Cover and title links continue to open the album.

Artist detail presentation follows the existing local implementation as a visual reference while retaining the current read-only data model:

- The NFO summary shows its path, modification time, primary facts and separate genre labels.
- Biography is a native collapsible section.
- Albums are the scanned Library albums actually available for that artist.
- Additional information separates disambiguation, years active, style, mood and instruments from primary facts.
- Discography from the NFO is a separate collapsible list of titles and years. A valid release-group MBID provides a MusicBrainz link; an invalid or absent ID leaves plain text. These entries never become owned albums or affect Library counts.

Biography and discography start collapsed. Opening or closing them works with the keyboard, and automatic page refreshes preserve their disclosure state. Conflicts and read failures remain visible rather than being replaced by an apparently successful empty profile.

Artist history is deferred. This contribution does not add artist editing, metadata importing, NFO writing or an artist history store. Browsing remains offline and read-only: it adds no MusicBrainz/image-provider calls and changes no audio tags, sidecars, NFOs or music directories.

## Validation

The contribution was rebased onto upstream `f6c19ce` before the final validation.

- `make css` regenerates the committed Tailwind artifact after template changes.
- `make check` passed with exit code 0: 3,316 tests passed and 283 were skipped.
- `E2E_LATENCY_MS=300 make e2e` passed with exit code 0: 264 tests passed and four were skipped, using Chromium and WebKit with four workers.
- Tests cover stable identities, special-purpose exclusions, collaborations, partial/missing IDs, global discovery, conflicts, orphan checks across Inbox and Library, incomplete scans, XML safety, thumbnail priority, directory evidence, path containment, image-only updates, stale-refresh protection, conditional `304` responses, filters, navigation, keyboard operation and responsive layouts.
- Browser mutation checks verify that removing artist links or disclosure-state preservation causes the relevant test to fail. Image refresh tests also detected and drove the fix for stale decoded browser images.
- A test synchronization correction waits for the artwork viewer's existing “fit to viewer” state before checking its geometry; its size assertions remain intact and were mutation-checked against an incorrect fit.

Personal editor configuration, local runtime data and the other local development branch are outside this contribution.
