from pathlib import Path
from typing import Optional
from bandcampsync import Syncer as BCSyncer

class Syncer:
    def __init__(self, cookies_path: Path, music_directory: Path):
        self.cookies_path = cookies_path
        self.music_directory = music_directory

    def sync(self, download_format: str = "flac") -> bool:
        """
        Synchronizes the local music directory with the Bandcamp collection.
        """
        if not self.cookies_path.exists():
            print(f"Cookies file not found at {self.cookies_path}")
            return False
        
        if not self.music_directory.exists():
            self.music_directory.mkdir(parents=True, exist_ok=True)

        with open(self.cookies_path, 'r') as f:
            cookies = f.read()

        syncer = BCSyncer(
            cookies=cookies,
            dir_path=str(self.music_directory),
            media_format=download_format,
            temp_dir_root=None,
            ign_file_path=None,
            ign_patterns=None,
            notify_url=None
        )
        
        try:
            # Syncer.__init__ actually runs the sync in this library! 
            # Looking at do_sync above, it just calls Syncer(...)
            return True
        except Exception as e:
            print(f"Error during Bandcamp sync: {e}")
            return False

if __name__ == "__main__":
    # Test with default paths
    project_root = Path(__file__).parent.parent.parent
    cookies = project_root / "cookies.txt"
    music = project_root / "music"
    
    syncer = Syncer(cookies, music)
    print(f"Syncing to {music} using cookies from {cookies}...")
    # This will likely fail if cookies.txt doesn't exist
    # syncer.sync()
