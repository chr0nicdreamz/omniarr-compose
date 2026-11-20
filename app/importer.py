import zipfile
import sqlite3
import tempfile
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from sqlalchemy.orm import Session
from .models import MediaItem

def parse_and_import_backup(zip_bytes: bytes, db: Session) -> dict:
    summary = {"app": "unknown", "imported_media": 0, "settings_found": {}}

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        zip_path = tmp_path / "backup.zip"
        zip_path.write_bytes(zip_bytes)

        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmp_path)

        # 1. Parse config.xml for API Key / Port
        config_file = tmp_path / "config.xml"
        if config_file.exists():
            tree = ET.parse(config_file)
            root = tree.getroot()
            api_key = root.findtext("ApiKey")
            if api_key:
                summary["settings_found"]["api_key"] = api_key

        # 2. Locate the SQLite database
        db_files = list(tmp_path.glob("*.db"))
        if not db_files:
            raise ValueError("No .db file found inside backup archive.")

        db_file = db_files[0]
        app_name = db_file.stem.lower()  # sonarr, radarr, lidarr, or prowlarr
        summary["app"] = app_name

        conn = sqlite3.connect(str(db_file))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # 3. Extract Download Client (qBittorrent) & Notification (Plex) settings
        try:
            cur.execute("SELECT Name, Implementation, Settings FROM DownloadClients")
            for row in cur.fetchall():
                if "qbittorrent" in row["Implementation"].lower():
                    summary["settings_found"]["qbittorrent"] = json.loads(row["Settings"])
        except sqlite3.OperationalError:
            pass

        try:
            cur.execute("SELECT Name, Implementation, Settings FROM Notifications")
            for row in cur.fetchall():
                if "plex" in row["Implementation"].lower():
                    summary["settings_found"]["plex"] = json.loads(row["Settings"])
        except sqlite3.OperationalError:
            pass

        # 4. Import Media Libraries based on which *Arr backup was uploaded
        if app_name == "radarr":
            cur.execute("""
                SELECT m.Title, m.Year, m.TmdbId, m.Monitored, m.MovieFileId, m.Path
                FROM Movies m
            """)
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
            cur.execute("""
                SELECT s.Title, s.Year, s.TvdbId, s.Monitored, s.Path
                FROM Series s
            """)
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
            cur.execute("""
                SELECT a.Name, a.ForeignArtistId, a.Monitored, a.Path
                FROM Artists a
            """)
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
            cur.execute("SELECT Name, Implementation, Enable FROM Indexers")
            summary["settings_found"]["prowlarr_indexers"] = [
                {"name": r["Name"], "type": r["Implementation"], "enabled": bool(r["Enable"])}
                for r in cur.fetchall()
            ]

        conn.close()
        db.commit()

    return summary
