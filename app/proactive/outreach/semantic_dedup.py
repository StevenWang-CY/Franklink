"""Semantic deduplication for proactive outreach signals.

Uses Jaccard similarity and SequenceMatcher to detect semantically
similar signals, avoiding duplicate outreach for similar networking needs.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Set

# Pattern to normalize course identifiers: "CS 161" -> "cs161"
COURSE_PATTERN = re.compile(r"([A-Za-z]+)\s*(\d+)")

# Stop words to exclude from keyword extraction
STOP_WORDS = {
    # Articles and basic connectors
    "a", "an", "the", "and", "or", "but", "nor", "so", "yet",
    # Prepositions
    "in", "on", "at", "to", "for", "of", "with", "by", "from", "as",
    "into", "through", "during", "before", "after", "above", "below",
    "between", "under", "over",
    # Common verbs
    "is", "are", "was", "were", "been", "be", "have", "has", "had",
    "do", "does", "did", "will", "would", "could", "should", "may",
    "might", "must", "shall", "can", "need",
    # Pronouns
    "i", "me", "my", "we", "us", "our", "you", "your", "he", "him",
    "his", "she", "her", "it", "its", "they", "them", "their",
    # Networking-specific common words (too generic)
    "looking", "want", "need", "interested", "help", "find", "connect",
    "someone", "people", "person", "group", "anyone", "everybody",
    # Question words
    "who", "what", "where", "when", "how", "why", "which",
    # Other common words
    "this", "that", "these", "those", "am", "just", "also", "very",
    "really", "some", "any", "all", "most", "other", "more", "less",
}

# Thresholds for similarity matching
JACCARD_THRESHOLD = 0.5  # 50% keyword overlap
SEQUENCE_THRESHOLD = 0.6  # 60% character sequence similarity


def normalize_text(text: str) -> str:
    """
    Normalize text for semantic comparison.

    - Lowercase
    - Normalize course patterns (CS 161 -> cs161)
    - Remove punctuation
    - Collapse whitespace
    """
    if not text:
        return ""

    text = text.lower().strip()

    # Normalize course patterns: "CS 161", "cs-161", "CS161" -> "cs161"
    text = COURSE_PATTERN.sub(lambda m: f"{m.group(1).lower()}{m.group(2)}", text)

    # Remove punctuation (keep alphanumeric and spaces)
    text = re.sub(r"[^\w\s]", " ", text)

    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()

    return text


def extract_keywords(text: str) -> Set[str]:
    """
    Extract meaningful keywords from text.

    - Normalizes text
    - Removes stop words
    - Keeps only words with length > 1
    """
    normalized = normalize_text(text)
    if not normalized:
        return set()

    words = normalized.split()
    return {w for w in words if len(w) > 1 and w not in STOP_WORDS}


def jaccard_similarity(text1: str, text2: str) -> float:
    """
    Calculate Jaccard similarity between two texts based on keyword overlap.

    Jaccard = |intersection| / |union|

    Returns:
        Float between 0 and 1 (1 = identical keyword sets)
    """
    kw1 = extract_keywords(text1)
    kw2 = extract_keywords(text2)

    if not kw1 or not kw2:
        return 0.0

    intersection = kw1 & kw2
    union = kw1 | kw2

    return len(intersection) / len(union) if union else 0.0


def sequence_similarity(text1: str, text2: str) -> float:
    """
    Calculate character-level sequence similarity using SequenceMatcher.

    This is more lenient than Jaccard and catches similar spellings/variations.

    Returns:
        Float between 0 and 1 (1 = identical strings)
    """
    norm1 = normalize_text(text1)
    norm2 = normalize_text(text2)

    if not norm1 or not norm2:
        return 0.0

    return SequenceMatcher(None, norm1, norm2).ratio()


def is_semantic_duplicate(
    text1: str,
    text2: str,
    jaccard_threshold: float = JACCARD_THRESHOLD,
    sequence_threshold: float = SEQUENCE_THRESHOLD,
) -> bool:
    """
    Check if two texts are semantically duplicate.

    Returns True if either:
    - Jaccard similarity >= threshold (default 0.5), OR
    - SequenceMatcher ratio >= threshold (default 0.6)

    This dual approach catches:
    - Keyword overlap: "CS 161 study group" vs "study partners for cs161"
    - Similar phrasing: "PM interview prep" vs "product manager interview preparation"

    Args:
        text1: First signal text
        text2: Second signal text
        jaccard_threshold: Minimum Jaccard similarity to consider duplicate
        sequence_threshold: Minimum SequenceMatcher ratio to consider duplicate

    Returns:
        True if texts are semantically duplicate
    """
    if not text1 or not text2:
        return False

    # Check Jaccard similarity first (faster)
    jaccard = jaccard_similarity(text1, text2)
    if jaccard >= jaccard_threshold:
        return True

    # Fall back to sequence similarity
    sequence = sequence_similarity(text1, text2)
    if sequence >= sequence_threshold:
        return True

    return False


def get_similarity_scores(text1: str, text2: str) -> dict:
    """
    Get detailed similarity scores for debugging/logging.

    Returns:
        Dict with jaccard, sequence, and is_duplicate scores
    """
    jaccard = jaccard_similarity(text1, text2)
    sequence = sequence_similarity(text1, text2)

    return {
        "jaccard": round(jaccard, 3),
        "sequence": round(sequence, 3),
        "is_duplicate": jaccard >= JACCARD_THRESHOLD or sequence >= SEQUENCE_THRESHOLD,
        "keywords_1": list(extract_keywords(text1))[:10],
        "keywords_2": list(extract_keywords(text2))[:10],
    }
