from pathlib import Path
import shutil
import os
import json
import httpx
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, Request, Form, HTTPException, Header, UploadFile, File, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .database import engine, Base, get_db, SessionLocal
from .models import MediaItem, EpisodeItem, IndexerConfig, QualityProfile, CustomFormat
from .services import (
    search_prowlarr, send_to_qbittorrent, check_downloads_and_import,
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
                t_hash = await send_to_qbittorrent(best["downloadUrl"], item.id, db)
                if t_hash:
                    item.torrent_hash = t_hash
                    item.status = "downloading"
                    db.commit()
    finally:
        db.close()

@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = AsyncIOScheduler()
    scheduler.add_job(scheduled_tasks, "interval", minutes=2)
    scheduler.start()
    yield
    scheduler.shutdown()

app = FastAPI(title="OmniArr", lifespan=lifespan)

def verify_api_key(x_api_key: str = Header(None), apikey: str = None, db: Session = Depends(get_db)):
    cfg = get_settings(db)
    key = x_api_key or apikey
    if key != cfg["omniarr_api_key"]:
        raise HTTPException(status_code=401, detail="Invalid API Key")

# --- WEB UI ROUTES ---

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, tab: str = "library", sort: str = "added_desc", msg: str = "", db: Session = Depends(get_db)):
    q = db.query(MediaItem)
    if sort == "added_asc":
        q = q.order_by(MediaItem.added_at.asc())
    elif sort == "title_asc":
        q = q.order_by(MediaItem.title.asc())
    elif sort == "title_desc":
        q = q.order_by(MediaItem.title.desc())
    else:
        q = q.order_by(MediaItem.added_at.desc())
    items = q.all()
    episodes_by_media = {}
    for ep in db.query(EpisodeItem).order_by(EpisodeItem.season_number.asc(), EpisodeItem.episode_number.asc()).all():
        episodes_by_media.setdefault(ep.media_item_id, []).append(ep)

    cfg = get_settings(db)
    indexers = db.query(IndexerConfig).order_by(IndexerConfig.priority.asc(), IndexerConfig.name.asc()).all()
    profiles = db.query(QualityProfile).order_by(QualityProfile.name.asc()).all()
    custom_formats = db.query(CustomFormat).order_by(CustomFormat.name.asc()).all()
    drives = get_drive_stats(db)
    roots_map = get_media_roots(db)
    root_folders = []
    for m_type, paths in roots_map.items():
        for p in paths:
            exists = p.exists()
            free_gb = round(shutil.disk_usage(p).free / (1024**3), 1) if exists else None
            root_folders.append({"media_type": m_type, "path": str(p), "exists": exists, "free_gb": free_gb})

    return templates.TemplateResponse("index.html", {
        "request": request,
        "items": items,
        "episodes_by_media": episodes_by_media,
        "cfg": cfg,
        "indexers": indexers,
        "profiles": profiles,
        "custom_formats": custom_formats,
        "drives": drives,
        "root_folders": root_folders,
        "active_tab": tab,
        "sort": sort,
        "msg": msg,
    })

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
