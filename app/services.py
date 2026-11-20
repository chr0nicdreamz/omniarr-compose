import os
import json
import shutil
import httpx
import re
from pathlib import Path
from sqlalchemy.orm import Session
from .models import MediaItem, AppSetting, IndexerConfig, QualityProfile, CustomFormat

def _parse_roots(env_key: str, default_paths: list[str]) -> list[Path]:
    raw = os.getenv(env_key, "")
    if raw.strip():
        return [Path(p.strip()) for p in raw.split(",") if p.strip()]
    return [Path(p) for p in default_paths]

MEDIA_ROOTS = {
    "movie": _parse_roots("OMNIARR_MOVIE_ROOTS", ["/data/media/movies"]),
    "tv": _parse_roots("OMNIARR_TV_ROOTS", ["/data/media/tv"]),
    "music": _parse_roots("OMNIARR_MUSIC_ROOTS", ["/data/media/music"]),
}

def get_media_roots(db: Session = None) -> dict[str, list[Path]]:
    """Returns configured root folders from SQLite settings, falling back to env vars / defaults."""
    if db is None:
        return MEDIA_ROOTS
    cfg = get_settings(db)
    resolved = {}
    for m_type in ("movie", "tv", "music"):
        raw = cfg.get(f"{m_type}_roots", "").strip()
        if raw:
            resolved[m_type] = [Path(p.strip()) for p in raw.split(",") if p.strip()]
        else:
            resolved[m_type] = list(MEDIA_ROOTS[m_type])
    return resolved

DEFAULT_SETTINGS = {
    "omniarr_api_key": os.getenv("OMNIARR_API_KEY", "change_me_to_a_secure_key"),
    "prowlarr_url": os.getenv("PROWLARR_URL", "http://localhost:9696"),
    "prowlarr_api_key": os.getenv("PROWLARR_API_KEY", ""),
    "plex_url": os.getenv("PLEX_URL", "http://localhost:32400"),
    "plex_token": os.getenv("PLEX_TOKEN", ""),
    "plex_notify_grab": "false",
    "plex_notify_import": "true",
    "jellyfin_url": os.getenv("JELLYFIN_URL", ""),
    "jellyfin_api_key": os.getenv("JELLYFIN_API_KEY", ""),
    "jellyfin_notify": "true",
    "emby_url": os.getenv("EMBY_URL", ""),
    "emby_api_key": os.getenv("EMBY_API_KEY", ""),
    "emby_notify": "true",
    "qbit_url": os.getenv("QBIT_URL", "http://localhost:8080"),
    "qbit_user": os.getenv("QBIT_USER", "admin"),
    "qbit_pass": os.getenv("QBIT_PASS", ""),
    "qbit_category": os.getenv("QBIT_CATEGORY", "omniarr"),
    "qbit_remote_path": os.getenv("QBIT_REMOTE_PATH", "plex_e/NAS/Downloads"),
    "qbit_local_mount": os.getenv("QBIT_LOCAL_MOUNT", "/mnt/plex_e/NAS/Downloads"),
    "import_mode": "hardlink",
    "remove_completed_download": "false",
    "min_free_space_gb": "20",
    "movie_roots": "",
    "tv_roots": "",
    "music_roots": "",
    "movie_folder_format": "{Title} ({Year})",
    "tv_folder_format": "{Title} ({Year})",
    "music_folder_format": "{Title}",
    "default_movie_quality": "1080p",
    "default_tv_quality": "1080p",
    "default_music_quality": "lossless",
    "min_seeders": "3",
    "max_size_gb": "40",
    "preferred_keywords": "REMUX, WEB-DL, BluRay, 10bit, Atmos, FLAC",
    "blocked_keywords": "CAM, HDCAM, TS, TELESYNC, TC, SCREENER, R5",
    "rss_sync_minutes": "2",
}

QUALITY_KEYWORDS = {
    "2160p": ["2160p", "4k", "uhd"],
    "1080p": ["1080p"],
    "720p": ["720p"],
    "lossless": ["flac", "alac", "24bit"],
    "any": [""],
}

def get_settings(db: Session) -> dict:
    cfg = DEFAULT_SETTINGS.copy()
    for row in db.query(AppSetting).all():
        if row.value is not None and row.value != "":
            cfg[row.key] = row.value
        elif row.key in ("plex_notify_grab", "plex_notify_import", "remove_completed_download", "blocked_keywords", "preferred_keywords"):
            cfg[row.key] = row.value
    return cfg

