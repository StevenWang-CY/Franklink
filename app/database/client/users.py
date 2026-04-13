"""Internal database client implementation (users)."""

import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional

from postgrest.exceptions import APIError

from app.database.models import User
from app.utils.demand_value_history import append_history, combine_texts, latest_text, normalize_history

logger = logging.getLogger(__name__)


class _UserMethods:
    async def get_or_create_user(self, phone_number: str) -> Dict[str, Any]:
        """
        Get existing user or create new one.
        """
        try:
            result = self.client.table("users").select("*").eq("phone_number", phone_number).execute()

            if result.data:
                logger.info(f"Found existing user for {phone_number}")
                return result.data[0]

            new_user = User(phone_number=phone_number)
            user_dict = json.loads(new_user.model_dump_json(exclude_none=True))
            result = self.client.table("users").insert(user_dict).execute()

            logger.info(f"Created new user for {phone_number}")
            return result.data[0]

        except APIError:
            raise
        except Exception as e:
            logger.error(f"Error getting/creating user: {str(e)}", exc_info=True)
            raise

    async def update_user_profile(self, user_id: str, profile_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Update user profile information.
        """
        try:
            ALLOWED_FIELDS = {
                "name",
                "email",
                "demand_history",
                "value_history",
                "latest_demand",
                "all_demand",
                "all_value",
                "intro_fee_cents",
                "university",
                "location",
                "major",
                "year",
                "career_interests",
                "career_goals",
                "needs",
                "is_onboarded",
                "linkedin_url",
                "linkedin_data",
                "grade_level",
                "linkedin_scrape_status",
                "linkedin_scraped_at",
                "personal_facts",
                "networking_clarification",
                "metadata",
                "onboarding_stage",
                "subscription_tier",
                "offer_status",
            }

            sanitized_data = {key: value for key, value in profile_data.items() if key in ALLOWED_FIELDS}

            if not sanitized_data:
                logger.warning(f"No valid fields to update in profile_data: {profile_data.keys()}")
                result = self.client.table("users").select("*").eq("id", user_id).execute()
                if result.data:
                    return result.data[0]
                raise ValueError("User not found")

            sanitized_data["updated_at"] = datetime.utcnow().isoformat()

            result = self.client.table("users").update(sanitized_data).eq("id", user_id).execute()

            logger.info(f"Updated profile for user {user_id} with fields: {list(sanitized_data.keys())}")
            return result.data[0]

        except Exception as e:
            logger.error(f"Error updating user profile: {str(e)}", exc_info=True)
            raise

    async def get_demand_value_state(self, user_id: str) -> Dict[str, Any]:
        """
        Fetch demand/value history and metadata for a user.
        """
        try:
            result = (
                self.client.table("users")
                .select("demand_history,value_history,metadata")
                .eq("id", user_id)
                .limit(1)
                .execute()
            )
            if not result.data:
                raise ValueError(f"User {user_id} not found")
            row = result.data[0] or {}
            return row if isinstance(row, dict) else {}
        except Exception as e:
            logger.error(f"Error fetching demand/value state: {str(e)}", exc_info=True)
            raise

    async def append_demand_value_history(
        self,
        user_id: str,
        *,
        demand_update: Optional[str] = None,
        value_update: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Append demand/value updates to history lists.
        """
        try:
            result = (
                self.client.table("users")
                .select("demand_history,value_history")
                .eq("id", user_id)
                .limit(1)
                .execute()
            )
            row = result.data[0] if result.data else {}
            demand_history = normalize_history(row.get("demand_history"))
            value_history = normalize_history(row.get("value_history"))

            demand_history = append_history(
                demand_history,
                demand_update,
                created_at=created_at,
            )
            value_history = append_history(
                value_history,
                value_update,
                created_at=created_at,
            )

            latest_demand = latest_text(demand_history)
            all_demand = combine_texts(demand_history).strip()
            all_value = combine_texts(value_history).strip()
            update_payload: Dict[str, Any] = {
                "demand_history": demand_history,
                "value_history": value_history,
                "latest_demand": latest_demand or None,
                "all_demand": all_demand or None,
                "all_value": all_value or None,
                "updated_at": datetime.utcnow().isoformat(),
            }

            update_result = (
                self.client.table("users")
                .update(update_payload)
                .eq("id", user_id)
                .execute()
            )

            if not update_result.data:
                raise ValueError(f"User {user_id} not found")
            return update_result.data[0]

        except Exception as e:
            logger.error(f"Error appending demand/value history: {str(e)}", exc_info=True)
            raise

    async def update_user_subscription(
        self,
        user_id: str,
        tier: Optional[str] = None,
        status: Optional[str] = None,
        stripe_customer_id: Optional[str] = None,
        stripe_subscription_id: Optional[str] = None,
        subscription_started_at: Optional[str] = None,
        subscription_ends_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Update user subscription information after Stripe payment.
        """
        try:
            subscription_data: Dict[str, Any] = {}

            if tier is not None:
                from app.integrations.stripe_client import PricingTier

                if isinstance(tier, PricingTier):
                    subscription_data["subscription_tier"] = tier.value
                else:
                    subscription_data["subscription_tier"] = tier

            if status is not None:
                from app.integrations.stripe_client import SubscriptionStatus

                if isinstance(status, SubscriptionStatus):
                    subscription_data["subscription_status"] = status.value
                else:
                    subscription_data["subscription_status"] = status

            if stripe_customer_id is not None:
                subscription_data["stripe_customer_id"] = stripe_customer_id
            if stripe_subscription_id is not None:
                subscription_data["stripe_subscription_id"] = stripe_subscription_id
            if subscription_started_at is not None:
                subscription_data["subscription_started_at"] = subscription_started_at
            if subscription_ends_at is not None:
                subscription_data["subscription_ends_at"] = subscription_ends_at

            ALLOWED_FIELDS = {
                "subscription_tier",
                "subscription_status",
                "stripe_customer_id",
                "stripe_subscription_id",
                "subscription_started_at",
                "subscription_ends_at",
            }

            VALID_TIERS = {"free", "premium", "enterprise"}
            if "subscription_tier" in subscription_data:
                tier_value = subscription_data["subscription_tier"]
                if tier_value not in VALID_TIERS:
                    raise ValueError(f"Invalid subscription_tier: {tier_value}. Must be one of {VALID_TIERS}")

            VALID_STATUSES = {"active", "canceled", "past_due", "trialing", "incomplete"}
            if "subscription_status" in subscription_data:
                status_value = subscription_data["subscription_status"]
                if status_value not in VALID_STATUSES:
                    raise ValueError(
                        f"Invalid subscription_status: {status_value}. Must be one of {VALID_STATUSES}"
                    )

            sanitized_data = {key: value for key, value in subscription_data.items() if key in ALLOWED_FIELDS}

            if not sanitized_data:
                logger.warning(f"No valid subscription fields to update for user {user_id}")
                result = self.client.table("users").select("*").eq("id", user_id).execute()
                if result.data:
                    return result.data[0]
                raise ValueError("User not found")

            sanitized_data["updated_at"] = datetime.utcnow().isoformat()

            result = self.client.table("users").update(sanitized_data).eq("id", user_id).execute()

            if not result.data:
                raise ValueError(f"User {user_id} not found")

            logger.info(
                f"Updated subscription for user {user_id}: tier={subscription_data.get('subscription_tier')}, "
                f"status={subscription_data.get('subscription_status')}"
            )

            return result.data[0]

        except ValueError as ve:
            logger.error(f"Validation error updating subscription: {str(ve)}")
            raise
        except Exception as e:
            logger.error(f"Error updating user subscription: {str(e)}", exc_info=True)
            raise

    async def get_user_interests(self, user_id: str) -> list[str]:
        """
        Get user's career interests.
        """
        try:
            result = self.client.table("users").select("career_interests").eq("id", user_id).execute()
            if result.data and result.data[0].get("career_interests"):
                return result.data[0]["career_interests"]
            return []
        except Exception as e:
            logger.error(f"Error getting user interests: {str(e)}", exc_info=True)
            return []

    async def get_user_by_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        """
        Get user by UUID.
        """
        try:
            result = self.client.table("users").select("*").eq("id", user_id).execute()

            if result.data:
                logger.debug(f"Found user {user_id}")
                return result.data[0]

            logger.warning(f"User {user_id} not found")
            return None

        except APIError:
            raise
        except Exception as e:
            logger.error(f"Error getting user by ID: {str(e)}", exc_info=True)
            return None

