import os, re, shutil
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
from .models import MediaItem, EpisodeItem, IndexerConfig, BlacklistedRelease, HistoryEvent, QualityProfile, CustomFormat
from .services import (
    sync_calendar_schedules,
    hunt_aired_calendar_items,
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
        await hunt_aired_calendar_items(db)
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


async def calendar_sync_job():
    db = SessionLocal()
    try:
        await sync_calendar_schedules(db)
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
    scheduler.add_job(calendar_sync_job, "interval", hours=6)
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
    check_downloads_and_import, sync_posters_from_plex,
    get_drive_storage_stats,
    move_media_item_folder,
    get_tv_seasons_and_episodes,
    _find_kometa_asset_file,
    _find_local_poster_file,
    _search_plex_poster,
    _fetch_public_poster,
    get_poster_candidates, fetch_media_metadata, move_media_folder, fetch_tautulli_activity, fetch_tautulli_watch_stats, run_library_health_audit, scan_franchise_collections, scan_existing_library, clean_media_title, _find_local_poster_file, build_clean_filename, record_history, send_notification
)

app = FastAPI(title="OmniArr", lifespan=lifespan)

def verify_api_key(x_api_key: str = Header(None), apikey: str = None, db: Session = Depends(get_db)):
    cfg = get_settings(db)
    key = x_api_key or apikey
    if key != cfg["omniarr_api_key"]:
        raise HTTPException(status_code=401, detail="Invalid API Key")




import asyncio as _asyncio

BG_TASKS: dict[str, dict] = {}

def _update_bg_task(key: str, label: str, state: str = "running", detail: str = ""):
    BG_TASKS[key] = {
        "key": key,
        "label": label,
        "state": state,  # running | done | error
        "detail": detail,
    }

@app.get("/api/tasks/status")
def api_get_bg_tasks():
    return {"tasks": list(BG_TASKS.values())}

async def _bg_sync_all_posters(rematch_all: bool = False):
    label = "Poster Rematch" if rematch_all else "Poster Sync"
    _update_bg_task("posters", label, "running", "Preparing poster sweep...")
    try:
        db = SessionLocal()
        try:
            if rematch_all:
                db.query(MediaItem).update({MediaItem.poster_url: None})
                db.commit()
            cfg = get_settings(db)
            kometa_dir = cfg.get("kometa_assets_dir") or ""
            missing = db.query(MediaItem).filter((MediaItem.poster_url == None) | (MediaItem.poster_url == "")).all()
            total = len(missing)
            if total == 0:
                _update_bg_task("posters", label, "done", "All posters already matched")
                return

            sem = _asyncio.Semaphore(12)
            matched = 0
            checked = 0

            async with httpx.AsyncClient(verify=False, timeout=8.0) as client:
                async def resolve(item):
                    nonlocal matched, checked
                    async with sem:
                        try:
                            if _find_kometa_asset_file(item.title, item.year, kometa_dir):
                                item.poster_url = f"/api/media/{item.id}/kometa-poster"
                                matched += 1
                                return
                            if _find_local_poster_file(item.file_path):
                                item.poster_url = f"/api/media/{item.id}/local-poster"
                                matched += 1
                                return
                            clean_q = re.sub(r"[^a-zA-Z0-9\s]", " ", item.title or "").strip()
                            art = await _search_plex_poster(clean_q, item.year, cfg, client)
                            if not art:
                                art = await _fetch_public_poster(clean_q, item.year, item.media_type, client)
                            if art:
                                item.poster_url = art
                                matched += 1
                        finally:
                            checked += 1
                            if checked % 50 == 0 or checked == total:
                                _update_bg_task("posters", label, "running", f"{matched} matched ({checked}/{total} checked)...")

                await _asyncio.gather(*(resolve(it) for it in missing))
            db.commit()
            _update_bg_task("posters", label, "done", f"Synced {matched}/{total} posters")
        finally:
            db.close()
    except Exception as e:
        _update_bg_task("posters", label, "error", str(e)[:80])


@app.post("/api/posters/rematch-all")
async def api_rematch_all_posters():
    if BG_TASKS.get("posters", {}).get("state") == "running":
        return {"ok": False, "error": "A poster sync/rematch task is already running."}
    _asyncio.create_task(_bg_sync_all_posters(rematch_all=True))
    return {"ok": True}

