"""Proactive outreach service.

Analyzes email-derived signals and suggests networking matches.
Runs daily at 6 PM UTC.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.config import settings
from app.database.client import DatabaseClient
from app.integrations.azure_openai_client import AzureOpenAIClient
from app.proactive.config import (
    PROACTIVE_OUTREACH_COOLDOWN_DAYS,
    PROACTIVE_OUTREACH_MAX_SIGNALS,
    PROACTIVE_OUTREACH_WORKER_MAX_ATTEMPTS,
    PROACTIVE_OUTREACH_WORKER_STALE_MINUTES,
    PROACTIVE_MULTI_MATCH_THRESHOLD,
    PROACTIVE_MULTI_MATCH_MAX_TARGETS,
    compute_backoff_seconds,
)
from app.proactive.outreach.signal_extractor import extract_signals_from_zep
from app.proactive.outreach.duplicate_checker import check_duplicate_outreach
from app.proactive.outreach.message_generator import (
    build_email_context_summary,
    generate_proactive_suggestion_message,
)

logger = logging.getLogger(__name__)


def _clip(s: str, max_len: int = 40) -> str:
    """Truncate string for logging."""
    return s[:max_len] if len(s) > max_len else s


@dataclass
class ProactiveOutreachService:
    """Service for proactive outreach jobs."""

    db: DatabaseClient
    worker_id: str
    openai: Optional[AzureOpenAIClient] = None

    async def run_once(self, *, max_jobs: int) -> int:
        """
        Process a batch of proactive outreach jobs.

        Returns:
            Number of jobs processed
        """
        if not getattr(settings, "proactive_outreach_worker_enabled", False):
            logger.info("[PROACTIVE_OUTREACH] disabled")
            return 0

        # Claim available jobs
        jobs = await self.db.claim_proactive_outreach_jobs_v1(
            worker_id=self.worker_id,
            max_jobs=max_jobs,
            stale_minutes=PROACTIVE_OUTREACH_WORKER_STALE_MINUTES,
        )

        if not jobs:
            logger.debug(
                "[PROACTIVE_OUTREACH] idle worker=%s max_jobs=%d",
                self.worker_id,
                max_jobs,
            )
            return 0

        logger.info(
            "[PROACTIVE_OUTREACH] claimed worker=%s count=%d",
            self.worker_id,
            len(jobs),
        )

        processed = 0
        for job in jobs:
            processed += 1
            user_id = str((job or {}).get("user_id") or "").strip()
            attempts = int((job or {}).get("attempts") or 0)

            try:
                await self._process_job(job)
            except Exception as e:
                logger.error(
                    "[PROACTIVE_OUTREACH] job_crash user_id=%s err=%s",
                    _clip(user_id),
                    str(e),
                    exc_info=True,
                )
                if user_id:
                    try:
                        await self._fail_job(
                            user_id=user_id,
                            attempts=attempts,
                            error=f"job_crash:{type(e).__name__}:{e}",
                        )
                    except Exception:
                        await self.db.release_proactive_outreach_job_v1(
                            user_id=user_id,
                            worker_id=self.worker_id,
                        )

        return processed

    async def _process_job(self, job: Dict[str, Any]) -> None:
        """Process a single proactive outreach job."""
        user_id = str(job.get("user_id") or "").strip()
        if not user_id:
            return

        attempts = int(job.get("attempts") or 0)

        logger.info(
            "[PROACTIVE_OUTREACH] job_start user_id=%s attempts=%d",
            _clip(user_id),
            attempts,
        )

        # Get user and check preconditions
        user = await self.db.get_user_by_id(user_id)
        if not user:
            logger.warning("[PROACTIVE_OUTREACH] user_not_found user_id=%s", _clip(user_id))
            await self._skip_job(user_id=user_id, reason="user_not_found")
            return

        # Check if user is onboarded
        if not user.get("is_onboarded"):
            logger.info("[PROACTIVE_OUTREACH] user_not_onboarded user_id=%s", _clip(user_id))
            await self._skip_job(user_id=user_id, reason="not_onboarded")
            return

        # Check email connection status
        personal_facts = user.get("personal_facts") or {}
        email_connect = personal_facts.get("email_connect") or {}
        if email_connect.get("status") != "connected":
            logger.info("[PROACTIVE_OUTREACH] email_not_connected user_id=%s", _clip(user_id))
            await self._skip_job(user_id=user_id, reason="email_not_connected")
            return

        # Check proactive preference (opt-out)
        if not user.get("proactive_preference", True):
            logger.info("[PROACTIVE_OUTREACH] user_opted_out user_id=%s", _clip(user_id))
            await self._skip_job(user_id=user_id, reason="user_opted_out")
            return

        # Step 1: Extract signals directly from Zep knowledge graph
        # This replaces the old flow: highlights → intent_events → signals
        # New flow: Zep search_graph() → LLM → signals
        signals = await extract_signals_from_zep(
            user_id=user_id,
            user_profile=user,
            max_signals=PROACTIVE_OUTREACH_MAX_SIGNALS,
        )

        if not signals:
            logger.info("[PROACTIVE_OUTREACH] no_signals user_id=%s", _clip(user_id))
            await self._skip_job(user_id=user_id, reason="no_signals")
            return

        # Store signals for tracking
        await self.db.upsert_user_email_signals_v1(user_id=user_id, signals=signals)

        # Try to find match for the BEST signal only
        # Proactive outreach uses ONE signal per run (the first that gets a match).
        # This differs from CASE A Purpose Suggestion Flow where user sees ALL suggestions and picks which to pursue.
        from app.agents.tools.networking import find_match

        match_found = False
        used_signal = None
        match_result = None

        for signal in signals:
            signal_text = signal.get("signal_text") or ""
            match_type = signal.get("match_type", "single")

            # Check for duplicate outreach
            is_duplicate = await check_duplicate_outreach(
                self.db,
                user_id=user_id,
                signal_text=signal_text,
                cooldown_days=PROACTIVE_OUTREACH_COOLDOWN_DAYS,
            )
            if is_duplicate:
                logger.info(
                    "[PROACTIVE_OUTREACH] duplicate_signal user_id=%s signal=%s",
                    _clip(user_id),
                    _clip(signal_text, 50),
                )
                continue

            # Try to find match(es)
            logger.info(
                "[PROACTIVE_OUTREACH] finding_match user_id=%s rank=%d match_type=%s signal=%s",
                _clip(user_id),
                signal.get("signal_rank", 0),
                match_type,
                _clip(signal_text, 50),
            )

            if match_type == "multi":
                # Multi-match: find multiple people
                matches = await self._find_multi_matches(
                    user_id=user_id,
                    user_profile=user,
                    signal_text=signal_text,
                    max_matches=signal.get("max_matches", PROACTIVE_MULTI_MATCH_MAX_TARGETS),
                )
                if matches:
                    match_found = True
                    used_signal = signal
                    match_result = matches[0]  # Primary match for message generation
                    # Store all matches for multi-match handling
                    used_signal["all_matches"] = matches
                    break
            else:
                # Single match: find one best person
                result = await find_match(
                    user_id=user_id,
                    user_profile=user,
                    override_demand=signal_text,
                )

                if not result.success:
                    logger.info(
                        "[PROACTIVE_OUTREACH] no_match user_id=%s signal=%s",
                        _clip(user_id),
                        _clip(signal_text, 50),
                    )
                    continue

                # Check if target was recently suggested
                target_user_id = result.data.get("target_user_id")
                if target_user_id:
                    is_target_duplicate = await check_duplicate_outreach(
                        self.db,
                        user_id=user_id,
                        signal_text=signal_text,
                        target_user_id=target_user_id,
                        cooldown_days=PROACTIVE_OUTREACH_COOLDOWN_DAYS,
                    )
                    if is_target_duplicate:
                        logger.info(
                            "[PROACTIVE_OUTREACH] duplicate_target user_id=%s target=%s",
                            _clip(user_id),
                            _clip(target_user_id),
                        )
                        continue

                # Match found!
                match_found = True
                used_signal = signal
                match_result = result.data
                break

        if not match_found:
            logger.info("[PROACTIVE_OUTREACH] no_match_found user_id=%s", _clip(user_id))
            await self._skip_job(user_id=user_id, reason="no_match")
            return

        # Step 2: Create connection request and send message
        await self._create_outreach(
            user_id=user_id,
            user=user,
            signal=used_signal,
            match_result=match_result,
        )

    async def _find_multi_matches(
        self,
        *,
        user_id: str,
        user_profile: Dict[str, Any],
        signal_text: str,
        max_matches: int = 5,
    ) -> List[Dict[str, Any]]:
        """Find multiple matches for a multi-person signal."""
        from app.agents.tools.networking import find_match

        matches = []
        excluded_ids = []

        for _ in range(min(max_matches, PROACTIVE_MULTI_MATCH_MAX_TARGETS)):
            result = await find_match(
                user_id=user_id,
                user_profile=user_profile,
                override_demand=signal_text,
                excluded_user_ids=excluded_ids,
            )

            if not result.success:
                break

            target_id = result.data.get("target_user_id")
            if target_id:
                # Check if target was recently suggested
                is_dup = await check_duplicate_outreach(
                    self.db,
                    user_id=user_id,
                    signal_text=signal_text,
                    target_user_id=target_id,
                    cooldown_days=PROACTIVE_OUTREACH_COOLDOWN_DAYS,
                )
                if is_dup:
                    excluded_ids.append(target_id)
                    continue

                matches.append(result.data)
                excluded_ids.append(target_id)

        logger.info(
            "[PROACTIVE_OUTREACH] multi_match_found user_id=%s count=%d",
            _clip(user_id),
            len(matches),
        )

        return matches

    async def _create_outreach(
        self,
        *,
        user_id: str,
        user: Dict[str, Any],
        signal: Dict[str, Any],
        match_result: Dict[str, Any],
    ) -> None:
        """Create connection request and send proactive message."""
        from app.agents.tools.networking import create_connection_request
        from app.integrations.photon_client import PhotonClient
        import uuid

        target_user_id = match_result.get("target_user_id")
        target_name = match_result.get("target_name")
        target_phone = match_result.get("target_phone")
        match_type = signal.get("match_type", "single")
        all_matches = signal.get("all_matches", [match_result])

        logger.info(
            "[PROACTIVE_OUTREACH] creating_outreach user_id=%s target=%s match_type=%s",
            _clip(user_id),
            _clip(target_name or target_user_id or "?"),
            match_type,
        )

        # Generate signal_group_id for multi-match
        signal_group_id = str(uuid.uuid4()) if match_type == "multi" and len(all_matches) > 1 else None

        # Create connection request(s)
        connection_request_ids = []

        for i, match in enumerate(all_matches):
            conn_result = await create_connection_request(
                initiator_id=user_id,
                target_user_id=match.get("target_user_id"),
                target_name=match.get("target_name"),
                target_phone=match.get("target_phone"),
                match_score=match.get("match_score"),
                matching_reasons=match.get("matching_reasons", []),
                llm_introduction=match.get("llm_introduction"),
                llm_concern=match.get("llm_concern"),
            )

            if not conn_result.success:
                logger.error(
                    "[PROACTIVE_OUTREACH] connection_request_failed user_id=%s target=%s error=%s",
                    _clip(user_id),
                    _clip(match.get("target_name", "?")),
                    conn_result.error,
                )
                continue

            request_id = conn_result.data.get("connection_request_id")
            connection_request_ids.append(request_id)

            # Update with multi-match tracking if applicable
            if signal_group_id:
                try:
                    update_data = {
                        "signal_group_id": signal_group_id,
                        "signal_id": signal.get("id"),
                        "is_multi_match": True,
                        "multi_match_threshold": PROACTIVE_MULTI_MATCH_THRESHOLD,
                    }
                    # Store group_name for iMessage group naming (fallback to signal_text)
                    group_name = signal.get("group_name") or signal.get("signal_text")
                    if group_name:
                        update_data["connection_purpose"] = group_name
                    await self.db.client.table("connection_requests").update(
                        update_data
                    ).eq("id", request_id).execute()
                except Exception as e:
                    logger.warning(
                        "[PROACTIVE_OUTREACH] multi_match_update_failed request_id=%s error=%s",
                        request_id,
                        str(e),
                    )

        if not connection_request_ids:
            logger.error(
                "[PROACTIVE_OUTREACH] all_connection_requests_failed user_id=%s",
                _clip(user_id),
            )
            await self._fail_job(
                user_id=user_id,
                attempts=0,
                error="all_connection_requests_failed",
            )
            return

        # Use first connection request ID for tracking
        connection_request_id = connection_request_ids[0]

        # Generate proactive message
        # Build context from signal's extraction_reasoning (no longer needs highlights)
        email_context = build_email_context_summary([], signal)
        message = await generate_proactive_suggestion_message(
            user_profile=user,
            signal=signal,
            match_result=match_result,
            email_context=email_context,
            is_multi_match=(match_type == "multi" and len(all_matches) > 1),
            all_matches=all_matches,
        )

        if not message:
            logger.error(
                "[PROACTIVE_OUTREACH] message_generation_failed user_id=%s",
                _clip(user_id),
            )
            # Still complete - connection request was created
            message = self._fallback_message(user, signal, match_result, all_matches)

        # Send message
        user_phone = user.get("phone_number")
        if not user_phone:
            logger.error(
                "[PROACTIVE_OUTREACH] no_phone user_id=%s",
                _clip(user_id),
            )
            await self._fail_job(
                user_id=user_id,
                attempts=0,
                error="no_user_phone",
            )
            return

        try:
            photon = PhotonClient()
            await photon.send_message(to_number=user_phone, content=message)
            logger.info(
                "[PROACTIVE_OUTREACH] message_sent user_id=%s",
                _clip(user_id),
            )
        except Exception as e:
            logger.error(
                "[PROACTIVE_OUTREACH] send_failed user_id=%s error=%s",
                _clip(user_id),
                str(e),
            )
            await self._fail_job(
                user_id=user_id,
                attempts=0,
                error=f"send_failed:{e}",
            )
            return

        # Store message in conversation history
        try:
            await self.db.store_message(
                user_id=user_id,
                content=message,
                message_type="bot",
                metadata={
                    "intent": "proactive_networking_suggestion",
                    "connection_request_id": connection_request_id,
                    "connection_request_ids": connection_request_ids,
                    "proactive": True,
                    "signal_text": signal.get("signal_text"),
                    "match_type": match_type,
                    "target_name": target_name,
                    "target_names": [m.get("target_name") for m in all_matches],
                    "signal_group_id": signal_group_id,
                },
            )
        except Exception as e:
            logger.warning(
                "[PROACTIVE_OUTREACH] store_message_failed user_id=%s error=%s",
                _clip(user_id),
                str(e),
            )
            # Don't fail job - message was sent

        # Track the outreach
        signal_text = signal.get("signal_text") or ""
        try:
            await self.db.create_proactive_outreach_tracking_v1(
                user_id=user_id,
                signal_id=signal.get("id"),
                signal_text=signal_text,
                target_user_id=target_user_id,
                connection_request_id=connection_request_id,
                outreach_type="email_derived",
                message_sent=message,
            )
        except Exception as e:
            logger.warning(
                "[PROACTIVE_OUTREACH] tracking_failed user_id=%s error=%s",
                _clip(user_id),
                str(e),
            )
            # Don't fail job - outreach was sent

        # Complete job
        await self._complete_job(
            user_id=user_id,
            signal_id=signal.get("id"),
            connection_request_id=connection_request_id,
        )

    def _fallback_message(
        self,
        user: Dict[str, Any],
        signal: Dict[str, Any],
        match_result: Dict[str, Any],
        all_matches: List[Dict[str, Any]],
    ) -> str:
        """Generate a fallback message if LLM fails."""
        name = (user.get("name") or "there").split()[0].lower()
        match_type = signal.get("match_type", "single")

        if match_type == "multi" and len(all_matches) > 1:
            # Multi-match fallback
            target_names = [m.get("target_name", "someone").split()[0] for m in all_matches[:3]]
            names_str = ", ".join(target_names[:-1]) + f" and {target_names[-1]}" if len(target_names) > 1 else target_names[0]
            return f"hey {name}, found some people who might be helpful for what you're working on. {names_str} could all be good connections. want me to send intros to all of them"
        else:
            # Single match fallback
            target = (match_result.get("target_name") or "someone").split()[0]
            reasons = match_result.get("matching_reasons") or []
            reason = reasons[0] if reasons else "they might be a good connection"
            return f"hey {name}, found someone who might be helpful for what you're working on. {target} could be a good match because {reason}. want me to send an intro"

    async def _complete_job(
        self,
        *,
        user_id: str,
        signal_id: Optional[str],
        connection_request_id: str,
    ) -> None:
        """Mark job as complete (outreach sent)."""
        result = await self.db.complete_proactive_outreach_job_v1(
            user_id=user_id,
            worker_id=self.worker_id,
            demand_id=signal_id,  # Using demand_id param name for backwards compat
            connection_request_id=connection_request_id,
        )
        if result:
            logger.info(
                "[PROACTIVE_OUTREACH] job_complete user_id=%s",
                _clip(user_id),
            )
        else:
            logger.warning(
                "[PROACTIVE_OUTREACH] job_complete_failed user_id=%s",
                _clip(user_id),
            )

    async def _skip_job(
        self,
        *,
        user_id: str,
        reason: str,
    ) -> None:
        """Mark job as skipped (no outreach sent)."""
        result = await self.db.skip_proactive_outreach_job_v1(
            user_id=user_id,
            worker_id=self.worker_id,
            skip_reason=reason,
        )
        if result:
            logger.info(
                "[PROACTIVE_OUTREACH] job_skipped user_id=%s reason=%s",
                _clip(user_id),
                reason,
            )
        else:
            logger.warning(
                "[PROACTIVE_OUTREACH] job_skip_failed user_id=%s",
                _clip(user_id),
            )

    async def _fail_job(
        self,
        *,
        user_id: str,
        attempts: int,
        error: str,
    ) -> None:
        """Mark job as failed with backoff."""
        backoff = compute_backoff_seconds(attempts)
        logger.warning(
            "[PROACTIVE_OUTREACH] job_fail user_id=%s attempts=%d backoff_sec=%d err=%s",
            _clip(user_id),
            attempts,
            backoff,
            _clip(str(error), 160),
        )
        result = await self.db.fail_proactive_outreach_job_v1(
            user_id=user_id,
            worker_id=self.worker_id,
            error=error,
            backoff_seconds=backoff,
            max_attempts=PROACTIVE_OUTREACH_WORKER_MAX_ATTEMPTS,
        )
        if result is None:
            await self.db.release_proactive_outreach_job_v1(
                user_id=user_id,
                worker_id=self.worker_id,
            )
