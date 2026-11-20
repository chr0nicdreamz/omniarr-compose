import zipfile
import sqlite3
import tempfile
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from sqlalchemy.orm import Session
from .models import MediaItem, EpisodeItem, IndexerConfig, QualityProfile, CustomFormat
from .services import save_settings

def _extract_poster(images_raw: str) -> str:
    if not images_raw:
        return ""
    try:
        imgs = json.loads(images_raw)
        for img in imgs:
            if img.get("coverType", "").lower() == "poster":
                return img.get("remoteUrl") or img.get("url") or ""
    except Exception:
        pass
    return ""

def _import_profiles_and_cfs(cur: sqlite3.Cursor, app_name: str, db: Session) -> tuple[int, int]:
    profiles_added = 0
    cfs_added = 0

    try:
        cur.execute("SELECT Id, Name, Specifications FROM CustomFormats")
        for row in cur.fetchall():
            specs = json.loads(row["Specifications"] or "[]")
            patterns = []
            for s in specs:
                fields = s.get("fields") or {}
                val = fields.get("value") if isinstance(fields, dict) else None
                if isinstance(val, str) and val.strip():
                    patterns.append(val.strip())
            cf_obj = db.query(CustomFormat).filter_by(name=row["Name"], source_app=app_name).first()
            if cf_obj:
                cf_obj.external_cf_id = row["Id"]
                cf_obj.regex_patterns_json = json.dumps(patterns)
            else:
                db.add(CustomFormat(
                    external_cf_id=row["Id"],
                    name=row["Name"],
                    source_app=app_name,
                    regex_patterns_json=json.dumps(patterns),
                    default_score=0
                ))
                cfs_added += 1
    except sqlite3.OperationalError:
        pass

    try:
        cur.execute("SELECT Name, UpgradeAllowed, MinFormatScore, FormatItems FROM QualityProfiles")
        for row in cur.fetchall():
            format_items = json.loads(row["FormatItems"] or "[]")
            scores_map = {}
            for fi in format_items:
                cf_id = fi.get("format") or fi.get("customFormatId")
                score = fi.get("score", 0)
                if cf_id is not None and score != 0:
                    scores_map[str(cf_id)] = score

            prof = db.query(QualityProfile).filter_by(name=row["Name"]).first()
            if prof:
                prof.min_format_score = row["MinFormatScore"] or 0
                prof.upgrade_allowed = bool(row["UpgradeAllowed"])
                prof.format_scores_json = json.dumps(scores_map)
            else:
                db.add(QualityProfile(
                    name=row["Name"],
                    source_app=app_name,
                    cutoff=row["Name"],
                    min_format_score=row["MinFormatScore"] or 0,
                    upgrade_allowed=bool(row["UpgradeAllowed"]),
                    format_scores_json=json.dumps(scores_map)
                ))
                profiles_added += 1
    except sqlite3.OperationalError:
        pass

    return profiles_added, cfs_added

