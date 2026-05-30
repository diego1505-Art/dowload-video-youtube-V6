"""
Extracteur franime.fr — supporte SIBNET, FILEMOON, SENDVID, VIDMOLY et HLS direct.
Lance Chrome visible, intercepte les URLs des lecteurs, essaie chaque lecteur dans l'ordre.
"""
from __future__ import annotations
import re, os, shutil, tempfile
from pathlib import Path
from typing import Any

FRANIME_PATTERN = re.compile(r"https?://(?:www\.)?franime\.fr/", re.IGNORECASE)

# ── Lecteurs connus et leurs patterns d'URL ──────────────────────────────────
# Chaque entrée : (nom, regex de détection, blacklist regex)
LECTEURS = [
    ("SIBNET",   re.compile(r"sibnet\.ru/shell\.php",          re.I), re.compile(r"sibnet\.ru/export/|sbcount|/time", re.I)),
    ("FILEMOON", re.compile(r"filemoon\.\w+/",                 re.I), None),
    ("SENDVID",  re.compile(r"sendvid\.com/",                  re.I), None),
    ("VIDMOLY",  re.compile(r"vidmoly\.\w+/",                  re.I), None),
    ("HLS",      re.compile(r"\.(m3u8|mpd)(\?|$)",            re.I), None),
    ("WATCH2",   re.compile(r"franime\.fr/watch2/",            re.I), None),
]

SKIP_HOSTS = {
    "google-analytics", "googletagmanager", "doubleclick", "facebook",
    "twitter", "sentry", "fonts.google", "gstatic", "clarity.ms",
    "hotjar", "segment.io", "amplitude", "adskeeper", "bidgear",
    "a-ads.com", "betweendigital", "yandex", "mail.ru",
}

# Priorité de téléchargement (yt-dlp supporte tous ces domaines)
LECTEUR_PRIORITY = ["SIBNET", "FILEMOON", "SENDVID", "VIDMOLY", "HLS", "WATCH2"]


def is_franime_url(url: str) -> bool:
    return bool(FRANIME_PATTERN.match(url))


def _skip(url: str) -> bool:
    return url.startswith("blob:") or any(h in url for h in SKIP_HOSTS)


def _detect_lecteur(url: str) -> str | None:
    """Retourne le nom du lecteur détecté pour une URL donnée."""
    for name, pattern, blacklist in LECTEURS:
        if blacklist and blacklist.search(url):
            continue
        if pattern.search(url):
            return name
    return None


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
        for known_ext in (".mp4", ".webm", ".mkv", ".m4v", ".mov", ".avi", ".mp3", ".m4a", ".wav", ".aac", ".ogg", ".opus"):
            candidates.append(os.path.join(output_path, f"{title}{known_ext}"))

    unique_candidates: list[str] = []
    seen: set[str] = set()
    for path in candidates:
        normalized = os.path.normpath(path)
        if normalized not in seen:
            unique_candidates.append(normalized)
            seen.add(normalized)

    return unique_candidates


def _resolve_downloaded_file(info: dict[str, Any], output_path: str, before_mtime: dict[str, float]) -> str | None:
    ignored = {".part", ".ytdl", ".jpg", ".jpeg", ".png", ".webp", ".description"}

    for candidate in _candidate_filepaths_from_info(info, output_path):
        if os.path.isfile(candidate) and os.path.splitext(candidate)[1].lower() not in ignored:
            return candidate

    candidates = []
    for f in os.listdir(output_path):
        fp = os.path.join(output_path, f)
        mt = os.path.getmtime(fp)
        if f not in before_mtime or mt > before_mtime[f]:
            if os.path.splitext(f)[1].lower() not in ignored:
                candidates.append((mt, fp))

    candidates.sort(reverse=True)
    return candidates[0][1] if candidates else None


