import os
import json
import shutil
import httpx
import re
from pathlib import Path
from sqlalchemy.orm import Session
from .models import EpisodeItem, MediaItem, AppSetting, IndexerConfig, BlacklistedRelease, HistoryEvent, QualityProfile, CustomFormat

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
    "tautulli_url": os.getenv("TAUTULLI_URL", ""),
    "tautulli_api_key": os.getenv("TAUTULLI_API_KEY", ""),
    "tmdb_api_key": os.getenv("TMDB_API_KEY", ""),
    "omdb_api_key": os.getenv("OMDB_API_KEY", ""),
    "qbit_url": os.getenv("QBIT_URL", "http://localhost:8080"),
    "qbit_user": os.getenv("QBIT_USER", "admin"),
    "qbit_pass": os.getenv("QBIT_PASS", ""),
    "qbit_category": os.getenv("QBIT_CATEGORY", "omniarr"),
    "qbit_remote_path": os.getenv("QBIT_REMOTE_PATH", "/downloads"),
    "qbit_local_mount": os.getenv("QBIT_LOCAL_MOUNT", "/downloads"),
    "import_mode": "hardlink",
    "remove_completed_download": "false",
    "min_free_space_gb": "20",
    "movie_roots": "",
    "tv_roots": "",
    "music_roots": "",
    "movie_folder_format": "{Title} ({Year})",
    "tv_folder_format": "{Title} ({Year})",
    "music_folder_format": "{Title}",
    "movie_file_format": "{Title} ({Year}) - {Quality}",
    "episode_file_format": "{Title} - S{Season:02d}E{Episode:02d} - {Quality}",
    "rename_on_import": "true",
    "discord_webhook_url": os.getenv("DISCORD_WEBHOOK_URL", ""),
    "gotify_url": os.getenv("GOTIFY_URL", ""),
    "gotify_token": os.getenv("GOTIFY_TOKEN", ""),
    "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN", ""),
    "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID", ""),
    "default_movie_quality": "1080p",
    "default_tv_quality": "1080p",
    "default_music_quality": "lossless",
    "min_seeders": "3",
    "min_size_gb": "0",
    "max_size_gb": "40",
    "preferred_keywords": "REMUX, WEB-DL, BluRay, 10bit, Atmos, FLAC",
    "blocked_keywords": "CAM, HDCAM, TS, TELESYNC, TC, SCREENER, R5",
    "rss_sync_minutes": "2",
    "stalled_timeout_minutes": "120",
    "automation_enabled": "true",
    "auto_import_enabled": "true",
    "auto_hunt_enabled": "true",
    "auto_upgrade_enabled": "true",
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
    norm_path = (remote_path or "").replace(chr(92), "/")
    remote_prefix = (cfg.get("qbit_remote_path", "/downloads") or "").replace(chr(92), "/").rstrip("/")
    local_prefix = (cfg.get("qbit_local_mount", "/downloads") or "/downloads").rstrip("/")
    if remote_prefix and norm_path.lower().startswith(remote_prefix.lower()):
        rel = norm_path[len(remote_prefix):].lstrip("/")
        return Path(local_prefix) / rel
    candidate = Path(local_prefix) / Path(norm_path).name
    if candidate.exists():
        return candidate
    return Path(norm_path)

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
    async with httpx.AsyncClient(verify=False, timeout=20.0) as client:
        resp = await client.get(f"{cfg['prowlarr_url'].rstrip('/')}/api/v1/search", params=params, headers=headers)
        resp.raise_for_status()
        results = resp.json()

    disabled_indexers = {
        idx.name.lower() for idx in db.query(IndexerConfig).filter_by(enabled=False).all()
    }
    blacklisted_titles = {
        b.release_title.lower() for b in db.query(BlacklistedRelease).all() if b.release_title
    }
    min_seeders = int(cfg.get("min_seeders", 1))
    min_size_bytes = float(cfg.get("min_size_gb", 0)) * (1024**3)
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
        raw_title = r.get("title") or ""
        title_l = raw_title.lower()
        indexer_l = r.get("indexer", "").lower()
        if indexer_l in disabled_indexers:
            continue
        if title_l in blacklisted_titles:
            continue
        if r.get("seeders", 0) < min_seeders:
            continue
        if min_size_bytes > 0 and r.get("size", 0) < min_size_bytes:
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


def _find_local_poster_file(file_path: str | None) -> Path | None:
    if not file_path:
        return None
    p = Path(file_path)
    if not p.exists():
        m_drv = re.match(r"^([a-zA-Z]):[\\/](.*)$", file_path)
        if m_drv:
            drv = m_drv.group(1).lower()
            rest = m_drv.group(2).replace(chr(92), "/")
            for base in (f"/mnt/plex_{drv}", f"/mnt/{drv}"):
                cand = Path(base) / rest
                if cand.exists():
                    p = cand
                    break
    folder = p if p.is_dir() else p.parent
    if not folder.exists():
        return None
    for name in ("poster.jpg", "poster.png", "folder.jpg", "folder.png", "cover.jpg", "show.jpg"):
        art = folder / name
        if art.is_file():
            return art
    try:
        from datetime import datetime as _dt, timedelta as _td
        top_title = None[0].get("title", "") if None else "No matches"
        db.add(HistoryEvent(
            event_type="search",
            title=f"{query} ({media_type.upper()})",
            detail=f"{len(None)} result(s) • Top: {top_title[:140]}",
        ))
        # Prune search/history logs older than 30 days
        cutoff_30d = _dt.utcnow() - _td(days=30)
        db.query(HistoryEvent).filter(HistoryEvent.created_at < cutoff_30d).delete()
        db.commit()
    except Exception:
        db.rollback()

    return None

async def _search_plex_poster(title: str, year: int | None, cfg: dict, client: httpx.AsyncClient) -> str:
    plex_url = (cfg.get("plex_url") or "").split("/web")[0].strip().rstrip("/")
    plex_tok = (cfg.get("plex_token") or "").strip()
    if not plex_url or not plex_tok:
        return ""
    words = [w for w in title.strip().split() if w]
    queries = [title]
    if len(words) >= 3:
        queries.append(" ".join(words[:3]))
    if len(words) >= 2:
        queries.append(" ".join(words[:2]))
    seen_q = set()
    for q in queries:
        ql = q.lower()
        if ql in seen_q:
            continue
        seen_q.add(ql)
        try:
            r = await client.get(
                f"{plex_url}/hubs/search",
                params={"query": q, "limit": 12},
                headers={"X-Plex-Token": plex_tok, "Accept": "application/json"},
            )
            if r.status_code == 200:
                hubs = (r.json().get("MediaContainer") or {}).get("Hub") or []
                for hub in hubs:
                    for meta in (hub.get("Metadata") or []):
                        if meta.get("type") not in ("movie", "show", "collection", None):
                            continue
                        m_yr = meta.get("year")
                        if year and m_yr and abs(int(m_yr) - int(year)) > 1:
                            continue
                        thumb = meta.get("thumb")
                        if thumb:
                            return f"{plex_url}{thumb}?X-Plex-Token={plex_tok}"
        except Exception:
            pass
    return ""

async def _fetch_public_poster(title: str, year: int | None, media_type: str, client: httpx.AsyncClient) -> str:
    """Fetches official poster artwork from TVMaze (TV) or iTunes (Movies) with zero API keys required."""
    try:
        if media_type == "tv":
            r = await client.get("https://api.tvmaze.com/singlesearch/shows", params={"q": title})
            if r.status_code == 200:
                img = (r.json().get("image") or {})
                return img.get("original") or img.get("medium") or ""
        elif media_type == "movie":
            words = [w for w in title.strip().split() if w]
            search_terms = [f"{title} {year}" if year else title]
            if len(words) >= 3:
                search_terms.append(" ".join(words[:3]))
            for q in search_terms:
                r = await client.get("https://itunes.apple.com/search", params={"term": q, "media": "movie", "entity": "movie", "limit": 5})
                if r.status_code == 200:
                    results = r.json().get("results") or []
                    for res in results:
                        art = res.get("artworkUrl100") or ""
                        if art:
                            return art.replace("100x100bb", "600x600bb")
    except Exception:
        pass
    return ""

async def sync_posters_from_plex(db: Session, min_id: int = 0) -> tuple[int, int]:
    """Matches OmniArr library items against Plex metadata to populate poster_url."""
    cfg = get_settings(db)
    plex_url = cfg.get("plex_url", "").split("/web")[0].rstrip("/")
    token = cfg.get("plex_token", "")
    if not plex_url or not token:
        return 0

    updated = 0
    headers = {"Accept": "application/json"}
    try:
        async with httpx.AsyncClient(verify=False, timeout=httpx.Timeout(60.0, connect=5.0)) as client:
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
                    async with httpx.AsyncClient(verify=False, timeout=httpx.Timeout(30.0, connect=5.0)) as client:
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
    missing_items = db.query(MediaItem).filter(
        ((MediaItem.poster_url == None) | (MediaItem.poster_url == "")),
        MediaItem.id > min_id
    ).order_by(MediaItem.id.asc()).limit(200).all()
    next_min_id = missing_items[-1].id if missing_items else min_id
    if missing_items:
        async with httpx.AsyncClient(verify=False, timeout=8.0) as pub_client:
            for item in missing_items:
                art = await _fetch_public_poster(item.title, item.year, item.media_type, pub_client)
                if art:
                    item.poster_url = art
                    updated += 1
        db.commit()
    return updated, next_min_id