@app.get("/api/media/{item_id}/kometa-poster")
def api_serve_kometa_poster(item_id: int, db: Session = Depends(get_db)):
    item = db.query(MediaItem).filter(MediaItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Not found")
    cfg = get_settings(db)
    art_path = _find_kometa_asset_file(item.title, item.year, cfg.get("kometa_assets_dir") or "")
    if not art_path or not os.path.isfile(art_path):
        raise HTTPException(status_code=404, detail="Kometa asset not found")
    return FileResponse(art_path)

@app.get("/api/media/{item_id}/posters")
async def api_media_poster_candidates(item_id: int, db: Session = Depends(get_db)):
    item = db.query(MediaItem).filter(MediaItem.id == item_id).first()
    if not item:
        return {"candidates": []}
    cands = await get_poster_candidates(db, item)
    return {"candidates": cands}

@app.post("/api/media/{item_id}/poster")
def api_set_media_poster(item_id: int, payload: dict, db: Session = Depends(get_db)):
    item = db.query(MediaItem).filter(MediaItem.id == item_id).first()
    if not item:
        return {"ok": False, "error": "Item not found"}
    new_url = (payload.get("poster_url") or "").strip()
    item.poster_url = new_url or None
    db.add(HistoryLog(event_type="PosterUpdated", title=item.title, details=f"Updated artwork -> {new_url[:80]}"))
    db.commit()
    return {"ok": True, "poster_url": item.poster_url}

@app.post("/api/posters/sync-bg")
async def api_start_bg_poster_sync():
    if BG_TASKS.get("posters", {}).get("state") == "running":
        return {"ok": True, "already_running": True}
    _asyncio.create_task(_bg_sync_all_posters())
    return {"ok": True}

async def _bg_scan_disk():
    _update_bg_task("disk_scan", "Disk Scan", "running", "Scanning root folders on disk...")
    db = SessionLocal()
    try:
        res = scan_existing_library(db)
        added = res.get("added", 0)
        _update_bg_task("disk_scan", "Disk Scan", "done", f"Added {added} new title(s)")
        if added > 0:
            _asyncio.create_task(_bg_sync_all_posters())
    except Exception as e:
        _update_bg_task("disk_scan", "Disk Scan", "error", str(e)[:80])
    finally:
        db.close()

@app.post("/api/library/scan-disk-bg")
async def api_start_bg_disk_scan():
    if BG_TASKS.get("disk_scan", {}).get("state") == "running":
        return {"ok": True, "already_running": True}
    _asyncio.create_task(_bg_scan_disk())
    return {"ok": True}

async def _bg_normalize_titles():
    _update_bg_task("normalize", "Title Cleanup", "running", "Normalizing raw filenames...")
    db = SessionLocal()
    try:
        items = db.query(MediaItem).all()
        cleaned = 0
        for it in items:
            nt, ny = clean_media_title(it.title)
            if nt != it.title or (ny and not it.year):
                it.title = nt
                if ny and not it.year:
                    it.year = ny
                cleaned += 1
        db.commit()
        _update_bg_task("normalize", "Title Cleanup", "done", f"Cleaned {cleaned} title(s)")
        _asyncio.create_task(_bg_sync_all_posters())
    except Exception as e:
        _update_bg_task("normalize", "Title Cleanup", "error", str(e)[:80])
    finally:
        db.close()

@app.post("/api/library/normalize-titles-bg")
async def api_start_bg_normalize():
    if BG_TASKS.get("normalize", {}).get("state") == "running":
        return {"ok": True, "already_running": True}
    _asyncio.create_task(_bg_normalize_titles())
    return {"ok": True}


@app.get("/api/storage/drives")
def api_storage_drives(db: Session = Depends(get_db)):
    return {"drives": get_drive_storage_stats(db)}

async def _bg_bulk_move(item_ids: list[int], target_root: str):
    total = len(item_ids)
    moved = 0
    errors = 0
    _update_bg_task("bulk_move", "Drive Move", "running", f"0/{total} moved to {target_root}...")
    for idx, mid in enumerate(item_ids, start=1):
        db = SessionLocal()
        try:
            ok, msg = await _asyncio.to_thread(move_media_item_folder, db, mid, target_root)
            if ok:
                moved += 1
            else:
                errors += 1
        except Exception:
            errors += 1
        finally:
            db.close()
        _update_bg_task("bulk_move", "Drive Move", "running", f"{moved}/{total} moved ({errors} skipped)...")
    state = "done" if errors == 0 else "error"
    _update_bg_task("bulk_move", "Drive Move", state, f"Moved {moved}/{total} to {target_root}")

@app.post("/api/library/bulk-move")
async def api_bulk_move_items(payload: dict):
    item_ids = [int(x) for x in (payload.get("item_ids") or [])]
    target_root = (payload.get("target_root") or "").strip()
    if not item_ids or not target_root:
        return {"ok": False, "error": "Select at least one title and a destination drive."}
    if BG_TASKS.get("bulk_move", {}).get("state") == "running":
        return {"ok": False, "error": "A bulk drive migration is already running."}
    _asyncio.create_task(_bg_bulk_move(item_ids, target_root))
    return {"ok": True, "queued": len(item_ids)}

@app.get("/api/media/{item_id}/seasons")
async def api_media_seasons(item_id: int, db: Session = Depends(get_db)):
    item = db.query(MediaItem).filter(MediaItem.id == item_id).first()
    if not item:
        return {"seasons": []}
    seasons = await get_tv_seasons_and_episodes(item)
    return {"seasons": seasons}

@app.get("/api/history")
def api_get_history(db: Session = Depends(get_db)):
    events = db.query(HistoryEvent).order_by(HistoryEvent.id.desc()).limit(100).all()
    return {
        "events": [
            {
                "id": e.id,
                "type": e.event_type,
                "title": e.title,
                "detail": e.detail,
                "media_id": e.media_item_id,
                "date": e.created_at.strftime("%Y-%m-%d %H:%M:%S") if e.created_at else "",
            }
            for e in events
        ]
    }

@app.get("/api/system/logs")
def api_get_system_logs(lines: int = 120):
    import subprocess
    try:
        out = subprocess.check_output(["journalctl", "-u", "omniarr", "-n", str(min(lines, 300)), "--no-pager"], text=True, stderr=subprocess.STDOUT)
        return {"logs": out}
    except Exception as e:
        return {"logs": f"Unable to read journalctl logs: {e}"}


@app.post("/api/library/normalize-titles")
async def api_normalize_library_titles(db: Session = Depends(get_db)):
    items = db.query(MediaItem).all()
    cleaned = 0
    for it in items:
        new_title, new_year = clean_media_title(it.title)
        if new_title != it.title or (new_year and not it.year):
            it.title = new_title
            if new_year and not it.year:
                it.year = new_year
            cleaned += 1
    db.commit()
    posters, _ = await sync_posters_from_plex(db)
    return {"ok": True, "cleaned": cleaned, "posters_matched": posters}


@app.get("/api/media/{item_id}/local-poster")
def api_serve_local_poster(item_id: int, db: Session = Depends(get_db)):
    from fastapi.responses import FileResponse
    item = db.query(MediaItem).filter_by(id=item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    art = _find_local_poster_file(item.file_path)
    if not art:
        raise HTTPException(status_code=404, detail="No local poster found")
    return FileResponse(str(art))

@app.post("/api/media/{item_id}/edit")
async def api_edit_media_item(item_id: int, payload: dict, db: Session = Depends(get_db)):
    item = db.query(MediaItem).filter_by(id=item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Media item not found")
    if payload.get("title"):
        item.title = str(payload["title"]).strip()
    if "year" in payload:
        yr = str(payload["year"] or "").strip()
        item.year = int(yr) if yr.isdigit() else None
    if payload.get("quality_profile"):
        item.quality_profile = str(payload["quality_profile"]).strip()
    db.commit()
    await sync_posters_from_plex(db)
    return {"ok": True}

@app.post("/api/library/scan-disk")
async def api_scan_library_disk(db: Session = Depends(get_db)):
    res = scan_existing_library(db)
    if res["added"] > 0:
        await sync_posters_from_plex(db)
    return res

@app.post("/api/notifications/test")
async def api_test_notifications(db: Session = Depends(get_db)):
    cfg = get_settings(db)
    await send_notification("OmniArr Test Alert", "Notifications are configured and working properly!", cfg)
    return {"ok": True}

@app.get("/api/activity")
async def api_get_activity(db: Session = Depends(get_db)):
    cfg = get_settings(db)
    qbit_url = cfg["qbit_url"].rstrip("/")
    q_hdr = {"Referer": qbit_url, "Origin": qbit_url}
    items_by_hash = {i.torrent_hash: i for i in db.query(MediaItem).filter(MediaItem.torrent_hash != None).all()}
    try:
        async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
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

    async with httpx.AsyncClient(verify=False, timeout=15.0) as client:
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
        transfer_file(content_path, target_base / build_clean_filename(item, content_path.name, cfg), mode)
        imported_files = 1
    elif content_path.is_dir():
        for root, _, files in os.walk(content_path):
            for f in files:
                if f.lower().endswith((".mkv", ".mp4", ".avi", ".m4v", ".flac", ".mp3", ".m4a")) and "sample" not in f.lower():
                    transfer_file(Path(root) / f, target_base / build_clean_filename(item, f, cfg), mode)
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
            async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
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
        async with httpx.AsyncClient(verify=False, timeout=4.0) as client:
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


async def _bg_sync_calendar():
    db = SessionLocal()
    try:
        _update_bg_task("calendar_sync", "Calendar Air-Date Sync", "running", "Fetching TVMaze & TMDB release schedules...")
        res = await sync_calendar_schedules(db)
        _update_bg_task(
            "calendar_sync",
            "Calendar Air-Date Sync",
            "done",
            f"Checked {res.get('shows_checked', 0)} shows • Updated {res.get('updated_episodes', 0)} eps, {res.get('updated_movies', 0)} movies",
        )
    except Exception as e:
        _update_bg_task("calendar_sync", "Calendar Air-Date Sync", "error", str(e))
    finally:
        db.close()

@app.post("/api/calendar/sync")
async def api_sync_calendar():
    _asyncio.create_task(_bg_sync_calendar())
    return {"status": "started"}

@app.post("/api/calendar/hunt-ep/{ep_id}")
async def api_hunt_calendar_episode(ep_id: int, db: Session = Depends(get_db)):
    ep = db.query(EpisodeItem).filter(EpisodeItem.id == ep_id).first()
    if not ep:
        raise HTTPException(status_code=404, detail="Episode not found")
    show = db.query(MediaItem).filter(MediaItem.id == ep.media_item_id).first()
    if not show:
        raise HTTPException(status_code=404, detail="Show not found")
    tag = f"S{ep.season_number:02d}E{ep.episode_number:02d}"
    releases = await search_prowlarr(f"{show.title} {tag}", "tv", show.quality_profile, db)
    if not releases:
        return {"status": "no_results", "query": f"{show.title} {tag}"}
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
            detail=f"Manual calendar hunt grabbed: {best.get('title', tag)}",
        ))
        db.commit()
        return {"status": "grabbed", "release": best.get("title", tag)}
    return {"status": "failed"}


@app.get("/favicon.svg")
@app.get("/favicon.ico")
def serve_omniarr_favicon():
    from fastapi.responses import Response
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
      <defs>
        <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stop-color="#6366f1"/>
          <stop offset="100%" stop-color="#38bdf8"/>
        </linearGradient>
      </defs>
      <rect width="64" height="64" rx="16" fill="#0f172a"/>
      <rect x="4" y="4" width="56" height="56" rx="13" fill="none" stroke="url(#g)" stroke-width="3.5"/>
      <circle cx="32" cy="32" r="15" fill="none" stroke="url(#g)" stroke-width="4.5"/>
      <polygon points="29,24 41,32 29,40" fill="#e0e7ff"/>
    </svg>"""
    return Response(content=svg, media_type="image/svg+xml")


@app.get("/api/activity/history-30d")
def api_activity_history_30d(db: Session = Depends(get_db)):
    from datetime import datetime as _dt, timedelta as _td
    cutoff = _dt.utcnow() - _td(days=30)

    events = (
        db.query(HistoryEvent)
        .filter(HistoryEvent.created_at >= cutoff)
        .order_by(HistoryEvent.created_at.desc(), HistoryEvent.id.desc())
        .limit(400)
        .all()
    )
    blacklisted = (
        db.query(BlacklistedRelease)
        .filter(BlacklistedRelease.created_at >= cutoff)
        .order_by(BlacklistedRelease.created_at.desc(), BlacklistedRelease.id.desc())
        .limit(200)
        .all()
    )

    items = []
    for ev in events:
        etype = (ev.event_type or "info").lower()
        cat = "searched" if etype == "search" else ("downloaded" if etype in ("grab", "import", "download") else "other")
        items.append({
            "id": f"ev-{ev.id}",
            "raw_id": ev.id,
            "category": cat,
            "event_type": etype,
            "media_item_id": ev.media_item_id,
            "title": ev.title or "",
            "detail": ev.detail or "",
            "created_at": ev.created_at.strftime("%Y-%m-%d %H:%M") if ev.created_at else "",
            "ts": ev.created_at.timestamp() if ev.created_at else 0,
        })

    for bl in blacklisted:
        items.append({
            "id": f"bl-{bl.id}",
            "raw_id": bl.id,
            "category": "blacklisted",
            "event_type": "blacklist",
            "media_item_id": bl.media_item_id,
            "title": bl.release_title or bl.torrent_hash or "Blacklisted Release",
            "detail": bl.reason or "Blacklisted",
            "created_at": bl.created_at.strftime("%Y-%m-%d %H:%M") if bl.created_at else "",
            "ts": bl.created_at.timestamp() if bl.created_at else 0,
        })

    items.sort(key=lambda x: x["ts"], reverse=True)
    counts = {
        "all": len(items),
        "searched": sum(1 for x in items if x["category"] == "searched"),
        "downloaded": sum(1 for x in items if x["category"] == "downloaded"),
        "blacklisted": sum(1 for x in items if x["category"] == "blacklisted"),
    }
    return {"counts": counts, "items": items}

@app.post("/api/activity/unblacklist/{bl_id}")
def api_unblacklist_release(bl_id: int, db: Session = Depends(get_db)):
    row = db.query(BlacklistedRelease).filter(BlacklistedRelease.id == bl_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Blacklist entry not found")
    db.delete(row)
    db.commit()
    return {"status": "removed"}

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

    is_overview = (tab == "library" and not search_q and media_type not in ("movie", "tv"))

    # Latest 10 added movies
    recent_movies = (
        db.query(MediaItem)
        .filter(MediaItem.media_type == "movie")
        .order_by(MediaItem.added_at.desc(), MediaItem.id.desc())
        .limit(10)
        .all()
    )

    # Latest 10 updated TV shows (ordered by latest EpisodeItem id/air activity, falling back to MediaItem.added_at/id)
    latest_ep_subq = (
        db.query(EpisodeItem.media_item_id, func.max(EpisodeItem.id).label("max_ep_id"))
        .group_by(EpisodeItem.media_item_id)
        .subquery()
    )
    recent_tv = (
        db.query(MediaItem)
        .outerjoin(latest_ep_subq, MediaItem.id == latest_ep_subq.c.media_item_id)
        .filter(MediaItem.media_type == "tv")
        .order_by(func.coalesce(latest_ep_subq.c.max_ep_id, 0).desc(), MediaItem.added_at.desc(), MediaItem.id.desc())
        .limit(10)
        .all()
    )

    if media_type == "movie":
        active_q = movie_q
        active_total = total_movies
    elif media_type == "tv":
        active_q = tv_q
        active_total = total_tv
    else:
        active_q = base_q
        active_total = total_items

    total_pages = max(1, (max(active_total, 1) + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    offset = (page - 1) * per_page

    if is_overview:
        items = recent_movies + recent_tv
    else:
        items = active_q.order_by(order_col).offset(offset).limit(per_page).all()
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


    import calendar as _pycal
    from datetime import datetime as _dt, timedelta as _td
    today_dt = _dt.now().date()
    today_str = today_dt.strftime("%Y-%m-%d")
    cal_month_param = (request.query_params.get("cal_month") or "").strip()
    try:
        cy, cm = map(int, cal_month_param.split("-"))
        first_of_month = today_dt.replace(year=cy, month=cm, day=1)
    except Exception:
        first_of_month = today_dt.replace(day=1)

    # Sunday-start calendar grid boundaries
    start_offset = (first_of_month.weekday() + 1) % 7
    grid_start = first_of_month - _td(days=start_offset)
    days_in_month = _pycal.monthrange(first_of_month.year, first_of_month.month)[1]
    last_of_month = first_of_month.replace(day=days_in_month)
    end_offset = 6 - ((last_of_month.weekday() + 1) % 7)
    grid_end = last_of_month + _td(days=end_offset)

    prev_month_str = (first_of_month - _td(days=1)).strftime("%Y-%m")
    next_month_str = (last_of_month + _td(days=1)).strftime("%Y-%m")
    cal_month_label = first_of_month.strftime("%B %Y")
    cal_month_str = first_of_month.strftime("%Y-%m")

    start_str = grid_start.strftime("%Y-%m-%d")
    end_str = grid_end.strftime("%Y-%m-%d")

    cal_eps = (
        db.query(EpisodeItem, MediaItem)
        .join(MediaItem, EpisodeItem.media_item_id == MediaItem.id)
        .filter(EpisodeItem.air_date >= start_str, EpisodeItem.air_date <= end_str)
        .order_by(EpisodeItem.air_date.asc(), MediaItem.title.asc())
        .all()
    )
    cal_movies = (
        db.query(MediaItem)
        .filter(
            MediaItem.media_type == "movie",
            MediaItem.release_date >= start_str,
            MediaItem.release_date <= end_str,
        )
        .order_by(MediaItem.release_date.asc(), MediaItem.title.asc())
        .all()
    )

    calendar_entries = []
    for ep, show in cal_eps:
        calendar_entries.append({
            "kind": "tv",
            "id": ep.id,
            "media_id": show.id,
            "date": ep.air_date,
            "title": show.title,
            "subtitle": f"S{ep.season_number:02d}E{ep.episode_number:02d}" + (f" • {ep.title}" if ep.title else ""),
            "has_file": bool(ep.has_file),
            "is_past_or_today": ep.air_date <= today_str,
            "is_today": ep.air_date == today_str,
            "poster_url": show.poster_url,
            "quality_profile": show.quality_profile,
        })
    for mov in cal_movies:
        calendar_entries.append({
            "kind": "movie",
            "id": mov.id,
            "media_id": mov.id,
            "date": mov.release_date,
            "title": mov.title,
            "subtitle": f"Movie Release ({mov.year or ''})".strip(),
            "has_file": (mov.status == "available"),
            "is_past_or_today": mov.release_date <= today_str,
            "is_today": mov.release_date == today_str,
            "poster_url": mov.poster_url,
            "quality_profile": mov.quality_profile,
        })
    calendar_entries.sort(key=lambda x: (x["date"], x["title"]))
    by_date = {}
    for _ce in calendar_entries:
        by_date.setdefault(_ce["date"], []).append(_ce)

    calendar_weeks = []
    cur_d = grid_start
    while cur_d <= grid_end:
        week_cells = []
        for _ in range(7):
            d_iso = cur_d.strftime("%Y-%m-%d")
            week_cells.append({
                "iso": d_iso,
                "day": cur_d.day,
                "in_month": (cur_d.month == first_of_month.month),
                "is_today": (d_iso == today_str),
                "entries": by_date.get(d_iso, []),
            })
            cur_d += _td(days=1)
        calendar_weeks.append(week_cells)

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
        "active_tab": ("settings" if tab in ("media", "profiles", "indexers", "clients", "connect", "settings") else tab),
        "settings_section": (tab if tab in ("media", "profiles", "indexers", "clients", "connect") else request.query_params.get("section", "media")),
        "is_overview": is_overview,
        "recent_movies": recent_movies,
        "recent_tv": recent_tv,
        "sort": sort,
        "q": search_q,
        "media_type": media_type,
        "page": page,
        "total_pages": total_pages,
        "total_items": total_items,
        "total_movies": total_movies,
        "total_tv": total_tv,
        "msg": msg,
        "calendar_entries": calendar_entries,
        "today_str": today_str,
        "calendar_weeks": calendar_weeks,
        "cal_month_label": cal_month_label,
        "cal_month_str": cal_month_str,
        "prev_month_str": prev_month_str,
        "next_month_str": next_month_str,
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
        record_history(db, "move", item.title, f"Moved to {res['new_path']} ({res['moved_files']} files)", item.id)
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


@app.post("/api/settings/roots/add-batch")
async def api_add_roots_batch(payload: dict, db: Session = Depends(get_db)):
    media_type = (payload.get("media_type") or "movie").strip().lower()
    paths = [p.strip() for p in (payload.get("paths") or []) if p and p.strip()]
    create_dir = bool(payload.get("create_dir", True))
    if not paths:
        raise HTTPException(status_code=400, detail="No folder paths provided")
    roots_map = get_media_roots(db)
    existing = [str(p) for p in roots_map.get(media_type, []) if str(p)]
    added = 0
    for p in paths:
        if create_dir:
            try:
                Path(p).mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
        if p not in existing:
            existing.append(p)
            added += 1
    key = f"{media_type}_roots"
    save_settings(db, {key: ",".join(existing)})
    return {"ok": True, "added": added}


@app.get("/api/filesystem/all-drives")
def browse_all_drives_folders():
    """Walks up to 4 levels deep across all mounted drives to find nested root folders without listing individual movie/show folders."""
    info = browse_filesystem("")
    all_dirs = []
    seen = set()
    skip_names = {"lost+found", "$recycle.bin", "system volume information", "windows", "program files", "program files (x86)", "programdata", "users", "perflogs", "downloads", "incomplete"}
    video_exts = (".mkv", ".mp4", ".avi", ".m4v")

    def _walk_roots(base_mp: Path, free_gb: float, cur: Path, depth: int):
        if depth > 4:
            return
        try:
            children = [c for c in sorted(cur.iterdir(), key=lambda x: x.name.lower())
                        if c.is_dir() and not c.name.startswith(".") and c.name.lower() not in skip_names]
        except PermissionError:
            return
        for c in children:
            # Skip leaf title folders that directly hold video files
            try:
                has_direct_video = any(f.is_file() and f.suffix.lower() in video_exts for f in c.iterdir())
            except PermissionError:
                has_direct_video = False
            if has_direct_video:
                continue
            p_str = str(c)
            if p_str not in seen:
                seen.add(p_str)
                rel_name = str(c.relative_to(base_mp))
                all_dirs.append({"mount": str(base_mp), "free_gb": free_gb, "name": rel_name, "path": p_str, "depth": depth})
            _walk_roots(base_mp, free_gb, c, depth + 1)

    for mt in info.get("mounts", []):
        mp = Path(mt["path"])
        if str(mp) in ("/", "/boot", "/tmp", "/var", "/opt", "/home", "/root", "/srv", "/mnt"):
            continue
        _walk_roots(mp, mt.get("free_gb", 0), mp, 1)
    return {"directories": all_dirs}

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
        async with httpx.AsyncClient(verify=False, timeout=8.0) as client:
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


# === ONE-STOP HUB API ROUTES: TAUTULLI, LIBRARY HEALTH AUDITOR & COLLECTIONS ===
@app.get("/api/tautulli/overview")
async def api_tautulli_overview(db: Session = Depends(get_db)):
    cfg = get_settings(db)
    items = db.query(MediaItem).all()
    activity = await fetch_tautulli_activity(cfg)
    stats = await fetch_tautulli_watch_stats(cfg, items)
    return {"activity": activity, "stats": stats}


@app.get("/api/health/audit")
def api_health_audit(db: Session = Depends(get_db)):
    return run_library_health_audit(db)


@app.post("/api/health/delete-orphan")
def api_health_delete_orphan(payload: dict, db: Session = Depends(get_db)):
    cfg = get_settings(db)
    dl_mount = Path((cfg.get("qbit_local_mount") or "").strip())
    target = Path((payload.get("path") or "").strip())
    if not dl_mount.exists() or not target.exists() or not target.is_file():
        raise HTTPException(status_code=400, detail="Invalid orphan file path")
    if dl_mount.resolve() not in target.resolve().parents:
        raise HTTPException(status_code=403, detail="Path must reside inside configured download mount")
    target.unlink()
    record_history(db, "delete", target.name, detail="Deleted orphaned download file via Health Auditor")
    return {"ok": True}


@app.get("/api/collections")
async def api_get_collections(db: Session = Depends(get_db)):
    cols = await scan_franchise_collections(db)
    return {"collections": cols}


@app.post("/api/collections/grab-missing")
async def api_collections_grab_missing(payload: dict, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    cfg = get_settings(db)
    missing_items = payload.get("missing") or []
    added = 0
    for entry in missing_items:
        title = (entry.get("title") or "").strip()
        year = entry.get("year")
        if not title:
            continue
        existing = db.query(MediaItem).filter(MediaItem.title.ilike(title), MediaItem.media_type == "movie").first()
        if not existing:
            item = MediaItem(
                title=title,
                year=int(year) if year else None,
                media_type="movie",
                status="wanted",
                quality_profile=cfg.get("default_movie_profile") or "HD-1080p",
                poster_url=entry.get("poster_url") or "",
            )
            db.add(item)
            db.commit()
            db.refresh(item)
            added += 1
        else:
            item = existing
        if item.status != "available":
            background_tasks.add_task(auto_search_and_grab_item, item.id)
    return {"ok": True, "added": added}
