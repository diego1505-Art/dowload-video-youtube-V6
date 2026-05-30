import os
import re
import shutil
from typing import Any
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yt_dlp

from config import (
    VIDEO_EXTENSIONS,
    AUDIO_EXTENSIONS,
)
from utils import _resolve_ffmpeg_dir

IGNORED_EXTENSIONS = {".part", ".ytdl", ".jpg", ".jpeg", ".png", ".webp", ".description"}
VALID_DOWNLOAD_TYPES = {"audio", "video"}
VALID_QUALITIES = {"best", "ultra", "qhd", "high", "medium", "low"}
CONTAINER_SEGMENTS = {"anime", "watch", "video", "videos", "embed", "episode", "show"}


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "media"


def _derive_series_slug(parsed_url) -> str:
    raw_segments = [segment for segment in parsed_url.path.split("/") if segment.strip()]
    segments = [_slugify(segment) for segment in raw_segments if _slugify(segment)]
    if not segments:
        return "remote"
    if len(segments) >= 2 and segments[0] in CONTAINER_SEGMENTS:
        return segments[1]
    return segments[-1]


def _build_output_folder(url: str, base_output_path: str) -> str:
    parsed = urlparse(url)
    domain = parsed.netloc.lower().replace("www.", "") or "remote"

    if domain in {"youtube.com", "m.youtube.com", "youtu.be"}:
        return os.path.join(base_output_path, "youtube")

    query = parse_qs(parsed.query)
    folder_parts = [_slugify(domain.replace(".", "-")), _derive_series_slug(parsed)]

    stable_id = None
    for key in ("anime_id", "series_id", "show_id", "id"):
        values = query.get(key)
        if values and values[0].strip():
            stable_id = _slugify(values[0])
            break
    if stable_id:
        folder_parts.append(f"id-{stable_id}")

    season_value = None
    for key in ("s", "season"):
        values = query.get(key)
        if values and values[0].strip():
            season_value = _slugify(values[0])
            break
    if season_value:
        folder_parts.append(f"s-{season_value}")

    folder_name = "-".join(part for part in folder_parts if part)
    return os.path.join(base_output_path, folder_name)


def _relative_download_path(filepath: str, base_output_path: str) -> str:
    return os.path.relpath(filepath, start=base_output_path).replace("\\", "/")


def _collect_created_files(output_path: str, before_files: set[str]) -> list[str]:
    created_files: list[str] = []
    for name in os.listdir(output_path):
        full_path = os.path.join(output_path, name)
        if os.path.isfile(full_path) and name not in before_files:
            created_files.append(full_path)
    created_files.sort(key=os.path.getmtime, reverse=True)
    return created_files


def _candidate_filepaths_from_info(info: dict[str, Any], output_path: str) -> list[str]:
    candidates: list[str] = []

    requested_downloads = info.get("requested_downloads")
    if isinstance(requested_downloads, list):
        for item in requested_downloads:
            if isinstance(item, dict):
                filepath = item.get("filepath")
                if isinstance(filepath, str) and filepath:
                    candidates.append(filepath)

    for key in ("filepath", "_filename"):
        value = info.get(key)
        if isinstance(value, str) and value:
            candidates.append(value)

    title = info.get("title")
    ext = info.get("ext")
    if isinstance(title, str) and title:
        if isinstance(ext, str) and ext:
            candidates.append(os.path.join(output_path, f"{title}.{ext}"))
        for known_ext in sorted(VIDEO_EXTENSIONS | AUDIO_EXTENSIONS):
            candidates.append(os.path.join(output_path, f"{title}{known_ext}"))

    unique_candidates: list[str] = []
    seen: set[str] = set()
    for path in candidates:
        normalized = os.path.normpath(path)
        if normalized not in seen:
            unique_candidates.append(normalized)
            seen.add(normalized)

    return unique_candidates


def _pick_media_file(created_files: list[str], download_type: str) -> str | None:
    allowed_exts = AUDIO_EXTENSIONS if download_type == "audio" else VIDEO_EXTENSIONS | AUDIO_EXTENSIONS

    for path in created_files:
        suffix = os.path.splitext(path)[1].lower()
        if suffix in IGNORED_EXTENSIONS:
            continue
        if suffix in allowed_exts:
            return path

    for path in created_files:
        suffix = os.path.splitext(path)[1].lower()
        if suffix not in IGNORED_EXTENSIONS:
            return path

    return None


