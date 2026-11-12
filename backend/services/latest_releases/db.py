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


def compute_canonical_album_key(
    album_name: Optional[str],
    language: Optional[str] = "Telugu",
) -> Tuple[str, str, str]:
    """
    Computes a language-scoped canonical identity key for an album:
    Returns `(canonical_key, clean_album_display, normalized_language)`.
    Example: ('Adhurs - Telugu', 'Taelugu') -> ('adhurs::telugu', 'Adhurs', 'Telugu')
    """
    from backend.services.catalog import CloudCatalogService

    raw_name = (album_name or "").strip()
    clean_display, emb_lang = CloudCatalogService.strip_language_and_meta(raw_name)
    display_name = clean_display or raw_name
    norm_lang = CloudCatalogService.normalize_language_name(language or emb_lang) or "Telugu"
    norm_album = re.sub(r'[^a-z0-9]', '', display_name.lower())
    if not norm_album:
        norm_album = re.sub(r'[^a-z0-9]', '', raw_name.lower()) or "unknown"
    return f"{norm_album}::{norm_lang.lower()}", display_name, norm_lang


def song_identity_key(song_title: Optional[str]) -> Tuple[str, Tuple[str, ...]]:
    """
    Returns canonical version-aware song key `(base_core, version_tags)` so formatting
    differences collapse while genuinely distinct versions (Promo vs Full) remain separate.
    """
    from backend.services.catalog import CloudCatalogService

    comp = CloudCatalogService.parse_song_title_components(song_title or "")
    base = comp.get("base_core") or comp.get("raw_alnum") or ""
    return (base, tuple(comp.get("version_tags") or ()))


