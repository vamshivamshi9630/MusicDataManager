"""
Latest Releases Discovery Engine
Monitors https://naasongs.com.co/ for new Telugu movie albums and incremental song releases,
compares discovered content with the MusicData catalog, and produces deduplicated notifications.
"""

from backend.services.latest_releases.models import (
    NotificationType,
    TrackingStatus,
    MatchConfidence,
    DiscoveredAlbum,
    DiscoveredSong,
    NotificationItem,
    ScanRecord
)
from backend.services.latest_releases.db import get_latest_releases_db, LatestReleasesDB
from backend.services.latest_releases.discovery import LatestReleasesDiscoveryEngine

__all__ = [
    "NotificationType",
    "TrackingStatus",
    "MatchConfidence",
    "DiscoveredAlbum",
    "DiscoveredSong",
    "NotificationItem",
    "ScanRecord",
    "get_latest_releases_db",
    "LatestReleasesDB",
    "LatestReleasesDiscoveryEngine"
]
