import unittest
from backend.services.latest_releases.song_comparator import SongComparator
from backend.services.latest_releases.models import (
    NotificationType,
    TrackingStatus,
    MatchConfidence,
    DiscoveredAlbum,
    DiscoveredSong
)

class TestLatestReleasesComparator(unittest.TestCase):

    def setUp(self):
        # Mock Catalog Index with 2 existing albums
        self.mock_catalog = [
            {
                "id": "alb-1",
                "name": "Pushpa 3",
                "artist": "Devi Sri Prasad",
                "year": 2026,
                "partition": "P",
                "image": "Pushpa 3.png",
                "songCount": 2
            },
            {
                "id": "alb-2",
                "name": "Khaidi No 150",
                "artist": "Devi Sri Prasad",
                "year": 2017,
                "partition": "K",
                "image": None,
                "songCount": 3
            }
        ]

        self.comparator = SongComparator(catalog_albums=self.mock_catalog)

        # Mock the fetch_catalog_album_songs method to return mock songs for Pushpa 3
        def mock_fetch_songs(cat_album):
            name = cat_album.get("name")
            if name == "Pushpa 3":
                return [
                    {"id": "s1", "title": "Jaalilo Jaalilo", "normalizedTitle": "jaalilo jaalilo"},
                    {"id": "s2", "title": "The Wild Fire", "normalizedTitle": "the wild fire"}
                ]
            elif name == "Khaidi No 150":
                return [
                    {"id": "s1", "title": "Ammadu Lets Do Kummudu"},
                    {"id": "s2", "title": "Sundari"},
                    {"id": "s3", "title": "You And Me"}
                ]
            return []

        self.comparator.fetch_catalog_album_songs = mock_fetch_songs

    def test_case_a_brand_new_album(self):
        """Case A: Album does NOT exist in MusicData catalog -> NEW_ALBUM"""
        discovered = DiscoveredAlbum(
            album_name="Tyson Naidu",
            normalized_name="tyson naidu",
            source_url="https://naasongs.com.co/tyson-naidu.html",
            music_director="Bheems Ceciroleo",
            year=2026,
            songs=[
                DiscoveredSong(song_title="Madarasi Rabbaru Bomma", normalized_title="madarasi rabbaru bomma", track_number=1)
            ]
        )

        notif_type, analyzed = self.comparator.compare_album(discovered)
        self.assertEqual(notif_type, NotificationType.NEW_ALBUM)
        self.assertFalse(analyzed.exists_in_catalog)
        self.assertEqual(analyzed.tracking_status, TrackingStatus.DISCOVERED)
        self.assertEqual(len(analyzed.songs), 1)
        self.assertFalse(analyzed.songs[0].exists_in_catalog)
        self.assertEqual(analyzed.songs[0].tracking_status, TrackingStatus.MISSING_FROM_MUSICDATA)

    def test_case_b_existing_album_all_songs_present(self):
        """Case B1: Album exists in catalog, all songs match -> None (No notification)"""
        discovered = DiscoveredAlbum(
            album_name="Pushpa 3",
            normalized_name="pushpa 3",
            source_url="https://naasongs.com.co/pushpa-3.html",
            songs=[
                DiscoveredSong(song_title="Jaalilo Jaalilo", normalized_title="jaalilo jaalilo", track_number=1),
                DiscoveredSong(song_title="The Wild Fire", normalized_title="the wild fire", track_number=2)
            ]
        )

        notif_type, analyzed = self.comparator.compare_album(discovered)
        self.assertIsNone(notif_type)
        self.assertTrue(analyzed.exists_in_catalog)
        self.assertTrue(all(s.exists_in_catalog for s in analyzed.songs))

    def test_case_c_existing_album_one_new_song(self):
        """Case B2: Album exists in catalog, 1 newly released song is missing -> NEW_SONG"""
        discovered = DiscoveredAlbum(
            album_name="Pushpa 3",
            normalized_name="pushpa 3",
            source_url="https://naasongs.com.co/pushpa-3.html",
            songs=[
                DiscoveredSong(song_title="Jaalilo Jaalilo", normalized_title="jaalilo jaalilo", track_number=1),
                DiscoveredSong(song_title="The Wild Fire", normalized_title="the wild fire", track_number=2),
                DiscoveredSong(song_title="Rampage Theme", normalized_title="rampage theme", track_number=3) # New song!
            ]
        )

        notif_type, analyzed = self.comparator.compare_album(discovered)
        self.assertEqual(notif_type, NotificationType.NEW_SONG)
        self.assertTrue(analyzed.exists_in_catalog)

        missing = [s for s in analyzed.songs if not s.exists_in_catalog]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0].song_title, "Rampage Theme")
        self.assertEqual(missing[0].tracking_status, TrackingStatus.MISSING_FROM_MUSICDATA)

    def test_case_d_existing_album_multiple_new_songs(self):
        """Case B3: Album exists, multiple newly released songs missing -> MISSING_SONGS"""
        discovered = DiscoveredAlbum(
            album_name="Pushpa 3",
            normalized_name="pushpa 3",
            source_url="https://naasongs.com.co/pushpa-3.html",
            songs=[
                DiscoveredSong(song_title="Jaalilo Jaalilo", normalized_title="jaalilo jaalilo", track_number=1),
                DiscoveredSong(song_title="The Wild Fire", normalized_title="the wild fire", track_number=2),
                DiscoveredSong(song_title="Rampage Theme", normalized_title="rampage theme", track_number=3),
                DiscoveredSong(song_title="Pushpa Reloaded", normalized_title="pushpa reloaded", track_number=4)
            ]
        )

        notif_type, analyzed = self.comparator.compare_album(discovered)
        self.assertEqual(notif_type, NotificationType.MISSING_SONGS)
        missing = [s for s in analyzed.songs if not s.exists_in_catalog]
        self.assertEqual(len(missing), 2)
        missing_titles = [s.song_title for s in missing]
        self.assertIn("Rampage Theme", missing_titles)
        self.assertIn("Pushpa Reloaded", missing_titles)

    def test_case_e_fuzzy_matching_and_uncertainty(self):
        """Case E: Minor spelling variations vs uncertain matches"""
        # Minor variation: 'Jaalilo Jaalilo Song' should match 'Jaalilo Jaalilo' with HIGH confidence
        discovered = DiscoveredAlbum(
            album_name="Pushpa 3",
            normalized_name="pushpa 3",
            source_url="https://naasongs.com.co/pushpa-3.html",
            songs=[
                DiscoveredSong(song_title="Jaalilo Jaalilo Song", normalized_title="jaalilo jaalilo song", track_number=1)
            ]
        )
        notif_type, analyzed = self.comparator.compare_album(discovered)
        self.assertTrue(analyzed.songs[0].exists_in_catalog)
        self.assertIn(analyzed.songs[0].match_confidence, [MatchConfidence.HIGH, MatchConfidence.EXACT])

    def test_case_f_album_updated_artwork(self):
        """Case F: Existing album has all songs, but new artwork discovered -> ALBUM_UPDATED"""
        discovered = DiscoveredAlbum(
            album_name="Khaidi No 150", # Has image=None in mock catalog
            normalized_name="khaidi no 150",
            source_url="https://naasongs.com.co/khaidi-no-150.html",
            artwork_url="https://naasongs.com.co/khaidi-cover.jpg",
            songs=[
                DiscoveredSong(song_title="Ammadu Lets Do Kummudu", normalized_title="ammadu lets do kummudu"),
                DiscoveredSong(song_title="Sundari", normalized_title="sundari"),
                DiscoveredSong(song_title="You And Me", normalized_title="you and me")
            ]
        )
        notif_type, analyzed = self.comparator.compare_album(discovered)
        self.assertEqual(notif_type, NotificationType.ALBUM_UPDATED)

if __name__ == "__main__":
    unittest.main()
