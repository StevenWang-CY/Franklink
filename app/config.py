"""Configuration settings for Frank application."""

from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings

try:
    from dotenv import load_dotenv  # type: ignore
except Exception:  # pragma: no cover
    load_dotenv = None

if load_dotenv is not None:
    _env_path = Path(__file__).resolve().parents[1] / ".env"
    load_dotenv(_env_path, override=False)


class Settings(BaseSettings):
    """Application settings."""

    # Photon Configuration
    photon_server_url: str
    photon_default_number: str  # must be provided via environment
    photon_enable_listener: bool = True
    photon_api_key: Optional[str] = None

    # Azure OpenAI Configuration
    azure_openai_api_key: str
    azure_openai_endpoint: str
    azure_openai_api_version: str = "2025-01-01-preview"
    azure_openai_deployment_name: str
    azure_openai_reasoning_deployment_name: str
    azure_openai_embedding_deployment: str = "text-embedding-3-small"

    # Supabase Configuration
    supabase_url: str
    supabase_key: str
    supabase_service_key: Optional[str] = None

    # Franklink Resources Database
    resources_supabase_url: str
    resources_supabase_key: str
    resources_supabase_service_key: Optional[str] = None
    resources_news_table: str = "google_news_articles"

    # Group chat icebreaker (post-intro)
    icebreaker_enabled: bool = True
    icebreaker_poll_options: int = 4
    icebreaker_poll_backup_text_enabled: bool = False

    # Group chat summarization (background job)
    groupchat_summary_enabled: bool = True
    groupchat_summary_inactivity_minutes: int = 120
    groupchat_summary_model: str = "gpt-4o-mini"
    groupchat_summary_worker_max_jobs: int = 5
    groupchat_summary_worker_stale_minutes: int = 20

    # Group chat behavior
    groupchat_icebreaker_followup_opinion_enabled: bool = False

    # Group chat inactivity follow-up (background job)
    groupchat_followup_enabled: bool = True
    groupchat_followup_inactivity_minutes: int = 10080
    groupchat_followup_summary_window_days: int = 14
    groupchat_followup_model: str = "gpt-4o-mini"
    groupchat_followup_worker_max_jobs: int = 5
    groupchat_followup_worker_stale_minutes: int = 20
    groupchat_followup_poll_seconds: int = 10
    groupchat_followup_worker_max_attempts: int = 6

    # Tapback reactions (Photon)
    reactions_enabled: bool = True
    reactions_llm_enabled: bool = True
    reactions_model: str = "gpt-4o-mini"

    # Redis Configuration
    redis_url: str = "redis://localhost:6379/0"
    redis_max_connections: int = 50
    redis_idempotency_ttl: int = 86400
    redis_cache_ttl: int = 300
    redis_rate_limit_window: int = 60

    # FastAPI Configuration
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    app_env: str = "development"
    debug: bool = True
    app_log_level: str = "INFO"
    diagnostics_token: Optional[str] = None

    # CORS
    cors_allowed_origins: str = "*"

    # Sentry
    sentry_dsn: Optional[str] = None

    # Rate Limiting
    rate_limit_per_minute: int = 60
    rate_limit_per_hour: int = 1000

    # Proactive Daily Email Worker
    daily_email_worker_enabled: bool = True

    # Proactive Outreach Worker
    proactive_outreach_worker_enabled: bool = True

    # Location Update Worker
    location_update_worker_enabled: bool = True

    # Zep Memory
    zep_api_key: Optional[str] = None
    zep_base_url: str = "https://api.getzep.com"
    zep_enabled: bool = True

    # Zep Graph (knowledge graph for email context)
    zep_graph_enabled: bool = True
    zep_graph_chunk_size: int = 9000  # Max chars per graph.add call (limit is 10k)
    zep_graph_sync_emails: bool = True  # Sync emails to user's graph
    zep_graph_sync_signals: bool = True  # Sync networking signals to graph
    zep_graph_fallback_to_supabase: bool = True  # Fallback to Supabase on Zep failure
    zep_graph_enrich_candidates: bool = True  # Enrich match candidates with Zep facts
    zep_graph_max_facts_per_candidate: int = 3  # Max Zep facts per candidate

    # User Profile Synthesis (holistic user understanding)
    profile_synthesis_enabled: bool = True  # Enable profile synthesis job
    profile_synthesis_stale_days: int = 7  # Resynthesize profiles older than this
    profile_synthesis_min_facts: int = 3  # Min Zep facts required for synthesis
    profile_synthesis_batch_size: int = 50  # Max users per job run
    profile_synthesis_rate_limit: float = 2.0  # Seconds between users (API rate limit)
    profile_synthesis_model: str = "gpt-4o"  # Model for synthesis (use quality model)
    profile_synthesis_use_in_matching: bool = True  # Use holistic profiles in matching

    # Composio (email context)
    composio_api_key: Optional[str] = None
    composio_base_url: Optional[str] = None
    composio_entity_prefix: str = "franklink"
    composio_gmail_provider: str = "gmail"
    composio_auth_config_id: Optional[str] = None
    composio_gmail_toolkit_version: Optional[str] = None
    composio_callback_url: Optional[str] = None

    # Login page URL for wrapping OAuth links (shows franklink.ai/login instead of raw Composio URL)
    login_page_url: Optional[str] = None  # e.g., "https://franklink.ai/login"

    # Email context signals (derived from inbox)
    email_context_query: str = "newer_than:90d"
    email_context_max_threads: int = 30
    email_context_max_evidence: int = 100  # 50 received + 50 sent emails
    email_context_refresh_days: int = 14

    # Stripe Payment
    stripe_api_key: Optional[str] = None
    stripe_webhook_secret: Optional[str] = None
    stripe_success_url: str = "http://localhost:8000/payment/success"
    stripe_cancel_url: str = "http://localhost:8000/payment/cancel"

    # Legal
    privacy_policy_url: str = "https://franklink.ai/privacy"
    terms_of_service_url: str = "https://franklink.ai/terms"
    data_deletion_url: str = "https://franklink.ai/data-deletion"

    @property
    def cors_origins_list(self):
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]


settings = Settings()  # type: ignore
