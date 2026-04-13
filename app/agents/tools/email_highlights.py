"""Helpers for deriving key email highlights from stored user emails."""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Iterable, List, Optional

from app.database.client import DatabaseClient
from app.agents.tools.onboarding.email_context import (
    CAREER_TO_KEYWORDS,
    EXCLUDE_SENDER_PATTERNS,
    expand_keywords,
)

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_WORD_RE = re.compile(r"[a-z0-9]{3,}")

_STOPWORDS = {
    "about", "after", "again", "also", "another", "anyone", "because", "before", "being",
    "between", "could", "did", "does", "doing", "dont", "from", "give", "going", "have",
    "here", "just", "know", "like", "more", "most", "much", "only", "other", "over",
    "some", "still", "such", "than", "that", "their", "them", "then", "there", "these",
    "they", "this", "those", "through", "very", "what", "when", "where", "which", "will",
    "with", "would", "your", "youre", "cant", "dont", "lets", "subject", "body", "email",
}

_PROMO_SUBJECT_PATTERNS = {
    "newsletter",
    "digest",
    "promo",
    "promotional",
    "sponsored",
    "sale",
    "flash sale",
    "clearance",
    "deal",
    "offer",
    "special offer",
    "exclusive",
    "limited time",
    "last chance",
    "discount",
    "save",
    "savings",
    "coupon",
    "promo code",
    "free shipping",
    "black friday",
    "cyber monday",
    "new arrivals",
    "shop now",
    "buy now",
    "best seller",
    "marketing",
    "advertisement",
}

_PROMO_SUBJECT_REGEX = [
    re.compile(r"\b\d{1,3}%\s*off\b"),
    re.compile(r"\b\d{1,3}\s*% off\b"),
    re.compile(r"\bfree\s+shipping\b"),
    re.compile(r"\bextra\s+\d{1,3}%\s*off\b"),
    re.compile(r"\buse\s+code\s+[A-Z0-9]{3,}\b", re.I),
    re.compile(r"\bends\s+(today|tonight)\b"),
    re.compile(r"\bdeal of the day\b"),
    re.compile(r"\bnew\s+arrivals?\b"),
    re.compile(r"\bfinal\s+hours\b"),
    re.compile(r"\bclearance\b"),
    re.compile(r"\bbogo\b", re.I),
]

_EMOJI_RE = re.compile(
    r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]"
)

_SENDER_BLOCK_TOKENS = {
    "news",
    "newsletter",
    "digest",
    "day",
    "daily",
    "week",
    "weekly",
    "month",
    "monthly",
    "roundup",
    "bulletin",
    "updates",
    "update",
    "announcements",
    "announcement",
    "press",
    "media",
    "nytimes",
    "new york times",
    "wsj",
    "wall street journal",
    "bloomberg",
    "reuters",
    "associated press",
    "ap news",
    "cnn",
    "bbc",
    "fox news",
    "nbc news",
    "cbs news",
    "abc news",
    "guardian",
    "the guardian",
    "economist",
    "financial times",
    "ft.com",
    "axios",
    "politico",
    "the information",
    "techcrunch",
    "the verge",
    "wired",
    "arstechnica",
    "marketing",
    "promo",
    "promotion",
    "promotions",
    "offers",
    "deals",
    "deal",
    "sales",
    "sale",
    "coupon",
    "coupons",
    "discount",
    "discounts",
    "savings",
    "special",
    "exclusive",
    "mailchimp",
    "klaviyo",
    "constant contact",
    "sendgrid",
    "sendinblue",
    "campaign monitor",
    "mailerlite",
    "customer.io",
    "braze",
    "marketo",
    "pardot",
    "salesforce",
    "hubspot",
    "activecampaign",
    "convertkit",
    "substack",
    "campaign",
    "campaigns",
    "blast",
    "mailer",
    "mailing",
    "subscriber",
    "subscribers",
    "events",
    "webinar",
    "community",
}

_CORE_SIGNAL_KEYWORDS = {
    "interview",
    "deadline",
    "funding",
    "pitch",
    "meeting",
    "cofounder",
    "internship",
    "connection",
    "confirmation",
    "confirmed",
    "confirm",
    "schedule",
}


