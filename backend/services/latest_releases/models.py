import time
from enum import Enum
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any

class NotificationType(str, Enum):
    NEW_ALBUM = "NEW_ALBUM"
    NEW_SONG = "NEW_SONG"
    MISSING_SONGS = "MISSING_SONGS"
    ALBUM_UPDATED = "ALBUM_UPDATED"
    SCAN_FAILED = "SCAN_FAILED"

class TrackingStatus(str, Enum):
    DISCOVERED = "DISCOVERED"
    MISSING_FROM_MUSICDATA = "MISSING_FROM_MUSICDATA"
    DOWNLOAD_SUGGESTED = "DOWNLOAD_SUGGESTED"
    DOWNLOADED_BY_USER = "DOWNLOADED_BY_USER"
    ADDED_TO_MUSICDATA = "ADDED_TO_MUSICDATA"
    IGNORED = "IGNORED"

class MatchConfidence(str, Enum):
    EXACT = "EXACT"        # 1.0 match
    HIGH = "HIGH"          # >= 0.88 match (auto-accepted as existing)
    UNCERTAIN = "UNCERTAIN"# 0.65 - 0.87 (shown for manual review)
    NONE = "NONE"          # < 0.65 (treated as new/missing)

@dataclass
class DiscoveredSong:
    song_title: str
    normalized_title: str
    track_number: int = 1
    source_audio_url: Optional[str] = None
    exists_in_catalog: bool = False
    catalog_song_title: Optional[str] = None
    catalog_song_id: Optional[str] = None
    match_confidence: MatchConfidence = MatchConfidence.NONE
    match_score: float = 0.0
    tracking_status: TrackingStatus = TrackingStatus.DISCOVERED
    first_discovered_at: float = field(default_factory=time.time)
    last_checked_at: float = field(default_factory=time.time)
    id: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "song_title": self.song_title,
            "normalized_title": self.normalized_title,
            "track_number": self.track_number,
            "source_audio_url": self.source_audio_url,
            "exists_in_catalog": self.exists_in_catalog,
            "catalog_song_title": self.catalog_song_title,
            "catalog_song_id": self.catalog_song_id,
            "match_confidence": self.match_confidence.value,
            "match_score": self.match_score,
            "tracking_status": self.tracking_status.value,
            "first_discovered_at": self.first_discovered_at,
            "last_checked_at": self.last_checked_at
        }

@dataclass
class DiscoveredAlbum:
    album_name: str
    normalized_name: str
    source_url: str
    music_director: str = "Unknown"
    year: int = 2026
    director: Optional[str] = None
    cast_crew: Optional[str] = None
    artwork_url: Optional[str] = None
    exists_in_catalog: bool = False
    catalog_album_name: Optional[str] = None
    tracking_status: TrackingStatus = TrackingStatus.DISCOVERED
    songs: List[DiscoveredSong] = field(default_factory=list)
    first_discovered_at: float = field(default_factory=time.time)
    last_checked_at: float = field(default_factory=time.time)
    id: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "album_name": self.album_name,
            "normalized_name": self.normalized_name,
            "source_url": self.source_url,
            "music_director": self.music_director,
            "year": self.year,
            "director": self.director,
            "cast_crew": self.cast_crew,
            "artwork_url": self.artwork_url,
            "exists_in_catalog": self.exists_in_catalog,
            "catalog_album_name": self.catalog_album_name,
            "tracking_status": self.tracking_status.value,
            "songs": [s.to_dict() for s in self.songs],
            "first_discovered_at": self.first_discovered_at,
            "last_checked_at": self.last_checked_at
        }

@dataclass
class NotificationItem:
    id: int
    notification_type: NotificationType
    title: str
    message: str
    album_id: Optional[int] = None
    album_name: str = ""
    artwork_url: Optional[str] = None
    music_director: str = "Unknown"
    year: int = 2026
    source_url: str = ""
    songs: List[Dict[str, Any]] = field(default_factory=list)
    missing_count: int = 0
    is_read: bool = False
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "notification_type": self.notification_type.value,
            "title": self.title,
            "message": self.message,
            "album_id": self.album_id,
            "album_name": self.album_name,
            "artwork_url": self.artwork_url,
            "music_director": self.music_director,
            "year": self.year,
            "source_url": self.source_url,
            "songs": self.songs,
            "missing_count": self.missing_count,
            "is_read": self.is_read,
            "created_at": self.created_at,
            "updated_at": self.updated_at
        }

@dataclass
class ScanRecord:
    id: int
    started_at: float
    completed_at: Optional[float] = None
    status: str = "IN_PROGRESS"
    albums_found: int = 0
    new_albums_count: int = 0
    new_songs_count: int = 0
    error_message: Optional[str] = None
    locked_by: str = "cron"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "status": self.status,
            "albums_found": self.albums_found,
            "new_albums_count": self.new_albums_count,
            "new_songs_count": self.new_songs_count,
            "error_message": self.error_message,
            "locked_by": self.locked_by
        }
