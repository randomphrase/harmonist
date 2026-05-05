import os
import shutil
from pathlib import Path
from mutagen.mp4 import MP4

# Global template search
def get_template_path():
    # Try hardcoded local one first
    local_path = Path("music/Album1/track1.m4a")
    if local_path.exists():
        return local_path
    
    # Try the one we found in the system
    system_path = Path("/Users/alastair/Music/Traktor/02 Declino.m4a")
    if system_path.exists():
        return system_path
        
    return None

def create_dummy_m4a(path: Path, title: str, album: str, artist: str, comment: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    template = get_template_path()
    
    if template:
        shutil.copy(template, path)
        audio = MP4(path)
        # Clear existing tags if possible, or just overwrite
        audio["\xa9nam"] = [title]
        audio["\xa9alb"] = [album]
        audio["\xa9ART"] = [artist]
        audio["\xa9cmt"] = [comment]
        # Remove MBID if present
        mbid_key = "----:com.apple.iTunes:MUSICBRAINZ_RELEASEID"
        if mbid_key in audio:
            del audio[mbid_key]
        audio.save()
    else:
        # Fallback to a zero-byte file if no template, 
        # but the scanner will fail to read it with MP4()
        path.touch()
        print(f"Warning: No .m4a template found. Created empty file at {path}")

def setup_demo():
    base_dir = Path(__file__).resolve().parent.parent.parent
    demo_dir = base_dir / "music_demo"
    
    if demo_dir.exists():
        shutil.rmtree(demo_dir)
    
    demo_dir.mkdir()
    
    # 1. An album that will have MB matches (The Beatles)
    create_dummy_m4a(
        demo_dir / "The Beatles" / "Abbey Road" / "01 Come Together.m4a",
        "Come Together",
        "Abbey Road",
        "The Beatles",
        "https://thebeatles.bandcamp.com/album/abbey-road"
    )
    
    # 2. An album that needs seeding
    create_dummy_m4a(
        demo_dir / "Mock Artist" / "Mock Album" / "01 Track 1.m4a",
        "Track 1",
        "Mock Album",
        "Mock Artist",
        "https://mock.bandcamp.com/album/mock-album"
    )
    
    print(f"Demo directory set up at {demo_dir}")

if __name__ == "__main__":
    setup_demo()
