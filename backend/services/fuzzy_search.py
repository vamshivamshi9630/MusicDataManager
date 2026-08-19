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
    s = text.lower().strip()
    s = re.sub(r'[\-_:\.,\(\)\[\]\{\}\'"!@#$%^&\*\+=<>\/\?\\\|]', ' ', s)
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
        ratio = fuzz.ratio(norm1, norm2) / 100.0
        token_ratio = fuzz.token_sort_ratio(norm1, norm2) / 100.0
        return max(ratio, token_ratio)
    else:
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

    # 2. Candidate starts with Query (e.g. "push" -> "pushpa", "pushpa 2")
    if norm_c.startswith(norm_q):
        len_ratio = len(norm_q) / len(norm_c)
        return round(0.88 + (0.09 * len_ratio), 3)  # 0.88 to 0.97

    # 3. Query starts with Candidate
    if norm_q.startswith(norm_c):
        len_ratio = len(norm_c) / len(norm_q)
        return round(0.82 + (0.08 * len_ratio), 3)

    # 4. Word-level Prefix Match (e.g. any word in candidate starts with query)
    q_words = norm_q.split()
    c_words = norm_c.split()

    if len(q_words) == 1:
        qw = q_words[0]
        word_starts = [cw for cw in c_words if cw.startswith(qw)]
        if word_starts:
            # If candidate word starts with query word (e.g. "push" in "Pushpaka Vimanam")
            return 0.84

    # 5. Whole phrase substring match (e.g. "rule" in "pushpa 2 the rule")
    if len(norm_q) >= 4 and norm_q in norm_c:
        return 0.80

    # 6. Fuzzy Edit Distance Match (for genuine typos like "puspa" -> "pushpa")
    len_diff = abs(len(norm_q) - len(norm_c))
    max_len = max(len(norm_q), len(norm_c))
    if max_len > 0 and (len_diff / max_len) <= 0.35:
        sim = calculate_similarity_ratio(norm_q, norm_c)
        if sim >= 0.70:
            return round(sim, 3)

    return 0.0

def search_albums_fuzzy(query: str, album_list: List[Dict[str, Any]], limit: int = 10, min_score: float = 0.75) -> List[Dict[str, Any]]:
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
            res_entry["match_score"] = score
            scored_results.append((score, res_entry))

    # Sort descending by score, then ascending by candidate name length
    scored_results.sort(key=lambda x: (x[0], -len(x[1].get("name", ""))), reverse=True)
    return [res[1] for res in scored_results[:limit]]

def check_near_duplicate_album(new_name: str, existing_album_names: List[str]) -> Dict[str, Any]:
    """
    Backend validation check to block exact or near-duplicate album creation.
    Protects against typos (Puspa -> Pushpa) while permitting distinct multi-word titles (Lovers Day vs Lover).
    """
    norm_new = normalize_text(new_name)
    if not norm_new:
        return {"duplicate": False}

    new_words = norm_new.split()

    for existing in existing_album_names:
        norm_ex = normalize_text(existing)
        ex_words = norm_ex.split()

        # 1. Exact match
        if norm_new == norm_ex:
            return {
                "duplicate": True,
                "exact": True,
                "matched_album": existing,
                "score": 1.0,
                "reason": f"Album '{existing}' already exists in repository."
            }

        # 2. Multi-word phrases guard:
        # If new_name has multiple words (e.g. "lovers day") and candidate has fewer words ("lover"),
        # do NOT flag as duplicate if new_name has distinct unmatched words!
        if len(new_words) > len(ex_words):

            # Check if any new word is completely distinct and not in existing
            distinct_words = [w for w in new_words if not any(w in ew or ew in w for ew in ex_words)]
            if distinct_words:
                continue  # "Lovers Day" vs "Lover": "day" is distinct! Skip duplicate flag.

        # 3. High similarity typo match (e.g. "Puspa" vs "Pushpa")
        len_diff = abs(len(norm_new) - len(norm_ex))
        max_len = max(len(norm_new), len(norm_ex))
        if max_len > 0 and (len_diff / max_len) <= 0.30:
            sim = calculate_similarity_ratio(norm_new, norm_ex)
            if sim >= 0.82:
                return {
                    "duplicate": True,
                    "exact": False,
                    "matched_album": existing,
                    "score": round(sim, 3),
                    "reason": f"A very similar existing album ('{existing}') already exists."
                }

    return {"duplicate": False}