async def process_user_email_highlights(
    *,
    user_id: str,
) -> Dict[str, Any]:
    """
    Process stored user emails into high-signal highlights and persist them.

    Returns a dict summary with counts and keywords used.
    """
    if not str(user_id or "").strip():
        return {"status": "error", "error": "missing_user_id", "stored": 0, "total": 0}

    db = DatabaseClient()
    user = await db.get_user_by_id(user_id)
    if not user:
        return {"status": "error", "error": "user_not_found", "stored": 0, "total": 0}

    emails = await _fetch_all_user_emails(db, user_id)
    if not emails:
        return {"status": "ok", "stored": 0, "total": 0, "keywords_used": []}

    user_email = _normalize_email(user.get("email"))
    keywords = _build_active_keywords(user)
    highlights = _select_email_highlights(
        emails=emails,
        user_email=user_email,
        keywords=keywords,
    )

    stored = await db.store_user_email_highlights(user_id, highlights)
    return {
        "status": "ok",
        "stored": stored,
        "total": len(highlights),
        "keywords_used": sorted(list(keywords))[:50],
        "highlights": highlights,
    }


async def process_new_email_highlights(
    *,
    user_id: str,
    emails: List[Dict[str, Any]],
    user_profile: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Process just-written emails into highlights and persist them.

    Returns a dict summary with counts and keywords used.
    """
    if not str(user_id or "").strip():
        return {"status": "error", "error": "missing_user_id", "stored": 0, "total": 0}
    if not emails:
        return {"status": "ok", "stored": 0, "total": 0, "keywords_used": []}

    db = DatabaseClient()
    user = user_profile or await db.get_user_by_id(user_id)
    if not user:
        return {"status": "error", "error": "user_not_found", "stored": 0, "total": 0}

    user_email = _normalize_email(user.get("email"))
    keywords = _build_active_keywords(user)
    highlights = _select_email_highlights(
        emails=emails,
        user_email=user_email,
        keywords=keywords,
    )

    stored = await db.store_user_email_highlights(user_id, highlights)
    return {
        "status": "ok",
        "stored": stored,
        "total": len(highlights),
        "keywords_used": sorted(list(keywords))[:50],
    }


def _select_email_highlights(
    *,
    emails: Iterable[Dict[str, Any]],
    user_email: Optional[str],
    keywords: set[str],
) -> List[Dict[str, Any]]:
    highlights: List[Dict[str, Any]] = []
    for email in emails:
        sender = _as_str(email.get("sender")).strip()
        sender_domain = _as_str(email.get("sender_domain")).strip().lower()
        subject = _as_str(email.get("subject")).strip()
        body = _as_str(email.get("body")).strip()

        direction = "inbound"
        if user_email and _sender_matches_user(sender, user_email):
            direction = "outbound"

        if direction == "inbound" and _is_promotional(sender, sender_domain, subject, body):
            continue

        if direction == "inbound":
            if not _has_keyword_match(
                sender=sender,
                sender_domain=sender_domain,
                subject=subject,
                body=body,
                keywords=keywords,
            ):
                continue

        highlights.append(
            {
                "message_id": email.get("message_id"),
                "direction": direction,
                "is_from_me": direction == "outbound",
                "sender": sender,
                "sender_domain": sender_domain or None,
                "subject": subject,
                "body_excerpt": _truncate(body, 240),
                "received_at": email.get("received_at"),
                "fetched_at": email.get("fetched_at"),
            }
        )

    return highlights


async def _fetch_all_user_emails(db: DatabaseClient, user_id: str) -> List[Dict[str, Any]]:
    emails: List[Dict[str, Any]] = []
    batch_size = 500
    offset = 0

    while True:
        batch = (
            db.client.table("user_emails")
            .select("message_id,sender,sender_domain,subject,body,snippet,received_at,fetched_at")
            .eq("user_id", user_id)
            .eq("is_sensitive", False)
            .order("fetched_at", desc=True)
            .range(offset, offset + batch_size - 1)
            .execute()
        )
        rows = list(batch.data or [])
        if not rows:
            break
        emails.extend(rows)
        if len(rows) < batch_size:
            break
        offset += batch_size

    return emails


def _build_active_keywords(user_profile: Dict[str, Any]) -> set[str]:
    keywords: set[str] = set(_CORE_SIGNAL_KEYWORDS)

    for interest in user_profile.get("career_interests") or []:
        interest_lower = _as_str(interest).lower().strip()
        if not interest_lower:
            continue
        if interest_lower in CAREER_TO_KEYWORDS:
            keywords.update(CAREER_TO_KEYWORDS[interest_lower])
        else:
            keywords.add(interest_lower)

    for text in _iter_text_fields(user_profile):
        keywords.update(_tokenize_text(text))

    personal_facts = user_profile.get("personal_facts") if isinstance(user_profile.get("personal_facts"), dict) else {}
    need_state = personal_facts.get("frank_need_eval") if isinstance(personal_facts, dict) else None
    if isinstance(need_state, dict):
        for key in ("targets", "outcomes"):
            values = need_state.get(key)
            if isinstance(values, list):
                for item in values:
                    keywords.update(_tokenize_text(_as_str(item)))

    return expand_keywords(keywords)


def _iter_text_fields(user_profile: Dict[str, Any]) -> Iterable[str]:
    for key in ("latest_demand", "all_demand", "all_value"):
        value = _as_str(user_profile.get(key))
        if value:
            yield value
    needs = user_profile.get("needs") or []
    if isinstance(needs, list):
        for need in needs:
            if isinstance(need, dict):
                for sub_key in ("need", "label", "topic"):
                    yield _as_str(need.get(sub_key))
            else:
                yield _as_str(need)


def _has_keyword_match(
    *,
    sender: str,
    sender_domain: str,
    subject: str,
    body: str,
    keywords: set[str],
) -> bool:
    if not keywords:
        return False
    subject_lower = subject.lower()
    sender_lower = sender.lower()
    body_lower = body.lower()
    sender_domain_lower = sender_domain.lower()

    for kw in keywords:
        kw_lower = kw.lower().strip()
        if not kw_lower:
            continue
        if kw_lower in subject_lower:
            return True
        if kw_lower in sender_lower or (sender_domain_lower and kw_lower in sender_domain_lower):
            return True
        if kw_lower in body_lower:
            return True

    return False


def _is_promotional(sender: str, sender_domain: str, subject: str, body: str) -> bool:
    sender_lower = sender.lower()
    for pattern in EXCLUDE_SENDER_PATTERNS:
        pattern_lower = pattern.lower()
        if pattern_lower in sender_lower or (sender_domain and pattern_lower in sender_domain.lower()):
            return True
    if _is_blocked_sender(sender_lower, sender_domain):
        return True
    if _is_promotional_text(subject) or _is_promotional_text(body):
        return True
    for token in _PROMO_SUBJECT_PATTERNS:
        if token in sender_lower:
            return True
    return False


def _is_blocked_sender(sender_lower: str, sender_domain: str) -> bool:
    if not sender_lower and not sender_domain:
        return False
    sender_domain_lower = (sender_domain or "").lower()
    for token in _SENDER_BLOCK_TOKENS:
        if token in sender_lower or (sender_domain_lower and token in sender_domain_lower):
            return True
    return False


def _is_promotional_text(text: str) -> bool:
    text_lower = text.lower()
    if _EMOJI_RE.search(text_lower):
        return True
    text_norm = re.sub(r"[^a-z0-9% ]+", " ", text_lower)
    text_norm = re.sub(r"\s+", " ", text_norm).strip()

    for token in _PROMO_SUBJECT_PATTERNS:
        if token in text_norm:
            return True
    for pattern in _PROMO_SUBJECT_REGEX:
        if pattern.search(text_norm):
            return True
    return False


def _sender_matches_user(sender: str, user_email: str) -> bool:
    if not sender or not user_email:
        return False
    sender_lower = sender.lower()
    user_email_lower = user_email.lower()
    if user_email_lower in sender_lower:
        return True
    emails = _EMAIL_RE.findall(sender_lower)
    return user_email_lower in emails


def _tokenize_text(text: str) -> set[str]:
    if not text:
        return set()
    tokens = set()
    for word in _WORD_RE.findall(text.lower()):
        if word in _STOPWORDS:
            continue
        tokens.add(word)
    return tokens


def _normalize_email(value: Any) -> Optional[str]:
    text = _as_str(value).strip().lower()
    if not text:
        return None
    match = _EMAIL_RE.search(text)
    return match.group(0) if match else None


def _truncate(text: str, max_len: int) -> str:
    if not text:
        return ""
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "..."


def _as_str(value: Any) -> str:
    return str(value or "").strip()
