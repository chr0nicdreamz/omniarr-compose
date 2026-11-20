from fastapi.responses import FileResponse
import tempfile
import sqlite3
import secrets
from pathlib import Path
import shutil
import os
import json
import httpx
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, Request, Form, HTTPException, Header, UploadFile, File, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .database import engine, Base, get_db, SessionLocal
from .models import MediaItem, EpisodeItem, IndexerConfig, BlacklistedRelease, QualityProfile, CustomFormat
from .services import (
    search_prowlarr, manual_search_prowlarr, send_to_qbittorrent, check_downloads_and_import,
    get_settings, save_settings, get_drive_stats, get_media_roots, sync_posters_from_plex
)
from .importer import parse_and_import_backup

Base.metadata.create_all(bind=engine)
templates = Jinja2Templates(directory="app/templates")

async def scheduled_tasks():
    db = SessionLocal()
    try:
        await check_downloads_and_import(db)
        wanted = db.query(MediaItem).filter(MediaItem.status == "wanted", MediaItem.monitored == True).all()
        for item in wanted:
            query = f"{item.title} {item.year}" if item.year else item.title
            releases = await search_prowlarr(query, item.media_type, item.quality_profile, db)
            if releases:
                best = releases[0]
                dl_link = best.get("downloadUrl") or best.get("magnetUrl") or best.get("guid")
                t_hash = await send_to_qbittorrent(dl_link, item.id, db) if dl_link else None
                if t_hash:
                    item.torrent_hash = t_hash
                    item.status = "downloading"
                    db.commit()
    finally:
        db.close()

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        from .database import SessionLocal
        _db = SessionLocal()
        _cfg = get_settings(_db)
        if not _cfg.get("omniarr_api_key") or _cfg.get("omniarr_api_key") == "change_me_to_a_secure_key":
            save_settings(_db, {"omniarr_api_key": secrets.token_hex(16)})
        _db.close()
    except Exception:
        pass
    scheduler = AsyncIOScheduler()
    scheduler.add_job(scheduled_tasks, "interval", minutes=2)
    import fcntl as _fcntl
    _sched_lock = open("/tmp/omniarr_scheduler.lock", "w")
    try:
        _fcntl.flock(_sched_lock, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        scheduler.start()
    except BlockingIOError:
        pass
    yield
    scheduler.shutdown()

from .services import (
    get_settings, save_settings, get_drive_stats, get_media_roots,
    get_best_dest_dir, translate_qbit_path, transfer_file, trigger_plex_scan,
    search_prowlarr, manual_search_prowlarr, send_to_qbittorrent,
    check_downloads_and_import, sync_posters_from_plex, fetch_media_metadata, move_media_folder
)

app = FastAPI(title="OmniArr", lifespan=lifespan)

def verify_api_key(x_api_key: str = Header(None), apikey: str = None, db: Session = Depends(get_db)):
    cfg = get_settings(db)
    key = x_api_key or apikey
    if key != cfg["omniarr_api_key"]:
        raise HTTPException(status_code=401, detail="Invalid API Key")


@app.get("/api/activity")
async def api_get_activity(db: Session = Depends(get_db)):
    cfg = get_settings(db)
    qbit_url = cfg["qbit_url"].rstrip("/")
    q_hdr = {"Referer": qbit_url, "Origin": qbit_url}
    items_by_hash = {i.torrent_hash: i for i in db.query(MediaItem).filter(MediaItem.torrent_hash != None).all()}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(f"{qbit_url}/api/v2/auth/login", data={"username": cfg["qbit_user"], "password": cfg["qbit_pass"]}, headers=q_hdr)
            resp = await client.get(f"{qbit_url}/api/v2/torrents/info", params={"category": cfg["qbit_category"]}, headers=q_hdr)
            raw_list = resp.json() if resp.status_code == 200 else []
    except Exception as e:
        return {"error": str(e), "torrents": [], "blacklist": []}

    torrents_out = []
    for t in raw_list:
        thash = t.get("hash", "")
        linked = items_by_hash.get(thash)
        torrents_out.append({
            "hash": thash,
            "name": t.get("name", ""),
            "progress": round((t.get("progress", 0) or 0) * 100, 1),
            "dlspeed_mb": round((t.get("dlspeed", 0) or 0) / (1024 * 1024), 2),
            "eta": t.get("eta", 0),
            "state": t.get("state", "unknown"),
            "size_gb": round((t.get("size", 0) or 0) / (1024**3), 2),
            "media_id": linked.id if linked else None,
            "media_title": linked.title if linked else None,
        })
    recent_bl = db.query(BlacklistedRelease).order_by(BlacklistedRelease.id.desc()).limit(20).all()
    bl_out = [{"id": b.id, "title": b.release_title, "reason": b.reason, "date": b.created_at.strftime("%Y-%m-%d %H:%M") if b.created_at else ""} for b in recent_bl]
    return {"torrents": torrents_out, "blacklist": bl_out}


@app.post("/api/activity/import")
async def api_force_import_torrent(payload: dict, db: Session = Depends(get_db)):
    try:
        return await _do_force_import_torrent(payload, db)
    except HTTPException as he:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=he.status_code, content={"ok": False, "detail": he.detail})
    except Exception as e:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=500, content={"ok": False, "detail": f"{type(e).__name__}: {e}"})

