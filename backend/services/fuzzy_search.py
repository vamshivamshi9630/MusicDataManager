import re
import difflib
from typing import List, Dict, Any, Optional, Tuple

try:
    from rapidfuzz import fuzz
    HAS_RAPIDFUZZ = True
except ImportError:
    HAS_RAPIDFUZZ = False

def normalize_text(text: str) -> str:
    """Normalize text for consistent comparison (lowercase, stripped, collapsed spaces & punctuation)."""
    if not text:
        return ""
    # Convert to lowercase
    s = text.lower().strip()
    # Replace punctuation with spaces
    s = re.sub(r'[\-_:\.,\(\)\[\]\{\}\'"!@#$%^&\*\+=<>\/\?\\\|]', ' ', s)
    # Collapse multiple spaces
    s = re.sub(r'\s+', ' ', s).strip()
    return s

def calculate_similarity_ratio(str1: str, str2: str) -> float:
    """Calculate similarity ratio between two normalized strings (0.0 to 1.0)."""
    norm1 = normalize_text(str1)
    norm2 = normalize_text(str2)
    
    if not norm1 or not norm2:
        return 0.0
    if norm1 == norm2:
        return 1.0
        
    if HAS_RAPIDFUZZ:
        # RapidFuzz returns 0-100 score
        ratio = fuzz.ratio(norm1, norm2) / 100.0
        token_ratio = fuzz.token_sort_ratio(norm1, norm2) / 100.0
        return max(ratio, token_ratio)
    else:
        # difflib fallback
        return difflib.SequenceMatcher(None, norm1, norm2).ratio()

def score_album_match(query: str, candidate_name: str) -> float:
    """Compute ranked similarity score between query and candidate album name."""
    norm_q = normalize_text(query)
    norm_c = normalize_text(candidate_name)

    if not norm_q or not norm_c:
        return 0.0

    # 1. Exact Match (Score 1.0)
    if norm_q == norm_c:
        return 1.0

    # 2. Prefix Match (Score 0.92 - 0.97)
    if norm_c.startswith(norm_q):
        return 0.95
    if norm_q.startswith(norm_c):
        return 0.92

    # 3. Substring Match (Score 0.85)
    if norm_q in norm_c:
        return 0.88
    if norm_c in norm_q:
        return 0.82

    # 4. Token Set / Word Match
    q_words = set(norm_q.split())
    c_words = set(norm_c.split())
    if q_words and c_words and q_words.issubset(c_words):
        return 0.84

    # 5. Fuzzy Edit Distance Match
    raw_ratio = calculate_similarity_ratio(norm_q, norm_c)
    
    # Extra boost if words share substantial prefixes (e.g. puspa -> pushpa)
    if len(norm_q) >= 3 and len(norm_c) >= 3:
        if norm_q[:3] == norm_c[:3]:
            raw_ratio = max(raw_ratio, 0.75)
            
    return raw_ratio

def search_albums_fuzzy(query: str, album_list: List[Dict[str, Any]], limit: int = 10, min_score: float = 0.40) -> List[Dict[str, Any]]:
    """Generic fuzzy search and ranking over a list of album metadata dictionaries or strings."""
    if not query or not query.strip():
        return []

    scored_results: List[Tuple[float, Dict[str, Any]]] = []
    
    for item in album_list:
        if isinstance(item, str):
            album_name = item
            entry = {"name": item}
        else:
            album_name = item.get("name", "")
            entry = item

        score = score_album_match(query, album_name)
        if score >= min_score:
            res_entry = dict(entry)
            res_entry["match_score"] = round(score, 3)
            scored_results.append((score, res_entry))

    # Sort descending by score, then ascending by name length
    scored_results.sort(key=lambda x: (x[0], -len(x[1].get("name", ""))), reverse=True)
    return [res[1] for res in scored_results[:limit]]

def check_near_duplicate_album(new_name: str, existing_album_names: List[str]) -> Dict[str, Any]:
    """
    Backend validation check to block exact or near-duplicate album creation.
    Returns:
    {
        "duplicate": bool,
        "exact": bool,
        "matched_album": str,
        "score": float,
        "reason": str
    }
    """
    norm_new = normalize_text(new_name)
    if not norm_new:
        return {"duplicate": False}

    best_match = None
    best_score = 0.0

    for existing in existing_album_names:
        norm_ex = normalize_text(existing)
        if norm_new == norm_ex:
            return {
                "duplicate": True,
                "exact": True,
                "matched_album": existing,
                "score": 1.0,
                "reason": f"Album '{existing}' already exists in repository."
            }

        score = score_album_match(new_name, existing)
        if score > best_score:
            best_score = score
            best_match = existing

    # Strict fuzzy threshold for near duplicates (e.g., Puspa -> Pushpa)
    # Require score >= 0.78 for strings length >= 4
    threshold = 0.75 if len(norm_new) >= 4 else 0.88
    if best_match and best_score >= threshold:
        return {
            "duplicate": True,
            "exact": False,
            "matched_album": best_match,
            "score": round(best_score, 3),
            "reason": f"A very similar existing album ('{best_match}') already exists."
        }

    return {"duplicate": False}
