from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, ForeignKey
from datetime import datetime
from .database import Base

class MediaItem(Base):
    __tablename__ = "media_items"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, index=True)
    year = Column(Integer, nullable=True)
    media_type = Column(String)  # 'movie', 'tv', 'music'
    external_id = Column(String, index=True)
    quality_profile = Column(String, default="1080p")
    monitored = Column(Boolean, default=True)
    status = Column(String, default="wanted")  # wanted, downloading, available
    torrent_hash = Column(String, nullable=True)
    file_path = Column(String, nullable=True)
    poster_url = Column(String, nullable=True)
    added_at = Column(DateTime, default=datetime.utcnow)

class EpisodeItem(Base):
    __tablename__ = "episode_items"

    id = Column(Integer, primary_key=True, index=True)
    media_item_id = Column(Integer, ForeignKey("media_items.id", ondelete="CASCADE"), index=True)
    season_number = Column(Integer)
    episode_number = Column(Integer)
    title = Column(String, default="")
    has_file = Column(Boolean, default=False)
    quality = Column(String, default="")
    file_path = Column(String, nullable=True)

class QualityProfile(Base):
    __tablename__ = "quality_profiles"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)
    source_app = Column(String, default="omniarr")  # sonarr, radarr, omniarr
    cutoff = Column(String, default="1080p")
    min_format_score = Column(Integer, default=0)
    upgrade_allowed = Column(Boolean, default=True)
    format_scores_json = Column(Text, default="{}")  # {cf_id: score}

class CustomFormat(Base):
    __tablename__ = "custom_formats"

    id = Column(Integer, primary_key=True, index=True)
    external_cf_id = Column(Integer, nullable=True)
    name = Column(String, index=True)
    source_app = Column(String, default="omniarr")
    regex_patterns_json = Column(Text, default="[]")  # list of regex strings
    default_score = Column(Integer, default=0)

class AppSetting(Base):
    __tablename__ = "app_settings"

    key = Column(String, primary_key=True, index=True)
    value = Column(Text, default="")

class IndexerConfig(Base):
    __tablename__ = "indexer_configs"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)
    implementation = Column(String, default="Cardigann")
    enabled = Column(Boolean, default=True)
    priority = Column(Integer, default=25)


class BlacklistedRelease(Base):
    __tablename__ = "blacklisted_releases"

    id = Column(Integer, primary_key=True, index=True)
    media_item_id = Column(Integer, index=True, nullable=True)
    torrent_hash = Column(String, index=True, default="")
    release_title = Column(String, index=True, default="")
    reason = Column(String, default="Failed or stalled")
    created_at = Column(DateTime, default=datetime.utcnow)
