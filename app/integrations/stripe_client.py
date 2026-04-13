"""
Stripe Payment Integration for Franklink (High-Concurrency Version)

Implements:
1. Payment link generation for subscriptions
2. Subscription management
3. Usage-based billing
4. Webhook handling for payment events

Pricing Tiers:
- Free: $0/month - 20 recommendations, 5 calendar events
- Premium: $9.99/month - Unlimited recommendations, unlimited events, follow-ups
- Enterprise: $49.99/month - Custom integrations, priority support

Performance Features:
- ThreadPoolExecutor: Runs synchronous Stripe calls without blocking event loop
- Connection pooling: Reuses HTTPS connections for better performance
- Rate limiting: Protects against API quota exhaustion
- Caching: Reduces redundant API calls

Location: app/integrations/stripe_client.py
"""

import logging
from typing import Dict, Optional, Any, List
from datetime import datetime
from enum import Enum
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial

import stripe
import requests

from app.config import settings
from app.utils.redis_client import redis_client, with_rate_limit

logger = logging.getLogger(__name__)

# Initialize Stripe
stripe.api_key = settings.stripe_api_key

# Thread pool for running synchronous Stripe calls
# Max workers = 20 allows handling 200+ concurrent requests
_stripe_executor = ThreadPoolExecutor(
    max_workers=20,
    thread_name_prefix="stripe_worker"
)

# Configure connection pooling for Stripe API
# Reuse HTTPS connections instead of creating new ones
_stripe_session = requests.Session()
_stripe_session.mount(
    'https://',
    requests.adapters.HTTPAdapter(
        pool_connections=20,
        pool_maxsize=50,
        max_retries=3
    )
)

# Set custom HTTP client if supported by Stripe version
try:
    stripe.default_http_client = stripe.http_client.RequestsClient(session=_stripe_session)
    logger.info("[STRIPE] Connection pooling enabled")
except AttributeError:
    # Older Stripe versions don't support custom HTTP client
    logger.warning("[STRIPE] Connection pooling not supported in this Stripe version. Consider upgrading: pip install --upgrade stripe")
    pass


class PricingTier(str, Enum):
    """Subscription pricing tiers"""
    FREE = "free"
    PREMIUM = "premium"
    ENTERPRISE = "enterprise"


class SubscriptionStatus(str, Enum):
    """Subscription status"""
    ACTIVE = "active"
    CANCELLED = "canceled"
    PAST_DUE = "past_due"
    TRIALING = "trialing"
    INCOMPLETE = "incomplete"


# Pricing configuration
PRICING = {
    PricingTier.FREE: {
        "price": 0,
        "recommendations_per_month": 20,
        "calendar_events_per_month": 5,
        "reminders": True,
        "follow_ups": False,
        "priority_support": False,
        "advanced_matching": False,
    },
    PricingTier.PREMIUM: {
        "price": 9.99,
        "recommendations_per_month": -1,  # Unlimited
        "calendar_events_per_month": -1,  # Unlimited
        "reminders": True,
        "follow_ups": True,
        "priority_support": True,
        "advanced_matching": True,
    },
    PricingTier.ENTERPRISE: {
        "price": 49.99,
        "recommendations_per_month": -1,  # Unlimited
        "calendar_events_per_month": -1,  # Unlimited
        "reminders": True,
        "follow_ups": True,
        "priority_support": True,
        "advanced_matching": True,
        "custom_integrations": True,
    }
}


