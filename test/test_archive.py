"""Tests for archiving an album off disk ahead of a re-download (#132).

The whole safety argument for #132 is an ordering claim — the zip is written and
proved good BEFORE a byte is deleted — so most of what is worth testing here is
what the module does when something goes wrong partway. The web rung can't see
any of it: it would need the same failures injected, and would then be asserting
on this module's behaviour through two extra layers.
"""

from __future__ import annotations

import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from harmonist import archive

WHEN = datetime(2026, 8, 24, tzinfo=UTC)


def _album(root: Path, artist: str = "Artist", album: str = "Album", *, tracks: int = 3) -> Path:
    d = root / artist / album
    d.mkdir(parents=True)
    for i in range(1, tracks + 1):
        (d / f"{i:02d} Track.m4a").write_bytes(b"audio" * (100 * i))
    (d / "cover.jpg").write_bytes(b"jpeg")
    (d / ".harmonist.json").write_text('{"schema_version": 1}')
    (d / "bandcamp_item_id.txt").write_text("4242")
    return d


def test_archives_everything_and_removes_the_album(tmp_path):
    d = _album(tmp_path)
    result = archive.archive_and_remove([d], music_root=tmp_path, label="Artist — Album", now=WHEN)

    assert result.path == tmp_path / "Artist — Album (archived 2026-08-24).zip"
    assert not d.exists()
    with zipfile.ZipFile(result.path) as zf:
        names = set(zf.namelist())
    # The bookkeeping goes in too: an archive that restores as an unreconciled
    # NEW album is not the recovery the button promises.
    assert names == {
        "Artist/Album/01 Track.m4a",
        "Artist/Album/02 Track.m4a",
        "Artist/Album/03 Track.m4a",
        "Artist/Album/cover.jpg",
        "Artist/Album/.harmonist.json",
        "Artist/Album/bandcamp_item_id.txt",
    }
    assert result.file_count == 6


def test_archive_restores_the_album_byte_for_byte(tmp_path):
    d = _album(tmp_path)
    before = {p.relative_to(tmp_path).as_posix(): p.read_bytes() for p in d.rglob("*")}

    result = archive.archive_and_remove([d], music_root=tmp_path, label="A — B", now=WHEN)

    # Unzipping at the music root is the escape hatch the UI tells the user
    # about, so it has to actually put the album back where it was.
    with zipfile.ZipFile(result.path) as zf:
        zf.extractall(tmp_path)
    after = {p.relative_to(tmp_path).as_posix(): p.read_bytes() for p in d.rglob("*")}
    assert after == before


def test_multi_directory_album_lands_in_one_archive(tmp_path):
    """A release split across per-disc folders (§13.5) is one album, so it gets
    one zip — restoring half of it would be worse than not offering the button."""
    disc1 = _album(tmp_path, album="Album/Disc 1", tracks=1)
    disc2 = _album(tmp_path, album="Album/Disc 2", tracks=1)

    result = archive.archive_and_remove(
        [disc1, disc2], music_root=tmp_path, label="A — B", now=WHEN
    )

    with zipfile.ZipFile(result.path) as zf:
        names = set(zf.namelist())
    assert "Artist/Album/Disc 1/01 Track.m4a" in names
    assert "Artist/Album/Disc 2/01 Track.m4a" in names
    assert not disc1.exists() and not disc2.exists()
    # Both discs and the folder that held them are gone, so nothing is left for
    # Plex to show as an artist with no music.
    assert not (tmp_path / "Artist").exists()


def test_empties_parent_directories_it_emptied_but_not_ones_holding_music(tmp_path):
    d = _album(tmp_path)
    sibling = _album(tmp_path, album="Other Album")

    archive.archive_and_remove([d], music_root=tmp_path, label="A — B", now=WHEN)

    # The artist still has an album, so the artist folder stays.
    assert sibling.exists()
    assert (tmp_path / "Artist").is_dir()


def test_leaves_a_parent_that_still_holds_a_stray_file(tmp_path):
    d = _album(tmp_path)
    (tmp_path / "Artist" / ".DS_Store").write_bytes(b"junk")

    archive.archive_and_remove([d], music_root=tmp_path, label="A — B", now=WHEN)

    assert (tmp_path / "Artist" / ".DS_Store").exists()


def test_a_second_archive_of_the_same_album_does_not_overwrite_the_first(tmp_path):
    first = _album(tmp_path)
    r1 = archive.archive_and_remove([first], music_root=tmp_path, label="A — B", now=WHEN)
    again = _album(tmp_path)
    r2 = archive.archive_and_remove([again], music_root=tmp_path, label="A — B", now=WHEN)

    # The first archive may be the only copy of those files in existence.
    assert r1.path != r2.path
    assert r1.path.exists() and r2.path.exists()
    assert r2.path.name == "A — B (archived 2026-08-24) (2).zip"


def test_refuses_when_there_is_not_enough_free_space_and_keeps_the_album(monkeypatch, tmp_path):
    import shutil as shutil_mod

    d = _album(tmp_path)
    monkeypatch.setattr(
        archive.shutil, "disk_usage", lambda _p: shutil_mod._ntuple_diskusage(1, 1, 0)
    )

    with pytest.raises(archive.InsufficientSpaceError):
        archive.archive_and_remove([d], music_root=tmp_path, label="A — B", now=WHEN)

    assert d.exists()
    assert list(d.glob("*.m4a"))
    assert not list(tmp_path.glob("*.zip"))