async def manual_search_prowlarr(query: str, media_type: str, quality: str, db: Session) -> list[dict]:
    """Returns all Prowlarr releases annotated with score and guardrail status for interactive picking."""
    cfg = get_settings(db)
    cat_map = {"movie": [2000], "tv": [5000], "music": [3000]}
    params = {"query": query, "type": "search"}
    for c in cat_map.get(media_type, []):
        params.setdefault("categories", []).append(c)

    headers = {"X-Api-Key": cfg["prowlarr_api_key"]}
    async with httpx.AsyncClient(verify=False, timeout=45.0) as client:
        resp = await client.get(f"{cfg['prowlarr_url'].rstrip('/')}/api/v1/search", params=params, headers=headers)
        resp.raise_for_status()
        results = resp.json()

    min_seeders = int(cfg.get("min_seeders", 1))
    min_size_bytes = float(cfg.get("min_size_gb", 0)) * (1024**3)
    max_size_bytes = float(cfg.get("max_size_gb", 100)) * (1024**3)
    blocked = [k.strip().lower() for k in cfg.get("blocked_keywords", "").split(",") if k.strip()]

    annotated = []
    for r in results:
        raw_title = r.get("title") or ""
        title_l = raw_title.lower()
        size_b = r.get("size", 0) or 0
        seeders = r.get("seeders", 0) or 0
        reasons = []
        if seeders < min_seeders:
            reasons.append(f"Seeders < {min_seeders}")
        if min_size_bytes > 0 and size_b < min_size_bytes:
            reasons.append("Below min size")
        if max_size_bytes > 0 and size_b > max_size_bytes:
            reasons.append("Above max size")
        if any(b in title_l for b in blocked):
            reasons.append("Blocked keyword")

        cf_score, _ = evaluate_custom_formats(raw_title, quality, db)
        total_score = score_release(r, cfg, cf_score)
        dl_url = r.get("downloadUrl") or r.get("magnetUrl") or ""
        if not dl_url:
            continue
        annotated.append({
            "title": raw_title,
            "indexer": r.get("indexer") or "Unknown",
            "size_gb": round(size_b / (1024**3), 2),
            "seeders": seeders,
            "peers": r.get("leechers", 0) or 0,
            "score": total_score,
            "cf_score": cf_score,
            "download_url": dl_url,
            "warnings": reasons,
        })
    annotated.sort(key=lambda x: x["score"], reverse=True)
    return annotated[:75]

async def send_to_qbittorrent(download_url: str, item_id: int, db: Session) -> str:
    cfg = get_settings(db)
    qbit_url = cfg["qbit_url"].rstrip("/")
    tag = f"omniarr_{item_id}"
    async with httpx.AsyncClient(verify=False, timeout=15.0) as client:
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
    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
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
    async with httpx.AsyncClient(verify=False, timeout=15.0) as client:
        await client.post(
            f"{qbit_url}/api/v2/auth/login",
            data={"username": cfg["qbit_user"], "password": cfg["qbit_pass"]}
        )
        resp = await client.get(f"{qbit_url}/api/v2/torrents/info", params={"category": cfg["qbit_category"]})
        torrents = {t["hash"]: t for t in resp.json()}

    imported_any = False
    mode = cfg.get("import_mode", "hardlink")

    import time as _t_now
    stalled_limit_sec = int(cfg.get("stalled_timeout_minutes", 120)) * 60

    for item in downloading:
        t = torrents.get(item.torrent_hash)
        if not t:
            continue
        t_state = t.get("state", "")
        added_on = t.get("added_on", 0) or 0
        is_errored = t_state in ("error", "missingFiles")
        is_stalled_out = (t_state == "stalledDL" and stalled_limit_sec > 0 and added_on > 0 and (_t_now.time() - added_on) > stalled_limit_sec)

        if is_errored or is_stalled_out:
            reason = f"qBittorrent {t_state}"
            db.add(BlacklistedRelease(
                media_item_id=item.id,
                torrent_hash=item.torrent_hash or "",
                release_title=t.get("name", ""),
                reason=reason
            ))
            try:
                async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
                    await client.post(f"{qbit_url}/api/v2/auth/login", data={"username": cfg["qbit_user"], "password": cfg["qbit_pass"]})
                    await client.post(f"{qbit_url}/api/v2/torrents/delete", data={"hashes": item.torrent_hash, "deleteFiles": "true"})
            except Exception:
                pass
            item.torrent_hash = None
            item.status = "wanted"
            db.commit()
            continue

        if t.get("progress", 0) >= 1.0:
            content_path = translate_qbit_path(t["content_path"], cfg)
            fmt_key = f"{item.media_type}_folder_format"
            template = cfg.get(fmt_key, "{Title} ({Year})")
            folder_name = template.replace("{Title}", item.title).replace("{Year}", str(item.year or "")).replace("()", "").strip()
            clean_name = re.sub(r'[\\/*?:"<>|]', "", folder_name)
            target_base = get_best_dest_dir(item.media_type, cfg, db) / clean_name

            if content_path.is_file():
                transfer_file(content_path, target_base / build_clean_filename(item, content_path.name, cfg), mode)
            elif content_path.is_dir():
                for root, _, files in os.walk(content_path):
                    for f in files:
                        if f.lower().endswith((".mkv", ".mp4", ".avi", ".flac", ".mp3", ".m4a")):
                            src_file = Path(root) / f
                            transfer_file(src_file, target_base / build_clean_filename(item, f, cfg), mode)

            if cfg.get("remove_completed_download") == "true":
                async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
                    await client.post(f"{qbit_url}/api/v2/auth/login", data={"username": cfg["qbit_user"], "password": cfg["qbit_pass"]})
                    await client.post(f"{qbit_url}/api/v2/torrents/delete", data={"hashes": item.torrent_hash, "deleteFiles": "true"})

            item.status = "available"
            item.file_path = str(target_base)
            db.commit()
            record_history(db, "import", item.title, f"Imported to {target_base}", item.id)
            await send_notification(f"Imported: {item.title}", f"Saved to `{target_base}`", cfg)
            imported_any = True

    if imported_any:
        if cfg.get("plex_notify_import", "true") == "true":
            await trigger_plex_scan(cfg)
        await sync_posters_from_plex(db)


