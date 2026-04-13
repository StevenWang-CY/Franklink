"""
Group chat opinion follow-up for icebreaker discussions.

Group chat feature (owned by app/groupchat/features).

Once Frank sends the news + discussion prompt, we wait for BOTH users to respond
in the group chat. When enabled, Frank can send one short follow-up opinion
to keep momentum.

By default, Frank stays quiet unless explicitly invoked by a message that
starts with "frank ...".
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

from app.database.client import DatabaseClient
from app.integrations.azure_openai_client import AzureOpenAIClient
from app.integrations.photon_client import PhotonClient
from app.groupchat.io import GroupChatRecorder, GroupChatSender
from app.config import settings

logger = logging.getLogger(__name__)

_FRANK_INVOKE_RE = re.compile(r"^\s*@?(?:hey|hi|yo)?\s*frank\b[\s,:;.!?\-]*(.*)$", re.IGNORECASE)

def _fallback_sender_name(sender_phone: str | None) -> str:
    handle = str(sender_phone or "").strip()
    if handle and "@" in handle:
        local = handle.split("@", 1)[0].strip().lower()
        local = re.sub(r"[^a-z0-9]+", " ", local).strip()
        token = (local.split(" ", 1)[0] if local else "").strip()
        return (token[:18] if token else "there")
    return "there"


def _normalize_handle(handle: str | None) -> tuple[str, str]:
    raw = str(handle or "").strip()
    if not raw:
        return ("", "")
    if "@" in raw:
        return (raw.lower(), "")
    return ("", _digits_last10(raw))

def _digits_last10(phone: str) -> str:
    digits = re.sub(r"\D+", "", str(phone or ""))
    return digits[-10:] if len(digits) >= 10 else digits


def _mask_phone(phone: str) -> str:
    d = _digits_last10(phone)
    if not d:
        return ""
    if len(d) <= 4:
        return "*" * len(d)
    return ("*" * (len(d) - 4)) + d[-4:]


@dataclass(frozen=True)
class IcebreakerContext:
    user_a_id: str
    user_b_id: str
    user_a_name: str
    user_b_name: str
    news_title: str
    news_url: str
    discussion_prompt: str
    poll_title: str
    poll_options: list[str]
    sent_at: str = ""
    active: bool = True


def _is_group_chat(chat_guid: str) -> bool:
    guid = str(chat_guid or "")
    return bool(guid) and (";+;" in guid or guid.startswith("chat"))


def _strip_code_fences(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    if cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    return cleaned.strip()


def _clean_opinion(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    # Prefer a single bubble; flatten newlines.
    text = " ".join(text.split()).lower().strip()
    text = text.rstrip().rstrip(".!?")

    if len(text) > 220:
        trimmed = text[:220].rstrip()
        cut = trimmed.rfind(" ")
        if cut > 0:
            trimmed = trimmed[:cut]
        text = trimmed.rstrip().rstrip(".!?")

    return text


def _trim_offtopic_pivot(text: str) -> str:
    """
    Guardrail for direct answers: if the model tries to "pivot" into a new topic,
    truncate the message before the pivot phrase.
    """
    s = " ".join((text or "").split()).strip()
    if not s:
        return ""

    lowered = s.lower()
    markers = (
        "switching gears",
        "new topic",
        "fresh topic",
        "quick pivot",
        "different angle",
    )
    cut_at = None
    for m in markers:
        idx = lowered.find(m)
        if idx == -1:
            continue
        cut_at = idx if cut_at is None else min(cut_at, idx)
    if cut_at is not None and cut_at > 0:
        s = s[:cut_at].strip()

    return _clean_opinion(s)


@dataclass
class UserReplyState:
    user_id: str
    name: str
    vote_index: Optional[int] = None
    vote_option: Optional[str] = None
    first_comment: Optional[str] = None
    last_comment: Optional[str] = None
    message_count: int = 0
    last_message_at: Optional[str] = None

    def has_responded(self) -> bool:
        return bool(self.vote_index) or bool(self.first_comment)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id,
            "name": self.name,
            "vote_index": self.vote_index,
            "vote_option": self.vote_option,
            "first_comment": self.first_comment,
            "last_comment": self.last_comment,
            "message_count": self.message_count,
            "last_message_at": self.last_message_at,
        }


def _now_iso() -> str:
    return datetime.utcnow().isoformat()


def _safe_json_loads(raw: str) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _looks_like_legacy_vote(text: str) -> Optional[str]:
    msg = (text or "").strip()
    if msg.lower().startswith("voted "):
        return msg[6:].strip()
    return None


def _is_generic_opinion(text: str) -> bool:
    lowered = (text or "").lower()
    generic_markers = [
        "i get where you both are coming from",
        "i get where you're coming from",
        "there's more nuance",
        "more nuance",
        "gets overlooked",
        "hard to say",
    ]
    return any(m in lowered for m in generic_markers)

def _extract_frank_invocation(text: str) -> Optional[str]:
    """
    Returns the invocation "payload" if the message is a Frank call.
    Examples:
      - "frank what do you think" -> "what do you think"
      - "hey frank" -> "" (bare invocation)
    """
    msg = (text or "").strip()
    if not msg:
        return None
    m = _FRANK_INVOKE_RE.match(msg)
    if not m:
        return None
    return (m.group(1) or "").strip()

def _invocation_requests_new_topic(invocation_payload: str) -> bool:
    msg = (invocation_payload or "").strip().lower()
    if not msg:
        return False
    markers = (
        "new topic",
        "another topic",
        "any topic",
        "any interesting topic",
        "something interesting",
        "interesting to discuss",
        "topic for both",
        "topic for us",
        "what should we talk about",
        "what to talk about",
        "what should we discuss",
        "what to discuss",
        "conversation starter",
        "give us a topic",
        "pick a topic",
        "suggest a topic",
    )
    return any(m in msg for m in markers) or ("topic" in msg and "discuss" in msg)

def _invocation_is_direct_question(invocation_payload: str) -> bool:
    """
    Heuristic: caller is asking Frank to answer something directly (not just facilitate).
    Examples:
      - "who is trump"
      - "what does vesting mean"
      - "explain rag vs finetuning"
      - "thoughts on remote vs in-office?"
    """
    msg = (invocation_payload or "").strip()
    if not msg:
        return False

    lowered = msg.lower().strip()

    if lowered.endswith("?"):
        return True
    if re.search(r"\bvs\b", lowered):
        return True

    starters = (
        "who",
        "what",
        "when",
        "where",
        "why",
        "how",
        "which",
        "is",
        "are",
        "do",
        "does",
        "did",
        "can",
        "could",
        "should",
        "would",
        "will",
        "tell me",
        "explain",
        "define",
        "summarize",
        "break down",
        "help me",
        "help us",
        "thoughts on",
        "take on",
        "opinion on",
        "what's",
        "whats",
        "who's",
        "whos",
        "what is",
        "who is",
        "difference between",
        "compare",
    )
    if any(lowered.startswith(s + " ") or lowered == s for s in starters):
        return True

    return False


def _normalize_group_mode(value: Any) -> str:
    mode = str(value or "").strip().lower()
    return mode if mode in {"active", "quiet", "muted"} else "active"


def _effective_group_mode(user_a_mode: Any, user_b_mode: Any) -> str:
    """
    Most restrictive mode wins.
    Order: muted > quiet > active
    """
    rank = {"active": 0, "quiet": 1, "muted": 2}
    a = _normalize_group_mode(user_a_mode)
    b = _normalize_group_mode(user_b_mode)
    return a if rank[a] >= rank[b] else b


def _extract_memory_anchors(recent_messages: list[Dict[str, str]], max_items: int = 8) -> list[str]:
    """
    Pull a small set of topical keywords from recent user messages.
    Used to keep Frank's replies grounded in recent chat context.
    """
    stop = {
        "this",
        "that",
        "with",
        "have",
        "just",
        "like",
        "what",
        "your",
        "from",
        "they",
        "them",
        "were",
        "when",
        "then",
        "been",
        "some",
        "more",
        "only",
        "also",
        "really",
        "because",
        "about",
        "think",
        "feel",
        "frank",
        "yeah",
        "lol",
        "lmao",
    }

    blob = " ".join(
        (m.get("text") or "")
        for m in (recent_messages or [])
        if str(m.get("role") or "").lower() == "user"
    )
    tokens = re.findall(r"[a-zA-Z]{4,}", blob.lower())
    counts = Counter(t for t in tokens if t not in stop)
    limit = max(1, min(int(max_items or 8), 12))
    return [w for w, _ in counts.most_common(limit)]


class GroupChatOpinionService:
    """
    Handles inbound group chat messages for the icebreaker follow-up.

    Uses Redis when available for:
    - icebreaker context
    - per-user first replies
    """

    CONTEXT_TTL_SEC = 7 * 24 * 60 * 60  # 7 days

    def __init__(
        self,
        *,
        db: Optional[DatabaseClient] = None,
        photon: Optional[PhotonClient] = None,
        openai: Optional[AzureOpenAIClient] = None,
        sender: Optional[GroupChatSender] = None,
    ):
        self.db = db or DatabaseClient()
        self.photon = photon or PhotonClient()
        self.openai = openai or AzureOpenAIClient()
        self.sender = sender or GroupChatSender(photon=self.photon, recorder=GroupChatRecorder(db=self.db))

    async def handle_inbound_group_message(
        self,
        *,
        chat_guid: str,
        sender_user_id: str,
        sender_phone: str | None = None,
        message_text: str,
    ) -> bool:
        """
        Record replies to the icebreaker and send Frank's opinion once both users respond.

        Returns True if the message was in a managed group chat (handled/ignored),
        False if the chat isn't managed by Franklink.
        """
        if not chat_guid or not _is_group_chat(chat_guid):
            return False

        message_text = (message_text or "").strip()
        if not message_text:
            return True

        invocation_payload = _extract_frank_invocation(message_text)
        is_invocation = invocation_payload is not None

        chat = await self.db.get_group_chat_by_guid(chat_guid)
        if chat:
            user_a_id = str(chat.get("user_a_id") or sender_user_id or "")
            user_b_id = str(chat.get("user_b_id") or sender_user_id or "")
        else:
            # Fallback: respond even if the chat record is missing.
            user_a_id = str(sender_user_id or "") or "user_a"
            user_b_id = user_a_id
            chat = {}

        # Allow messages from any sender; best-effort mapping for handle mismatches.
        if sender_user_id not in {user_a_id, user_b_id}:
            resolved = await self._resolve_sender_user_id_by_phone(
                sender_phone=sender_phone,
                user_a_id=user_a_id,
                user_b_id=user_b_id,
            )
            if resolved:
                sender_user_id = resolved

        ctx = await self._load_active_context(chat_guid=chat_guid, user_a_id=user_a_id, user_b_id=user_b_id)
        user_a_name = (ctx.user_a_name if ctx else "") or ""
        user_b_name = (ctx.user_b_name if ctx else "") or ""
        sender_name = None
        if ctx and sender_user_id in {user_a_id, user_b_id}:
            sender_name = ctx.user_a_name if sender_user_id == user_a_id else ctx.user_b_name

        if not sender_name:
            try:
                user_row = await self.db.get_user_by_id(sender_user_id)
                sender_name = str((user_row or {}).get("name") or "").strip() or None
            except Exception:
                sender_name = None
        if not user_a_name:
            try:
                row = await self.db.get_user_by_id(user_a_id)
                user_a_name = str((row or {}).get("name") or "").strip() or ""
            except Exception:
                user_a_name = ""
        if not user_b_name:
            try:
                row = await self.db.get_user_by_id(user_b_id)
                user_b_name = str((row or {}).get("name") or "").strip() or ""
            except Exception:
                user_b_name = ""

        sender_name = sender_name or (
            "user a"
            if sender_user_id == user_a_id
            else "user b"
            if sender_user_id == user_b_id
            else _fallback_sender_name(sender_phone)
        )

        if not ctx:
            ctx = self._build_fallback_context(
                user_a_id=user_a_id,
                user_b_id=user_b_id,
                user_a_name=user_a_name or "user a",
                user_b_name=user_b_name or "user b",
            )

        mode = _effective_group_mode(chat.get("user_a_mode"), chat.get("user_b_mode"))
        if mode == "muted":
            return True

        try:
            from app.utils.redis_client import redis_client
        except Exception:
            redis_client = None

        context_key = f"groupchat:icebreaker:v1:context:{chat_guid}"

        # Always respond to explicit "frank ..." invocations (unless muted).
        if is_invocation:
            await self._maybe_reply_to_invocation(
                chat_guid=chat_guid,
                redis_client=redis_client,
                sender_user_id=sender_user_id,
                sender_name=sender_name,
                invocation_payload=invocation_payload or "",
                user_a_id=user_a_id,
                user_b_id=user_b_id,
                ctx=ctx,
            )
            return True

        # Disable auto follow-ups in active conversations by default.
        if not getattr(settings, "groupchat_icebreaker_followup_opinion_enabled", False):
            # Best-effort: mark Redis context inactive so we stop evaluating follow-ups.
            try:
                if redis_client:
                    raw_ctx = redis_client.client.get(context_key)
                    if raw_ctx:
                        data = json.loads(raw_ctx)
                        if isinstance(data, dict):
                            data["active"] = False
                            redis_client.client.setex(
                                context_key,
                                self.CONTEXT_TTL_SEC,
                                json.dumps(data, ensure_ascii=False),
                            )
            except Exception:
                pass
            return True

        # If the context was already completed, never send another opinion for it.
        if ctx and not ctx.active:
            return True

        # Derive "both users responded" + "already sent opinion" from Supabase raw transcript tail.
        start_at = str(getattr(ctx, "sent_at", "") or "").strip() if ctx else ""
        try:
            rows = await self.db.get_group_chat_raw_messages_window_v1(
                chat_guid=chat_guid,
                start_at=start_at or None,
                limit=260,
            )
        except Exception:
            rows = []

        opinion_already_sent = any(
            str((r or {}).get("role") or "").lower() == "assistant"
            and str((r or {}).get("msg_type") or "") == "icebreaker_followup_opinion"
            for r in (rows or [])
            if isinstance(r, dict)
        )
        if opinion_already_sent:
            return True

        # Aggregate per-user state from messages since the icebreaker context started.
        user_a_state = UserReplyState(user_id=user_a_id, name=str(getattr(ctx, "user_a_name", "") or "user a"))
        user_b_state = UserReplyState(user_id=user_b_id, name=str(getattr(ctx, "user_b_name", "") or "user b"))
        by_id = {user_a_id: user_a_state, user_b_id: user_b_state}
        responded: set[str] = set()

        for r in rows or []:
            if not isinstance(r, dict):
                continue
            if str(r.get("role") or "").lower() != "user":
                continue
            uid = str(r.get("sender_user_id") or "").strip()
            if uid not in by_id:
                continue
            content = str(r.get("content") or "").strip()
            if not content:
                continue
            if _extract_frank_invocation(content) is not None:
                continue
            responded.add(uid)
            by_id[uid] = self._apply_message_to_state(by_id[uid], content, ctx)

        # Only send once both users have responded to this icebreaker (prevents "double send" on each reply).
        if user_a_id and user_b_id and user_a_id != user_b_id:
            if responded != {user_a_id, user_b_id}:
                return True

        try:
            recent_messages = await self._get_recent_user_messages(
                chat_guid=chat_guid,
                user_ids=[user_a_id, user_b_id],
                limit=30,
            )
            opinion = await self._generate_opinion(
                ctx=ctx,
                user_a=user_a_state,
                user_b=user_b_state,
                recent_messages=recent_messages,
            )
            if not opinion:
                raise ValueError("empty opinion")
            await self.sender.send_and_record(
                chat_guid=chat_guid,
                content=opinion,
                metadata={"type": "icebreaker_followup_opinion"},
            )

            # Best-effort: mark Redis context inactive (if present).
            try:
                if redis_client:
                    raw_ctx = redis_client.client.get(context_key)
                    if raw_ctx:
                        data = json.loads(raw_ctx)
                        if isinstance(data, dict):
                            data["active"] = False
                            redis_client.client.setex(
                                context_key,
                                self.CONTEXT_TTL_SEC,
                                json.dumps(data, ensure_ascii=False),
                            )
            except Exception:
                pass

            return True
        except Exception as e:
            logger.error("[GROUPCHAT][OPINION] Failed to send opinion: %s", e, exc_info=True)
            return True

    async def _resolve_sender_user_id_by_phone(
        self,
        *,
        sender_phone: str | None,
        user_a_id: str,
        user_b_id: str,
    ) -> str | None:
        """
        Best-effort mapping when `get_or_create_user(from_number)` creates a duplicate user row
        due to phone formatting differences. Compare last-10 digits against group participants.
        """
        sender_email, sender_digits = _normalize_handle(sender_phone)
        if not sender_email and not sender_digits:
            return None

        try:
            user_a = await self.db.get_user_by_id(user_a_id)
            user_b = await self.db.get_user_by_id(user_b_id)
        except Exception as e:
            logger.debug("[GROUPCHAT][OPINION] Failed to resolve sender by phone: %s", e)
            return None

        a_handle = str((user_a or {}).get("phone_number") or "")
        b_handle = str((user_b or {}).get("phone_number") or "")
        a_email, a_digits = _normalize_handle(a_handle)
        b_email, b_digits = _normalize_handle(b_handle)

        if sender_email and a_email and sender_email == a_email:
            return user_a_id
        if sender_email and b_email and sender_email == b_email:
            return user_b_id
        if sender_digits and a_digits and sender_digits == a_digits:
            return user_a_id
        if sender_digits and b_digits and sender_digits == b_digits:
            return user_b_id
        return None

    async def _maybe_reply_to_invocation(
        self,
        *,
        chat_guid: str,
        redis_client: Any,
        sender_user_id: str,
        sender_name: str,
        invocation_payload: str,
        user_a_id: str,
        user_b_id: str,
        ctx: Optional[IcebreakerContext],
    ) -> None:
        """
        Respond to "frank ..." messages with a short, helpful nudge or answer.
        """
        try:
            # Resolve participant names for group-aware replies.
            user_a_name = ""
            user_b_name = ""
            if ctx:
                user_a_name = (ctx.user_a_name or "").strip()
                user_b_name = (ctx.user_b_name or "").strip()
            if not user_a_name:
                try:
                    row = await self.db.get_user_by_id(user_a_id)
                    user_a_name = str((row or {}).get("name") or "").strip()
                except Exception:
                    user_a_name = ""
            if not user_b_name:
                try:
                    row = await self.db.get_user_by_id(user_b_id)
                    user_b_name = str((row or {}).get("name") or "").strip()
                except Exception:
                    user_b_name = ""

            other_name = None
            if sender_user_id == user_a_id and user_b_name:
                other_name = user_b_name
            elif sender_user_id == user_b_id and user_a_name:
                other_name = user_a_name
            elif sender_name:
                # If sender_user_id couldn't be mapped, fall back to name matching.
                sn = sender_name.strip().lower()
                if sn and user_a_name and sn == user_a_name.strip().lower() and user_b_name:
                    other_name = user_b_name
                elif sn and user_b_name and sn == user_b_name.strip().lower() and user_a_name:
                    other_name = user_a_name

            request_new_topic = _invocation_requests_new_topic(invocation_payload)
            # When someone calls "frank ...", answer their ask directly unless they explicitly request a new topic.
            direct_question = (not request_new_topic) and bool((invocation_payload or "").strip())

            career_interests: list[str] = []
            try:
                a_interests = await self.db.get_user_interests(user_a_id)
                b_interests = await self.db.get_user_interests(user_b_id)
                seen: set[str] = set()
                for it in (a_interests or []) + (b_interests or []):
                    s = str(it or "").strip()
                    k = s.lower()
                    if not s or k in seen:
                        continue
                    seen.add(k)
                    career_interests.append(s[:60])
                career_interests = career_interests[:8]
            except Exception:
                career_interests = []

            recent_messages = await self._get_recent_chat_messages(
                chat_guid=chat_guid,
                user_ids=[user_a_id, user_b_id],
                limit=40,
            )
            anchors = _extract_memory_anchors(recent_messages)
            seed = ""
            if not anchors and ctx:
                seed = " ".join(
                    [
                        str(ctx.news_title or ""),
                        str(ctx.discussion_prompt or ""),
                        str(ctx.poll_title or ""),
                        " ".join(ctx.poll_options or []),
                    ]
                ).strip()
            if seed:
                anchors = _extract_memory_anchors([{"role": "user", "text": seed}], max_items=8)
            memory_summary = await self._get_summary_memory(chat_guid=chat_guid, limit=6)
            logger.info(
                "[GROUPCHAT][INVOKE] context chat=%s recent=%d anchors=%d summary=%s icebreaker_ctx=%s",
                str(chat_guid)[:18],
                len(recent_messages or []),
                len(anchors or []),
                "yes" if (memory_summary or "").strip() else "no",
                "yes" if ctx else "no",
            )

            topic_hint = ""
            if anchors:
                topic_hint = str(anchors[0] or "").strip()
            elif ctx:
                topic_hint = str(ctx.poll_title or ctx.news_title or "").strip()
            if topic_hint:
                topic_hint = topic_hint[:42].strip()

            greeting = (
                f"hey {sender_name} and {other_name}"
                if other_name
                else f"hey {sender_name}, you two"
                if sender_name
                else "hey you two"
            )
            if request_new_topic:
                fallback = _clean_opinion(
                    f"{greeting} — switching gears: leetcode vs projects for hiring, each share 1 tradeoff and 1 resume bullet you’d rewrite this week which would you pick"
                )
            elif direct_question:
                fallback = _clean_opinion(
                    f"{greeting} — i’m having trouble answering that right now"
                )
            else:
                fallback = _clean_opinion(
                    f"{greeting} — what do you want me to do"
                )

            reply = ""
            try:
                reply = await self._generate_invocation_reply(
                    ctx=ctx,
                    sender_name=sender_name,
                    other_name=other_name,
                    participant_names=[n for n in [user_a_name, user_b_name] if n],
                    invocation_payload=invocation_payload,
                    request_new_topic=request_new_topic,
                    direct_question=direct_question,
                    career_interests=career_interests,
                    recent_messages=recent_messages,
                    memory_summary=memory_summary,
                    anchors=anchors,
                )
            except Exception as e:
                logger.warning("[GROUPCHAT][INVOKE] LLM invocation reply failed: %s", e)
                reply = ""

            reply = reply or fallback
            if direct_question:
                reply = _trim_offtopic_pivot(reply) or fallback

            try:
                await self.sender.send_and_record(
                    chat_guid=chat_guid,
                    content=reply,
                    metadata={"type": "frank_invocation_reply"},
                )
            except Exception as e:
                logger.warning("[GROUPCHAT][INVOKE] Failed to send invocation reply: %s", e)
                return
        except Exception as e:
            logger.warning("[GROUPCHAT][INVOKE] Failed to reply: %s", e)

    @staticmethod
    def _parse_context(raw: str) -> IcebreakerContext:
        data = json.loads(raw)
        return IcebreakerContext(
            user_a_id=str(data.get("user_a_id") or ""),
            user_b_id=str(data.get("user_b_id") or ""),
            user_a_name=str(data.get("user_a_name") or "user a"),
            user_b_name=str(data.get("user_b_name") or "user b"),
            news_title=str(data.get("news_title") or ""),
            news_url=str(data.get("news_url") or ""),
            discussion_prompt=str(data.get("discussion_prompt") or ""),
            poll_title=str(data.get("poll_title") or ""),
            poll_options=list(data.get("poll_options") or []),
            sent_at=str(data.get("sent_at") or data.get("created_at") or ""),
            active=bool(data.get("active", True)),
        )

    async def _load_active_context(
        self,
        *,
        chat_guid: str,
        user_a_id: str,
        user_b_id: str,
    ) -> Optional[IcebreakerContext]:
        """
        Load the latest icebreaker context for this chat.

        Source of truth: Redis context written by provisioning (short-lived).
        """
        try:
            from app.utils.redis_client import redis_client

            raw_context = redis_client.client.get(f"groupchat:icebreaker:v1:context:{chat_guid}")
            if raw_context:
                return self._parse_context(raw_context)
        except Exception:
            return None

        return None

    @staticmethod
    def _build_fallback_context(
        *,
        user_a_id: str,
        user_b_id: str,
        user_a_name: str,
        user_b_name: str,
    ) -> IcebreakerContext:
        """
        Minimal context used when Redis context is unavailable; keeps the opinion flow running.
        """
        return IcebreakerContext(
            user_a_id=str(user_a_id or ""),
            user_b_id=str(user_b_id or ""),
            user_a_name=user_a_name or "user a",
            user_b_name=user_b_name or "user b",
            news_title="quick chat",
            news_url="",
            discussion_prompt="quick thoughts?",
            poll_title="",
            poll_options=[],
            sent_at=_now_iso(),
            active=True,
        )

    async def _get_summary_memory(self, *, chat_guid: str, limit: int = 6) -> Optional[str]:
        """
        Best-effort: return recent stored summary segments (Supabase) as a single string.

        This replaces the old Zep thread summary for group chats.
        """
        try:
            segments = await self.db.get_group_chat_summary_segments_v1(chat_guid=chat_guid, limit=limit)
        except Exception:
            segments = []

        parts: list[str] = []
        for seg in segments or []:
            md = str(seg.get("summary_md") or "").strip()
            if not md:
                continue
            end_at = str(seg.get("segment_end_at") or "").strip()
            header = f"### segment_end_at={end_at}" if end_at else "### segment"
            parts.append(f"{header}\n{md}")

        out = "\n\n".join(parts).strip()
        if not out:
            return None
        return out[:6000]

    async def _get_recent_user_messages(
        self,
        *,
        chat_guid: str,
        user_ids: list[str],
        limit: int = 20,
    ) -> list[Dict[str, str]]:
        """
        Get recent user messages from Supabase raw transcript tail.
        Returns lightweight {name, text} dicts.
        """
        rows = await self.db.get_group_chat_raw_messages_window_v1(chat_guid=chat_guid, limit=limit)

        allowed = {str(u) for u in (user_ids or [])}
        name_by_user_id: Dict[str, str] = {}
        try:
            for uid in list(allowed)[:4]:
                user_row = await self.db.get_user_by_id(uid)
                name = str((user_row or {}).get("name") or "").strip()
                if name:
                    name_by_user_id[uid] = name
        except Exception:
            name_by_user_id = {}

        out: list[Dict[str, str]] = []
        for msg in rows or []:
            if not isinstance(msg, dict):
                continue
            if str(msg.get("role") or "").lower() != "user":
                continue
            uid = str(msg.get("sender_user_id") or "").strip()
            if uid and uid not in allowed:
                continue
            content = str(msg.get("content") or "").strip()
            if not content:
                continue
            name = name_by_user_id.get(uid) or "user"
            out.append({"name": name, "text": content[:280]})

        return out[-8:]

    async def _get_recent_chat_messages(
        self,
        *,
        chat_guid: str,
        user_ids: list[str],
        limit: int = 30,
    ) -> list[Dict[str, str]]:
        """
        Get recent group chat messages from Supabase raw transcript tail.
        Returns lightweight dicts: {role, name, text, type}.
        """
        rows = await self.db.get_group_chat_raw_messages_window_v1(chat_guid=chat_guid, limit=limit)

        allowed = {str(u) for u in (user_ids or [])}
        name_by_user_id: Dict[str, str] = {}
        try:
            for uid in list(allowed)[:4]:
                user_row = await self.db.get_user_by_id(uid)
                name = str((user_row or {}).get("name") or "").strip()
                if name:
                    name_by_user_id[uid] = name
        except Exception:
            name_by_user_id = {}

        out: list[Dict[str, str]] = []
        for msg in rows or []:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "").lower()
            if role not in {"user", "assistant"}:
                continue
            content = str(msg.get("content") or "").strip()
            if not content:
                continue

            uid = str(msg.get("sender_user_id") or "").strip()
            name = "frank" if role == "assistant" else (name_by_user_id.get(uid) or "user")
            typ = str(msg.get("msg_type") or "").strip()
            if role == "user" and uid and allowed and uid not in allowed:
                # Don't drop: in production we sometimes get duplicate user rows
                # (handle formatting differences). Keep the context for grounding.
                name = name or "user"

            out.append(
                {
                    "role": role,
                    "name": name,
                    "text": content[:320],
                    "type": typ[:60],
                }
            )

        return out[-18:]

    @staticmethod
    def _parse_reply_state(
        *,
        raw: Optional[str],
        user_id: str,
        name: str,
        ctx: IcebreakerContext,
    ) -> UserReplyState:
        if not raw:
            return UserReplyState(user_id=user_id, name=name)

        raw_str = str(raw)
        data = _safe_json_loads(raw_str)
        if data:
            vote_index = data.get("vote_index")
            return UserReplyState(
                user_id=str(data.get("user_id") or user_id),
                name=str(data.get("name") or name),
                vote_index=(int(vote_index) if vote_index is not None else None),
                vote_option=(str(data.get("vote_option")) if data.get("vote_option") else None),
                first_comment=(str(data.get("first_comment")) if data.get("first_comment") else None),
                last_comment=(str(data.get("last_comment")) if data.get("last_comment") else None),
                message_count=int(data.get("message_count") or 0),
                last_message_at=(str(data.get("last_message_at")) if data.get("last_message_at") else None),
            )

        # Legacy: a single string (maybe "voted X" or a comment).
        state = UserReplyState(user_id=user_id, name=name, message_count=1)
        legacy_vote = _looks_like_legacy_vote(raw_str)
        if legacy_vote:
            state.vote_option = legacy_vote[:80]
            if ctx.poll_options:
                lowered = [o.lower() for o in ctx.poll_options]
                try:
                    state.vote_index = lowered.index(legacy_vote.lower()) + 1
                except ValueError:
                    state.vote_index = None
            return state

        comment = raw_str.strip()[:240]
        if comment:
            state.first_comment = comment
            state.last_comment = comment
        return state

    @staticmethod
    def _apply_message_to_state(state: UserReplyState, message_text: str, ctx: IcebreakerContext) -> UserReplyState:
        msg = (message_text or "").strip()
        if not msg:
            return state

        state.message_count = int(state.message_count or 0) + 1
        state.last_message_at = _now_iso()

        if msg.isdigit() and ctx.poll_options:
            idx = int(msg)
            if 1 <= idx <= len(ctx.poll_options):
                state.vote_index = idx
                state.vote_option = ctx.poll_options[idx - 1]
                return state

        comment = msg[:240]
        if comment:
            if not state.first_comment:
                state.first_comment = comment
            state.last_comment = comment
        return state

    @staticmethod
    def _load_recent_messages(raw_items: list[str]) -> list[Dict[str, str]]:
        out: list[Dict[str, str]] = []
        for raw in raw_items or []:
            data = _safe_json_loads(str(raw)) or {}
            name = str(data.get("name") or "")
            text = str(data.get("text") or "")
            if not name or not text:
                continue
            out.append({"name": name, "text": text[:280]})
        return out[-12:]

    async def _generate_opinion(
        self,
        *,
        ctx: IcebreakerContext,
        user_a: UserReplyState,
        user_b: UserReplyState,
        recent_messages: Optional[list[Dict[str, str]]] = None,
    ) -> str:
        system_prompt = (
            "you are frank in a tiny imessage group chat with two early-career people. "
            "you just dropped a spicy news link + quick poll, and both users replied. "
            "write ONE follow-up message that feels natural and fun. "
            "you are a pre-professional networking facilitator: build rapport and shared value without being pushy.\n\n"
            "hard requirements:\n"
            "- explicitly reference both users by name\n"
            "- explicitly reference each user's vote (if present) or their comment\n"
            "- add a clear, friendly hot take (lightly opinionated)\n"
            "- end by asking ONE short follow-up question that helps them learn about each other and moves the convo forward\n\n"
            "style:\n"
            "- lowercase only\n"
            "- no emojis, no markdown, no bullets\n"
            "- do not end with punctuation\n"
            "- 1-2 short sentences, max 26 words\n"
            "- playful + likeable, never scoldy or preachy\n"
            "- avoid generic filler like 'i get where you both are coming from' or 'more nuance'\n"
            "- do not paste the url or restate the full headline"
        )

        def _user_summary(u: UserReplyState) -> Dict[str, Any]:
            return {
                "name": u.name,
                "vote": (u.vote_option if u.vote_option else None),
                "comment": (u.last_comment if u.last_comment else None),
            }

        payload: Dict[str, Any] = {
            "news_title": ctx.news_title,
            "discussion_prompt": ctx.discussion_prompt,
            "poll": {"title": ctx.poll_title, "options": ctx.poll_options},
            "users": [_user_summary(user_a), _user_summary(user_b)],
            "recent_messages": (recent_messages or [])[-8:],
        }

        user_prompt = f"CONTEXT_JSON:\n{json.dumps(payload, ensure_ascii=False)}\n\nwrite frank's message"

        async def _attempt(extra_user_instruction: str = "", temperature: float = 0.7) -> str:
            prompt = user_prompt if not extra_user_instruction else f"{user_prompt}\n\n{extra_user_instruction}"
            raw = await self.openai.generate_response(
                system_prompt=system_prompt,
                user_prompt=prompt,
                model="gpt-4o-mini",
                temperature=temperature,
                trace_label="groupchat_opinion_followup",
            )
            return _clean_opinion(_strip_code_fences(raw))

        cleaned = await _attempt()
        words = cleaned.split()
        has_question = any(
            marker in cleaned
            for marker in (
                "what ",
                "what's",
                "how ",
                "why ",
                "would ",
                "do you",
                "are you",
                "which ",
                "where ",
                "when ",
            )
        )
        need_retry = (
            not cleaned
            or _is_generic_opinion(cleaned)
            or ctx.user_a_name.lower() not in cleaned
            or ctx.user_b_name.lower() not in cleaned
            or len(words) > 30
            or not has_question
        )
        if need_retry:
            cleaned2 = await _attempt(
                extra_user_instruction=(
                    "rewrite and be more specific. you must name both users and refer to their vote/comment directly. "
                    "make it playful but friendly. keep it under 24 words and end with a short question (no question mark)"
                ),
                temperature=0.85,
            )
            if cleaned2 and not _is_generic_opinion(cleaned2):
                cleaned = cleaned2

        return cleaned

    async def _generate_invocation_reply(
        self,
        *,
        ctx: Optional[IcebreakerContext],
        sender_name: str,
        other_name: Optional[str] = None,
        participant_names: Optional[list[str]] = None,
        invocation_payload: str,
        request_new_topic: bool = False,
        direct_question: bool = False,
        career_interests: Optional[list[str]] = None,
        recent_messages: Optional[list[Dict[str, str]]] = None,
        memory_summary: Optional[str] = None,
        anchors: Optional[list[str]] = None,
    ) -> str:
        topic_bank: list[str] = [
            "specialize early vs stay broad as a junior",
            "brand name internship vs max learning density",
            "leetcode grind vs portfolio projects for hiring",
            "remote vs in-office early career development",
            "ai copilots: shortcut or skill amplifier at work",
            "masters degree vs industry experience right now",
            "open source contributions vs startup internships",
            "take-home assignments: fair signal or free labor",
        ]

        avoid_topics: list[str] = []
        if ctx:
            for s in (ctx.poll_title, ctx.news_title, ctx.discussion_prompt):
                t = str(s or "").strip().lower()
                if t and t not in avoid_topics:
                    avoid_topics.append(t[:120])
        for a in (anchors or [])[:8]:
            t = str(a or "").strip().lower()
            if t and t not in avoid_topics:
                avoid_topics.append(t[:48])

        system_prompt_parts: list[str] = [
            "you are frank, an ai relationship concierge embedded in a tiny imessage GROUP CHAT with exactly two people.\n"
            "your mission is to transform weak, short-lived peer connections into long-lasting, meaningful pre-professional relationships BETWEEN THESE TWO PEOPLE.\n"
            "you reduce friction, create shared value, and keep it fun without being pushy.\n"
            "you only speak when someone explicitly calls you by name.\n\n"
            "critical context:\n"
            "- this is NOT a 1:1 dm; speak to both people at once in the group\n"
            "- never talk like you’re messaging only the caller\n"
            "- do NOT suggest introducing a third person or setting up an intro with someone else unless they explicitly ask\n\n"
        ]

        if direct_question:
            system_prompt_parts.append(
                "if the caller asks a direct question (facts, definitions, quick explanations, or your take):\n"
                "- answer the caller’s question directly in 1–2 short, neutral sentences\n"
                "- do not pivot into facilitation, engagement prompts, or next steps\n"
                "- do not ask follow-up questions\n\n"
            )

        what_to_produce = (
            "what to produce:\n"
            "- start by addressing both participants in one phrase (e.g., 'eric + yincheng —' or 'eric and you two —')\n"
        )
        if direct_question:
            what_to_produce += "- directly answer the caller’s ask and then stop\n\n"
        else:
            what_to_produce += (
                "- give a quick, playful callback to what they were discussing\n"
                "- add one small, low-friction next step they can do together (resume swap, mock interview, 10-min call agenda, small build)\n"
                "- ask one question that invites BOTH to answer\n\n"
            )

        engagement_section = ""
        if not direct_question:
            engagement_section = (
                "engagement (important):\n"
                "- avoid jumping straight to scheduling a call; start with an easy in-chat micro-step, and only suggest a call if both seem into it\n\n"
            )

        grounding_line = (
            "- if the ask is ambiguous, pick the most likely interpretation and answer briefly; do not ask clarifying questions\n\n"
            if direct_question
            else "- if memory is too thin, ask one clarifying question to both people\n\n"
        )

        max_words_line = "- one short message, max 55 words\n" if direct_question else "- one short message, max 34 words\n"
        style_constraints = (
            "style constraints:\n"
            "- lowercase only\n"
            "- no emojis, no markdown, no bullets\n"
            f"{max_words_line}"
            "- end without punctuation; avoid question marks entirely\n"
            "- never mention zep, memory, databases, logs, or that you are reading history\n"
        )

        system_prompt_parts.extend(
            [
                (
                    "if the caller asks for a new/interesting topic:\n"
                    "- do NOT re-ask about the current thread or the last icebreaker\n"
                    "- propose a fresh, debate-worthy topic (pick from topic_bank or invent a similar one) that fits their career interests\n"
                    "- you may optionally do a 3-5 word pivot referencing what they were just discussing, but the NEW topic must be different\n\n"
                    "topic guidance:\n"
                    "- prefer fun, slightly disputable career/tech topics that lead to a concrete next step\n"
                    "- avoid repeating phrases from avoid_topics\n"
                    "- when you pivot to a new topic, explicitly signal it with 'switching gears' or 'new topic'\n\n"
                    "grounding (mandatory):\n"
                    "- base your reply on the provided chat memory (recent_messages + optional memory_summary)\n"
                    "- reference at least one concrete detail from memory OR their career interests (topic, claim, vote, or exact phrasing)\n"
                    f"{grounding_line}"
                ),
                what_to_produce,
                (
                    "safety + tone:\n"
                    "- be neutral on politics and controversial topics; don’t persuade, endorse, or attack\n"
                    "- prioritize comfort; never guilt, pressure, or imply you’re monitoring\n"
                    "- stay lightweight and practical; no lectures\n\n"
                ),
                engagement_section,
                style_constraints,
            ]
        )

        system_prompt = "".join(system_prompt_parts)
        if direct_question:
            system_prompt = (
                "you are frank, an ai assistant inside an imessage group chat with two people.\n"
                "you only speak when someone explicitly calls you by name.\n\n"
                "task:\n"
                "- answer the caller’s request directly and only about what they asked\n"
                "- do not pivot into a new topic, facilitation, networking prompts, or conversation starters\n"
                "- do not use phrases like 'switching gears' or 'new topic'\n"
                "- if you need info to fulfill the request, ask at most one clarifying question, strictly about the request\n\n"
                "safety:\n"
                "- be neutral on politics and controversial topics; don’t persuade, endorse, or attack\n\n"
                "style:\n"
                "- one short message\n"
                "- lowercase only\n"
                "- no emojis, no markdown, no bullets\n"
                "- avoid question marks and end without punctuation\n"
            )

        payload: Dict[str, Any] = {
            "caller_name": sender_name,
            "other_participant_name": (other_name or ""),
            "participant_names": participant_names or [],
            "caller_ask": (invocation_payload or ""),
            "request_new_topic": bool(request_new_topic),
            "direct_question": bool(direct_question),
            "career_interests": career_interests or [],
            "topic_bank": topic_bank,
            "avoid_topics": avoid_topics,
            "memory_summary": (memory_summary or ""),
            "anchors": anchors or [],
            "recent_messages": (recent_messages or [])[-18:],
        }
        if ctx:
            payload["icebreaker"] = {
                "news_title": ctx.news_title,
                "discussion_prompt": ctx.discussion_prompt,
                "poll_title": ctx.poll_title,
                "poll_options": ctx.poll_options,
                "active": ctx.active,
            }

        transcript_lines: list[str] = []
        for m in (payload.get("recent_messages") or [])[-24:]:
            if not isinstance(m, dict):
                continue
            who = str(m.get("name") or m.get("role") or "user").strip()[:24]
            txt = str(m.get("text") or "").strip()
            if not txt:
                continue
            transcript_lines.append(f"{who}: {txt}")
        transcript = "\n".join(transcript_lines)

        user_prompt = (
            f"CONTEXT_JSON:\n{json.dumps(payload, ensure_ascii=False)}\n\n"
            f"MEMORY_SUMMARY:\n{(payload.get('memory_summary') or '').strip()}\n\n"
            f"RECENT_CHAT_TRANSCRIPT:\n{transcript}\n\n"
            "write frank's one message"
        )

        async def _attempt(extra_user_instruction: str = "", temperature: float = 0.7) -> str:
            prompt = user_prompt if not extra_user_instruction else f"{user_prompt}\n\n{extra_user_instruction}"
            raw = await self.openai.generate_response(
                system_prompt=system_prompt,
                user_prompt=prompt,
                model="gpt-4o-mini",
                temperature=temperature,
                trace_label="groupchat_frank_invocation",
            )
            return _clean_opinion(_strip_code_fences(raw))

        caller = (sender_name or "").strip()
        other = (other_name or "").strip()
        anchors = anchors or []
        career_interests = career_interests or []

        def _is_placeholder_name(name: str) -> bool:
            n = (name or "").strip().lower()
            return (not n) or n in {"there", "user", "user a", "user b"}

        participant_names_clean = [str(n).strip() for n in (participant_names or []) if str(n).strip()]
        meaningful_participants = [n for n in participant_names_clean if not _is_placeholder_name(n)]
        require_both_names = len(meaningful_participants) >= 2
        require_caller_name = bool(caller and not _is_placeholder_name(caller))
        require_other_name = bool(other and not _is_placeholder_name(other))

        def _mentions_required_people(text: str) -> bool:
            lowered = (text or "").lower()
            if require_both_names:
                return all(n.lower() in lowered for n in meaningful_participants[:2])
            if require_other_name:
                return other.lower() in lowered
            return any(m in lowered for m in ("you two", "you both", "both of you"))

        def _looks_like_third_party_intro(text: str) -> bool:
            lowered = (text or "").lower()
            bad = (
                "set up a quick intro with",
                "introduce you to",
                "connect you with",
                "someone in your field",
                "someone else",
            )
            return any(p in lowered for p in bad)

        def _mentions_interest(text: str) -> bool:
            lowered = (text or "").lower()
            tokens: list[str] = []
            stop = {"with", "your", "from", "that", "this", "have", "just", "like", "what", "about", "think"}
            for it in career_interests[:8]:
                for w in re.findall(r"[a-zA-Z]{4,}", str(it).lower()):
                    if w in stop:
                        continue
                    tokens.append(w)
            for it in topic_bank[:8]:
                for w in re.findall(r"[a-zA-Z]{4,}", str(it).lower()):
                    if w in stop:
                        continue
                    tokens.append(w)
            tokens = list(dict.fromkeys(tokens))[:10]
            if not tokens:
                return True
            return any(t in lowered for t in tokens)

        def _looks_like_new_topic(text: str) -> bool:
            if not request_new_topic:
                return True
            lowered = (text or "").lower()
            markers = ("switching gears", "new topic", "fresh topic", "different angle", "switch it up", "quick pivot")
            return any(m in lowered for m in markers)

        def _mentions_query_keyword(text: str) -> bool:
            if not direct_question:
                return True
            lowered = (text or "").lower()
            ask = (invocation_payload or "").lower()
            tokens = [t for t in re.findall(r"[a-z0-9]{4,}", ask) if t not in {"frank", "what", "when", "where", "which", "that", "this"}]
            tokens = tokens[:6]
            if not tokens:
                return True
            return any(t in lowered for t in tokens)

        def _meets_requirements(text: str) -> bool:
            if not text:
                return False
            lowered = text.lower()
            if _is_generic_opinion(text):
                return False
            if direct_question:
                # Direct asks: accept concise answers; final output is post-processed to remove pivots.
                pass
            else:
                if require_caller_name and caller.lower() not in lowered:
                    return False
                if not _mentions_required_people(text):
                    return False
                if anchors and (not request_new_topic) and (not any(a.lower() in lowered for a in anchors)):
                    return False
                if request_new_topic and not _looks_like_new_topic(text):
                    return False
                if request_new_topic and not _mentions_interest(text):
                    return False
            if _looks_like_third_party_intro(text):
                ask = (invocation_payload or "").lower()
                if not any(k in ask for k in ("intro", "introduce", "connect")):
                    return False
            max_words = 70 if direct_question else 40
            if len(text.split()) > max_words:
                return False
            return True

        cleaned = await _attempt()
        if _meets_requirements(cleaned):
            return cleaned

        cleaned2 = await _attempt(
            extra_user_instruction=(
                "rewrite: answer the caller’s ask directly. do not pivot or suggest a new topic. keep it under 70 words and end without punctuation"
                if direct_question
                else (
                    "rewrite to be more specific and memory-grounded. start by addressing both participants in one phrase. "
                    "include both participant names (or say 'you two' if a name is missing). "
                    + (
                        "caller asked for a new topic: explicitly say 'switching gears' or 'new topic', pick a topic from topic_bank that is NOT in avoid_topics, and tie it to career_interests. "
                        if request_new_topic
                        else "reference one concrete detail from recent_messages (use one of the anchors). "
                    )
                    + "suggest one low-friction next step they can do together. keep it under 30 words and end without punctuation"
                )
            ),
            temperature=0.85,
        )
        if _meets_requirements(cleaned2):
            return cleaned2

        return ""
