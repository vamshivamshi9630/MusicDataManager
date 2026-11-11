import pytest
from backend.services.latest_releases.models import LanguageConfidence, DiscoveredAlbum, DiscoveredSong, NotificationType
from backend.services.latest_releases.album_parser import AlbumParser
from backend.services.latest_releases.song_comparator import SongComparator
from backend.services.latest_releases.db import LatestReleasesDB

class TestLatestReleasesV2Language:

    def test_explicit_html_language_extraction(self):
        html_sample = """
        <html>
        <div class="entry-content">
            <p>Movie Title:- OM</p>
            <p>Music:- Anirudh</p>
            <p>Language:- Tamil</p>
            <p>Year:- 2026</p>
            <p>01. First Song - <a href="https://naasongs.com.co/om-tamil-1.mp3">Download</a></p>
        </div>
        </html>
        """
        album = AlbumParser.parse_album_page(html_sample, "https://naasongs.com.co/om-tamil.html")
        assert album is not None
        assert album.album_name == "OM"
        assert album.language == "Tamil"
        assert album.language_confidence == LanguageConfidence.HIGH

    def test_h1_suffix_language_extraction_and_cleaning(self):
        # Case 1: OM Tamil
        html_tamil = """
        <html>
        <h1 class="entry-title">OM (2026) &#8211; Tamil Songs</h1>
        <div class="entry-content">
            <p>Music:- Anirudh</p>
            <p>Year:- 2026</p>
            <p>01. Tamil Track - <a href="https://naasongs.com.co/track1.mp3">Download</a></p>
        </div>
        </html>
        """
        album_tamil = AlbumParser.parse_album_page(html_tamil, "https://naasongs.com.co/om-2026-tamil-songs-2.html")
        assert album_tamil is not None
        assert album_tamil.album_name == "OM"
        assert album_tamil.language == "Tamil"
        assert album_tamil.language_confidence == LanguageConfidence.HIGH

        # Case 2: OM Telugu
        html_telugu = """
        <html>
        <h1 class="entry-title">OM (2026) – Telugu Songs</h1>
        <div class="entry-content">
            <p>Music:- Thaman</p>
            <p>Year:- 2026</p>
            <p>01. Telugu Track - <a href="https://naasongs.com.co/track2.mp3">Download</a></p>
        </div>
        </html>
        """
        album_telugu = AlbumParser.parse_album_page(html_telugu, "https://naasongs.com.co/om-2026-telugu-songs-a.html")
        assert album_telugu is not None
        assert album_telugu.album_name == "OM"
        assert album_telugu.language == "Telugu"
        assert album_telugu.language_confidence == LanguageConfidence.HIGH

    def test_url_category_language_extraction(self):
        html_kannada = """
        <html>
        <div class="entry-content">
            <p>Movie Title:- Yuva</p>
            <p>Music:- Ajaneesh Loknath</p>
            <p>Year:- 2026</p>
        </div>
        </html>
        """
        album_kannada = AlbumParser.parse_album_page(html_kannada, "https://naasongs.com.co/category/kannada/yuva-songs.html")
        assert album_kannada is not None
        assert album_kannada.album_name == "Yuva"
        assert album_kannada.language == "Kannada"
        assert album_kannada.language_confidence == LanguageConfidence.MEDIUM

    def test_distinct_catalog_matching_by_language(self):
        # Catalog has "OM" (Telugu)
        catalog = [
            {"name": "OM", "language": "Telugu", "year": 2026, "artist": "Thaman", "partition": "O"}
        ]
        comparator = SongComparator(catalog_albums=catalog)

        # Discovered album 1: OM (Telugu) -> Matches
        disc_telugu = DiscoveredAlbum(album_name="OM", normalized_name="om", source_url="https://naasongs.com.co/om-telugu.html", language="Telugu")
        match_telugu = comparator.find_matching_catalog_album(disc_telugu.album_name, disc_telugu.language)
        assert match_telugu is not None
        assert match_telugu["name"] == "OM"

        # Discovered album 2: OM (Tamil) -> Does NOT match Telugu catalog entry!
        disc_tamil = DiscoveredAlbum(album_name="OM", normalized_name="om", source_url="https://naasongs.com.co/om-tamil.html", language="Tamil")
        match_tamil = comparator.find_matching_catalog_album(disc_tamil.album_name, disc_tamil.language)
        assert match_tamil is None

    def test_distinct_deduplication_hashes_for_different_languages(self, tmp_path):
        db_file = tmp_path / "test_lang_db.db"
        db = LatestReleasesDB(sqlite_path=str(db_file))

        # Insert notification for OM (Telugu)
        n1 = db.create_or_update_notification(
            album_id=1,
            notification_type=NotificationType.NEW_ALBUM,
            title="New Album: OM",
            message="Telugu release",
            album_name="OM",
            artwork_url=None,
            music_director="Thaman",
            year=2026,
            source_url="https://naasongs.com.co/om-telugu.html",
            songs_list=[{"song_title": "Track 1", "exists_in_catalog": False}],
            missing_count=1,
            language="Telugu"
        )
        assert n1 is True

        # Insert notification for OM (Tamil) -> MUST NOT be deduplicated as duplicate of Telugu!
        n2 = db.create_or_update_notification(
            album_id=2,
            notification_type=NotificationType.NEW_ALBUM,
            title="New Album: OM",
            message="Tamil release",
            album_name="OM",
            artwork_url=None,
            music_director="Anirudh",
            year=2026,
            source_url="https://naasongs.com.co/om-tamil.html",
            songs_list=[{"song_title": "Track 1", "exists_in_catalog": False}],
            missing_count=1,
            language="Tamil"
        )
        assert n2 is True

        # Both notifications should exist in DB
        all_notifs = db.get_notifications(status="all")
        assert len(all_notifs) == 2
        langs = {n["language"] for n in all_notifs}
        assert langs == {"Telugu", "Tamil"}