async def _do_force_import_torrent(payload: dict, db: Session):
    thash = (payload.get("hash") or "").strip()
    raw_mid = payload.get("media_id")
    media_id = int(raw_mid) if str(raw_mid or "").strip().isdigit() else None
    cfg = get_settings(db)
    qbit_url = cfg["qbit_url"].rstrip("/")
    q_hdr = {"Referer": qbit_url, "Origin": qbit_url}

    async with httpx.AsyncClient(timeout=15.0) as client:
        await client.post(f"{qbit_url}/api/v2/auth/login", data={"username": cfg["qbit_user"], "password": cfg["qbit_pass"]}, headers=q_hdr)
        resp = await client.get(f"{qbit_url}/api/v2/torrents/info", params={"hashes": thash} if thash else {"category": cfg["qbit_category"]}, headers=q_hdr)
        torrents = resp.json() if resp.status_code == 200 else []

    target_t = None
    if thash:
        target_t = next((t for t in torrents if t.get("hash") == thash), None)
    elif media_id:
        item = db.query(MediaItem).filter_by(id=int(media_id)).first()
        if item and item.torrent_hash:
            target_t = next((t for t in torrents if t.get("hash") == item.torrent_hash), None)

    if not target_t:
        raise HTTPException(status_code=404, detail="Torrent not found in qBittorrent")

    item = db.query(MediaItem).filter_by(id=int(media_id)).first() if media_id else db.query(MediaItem).filter_by(torrent_hash=target_t["hash"]).first()
    if not item:
        raise HTTPException(status_code=400, detail="Torrent is not linked to a library item. Use Manual Import Scanner below to pick the title.")

    content_path = translate_qbit_path(target_t["content_path"], cfg)
    if not content_path.exists():
        raise HTTPException(status_code=400, detail=f"Path not found on disk: {content_path}")

    fmt_key = f"{item.media_type}_folder_format"
    template = cfg.get(fmt_key, "{Title} ({Year})")
    folder_name = template.replace("{Title}", item.title).replace("{Year}", str(item.year or "")).replace("()", "").strip()
    clean_name = re.sub(r'[\\/*?:"<>|]', "", folder_name)
    target_base = get_best_dest_dir(item.media_type, cfg, db) / clean_name
    mode = cfg.get("import_mode", "hardlink")

    imported_files = 0
    if content_path.is_file():
        transfer_file(content_path, target_base / content_path.name, mode)
        imported_files = 1
    elif content_path.is_dir():
        for root, _, files in os.walk(content_path):
            for f in files:
                if f.lower().endswith((".mkv", ".mp4", ".avi", ".m4v", ".flac", ".mp3", ".m4a")) and "sample" not in f.lower():
                    transfer_file(Path(root) / f, target_base / f, mode)
                    imported_files += 1

    if imported_files == 0:
        raise HTTPException(status_code=400, detail="No media files found inside torrent directory")

    item.status = "available"
    item.file_path = str(target_base)
    db.commit()

    if cfg.get("plex_notify_import", "true") == "true":
        await trigger_plex_scan(cfg)
    return {"ok": True, "files": imported_files, "dest": str(target_base)}

@app.post("/api/activity/blacklist")
async def api_blacklist_torrent(payload: dict, db: Session = Depends(get_db)):
    thash = payload.get("hash", "").strip()
    tname = payload.get("name", "").strip()
    media_id = payload.get("media_id")
    cfg = get_settings(db)
    qbit_url = cfg["qbit_url"].rstrip("/")
    q_hdr = {"Referer": qbit_url, "Origin": qbit_url}
    if thash:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(f"{qbit_url}/api/v2/auth/login", data={"username": cfg["qbit_user"], "password": cfg["qbit_pass"]}, headers=q_hdr)
                await client.post(f"{qbit_url}/api/v2/torrents/delete", data={"hashes": thash, "deleteFiles": "true"}, headers=q_hdr)
        except Exception:
            pass
    db.add(BlacklistedRelease(media_item_id=media_id, torrent_hash=thash, release_title=tname, reason="Manual blacklist & retry"))
    if media_id:
        item = db.query(MediaItem).filter_by(id=int(media_id)).first()
        if item:
            item.torrent_hash = None
            item.status = "wanted"
    db.commit()
    return {"ok": True}

