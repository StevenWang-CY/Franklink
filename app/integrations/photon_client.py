"""Photon API client for Frank's iMessage integration."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Optional, Dict, Any, List
from urllib.parse import quote

import httpx
import socketio
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import settings
from app.utils.phone_validator import (
    is_valid_phone_number,
    normalize_phone_number,
    get_invalid_phone_reason,
)

logger = logging.getLogger(__name__)

# Global Socket.IO client for chat operations that require it
_socketio_client: Optional[socketio.AsyncClient] = None
_socketio_connected: bool = False


async def _get_socketio_client(server_url: str, api_key: Optional[str] = None) -> socketio.AsyncClient:
    """Get or create a Socket.IO client for Photon server."""
    global _socketio_client, _socketio_connected

    if _socketio_client is None:
        _socketio_client = socketio.AsyncClient(
            reconnection=True,
            reconnection_attempts=3,
        )

        @_socketio_client.on("connect")
        async def on_connect():
            global _socketio_connected
            _socketio_connected = True
            logger.info("[PHOTON] Socket.IO client connected for chat operations")

        @_socketio_client.on("disconnect")
        async def on_disconnect():
            global _socketio_connected
            _socketio_connected = False
            logger.warning("[PHOTON] Socket.IO client disconnected")

    if not _socketio_connected:
        url = server_url.rstrip("/")
        if not url.startswith("http"):
            url = f"https://{url}"
        auth = {"apiKey": api_key} if api_key else None
        await _socketio_client.connect(url, transports=["websocket"], auth=auth)
        # Wait for connection to establish
        await asyncio.sleep(0.5)

    return _socketio_client


class PhotonClientError(Exception):
    """Custom exception raised for Photon API failures."""


class PhotonClient:
    """
    Photon HTTP client for iMessage integration via Advanced iMessage Kit.

    Supports both phone numbers and Apple ID (email) recipients.

    Photon exposes an HTTP + Socket.IO gateway via the Advanced iMessage Kit reference server.
    For sending messages we use the REST API:
        POST /api/v1/message/text
        POST /api/v1/chat/{chatGuid}/typing
        DELETE /api/v1/chat/{chatGuid}/typing
    """

    def __init__(
        self,
        server_url: Optional[str] = None,
        default_number: Optional[str] = None,
        api_key: Optional[str] = None,
    ):
        base_url = server_url or settings.photon_server_url
        if not base_url:
            raise PhotonClientError("Photon server URL is not configured")

        base_url = base_url.rstrip("/")
        if not base_url.startswith("http"):
            base_url = f"https://{base_url}"

        self.base_url = base_url
        self.default_number = default_number or settings.photon_default_number
        self.api_key = api_key or settings.photon_api_key

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10)
    )
    async def send_message(
        self,
        to_number: str,
        content: str,
        *,
        from_number: Optional[str] = None,  # For API compatibility (not used by Photon)
        chat_guid: Optional[str] = None,
        effect_id: Optional[str] = None,
        subject: Optional[str] = None,
        media_url: Optional[str] = None,  # For future implementation
        send_style: Optional[str] = None,  # For future implementation
        group_id: Optional[str] = None,  # For future implementation
    ) -> Dict[str, Any]:
        """
        Send a text message via Photon.

        Args:
            to_number: Recipient phone number or email (Apple ID)
            content: Message text
            from_number: Unused (for API compatibility with SendBlue)
            chat_guid: Optional chat GUID (auto-generated if not provided)
            effect_id: Optional iMessage effect (e.g., "com.apple.messages.effect.CKConfettiEffect")
            subject: Optional message subject line
            media_url: Optional media attachment URL (not yet implemented)
            send_style: Optional send style (not yet implemented)
            group_id: Optional group ID (not yet implemented)

        Returns:
            Dict containing messageId and response data

        Raises:
            PhotonClientError: If validation or API call fails
        """
        if not to_number or not content:
            raise PhotonClientError("Missing required to_number or content")

        # Determine if recipient is email or phone number
        is_email = "@" in to_number

        if is_email:
            # Basic email validation
            if not self._is_valid_email(to_number):
                raise PhotonClientError(f"Invalid email address: {to_number}")
            normalized = to_number.lower().strip()
            logger.info(f"[PHOTON] Validated email recipient: {normalized}")
        else:
            # Phone number validation
            if not is_valid_phone_number(to_number):
                reason = get_invalid_phone_reason(to_number)
                raise PhotonClientError(f"Invalid phone number: {reason}")

            normalized = normalize_phone_number(to_number)
            if not normalized:
                raise PhotonClientError("Failed to normalize phone number")
            logger.info(f"[PHOTON] Validated phone recipient: {normalized}")

        # Photon Advanced iMessage Kit requires "message" (can be empty). Keep it minimal to avoid 400s.
        payload: Dict[str, Any] = {
            "chatGuid": chat_guid or self._build_chat_guid(normalized, is_email=is_email),
            "message": content or "",  # required by Photon validation
        }

        if effect_id:
            payload["effectId"] = effect_id
        if subject:
            payload["subject"] = subject

        logger.info(f"[PHOTON] Sending message to {normalized}: {content[:50]}...")
        logger.debug(f"[PHOTON] Full payload: {payload}")

        headers = {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key

        async with httpx.AsyncClient(base_url=self.base_url, timeout=30.0, headers=headers) as client:
            response = await client.post("/api/v1/message/text", json=payload)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "[PHOTON] API error %s - %s",
                    exc.response.status_code,
                    exc.response.text,
                )
                raise PhotonClientError(f"Photon send_message failed: {exc}") from exc

            data = response.json().get("data") if response.content else None
            message_id = (data or {}).get("guid")

            logger.info("✅ Message sent successfully to %s", normalized)
            logger.info("📧 Message ID: %s", message_id)
            logger.info("🔍 Full Photon response: %s", data)

            return {
                "messageId": message_id,
                "data": data,
            }

    async def start_typing(self, to_number: str, *, chat_guid: Optional[str] = None) -> None:
        """Send 'start typing' indicator for the chat."""
        # Try to get cached chat GUID from Apple first (includes correct iMessage/SMS prefix)
        if not chat_guid:
            from app.utils.redis_client import redis_client
            chat_guid = redis_client.get_cached_chat_guid(to_number)
            logger.debug(f"[PHOTON] Cached GUID for {to_number}: {chat_guid}")

        # Fall back to building GUID if no cache hit
        guid = chat_guid or self._build_chat_guid_from_number(to_number)
        if not guid:
            return

        headers = {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key

        # URL-encode the GUID to handle special chars like + in phone numbers
        encoded_guid = quote(guid, safe="")

        async with httpx.AsyncClient(base_url=self.base_url, timeout=10.0, headers=headers) as client:
            try:
                await client.post(f"/api/v1/chat/{encoded_guid}/typing")
                logger.debug(f"[PHOTON] Started typing indicator for {guid[:30]}...")
            except Exception as exc:
                # Log at info level to help debug iMessage vs SMS issues
                logger.info(f"[PHOTON] Failed to start typing indicator for {guid[:30]}: {exc}")

    async def stop_typing(self, to_number: str, *, chat_guid: Optional[str] = None) -> None:
        """Send 'stop typing' indicator."""
        # Try to get cached chat GUID from Apple first (includes correct iMessage/SMS prefix)
        if not chat_guid:
            from app.utils.redis_client import redis_client
            chat_guid = redis_client.get_cached_chat_guid(to_number)
            logger.debug(f"[PHOTON] Cached GUID for {to_number}: {chat_guid}")

        # Fall back to building GUID if no cache hit
        guid = chat_guid or self._build_chat_guid_from_number(to_number)
        if not guid:
            return

        headers = {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key

        # URL-encode the GUID to handle special chars like + in phone numbers
        encoded_guid = quote(guid, safe="")

        async with httpx.AsyncClient(base_url=self.base_url, timeout=10.0, headers=headers) as client:
            try:
                await client.delete(f"/api/v1/chat/{encoded_guid}/typing")
                logger.debug(f"[PHOTON] Stopped typing indicator for {guid[:30]}...")
            except Exception as exc:
                logger.debug(f"[PHOTON] Failed to stop typing indicator for {guid[:30]}: {exc}")

    async def mark_chat_read(self, chat_guid: str) -> None:
        """Mark a chat as read."""
        if not chat_guid:
            return

        headers = {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key

        encoded_guid = quote(chat_guid, safe="")

        async with httpx.AsyncClient(base_url=self.base_url, timeout=10.0, headers=headers) as client:
            try:
                await client.post(f"/api/v1/chat/{encoded_guid}/read")
                logger.debug(f"[PHOTON] Marked chat as read: {chat_guid[:30]}...")
            except Exception as exc:
                logger.debug(f"[PHOTON] Failed to mark chat as read: {exc}")

    async def mark_chat_unread(self, chat_guid: str) -> None:
        """Mark a chat as unread."""
        if not chat_guid:
            return

        headers = {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key

        encoded_guid = quote(chat_guid, safe="")

        async with httpx.AsyncClient(base_url=self.base_url, timeout=10.0, headers=headers) as client:
            try:
                await client.post(f"/api/v1/chat/{encoded_guid}/unread")
                logger.debug(f"[PHOTON] Marked chat as unread: {chat_guid[:30]}...")
            except Exception as exc:
                logger.debug(f"[PHOTON] Failed to mark chat as unread: {exc}")

    async def send_typing_indicator(
        self,
        to_number: str,
        duration: float = 1.0,
        *,
        chat_guid: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Show typing indicator for a short duration.

        Args:
            to_number: Recipient phone number or email
            duration: How long to show typing (in seconds)
            chat_guid: Optional chat GUID

        Returns:
            Empty dict (for API compatibility with SendBlue)
        """
        try:
            await self.start_typing(to_number, chat_guid=chat_guid)
            await asyncio.sleep(duration)
            return {}
        except Exception as e:
            logger.error(f"Error sending typing indicator: {str(e)}")
            # Don't raise - typing indicator is not critical
            return {}
        finally:
            try:
                await self.stop_typing(to_number, chat_guid=chat_guid)
            except Exception:
                pass  # Typing indicator stop is non-critical

    async def send_reaction(
        self,
        to_number: str,
        message_guid: str,
        reaction: str,
        *,
        chat_guid: Optional[str] = None,
        part_index: int = 0
    ) -> Dict[str, Any]:
        """
        Send a tapback reaction to a user's message.

        This adds an emoji reaction (like ❤️, 😂, !!, etc.) to a specific message,
        making the bot feel more human and engaged.

        Args:
            to_number: Recipient phone number or email (Apple ID)
            message_guid: GUID of the message to react to
            reaction: Reaction type - one of:
                - "love" (❤️ heart)
                - "like" (👍 thumbs up)
                - "dislike" (👎 thumbs down)
                - "laugh" (😂 haha)
                - "emphasize" (!! exclamation marks)
                - "question" (?? question marks)
            chat_guid: Optional chat GUID (auto-detected if not provided)
            part_index: Message part index for multi-part messages (default: 0)

        Returns:
            Dict containing response data, or empty dict on failure

        Example:
            >>> await client.send_reaction(
            ...     to_number="+1234567890",
            ...     message_guid="p:0/ABC-123-XYZ",
            ...     reaction="love"
            ... )
        """
        if not message_guid:
            logger.warning(f"[PHOTON] Cannot send reaction: missing message_guid")
            return {}

        # Validate reaction type
        valid_reactions = ["love", "like", "dislike", "laugh", "emphasize", "question"]
        if reaction not in valid_reactions:
            logger.warning(f"[PHOTON] Invalid reaction type: {reaction}. Must be one of {valid_reactions}")
            return {}

        # Try to get cached chat GUID from Apple first
        if not chat_guid:
            from app.utils.redis_client import redis_client
            chat_guid = redis_client.get_cached_chat_guid(to_number)

        # Fall back to building GUID if no cache hit
        if not chat_guid:
            chat_guid = self._build_chat_guid_from_number(to_number)
            if not chat_guid:
                logger.warning(f"[PHOTON] Cannot send reaction: failed to determine chat GUID")
                return {}

        payload = {
            "chatGuid": chat_guid,
            "selectedMessageGuid": message_guid,  # Photon uses 'selectedMessageGuid', not 'messageGuid'
            "reaction": reaction,
            "partIndex": part_index
        }

        logger.info(f"[PHOTON] Sending '{reaction}' reaction to message {message_guid[:20]}...")
        logger.debug(f"[PHOTON] Reaction payload: {payload}")

        headers = {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key

        async with httpx.AsyncClient(base_url=self.base_url, timeout=30.0, headers=headers) as client:
            try:
                # Photon API endpoint: /api/v1/message/react (verified from source code)
                response = await client.post("/api/v1/message/react", json=payload)
                response.raise_for_status()

                data = response.json().get("data") if response.content else None
                logger.info(f"✅ Reaction '{reaction}' sent successfully")
                logger.debug(f"🔍 Reaction response: {data}")

                return {
                    "success": True,
                    "data": data
                }

            except httpx.HTTPStatusError as exc:
                logger.warning(
                    f"[PHOTON] Reaction API error {exc.response.status_code} - {exc.response.text}"
                )
                # Don't raise - reactions are nice-to-have, not critical
                return {}
            except Exception as exc:
                logger.warning(f"[PHOTON] Failed to send reaction: {exc}")
                # Don't raise - reactions are nice-to-have, not critical
                return {}

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    async def send_message_to_chat(
        self,
        chat_guid: str,
        content: str,
        *,
        effect_id: Optional[str] = None,
        subject: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Send a message to an existing chat (1:1 or group) by chat GUID.

        This avoids needing a `to_number` for group chats and matches how services
        in this repo address group conversations.
        """
        if not chat_guid:
            raise PhotonClientError("Missing chat_guid")
        if not content:
            raise PhotonClientError("Missing content")

        payload: Dict[str, Any] = {
            "chatGuid": chat_guid,
            "message": content,
        }
        if effect_id:
            payload["effectId"] = effect_id
        if subject:
            payload["subject"] = subject

        headers = {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key

        async with httpx.AsyncClient(base_url=self.base_url, timeout=30.0, headers=headers) as client:
            response = await client.post("/api/v1/message/text", json=payload)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "[PHOTON] send_message_to_chat failed %s - %s",
                    exc.response.status_code,
                    exc.response.text,
                )
                raise PhotonClientError(f"Photon send_message_to_chat failed: {exc}") from exc

            data = response.json().get("data") if response.content else None
            return {"data": data}

    async def create_poll(self, chat_guid: str, *, title: str, options: List[str]) -> Dict[str, Any]:
        """
        Create a native iMessage poll in an existing chat.

        Backed by Photon Advanced iMessage Kit:
            POST /api/v1/poll/create
        """
        if not chat_guid:
            raise PhotonClientError("Missing chat_guid")

        cleaned_options = [(o or "").strip() for o in (options or [])]
        cleaned_options = [o for o in cleaned_options if o]
        if len(cleaned_options) < 2:
            raise PhotonClientError("Poll must have at least 2 non-empty options")

        payload: Dict[str, Any] = {
            "chatGuid": chat_guid,
            "options": cleaned_options,
        }
        cleaned_title = (title or "").strip()
        if cleaned_title:
            payload["title"] = cleaned_title

        headers = {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key

        async with httpx.AsyncClient(base_url=self.base_url, timeout=30.0, headers=headers) as client:
            response = await client.post("/api/v1/poll/create", json=payload)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "[PHOTON] create_poll failed %s - %s",
                    exc.response.status_code,
                    exc.response.text,
                )
                if exc.response.status_code == 404:
                    raise PhotonClientError("Server does not support polls (404 /api/v1/poll/create)") from exc
                raise PhotonClientError(f"Photon create_poll failed: {exc}") from exc

            data = response.json().get("data") if response.content else None
            return {"data": data}

    async def send_chunked_messages(
        self,
        to_number: str,
        message_chunks: List[str],
        from_number: Optional[str] = None,
        delay_range: tuple = (1.0, 2.5),
        show_typing: bool = True
    ) -> List[Dict[str, Any]]:
        """
        Send multiple message chunks with human-like delays and typing indicators.

        This makes the bot feel more natural, like a real person texting in bursts
        rather than sending one giant wall of text.

        Args:
            to_number: Recipient phone number or email (Apple ID)
            message_chunks: List of message chunks to send in sequence
            from_number: Unused (for API compatibility with SendBlue)
            delay_range: Tuple of (min_delay, max_delay) in seconds between chunks
            show_typing: Whether to show typing indicator before each chunk

        Returns:
            List of API response dictionaries for each chunk sent

        Example:
            >>> chunks = ["Yo! Found 2 opportunities for you",
            ...           "1. Google SWE Intern - deadline March 15",
            ...           "2. Meta ML Intern - deadline March 20"]
            >>> await client.send_chunked_messages("+1234567890", chunks)
        """
        if not message_chunks:
            logger.warning("send_chunked_messages called with empty chunks list")
            return []

        results = []
        min_delay, max_delay = delay_range

        # Determine chat_guid once for all chunks
        is_email = "@" in to_number
        if is_email:
            normalized = to_number.lower().strip()
        else:
            normalized = normalize_phone_number(to_number)
            if not normalized:
                logger.error(f"Failed to normalize phone number: {to_number}")
                return []

        chat_guid = self._build_chat_guid(normalized, is_email=is_email)

        for i, chunk in enumerate(message_chunks):
            try:
                # Show typing indicator before sending each chunk (including first one)
                if show_typing:
                    try:
                        # Short typing duration for natural feel
                        typing_duration = min(len(chunk) / 100, 1.5)  # Faster "typing" speed
                        await self.send_typing_indicator(
                            to_number=to_number,
                            duration=typing_duration,
                            chat_guid=chat_guid
                        )
                    except Exception as e:
                        logger.warning(f"Could not send typing indicator: {str(e)}")
                        # Continue anyway - typing is nice-to-have

                # Send the chunk
                logger.info(f"Sending chunk {i+1}/{len(message_chunks)} to {to_number}: {chunk[:50]}...")
                result = await self.send_message(
                    to_number=to_number,
                    content=chunk,
                    chat_guid=chat_guid
                )

                results.append({
                    "success": True,
                    "chunk_index": i,
                    "result": result
                })

                logger.info(f"Successfully sent chunk {i+1}/{len(message_chunks)}")

                # Add human-like delay before next chunk (except after last chunk)
                if i < len(message_chunks) - 1:
                    delay = random.uniform(min_delay, max_delay)
                    logger.debug(f"Waiting {delay:.1f}s before next chunk...")
                    await asyncio.sleep(delay)

            except PhotonClientError as e:
                logger.error(f"Failed to send chunk {i+1}/{len(message_chunks)}: {str(e)}")
                results.append({
                    "success": False,
                    "chunk_index": i,
                    "error": str(e)
                })
                # Continue sending remaining chunks even if one fails

            except Exception as e:
                logger.error(f"Unexpected error sending chunk {i+1}: {str(e)}", exc_info=True)
                results.append({
                    "success": False,
                    "chunk_index": i,
                    "error": str(e)
                })

        successful_chunks = sum(1 for r in results if r.get("success"))
        logger.info(f"Sent {successful_chunks}/{len(message_chunks)} chunks successfully")

        return results

    def _build_chat_guid_from_number(self, to_number: str) -> Optional[str]:
        """
        Normalize the number/email and convert to Photon chat GUID.

        Args:
            to_number: Phone number or email address

        Returns:
            Chat GUID or None if invalid
        """
        if not to_number:
            return None

        is_email = "@" in to_number

        if is_email:
            normalized = to_number.lower().strip()
        else:
            normalized = normalize_phone_number(to_number)
            if not normalized:
                return None

        return self._build_chat_guid(normalized, is_email=is_email)

    @staticmethod
    def _build_chat_guid(normalized_identifier: str, is_email: bool = False) -> str:
        """
        Build Apple iMessage chat GUID for 1:1 conversations.

        Format based on Apple's iMessage database structure:
        - Phone: iMessage;-;+<E.164 number>
        - Email: iMessage;-;<email@example.com>

        Args:
            normalized_identifier: Phone number (E.164) or email address (lowercase)
            is_email: Whether the identifier is an email address

        Returns:
            Properly formatted chat GUID
        """
        if is_email:
            # Email format: iMessage;-;email@example.com
            return f"iMessage;-;{normalized_identifier}"
        else:
            # Phone format: iMessage;-;+1234567890
            if not normalized_identifier.startswith("+"):
                normalized_identifier = f"+{normalized_identifier}"
            return f"iMessage;-;{normalized_identifier}"

    @staticmethod
    def _is_valid_email(email: str) -> bool:
        """
        Basic email validation.

        Args:
            email: Email address to validate

        Returns:
            True if email format is valid
        """
        import re
        # Basic email regex pattern
        pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        return bool(re.match(pattern, email))

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10)
    )
    async def create_group_chat(
        self,
        addresses: List[str],
        message: str,
        *,
        service: str = "iMessage",
        method: str = "private-api"
    ) -> Dict[str, Any]:
        """
        Create a group chat with multiple participants.

        Uses the Photon Advanced iMessage Kit to create a new group chat
        with the specified participants and sends an initial message.

        Args:
            addresses: List of phone numbers or emails (2+ participants)
            message: Initial message to send to the group
            service: Service type ("iMessage" or "SMS")
            method: Sending method ("private-api" for group creation)

        Returns:
            Dict containing the chat_guid and response data

        Raises:
            PhotonClientError: If validation or API call fails
        """
        if not addresses or len(addresses) < 2:
            raise PhotonClientError("Group chat requires at least 2 participants")

        if not message:
            raise PhotonClientError("Initial message is required for group creation")

        # Normalize all addresses
        normalized_addresses = []
        for addr in addresses:
            if "@" in addr:
                if not self._is_valid_email(addr):
                    raise PhotonClientError(f"Invalid email address: {addr}")
                normalized_addresses.append(addr.lower().strip())
            else:
                if not is_valid_phone_number(addr):
                    reason = get_invalid_phone_reason(addr)
                    raise PhotonClientError(f"Invalid phone number {addr}: {reason}")
                normalized = normalize_phone_number(addr)
                if not normalized:
                    raise PhotonClientError(f"Failed to normalize phone number: {addr}")
                normalized_addresses.append(normalized)

        logger.info(
            f"[PHOTON] Creating group chat with {len(normalized_addresses)} participants"
        )

        # Use the message/text endpoint with addresses array for group chat creation
        # This mirrors how the TypeScript SDK's createChat works internally
        payload = {
            "addresses": normalized_addresses,
            "message": message,
            "service": service,
            "method": method
        }

        headers = {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key

        logger.info(f"[PHOTON] Sending group chat creation request to /api/v1/chat/new")
        logger.debug(f"[PHOTON] Group chat payload: {payload}")

        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=30.0,
            headers=headers
        ) as client:
            response = await client.post("/api/v1/chat/new", json=payload)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "[PHOTON] Group chat creation failed %s - %s",
                    exc.response.status_code,
                    exc.response.text,
                )
                raise PhotonClientError(
                    f"Photon create_group_chat failed: {exc}"
                ) from exc

            data = response.json().get("data") if response.content else None

            # Extract chat GUID from response - for group chats it might be in different fields
            chat_guid = None
            if data:
                chat_guid = data.get("chatGuid") or data.get("guid")
                # If sending to multiple addresses, check the chats array
                chats = data.get("chats", [])
                if chats and not chat_guid:
                    chat_guid = chats[0].get("guid") if chats else None

            logger.info(f"[PHOTON] Group chat created successfully: {chat_guid}")
            logger.debug(f"[PHOTON] Full response: {data}")

            return {
                "chat_guid": chat_guid,
                "data": data
            }

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10)
    )
    async def update_chat(
        self,
        chat_guid: str,
        *,
        display_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Update a chat's metadata (e.g., rename a group chat).

        Photon Advanced iMessage Kit:
          PUT /api/v1/chat/:guid  { displayName: "New Name" }
        """
        guid = str(chat_guid or "").strip()
        if not guid:
            raise PhotonClientError("Missing chat GUID")

        payload: Dict[str, Any] = {}
        if display_name is not None:
            name = str(display_name).strip()
            if name:
                payload["displayName"] = name

        if not payload:
            raise PhotonClientError("No valid chat updates provided")

        headers = {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key

        encoded_guid = quote(guid, safe="")

        async with httpx.AsyncClient(base_url=self.base_url, timeout=30.0, headers=headers) as client:
            response = await client.put(f"/api/v1/chat/{encoded_guid}", json=payload)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "[PHOTON] Chat update failed %s - %s",
                    exc.response.status_code,
                    exc.response.text,
                )
                raise PhotonClientError(f"Photon update_chat failed: {exc}") from exc

            data = response.json().get("data") if response.content else None
            return {
                "chat_guid": guid,
                "data": data,
            }

    async def should_share_contact(self, chat_guid: str) -> bool:
        """
        Check whether the SDK recommends sharing your contact card in this chat.

        Returns:
        - true: sharing is recommended (typically when the other side shared theirs
                and you haven't shared yours yet)
        - false: NOT recommended (e.g. you've already shared, OR the other side
                 hasn't shared theirs yet)

        Args:
            chat_guid: The chat identifier (e.g. the guid field from chat APIs/events)

        Returns:
            bool indicating whether contact card sharing is recommended
        """
        if not chat_guid:
            raise PhotonClientError("Missing chat GUID for should_share_contact check")

        logger.info(f"[PHOTON] Checking should_share_contact for {chat_guid[:30]}...")

        headers = {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key

        encoded_guid = quote(str(chat_guid), safe="")

        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=10.0, headers=headers) as client:
                response = await client.get(f"/api/v1/chat/{encoded_guid}/share/contact/status")
                response.raise_for_status()
                data = response.json()
                # Response format: { data: { data: boolean } }
                should_share = data.get("data", {}).get("data", False) if isinstance(data.get("data"), dict) else data.get("data", False)
                logger.info(f"[PHOTON] should_share_contact for {chat_guid[:30]}...: {should_share}")
                return bool(should_share)
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "[PHOTON] should_share_contact check failed %s - %s",
                exc.response.status_code,
                exc.response.text,
            )
            # If the endpoint doesn't exist or fails, default to False (don't share)
            return False
        except Exception as exc:
            logger.warning(f"[PHOTON] should_share_contact error: {exc}")
            # If there's an error, default to False (don't share)
            return False

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10)
    )
    async def add_participant(
        self,
        chat_guid: str,
        address: str,
    ) -> Dict[str, Any]:
        """
        Add a participant to an existing group chat.

        Used for adding late joiners to multi-person group chats.

        Args:
            chat_guid: The existing group chat GUID
            address: Phone number or email of the participant to add

        Returns:
            Dict containing the response data

        Raises:
            PhotonClientError: If the operation fails
        """
        if not chat_guid:
            raise PhotonClientError("Missing chat_guid for add_participant")
        if not address:
            raise PhotonClientError("Missing address for add_participant")

        # Normalize the address
        is_email = "@" in address
        if is_email:
            if not self._is_valid_email(address):
                raise PhotonClientError(f"Invalid email address: {address}")
            normalized = address.lower().strip()
        else:
            if not is_valid_phone_number(address):
                reason = get_invalid_phone_reason(address)
                raise PhotonClientError(f"Invalid phone number: {reason}")
            normalized = normalize_phone_number(address)
            if not normalized:
                raise PhotonClientError("Failed to normalize phone number")

        logger.info(f"[PHOTON] Adding participant {normalized} to chat {chat_guid[:30]}...")

        headers = {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key

        encoded_guid = quote(chat_guid, safe="")

        # Photon SDK: sdk.chats.addParticipant(chatGuid, address)
        # Maps to: POST /api/v1/chat/{guid}/participant
        payload = {"address": normalized}

        async with httpx.AsyncClient(base_url=self.base_url, timeout=30.0, headers=headers) as client:
            response = await client.post(
                f"/api/v1/chat/{encoded_guid}/participant",
                json=payload
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "[PHOTON] add_participant failed %s - %s",
                    exc.response.status_code,
                    exc.response.text,
                )
                raise PhotonClientError(f"Photon add_participant failed: {exc}") from exc

            data = response.json().get("data") if response.content else None
            logger.info(f"[PHOTON] Successfully added participant {normalized} to chat")
            return {
                "chat_guid": chat_guid,
                "added_address": normalized,
                "data": data,
            }

    async def share_contact_card(self, chat_guid: str) -> Dict[str, Any]:
        """
        Share your contact card (iMessage "Share Name and Photo") to the specified chat.

        This sends Franklink's contact information to the user via HTTP API,
        allowing them to save it to their contacts.

        Args:
            chat_guid: The chat identifier where to share the contact card

        Returns:
            Dict containing the response data

        Raises:
            PhotonClientError: If the operation fails
        """
        if not chat_guid:
            raise PhotonClientError("Missing chat GUID for share_contact_card")

        logger.info(f"[PHOTON] Sharing contact card to chat: {chat_guid[:30]}...")

        headers = {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key

        encoded_guid = quote(str(chat_guid), safe="")

        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=30.0, headers=headers) as client:
                response = await client.post(f"/api/v1/chat/{encoded_guid}/share/contact")
                response.raise_for_status()
                data = response.json() if response.content else None
                logger.info(f"✅ Contact card shared successfully to {chat_guid[:30]}...")
                return {
                    "chat_guid": chat_guid,
                    "data": data,
                }
        except httpx.HTTPStatusError as exc:
            logger.error(
                "[PHOTON] share_contact_card failed %s - %s",
                exc.response.status_code,
                exc.response.text,
            )
            raise PhotonClientError(f"Photon share_contact_card failed: {exc}") from exc
        except Exception as exc:
            logger.error(f"[PHOTON] share_contact_card failed: {exc}")
            raise PhotonClientError(f"Photon share_contact_card failed: {exc}") from exc

    async def refresh_find_my_friends(self) -> List[Dict[str, Any]]:
        """Refresh and get friends' locations via iCloud Find My.

        Calls POST /api/v1/icloud/findmy/friends/refresh

        Returns:
            List of location items, each containing:
            - handle: phone number or email
            - coordinates: [latitude, longitude]
            - long_address: street address (optional)
            - short_address: abbreviated address (optional)
            - last_updated: timestamp
            - status: "legacy", "live", or "shallow"
            - expiry: timestamp when location expires (optional)

            Returns empty list on failure.
        """
        headers = {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key

        async with httpx.AsyncClient(
            base_url=self.base_url, timeout=30.0, headers=headers
        ) as client:
            try:
                response = await client.post(
                    "/api/v1/icloud/findmy/friends/refresh"
                )
                response.raise_for_status()
                data = response.json().get("data", [])
                logger.info(f"[PHOTON] Find My Friends: got {len(data)} locations")
                return data if isinstance(data, list) else []
            except Exception as exc:
                logger.warning(f"[PHOTON] Find My Friends refresh failed: {exc}")
                return []
