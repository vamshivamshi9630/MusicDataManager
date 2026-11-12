import re
import html
import urllib.parse
from typing import List, Dict, Any, Optional, Tuple
from bs4 import BeautifulSoup, Tag, NavigableString
from backend.services.fuzzy_search import normalize_text
from backend.services.latest_releases.models import DiscoveredAlbum, DiscoveredSong, LanguageConfidence


class AlbumParser:
    """
    Structure-aware HTML DOM parser for naasongs.com.co homepage and album detail pages.
    """

    KNOWN_LANGUAGES = [
        "Telugu", "Tamil", "Hindi", "Kannada", "Malayalam",
        "Bengali", "Marathi", "Punjabi", "English"
    ]

    ALLOWED_DOWNLOAD_DOMAINS = (
        "naasongs.com.co",
        "mp3teluguwap.net",
        "sensongsmp3.tv",
        "sensongsmp3.co",
    )

    PROHIBITED_SONG_TITLES = {
        "download", "listen", "play", "download song", "listen online",
        "audio songs download", "mp3 download", "album download",
        "free download", "songs download", "mp3 songs download",
        "click here", "source", "upcoming", "songs", "song",
        "128 kbps", "320 kbps", "192 kbps", "64 kbps", "256 kbps", "kbps"
    }

    ACTION_ANCHOR_LABELS = {
        "download", "listen", "play", "click here", "direct download",
        "listen online", "128 kbps", "320 kbps"
    }

    NOISE_CONTAINER_HINTS = (
        "sidebar", "secondary", "widget", "related", "recent-posts",
        "comments", "comment", "footer", "navigation", "nav-", "menu-",
        "entry-footer", "entry-tags", "sharedaddy", "yarpp", "tagcloud"
    )

    AUDIO_EXTENSIONS = (".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg")

    @classmethod
    def is_allowed_download_domain(cls, url: str) -> bool:
        try:
            parsed = urllib.parse.urlparse(url)
            hostname = (parsed.hostname or "").lower()
            if not hostname or hostname in ("localhost", "127.0.0.1", "::1"):
                return False
            if hostname.startswith("192.168.") or hostname.startswith("10.") or hostname.startswith("172."):
                return False
            for allowed in cls.ALLOWED_DOWNLOAD_DOMAINS:
                if hostname == allowed or hostname.endswith("." + allowed):
                    return True
            return False
        except Exception:
            return False

    @classmethod
    def _is_valid_song_title(cls, candidate: str) -> bool:
        if not candidate or not candidate.strip():
            return False
        low = candidate.strip().lower()
        low = re.sub(r'\s+', ' ', low)
        if len(low) < 2:
            return False
        if low in cls.PROHIBITED_SONG_TITLES:
            return False
        if re.match(r'^\d+\s*kbps$', low):
            return False
        if any(low == lbl or low.endswith(f" {lbl}") for lbl in (
            "audio songs download", "mp3 download", "album download",
            "free download", "songs download", "mp3 songs download"
        )):
            return False
        # Skip metadata fields if accidentally encountered
        if re.match(r'^(?:movie\s+(?:title|name)|cast(?:\s+crew)?|starring|music|director|language|year|artist|singers?)\s*[:-]', low):
            return False
        # Skip SEO keyword dump lines (multiple comma-separated download phrases)
        if low.count(",") >= 2 and any(k in low for k in ("download", "ringtones", "gaana", "jiomusic", "listen online")):
            return False
        return True

    @classmethod
    def _clean_display_song_title(cls, raw: str) -> str:
        text = html.unescape(raw or "").replace("\xa0", " ").strip()
        text = re.sub(r'^\s*0*\d{1,2}\s*[\.\)\-:]\s*', '', text).strip()
        text = re.sub(r'[\-_:\.\s]+$', '', text).strip()
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    @classmethod
    def _clean_singer_text(cls, raw: str) -> Optional[str]:
        if not raw:
            return None
        text = html.unescape(raw).replace("\xa0", " ").strip()
        text = re.sub(r'^(?:Artists?|Singers?|Vocals?)\s*[:-]\s*', '', text, flags=re.IGNORECASE).strip()
        text = re.sub(r'^[\-_:\.\s]+|[\-_:\.\s]+$', '', text).strip()
        text = re.sub(r'\s+', ' ', text).strip()
        low = text.lower()
        if not text or text == "-" or low in cls.PROHIBITED_SONG_TITLES or re.match(r'^\d+\s*kbps$', low):
            return None
        if any(k in low for k in ("download", "click here", "128 kbps", "320 kbps")):
            # If download keyword leaked into singer text, reject
            return None
        return text

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

    @classmethod
    def _is_noise_element(cls, tag: Tag) -> bool:
        if not isinstance(tag, Tag):
            return False
        name = (tag.name or "").lower()
        if name in ("script", "style", "noscript", "aside", "nav", "footer", "form", "button", "svg"):
            return True
        if name == "header" and tag.get("id") == "masthead":
            return True
        tid = (tag.get("id") or "").lower()
        classes = " ".join(tag.get("class") or []).lower()
        role = (tag.get("role") or "").lower()
        if role in ("complementary", "navigation", "banner", "contentinfo"):
            return True
        combined = f"{tid} {classes}"
        return any(hint in combined for hint in cls.NOISE_CONTAINER_HINTS)

    @staticmethod
    def parse_homepage_albums(html_content: str) -> List[Dict[str, Any]]:
        """
        Extracts recent album links and thumbnails from homepage or paginated index.
        Excludes sidebar/navigation/footer containers.
        Returns list of dicts: {"url": str, "title": str, "thumbnail": Optional[str]}
        """
        if not html_content:
            return []

        soup = BeautifulSoup(html_content, "html.parser")
        for tag in list(soup.find_all(True)):
            if getattr(tag, "attrs", None) is not None and AlbumParser._is_noise_element(tag):
                tag.decompose()

        candidates = []
        seen_urls = set()

        ignored_slugs = [
            "/category/", "/tag/", "/contact", "/privacy", "/disclaimer",
            "/dmca", "/about", "/author/", "/page/"
        ]

        for a_tag in soup.find_all("a", href=True):
            href_clean = a_tag["href"].strip()
            if not href_clean.startswith("https://naasongs.com.co/"):
                continue
            if "#" in href_clean or href_clean in seen_urls or href_clean == "https://naasongs.com.co/":
                continue
            if any(x in href_clean.lower() for x in ignored_slugs):
                continue
            if not href_clean.endswith(".html"):
                continue

            img_tag = a_tag.find("img", src=True)
            thumb = img_tag["src"].strip() if img_tag else None
            raw_title = html.unescape(a_tag.get_text(" ", strip=True))

            seen_urls.add(href_clean)
            candidates.append({
                "url": href_clean,
                "title": raw_title,
                "thumbnail": thumb
            })

        return candidates

    @classmethod
    def parse_pagination_info(cls, html_content: str, current_url: str = "") -> Dict[str, Any]:
        """
        Inspects WordPress/NaaSongs pagination markup (`div.nav-links`, `a.next.page-numbers`, etc.).
        Returns structured pagination metadata without mutating or failing on minimal HTML snippets.
        """
        default_page = 1
        if current_url:
            m_url = re.search(r'/page/(\d+)(?:/|\?|$)', current_url)
            if m_url:
                try:
                    default_page = int(m_url.group(1))
                except ValueError:
                    pass

        if not html_content:
            return {
                "pagination_present": False,
                "current_page": default_page,
                "next_page_url": None,
                "last_page_number": None,
                "page_numbers": [],
                "has_next": None,
            }

        soup = BeautifulSoup(html_content, "html.parser")
        nav_container = (
            soup.select_one("div.nav-links")
            or soup.select_one("nav.navigation.pagination")
            or soup.select_one("div.wp-pagenavi")
            or soup.select_one("div.pagination")
            or soup.select_one("ul.page-numbers")
        )
        head_next = soup.select_one('link[rel="next"][href]')

        if nav_container is None and head_next is None:
            return {
                "pagination_present": False,
                "current_page": default_page,
                "next_page_url": None,
                "last_page_number": None,
                "page_numbers": [],
                "has_next": None,
            }

        current_page = default_page
        page_numbers: List[int] = []
        next_page_url: Optional[str] = None
        base_for_join = current_url or "https://naasongs.com.co/"

        if nav_container is not None:
            cur_el = nav_container.select_one(".page-numbers.current") or nav_container.select_one("span.current")
            if cur_el:
                cur_txt = re.sub(r'[^\d]', '', cur_el.get_text(" ", strip=True))
                if cur_txt:
                    try:
                        current_page = int(cur_txt)
                        page_numbers.append(current_page)
                    except ValueError:
                        pass

            nxt_el = nav_container.select_one("a.next.page-numbers[href]") or nav_container.select_one('a[rel="next"][href]')
            if nxt_el and nxt_el.get("href"):
                next_page_url = urllib.parse.urljoin(base_for_join, nxt_el["href"].strip())

            for el in nav_container.select("a.page-numbers, a.page, span.page-numbers"):
                classes = " ".join(el.get("class") or []).lower()
                if "next" in classes or "prev" in classes or "dots" in classes:
                    continue
                txt = re.sub(r'[^\d]', '', el.get_text(" ", strip=True))
                if txt:
                    try:
                        page_numbers.append(int(txt))
                    except ValueError:
                        pass

            # Fallback: extract page numbers from /page/<N> hrefs inside nav container
            for a_el in nav_container.find_all("a", href=True):
                m_href = re.search(r'/page/(\d+)(?:/|\?|$)', a_el["href"])
                if m_href:
                    try:
                        page_numbers.append(int(m_href.group(1)))
                    except ValueError:
                        pass

        if not next_page_url and head_next and head_next.get("href"):
            next_page_url = urllib.parse.urljoin(base_for_join, head_next["href"].strip())

        unique_pages = sorted(set(page_numbers))
        last_page_number = max(unique_pages) if unique_pages else None
        has_next = bool(next_page_url) or any(p > current_page for p in unique_pages)

        return {
            "pagination_present": True,
            "current_page": current_page,
            "next_page_url": next_page_url,
            "last_page_number": last_page_number,
            "page_numbers": unique_pages,
            "has_next": has_next,
        }

    @classmethod
    def _find_main_content_root(cls, soup: BeautifulSoup) -> Tag:
        """
        Locates main article/entry-content container and excludes sidebars, SEO footers, navigation.
        """
        content_div = soup.find(
            "div",
            class_=lambda c: c and any("entry-content" in cls_item for cls_item in (c if isinstance(c, list) else str(c).split()))
        )
        if content_div:
            return content_div

        article = soup.find("article")
        if article:
            return article

        main_tag = soup.find("main")
        if main_tag:
            return main_tag

        return soup.body or soup

    @classmethod
    def _extract_logical_lines(cls, root: Tag) -> List[Dict[str, Any]]:
        """
        Walks the HTML DOM of the main content area and emits logical lines split by <br> and block elements,
        keeping exact text and anchor links scoped cleanly to each line.
        """
        BLOCK_TAGS = {
            "p", "div", "li", "tr", "ul", "ol", "table", "tbody", "thead",
            "h1", "h2", "h3", "h4", "h5", "h6", "hr", "blockquote", "section", "article"
        }
        HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

        lines: List[Dict[str, Any]] = []
        buf_full: List[str] = []
        buf_no_act: List[str] = []
        buf_links: List[Dict[str, Any]] = []

        def flush(is_heading: bool = False, block_tag: Optional[str] = None):
            full_t = html.unescape("".join(buf_full)).replace("\xa0", " ")
            full_t = re.sub(r'\s+', ' ', full_t).strip()
            no_act_t = html.unescape("".join(buf_no_act)).replace("\xa0", " ")
            no_act_t = re.sub(r'\s+', ' ', no_act_t).strip()
            if full_t or buf_links:
                lines.append({
                    "full_text": full_t,
                    "text_no_actions": no_act_t,
                    "links": list(buf_links),
                    "is_heading": is_heading,
                    "block_tag": block_tag
                })
            buf_full.clear()
            buf_no_act.clear()
            buf_links.clear()

        def is_action_anchor(text: str) -> bool:
            low = text.strip().lower()
            return (
                low in cls.ACTION_ANCHOR_LABELS
                or low.startswith("download")
                or low.startswith("listen")
                or low.startswith("play")
            )

        def walk(node: Any, current_heading: bool = False):
            if isinstance(node, NavigableString):
                s = str(node)
                buf_full.append(s)
                buf_no_act.append(s)
                return

            if not isinstance(node, Tag):
                return

            if cls._is_noise_element(node):
                return

            tag_name = (node.name or "").lower()

            if tag_name == "br":
                flush(is_heading=current_heading)
                return

            if tag_name == "hr":
                flush(is_heading=True, block_tag="hr")
                lines.append({"full_text": "", "text_no_actions": "", "links": [], "is_heading": True, "block_tag": "hr"})
                return

            if tag_name == "a":
                href = (node.get("href") or "").strip()
                a_text = html.unescape(node.get_text(" ", strip=True)).replace("\xa0", " ").strip()
                action_flag = is_action_anchor(a_text)
                if href:
                    buf_links.append({
                        "href": href,
                        "text": a_text,
                        "is_action": action_flag
                    })
                if a_text:
                    buf_full.append(f" {a_text} ")
                    if not action_flag:
                        buf_no_act.append(f" {a_text} ")
                return

            if tag_name in BLOCK_TAGS:
                flush(is_heading=current_heading)
                child_heading = current_heading or (tag_name in HEADING_TAGS)
                for child in node.children:
                    walk(child, current_heading=child_heading)
                flush(is_heading=child_heading, block_tag=tag_name)
                return

            for child in node.children:
                walk(child, current_heading=current_heading)

        walk(root)
        flush()
        return lines

    @classmethod
    def _detect_bitrate_label(cls, text: str, href: str = "") -> Optional[str]:
        m = re.search(r'\b(64|128|192|256|320)\s*kbps\b', text or "", re.IGNORECASE)
        if m:
            return f"{m.group(1)} Kbps"
        if href:
            decoded = urllib.parse.unquote(href)
            m2 = re.search(r'(64|128|192|256|320)\s*kbps', decoded, re.IGNORECASE)
            if m2:
                return f"{m2.group(1)} Kbps"
        return None

    @classmethod
    def _attach_links_to_track(cls, track: Dict[str, Any], line_text: str, links: List[Dict[str, Any]]) -> None:
        line_kbps = cls._detect_bitrate_label(line_text)
        for lnk in links:
            href = lnk.get("href", "").strip()
            a_text = lnk.get("text", "").strip()
            if not href or href.startswith("#") or href.lower().startswith("javascript:"):
                continue

            parsed_path = urllib.parse.urlparse(href).path.lower()
            if parsed_path.endswith(".html") or parsed_path.endswith(".htm") or "/category/" in parsed_path or "/tag/" in parsed_path:
                continue

            is_audio_file = any(parsed_path.endswith(ext) for ext in cls.AUDIO_EXTENSIONS)
            a_low = a_text.lower()
            is_listen_only = a_low in ("listen", "play", "listen online")
            is_explicit_dl = (not is_listen_only) and (
                "download" in a_low
                or bool(re.search(r'\d+\s*kbps', a_low))
                or (is_audio_file and a_low in ("", "download", "mp3") or is_audio_file)
            )
            if is_listen_only:
                is_explicit_dl = False

            if not (is_audio_file or is_explicit_dl or is_listen_only):
                continue

            track["link_available"] = True
            bitrate = cls._detect_bitrate_label(f"{line_text} {a_text}", href) or line_kbps or "Standard"

            if is_explicit_dl and cls.is_allowed_download_domain(href):
                track["download_authorized"] = True
                if bitrate not in track["download_links"]:
                    track["download_links"][bitrate] = href
                if bitrate not in track["available_bitrates"]:
                    track["available_bitrates"].append(bitrate)

        # Select primary preferred download URL (320 Kbps > 256 > 192 > 128 > Standard)
        if track["download_links"]:
            def bitrate_rank(k: str) -> int:
                m = re.match(r'^(\d+)', k)
                return int(m.group(1)) if m else 1

            best_key = max(track["download_links"].keys(), key=bitrate_rank)
            track["source_audio_url"] = track["download_links"][best_key]

    @classmethod
    def parse_album_page(cls, html_content: str, source_url: str) -> Optional[DiscoveredAlbum]:
        """
        Parses an album detail page into a DiscoveredAlbum instance with its structure-parsed song list.
        """
        if not html_content:
            return None

        soup = BeautifulSoup(html_content, "html.parser")

        # Extract H1 and og:image before cleaning chrome
        h1_tag = soup.find("h1")
        h1_text = html.unescape(h1_tag.get_text(" ", strip=True)) if h1_tag else ""

        og_img_tag = soup.find("meta", property="og:image")
        og_img_url = (og_img_tag.get("content") or "").strip() if og_img_tag else None

        content_root = cls._find_main_content_root(soup)
        content_text = html.unescape(content_root.get_text("\n", strip=True)).replace("\xa0", " ")

        # 1. Movie / Album Name
        raw_album_name = None
        title_meta = re.search(r'Movie\s+(?:Title|Name)\s*:-\s*([^\n\r<]+)', content_text, re.IGNORECASE)
        if not title_meta:
            title_meta = re.search(r'Movie\s+(?:Title|Name):-\s*([^<\n\r]+)', html_content, re.IGNORECASE)
        if title_meta:
            raw_album_name = html.unescape(title_meta.group(1)).strip()

        if not raw_album_name and h1_text:
            raw_album_name = h1_text

        if not raw_album_name:
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
        music_meta = re.search(r'Music\s*:-\s*([^\n\r<]+)', content_text, re.IGNORECASE)
        if not music_meta:
            music_meta = re.search(r'Music:-\s*([^<\n\r]+)', html_content, re.IGNORECASE)
        if music_meta:
            clean_music = html.unescape(music_meta.group(1)).strip()
            if clean_music and clean_music != "-":
                music_director = clean_music

        # 3. Release Year
        year = 2026
        year_meta = re.search(r'Year\s*:-\s*(\d{4})', content_text, re.IGNORECASE)
        if not year_meta:
            year_meta = re.search(r'Year:-\s*(\d{4})', html_content, re.IGNORECASE)
        if year_meta:
            try:
                year = int(year_meta.group(1))
            except ValueError:
                pass

        # 4. Director & Cast
        director = None
        dir_meta = re.search(r'Director\s*:-\s*([^\n\r<]+)', content_text, re.IGNORECASE)
        if dir_meta:
            director = html.unescape(dir_meta.group(1)).strip()

        cast_crew = None
        cast_meta = re.search(r'Cast\s+Crew\s*:-\s*([^\n\r<]+)', content_text, re.IGNORECASE)
        if cast_meta:
            cast_crew = html.unescape(cast_meta.group(1)).strip()

        # 5. Artwork Image URL (scoped inside main content, fallback og:image)
        artwork_url = None
        img_tag = content_root.find("img", src=True)
        if img_tag:
            artwork_url = img_tag["src"].strip()
        elif og_img_url:
            artwork_url = og_img_url

        # 6. Structure-Aware Songs Extraction
        songs = cls._extract_songs(html_content, source_url=source_url)

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
    def _extract_songs(cls, html_content: str, source_url: Optional[str] = None) -> List[DiscoveredSong]:
        """
        Structure-aware DOM parser for song tracks on NaaSongs album pages.
        - Isolates main entry-content container (excludes sidebars, navigation, footers).
        - Extracts numbered tracks, titles, and singer credits.
        - Associates 128 Kbps / 320 Kbps links strictly with their parent track.
        - Handles missing links, missing singers, and varying markup spacing safely.
        """
        if not html_content:
            return []

        soup = BeautifulSoup(html_content, "html.parser")
        content_root = cls._find_main_content_root(soup)
        logical_lines = cls._extract_logical_lines(content_root)

        parsed_tracks: List[Dict[str, Any]] = []
        active_track: Optional[Dict[str, Any]] = None

        for line in logical_lines:
            full_text = line["full_text"]
            text_no_actions = line["text_no_actions"]
            links = line["links"]
            is_heading = line["is_heading"]

            if is_heading:
                active_track = None
                continue

            if not full_text and not links:
                continue

            low = full_text.lower()

            # Reset/skip SEO keyword clouds
            if low.count(",") >= 2 and any(k in low for k in ("download", "ringtones", "gaana", "jiomusic", "listen online")):
                active_track = None
                continue

            # Reset/skip album info metadata fields
            if re.match(r'^(?:movie\s+(?:title|name)|cast(?:\s+crew)?|starring|music(?:\s+director)?|director|language|year|label|banner)\s*[:-]|^[^\n]*Cast\s+Crew\s*:-', full_text, re.IGNORECASE):
                active_track = None
                continue

            # Skip standalone cross-links to other HTML album pages
            track_num_re = r'^\s*0*(\d{1,2})\s*(?:[\.\)\-:]\s+|[\.\)](?=[A-Za-z]))(.+)$'
            is_numbered = bool(re.match(track_num_re, full_text))
            if not is_numbered and links and all(
                urllib.parse.urlparse(l["href"]).path.lower().endswith(".html") or "/category/" in l["href"].lower()
                for l in links if l.get("href")
            ):
                active_track = None
                continue

            # 1. Check Artist / Singer line belonging to active_track
            if active_track and re.match(r'^\s*(?:Artists?|Singers?|Vocals?)\s*[:-]\s*(.+)$', full_text, re.IGNORECASE):
                singer_val = cls._clean_singer_text(full_text)
                if singer_val:
                    active_track["singer"] = singer_val
                continue

            # 2. Check Numbered Song Entry line (excludes bitrate prefixes like "128 Kbps", "320 Kbps")
            if not re.match(r'^\s*\d+\s*kbps\b', full_text, re.IGNORECASE):
                num_match = re.match(track_num_re, text_no_actions or full_text)
                if num_match:
                    track_num = int(num_match.group(1))
                    rest = num_match.group(2).strip()
                    # Strip any trailing separator left after stripping inline action <a>
                    rest = re.sub(r'[\-–—:\s]+$', '', rest).strip()

                    # Split song title and singer credit (prefer en-dash/em-dash first, then spaced hyphen)
                    singer_part = None
                    if re.search(r'[–—]', rest):
                        parts = re.split(r'\s*[–—]\s*', rest, maxsplit=1)
                        raw_song = parts[0].strip()
                        if len(parts) > 1:
                            singer_part = cls._clean_singer_text(parts[1])
                    elif re.search(r'\s+-\s+', rest):
                        parts = re.split(r'\s+-\s+', rest, maxsplit=1)
                        raw_song = parts[0].strip()
                        if len(parts) > 1:
                            singer_part = cls._clean_singer_text(parts[1])
                    else:
                        raw_song = rest

                    clean_title = cls._clean_display_song_title(raw_song)
                    if cls._is_valid_song_title(clean_title):
                        track_dict = {
                            "track_number": track_num,
                            "song_title": clean_title,
                            "singer": singer_part,
                            "download_links": {},
                            "available_bitrates": [],
                            "source_audio_url": None,
                            "link_available": False,
                            "download_authorized": False,
                        }
                        if links:
                            cls._attach_links_to_track(track_dict, full_text, links)
                        parsed_tracks.append(track_dict)
                        active_track = track_dict
                        continue

            # 3. Check Bitrate / Download / Audio link line belonging to active_track
            if active_track is not None:
                has_kbps = bool(re.search(r'\b\d{2,3}\s*kbps\b', full_text, re.IGNORECASE))
                has_audio_or_dl_link = any(
                    any(urllib.parse.urlparse(l["href"]).path.lower().endswith(ext) for ext in cls.AUDIO_EXTENSIONS)
                    or "download" in l.get("text", "").lower()
                    or "listen" in l.get("text", "").lower()
                    for l in links if l.get("href")
                )
                if has_kbps or has_audio_or_dl_link:
                    cls._attach_links_to_track(active_track, full_text, links)
                    continue

        # Convert structured track dicts into DiscoveredSong objects preserving order
        songs: List[DiscoveredSong] = []
        seen_keys = set()
        for t in parsed_tracks:
            title = t["song_title"]
            norm_title = normalize_text(title)
            dedup_key = (t["track_number"], norm_title)
            if not norm_title or dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)

            bitrates = t["available_bitrates"]
            bitrate_str = ", ".join(bitrates) if bitrates else None

            songs.append(
                DiscoveredSong(
                    song_title=title,
                    normalized_title=norm_title,
                    track_number=t["track_number"],
                    source_audio_url=t["source_audio_url"],
                    singer=t["singer"],
                    available_bitrate=bitrate_str,
                    available_bitrates=bitrates,
                    download_links=t["download_links"],
                    source_page_url=source_url,
                    link_available=t["link_available"],
                    download_authorized=t["download_authorized"],
                )
            )

        return songs

    @staticmethod
    def _clean_song_title(raw_title: str, fallback_track: int = 1) -> Tuple[str, int]:
        """
        Legacy normalization helper retained for backward compatibility.
        """
        text = html.unescape(raw_title).replace("\xa0", " ").strip()
        track_num = fallback_track

        num_match = re.match(r'^(\d{1,2})[\.\)\-]\s*', text)
        if num_match:
            try:
                track_num = int(num_match.group(1))
            except ValueError:
                pass
            text = text[num_match.end():].strip()

        dash_parts = re.split(r'&#8211;|-|–|—|\u2013|\u2014', text)
        if dash_parts:
            text = dash_parts[0].strip()

        text = re.sub(r'\s*(?:Full\s+)?Song(?:s)?\s*$', '', text, flags=re.IGNORECASE)
        text = re.sub(r'\s*(?:Lyrical|Audio|Video)\s*$', '', text, flags=re.IGNORECASE)
        text = re.sub(r'\s*\(\d{4}\)\s*$', '', text)
        text = re.sub(r'[\-_:\.]+$', '', text).strip()

        return text, track_num
