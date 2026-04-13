"""
Icebreaker generation for newly created group chats.

Group chat feature (owned by app/groupchat/features).

After Frank sends the warm welcome intro, we follow up with:
- a short, relevant topic message, and
- a short discussion prompt.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from app.database.client import DatabaseClient
from app.database.resources_client import ResourcesDatabaseClient
from app.integrations.azure_openai_client import AzureOpenAIClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IcebreakerContent:
    news_title_message: Optional[str]
    news_url: Optional[str]
    discussion_message: str
    poll_title: str
    poll_options: List[str]
    article: Optional[Dict[str, Any]] = None


def _strip_code_fences(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    if cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    return cleaned.strip()


def _dedupe_preserve_order(items: List[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for item in items:
        key = item.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item.strip())
    return out


def _normalize_keywords(interests: List[str]) -> List[str]:
    stop = {"and", "or", "the", "a", "an", "of", "to", "in", "for", "with", "on"}
    words: List[str] = []
    for interest in interests or []:
        for w in re.split(r"[^a-zA-Z0-9]+", str(interest).lower()):
            if not w or w in stop or len(w) < 3:
                continue
            words.append(w)
    return _dedupe_preserve_order(words)


def _is_valid_url(url: str) -> bool:
    try:
        parsed = urlparse(url.strip())
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


def _normalize_url_for_message(url: str) -> str:
    """
    Shorten URLs for iMessage readability (strip tracking query params/fragments).
    Keeps scheme + host + path.
    """
    url = (url or "").strip()
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return url
        # Drop query + fragment to avoid huge tracking URLs
        normalized = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        return normalized.rstrip("/")
    except Exception:
        return url


def _clean_message_lines(text: str) -> str:
    """
    Light normalization for user-facing messages:
    - Trim whitespace
    - Strip trailing punctuation per line
    """
    lines = [(line or "").strip() for line in (text or "").splitlines()]
    cleaned = []
    for line in lines:
        if not line:
            continue
        cleaned.append(line.rstrip(".!?"))
    return "\n".join(cleaned).strip()


def _build_news_title_message(article: Optional[Dict[str, Any]]) -> Optional[str]:
    if not article:
        return None
    title = _clean_message_lines(str(article.get("title") or ""))
    if not title:
        return None
    return f"hot one for you two: {title}"[:240]


def _build_news_url(article: Optional[Dict[str, Any]]) -> Optional[str]:
    if not article:
        return None
    url = _normalize_url_for_message(str(article.get("url") or ""))
    if not url or not _is_valid_url(url):
        return None
    return url


def _coerce_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


class IcebreakerService:
    def __init__(
        self,
        *,
        db: Optional[DatabaseClient] = None,
        resources_db: Optional[ResourcesDatabaseClient] = None,
        openai: Optional[AzureOpenAIClient] = None,
    ):
        self.db = db or DatabaseClient()
        self.resources_db = resources_db or ResourcesDatabaseClient()
        self.openai = openai or AzureOpenAIClient()

    async def build_icebreaker(
        self,
        *,
        user_a_id: str,
        user_b_id: str,
        user_a_name: str,
        user_b_name: str,
        shared_interests: Optional[List[str]] = None,
        max_poll_options: int = 4,
    ) -> IcebreakerContent:
        shared_interests = shared_interests or []

        user_a_interests = await self.db.get_user_interests(user_a_id) if user_a_id else []
        user_b_interests = await self.db.get_user_interests(user_b_id) if user_b_id else []

        combined_interests = _dedupe_preserve_order(
            [*shared_interests, *user_a_interests, *user_b_interests]
        )[:8]

        article = await self._pick_relevant_article(combined_interests)

        try:
            return await self._generate_llm_icebreaker(
                user_a_name=user_a_name,
                user_b_name=user_b_name,
                interests=combined_interests,
                article=article,
                max_poll_options=max_poll_options,
            )
        except Exception as e:
            logger.error(f"[ICEBREAKER] LLM generation failed: {e}", exc_info=True)
            return self._fallback_icebreaker(
                user_a_name=user_a_name,
                user_b_name=user_b_name,
                interests=combined_interests,
                article=article,
                max_poll_options=max_poll_options,
            )

    async def _pick_relevant_article(self, interests: List[str]) -> Optional[Dict[str, Any]]:
        try:
            rows = await self.resources_db.list_news(limit=80)
        except Exception as e:
            logger.warning(f"[ICEBREAKER] News fetch failed: {e}")
            return None

        # Require URL (we always include it in the message).
        rows = [r for r in (rows or []) if _is_valid_url(_coerce_str(r.get("url") or r.get("link") or r.get("source_url")))]
        if not rows:
            return None

        # First pass: relevance by keyword overlap to reduce candidate set.
        keywords = _normalize_keywords(interests)
        scored: List[tuple[int, Dict[str, Any]]] = []
        for row in rows:
            title = _coerce_str(row.get("title") or row.get("headline"))
            summary = _coerce_str(row.get("summary") or row.get("description") or row.get("content"))
            tags = row.get("tags") or row.get("topics") or row.get("keywords") or []
            blob = " ".join([title.lower(), summary.lower(), _coerce_str(tags).lower()])
            score = sum(1 for kw in keywords if kw in blob) if keywords else 0
            scored.append((score, row))

        scored.sort(key=lambda x: x[0], reverse=True)
        top = [row for _, row in scored[:20]] if scored else []
        if not top:
            return self._summarize_article_row(rows[0])

        # Second pass: LLM picks the most "interesting/disputable" among relevant candidates.
        selected = await self._select_debate_worthy_article(top, interests=interests)
        return self._summarize_article_row(selected or top[0])

    @staticmethod
    def _summarize_article_row(row: Dict[str, Any]) -> Dict[str, Any]:
        raw_url = row.get("url") or row.get("link") or row.get("source_url") or ""
        normalized_url = _normalize_url_for_message(_coerce_str(raw_url))
        return {
            "title": row.get("title") or row.get("headline") or "",
            "url": normalized_url,
            "source": row.get("source") or row.get("publisher") or "",
            "published_at": row.get("published_at") or row.get("publishedAt") or row.get("created_at") or "",
            "summary": row.get("summary") or row.get("description") or "",
        }

    async def _select_debate_worthy_article(
        self,
        candidates: List[Dict[str, Any]],
        *,
        interests: List[str],
    ) -> Optional[Dict[str, Any]]:
        """
        Pick an article that is both relevant to the interests and likely to spark discussion.
        Avoid pure politics/partisan content; prefer career/tech/business debates (RTO, layoffs, AI, hiring, salaries).
        """
        if not candidates:
            return None

        items: List[Dict[str, str]] = []
        for idx, row in enumerate(candidates[:20], 1):
            title = _coerce_str(row.get("title") or row.get("headline")).strip()
            url = _coerce_str(row.get("url") or row.get("link") or row.get("source_url")).strip()
            summary = _coerce_str(row.get("summary") or row.get("description") or row.get("content")).strip()
            if not _is_valid_url(url):
                continue
            items.append(
                {
                    "i": str(idx),
                    "title": title[:140],
                    "url": url,
                    "summary": summary[:220],
                }
            )

        if not items:
            return None

        system_prompt = (
            "you are frank. pick ONE news item that is most likely to spark a fun, debate-worthy discussion "
            "between two early-career people, while staying relevant to their career interests. "
            "prefer tech/career/business controversies (ai impact, layoffs, rto, comp, hiring) over politics. "
            "return JSON only: {\"index\": <number>, \"why\": \"short\"}"
        )
        user_prompt = (
            f"career_interests: {', '.join(interests) if interests else 'unknown'}\n"
            f"candidates: {json.dumps(items, ensure_ascii=False)}\n"
            "pick the best index"
        )

        try:
            raw = await self.openai.generate_response(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                model="gpt-4o-mini",
                temperature=0.4,
                trace_label="icebreaker_news_selection",
            )
            data = json.loads(_strip_code_fences(raw))
            index = int(data.get("index", 1))
            index = max(1, min(index, len(candidates)))
            return candidates[index - 1]
        except Exception as e:
            logger.warning(f"[ICEBREAKER] LLM news selection failed, falling back: {e}")
            return None

    async def _generate_llm_icebreaker(
        self,
        *,
        user_a_name: str,
        user_b_name: str,
        interests: List[str],
        article: Optional[Dict[str, Any]],
        max_poll_options: int,
    ) -> IcebreakerContent:
        system_prompt = (
            "you generate a post-intro icebreaker for a small imessage group chat (user a + user b + frank). "
            "tone: casual, lowercase, friendly, not salesy. "
            "style: no emojis, no markdown, no bullets, and don't end lines with punctuation. "
            "you will be given a news item separately (title + url). your job is to write only the discussion prompt "
            "that follows the news, plus a quick poll title and options. "
            "make it a little spicy or disputable (friendly), so it sparks discussion. "
            "do not mention private dms, tracking, or that you 'pulled' data. "
            "return JSON only with keys: discussion_message, poll_title, poll_options. "
            "constraints: discussion_message <= 220 chars. poll_title <= 90 chars. poll_options is 2-"
            f"{max(2, min(6, max_poll_options))} short strings (<= 40 chars each)."
        )

        user_payload = {
            "user_a_name": user_a_name,
            "user_b_name": user_b_name,
            "career_interests": interests,
            "news": article or None,
        }

        user_prompt = (
            "create a fun icebreaker topic + poll for this new group chat.\n"
            "return JSON only.\n\n"
            f"INPUT:\n{json.dumps(user_payload, ensure_ascii=False)}"
        )

        response = await self.openai.generate_response(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            model="gpt-4o-mini",
            temperature=0.6,
            trace_label="networking_groupchat_icebreaker",
        )

        cleaned = _strip_code_fences(response)
        data = json.loads(cleaned)

        discussion_message = str(data.get("discussion_message") or "").strip()
        poll_title = str(data.get("poll_title") or "").strip()
        poll_options_raw = data.get("poll_options") or []

        if not isinstance(poll_options_raw, list):
            poll_options_raw = [str(poll_options_raw)]

        poll_options = _dedupe_preserve_order([str(o).strip() for o in poll_options_raw])
        poll_options = poll_options[: max(2, min(6, max_poll_options))]

        if not discussion_message or not poll_title or len(poll_options) < 2:
            raise ValueError("LLM returned incomplete icebreaker JSON")

        discussion_message = _clean_message_lines(discussion_message)[:220]

        return IcebreakerContent(
            news_title_message=_build_news_title_message(article),
            news_url=_build_news_url(article),
            discussion_message=discussion_message,
            poll_title=poll_title[:90],
            poll_options=[o[:40] for o in poll_options],
            article=article,
        )

    @staticmethod
    def _fallback_icebreaker(
        *,
        user_a_name: str,
        user_b_name: str,
        interests: List[str],
        article: Optional[Dict[str, Any]],
        max_poll_options: int,
    ) -> IcebreakerContent:
        focus = (interests[0] if interests else "your field").strip()

        headline = (article or {}).get("title") or ""
        url = (article or {}).get("url") or ""
        discussion = f"what do you think about this" if headline else f"what’s one thing you’re curious about in {focus} right now?"

        options = [
            "internships + recruiting",
            "side projects",
            "learning roadmap",
            "industry trends",
        ]
        options = options[: max(2, min(6, max_poll_options))]

        return IcebreakerContent(
            news_title_message=_build_news_title_message(article) if headline else None,
            news_url=_build_news_url(article) if url else None,
            discussion_message=_clean_message_lines(discussion)[:220],
            poll_title=f"pick a direction for this chat ({focus})"[:90],
            poll_options=[o[:40] for o in options],
            article=article,
        )
