import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_FOLDER = BASE_DIR / "downloads"

# FFmpeg configuration
FFMPEG_FALLBACK_DIRS = [
    Path(os.environ.get("LOCALAPPDATA", ""))
    / "Microsoft"
    / "WinGet"
    / "Packages"
    / "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
    / "ffmpeg-8.1.1-full_build"
    / "bin"
]

# Media settings
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mkv", ".m4v", ".mov", ".avi"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".wav", ".aac", ".opus", ".ogg"}
ALLOWED_LOCAL_EXTENSIONS = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS
PREVIEWABLE_EXTENSIONS = {
    ".mp4", ".webm",          # Vidéo HTML5 native
    ".mp3", ".m4a", ".aac",   # Audio universel
    ".ogg", ".opus", ".wav",  # Audio Firefox/Chrome
}

# App settings
LOCAL_MEDIA_TTL_SECONDS = 3600
LOCAL_MEDIA_MAX_ENTRIES = 128