async def fetch_media_metadata(item: MediaItem, cfg: dict) -> dict:
    """Fetches synopsis, runtime, release date, multi-site scores, cast, and critic/user reviews."""
    import re as _re
    meta = {
        "summary": "",
        "runtime_min": None,
        "release_date": str(item.year) if item.year else "",
        "content_rating": "",
        "rating": "",
        "studio": "",
        "backdrop_url": "",
        "genres": [],
        "tagline": "",
        "cast": [],       # [{"name": str, "character": str, "photo": str}]
        "scores": [],     # [{"source": str, "value": str, "badge": str}]
        "reviews": [],    # [{"source": str, "author": str, "rating": str, "is_critic": bool, "content": str}]
    }
    clean_title = _re.sub(r"\s*\(\d{4}\)\s*$", "", (item.title or "")).strip()
    plex_url = cfg.get("plex_url", "").split("/web")[0].rstrip("/")
    token = cfg.get("plex_token", "")
    tmdb_key = (cfg.get("tmdb_api_key") or "").strip()
    omdb_key = (cfg.get("omdb_api_key") or "").strip()

    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        # 1. Plex Metadata (Synopsis, Cast Roles, Critic & Audience Ratings)
        if plex_url and token:
            try:
                r = await client.get(
                    f"{plex_url}/library/search",
                    params={"query": clean_title, "X-Plex-Token": token},
                    headers={"Accept": "application/json"},
                )
                if r.status_code == 200:
                    hubs = r.json().get("MediaContainer", {}).get("SearchResult", [])
                    for entry in hubs:
                        md = entry.get("Metadata") or {}
                        if md.get("title", "").lower() == clean_title.lower() and (
                            not item.year or md.get("year") == item.year or abs((md.get("year") or 0) - item.year) <= 1
                        ):
                            meta["summary"] = md.get("summary") or ""
                            meta["tagline"] = md.get("tagline") or ""
                            if md.get("duration"):
                                meta["runtime_min"] = max(1, round(int(md["duration"]) / 60000))
                            meta["release_date"] = md.get("originallyAvailableAt") or meta["release_date"]
                            meta["content_rating"] = md.get("contentRating") or ""
                            meta["rating"] = str(md.get("audienceRating") or md.get("rating") or "")
                            meta["studio"] = md.get("studio") or ""
                            if md.get("art"):
                                meta["backdrop_url"] = f"{plex_url}{md['art']}?X-Plex-Token={token}"
                            meta["genres"] = [g.get("tag") for g in (md.get("Genre") or []) if g.get("tag")]

                            if md.get("rating"):
                                meta["scores"].append({"source": "Critic Score", "value": f"{md['rating']}/10", "badge": "critic"})
                            if md.get("audienceRating"):
                                meta["scores"].append({"source": "Audience Score", "value": f"{md['audienceRating']}/10", "badge": "audience"})

                            # Fetch full Plex item details for complete Role cast list
                            rk = md.get("ratingKey")
                            if rk:
                                r_full = await client.get(
                                    f"{plex_url}/library/metadata/{rk}",
                                    params={"X-Plex-Token": token},
                                    headers={"Accept": "application/json"},
                                )
                                if r_full.status_code == 200:
                                    full_md = (r_full.json().get("MediaContainer", {}).get("Metadata") or [{}])[0]
                                    for role in (full_md.get("Role") or [])[:16]:
                                        if role.get("tag"):
                                            meta["cast"].append({
                                                "name": role["tag"],
                                                "character": role.get("role") or "",
                                                "photo": role.get("thumb") or "",
                                            })
                            break
            except Exception:
                pass

        # 2. TVMaze (TV Shows: Cast with Photos, Rating, Network, Summary - Zero API Key Needed)
        if item.media_type == "tv":
            try:
                r = await client.get(
                    "https://api.tvmaze.com/singlesearch/shows",
                    params={"q": clean_title, "embed": "cast"},
                )
                if r.status_code == 200:
                    tv = r.json()
                    if not meta["summary"] and tv.get("summary"):
                        meta["summary"] = _re.sub(r"<[^>]+>", "", tv["summary"]).strip()
                    if not meta["runtime_min"] and (tv.get("averageRuntime") or tv.get("runtime")):
                        meta["runtime_min"] = tv.get("averageRuntime") or tv.get("runtime")
                    if not meta["release_date"] and tv.get("premiered"):
                        meta["release_date"] = tv["premiered"]
                    if not meta["genres"] and tv.get("genres"):
                        meta["genres"] = tv["genres"]
                    if not meta["studio"]:
                        net = (tv.get("network") or tv.get("webChannel") or {}).get("name")
                        if net:
                            meta["studio"] = net
                    tv_avg = (tv.get("rating") or {}).get("average")
                    if tv_avg:
                        if not meta["rating"]:
                            meta["rating"] = str(tv_avg)
                        meta["scores"].append({"source": "TVMaze", "value": f"{tv_avg}/10", "badge": "audience"})

                    cast_list = tv.get("_embedded", {}).get("cast", [])
                    if cast_list:
                        # Prefer TVMaze cast since it includes actor portraits
                        meta["cast"] = []
                        for c in cast_list[:16]:
                            person = c.get("person") or {}
                            char = c.get("character") or {}
                            img = (person.get("image") or {}).get("medium") or (char.get("image") or {}).get("medium") or ""
                            if person.get("name"):
                                meta["cast"].append({
                                    "name": person["name"],
                                    "character": char.get("name") or "",
                                    "photo": img,
                                })
            except Exception:
                pass

        # 3. Apple iTunes Search + Public Customer Reviews RSS (Movies - Zero API Key Needed)
        if item.media_type == "movie":
            try:
                r_it = await client.get(
                    "https://itunes.apple.com/search",
                    params={"term": clean_title, "media": "movie", "limit": 5},
                )
                if r_it.status_code == 200:
                    hits = r_it.json().get("results") or []
                    best_it = None
                    for h in hits:
                        if h.get("trackName", "").lower() == clean_title.lower():
                            best_it = h
                            break
                    if not best_it and hits:
                        best_it = hits[0]
                    if best_it:
                        if not meta["summary"] and best_it.get("longDescription"):
                            meta["summary"] = best_it["longDescription"]
                        if not meta["content_rating"] and best_it.get("contentAdvisoryRating"):
                            meta["content_rating"] = best_it["contentAdvisoryRating"]
                        if not meta["runtime_min"] and best_it.get("trackTimeMillis"):
                            meta["runtime_min"] = max(1, round(int(best_it["trackTimeMillis"]) / 60000))
                        if not meta["genres"] and best_it.get("primaryGenreName"):
                            meta["genres"] = [best_it["primaryGenreName"]]
                        if not meta["cast"] and best_it.get("artistName"):
                            meta["studio"] = meta["studio"] or f"Dir. {best_it['artistName']}"

                        track_id = best_it.get("trackId")
                        if track_id:
                            r_rev = await client.get(
                                f"https://itunes.apple.com/us/rss/customerreviews/id={track_id}/sortBy=mostRecent/json"
                            )
                            if r_rev.status_code == 200:
                                entries = (r_rev.json().get("feed") or {}).get("entry") or []
                                if isinstance(entries, dict):
                                    entries = [entries]
                                for ent in entries[:8]:
                                    content = ((ent.get("content") or {}).get("label") or "").strip()
                                    author = (((ent.get("author") or {}).get("name") or {}).get("label") or "Viewer").strip()
                                    stars = ((ent.get("im:rating") or {}).get("label") or "").strip()
                                    r_title = ((ent.get("title") or {}).get("label") or "").strip()
                                    if content:
                                        full_txt = f"{r_title}: {content}" if r_title else content
                                        meta["reviews"].append({
                                            "source": "Apple TV / iTunes",
                                            "author": author,
                                            "rating": f"{stars}/5 ★" if stars else "",
                                            "is_critic": False,
                                            "content": full_txt[:700],
                                        })
            except Exception:
                pass

        # 4. TMDB Credits, User Score & Critic/User Reviews (when TMDB API Key is configured)
        if tmdb_key:
            try:
                kind = "movie" if item.media_type == "movie" else "tv"
                s_params = {"api_key": tmdb_key, "query": clean_title}
                if item.year:
                    s_params["year" if kind == "movie" else "first_air_date_year"] = item.year
                r_s = await client.get(f"https://api.themoviedb.org/3/search/{kind}", params=s_params)
                if r_s.status_code == 200:
                    res_list = r_s.json().get("results") or []
                    if res_list:
                        tmdb_id = res_list[0]["id"]
                        r_d = await client.get(
                            f"https://api.themoviedb.org/3/{kind}/{tmdb_id}",
                            params={"api_key": tmdb_key, "append_to_response": "credits,reviews"},
                        )
                        if r_d.status_code == 200:
                            td = r_d.json()
                            if not meta["summary"] and td.get("overview"):
                                meta["summary"] = td["overview"]
                            if not meta["tagline"] and td.get("tagline"):
                                meta["tagline"] = td["tagline"]
                            if td.get("backdrop_path") and not meta["backdrop_url"]:
                                meta["backdrop_url"] = f"https://image.tmdb.org/t/p/w1280{td['backdrop_path']}"
                            if td.get("vote_average"):
                                score_val = round(float(td["vote_average"]), 1)
                                meta["scores"].append({
                                    "source": f"TMDB ({td.get('vote_count', 0)} votes)",
                                    "value": f"{score_val}/10",
                                    "badge": "audience",
                                })
                            credits = (td.get("credits") or {}).get("cast") or []
                            if credits:
                                meta["cast"] = []
                                for c in credits[:16]:
                                    photo = f"https://image.tmdb.org/t/p/w185{c['profile_path']}" if c.get("profile_path") else ""
                                    meta["cast"].append({
                                        "name": c.get("name", ""),
                                        "character": c.get("character", ""),
                                        "photo": photo,
                                    })
                            for rv in ((td.get("reviews") or {}).get("results") or [])[:8]:
                                r_author = rv.get("author") or "TMDB Reviewer"
                                r_rating = (rv.get("author_details") or {}).get("rating")
                                r_body = (rv.get("content") or "").strip()
                                if r_body:
                                    meta["reviews"].append({
                                        "source": "TMDB Community",
                                        "author": r_author,
                                        "rating": f"{r_rating}/10 ★" if r_rating else "",
                                        "is_critic": False,
                                        "content": r_body[:750],
                                    })
            except Exception:
                pass

        # 5. OMDb Multi-Site Critic Scores (IMDb, Rotten Tomatoes, Metacritic - if OMDb key configured)
        if omdb_key:
            try:
                o_params = {"apikey": omdb_key, "t": clean_title, "type": ("movie" if item.media_type == "movie" else "series")}
                if item.year:
                    o_params["y"] = item.year
                r_o = await client.get("https://www.omdbapi.com/", params=o_params)
                if r_o.status_code == 200:
                    od = r_o.json()
                    if od.get("Response") == "True":
                        for rt in (od.get("Ratings") or []):
                            src = rt.get("Source", "")
                            val = rt.get("Value", "")
                            if "Internet Movie Database" in src:
                                meta["scores"].append({"source": "IMDb", "value": val, "badge": "audience"})
                            elif "Rotten Tomatoes" in src:
                                meta["scores"].append({"source": "Rotten Tomatoes", "value": val, "badge": "critic"})
                            elif "Metacritic" in src:
                                meta["scores"].append({"source": "Metacritic", "value": val, "badge": "critic"})
                        if not meta["cast"] and od.get("Actors"):
                            for actor_name in od["Actors"].split(","):
                                if actor_name.strip():
                                    meta["cast"].append({"name": actor_name.strip(), "character": "", "photo": ""})
            except Exception:
                pass

    return meta



async def send_notification(title: str, message: str, cfg: dict):
    """Dispatches out-of-band notifications to Discord, Gotify, and/or Telegram if configured."""
    async with httpx.AsyncClient(verify=False, timeout=8.0) as client:
        discord_url = (cfg.get("discord_webhook_url") or "").strip()
        if discord_url:
            try:
                await client.post(discord_url, json={
                    "embeds": [{"title": title, "description": message, "color": 6513654}]
                })
            except Exception:
                pass
        gotify_url = (cfg.get("gotify_url") or "").strip().rstrip("/")
        gotify_tok = (cfg.get("gotify_token") or "").strip()
        if gotify_url and gotify_tok:
            try:
                await client.post(f"{gotify_url}/message", params={"token": gotify_tok}, json={"title": title, "message": message, "priority": 5})
            except Exception:
                pass
        tg_tok = (cfg.get("telegram_bot_token") or "").strip()
        tg_chat = (cfg.get("telegram_chat_id") or "").strip()
        if tg_tok and tg_chat:
            try:
                await client.post(f"https://api.telegram.org/bot{tg_tok}/sendMessage", json={"chat_id": tg_chat, "text": f"*{title}*\n{message}", "parse_mode": "Markdown"})
            except Exception:
                pass

def record_history(db: Session, event_type: str, title: str, detail: str = "", media_item_id: int | None = None):
    try:
        db.add(HistoryEvent(event_type=event_type, title=title, detail=detail, media_item_id=media_item_id))
        db.commit()
    except Exception:
        db.rollback()

def _detect_quality_tag(filename: str) -> str:
    low = filename.lower()
    for tag, label in [("2160p", "2160p"), ("4k", "2160p"), ("1080p", "1080p"), ("720p", "720p"), ("480p", "480p"), ("remux", "REMUX"), ("bluray", "BluRay"), ("web-dl", "WEB-DL"), ("webrip", "WEBRip"), ("flac", "FLAC")]:
        if tag in low:
            return label
    return "HD"

def build_clean_filename(item: MediaItem, src_name: str, cfg: dict) -> str:
    """Formats a clean Plex/Jellyfin filename using configured naming tokens when rename_on_import is enabled."""
    if cfg.get("rename_on_import", "true") != "true":
        return src_name
    ext = Path(src_name).suffix
    quality = _detect_quality_tag(src_name)
    year_str = str(item.year or "")
    if item.media_type == "movie":
        tpl = cfg.get("movie_file_format", "{Title} ({Year}) - {Quality}")
        out = tpl.replace("{Title}", item.title).replace("{Year}", year_str).replace("{Quality}", quality).replace("()", "").strip()
        out = re.sub(r"\s+-\s+$", "", out)
        return re.sub(r'[\\/*?:"<>|]', "", out) + ext
    elif item.media_type == "tv":
        m = re.search(r"[sS](\d{1,2})[eE](\d{1,2})", src_name)
        if m:
            s_num, e_num = int(m.group(1)), int(m.group(2))
            tpl = cfg.get("episode_file_format", "{Title} - S{Season:02d}E{Episode:02d} - {Quality}")
            out = tpl.replace("{Title}", item.title).replace("{Year}", year_str).replace("{Quality}", quality)
            out = out.replace("{Season:02d}", f"{s_num:02d}").replace("{Episode:02d}", f"{e_num:02d}")
            out = out.replace("{Season}", str(s_num)).replace("{Episode}", str(e_num)).replace("()", "").strip()
            return re.sub(r'[\\/*?:"<>|]', "", out) + ext
    return src_name


