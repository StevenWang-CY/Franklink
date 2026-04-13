"""
Franklink Resources Database Client

Connects to the separate "Franklink Resources" Supabase project.
This database stores static resources in separate tables by type.

Database tables:
- amazon_books: Books from Amazon
- youtube_videos: Videos from YouTube
- eventbrite_events: Events from Eventbrite
- leetcode_problems: Coding problems from LeetCode
- linkedin_jobs: Jobs from LinkedIn
- reddit_insight: Insights from Reddit

Separation rationale:
- Static resources are shared across products
- Different access patterns (mostly read-only)
- Can scale independently
- Easier to maintain and update resource catalog
"""

import logging
from typing import List, Dict, Any, Optional
from supabase import create_client, Client
from app.config import settings

logger = logging.getLogger(__name__)


class ResourcesDatabaseClient:
    """
    Client for Franklink Resources database (separate Supabase project).

    This database contains separate tables for each content type:
    - amazon_books: Book resources
    - youtube_videos: Video resources
    - eventbrite_events: Event resources
    - leetcode_problems: Coding problem resources
    - linkedin_jobs: Job postings
    - reddit_insight: Reddit insights

    Each table has:
    - Embedding vectors for semantic search
    - RPC functions for vector similarity search

    Note: user_resource_interactions remains in the MAIN database
    """

    # Mapping from content_type to table name
    TABLE_MAPPING = {
        "book": "amazon_books",
        "video": "youtube_videos",
        "event": "eventbrite_events",
        "problem": "leetcode_problems",
        "job": "linkedin_jobs",
        "insight": "reddit_insight"
    }

    # All resource tables
    ALL_TABLES = list(TABLE_MAPPING.values())

    def __init__(self, use_service_key: bool = False):
        """
        Initialize connection to Resources database.

        Args:
            use_service_key: If True, uses service key (bypasses RLS, for admin operations).
                           If False, uses anon key (respects RLS, for read operations).
        """
        # Choose appropriate key
        if use_service_key and settings.resources_supabase_service_key:
            key = settings.resources_supabase_service_key
            logger.debug("Using Resources DB service key (admin access)")
        else:
            key = settings.resources_supabase_key
            logger.debug("Using Resources DB anon key (RLS-protected)")

        # Create Supabase client for Resources database
        self.client: Client = create_client(
            settings.resources_supabase_url,
            key
        )

        logger.info(f"Resources database client initialized (URL: {settings.resources_supabase_url[:30]}...)")

    async def execute_query(self, query: str, *params) -> Optional[List[Dict[str, Any]]]:
        """
        Raw SQL queries not supported on Resources database.

        The Supabase Python client doesn't support raw SQL with params directly.
        Use alternative methods instead:
        - execute_rpc() for RPC functions
        - list_resources() for listing resources
        - get_resource_by_id() for fetching single resources
        - search_static_resources() for vector search

        Args:
            query: SQL query string
            *params: Query parameters

        Raises:
            NotImplementedError: Always raised - raw queries not supported

        Example:
            Instead of execute_query("SELECT * FROM amazon_books WHERE id = $1", book_id),
            use get_resource_by_id(book_id)
        """
        raise NotImplementedError(
            "execute_query() not supported on Resources database. "
            "Use execute_rpc() for RPC functions, or table-specific methods like "
            "list_resources(), get_resource_by_id(), or search_static_resources()."
        )

    async def execute_rpc(self, function_name: str, **params) -> List[Dict[str, Any]]:
        """
        Call an RPC function on the resources database.

        Args:
            function_name: Name of the PostgreSQL function
            **params: Parameters to pass to the function

        Returns:
            List of result dictionaries
        """
        try:
            logger.debug(f"Calling RPC function: {function_name}")
            result = self.client.rpc(function_name, params).execute()

            if result.data:
                logger.debug(f"RPC {function_name} returned {len(result.data)} results")
                return result.data
            else:
                logger.debug(f"RPC {function_name} returned no results")
                return []

        except Exception as e:
            logger.error(f"RPC function {function_name} failed: {str(e)}")
            return []

    async def search_static_resources(
        self,
        query_embedding: List[float],
        resource_type: str,
        match_threshold: float = 0.5,
        match_count: int = 10,
        platform_filter: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Search for static resources using vector similarity.

        Calls the match_static_resources RPC function in the resources database.

        Args:
            query_embedding: Vector embedding (1536 dimensions)
            resource_type: Type of resource ('book', 'video', 'course', etc.)
            match_threshold: Minimum similarity threshold (0-1)
            match_count: Maximum number of results
            platform_filter: Optional platform filter (e.g., 'youtube', 'amazon')

        Returns:
            List of matching resources with similarity scores
        """
        return await self.execute_rpc(
            "match_static_resources",
            query_embedding=query_embedding,
            resource_type=resource_type,
            match_threshold=match_threshold,
            match_count=match_count,
            platform_filter=platform_filter
        )

    async def get_resources_without_embeddings(
        self,
        batch_size: int = 100,
        content_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Get resources that don't have embeddings yet.

        Used by the embedding generation script to find resources that need processing.

        Args:
            batch_size: Number of resources to return
            content_type: Optional filter by content type ('book', 'video', 'event', etc.)

        Returns:
            List of resources without embeddings
        """
        params = {"batch_size": batch_size}
        if content_type:
            params["filter_content_type"] = content_type

        return await self.execute_rpc(
            "get_static_resources_without_embeddings",
            **params
        )

    async def get_resource_by_id(self, resource_id: str) -> Optional[Dict[str, Any]]:
        """
        Get a single resource by ID.

        Since we don't know which table contains the resource,
        we search across all tables until we find it.

        Args:
            resource_id: UUID of the resource

        Returns:
            Resource dictionary, or None if not found
        """
        try:
            # Search each table until we find the resource
            for table_name in self.ALL_TABLES:
                try:
                    result = self.client.table(table_name)\
                        .select("*")\
                        .eq("id", resource_id)\
                        .single()\
                        .execute()

                    if result.data:
                        # Add content_type based on table
                        resource = result.data
                        resource["_source_table"] = table_name
                        # Reverse lookup content_type from table name
                        for content_type, tbl in self.TABLE_MAPPING.items():
                            if tbl == table_name:
                                resource["content_type"] = content_type
                                break
                        return resource
                except Exception:
                    # Resource not in this table, try next
                    continue

            logger.warning(f"Resource {resource_id} not found in any table")
            return None

        except Exception as e:
            logger.error(f"Failed to get resource {resource_id}: {str(e)}")
            return None

    async def update_resource_embedding(
        self,
        resource_id: str,
        embedding: List[float],
        table_name: Optional[str] = None
    ) -> bool:
        """
        Update the embedding for a resource.

        Note: Requires service key access.

        Args:
            resource_id: UUID of the resource
            embedding: Vector embedding (1536 dimensions)
            table_name: Optional table name (if known). If not provided, searches all tables.

        Returns:
            True if successful, False otherwise
        """
        try:
            # If table name provided, update directly
            if table_name:
                result = self.client.table(table_name)\
                    .update({"embedding": embedding})\
                    .eq("id", resource_id)\
                    .execute()

                if result.data:
                    logger.debug(f"Updated embedding for resource {resource_id} in {table_name}")
                    return True
                else:
                    logger.warning(f"No resource found with ID {resource_id} in {table_name}")
                    return False

            # Otherwise, try all tables
            for tbl in self.ALL_TABLES:
                try:
                    result = self.client.table(tbl)\
                        .update({"embedding": embedding})\
                        .eq("id", resource_id)\
                        .execute()

                    if result.data:
                        logger.debug(f"Updated embedding for resource {resource_id} in {tbl}")
                        return True
                except Exception:
                    continue

            logger.warning(f"No resource found with ID {resource_id} in any table")
            return False

        except Exception as e:
            logger.error(f"Failed to update embedding for {resource_id}: {str(e)}")
            return False

    async def list_resources(
        self,
        content_type: Optional[str] = None,
        limit: int = 100,
        offset: int = 0
    ) -> List[Dict[str, Any]]:
        """
        List resources with optional filtering.

        Args:
            content_type: Filter by content type ('book', 'video', etc.)
            limit: Maximum number of results
            offset: Number of results to skip

        Returns:
            List of resources with content_type field added
        """
        try:
            # If content_type specified, query specific table
            if content_type:
                table_name = self.TABLE_MAPPING.get(content_type)
                if not table_name:
                    valid_types = list(self.TABLE_MAPPING.keys())
                    raise ValueError(
                        f"Invalid content_type: '{content_type}'. "
                        f"Valid types: {', '.join(valid_types)}"
                    )

                result = self.client.table(table_name)\
                    .select("*")\
                    .range(offset, offset + limit - 1)\
                    .execute()

                resources = result.data if result.data else []

                # Add content_type field to each resource
                for resource in resources:
                    resource["content_type"] = content_type
                    resource["_source_table"] = table_name

                return resources

            # Otherwise, query all tables and merge
            all_resources = []
            per_table_limit = limit // len(self.ALL_TABLES) + 1

            for table_name in self.ALL_TABLES:
                try:
                    result = self.client.table(table_name)\
                        .select("*")\
                        .range(0, per_table_limit - 1)\
                        .execute()

                    if result.data:
                        # Add content_type based on table
                        for resource in result.data:
                            resource["_source_table"] = table_name
                            # Reverse lookup content_type
                            for ctype, tbl in self.TABLE_MAPPING.items():
                                if tbl == table_name:
                                    resource["content_type"] = ctype
                                    break
                            all_resources.append(resource)

                except Exception as e:
                    logger.warning(f"Failed to list resources from {table_name}: {str(e)}")
                    continue

            # Apply offset and limit
            return all_resources[offset:offset + limit]

        except Exception as e:
            logger.error(f"Failed to list resources: {str(e)}")
            return []

    def is_available(self) -> bool:
        """
        Check if the resources database connection is available.

        Returns:
            True if client is initialized, False otherwise
        """
        return self.client is not None

    async def list_news(self, limit: int = 50) -> List[Dict[str, Any]]:
        """
        List recent Google News-style items from the Resources database.

        Configure the table name via `settings.resources_news_table`.
        This method is schema-flexible: it selects `*` and returns raw rows.
        """
        limit = max(1, min(int(limit or 50), 200))

        candidates: List[str] = []
        explicit = getattr(settings, "resources_news_table", None)
        if explicit:
            candidates.append(str(explicit))
        candidates.extend(["google_news_articles", "google_news"])

        seen: set[str] = set()
        tables: List[str] = []
        for t in candidates:
            if t and t not in seen:
                seen.add(t)
                tables.append(t)

        last_err: Optional[Exception] = None
        for table_name in tables:
            try:
                query = self.client.table(table_name).select("*").limit(limit)

                # Try common timestamp columns for ordering, but don't fail if missing.
                for column in ("published_at", "publishedAt", "created_at", "inserted_at"):
                    try:
                        result = query.order(column, desc=True).execute()
                        return result.data or []
                    except Exception:
                        continue

                result = query.execute()
                return result.data or []
            except Exception as e:
                last_err = e
                continue

        if last_err:
            logger.error(f"Failed to list news (tables tried: {tables}): {last_err}")
        return []
