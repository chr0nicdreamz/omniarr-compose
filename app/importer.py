import zipfile
import sqlite3
import tempfile
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from sqlalchemy.orm import Session
from .models import MediaItem, IndexerConfig
from .services import save_settings

def parse_and_import_backup(zip_bytes: bytes, db: Session) -> dict:
    summary = {"app": "unknown", "imported_media": 0, "settings_found": {}}
    setting_updates = {}

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        zip_path = tmp_path / "backup.zip"
        zip_path.write_bytes(zip_bytes)

        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmp_path)

        db_files = list(tmp_path.glob("*.db"))
        if not db_files:
            raise ValueError("No .db file found inside backup archive.")

        db_file = db_files[0]
        app_name = db_file.stem.lower()
        summary["app"] = app_name

        config_file = tmp_path / "config.xml"
        if config_file.exists():
            tree = ET.parse(config_file)
            root = tree.getroot()
            api_key = root.findtext("ApiKey")
            port = root.findtext("Port") or "9696"
            if api_key and app_name == "prowlarr":
                summary["settings_found"]["prowlarr_api_key"] = api_key
                setting_updates["prowlarr_api_key"] = api_key

        conn = sqlite3.connect(str(db_file))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        try:
            cur.execute("SELECT Name, Implementation, Settings FROM DownloadClients")
            for row in cur.fetchall():
                if "qbittorrent" in row["Implementation"].lower():
                    q = json.loads(row["Settings"])
                    summary["settings_found"]["qbittorrent"] = q
                    if q.get("host"):
                        proto = "https" if q.get("useSsl") else "http"
                        setting_updates["qbit_url"] = f"{proto}://{q['host']}:{q.get('port', 8080)}"
                    if q.get("username"):
                        setting_updates["qbit_user"] = q["username"]
                    if q.get("password"):
                        setting_updates["qbit_pass"] = q["password"]
        except sqlite3.OperationalError:
            pass

        try:
            cur.execute("SELECT Name, Implementation, Settings FROM Notifications")
            for row in cur.fetchall():
                if "plex" in row["Implementation"].lower():
                    p = json.loads(row["Settings"])
                    summary["settings_found"]["plex"] = p
                    if p.get("host"):
                        proto = "https" if p.get("useSsl") else "http"
                        setting_updates["plex_url"] = f"{proto}://{p['host']}:{p.get('port', 32400)}"
                    if p.get("authToken"):
                        setting_updates["plex_token"] = p["authToken"]
        except sqlite3.OperationalError:
            pass

        if app_name == "radarr":
            cur.execute("SELECT Title, Year, TmdbId, Monitored, MovieFileId, Path FROM Movies")
            for row in cur.fetchall():
                exists = db.query(MediaItem).filter_by(media_type="movie", external_id=str(row["TmdbId"])).first()
                if not exists:
                    db.add(MediaItem(
                        title=row["Title"],
                        year=row["Year"],
                        media_type="movie",
                        external_id=str(row["TmdbId"]),
                        monitored=bool(row["Monitored"]),
                        status="available" if row["MovieFileId"] and row["MovieFileId"] > 0 else "wanted",
                        file_path=row["Path"]
                    ))
                    summary["imported_media"] += 1

        elif app_name == "sonarr":
            cur.execute("SELECT Title, Year, TvdbId, Monitored, Path FROM Series")
            for row in cur.fetchall():
                exists = db.query(MediaItem).filter_by(media_type="tv", external_id=str(row["TvdbId"])).first()
                if not exists:
                    db.add(MediaItem(
                        title=row["Title"],
                        year=row["Year"],
                        media_type="tv",
                        external_id=str(row["TvdbId"]),
                        monitored=bool(row["Monitored"]),
                        status="wanted",
                        file_path=row["Path"]
                    ))
                    summary["imported_media"] += 1

        elif app_name == "lidarr":
            cur.execute("SELECT Name, ForeignArtistId, Monitored, Path FROM Artists")
            for row in cur.fetchall():
                exists = db.query(MediaItem).filter_by(media_type="music", external_id=str(row["ForeignArtistId"])).first()
                if not exists:
                    db.add(MediaItem(
                        title=row["Name"],
                        media_type="music",
                        external_id=str(row["ForeignArtistId"]),
                        monitored=bool(row["Monitored"]),
                        status="wanted",
                        file_path=row["Path"]
                    ))
                    summary["imported_media"] += 1

        elif app_name == "prowlarr":
            cur.execute("SELECT Name, Implementation, Enable, Priority FROM Indexers")
            imported_idx = []
            for r in cur.fetchall():
                idx_obj = db.query(IndexerConfig).filter_by(name=r["Name"]).first()
                if idx_obj:
                    idx_obj.enabled = bool(r["Enable"])
                    idx_obj.implementation = r["Implementation"]
                    idx_obj.priority = r["Priority"] or 25
                else:
                    db.add(IndexerConfig(
                        name=r["Name"],
                        implementation=r["Implementation"],
                        enabled=bool(r["Enable"]),
                        priority=r["Priority"] or 25
                    ))
                imported_idx.append({"name": r["Name"], "type": r["Implementation"], "enabled": bool(r["Enable"])})
            summary["settings_found"]["prowlarr_indexers"] = imported_idx

        conn.close()
        if setting_updates:
            save_settings(db, setting_updates)
        db.commit()

    return summary