def _merge_single_song_dict(existing: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    """Merges two song dicts representing the same canonical song identity."""
    merged = dict(existing)

    # Prefer cleaner/longer display title without weird file artifacts if available
    inc_title = (incoming.get("song_title") or "").strip()
    ext_title = (existing.get("song_title") or "").strip()
    if inc_title and (not ext_title or len(inc_title) > len(ext_title)):
        merged["song_title"] = inc_title

    # Never downgrade catalog verification once verified in MusicData
    ext_exists = bool(existing.get("exists_in_catalog"))
    inc_exists = bool(incoming.get("exists_in_catalog"))
    merged["exists_in_catalog"] = ext_exists or inc_exists

    if inc_exists and not ext_exists:
        for k in (
            "match_confidence",
            "match_classification",
            "match_score",
            "match_reason",
            "catalog_song_title",
            "catalog_song_id",
            "catalog_album_name",
            "tracking_status",
        ):
            if incoming.get(k) is not None:
                merged[k] = incoming[k]
    elif ext_exists:
        # Keep catalog match fields from existing verified entry; fill any blanks from incoming
        for k in (
            "match_confidence",
            "match_classification",
            "match_score",
            "match_reason",
            "catalog_song_title",
            "catalog_song_id",
            "catalog_album_name",
            "tracking_status",
        ):
            if not merged.get(k) and incoming.get(k) is not None:
                merged[k] = incoming[k]
    else:
        # Both not in catalog: preserve version/uncertain diagnostics if incoming has richer detail
        inc_cls = str(incoming.get("match_classification") or "")
        ext_cls = str(existing.get("match_classification") or "")
        if inc_cls == "POSSIBLE_VERSION_MATCH" or (not ext_cls and inc_cls):
            for k in (
                "match_confidence",
                "match_classification",
                "match_score",
                "match_reason",
                "catalog_version_candidates",
                "tracking_status",
            ):
                if incoming.get(k) is not None:
                    merged[k] = incoming[k]

    # Merge singer, URL, and bitrate/download links
    if incoming.get("singer") and (not merged.get("singer") or str(merged.get("singer")).lower() == "unknown"):
        merged["singer"] = incoming["singer"]
    if incoming.get("source_audio_url") and not merged.get("source_audio_url"):
        merged["source_audio_url"] = incoming["source_audio_url"]
    if incoming.get("source_page_url") and not merged.get("source_page_url"):
        merged["source_page_url"] = incoming["source_page_url"]
    if incoming.get("available_bitrate") and not merged.get("available_bitrate"):
        merged["available_bitrate"] = incoming["available_bitrate"]

    ext_links = dict(existing.get("download_links") or {})
    inc_links = dict(incoming.get("download_links") or {})
    if inc_links:
        ext_links.update({k: v for k, v in inc_links.items() if v})
    if ext_links:
        merged["download_links"] = ext_links
        merged["available_bitrates"] = sorted(
            set(list(existing.get("available_bitrates") or []) + list(incoming.get("available_bitrates") or []) + list(ext_links.keys()))
        )

    merged["link_available"] = bool(existing.get("link_available") or incoming.get("link_available") or merged.get("source_audio_url"))
    merged["download_authorized"] = bool(existing.get("download_authorized") or incoming.get("download_authorized"))
    return merged


def deduplicate_and_merge_song_dicts(
    existing_songs: Optional[List[Dict[str, Any]]],
    incoming_songs: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """
    Deduplicates and merges song payload lists by version-aware canonical song identity,
    preserving distinct versions (e.g. Promo vs Full) while collapsing identical tracks.
    """
    ordered_keys: List[Tuple[str, Tuple[str, ...]]] = []
    by_key: Dict[Tuple[str, Tuple[str, ...]], Dict[str, Any]] = {}

    for item in (existing_songs or []) + (incoming_songs or []):
        if not isinstance(item, dict):
            continue
        title = item.get("song_title") or ""
        if not title:
            continue
        key = song_identity_key(title)
        if not key[0]:
            key = (title.strip().lower(), ())
        if key in by_key:
            by_key[key] = _merge_single_song_dict(by_key[key], item)
        else:
            ordered_keys.append(key)
            by_key[key] = dict(item)

    return [by_key[k] for k in ordered_keys]


def build_notification_summary_text(
    notification_type: NotificationType,
    album_name: str,
    language: str,
    songs_list: List[Dict[str, Any]],
    music_director: str = "Unknown",
    year: int = 2026,
) -> Tuple[str, str]:
    """Builds consistent notification title and message from current missing/version state."""
    lang_badge = f"[{language}] " if language else ""
    missing_songs = [s for s in songs_list if not s.get("exists_in_catalog")]
    version_verify_songs = [
        s for s in missing_songs
        if str(s.get("match_classification") or "") == "POSSIBLE_VERSION_MATCH"
        or str(s.get("match_confidence") or "") == MatchConfidence.UNCERTAIN.value
    ]
    genuine_missing_songs = [s for s in missing_songs if s not in version_verify_songs]

    if notification_type == NotificationType.NEW_ALBUM:
        title = f"New Album: {album_name}"
        message = f"{lang_badge}{len(songs_list)} songs available • Music by {music_director} ({year})"
        return title, message

    if notification_type == NotificationType.NEW_SONG:
        missing_title = (missing_songs[0].get("song_title") if missing_songs else None) or "New Track"
        title = f"New Song: {album_name}"
        if version_verify_songs:
            message = f"{lang_badge}'{missing_title}' needs version verification against MusicData"
        else:
            message = f"{lang_badge}'{missing_title}' released • Missing from MusicData"
        return title, message

    if notification_type == NotificationType.MISSING_SONGS:
        title = f"Missing Songs: {album_name}"
        missing_count = len(missing_songs)
        if version_verify_songs and genuine_missing_songs:
            names_snippet = ", ".join([s.get("song_title", "") for s in genuine_missing_songs[:2]])
            if len(genuine_missing_songs) > 2:
                names_snippet += f" and {len(genuine_missing_songs) - 2} more"
            message = f"{lang_badge}{len(genuine_missing_songs)} missing ({names_snippet}) • {len(version_verify_songs)} version match to verify"
        elif version_verify_songs and not genuine_missing_songs:
            message = f"{lang_badge}{len(version_verify_songs)} track(s) need version verification"
        else:
            names_snippet = ", ".join([s.get("song_title", "") for s in missing_songs[:2]])
            if len(missing_songs) > 2:
                names_snippet += f" and {len(missing_songs) - 2} more"
            message = f"{lang_badge}{missing_count} songs missing ({names_snippet})"
        return title, message

    if notification_type == NotificationType.ALBUM_UPDATED:
        return f"Album Updated: {album_name}", f"{lang_badge}New artwork or updated tracklist discovered."

    return f"Catalog Update: {album_name}", "New details discovered."


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
                    source_page INT,
                    source_position INT,
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
                    source_page INT,
                    source_position INT,
                    missing_count INT DEFAULT 0,
                    is_read {bool_type} DEFAULT {'FALSE' if self.is_postgres else '0'},
                    created_at DOUBLE PRECISION NOT NULL,
                    updated_at DOUBLE PRECISION NOT NULL,
                    details_json TEXT,
                    dedup_hash TEXT UNIQUE,
                    canonical_album_key VARCHAR(255)
                );
            """)

            # 6. Direct-to-MusicData Background Sync Jobs Table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS musicdata_sync_jobs (
                    job_id VARCHAR(64) PRIMARY KEY,
                    idempotency_key VARCHAR(128) UNIQUE,
                    parent_job_id VARCHAR(64),
                    status VARCHAR(32) NOT NULL,
                    created_at DOUBLE PRECISION NOT NULL,
                    updated_at DOUBLE PRECISION NOT NULL,
                    completed_at DOUBLE PRECISION,
                    request_json TEXT NOT NULL,
                    progress_json TEXT NOT NULL,
                    result_json TEXT,
                    error_message TEXT
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
            ("discovered_albums", "source_page", "INT"),
            ("discovered_albums", "source_position", "INT"),
            ("notifications", "language", "TEXT DEFAULT 'Telugu'"),
            ("notifications", "resolution_status", "VARCHAR(32) DEFAULT 'PENDING'"),
            ("notifications", "source_page", "INT"),
            ("notifications", "source_position", "INT"),
            ("notifications", "canonical_album_key", "VARCHAR(255)"),
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
            ("musicdata_sync_jobs", "parent_job_id", "VARCHAR(64)"),
        ]

        for table, col, col_def in migrations:
            try:
                if not column_exists(table, col):
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def};")
            except Exception as e:
                # Log and continue if already exists or race condition
                print(f"[LatestReleasesDB] Column migration notice ({table}.{col}): {e}")

    # =========================================================================
    # Distributed Locking (Scan Lock + Per-Album Sync Locks)
    # =========================================================================

    @staticmethod
    def _bound_lock_name(lock_name: str) -> str:
        raw = (lock_name or "global_scan").strip()
        if len(raw) <= 64:
            return raw
        prefix = raw[:15]
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:48]
        return f"{prefix}{digest}"

    def acquire_named_lock(self, lock_name: str, locked_by: str = "cron", timeout_minutes: int = 15) -> bool:
        """
        Attempt to acquire a named distributed lock atomically.
        Returns True if acquired, False if an active non-expired lock is held.
        """
        safe_lock_name = self._bound_lock_name(lock_name)
        now = time.time()
        expires_at = now + (timeout_minutes * 60)
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            if self.is_postgres:
                cur.execute("""
                    INSERT INTO latest_releases_lock (lock_name, locked_at, locked_by, expires_at)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (lock_name) DO UPDATE
                    SET locked_at = EXCLUDED.locked_at,
                        locked_by = EXCLUDED.locked_by,
                        expires_at = EXCLUDED.expires_at
                    WHERE latest_releases_lock.expires_at IS NULL OR latest_releases_lock.expires_at <= %s;
                """, (safe_lock_name, now, locked_by, expires_at, now))
                acquired = (cur.rowcount == 1)
                conn.commit()
                return acquired

            cur.execute(
                "SELECT lock_name, locked_at, locked_by, expires_at FROM latest_releases_lock WHERE lock_name = ?;",
                (safe_lock_name,)
            )
            row = cur.fetchone()

            if row:
                r_expires_at = row[3] if isinstance(row, tuple) else row["expires_at"]
                if r_expires_at and now < r_expires_at:
                    return False
                cur.execute("""
                    UPDATE latest_releases_lock
                    SET locked_at = ?, locked_by = ?, expires_at = ?
                    WHERE lock_name = ?;
                """, (now, locked_by, expires_at, safe_lock_name))
            else:
                cur.execute("""
                    INSERT INTO latest_releases_lock (lock_name, locked_at, locked_by, expires_at)
                    VALUES (?, ?, ?, ?);
                """, (safe_lock_name, now, locked_by, expires_at))

            conn.commit()
            return True
        finally:
            conn.close()

    def release_named_lock(self, lock_name: str, locked_by: Optional[str] = None):
        """Release a named distributed lock."""
        safe_lock_name = self._bound_lock_name(lock_name)
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            if locked_by:
                if self.is_postgres:
                    cur.execute(
                        "DELETE FROM latest_releases_lock WHERE lock_name = %s AND locked_by = %s;",
                        (safe_lock_name, locked_by)
                    )
                else:
                    cur.execute(
                        "DELETE FROM latest_releases_lock WHERE lock_name = ? AND locked_by = ?;",
                        (safe_lock_name, locked_by)
                    )
            else:
                if self.is_postgres:
                    cur.execute("DELETE FROM latest_releases_lock WHERE lock_name = %s;", (safe_lock_name,))
                else:
                    cur.execute("DELETE FROM latest_releases_lock WHERE lock_name = ?;", (safe_lock_name,))
            conn.commit()
        finally:
            conn.close()

    def get_named_lock_status(self, lock_name: str) -> Dict[str, Any]:
        safe_lock_name = self._bound_lock_name(lock_name)
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            if self.is_postgres:
                cur.execute(
                    "SELECT lock_name, locked_at, locked_by, expires_at FROM latest_releases_lock WHERE lock_name = %s;",
                    (safe_lock_name,)
                )
            else:
                cur.execute(
                    "SELECT lock_name, locked_at, locked_by, expires_at FROM latest_releases_lock WHERE lock_name = ?;",
                    (safe_lock_name,)
                )
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

    def acquire_scan_lock(self, locked_by: str = "cron", timeout_minutes: int = 15) -> bool:
        """
        Attempt to acquire distributed scan lock atomically.
        Returns True if acquired, False if an active scan is already running.
        """
        return self.acquire_named_lock("global_scan", locked_by=locked_by, timeout_minutes=timeout_minutes)

    def release_scan_lock(self, locked_by: Optional[str] = None):
        """Release distributed scan lock."""
        self.release_named_lock("global_scan", locked_by=locked_by)

    def get_lock_status(self) -> Dict[str, Any]:
        return self.get_named_lock_status("global_scan")

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
            src_page = getattr(album, "source_page", None)
            src_pos = getattr(album, "source_position", None)

            if row:
                album_id = row[0]
                if self.is_postgres:
                    cur.execute("""
                        UPDATE discovered_albums
                        SET album_name = %s, normalized_name = %s, music_director = %s, year = %s,
                            language = %s, language_confidence = %s,
                            director = %s, cast_crew = %s, artwork_url = %s, exists_in_catalog = %s,
                            catalog_album_name = %s, tracking_status = %s,
                            source_page = COALESCE(%s, source_page),
                            source_position = COALESCE(%s, source_position),
                            last_checked_at = %s
                        WHERE id = %s;
                    """, (album.album_name, album.normalized_name, album.music_director, album.year,
                          lang, lang_conf,
                          album.director, album.cast_crew, album.artwork_url, album.exists_in_catalog,
                          album.catalog_album_name, album.tracking_status.value, src_page, src_pos, now, album_id))
                else:
                    cur.execute("""
                        UPDATE discovered_albums
                        SET album_name = ?, normalized_name = ?, music_director = ?, year = ?,
                            language = ?, language_confidence = ?,
                            director = ?, cast_crew = ?, artwork_url = ?, exists_in_catalog = ?,
                            catalog_album_name = ?, tracking_status = ?,
                            source_page = COALESCE(?, source_page),
                            source_position = COALESCE(?, source_position),
                            last_checked_at = ?
                        WHERE id = ?;
                    """, (album.album_name, album.normalized_name, album.music_director, album.year,
                          lang, lang_conf,
                          album.director, album.cast_crew, album.artwork_url, 1 if album.exists_in_catalog else 0,
                          album.catalog_album_name, album.tracking_status.value, src_page, src_pos, now, album_id))
            else:
                if self.is_postgres:
                    cur.execute("""
                        INSERT INTO discovered_albums (
                            source_url, album_name, normalized_name, music_director, year,
                            language, language_confidence,
                            director, cast_crew, artwork_url, exists_in_catalog, catalog_album_name,
                            tracking_status, source_page, source_position, first_discovered_at, last_checked_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id;
                    """, (album.source_url, album.album_name, album.normalized_name, album.music_director,
                          album.year, lang, lang_conf, album.director, album.cast_crew, album.artwork_url, album.exists_in_catalog,
                          album.catalog_album_name, album.tracking_status.value, src_page, src_pos, now, now))
                    album_id = cur.fetchone()[0]
                else:
                    cur.execute("""
                        INSERT INTO discovered_albums (
                            source_url, album_name, normalized_name, music_director, year,
                            language, language_confidence,
                            director, cast_crew, artwork_url, exists_in_catalog, catalog_album_name,
                            tracking_status, source_page, source_position, first_discovered_at, last_checked_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """, (album.source_url, album.album_name, album.normalized_name, album.music_director,
                          album.year, lang, lang_conf, album.director, album.cast_crew, album.artwork_url, 1 if album.exists_in_catalog else 0,
                          album.catalog_album_name, album.tracking_status.value, src_page, src_pos, now, now))
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
        resolution_status: str = "PENDING",
        source_page: Optional[int] = None,
        source_position: Optional[int] = None
    ) -> bool:
        """
        Creates a notification.
        - Normalizes album language and canonical `(clean_album, language)` key.
        - Deduplicates identical hashes and preserves COMPLETED status without resurrecting resolved rows.
        - Consolidates any active duplicate notifications sharing the same canonical `(album, language)`
          identity into a single active row, preserving earliest chronological position and read state.
        Returns True if a new notification was inserted, False if deduplicated/consolidated.
        """
        now = time.time()
        ph = "%s" if self.is_postgres else "?"
        canonical_key, clean_display_name, norm_lang = compute_canonical_album_key(album_name, language)
        clean_songs = deduplicate_and_merge_song_dicts([], songs_list)

        missing_titles = sorted([s.get("song_title", "") for s in clean_songs if not s.get("exists_in_catalog")])
        hash_input = f"{clean_display_name}:{norm_lang}:{notification_type.value}:{','.join(missing_titles)}"
        dedup_hash = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()
        # Backward-compatible legacy hash check so pre-migration rows also dedup cleanly
        legacy_hash_input = f"{album_name}:{language}:{notification_type.value}:{','.join(missing_titles)}"
        legacy_dedup_hash = hashlib.sha256(legacy_hash_input.encode("utf-8")).hexdigest()
        details_json = json.dumps(clean_songs)

        conn = self._get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                f"SELECT id, resolution_status, details_json, missing_count, source_url FROM notifications "
                f"WHERE dedup_hash = {ph} OR dedup_hash = {ph};",
                (dedup_hash, legacy_dedup_hash),
            )
            existing = cur.fetchone()

            if existing:
                notif_id = existing[0]
                existing_status = (existing[1] or "PENDING").upper()
                if existing_status == "COMPLETED":
                    cur.execute(
                        f"UPDATE notifications SET updated_at = {ph}, canonical_album_key = COALESCE(canonical_album_key, {ph}) WHERE id = {ph};",
                        (now, canonical_key, notif_id),
                    )
                    conn.commit()
                    return False

                try:
                    existing_songs = json.loads(existing[2] or "[]")
                except Exception:
                    existing_songs = []
                merged_songs = deduplicate_and_merge_song_dicts(existing_songs, clean_songs)
                merged_missing = len([s for s in merged_songs if not s.get("exists_in_catalog")]) if merged_songs else missing_count
                next_status = existing_status if existing_status == "PARTIALLY_COMPLETED" else resolution_status
                if merged_songs and merged_missing == 0 and notification_type != NotificationType.ALBUM_UPDATED:
                    next_status = "COMPLETED"

                cur.execute(f"""
                    UPDATE notifications
                    SET updated_at = {ph}, details_json = {ph}, missing_count = {ph},
                        language = {ph}, canonical_album_key = {ph}, resolution_status = {ph},
                        source_page = COALESCE({ph}, source_page),
                        source_position = COALESCE({ph}, source_position)
                    WHERE id = {ph};
                """, (now, json.dumps(merged_songs), merged_missing, norm_lang, canonical_key, next_status, source_page, source_position, notif_id))
                if next_status == "COMPLETED":
                    cur.execute(
                        f"UPDATE notifications SET is_read = {'TRUE' if self.is_postgres else '1'} WHERE id = {ph};",
                        (notif_id,),
                    )
                conn.commit()
                return False

            # Canonical consolidation check across active & completed rows
            cur.execute(
                f"""
                SELECT id, notification_type, album_name, language, resolution_status,
                       source_url, source_page, source_position, missing_count, is_read,
                       details_json, music_director, year, artwork_url
                FROM notifications
                WHERE COALESCE(canonical_album_key, '') = {ph}
                   OR canonical_album_key IS NULL
                   OR COALESCE(resolution_status, 'PENDING') IN ('PENDING', 'PARTIALLY_COMPLETED');
                """,
                (canonical_key,),
            )
            candidates = cur.fetchall()
            active_matches = []
            completed_matches = []
            for row in candidates:
                r_key, _, _ = compute_canonical_album_key(row[2], row[3])
                if r_key != canonical_key:
                    continue
                r_status = (row[4] or "PENDING").upper()
                if r_status in ("PENDING", "PARTIALLY_COMPLETED"):
                    active_matches.append(row)
                elif r_status == "COMPLETED":
                    completed_matches.append(row)

            # Never recreate notification if every incoming missing song was already verified COMPLETED
            if not active_matches and completed_matches and notification_type != NotificationType.ALBUM_UPDATED:
                verified_completed_keys = set()
                for c_row in completed_matches:
                    try:
                        c_songs = json.loads(c_row[10] or "[]")
                    except Exception:
                        c_songs = []
                    for cs in c_songs:
                        if cs.get("exists_in_catalog") and cs.get("song_title"):
                            verified_completed_keys.add(song_identity_key(cs["song_title"]))
                incoming_missing_list = [s for s in clean_songs if not s.get("exists_in_catalog")]
                if incoming_missing_list and all(
                    song_identity_key(s.get("song_title")) in verified_completed_keys
                    for s in incoming_missing_list
                ):
                    return False

            if active_matches:
                # Choose surviving active notification row (earliest page/position, then partial status, then specific type, then lowest id)
                def _row_sort_key(r):
                    pg = r[6] if r[6] is not None else 999999
                    ps = r[7] if r[7] is not None else 999999
                    st_rank = 0 if (r[4] or "").upper() == "PARTIALLY_COMPLETED" else 1
                    tp_rank = 0 if r[1] in (NotificationType.NEW_SONG.value, NotificationType.MISSING_SONGS.value) else 1
                    return (pg, ps, st_rank, tp_rank, r[0])

                active_matches.sort(key=_row_sort_key)
                survivor = active_matches[0]
                survivor_id = survivor[0]

                merged_songs: List[Dict[str, Any]] = []
                any_partial = (resolution_status or "").upper() == "PARTIALLY_COMPLETED"
                any_song_level_type = notification_type in (NotificationType.NEW_SONG, NotificationType.MISSING_SONGS)
                page_pos_candidates = []
                if source_page is not None:
                    page_pos_candidates.append((source_page, source_position if source_position is not None else 999999, source_position))

                for m in active_matches:
                    if (m[4] or "").upper() == "PARTIALLY_COMPLETED":
                        any_partial = True
                    if m[1] in (NotificationType.NEW_SONG.value, NotificationType.MISSING_SONGS.value):
                        any_song_level_type = True
                    if m[6] is not None:
                        page_pos_candidates.append((m[6], m[7] if m[7] is not None else 999999, m[7]))
                    try:
                        m_songs = json.loads(m[10] or "[]")
                    except Exception:
                        m_songs = []
                    merged_songs = deduplicate_and_merge_song_dicts(merged_songs, m_songs)

                merged_songs = deduplicate_and_merge_song_dicts(merged_songs, clean_songs)
                missing_list = [s for s in merged_songs if not s.get("exists_in_catalog")]
                new_missing_count = len(missing_list) if merged_songs else missing_count

                # Mark duplicate active rows COMPLETED + read so bell list never has >1 active row per (album, language)
                for dup_row in active_matches[1:]:
                    cur.execute(
                        f"UPDATE notifications SET resolution_status = 'COMPLETED', is_read = {'TRUE' if self.is_postgres else '1'}, updated_at = {ph} WHERE id = {ph};",
                        (now, dup_row[0]),
                    )

                any_catalog_song = any(bool(s.get("exists_in_catalog")) for s in merged_songs)
                if any_catalog_song or notification_type in (NotificationType.NEW_SONG, NotificationType.MISSING_SONGS):
                    effective_type = NotificationType.NEW_SONG if new_missing_count <= 1 else NotificationType.MISSING_SONGS
                else:
                    effective_type = notification_type

                if merged_songs and new_missing_count == 0 and effective_type != NotificationType.ALBUM_UPDATED:
                    effective_status = "COMPLETED"
                elif any_partial:
                    effective_status = "PARTIALLY_COMPLETED"
                else:
                    effective_status = "PENDING"

                if page_pos_candidates:
                    best_cand = min(page_pos_candidates, key=lambda x: (x[0], x[1]))
                    best_page, best_pos = best_cand[0], best_cand[2]
                else:
                    best_page, best_pos = survivor[6], survivor[7]

                eff_director = music_director if (music_director and music_director != "Unknown") else (survivor[11] or "Unknown")
                eff_year = year or survivor[12] or 2026
                eff_artwork = artwork_url or survivor[13]
                eff_source_url = survivor[5] or source_url
                if source_url and (not eff_source_url or len(source_url) > len(eff_source_url)):
                    eff_source_url = source_url

                new_title, new_message = build_notification_summary_text(
                    effective_type,
                    clean_display_name,
                    norm_lang,
                    merged_songs,
                    music_director=eff_director,
                    year=eff_year,
                )

                new_missing_titles = sorted([s.get("song_title", "") for s in missing_list])
                candidate_hash = hashlib.sha256(
                    f"{clean_display_name}:{norm_lang}:{effective_type.value}:{','.join(new_missing_titles)}".encode("utf-8")
                ).hexdigest()
                cur.execute(f"SELECT id FROM notifications WHERE dedup_hash = {ph} AND id != {ph};", (candidate_hash, survivor_id))
                if cur.fetchone():
                    candidate_hash = f"{candidate_hash}:{survivor_id}"

                if self.is_postgres:
                    cur.execute("""
                        UPDATE notifications
                        SET notification_type = %s, title = %s, message = %s, album_name = %s,
                            artwork_url = %s, music_director = %s, year = %s, language = %s,
                            canonical_album_key = %s, resolution_status = %s, source_url = %s,
                            source_page = %s, source_position = %s, missing_count = %s,
                            details_json = %s, dedup_hash = %s, updated_at = %s
                        WHERE id = %s;
                    """, (
                        effective_type.value, new_title, new_message, clean_display_name,
                        eff_artwork, eff_director, eff_year, norm_lang,
                        canonical_key, effective_status, eff_source_url,
                        best_page, best_pos, new_missing_count,
                        json.dumps(merged_songs), candidate_hash, now, survivor_id
                    ))
                else:
                    cur.execute("""
                        UPDATE notifications
                        SET notification_type = ?, title = ?, message = ?, album_name = ?,
                            artwork_url = ?, music_director = ?, year = ?, language = ?,
                            canonical_album_key = ?, resolution_status = ?, source_url = ?,
                            source_page = ?, source_position = ?, missing_count = ?,
                            details_json = ?, dedup_hash = ?, updated_at = ?
                        WHERE id = ?;
                    """, (
                        effective_type.value, new_title, new_message, clean_display_name,
                        eff_artwork, eff_director, eff_year, norm_lang,
                        canonical_key, effective_status, eff_source_url,
                        best_page, best_pos, new_missing_count,
                        json.dumps(merged_songs), candidate_hash, now, survivor_id
                    ))
                if effective_status == "COMPLETED":
                    cur.execute(
                        f"UPDATE notifications SET is_read = {'TRUE' if self.is_postgres else '1'} WHERE id = {ph};",
                        (survivor_id,),
                    )
                conn.commit()
                return False

            # Insert new notification
            if self.is_postgres:
                cur.execute("""
                    INSERT INTO notifications (
                        album_id, notification_type, title, message, album_name, artwork_url,
                        music_director, year, language, resolution_status, source_url, source_page, source_position,
                        missing_count, is_read, created_at, updated_at, details_json, dedup_hash, canonical_album_key
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, FALSE, %s, %s, %s, %s, %s);
                """, (album_id, notification_type.value, title, message, album_name, artwork_url,
                      music_director, year, norm_lang, resolution_status, source_url, source_page, source_position,
                      missing_count, now, now, details_json, dedup_hash, canonical_key))
            else:
                cur.execute("""
                    INSERT INTO notifications (
                        album_id, notification_type, title, message, album_name, artwork_url,
                        music_director, year, language, resolution_status, source_url, source_page, source_position,
                        missing_count, is_read, created_at, updated_at, details_json, dedup_hash, canonical_album_key
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?);
                """, (album_id, notification_type.value, title, message, album_name, artwork_url,
                      music_director, year, norm_lang, resolution_status, source_url, source_page, source_position,
                      missing_count, now, now, details_json, dedup_hash, canonical_key))

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
        limit: int = 200
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
                       updated_at, details_json, dedup_hash, language, resolution_status,
                       source_page, source_position, canonical_album_key
                FROM notifications
                {where_clause}
                ORDER BY
                    CASE WHEN source_page IS NULL THEN 1 ELSE 0 END ASC,
                    source_page ASC,
                    CASE WHEN source_position IS NULL THEN 1 ELSE 0 END ASC,
                    source_position ASC,
                    created_at DESC,
                    id ASC
                LIMIT {int(limit)};
            """

            cur.execute(query, tuple(params) if params else ())
            rows = cur.fetchall()
            col_names = [desc[0].lower() for desc in (cur.description or [])]
            results = []
            for r in rows:
                if isinstance(r, tuple) and col_names:
                    d = dict(zip(col_names, r))
                else:
                    d = {k.lower(): v for k, v in dict(r).items()}

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
        missing_count: Optional[int] = None,
        notification_type: Optional[str] = None,
        title: Optional[str] = None,
        message: Optional[str] = None,
        language: Optional[str] = None,
        canonical_album_key: Optional[str] = None,
        is_read: Optional[bool] = None,
    ):
        now = time.time()
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            ph = "%s" if self.is_postgres else "?"
            set_clauses = [f"resolution_status = {ph}", f"updated_at = {ph}"]
            params: List[Any] = [status, now]

            if updated_songs is not None:
                set_clauses.append(f"details_json = {ph}")
                params.append(json.dumps(updated_songs))

            if missing_count is not None:
                set_clauses.append(f"missing_count = {ph}")
                params.append(missing_count)

            if notification_type is not None:
                set_clauses.append(f"notification_type = {ph}")
                params.append(notification_type)

            if title is not None:
                set_clauses.append(f"title = {ph}")
                params.append(title)

            if message is not None:
                set_clauses.append(f"message = {ph}")
                params.append(message)

            if language is not None:
                set_clauses.append(f"language = {ph}")
                params.append(language)

            if canonical_album_key is not None:
                set_clauses.append(f"canonical_album_key = {ph}")
                params.append(canonical_album_key)

            if is_read is not None:
                set_clauses.append(f"is_read = {ph}")
                params.append(bool(is_read) if self.is_postgres else (1 if is_read else 0))

            params.append(notification_id)
            query = f"UPDATE notifications SET {', '.join(set_clauses)} WHERE id = {ph};"
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

    def get_notifications_by_ids(self, notification_ids: List[int]) -> List[Dict[str, Any]]:
        """Fetch notifications by IDs preserving caller order and decoding details_json."""
        clean_ids: List[int] = []
        seen = set()
        for raw_id in notification_ids or []:
            try:
                nid = int(raw_id)
            except (TypeError, ValueError):
                continue
            if nid > 0 and nid not in seen:
                seen.add(nid)
                clean_ids.append(nid)
        if not clean_ids:
            return []

        conn = self._get_connection()
        try:
            cur = conn.cursor()
            ph = "%s" if self.is_postgres else "?"
            placeholders = ", ".join([ph] * len(clean_ids))
            query = f"""
                SELECT id, album_id, notification_type, title, message, album_name, artwork_url,
                       music_director, year, source_url, missing_count, is_read, created_at,
                       updated_at, details_json, dedup_hash, language, resolution_status,
                       source_page, source_position, canonical_album_key
                FROM notifications
                WHERE id IN ({placeholders});
            """
            cur.execute(query, tuple(clean_ids))
            rows = cur.fetchall()
            col_names = [desc[0].lower() for desc in (cur.description or [])]
            by_id: Dict[int, Dict[str, Any]] = {}
            for r in rows:
                if isinstance(r, tuple) and col_names:
                    d = dict(zip(col_names, r))
                else:
                    d = {k.lower(): v for k, v in dict(r).items()}
                d["is_read"] = bool(d.get("is_read"))
                d["language"] = d.get("language") or "Telugu"
                d["resolution_status"] = d.get("resolution_status") or "PENDING"
                try:
                    d["songs"] = json.loads(d.get("details_json") or "[]")
                except Exception:
                    d["songs"] = []
                by_id[int(d["id"])] = d
            return [by_id[nid] for nid in clean_ids if nid in by_id]
        finally:
            conn.close()

    def get_notification_by_id(self, notification_id: int) -> Optional[Dict[str, Any]]:
        rows = self.get_notifications_by_ids([notification_id])
        return rows[0] if rows else None

    # =========================================================================
    # Direct-to-MusicData Background Sync Job Persistence
    # =========================================================================

    @staticmethod
    def _decode_sync_job_row(row_dict: Dict[str, Any]) -> Dict[str, Any]:
        for src_col, dest_key, default_val in (
            ("request_json", "request", {}),
            ("progress_json", "progress", {}),
            ("result_json", "result", None),
        ):
            raw = row_dict.get(src_col)
            if raw:
                try:
                    row_dict[dest_key] = json.loads(raw)
                except Exception:
                    row_dict[dest_key] = default_val
            else:
                row_dict[dest_key] = default_val
        return row_dict

    def get_sync_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            ph = "%s" if self.is_postgres else "?"
            cur.execute(f"""
                SELECT job_id, idempotency_key, parent_job_id, status, created_at, updated_at,
                       completed_at, request_json, progress_json, result_json, error_message
                FROM musicdata_sync_jobs
                WHERE job_id = {ph};
            """, (job_id,))
            row = cur.fetchone()
            if not row:
                return None
            col_names = [desc[0].lower() for desc in (cur.description or [])]
            d = dict(zip(col_names, row)) if isinstance(row, tuple) and col_names else {k.lower(): v for k, v in dict(row).items()}
            return self._decode_sync_job_row(d)
        finally:
            conn.close()

    def get_sync_job_by_idempotency(self, idempotency_key: str) -> Optional[Dict[str, Any]]:
        if not idempotency_key:
            return None
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            ph = "%s" if self.is_postgres else "?"
            cur.execute(f"""
                SELECT job_id, idempotency_key, parent_job_id, status, created_at, updated_at,
                       completed_at, request_json, progress_json, result_json, error_message
                FROM musicdata_sync_jobs
                WHERE idempotency_key = {ph};
            """, (idempotency_key,))
            row = cur.fetchone()
            if not row:
                return None
            col_names = [desc[0].lower() for desc in (cur.description or [])]
            d = dict(zip(col_names, row)) if isinstance(row, tuple) and col_names else {k.lower(): v for k, v in dict(row).items()}
            return self._decode_sync_job_row(d)
        finally:
            conn.close()

    def create_sync_job(
        self,
        job_id: str,
        idempotency_key: Optional[str],
        status: str,
        request_data: Dict[str, Any],
        progress_data: Dict[str, Any],
        parent_job_id: Optional[str] = None
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        Persist a new Add-to-MusicData sync job or return existing idempotent job.
        Returns (created_new: bool, job_dict).
        """
        if idempotency_key:
            existing = self.get_sync_job_by_idempotency(idempotency_key)
            if existing:
                return False, existing

        now = time.time()
        req_json = json.dumps(request_data or {})
        prog_json = json.dumps(progress_data or {})
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            if self.is_postgres:
                try:
                    cur.execute("""
                        INSERT INTO musicdata_sync_jobs (
                            job_id, idempotency_key, parent_job_id, status,
                            created_at, updated_at, completed_at,
                            request_json, progress_json, result_json, error_message
                        ) VALUES (%s, %s, %s, %s, %s, %s, NULL, %s, %s, NULL, NULL);
                    """, (job_id, idempotency_key, parent_job_id, status, now, now, req_json, prog_json))
                    conn.commit()
                except Exception:
                    conn.rollback()
                    if idempotency_key:
                        existing = self.get_sync_job_by_idempotency(idempotency_key)
                        if existing:
                            return False, existing
                    raise
            else:
                try:
                    cur.execute("""
                        INSERT INTO musicdata_sync_jobs (
                            job_id, idempotency_key, parent_job_id, status,
                            created_at, updated_at, completed_at,
                            request_json, progress_json, result_json, error_message
                        ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL, NULL);
                    """, (job_id, idempotency_key, parent_job_id, status, now, now, req_json, prog_json))
                    conn.commit()
                except sqlite3.IntegrityError:
                    if idempotency_key:
                        existing = self.get_sync_job_by_idempotency(idempotency_key)
                        if existing:
                            return False, existing
                    raise
        finally:
            conn.close()

        created = self.get_sync_job(job_id)
        return True, created or {"job_id": job_id, "status": status}

    def update_sync_job(
        self,
        job_id: str,
        status: str,
        progress_data: Dict[str, Any],
        result_data: Optional[Dict[str, Any]] = None,
        error_message: Optional[str] = None,
        completed_at: Optional[float] = None
    ):
        now = time.time()
        prog_json = json.dumps(progress_data or {})
        res_json = json.dumps(result_data) if result_data is not None else None
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            if self.is_postgres:
                cur.execute("""
                    UPDATE musicdata_sync_jobs
                    SET status = %s, updated_at = %s, completed_at = COALESCE(%s, completed_at),
                        progress_json = %s, result_json = COALESCE(%s, result_json),
                        error_message = %s
                    WHERE job_id = %s;
                """, (status, now, completed_at, prog_json, res_json, error_message, job_id))
            else:
                cur.execute("""
                    UPDATE musicdata_sync_jobs
                    SET status = ?, updated_at = ?, completed_at = COALESCE(?, completed_at),
                        progress_json = ?, result_json = COALESCE(?, result_json),
                        error_message = ?
                    WHERE job_id = ?;
                """, (status, now, completed_at, prog_json, res_json, error_message, job_id))
            conn.commit()
        finally:
            conn.close()

    def list_recent_sync_jobs(self, limit: int = 20) -> List[Dict[str, Any]]:
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            cur.execute(f"""
                SELECT job_id, idempotency_key, parent_job_id, status, created_at, updated_at,
                       completed_at, request_json, progress_json, result_json, error_message
                FROM musicdata_sync_jobs
                ORDER BY created_at DESC LIMIT {int(limit)};
            """)
            rows = cur.fetchall()
            col_names = [desc[0].lower() for desc in (cur.description or [])]
            results = []
            for r in rows:
                d = dict(zip(col_names, r)) if isinstance(r, tuple) and col_names else {k.lower(): v for k, v in dict(r).items()}
                results.append(self._decode_sync_job_row(d))
            return results
        finally:
            conn.close()

_db_instance: Optional[LatestReleasesDB] = None

def get_latest_releases_db() -> LatestReleasesDB:
    global _db_instance
    if _db_instance is None:
        _db_instance = LatestReleasesDB()
    return _db_instance
