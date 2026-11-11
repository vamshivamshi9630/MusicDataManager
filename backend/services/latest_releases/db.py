import os
import json
import time
import hashlib
import sqlite3
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple
from backend.core.config import settings
from backend.services.latest_releases.models import (
    NotificationType,
    TrackingStatus,
    MatchConfidence,
    DiscoveredAlbum,
    DiscoveredSong,
    NotificationItem,
    ScanRecord
)

class LatestReleasesDB:
    """
    Unified database manager for Latest Releases.
    Supports Render PostgreSQL (via DATABASE_URL) with seamless SQLite fallback for offline local development.
    """

    def __init__(self, database_url: Optional[str] = None, sqlite_path: Optional[str] = None):
        self.raw_url = database_url if database_url is not None else (settings.DATABASE_URL or "")
        self.is_postgres = self.raw_url.startswith("postgres://") or self.raw_url.startswith("postgresql://")
        
        # Render provides postgres:// which psycopg2 prefers as postgresql://
        if self.is_postgres and self.raw_url.startswith("postgres://"):
            self.pg_url = self.raw_url.replace("postgres://", "postgresql://", 1)
        else:
            self.pg_url = self.raw_url

        db_file = sqlite_path or os.environ.get("LATEST_RELEASES_DB_PATH") or settings.LATEST_RELEASES_DB_PATH
        self.sqlite_path = Path(db_file).resolve()
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)

        self._init_schema()

    def _get_connection(self):
        if self.is_postgres:
            try:
                import psycopg2
                import psycopg2.extras
                return psycopg2.connect(self.pg_url)
            except Exception as e:
                # If PostgreSQL fails to connect, fallback to SQLite to prevent complete downtime
                print(f"[LatestReleasesDB] PostgreSQL connection failed ({e}), falling back to SQLite.")
                self.is_postgres = False

        conn = sqlite3.connect(str(self.sqlite_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self):
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            id_type = "SERIAL PRIMARY KEY" if self.is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
            bool_type = "BOOLEAN" if self.is_postgres else "INTEGER"

            # 1. Distributed Lock Table
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS latest_releases_lock (
                    lock_name VARCHAR(64) PRIMARY KEY,
                    locked_at DOUBLE PRECISION,
                    locked_by VARCHAR(128),
                    expires_at DOUBLE PRECISION
                );
            """)

            # 2. Discovery Scans Table
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS discovery_scans (
                    id {id_type},
                    started_at DOUBLE PRECISION NOT NULL,
                    completed_at DOUBLE PRECISION,
                    status VARCHAR(32) DEFAULT 'IN_PROGRESS',
                    albums_found INT DEFAULT 0,
                    new_albums_count INT DEFAULT 0,
                    new_songs_count INT DEFAULT 0,
                    error_message TEXT,
                    locked_by VARCHAR(64)
                );
            """)

            # 3. Discovered Albums Table
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS discovered_albums (
                    id {id_type},
                    source_url TEXT UNIQUE NOT NULL,
                    album_name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL,
                    music_director TEXT DEFAULT 'Unknown',
                    year INT DEFAULT 2026,
                    director TEXT,
                    cast_crew TEXT,
                    artwork_url TEXT,
                    exists_in_catalog {bool_type} DEFAULT {'FALSE' if self.is_postgres else '0'},
                    catalog_album_name TEXT,
                    tracking_status VARCHAR(64) DEFAULT 'DISCOVERED',
                    first_discovered_at DOUBLE PRECISION NOT NULL,
                    last_checked_at DOUBLE PRECISION NOT NULL
                );
            """)

            # 4. Discovered Songs Table
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS discovered_songs (
                    id {id_type},
                    album_id INT NOT NULL REFERENCES discovered_albums(id) ON DELETE CASCADE,
                    song_title TEXT NOT NULL,
                    normalized_title TEXT NOT NULL,
                    track_number INT DEFAULT 1,
                    source_audio_url TEXT,
                    exists_in_catalog {bool_type} DEFAULT {'FALSE' if self.is_postgres else '0'},
                    catalog_song_title TEXT,
                    catalog_song_id TEXT,
                    match_confidence VARCHAR(32) DEFAULT 'NONE',
                    match_score DOUBLE PRECISION DEFAULT 0.0,
                    tracking_status VARCHAR(64) DEFAULT 'DISCOVERED',
                    first_discovered_at DOUBLE PRECISION NOT NULL,
                    last_checked_at DOUBLE PRECISION NOT NULL,
                    CONSTRAINT unq_song UNIQUE (album_id, normalized_title)
                );
            """)

            # 5. Notifications Table
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS notifications (
                    id {id_type},
                    album_id INT,
                    notification_type VARCHAR(64) NOT NULL,
                    title TEXT NOT NULL,
                    message TEXT NOT NULL,
                    album_name TEXT NOT NULL,
                    artwork_url TEXT,
                    music_director TEXT DEFAULT 'Unknown',
                    year INT DEFAULT 2026,
                    source_url TEXT,
                    missing_count INT DEFAULT 0,
                    is_read {bool_type} DEFAULT {'FALSE' if self.is_postgres else '0'},
                    created_at DOUBLE PRECISION NOT NULL,
                    updated_at DOUBLE PRECISION NOT NULL,
                    details_json TEXT,
                    dedup_hash TEXT UNIQUE
                );
            """)

            conn.commit()
        finally:
            conn.close()

    # =========================================================================
    # Distributed Scan Locking (Multi-Process & Render Cron Safe)
    # =========================================================================

    def acquire_scan_lock(self, locked_by: str = "cron", timeout_minutes: int = 15) -> bool:
        """
        Attempt to acquire distributed scan lock.
        Returns True if acquired, False if an active scan is already running.
        """
        now = time.time()
        expires_at = now + (timeout_minutes * 60)
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT lock_name, locked_at, locked_by, expires_at FROM latest_releases_lock WHERE lock_name = 'global_scan';")
            row = cur.fetchone()

            if row:
                r_expires_at = row[3] if isinstance(row, tuple) else row["expires_at"]
                if r_expires_at and now < r_expires_at:
                    # Scan is actively running and has not expired
                    return False
                # Lock expired, take over
                if self.is_postgres:
                    cur.execute("""
                        UPDATE latest_releases_lock
                        SET locked_at = %s, locked_by = %s, expires_at = %s
                        WHERE lock_name = 'global_scan';
                    """, (now, locked_by, expires_at))
                else:
                    cur.execute("""
                        UPDATE latest_releases_lock
                        SET locked_at = ?, locked_by = ?, expires_at = ?
                        WHERE lock_name = 'global_scan';
                    """, (now, locked_by, expires_at))
            else:
                if self.is_postgres:
                    cur.execute("""
                        INSERT INTO latest_releases_lock (lock_name, locked_at, locked_by, expires_at)
                        VALUES ('global_scan', %s, %s, %s);
                    """, (now, locked_by, expires_at))
                else:
                    cur.execute("""
                        INSERT INTO latest_releases_lock (lock_name, locked_at, locked_by, expires_at)
                        VALUES ('global_scan', ?, ?, ?);
                    """, (now, locked_by, expires_at))

            conn.commit()
            return True
        finally:
            conn.close()

    def release_scan_lock(self, locked_by: Optional[str] = None):
        """Release distributed scan lock."""
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            if locked_by:
                if self.is_postgres:
                    cur.execute("DELETE FROM latest_releases_lock WHERE lock_name = 'global_scan' AND locked_by = %s;", (locked_by,))
                else:
                    cur.execute("DELETE FROM latest_releases_lock WHERE lock_name = 'global_scan' AND locked_by = ?;", (locked_by,))
            else:
                cur.execute("DELETE FROM latest_releases_lock WHERE lock_name = 'global_scan';")
            conn.commit()
        finally:
            conn.close()

    def get_lock_status(self) -> Dict[str, Any]:
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT lock_name, locked_at, locked_by, expires_at FROM latest_releases_lock WHERE lock_name = 'global_scan';")
            row = cur.fetchone()
            if not row:
                return {"is_locked": False}
            locked_at = row[1] if isinstance(row, tuple) else row["locked_at"]
            locked_by = row[2] if isinstance(row, tuple) else row["locked_by"]
            expires_at = row[3] if isinstance(row, tuple) else row["expires_at"]
            is_active = time.time() < expires_at
            return {
                "is_locked": is_active,
                "locked_at": locked_at,
                "locked_by": locked_by,
                "expires_at": expires_at
            }
        finally:
            conn.close()

    # =========================================================================
    # Scan History Telemetry
    # =========================================================================

    def record_scan_start(self, locked_by: str = "cron") -> int:
        now = time.time()
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            if self.is_postgres:
                cur.execute("""
                    INSERT INTO discovery_scans (started_at, status, locked_by)
                    VALUES (%s, 'IN_PROGRESS', %s) RETURNING id;
                """, (now, locked_by))
                scan_id = cur.fetchone()[0]
            else:
                cur.execute("""
                    INSERT INTO discovery_scans (started_at, status, locked_by)
                    VALUES (?, 'IN_PROGRESS', ?);
                """, (now, locked_by))
                scan_id = cur.lastrowid
            conn.commit()
            return scan_id
        finally:
            conn.close()

    def record_scan_complete(self, scan_id: int, albums_found: int, new_albums: int, new_songs: int, error_message: Optional[str] = None):
        now = time.time()
        status_str = "FAILED" if error_message else "COMPLETED"
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            if self.is_postgres:
                cur.execute("""
                    UPDATE discovery_scans
                    SET completed_at = %s, status = %s, albums_found = %s, new_albums_count = %s, new_songs_count = %s, error_message = %s
                    WHERE id = %s;
                """, (now, status_str, albums_found, new_albums, new_songs, error_message, scan_id))
            else:
                cur.execute("""
                    UPDATE discovery_scans
                    SET completed_at = ?, status = ?, albums_found = ?, new_albums_count = ?, new_songs_count = ?, error_message = ?
                    WHERE id = ?;
                """, (now, status_str, albums_found, new_albums, new_songs, error_message, scan_id))
            conn.commit()
        finally:
            conn.close()

    def get_recent_scans(self, limit: int = 15) -> List[Dict[str, Any]]:
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            cur.execute(f"SELECT * FROM discovery_scans ORDER BY started_at DESC LIMIT {limit};")
            rows = cur.fetchall()
            results = []
            for r in rows:
                if isinstance(r, tuple):
                    results.append({
                        "id": r[0], "started_at": r[1], "completed_at": r[2], "status": r[3],
                        "albums_found": r[4], "new_albums_count": r[5], "new_songs_count": r[6],
                        "error_message": r[7], "locked_by": r[8]
                    })
                else:
                    results.append(dict(r))
            return results
        finally:
            conn.close()

    # =========================================================================
    # Album & Song Persistence
    # =========================================================================

    def upsert_discovered_album(self, album: DiscoveredAlbum) -> int:
        now = time.time()
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            # Check existing by source_url
            if self.is_postgres:
                cur.execute("SELECT id FROM discovered_albums WHERE source_url = %s;", (album.source_url,))
            else:
                cur.execute("SELECT id FROM discovered_albums WHERE source_url = ?;", (album.source_url,))
            row = cur.fetchone()

            if row:
                album_id = row[0]
                if self.is_postgres:
                    cur.execute("""
                        UPDATE discovered_albums
                        SET album_name = %s, normalized_name = %s, music_director = %s, year = %s,
                            director = %s, cast_crew = %s, artwork_url = %s, exists_in_catalog = %s,
                            catalog_album_name = %s, tracking_status = %s, last_checked_at = %s
                        WHERE id = %s;
                    """, (album.album_name, album.normalized_name, album.music_director, album.year,
                          album.director, album.cast_crew, album.artwork_url, album.exists_in_catalog,
                          album.catalog_album_name, album.tracking_status.value, now, album_id))
                else:
                    cur.execute("""
                        UPDATE discovered_albums
                        SET album_name = ?, normalized_name = ?, music_director = ?, year = ?,
                            director = ?, cast_crew = ?, artwork_url = ?, exists_in_catalog = ?,
                            catalog_album_name = ?, tracking_status = ?, last_checked_at = ?
                        WHERE id = ?;
                    """, (album.album_name, album.normalized_name, album.music_director, album.year,
                          album.director, album.cast_crew, album.artwork_url, 1 if album.exists_in_catalog else 0,
                          album.catalog_album_name, album.tracking_status.value, now, album_id))
            else:
                if self.is_postgres:
                    cur.execute("""
                        INSERT INTO discovered_albums (
                            source_url, album_name, normalized_name, music_director, year,
                            director, cast_crew, artwork_url, exists_in_catalog, catalog_album_name,
                            tracking_status, first_discovered_at, last_checked_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id;
                    """, (album.source_url, album.album_name, album.normalized_name, album.music_director,
                          album.year, album.director, album.cast_crew, album.artwork_url, album.exists_in_catalog,
                          album.catalog_album_name, album.tracking_status.value, now, now))
                    album_id = cur.fetchone()[0]
                else:
                    cur.execute("""
                        INSERT INTO discovered_albums (
                            source_url, album_name, normalized_name, music_director, year,
                            director, cast_crew, artwork_url, exists_in_catalog, catalog_album_name,
                            tracking_status, first_discovered_at, last_checked_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """, (album.source_url, album.album_name, album.normalized_name, album.music_director,
                          album.year, album.director, album.cast_crew, album.artwork_url, 1 if album.exists_in_catalog else 0,
                          album.catalog_album_name, album.tracking_status.value, now, now))
                    album_id = cur.lastrowid

            conn.commit()
            return album_id
        finally:
            conn.close()

    def upsert_discovered_song(self, album_id: int, song: DiscoveredSong) -> int:
        now = time.time()
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            if self.is_postgres:
                cur.execute("""
                    SELECT id, tracking_status FROM discovered_songs
                    WHERE album_id = %s AND normalized_title = %s;
                """, (album_id, song.normalized_title))
            else:
                cur.execute("""
                    SELECT id, tracking_status FROM discovered_songs
                    WHERE album_id = ? AND normalized_title = ?;
                """, (album_id, song.normalized_title))
            row = cur.fetchone()

            if row:
                song_id = row[0]
                # Preserve user download tracking if set
                existing_tracking = row[1]
                effective_status = existing_tracking if existing_tracking in (TrackingStatus.DOWNLOADED_BY_USER.value, TrackingStatus.IGNORED.value) else song.tracking_status.value

                if self.is_postgres:
                    cur.execute("""
                        UPDATE discovered_songs
                        SET song_title = %s, track_number = %s, source_audio_url = %s,
                            exists_in_catalog = %s, catalog_song_title = %s, catalog_song_id = %s,
                            match_confidence = %s, match_score = %s, tracking_status = %s,
                            last_checked_at = %s
                        WHERE id = %s;
                    """, (song.song_title, song.track_number, song.source_audio_url,
                          song.exists_in_catalog, song.catalog_song_title, song.catalog_song_id,
                          song.match_confidence.value, song.match_score, effective_status,
                          now, song_id))
                else:
                    cur.execute("""
                        UPDATE discovered_songs
                        SET song_title = ?, track_number = ?, source_audio_url = ?,
                            exists_in_catalog = ?, catalog_song_title = ?, catalog_song_id = ?,
                            match_confidence = ?, match_score = ?, tracking_status = ?,
                            last_checked_at = ?
                        WHERE id = ?;
                    """, (song.song_title, song.track_number, song.source_audio_url,
                          1 if song.exists_in_catalog else 0, song.catalog_song_title, song.catalog_song_id,
                          song.match_confidence.value, song.match_score, effective_status,
                          now, song_id))
            else:
                if self.is_postgres:
                    cur.execute("""
                        INSERT INTO discovered_songs (
                            album_id, song_title, normalized_title, track_number, source_audio_url,
                            exists_in_catalog, catalog_song_title, catalog_song_id, match_confidence,
                            match_score, tracking_status, first_discovered_at, last_checked_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id;
                    """, (album_id, song.song_title, song.normalized_title, song.track_number,
                          song.source_audio_url, song.exists_in_catalog, song.catalog_song_title,
                          song.catalog_song_id, song.match_confidence.value, song.match_score,
                          song.tracking_status.value, now, now))
                    song_id = cur.fetchone()[0]
                else:
                    cur.execute("""
                        INSERT INTO discovered_songs (
                            album_id, song_title, normalized_title, track_number, source_audio_url,
                            exists_in_catalog, catalog_song_title, catalog_song_id, match_confidence,
                            match_score, tracking_status, first_discovered_at, last_checked_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """, (album_id, song.song_title, song.normalized_title, song.track_number,
                          song.source_audio_url, 1 if song.exists_in_catalog else 0, song.catalog_song_title,
                          song.catalog_song_id, song.match_confidence.value, song.match_score,
                          song.tracking_status.value, now, now))
                    song_id = cur.lastrowid

            conn.commit()
            return song_id
        finally:
            conn.close()

    # =========================================================================
    # Notifications Management & Deduplication
    # =========================================================================

    def create_or_update_notification(
        self,
        album_id: int,
        notification_type: NotificationType,
        title: str,
        message: str,
        album_name: str,
        artwork_url: Optional[str],
        music_director: str,
        year: int,
        source_url: str,
        songs_list: List[Dict[str, Any]],
        missing_count: int
    ) -> bool:
        """
        Creates a notification. If an identical notification hash exists, updates timestamp without creating duplicate.
        Returns True if a new notification was inserted, False if deduplicated.
        """
        now = time.time()
        # Compute deduplication hash based on album, notification type, and list of missing song titles
        missing_titles = sorted([s.get("song_title", "") for s in songs_list if not s.get("exists_in_catalog")])
        hash_input = f"{album_name}:{notification_type.value}:{','.join(missing_titles)}"
        dedup_hash = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()
        details_json = json.dumps(songs_list)

        conn = self._get_connection()
        try:
            cur = conn.cursor()
            if self.is_postgres:
                cur.execute("SELECT id FROM notifications WHERE dedup_hash = %s;", (dedup_hash,))
            else:
                cur.execute("SELECT id FROM notifications WHERE dedup_hash = ?;", (dedup_hash,))
            existing = cur.fetchone()

            if existing:
                # Deduplicated: update timestamp, don't spam duplicate notification
                notif_id = existing[0]
                if self.is_postgres:
                    cur.execute("""
                        UPDATE notifications
                        SET updated_at = %s, details_json = %s, missing_count = %s
                        WHERE id = %s;
                    """, (now, details_json, missing_count, notif_id))
                else:
                    cur.execute("""
                        UPDATE notifications
                        SET updated_at = ?, details_json = ?, missing_count = ?
                        WHERE id = ?;
                    """, (now, details_json, missing_count, notif_id))
                conn.commit()
                return False

            # Insert new notification
            if self.is_postgres:
                cur.execute("""
                    INSERT INTO notifications (
                        album_id, notification_type, title, message, album_name, artwork_url,
                        music_director, year, source_url, missing_count, is_read, created_at,
                        updated_at, details_json, dedup_hash
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, FALSE, %s, %s, %s, %s);
                """, (album_id, notification_type.value, title, message, album_name, artwork_url,
                      music_director, year, source_url, missing_count, now, now, details_json, dedup_hash))
            else:
                cur.execute("""
                    INSERT INTO notifications (
                        album_id, notification_type, title, message, album_name, artwork_url,
                        music_director, year, source_url, missing_count, is_read, created_at,
                        updated_at, details_json, dedup_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?);
                """, (album_id, notification_type.value, title, message, album_name, artwork_url,
                      music_director, year, source_url, missing_count, now, now, details_json, dedup_hash))

            conn.commit()
            return True
        finally:
            conn.close()

    def get_notifications(
        self,
        category: Optional[str] = None,
        unread_only: bool = False,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            conditions = []
            params = []

            if category and category.lower() != "all":
                cat_upper = category.upper()
                if self.is_postgres:
                    conditions.append("notification_type = %s")
                else:
                    conditions.append("notification_type = ?")
                params.append(cat_upper)

            if unread_only:
                if self.is_postgres:
                    conditions.append("is_read = FALSE")
                else:
                    conditions.append("is_read = 0")

            where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            query = f"SELECT * FROM notifications {where_clause} ORDER BY created_at DESC LIMIT {limit};"

            cur.execute(query, tuple(params) if params else ())
            rows = cur.fetchall()
            results = []
            for r in rows:
                if isinstance(r, tuple):
                    d = {
                        "id": r[0], "album_id": r[1], "notification_type": r[2], "title": r[3],
                        "message": r[4], "album_name": r[5], "artwork_url": r[6], "music_director": r[7],
                        "year": r[8], "source_url": r[9], "missing_count": r[10], "is_read": bool(r[11]),
                        "created_at": r[12], "updated_at": r[13], "details_json": r[14], "dedup_hash": r[15]
                    }
                else:
                    d = dict(r)
                    d["is_read"] = bool(d["is_read"])

                try:
                    d["songs"] = json.loads(d.get("details_json") or "[]")
                except Exception:
                    d["songs"] = []
                results.append(d)
            return results
        finally:
            conn.close()

    def get_unread_count(self) -> int:
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            query = "SELECT COUNT(*) FROM notifications WHERE is_read = FALSE;" if self.is_postgres else "SELECT COUNT(*) FROM notifications WHERE is_read = 0;"
            cur.execute(query)
            row = cur.fetchone()
            return row[0] if row else 0
        finally:
            conn.close()

    def mark_notification_read(self, notification_id: int):
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            if self.is_postgres:
                cur.execute("UPDATE notifications SET is_read = TRUE, updated_at = %s WHERE id = %s;", (time.time(), notification_id))
            else:
                cur.execute("UPDATE notifications SET is_read = 1, updated_at = ? WHERE id = ?;", (time.time(), notification_id))
            conn.commit()
        finally:
            conn.close()

    def mark_all_notifications_read(self):
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            now = time.time()
            if self.is_postgres:
                cur.execute("UPDATE notifications SET is_read = TRUE, updated_at = %s;", (now,))
            else:
                cur.execute("UPDATE notifications SET is_read = 1, updated_at = ?;", (now,))
            conn.commit()
        finally:
            conn.close()

    def update_song_tracking_status(self, song_id: int, new_status: TrackingStatus):
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            now = time.time()
            if self.is_postgres:
                cur.execute("UPDATE discovered_songs SET tracking_status = %s, last_checked_at = %s WHERE id = %s;", (new_status.value, now, song_id))
            else:
                cur.execute("UPDATE discovered_songs SET tracking_status = ?, last_checked_at = ? WHERE id = ?;", (new_status.value, now, song_id))
            conn.commit()
        finally:
            conn.close()

_db_instance: Optional[LatestReleasesDB] = None

def get_latest_releases_db() -> LatestReleasesDB:
    global _db_instance
    if _db_instance is None:
        _db_instance = LatestReleasesDB()
    return _db_instance
