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
    UNCERTAIN = "UNCERTAIN"# 0.65 - 0.87 OR Possible Version Match (requires verification)
    NONE = "NONE"          # < 0.65 (treated as new/missing)

class MatchClassification(str, Enum):
    EXACT_MATCH = "EXACT_MATCH"
    POSSIBLE_VERSION_MATCH = "POSSIBLE_VERSION_MATCH"
    UNCERTAIN = "UNCERTAIN"
    MISSING = "MISSING"

class LanguageConfidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"

class NotificationResolutionStatus(str, Enum):
    PENDING = "PENDING"
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"
    COMPLETED = "COMPLETED"
    IGNORED = "IGNORED"

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
    singer: Optional[str] = None
    available_bitrate: Optional[str] = None
    available_bitrates: List[str] = field(default_factory=list)
    download_links: Dict[str, str] = field(default_factory=dict)
    source_page_url: Optional[str] = None
    link_available: bool = False
    download_authorized: bool = False
    match_classification: MatchClassification = MatchClassification.MISSING
    match_reason: Optional[str] = None
    catalog_version_candidates: List[str] = field(default_factory=list)

    def __post_init__(self):
        if self.source_audio_url and not self.link_available:
            self.link_available = True
        if self.available_bitrates and not self.available_bitrate:
            self.available_bitrate = ", ".join(self.available_bitrates)

    def to_dict(self) -> Dict[str, Any]:
        cls_val = self.match_classification.value if hasattr(self.match_classification, "value") else str(self.match_classification)
        return {
            "id": self.id,
            "song_title": self.song_title,
            "normalized_title": self.normalized_title,
            "track_number": self.track_number,
            "singer": self.singer,
            "available_bitrate": self.available_bitrate,
            "available_bitrates": list(self.available_bitrates),
            "download_links": dict(self.download_links),
            "source_audio_url": self.source_audio_url,
            "source_page_url": self.source_page_url,
            "link_available": self.link_available,
            "download_authorized": self.download_authorized,
            "exists_in_catalog": self.exists_in_catalog,
            "catalog_song_title": self.catalog_song_title,
            "catalog_song_id": self.catalog_song_id,
            "match_confidence": self.match_confidence.value,
            "match_classification": cls_val,
            "match_score": self.match_score,
            "match_reason": self.match_reason,
            "catalog_version_candidates": list(self.catalog_version_candidates),
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
    language: str = "Telugu"
    language_confidence: LanguageConfidence = LanguageConfidence.UNKNOWN
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
        conf_val = self.language_confidence.value if hasattr(self.language_confidence, 'value') else str(self.language_confidence)
        return {
            "id": self.id,
            "album_name": self.album_name,
            "normalized_name": self.normalized_name,
            "source_url": self.source_url,
            "music_director": self.music_director,
            "year": self.year,
            "language": self.language,
            "language_confidence": conf_val,
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
    language: str = "Telugu"
    resolution_status: str = "PENDING"
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
            "language": self.language,
            "resolution_status": self.resolution_status,
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
    pages_checked: int = 1
    pages_requested: int = 0
    pages_successfully_checked: int = 0
    pages_unavailable: int = 0
    albums_discovered: int = 0
    albums_inspected: int = 0
    resolved_notifications_count: int = 0
    failed_pages: List[str] = field(default_factory=list)
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
            "pages_checked": self.pages_checked,
            "pages_requested": self.pages_requested,
            "pages_successfully_checked": self.pages_successfully_checked,
            "pages_unavailable": self.pages_unavailable,
            "albums_discovered": self.albums_discovered,
            "albums_inspected": self.albums_inspected,
            "resolved_notifications_count": self.resolved_notifications_count,
            "failed_pages": self.failed_pages,
            "error_message": self.error_message,
            "locked_by": self.locked_by
        }
