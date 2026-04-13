"""Socket.IO listener that streams Photon iMessage events into our FastAPI app."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime
from typing import Awaitable, Callable, Dict, Any, Optional

import socketio
from cachetools import TTLCache

logger = logging.getLogger(__name__)

# Best-effort in-process dedupe for inbound events.
# This protects against transient Redis outages or Socket.IO re-deliveries that
# would otherwise cause duplicate outbound bot messages.
# Using TTLCache with bounded size to prevent memory exhaustion under high load.
_INBOUND_DEDUPE_TTL_SECONDS = 12
_INBOUND_DEDUPE_MAX_SIZE = 10000  # Maximum number of entries to prevent OOM
_INBOUND_DEDUPE_CACHE: TTLCache = TTLCache(
    maxsize=_INBOUND_DEDUPE_MAX_SIZE,
    ttl=_INBOUND_DEDUPE_TTL_SECONDS
)


def _in_memory_dedupe(key: str, *, ttl_seconds: int = _INBOUND_DEDUPE_TTL_SECONDS) -> bool:
    """Check if a message key has been seen recently.

    Uses a TTLCache with bounded size to prevent memory exhaustion.
    Note: ttl_seconds parameter is ignored (TTLCache uses fixed TTL), kept for API compatibility.

    Args:
        key: Unique message identifier
        ttl_seconds: Ignored - using TTLCache's fixed TTL

    Returns:
        True if this is a new message, False if duplicate
    """
    if not key:
        return True

    if key in _INBOUND_DEDUPE_CACHE:
        return False

    _INBOUND_DEDUPE_CACHE[key] = time.time()
    return True

# Helper function for memory monitoring
def _get_memory_mb() -> float:
    """Get current process memory usage in MB."""
    try:
        import psutil
        process = psutil.Process(os.getpid())
        return process.memory_info().rss / 1024 / 1024
    except Exception:
        return -1.0  # Return -1 if psutil not available


def _is_group_chat(chat_guid: str) -> bool:
    """
    Determine if a chat GUID represents a group chat.

    Chat GUID formats:
    - DM: "any;-;+12152073992" or "iMessage;-;email@example.com" (contains ";-;")
    - Group: "any;+;chat123456789" (contains ";+;")
    """
    if not chat_guid:
        return False
    guid = str(chat_guid)
    return ";+;" in guid or guid.startswith("chat")


class PhotonListener:
    """
    Connects to Photon Socket.IO server and forwards inbound messages to a callback.

    Listens for 'new-message' events from Photon's Socket.IO gateway and
    transforms them into a format compatible with our application's webhook handler.
    """

    def __init__(
        self,
        server_url: str,
        default_number: str,
        message_callback: Callable[[Dict[str, Any]], Awaitable[None]],
        api_key: Optional[str] = None,
    ):
        if not server_url:
            raise ValueError("Photon server URL is required")

        self.server_url = server_url.rstrip("/")
        if not self.server_url.startswith("http"):
            self.server_url = f"https://{self.server_url}"

        self.default_number = default_number
        self._callback = message_callback
        self.api_key = api_key
        self._client = socketio.AsyncClient(
            reconnection=True,
            reconnection_attempts=0,  # Infinite reconnection attempts
        )
        # Register event handlers
        self._client.on("connect", self._on_connect)
        self._client.on("disconnect", self._on_disconnect)
        self._client.on("new-message", self._handle_new_message)
        self._client.on("message-send-error", self._handle_error)
        self._client.on("error", self._handle_error)

        # Debug: Log ALL Socket.IO events
        @self._client.event
        async def __generic_event(event, *args):
            logger.debug(f"[PHOTON] Received event: {event}, args: {args}")

    async def start(self) -> None:
        """Connect to Photon server."""
        logger.info("[PHOTON] Connecting to %s", self.server_url)
        try:
            # Include API key in auth if provided (Photon v1.2.1+)
            auth = {"apiKey": self.api_key} if self.api_key else None
            await self._client.connect(self.server_url, transports=["websocket"], auth=auth)
            logger.info("[PHOTON] ✅ Successfully connected to Photon server")
        except Exception as e:
            logger.error(f"[PHOTON] ❌ Failed to connect to Photon server: {e}")
            raise

    async def stop(self) -> None:
        """Disconnect from Photon server."""
        if self._client.connected:
            await self._client.disconnect()
            logger.info("[PHOTON] Disconnected from server")

    async def _on_connect(self) -> None:
        logger.info("[PHOTON] Socket connected successfully")

    async def _on_disconnect(self) -> None:
        logger.warning("[PHOTON] Socket disconnected - will attempt to reconnect")

    async def _handle_error(self, data: Any) -> None:
        logger.error(f"[PHOTON] Socket error: {data}")

    async def _handle_new_message(self, message: Dict[str, Any]) -> None:
        """
        Handle inbound message from Photon and forward to callback.

        Transforms Photon's message format into a format compatible with
        our application's SendBlue webhook handler.

        Args:
            message: Raw message data from Photon Socket.IO event
        """
        # Crash detection: Log at entry point
        pid = os.getpid()
        mem_mb = _get_memory_mb()
        message_guid = message.get("guid", "NO_GUID")
        logger.info(f"[CRASH DETECT] MESSAGE START - PID={pid}, Memory={mem_mb:.1f}MB, GUID={message_guid[:30] if message_guid != 'NO_GUID' else 'NO_GUID'}")

        logger.info(f"[PHOTON] Received new-message event: {message}")
        try:
            if not message:
                logger.debug("[PHOTON] Dropping empty message")
                return

            # Skip messages from ourselves
            if message.get("isFromMe"):
                logger.debug("[PHOTON] Ignoring message from self")
                return  # Ignore echoes

            # Extract sender information
            handle = message.get("handle") or {}
            from_number = handle.get("address") or handle.get("id")
            if not from_number:
                logger.warning("[PHOTON] Dropping message without handle: %s", message)
                return

            # Extract message text
            text = message.get("text")
            if not text:
                # Fallback to attributed body if available
                attributed = message.get("attributedBody")
                if isinstance(attributed, list) and attributed:
                    text = attributed[0].get("string")

            # Extract attachments
            # Photon attachments have a 'guid' field - use sdk.attachments.downloadAttachment(guid) to get the file
            media_url = None
            attachments = message.get("attachments")
            if isinstance(attachments, list) and attachments:
                first_attachment = attachments[0]
                logger.info(f"[PHOTON] Found attachment: {first_attachment}")

                if isinstance(first_attachment, dict):
                    # Primary: use attachment guid (Photon's identifier)
                    attachment_guid = first_attachment.get("guid")
                    if attachment_guid:
                        media_url = f"photon-attachment:{attachment_guid}"
                        logger.info(f"[PHOTON] Found attachment guid: {attachment_guid}")
                    else:
                        # Fallback: try other field names
                        media_url = (
                            first_attachment.get("path")
                            or first_attachment.get("filename")
                            or first_attachment.get("filePath")
                            or first_attachment.get("transferName")
                        )
                        # If still no media_url but attachment exists, mark it as present
                        if not media_url and first_attachment:
                            mime = first_attachment.get("mime") or first_attachment.get("mimeType") or "unknown"
                            media_url = f"attachment:{mime}"
                            logger.info(f"[PHOTON] Using placeholder media_url: {media_url}")
                elif isinstance(first_attachment, str):
                    media_url = first_attachment

                if media_url:
                    logger.info(f"[PHOTON] Extracted media_url: {media_url}")

            # Detect location shares from iMessage
            is_location_share = False
            balloon_id = message.get("balloonBundleId") or ""
            payload_data = message.get("payloadData") or message.get("pluginPayload")

            # Check multiple indicators for location shares:
            # 1. balloonBundleId — real iMessage location shares use:
            #    "com.apple.messages.MSMessageExtensionBalloonPlugin:...:com.apple.findmy.FindMyMessagesApp"
            if balloon_id and any(kw in balloon_id.lower() for kw in (
                "findmy", "location", "map", "com.apple.locationsharing",
            )):
                is_location_share = True
            # 2. maps.apple.com URL in text
            elif text and "maps.apple.com" in text:
                is_location_share = True
            # 3. Location-related attachment MIME types
            elif media_url and any(kw in (media_url or "").lower() for kw in ("location", "vcard", "map")):
                is_location_share = True
            # 4. attributedBody breadcrumb text (e.g., "Started Sharing Location")
            if not is_location_share:
                attributed = message.get("attributedBody")
                if isinstance(attributed, list):
                    for attr in attributed:
                        runs = attr.get("runs") if isinstance(attr, dict) else []
                        if isinstance(runs, list):
                            for run in runs:
                                attrs = run.get("attributes", {}) if isinstance(run, dict) else {}
                                breadcrumb = attrs.get("__kIMBreadcrumbTextMarkerAttributeName", "")
                                if breadcrumb and "sharing location" in str(breadcrumb).lower():
                                    is_location_share = True
                                    break
                        if is_location_share:
                            break

            if is_location_share:
                logger.info("[PHOTON] Detected location share from %s (balloon=%s)", from_number, balloon_id or "n/a")
                # Clear the placeholder text (U+FFFC object replacement character)
                # so it doesn't get passed to the LLM as a garbled message
                if text and len(text.strip()) <= 1:
                    text = None

            if not text and not media_url and not is_location_share:
                logger.debug("[PHOTON] Dropping empty message from %s", from_number)
                return

            # Extract chat GUID
            chat_guid = (
                message.get("chatGuid")
                or message.get("chat_guid")
                or message.get("chatGUID")
                or None
            )
            chats = message.get("chats") or message.get("chat")
            if not chat_guid:
                if isinstance(chats, list) and chats and isinstance(chats[0], dict):
                    chat_guid = chats[0].get("guid") or chats[0].get("chatGuid") or chats[0].get("chat_guid")
                elif isinstance(chats, dict):
                    chat_guid = chats.get("guid") or chats.get("chatGuid") or chats.get("chat_guid")

            # Forward group chat messages too; the orchestrator will decide how to handle them.
            if chat_guid and _is_group_chat(chat_guid):
                logger.info(f"[PHOTON] Received group chat message from {from_number} in {chat_guid[:40]}...")

            # Idempotency check - prevent duplicate message processing.
            # Socket.IO may re-deliver messages during reconnection, but for group chats we prefer
            # to be fail-open (never block a legitimate user reply), so duplicates only log.
            import hashlib

            is_group = bool(chat_guid and _is_group_chat(chat_guid))

            fingerprint_payload = "|".join(
                [
                    str(from_number or "unknown"),
                    str(chat_guid or ""),
                    str(text or ""),
                    str(media_url or ""),
                ]
            )
            content_fingerprint = hashlib.sha256(fingerprint_payload.encode("utf-8")).hexdigest()[:16]

            # In-process dedupe (DMs only) in case Redis is down or Photon re-delivers with a new GUID.
            if not is_group:
                if not _in_memory_dedupe(f"photon_in:{from_number}:{content_fingerprint}"):
                    logger.info("[PHOTON] In-memory dedupe skip sender=%s", from_number)
                    return

            message_guid = message.get("guid")
            # Use environment-based prefix to prevent local/production key conflicts
            from app.config import settings
            env_prefix = "dev_" if settings.app_env == "development" else ""
            if message_guid:
                idempotency_key = f"{env_prefix}photon_msg:{message_guid}"
            else:
                idempotency_key = f"{env_prefix}photon_msg_hash:{content_fingerprint}"
                logger.warning(f"[PHOTON] No GUID in message, using content hash: {idempotency_key}")

            logger.info(f"[CRASH DETECT] BEFORE IDEMPOTENCY CHECK - PID={pid}")
            logger.info(f"[PHOTON] Checking idempotency for: {idempotency_key}")
            from app.utils.redis_client import redis_client
            is_new = redis_client.check_idempotency(idempotency_key, ttl=300)
            logger.info(f"[PHOTON] Idempotency check result: is_new={is_new}")
            logger.info(f"[CRASH DETECT] AFTER IDEMPOTENCY CHECK - PID={pid}, is_new={is_new}")
            if not is_new and not is_group:
                logger.info(f"[PHOTON] Skipping duplicate message: {idempotency_key}")
                return  # Already processed within TTL
            if not is_new and is_group:
                logger.warning(f"[PHOTON] Duplicate key for group chat (processing anyway): {idempotency_key}")

            # Cache the real chat GUID from Apple for this sender (DMs only).
            # This enables typing indicators to work correctly for phone numbers
            # by using Apple's actual GUID (which includes iMessage;-; or SMS;-; prefix).
            # Do NOT cache group chat GUIDs under a sender handle (would break DM typing indicators).
            if chat_guid and from_number and not _is_group_chat(chat_guid):
                from app.utils.redis_client import redis_client
                redis_client.cache_chat_guid(from_number, chat_guid, ttl=86400)
                logger.debug(f"[PHOTON] Cached chat GUID for {from_number}: {chat_guid[:30]}...")

            # Transform to SendBlue-compatible format for our webhook handler
            payload = {
                "from_number": from_number,
                "to_number": self.default_number,
                "content": text,
                "message_id": message_guid or f"photon_hash:{content_fingerprint}",
                "timestamp": datetime.utcnow().isoformat(),
                "chat_guid": chat_guid,
                "is_outbound": False,  # Inbound message
                "status": "received",
                "media_url": media_url,  # Pass extracted media URL
                "is_location_share": is_location_share,
            }

            logger.info(f"[PHOTON] Forwarding message to callback - from: {from_number}, text: {(text or '')[:50]}...")
            logger.debug(f"[PHOTON] Full payload: {payload}")

            # Forward to callback and await it so exceptions are properly caught
            logger.info(f"[CRASH DETECT] BEFORE CALLBACK - PID={pid}, Memory={_get_memory_mb():.1f}MB")
            await self._callback(payload)
            logger.info(f"[CRASH DETECT] AFTER CALLBACK - PID={pid}, Memory={_get_memory_mb():.1f}MB")

        except SystemExit as exc:
            logger.critical(f"[CRASH DETECT] SystemExit caught in message handler - PID={pid}: {exc}", exc_info=True)
            raise  # Re-raise to preserve exit behavior
        except KeyboardInterrupt as exc:
            logger.critical(f"[CRASH DETECT] KeyboardInterrupt caught in message handler - PID={pid}: {exc}", exc_info=True)
            raise  # Re-raise to preserve interrupt behavior
        except Exception as exc:
            logger.error(f"[CRASH DETECT] Exception in message handler - PID={pid}: {exc}", exc_info=True)
            logger.error("[PHOTON] Failed to process inbound message: %s", exc, exc_info=True)
