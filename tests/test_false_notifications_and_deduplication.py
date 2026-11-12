import json
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.services.catalog import CatalogLookupError, CloudCatalogService
from backend.services.latest_releases.album_parser import AlbumParser
from backend.services.latest_releases.db import (
    LatestReleasesDB,
    compute_canonical_album_key,
    deduplicate_and_merge_song_dicts,
)
from backend.services.latest_releases.discovery import LatestReleasesDiscoveryEngine
from backend.services.latest_releases.models import (
    DiscoveredAlbum,
    DiscoveredSong,
    MatchConfidence,
    NotificationType,
)
from backend.services.latest_releases.musicdata_uploader import (
    compute_song_key,
    selective_reconcile_notification,
)
from backend.services.latest_releases.reconciliation import reconcile_active_notifications
from backend.services.latest_releases.song_comparator import SongComparator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(tmp_path: Path) -> LatestReleasesDB:
    db_file = tmp_path / "test_latest_releases.db"
    return LatestReleasesDB(sqlite_path=str(db_file))


def _insert_dummy_album(db: LatestReleasesDB, name: str, url: str, language: str = "Telugu", year: int = 2026) -> int:
    alb = DiscoveredAlbum(
        album_name=name,
        normalized_name=name.lower(),
        source_url=url,
        language=language,
        year=year,
        music_director="Test Director",
    )
    return db.upsert_discovered_album(alb)


# ---------------------------------------------------------------------------
# 1. Catalog Matching & Normalization Regression Tests
# ---------------------------------------------------------------------------

def test_parse_song_title_strips_dot_mp_artifacts():
    """Real catalog .Mp.mp3 stems (Adhurs/Andhrawala/Badri/Yamadonga) match cleanly."""
    cases = [
        ("Assalaam Valekhum Song", "Assalaam Valekhum.Mp"),
        ("Nairey Nairey Song", "Nairey Nairey.Mp"),
        ("Bangala Kathamlo Song", "Bangala Kathamlo.Mp"),
        ("Yey Chikitha Song", "Yey Chikitha.Mp"),
        ("Naachore Song", "Naachore.Mp"),
    ]
    for src_title, cat_title in cases:
        res = CloudCatalogService.match_song_detailed(
            src_title,
            [{"id": "s1", "title": cat_title, "audio": f"{cat_title}.mp3"}],
        )
        assert res["exists_in_catalog"] is True, f"Expected exact match for {src_title} vs {cat_title}"
        assert res["classification"] == "EXACT_MATCH"
        assert res["score"] == 1.0


def test_find_canonical_album_ignores_mistagged_year_when_unique_match():
    """Unique exact album + language match succeeds even when year differs (Andhrawala 2004 vs 2026)."""
    svc = CloudCatalogService()
    catalog = [
        {"name": "Andhrawala", "language": "Telugu", "year": 2004},
        {"name": "Yamadonga", "language": "Telugu", "year": 2007},
        {"name": "Adhurs", "language": "Telugu", "year": 2010},
    ]
    match_andhra = svc.find_canonical_album("Andhrawala", query_language="Telugu", query_year=2026, candidate_albums=catalog)
    assert match_andhra is not None
    assert match_andhra["name"] == "Andhrawala"

    match_yama = svc.find_canonical_album("Yamadonga", query_language="Telugu", query_year=2016, candidate_albums=catalog)
    assert match_yama is not None
    assert match_yama["name"] == "Yamadonga"


def test_language_typo_healing_and_language_separation():
    """'Taelugu' heals to 'Telugu' while Telugu vs Tamil albums remain strictly separate."""
    assert CloudCatalogService.normalize_language_name("Taelugu") == "Telugu"

    clean_title, lang, conf = AlbumParser.extract_language_and_clean_title(
        "<div class='entry-content'><p>Language:- Taelugu</p></div>",
        "https://naasongs.com.co/srinivasa-mangapuram-songs.html",
        "Srinivasa Mangapuram Songs",
    )
    assert lang == "Telugu"
    assert clean_title == "Srinivasa Mangapuram"

    svc = CloudCatalogService()
    catalog = [
        {"name": "OM - Telugu", "language": "Telugu", "year": 2024},
        {"name": "OM - Tamil", "language": "Tamil", "year": 2024},
    ]
    m_te = svc.find_canonical_album("OM", query_language="Taelugu", query_year=2024, candidate_albums=catalog)
    m_ta = svc.find_canonical_album("OM", query_language="Tamil", query_year=2024, candidate_albums=catalog)
    assert m_te["name"] == "OM - Telugu"
    assert m_ta["name"] == "OM - Tamil"