def clean_media_title(raw_name: str) -> tuple[str, int | None]:
    """Converts scene/dot-separated folder names (e.g. 'Troll.1986.1080p.BluRay...') into ('Troll', 1986)."""
    name = raw_name.strip()
    # Extract (YYYY) in parens first if present
    m_paren = re.search(r"\((19\d{2}|20\d{2})\)", name)
    year = int(m_paren.group(1)) if m_paren else None
    if m_paren:
        name = name[:m_paren.start()].strip()

    # Replace dots/underscores with spaces when dots are used as word separators
    if name.count(".") >= 2:
        name = name.replace(".", " ")
    name = name.replace("_", " ")

    # Cut off at standalone 4-digit year if not already extracted
    if year is None:
        m_yr = re.search(r"\b(19\d{2}|20\d{2})\b", name)
        if m_yr and m_yr.start() > 1:
            year = int(m_yr.group(1))
            name = name[:m_yr.start()].strip()

    # Cut off at common release/quality tokens
    cut_tokens = r"\b(2160p|1080p|720p|480p|4k|uhd|hdr|dv|dovi|remux|bluray|blu-ray|bdrip|brrip|web-dl|webdl|webrip|web|hdtv|dvdrip|x264|x265|h264|h265|hevc|av1|10bit|aac|ac3|eac3|dts|truehd|atmos|repack|proper|extended|unrated|directors\.cut)\b"
    m_tok = re.search(cut_tokens, name, flags=re.IGNORECASE)
    if m_tok and m_tok.start() > 1:
        name = name[:m_tok.start()].strip()

    name = re.sub(r"[-\[\(]+$", "", name).strip()
    name = re.sub(r"\s+", " ", name).strip()
    # Title-case only if the entire string is lowercase
    if name == name.lower():
        name = name.title()
    return (name or raw_name.strip()), year

def scan_existing_library(db: Session) -> dict:
    """Scans configured movie_roots and tv_roots on disk and registers untracked folders into OmniArr."""
    roots_map = get_media_roots(db)
    all_items = db.query(MediaItem).all()
    existing_paths = {str(i.file_path).rstrip("/").lower() for i in all_items if i.file_path}
    existing_by_key = {(i.title.lower(), i.media_type): i for i in all_items}
    video_exts = (".mkv", ".mp4", ".avi", ".m4v")
    added = 0
    updated = 0

    for mtype in ("movie", "tv"):
        for root_dir in roots_map.get(mtype, []):
            rp = Path(root_dir)
            if not rp.exists() or not rp.is_dir():
                continue
            for entry in sorted(rp.iterdir()):
                if not entry.is_dir() or entry.name.startswith("."):
                    continue
                if str(entry).rstrip("/").lower() in existing_paths:
                    continue
                # Verify folder actually contains at least one video file
                has_vid = any(f.lower().endswith(video_exts) for _, _, files in os.walk(entry) for f in files)
                if not has_vid:
                    continue
                title, year = clean_media_title(entry.name.strip())
                existing_item = existing_by_key.get((title.lower(), mtype))
                if existing_item is not None:
                    old_p = Path(existing_item.file_path) if existing_item.file_path else None
                    if not old_p or not old_p.exists():
                        existing_item.file_path = str(entry)
                        existing_item.status = "available"
                        existing_paths.add(str(entry).rstrip("/").lower())
                        updated += 1
                    continue
                new_item = MediaItem(
                    title=title,
                    year=year,
                    media_type=mtype,
                    quality_profile="1080p",
                    status="available",
                    file_path=str(entry),
                    monitored=True,
                )
                db.add(new_item)
                existing_by_key[(title.lower(), mtype)] = new_item
                added += 1

    if added > 0 or updated > 0:
        db.commit()
        record_history(db, "scan", f"Library Disk Scan ({added} added, {updated} relocated)", f"Discovered {added} new titles and updated {updated} relocated paths on disk")
    return {"added": added, "updated": updated}



def translate_to_local_path(p: str, cfg: dict | None = None) -> str:
    if not p:
        return ""
    if cfg is None:
        db = SessionLocal()
        try:
            cfg = get_settings(db)
        finally:
            db.close()
    return translate_qbit_path(p, cfg)

# === WAVE 2: BULK DRIVE REBALANCER & TV SEASON/EPISODE MANAGER ===

def get_drive_storage_stats(db: Session) -> list[dict]:
    roots = db.query(RootFolder).all()
    seen_mounts: dict[str, dict] = {}
    for rf in roots:
        linux_p = translate_to_local_path(rf.path)
        check_p = linux_p
        while check_p and check_p != "/" and not os.path.exists(check_p):
            check_p = os.path.dirname(check_p)
        if not check_p or not os.path.exists(check_p):
            continue
        st = os.statvfs(check_p)
        total_gb = round((st.f_blocks * st.f_frsize) / (1024 ** 3), 1)
        free_gb = round((st.f_bavail * st.f_frsize) / (1024 ** 3), 1)
        used_gb = round(total_gb - free_gb, 1)
        pct = round((used_gb / total_gb) * 100, 1) if total_gb > 0 else 0.0
        mount_key = f"{st.f_fsid}_{total_gb}"
        if mount_key not in seen_mounts:
            seen_mounts[mount_key] = {
                "id": rf.id,
                "label": rf.path,
                "local_path": linux_p,
                "media_type": rf.media_type,
                "total_gb": total_gb,
                "used_gb": used_gb,
                "free_gb": free_gb,
                "used_pct": pct,
                "item_count": 0,
            }
    # Count library items per root prefix
    items = db.query(MediaItem).filter(MediaItem.file_path != None).all()
    for it in items:
        lp = translate_to_local_path(it.file_path or "")
        for info in seen_mounts.values():
            base_dir = os.path.dirname(info["local_path"].rstrip("/")) or info["local_path"]
            if lp.startswith(info["local_path"].rstrip("/") + "/") or lp.startswith(base_dir + "/"):
                info["item_count"] += 1
                break
    return sorted(seen_mounts.values(), key=lambda x: x["free_gb"], reverse=True)


def move_media_item_folder(db: Session, item_id: int, target_root: str) -> tuple[bool, str]:
    item = db.query(MediaItem).filter(MediaItem.id == item_id).first()
    if not item or not item.file_path:
        return False, "Item has no local file path"
    src_local = translate_to_local_path(item.file_path)
    dst_root_local = translate_to_local_path(target_root).rstrip("/")
    if not os.path.exists(src_local):
        return False, f"Source not found on disk: {src_local}"
    os.makedirs(dst_root_local, exist_ok=True)
    folder_name = os.path.basename(src_local.rstrip("/"))
    dst_local = os.path.join(dst_root_local, folder_name)
    if os.path.abspath(src_local) == os.path.abspath(dst_local):
        return True, "Already on target drive"
    if os.path.exists(dst_local):
        return False, f"Destination already exists: {dst_local}"
    shutil.move(src_local, dst_local)
    item.file_path = dst_local
    db.add(HistoryLog(
        event_type="BulkRelocate",
        title=item.title,
        details=f"Moved {folder_name} -> {dst_root_local}",
    ))
    db.commit()
    return True, dst_local


async def get_tv_seasons_and_episodes(item: MediaItem) -> list[dict]:
    # 1. Scan disk for SxxExx video files
    video_exts = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".mov", ".wmv"}
    disk_eps: dict[tuple[int, int], dict] = {}
    local_dir = translate_to_local_path(item.file_path or "")
    if local_dir and os.path.isdir(local_dir):
        for root, _, files in os.walk(local_dir):
            for fn in files:
                ext = os.path.splitext(fn)[1].lower()
                if ext not in video_exts:
                    continue
                m = re.search(r"[Ss](\d{1,2})[Ee](\d{1,3})", fn)
                if m:
                    s_num, e_num = int(m.group(1)), int(m.group(2))
                    full_p = os.path.join(root, fn)
                    size_mb = round(os.path.getsize(full_p) / (1024 * 1024), 1)
                    qual = "2160p" if "2160" in fn or "4k" in fn.lower() else ("1080p" if "1080" in fn else ("720p" if "720" in fn else "SD/WEB"))
                    disk_eps[(s_num, e_num)] = {
                        "file": fn,
                        "size_mb": size_mb,
                        "quality": qual,
                    }

    # 2. Fetch episode guide from TVMaze (no API key needed)
    seasons_map: dict[int, list[dict]] = {}
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get("https://api.tvmaze.com/singlesearch/shows", params={"q": item.title, "embed": "episodes"})
            if r.status_code == 200:
                data = r.json()
                eps = ((data.get("_embedded") or {}).get("episodes")) or []
                for ep in eps:
                    s_num = int(ep.get("season") or 0)
                    e_num = int(ep.get("number") or 0)
                    if s_num <= 0 or e_num <= 0:
                        continue
                    disk_info = disk_eps.pop((s_num, e_num), None)
                    seasons_map.setdefault(s_num, []).append({
                        "season": s_num,
                        "episode": e_num,
                        "code": f"S{s_num:02d}E{e_num:02d}",
                        "name": ep.get("name") or f"Episode {e_num}",
                        "airdate": ep.get("airdate") or "",
                        "on_disk": disk_info is not None,
                        "file": disk_info["file"] if disk_info else "",
                        "size_mb": disk_info["size_mb"] if disk_info else 0,
                        "quality": disk_info["quality"] if disk_info else "",
                    })
    except Exception:
        pass

    # 3. Merge any extra on-disk episodes not in TVMaze response
    for (s_num, e_num), disk_info in sorted(disk_eps.items()):
        seasons_map.setdefault(s_num, []).append({
            "season": s_num,
            "episode": e_num,
            "code": f"S{s_num:02d}E{e_num:02d}",
            "name": disk_info["file"],
            "airdate": "",
            "on_disk": True,
            "file": disk_info["file"],
            "size_mb": disk_info["size_mb"],
            "quality": disk_info["quality"],
        })

    import json as _json
    monitored_set = set()
    try:
        raw_ms = getattr(item, "monitored_seasons", None) or "[]"
        parsed_ms = _json.loads(raw_ms) if isinstance(raw_ms, str) else raw_ms
        if isinstance(parsed_ms, list):
            for x in parsed_ms:
                if isinstance(x, int) and not isinstance(x, bool):
                    monitored_set.add(x)
                elif isinstance(x, str) and x.strip().isdigit():
                    monitored_set.add(int(x.strip()))
    except Exception:
        monitored_set = set()

    result = []
    for s_num in sorted(seasons_map.keys(), reverse=True):
        ep_list = sorted(seasons_map[s_num], key=lambda x: x["episode"])
        have_count = sum(1 for e in ep_list if e["on_disk"])
        is_mon = True if not monitored_set else (s_num in monitored_set)
        result.append({
            "season": s_num,
            "monitored": is_mon,
            "total": len(ep_list),
            "on_disk": have_count,
            "episodes": ep_list,
        })
    return result


