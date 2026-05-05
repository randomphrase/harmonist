import httpx
from bs4 import BeautifulSoup
from typing import Optional, List, Dict, Protocol
import json
import re

class Scraper(Protocol):
    async def scrape_album_metadata(self, album_url: str) -> Optional[Dict]:
        ...

class BandcampScraper:
    async def scrape_album_metadata(self, album_url: str) -> Optional[Dict]:
        """
        Scrapes a Bandcamp album page for full metadata needed for MusicBrainz seeding.
        """
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(album_url)
                response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Method 1: TralbumData (very comprehensive)
            script_tag = soup.find('script', string=re.compile(r'var TralbumData ='))
            if script_tag:
                match = re.search(r'var TralbumData = (\{.*?\});', script_tag.string, re.DOTALL)
                if match:
                    data = json.loads(match.group(1))
                    
                    metadata = {
                        "title": data.get("current", {}).get("title"),
                        "artist": data.get("artist"),
                        "release_date": data.get("current", {}).get("release_date"),
                        "tracklist": [],
                        "url": album_url,
                        "upc": data.get("current", {}).get("upc"),
                    }
                    
                    # Extract tracks
                    for track in data.get("trackinfo", []):
                        metadata["tracklist"].append({
                            "title": track.get("title"),
                            "duration": track.get("duration"), # in seconds
                            "position": track.get("track_num")
                        })
                    
                    # Extract release year from date string (e.g., "15 Jun 2021 00:00:00 GMT")
                    if metadata["release_date"]:
                        year_match = re.search(r'\d{4}', metadata["release_date"])
                        if year_match:
                            metadata["year"] = year_match.group(0)
                    
                    # Get labels if available
                    label_link = soup.find('a', class_='back-to-label-link')
                    if label_link:
                        metadata["label"] = label_link.get_text().strip().replace("back to ", "")

                    return metadata
        except Exception as e:
            print(f"Error scraping metadata: {e}")
        
        return None

class MockScraper:
    async def scrape_album_metadata(self, album_url: str) -> Optional[Dict]:
        return {
            "title": "Mock Album",
            "artist": "Mock Artist",
            "year": "2024",
            "label": "Mock Records",
            "tracklist": [
                {"title": "Track 1", "duration": 180, "position": 1},
                {"title": "Track 2", "duration": 200, "position": 2}
            ],
            "url": album_url
        }

async def get_album_url(artist_bandcamp_url: str, album_name: str) -> Optional[str]:
    """
    Scrapes the artist's Bandcamp page to find the specific album URL.
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(artist_bandcamp_url)
            response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        album_links = soup.find_all('a', class_='item-link') or soup.find_all('a', class_='title')

        for link in album_links:
            if link.get('title') and link['title'].strip().lower() == album_name.lower():
                return str(response.url.join(link['href']))
            if link.get_text().strip().lower() == album_name.lower():
                return str(response.url.join(link['href']))

        # Fallback
        for link in soup.find_all('a'):
            if (album_name.lower() in link.get_text().strip().lower() or
                (link.get('title') and album_name.lower() in link['title'].strip().lower())):
                return str(response.url.join(link['href']))

    except Exception as e:
        print(f"Error finding album URL: {e}")
            
    return None

if __name__ == "__main__":
    import asyncio

    async def main():
        scraper = BandcampScraper()
        album_url = "https://bvdub.bandcamp.com/album/when-love-lived"
        metadata = await scraper.scrape_album_metadata(album_url)
        if metadata:
            print(f"Metadata for {metadata['title']} by {metadata['artist']}:")
            print(f"Year: {metadata.get('year')}, Label: {metadata.get('label')}")
            print(f"Tracks: {len(metadata['tracklist'])}")
            for t in metadata['tracklist']:
                print(f"  {t['position']}. {t['title']} ({t['duration']}s)")

    asyncio.run(main())
