"""Optional MusicBrainz contributions, derived separately from tag updates.

Release observations can be shared, but eligibility and conclusions belong to
one local copy. A purchase link alone says nothing about where its files came
from. No function here writes music, sidecars, or persisted status flags.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from . import barcodes, mb_cache, mb_lookup, provenance
from .models import Album, Release

BarcodeStatus = Literal["missing", "barcode_free", "different", "same"]


@dataclass(frozen=True)
class Observation:
    mbid: str
    formats: tuple[str, ...]
    urls: frozenset[str]
    checked_at: datetime | None
    # None is unknown; an explicitly empty MB barcode means barcode-free.
    barcode: str | None = None
    # The last stored browse of the release's group (#618), and how many
    # releases it said the group has. None when it was never browsed.
    group: tuple[Release, ...] | None = None
    group_total: int = 0
    # Each local copy's classification of `group`, keyed by the evidence it was
    # classified against. The Library assesses every album on every render, and
    # classifying a hundred-release group each time is the cost worth keeping.
    # Not an init field, so `replace()` starts a new observation with none.
    checks: dict[tuple[object, ...], SiblingCheck] = field(
        default_factory=dict, init=False, compare=False, repr=False
    )


@dataclass(frozen=True)
class Assessment:
    eligible: bool = False
    # The files are a CD rip rather than a download (#633): their evidence is
    # the ripper's UPC alone, it points at physical releases rather than
    # digital ones, and it is never offered to MusicBrainz.
    rip: bool = False
    private: bool = False
    store_url: str | None = None
    media_mismatch: bool | None = None
    missing_url: bool | None = None
    observation: Observation | None = None
    source_upc: str | None = None
    barcode_status: BarcodeStatus | None = None
    # The user said this is the release they bought (#618). Silences mismatch
    # evidence; never removes it, so the warning can be brought back.
    accepted: bool = False
    # From the observation's stored group browse; None when never browsed.
    siblings: SiblingCheck | None = None

    @property
    def offline_mismatch(self) -> bool:
        """Mismatch evidence available without browsing the release group."""
        return self.media_mismatch is True or (
            self.media_mismatch is False and self.barcode_status == "different"
        )

    @property
    def has_findings(self) -> bool:
        if self.media_mismatch is None:
            return False  # unknown media is unchecked, not a finding
        return (
            self.offline_mismatch
            or self.missing_url is True
            or self.barcode_status in {"missing", "barcode_free", "different"}
        )


MismatchReason = Literal["source_match", "same_store", "media", "barcode_different"]
ContributionFinding = Literal[
    "barcode_different",
    "barcode_free",
    "same_store",
    "missing_url",
    "barcode_missing",
]


@dataclass(frozen=True)
class SiblingCheck:
    """A successful browse of the current release's group, classified against one
    local copy's evidence. Complete unless truncated or holding unspecified media;
    an incomplete one supports no finding at all."""

    # Releases other than the current one that the files could be: digital ones
    # for a download, physical ones for a rip.
    editions: list[dict[str, Any]]
    store_linked: list[dict[str, Any]]  # other releases linking the store URL exactly
    # Other releases linking another page on the download's store, each with
    # those pages as `store_pages`.
    same_store: list[dict[str, Any]]
    current_linked: bool  # the current release now links the store URL
    current_on_store: bool = False  # ...or any page on the download's store
    unknown: int = 0
    truncated: bool = False

    @property
    def complete(self) -> bool:
        return not self.unknown and not self.truncated

    @property
    def store_host(self) -> str | None:
        pages = [p for r in self.same_store for p in r["store_pages"]]
        return _store_host(pages[0]) if pages else None

    @property
    def source_matches(self) -> list[dict[str, Any]]:
        return [e for e in self.editions if e["source_matches"]]


@dataclass(frozen=True)
class Panel:
    """What the album page shows after a complete discovery.

    Both halves are decided up front so accepting the release only reveals the
    contribution; it never re-renders the page (#618).
    """

    # Evidence the match may be wrong, whether or not the user has silenced it.
    reasons: tuple[MismatchReason, ...] = ()
    # The one contribution for this release: offered when there are no reasons,
    # or once the user accepts the release despite them.
    finding: ContributionFinding | None = None
    add_release: bool = False


def _store_host(url: str) -> str | None:
    return urlsplit(url).hostname if url else None


def sibling_check(
    releases: list[Release], assessment: Assessment, current_mbid: str, total: int
) -> SiblingCheck:
    """Classify a release-group browse against one local copy's evidence.

    Same-store links are withheld evidence, never a match: one Bandcamp item can
    back two releases once tracks are added to it, and a store renames pages.
    """
    store = None if assessment.private else assessment.store_url
    host = _store_host(store) if store else None
    store_linked: list[dict[str, Any]] = []
    same_store: list[dict[str, Any]] = []
    current_linked = False
    current_on_store = False
    for release in releases:
        urls = {
            url
            for rel in (release.get("url-relation-list") or [])
            if (url := release_url(rel.get("target")))
        }
        if store is None:
            continue
        pages = sorted(u for u in urls if _store_host(u) == host)
        if release["id"] == current_mbid:
            current_linked = store in urls
            current_on_store = bool(pages)
        elif store in urls:
            store_linked.append(mb_lookup.release_summary(release))
        elif pages:
            same_store.append({**mb_lookup.release_summary(release), "store_pages": pages})
    editions, unknown = candidate_editions(releases, assessment)
    editions = [e for e in editions if e["id"] != current_mbid]
    for edition in editions:
        # Evidence against the match only where the matched release lacks it:
        # releases reuse barcodes, and one page can be linked from two releases.
        edition["url_evidence"] = edition["store_linked"] is True and not current_linked
        edition["upc_evidence"] = (
            edition["upc_matches"] is True and assessment.barcode_status != "same"
        )
        edition["source_matches"] = edition["url_evidence"] or edition["upc_evidence"]
    editions.sort(key=lambda edition: not edition["source_matches"])
    # A digital release linking the exact URL is a source match, shown as a row.
    digital_ids = {e["id"] for e in editions}
    return SiblingCheck(
        editions=editions,
        store_linked=[r for r in store_linked if r["id"] not in digital_ids],
        same_store=same_store,
        current_linked=current_linked,
        current_on_store=current_on_store,
        unknown=unknown,
        truncated=total > len(releases),
    )


def panel(assessment: Assessment, check: SiblingCheck) -> Panel | None:
    """The mismatch evidence and the single contribution, from a complete check.

    Release match comes first: data edits are offered only for a release the
    user accepted, or one with no mismatch evidence. Dismissing the warning is
    the user's word that this is the release they bought, so the contribution
    then offers what the matched release is missing, except a store URL already
    linked from another release: accepting the files' identity does not prove
    that today's store page belongs to it. None while the check is incomplete,
    which can authorize nothing.
    """
    if not check.complete:
        return None
    if assessment.media_mismatch is None:
        return Panel()
    reasons: list[MismatchReason] = []
    if check.source_matches:
        reasons.append("source_match")
    # Evidence against the match only while it links nothing on that store: a
    # release sold from both label and artist pages may link another page too.
    # And only while no release links the exact URL, which settles it: another
    # page on the store is the weaker evidence, and stating both equivocates
    # over an answer already given (#652).
    exact = any(e["url_evidence"] for e in check.source_matches)
    if check.same_store and not check.current_on_store and not exact:
        reasons.append("same_store")
    if assessment.media_mismatch:
        reasons.append("media")
    if assessment.media_mismatch is False and assessment.barcode_status == "different":
        reasons.append("barcode_different")
    # A download's UPC legitimately differs from an accepted physical release's
    # barcode; comparing them would invite overwriting a correct one. A rip's
    # UPC came from the ripper's metadata provider, not off the disc, so it is
    # evidence about the match and never a barcode to offer (#633).
    barcode = None if assessment.media_mismatch or assessment.rip else assessment.barcode_status
    # With reasons, the finding is shown only once the user dismisses them.
    trusted = bool(reasons) or assessment.accepted
    linked_elsewhere = exact or bool(check.store_linked)
    finding: ContributionFinding | None = None
    if barcode == "different":
        finding = "barcode_different"
    elif barcode == "barcode_free":
        finding = "barcode_free"
    elif assessment.missing_url and not check.current_linked and not linked_elsewhere:
        if trusted:
            finding = "missing_url"
        elif check.same_store:
            finding = "same_store"
        else:
            finding = "missing_url"
    elif barcode == "missing":
        finding = "barcode_missing"
    return Panel(
        reasons=tuple(reasons),
        finding=finding,
        # Harmony finds releases in digital stores, which can't add a CD.
        add_release=(
            bool(reasons)
            and not assessment.rip
            and "source_match" not in reasons
            and not check.store_linked
            and not assessment.private
            and bool(assessment.store_url or assessment.source_upc)
        ),
    )


def _library_panel(assessment: Assessment) -> Panel | None:
    """What the album page would show, from the stored browse; None until a
    complete one exists. Until then there is no reason to doubt the match, so
    neither filter lists the album, whatever its own release says."""
    if not assessment.has_findings or assessment.siblings is None:
        return None
    return panel(assessment, assessment.siblings)


def possible_mismatch(assessment: Assessment) -> bool:
    """The Library's Possible mismatch filter: evidence the user hasn't silenced.

    Pure: sibling evidence comes from the stored browse on the observation.
    """
    shown = _library_panel(assessment)
    return shown is not None and bool(shown.reasons) and not assessment.accepted


def contribution_due(assessment: Assessment) -> bool:
    """The Library's MB contributions filter: the album page's finding for the
    matched release — once it is accepted, or when nothing casts doubt on it.

    Never an album in Possible mismatch.
    """
    shown = _library_panel(assessment)
    return (
        shown is not None
        and shown.finding is not None
        and (assessment.accepted or not shown.reasons)
    )


def release_url(url: str | None) -> str | None:
    """Conservative release URL equality; never match a slug across hosts.

    HTTP/HTTPS, a trailing slash, and tracking query/fragment do not distinguish
    Bandcamp album/track pages. Other path shapes cannot identify a download.
    """
    if not url:
        return None
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None  # malformed input cannot establish release identity
    path = parts.path.rstrip("/")
    segments = path.split("/")
    if (
        parts.scheme not in {"http", "https"}
        or not host
        or parts.username
        or parts.password
        or port not in {None, 80, 443}
        or len(segments) != 3
        or segments[1] not in {"album", "track"}
        or not segments[2]
    ):
        return None
    return urlunsplit(("https", host.lower().removeprefix("www."), path, "", ""))


def downloaded(album: Album) -> bool:
    """Positive evidence the files are a download, and none that they're a rip.

    Their origin is a store (#634's `provenance.origin`, the same conclusion
    the album page shows): Harmonist's own Bandcamp download, or a store's mark
    in the files that nothing contradicts. A UPC alone never qualifies (#632).
    """
    return provenance.origin(album) in provenance.STORES


def _rip_with_upc(album: Album) -> bool:
    """A CD rip whose ripper wrote a consistent UPC (#633): the one piece of
    evidence a rip carries about which release it is. Without it there is
    nothing to check."""
    return provenance.origin(album) is provenance.Origin.CD and bool(album.source_upc)


def _eligible(album: Album) -> bool:
    """A confirmed release plus download provenance, or a CD rip with a UPC:
    see docs/design/contributions.md."""
    sc = album.sidecar
    if sc is None or not sc.mb_release_id:
        return False
    return downloaded(album) or _rip_with_upc(album)


def _media_mismatch(formats: tuple[str, ...], rip: bool) -> bool | None:
    """Whether the matched release's media contradict what the files are: a
    physical release for a download, a digital-only one for a CD rip. None
    when media MusicBrainz hasn't specified leave that open."""
    physical = any(f and f != "Digital Media" for f in formats)
    known = bool(formats) and all(formats)
    if rip:
        return (not physical) if known else (False if physical else None)
    return True if physical else (False if known else None)


def assess(album: Album) -> Assessment:
    sc = album.sidecar
    if sc is None or not sc.mb_release_id or not _eligible(album):
        return Assessment()
    rip = not downloaded(album)
    bandcamp = sc.bandcamp_downloaded or bool(album.bandcamp_comment_urls)
    # A purchase link says nothing about where a rip's files came from.
    private = not rip and bool(sc.bandcamp and sc.bandcamp.is_private)
    comments = {u for raw in album.bandcamp_comment_urls if (u := release_url(raw))}
    # An actual download records its own store URL. Otherwise prefer precise
    # file evidence over an MB-derived URL that may describe a nearby edition.
    url = release_url(sc.store_url) if sc.bandcamp_downloaded else None
    if url is None and not rip:
        if len(comments) == 1:
            url = next(iter(comments))
        elif not comments and bandcamp:
            url = release_url(sc.store_url)
    observed = album.contribution_observation
    if observed is not None and observed.mbid != sc.mb_release_id:
        observed = None  # rematching never carries an old release's findings
    media: bool | None = None
    missing: bool | None = None
    if observed is not None:
        media = _media_mismatch(observed.formats, rip)
        if url and not private:
            missing = url not in observed.urls
    barcode_status: BarcodeStatus | None = None
    if observed is not None and album.source_upc:
        if observed.barcode is None:
            barcode_status = "missing"
        elif observed.barcode == "":
            barcode_status = "barcode_free"
        elif barcodes.normalise(observed.barcode) == barcodes.normalise(album.source_upc):
            barcode_status = "same"
        else:
            barcode_status = "different"
    assessment = Assessment(
        eligible=True,
        rip=rip,
        private=private,
        store_url=url,
        media_mismatch=media,
        missing_url=missing,
        observation=observed,
        source_upc=album.source_upc,
        barcode_status=barcode_status,
        accepted=sc.accepted_release_id == sc.mb_release_id,
    )
    if observed is None or observed.group is None:
        return assessment
    # Everything `sibling_check` reads from this copy, so one classification
    # serves every render until the evidence or the stored browse changes.
    key = (url, private, rip, album.source_upc, barcode_status, sc.mb_release_id)
    check = observed.checks.get(key)
    if check is None:
        check = sibling_check(
            list(observed.group), assessment, sc.mb_release_id, observed.group_total
        )
        observed.checks[key] = check
    return replace(assessment, siblings=check)


def observe(album: Album, release: Release, checked_at: datetime | None) -> None:
    """Use a successful full release fetch, whose includes request url-rels.

    A missing url-relation-list in that response means no relationships. An
    absent cache row is handled by warm(), never by calling this with {}. The
    group's stored browse rides along, read only for an eligible album.
    """
    group: tuple[Release, ...] | None = None
    total = 0
    group_id = (release.get("release-group") or {}).get("id")
    if group_id and _eligible(album):
        stored = mb_cache.stored_release_group_editions(str(group_id))
        if stored is not None:
            group, total = tuple(stored[0]), stored[1]
    album.contribution_observation = Observation(
        str(release["id"]),
        tuple(str(m.get("format") or "") for m in (release.get("medium-list") or [])),
        frozenset(
            url
            for rel in (release.get("url-relation-list") or [])
            if (url := release_url(rel.get("target")))
        ),
        checked_at,
        release.get("barcode"),
        group,
        total,
    )


def observe_group(album: Album, releases: list[Release], total: int) -> None:
    """Attach a fresh browse of the release's group to the album's observation."""
    if album.contribution_observation is not None:
        album.contribution_observation = replace(
            album.contribution_observation, group=tuple(releases), group_total=total
        )


def warm(album: Album) -> None:
    """Rebuild from the durable current-includes cache without network I/O."""
    if not assess(album).eligible:
        return
    assert album.sidecar is not None and album.sidecar.mb_release_id is not None
    mbid = album.sidecar.mb_release_id
    snapshot = mb_cache.stored_release_snapshot(mbid)
    if snapshot is not None:
        observe(album, snapshot.payload, snapshot.fetched_at)


def media_fit(release: Release, rip: bool) -> bool | None:
    """Whether a release's media fit the files: digital only for a download,
    anything physical for a CD rip (#633). None when a medium MusicBrainz hasn't
    specified leaves it open. One rule for the candidates and for the review
    that replaces the match with one, so every candidate offered can be used."""
    formats = [m.get("format") for m in (release.get("medium-list") or [])]
    physical = any(f and f != "Digital Media" for f in formats)
    unspecified = not formats or not all(formats)
    if physical:
        return rip
    return None if unspecified else not rip


def candidate_editions(
    releases: list[Release], assessment: Assessment
) -> tuple[list[dict[str, Any]], int]:
    """Candidates, never matches: the releases the files could be — digital ones
    for a download, physical ones for a CD rip (#633). Retain uncertainty about
    missing media: a release with a medium MusicBrainz hasn't specified can't be
    ruled in or out, so it is counted instead."""
    editions = []
    unknown = 0
    for release in releases:
        fit = media_fit(release, assessment.rip)
        if fit is None:
            unknown += 1
        if not fit:
            continue
        urls = {
            url
            for rel in (release.get("url-relation-list") or [])
            if (url := release_url(rel.get("target")))
        }
        store_linked = (
            assessment.store_url in urls
            if assessment.store_url and not assessment.private
            else None
        )
        upc_matches = (
            barcodes.normalise(release.get("barcode") or "")
            == barcodes.normalise(assessment.source_upc)
            if assessment.source_upc
            else None
        )
        editions.append(
            {
                **mb_lookup.release_summary(release),
                "store_linked": store_linked,
                "barcode": release.get("barcode"),
                "upc_matches": upc_matches,
                # Either original identifier can support a reviewed suggestion.
                # Keep siblings with missing evidence visible for manual choice.
                "source_matches": store_linked is True or upc_matches is True,
            }
        )
    return editions, unknown