# === WAVE 3: FULL POSTER REMATCH, PER-ITEM POSTER PICKER & KOMETA ASSETS ===

def _find_kometa_asset_file(title: str, year: int | None, kometa_dir: str) -> str:
    base = translate_to_local_path((kometa_dir or "").strip())
    if not base or not os.path.isdir(base):
        return ""
    clean_t = re.sub(r'[\\/:*?"<>|]', "", title or "").strip()
    candidates = []
    if year:
        candidates.extend([
            os.path.join(base, f"{clean_t} ({year})", "poster.jpg"),
            os.path.join(base, f"{clean_t} ({year})", "poster.png"),
            os.path.join(base, f"{clean_t} ({year}).jpg"),
            os.path.join(base, f"{clean_t} ({year}).png"),
        ])
    candidates.extend([
        os.path.join(base, clean_t, "poster.jpg"),
        os.path.join(base, clean_t, "poster.png"),
        os.path.join(base, f"{clean_t}.jpg"),
        os.path.join(base, f"{clean_t}.png"),
    ])
    for c in candidates:
        if os.path.isfile(c):
            return c
    return ""


async def get_poster_candidates(db: Session, item: MediaItem) -> list[dict]:
    cfg = get_settings(db)
    results: list[dict] = []
    seen_urls: set[str] = set()

    def add_cand(source: str, label: str, url: str):
        if url and url not in seen_urls:
            seen_urls.add(url)
            results.append({"source": source, "label": label, "url": url})

    # 1. Kometa asset folder
    kometa_dir = cfg.get("kometa_assets_dir") or ""
    if _find_kometa_asset_file(item.title, item.year, kometa_dir):
        add_cand("Local Art", f"Overlay / Asset ({item.title})", f"/api/media/{item.id}/kometa-poster")

    # 2. Local media folder poster.jpg / folder.jpg
    if _find_local_poster_file(item.file_path):
        add_cand("Disk", "Local Folder Artwork", f"/api/media/{item.id}/local-poster")

    # 3. Plex server hubs + collections
    plex_url = (cfg.get("plex_url") or "").split("/web")[0].strip().rstrip("/")
    plex_tok = (cfg.get("plex_token") or "").strip()
    raw_title = (item.title or "").strip()
    clean_q = re.sub(r"[^a-zA-Z0-9\s]", " ", raw_title).strip()
    base_no_coll = re.sub(r"\bcollection\b", "", clean_q, flags=re.I).strip()
    words = [w for w in (base_no_coll or clean_q).split() if w]
    queries = [q for q in [clean_q, base_no_coll, " ".join(words[:2]) if len(words) >= 2 else ""] if q]

    async with httpx.AsyncClient(verify=False, timeout=5.0) as client:
        if plex_url and plex_tok:
            p_headers = {"X-Plex-Token": plex_tok, "Accept": "application/json"}
            # Search Plex collections across movie/show sections when title looks like a collection
            if "collection" in raw_title.lower() or getattr(item, "media_type", "") == "collection":
                try:
                    sec_r = await client.get(f"{plex_url}/library/sections", headers=p_headers)
                    if sec_r.status_code == 200:
                        for sec in (sec_r.json().get("MediaContainer") or {}).get("Directory") or []:
                            sec_key = sec.get("key")
                            if not sec_key:
                                continue
                            cr = await client.get(f"{plex_url}/library/sections/{sec_key}/collections", headers=p_headers)
                            if cr.status_code == 200:
                                for meta in (cr.json().get("MediaContainer") or {}).get("Metadata") or []:
                                    c_title = meta.get("title") or ""
                                    if (base_no_coll or clean_q).lower() in c_title.lower() and meta.get("thumb"):
                                        add_cand("Plex Collection", c_title, f"{plex_url}{meta['thumb']}?X-Plex-Token={plex_tok}")
                except Exception:
                    pass

            for q in dict.fromkeys(queries):
                try:
                    r = await client.get(
                        f"{plex_url}/hubs/search",
                        params={"query": q, "limit": 10},
                        headers=p_headers,
                    )
                    if r.status_code == 200:
                        for hub in (r.json().get("MediaContainer") or {}).get("Hub") or []:
                            for meta in (hub.get("Metadata") or []):
                                thumb = meta.get("thumb")
                                if thumb:
                                    m_title = meta.get("title") or item.title
                                    m_yr = meta.get("year") or ""
                                    add_cand("Plex", f"{m_title} ({m_yr})".strip(" ()"), f"{plex_url}{thumb}?X-Plex-Token={plex_tok}")
                except Exception:
                    pass

        # 4. TMDB public metadata (collections + movies/shows) if key configured
        tmdb_key = (cfg.get("tmdb_api_key") or "").strip()
        if tmdb_key:
            try:
                endpoint = "collection" if "collection" in raw_title.lower() else ("tv" if item.media_type == "tv" else "movie")
                tr = await client.get(
                    f"https://api.themoviedb.org/3/search/{endpoint}",
                    params={"api_key": tmdb_key, "query": base_no_coll or clean_q},
                )
                if tr.status_code == 200:
                    for row in (tr.json().get("results") or [])[:6]:
                        pp = row.get("poster_path")
                        if pp:
                            t_name = row.get("name") or row.get("title") or item.title
                            add_cand("TMDB", t_name, f"https://image.tmdb.org/t/p/w500{pp}")
            except Exception:
                pass

        # 5. iTunes / TVMaze public artwork fallback
        try:
            if item.media_type == "tv":
                r = await client.get("https://api.tvmaze.com/search/shows", params={"q": base_no_coll or clean_q})
                if r.status_code == 200:
                    for row in (r.json() or [])[:5]:
                        show = row.get("show") or {}
                        img = (show.get("image") or {}).get("original") or (show.get("image") or {}).get("medium")
                        if img:
                            add_cand("TVMaze", show.get("name") or item.title, img)
            else:
                search_base = base_no_coll or clean_q
                q_term = f"{search_base} {item.year}" if item.year else search_base
                r = await client.get("https://itunes.apple.com/search", params={"term": q_term, "media": "movie", "entity": "movie", "limit": 6})
                if r.status_code == 200:
                    for res in (r.json().get("results") or []):
                        art = (res.get("artworkUrl100") or "").replace("100x100bb", "600x600bb")
                        if art:
                            add_cand("iTunes", f"{res.get('trackName', item.title)} ({str(res.get('releaseDate', ''))[:4]})", art)
        except Exception:
            pass

    return results


def move_media_folder(item: MediaItem, new_parent_dir: str, transfer_files: bool, db: Session) -> dict:
    """Moves a show or movie folder to a new root directory and optionally auto-transfers all existing files."""
    cfg = get_settings(db)
    parent = Path(new_parent_dir.strip())
    if not str(parent) or str(parent) == ".":
        raise ValueError("Destination folder path is required.")

    fmt_key = f"{item.media_type}_folder_format"
    template = cfg.get(fmt_key, "{Title} ({Year})")
    folder_name = template.replace("{Title}", item.title).replace("{Year}", str(item.year or "")).replace("()", "").strip()
    clean_name = re.sub(r'[\\/*?:"<>|]', "", folder_name)
    new_target = parent / clean_name

    def _resolve_existing_path(raw_p: str) -> Path | None:
        if not raw_p:
            return None
        norm = raw_p.replace(chr(92), "/")
        direct = Path(norm)
        if direct.exists():
            return direct
        # Translate Windows drive letter (e.g. D:/Media/Movies/... -> /mnt/media_d/Media/Movies/...)
        m_drv = re.match(r"^([a-zA-Z]):/(.*)$", norm)
        if m_drv:
            drv = m_drv.group(1).lower()
            rest = m_drv.group(2)
            for base in [f"/mnt/plex_{drv}", f"/mnt/{drv}"]:
                cand = Path(base) / rest
                if cand.exists():
                    return cand
        return direct

    old_path = _resolve_existing_path(item.file_path)
    moved_files = 0

    if transfer_files and old_path and old_path.exists():
        if old_path.resolve() == new_target.resolve():
            return {"ok": True, "new_path": str(new_target), "moved_files": 0, "note": "Already at destination"}
        new_target.mkdir(parents=True, exist_ok=True)
        if old_path.is_file():
            dst_file = new_target / old_path.name
            shutil.move(str(old_path), str(dst_file))
            moved_files = 1
        elif old_path.is_dir():
            for root, _, files in os.walk(old_path):
                rel_dir = Path(root).relative_to(old_path)
                dest_dir = new_target / rel_dir
                dest_dir.mkdir(parents=True, exist_ok=True)
                for f in files:
                    src_f = Path(root) / f
                    dst_f = dest_dir / f
                    if not dst_f.exists():
                        shutil.move(str(src_f), str(dst_f))
                        moved_files += 1
            # Clean up empty leftover source directories
            for root, dirs, files in os.walk(old_path, topdown=False):
                try:
                    if not os.listdir(root):
                        os.rmdir(root)
                except OSError:
                    pass
    else:
        new_target.mkdir(parents=True, exist_ok=True)

    # Update MediaItem and any EpisodeItem paths in SQLite
    old_prefix = str(old_path) if old_path else ""
    item.file_path = str(new_target)
    if transfer_files and moved_files > 0:
        item.status = "available"

    episodes = db.query(EpisodeItem).filter_by(media_item_id=item.id).all()
    for ep in episodes:
        if ep.file_path and old_prefix and ep.file_path.startswith(old_prefix):
            rel_ep = ep.file_path[len(old_prefix):].lstrip("/\\")
            ep.file_path = str(new_target / rel_ep)

    db.commit()
    return {"ok": True, "new_path": str(new_target), "moved_files": moved_files}