def test_promo_vs_full_remains_possible_version_match():
    """Ranabaali: Full version vs existing Promo stays POSSIBLE_VERSION_MATCH and missing."""
    res = CloudCatalogService.match_song_detailed(
        "Endhayya Saami Song (Full)",
        [{"id": "p1", "title": "Endhayya Saami Song (Promo)", "audio": "Endhayya Saami Song (Promo).mp3"}],
    )
    assert res["exists_in_catalog"] is False
    assert res["classification"] == "POSSIBLE_VERSION_MATCH"
    assert res["confidence"] == "UNCERTAIN"


def test_catalog_lookup_failure_never_creates_false_new_album_or_resolves(tmp_path):
    """CatalogLookupError preserves active notifications and comparator marks UNCERTAIN."""
    db = _make_db(tmp_path)
    alb_id = _insert_dummy_album(db, "Catalog Error Album", "https://naasongs.com.co/cat-err.html")
    db.create_or_update_notification(
        album_id=alb_id,
        notification_type=NotificationType.NEW_SONG,
        title="New Song: Catalog Error Album",
        message="[Telugu] 'Track 1' released",
        album_name="Catalog Error Album",
        artwork_url=None,
        music_director="Dir",
        year=2026,
        source_url="https://naasongs.com.co/cat-err.html",
        songs_list=[{"song_title": "Track 1", "exists_in_catalog": False}],
        missing_count=1,
        language="Telugu",
    )

    class FailingComparator(SongComparator):
        def find_matching_catalog_album(self, album_name, language=None, year=None):
            raise CatalogLookupError("GitHub 503", reason="http_error", status_code=503)

    summary = reconcile_active_notifications(db=db, comparator=FailingComparator())
    assert summary["deferred_on_catalog_error"] == 1
    assert summary["resolved_notifications"] == 0
    active = db.get_active_notifications()
    assert len(active) == 1


# ---------------------------------------------------------------------------
# 2. Duplicate Prevention (Parser, Discovery Consolidation, DB Consolidation)
# ---------------------------------------------------------------------------

def test_album_parser_deduplicates_identical_tracks_within_page_preserving_versions():
    """Same song listed twice in slightly different text collapses, Promo vs Full stays distinct."""
    html = """
    <div class="entry-content">
      <p>01. Endhayya Saami Song (Promo) - <a href="https://naasongs.com.co/dl/endhayya-promo.mp3">Download</a></p>
      <p>02. Endhayya Saami Song (Full) - <a href="https://naasongs.com.co/dl/endhayya-full.mp3">Download</a></p>
      <p>03. Kobali Song - <a href="https://naasongs.com.co/dl/kobali-128.mp3">128 Kbps</a></p>
      <p>04. Kobali.mp3 - <a href="https://naasongs.com.co/dl/kobali-320.mp3">320 Kbps</a></p>
    </div>
    """
    songs = AlbumParser._extract_songs(html, "https://naasongs.com.co/ranabaali.html")
    titles = [s.song_title for s in songs]
    assert len(songs) == 3, f"Expected 3 tracks (Promo, Full, merged Kobali), got: {titles}"
    kobali = [s for s in songs if "kobali" in s.song_title.lower()][0]
    assert "kobali-320.mp3" in kobali.source_audio_url


