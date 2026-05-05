import musicbrainzngs
from typing import List, Dict, Optional, Protocol

class Searcher(Protocol):
    def search_album(self, artist: str, title: str) -> List[Dict]:
        ...

class MusicBrainzSearcher:
    def __init__(self, app_name: str = "Harmonist", app_version: str = "0.1.0", contact: str = "https://github.com/yourusername/harmonist"):
        musicbrainzngs.set_useragent(app_name, app_version, contact)

    def search_album(self, artist: str, title: str) -> List[Dict]:
        """
        Searches MusicBrainz for a release and returns a list of matches.
        """
        try:
            # Use Lucene query for more precision
            query = f'artist:"{artist}" AND release:"{title}"'
            result = musicbrainzngs.search_releases(query=query, limit=10)
            
            matches = []
            for release in result.get('release-list', []):
                matches.append({
                    "id": release.get('id'),
                    "title": release.get('title'),
                    "artist": release.get('artist-credit-phrase'),
                    "date": release.get('date'),
                    "country": release.get('country'),
                    "barcode": release.get('barcode'),
                    "status": release.get('status'),
                    "track_count": release.get('medium-track-count')
                })
            return matches
        except Exception as e:
            print(f"Error searching MusicBrainz: {e}")
            return []

class MockSearcher:
    def search_album(self, artist: str, title: str) -> List[Dict]:
        if "Beatles" in artist:
            return [{
                "id": "mock-mbid-1",
                "title": title,
                "artist": artist,
                "date": "1969",
                "track_count": 12
            }]
        return []

if __name__ == "__main__":
    searcher = MusicBrainzSearcher()
    results = searcher.search_album("The Beatles", "Abbey Road")
    for r in results:
        print(f"{r['title']} by {r['artist']} ({r['date']}) - ID: {r['id']}")
