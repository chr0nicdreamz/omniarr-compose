import os
import shutil
import httpx
import re
from pathlib import Path
from sqlalchemy.orm import Session
from .models import MediaItem

PROWLARR_URL = os.getenv("PROWLARR_URL", "http://prowlarr:9696")
PROWLARR_API_KEY = os.getenv("PROWLARR_API_KEY", "")
QBIT_URL = os.getenv("QBIT_URL", "http://qbittorrent:8080")
QBIT_USER = os.getenv("QBIT_USER", "admin")
QBIT_PASS = os.getenv("QBIT_PASS", "adminadmin")
QBIT_CATEGORY = os.getenv("QBIT_CATEGORY", "omniarr")
PLEX_URL = os.getenv("PLEX_URL", "http://plex:32400")
PLEX_TOKEN = os.getenv("PLEX_TOKEN", "")

# Remote Path Mapping (Matches any Windows drive/UNC path containing plex_e/NAS/Downloads)
QBIT_REMOTE_PATH = os.getenv("QBIT_REMOTE_PATH", "plex_e/NAS/Downloads")
QBIT_LOCAL_MOUNT = os.getenv("QBIT_LOCAL_MOUNT", "/mnt/plex_e/NAS/Downloads")

# Your bound Plex media folders across all drives (excluding plex_e download drive)
MEDIA_ROOTS = {
    "movie": [
        Path("/mnt/plex_g/Movies"),
        Path("/mnt/plex_h/Movies"),
        Path("/mnt/plex_i/Movies"),
        Path("/mnt/plex_j/Movies"),
        Path("/mnt/plex_l/Movies"),
        Path("/mnt/plex_m/Movies"),
        Path("/mnt/plex_n/Movies"),
        Path("/mnt/plex_o/Movies"),
        Path("/mnt/plex_p/Moviez"),
        Path("/mnt/plex_q/Movies"),
        Path("/mnt/plex_r/Movies"),
    ],
    "tv": [
        Path("/mnt/plex_g/Tv Shows"),
        Path("/mnt/plex_h/TV"),
        Path("/mnt/plex_i/TV Shows"),
        Path("/mnt/plex_j/TV"),
        Path("/mnt/plex_l/TV"),
        Path("/mnt/plex_m/TV Shows"),
        Path("/mnt/plex_n/TV"),
        Path("/mnt/plex_o/TV"),
        Path("/mnt/plex_p/TV"),
        Path("/mnt/plex_q/TV"),
        Path("/mnt/plex_r/TV"),
    ],
    "music": [
        Path("/mnt/plex_i/Music"),
        Path("/mnt/plex_m/Music"),
        Path("/mnt/plex_o/Music"),
        Path("/mnt/plex_p/Music"),
        Path("/mnt/plex_r/Music"),
    ],
}

QUALITY_KEYWORDS = {
    "2160p": ["2160p", "4k", "uhd"],
    "1080p": ["1080p"],
    "720p": ["720p"],
    "lossless": ["flac", "alac", "24bit"],
    "any": [""]
}


def translate_qbit_path(remote_path: str) -> Path:
    """Translates qBittorrent's Windows/UNC path to OmniArr's local /mnt/plex_e mount."""
    normalized = remote_path.replace("\\", "/")
    marker = QBIT_REMOTE_PATH.strip("/")
    if marker and marker.lower() in normalized.lower():
        idx = normalized.lower().index(marker.lower()) + len(marker)
        relative = normalized[idx:].lstrip("/")
        return Path(QBIT_LOCAL_MOUNT) / relative
    return Path(normalized)


def get_best_dest_dir(media_type: str) -> Path:
    """Selects the existing Plex drive folder with the most free disk space."""
    candidates = [p for p in MEDIA_ROOTS.get(media_type, []) if p.exists()]
    if not candidates:
        fallback = MEDIA_ROOTS[media_type][0]
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback
    return max(candidates, key=lambda p: shutil.disk_usage(p).free)


async def search_prowlarr(query: str, media_type: str, quality: str = "1080p"):
    """Queries Prowlarr's unified search API and filters by quality and seeders."""
    cat_map = {"movie": [2000], "tv": [5000], "music": [3000]}
    params = {"query": query, "type": "search"}
    for c in cat_map.get(media_type, []):
        params.setdefault("categories", []).append(c)

    headers = {"X-Api-Key": PROWLARR_API_KEY}
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(f"{PROWLARR_URL}/api/v1/search", params=params, headers=headers)
        resp.raise_for_status()
        results = resp.json()

    keywords = QUALITY_KEYWORDS.get(quality.lower(), [""])
    filtered = [
        r for r in results
        if any(k in r.get("title", "").lower() for k in keywords) and r.get("seeders", 0) > 0
    ]
    filtered.sort(key=lambda x: x.get("seeders", 0), reverse=True)
    return filtered


async def send_to_qbittorrent(download_url: str, item_id: int) -> str:
    """Logs into qBittorrent and adds the torrent with a tracking tag."""
    tag = f"omniarr_{item_id}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        await client.post(
            f"{QBIT_URL}/api/v2/auth/login",
            data={"username": QBIT_USER, "password": QBIT_PASS}
        )
        await client.post(
            f"{QBIT_URL}/api/v2/torrents/add",
            data={
                "urls": download_url,
                "category": QBIT_CATEGORY,
                "tags": tag
            }
        )
        resp = await client.get(f"{QBIT_URL}/api/v2/torrents/info", params={"tag": tag})
        torrents = resp.json()
        return torrents[0]["hash"] if torrents else ""


async def trigger_plex_scan():
    """Notifies Plex to scan updated library sections."""
    if not PLEX_TOKEN:
        return
    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.get(f"{PLEX_URL}/library/sections/all/refresh", params={"X-Plex-Token": PLEX_TOKEN})


def hardlink_or_copy(src: Path, dst: Path):
    """Creates a hardlink if on the same filesystem, otherwise copies the file."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        return
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


async def check_downloads_and_import(db: Session):
    """Background worker: checks qBittorrent for completed grabs and imports them."""
    downloading = db.query(MediaItem).filter(MediaItem.status == "downloading").all()
    if not downloading:
        return

    async with httpx.AsyncClient(timeout=15.0) as client:
        await client.post(
            f"{QBIT_URL}/api/v2/auth/login",
            data={"username": QBIT_USER, "password": QBIT_PASS}
        )
        resp = await client.get(f"{QBIT_URL}/api/v2/torrents/info", params={"category": QBIT_CATEGORY})
        torrents = {t["hash"]: t for t in resp.json()}

    imported_any = False
    for item in downloading:
        t = torrents.get(item.torrent_hash)
        if not t:
            continue
        if t.get("progress", 0) >= 1.0:
            content_path = translate_qbit_path(t["content_path"])
            clean_name = re.sub(r'[\\/*?:"<>|]', "", f"{item.title} ({item.year})" if item.year else item.title)
            target_base = get_best_dest_dir(item.media_type) / clean_name

            if content_path.is_file():
                hardlink_or_copy(content_path, target_base / content_path.name)
            elif content_path.is_dir():
                for root, _, files in os.walk(content_path):
                    for f in files:
                        if f.lower().endswith((".mkv", ".mp4", ".avi", ".flac", ".mp3", ".m4a")):
                            src_file = Path(root) / f
                            hardlink_or_copy(src_file, target_base / f)

            item.status = "available"
            item.file_path = str(target_base)
            db.commit()
            imported_any = True

    if imported_any:
        await trigger_plex_scan()
