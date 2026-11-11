import unittest
from backend.services.latest_releases.album_parser import AlbumParser
from backend.services.latest_releases.models import DiscoveredAlbum, DiscoveredSong

SAMPLE_HOMEPAGE_HTML = """
<!DOCTYPE html>
<html>
<body>
<div class="content">
    <a href="https://naasongs.com.co/tyson-naidu-2026-songs-a.html">
        <img src="https://naasongs.com.co/wp-content/uploads/2026/11/Tyson-Naidu-2026-Telugu-350x280.jpg" />
        Tyson Naidu (2026)
    </a>
    <a href="https://naasongs.com.co/vrushakarma-2026-songs-b.html">
        <img src="https://naasongs.com.co/wp-content/uploads/2026/11/Vrushakarma-2026-Telugu-350x280.jpg" />
        Vrushakarma (2026)
    </a>
    <a href="https://naasongs.com.co/category/telugu">Telugu Category</a>
    <a href="https://naasongs.com.co/about">About Us</a>
</div>
</body>
</html>
"""

SAMPLE_MULTI_SONG_HTML = """
<!DOCTYPE html>
<html>
<body>
<article class="post">
    <h1>Aakasamlo Oka Tara (2026)</h1>
    <div class="entry-content clearfix">
        <p>
            <img src="https://naasongs.com.co/wp-content/uploads/2026/07/Aakasamlo-Oka-Tara-Songs.jpg" /><br />
            Movie Title:-Aakasamlo Oka Tara<br />
            Cast Crew:-Dulquer Salmaan, Pooja Hegde<br />
            Music:-GV Prakash Kumar<br />
            Director:-Pavan Sadineni<br />
            Language:-Telugu<br />
            Year:-2026
        </p>
        <h2>Aakasamlo Oka Tara Audio Songs Download</h2>
        <p>01. Chitti Chitti Cheema Song &#8211; <a href="https://mp3teluguwap.net/mp3/2026/Aakasamlo/Chitti_Chitti_Cheema.mp3">Download</a><br />
           Artist:-GV Prakash Kumar, Ramya Behara</p>
        <p>02. Radhe Radhe Song &#8211; <a href="https://mp3teluguwap.net/mp3/2026/Aakasamlo/Radhe_Radhe.mp3">Download</a><br />
           Artist:-Sid Sriram</p>
    </div>
</article>
</body>
</html>
"""

SAMPLE_UPCOMING_PLAIN_HTML = """
<!DOCTYPE html>
<html>
<body>
<article class="post">
    <div class="entry-content clearfix">
        <p>
            <img src="https://naasongs.com.co/wp-content/uploads/2026/07/Dada-2026Movie.jpg" /><br />
            Dada Cast Crew:-<br />
            Movie Name:-Dada<br />
            Cast Crew:-Balakrishna, Kajal Aggarwal<br />
            Music:-SS Thaman<br />
            Director:-Gopichand Malineni<br />
            Year:-2026
        </p>
        <h2>Dada Audio Songs Download</h2>
        <p>01. Dada Dhamaka Song &#8211; SS Thaman<br /> 128 Kbps &#8211; Download<br /> 320 Kbps &#8211; Download</p>
    </div>
</article>
</body>
</html>
"""

class TestLatestReleasesParser(unittest.TestCase):

    def test_parse_homepage_albums(self):
        albums = AlbumParser.parse_homepage_albums(SAMPLE_HOMEPAGE_HTML)
        self.assertEqual(len(albums), 2)
        urls = [a["url"] for a in albums]
        self.assertIn("https://naasongs.com.co/tyson-naidu-2026-songs-a.html", urls)
        self.assertIn("https://naasongs.com.co/vrushakarma-2026-songs-b.html", urls)

    def test_parse_multi_song_album(self):
        url = "https://naasongs.com.co/aakasamlo-oka-tara-2026-songs-b.html"
        album = AlbumParser.parse_album_page(SAMPLE_MULTI_SONG_HTML, url)
        self.assertIsNotNone(album)
        self.assertEqual(album.album_name, "Aakasamlo Oka Tara")
        self.assertEqual(album.music_director, "GV Prakash Kumar")
        self.assertEqual(album.year, 2026)
        self.assertEqual(album.artwork_url, "https://naasongs.com.co/wp-content/uploads/2026/07/Aakasamlo-Oka-Tara-Songs.jpg")
        self.assertEqual(len(album.songs), 2)

        s1 = album.songs[0]
        self.assertEqual(s1.song_title, "Chitti Chitti Cheema")
        self.assertEqual(s1.track_number, 1)
        self.assertIn("Chitti_Chitti_Cheema.mp3", s1.source_audio_url)

        s2 = album.songs[1]
        self.assertEqual(s2.song_title, "Radhe Radhe")
        self.assertEqual(s2.track_number, 2)
        self.assertIn("Radhe_Radhe.mp3", s2.source_audio_url)

    def test_parse_upcoming_album_plain_text(self):
        url = "https://naasongs.com.co/dada-2026-songs-2.html"
        album = AlbumParser.parse_album_page(SAMPLE_UPCOMING_PLAIN_HTML, url)
        self.assertIsNotNone(album)
        self.assertEqual(album.album_name, "Dada")
        self.assertEqual(album.music_director, "SS Thaman")
        self.assertEqual(len(album.songs), 1)
        self.assertEqual(album.songs[0].song_title, "Dada Dhamaka")
        self.assertIsNone(album.songs[0].source_audio_url)

    def test_clean_song_title(self):
        title1, t1 = AlbumParser._clean_song_title("01. Madarasi Rabbaru Bomma Song")
        self.assertEqual(title1, "Madarasi Rabbaru Bomma")
        self.assertEqual(t1, 1)

        title2, t2 = AlbumParser._clean_song_title("2) Swag of Arjun Lyrical")
        self.assertEqual(title2, "Swag of Arjun")
        self.assertEqual(t2, 2)

        title3, t3 = AlbumParser._clean_song_title("Pushpa Pushpa Full Song")
        self.assertEqual(title3, "Pushpa Pushpa")

if __name__ == "__main__":
    unittest.main()
