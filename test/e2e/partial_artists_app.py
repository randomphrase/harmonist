"""A controllable NAS permission failure on the ordinary artist demo."""

import errno
from pathlib import Path

from test.e2e.artists_app import app

from harmonist import demo, scanner

_seed = demo.seed
_walk = scanner.os.walk


def seed(music_dir, **kwargs):
    _seed(music_dir, **kwargs)
    (music_dir / "Unavailable").mkdir()
    (music_dir / ".deny-nfo-walk").touch()


def walk(root, *args, **kwargs):
    for directory, directories, files in _walk(root, *args, **kwargs):
        path = Path(directory)
        if (path / ".deny-nfo-walk").exists() and "Unavailable" in directories:
            directories.remove("Unavailable")
            if callback := kwargs.get("onerror"):
                callback(
                    PermissionError(errno.EACCES, "Permission denied", str(path / "Unavailable"))
                )
        yield directory, directories, files


demo.seed = seed
scanner.os.walk = walk

__all__ = ["app"]
