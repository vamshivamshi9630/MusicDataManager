import os
import re
import json
import time
import hashlib
import sqlite3
import urllib.parse
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


def normalize_database_url(raw_url: Optional[str]) -> str:
    """
    Normalizes a database connection string cleanly without mutating embedded credentials.
    Strips surrounding whitespace/quotes, optional 'DATABASE_URL=' prefix, and normalizes
    PostgreSQL scheme prefixes ('postgres://', 'postgresql+psycopg2://') to 'postgresql://'.
    """
    val = (raw_url or "").strip()
    if not val:
        return ""

    # Strip accidental matching surrounding quotes
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
        val = val[1:-1].strip()

    # Strip accidental 'DATABASE_URL=' key prefix if user pasted key=value
    if re.match(r'^DATABASE_URL\s*=', val, re.IGNORECASE):
        val = re.sub(r'^DATABASE_URL\s*=\s*', '', val, count=1, flags=re.IGNORECASE).strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
            val = val[1:-1].strip()

    m_scheme = re.match(r'^([A-Za-z][A-Za-z0-9+.-]{0,24})://(.*)$', val, re.DOTALL)
    if m_scheme:
        scheme_low = m_scheme.group(1).lower()
        rest = m_scheme.group(2)
        if scheme_low in ("postgres", "postgresql", "postgresql+psycopg2", "postgresql+psycopg"):
            return f"postgresql://{rest}"
        return f"{scheme_low}://{rest}"

    return val


def describe_database_url(raw_url: Optional[str]) -> Dict[str, Any]:
    """
    Returns non-sensitive diagnostic metadata about DATABASE_URL.
    NEVER exposes username, password, hostname, port, query params, or full URL.
    """
    raw = raw_url or ""
    cleaned = normalize_database_url(raw)
    present = bool(cleaned)

    m_scheme = re.match(r'^([A-Za-z][A-Za-z0-9+.-]{0,24})://', cleaned)
    if not present:
        scheme = "none"
    elif m_scheme:
        scheme = m_scheme.group(1).lower()
    elif "psql" in cleaned.lower()[:40] or cleaned.lower().startswith("pgpassword="):
        scheme = "psql_command_detected (invalid URI)"
    else:
        scheme = "none (missing :// URI scheme)"

    valid_postgres_scheme = cleaned.startswith("postgresql://") or cleaned.startswith("postgres://")
    had_wrapping = bool(raw != cleaned and valid_postgres_scheme)

    return {
        "present": present,
        "length": len(cleaned),
        "scheme": scheme,
        "valid_postgres_scheme": valid_postgres_scheme,
        "normalized_wrapping": had_wrapping,
    }