def save_settings(db: Session, updates: dict):
    for k, v in updates.items():
        if k not in DEFAULT_SETTINGS:
            continue
        row = db.query(AppSetting).filter_by(key=k).first()
        if row:
            row.value = str(v)
        else:
            db.add(AppSetting(key=k, value=str(v)))
    db.commit()

import time as _time
_DRIVE_CACHE = {"ts": 0.0, "data": []}

def get_drive_stats(db: Session = None, force: bool = False) -> list:
    now = _time.time()
    if not force and _DRIVE_CACHE["data"] and (now - _DRIVE_CACHE["ts"]) < 60:
        return _DRIVE_CACHE["data"]
    seen = set()
    drives = []
    for roots in get_media_roots(db).values():
        for p in roots:
            mount = p.parent
            if str(mount) in seen:
                continue
            seen.add(str(mount))
            if mount.exists():
                usage = shutil.disk_usage(mount)
                total_gb = round(usage.total / (1024**3), 1)
                free_gb = round(usage.free / (1024**3), 1)
                used_pct = round(((usage.total - usage.free) / usage.total) * 100, 1) if usage.total else 0
                drives.append({"mount": str(mount), "free_gb": free_gb, "total_gb": total_gb, "used_pct": used_pct})
    return sorted(drives, key=lambda d: d["mount"])

def translate_qbit_path(remote_path: str, cfg: dict) -> Path:
    normalized = remote_path.replace("\\", "/")
    marker = cfg["qbit_remote_path"].strip("/")
    if marker and marker.lower() in normalized.lower():
        idx = normalized.lower().index(marker.lower()) + len(marker)
        relative = normalized[idx:].lstrip("/")
        return Path(cfg["qbit_local_mount"]) / relative
    return Path(normalized)

def get_best_dest_dir(media_type: str, cfg: dict, db: Session = None) -> Path:
    min_free_bytes = float(cfg.get("min_free_space_gb", 20)) * (1024**3)
    roots_map = get_media_roots(db)
    type_roots = roots_map.get(media_type, MEDIA_ROOTS[media_type])
    candidates = [p for p in type_roots if p.exists()]
    if not candidates:
        fallback = type_roots[0]
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback
    eligible = [p for p in candidates if shutil.disk_usage(p).free >= min_free_bytes]
    pool = eligible if eligible else candidates
    return max(pool, key=lambda p: shutil.disk_usage(p).free)

def evaluate_custom_formats(title: str, profile_name: str, db: Session) -> tuple[int, int]:
    """Returns (total_cf_score, min_required_score) using imported Sonarr/Radarr Custom Formats."""
    profile = db.query(QualityProfile).filter_by(name=profile_name).first()
    scores_map = json.loads(profile.format_scores_json) if profile and profile.format_scores_json else {}
    min_score = profile.min_format_score if profile else 0

    total_score = 0
    for cf in db.query(CustomFormat).all():
        weight = scores_map.get(str(cf.external_cf_id), cf.default_score)
        if not weight:
            continue
        patterns = json.loads(cf.regex_patterns_json or "[]")
        for pat in patterns:
            try:
                if re.search(pat, title, re.IGNORECASE):
                    total_score += int(weight)
                    break
            except re.error:
                continue
    return total_score, min_score

def score_release(release: dict, cfg: dict, cf_score: int = 0) -> int:
    title = release.get("title", "").lower()
    score = release.get("seeders", 0) + cf_score
    preferred = [k.strip().lower() for k in cfg.get("preferred_keywords", "").split(",") if k.strip()]
    for kw in preferred:
        if kw in title:
            score += 50
    return score

