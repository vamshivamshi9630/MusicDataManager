import sqlite3
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from backend.main import app
from backend.services.latest_releases.db import LatestReleasesDB
from backend.services.latest_releases.discovery import LatestReleasesDiscoveryEngine
from backend.services.latest_releases.models import NotificationType


def _make_index_html(page_num: int, count: int = 3, max_page: int = 20) -> str:
    links = "".join(
        f'<a href="https://naasongs.com.co/p{page_num}-a{pos}.html">Album P{page_num} A{pos}</a>'
        for pos in range(1, count + 1)
    )
    nxt = (
        f'<a class="next page-numbers" href="https://naasongs.com.co/page/{page_num + 1}">Next</a>'
        if page_num < max_page
        else ""
    )
    return f"""
    <html>
      <div class="entry-content">{links}</div>
      <div class="nav-links">
        <span aria-current="page" class="page-numbers current">{page_num}</span>
        {nxt}
      </div>
    </html>
    """


def _make_album_html(album_title: str, lang: str = "Telugu") -> str:
    return f"""
    <html>
      <div class="entry-content">
        <p>Movie Title:- {album_title}</p>
        <p>Music:- Composer X</p>
        <p>Language:- {lang}</p>
        <p>Year:- 2026</p>
        <p>01. Track One - Singer A</p>
        <p>128 Kbps - <a href="https://mp3teluguwap.net/mp3/2026/t1.mp3">Download</a></p>
      </div>
    </html>
    """