async def sync_calendar_schedules(db) -> dict:
    """Sync upcoming/recent TV episode air dates (TVMaze) and movie release dates (TMDB)."""
    import asyncio, re
    import httpx
    from .models import MediaItem, EpisodeItem

    cfg = get_settings(db)
    tmdb_key = (cfg.get("tmdb_api_key") or "").strip()
    updated_eps = 0
    updated_movies = 0
    shows_checked = 0
    errors = 0

    monitored_shows = db.query(MediaItem).filter(
        MediaItem.media_type == "tv",
        MediaItem.monitored == True,
    ).all()

    async with httpx.AsyncClient(timeout=15.0) as client:
        for show in monitored_shows:
            clean_title = re.sub(r"\s*\(\d{4}\)\s*$", "", (show.title or "")).strip()
            if not clean_title:
                continue
            try:
                resp = await client.get(
                    "https://api.tvmaze.com/singlesearch/shows",
                    params={"q": clean_title, "embed": "episodes"},
                )
                if resp.status_code == 429:
                    await asyncio.sleep(2.5)
                    resp = await client.get(
                        "https://api.tvmaze.com/singlesearch/shows",
                        params={"q": clean_title, "embed": "episodes"},
                    )
                if resp.status_code != 200:
                    continue
                shows_checked += 1
                data = resp.json()
                episodes = data.get("_embedded", {}).get("episodes", [])
                for ep in episodes:
                    s_num = ep.get("season")
                    e_num = ep.get("number")
                    airdate = (ep.get("airdate") or "").strip()
                    ep_title = (ep.get("name") or "").strip()
                    if not s_num or not e_num or not airdate:
                        continue
                    existing = db.query(EpisodeItem).filter(
                        EpisodeItem.media_item_id == show.id,
                        EpisodeItem.season_number == s_num,
                        EpisodeItem.episode_number == e_num,
                    ).first()
                    if existing:
                        if existing.air_date != airdate or (ep_title and not existing.title):
                            existing.air_date = airdate
                            if ep_title and not existing.title:
                                existing.title = ep_title
                            updated_eps += 1
                    else:
                        db.add(EpisodeItem(
                            media_item_id=show.id,
                            season_number=s_num,
                            episode_number=e_num,
                            title=ep_title,
                            air_date=airdate,
                            has_file=False,
                        ))
                        updated_eps += 1
                if shows_checked % 15 == 0:
                    db.commit()
                    await asyncio.sleep(0.5)
            except Exception:
                errors += 1
                continue

        # Prioritize wanted/downloading movies and recent years (2024+) so current calendar months populate immediately
        from sqlalchemy import case
        all_movies = (
            db.query(MediaItem)
            .filter(MediaItem.media_type == "movie")
            .order_by(
                case((MediaItem.status != "available", 0), else_=1),
                MediaItem.year.desc().nullslast(),
                MediaItem.id.desc(),
            )
            .limit(400)
            .all()
        )

        # Build fast lookup map of Plex movie release dates (originallyAvailableAt) if Plex is configured
        plex_dates = {}
        plex_url = (cfg.get("plex_url") or "").strip().rstrip("/")
        plex_token = (cfg.get("plex_token") or "").strip()
        if plex_url and plex_token:
            try:
                sec_r = await client.get(
                    f"{plex_url}/library/sections",
                    headers={"X-Plex-Token": plex_token, "Accept": "application/json"},
                )
                if sec_r.status_code == 200:
                    for sec in (sec_r.json().get("MediaContainer", {}).get("Directory") or []):
                        if sec.get("type") == "movie":
                            key = sec.get("key")
                            lib_r = await client.get(
                                f"{plex_url}/library/sections/{key}/all",
                                headers={"X-Plex-Token": plex_token, "Accept": "application/json"},
                            )
                            if lib_r.status_code == 200:
                                for meta in (lib_r.json().get("MediaContainer", {}).get("Metadata") or []):
                                    p_title = (meta.get("title") or "").strip().lower()
                                    p_orig = (meta.get("originallyAvailableAt") or "").strip()
                                    if p_title and len(p_orig) >= 10:
                                        plex_dates[p_title] = p_orig[:10]
            except Exception:
                pass

        movies_checked = 0
        for mov in all_movies:
            if mov.release_date and len(mov.release_date) >= 10 and mov.status == "available":
                continue
            clean_mov = re.sub(r"\s*\(\d{4}\)\s*$", "", (mov.title or "")).strip()
            if not clean_mov:
                continue
            rel = plex_dates.get(clean_mov.lower(), "")
            try:
                if not rel and tmdb_key:
                    params = {"api_key": tmdb_key, "query": clean_mov}
                    if mov.year:
                        params["year"] = mov.year
                    r = await client.get("https://api.themoviedb.org/3/search/movie", params=params)
                    results = (r.json().get("results") or []) if r.status_code == 200 else []
                    if not results and mov.year:
                        r2 = await client.get(
                            "https://api.themoviedb.org/3/search/movie",
                            params={"api_key": tmdb_key, "query": clean_mov},
                        )
                        if r2.status_code == 200:
                            results = r2.json().get("results") or []
                    if results:
                        tmdb_id = results[0].get("id")
                        rel = (results[0].get("release_date") or "").strip()
                        if tmdb_id:
                            rd_resp = await client.get(
                                f"https://api.themoviedb.org/3/movie/{tmdb_id}/release_dates",
                                params={"api_key": tmdb_key},
                            )
                            if rd_resp.status_code == 200:
                                for country in (rd_resp.json().get("results") or []):
                                    if country.get("iso_3166_1") == "US":
                                        digital_dates = [
                                            d.get("release_date", "")[:10]
                                            for d in (country.get("release_dates") or [])
                                            if d.get("type") in (4, 5) and d.get("release_date")
                                        ]
                                        if digital_dates:
                                            digital_dates.sort()
                                            rel = digital_dates[0]
                                        break

                if not rel:
                    it_resp = await client.get(
                        "https://itunes.apple.com/search",
                        params={"term": clean_mov, "media": "movie", "entity": "movie", "limit": 5},
                    )
                    if it_resp.status_code == 200:
                        for item_res in (it_resp.json().get("results") or []):
                            r_date = (item_res.get("releaseDate") or "")[:10]
                            if r_date and (not mov.year or str(mov.year) in r_date or abs(int(r_date[:4]) - int(mov.year)) <= 1):
                                rel = r_date
                                break

                if rel and len(rel) >= 10 and mov.release_date != rel[:10]:
                    mov.release_date = rel[:10]
                    updated_movies += 1
                movies_checked += 1
                if movies_checked % 15 == 0:
                    db.commit()
                    await asyncio.sleep(0.15)
            except Exception:
                errors += 1
                continue

    db.commit()
    return {
        "shows_checked": shows_checked,
        "updated_episodes": updated_eps,
        "updated_movies": updated_movies,
        "tmdb_configured": bool(tmdb_key),
        "errors": errors,
    }



def _is_season_monitored(item, season_num: int) -> bool:
    import json as _json
    raw = getattr(item, "monitored_seasons", None) or "[]"
    try:
        parsed = _json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        parsed = []
    if not parsed:
        return True
    return int(season_num) in {int(x) for x in parsed if str(x).isdigit() or isinstance(x, int)}


def _resolve_target_root(item, media_type: str, db) -> Path:
    custom_rf = (getattr(item, "root_folder", None) or "").strip()
    if custom_rf:
        p = Path(custom_rf)
        try:
            p.mkdir(parents=True, exist_ok=True)
            return p
        except Exception:
            pass
    roots = get_media_roots(db).get(media_type, [])
    if not roots:
        return Path("/mnt") / media_type
    best_root = max(roots, key=lambda r: shutil.disk_usage(r).free if r.exists() else -1)
    best_root.mkdir(parents=True, exist_ok=True)
    return best_root

async def hunt_aired_calendar_items(db) -> int:
    """Automatically search and grab newly aired missing episodes and released wanted movies."""
    from datetime import datetime
    from .models import MediaItem, EpisodeItem, HistoryEvent

    today_str = datetime.now().strftime("%Y-%m-%d")
    grabbed = 0

    due_episodes = (
        db.query(EpisodeItem, MediaItem)
        .join(MediaItem, EpisodeItem.media_item_id == MediaItem.id)
        .filter(
            MediaItem.monitored == True,
            EpisodeItem.has_file == False,
            EpisodeItem.air_date != "",
            EpisodeItem.air_date <= today_str,
        )
        .order_by(EpisodeItem.air_date.desc())
        .limit(8)
        .all()
    )

    for ep, show in due_episodes:
        if not _is_season_monitored(show, getattr(ep, "season_number", 0)):
            continue
        tag = f"S{ep.season_number:02d}E{ep.episode_number:02d}"
        query = f"{show.title} {tag}"
        try:
            releases = await search_prowlarr(query, "tv", show.quality_profile, db)
            if releases:
                best = releases[0]
                dl_link = best.get("downloadUrl") or best.get("magnetUrl") or best.get("guid")
                t_hash = await send_to_qbittorrent(dl_link, show.id, db) if dl_link else None
                if t_hash:
                    show.torrent_hash = t_hash
                    show.status = "downloading"
                    db.add(HistoryEvent(
                        event_type="grab",
                        media_item_id=show.id,
                        title=f"{show.title} {tag}",
                        detail=f"Calendar auto-hunt grabbed: {best.get('title', query)}",
                    ))
                    grabbed += 1
        except Exception:
            continue

    db.commit()
    return grabbed


# === ONE-STOP HUB: TAUTULLI, LIBRARY HEALTH/CODEC AUDITOR & COLLECTIONS ===
import subprocess as _subprocess
import json as _json

async def fetch_tautulli_activity(cfg: dict) -> dict:
    url = (cfg.get("tautulli_url") or "").rstrip("/")
    key = (cfg.get("tautulli_api_key") or "").strip()
    if not url or not key:
        return {"configured": False, "stream_count": 0, "total_bandwidth": 0, "sessions": []}
    try:
        async with httpx.AsyncClient(verify=False, timeout=8.0) as client:
            r = await client.get(f"{url}/api/v2", params={"apikey": key, "cmd": "get_activity"})
            if r.status_code != 200:
                return {"configured": True, "error": f"HTTP {r.status_code}", "stream_count": 0, "sessions": []}
            data = (r.json().get("response") or {}).get("data") or {}
            sessions = []
            for s in (data.get("sessions") or []):
                sessions.append({
                    "user": s.get("friendly_name") or s.get("user") or "User",
                    "title": s.get("full_title") or s.get("title") or "Unknown",
                    "media_type": s.get("media_type") or "",
                    "state": s.get("state") or "playing",
                    "progress_percent": s.get("progress_percent") or 0,
                    "transcode_decision": s.get("transcode_decision") or "direct play",
                    "video_resolution": s.get("stream_video_full_resolution") or s.get("video_full_resolution") or "",
                    "bandwidth": s.get("bandwidth") or 0,
                    "player": s.get("player") or "",
                })
            return {
                "configured": True,
                "stream_count": int(data.get("stream_count") or len(sessions)),
                "total_bandwidth": data.get("total_bandwidth") or 0,
                "sessions": sessions,
            }
    except Exception as e:
        return {"configured": True, "error": str(e), "stream_count": 0, "sessions": []}


