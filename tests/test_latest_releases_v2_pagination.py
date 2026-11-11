import pytest
from unittest.mock import MagicMock
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
        result = engine.run_scan(locked_by="test_crawler", max_pages=5)

        assert result["success"] is True
        # Page 1, Page 2, Page 3 were checked before stopping
        assert result["pages_checked"] == 3
        # Album 1, 2, 3 collected (Album 2 was deduplicated)
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
        result = engine.run_scan(locked_by="test_crawler", max_pages=10)

        assert result["success"] is True
        assert result["pages_checked"] == 2
        assert result["albums_found"] == 1