class LatestReleasesDB:
    """
    Unified database manager for Latest Releases.
    Supports Render PostgreSQL (via DATABASE_URL) with seamless SQLite fallback for offline local development.
    """

    def __init__(
        self,
        database_url: Optional[str] = None,
        sqlite_path: Optional[str] = None,
        require_postgres: Optional[bool] = None
    ):
        env_db_url = os.environ.get("DATABASE_URL", "")
        raw_input = database_url if database_url is not None else (env_db_url or settings.DATABASE_URL or "")
        self.raw_url = normalize_database_url(raw_input)
        self.require_postgres = (
            require_postgres if require_postgres is not None
            else os.environ.get("REQUIRE_POSTGRES", "").strip().lower() in ("1", "true")
        )
        self.is_postgres = self.raw_url.startswith("postgres://") or self.raw_url.startswith("postgresql://")

        if self.require_postgres and not self.is_postgres:
            diag = describe_database_url(raw_input)
            raise RuntimeError(
                "PostgreSQL is required (REQUIRE_POSTGRES=1), but DATABASE_URL is missing or not a "
                f"PostgreSQL connection string (present={diag['present']}, scheme={diag['scheme']!r}, length={diag['length']})."
            )

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
                connect_kwargs = {}
                if "sslmode=" not in self.pg_url.lower():
                    hostname = (urllib.parse.urlparse(self.pg_url).hostname or "").lower()
                    if hostname and hostname not in ("localhost", "127.0.0.1") and "." in hostname:
                        connect_kwargs["sslmode"] = "require"
                return psycopg2.connect(self.pg_url, **connect_kwargs)
            except Exception as e:
                if self.require_postgres:
                    raise RuntimeError(f"PostgreSQL connection failed ({type(e).__name__}). Refusing SQLite fallback.") from None
                # Fallback to SQLite only when PostgreSQL is not strictly required
                print(f"[LatestReleasesDB] PostgreSQL connection failed ({type(e).__name__}), falling back to SQLite.")
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
                    pages_checked INT DEFAULT 1,
                    pages_requested INT,
                    pages_successfully_checked INT,
                    pages_unavailable INT,
                    albums_discovered INT,
                    albums_inspected INT,
                    resolved_notifications_count INT DEFAULT 0,
                    failed_pages TEXT,
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
                    language TEXT DEFAULT 'Telugu',
                    language_confidence VARCHAR(32) DEFAULT 'UNKNOWN',
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
                    language TEXT DEFAULT 'Telugu',
                    resolution_status VARCHAR(32) DEFAULT 'PENDING',
                    source_url TEXT,
                    missing_count INT DEFAULT 0,
                    is_read {bool_type} DEFAULT {'FALSE' if self.is_postgres else '0'},
                    created_at DOUBLE PRECISION NOT NULL,
                    updated_at DOUBLE PRECISION NOT NULL,
                    details_json TEXT,
                    dedup_hash TEXT UNIQUE
                );
            """)

            self._migrate_schema(cur)
            conn.commit()
        finally:
            conn.close()

    def _migrate_schema(self, cur):
        """
        Idempotent schema migration to add new columns to existing tables without data loss.
        Works across PostgreSQL and SQLite.
        """
        def column_exists(table: str, col: str) -> bool:
            if self.is_postgres:
                cur.execute("""
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name = %s AND column_name = %s;
                """, (table.lower(), col.lower()))
                return cur.fetchone() is not None
            else:
                cur.execute(f"PRAGMA table_info({table});")
                cols = [r[1].lower() for r in cur.fetchall()]
                return col.lower() in cols

        bool_default = "BOOLEAN DEFAULT FALSE" if self.is_postgres else "INTEGER DEFAULT 0"
        migrations = [
            ("discovered_albums", "language", "TEXT DEFAULT 'Telugu'"),
            ("discovered_albums", "language_confidence", "VARCHAR(32) DEFAULT 'UNKNOWN'"),
            ("notifications", "language", "TEXT DEFAULT 'Telugu'"),
            ("notifications", "resolution_status", "VARCHAR(32) DEFAULT 'PENDING'"),
            ("discovery_scans", "pages_checked", "INT DEFAULT 1"),
            ("discovery_scans", "pages_requested", "INT"),
            ("discovery_scans", "pages_successfully_checked", "INT"),
            ("discovery_scans", "pages_unavailable", "INT"),
            ("discovery_scans", "albums_discovered", "INT"),
            ("discovery_scans", "albums_inspected", "INT"),
            ("discovery_scans", "resolved_notifications_count", "INT DEFAULT 0"),
            ("discovery_scans", "failed_pages", "TEXT"),
            ("discovered_songs", "singer", "TEXT"),
            ("discovered_songs", "available_bitrate", "TEXT"),
            ("discovered_songs", "download_links_json", "TEXT"),
            ("discovered_songs", "link_available", bool_default),
            ("discovered_songs", "download_authorized", bool_default),
            ("discovered_songs", "match_classification", "VARCHAR(64) DEFAULT 'MISSING'"),
            ("discovered_songs", "match_reason", "TEXT"),
        ]

        for table, col, col_def in migrations:
            try:
                if not column_exists(table, col):
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def};")
            except Exception as e:
                # Log and continue if already exists or race condition
                print(f"[LatestReleasesDB] Column migration notice ({table}.{col}): {e}")

    # =========================================================================
    # Distributed Scan Locking (Multi-Process & Render Cron Safe)
    # =========================================================================

    def acquire_scan_lock(self, locked_by: str = "cron", timeout_minutes: int = 15) -> bool:
        """
        Attempt to acquire distributed scan lock atomically.
        Returns True if acquired, False if an active scan is already running.
        """
        now = time.time()
        expires_at = now + (timeout_minutes * 60)
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            if self.is_postgres:
                cur.execute("""
                    INSERT INTO latest_releases_lock (lock_name, locked_at, locked_by, expires_at)
                    VALUES ('global_scan', %s, %s, %s)
                    ON CONFLICT (lock_name) DO UPDATE
                    SET locked_at = EXCLUDED.locked_at,
                        locked_by = EXCLUDED.locked_by,
                        expires_at = EXCLUDED.expires_at
                    WHERE latest_releases_lock.expires_at IS NULL OR latest_releases_lock.expires_at <= %s;
                """, (now, locked_by, expires_at, now))
                acquired = (cur.rowcount == 1)
                conn.commit()
                return acquired

            cur.execute("SELECT lock_name, locked_at, locked_by, expires_at FROM latest_releases_lock WHERE lock_name = 'global_scan';")
            row = cur.fetchone()

            if row:
                r_expires_at = row[3] if isinstance(row, tuple) else row["expires_at"]
                if r_expires_at and now < r_expires_at:
                    # Scan is actively running and has not expired
                    return False
                # Lock expired, take over
                cur.execute("""
                    UPDATE latest_releases_lock
                    SET locked_at = ?, locked_by = ?, expires_at = ?
                    WHERE lock_name = 'global_scan';
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

    def record_scan_complete(
        self,
        scan_id: int,
        albums_found: int,
        new_albums: int,
        new_songs: int,
        pages_checked: int = 1,
        pages_requested: Optional[int] = None,
        pages_successfully_checked: Optional[int] = None,
        pages_unavailable: Optional[int] = None,
        albums_discovered: Optional[int] = None,
        albums_inspected: Optional[int] = None,
        resolved_notifications: int = 0,
        failed_pages: Optional[List[str]] = None,
        error_message: Optional[str] = None
    ):
        now = time.time()
        status_str = "FAILED" if error_message else "COMPLETED"
        failed_json = json.dumps(failed_pages or [])
        eff_pages_success = pages_checked if pages_successfully_checked is None else pages_successfully_checked
        eff_pages_req = pages_checked if pages_requested is None else pages_requested
        eff_pages_unavail = max(0, eff_pages_req - eff_pages_success) if pages_unavailable is None else pages_unavailable
        eff_albums_disc = albums_found if albums_discovered is None else albums_discovered
        eff_albums_insp = albums_found if albums_inspected is None else albums_inspected

        conn = self._get_connection()
        try:
            cur = conn.cursor()
            if self.is_postgres:
                cur.execute("""
                    UPDATE discovery_scans
                    SET completed_at = %s, status = %s, albums_found = %s, new_albums_count = %s, new_songs_count = %s,
                        pages_checked = %s, pages_requested = %s, pages_successfully_checked = %s,
                        pages_unavailable = %s, albums_discovered = %s, albums_inspected = %s,
                        resolved_notifications_count = %s, failed_pages = %s, error_message = %s
                    WHERE id = %s;
                """, (
                    now, status_str, albums_found, new_albums, new_songs,
                    eff_pages_success, eff_pages_req, eff_pages_success,
                    eff_pages_unavail, eff_albums_disc, eff_albums_insp,
                    resolved_notifications, failed_json, error_message, scan_id
                ))
            else:
                cur.execute("""
                    UPDATE discovery_scans
                    SET completed_at = ?, status = ?, albums_found = ?, new_albums_count = ?, new_songs_count = ?,
                        pages_checked = ?, pages_requested = ?, pages_successfully_checked = ?,
                        pages_unavailable = ?, albums_discovered = ?, albums_inspected = ?,
                        resolved_notifications_count = ?, failed_pages = ?, error_message = ?
                    WHERE id = ?;
                """, (
                    now, status_str, albums_found, new_albums, new_songs,
                    eff_pages_success, eff_pages_req, eff_pages_success,
                    eff_pages_unavail, eff_albums_disc, eff_albums_insp,
                    resolved_notifications, failed_json, error_message, scan_id
                ))
            conn.commit()
        finally:
            conn.close()

    def get_recent_scans(self, limit: int = 15) -> List[Dict[str, Any]]:
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            cur.execute(f"""
                SELECT id, started_at, completed_at, status, albums_found,
                       new_albums_count, new_songs_count, pages_checked,
                       resolved_notifications_count, failed_pages, error_message, locked_by,
                       pages_requested, pages_successfully_checked, pages_unavailable,
                       albums_discovered, albums_inspected
                FROM discovery_scans
                ORDER BY started_at DESC LIMIT {int(limit)};
            """)
            rows = cur.fetchall()
            col_names = [desc[0].lower() for desc in (cur.description or [])]
            results = []
            for r in rows:
                if isinstance(r, tuple) and col_names:
                    d = dict(zip(col_names, r))
                else:
                    d = {k.lower(): v for k, v in dict(r).items()}

                try:
                    d["failed_pages"] = json.loads(d.get("failed_pages") or "[]")
                except Exception:
                    d["failed_pages"] = []

                pages_chk = d.get("pages_checked")
                if pages_chk is None:
                    pages_chk = 0
                d["pages_checked"] = pages_chk
                d["resolved_notifications_count"] = d.get("resolved_notifications_count") or 0

                # Backfill legacy rows (where new columns are NULL) without fabricating false counts
                if d.get("pages_successfully_checked") is None:
                    d["pages_successfully_checked"] = pages_chk
                if d.get("pages_unavailable") is None:
                    d["pages_unavailable"] = len(d["failed_pages"])
                if d.get("pages_requested") is None:
                    d["pages_requested"] = d["pages_successfully_checked"] + d["pages_unavailable"]
                if d.get("albums_discovered") is None:
                    d["albums_discovered"] = d.get("albums_found") or 0
                if d.get("albums_inspected") is None:
                    d["albums_inspected"] = d.get("albums_found") or 0

                results.append(d)
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

            lang = album.language or "Telugu"
            lang_conf = album.language_confidence.value if hasattr(album.language_confidence, 'value') else str(album.language_confidence or "UNKNOWN")

            if row:
                album_id = row[0]
                if self.is_postgres:
                    cur.execute("""
                        UPDATE discovered_albums
                        SET album_name = %s, normalized_name = %s, music_director = %s, year = %s,
                            language = %s, language_confidence = %s,
                            director = %s, cast_crew = %s, artwork_url = %s, exists_in_catalog = %s,
                            catalog_album_name = %s, tracking_status = %s, last_checked_at = %s
                        WHERE id = %s;
                    """, (album.album_name, album.normalized_name, album.music_director, album.year,
                          lang, lang_conf,
                          album.director, album.cast_crew, album.artwork_url, album.exists_in_catalog,
                          album.catalog_album_name, album.tracking_status.value, now, album_id))
                else:
                    cur.execute("""
                        UPDATE discovered_albums
                        SET album_name = ?, normalized_name = ?, music_director = ?, year = ?,
                            language = ?, language_confidence = ?,
                            director = ?, cast_crew = ?, artwork_url = ?, exists_in_catalog = ?,
                            catalog_album_name = ?, tracking_status = ?, last_checked_at = ?
                        WHERE id = ?;
                    """, (album.album_name, album.normalized_name, album.music_director, album.year,
                          lang, lang_conf,
                          album.director, album.cast_crew, album.artwork_url, 1 if album.exists_in_catalog else 0,
                          album.catalog_album_name, album.tracking_status.value, now, album_id))
            else:
                if self.is_postgres:
                    cur.execute("""
                        INSERT INTO discovered_albums (
                            source_url, album_name, normalized_name, music_director, year,
                            language, language_confidence,
                            director, cast_crew, artwork_url, exists_in_catalog, catalog_album_name,
                            tracking_status, first_discovered_at, last_checked_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id;
                    """, (album.source_url, album.album_name, album.normalized_name, album.music_director,
                          album.year, lang, lang_conf, album.director, album.cast_crew, album.artwork_url, album.exists_in_catalog,
                          album.catalog_album_name, album.tracking_status.value, now, now))
                    album_id = cur.fetchone()[0]
                else:
                    cur.execute("""
                        INSERT INTO discovered_albums (
                            source_url, album_name, normalized_name, music_director, year,
                            language, language_confidence,
                            director, cast_crew, artwork_url, exists_in_catalog, catalog_album_name,
                            tracking_status, first_discovered_at, last_checked_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """, (album.source_url, album.album_name, album.normalized_name, album.music_director,
                          album.year, lang, lang_conf, album.director, album.cast_crew, album.artwork_url, 1 if album.exists_in_catalog else 0,
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

            dl_links_json = json.dumps(getattr(song, "download_links", {}) or {})
            cls_val = song.match_classification.value if hasattr(getattr(song, "match_classification", None), "value") else str(getattr(song, "match_classification", "MISSING"))
            singer_val = getattr(song, "singer", None)
            bitrate_val = getattr(song, "available_bitrate", None)
            link_avail = bool(getattr(song, "link_available", False) or song.source_audio_url)
            dl_auth = bool(getattr(song, "download_authorized", False))
            reason_val = getattr(song, "match_reason", None)

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
                            singer = %s, available_bitrate = %s, download_links_json = %s,
                            link_available = %s, download_authorized = %s,
                            match_classification = %s, match_reason = %s,
                            last_checked_at = %s
                        WHERE id = %s;
                    """, (song.song_title, song.track_number, song.source_audio_url,
                          song.exists_in_catalog, song.catalog_song_title, song.catalog_song_id,
                          song.match_confidence.value, song.match_score, effective_status,
                          singer_val, bitrate_val, dl_links_json, link_avail, dl_auth,
                          cls_val, reason_val, now, song_id))
                else:
                    cur.execute("""
                        UPDATE discovered_songs
                        SET song_title = ?, track_number = ?, source_audio_url = ?,
                            exists_in_catalog = ?, catalog_song_title = ?, catalog_song_id = ?,
                            match_confidence = ?, match_score = ?, tracking_status = ?,
                            singer = ?, available_bitrate = ?, download_links_json = ?,
                            link_available = ?, download_authorized = ?,
                            match_classification = ?, match_reason = ?,
                            last_checked_at = ?
                        WHERE id = ?;
                    """, (song.song_title, song.track_number, song.source_audio_url,
                          1 if song.exists_in_catalog else 0, song.catalog_song_title, song.catalog_song_id,
                          song.match_confidence.value, song.match_score, effective_status,
                          singer_val, bitrate_val, dl_links_json, 1 if link_avail else 0, 1 if dl_auth else 0,
                          cls_val, reason_val, now, song_id))
            else:
                if self.is_postgres:
                    cur.execute("""
                        INSERT INTO discovered_songs (
                            album_id, song_title, normalized_title, track_number, source_audio_url,
                            exists_in_catalog, catalog_song_title, catalog_song_id, match_confidence,
                            match_score, tracking_status, singer, available_bitrate, download_links_json,
                            link_available, download_authorized, match_classification, match_reason,
                            first_discovered_at, last_checked_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id;
                    """, (album_id, song.song_title, song.normalized_title, song.track_number,
                          song.source_audio_url, song.exists_in_catalog, song.catalog_song_title,
                          song.catalog_song_id, song.match_confidence.value, song.match_score,
                          song.tracking_status.value, singer_val, bitrate_val, dl_links_json,
                          link_avail, dl_auth, cls_val, reason_val, now, now))
                    song_id = cur.fetchone()[0]
                else:
                    cur.execute("""
                        INSERT INTO discovered_songs (
                            album_id, song_title, normalized_title, track_number, source_audio_url,
                            exists_in_catalog, catalog_song_title, catalog_song_id, match_confidence,
                            match_score, tracking_status, singer, available_bitrate, download_links_json,
                            link_available, download_authorized, match_classification, match_reason,
                            first_discovered_at, last_checked_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """, (album_id, song.song_title, song.normalized_title, song.track_number,
                          song.source_audio_url, 1 if song.exists_in_catalog else 0, song.catalog_song_title,
                          song.catalog_song_id, song.match_confidence.value, song.match_score,
                          song.tracking_status.value, singer_val, bitrate_val, dl_links_json,
                          1 if link_avail else 0, 1 if dl_auth else 0, cls_val, reason_val, now, now))
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
        missing_count: int,
        language: str = "Telugu",
        resolution_status: str = "PENDING"
    ) -> bool:
        """
        Creates a notification. If an identical notification hash exists, updates timestamp without creating duplicate.
        Returns True if a new notification was inserted, False if deduplicated.
        """
        now = time.time()
        # Compute deduplication hash based on album, language, notification type, and list of missing song titles
        missing_titles = sorted([s.get("song_title", "") for s in songs_list if not s.get("exists_in_catalog")])
        hash_input = f"{album_name}:{language}:{notification_type.value}:{','.join(missing_titles)}"
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
                        SET updated_at = %s, details_json = %s, missing_count = %s,
                            language = %s, resolution_status = %s
                        WHERE id = %s;
                    """, (now, details_json, missing_count, language, resolution_status, notif_id))
                else:
                    cur.execute("""
                        UPDATE notifications
                        SET updated_at = ?, details_json = ?, missing_count = ?,
                            language = ?, resolution_status = ?
                        WHERE id = ?;
                    """, (now, details_json, missing_count, language, resolution_status, notif_id))
                conn.commit()
                return False

            # Insert new notification
            if self.is_postgres:
                cur.execute("""
                    INSERT INTO notifications (
                        album_id, notification_type, title, message, album_name, artwork_url,
                        music_director, year, language, resolution_status, source_url, missing_count, is_read, created_at,
                        updated_at, details_json, dedup_hash
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, FALSE, %s, %s, %s, %s);
                """, (album_id, notification_type.value, title, message, album_name, artwork_url,
                      music_director, year, language, resolution_status, source_url, missing_count, now, now, details_json, dedup_hash))
            else:
                cur.execute("""
                    INSERT INTO notifications (
                        album_id, notification_type, title, message, album_name, artwork_url,
                        music_director, year, language, resolution_status, source_url, missing_count, is_read, created_at,
                        updated_at, details_json, dedup_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?);
                """, (album_id, notification_type.value, title, message, album_name, artwork_url,
                      music_director, year, language, resolution_status, source_url, missing_count, now, now, details_json, dedup_hash))

            conn.commit()
            return True
        finally:
            conn.close()

    def get_notifications(
        self,
        category: Optional[str] = None,
        language: Optional[str] = None,
        status: Optional[str] = "active",
        unread_only: bool = False,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            conditions = []
            params = []

            # 1. Category Filter
            if category and category.lower() != "all":
                cat_upper = category.upper()
                if cat_upper == "UNCERTAIN":
                    if self.is_postgres:
                        conditions.append("details_json LIKE %s")
                    else:
                        conditions.append("details_json LIKE ?")
                    params.append('%"match_confidence": "UNCERTAIN"%')
                elif cat_upper in ("NEW_ALBUM", "NEW_SONG", "MISSING_SONGS", "ALBUM_UPDATED"):
                    if self.is_postgres:
                        conditions.append("notification_type = %s")
                    else:
                        conditions.append("notification_type = ?")
                    params.append(cat_upper)

            # 2. Language Filter
            if language and language.lower() != "all":
                if self.is_postgres:
                    conditions.append("LOWER(COALESCE(language, 'telugu')) = LOWER(%s)")
                else:
                    conditions.append("LOWER(COALESCE(language, 'telugu')) = LOWER(?)")
                params.append(language)

            # 3. Status Filter (active vs completed vs all)
            if status:
                status_lower = status.lower()
                if status_lower == "active":
                    conditions.append("COALESCE(resolution_status, 'PENDING') IN ('PENDING', 'PARTIALLY_COMPLETED')")
                elif status_lower == "completed":
                    conditions.append("COALESCE(resolution_status, 'PENDING') = 'COMPLETED'")
                elif status_lower in ("pending", "partially_completed", "ignored"):
                    if self.is_postgres:
                        conditions.append("COALESCE(resolution_status, 'PENDING') = %s")
                    else:
                        conditions.append("COALESCE(resolution_status, 'PENDING') = ?")
                    params.append(status.upper())

            # 4. Unread only
            if unread_only:
                if self.is_postgres:
                    conditions.append("is_read = FALSE")
                else:
                    conditions.append("is_read = 0")

            where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            query = f"""
                SELECT id, album_id, notification_type, title, message, album_name, artwork_url,
                       music_director, year, source_url, missing_count, is_read, created_at,
                       updated_at, details_json, dedup_hash, language, resolution_status
                FROM notifications
                {where_clause}
                ORDER BY created_at DESC LIMIT {limit};
            """

            cur.execute(query, tuple(params) if params else ())
            rows = cur.fetchall()
            results = []
            for r in rows:
                if isinstance(r, tuple):
                    d = {
                        "id": r[0], "album_id": r[1], "notification_type": r[2], "title": r[3],
                        "message": r[4], "album_name": r[5], "artwork_url": r[6], "music_director": r[7],
                        "year": r[8], "source_url": r[9], "missing_count": r[10], "is_read": bool(r[11]),
                        "created_at": r[12], "updated_at": r[13], "details_json": r[14], "dedup_hash": r[15],
                        "language": r[16] if len(r) > 16 and r[16] else "Telugu",
                        "resolution_status": r[17] if len(r) > 17 and r[17] else "PENDING"
                    }
                else:
                    d = dict(r)
                    d["is_read"] = bool(d.get("is_read"))
                    d["language"] = d.get("language") or "Telugu"
                    d["resolution_status"] = d.get("resolution_status") or "PENDING"

                try:
                    d["songs"] = json.loads(d.get("details_json") or "[]")
                except Exception:
                    d["songs"] = []
                results.append(d)
            return results
        finally:
            conn.close()

    def update_notification_resolution(
        self,
        notification_id: int,
        status: str,
        updated_songs: Optional[List[Dict[str, Any]]] = None,
        missing_count: Optional[int] = None
    ):
        now = time.time()
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            set_clauses = ["resolution_status = %s" if self.is_postgres else "resolution_status = ?",
                           "updated_at = %s" if self.is_postgres else "updated_at = ?"]
            params = [status, now]

            if updated_songs is not None:
                set_clauses.append("details_json = %s" if self.is_postgres else "details_json = ?")
                params.append(json.dumps(updated_songs))

            if missing_count is not None:
                set_clauses.append("missing_count = %s" if self.is_postgres else "missing_count = ?")
                params.append(missing_count)

            params.append(notification_id)
            query = f"UPDATE notifications SET {', '.join(set_clauses)} WHERE id = {'%s' if self.is_postgres else '?'};"
            cur.execute(query, tuple(params))
            conn.commit()
        finally:
            conn.close()

    def get_active_notifications(self, limit: int = 100) -> List[Dict[str, Any]]:
        return self.get_notifications(status="active", limit=limit)

    def get_unread_count(self) -> int:
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            if self.is_postgres:
                query = "SELECT COUNT(*) FROM notifications WHERE is_read = FALSE AND COALESCE(resolution_status, 'PENDING') IN ('PENDING', 'PARTIALLY_COMPLETED');"
            else:
                query = "SELECT COUNT(*) FROM notifications WHERE is_read = 0 AND COALESCE(resolution_status, 'PENDING') IN ('PENDING', 'PARTIALLY_COMPLETED');"
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
