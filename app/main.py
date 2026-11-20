import os
import httpx
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, Request, Form, HTTPException, Header, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .database import engine, Base, get_db, SessionLocal
from .models import MediaItem, IndexerConfig
from .services import (
    search_prowlarr, send_to_qbittorrent, check_downloads_and_import,
    get_settings, save_settings, get_drive_stats, trigger_plex_scan
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
def dashboard(request: Request, tab: str = "library", msg: str = "", db: Session = Depends(get_db)):
    items = db.query(MediaItem).order_by(MediaItem.added_at.desc()).all()
    cfg = get_settings(db)
    indexers = db.query(IndexerConfig).order_by(IndexerConfig.priority.asc(), IndexerConfig.name.asc()).all()
    drives = get_drive_stats()
    return templates.TemplateResponse("index.html", {
        "request": request,
        "items": items,
        "cfg": cfg,
        "indexers": indexers,
        "drives": drives,
        "active_tab": tab,
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

    return RedirectResponse(url="/", status_code=303)

@app.post("/delete/{item_id}")
def delete_media(item_id: int, db: Session = Depends(get_db)):
    item = db.query(MediaItem).filter(MediaItem.id == item_id).first()
    if item:
        db.delete(item)
        db.commit()
    return RedirectResponse(url="/", status_code=303)

@app.post("/import-backup")
async def import_backup(backup_file: UploadFile = File(...), db: Session = Depends(get_db)):
    if not backup_file.filename.endswith(".zip"):
        raise HTTPException(status_code=400, detail="Please upload a valid .zip backup file.")
    content = await backup_file.read()
    result = parse_and_import_backup(content, db)
    return RedirectResponse(url=f"/?msg=Imported+{result['app']}+({result['imported_media']}+items)", status_code=303)

@app.post("/settings")
async def update_settings(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    updates = dict(form)
    section = updates.pop("section", "settings")
    # Handle unchecked HTML checkboxes
    if section == "media":
        updates.setdefault("remove_completed_download", "false")
    if section == "connect":
        updates.setdefault("plex_notify_grab", "false")
        updates.setdefault("plex_notify_import", "false")
    save_settings(db, updates)
    return RedirectResponse(url=f"/?tab={section}&msg=Settings+saved", status_code=303)

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
                r = await client.post(f"{cfg['qbit_url'].rstrip('/')}/api/v2/auth/login", data={"username": cfg["qbit_user"], "password": cfg["qbit_pass"]})
                if r.text.strip() != "Ok.":
                    return {"ok": False, "detail": f"Login failed: {r.text}"}
                ver = await client.get(f"{cfg['qbit_url'].rstrip('/')}/api/v2/app/version")
                return {"ok": True, "detail": f"Connected to qBittorrent {ver.text.strip()}"}
            elif service == "plex":
                r = await client.get(f"{cfg['plex_url'].rstrip('/')}/", params={"X-Plex-Token": cfg["plex_token"]}, headers={"Accept": "application/json"})
                r.raise_for_status()
                name = r.json().get("MediaContainer", {}).get("friendlyName", "Plex")
                return {"ok": True, "detail": f"Connected to {name}"}
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
def arr_quality_profiles():
    return [
        {"id": 1, "name": "1080p"},
        {"id": 2, "name": "2160p"},
        {"id": 3, "name": "720p"},
        {"id": 4, "name": "Any"},
    ]

@app.get("/api/v3/rootfolder", dependencies=[Depends(verify_api_key)])
def arr_root_folders():
    return [
        {"id": 1, "path": "/mnt/plex_g/Movies"},
        {"id": 2, "path": "/mnt/plex_g/Tv Shows"},
    ]

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
