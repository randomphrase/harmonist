import hashlib
from pathlib import Path
from typing import Optional, List, Dict
from mutagen.mp4 import MP4

class Album:
    def __init__(self, name: str, artist: str, bandcamp_url: str, path: Path, track_count: int = 0, musicbrainz_releaseid: Optional[str] = None):
        self.title = name
        self.artist = artist
        self.bandcamp_url = bandcamp_url
        self.path = path  # Path to the album directory
        self.track_count = track_count
        self.musicbrainz_releaseid = musicbrainz_releaseid
        self.id = hashlib.md5(str(path).encode()).hexdigest()

    @property
    def cover_url(self) -> str:
        cover_file = self.path / "cover.jpg"
        if cover_file.exists():
             return f"/static/music/{self.path.name}/cover.jpg"
        return "https://placehold.co/400x400/1e293b/white?text=" + self.title.replace(" ", "+")

    def is_untagged(self) -> bool:
        return self.musicbrainz_releaseid is None

    def tag_with_mbid(self, mbid: str):
        """
        Tags all .m4a files in the album directory with the provided MusicBrainz Release ID.
        """
        for filepath in self.path.glob("*.m4a"):
            try:
                audio = MP4(filepath)
                # MP4 uses '----:com.apple.iTunes:MUSICBRAINZ_RELEASEID' for MBID
                # It expects a list of bytes
                audio["----:com.apple.iTunes:MUSICBRAINZ_RELEASEID"] = [mbid.encode('utf-8')]
                audio.save()
            except Exception as e:
                print(f"Error tagging {filepath}: {e}")
        self.musicbrainz_releaseid = mbid

    def __repr__(self):
        status = "Untagged" if self.is_untagged() else f"Tagged ({self.musicbrainz_releaseid})"
        return f"Album(title='{self.title}', artist='{self.artist}', path='{self.path.name}', status='{status}')"