def test_discovery_consolidates_duplicate_album_posts_across_pages_and_urls():
    """Alias Rukmini appearing on Page 1 (album post) and Page 4 (single-song post) consolidates into Page 1."""
    a1 = DiscoveredAlbum(
        album_name="Alias Rukmini",
        normalized_name="alias rukmini",
        source_url="https://naasongs.com.co/alias-rukmini-2026-songs-1.html",
        language="Telugu",
        year=2026,
        music_director="Vijai Bulganin",
        source_page=1,
        source_position=2,
        songs=[DiscoveredSong(song_title="Ruku Ruku Rukmini Song", normalized_title="ruku ruku rukmini", track_number=1)],
    )
    a2 = DiscoveredAlbum(
        album_name="Alias Rukmini - Telugu",
        normalized_name="alias rukmini",
        source_url="https://naasongs.com.co/pichhi-premalo-song-alias-rukmini.html",
        language="Telugu",
        year=2026,
        music_director="Unknown",
        source_page=4,
        source_position=5,
        songs=[
            DiscoveredSong(song_title="Ruku Ruku Rukmini", normalized_title="ruku ruku rukmini", track_number=1),
            DiscoveredSong(song_title="Pichhi Premalo Song", normalized_title="pichhi premalo", track_number=2),
        ],
    )
    consolidated = LatestReleasesDiscoveryEngine._consolidate_discovered_albums([
        ({"url": a1.source_url, "source_page": 1, "source_position": 2}, a1, None),
        ({"url": a2.source_url, "source_page": 4, "source_position": 5}, a2, None),
    ])
    assert len(consolidated) == 1
    _, merged_alb, err = consolidated[0]
    assert err is None
    assert merged_alb.source_page == 1
    assert merged_alb.source_position == 2
    assert len(merged_alb.songs) == 2
    assert {s.song_title for s in merged_alb.songs} == {"Ruku Ruku Rukmini Song", "Pichhi Premalo Song"}


def test_db_consolidates_cross_type_duplicate_notifications_keeping_earliest_order(tmp_path):
    """NEW_ALBUM + NEW_SONG + MISSING_SONGS for same (album, language) collapse into one active notification."""
    db = _make_db(tmp_path)
    alb_id = _insert_dummy_album(db, "Alias Rukmini", "https://naasongs.com.co/alias-1.html")

    # 1) Legacy NEW_ALBUM at page 4
    db.create_or_update_notification(
        album_id=alb_id,
        notification_type=NotificationType.NEW_ALBUM,
        title="New Album: Alias Rukmini",
        message="[Telugu] 1 songs available",
        album_name="Alias Rukmini",
        artwork_url=None,
        music_director="Vijai Bulganin",
        year=2026,
        source_url="https://naasongs.com.co/alias-p4.html",
        songs_list=[{"song_title": "Pichhi Premalo Song", "exists_in_catalog": False}],
        missing_count=1,
        language="Telugu",
        source_page=4,
        source_position=3,
    )
    # 2) Rescan discovers Ruku exists in catalog & album on Page 1 -> NEW_SONG
    db.create_or_update_notification(
        album_id=alb_id,
        notification_type=NotificationType.NEW_SONG,
        title="New Song: Alias Rukmini",
        message="[Telugu] 'Pichhi Premalo Song' released",
        album_name="Alias Rukmini - Telugu",
        artwork_url=None,
        music_director="Vijai Bulganin",
        year=2026,
        source_url="https://naasongs.com.co/alias-p1.html",
        songs_list=[
            {"song_title": "Ruku Ruku Rukmini Song", "exists_in_catalog": True},
            {"song_title": "Pichhi Premalo Song", "exists_in_catalog": False},
        ],
        missing_count=1,
        language="Taelugu",
        source_page=1,
        source_position=2,
    )

    active = db.get_active_notifications()
    assert len(active) == 1
    assert active[0]["notification_type"] == "NEW_SONG"
    assert active[0]["language"] == "Telugu"
    assert active[0]["source_page"] == 1
    assert active[0]["source_position"] == 2
    assert active[0]["missing_count"] == 1


