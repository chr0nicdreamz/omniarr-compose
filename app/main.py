import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, Request, Form, HTTPException, Header
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .database import engine, Base, get_db, SessionLocal
from .models import MediaItem
from .services import search_prowlarr, send_to_qbittorrent, check_downloads_and_import

Base.metadata.create_all(bind=engine)
API_KEY = os.getenv("OMNIARR_API_KEY", "change_me_to_a_secure_key")
templates = Jinja2Templates(directory="app/templates")

async def scheduled_tasks():
    db = SessionLocal()
    try:
        await check_downloads_and_import(db)
        # Auto-search wanted items
        wanted = db.query(MediaItem).filter(MediaItem.status == "wanted", MediaItem.monitored == True).all()
        for item in wanted:
            query = f"{item.title} {item.year}" if item.year else item.title
            releases = await search_prowlarr(query, item.media_type, item.quality_profile)
            if releases:
                best = releases[0]
                t_hash = await send_to_qbittorrent(best["downloadUrl"], item.id)
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

def verify_api_key(x_api_key: str = Header(None), apikey: str = None):
    key = x_api_key or apikey
    if key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API Key")

# --- WEB UI ROUTES ---

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    items = db.query(MediaItem).order_by(MediaItem.added_at.desc()).all()
    return templates.TemplateResponse("index.html", {"request": request, "items": items})

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

    # Trigger immediate search
    query = f"{title} {year}" if year else title
    releases = await search_prowlarr(query, media_type, quality_profile)
    if releases:
        t_hash = await send_to_qbittorrent(releases[0]["downloadUrl"], item.id)
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

# --- SEERR (OVERSEERR / JELLYSEERR) COMPATIBILITY API ---
# Point Seerr's Radarr & Sonarr settings to http://omniarr:8585 with OMNIARR_API_KEY

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
        {"id": 1, "path": "/data/media/movies"},
        {"id": 2, "path": "/data/media/tv"},
    ]

@app.get("/api/v3/movie", dependencies=[Depends(verify_api_key)])
def seerr_get_movies(tmdbId: int = None, db: Session = Depends(get_db)):
    q = db.query(MediaItem).filter(MediaItem.media_type == "movie")
    if tmdbId:
        q = q.filter(MediaItem.external_id == str(tmdbId))
    return [{"id": m.id, "title": m.title, "tmdbId": int(m.external_id or 0), "hasFile": m.status == "available"} for m in q.all()]

@app.post("/api/v3/movie", dependencies=[Depends(verify_api_key)])
async def seerr_add_movie(payload: dict, db: Session = Depends(get_db)):
    item = MediaItem(
        title=payload.get("title"),
        year=payload.get("year"),
        media_type="movie",
        external_id=str(payload.get("tmdbId", "")),
        quality_profile="1080p"
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
    item = MediaItem(
        title=payload.get("title"),
        year=payload.get("year"),
        media_type="tv",
        external_id=str(payload.get("tvdbId", "")),
        quality_profile="1080p"
    )
    db.add(item)
    db.commit()
    return {"id": item.id, "title": item.title, "tvdbId": payload.get("tvdbId")}
