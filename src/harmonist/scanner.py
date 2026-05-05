from typing import List, Optional, Dict
from pathlib import Path
import os
from mutagen.mp4 import MP4
from harmonist.metadata import Album

def scan_music_directory(music_directory: Path) -> List[Album]:
    albums: Dict[Path, Album] = {}

    for root, _, files in os.walk(music_directory):
        for file in files:
            if file.endswith(".m4a"):
                filepath = Path(root) / file
                try:
                    audio = MP4(filepath)

                    album_name = audio.get("\xa9alb", [None])[0]
                    artist_name = audio.get("\xa9ART", [None])[0]
                    comment = audio.get("\xa9cmt", [None])[0]
                    musicbrainz_releaseid_bytes = audio.get("----:com.apple.iTunes:MUSICBRAINZ_RELEASEID", [None])[0]
                    musicbrainz_releaseid = musicbrainz_releaseid_bytes.decode('utf-8') if musicbrainz_releaseid_bytes else None

                    if album_name and comment:
                        # Use the album's parent directory as the unique identifier for the album
                        album_path = filepath.parent
                        if album_path not in albums:
                            albums[album_path] = Album(
                                name=album_name,
                                artist=artist_name or "Unknown Artist",
                                bandcamp_url=comment,
                                path=album_path,
                                track_count=1,
                                musicbrainz_releaseid=musicbrainz_releaseid
                            )
                        else:
                            albums[album_path].track_count += 1
                            # If an album is found to be tagged by any track, mark the album as tagged
                            if musicbrainz_releaseid and albums[album_path].is_untagged():
                                albums[album_path].musicbrainz_releaseid = musicbrainz_releaseid

                except Exception as e:
                    print(f"Error processing {filepath}: {e}")
                    continue
    return list(albums.values())

if __name__ == "__main__":
    # Assuming 'music' directory is at the project root for testing
    current_dir = Path(__file__).parent.parent.parent
    music_dir = current_dir / "music"
    
    print(f"Scanning music directory: {music_dir}")
    found_albums = scan_music_directory(music_dir)
    
    print("\n--- Found Albums ---")
    for album in found_albums:
        print(album)

    print("\n--- Untagged Albums ---")
    for album in found_albums:
        if album.is_untagged():
            print(album)