"""Main FastAPI application for Frank."""

import logging
import json
from datetime import datetime
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.config import settings
from app.orchestrator import MainOrchestrator
from app.integrations.photon_client import PhotonClient
from app.integrations.photon_listener import PhotonListener
from app.integrations.stripe_client import StripeClient
from app.integrations.composio_client import ComposioClient
from app.agents.queue import AsyncOperationProcessor, register_all_handlers

_level_name = str(getattr(settings, "app_log_level", "") or "").strip().upper()
if not _level_name:
    _level_name = "DEBUG" if settings.debug else "INFO"
_level = getattr(logging, _level_name, logging.INFO)
logging.basicConfig(
    level=_level,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
if _level > logging.DEBUG:
    for noisy in ("httpx", "httpcore", "hpack", "h2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Frank API",
    description="AI Career Counselor via iMessage",
    version="1.0.0",
    debug=settings.debug,
)

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

cors_origins = settings.cors_origins_list if hasattr(settings, "cors_origins_list") else settings.cors_allowed_origins.split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

orchestrator = MainOrchestrator()
# Global Photon listener instance (Socket.IO) for inbound events
photon_listener: Optional[PhotonListener] = None
# Global async operation processor for long-running tasks
async_processor: Optional[AsyncOperationProcessor] = None


class PhotonWebhook(BaseModel):
    """Photon webhook payload model."""

    from_number: Optional[str] = None
    to_number: Optional[str] = None
    content: Optional[str] = None
    media_url: Optional[str] = None
    message_id: Optional[str] = None
    timestamp: Optional[str] = None
    chat_guid: Optional[str] = None
    is_outbound: bool = False
    status: Optional[str] = None

    class Config:
        extra = "allow"


class HealthResponse(BaseModel):
    status: str
    timestamp: str
    version: str = "1.0.0"


class SendPollRequest(BaseModel):
    chat_guid: Optional[str] = None
    to_number: Optional[str] = None
    title: str = ""
    options: List[str]


@app.get("/", response_model=HealthResponse)
async def root():
    return HealthResponse(status="healthy", timestamp=datetime.utcnow().isoformat())


@app.get("/health", response_model=HealthResponse)
async def health_check():
    return HealthResponse(status="healthy", timestamp=datetime.utcnow().isoformat())


def _require_diagnostics_token(request: Request) -> None:
    token = getattr(settings, "diagnostics_token", None)
    if not token:
        raise HTTPException(status_code=404, detail="Not found")
    provided = request.headers.get("x-diagnostics-token")
    if not provided or provided != token:
        raise HTTPException(status_code=403, detail="Forbidden")


def _safe_url_hint(url: str) -> str:
    try:
        parsed = urlparse(url)
        path = parsed.path or ""
        hint_path = path[:18] + "…" if len(path) > 18 else path
        return f"{parsed.scheme}://{parsed.netloc}{hint_path}"
    except Exception:
        return "unparseable"


@app.get("/debug/composio")
async def debug_composio(request: Request, generate_link: bool = False):
    """
    Safe Composio diagnostics endpoint.
    Requires `DIAGNOSTICS_TOKEN` to be set and passed as header `X-Diagnostics-Token`.
    """
    _require_diagnostics_token(request)

    client = ComposioClient()
    payload: Dict[str, Any] = {
        "composio_available": client.is_available(),
        "api_key_present": bool(getattr(client, "api_key", None)),
        "base_url_set": bool(getattr(client, "base_url", None)),
        "provider": getattr(client, "provider", None),
        "entity_prefix": getattr(client, "entity_prefix", None),
        "auth_config_id_present": bool(getattr(client, "auth_config_id", None)),
        "callback_url_present": bool(getattr(client, "callback_url", None)),
        "gmail_toolkit_version": getattr(client, "gmail_toolkit_version", None),
        "timestamp": datetime.utcnow().isoformat(),
    }

    try:
        import importlib.metadata as md

        payload["composio_version"] = md.version("composio")
    except Exception:
        payload["composio_version"] = None

    try:
        resolved = await client._resolve_auth_config_id(force_lookup=True)  # noqa
        payload["resolved_auth_config_id_prefix"] = f"{resolved[:6]}..." if resolved else None
    except Exception as exc:
        payload["resolved_auth_config_error"] = f"{type(exc).__name__}: {exc}"

    if generate_link:
        try:
            link = await client.initiate_gmail_connect(user_id="diagnostics")
            payload["auth_link_generated"] = bool(link)
            payload["last_error_code"] = client.get_last_connect_error_code()
            if link:
                payload["auth_link_hint"] = _safe_url_hint(link)
        except Exception as exc:
            payload["auth_link_generated"] = False
            payload["last_error_code"] = client.get_last_connect_error_code()
            payload["auth_link_error"] = f"{type(exc).__name__}: {exc}"

    return payload


@app.post("/webhook/debug")
async def debug_webhook(request: Request):
    body = await request.body()
    json_body = await request.json()
    logger.info(f"Headers: {dict(request.headers)}")
    logger.info(f"Raw body: {body.decode() if body else 'Empty'}")
    logger.info(f"JSON body: {json.dumps(json_body, indent=2)}")
    return {"status": "received", "debug": True}


@app.post("/send-message")
@limiter.limit("10/minute")
async def send_message(
    request: Request,
    to_number: str,
    content: str,
    media_url: Optional[str] = None,
):
    try:
        client = PhotonClient(
            server_url=settings.photon_server_url,
            default_number=settings.photon_default_number,
        )
        result = await client.send_message(
            to_number=to_number,
            content=content,
            media_url=media_url,
        )
        return {"status": "sent", "result": result}
    except Exception as e:
        logger.error(f"Error sending message: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/send-poll")
@limiter.limit("5/minute")
async def send_poll(request: Request, payload: SendPollRequest):
    try:
        client = PhotonClient(
            server_url=settings.photon_server_url,
            default_number=settings.photon_default_number,
        )

        chat_guid = payload.chat_guid
        if not chat_guid and payload.to_number:
            chat_guid = client._build_chat_guid_from_number(payload.to_number)

        if not chat_guid:
            raise HTTPException(status_code=400, detail="Provide chat_guid or to_number")

        result = await client.create_poll(chat_guid, title=payload.title, options=payload.options)
        return {"status": "sent", "chat_guid": chat_guid, "result": result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error sending poll: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/webhook/photon")
async def photon_webhook(webhook: PhotonWebhook):
    """Primary inbound webhook endpoint."""
    try:
        await orchestrator.handle_message(webhook)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"[PHOTON] Webhook error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    """Handle Stripe webhook events and send iMessage notifications on payment."""
    try:
        payload = await request.body()
        signature = request.headers.get("stripe-signature")
        if not signature:
            raise HTTPException(status_code=400, detail="Missing signature")

        import json

        event_data = json.loads(payload)
        event_id = event_data.get("id")
        if not event_id:
            raise HTTPException(status_code=400, detail="Missing event ID")

        from app.utils.redis_client import redis_client

        idempotency_key = f"stripe_webhook:{event_id}"
        if not redis_client.check_idempotency(idempotency_key):
            logger.warning(f"[STRIPE] Duplicate webhook detected: {event_id}")
            return {"status": "success", "duplicate": True, "message": "Event already processed"}

        # Process the webhook event
        stripe_client = StripeClient()
        try:
            result = await stripe_client.process_webhook_event(payload, signature)
            logger.info(f"[STRIPE] Webhook processed: {event_id}, result: {result}")

            # Send iMessage notification on intro fee payment completion
            if result.get("action") == "intro_payment_completed":
                phone_number = result.get("phone_number")
                if phone_number:
                    try:
                        from app.integrations.photon_client import PhotonClient
                        photon = PhotonClient(
                            server_url=settings.photon_server_url,
                            default_number=settings.photon_default_number,
                            api_key=settings.photon_api_key,
                        )
                        await photon.send_message(
                            to_number=phone_number,
                            content="payment received, you're all set! text me whenever you want to make a connection",
                        )
                        logger.info(f"[STRIPE] Payment confirmation iMessage sent to {phone_number}")
                    except Exception as msg_error:
                        logger.error(f"[STRIPE] Failed to send payment confirmation iMessage: {msg_error}")

            return {"status": "success", "event_id": event_id, "result": result}

        except ValueError as sig_error:
            logger.error(f"[STRIPE] Invalid signature during processing: {sig_error}")
            raise HTTPException(status_code=400, detail="Invalid signature")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[STRIPE] Webhook error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/payment/success")
async def payment_success(session_id: str = None):
    logger.info(f"[STRIPE] Payment success redirect - session_id: {session_id}")
    try:
        if not session_id:
            return {"status": "success", "message": "Payment completed successfully! Return to iMessage to continue."}

        stripe_client = StripeClient()
        import stripe

        stripe.api_key = settings.stripe_api_key
        session = stripe.checkout.Session.retrieve(session_id)

        if session.payment_status == "paid":
            user_id = session.metadata.get("user_id")
            tier = session.metadata.get("tier", "premium")
            return {
                "status": "success",
                "message": f"Payment confirmed! You're now on {tier.upper()} tier.",
                "details": {
                    "session_id": session_id,
                    "user_id": user_id,
                    "tier": tier,
                    "amount_paid": session.amount_total / 100,
                    "currency": session.currency,
                },
                "next_step": "Return to iMessage to continue.",
            }
        else:
            return {
                "status": "pending",
                "message": "Payment is still processing. Please wait a moment and refresh.",
                "payment_status": session.payment_status,
            }
    except Exception as e:
        logger.error(f"[STRIPE] Error verifying payment: {e}", exc_info=True)
        return {"status": "success", "message": "Payment completed! Please return to your iMessage conversation."}


@app.get("/payment/cancel")
async def payment_cancel():
    return {
        "status": "canceled",
        "message": "Payment was canceled. No charges were made.",
        "options": {
            "continue_free": "You can continue using the FREE tier with limited features.",
            "retry_payment": "Return to your iMessage conversation to try upgrading again.",
            "contact_support": "Need help? Reply 'help' in iMessage.",
        },
        "next_step": "Please return to your iMessage conversation to continue.",
    }


@app.on_event("startup")
async def startup_event():
    logger.info("Starting Frank API...")
    import asyncio

    async def _init_services():
        try:
            # Start Photon Socket.IO listener for inbound messages
            global photon_listener
            photon_listener = PhotonListener(
                server_url=settings.photon_server_url,
                default_number=settings.photon_default_number,
                api_key=settings.photon_api_key,
                message_callback=_forward_photon_message,
            )
            await photon_listener.start()
            logger.info("[PHOTON] Listener initialized and connected")
        except Exception as e:
            logger.warning(f"Startup background init failed: {e}")

    async def _init_async_processor():
        try:
            # Start async operation processor for long-running tasks
            global async_processor
            async_processor = AsyncOperationProcessor()
            # Register handlers for group chat creation, multi-match invitations, etc.
            register_all_handlers(async_processor)
            asyncio.create_task(async_processor.start_processing())
            logger.info("[ASYNC_QUEUE] Operation processor started")
        except Exception as e:
            logger.warning(f"Async processor init failed: {e}")

    async def _init_profile_synthesis_scheduler():
        """Run profile synthesis job periodically."""
        from app.jobs.user_profile_synthesis import run_profile_synthesis_job

        if not getattr(settings, "profile_synthesis_enabled", True):
            logger.info("[PROFILE_SYNTHESIS] Job disabled via settings")
            return

        # Run initial job after 60 second delay to allow other services to start
        await asyncio.sleep(60)

        while True:
            try:
                logger.info("[PROFILE_SYNTHESIS] Starting scheduled job run")
                # Add timeout to prevent hanging jobs (1 hour max)
                stats = await asyncio.wait_for(
                    run_profile_synthesis_job(
                        batch_size=getattr(settings, "profile_synthesis_batch_size", 50),
                        stale_days=getattr(settings, "profile_synthesis_stale_days", 7),
                        rate_limit_seconds=getattr(settings, "profile_synthesis_rate_limit", 2.0),
                    ),
                    timeout=3600,
                )
                logger.info(f"[PROFILE_SYNTHESIS] Job completed: {stats}")
            except asyncio.TimeoutError:
                logger.error("[PROFILE_SYNTHESIS] Job timed out after 1 hour")
            except asyncio.CancelledError:
                logger.info("[PROFILE_SYNTHESIS] Job cancelled, shutting down")
                break
            except Exception as e:
                logger.error(f"[PROFILE_SYNTHESIS] Job failed: {e}", exc_info=True)

            # Run every 6 hours
            await asyncio.sleep(6 * 60 * 60)

    asyncio.create_task(_init_services())
    asyncio.create_task(_init_async_processor())
    asyncio.create_task(_init_profile_synthesis_scheduler())

    logger.info("Frank API started successfully")


@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Shutting down Frank API...")
    try:
        if photon_listener:
            await photon_listener.stop()
            logger.info("[PHOTON] Listener stopped")
    except Exception as e:
        logger.warning(f"[PHOTON] Failed to stop listener: {e}")

    try:
        if async_processor:
            await async_processor.stop_processing()
            logger.info("[ASYNC_QUEUE] Operation processor stopped")
    except Exception as e:
        logger.warning(f"[ASYNC_QUEUE] Failed to stop processor: {e}")

    logger.info("Frank API shut down successfully")


async def _forward_photon_message(payload: Dict[str, Any]) -> None:
    """
    Bridge PhotonListener inbound payloads into orchestrator.handle_message.
    """
    from types import SimpleNamespace

    # PhotonListener builds a dict compatible with our webhook model
    webhook_obj = SimpleNamespace(**payload)
    await orchestrator.handle_message(webhook_obj)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.debug,
        log_level=str(getattr(settings, "app_log_level", "") or "").strip().lower()
        or ("debug" if settings.debug else "info"),
    )
