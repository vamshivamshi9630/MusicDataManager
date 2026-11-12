import os
import re
import time
import json
import socket
import logging
import urllib.request
import urllib.error
import urllib.parse
from difflib import SequenceMatcher
from typing import List, Dict, Any, Optional, Tuple
from backend.core.config import settings
from backend.services.fuzzy_search import search_albums_fuzzy, check_near_duplicate_album

logger = logging.getLogger(__name__)


class CatalogLookupError(Exception):
    """Raised when authoritative catalog metadata cannot be verified from GitHub."""

    def __init__(
        self,
        message: str,
        reason: str = "catalog_unavailable",
        status_code: Optional[int] = None,
        path: Optional[str] = None,
    ):
        super().__init__(message)
        self.reason = reason
        self.status_code = status_code
        self.path = path


class CloudCatalogService:
    """Fetches and caches dynamic catalog metadata directly from GitHub in Cloud Mode."""

    CACHE_TTL_SECONDS = 60
    MAX_STALE_CACHE_AGE_SECONDS = 3600

    KNOWN_LANGUAGES = [
        "Telugu", "Tamil", "Hindi", "Kannada", "Malayalam",
        "Bengali", "Marathi", "Punjabi", "English"
    ]

    def __init__(self, owner: Optional[str] = None, repo: Optional[str] = None, branch: Optional[str] = None):
        self._owner_override = owner
        self._repo_override = repo
        self._branch_override = branch
        self._raw_base_url_override: Optional[str] = None
        self._stats_cache: Optional[Dict[str, Any]] = None
        self._albums_index_cache: Optional[List[Dict[str, Any]]] = None
        self._artists_index_cache: Optional[List[Dict[str, Any]]] = None
        self._album_metadata_cache: Dict[str, Dict[str, Any]] = {}
        self._last_fetch_time: float = 0.0
        self._cache_is_stale: bool = False
        self._last_catalog_error: Optional[Dict[str, Any]] = None

    @property
    def owner(self) -> str:
        return self._owner_override or settings.GITHUB_OWNER

    @owner.setter
    def owner(self, value: Optional[str]):
        self._owner_override = value

    @property
    def repo(self) -> str:
        return self._repo_override or settings.MUSICDATA_GITHUB_REPOSITORY

    @repo.setter
    def repo(self, value: Optional[str]):
        self._repo_override = value

    @property
    def branch(self) -> str:
        return self._branch_override or settings.GITHUB_BRANCH

    @branch.setter
    def branch(self, value: Optional[str]):
        self._branch_override = value

    @property
    def raw_base_url(self) -> str:
        if self._raw_base_url_override:
            return self._raw_base_url_override.rstrip("/")
        if self._owner_override is None and self._repo_override is None and self._branch_override is None:
            return settings.musicdata_raw_base_url
        return f"https://raw.githubusercontent.com/{self.owner}/{self.repo}/{self.branch}"

    @raw_base_url.setter
    def raw_base_url(self, value: Optional[str]):
        self._raw_base_url_override = value

    def refresh_catalog(self):
        """Invalidate in-memory caches to force fetching fresh metadata from GitHub."""
        self._stats_cache = None
        self._albums_index_cache = None
        self._artists_index_cache = None
        self._album_metadata_cache.clear()
        self._last_fetch_time = 0.0
        self._cache_is_stale = False
        self._last_catalog_error = None

    def get_cache_status(self) -> Dict[str, Any]:
        now = time.time()
        age = round(now - self._last_fetch_time, 1) if self._last_fetch_time else None
        return {
            "cached": self._albums_index_cache is not None,
            "stale": self._cache_is_stale,
            "age_seconds": age,
            "last_error": self._last_catalog_error,
            "repository": f"{self.owner}/{self.repo}",
            "branch": self.branch,
        }

    @staticmethod
    def _sanitize_url_for_log(url: str) -> str:
        try:
            parsed = urllib.parse.urlsplit(url)
            host = parsed.hostname or ""
            if parsed.port:
                host = f"{host}:{parsed.port}"
            return urllib.parse.urlunsplit((parsed.scheme, host, parsed.path, "", ""))
        except Exception:
            return url.split("?")[0]

    def _fetch_json(self, path: str, bypass_cache: bool = True) -> Any:
        clean_path = path.lstrip("/")
        quoted_path = urllib.parse.quote(clean_path, safe="/:")
        t_param = f"?t={int(time.time())}" if bypass_cache else ""
        url = f"{self.raw_base_url}/{quoted_path}{t_param}"
        safe_url = self._sanitize_url_for_log(url)
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "MusicData-Manager-Cloud/1.0",
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache"
            }
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw_body = resp.read().decode("utf-8")
            return json.loads(raw_body)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                msg = (
                    f"Catalog resource not found (HTTP 404) at {safe_url} "
                    f"(repo={self.owner}/{self.repo}, branch={self.branch})."
                )
                logger.error(f"[CloudCatalogService] {msg}")
                raise CatalogLookupError(msg, reason="not_found", status_code=404, path=clean_path) from e
            if e.code in (401, 403):
                msg = f"GitHub authentication/authorization failed (HTTP {e.code}) fetching {safe_url}."
                logger.error(f"[CloudCatalogService] {msg}")
                raise CatalogLookupError(msg, reason="auth_error", status_code=e.code, path=clean_path) from e
            msg = f"GitHub HTTP {e.code} error fetching {safe_url}: {e.reason}"
            logger.error(f"[CloudCatalogService] {msg}")
            raise CatalogLookupError(msg, reason="http_error", status_code=e.code, path=clean_path) from e
        except (socket.timeout, TimeoutError) as e:
            msg = f"Network timeout fetching {safe_url} from GitHub: {e}"
            logger.error(f"[CloudCatalogService] {msg}")
            raise CatalogLookupError(msg, reason="timeout", path=clean_path) from e
        except urllib.error.URLError as e:
            if isinstance(getattr(e, "reason", None), (socket.timeout, TimeoutError)) or "timed out" in str(e.reason).lower():
                msg = f"Network timeout fetching {safe_url} from GitHub: {e.reason}"
                logger.error(f"[CloudCatalogService] {msg}")
                raise CatalogLookupError(msg, reason="timeout", path=clean_path) from e
            msg = f"Network error fetching {safe_url} from GitHub: {e.reason}"
            logger.error(f"[CloudCatalogService] {msg}")
            raise CatalogLookupError(msg, reason="network_error", path=clean_path) from e
        except json.JSONDecodeError as e:
            msg = f"Invalid JSON received from {safe_url}: {e}"
            logger.error(f"[CloudCatalogService] {msg}")
            raise CatalogLookupError(msg, reason="invalid_json", path=clean_path) from e
        except CatalogLookupError:
            raise
        except Exception as e:
            msg = f"Unexpected catalog lookup failure for {safe_url}: {e}"
            logger.error(f"[CloudCatalogService] {msg}")
            raise CatalogLookupError(msg, reason="catalog_unavailable", path=clean_path) from e

    def get_statistics(self) -> Dict[str, Any]:
        try:
            raw_stats = self._fetch_json("metadata/statistics.json")
            albums_idx = self.get_albums_index()
            png_count = sum(1 for a in albums_idx if a.get("image"))
            return {
                "albums": raw_stats.get("totalAlbums", len(albums_idx)),
                "songs": raw_stats.get("totalSongs", 0),
                "png_artwork": png_count,
                "zero_byte_shield": "Active",
                "mode": "CLOUD",
                "catalog_stale": self._cache_is_stale,
            }
        except CatalogLookupError as stat_err:
            # Allow index-derived statistics only if catalog index itself is valid
            if stat_err.path == "metadata/indexes/albums.json":
                raise
            albums_idx = self.get_albums_index()
            total_songs = sum(a.get("songCount", 0) for a in albums_idx)
            png_count = sum(1 for a in albums_idx if a.get("image"))
            return {
                "albums": len(albums_idx),
                "songs": total_songs,
                "png_artwork": png_count,
                "zero_byte_shield": "Active",
                "mode": "CLOUD",
                "catalog_stale": self._cache_is_stale,
            }

    def get_albums_index(self, force_refresh: bool = False, allow_stale_fallback: bool = True) -> List[Dict[str, Any]]:
        now = time.time()
        # Return fresh cache when within TTL
        if not force_refresh and self._albums_index_cache is not None and (now - self._last_fetch_time) < self.CACHE_TTL_SECONDS:
            self._cache_is_stale = False
            return self._albums_index_cache

        try:
            idx = self._fetch_json("metadata/indexes/albums.json")
            if not isinstance(idx, list):
                raise CatalogLookupError(
                    f"Invalid catalog index format (expected list, got {type(idx).__name__}).",
                    reason="invalid_format",
                    path="metadata/indexes/albums.json",
                )
            self._albums_index_cache = idx
            self._last_fetch_time = now
            self._cache_is_stale = False
            self._last_catalog_error = None
            return idx
        except CatalogLookupError as err:
            self._last_catalog_error = {
                "reason": err.reason,
                "status_code": err.status_code,
                "message": str(err),
                "timestamp": now,
            }
            cache_age = (now - self._last_fetch_time) if self._last_fetch_time else float("inf")
            can_use_stale = (
                allow_stale_fallback
                and not force_refresh
                and self._albums_index_cache is not None
                and cache_age <= self.MAX_STALE_CACHE_AGE_SECONDS
                and err.reason not in ("not_found", "auth_error", "invalid_json", "invalid_format")
            )
            if can_use_stale:
                self._cache_is_stale = True
                logger.warning(
                    f"[CloudCatalogService] Using stale cached catalog index (age={cache_age:.1f}s, reason={err.reason}) "
                    f"after transient failure: {err}"
                )
                return self._albums_index_cache
            raise

    def get_albums_list(self) -> Dict[str, Any]:
        idx = self.get_albums_index()
        result = []
        for item in idx:
            result.append({
                "name": item.get("name", "Unknown"),
                "musicDirector": item.get("artist") or item.get("musicDirector") or "Unknown",
                "year": item.get("year", 2026),
                "songCount": item.get("songCount", 0),
                "hasArtwork": bool(item.get("image"))
            })
        return {"total_albums": len(result), "albums": result}

    def search_albums(self, query: str) -> Dict[str, Any]:
        """Performs ranked generic fuzzy search over all catalog albums."""
        idx = self.get_albums_index()
        matches = search_albums_fuzzy(query, idx, limit=10)
        formatted = []
        for item in matches:
            formatted.append({
                "name": item.get("name", "Unknown"),
                "musicDirector": item.get("artist") or item.get("musicDirector") or "Unknown",
                "year": item.get("year", 2026),
                "songCount": item.get("songCount", 0),
                "hasArtwork": bool(item.get("image")),
                "match_score": item.get("match_score", 0.0)
            })
        return {"query": query, "total_matches": len(formatted), "suggestions": formatted}

    def check_duplicate_album(self, album_name: str) -> Dict[str, Any]:
        """Backend near-duplicate check against cloud catalog."""
        idx = self.get_albums_index()
        existing_names = [a.get("name", "") for a in idx if a.get("name")]
        return check_near_duplicate_album(album_name, existing_names)

    def match_music_director(self, query: str) -> Dict[str, List[str]]:
        if not query or not query.strip():
            return {"suggestions": []}
        
        q_lower = query.strip().lower()
        if self._artists_index_cache is None:
            try:
                artists_data = self._fetch_json("metadata/indexes/artists.json")
                if isinstance(artists_data, list):
                    self._artists_index_cache = artists_data
                else:
                    self._artists_index_cache = []
            except Exception:
                self._artists_index_cache = []

        matches = []
        for artist_entry in self._artists_index_cache:
            name = artist_entry.get("name", "")
            if name and q_lower in name.lower():
                matches.append(name)

        matches = sorted(list(set(matches)), key=lambda s: s.lower())[:10]
        return {"suggestions": matches}

    def get_album_metadata(self, album_identifier: Any, bypass_cache: bool = False) -> Dict[str, Any]:
        """
        Fetches the authoritative metadata JSON for an album directly from:
        metadata/albums/{partition}/{album_name}.json
        Raises CatalogLookupError if fetch fails.
        """
        album_name = ""
        partition = ""
        if isinstance(album_identifier, dict):
            album_name = album_identifier.get("name") or album_identifier.get("album_name", "")
            partition = album_identifier.get("partition", "")
        elif isinstance(album_identifier, str):
            album_name = album_identifier

        if not album_name:
            raise CatalogLookupError("Cannot fetch album metadata without an album name.", reason="invalid_format")

        if not partition:
            from backend.core.repository import RepositoryContext
            partition = RepositoryContext.get_partition(album_name)

        cache_key = f"{partition}/{album_name}"
        if not bypass_cache and cache_key in self._album_metadata_cache:
            return self._album_metadata_cache[cache_key]

        path = f"metadata/albums/{partition}/{album_name}.json"
        data = self._fetch_json(path, bypass_cache=bypass_cache)
        if not isinstance(data, dict):
            raise CatalogLookupError(f"Invalid metadata format received for album '{album_name}'.", reason="invalid_format", path=path)

        self._album_metadata_cache[cache_key] = data
        return data

    @classmethod
    def strip_language_and_meta(cls, raw_title: str) -> Tuple[str, Optional[str]]:
        """
        Strips language tags/suffixes (e.g. ' – Telugu', ' (Tamil)', ' - Hindi') from album title.
        Returns: (cleaned_title, detected_language_or_None)
        """
        base = raw_title.strip()
        detected_lang = None
        for lang in cls.KNOWN_LANGUAGES:
            pattern = rf'(?:&#8211;|-|–|—|\u2013|\u2014|\/)\s*{lang}\b|\(\s*{lang}\s*\)|\[\s*{lang}\s*\]'
            if re.search(pattern, base, re.IGNORECASE):
                detected_lang = lang
                base = re.split(pattern, base, flags=re.IGNORECASE)[0].strip()
                break

        # Also strip trailing punctuation/year
        base = re.sub(r'\s*\(\d{4}\).*$', '', base).strip()
        base = re.sub(r'[\-_:\.\s]+$', '', base).strip()
        base = re.sub(r'\s+', ' ', base).strip()
        return base, detected_lang

    def find_canonical_album(
        self,
        query_name: str,
        query_language: Optional[str] = None,
        query_year: Optional[int] = None,
        candidate_albums: Optional[List[Dict[str, Any]]] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Authoritative album matching across MusicData catalog.
        Strictly enforces language verification:
        - OM Telugu must NEVER match OM Tamil.
        - Matches stripped base names (e.g. 'OM' matches 'OM – Telugu' if language is Telugu).
        - Verifies release year delta <= 2 if years are present.
        """
        if not query_name or not query_name.strip():
            return None

        catalog = candidate_albums if candidate_albums is not None else self.get_albums_index()
        if not catalog:
            return None

        clean_query, emb_query_lang = self.strip_language_and_meta(query_name)
        effective_query_lang = query_language or emb_query_lang

        norm_query_raw = re.sub(r'[^a-z0-9]', '', query_name.lower())
        norm_query_clean = re.sub(r'[^a-z0-9]', '', clean_query.lower())

        def languages_match(cat_album: Dict[str, Any]) -> bool:
            """Strict language compatibility check."""
            _, cat_emb_lang = self.strip_language_and_meta(cat_album.get("name", ""))
            # Explicit title language always takes precedence over default catalog index language
            effective_cat_lang = cat_emb_lang or cat_album.get("language")
            if cat_emb_lang and effective_query_lang and cat_emb_lang.lower() != effective_query_lang.lower():
                return False
            if not effective_query_lang or not effective_cat_lang:
                return True
            return str(effective_cat_lang).strip().lower() == str(effective_query_lang).strip().lower()

        def years_compatible(cat_album: Dict[str, Any]) -> bool:
            if not query_year:
                return True
            cat_year = cat_album.get("year")
            if not cat_year:
                return True
            try:
                return abs(int(cat_year) - int(query_year)) <= 2
            except (ValueError, TypeError):
                return True

        # Phase 1: Exact raw name match with language check
        exact_matches = []
        for alb in catalog:
            cat_name = alb.get("name", "")
            norm_cat_raw = re.sub(r'[^a-z0-9]', '', cat_name.lower())
            if norm_query_raw and norm_query_raw == norm_cat_raw and languages_match(alb):
                exact_matches.append(alb)

        if len(exact_matches) == 1:
            return exact_matches[0]
        elif len(exact_matches) > 1:
            for alb in exact_matches:
                if years_compatible(alb):
                    return alb
            return exact_matches[0]

        # Phase 2: Cleaned base name match with strict language check
        candidates = []
        for alb in catalog:
            cat_name = alb.get("name", "")
            clean_cat, cat_emb_lang = self.strip_language_and_meta(cat_name)
            norm_cat_clean = re.sub(r'[^a-z0-9]', '', clean_cat.lower())

            if norm_query_clean and norm_query_clean == norm_cat_clean:
                if languages_match(alb):
                    candidates.append(alb)

        if len(candidates) == 1:
            if years_compatible(candidates[0]):
                return candidates[0]
        elif len(candidates) > 1:
            # Ambiguous match (e.g. OM in multiple languages and query language was missing/generic)
            # Require exact language match
            filtered = [c for c in candidates if years_compatible(c)]
            if not filtered:
                filtered = candidates
            if effective_query_lang:
                for c in filtered:
                    c_clean, c_emb = self.strip_language_and_meta(c.get("name", ""))
                    c_l = c.get("language") or c_emb
                    if c_l and str(c_l).strip().lower() == str(effective_query_lang).strip().lower():
                        return c
            # Multiple candidates with unresolved language -> Return None to prevent incorrect merge
            return None

        # Phase 3: High similarity ratio (>= 0.90) on cleaned names
        best_alb = None
        best_ratio = 0.0
        for alb in catalog:
            if not languages_match(alb) or not years_compatible(alb):
                continue
            cat_name = alb.get("name", "")
            clean_cat, _ = self.strip_language_and_meta(cat_name)
            norm_cat_clean = re.sub(r'[^a-z0-9]', '', clean_cat.lower())
            ratio = SequenceMatcher(None, norm_query_clean, norm_cat_clean).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_alb = alb

        if best_ratio >= 0.90:
            return best_alb

        return None

    PAREN_VERSION_TOKENS = {
        "promo": "promo",
        "promo song": "promo",
        "promo version": "promo",
        "teaser": "teaser",
        "teaser song": "teaser",
        "glimpse": "glimpse",
        "full": "full",
        "full song": "full",
        "full version": "full",
        "female": "female",
        "female version": "female",
        "male": "male",
        "male version": "male",
        "duet": "duet",
        "duet version": "duet",
        "sad": "sad",
        "sad version": "sad",
        "happy": "happy",
        "reprise": "reprise",
        "unplugged": "unplugged",
        "acoustic": "acoustic",
        "remix": "remix",
        "bgm": "bgm",
        "theme": "theme",
        "instrumental": "instrumental",
        "extended": "extended",
        "slowed": "slowed",
        "slowed reverb": "slowed",
        "slowed and reverb": "slowed",
        "lofi": "lofi",
        "karaoke": "karaoke",
        "lyrical": "lyrical",
        "lyric": "lyrical",
        "lyrical version": "lyrical",
        "short": "short",
        "bit": "bit",
        "trailer": "trailer",
    }

    STANDALONE_VERSION_WORDS = (
        "promo", "teaser", "glimpse", "reprise", "unplugged", "remix", "karaoke"
    )

    @classmethod
    def parse_song_title_components(cls, title: str) -> Dict[str, Any]:
        """
        Extracts canonical base core title and explicit version qualifiers (e.g. promo, full, female).
        """
        raw = (title or "").strip()
        raw_alnum = re.sub(r'[^a-z0-9]', '', re.sub(r'\.mp3$', '', raw, flags=re.IGNORECASE).lower())

        t = raw.lower()
        t = re.sub(r'\.mp3$', '', t, flags=re.IGNORECASE)
        t = re.sub(r'^\s*0*\d+\s*[\-_.]\s*', '', t)

        versions: List[str] = []

        def _extract_paren(match):
            inner = (match.group(1) or match.group(2) or "").strip()
            inner_clean = re.sub(r'[^a-z0-9\s]', ' ', inner.lower())
            inner_clean = re.sub(r'\s+', ' ', inner_clean).strip()
            if not inner_clean:
                return " "
            if inner_clean in cls.PAREN_VERSION_TOKENS:
                v = cls.PAREN_VERSION_TOKENS[inner_clean]
                if v not in versions:
                    versions.append(v)
                return " "
            # Check individual tokens inside parentheses e.g. "(Full Audio Version)"
            matched_any = False
            for k, v in cls.PAREN_VERSION_TOKENS.items():
                if re.search(rf'\b{re.escape(k)}\b', inner_clean):
                    if v not in versions:
                        versions.append(v)
                    matched_any = True
            return " " if matched_any else f" {inner_clean} "

        t = re.sub(r'\(([^)]+)\)|\[([^\]]+)\]', _extract_paren, t)

        # Standalone unambiguous version markers (e.g. "Full Version", "Promo")
        if re.search(r'\bfull\s+(?:song|version)\b', t):
            if "full" not in versions:
                versions.append("full")
            t = re.sub(r'\bfull\s+(?:song|version)\b', ' ', t)

        for vw in cls.STANDALONE_VERSION_WORDS:
            if re.search(rf'\b{vw}\b', t):
                if vw not in versions:
                    versions.append(vw)
                t = re.sub(rf'\b{vw}\b', ' ', t)

        t_clean = re.sub(r'[^a-z0-9\s]', ' ', t)
        t_clean = re.sub(r'\b(song|songs|audio|video|track|version)\b', ' ', t_clean)
        base_core = re.sub(r'\s+', ' ', t_clean).strip()

        version_tuple = tuple(sorted(set(versions)))
        full_norm = " ".join([base_core] + list(version_tuple)).strip()

        return {
            "raw": raw,
            "raw_alnum": raw_alnum,
            "base_core": base_core,
            "version_tags": version_tuple,
            "full_norm": full_norm,
        }

    @staticmethod
    def compare_singers(source_singer: Optional[str], catalog_singers: Any) -> Optional[float]:
        """
        Compares singer credit strings/lists. Returns float in [0.0, 1.0], or None if unavailable.
        """
        def split_singers(val: Any) -> List[str]:
            if not val:
                return []
            if isinstance(val, list):
                items = [str(x) for x in val if x]
            else:
                items = [str(val)]
            names = []
            for item in items:
                for part in re.split(r'[,/&]|\band\b|\bfeat\.?\b', item, flags=re.IGNORECASE):
                    norm = re.sub(r'[^a-z0-9\s]', ' ', part.lower())
                    norm = re.sub(r'\s+', ' ', norm).strip()
                    if norm and norm != "unknown":
                        names.append(norm)
            return names

        src_list = split_singers(source_singer)
        cat_list = split_singers(catalog_singers)
        if not src_list or not cat_list:
            return None

        matched = 0
        for s in src_list:
            s_compact = s.replace(" ", "")
            best = max(
                max(
                    SequenceMatcher(None, s, c).ratio(),
                    SequenceMatcher(None, s_compact, c.replace(" ", "")).ratio()
                )
                for c in cat_list
            )
            if best >= 0.80:
                matched += 1
        return matched / max(len(src_list), len(cat_list))

    @staticmethod
    def normalize_song_title(title: str) -> str:
        t = title.strip().lower()
        t = re.sub(r'\.mp3$', '', t, flags=re.IGNORECASE)
        t = re.sub(r'^\d+\s*[\-_.]\s*', '', t) # remove track number prefix "01 - "
        t = re.sub(r'\b(song|songs|audio|video|full song|lyrical|track)\b', '', t).strip()
        t = re.sub(r'[^a-z0-9\s]', ' ', t)
        t = re.sub(r'\s+', ' ', t).strip()
        return t

    @classmethod
    def match_song_detailed(
        cls,
        source_title: str,
        catalog_songs: List[Dict[str, Any]],
        source_singer: Optional[str] = None,
        source_filename: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Version-aware, metadata-aware song comparator.
        Classifies match into EXACT_MATCH, POSSIBLE_VERSION_MATCH, UNCERTAIN, or MISSING.
        Never conflates conflicting versions such as (Promo) vs (Full).
        """
        empty_res = {
            "exists_in_catalog": False,
            "classification": "MISSING",
            "confidence": "NONE",
            "score": 0.0,
            "matched_song": None,
            "version_candidates": [],
            "reason": None,
            "singer_match_score": None
        }
        if not source_title or not catalog_songs:
            return empty_res

        src = cls.parse_song_title_components(source_title)
        src_file = cls.parse_song_title_components(source_filename) if source_filename else None

        exact_matches: List[Tuple[float, Optional[float], Dict[str, Any], str]] = []
        version_candidates: List[Tuple[float, int, Optional[float], Dict[str, Any], str]] = []
        best_fuzzy: Optional[Tuple[float, Optional[float], Dict[str, Any]]] = None

        for cat_song in catalog_songs:
            cat_title = cat_song.get("title", "")
            cat = cls.parse_song_title_components(cat_title)
            cat_audio_raw = cat_song.get("audio") or cat_song.get("filename") or ""
            cat_audio = re.sub(r'\.mp3$', '', cat_audio_raw, flags=re.IGNORECASE)
            cat_file = cls.parse_song_title_components(cat_audio) if cat_audio else None

            singer_sim = cls.compare_singers(source_singer, cat_song.get("singers") or cat_song.get("artist"))

            # 1. Exact raw alphanumeric match
            if src["raw_alnum"] and src["raw_alnum"] == cat["raw_alnum"]:
                exact_matches.append((1.0, singer_sim, cat_song, "Exact title match"))
                continue

            # 2. Check normalized title & version tags
            if src["base_core"] and src["base_core"] == cat["base_core"]:
                if src["version_tags"] == cat["version_tags"]:
                    exact_matches.append((1.0, singer_sim, cat_song, "Exact normalized match"))
                else:
                    # Same base title, conflicting/unspecified version (e.g. Full vs Promo or Full vs untagged)
                    priority_tagged = 1 if cat["version_tags"] else 0
                    src_v = ", ".join(src["version_tags"]) or "standard"
                    cat_v = ", ".join(cat["version_tags"]) or "standard"
                    reason = f"Possible version match: source '{source_title}' ({src_v}) vs catalog '{cat_title}' ({cat_v})"
                    version_candidates.append((0.92, priority_tagged, singer_sim, cat_song, reason))
                continue

            # 3. Check catalog audio filename (if not shadowed by explicit version conflict on title)
            if cat_file and src["base_core"] and cat_file["base_core"] == src["base_core"]:
                if src["version_tags"] == cat_file["version_tags"]:
                    exact_matches.append((0.98, singer_sim, cat_song, "Exact filename match"))
                else:
                    priority_tagged = 1 if (cat["version_tags"] or cat_file["version_tags"]) else 0
                    src_v = ", ".join(src["version_tags"]) or "standard"
                    cat_v = ", ".join(cat_file["version_tags"]) or "standard"
                    reason = f"Possible version match (filename): source '{source_title}' ({src_v}) vs catalog '{cat_title}' ({cat_v})"
                    version_candidates.append((0.90, priority_tagged, singer_sim, cat_song, reason))
                continue

            # 4. Check source filename against catalog only when source title has NO version tag conflict
            if src_file and not src["version_tags"] and src_file["base_core"]:
                if src_file["base_core"] == cat["base_core"] and src_file["version_tags"] == cat["version_tags"]:
                    exact_matches.append((0.96, singer_sim, cat_song, "Matched by source filename"))
                    continue

            # 5. Fuzzy base & full similarity
            base_sim = SequenceMatcher(None, src["base_core"], cat["base_core"]).ratio() if (src["base_core"] and cat["base_core"]) else 0.0
            full_sim = SequenceMatcher(None, src["full_norm"], cat["full_norm"]).ratio() if (src["full_norm"] and cat["full_norm"]) else 0.0

            # If near-identical base with version difference
            if base_sim >= 0.90 and src["version_tags"] != cat["version_tags"]:
                priority_tagged = 1 if cat["version_tags"] else 0
                src_v = ", ".join(src["version_tags"]) or "standard"
                cat_v = ", ".join(cat["version_tags"]) or "standard"
                reason = f"Possible version match: source '{source_title}' ({src_v}) vs catalog '{cat_title}' ({cat_v})"
                version_candidates.append((round(base_sim, 3), priority_tagged, singer_sim, cat_song, reason))
                continue

            sim = max(base_sim, full_sim)
            if src["version_tags"] == cat["version_tags"] and singer_sim is not None and sim >= 0.65:
                if singer_sim >= 0.70:
                    sim = min(0.95, sim + 0.04)
                elif singer_sim < 0.20 and sim < 0.92:
                    sim = max(0.0, sim - 0.05)

            if best_fuzzy is None or sim > best_fuzzy[0]:
                best_fuzzy = (sim, singer_sim, cat_song)

        if exact_matches:
            # Prefer highest score, breaking ties by highest singer match
            best = max(exact_matches, key=lambda x: (x[0], x[1] if x[1] is not None else 0.5))
            return {
                "exists_in_catalog": True,
                "classification": "EXACT_MATCH",
                "confidence": "EXACT" if best[0] == 1.0 else "HIGH",
                "score": round(best[0], 3),
                "matched_song": best[2],
                "version_candidates": [],
                "reason": best[3],
                "singer_match_score": best[1]
            }

        if version_candidates:
            # Prefer explicitly version-tagged match (e.g. Promo) with highest score/singer sim
            version_candidates.sort(key=lambda x: (x[0], x[1], x[2] if x[2] is not None else 0.5), reverse=True)
            best_v = version_candidates[0]
            return {
                "exists_in_catalog": False,
                "classification": "POSSIBLE_VERSION_MATCH",
                "confidence": "UNCERTAIN",
                "score": round(best_v[0], 3),
                "matched_song": best_v[3],
                "version_candidates": [vc[3] for vc in version_candidates],
                "reason": best_v[4],
                "singer_match_score": best_v[2]
            }

        if best_fuzzy:
            sim, singer_sim, cat_song = best_fuzzy
            if sim >= 0.88:
                return {
                    "exists_in_catalog": True,
                    "classification": "EXACT_MATCH",
                    "confidence": "HIGH",
                    "score": round(sim, 3),
                    "matched_song": cat_song,
                    "version_candidates": [],
                    "reason": "High-confidence fuzzy match",
                    "singer_match_score": singer_sim
                }
            elif sim >= 0.68:
                return {
                    "exists_in_catalog": False,
                    "classification": "UNCERTAIN",
                    "confidence": "UNCERTAIN",
                    "score": round(sim, 3),
                    "matched_song": cat_song,
                    "version_candidates": [],
                    "reason": f"Uncertain match against '{cat_song.get('title')}' (score {sim:.2f})",
                    "singer_match_score": singer_sim
                }

        best_score = round(best_fuzzy[0], 3) if best_fuzzy else 0.0
        return {
            **empty_res,
            "score": best_score,
            "reason": "No matching track in catalog"
        }

    @classmethod
    def match_song_to_catalog(
        cls,
        source_title: str,
        catalog_songs: List[Dict[str, Any]],
        source_singer: Optional[str] = None,
        source_filename: Optional[str] = None
    ) -> Tuple[bool, str, float, Optional[Dict[str, Any]]]:
        """
        Unified song comparator (backward-compatible 4-tuple wrapper around match_song_detailed).
        Returns: (exists_in_catalog: bool, confidence: str, score: float, matched_song: Optional[dict])
        """
        res = cls.match_song_detailed(
            source_title=source_title,
            catalog_songs=catalog_songs,
            source_singer=source_singer,
            source_filename=source_filename
        )
        return (
            res["exists_in_catalog"],
            res["confidence"],
            res["score"],
            res["matched_song"]
        )

cloud_catalog_service = CloudCatalogService()
