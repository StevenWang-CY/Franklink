"""Azure OpenAI API client for LLM interactions."""

import logging
from typing import List, Dict, Any, Optional
import json
from datetime import datetime
import asyncio
import time

from openai import AsyncAzureOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import settings

logger = logging.getLogger(__name__)


class AzureOpenAIClient:
    """
    Client for interacting with Azure OpenAI API.

    Handles conversation generation, intent classification,
    and data extraction.
    """

    # API timeout configuration (in seconds)
    DEFAULT_TIMEOUT = 60.0  # 1 minute for most operations
    QUICK_TIMEOUT = 30.0    # 30 seconds for quick operations (intent, extraction)
    LONG_TIMEOUT = 120.0    # 2 minutes for complex operations (scoring)

    def __init__(self):
        """Initialize Azure OpenAI client."""
        self.client = AsyncAzureOpenAI(
            api_key=settings.azure_openai_api_key,
            api_version=settings.azure_openai_api_version,
            azure_endpoint=settings.azure_openai_endpoint,
            timeout=self.DEFAULT_TIMEOUT  # Set default timeout on client
        )
        self.default_deployment = settings.azure_openai_deployment_name
        self.reasoning_deployment = settings.azure_openai_reasoning_deployment_name

    async def close(self):
        """Close the client connections properly."""
        try:
            await self.client.close()
        except Exception as e:
            logger.debug(f"Error closing OpenAI client: {e}")

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10)
    )
    async def generate_response(
        self,
        messages: Optional[List[Dict[str, str]]] = None,
        use_reasoning: bool = False,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        model: Optional[str] = None,
        trace_label: Optional[str] = None,
        system_prompt: Optional[str] = None,
        user_prompt: Optional[str] = None,
    ) -> str:
        """
        Generate a response using Azure OpenAI chat completion.

        Args:
            messages: List of message dictionaries with 'role' and 'content'. If omitted, uses
                `system_prompt` + `user_prompt` to build a 2-message chat.
            use_reasoning: Whether to use reasoning model (more expensive)
            temperature: Response randomness (0-2)
            max_tokens: Maximum tokens in response (may be ignored depending on model)
            model: Optional specific model deployment to use (overrides use_reasoning)
            system_prompt: Convenience system prompt (used when `messages` omitted)
            user_prompt: Convenience user prompt (used when `messages` omitted)

        Returns:
            Generated response text
        """
        started_at = time.perf_counter()
        label = trace_label or "generate_response"

        try:
            if messages is None:
                if system_prompt is None and user_prompt is None:
                    raise ValueError("generate_response requires `messages` or `system_prompt`/`user_prompt`")
                messages = [
                    {"role": "system", "content": system_prompt or ""},
                    {"role": "user", "content": user_prompt or ""},
                ]

            # Use specified model if provided, otherwise use reasoning or default
            deployment = model or (self.reasoning_deployment if use_reasoning else self.default_deployment)

            logger.info(f"[LLM] start label={label} deployment={deployment}")

            # Build completion parameters
            completion_params = {
                "model": deployment,
                "messages": messages
            }

            # GPT-5-mini only supports temperature=1 (default), so skip for that model
            # Other models honor caller preferences
            is_gpt5_mini = deployment and "gpt-5" in deployment.lower()
            if temperature is not None and not is_gpt5_mini:
                completion_params["temperature"] = temperature

            # NOTE: max_tokens not added - GPT-5-mini doesn't support it reliably
            # Removed to fix "max_completion_tokens" error

            response = await self.client.chat.completions.create(**completion_params)

            content = response.choices[0].message.content
            duration = time.perf_counter() - started_at
            logger.info(f"[LLM] end label={label} deployment={deployment} duration_sec={duration:.2f} preview={content[:120]!r}")

            return content

        except Exception as e:
            duration = time.perf_counter() - started_at
            logger.error(f"[LLM] error label={label} duration_sec={duration:.2f} err={str(e)}", exc_info=True)
            raise OpenAIError(f"Failed to generate response: {str(e)}")

    async def classify_intent(
        self,
        system_prompt: str,
        user_prompt: str,
        model: Optional[str] = None,
    ) -> str:
        """
        Classify the intent of a user message.

        Args:
            system_prompt: System instructions for classification
            user_prompt: User message to classify

        Returns:
            Intent classification (onboarding, recommendation, networking, general)
        """
        try:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]

            deployment = model or self.default_deployment

            response = await self.client.chat.completions.create(
                model=deployment,
                messages=messages,
            )

            intent = response.choices[0].message.content.strip().lower()
            logger.info(f"Raw intent from LLM: {intent}")

            valid_intents = ["onboarding", "recommendation", "networking", "general"]
            if intent not in valid_intents:
                intent = "general"

            logger.info(f"Classified intent: {intent}")
            return intent

        except Exception as e:
            logger.error(f"Error classifying intent: {str(e)}", exc_info=True)
            return "general"

    async def extract_profile_data(
        self,
        conversation: str,
        user_message: str = None,
        conversation_history: List[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Extract structured profile data from conversation.

        Args:
            conversation: Conversation text to extract from (bot response - for backward compatibility)
            user_message: The actual user message to extract from (preferred)
            conversation_history: Recent conversation history for context

        Returns:
            Dictionary of extracted profile fields with confidence scores
        """
        try:
            # Build context from history if provided
            context_messages = []
            if conversation_history:
                # Get last 3 message pairs for context
                for msg in conversation_history[-6:]:
                    role = "User" if msg.get('message_type') == 'user' else "Frank"
                    context_messages.append(f"{role}: {msg.get('content', '')}")

            context_str = "\n".join(context_messages) if context_messages else ""

            # Prioritize user message if provided, otherwise use conversation
            text_to_analyze = user_message if user_message else conversation

            extraction_prompt = f"""
            Extract user profile information from this conversation.

            CONVERSATION CONTEXT (recent messages):
            {context_str if context_str else "No prior context"}

            CURRENT USER MESSAGE TO ANALYZE:
            {text_to_analyze}

            CRITICAL EXTRACTION RULES:
            1. Extract ONLY from the user's actual words, not the bot's response
            2. Look for information in BOTH the context and current message
            3. Handle university abbreviations correctly:
               - UIUC -> University of Illinois Urbana-Champaign
               - MIT -> Massachusetts Institute of Technology
               - UCLA -> University of California, Los Angeles
               - USC -> University of Southern California
               - NYU -> New York University
               - GT -> Georgia Institute of Technology
               - If you see an abbreviation, expand it to full name

            4. For year, convert text to numbers:
               - freshman/first year -> 1
               - sophomore/second year -> 2
               - junior/third year -> 3
               - senior/fourth year -> 4
               - graduate/grad student -> 5

            5. For major, extract the FULL field name:
               - "CS" or "CompSci" -> "Computer Science"
               - "CE" -> "Computer Engineering"
               - "EE" -> "Electrical Engineering"
               - Keep full names as-is

            Extract the following fields (use null if not mentioned):
            - name: (first name or full name - extract from "My name is X" or "I'm X")
            - university: (FULL university name, expand abbreviations)
            - location: (city, state, or region)
            - major: (FULL field of study name, expand abbreviations)
            - year: (1-5 as integer, null if not mentioned)
            - career_interests: (list of specific career interests mentioned)
            - confidence: (your confidence in extractions: "high", "medium", or "low")

            EXAMPLES:

            User: "My name is Edward, I go to UIUC"
            Output: {{"name": "Edward", "university": "University of Illinois Urbana-Champaign", "major": null, "year": null, "location": null, "career_interests": [], "confidence": "high"}}

            User: "MIT, studying CS"
            Output: {{"name": null, "university": "Massachusetts Institute of Technology", "major": "Computer Science", "year": null, "location": null, "career_interests": [], "confidence": "high"}}

            User: "I'm a freshman at UCLA majoring in business"
            Output: {{"name": null, "university": "University of California, Los Angeles", "major": "Business", "year": 1, "location": null, "career_interests": [], "confidence": "high"}}

            Format response as JSON only, no other text.
            """

            messages = [
                {"role": "system", "content": "You are a precise data extraction assistant. Extract only explicitly stated information from the USER's message, not the bot's response. Expand all abbreviations to full names."},
                {"role": "user", "content": extraction_prompt}
            ]

            # GPT-5-mini has parameter restrictions - use minimal params
            response = await self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages
                # Note: temperature and max_tokens not set - GPT-5-mini restrictions
            )

            response_text = response.choices[0].message.content

            # Try to parse JSON from response
            try:
                # Find JSON object in response
                import re
                json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
                if json_match:
                    result = json.loads(json_match.group())
                else:
                    result = {}
            except json.JSONDecodeError:
                logger.warning(f"Failed to parse JSON from response: {response_text}")
                result = {}

            # Filter out null values
            profile_data = {k: v for k, v in result.items() if v is not None}

            logger.info(f"Extracted profile data: {profile_data}")
            return profile_data

        except Exception as e:
            logger.error(f"Error extracting profile data: {str(e)}", exc_info=True)
            return {}

    def _calculate_keyword_bonus(
        self,
        opportunity: Dict[str, Any],
        user_query: str
    ) -> float:
        """
        Calculate keyword matching bonus for an opportunity based on query terms.

        This helps ensure that opportunities with explicit keyword matches get boosted
        before AI scoring, preventing broader queries from missing specific opportunities.

        Args:
            opportunity: Opportunity data with title, description, tags, organization
            user_query: The user's query string

        Returns:
            Bonus score between 0.0 and 0.2
        """
        # Common words to exclude from keyword matching
        STOP_WORDS = {
            'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
            'of', 'with', 'by', 'from', 'up', 'about', 'into', 'through', 'during',
            'show', 'me', 'find', 'get', 'see', 'tell', 'give', 'want', 'need',
            'looking', 'search', 'help', 'can', 'you', 'i', 'my', 'is', 'are', 'am'
        }

        # Extract meaningful keywords from query (lowercase, remove common words)
        query_words = user_query.lower().split()
        query_keywords = [word.strip('.,!?;:') for word in query_words
                         if word.lower() not in STOP_WORDS and len(word) > 2]

        if not query_keywords:
            return 0.0

        # Prepare searchable text from opportunity (use full description, not truncated)
        # Handle None values properly
        title = opportunity.get('title') or ''
        description = opportunity.get('description') or ''
        organization = opportunity.get('organization') or ''
        tags = opportunity.get('tags') or []

        searchable_text = ' '.join([
            title.lower(),
            description.lower(),
            organization.lower(),
            ' '.join(tags).lower()
        ])

        # Count keyword matches
        matches = 0
        total_keywords = len(query_keywords)

        for keyword in query_keywords:
            if keyword in searchable_text:
                matches += 1

        # Calculate bonus: 0.0 to 0.2 based on match percentage
        # This ensures keyword-matched opportunities get a boost without overwhelming AI scoring
        match_ratio = matches / total_keywords if total_keywords > 0 else 0
        bonus = match_ratio * 0.2  # Max bonus of 0.2 (20%)

        if bonus > 0:
            logger.debug(f"Keyword bonus {bonus:.3f} for '{opportunity.get('title')}' - matched {matches}/{total_keywords} keywords")

        return bonus

    async def score_opportunities(
        self,
        user_profile: Dict[str, Any],
        opportunities: List[Dict[str, Any]],
        user_query: str
    ) -> List[Dict[str, Any]]:
        """
        Score opportunities based on how well they match the user's query and profile.

        Args:
            user_profile: User's profile data
            opportunities: List of opportunities to score
            user_query: The user's actual request message

        Returns:
            List of opportunities with scores and reasoning
        """
        try:
            # Prepare opportunities for scoring (include key fields only)
            opp_summaries = []
            for i, opp in enumerate(opportunities):
                opp_summaries.append({
                    "index": i,
                    "title": opp.get("title", ""),
                    "organization": opp.get("organization", ""),
                    "description": opp.get("description", "")[:500] if opp.get("description") else "",
                    "tags": opp.get("tags", []),
                    "location": opp.get("location", "")
                })

            system_prompt = """
            You are an intelligent career matching system. Score each opportunity from 0.0 to 1.0
            based on how well it matches what the user is asking for.

            Scoring priorities:
            1. If the user asks for something specific (e.g., "finance internships"), prioritize that heavily (70% weight)
            2. If the query is generic (e.g., "show me opportunities"), use their profile interests (70% weight)
            3. Always consider profile as secondary factor (30% weight)

            **IMPORTANT: Pay close attention to the opportunity description field.** The description contains detailed
            information about job requirements, responsibilities, qualifications, and what the role entails. Use this
            information heavily when scoring - it often provides the most relevant context for matching.

            **CRITICAL: Check for program type keywords!** When the user mentions specific program types (e.g.,
            "insight program", "internship", "full-time", "fellowship"), check if those exact terms or semantic
            equivalents appear in the opportunity's title, description, or tags. Opportunities that match program
            type should score 0.8-1.0 even if other details vary.

            Suggested weighting for scoring:
            - Description content (specific requirements, responsibilities, qualifications): 50%
            - Title and tags (role type, career field, program type): 30%
            - Location and organization: 20%

            Return JSON array with scores: [{"index": 0, "score": 0.85, "reason": "matches query for finance"}, ...]

            Examples of good matching:
            - User asks "finance roles" -> Finance opportunities get 0.8-1.0
            - CS major asks "finance" -> Still give finance 0.8+, CS gets 0.3
            - User asks "active_resources" -> Use their profile interests
            - User asks "Company X opportunities" -> All Company X opportunities should score 0.6+ base score
            - User asks "Company X insight program" -> Company X insight programs get 0.9-1.0
            """

            user_prompt = f"""
            User Query: "{user_query}"

            User Profile:
            - Major: {user_profile.get('major', 'Not specified')}
            - Interests: {', '.join(user_profile.get('career_interests', [])) or 'Not specified'}
            - Year: {user_profile.get('year', 'Not specified')}

            Opportunities to score:
            {json.dumps(opp_summaries, indent=2)}

            Score each opportunity. Return ONLY a JSON array.
            """

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ]

            api_response = await self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages
            )

            response = api_response.choices[0].message.content
            logger.info(f"AI scoring completed. Finish reason: {api_response.choices[0].finish_reason}")

            # Parse response
            try:
                if not response:
                    logger.error("Empty response from AI")
                    return opportunities

                cleaned = response.strip()
                if cleaned.startswith("```json"):
                    cleaned = cleaned[7:]
                if cleaned.endswith("```"):
                    cleaned = cleaned[:-3]

                cleaned = cleaned.strip()
                logger.info(f"Cleaned response for parsing: {cleaned[:500]}")

                scores = json.loads(cleaned)

                # Apply scores back to opportunities with keyword bonus
                scored_opps = []
                for score_data in scores:
                    idx = score_data["index"]
                    if idx < len(opportunities):
                        opp = opportunities[idx].copy()

                        # Calculate keyword matching bonus
                        keyword_bonus = self._calculate_keyword_bonus(opp, user_query)

                        # Combine AI score with keyword bonus (cap at 1.0)
                        ai_score = score_data["score"]
                        combined_score = min(ai_score + keyword_bonus, 1.0)

                        opp["match_score"] = combined_score
                        opp["match_reason"] = score_data.get("reason", "")

                        # Log when keyword bonus significantly affects score
                        if keyword_bonus > 0.05:
                            logger.info(f"Applied keyword bonus +{keyword_bonus:.2f} to '{opp.get('title', 'Unknown')}' (AI: {ai_score:.2f} -> Final: {combined_score:.2f})")

                        scored_opps.append(opp)

                # Sort by score
                scored_opps.sort(key=lambda x: x.get("match_score", 0), reverse=True)

                logger.info(f"Scored {len(scored_opps)} opportunities for query: '{user_query}'")
                return scored_opps

            except (json.JSONDecodeError, KeyError) as e:
                logger.error(f"Error parsing scoring response: {str(e)}")
                # Fallback to original opportunities
                return opportunities

        except Exception as e:
            logger.error(f"Error scoring opportunities: {str(e)}", exc_info=True)
            return opportunities

    async def generate_opportunity_recommendation(
        self,
        user_profile: Dict[str, Any],
        opportunities: List[Dict[str, Any]]
    ) -> str:
        """
        Generate personalized opportunity recommendations.

        Args:
            user_profile: User's profile data
            opportunities: List of available opportunities

        Returns:
            Personalized recommendation message
        """
        try:
            system_prompt = """
            You are a friendly career counselor helping a student find opportunities.
            Based on their profile and the available opportunities, provide personalized
            recommendations. Be encouraging and specific about why each opportunity
            matches their interests.
            """

            user_prompt = f"""
            User Profile:
            - Name: {user_profile.get('name', 'Student')}
            - University: {user_profile.get('university', 'N/A')}
            - Major: {user_profile.get('major', 'N/A')}
            - Year: {user_profile.get('year', 'N/A')}
            - Interests: {', '.join(user_profile.get('career_interests', []))}

            Available Opportunities:
            {json.dumps(opportunities, indent=2)}

            Please recommend the top 2-3 opportunities that best match this student's profile.
            Keep the response conversational and under 200 words.
            """

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ]

            response = await self.generate_response(
                messages=messages,
                model="gpt-4o-mini",
            )

            return response

        except Exception as e:
            logger.error(f"Error generating recommendations: {str(e)}", exc_info=True)
            return "I'm having trouble generating recommendations right now. Please try again later."

    async def analyze_opportunity_match(
        self,
        user_profile: Dict[str, Any],
        opportunity: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Analyze how well an opportunity matches a user's profile.

        Args:
            user_profile: User's profile data
            opportunity: Opportunity to analyze

        Returns:
            Match analysis with score and reasons
        """
        try:
            analysis_prompt = f"""
            Analyze how well this opportunity matches the user's profile.

            User Profile:
            {json.dumps(user_profile, indent=2)}

            Opportunity:
            {json.dumps(opportunity, indent=2)}

            Provide your analysis in the following format:
            - Match Score: (0.0 to 1.0)
            - Match Reasons: (list 2-3 specific reasons)
            - Recommendation: (highly_recommended, recommended, neutral, or not_recommended)

            Format as JSON.
            """

            messages = [
                {"role": "system", "content": "You are a career matching expert. Analyze opportunities objectively."},
                {"role": "user", "content": analysis_prompt}
            ]

            response = await self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages
            )

            response_text = response.choices[0].message.content

            # Parse the response
            try:
                import re
                json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
                if json_match:
                    result = json.loads(json_match.group())
                else:
                    # Try to extract from text format
                    result = {
                        "match_score": 0.5,
                        "match_reasons": ["General opportunity"],
                        "recommendation": "neutral"
                    }
            except Exception as parse_error:
                logger.warning(f"Failed to parse match analysis response: {parse_error}")
                result = {
                    "match_score": 0.5,
                    "match_reasons": ["Unable to analyze match"],
                    "recommendation": "neutral"
                }

            logger.info(f"Match analysis: score={result.get('match_score')}, recommendation={result.get('recommendation')}")
            return result

        except Exception as e:
            logger.error(f"Error analyzing match: {str(e)}", exc_info=True)
            return {
                "match_score": 0.5,
                "match_reasons": ["Unable to analyze match"],
                "recommendation": "neutral"
            }

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10)
    )
    async def get_embedding(
        self,
        text: str,
        model: Optional[str] = None
    ) -> Optional[List[float]]:
        """
        Generate embedding vector for text.

        Args:
            text: Text to embed
            model: Embedding model to use

        Returns:
            List of floats representing the embedding vector, or None on failure
        """
        try:
            deployment = model or settings.azure_openai_embedding_deployment
            if not deployment:
                logger.error("[LLM] Embedding deployment not configured")
                return None

            response = await self.client.embeddings.create(
                input=text,
                model=deployment
            )

            embedding = response.data[0].embedding
            logger.debug(f"Generated embedding of length {len(embedding)}")

            return embedding

        except Exception as e:
            logger.error(f"Error generating embedding: {str(e)}", exc_info=True)
            # Return None to indicate failure - callers should handle gracefully
            return None


    async def extract_image_text(
        self,
        image_url: str,
        instruction: Optional[str] = None,
        detail: str = "auto"
    ) -> str:
        """Extract textual content from image via Azure OpenAI Vision."""
        try:
            prompt = instruction or (
                "Extract all readable text from this image. Respond with plain text only, preserving line breaks."
            )

            messages: List[Dict[str, Any]] = [
                {"role": "system", "content": "You transcribe visuals into text."},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": image_url,
                                "detail": detail
                            }
                        }
                    ]
                }
            ]

            # Use fast deployment for vision/text extraction (assumes deployment supports vision, e.g., GPT-4o family)
            response = await self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                max_tokens=1000  # Allow enough tokens for text extraction
            )

            content = response.choices[0].message.content
            logger.info(f"Extracted text from image: {content[:100]}...")
            return content

        except Exception as e:
            logger.error(f"Error extracting text from image: {str(e)}", exc_info=True)
            raise OpenAIError(f"Failed to extract text from image: {str(e)}")


class OpenAIError(Exception):
    """Custom exception for OpenAI API errors."""
    pass