async def search_prowlarr(query: str, media_type: str, quality: str, db: Session):
    cfg = get_settings(db)
    cat_map = {"movie": [2000], "tv": [5000], "music": [3000]}
    params = {"query": query, "type": "search"}
    for c in cat_map.get(media_type, []):
        params.setdefault("categories", []).append(c)

    headers = {"X-Api-Key": cfg["prowlarr_api_key"]}
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(f"{cfg['prowlarr_url'].rstrip('/')}/api/v1/search", params=params, headers=headers)
        resp.raise_for_status()
        results = resp.json()

    disabled_indexers = {
        idx.name.lower() for idx in db.query(IndexerConfig).filter_by(enabled=False).all()
    }
    min_seeders = int(cfg.get("min_seeders", 1))
    max_size_bytes = float(cfg.get("max_size_gb", 100)) * (1024**3)
    blocked = [k.strip().lower() for k in cfg.get("blocked_keywords", "").split(",") if k.strip()]

    # Match either built-in quality tier or imported QualityProfile cutoff
    profile_obj = db.query(QualityProfile).filter_by(name=quality).first()
    effective_quality = (profile_obj.cutoff if profile_obj else quality).lower()
    keywords = QUALITY_KEYWORDS.get(effective_quality, [""])
    for tier, kws in QUALITY_KEYWORDS.items():
        if tier in effective_quality:
            keywords = kws
            break

    scored_releases = []
    for r in results:
        raw_title = r.get("title", "")
        title_l = raw_title.lower()
        indexer_l = r.get("indexer", "").lower()
        if indexer_l in disabled_indexers:
            continue
        if r.get("seeders", 0) < min_seeders:
            continue
        if max_size_bytes > 0 and r.get("size", 0) > max_size_bytes:
            continue
        if any(b in title_l for b in blocked):
            continue
        if not any(k in title_l for k in keywords):
            continue

        cf_score, min_cf_score = evaluate_custom_formats(raw_title, quality, db)
        if cf_score < min_cf_score:
            continue

        r["_omni_score"] = score_release(r, cfg, cf_score)
        scored_releases.append(r)

    scored_releases.sort(key=lambda x: x.get("_omni_score", 0), reverse=True)
    return scored_releases

async def sync_posters_from_plex(db: Session) -> int:
    """Matches OmniArr library items against Plex metadata to populate poster_url."""
    cfg = get_settings(db)
    plex_url = cfg.get("plex_url", "").rstrip("/")
    token = cfg.get("plex_token", "")
    if not plex_url or not token:
        return 0

    updated = 0
    headers = {"Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=5.0)) as client:
            sec_resp = await client.get(f"{plex_url}/library/sections", params={"X-Plex-Token": token}, headers=headers)
            sec_resp.raise_for_status()
            sections = sec_resp.json().get("MediaContainer", {}).get("Directory", [])

            plex_posters = {}  # (title_lower, year) -> thumb path
            for sec in sections:
                key = sec.get("key")
                all_resp = await client.get(f"{plex_url}/library/sections/{key}/all", params={"X-Plex-Token": token}, headers=headers)
                if all_resp.status_code != 200:
                    continue
                for meta in all_resp.json().get("MediaContainer", {}).get("Metadata", []):
                    t = meta.get("title", "").strip().lower()
                    y = meta.get("year")
                    thumb = meta.get("thumb")
                    if t and thumb:
                        plex_posters[(t, y)] = thumb
                        plex_posters[(t, None)] = thumb

            for item in db.query(MediaItem).all():
                key_exact = (item.title.strip().lower(), item.year)
                key_fallback = (item.title.strip().lower(), None)
                thumb = plex_posters.get(key_exact) or plex_posters.get(key_fallback)
                if thumb:
                    full_url = f"{plex_url}{thumb}?X-Plex-Token={token}"
                    if item.poster_url != full_url:
                        item.poster_url = full_url
                        updated += 1
            for srv_prefix in ("jellyfin", "emby"):
                s_url = cfg.get(f"{srv_prefix}_url", "").rstrip("/")
                s_key = cfg.get(f"{srv_prefix}_api_key", "").strip()
                if not s_url or not s_key:
                    continue
                try:
                    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0)) as client:
                        resp = await client.get(
                            f"{s_url}/Items",
                            params={"Recursive": "true", "IncludeItemTypes": "Movie,Series", "Fields": "ProviderIds"},
                            headers={"X-Emby-Token": s_key}
                        )
                        if resp.status_code == 200:
                            for j_item in resp.json().get("Items", []):
                                j_id = j_item.get("Id")
                                j_title = (j_item.get("Name") or "").lower()
                                j_year = j_item.get("ProductionYear")
                                if not j_id:
                                    continue
                                img_url = f"{s_url}/Items/{j_id}/Images/Primary?maxWidth=400&api_key={s_key}"
                                for m in items:
                                    if m.poster_url:
                                        continue
                                    if m.title.lower() == j_title and (not m.year or not j_year or m.year == j_year):
                                        m.poster_url = img_url
                                        updated += 1
                except Exception:
                    pass

        db.commit()
    except Exception:
        pass
    return updated

