import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from backend.main import app
from backend.services.latest_releases.album_parser import AlbumParser
from backend.services.latest_releases.discovery import LatestReleasesDiscoveryEngine
from backend.services.latest_releases.db import LatestReleasesDB


class TestLatestReleasesV2Pagination:

    def test_multi_page_scan_and_deduplication(self, tmp_path):
        db_file = tmp_path / "test_pagination_db.db"
        db = LatestReleasesDB(sqlite_path=str(db_file))

        mock_source = MagicMock()
        # Page 1 has Album 1 & Album 2
        mock_source.fetch_page.side_effect = [
            """
            <html><div class="entry-content">
            <a href="https://naasongs.com.co/album-1.html">Album 1</a>
            <a href="https://naasongs.com.co/album-2.html">Album 2</a>
            </div></html>
            """,
            # Page 2 has Album 2 (duplicate) & Album 3
            """
            <html><div class="entry-content">
            <a href="https://naasongs.com.co/album-2.html">Album 2</a>
            <a href="https://naasongs.com.co/album-3.html">Album 3</a>
            </div></html>
            """,
            # Page 3 is empty -> early exit
            """
            <html><div class="entry-content"></div></html>
            """
        ]

        mock_source.fetch_album_page.return_value = """
        <html>
        <div class="entry-content">
            <p>Movie Title:- Mock Album</p>
            <p>Music:- Mock Director</p>
            <p>Language:- Telugu</p>
            <p>Year:- 2026</p>
            <p>01. Song 1 - <a href="https://naasongs.com.co/s1.mp3">Download</a></p>
        </div>
        </html>
        """

        engine = LatestReleasesDiscoveryEngine(db=db, source_client=mock_source)
        with patch("backend.services.latest_releases.discovery.SongComparator.compare_album", side_effect=lambda alb: (None, alb)):
            result = engine.run_scan(locked_by="test_crawler", max_pages=5)

        assert result["success"] is True
        assert result["pages_requested"] == 3
        assert result["pages_successfully_checked"] == 2
        assert result["pages_unavailable"] == 1
        assert result["pages_checked"] == 2
        # Album 1, 2, 3 collected (Album 2 was deduplicated)
        assert result["albums_discovered"] == 3
        assert result["albums_inspected"] == 3
        assert result["albums_found"] == 3

    def test_pagination_stops_on_404_or_unreachable_page(self, tmp_path):
        db_file = tmp_path / "test_p404_db.db"
        db = LatestReleasesDB(sqlite_path=str(db_file))

        mock_source = MagicMock()
        mock_source.fetch_page.side_effect = [
            """
            <html><div class="entry-content">
            <a href="https://naasongs.com.co/album-a.html">Album A</a>
            </div></html>
            """,
            # Page 2 returns None (e.g. 404 from server)
            None
        ]
        mock_source.fetch_album_page.return_value = """
        <html><div class="entry-content">
            <p>Movie Title:- Album A</p>
            <p>Language:- Telugu</p>
        </div></html>
        """

        engine = LatestReleasesDiscoveryEngine(db=db, source_client=mock_source)
        with patch("backend.services.latest_releases.discovery.SongComparator.compare_album", side_effect=lambda alb: (None, alb)):
            result = engine.run_scan(locked_by="test_crawler", max_pages=10)

        assert result["success"] is True
        assert result["pages_requested"] == 2
        assert result["pages_successfully_checked"] == 1
        assert result["pages_unavailable"] == 1
        assert result["pages_checked"] == 1
        assert result["albums_discovered"] == 1
        assert result["albums_inspected"] == 1

    def test_parse_pagination_info_real_naasongs_markup(self):
        p1_html = """
        <div class="nav-links">
            <span aria-current="page" class="page-numbers current">1</span>
            <a class="page-numbers" href="https://naasongs.com.co/page/2">2</a>
            <a class="page-numbers" href="https://naasongs.com.co/page/3">3</a>
            <span class="page-numbers dots">…</span>
            <a class="page-numbers" href="https://naasongs.com.co/page/541">541</a>
            <a class="next page-numbers" href="https://naasongs.com.co/page/2"><span class="screen-reader-text">Next Posts</span>»</a>
        </div>
        """
        info1 = AlbumParser.parse_pagination_info(p1_html, "https://naasongs.com.co/")
        assert info1["pagination_present"] is True
        assert info1["current_page"] == 1
        assert info1["last_page_number"] == 541
        assert info1["next_page_url"] == "https://naasongs.com.co/page/2"
        assert info1["has_next"] is True

        p541_html = """
        <div class="nav-links">
            <a class="prev page-numbers" href="https://naasongs.com.co/page/540">«</a>
            <a class="page-numbers" href="https://naasongs.com.co/">1</a>
            <span class="page-numbers dots">…</span>
            <a class="page-numbers" href="https://naasongs.com.co/page/540">540</a>
            <span aria-current="page" class="page-numbers current">541</span>
        </div>
        """
        info541 = AlbumParser.parse_pagination_info(p541_html, "https://naasongs.com.co/page/541")
        assert info541["pagination_present"] is True
        assert info541["current_page"] == 541
        assert info541["last_page_number"] == 541
        assert info541["next_page_url"] is None
        assert info541["has_next"] is False

    def test_full_20_page_traversal_when_20_pages_exist(self, tmp_path):
        db_file = tmp_path / "test_20pages.db"
        db = LatestReleasesDB(sqlite_path=str(db_file))

        def build_index_page(p):
            links = "".join(
                f'<a href="https://naasongs.com.co/album-p{p}-{i}.html">Album P{p}-{i}</a>'
                for i in range(1, 11)
            )
            return f"""
            <html><div class="entry-content">{links}</div>
            <div class="nav-links">
                <span aria-current="page" class="page-numbers current">{p}</span>
                <a class="page-numbers" href="https://naasongs.com.co/page/541">541</a>
                <a class="next page-numbers" href="https://naasongs.com.co/page/{p + 1}">Next</a>
            </div></html>
            """

        mock_source = MagicMock()
        mock_source.fetch_page.side_effect = [build_index_page(p) for p in range(1, 21)]
        mock_source.fetch_album_page.return_value = """
        <html><div class="entry-content">
            <p>Movie Title:- Mock Album</p>
            <p>Language:- Telugu</p>
            <p>01. Track One - <a href="https://naasongs.com.co/t1.mp3">Download</a></p>
        </div></html>
        """

        engine = LatestReleasesDiscoveryEngine(db=db, source_client=mock_source)
        with patch("backend.services.latest_releases.discovery.SongComparator.compare_album", side_effect=lambda alb: (None, alb)):
            result = engine.run_scan(locked_by="test_20pages", max_pages=20)

        assert result["success"] is True
        assert result["pages_requested"] == 20
        assert result["pages_successfully_checked"] == 20
        assert result["pages_unavailable"] == 0
        assert result["pages_checked"] == 20
        assert result["albums_discovered"] == 200
        assert result["albums_inspected"] == 200

        scans = db.get_recent_scans(limit=1)
        assert len(scans) == 1
        assert scans[0]["pages_requested"] == 20
        assert scans[0]["pages_successfully_checked"] == 20
        assert scans[0]["pages_unavailable"] == 0
        assert scans[0]["albums_discovered"] == 200
        assert scans[0]["albums_inspected"] == 200
        assert scans[0]["locked_by"] == "test_20pages"
        assert scans[0]["error_message"] is None

    def test_stops_at_terminal_page_and_guards_redirect_to_page_1(self, tmp_path):
        db = LatestReleasesDB(sqlite_path=str(tmp_path / "test_term.db"))

        def make_page(p, has_next=True, current_override=None):
            cur = current_override or p
            nxt = f'<a class="next page-numbers" href="https://naasongs.com.co/page/{p+1}">Next</a>' if has_next else ""
            return f"""
            <html><div class="entry-content">
                <a href="https://naasongs.com.co/album-t-{p}.html">Album T{p}</a>
            </div>
            <div class="nav-links">
                <span aria-current="page" class="page-numbers current">{cur}</span>
                {nxt}
            </div></html>
            """

        mock_source = MagicMock()
        mock_source.fetch_page.side_effect = [
            make_page(1, has_next=True),
            make_page(2, has_next=True),
            # Suppose page 3 redirected silently back to page 1 (like NaaSongs page 542 -> /)
            make_page(3, has_next=True, current_override=1),
        ]
        mock_source.fetch_album_page.return_value = '<html><div class="entry-content"><p>Movie Title:- Mock</p></div></html>'

        engine = LatestReleasesDiscoveryEngine(db=db, source_client=mock_source)
        with patch("backend.services.latest_releases.discovery.SongComparator.compare_album", side_effect=lambda alb: (None, alb)):
            result = engine.run_scan(locked_by="test_redirect_guard", max_pages=20)

        assert result["success"] is True
        assert result["pages_requested"] == 3
        assert result["pages_successfully_checked"] == 2
        assert result["pages_unavailable"] == 1
        assert result["albums_discovered"] == 2
        assert result["albums_inspected"] == 2

    def test_discovered_vs_inspected_separation_on_album_fetch_skip(self, tmp_path):
        db = LatestReleasesDB(sqlite_path=str(tmp_path / "test_insp.db"))
        mock_source = MagicMock()
        mock_source.fetch_page.side_effect = [
            """
            <html><div class="entry-content">
                <a href="https://naasongs.com.co/a1.html">A1</a>
                <a href="https://naasongs.com.co/a2.html">A2</a>
                <a href="https://naasongs.com.co/a3.html">A3</a>
            </div><div class="nav-links"><span class="page-numbers current">1</span></div></html>
            """,
        ]
        mock_source.fetch_album_page.side_effect = [
            '<html><div class="entry-content"><p>Movie Title:- A1</p></div></html>',
            None,
            '<html><div class="entry-content"><p>Movie Title:- A3</p></div></html>',
        ]
        engine = LatestReleasesDiscoveryEngine(db=db, source_client=mock_source)
        with patch("backend.services.latest_releases.discovery.SongComparator.compare_album", side_effect=lambda alb: (None, alb)):
            result = engine.run_scan(locked_by="test_partial_insp", max_pages=20)

        assert result["success"] is True
        assert result["pages_requested"] == 1
        assert result["pages_successfully_checked"] == 1
        assert result["pages_unavailable"] == 0
        assert result["albums_discovered"] == 3
        assert result["albums_inspected"] == 2

    def test_check_now_and_cli_default_to_full_20_page_scan(self):
        client = TestClient(app)
        with patch("backend.services.latest_releases.discovery.LatestReleasesDiscoveryEngine.run_scan") as mock_scan:
            mock_scan.return_value = {
                "success": True,
                "status": "COMPLETED",
                "pages_requested": 20,
                "pages_successfully_checked": 20,
                "pages_unavailable": 0,
                "albums_discovered": 200,
                "albums_inspected": 200,
            }
            resp = client.post("/api/latest-releases/check-now")
            assert resp.status_code == 200
            mock_scan.assert_called_once_with(locked_by="manual_web_ui", max_pages=None, limit_albums=None)

        from backend.services.latest_releases import cli
        with patch("backend.services.latest_releases.cli.LatestReleasesDiscoveryEngine") as mock_cls, \
             patch.dict("os.environ", {"LATEST_RELEASES_LIMIT_ALBUMS": "", "LATEST_RELEASES_MAX_PAGES": ""}, clear=False), \
             patch("sys.argv", ["cli.py", "scan"]):
            mock_inst = MagicMock()
            mock_inst.run_scan.return_value = {"success": True, "status": "COMPLETED"}
            mock_cls.return_value = mock_inst
            with pytest.raises(SystemExit) as exc_info:
                cli.main()
            assert exc_info.value.code == 0
            mock_inst.run_scan.assert_called_once_with(locked_by="cli_cron", max_pages=None, limit_albums=None)

    def test_get_recent_scans_handles_reordered_columns(self, tmp_path):
        import sqlite3
        db_file = tmp_path / "reordered.db"
        conn = sqlite3.connect(db_file)
        conn.execute("""
            CREATE TABLE discovery_scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at DOUBLE PRECISION NOT NULL,
                completed_at DOUBLE PRECISION,
                status VARCHAR(32),
                albums_found INT,
                new_albums_count INT,
                new_songs_count INT,
                pages_checked INT,
                resolved_notifications_count INT,
                failed_pages TEXT,
                error_message TEXT,
                locked_by VARCHAR(64)
            );
        """)
        conn.execute("""
            INSERT INTO discovery_scans (
                started_at, completed_at, status, albums_found, new_albums_count,
                new_songs_count, pages_checked, resolved_notifications_count,
                failed_pages, error_message, locked_by
            ) VALUES (1000.0, 1020.0, 'COMPLETED', 15, 9, 18, 2, 0, '[]', NULL, 'manual_web_ui');
        """)
        conn.commit()
        conn.close()

        db = LatestReleasesDB(sqlite_path=str(db_file))
        scans = db.get_recent_scans(limit=5)
        assert len(scans) == 1
        assert scans[0]["pages_checked"] == 2
        assert scans[0]["locked_by"] == "manual_web_ui"
        assert scans[0]["error_message"] is None
        assert scans[0]["failed_pages"] == []
        assert scans[0]["pages_requested"] == 2
        assert scans[0]["pages_successfully_checked"] == 2
        assert scans[0]["pages_unavailable"] == 0
        assert scans[0]["albums_discovered"] == 15
        assert scans[0]["albums_inspected"] == 15

    def test_background_check_now_endpoint_accepted_and_conflict(self, tmp_path):
        db = LatestReleasesDB(sqlite_path=str(tmp_path / "bg_api.db"))
        client = TestClient(app)
        with patch("backend.api.latest_releases_routes.LatestReleasesDiscoveryEngine") as mock_engine_cls:
            mock_eng = MagicMock()
            mock_eng.db = db
            mock_eng.run_scan.return_value = {"success": True, "status": "COMPLETED"}
            mock_engine_cls.return_value = mock_eng

            resp = client.post("/api/latest-releases/check-now?background=true")
            assert resp.status_code == 202
            body = resp.json()
            assert body["status"] == "STARTED"
            assert body["async"] is True

            assert db.acquire_scan_lock(locked_by="existing_job", timeout_minutes=5) is True
            resp_locked = client.post("/api/latest-releases/check-now?background=true")
            assert resp_locked.status_code == 409
            db.release_scan_lock(locked_by="existing_job")