def parse_and_import_backup(zip_bytes: bytes, db: Session) -> dict:
    summary = {
        "app": "unknown",
        "imported_media": 0,
        "imported_episodes": 0,
        "imported_profiles": 0,
        "imported_custom_formats": 0,
        "settings_found": {}
    }
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

        if app_name in ("radarr", "sonarr"):
            p_cnt, cf_cnt = _import_profiles_and_cfs(cur, app_name, db)
            summary["imported_profiles"] = p_cnt
            summary["imported_custom_formats"] = cf_cnt

        # Map Profile ID -> Name for media assignment
        profile_names = {}
        try:
            cur.execute("SELECT Id, Name FROM QualityProfiles")
            profile_names = {r["Id"]: r["Name"] for r in cur.fetchall()}
        except sqlite3.OperationalError:
            pass

        if app_name == "radarr":
            cur.execute("SELECT Title, Year, TmdbId, Monitored, MovieFileId, Path, QualityProfileId, Images FROM Movies")
            for row in cur.fetchall():
                poster = _extract_poster(row["Images"] if "Images" in row.keys() else "")
                prof_name = profile_names.get(row["QualityProfileId"], "1080p")
                exists = db.query(MediaItem).filter_by(media_type="movie", external_id=str(row["TmdbId"])).first()
                if not exists:
                    db.add(MediaItem(
                        title=row["Title"],
                        year=row["Year"],
                        media_type="movie",
                        external_id=str(row["TmdbId"]),
                        quality_profile=prof_name,
                        monitored=bool(row["Monitored"]),
                        status="available" if row["MovieFileId"] and row["MovieFileId"] > 0 else "wanted",
                        file_path=row["Path"],
                        poster_url=poster or None
                    ))
                    summary["imported_media"] += 1
                elif poster and not exists.poster_url:
                    exists.poster_url = poster

        elif app_name == "sonarr":
            # Build EpisodeFile lookup map: EpisodeFileId -> {RelativePath, SceneName}
            ep_files = {}
            try:
                cur.execute("SELECT Id, RelativePath, SceneName FROM EpisodeFiles")
                for ef in cur.fetchall():
                    ep_files[ef["Id"]] = {
                        "path": ef["RelativePath"] or "",
                        "release": ef["SceneName"] or ef["RelativePath"] or ""
                    }
            except sqlite3.OperationalError:
                pass

            cur.execute("SELECT Id, Title, Year, TvdbId, Monitored, Path, QualityProfileId, Images FROM Series")
            series_map = {}  # Sonarr Series.Id -> MediaItem
            for row in cur.fetchall():
                poster = _extract_poster(row["Images"] if "Images" in row.keys() else "")
                prof_name = profile_names.get(row["QualityProfileId"], "1080p")
                media_obj = db.query(MediaItem).filter_by(media_type="tv", external_id=str(row["TvdbId"])).first()
                if not media_obj:
                    media_obj = MediaItem(
                        title=row["Title"],
                        year=row["Year"],
                        media_type="tv",
                        external_id=str(row["TvdbId"]),
                        quality_profile=prof_name,
                        monitored=bool(row["Monitored"]),
                        status="wanted",
                        file_path=row["Path"],
                        poster_url=poster or None
                    )
                    db.add(media_obj)
                    db.flush()
                    summary["imported_media"] += 1
                else:
                    if poster and not media_obj.poster_url:
                        media_obj.poster_url = poster
                series_map[row["Id"]] = media_obj

            # Import all grabbed episodes (EpisodeFileId > 0)
            try:
                cur.execute("""
                    SELECT SeriesId, SeasonNumber, EpisodeNumber, Title, EpisodeFileId
                    FROM Episodes
                    WHERE EpisodeFileId > 0
                    ORDER BY SeasonNumber ASC, EpisodeNumber ASC
                """)
                series_with_files = set()
                for ep in cur.fetchall():
                    parent = series_map.get(ep["SeriesId"])
                    if not parent:
                        continue
                    series_with_files.add(parent.id)
                    existing_ep = db.query(EpisodeItem).filter_by(
                        media_item_id=parent.id,
                        season_number=ep["SeasonNumber"],
                        episode_number=ep["EpisodeNumber"]
                    ).first()
                    if not existing_ep:
                        ef_info = ep_files.get(ep["EpisodeFileId"], {})
                        db.add(EpisodeItem(
                            media_item_id=parent.id,
                            season_number=ep["SeasonNumber"],
                            episode_number=ep["EpisodeNumber"],
                            title=ep["Title"] or "",
                            has_file=True,
                            quality=ef_info.get("release", "")[:60],
                            file_path=ef_info.get("path", "")
                        ))
                        summary["imported_episodes"] += 1
                for m_obj in series_map.values():
                    if m_obj.id in series_with_files:
                        m_obj.status = "available"
            except sqlite3.OperationalError:
                pass

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
