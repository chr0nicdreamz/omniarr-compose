from sqlalchemy import Column, Integer, String, Boolean, DateTime
from datetime import datetime
from .database import Base

class MediaItem(Base):
    __tablename__ = "media_items"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, index=True)
    year = Column(Integer, nullable=True)
    media_type = Column(String)  # 'movie', 'tv', 'music'
    external_id = Column(String, index=True)  # TMDB, TVDB, or MusicBrainz ID
    quality_profile = Column(String, default="1080p")
    monitored = Column(Boolean, default=True)
    status = Column(String, default="wanted")  # wanted, downloading, available
    torrent_hash = Column(String, nullable=True)
    file_path = Column(String, nullable=True)
    added_at = Column(DateTime, default=datetime.utcnow)