async def send_to_qbittorrent(download_url: str, item_id: int, db: Session) -> str:
    cfg = get_settings(db)
    qbit_url = cfg["qbit_url"].rstrip("/")
    tag = f"omniarr_{item_id}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        await client.post(
            f"{qbit_url}/api/v2/auth/login",
            data={"username": cfg["qbit_user"], "password": cfg["qbit_pass"]}
        )
        await client.post(
            f"{qbit_url}/api/v2/torrents/add",
            data={
                "urls": download_url,
                "category": cfg["qbit_category"],
                "tags": tag
            }
        )
        resp = await client.get(f"{qbit_url}/api/v2/torrents/info", params={"tag": tag})
        torrents = resp.json()
        if torrents and cfg.get("plex_notify_grab") == "true":
            await trigger_plex_scan(cfg)
        return torrents[0]["hash"] if torrents else ""

async def trigger_plex_scan(cfg: dict):
    """Notifies Plex, Jellyfin, and/or Emby to refresh libraries after a grab or import."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        if cfg.get("plex_url") and cfg.get("plex_token"):
            try:
                await client.get(
                    f"{cfg['plex_url'].rstrip('/')}/library/sections/all/refresh",
                    params={"X-Plex-Token": cfg["plex_token"]}
                )
            except Exception:
                pass

        if cfg.get("jellyfin_url") and cfg.get("jellyfin_api_key") and cfg.get("jellyfin_notify", "true") == "true":
            try:
                await client.post(
                    f"{cfg['jellyfin_url'].rstrip('/')}/Library/Refresh",
                    headers={"X-Emby-Token": cfg["jellyfin_api_key"]}
                )
            except Exception:
                pass

        if cfg.get("emby_url") and cfg.get("emby_api_key") and cfg.get("emby_notify", "true") == "true":
            try:
                await client.post(
                    f"{cfg['emby_url'].rstrip('/')}/Library/Refresh",
                    headers={"X-Emby-Token": cfg["emby_api_key"]}
                )
            except Exception:
                pass


def transfer_file(src: Path, dst: Path, mode: str):
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        return
    if mode == "move":
        shutil.move(str(src), str(dst))
    elif mode == "copy":
        shutil.copy2(src, dst)
    else:
        try:
            os.link(src, dst)
        except OSError:
            shutil.copy2(src, dst)

async def check_downloads_and_import(db: Session):
    downloading = db.query(MediaItem).filter(MediaItem.status == "downloading").all()
    if not downloading:
        return

    cfg = get_settings(db)
    qbit_url = cfg["qbit_url"].rstrip("/")
    async with httpx.AsyncClient(timeout=15.0) as client:
        await client.post(
            f"{qbit_url}/api/v2/auth/login",
            data={"username": cfg["qbit_user"], "password": cfg["qbit_pass"]}
        )
        resp = await client.get(f"{qbit_url}/api/v2/torrents/info", params={"category": cfg["qbit_category"]})
        torrents = {t["hash"]: t for t in resp.json()}

    imported_any = False
    mode = cfg.get("import_mode", "hardlink")

    for item in downloading:
        t = torrents.get(item.torrent_hash)
        if not t:
            continue
        if t.get("progress", 0) >= 1.0:
            content_path = translate_qbit_path(t["content_path"], cfg)
            fmt_key = f"{item.media_type}_folder_format"
            template = cfg.get(fmt_key, "{Title} ({Year})")
            folder_name = template.replace("{Title}", item.title).replace("{Year}", str(item.year or "")).replace("()", "").strip()
            clean_name = re.sub(r'[\\/*?:"<>|]', "", folder_name)
            target_base = get_best_dest_dir(item.media_type, cfg, db) / clean_name

            if content_path.is_file():
                transfer_file(content_path, target_base / content_path.name, mode)
            elif content_path.is_dir():
                for root, _, files in os.walk(content_path):
                    for f in files:
                        if f.lower().endswith((".mkv", ".mp4", ".avi", ".flac", ".mp3", ".m4a")):
                            src_file = Path(root) / f
                            transfer_file(src_file, target_base / f, mode)

            if cfg.get("remove_completed_download") == "true":
                async with httpx.AsyncClient(timeout=10.0) as client:
                    await client.post(f"{qbit_url}/api/v2/auth/login", data={"username": cfg["qbit_user"], "password": cfg["qbit_pass"]})
                    await client.post(f"{qbit_url}/api/v2/torrents/delete", data={"hashes": item.torrent_hash, "deleteFiles": "true"})

            item.status = "available"
            item.file_path = str(target_base)
            db.commit()
            imported_any = True

    if imported_any:
        if cfg.get("plex_notify_import", "true") == "true":
            await trigger_plex_scan(cfg)
        await sync_posters_from_plex(db)
