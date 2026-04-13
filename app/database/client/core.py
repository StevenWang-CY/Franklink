import logging

from supabase import create_client, Client

from app.config import settings

logger = logging.getLogger(__name__)


class _DatabaseClientCore:
    def __init__(self):
        """Initialize Supabase client with service key if available."""
        try:
            if settings.supabase_service_key and settings.supabase_service_key != "":
                self.client: Client = create_client(
                    settings.supabase_url,
                    settings.supabase_service_key
                )
                logger.info("Using service role key for database operations")
            else:
                self.client: Client = create_client(
                    settings.supabase_url,
                    settings.supabase_key
                )
                logger.info("Using anon key for database operations")
        except Exception as e:
            # Fall back to anon key if service key fails
            self.client: Client = create_client(
                settings.supabase_url,
                settings.supabase_key
            )
            logger.warning(f"Service key initialization failed, using anon key: {e}")