from datetime import datetime, timedelta

async def fetch_tautulli_watch_stats(cfg: dict, items: list) -> dict:
    url = (cfg.get("tautulli_url") or "").rstrip("/")
    key = (cfg.get("tautulli_api_key") or "").strip()
    if not url or not key:
        return {
            "configured": False,
            "stale_items": [],
            "recent_watches": [],
            "top_movies": [],
            "top_tv": [],
            "top_users": [],
        }
    try:
        async with httpx.AsyncClient(verify=False, timeout=12.0) as client:
            r = await client.get(f"{url}/api/v2", params={"apikey": key, "cmd": "get_history", "length": 1500})
            if r.status_code != 200:
                return {
                    "configured": True,
                    "error": f"HTTP {r.status_code}",
                    "stale_items": [],
                    "recent_watches": [],
                    "top_movies": [],
                    "top_tv": [],
                    "top_users": [],
                }
            rows = ((r.json().get("response") or {}).get("data") or {}).get("data") or []
            last_played = {}
            play_counts = {}
            recent_watches = []
            for row in rows:
                t_raw = (row.get("grandparent_title") or row.get("title") or "").strip()
                t_norm = re.sub(r"\s*\(\d{4}\)\s*$", "", t_raw).lower()
                ts = int(row.get("stopped") or row.get("started") or 0)
                if t_norm:
                    play_counts[t_norm] = play_counts.get(t_norm, 0) + 1
                    if ts > last_played.get(t_norm, 0):
                        last_played[t_norm] = ts
                if len(recent_watches) < 25:
                    recent_watches.append({
                        "title": row.get("full_title") or t_raw,
                        "user": row.get("friendly_name") or row.get("user") or "User",
                        "player": row.get("player") or "",
                        "date": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "",
                        "percent_complete": row.get("percent_complete") or 0,
                    })

            # Fetch Tautulli Home Stats (Top Movies, Top TV Shows, Top Users)
            top_movies, top_tv, top_users = [], [], []
            try:
                r_hs = await client.get(
                    f"{url}/api/v2",
                    params={"apikey": key, "cmd": "get_home_stats", "time_range": 30, "stats_count": 8},
                )
                if r_hs.status_code == 200:
                    stat_blocks = (r_hs.json().get("response") or {}).get("data") or []
                    for blk in stat_blocks:
                        sid = blk.get("stat_id") or ""
                        srows = blk.get("rows") or []
                        if sid == "top_movies":
                            top_movies = [
                                {"title": x.get("title") or "", "year": x.get("year") or "", "plays": x.get("total_plays") or 0}
                                for x in srows[:8]
                            ]
                        elif sid == "top_tv":
                            top_tv = [
                                {"title": x.get("title") or "", "plays": x.get("total_plays") or 0}
                                for x in srows[:8]
                            ]
                        elif sid == "top_users":
                            top_users = [
                                {
                                    "user": x.get("friendly_name") or x.get("user") or "User",
                                    "plays": x.get("total_plays") or 0,
                                    "duration_hrs": round(float(x.get("total_duration") or 0) / 3600.0, 1),
                                }
                                for x in srows[:8]
                            ]
            except Exception:
                pass

            cutoff_ts = int((datetime.utcnow() - timedelta(days=365)).timestamp())
            stale_items = []
            for it in items:
                if it.status != "available":
                    continue
                norm = re.sub(r"\s*\(\d{4}\)\s*$", "", (it.title or "")).strip().lower()
                lp = last_played.get(norm, 0)
                if lp == 0 or lp < cutoff_ts:
                    stale_items.append({
                        "id": it.id,
                        "title": it.title,
                        "year": it.year,
                        "media_type": it.media_type,
                        "plays": play_counts.get(norm, 0),
                        "last_watched": datetime.fromtimestamp(lp).strftime("%Y-%m-%d") if lp else "Never watched",
                        "file_path": it.file_path or "",
                    })
            return {
                "configured": True,
                "stale_items": stale_items[:120],
                "recent_watches": recent_watches,
                "top_movies": top_movies,
                "top_tv": top_tv,
                "top_users": top_users,
            }
    except Exception as e:
        return {
            "configured": True,
            "error": str(e),
            "stale_items": [],
            "recent_watches": [],
            "top_movies": [],
            "top_tv": [],
            "top_users": [],
        }

def _probe_video_file(fpath: Path, allow_ffprobe: bool = True) -> dict:
    import shutil as _shutil
    info = {"codec": "unknown", "resolution": "unknown", "audio": "unknown", "size_gb": 0.0}
    try:
        info["size_gb"] = round(fpath.stat().st_size / (1024**3), 2)
    except Exception:
        pass
    try:
        if not allow_ffprobe or not _shutil.which("ffprobe"):
            raise RuntimeError("ffprobe unavailable or skipped")
        cmd = [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_streams", str(fpath)
        ]
        out = _subprocess.check_output(cmd, timeout=2.5, stderr=_subprocess.DEVNULL)
        streams = _json.loads(out).get("streams") or []
        for st in streams:
            ctype = st.get("codec_type")
            if ctype == "video" and info["codec"] == "unknown":
                info["codec"] = (st.get("codec_name") or "unknown").upper()
                h = int(st.get("height") or 0)
                w = int(st.get("width") or 0)
                if h >= 2000 or w >= 3800:
                    info["resolution"] = "4K"
                elif h >= 1000 or w >= 1900:
                    info["resolution"] = "1080p"
                elif h >= 700 or w >= 1200:
                    info["resolution"] = "720p"
                elif h > 0:
                    info["resolution"] = "SD"
            elif ctype == "audio" and info["audio"] == "unknown":
                info["audio"] = (st.get("codec_name") or "unknown").upper()
    except Exception:
        # Filename heuristic fallback if ffprobe is not installed
        name_u = fpath.name.upper()
        for c in ("AV1", "HEVC", "H265", "X265", "H264", "X264"):
            if c in name_u:
                info["codec"] = "HEVC" if c in ("HEVC", "H265", "X265") else ("H264" if c in ("H264", "X264") else c)
                break
        for r in ("2160P", "4K", "1080P", "720P", "480P"):
            if r in name_u:
                info["resolution"] = "4K" if r in ("2160P", "4K") else r.lower()
                break
    return info


def run_library_health_audit(db: Session) -> dict:
    cfg = get_settings(db)
    items = db.query(MediaItem).filter_by(status="available").all()
    video_exts = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".mov"}
    codec_counts = {}
    res_counts = {}
    h264_candidates = []
    duplicates = []
    tracked_paths = set()

    probed_count = 0
    max_probes = 150
    for it in items:
        if not it.file_path:
            continue
        p = Path(it.file_path)
        try:
            if not p.exists():
                continue
        except Exception:
            continue
        vfiles = []
        try:
            if p.is_file():
                if p.suffix.lower() in video_exts and "sample" not in p.name.lower():
                    vfiles = [p]
            else:
                for f in p.rglob("*"):
                    if f.is_file() and f.suffix.lower() in video_exts and "sample" not in f.name.lower():
                        vfiles.append(f)
                        if len(vfiles) >= 25:
                            break
        except Exception:
            vfiles = []
        for vf in vfiles:
            try:
                tracked_paths.add(str(vf.resolve()))
            except Exception:
                tracked_paths.add(str(vf))
        if it.media_type == "movie" and len(vfiles) > 1:
            duplicates.append({
                "id": it.id,
                "title": it.title,
                "folder": str(p),
                "files": [f"{f.name} ({round(f.stat().st_size / (1024**3), 2)} GB)" for f in vfiles],
            })
        # Probe first/largest file per item with a cap so large libraries do not time out
        if vfiles:
            primary = max(vfiles, key=lambda f: f.stat().st_size)
            can_probe = probed_count < max_probes
            meta = _probe_video_file(primary, allow_ffprobe=can_probe)
            if can_probe:
                probed_count += 1
            codec_counts[meta["codec"]] = codec_counts.get(meta["codec"], 0) + 1
            res_counts[meta["resolution"]] = res_counts.get(meta["resolution"], 0) + 1
            if meta["codec"] in ("H264", "AVC") and meta["size_gb"] >= 2.0:
                h264_candidates.append({
                    "id": it.id,
                    "title": it.title,
                    "media_type": it.media_type,
                    "codec": meta["codec"],
                    "resolution": meta["resolution"],
                    "audio": meta["audio"],
                    "size_gb": meta["size_gb"],
                    "file_name": primary.name,
                })

    # Scan download mount for orphaned unlinked video files
    orphaned_downloads = []
    dl_mount = Path((cfg.get("qbit_local_mount") or "").strip())
    scanned_dl_files = 0
    if dl_mount.exists() and dl_mount.is_dir():
        for f in dl_mount.rglob("*"):
            scanned_dl_files += 1
            if scanned_dl_files > 2500:
                break
            if f.is_file() and f.suffix.lower() in video_exts and "sample" not in f.name.lower():
                try:
                    sz_gb = round(f.stat().st_size / (1024**3), 2)
                    if sz_gb >= 0.25 and str(f.resolve()) not in tracked_paths:
                        orphaned_downloads.append({
                            "name": f.name,
                            "path": str(f),
                            "size_gb": sz_gb,
                        })
                except Exception:
                    pass

    h264_candidates.sort(key=lambda x: x["size_gb"], reverse=True)
    orphaned_downloads.sort(key=lambda x: x["size_gb"], reverse=True)
    return {
        "scanned_items": len(items),
        "codecs": codec_counts,
        "resolutions": res_counts,
        "h264_candidates": h264_candidates[:60],
        "duplicates": duplicates[:40],
        "orphaned_downloads": orphaned_downloads[:50],
    }