def _resolve_downloaded_file(info: dict[str, Any], created_files: list[str], output_path: str, download_type: str) -> str | None:
    for candidate in _candidate_filepaths_from_info(info, output_path):
        if os.path.isfile(candidate):
            suffix = os.path.splitext(candidate)[1].lower()
            if suffix not in IGNORED_EXTENSIONS:
                return candidate

    return _pick_media_file(created_files, download_type)


def _build_video_format(quality: str, has_ffmpeg: bool) -> str:
    quality_map = {
        "best": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
        "ultra": "bestvideo[height<=2160][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=2160]+bestaudio/best",
        "qhd": "bestvideo[height<=1440][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1440]+bestaudio/best",
        "high": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best",
        "medium": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best",
        "low": "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best",
    }
    if has_ffmpeg:
        return quality_map.get(quality, quality_map["best"])

    max_height_map = {
        "ultra": 2160,
        "qhd": 1440,
        "high": 1080,
        "medium": 720,
        "low": 480,
        "best": 2160,
    }
    max_height = max_height_map.get(quality, 2160)
    return f"best[ext=mp4][acodec!=none][height<={max_height}]/best[acodec!=none][ext=mp4]/best[acodec!=none]/best"


def download_media(url, output_path="downloads", download_type="video", quality="best") -> dict[str, Any]:
    """
    Point d'entrée unique :
    1. franime.fr  → extracteur dédié
    2. Sites yt-dlp → yt-dlp direct
    3. Autres       → extracteur navigateur générique
    """
    import re as _re

    download_type = str(download_type).lower().strip()
    quality = str(quality).lower().strip()

    if download_type not in VALID_DOWNLOAD_TYPES:
        download_type = "video"
    if quality not in VALID_QUALITIES:
        quality = "best"

    base_output_path = output_path
    target_output_path = _build_output_folder(url, base_output_path)
    os.makedirs(target_output_path, exist_ok=True)

    # ── 1. Franime ───────────────────────────────────────────────────────────
    if _re.match(r"https?://(?:www\.)?franime\.fr/", url, _re.I):
        try:
            from franime_extractor import download_franime
            result = download_franime(url, output_path=target_output_path,
                                      download_type=download_type, quality=quality)
        except ImportError:
            raise RuntimeError("franime_extractor.py introuvable.")
    else:
        # ── 2. yt-dlp standard ───────────────────────────────────────────────────
        try:
            result = _ydlp_download(url, output_path=target_output_path,
                                    download_type=download_type, quality=quality)
        except Exception as e:
            # Détection via type d'exception yt-dlp (robuste aux changements de messages)
            try:
                from yt_dlp.utils import UnsupportedError, ExtractorError
                is_unsupported = isinstance(e, (UnsupportedError, ExtractorError))
            except ImportError:
                is_unsupported = False

            if not is_unsupported:
                msg = str(e).lower()
                is_unsupported = any(k in msg for k in ("unsupported url", "no video formats", "unable to extract"))

            if is_unsupported:
                print("  [yt-dlp] Non supporté → extracteur navigateur générique")
                try:
                    from browser_extractor import download_with_browser
                    result = download_with_browser(url, output_path=target_output_path,
                                                   download_type=download_type, quality=quality)
                except ImportError:
                    raise RuntimeError("browser_extractor.py introuvable.")
            else:
                raise

    filepath = result.get("filepath")
    if filepath:
        result["relative_path"] = _relative_download_path(filepath, base_output_path)
        result["filename"] = os.path.basename(filepath)
    result["folder"] = os.path.relpath(target_output_path, start=base_output_path).replace("\\", "/")
    return result