@app.get("/api/manual-import/scan")
def api_scan_unmapped(db: Session = Depends(get_db)):
    cfg = get_settings(db)
    mount = Path(cfg.get("qbit_local_mount", "/downloads"))
    if not mount.exists():
        return {"files": [], "media": []}
    exts = (".mkv", ".mp4", ".avi", ".m4v")
    found = []
    for root, _, files in os.walk(mount):
        for f in files:
            if f.lower().endswith(exts) and "sample" not in f.lower():
                p = Path(root) / f
                try:
                    sz = round(p.stat().st_size / (1024**3), 2)
                except OSError:
                    sz = 0
                if sz >= 0.05:
                    found.append({"path": str(p), "name": f, "size_gb": sz})
        if len(found) >= 100:
            break
    media_list = [{"id": i.id, "title": f"{i.title} ({i.year})" if i.year else i.title, "media_type": i.media_type} for i in db.query(MediaItem).order_by(MediaItem.title.asc()).all()]
    return {"files": found[:100], "media": media_list}

@app.post("/api/manual-import/run")
async def api_run_manual_import(payload: dict, db: Session = Depends(get_db)):
    src_str = payload.get("file_path", "").strip()
    media_id = int(payload.get("media_id", 0))
    src = Path(src_str)
    item = db.query(MediaItem).filter_by(id=media_id).first()
    if not src.exists() or not item:
        raise HTTPException(status_code=400, detail="Invalid source file or target media item")
    cfg = get_settings(db)
    fmt_key = f"{item.media_type}_folder_format"
    template = cfg.get(fmt_key, "{Title} ({Year})")
    folder_name = template.replace("{Title}", item.title).replace("{Year}", str(item.year or "")).replace("()", "").strip()
    clean_name = re.sub(r'[\/*?:"<>|]', "", folder_name)
    target_base = get_best_dest_dir(item.media_type, cfg, db) / clean_name
    transfer_file(src, target_base / src.name, cfg.get("import_mode", "hardlink"))
    item.status = "available"
    item.file_path = str(target_base)
    db.commit()
    if cfg.get("plex_notify_import", "true") == "true":
        await trigger_plex_scan(cfg)
    return {"ok": True, "dest": str(target_base / src.name)}

# --- WEB UI ROUTES ---

@app.post("/api/settings/rotate-key")
def rotate_omniarr_api_key(db: Session = Depends(get_db)):
    new_key = secrets.token_hex(16)
    save_settings(db, {"omniarr_api_key": new_key})
    return RedirectResponse(url="/?tab=connect&msg=OmniArr+API+Key+rotated+successfully", status_code=303)

_HEALTH_CACHE = {"ts": 0.0, "data": None}

@app.get("/api/health")
async def service_health(force: bool = False, db: Session = Depends(get_db)):
    import time as _t
    now = _t.time()
    if not force and _HEALTH_CACHE["data"] and (now - _HEALTH_CACHE["ts"]) < 60:
        return _HEALTH_CACHE["data"]
    cfg = get_settings(db)
    status = {
        "prowlarr": {"ok": False, "configured": bool(cfg.get("prowlarr_api_key"))},
        "qbittorrent": {"ok": False, "configured": bool(cfg.get("qbit_url"))},
        "media": {"ok": False, "configured": bool(cfg.get("plex_token") or cfg.get("jellyfin_api_key") or cfg.get("emby_api_key")), "label": "Media"}
    }
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            if status["prowlarr"]["configured"]:
                try:
                    r = await client.get(f"{cfg['prowlarr_url'].rstrip('/')}/api/v1/system/status", headers={"X-Api-Key": cfg["prowlarr_api_key"]})
                    status["prowlarr"]["ok"] = (r.status_code == 200)
                except Exception:
                    pass
            if status["qbittorrent"]["configured"]:
                try:
                    q_url = cfg["qbit_url"].rstrip("/")
                    q_hdr = {"Referer": q_url, "Origin": q_url}
                    r = await client.get(f"{q_url}/api/v2/app/version", headers=q_hdr)
                    if r.status_code == 200:
                        status["qbittorrent"]["ok"] = True
                    else:
                        lr = await client.post(f"{q_url}/api/v2/auth/login", data={"username": cfg["qbit_user"], "password": cfg["qbit_pass"]}, headers=q_hdr)
                        status["qbittorrent"]["ok"] = (lr.status_code == 200 and lr.text.strip() == "Ok.")
                except Exception:
                    pass
            if cfg.get("plex_token"):
                status["media"]["label"] = "Plex"
                try:
                    r = await client.get(f"{cfg['plex_url'].rstrip('/')}/identity", params={"X-Plex-Token": cfg["plex_token"]}, headers={"Accept": "application/json"})
                    status["media"]["ok"] = (r.status_code == 200)
                except Exception:
                    pass
            elif cfg.get("jellyfin_api_key"):
                status["media"]["label"] = "Jellyfin"
                try:
                    r = await client.get(f"{cfg['jellyfin_url'].rstrip('/')}/System/Info", headers={"X-Emby-Token": cfg["jellyfin_api_key"]})
                    status["media"]["ok"] = (r.status_code == 200)
                except Exception:
                    pass
            elif cfg.get("emby_api_key"):
                status["media"]["label"] = "Emby"
                try:
                    r = await client.get(f"{cfg['emby_url'].rstrip('/')}/System/Info", headers={"X-Emby-Token": cfg["emby_api_key"]})
                    status["media"]["ok"] = (r.status_code == 200)
                except Exception:
                    pass
    except Exception:
        pass
    _HEALTH_CACHE["ts"] = now
    _HEALTH_CACHE["data"] = status
    return status