def test_db_keeps_distinct_languages_separate(tmp_path):
    """Telugu and Tamil notifications for the same album title remain separate."""
    db = _make_db(tmp_path)
    alb_te = _insert_dummy_album(db, "Coolie", "https://naasongs.com.co/coolie-te.html", language="Telugu")
    alb_ta = _insert_dummy_album(db, "Coolie", "https://naasongs.com.co/coolie-ta.html", language="Tamil")

    db.create_or_update_notification(
        album_id=alb_te,
        notification_type=NotificationType.NEW_ALBUM,
        title="New Album: Coolie",
        message="[Telugu] 1 songs",
        album_name="Coolie",
        artwork_url=None,
        music_director="Anirudh",
        year=2025,
        source_url="https://naasongs.com.co/coolie-te.html",
        songs_list=[{"song_title": "Track A", "exists_in_catalog": False}],
        missing_count=1,
        language="Telugu",
        source_page=1,
        source_position=1,
    )
    db.create_or_update_notification(
        album_id=alb_ta,
        notification_type=NotificationType.NEW_ALBUM,
        title="New Album: Coolie",
        message="[Tamil] 1 songs",
        album_name="Coolie",
        artwork_url=None,
        music_director="Anirudh",
        year=2025,
        source_url="https://naasongs.com.co/coolie-ta.html",
        songs_list=[{"song_title": "Track A", "exists_in_catalog": False}],
        missing_count=1,
        language="Tamil",
        source_page=1,
        source_position=2,
    )
    assert len(db.get_active_notifications()) == 2


# ---------------------------------------------------------------------------
# 3. Reconciliation & Post-Upload Auto-Resolution Tests
# ---------------------------------------------------------------------------