def _ydlp_download(url, output_path="downloads", download_type="video", quality="best") -> dict[str, Any]:
    """Téléchargement yt-dlp interne."""
    os.makedirs(output_path, exist_ok=True)

    ffmpeg_dir = _resolve_ffmpeg_dir()
    has_ffmpeg = bool(ffmpeg_dir) or (bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe")))

    if download_type == "audio":
        format_str = "bestaudio[ext=m4a]/bestaudio/best"
        postprocessor = {
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        } if has_ffmpeg else None
    elif download_type == "video":
        format_str = _build_video_format(quality, has_ffmpeg)
        postprocessor = None
    else:
        format_str = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
        if not has_ffmpeg:
            format_str = "best[ext=mp4][acodec!=none]/best[acodec!=none]/best"
        postprocessor = None

    ydl_opts = {
        "format": format_str,
        "outtmpl": os.path.join(output_path, "%(title)s.%(ext)s").replace("\\", "/"),
        "quiet": False,
        "no_warnings": False,
        "postprocessors": [postprocessor] if postprocessor else [],
        "prefer_ffmpeg": has_ffmpeg,
        "merge_output_format": "mp4" if download_type != "audio" else None,
        "noplaylist": True,
        "concurrent_fragment_downloads": 16,
        "retries": 10,
        "fragment_retries": 10,
        "file_access_retries": 5,
        "socket_timeout": 30,
        "http_chunk_size": 10485760,
    }
    if ffmpeg_dir:
        ydl_opts["ffmpeg_location"] = ffmpeg_dir

    # aria2c : téléchargeur externe plus rapide si disponible
    if shutil.which("aria2c"):
        ydl_opts["external_downloader"] = "aria2c"
        ydl_opts["external_downloader_args"] = [
            "--max-connection-per-server=16",
            "--split=16",
            "--min-split-size=1M",
            "--continue=true",
        ]
        print("  [aria2c] Téléchargeur rapide activé")

    before_files = set(os.listdir(output_path))

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # pyright: ignore[reportArgumentType]
            print(f"Downloading: {url}")
            print(
                f"Type: {download_type.upper()}"
                + (f" | Quality: {quality.upper()}" if download_type == "video" else "")
            )
            info = ydl.extract_info(url, download=True)
            print(f"Downloaded: {info.get('title')}")

        created_files = _collect_created_files(output_path, before_files)
        selected_file = _resolve_downloaded_file(info, created_files, output_path, download_type)

        return {
            "title": info.get("title") if isinstance(info, dict) else "download",
            "filepath": selected_file,
            "filename": os.path.basename(selected_file) if selected_file else None,
            "info": info,
        }
    except Exception as e:
        if "ffmpeg" in str(e).lower() or "ffprobe" in str(e).lower():
            print(f"\nFFmpeg Error: {e}")
            print("\nFFmpeg is required for audio conversion.")
            print("To install FFmpeg:")
            print("  - Download from: https://ffmpeg.org/download.html")
            print("  - Or use: choco install ffmpeg (requires admin)")
            print("  - Or use: winget install ffmpeg")
            print("\nAlternatively, download video as-is without conversion.")
        else:
            print(f"Error: {e}")
        raise


if __name__ == "__main__":
    print("=" * 50)
    print("DowFlow - Multi-Site Media Downloader")
    print("=" * 50)

    video_url = input("Enter YouTube URL: ").strip()

    print("\nDownload Options:")
    print("1. Audio Only (MP3)")
    print("2. Video (Best Quality)")
    print("3. Video with Custom Quality")

    choice = input("\nSelect option (1-3): ").strip()

    if choice == "1":
        download_media(video_url, download_type="audio")
    elif choice == "2":
        download_media(video_url, download_type="video", quality="best")
    elif choice == "3":
        print("\nQuality Options:")
        print("1. Auto max")
        print("2. 2160p")
        print("3. 1440p")
        print("4. 1080p")
        print("5. 720p")
        print("6. 480p")

        quality_choice = input("\nSelect quality (1-6): ").strip()
        quality_map = {
            "1": "best",
            "2": "ultra",
            "3": "qhd",
            "4": "high",
            "5": "medium",
            "6": "low",
        }
        quality = quality_map.get(quality_choice, "best")
        download_media(video_url, download_type="video", quality=quality)
    else:
        print("Invalid option. Using best quality video download.")
        download_media(video_url)