async def scan_franchise_collections(db: Session) -> list:
    cfg = get_settings(db)
    tmdb_key = (cfg.get("tmdb_api_key") or "").strip()
    plex_url = (cfg.get("plex_url") or "").split("/web")[0].rstrip("/")
    plex_token = (cfg.get("plex_token") or "").strip()
    movies = db.query(MediaItem).filter_by(media_type="movie").all()
    owned_by_title = {re.sub(r"\s*\(\d{4}\)\s*$", "", m.title).strip().lower(): m for m in movies}
    collections_map = {}

    async with httpx.AsyncClient(verify=False, timeout=12.0) as client:
        # 1. TMDB Franchise Collections (discovers missing sequels automatically)
        if tmdb_key:
            seen_col_ids = set()
            for m in movies[:80]:
                clean_t = re.sub(r"\s*\(\d{4}\)\s*$", "", m.title).strip()
                try:
                    params = {"api_key": tmdb_key, "query": clean_t}
                    if m.year:
                        params["year"] = m.year
                    rs = await client.get("https://api.themoviedb.org/3/search/movie", params=params)
                    hits = (rs.json().get("results") or []) if rs.status_code == 200 else []
                    if not hits:
                        continue
                    mid = hits[0]["id"]
                    rd = await client.get(f"https://api.themoviedb.org/3/movie/{mid}", params={"api_key": tmdb_key})
                    col_info = (rd.json().get("belongs_to_collection") or {}) if rd.status_code == 200 else {}
                    cid = col_info.get("id")
                    if not cid or cid in seen_col_ids:
                        continue
                    seen_col_ids.add(cid)
                    rc = await client.get(f"https://api.themoviedb.org/3/collection/{cid}", params={"api_key": tmdb_key})
                    if rc.status_code != 200:
                        continue
                    cdata = rc.json()
                    parts = sorted(cdata.get("parts") or [], key=lambda x: x.get("release_date") or "9999")
                    if len(parts) < 2:
                        continue
                    part_rows = []
                    owned_cnt = 0
                    for p in parts:
                        ptitle = p.get("title") or ""
                        pdate = p.get("release_date") or ""
                        pyear = int(pdate[:4]) if len(pdate) >= 4 and pdate[:4].isdigit() else None
                        matched = owned_by_title.get(ptitle.lower())
                        is_owned = bool(matched and matched.status == "available")
                        if is_owned:
                            owned_cnt += 1
                        part_rows.append({
                            "title": ptitle,
                            "year": pyear,
                            "release_date": pdate,
                            "poster_url": f"https://image.tmdb.org/t/p/w185{p['poster_path']}" if p.get("poster_path") else "",
                            "owned": is_owned,
                            "tracked_id": matched.id if matched else None,
                            "status": matched.status if matched else "missing",
                        })
                    collections_map[f"tmdb_{cid}"] = {
                        "id": f"tmdb_{cid}",
                        "name": cdata.get("name") or col_info.get("name") or "Collection",
                        "source": "TMDB Franchise",
                        "poster_url": f"https://image.tmdb.org/t/p/w342{cdata['poster_path']}" if cdata.get("poster_path") else "",
                        "owned_count": owned_cnt,
                        "total_count": len(part_rows),
                        "parts": part_rows,
                    }
                except Exception:
                    continue

        # 2. Plex Collections fallback / supplement
        if plex_url and plex_token:
            try:
                r_sec = await client.get(f"{plex_url}/library/sections", params={"X-Plex-Token": plex_token}, headers={"Accept": "application/json"})
                if r_sec.status_code == 200:
                    for sec in (r_sec.json().get("MediaContainer", {}).get("Directory") or []):
                        if sec.get("type") != "movie":
                            continue
                        sk = sec.get("key")
                        r_cols = await client.get(f"{plex_url}/library/sections/{sk}/collections", params={"X-Plex-Token": plex_token}, headers={"Accept": "application/json"})
                        if r_cols.status_code != 200:
                            continue
                        for col in (r_cols.json().get("MediaContainer", {}).get("Metadata") or []):
                            cname = col.get("title") or ""
                            ckey = col.get("ratingKey")
                            if not cname or not ckey:
                                continue
                            if any(v["name"].lower() == cname.lower() for v in collections_map.values()):
                                continue
                            r_items = await client.get(f"{plex_url}/library/collections/{ckey}/children", params={"X-Plex-Token": plex_token}, headers={"Accept": "application/json"})
                            children = (r_items.json().get("MediaContainer", {}).get("Metadata") or []) if r_items.status_code == 200 else []
                            if not children:
                                continue
                            part_rows = []
                            for ch in children:
                                ct = ch.get("title") or ""
                                matched = owned_by_title.get(ct.lower())
                                part_rows.append({
                                    "title": ct,
                                    "year": ch.get("year"),
                                    "release_date": ch.get("originallyAvailableAt") or "",
                                    "poster_url": f"{plex_url}{ch['thumb']}?X-Plex-Token={plex_token}" if ch.get("thumb") else "",
                                    "owned": True,
                                    "tracked_id": matched.id if matched else None,
                                    "status": "available",
                                })
                            collections_map[f"plex_{ckey}"] = {
                                "id": f"plex_{ckey}",
                                "name": cname,
                                "source": "Plex Collection",
                                "poster_url": f"{plex_url}{col['thumb']}?X-Plex-Token={plex_token}" if col.get("thumb") else "",
                                "owned_count": len(part_rows),
                                "total_count": len(part_rows),
                                "parts": part_rows,
                            }
            except Exception:
                pass

    res_list = list(collections_map.values())
    res_list.sort(key=lambda c: (c["total_count"] - c["owned_count"], c["name"]), reverse=True)
    return res_list

TRASH_PRESETS = {
    "4k_hdr_atmos": {
        "label": "4K HDR10 / Dolby Vision + Atmos",
        "formats": [
            {"name": "Dolby Vision / HDR10+", "patterns": [r"\b(DV|DoVi|Dolby[ ._-]?Vision|HDR10Plus|HDR10\+)\b"], "score": 150},
            {"name": "TrueHD Atmos / DTS-HD MA", "patterns": [r"\b(TrueHD|Atmos|DTS-HD[ ._-]?MA|DTS:X)\b"], "score": 120},
            {"name": "2160p Remux / WEB-DL", "patterns": [r"\b2160p\b.*\b(Remux|WEB-DL)\b"], "score": 100},
        ],
    },
    "balanced_1080p_hevc": {
        "label": "Balanced 1080p HEVC / x265",
        "formats": [
            {"name": "x265 / HEVC / AV1", "patterns": [r"\b(x265|H\.?265|HEVC|AV1)\b"], "score": 85},
            {"name": "1080p Bluray / WEB-DL", "patterns": [r"\b1080p\b.*\b(BluRay|WEB-DL|WEBRip)\b"], "score": 60},
        ],
    },
    "block_cam_hc": {
        "label": "Block CAM / Telesync / Hardcoded Subs",
        "formats": [
            {"name": "CAM / TS / Screener Block", "patterns": [r"\b(CAM|HDCAM|TS|HDTS|TELESYNC|TC|WORKPRINT|SCR|DVDSCR)\b"], "score": -10000},
            {"name": "Hardcoded / KORSUB Block", "patterns": [r"\b(HC|KORSUB|SUBBED|HARDSUB)\b"], "score": -5000},
        ],
    },
    "prefer_proper_repack": {
        "label": "Prefer Proper / Repack Releases",
        "formats": [
            {"name": "PROPER / REPACK", "patterns": [r"\b(PROPER|REPACK|RERIP)\b"], "score": 75},
        ],
    },
}

def apply_trash_preset(db, preset_key: str) -> int:
    import json
    from .models import CustomFormat
    preset = TRASH_PRESETS.get(preset_key)
    if not preset:
        return 0
    added = 0
    for fmt in preset["formats"]:
        existing = db.query(CustomFormat).filter_by(name=fmt["name"]).first()
        if existing:
            existing.regex_patterns_json = json.dumps(fmt["patterns"])
            existing.default_score = fmt["score"]
        else:
            db.add(CustomFormat(
                name=fmt["name"],
                source_app="trash",
                regex_patterns_json=json.dumps(fmt["patterns"]),
                default_score=fmt["score"],
            ))
            added += 1
    db.commit()
    return added

async def hunt_quality_upgrades(db) -> int:
    """Scan monitored available items whose file quality is below their profile cutoff and grab upgrades."""
    from .models import MediaItem, QualityProfile, HistoryEvent
    cfg = get_settings(db)
    if str(cfg.get("auto_upgrade_enabled", "true")).lower() != "true":
        return 0

    candidates = (
        db.query(MediaItem)
        .filter(MediaItem.monitored == True, MediaItem.status == "available")
        .order_by(MediaItem.id.desc())
        .limit(120)
        .all()
    )
    upgraded = 0
    for item in candidates:
        if upgraded >= 3:
            break
        fp = (item.file_path or "").lower()
        if not fp:
            continue
        prof = db.query(QualityProfile).filter_by(name=item.quality_profile).first()
        if prof and not prof.upgrade_allowed:
            continue
        target = (prof.cutoff if prof else (item.quality_profile or "")).lower()
        needs_upgrade = False
        if "2160" in target and "2160" not in fp and "4k" not in fp:
            needs_upgrade = True
        elif "1080" in target and not any(k in fp for k in ("1080p", "2160p", "4k")):
            needs_upgrade = True
        if not needs_upgrade:
            continue

        import json as _json
        upgrade_queries = [f"{item.title} {item.year}" if item.year else item.title]
        if (item.media_type or "").lower() == "tv":
            try:
                raw_ms = getattr(item, "monitored_seasons", None) or "[]"
                parsed_ms = _json.loads(raw_ms) if isinstance(raw_ms, str) else raw_ms
            except Exception:
                parsed_ms = []
            ms_nums = sorted({
                int(x) for x in (parsed_ms if isinstance(parsed_ms, list) else [])
                if (isinstance(x, int) and not isinstance(x, bool)) or (isinstance(x, str) and x.strip().isdigit())
            })
            if ms_nums:
                upgrade_queries = [f"{item.title} S{s:02d}" for s in ms_nums]
        try:
            grabbed_this_item = False
            for query in upgrade_queries:
                releases = await search_prowlarr(query, item.media_type, item.quality_profile, db)
                if releases:
                    best = releases[0]
                    best_title = (best.get("title") or "").lower()
                    if ("2160" in target and ("2160" in best_title or "4k" in best_title)) or ("1080" in target and "1080" in best_title):
                        dl_link = best.get("downloadUrl") or best.get("magnetUrl") or best.get("guid")
                        t_hash = await send_to_qbittorrent(dl_link, item.id, db) if dl_link else None
                        if t_hash:
                            item.torrent_hash = t_hash
                            item.status = "downloading"
                            db.add(HistoryEvent(
                                event_type="upgrade_grabbed",
                                media_title=item.title,
                                details=f"Quality cutoff upgrade ({target}): {best.get('title', '')[:80]}",
                            ))
                            upgraded += 1
                            grabbed_this_item = True
                            break
            if grabbed_this_item:
                pass
        except Exception:
            continue
    if upgraded:
        db.commit()
    return upgraded