def test_reconcile_resolves_false_mp_positives_and_consolidates_real_duplicates(tmp_path):
    """
    Simulates real production dump defects:
    - Adhurs (2 duplicates: NEW_SONG UNCERTAIN vs .Mp + stale NEW_ALBUM) -> 0 active (both COMPLETED)
    - Alias Rukmini (3 duplicates: NEW_ALBUM + NEW_SONG + stale url) -> 1 active NEW_SONG
    - Srinivasa Mangapuram ('Taelugu' typo) -> healed to 'Telugu'
    """
    db = _make_db(tmp_path)
    adhurs_id = _insert_dummy_album(db, "Adhurs", "https://naasongs.com.co/adhurs.html")
    alias_id = _insert_dummy_album(db, "Alias Rukmini", "https://naasongs.com.co/alias.html")

    # Force insert raw duplicate rows bypassing consolidation to simulate legacy DB state
    conn = db._get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO notifications (album_id, notification_type, title, message, album_name, language, resolution_status, source_url, source_page, source_position, missing_count, is_read, created_at, updated_at, details_json, dedup_hash)
        VALUES (?, 'NEW_SONG', 'New Song: Adhurs', 'uncertain', 'Adhurs', 'Telugu', 'PENDING', 'https://u/adhurs', 5, 1, 1, 0, 100, 100, ?, 'h1'),
               (?, 'NEW_ALBUM', 'New Album: Adhurs', 'stale', 'Adhurs', 'Telugu', 'PENDING', 'https://u/adhurs', 5, 2, 1, 0, 100, 100, ?, 'h2'),
               (?, 'NEW_ALBUM', 'New Album: Alias Rukmini', 'stale1', 'Alias Rukmini', 'Telugu', 'PARTIALLY_COMPLETED', 'https://u/alias1', 1, 2, 1, 0, 100, 100, ?, 'h3'),
               (?, 'NEW_SONG', 'New Song: Alias Rukmini', 'dup2', 'Alias Rukmini', 'Telugu', 'PENDING', 'https://u/alias1', 1, 3, 1, 0, 100, 100, ?, 'h4'),
               (?, 'NEW_ALBUM', 'New Album: Alias Rukmini', 'dup3', 'Alias Rukmini', 'Telugu', 'PENDING', 'https://u/alias-p4', 4, 1, 1, 0, 100, 100, ?, 'h5')
        """,
        (
            adhurs_id, json.dumps([{"song_title": "Assalaam Valekhum Song", "exists_in_catalog": False}]),
            adhurs_id, json.dumps([{"song_title": "Assalaam Valekhum Song", "exists_in_catalog": False}]),
            alias_id, json.dumps([{"song_title": "Ruku Ruku Rukmini Song", "exists_in_catalog": True}, {"song_title": "Pichhi Premalo Song", "exists_in_catalog": False}]),
            alias_id, json.dumps([{"song_title": "Ruku Ruku Rukmini Song", "exists_in_catalog": True}, {"song_title": "Pichhi Premalo Song", "exists_in_catalog": False}]),
            alias_id, json.dumps([{"song_title": "Pichhi Premalo Song", "exists_in_catalog": False}]),
        ),
    )
    conn.commit()
    conn.close()

    class MockCatComparator(SongComparator):
        def find_matching_catalog_album(self, album_name, language=None, year=None):
            if album_name.lower() == "adhurs":
                return {"name": "Adhurs", "language": "Telugu", "year": 2010}
            if "alias" in album_name.lower():
                return {"name": "Alias Rukmini", "language": "Telugu", "year": 2026}
            return None

        def fetch_catalog_album_songs(self, catalog_album):
            if catalog_album["name"] == "Adhurs":
                return [{"id": "c1", "title": "Assalaam Valekhum.Mp", "audio": "Assalaam Valekhum.Mp.mp3"}]
            return [{"id": "c2", "title": "Ruku Ruku Rukmini Song", "audio": "Ruku Ruku Rukmini Song.mp3"}]

    res = reconcile_active_notifications(db=db, comparator=MockCatComparator())
    assert res["consolidated_duplicates"] == 3
    assert res["resolved_notifications"] == 1

    active = db.get_active_notifications()
    assert len(active) == 1
    assert active[0]["album_name"] == "Alias Rukmini"
    assert active[0]["notification_type"] == "NEW_SONG"
    assert active[0]["missing_count"] == 1

    # History preserves COMPLETED Adhurs records
    all_rows = db.get_notifications(status="all", limit=50)
    assert len(all_rows) == 5
    adhurs_rows = [r for r in all_rows if r["album_name"] == "Adhurs"]
    assert all(r["resolution_status"] == "COMPLETED" and r["is_read"] for r in adhurs_rows)


def test_post_upload_selective_reconcile_and_rescan_non_recreation(tmp_path):
    """
    Uploading a missing song marks the primary notification AND duplicate siblings COMPLETED,
    and subsequent rescans do NOT recreate the resolved notification unless a new song/version arrives.
    """
    db = _make_db(tmp_path)
    alb_id = _insert_dummy_album(db, "Reality", "https://naasongs.com.co/reality.html")

    conn = db._get_connection()
    cur = conn.cursor()
    songs_data = [
        {"song_title": "Bhaja Govindam Song", "track_number": 1, "exists_in_catalog": True},
        {"song_title": "Rutthuu Song", "track_number": 2, "exists_in_catalog": False},
    ]
    cur.execute(
        """
        INSERT INTO notifications (album_id, notification_type, title, message, album_name, language, resolution_status, source_url, source_page, source_position, missing_count, is_read, created_at, updated_at, details_json, dedup_hash)
        VALUES (?, 'NEW_SONG', 'New Song: Reality', 'm', 'Reality', 'Telugu', 'PENDING', 'https://u/r1', 1, 1, 1, 0, 100, 100, ?, 'rh1'),
               (?, 'NEW_ALBUM', 'New Album: Reality', 'm', 'Reality', 'Telugu', 'PENDING', 'https://u/r2', 3, 2, 1, 0, 100, 100, ?, 'rh2')
        """,
        (alb_id, json.dumps(songs_data), alb_id, json.dumps([{"song_title": "Rutthuu Song", "track_number": 1, "exists_in_catalog": False}])),
    )
    conn.commit()
    conn.close()

    active_before = db.get_active_notifications()
    primary_id = [r["id"] for r in active_before if r["notification_type"] == "NEW_SONG"][0]
    key = compute_song_key(primary_id, 2, "Rutthuu Song")

    rec_res = selective_reconcile_notification(
        db=db,
        notification_id=primary_id,
        verified_added_song_keys=[key],
        verified_added_filenames=["Rutthuu Song.mp3"],
    )
    assert rec_res["resolution_status"] == "COMPLETED"
    assert rec_res["sibling_reconciled_count"] == 1
    assert len(db.get_active_notifications()) == 0

    # Subsequent scan finding only Rutthuu Song must NOT recreate active notification
    created = db.create_or_update_notification(
        album_id=alb_id,
        notification_type=NotificationType.NEW_SONG,
        title="New Song: Reality",
        message="[Telugu] 'Rutthuu Song' released",
        album_name="Reality",
        artwork_url=None,
        music_director="Bheems",
        year=2026,
        source_url="https://u/r1",
        songs_list=[{"song_title": "Rutthuu Song", "exists_in_catalog": False}],
        missing_count=1,
        language="Telugu",
    )
    assert created is False
    assert len(db.get_active_notifications()) == 0

    # Discovering a genuinely NEW song or DIFFERENT version DOES create a notification
    created_new = db.create_or_update_notification(
        album_id=alb_id,
        notification_type=NotificationType.NEW_SONG,
        title="New Song: Reality",
        message="[Telugu] 'Rutthuu Song (Remix)' released",
        album_name="Reality",
        artwork_url=None,
        music_director="Bheems",
        year=2026,
        source_url="https://u/r3",
        songs_list=[
            {"song_title": "Rutthuu Song", "exists_in_catalog": True},
            {"song_title": "Rutthuu Song (Remix)", "exists_in_catalog": False},
        ],
        missing_count=1,
        language="Telugu",
        source_page=1,
        source_position=1,
    )
    assert created_new is True
    assert len(db.get_active_notifications()) == 1


def test_partial_upload_keeps_remaining_missing_song_and_upgrades_type(tmp_path):
    """Uploading 1 of 2 missing songs upgrades NEW_ALBUM -> NEW_SONG PARTIALLY_COMPLETED."""
    db = _make_db(tmp_path)
    alb_id = _insert_dummy_album(db, "TwoTrack", "https://naasongs.com.co/two.html")
    db.create_or_update_notification(
        album_id=alb_id,
        notification_type=NotificationType.NEW_ALBUM,
        title="New Album: TwoTrack",
        message="[Telugu] 2 songs available",
        album_name="TwoTrack",
        artwork_url=None,
        music_director="Dir",
        year=2026,
        source_url="https://naasongs.com.co/two.html",
        songs_list=[
            {"song_title": "Track One", "track_number": 1, "exists_in_catalog": False},
            {"song_title": "Track Two", "track_number": 2, "exists_in_catalog": False},
        ],
        missing_count=2,
        language="Telugu",
        source_page=2,
        source_position=1,
    )
    notif = db.get_active_notifications()[0]
    key1 = compute_song_key(notif["id"], 1, "Track One")
    res = selective_reconcile_notification(
        db=db,
        notification_id=notif["id"],
        verified_added_song_keys=[key1],
        verified_added_filenames=["Track One.mp3"],
    )
    assert res["resolution_status"] == "PARTIALLY_COMPLETED"
    assert res["remaining_missing_count"] == 1

    updated = db.get_notification_by_id(notif["id"])
    assert updated["notification_type"] == "NEW_SONG"
    assert updated["resolution_status"] == "PARTIALLY_COMPLETED"
    assert updated["missing_count"] == 1


def test_refresh_catalog_clears_stale_cache():
    """refresh_catalog clears cached index/metadata so newly added catalog items are seen."""
    svc = CloudCatalogService()
    svc._albums_index_cache = [{"name": "OldOnly"}]
    svc._album_metadata_cache = {"A/OldOnly": {"songs": []}}
    svc._cache_is_stale = True
    svc.refresh_catalog()
    assert svc._albums_index_cache is None
    assert svc._album_metadata_cache == {}
    assert svc._cache_is_stale is False
    with patch.object(svc, "_fetch_json", return_value=[{"name": "OldOnly"}, {"name": "NewAlbum"}]) as m_fetch:
        idx = svc.get_albums_index()
        assert len(idx) == 2
        m_fetch.assert_called_once_with("metadata/indexes/albums.json")
