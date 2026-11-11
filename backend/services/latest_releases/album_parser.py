import re
import html
from typing import List, Dict, Any, Optional, Tuple
from backend.services.fuzzy_search import normalize_text
from backend.services.latest_releases.models import DiscoveredAlbum, DiscoveredSong, LanguageConfidence

class AlbumParser:
    """
    Parses HTML content from naasongs.com.co homepage and album detail pages.
    """

    KNOWN_LANGUAGES = [
        "Telugu", "Tamil", "Hindi", "Kannada", "Malayalam",
        "Bengali", "Marathi", "Punjabi", "English"
    ]

    @classmethod
    def extract_language_and_clean_title(
        cls,
        html_content: str,
        source_url: str,
        raw_name: str
    ) -> Tuple[str, str, LanguageConfidence]:
        """
        Extracts album language with confidence level and strips language tags from raw_name.
        Returns: (clean_album_name, normalized_language, LanguageConfidence)
        """
        detected_language = None
        confidence = LanguageConfidence.UNKNOWN
        clean_name = raw_name

        # 1. Tier 1: Explicit metadata in HTML: "Language:- Tamil"
        lang_meta = re.search(r'Language:-\s*([^<\n\r]+)', html_content, re.IGNORECASE)
        if lang_meta:
            candidate = html.unescape(lang_meta.group(1)).strip().title()
            for lang in cls.KNOWN_LANGUAGES:
                if lang.lower() in candidate.lower():
                    detected_language = lang
                    confidence = LanguageConfidence.HIGH
                    break
            if not detected_language and candidate and candidate != "-":
                detected_language = candidate
                confidence = LanguageConfidence.MEDIUM

        # 2. Tier 2: Check H1 or raw_name suffix/infix (e.g., "– Tamil", "(Tamil)")
        h1_text = ""
        h1_match = re.search(r'<h1[^>]*>(.*?)</h1>', html_content, re.DOTALL | re.IGNORECASE)
        if h1_match:
            h1_text = html.unescape(re.sub(r'<[^>]+>', '', h1_match.group(1))).strip()

        combined_title_context = f"{clean_name} {h1_text}"
        for lang in cls.KNOWN_LANGUAGES:
            pattern = rf'(?:&#8211;|-|–|—|\u2013|\u2014|\(|\/)\s*{lang}\b'
            if re.search(pattern, combined_title_context, re.IGNORECASE):
                if not detected_language or confidence in (LanguageConfidence.LOW, LanguageConfidence.UNKNOWN):
                    detected_language = lang
                    confidence = LanguageConfidence.HIGH
                break

        # 3. Tier 3: URL category, tags, or slug
        if not detected_language:
            for lang in cls.KNOWN_LANGUAGES:
                lang_l = lang.lower()
                if f"/category/{lang_l}" in source_url.lower() or f"/tag/{lang_l}" in source_url.lower() or f"-{lang_l}-" in source_url.lower():
                    detected_language = lang
                    confidence = LanguageConfidence.MEDIUM
                    break

        # 4. Tier 4: Fallback (NaaSongs is predominantly Telugu unless specified)
        if not detected_language:
            detected_language = "Telugu"
            confidence = LanguageConfidence.LOW

        # Clean title of any trailing/embedded language suffixes and year tags
        for lang in cls.KNOWN_LANGUAGES:
            clean_name = re.split(rf'(?:&#8211;|-|–|—|\u2013|\u2014)\s*{lang}\b', clean_name, flags=re.IGNORECASE)[0].strip()
            clean_name = re.sub(rf'\(\s*{lang}\s*\)', '', clean_name, flags=re.IGNORECASE).strip()

        clean_name = re.sub(r'\s*\(\d{4}\).*$', '', clean_name).strip()
        clean_name = re.sub(r'\s*Songs.*$', '', clean_name, flags=re.IGNORECASE).strip()
        clean_name = re.sub(r'[\-_:\.]+$', '', clean_name).strip()
        clean_name = re.sub(r'\s+', ' ', clean_name).strip()

        return clean_name, detected_language, confidence

    @staticmethod
    def parse_homepage_albums(html_content: str) -> List[Dict[str, Any]]:
        """
        Extracts recent album links and thumbnails from homepage or paginated index.
        Returns list of dicts: {"url": str, "title": str, "thumbnail": Optional[str]}
        """
        if not html_content:
            return []

        # Find all link tags pointing to naasongs.com.co HTML pages
        matches = re.findall(
            r'<a\s+[^>]*href=["\'](https://naasongs\.com\.co/[^"\'#]+)["\'][^>]*>(.*?)</a>',
            html_content,
            re.DOTALL
        )
        candidates = []
        seen_urls = set()

        ignored_slugs = [
            "/category/", "/tag/", "/contact", "/privacy", "/disclaimer",
            "/dmca", "/about", "/author/", "/page/"
        ]

        for href, inner in matches:
            href_clean = href.strip()
            if href_clean in seen_urls or href_clean == "https://naasongs.com.co/":
                continue
            if any(x in href_clean.lower() for x in ignored_slugs):
                continue
            if not href_clean.endswith(".html"):
                continue

            # Extract thumbnail image if present inside link
            img_match = re.search(r'<img\s+[^>]*src=["\']([^"\']+)["\']', inner)
            thumb = img_match.group(1).strip() if img_match else None

            # Clean inner text
            raw_title = re.sub(r'<[^>]+>', '', inner).strip()
            raw_title = html.unescape(raw_title)

            seen_urls.add(href_clean)
            candidates.append({
                "url": href_clean,
                "title": raw_title,
                "thumbnail": thumb
            })

        return candidates

    @classmethod
    def parse_album_page(cls, html_content: str, source_url: str) -> Optional[DiscoveredAlbum]:
        """
        Parses an album detail page into a DiscoveredAlbum instance with its song list.
        """
        if not html_content:
            return None

        # 1. Movie / Album Name
        raw_album_name = None
        title_meta = re.search(r'Movie\s+(?:Title|Name):-\s*([^<\n\r]+)', html_content, re.IGNORECASE)
        if title_meta:
            raw_album_name = html.unescape(title_meta.group(1)).strip()

        if not raw_album_name:
            h1_match = re.search(r'<h1[^>]*>(.*?)</h1>', html_content, re.DOTALL | re.IGNORECASE)
            if h1_match:
                raw_album_name = html.unescape(re.sub(r'<[^>]+>', '', h1_match.group(1))).strip()

        if not raw_album_name:
            # Fallback to URL slug
            slug = source_url.split("/")[-1].replace(".html", "")
            slug = re.sub(r'-\d{4}-.*$', '', slug)
            raw_album_name = slug.replace("-", " ").title()

        # Extract Language & Clean Album Name
        album_name, language, language_confidence = cls.extract_language_and_clean_title(
            html_content, source_url, raw_album_name
        )
        normalized_name = normalize_text(album_name)

        # 2. Music Director
        music_director = "Unknown"
        music_meta = re.search(r'Music:-\s*([^<\n\r]+)', html_content, re.IGNORECASE)
        if music_meta:
            clean_music = html.unescape(music_meta.group(1)).strip()
            if clean_music and clean_music != "-":
                music_director = clean_music

        # 3. Release Year
        year = 2026
        year_meta = re.search(r'Year:-\s*(\d{4})', html_content, re.IGNORECASE)
        if year_meta:
            try:
                year = int(year_meta.group(1))
            except ValueError:
                pass

        # 4. Director & Cast
        director = None
        dir_meta = re.search(r'Director:-\s*([^<\n\r]+)', html_content, re.IGNORECASE)
        if dir_meta:
            director = html.unescape(dir_meta.group(1)).strip()

        cast_crew = None
        cast_meta = re.search(r'Cast\s+Crew:-\s*([^<\n\r]+)', html_content, re.IGNORECASE)
        if cast_meta:
            cast_crew = html.unescape(cast_meta.group(1)).strip()

        # 5. Artwork Image URL
        artwork_url = None
        img_match = re.search(r'<div class="entry-content[^"]*">.*?<img[^>]*src="([^"]+)"', html_content, re.DOTALL | re.IGNORECASE)
        if img_match:
            artwork_url = img_match.group(1).strip()
        else:
            og_img = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', html_content, re.IGNORECASE)
            if og_img:
                artwork_url = og_img.group(1).strip()

        # 6. Songs Extraction
        songs = cls._extract_songs(html_content)

        return DiscoveredAlbum(
            album_name=album_name,
            normalized_name=normalized_name,
            source_url=source_url,
            music_director=music_director,
            year=year,
            language=language,
            language_confidence=language_confidence,
            director=director,
            cast_crew=cast_crew,
            artwork_url=artwork_url,
            songs=songs
        )

    @classmethod
    def _extract_songs(cls, html_content: str) -> List[DiscoveredSong]:
        """
        Extracts song entries from the audio download section of the page.
        Supports:
        - Download links with audio URLs: "01. Song Name - <a href="...">Download</a>"
        - Text announcements for upcoming tracks: "01. Song Name - SS Thaman"
        """
        songs: List[DiscoveredSong] = []
        seen_normalized = set()

        # Pattern A: "01. Song Name - <a href="...">Download</a>"
        pattern_a = re.compile(
            r'(\d+[\.\)]\s*[^<]+?)\s*(?:&#8211;|-|:)\s*<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(?:Download|Listen)</a>',
            re.IGNORECASE
        )
        for match in pattern_a.finditer(html_content):
            raw_title_block = match.group(1)
            audio_url = match.group(2).strip()

            clean_title, track_num = cls._clean_song_title(raw_title_block)
            norm_title = normalize_text(clean_title)

            if norm_title and norm_title not in seen_normalized:
                seen_normalized.add(norm_title)
                songs.append(
                    DiscoveredSong(
                        song_title=clean_title,
                        normalized_title=norm_title,
                        track_number=track_num,
                        source_audio_url=audio_url
                    )
                )

        # Pattern B: Direct MP3 link tags inside entry-content if Pattern A found nothing
        if not songs:
            pattern_b = re.compile(
                r'<a\s+[^>]*href=["\']([^"\']+\.mp3)["\'][^>]*>([^<]*)</a>',
                re.IGNORECASE
            )
            for idx, match in enumerate(pattern_b.finditer(html_content), start=1):
                audio_url = match.group(1).strip()
                raw_text = match.group(2).strip() or audio_url.split("/")[-1].replace(".mp3", "")
                clean_title, track_num = cls._clean_song_title(raw_text, fallback_track=idx)
                norm_title = normalize_text(clean_title)

                if norm_title and norm_title not in seen_normalized:
                    seen_normalized.add(norm_title)
                    songs.append(
                        DiscoveredSong(
                            song_title=clean_title,
                            normalized_title=norm_title,
                            track_number=track_num,
                            source_audio_url=audio_url
                        )
                    )

        # Pattern C: Plain text listings (e.g. upcoming songs before audio drops)
        if not songs:
            pattern_c = re.compile(
                r'<p>\s*(\d{1,2}[\.\)]\s*[^<\n\r]+?)(?:<br\s*/?>|</p>)',
                re.IGNORECASE
            )
            for match in pattern_c.finditer(html_content):
                line = match.group(1).strip()
                if any(x in line.lower() for x in ["download", "artist", "singers", "cast", "movie"]):
                    # If line has artist or download, split at dash
                    parts = re.split(r'&#8211;|-', line)
                    candidate = parts[0].strip()
                else:
                    candidate = line

                clean_title, track_num = cls._clean_song_title(candidate)
                norm_title = normalize_text(clean_title)
                if norm_title and len(norm_title) > 2 and norm_title not in seen_normalized:
                    seen_normalized.add(norm_title)
                    songs.append(
                        DiscoveredSong(
                            song_title=clean_title,
                            normalized_title=norm_title,
                            track_number=track_num,
                            source_audio_url=None
                        )
                    )

        return songs

    @staticmethod
    def _clean_song_title(raw_title: str, fallback_track: int = 1) -> Tuple[str, int]:
        """
        Cleans track numbers, 'Song' suffix, and formatting artifacts.
        E.g. '01. Madarasi Rabbaru Bomma Song' -> ('Madarasi Rabbaru Bomma', 1)
        """
        text = html.unescape(raw_title).strip()
        track_num = fallback_track

        # Extract track number prefix (e.g. "01.", "1)", "2 -")
        num_match = re.match(r'^(\d{1,2})[\.\)\-]\s*', text)
        if num_match:
            try:
                track_num = int(num_match.group(1))
            except ValueError:
                pass
            text = text[num_match.end():].strip()

        # Split on dash/en-dash if artist/singer is attached (e.g. "Song Name - SS Thaman")
        dash_parts = re.split(r'&#8211;|-|–|—|\u2013|\u2014', text)
        if dash_parts:
            text = dash_parts[0].strip()

        # Strip common trailing tags like 'Song', 'Audio', 'Full Song', 'Lyrical'
        text = re.sub(r'\s*(?:Full\s+)?Song(?:s)?\s*$', '', text, flags=re.IGNORECASE)
        text = re.sub(r'\s*(?:Lyrical|Audio|Video)\s*$', '', text, flags=re.IGNORECASE)
        text = re.sub(r'\s*\(\d{4}\)\s*$', '', text)
        text = re.sub(r'[\-_:\.]+$', '', text).strip()

        return text, track_num
