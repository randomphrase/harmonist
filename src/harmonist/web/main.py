import os
from pathlib import Path
from typing import Dict, List
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from harmonist.scanner import scan_music_directory
from harmonist.syncer import Syncer
from harmonist.bandcamp_scraper import BandcampScraper, MockScraper, Scraper
from harmonist.mb_searcher import MusicBrainzSearcher, MockSearcher, Searcher

app = FastAPI()

# Point to the templates directory in the project root
BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

DEMO_MODE = os.getenv("DEMO_MODE") == "1"

if DEMO_MODE:
    MUSIC_DIR = BASE_DIR / "music_demo"
    mb_searcher: Searcher = MockSearcher()
    scraper: Scraper = MockScraper()
    print("RUNNING IN DEMO MODE")
else:
    MUSIC_DIR = BASE_DIR / "music"
    mb_searcher: Searcher = MusicBrainzSearcher()
    scraper: Scraper = BandcampScraper()

COOKIES_PATH = BASE_DIR / "cookies.txt"

# In-memory cache for MB search results
SEARCH_CACHE: Dict[str, List[Dict]] = {}

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse(request, "index.html")

@app.get("/tasks", response_class=HTMLResponse)
async def get_tasks(request: Request):
    if not MUSIC_DIR.exists():
        MUSIC_DIR.mkdir(parents=True, exist_ok=True)
        
    albums = scan_music_directory(MUSIC_DIR)
    
    needs_seeding = []
    ambiguities = []
    
    for album in albums:
        if album.is_untagged():
            if album.id not in SEARCH_CACHE:
                # Search MusicBrainz
                matches = mb_searcher.search_album(album.artist, album.title)
                SEARCH_CACHE[album.id] = matches
            
            matches = SEARCH_CACHE[album.id]
            
            if not matches:
                needs_seeding.append(album)
            else:
                ambiguities.append({"album": album, "matches": matches})
    
    return templates.TemplateResponse(request, "task_list.html", {
        "needs_seeding": needs_seeding,
        "ambiguities": ambiguities
    })

@app.post("/sync")
async def sync_collection(background_tasks: BackgroundTasks):
    syncer = Syncer(COOKIES_PATH, MUSIC_DIR)
    background_tasks.add_task(syncer.sync)
    return HTMLResponse(content="Sync started in background...")

@app.post("/seed/{album_id}", response_class=HTMLResponse)
async def seed_album(request: Request, album_id: str):
    albums = scan_music_directory(MUSIC_DIR)
    album = next((a for a in albums if a.id == album_id), None)
    
    if not album:
        return HTMLResponse(content="Album not found", status_code=404)
    
    # Scrape metadata from Bandcamp
    metadata = await scraper.scrape_album_metadata(album.bandcamp_url)
    
    if not metadata:
        # Fallback to file-based metadata if scraper fails
        metadata = {
            "title": album.title,
            "artist": album.artist,
            "year": "",
            "label": "",
            "tracklist": [],
            "url": album.bandcamp_url
        }
    
    return templates.TemplateResponse(request, "partials/verify_seeding.html", {
        "album": album,
        "metadata": metadata
    })

@app.post("/match/{album_id}/{mbid}")
async def match_album(album_id: str, mbid: str):
    albums = scan_music_directory(MUSIC_DIR)
    album = next((a for a in albums if a.id == album_id), None)
    
    if not album:
        return HTMLResponse(content="Album not found", status_code=404)

    # Trigger tagging the files with the selected MBID
    print(f"Matching album {album.title} with MBID {mbid}")
    album.tag_with_mbid(mbid)
    
    return HTMLResponse(content="<div class='text-green-500 font-bold'>Matched! Refreshing...</div>", headers={"HX-Trigger": "load-tasks"})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
