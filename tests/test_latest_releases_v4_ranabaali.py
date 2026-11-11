import json
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.services.catalog import cloud_catalog_service
from backend.services.latest_releases.album_parser import AlbumParser
from backend.services.latest_releases.song_comparator import SongComparator
from backend.services.latest_releases.models import (
    NotificationType,
    MatchConfidence,
    MatchClassification,
    TrackingStatus
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def load_ranabaali_html() -> str:
    return (FIXTURES_DIR / "ranabaali_2026_source.html").read_text(encoding="utf-8")


def load_ranabaali_github_meta() -> dict:
    return json.loads((FIXTURES_DIR / "ranabaali_github.json").read_text(encoding="utf-8"))


class TestLatestReleasesV4Ranabaali:

    def test_01_extract_all_six_ranabaali_songs(self):
        html = load_ranabaali_html()
        album = AlbumParser.parse_album_page(html, "https://naasongs.com.co/ranabaali-2026-songs-c.html")
        assert album is not None
        assert album.album_name == "Ranabaali"
        assert album.language == "Telugu"
        assert album.year == 2026
        assert album.music_director == "Ajay Atul"

        titles = [s.song_title for s in album.songs]
        assert titles == [
            "Endhayya Saami Song (Full)",
            "Kobali Song",
            "Kaaru Nisale Song",
            "Paalegaadu Song",
            "Yerra Pilla Song",
            "Ghanuda Ranabaali Song",
        ]

    def test_02_exclusion_of_download_and_action_labels(self):
        html = load_ranabaali_html()
        album = AlbumParser.parse_album_page(html, "https://naasongs.com.co/ranabaali-2026-songs-c.html")
        titles_lower = [s.song_title.lower() for s in album.songs]
        prohibited = {"download", "listen", "play", "audio songs download", "mp3 download", "album download"}
        for t in titles_lower:
            assert t not in prohibited
            assert not t.endswith("audio songs download")

    def test_03_exclusion_of_bitrate_labels(self):
        html = load_ranabaali_html()
        album = AlbumParser.parse_album_page(html, "https://naasongs.com.co/ranabaali-2026-songs-c.html")
        for s in album.songs:
            assert "128 kbps" not in s.song_title.lower()
            assert "320 kbps" not in s.song_title.lower()

    def test_04_correct_singer_extraction(self):
        html = load_ranabaali_html()
        album = AlbumParser.parse_album_page(html, "https://naasongs.com.co/ranabaali-2026-songs-c.html")
        singers = [s.singer for s in album.songs]
        assert singers == [
            "Swetha Mohan, Ajay Gogavale",
            "V.M. Mahalingam",
            "Parvathi Meenakshi",
            "Vishal Dadlani, Parvathi Meenakshi",
            "Otu Subbamma, Lakkireddy Palle lakshmi Dev",
            "V.M. Mahalingam",
        ]

    def test_05_correct_bitrate_link_association_without_mixing(self):
        html = load_ranabaali_html()
        album = AlbumParser.parse_album_page(html, "https://naasongs.com.co/ranabaali-2026-songs-c.html")
        s1, s2 = album.songs[0], album.songs[1]

        assert s1.available_bitrates == ["128 Kbps", "320 Kbps"]
        assert s1.download_links["128 Kbps"].endswith("/128%20Kbps/Endhayy%20Saami.mp3")
        assert s1.download_links["320 Kbps"].endswith("/320%20Kbps/Endhayya%20Saami.mp3")
        assert s1.source_audio_url == s1.download_links["320 Kbps"]

        assert s2.available_bitrates == ["128 Kbps", "320 Kbps"]
        assert s2.download_links["128 Kbps"].endswith("/128%20Kbps/2-Kobali.mp3")
        assert s2.download_links["320 Kbps"].endswith("/320%20Kbps/Kobali.mp3")
        assert s2.source_audio_url == s2.download_links["320 Kbps"]

        for _, v in s1.download_links.items():
            assert "Kobali" not in v
        for _, v in s2.download_links.items():
            assert "Endhayy" not in v

    def test_06_sidebar_recommendations_excluded(self):
        html = load_ranabaali_html()
        album = AlbumParser.parse_album_page(html, "https://naasongs.com.co/ranabaali-2026-songs-c.html")
        titles = " | ".join(s.song_title for s in album.songs)
        for sidebar_item in ["Vrushakarma", "Tyson Naidu", "Mahakali", "Donga Naa Koduku", "Swag of Arjun Song"]:
            assert sidebar_item not in titles

    def test_07_seo_text_excluded(self):
        html = load_ranabaali_html()
        album = AlbumParser.parse_album_page(html, "https://naasongs.com.co/ranabaali-2026-songs-c.html")
        titles = " | ".join(s.song_title for s in album.songs)
        assert "Ranabaali Songs Free Download" not in titles
        assert "Ringtones Download" not in titles
        assert "Listen Online" not in titles

    def test_08_track_numbering_preserved(self):
        html = load_ranabaali_html()
        album = AlbumParser.parse_album_page(html, "https://naasongs.com.co/ranabaali-2026-songs-c.html")
        assert [s.track_number for s in album.songs] == [1, 2, 3, 4, 5, 6]

    def test_09_promo_vs_full_version_detection(self):
        meta = load_ranabaali_github_meta()
        cat_songs = meta["songs"]

        res_full = cloud_catalog_service.match_song_detailed(
            source_title="Endhayya Saami Song (Full)",
            catalog_songs=cat_songs,
            source_singer="Swetha Mohan, Ajay Gogavale",
            source_filename="Endhayya Saami.mp3"
        )
        assert res_full["classification"] == "POSSIBLE_VERSION_MATCH"
        assert res_full["confidence"] == "UNCERTAIN"
        assert res_full["exists_in_catalog"] is False
        assert res_full["matched_song"]["title"] == "Endhayya Saami Song (Promo)"
        candidate_titles = [c["title"] for c in res_full["version_candidates"]]
        assert "Endhayya Saami Song (Promo)" in candidate_titles
        assert "Endhayya Saami" in candidate_titles

        res_promo = cloud_catalog_service.match_song_detailed(
            source_title="Endhayya Saami Song (Promo)",
            catalog_songs=cat_songs,
            source_singer="Swetha Mohan, Ajay Gogavale"
        )
        assert res_promo["classification"] == "EXACT_MATCH"
        assert res_promo["exists_in_catalog"] is True

    def test_10_and_11_ranabaali_notification_and_no_duplicates(self, monkeypatch):
        html = load_ranabaali_html()
        meta = load_ranabaali_github_meta()

        discovered = AlbumParser.parse_album_page(html, "https://naasongs.com.co/ranabaali-2026-songs-c.html")
        mock_catalog = [{
            "id": meta["album"]["id"],
            "name": meta["album"]["name"],
            "artist": meta["album"]["musicDirector"],
            "year": meta["album"]["year"],
            "language": meta["album"]["language"],
            "image": meta["album"]["image"],
            "partition": "R",
            "songCount": len(meta["songs"])
        }]
        comparator = SongComparator(catalog_albums=mock_catalog)
        monkeypatch.setattr(comparator, "fetch_catalog_album_songs", lambda alb: meta["songs"])

        notif_type, analyzed = comparator.compare_album(discovered)

        assert analyzed.exists_in_catalog is True
        assert analyzed.catalog_album_name == "Ranabaali"
        assert notif_type == NotificationType.MISSING_SONGS
        assert len(analyzed.songs) == 6

        assert analyzed.songs[0].song_title == "Endhayya Saami Song (Full)"
        assert analyzed.songs[0].match_classification == MatchClassification.POSSIBLE_VERSION_MATCH
        assert analyzed.songs[0].exists_in_catalog is False

        for missing_song in analyzed.songs[1:]:
            assert missing_song.match_classification == MatchClassification.MISSING
            assert missing_song.exists_in_catalog is False
            assert missing_song.song_title.lower() != "download"

        # If 2 tracks match catalog exactly, only the remaining 4 are flagged
        extended_cat_songs = meta["songs"] + [
            {"id": "k1", "title": "Kobali Song", "audio": "Kobali Song.mp3", "singers": ["V.M. Mahalingam"]},
            {"id": "p1", "title": "Paalegaadu Song", "audio": "Paalegaadu Song.mp3", "singers": ["Vishal Dadlani", "Parvathi Meenakshi"]},
        ]
        discovered2 = AlbumParser.parse_album_page(html, "https://naasongs.com.co/ranabaali-2026-songs-c.html")
        comparator2 = SongComparator(catalog_albums=mock_catalog)
        monkeypatch.setattr(comparator2, "fetch_catalog_album_songs", lambda alb: extended_cat_songs)
        notif_type2, analyzed2 = comparator2.compare_album(discovered2)

        assert notif_type2 == NotificationType.MISSING_SONGS
        non_existing = [s for s in analyzed2.songs if not s.exists_in_catalog]
        assert [s.song_title for s in non_existing] == [
            "Endhayya Saami Song (Full)",
            "Kaaru Nisale Song",
            "Yerra Pilla Song",
            "Ghanuda Ranabaali Song",
        ]

    def test_12_missing_and_unauthorized_download_links_safe(self):
        html_mixed = """
        <div class="entry-content">
            <p>Movie Title:- TestMixed<br />Language:- Telugu<br />Year:- 2026</p>
            <h2>TestMixed Audio Songs Download</h2>
            <p>01. Upcoming Plain Track &#8211; Singer One<br />
               128 Kbps &#8211; Download<br />
               320 Kbps &#8211; Download</p>
            <p>02. Listen Only Track &#8211; Singer Two<br />
               <a href="https://naasongs.com.co/listen/track2.mp3">Listen</a></p>
            <p>03. Untrusted Domain Track &#8211; Singer Three<br />
               128 Kbps &#8211; <a href="https://untrusted-mirror.example.com/track3.mp3">Download</a></p>
            <p>04. Authorized Track &#8211; Singer Four<br />
               320 Kbps &#8211; <a href="https://mp3teluguwap.net/mp3/2026/track4.mp3">Download</a></p>
        </div>
        """
        album = AlbumParser.parse_album_page(html_mixed, "https://naasongs.com.co/testmixed.html")
        assert album is not None
        assert len(album.songs) == 4

        t1, t2, t3, t4 = album.songs
        assert t1.song_title == "Upcoming Plain Track"
        assert t1.singer == "Singer One"
        assert t1.link_available is False
        assert t1.download_authorized is False
        assert t1.source_audio_url is None

        assert t2.song_title == "Listen Only Track"
        assert t2.link_available is True
        assert t2.download_authorized is False
        assert t2.source_audio_url is None
        assert t2.source_page_url == "https://naasongs.com.co/testmixed.html"

        assert t3.song_title == "Untrusted Domain Track"
        assert t3.link_available is True
        assert t3.download_authorized is False
        assert t3.source_audio_url is None

        assert t4.song_title == "Authorized Track"
        assert t4.link_available is True
        assert t4.download_authorized is True
        assert t4.source_audio_url == "https://mp3teluguwap.net/mp3/2026/track4.mp3"

    def test_13_artwork_url_encoding_and_structure(self, monkeypatch):
        monkeypatch.setenv("CLOUD_MODE", "1")
        ranabaali_meta = load_ranabaali_github_meta()
        mock_idx = [
            {
                "id": "2a37f73ff827",
                "name": "Ranabaali",
                "artist": "Ajay-Atul",
                "year": 2026,
                "genre": "Tollywood Soundtrack",
                "language": "Telugu",
                "image": "Ranabaali.png",
                "songCount": 2,
                "partition": "R"
            },
            {
                "id": "abd30b2c5a3f",
                "name": "OM – Telugu",
                "artist": "Unknown",
                "year": 2026,
                "genre": "Tollywood Soundtrack",
                "language": "Telugu",
                "image": "OM – Telugu.png",
                "songCount": 3,
                "partition": "O"
            }
        ]
        monkeypatch.setattr(cloud_catalog_service, "get_albums_index", lambda force_refresh=False: mock_idx)

        def mock_get_meta(alb_ident, bypass_cache=False):
            name = alb_ident.get("name") if isinstance(alb_ident, dict) else alb_ident
            if name == "Ranabaali":
                return ranabaali_meta
            return {"album": {"name": "OM – Telugu", "image": "OM – Telugu.png"}, "songs": []}

        monkeypatch.setattr(cloud_catalog_service, "get_album_metadata", mock_get_meta)

        client = TestClient(app)

        res1 = client.get("/api/albums/Ranabaali")
        assert res1.status_code == 200
        d1 = res1.json()
        assert d1["has_artwork"] is True
        assert d1["artwork_url"] == "https://raw.githubusercontent.com/vamshivamshi9630/MusicData/main/Ranabaali/Ranabaali.png"
        assert "/R/Ranabaali/" not in d1["artwork_url"]
        assert d1["songs"][0]["audio_url"] == "https://raw.githubusercontent.com/vamshivamshi9630/MusicData/main/Ranabaali/Endhayya%20Saami%20Song%20%28Promo%29.mp3"

        res2 = client.get("/api/albums/OM%20%E2%80%93%20Telugu")
        assert res2.status_code == 200
        d2 = res2.json()
        assert d2["has_artwork"] is True
        assert d2["artwork_url"] == "https://raw.githubusercontent.com/vamshivamshi9630/MusicData/main/OM%20%E2%80%93%20Telugu/OM%20%E2%80%93%20Telugu.png"
        assert "/O/" not in d2["artwork_url"]