def extract_stream_url(page_url: str, preferred_lecteur: str | None = None) -> tuple[str | None, str | None]:
    """
    Retourne (url_stream, nom_lecteur) ou (None, None).
    preferred_lecteur : forcer un lecteur spécifique (ex: "FILEMOON")
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
    except ImportError:
        raise RuntimeError("Playwright non installé. Lance : pip install playwright && playwright install chromium")

    # dict lecteur → liste d'URLs trouvées
    captured: dict[str, list[str]] = {name: [] for name, *_ in LECTEURS}

    def on_request(req):
        url = req.url
        if _skip(url): return
        name = _detect_lecteur(url)
        if name and url not in captured[name]:
            print(f"  [franime] ✓ [{name}] {url[:110]}")
            captured[name].append(url)

    def on_response(resp):
        url = resp.url
        if _skip(url): return
        ct = resp.headers.get("content-type", "").lower()
        if any(t in ct for t in ["mpegurl", "dash+xml", "mp2t", "x-mpegurl"]):
            name = _detect_lecteur(url) or "HLS"
            if url not in captured.get(name, []):
                print(f"  [franime] ✓ [{name}] (content-type) {url[:100]}")
                captured.setdefault(name, []).append(url)
        if not any(e in url for e in [".js",".css",".png",".jpg",".svg",".woff",".ico",".gif",".webp",".wasm"]):
            print(f"  [franime]   {resp.status}  {url[:100]}")

    chrome_path   = _find_chrome()
    user_data_dir = _chrome_user_data_dir()
    tmp_profile   = None

    print(f"  [franime] Chrome : {chrome_path or 'Chromium Playwright'}")

    with sync_playwright() as pw:
        base_args = ["--no-sandbox", "--disable-blink-features=AutomationControlled", "--disable-infobars"]

        if chrome_path and user_data_dir and os.path.isdir(os.path.join(user_data_dir, "Default")):
            # Copie légère : cookies + session uniquement (pas le cache)
            tmp_profile = tempfile.mkdtemp(prefix="dowflow_")
            dst_default = os.path.join(tmp_profile, "Default")
            os.makedirs(dst_default, exist_ok=True)
            src_default = os.path.join(user_data_dir, "Default")
            for fname in ["Cookies", "Login Data", "Local State", "Preferences", "Web Data"]:
                src = os.path.join(src_default, fname)
                if os.path.isfile(src):
                    try: shutil.copy2(src, os.path.join(dst_default, fname))
                    except Exception: pass
            ls = os.path.join(user_data_dir, "Local State")
            if os.path.isfile(ls):
                try: shutil.copy2(ls, os.path.join(tmp_profile, "Local State"))
                except Exception: pass
            ctx = pw.chromium.launch_persistent_context(
                tmp_profile, headless=False, executable_path=chrome_path,
                args=base_args, no_viewport=True)
            page = ctx.new_page()
        else:
            browser = pw.chromium.launch(headless=False, executable_path=chrome_path or None, args=base_args)
            ctx = browser.new_context(viewport={"width": 1280, "height": 720}, locale="fr-FR")
            page = ctx.new_page()

        page.on("request",  on_request)
        page.on("response", on_response)

        try:
            page.goto(page_url, wait_until="domcontentloaded", timeout=45_000)
        except PwTimeout:
            print("  [franime] Timeout – on continue")

        # Si un lecteur préféré est demandé, le sélectionner dans le dropdown
        if preferred_lecteur:
            _select_lecteur(page, preferred_lecteur)

        # Clic "Regarder l'épisode"
        page.wait_for_timeout(2_000)
        for sel in ["button:has-text('Regarder')", "a:has-text('Regarder')", "button:has-text('épisode')"]:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.click(timeout=3_000)
                    print(f"  [franime] Clic 'Regarder' : {sel}")
                    page.wait_for_timeout(2_000)
                    break
            except Exception:
                pass

        # Attendre lecteur
        for sel in ["video", "iframe", "[class*='player']"]:
            try:
                page.wait_for_selector(sel, timeout=10_000)
                break
            except Exception:
                pass

        # Clic play
        for sel in ["video", ".play-button", "[class*='play']"]:
            try:
                el = page.query_selector(sel)
                if el:
                    el.click(timeout=2_000)
                    break
            except Exception:
                pass

        # Attendre jusqu'à 35 secondes
        for i in range(35):
            page.wait_for_timeout(1_000)
            total = sum(len(v) for v in captured.values())
            if total >= 1 and i >= 3:
                break

        ctx.close()

    if tmp_profile:
        try: shutil.rmtree(tmp_profile, ignore_errors=True)
        except Exception: pass

    # Choisir le meilleur lecteur disponible
    order = ([preferred_lecteur] if preferred_lecteur else []) + LECTEUR_PRIORITY
    for name in order:
        urls = captured.get(name, [])
        if urls:
            print(f"  [franime] Lecteur choisi : {name} → {urls[0][:100]}")
            return urls[0], name

    return None, None


def _select_lecteur(page, lecteur_name: str):
    """Essaie de sélectionner un lecteur spécifique dans le dropdown franime."""
    try:
        # Ouvrir le dropdown
        for sel in ["select", "[class*='lecteur']", "[class*='player-select']", "button:has-text('Lecteur')"]:
            el = page.query_selector(sel)
            if el:
                el.click(timeout=2_000)
                page.wait_for_timeout(500)
                # Chercher l'option du lecteur voulu
                opt = page.query_selector(f"option:has-text('{lecteur_name}'), li:has-text('{lecteur_name}')")
                if opt:
                    opt.click(timeout=2_000)
                    print(f"  [franime] Lecteur sélectionné : {lecteur_name}")
                    page.wait_for_timeout(1_500)
                    return
    except Exception as e:
        print(f"  [franime] Impossible de changer de lecteur : {e}")


def download_franime(url: str, output_path: str = "downloads",
                     download_type: str = "video", quality: str = "best") -> dict[str, Any]:
    os.makedirs(output_path, exist_ok=True)

    # Essayer chaque lecteur dans l'ordre jusqu'à succès
    lecteurs_a_essayer = [None] + LECTEUR_PRIORITY  # None = lecteur par défaut de la page

    last_error = None
    for lecteur in lecteurs_a_essayer:
        label = lecteur or "défaut"
        print(f"\n  [franime] Tentative avec lecteur : {label}")
        try:
            stream_url, nom = extract_stream_url(url, preferred_lecteur=lecteur)
            if not stream_url:
                print(f"  [franime] Aucun stream trouvé pour {label}, on essaie le suivant...")
                continue

            result = _download_stream(stream_url, nom or label, url, output_path, download_type, quality)
            if result.get("filename"):
                print(f"  [franime] ✓ Téléchargement réussi avec lecteur {nom or label}")
                return result

        except Exception as e:
            last_error = e
            print(f"  [franime] Lecteur {label} échoué : {e}")
            continue

    # Au lieu de lever une erreur, on retourne un dictionnaire indiquant l'échec
    # pour permettre à l'appelant de sauter l'épisode proprement.
    print(f"  [franime] ⚠ Aucun lecteur n'a fonctionné pour {url}")
    return {
        "success": False,
        "filename": None,
        "filepath": None,
        "error": f"Aucun lecteur disponible ou fonctionnel pour cette URL. (Dernière erreur: {last_error})",
        "reason": "not_found_or_blocked"
    }


def _download_stream(stream_url: str, lecteur_name: str, page_url: str,
                     output_path: str, download_type: str, quality: str) -> dict[str, Any]:
    import yt_dlp

    is_sibnet  = "sibnet.ru"   in stream_url
    is_filemoon= "filemoon"    in stream_url
    referer    = (
        "https://video.sibnet.ru/" if is_sibnet else
        "https://filemoon.sx/"     if is_filemoon else
        "https://franime.fr/"
    )

    slug   = re.search(r"/anime/([^?/]+)", page_url)
    title  = slug.group(1) if slug else "franime-video"
    params = dict(p.split("=",1) for p in page_url.split("?",1)[-1].split("&") if "=" in p)
    ep     = f"s{params.get('s','1')}-ep{params.get('ep','1')}-{params.get('lang','vo')}"

    ffmpeg_dir = _resolve_ffmpeg_dir()
    has_ffmpeg = bool(ffmpeg_dir) or (bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe")))

    ydl_opts: dict[str, Any] = {
        "format": "bestvideo+bestaudio/best" if download_type != "audio" else "bestaudio/best",
        "outtmpl": os.path.join(output_path, f"{title}-{ep}.%(ext)s"),
        "quiet": False,
        "noplaylist": True,
        # ── Vitesse ──────────────────────────────────────────────────────────
        "concurrent_fragment_downloads": 16,   # 16 fragments en parallèle
        "retries": 10,
        "fragment_retries": 10,
        "file_access_retries": 5,
        "http_chunk_size": 10 * 1024 * 1024,   # chunks de 10 MB
        "socket_timeout": 30,
        "http_headers": {
            "Referer":    referer,
            "Origin":     referer.rstrip("/"),
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        },
    }

    # aria2c = téléchargeur externe beaucoup plus rapide (16 connexions simultanées)
    if shutil.which("aria2c"):
        ydl_opts["external_downloader"] = "aria2c"
        ydl_opts["external_downloader_args"] = [
            "--max-connection-per-server=16", "--split=16",
            "--min-split-size=1M", "--continue=true",
        ]
        print("  [franime] aria2c détecté → téléchargement accéléré")
    if ffmpeg_dir: ydl_opts["ffmpeg_location"] = ffmpeg_dir
    if has_ffmpeg and download_type != "audio": ydl_opts["merge_output_format"] = "mp4"

    # Snapshot mtime avant téléchargement pour détecter nouveaux fichiers ET modifiés
    before_mtime: dict[str, float] = {
        f: os.path.getmtime(os.path.join(output_path, f))
        for f in os.listdir(output_path)
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(stream_url, download=True)

    selected = _resolve_downloaded_file(info, output_path, before_mtime)

    return {
        "title":      f"{title}-{ep}",
        "filepath":   selected,
        "filename":   os.path.basename(selected) if selected else None,
        "stream_url": stream_url,
        "lecteur":    lecteur_name,
    }


def _find_chrome() -> str | None:
    import platform
    if platform.system() == "Windows":
        for base in ["PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"]:
            p = os.path.join(os.environ.get(base, ""), "Google", "Chrome", "Application", "chrome.exe")
            if os.path.isfile(p): return p
    elif platform.system() == "Darwin":
        p = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if os.path.isfile(p): return p
    else:
        for cmd in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
            f = shutil.which(cmd); 
            if f: return f
    return None

def _chrome_user_data_dir() -> str | None:
    import platform
    if platform.system() == "Windows":
        return os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data")
    if platform.system() == "Darwin":
        return os.path.expanduser("~/Library/Application Support/Google/Chrome")
    return os.path.expanduser("~/.config/google-chrome")

def _resolve_ffmpeg_dir() -> str | None:
    if shutil.which("ffmpeg") and shutil.which("ffprobe"): return None
    p = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages" / \
        "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe" / "ffmpeg-8.1.1-full_build" / "bin"
    return str(p) if (p / "ffmpeg.exe").exists() else None