@app.get("/api/settings/backup")
def download_db_backup():
    db_file = Path("/app/config/omniarr.db")
    if not db_file.exists():
        db_file = Path("omniarr.db")
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    src = sqlite3.connect(str(db_file))
    dst = sqlite3.connect(tmp.name)
    with dst:
        src.backup(dst)
    dst.close()
    src.close()
    return FileResponse(tmp.name, filename="omniarr-backup.db", media_type="application/octet-stream")

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, tab: str = "library", sort: str = "added_desc", q: str = "", media_type: str = "", page: int = 1, msg: str = "", db: Session = Depends(get_db)):
    base_q = db.query(MediaItem)
    search_q = q.strip()
    if search_q:
        base_q = base_q.filter(MediaItem.title.ilike(f"%{search_q}%"))

    if sort == "added_asc":
        order_col = MediaItem.added_at.asc()
    elif sort == "title_asc":
        order_col = MediaItem.title.asc()
    elif sort == "title_desc":
        order_col = MediaItem.title.desc()
    else:
        order_col = MediaItem.added_at.desc()

    per_page = 30
    movie_q = base_q.filter(MediaItem.media_type == "movie")
    tv_q = base_q.filter(MediaItem.media_type == "tv")

    total_movies = movie_q.count()
    total_tv = tv_q.count()
    total_items = total_movies + total_tv

    max_count = max(total_movies, total_tv, 1)
    total_pages = max(1, (max_count + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    offset = (page - 1) * per_page

    movie_slice = movie_q.order_by(order_col).offset(offset).limit(per_page).all()
    tv_slice = tv_q.order_by(order_col).offset(offset).limit(per_page).all()
    items = movie_slice + tv_slice
    episode_counts = dict(
        db.query(EpisodeItem.media_item_id, func.count(EpisodeItem.id))
        .group_by(EpisodeItem.media_item_id)
        .all()
    )

    cfg = get_settings(db)
    indexers = db.query(IndexerConfig).order_by(IndexerConfig.priority.asc(), IndexerConfig.name.asc()).all()
    profiles = db.query(QualityProfile).order_by(QualityProfile.name.asc()).all()
    custom_formats = db.query(CustomFormat).order_by(CustomFormat.name.asc()).all()
    drives = get_drive_stats(db)
    drive_free_map = {d["mount"]: d["free_gb"] for d in drives}
    roots_map = get_media_roots(db)
    root_folders = []
    for m_type, paths in roots_map.items():
        for p in paths:
            p_str = str(p)
            free_gb = drive_free_map.get(p_str)
            exists = free_gb is not None or p.exists()
            if exists and free_gb is None:
                free_gb = round(shutil.disk_usage(p).free / (1024**3), 1)
            root_folders.append({"media_type": m_type, "path": p_str, "exists": exists, "free_gb": free_gb})

    return templates.TemplateResponse("index.html", {
        "request": request,
        "items": items,
        "episode_counts": episode_counts,
        "cfg": cfg,
        "indexers": indexers,
        "profiles": profiles,
        "custom_formats": custom_formats,
        "drives": drives,
        "root_folders": root_folders,
        "active_tab": tab,
        "sort": sort,
        "q": search_q,
        "media_type": media_type,
        "page": page,
        "total_pages": total_pages,
        "total_items": total_items,
        "total_movies": total_movies,
        "total_tv": total_tv,
        "msg": msg,
    })

@app.get("/api/media/{media_id}/episodes")
def get_media_episodes(media_id: int, db: Session = Depends(get_db)):
    eps = (
        db.query(EpisodeItem)
        .filter(EpisodeItem.media_item_id == media_id)
        .order_by(EpisodeItem.season_number.asc(), EpisodeItem.episode_number.asc())
        .all()
    )
    return [
        {
            "season": ep.season_number,
            "episode": ep.episode_number,
            "title": ep.title or "",
            "release": ep.quality or ep.file_path or "",
        }
        for ep in eps
    ]

@app.post("/add")
async def add_media(
    title: str = Form(...),
    year: int = Form(None),
    media_type: str = Form(...),
    quality_profile: str = Form("1080p"),
    db: Session = Depends(get_db)
):
    item = MediaItem(title=title, year=year, media_type=media_type, quality_profile=quality_profile)
    db.add(item)
    db.commit()
    db.refresh(item)

    query = f"{title} {year}" if year else title
    releases = await search_prowlarr(query, media_type, quality_profile, db)
    if releases:
        t_hash = await send_to_qbittorrent(releases[0]["downloadUrl"], item.id, db)
        if t_hash:
            item.torrent_hash = t_hash
            item.status = "downloading"
            db.commit()

    await sync_posters_from_plex(db)
    return RedirectResponse(url="/", status_code=303)



@app.post("/api/media/{item_id}/move")
async def api_move_media_folder(item_id: int, payload: dict, db: Session = Depends(get_db)):
    from fastapi.responses import JSONResponse
    item = db.query(MediaItem).filter_by(id=item_id).first()
    if not item:
        return JSONResponse(status_code=404, content={"ok": False, "detail": "Media item not found"})
    new_root = (payload.get("new_root") or "").strip()
    transfer_files = bool(payload.get("transfer_files", True))
    try:
        res = move_media_folder(item, new_root, transfer_files, db)
        cfg = get_settings(db)
        if cfg.get("plex_notify_import", "true") == "true":
            await trigger_plex_scan(cfg)
        return res
    except Exception as e:
        return JSONResponse(status_code=400, content={"ok": False, "detail": str(e)})

@app.get("/media/{item_id}", response_class=HTMLResponse)
async def media_detail_page(request: Request, item_id: int, db: Session = Depends(get_db)):
    item = db.query(MediaItem).filter_by(id=item_id).first()
    if not item:
        return RedirectResponse(url="/?msg=Media+item+not+found", status_code=303)
    cfg = get_settings(db)
    meta = await fetch_media_metadata(item, cfg)
    episodes = db.query(EpisodeItem).filter_by(media_item_id=item.id).order_by(EpisodeItem.season_number.asc(), EpisodeItem.episode_number.asc()).all()
    roots_map = get_media_roots(db)
    available_roots = [str(p) for p in roots_map.get(item.media_type, []) if str(p)]
    return templates.TemplateResponse("detail.html", {
        "request": request,
        "item": item,
        "meta": meta,
        "episodes": episodes,
        "cfg": cfg,
        "available_roots": available_roots,
    })

@app.get("/api/search/manual/{item_id}")
async def api_manual_search(item_id: int, q: str = "", db: Session = Depends(get_db)):
    item = db.query(MediaItem).filter_by(id=item_id).first()
    if not item:
        return {"item_id": item_id, "error": "Item not found", "releases": []}
    search_q = q.strip() or (f"{item.title} {item.year}" if item.year and item.media_type == "movie" else item.title)
    try:
        releases = await manual_search_prowlarr(search_q, item.media_type, item.quality_profile, db)
        return {"item_id": item.id, "title": item.title, "query": search_q, "releases": releases}
    except Exception as e:
        return {"item_id": item.id, "title": item.title, "query": search_q, "error": f"{type(e).__name__}: {e}", "releases": []}

@app.post("/api/grab/manual")
async def api_manual_grab(payload: dict, db: Session = Depends(get_db)):
    item_id = int(payload.get("item_id", 0))
    dl_url = payload.get("download_url", "").strip()
    item = db.query(MediaItem).filter_by(id=item_id).first()
    if not item or not dl_url:
        raise HTTPException(status_code=400, detail="Missing item_id or download_url")
    thash = await send_to_qbittorrent(dl_url, item.id, db)
    if thash:
        item.torrent_hash = thash
    item.status = "downloading"
    db.commit()
    return {"ok": True, "hash": thash}

@app.post("/search/{item_id}")
async def search_upgrade_media(item_id: int, db: Session = Depends(get_db)):
    item = db.query(MediaItem).filter(MediaItem.id == item_id).first()
    if not item:
        return RedirectResponse(url="/?msg=Item+not+found", status_code=303)
    query = f"{item.title} {item.year}" if item.year else item.title
    releases = await search_prowlarr(query, item.media_type, item.quality_profile, db)
    if releases:
        best = releases[0]
        t_hash = await send_to_qbittorrent(best["downloadUrl"], item.id, db)
        if t_hash:
            item.torrent_hash = t_hash
            item.status = "downloading"
            db.commit()
            return RedirectResponse(url=f"/?msg=Grabbed+release+for+{item.title.replace(' ', '+')}", status_code=303)
    return RedirectResponse(url=f"/?msg=No+matching+releases+found+for+{item.title.replace(' ', '+')}", status_code=303)

@app.post("/delete/{item_id}")
def delete_media(item_id: int, db: Session = Depends(get_db)):
    item = db.query(MediaItem).filter(MediaItem.id == item_id).first()
    if item:
        db.query(EpisodeItem).filter_by(media_item_id=item.id).delete()
        db.delete(item)
        db.commit()
    return RedirectResponse(url="/", status_code=303)

async def _bg_sync_posters():
    db = SessionLocal()
    try:
        await sync_posters_from_plex(db)
    finally:
        db.close()

@app.post("/import-backup")
async def import_backup(background_tasks: BackgroundTasks, backup_file: UploadFile = File(...), db: Session = Depends(get_db)):
    if not backup_file.filename.endswith(".zip"):
        raise HTTPException(status_code=400, detail="Please upload a valid .zip backup file.")
    content = await backup_file.read()
    result = parse_and_import_backup(content, db)
    background_tasks.add_task(_bg_sync_posters)
    summary_txt = (
        f"Imported {result['app']}: {result['imported_media']} titles, "
        f"{result['imported_episodes']} grabbed episodes, "
        f"{result['imported_profiles']} profiles, {result['imported_custom_formats']} custom formats"
    )
    return RedirectResponse(url=f"/?msg={summary_txt.replace(' ', '+')}", status_code=303)

@app.post("/sync-posters")
async def sync_posters_route(db: Session = Depends(get_db)):
    count = await sync_posters_from_plex(db)
    return RedirectResponse(url=f"/?msg=Synced+{count}+posters+from+Plex", status_code=303)

@app.post("/settings")
async def update_settings(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    updates = dict(form)
    section = updates.pop("section", "settings")
    if section == "media":
        updates.setdefault("remove_completed_download", "false")
    if section == "connect":
        updates.setdefault("plex_notify_grab", "false")
        updates.setdefault("plex_notify_import", "false")
        updates.setdefault("jellyfin_notify", "false")
        updates.setdefault("emby_notify", "false")
    save_settings(db, updates)
    return RedirectResponse(url=f"/?tab={section}&msg=Settings+saved", status_code=303)

@app.get("/api/filesystem")
def browse_filesystem(path: str = ""):
    """Lists mounted drives from /proc/mounts and subdirectories for a Sonarr/Radarr-style folder picker."""
    skip_fs = {"proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup", "cgroup2", "pstore", "bpf", "tracefs", "debugfs", "mqueue", "hugetlbfs", "fusectl", "configfs", "overlay", "squashfs", "nsfs"}
    skip_prefixes = ("/proc", "/sys", "/dev", "/run", "/etc", "/var/lib", "/usr", "/boot")
    mounts = []
    seen_mounts = set()
    try:
        with open("/proc/mounts", "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mp, fstype = parts[1], parts[2]
                if fstype in skip_fs or any(mp == p or mp.startswith(p + "/") for p in skip_prefixes):
                    continue
                if mp not in seen_mounts and Path(mp).exists():
                    seen_mounts.add(mp)
                    try:
                        du = shutil.disk_usage(mp)
                        free_gb = round(du.free / (1024**3), 1)
                        total_gb = round(du.total / (1024**3), 1)
                    except Exception:
                        free_gb, total_gb = 0.0, 0.0
                    mounts.append({"path": mp, "fstype": fstype, "free_gb": free_gb, "total_gb": total_gb})
    except Exception:
        pass

    # Include common media roots if they exist even on single-disk setups
    for common in ("/mnt", "/media", "/data"):
        if common not in seen_mounts and Path(common).exists():
            du = shutil.disk_usage(common)
            mounts.append({
                "path": common,
                "fstype": "dir",
                "free_gb": round(du.free / (1024**3), 1),
                "total_gb": round(du.total / (1024**3), 1)
            })

    mounts.sort(key=lambda m: m["path"])

    current_path = path.strip()
    dirs = []
    parent_path = ""
    if current_path:
        p = Path(current_path)
        if p.exists() and p.is_dir():
            parent_path = str(p.parent) if str(p) != "/" else ""
            try:
                for entry in sorted(p.iterdir(), key=lambda x: x.name.lower()):
                    if entry.is_dir() and not entry.name.startswith("."):
                        dirs.append({"name": entry.name, "path": str(entry)})
            except PermissionError:
                pass

    return {
        "current_path": current_path,
        "parent_path": parent_path,
        "mounts": mounts,
        "directories": dirs
    }

@app.post("/settings/roots/add")
async def add_root_folder(media_type: str = Form(...), path: str = Form(...), create_dir: str = Form("false"), db: Session = Depends(get_db)):
    clean_path = path.strip()
    if not clean_path or media_type not in ("movie", "tv", "music"):
        return RedirectResponse(url="/?tab=media&msg=Invalid+root+folder+path", status_code=303)
    p_obj = Path(clean_path)
    if create_dir == "true" and not p_obj.exists():
        try:
            p_obj.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return RedirectResponse(url=f"/?tab=media&msg=Failed+to+create+directory:+{str(e).replace(' ', '+')}", status_code=303)
    roots_map = get_media_roots(db)
    current = [str(p) for p in roots_map.get(media_type, [])]
    if clean_path not in current:
        current.append(clean_path)
    save_settings(db, {f"{media_type}_roots": ",".join(current)})
    return RedirectResponse(url="/?tab=media&msg=Root+folder+added", status_code=303)

@app.post("/settings/roots/delete")
async def delete_root_folder(media_type: str = Form(...), path: str = Form(...), db: Session = Depends(get_db)):
    if media_type not in ("movie", "tv", "music"):
        return RedirectResponse(url="/?tab=media", status_code=303)
    roots_map = get_media_roots(db)
    remaining = [str(p) for p in roots_map.get(media_type, []) if str(p) != path.strip()]
    save_settings(db, {f"{media_type}_roots": ",".join(remaining)})
    return RedirectResponse(url="/?tab=media&msg=Root+folder+removed", status_code=303)

@app.post("/settings/indexers")
async def update_indexers(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    enabled_ids = {int(k.split("_")[1]) for k in form.keys() if k.startswith("enabled_")}
    for idx in db.query(IndexerConfig).all():
        idx.enabled = idx.id in enabled_ids
        prio_val = form.get(f"priority_{idx.id}")
        if prio_val and prio_val.isdigit():
            idx.priority = int(prio_val)
    db.commit()
    return RedirectResponse(url="/?tab=indexers&msg=Indexers+updated", status_code=303)

@app.post("/settings/test/{service}")
async def test_connection(service: str, db: Session = Depends(get_db)):
    cfg = get_settings(db)
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            if service == "prowlarr":
                r = await client.get(f"{cfg['prowlarr_url'].rstrip('/')}/api/v1/system/status", headers={"X-Api-Key": cfg["prowlarr_api_key"]})
                r.raise_for_status()
                return {"ok": True, "detail": f"Connected to Prowlarr v{r.json().get('version', '?')}"}
            elif service == "qbittorrent":
                q_url = cfg['qbit_url'].rstrip('/')
                q_headers = {"Referer": q_url, "Origin": q_url}
                ver_bypass = await client.get(f"{q_url}/api/v2/app/version", headers=q_headers)
                if ver_bypass.status_code == 200:
                    return {"ok": True, "detail": f"Connected to qBittorrent {ver_bypass.text.strip()} (Subnet Auth Bypass)"}
                r = await client.post(f"{q_url}/api/v2/auth/login", data={"username": cfg["qbit_user"], "password": cfg["qbit_pass"]}, headers=q_headers)
                if r.status_code != 200 or r.text.strip() != "Ok.":
                    return {"ok": False, "detail": f"HTTP {r.status_code}: {r.text.strip() or 'Rejected'}"}
                ver = await client.get(f"{q_url}/api/v2/app/version", headers=q_headers)
                return {"ok": True, "detail": f"Connected to qBittorrent {ver.text.strip()}"}
            elif service == "plex":
                r = await client.get(f"{cfg['plex_url'].rstrip('/')}/identity", params={"X-Plex-Token": cfg["plex_token"]}, headers={"Accept": "application/json"})
                r.raise_for_status()
                return {"ok": True, "detail": "Connected to Plex Media Server"}
            elif service == "jellyfin":
                r = await client.get(f"{cfg['jellyfin_url'].rstrip('/')}/System/Info", headers={"X-Emby-Token": cfg["jellyfin_api_key"]})
                r.raise_for_status()
                info = r.json()
                return {"ok": True, "detail": f"Connected to Jellyfin {info.get('Version', '')} ({info.get('ServerName', 'Server')})"}
            elif service == "emby":
                r = await client.get(f"{cfg['emby_url'].rstrip('/')}/System/Info", headers={"X-Emby-Token": cfg["emby_api_key"]})
                r.raise_for_status()
                info = r.json()
                return {"ok": True, "detail": f"Connected to Emby {info.get('Version', '')} ({info.get('ServerName', 'Server')})"}
    except Exception as e:
        return {"ok": False, "detail": str(e)}
    return {"ok": False, "detail": "Unknown service"}

@app.post("/scan-now")
async def run_scan_now():
    await scheduled_tasks()
    return RedirectResponse(url="/?msg=Background+search+and+import+cycle+completed", status_code=303)

# --- SEERR (OVERSEERR / JELLYSEERR) COMPATIBILITY API ---

@app.get("/api/v3/system/status", dependencies=[Depends(verify_api_key)])
def arr_system_status():
    return {"version": "4.0.0.0", "appName": "OmniArr"}

@app.get("/api/v3/qualityProfile", dependencies=[Depends(verify_api_key)])
@app.get("/api/v3/qualityprofile", dependencies=[Depends(verify_api_key)])
def arr_quality_profiles(db: Session = Depends(get_db)):
    imported = db.query(QualityProfile).all()
    if imported:
        return [{"id": p.id, "name": p.name} for p in imported]
    return [
        {"id": 1, "name": "1080p"},
        {"id": 2, "name": "2160p"},
        {"id": 3, "name": "720p"},
        {"id": 4, "name": "Any"},
    ]

@app.get("/api/v3/rootFolder", dependencies=[Depends(verify_api_key)])
@app.get("/api/v3/rootfolder", dependencies=[Depends(verify_api_key)])
def arr_root_folders(db: Session = Depends(get_db)):
    roots_map = get_media_roots(db)
    out = []
    idx = 1
    for m_type in ("movie", "tv", "music"):
        for p in roots_map.get(m_type, []):
            free_space = shutil.disk_usage(p).free if p.exists() else 0
            out.append({"id": idx, "path": str(p), "accessible": p.exists(), "freeSpace": free_space})
            idx += 1
    return out

@app.get("/api/v3/tag", dependencies=[Depends(verify_api_key)])
def v3_tags():
    return []

@app.get("/api/v3/languageProfile", dependencies=[Depends(verify_api_key)])
@app.get("/api/v3/languageprofile", dependencies=[Depends(verify_api_key)])
def v3_language_profiles():
    return [{"id": 1, "name": "English", "upgradeAllowed": True, "cutoff": {"id": 1, "name": "English"}, "languages": []}]

@app.get("/api/v3/movie", dependencies=[Depends(verify_api_key)])
def seerr_get_movies(tmdbId: int = None, db: Session = Depends(get_db)):
    q = db.query(MediaItem).filter(MediaItem.media_type == "movie")
    if tmdbId:
        q = q.filter(MediaItem.external_id == str(tmdbId))
    return [{"id": m.id, "title": m.title, "tmdbId": int(m.external_id or 0), "hasFile": m.status == "available"} for m in q.all()]

@app.post("/api/v3/movie", dependencies=[Depends(verify_api_key)])
async def seerr_add_movie(payload: dict, db: Session = Depends(get_db)):
    cfg = get_settings(db)
    item = MediaItem(
        title=payload.get("title"),
        year=payload.get("year"),
        media_type="movie",
        external_id=str(payload.get("tmdbId", "")),
        quality_profile=cfg.get("default_movie_quality", "1080p")
    )
    db.add(item)
    db.commit()
    return {"id": item.id, "title": item.title, "tmdbId": payload.get("tmdbId")}

@app.get("/api/v3/series", dependencies=[Depends(verify_api_key)])
def seerr_get_series(tvdbId: int = None, db: Session = Depends(get_db)):
    q = db.query(MediaItem).filter(MediaItem.media_type == "tv")
    if tvdbId:
        q = q.filter(MediaItem.external_id == str(tvdbId))
    return [{"id": s.id, "title": s.title, "tvdbId": int(s.external_id or 0)} for s in q.all()]

@app.post("/api/v3/series", dependencies=[Depends(verify_api_key)])
async def seerr_add_series(payload: dict, db: Session = Depends(get_db)):
    cfg = get_settings(db)
    item = MediaItem(
        title=payload.get("title"),
        year=payload.get("year"),
        media_type="tv",
        external_id=str(payload.get("tvdbId", "")),
        quality_profile=cfg.get("default_tv_quality", "1080p")
    )
    db.add(item)
    db.commit()
    return {"id": item.id, "title": item.title, "tvdbId": payload.get("tvdbId")}
