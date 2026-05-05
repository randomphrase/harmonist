import pytest
import os
import shutil
from pathlib import Path
from fastapi.testclient import TestClient
from mutagen.mp4 import MP4
from harmonist.web.main import app, MUSIC_DIR
from harmonist.setup_demo import create_dummy_m4a

@pytest.fixture
def client():
    # Set DEMO_MODE for the duration of the test
    os.environ["DEMO_MODE"] = "1"
    # Ensure music_demo is clean
    if MUSIC_DIR.exists():
        shutil.rmtree(MUSIC_DIR)
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    
    with TestClient(app) as c:
        yield c
    
    # Cleanup
    if MUSIC_DIR.exists():
        shutil.rmtree(MUSIC_DIR)

def test_full_workflow(client):
    # 1. Setup: Create an untagged album
    album_path = MUSIC_DIR / "The Beatles" / "Abbey Road"
    track_path = album_path / "01 Come Together.m4a"
    create_dummy_m4a(
        track_path,
        "Come Together",
        "Abbey Road",
        "The Beatles",
        "https://thebeatles.bandcamp.com/album/abbey-road"
    )

    # 2. Scan: Verify album appears in tasks (ambiguities because of Beatles mock)
    response = client.get("/tasks")
    assert response.status_code == 200
    assert "Abbey Road" in response.text
    assert "Resolve Ambiguity" in response.text

    # Get the album ID from the page (it's a hash of the path)
    import hashlib
    album_id = hashlib.md5(str(album_path).encode()).hexdigest()

    # 3. Match: Trigger the tagging
    # MockSearcher returns 'mock-mbid-1' for Beatles
    mbid = "mock-mbid-1"
    response = client.post(f"/match/{album_id}/{mbid}")
    assert response.status_code == 200
    assert "Matched!" in response.text

    # 4. Verify: Check the file tags
    audio = MP4(track_path)
    mbid_tag = "----:com.apple.iTunes:MUSICBRAINZ_RELEASEID"
    assert mbid_tag in audio
    assert audio[mbid_tag][0].decode('utf-8') == mbid

    # 5. Refresh: Verify it's no longer in tasks
    response = client.get("/tasks")
    assert response.status_code == 200
    assert "Abbey Road" not in response.text