def test_a_zip_that_fails_verification_leaves_the_album_alone(monkeypatch, tmp_path):
    """The ordering claim, tested where it can actually break: if the archive is
    bad for any reason, nothing is deleted and no half-written zip is left."""
    d = _album(tmp_path)
    before = sorted(p.name for p in d.iterdir())

    def _truncating_write(zip_path, members):
        # Write every member but one — the failure `testzip` alone would miss,
        # since what it produces is a perfectly valid, incomplete archive.
        with zipfile.ZipFile(zip_path, "w") as zf:
            for source, arcname, _ in members[:-1]:
                zf.write(source, arcname)

    monkeypatch.setattr(archive, "_write", _truncating_write)

    with pytest.raises(archive.ArchiveError, match="missing from the archive"):
        archive.archive_and_remove([d], music_root=tmp_path, label="A — B", now=WHEN)

    assert sorted(p.name for p in d.iterdir()) == before
    assert not list(tmp_path.glob("*.zip"))


def test_a_member_stored_at_the_wrong_size_is_caught(monkeypatch, tmp_path):
    d = _album(tmp_path)

    def _short_write(zip_path, members):
        with zipfile.ZipFile(zip_path, "w") as zf:
            for _, arcname, _ in members:
                zf.writestr(arcname, b"")  # right name, no bytes

    monkeypatch.setattr(archive, "_write", _short_write)

    with pytest.raises(archive.ArchiveError, match="wrong size"):
        archive.archive_and_remove([d], music_root=tmp_path, label="A — B", now=WHEN)
    assert d.exists()


def test_refuses_a_directory_outside_the_music_folder(tmp_path):
    """The arcnames are relative to the music root, so a directory outside it has
    no representable place in the archive — and would restore somewhere else."""
    outside = tmp_path / "elsewhere" / "Album"
    outside.mkdir(parents=True)
    (outside / "t.m4a").write_bytes(b"x")
    music = tmp_path / "music"
    music.mkdir()

    with pytest.raises(archive.ArchiveError, match="not inside the music folder"):
        archive.archive_and_remove([outside], music_root=music, label="A — B", now=WHEN)
    assert outside.exists()


def test_refuses_an_empty_album_rather_than_writing_an_empty_zip(tmp_path):
    d = tmp_path / "Artist" / "Album"
    d.mkdir(parents=True)

    with pytest.raises(archive.ArchiveError, match="nothing to archive"):
        archive.archive_and_remove([d], music_root=tmp_path, label="A — B", now=WHEN)
    assert not list(tmp_path.glob("*.zip"))


def test_a_symlink_is_not_followed_out_of_the_library(tmp_path):
    """Following one would copy someone's whole home directory into the music
    folder, and deleting the album would then look like it lost the target."""
    d = _album(tmp_path, tracks=1)
    target = tmp_path / "outside.txt"
    target.write_text("not mine to archive")
    (d / "link.txt").symlink_to(target)

    result = archive.archive_and_remove([d], music_root=tmp_path, label="A — B", now=WHEN)

    with zipfile.ZipFile(result.path) as zf:
        assert "Artist/Album/link.txt" not in zf.namelist()
    assert target.read_text() == "not mine to archive"


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Artist / Band — Album", "Artist Band — Album (archived 2026-08-24).zip"),
        ("...hidden", "hidden (archived 2026-08-24).zip"),
        ("  spaced   out  ", "spaced out (archived 2026-08-24).zip"),
        ("/:*?", "album (archived 2026-08-24).zip"),
        ("x" * 400, "x" * 180 + " (archived 2026-08-24).zip"),
    ],
)
def test_archive_names_are_safe_filenames(tmp_path, label, expected):
    """A slash would make a directory, a leading dot would hide the file the user
    is being told to look for, and 400 characters exceeds every filesystem here."""
    d = _album(tmp_path, tracks=1)
    result = archive.archive_and_remove([d], music_root=tmp_path, label=label, now=WHEN)
    assert result.path.name == expected
    assert result.path.parent == tmp_path


def test_pruned_parents_are_named_the_way_the_caller_names_them(tmp_path, monkeypatch):
    """The audit log records paths relative to the configured music dir, and it
    relativises literally. A music dir reached through a symlink — every macOS
    $TMPDIR, and any bind mount — resolves to a different prefix, so a pruned
    parent built from `resolve()` relativises against nothing and prints an
    absolute path instead of the album's name (#98).
    """
    from harmonist import audit

    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "music"  # what the user configured
    link.symlink_to(real, target_is_directory=True)
    _album(link, tracks=1)
    monkeypatch.setattr(audit, "_library_root", link)
    recorded: list[str] = []
    monkeypatch.setattr(audit.log, "info", lambda _fmt, line: recorded.append(line))

    archive.archive_and_remove(
        [link / "Artist" / "Album"], music_root=link, label="A — B", now=WHEN
    )

    assert "redownload.prune dir=Artist" in recorded