class TestNotificationPrioritySorting:
    """
    Tests NaaSongs source-priority notification ordering:
    - Page 1 albums appear first, followed by Page 2, Page 3, ..., Page 20.
    - Original position within each page is preserved top-to-bottom.
    - Works across all filters, migrations/legacy rows, re-scans, read/unread preservation, and API calls.
    """

    def test_01_to_04_twenty_page_source_priority_and_position_order(self, tmp_path):
        db = LatestReleasesDB(sqlite_path=str(tmp_path / "prio_20p.db"))

        mock_source = MagicMock()
        mock_source.fetch_page.side_effect = [_make_index_html(p, count=3, max_page=20) for p in range(1, 21)]

        def fake_album_page(url: str):
            # Extract pX-aY
            slug = url.rsplit("/", 1)[-1].replace(".html", "")
            return _make_album_html(slug.upper())

        mock_source.fetch_album_page.side_effect = fake_album_page

        engine = LatestReleasesDiscoveryEngine(db=db, source_client=mock_source)
        with patch("backend.services.catalog.CloudCatalogService.get_albums_index", return_value=[]):
            res = engine.run_scan(locked_by="test_prio", max_pages=20)

        assert res["success"] is True
        assert res["pages_successfully_checked"] == 20
        assert res["albums_discovered"] == 60

        notifs = db.get_notifications(status="active", limit=200)
        assert len(notifs) == 60

        expected_order = [(p, pos) for p in range(1, 21) for pos in range(1, 4)]
        actual_order = [(n["source_page"], n["source_position"]) for n in notifs]
        assert actual_order == expected_order

        # Explicit sanity check: Page 1 < Page 2 < Page 3
        names = [n["album_name"] for n in notifs]
        assert names[:6] == ["P1-A1", "P1-A2", "P1-A3", "P2-A1", "P2-A2", "P2-A3"]
        assert names[6:9] == ["P3-A1", "P3-A2", "P3-A3"]
        assert names[-3:] == ["P20-A1", "P20-A2", "P20-A3"]

    def test_05_08_09_rescan_updates_order_without_duplicates_and_preserves_read_status(self, tmp_path):
        db = LatestReleasesDB(sqlite_path=str(tmp_path / "prio_rescan.db"))

        # Scan 1: Album B is on Page 1 Pos 1, Album A is on Page 2 Pos 1
        db.create_or_update_notification(
            album_id=1,
            notification_type=NotificationType.NEW_ALBUM,
            title="New Album: Album B",
            message="msg",
            album_name="Album B",
            artwork_url=None,
            music_director="Dir",
            year=2026,
            source_url="https://naasongs.com.co/album-b.html",
            songs_list=[{"song_title": "S1", "exists_in_catalog": False}],
            missing_count=1,
            source_page=1,
            source_position=1,
        )
        db.create_or_update_notification(
            album_id=2,
            notification_type=NotificationType.NEW_ALBUM,
            title="New Album: Album A",
            message="msg",
            album_name="Album A",
            artwork_url=None,
            music_director="Dir",
            year=2026,
            source_url="https://naasongs.com.co/album-a.html",
            songs_list=[{"song_title": "S2", "exists_in_catalog": False}],
            missing_count=1,
            source_page=2,
            source_position=1,
        )

        initial = db.get_notifications(status="all", limit=20)
        assert [n["album_name"] for n in initial] == ["Album B", "Album A"]

        # Mark Album A as read
        album_a_id = next(n["id"] for n in initial if n["album_name"] == "Album A")
        db.mark_notification_read(album_a_id)

        # Scan 2: New post pushes Album A to Page 1 Pos 1, Album B to Page 2 Pos 1
        created_a = db.create_or_update_notification(
            album_id=2,
            notification_type=NotificationType.NEW_ALBUM,
            title="New Album: Album A",
            message="msg",
            album_name="Album A",
            artwork_url=None,
            music_director="Dir",
            year=2026,
            source_url="https://naasongs.com.co/album-a.html",
            songs_list=[{"song_title": "S2", "exists_in_catalog": False}],
            missing_count=1,
            source_page=1,
            source_position=1,
        )
        created_b = db.create_or_update_notification(
            album_id=1,
            notification_type=NotificationType.NEW_ALBUM,
            title="New Album: Album B",
            message="msg",
            album_name="Album B",
            artwork_url=None,
            music_director="Dir",
            year=2026,
            source_url="https://naasongs.com.co/album-b.html",
            songs_list=[{"song_title": "S1", "exists_in_catalog": False}],
            missing_count=1,
            source_page=2,
            source_position=1,
        )

        # No duplicates created
        assert created_a is False
        assert created_b is False

        after = db.get_notifications(status="all", limit=20)
        assert len(after) == 2
        assert [n["album_name"] for n in after] == ["Album A", "Album B"]
        # Read/unread preserved across update
        assert after[0]["is_read"] is True
        assert after[1]["is_read"] is False

    def test_06_legacy_notifications_without_source_page_fallback_after_tracked(self, tmp_path):
        db_file = tmp_path / "legacy_prio.db"
        # Simulate pre-migration schema without source_page/source_position
        conn = sqlite3.connect(db_file)
        conn.execute("""
            CREATE TABLE notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
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
                is_read INTEGER DEFAULT 0,
                created_at DOUBLE PRECISION NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL,
                details_json TEXT,
                dedup_hash TEXT UNIQUE
            );
        """)
        conn.execute("""
            INSERT INTO notifications (
                album_id, notification_type, title, message, album_name, created_at, updated_at, details_json, dedup_hash
            ) VALUES (1, 'NEW_ALBUM', 'Legacy Album', 'msg', 'Legacy Album', 9999999999.0, 9999999999.0, '[]', 'legacyhash');
        """)
        conn.commit()
        conn.close()

        db = LatestReleasesDB(sqlite_path=str(db_file))
        db.create_or_update_notification(
            album_id=2,
            notification_type=NotificationType.NEW_ALBUM,
            title="New Album: Tracked P2",
            message="msg",
            album_name="Tracked P2",
            artwork_url=None,
            music_director="Dir",
            year=2026,
            source_url="https://naasongs.com.co/tracked-p2.html",
            songs_list=[{"song_title": "S1", "exists_in_catalog": False}],
            missing_count=1,
            source_page=2,
            source_position=1,
        )

        rows = db.get_notifications(status="all", limit=10)
        # Tracked Page 2 item must still appear before legacy NULL source_page row (despite huge created_at)
        assert [r["album_name"] for r in rows] == ["Tracked P2", "Legacy Album"]
        assert rows[1]["source_page"] is None

    def test_07_10_all_filters_and_repeated_api_calls_deterministic(self, tmp_path):
        db = LatestReleasesDB(sqlite_path=str(tmp_path / "api_filters.db"))

        entries = [
            ("P3 Tamil", NotificationType.NEW_ALBUM, "Tamil", 3, 2, [{"song_title": "T1", "exists_in_catalog": False, "match_confidence": "NONE"}]),
            ("P1 Telugu Missing", NotificationType.MISSING_SONGS, "Telugu", 1, 2, [{"song_title": "M1", "exists_in_catalog": False, "match_confidence": "NONE"}]),
            ("P2 Telugu Uncertain", NotificationType.NEW_SONG, "Telugu", 2, 1, [{"song_title": "U1", "exists_in_catalog": False, "match_confidence": "UNCERTAIN"}]),
            ("P1 Tamil New", NotificationType.NEW_ALBUM, "Tamil", 1, 1, [{"song_title": "N1", "exists_in_catalog": False, "match_confidence": "NONE"}]),
            ("P2 Telugu Missing", NotificationType.MISSING_SONGS, "Telugu", 2, 2, [{"song_title": "M2", "exists_in_catalog": False, "match_confidence": "NONE"}]),
            ("P1 Telugu Uncertain", NotificationType.MISSING_SONGS, "Telugu", 1, 3, [{"song_title": "U2", "exists_in_catalog": False, "match_confidence": "UNCERTAIN"}]),
        ]
        for idx, (name, ntype, lang, page, pos, songs) in enumerate(entries, start=1):
            db.create_or_update_notification(
                album_id=idx,
                notification_type=ntype,
                title=name,
                message="msg",
                album_name=name,
                artwork_url=None,
                music_director="Dir",
                year=2026,
                source_url=f"https://naasongs.com.co/{idx}.html",
                songs_list=songs,
                missing_count=len(songs),
                language=lang,
                source_page=page,
                source_position=pos,
            )

        client = TestClient(app)
        with patch("backend.api.latest_releases_routes.get_latest_releases_db", return_value=db), \
             patch("backend.services.latest_releases.reconciliation.reconcile_active_notifications", return_value={"resolved_notifications": 0}):
            # Repeated calls under category=all
            r1 = [n["album_name"] for n in client.get("/api/latest-releases/notifications?category=all").json()["notifications"]]
            r2 = [n["album_name"] for n in client.get("/api/latest-releases/notifications?category=all").json()["notifications"]]
            assert r1 == r2
            assert r1 == [
                "P1 Tamil New",
                "P1 Telugu Missing",
                "P1 Telugu Uncertain",
                "P2 Telugu Uncertain",
                "P2 Telugu Missing",
                "P3 Tamil",
            ]

            # Filter by language
            r_tam = [n["album_name"] for n in client.get("/api/latest-releases/notifications?category=all&language=tamil").json()["notifications"]]
            assert r_tam == ["P1 Tamil New", "P3 Tamil"]

            r_tel = [n["album_name"] for n in client.get("/api/latest-releases/notifications?category=all&language=telugu").json()["notifications"]]
            assert r_tel == ["P1 Telugu Missing", "P1 Telugu Uncertain", "P2 Telugu Uncertain", "P2 Telugu Missing"]

            # Filter by missing_songs category
            r_miss = [n["album_name"] for n in client.get("/api/latest-releases/notifications?category=missing_songs").json()["notifications"]]
            assert r_miss == ["P1 Telugu Missing", "P1 Telugu Uncertain", "P2 Telugu Missing"]

            # Filter by uncertain category
            r_unc = [n["album_name"] for n in client.get("/api/latest-releases/notifications?category=uncertain").json()["notifications"]]
            assert r_unc == ["P1 Telugu Uncertain", "P2 Telugu Uncertain"]