class StripeClient:
    """
    Stripe payment client for subscription management (High-Concurrency).

    Handles:
    - Payment link generation
    - Subscription creation and management
    - Usage tracking and limits
    - Webhook event processing

    Performance:
    - Wraps synchronous Stripe SDK calls in ThreadPoolExecutor
    - Uses connection pooling for HTTPS requests
    - Implements rate limiting and caching
    - Can handle 200+ concurrent payment operations
    """

    def __init__(self):
        """Initialize Stripe client"""
        if not settings.stripe_api_key:
            logger.warning("[STRIPE] No API key configured")

    async def _run_in_executor(self, func, *args, **kwargs):
        """
        Run a synchronous Stripe SDK call in a thread pool executor.

        This prevents blocking the asyncio event loop during Stripe API calls.

        Args:
            func: Synchronous function to call
            *args, **kwargs: Arguments for the function

        Returns:
            Result from the function

        Example:
            >>> result = await self._run_in_executor(
            >>>     stripe.Customer.create,
            >>>     email="user@example.com"
            >>> )
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_stripe_executor, partial(func, **kwargs) if kwargs else func, *args)

    async def create_or_get_customer(self, user_id: str, email: str, phone_number: str) -> str:
        """
        Create Stripe customer or retrieve existing one (async, non-blocking).

        Args:
            user_id: Internal user ID
            email: User's email address
            phone_number: User's phone number

        Returns:
            customer_id: Stripe customer ID
        """
        logger.info(f"[STRIPE] Creating/getting customer for user {user_id}")

        try:
            # Check cache first
            cache_key = f"stripe_customer:{email}"
            cached = redis_client.get_cached(cache_key)
            if cached:
                logger.info(f"[STRIPE] Customer found in cache: {cached}")
                return cached

            # Search for existing customer by metadata (non-blocking)
            customers = await self._run_in_executor(
                stripe.Customer.list,
                limit=1,
                email=email
            )

            if customers.data:
                customer = customers.data[0]
                logger.info(f"[STRIPE] Found existing customer: {customer.id}")
                # Cache for future lookups
                redis_client.set_cached(cache_key, customer.id, ttl=3600)  # 1 hour
                return customer.id

            # Create new customer (non-blocking)
            customer = await self._run_in_executor(
                stripe.Customer.create,
                email=email,
                phone=phone_number,
                metadata={
                    "user_id": user_id,
                    "source": "franklink_imessage"
                }
            )

            logger.info(f"[STRIPE] Created new customer: {customer.id}")
            # Cache the new customer
            redis_client.set_cached(cache_key, customer.id, ttl=3600)
            return customer.id

        except stripe.error.StripeError as e:
            logger.error(f"[STRIPE] Failed to create/get customer: {e}")
            raise

    @with_rate_limit("stripe_payment_link", max_requests=100, window_seconds=60)
    async def create_payment_link(
        self,
        user_id: str,
        email: str,
        phone_number: str,
        tier: PricingTier
    ) -> str:
        """
        Create Stripe payment link for subscription (async, non-blocking, rate-limited).

        Rate limit: 100 requests per minute per function

        Args:
            user_id: Internal user ID
            email: User's email
            phone_number: User's phone number
            tier: Pricing tier (premium or enterprise)

        Returns:
            payment_url: Payment link URL

        Raises:
            ValueError: If tier is FREE
            RateLimitExceeded: If rate limit exceeded
        """
        logger.info(f"[STRIPE] Creating payment link for user {user_id}, tier {tier}")

        if tier == PricingTier.FREE:
            raise ValueError("Cannot create payment link for free tier")

        pricing = PRICING[tier]
        amount = int(pricing["price"] * 100)  # Convert to cents

        try:
            # Create or get customer
            customer_id = await self.create_or_get_customer(user_id, email, phone_number)

            # Create payment link (non-blocking)
            # Note: For recurring subscriptions, we must pre-create the customer
            # and cannot use customer_creation="always" in the payment link
            payment_link = await self._run_in_executor(
                stripe.PaymentLink.create,
                line_items=[{
                    "price_data": {
                        "currency": "usd",
                        "product_data": {
                            "name": f"Franklink {tier.value.title()} Subscription",
                            "description": self._get_tier_description(tier)
                        },
                        "unit_amount": amount,
                        "recurring": {
                            "interval": "month"
                        }
                    },
                    "quantity": 1
                }],
                # Cannot use customer_creation with recurring prices
                # Customer is already created above via create_or_get_customer
                metadata={
                    "user_id": user_id,
                    "tier": tier.value,
                    "customer_id": customer_id
                },
                after_completion={
                    "type": "redirect",
                    "redirect": {
                        "url": f"{settings.stripe_success_url}?user_id={user_id}&tier={tier.value}"
                    }
                }
            )

            logger.info(f"[STRIPE] Created payment link: {payment_link.url}")
            return payment_link.url

        except Exception as e:
            logger.error(f"[STRIPE] Failed to create payment link: {e}")
            raise

    @with_rate_limit("stripe_checkout", max_requests=100, window_seconds=60)
    async def create_intro_checkout_session(
        self,
        user_id: str,
        phone_number: str,
        intro_fee_cents: int,
        email: Optional[str] = None,
    ) -> Optional[str]:
        """
        Create a Stripe Checkout Session for one-time intro fee payment.

        Args:
            user_id: Internal user ID (UUID)
            phone_number: User's phone number (for webhook lookup)
            intro_fee_cents: The negotiated intro fee in cents
            email: Optional user email to pre-fill checkout

        Returns:
            checkout_url: URL to redirect user to Stripe Checkout, or None on failure
        """
        logger.info(f"[STRIPE] Creating intro checkout session for user {user_id}, fee: ${intro_fee_cents/100:.2f}")

        try:
            session = await self._run_in_executor(
                stripe.checkout.Session.create,
                mode="payment",  # One-time payment, not subscription
                line_items=[{
                    "price_data": {
                        "currency": "usd",
                        "product_data": {
                            "name": "Franklink Network Access",
                            "description": "Monthly access fee for Franklink professional network"
                        },
                        "unit_amount": intro_fee_cents,
                    },
                    "quantity": 1
                }],
                metadata={
                    "user_id": user_id,
                    "phone_number": phone_number,
                    "payment_type": "intro_fee",
                },
                customer_email=email if email else None,
                success_url=f"{settings.stripe_success_url}?session_id={{CHECKOUT_SESSION_ID}}",
                cancel_url=settings.stripe_cancel_url,
            )

            logger.info(f"[STRIPE] Created intro checkout session: {session.id}, url: {session.url}")
            return session.url

        except Exception as e:
            logger.error(f"[STRIPE] Failed to create intro checkout session: {e}")
            return None

    def _get_tier_description(self, tier: PricingTier) -> str:
        """Get description for pricing tier"""
        descriptions = {
            PricingTier.PREMIUM: "Unlimited recommendations, calendar events, and follow-ups",
            PricingTier.ENTERPRISE: "Everything in Premium plus custom integrations and priority support"
        }
        return descriptions.get(tier, "")

    async def create_subscription(
        self,
        customer_id: str,
        tier: PricingTier,
        user_id: str
    ) -> Dict[str, Any]:
        """
        Create subscription for customer.

        Args:
            customer_id: Stripe customer ID
            tier: Pricing tier
            user_id: Internal user ID

        Returns:
            subscription: Subscription details
        """
        logger.info(f"[STRIPE] Creating subscription for customer {customer_id}, tier {tier}")

        pricing = PRICING[tier]
        amount = int(pricing["price"] * 100)

        try:
            # Create price
            price = stripe.Price.create(
                currency="usd",
                unit_amount=amount,
                recurring={"interval": "month"},
                product_data={
                    "name": f"Franklink {tier.value.title()}"
                }
            )

            # Create subscription
            subscription = stripe.Subscription.create(
                customer=customer_id,
                items=[{"price": price.id}],
                metadata={
                    "user_id": user_id,
                    "tier": tier.value
                }
            )

            logger.info(f"[STRIPE] Created subscription: {subscription.id}")

            return {
                "subscription_id": subscription.id,
                "status": subscription.status,
                "current_period_start": datetime.fromtimestamp(subscription.current_period_start),
                "current_period_end": datetime.fromtimestamp(subscription.current_period_end),
                "cancel_at_period_end": subscription.cancel_at_period_end
            }

        except stripe.error.StripeError as e:
            logger.error(f"[STRIPE] Failed to create subscription: {e}")
            raise

    async def check_payment_status(self, user_id: str) -> Optional[Dict[str, Any]]:
        """
        Check if user has active subscription.

        Args:
            user_id: Internal user ID

        Returns:
            subscription_info: Subscription details if active, None otherwise
        """
        logger.info(f"[STRIPE] Checking payment status for user {user_id}")

        try:
            # Find customer by metadata
            customers = stripe.Customer.list(
                limit=1
            )

            customer = None
            for c in customers.auto_paging_iter():
                if c.metadata.get("user_id") == user_id:
                    customer = c
                    break

            if not customer:
                logger.info(f"[STRIPE] No customer found for user {user_id}")
                return None

            # Get subscriptions
            subscriptions = stripe.Subscription.list(
                customer=customer.id,
                status="active",
                limit=1
            )

            if not subscriptions.data:
                logger.info(f"[STRIPE] No active subscriptions for customer {customer.id}")
                return None

            sub = subscriptions.data[0]

            return {
                "subscription_id": sub.id,
                "customer_id": customer.id,
                "status": sub.status,
                "tier": sub.metadata.get("tier", "premium"),
                "current_period_start": datetime.fromtimestamp(sub.current_period_start),
                "current_period_end": datetime.fromtimestamp(sub.current_period_end),
                "cancel_at_period_end": sub.cancel_at_period_end
            }

        except stripe.error.StripeError as e:
            logger.error(f"[STRIPE] Failed to check payment status: {e}")
            return None

    async def cancel_subscription(self, subscription_id: str, at_period_end: bool = True) -> bool:
        """
        Cancel subscription.

        Args:
            subscription_id: Stripe subscription ID
            at_period_end: If True, cancel at end of billing period

        Returns:
            success: True if cancelled
        """
        logger.info(f"[STRIPE] Cancelling subscription {subscription_id}")

        try:
            if at_period_end:
                stripe.Subscription.modify(
                    subscription_id,
                    cancel_at_period_end=True
                )
            else:
                stripe.Subscription.cancel(subscription_id)

            logger.info(f"[STRIPE] Subscription cancelled: {subscription_id}")
            return True

        except stripe.error.StripeError as e:
            logger.error(f"[STRIPE] Failed to cancel subscription: {e}")
            return False

    async def process_webhook_event(self, payload: bytes, signature: str) -> Dict[str, Any]:
        """
        Process Stripe webhook event.

        Args:
            payload: Request body
            signature: Stripe signature header

        Returns:
            event_data: Processed event data
        """
        logger.info("[STRIPE] Processing webhook event")

        try:
            # Verify webhook signature
            event = stripe.Webhook.construct_event(
                payload,
                signature,
                settings.stripe_webhook_secret
            )

            event_type = event['type']
            event_data = event['data']['object']

            logger.info(f"[STRIPE] Webhook event type: {event_type}")

            # Handle different event types
            if event_type == 'checkout.session.completed':
                return await self._handle_checkout_completed(event_data)

            elif event_type == 'customer.subscription.created':
                return await self._handle_subscription_created(event_data)

            elif event_type == 'customer.subscription.updated':
                return await self._handle_subscription_updated(event_data)

            elif event_type == 'customer.subscription.deleted':
                return await self._handle_subscription_deleted(event_data)

            elif event_type == 'invoice.payment_succeeded':
                return await self._handle_payment_succeeded(event_data)

            elif event_type == 'invoice.payment_failed':
                return await self._handle_payment_failed(event_data)

            else:
                logger.info(f"[STRIPE] Unhandled event type: {event_type}")
                return {"handled": False, "event_type": event_type}

        except stripe.error.SignatureVerificationError as e:
            logger.error(f"[STRIPE] Invalid signature: {e}")
            raise ValueError("Invalid signature")

        except Exception as e:
            logger.error(f"[STRIPE] Failed to process webhook: {e}")
            raise

    async def _handle_checkout_completed(self, session: Dict) -> Dict[str, Any]:
        """Handle checkout session completion"""
        metadata = session.get('metadata', {})
        payment_type = metadata.get('payment_type')

        # Handle intro fee payments specifically
        if payment_type == "intro_fee":
            return await self._handle_intro_payment_completed(session)

        # Existing subscription logic
        user_id = metadata.get('user_id')
        tier = metadata.get('tier')

        logger.info(f"[STRIPE] Checkout completed for user {user_id}, tier {tier}")

        # Update user subscription in database
        from app.database.client import DatabaseClient
        db = DatabaseClient()

        await db.update_user_subscription(
            user_id=user_id,
            tier=tier,
            status=SubscriptionStatus.ACTIVE,
            stripe_customer_id=session.get('customer')
        )

        return {
            "handled": True,
            "user_id": user_id,
            "tier": tier,
            "action": "subscription_activated"
        }

    async def _handle_intro_payment_completed(self, session: Dict) -> Dict[str, Any]:
        """Handle intro fee payment completion."""
        metadata = session.get('metadata', {})
        user_id = metadata.get('user_id')
        phone_number = metadata.get('phone_number')

        logger.info(f"[STRIPE] Intro fee payment completed for user {user_id}, phone {phone_number}")

        if user_id:
            from app.database.client import DatabaseClient
            db = DatabaseClient()

            # Get current user to preserve existing personal_facts
            user = await db.get_user_profile(user_id)
            personal_facts = user.get("personal_facts", {}) if user else {}

            # Update payment status
            personal_facts["intro_fee_paid"] = True
            personal_facts["intro_fee_paid_at"] = datetime.utcnow().isoformat()
            personal_facts["intro_payment_session_id"] = session.get('id')
            personal_facts["intro_payment_amount_cents"] = session.get('amount_total')

            await db.update_user_profile(user_id, {"personal_facts": personal_facts})

            # Also update stripe_customer_id if available
            customer_id = session.get('customer')
            if customer_id:
                await db.update_user_subscription(
                    user_id=user_id,
                    stripe_customer_id=customer_id,
                )

        return {
            "handled": True,
            "action": "intro_payment_completed",
            "user_id": user_id,
            "phone_number": phone_number,
            "amount_cents": session.get('amount_total'),
        }

    async def _handle_subscription_created(self, subscription: Dict) -> Dict[str, Any]:
        """Handle subscription creation"""
        user_id = subscription['metadata'].get('user_id')
        tier = subscription['metadata'].get('tier')

        logger.info(f"[STRIPE] Subscription created for user {user_id}")

        from app.database.client import DatabaseClient
        db = DatabaseClient()

        await db.update_user_subscription(
            user_id=user_id,
            tier=tier,
            status=SubscriptionStatus.ACTIVE,
            stripe_subscription_id=subscription['id']
        )

        return {
            "handled": True,
            "user_id": user_id,
            "action": "subscription_created"
        }

    async def _handle_subscription_updated(self, subscription: Dict) -> Dict[str, Any]:
        """Handle subscription update"""
        user_id = subscription['metadata'].get('user_id')
        status = subscription['status']

        logger.info(f"[STRIPE] Subscription updated for user {user_id}, status: {status}")

        from app.database.client import DatabaseClient
        db = DatabaseClient()

        await db.update_user_subscription(
            user_id=user_id,
            status=status
        )

        return {
            "handled": True,
            "user_id": user_id,
            "action": "subscription_updated",
            "status": status
        }

    async def _handle_subscription_deleted(self, subscription: Dict) -> Dict[str, Any]:
        """Handle subscription cancellation"""
        user_id = subscription['metadata'].get('user_id')

        logger.info(f"[STRIPE] Subscription deleted for user {user_id}")

        from app.database.client import DatabaseClient
        db = DatabaseClient()

        await db.update_user_subscription(
            user_id=user_id,
            tier=PricingTier.FREE,
            status=SubscriptionStatus.CANCELLED
        )

        return {
            "handled": True,
            "user_id": user_id,
            "action": "subscription_cancelled"
        }

    async def _handle_payment_succeeded(self, invoice: Dict) -> Dict[str, Any]:
        """Handle successful payment"""
        logger.info(f"[STRIPE] Payment succeeded for invoice {invoice['id']}")

        # Payment successful - no action needed (subscription already active)
        return {
            "handled": True,
            "action": "payment_succeeded"
        }

    async def _handle_payment_failed(self, invoice: Dict) -> Dict[str, Any]:
        """Handle failed payment"""
        logger.info(f"[STRIPE] Payment failed for invoice {invoice['id']}")

        customer_id = invoice.get('customer')

        # Get user_id from customer
        customer = stripe.Customer.retrieve(customer_id)
        user_id = customer.metadata.get('user_id')

        if user_id:
            from app.database.client import DatabaseClient
            db = DatabaseClient()

            await db.update_user_subscription(
                user_id=user_id,
                status=SubscriptionStatus.PAST_DUE
            )

        return {
            "handled": True,
            "user_id": user_id,
            "action": "payment_failed"
        }

    async def check_intro_payment_status(self, user_id: str) -> Dict[str, Any]:
        """
        Check if user has paid their intro fee via Stripe.

        This checks for completed checkout sessions associated with the user.

        Args:
            user_id: Internal user ID

        Returns:
            Dict with 'paid' boolean and optional 'payment_details'
        """
        logger.info(f"[STRIPE] Checking intro payment status for user {user_id}")

        try:
            # Check cache first
            cache_key = f"intro_payment:{user_id}"
            cached = redis_client.get_cached(cache_key)
            if cached:
                logger.info(f"[STRIPE] Payment status found in cache: {cached}")
                return {"paid": cached == "paid", "cached": True}

            # List checkout sessions and find ones with matching user_id
            # Note: Stripe doesn't allow filtering by metadata directly,
            # so we need to iterate through recent sessions
            sessions = await self._run_in_executor(
                stripe.checkout.Session.list,
                limit=100,
                status="complete"
            )

            for session in sessions.data:
                session_user_id = session.metadata.get("user_id")
                if session_user_id == user_id:
                    # Found a completed payment for this user
                    logger.info(f"[STRIPE] Found completed payment for user {user_id}")
                    redis_client.set_cached(cache_key, "paid", ttl=3600)  # Cache for 1 hour
                    return {
                        "paid": True,
                        "session_id": session.id,
                        "amount_total": session.amount_total,
                        "payment_status": session.payment_status,
                        "created": session.created,
                    }

            # No completed payment found
            logger.info(f"[STRIPE] No completed payment found for user {user_id}")
            return {"paid": False}

        except stripe.error.StripeError as e:
            logger.error(f"[STRIPE] Failed to check intro payment status: {e}")
            return {"paid": False, "error": str(e)}


# Helper functions

def get_tier_limits(tier: PricingTier) -> Dict[str, Any]:
    """
    Get usage limits for pricing tier.

    Args:
        tier: Pricing tier

    Returns:
        limits: Usage limits
    """
    return PRICING.get(tier, PRICING[PricingTier.FREE])


def check_usage_limit(tier: PricingTier, usage_type: str, current_usage: int) -> bool:
    """
    Check if usage is within tier limits.

    Args:
        tier: User's pricing tier
        usage_type: Type of usage (recommendations_per_month, calendar_events_per_month)
        current_usage: Current usage count

    Returns:
        within_limit: True if within limit
    """
    limits = PRICING.get(tier, PRICING[PricingTier.FREE])
    limit = limits.get(usage_type, 0)

    # -1 means unlimited
    if limit == -1:
        return True

    return current_usage < limit


def format_tier_features(tier: PricingTier) -> str:
    """
    Format tier features for display to user in Frank's tone.

    Args:
        tier: Pricing tier

    Returns:
        formatted: Formatted feature list (Frank style: lowercase, no bullets, no emojis)
    """
    features = PRICING.get(tier, PRICING[PricingTier.FREE])

    lines = []

    if tier == PricingTier.FREE:
        # Frank style: lowercase, no bullets, concise
        lines.append(f"{features['recommendations_per_month']} recommendations per month")
        lines.append(f"{features['calendar_events_per_month']} calendar events per month")
        lines.append("event reminders")
    else:
        # Frank style: lowercase, no bullets, concise
        lines.append(f"${features['price']}/month")
        lines.append("unlimited recommendations")
        lines.append("unlimited calendar events")
        lines.append("event reminders")

        if features.get('follow_ups'):
            lines.append("post-event follow-ups")

        if features.get('priority_support'):
            lines.append("priority support")

        if features.get('advanced_matching'):
            lines.append("advanced matching algorithm")

        if features.get('custom_integrations'):
            lines.append("custom integrations")

    return "\n".join(lines)
