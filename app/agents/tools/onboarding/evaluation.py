"""
Evaluation utilities for onboarding need and value assessment.

Provides functions for:
- Seeding need/value evaluation state
- Building initial prompts for evaluation
- Evaluating user needs and value through multi-turn conversation
- LLM-based scoring of value responses
"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.integrations.azure_openai_client import AzureOpenAIClient

logger = logging.getLogger(__name__)

# Fee calculation constants
FEE_MAX_CENTS = 999  # $9.99 maximum
FEE_MIN_CENTS = 99   # $0.99 minimum
FEE_DECAY_RATE = 0.25  # Base decay rate per score point (25% - aggressive drops)
FEE_SCORE_EXPONENT = 1.5  # Makes higher scores disproportionately impactful

# Legacy fee tiers (kept for reference)
FEE_TIERS = [999, 699, 499, 299, 99]  # $9.99, $6.99, $4.99, $2.99, $0.99

# Question types for different turns
QUESTION_TYPES = [
    "concrete_example",      # Turn 1: What have you built/shipped?
    "impact_probe",          # Turn 2: What was the impact?
    "credibility_challenge", # Turn 3: How would someone verify that?
    "value_to_others",       # Turn 4: What's in it for them?
    "final_push",            # Turn 5: What's your ONE differentiator?
]


def calculate_fee_from_score(current_fee_cents: int, score: int) -> int:
    """Calculate new fee using exponential decay based on response score.

    Formula: new_fee = current_fee * (1 - decay_rate) ^ (score^exponent / 10)

    Higher scores have disproportionately larger impact due to the exponent.
    Fee is bounded between FEE_MIN_CENTS and FEE_MAX_CENTS.

    Args:
        current_fee_cents: Current fee in cents
        score: Response score from 1-10

    Returns:
        New fee in cents, bounded and rounded to integer
    """
    # Ensure score is in valid range
    score = max(1, min(10, score))

    # Calculate exponential decay multiplier
    # Higher scores produce smaller multipliers (more reduction)
    exponent_term = (score ** FEE_SCORE_EXPONENT) / 10
    multiplier = (1 - FEE_DECAY_RATE) ** exponent_term

    # Apply multiplier to current fee
    new_fee = current_fee_cents * multiplier

    # Enforce bounds and round to integer
    new_fee = max(FEE_MIN_CENTS, min(FEE_MAX_CENTS, int(round(new_fee))))

    return new_fee


def seed_need_state(
    first_prompt: str,
    prior_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Initialize or update need evaluation state.

    Args:
        first_prompt: Initial question to ask the user
        prior_state: Optional prior state to preserve

    Returns:
        Initialized need evaluation state
    """
    state = {
        "status": "asking",
        "mode": "need_eval",
        "asked_questions": [first_prompt],
        "turn_history": [{"role": "frank", "content": first_prompt}],
        "updated_at": datetime.utcnow().isoformat(),
    }

    # Preserve prior state fields if provided
    if prior_state:
        for key in ["user_need", "targets", "goals"]:
            if key in prior_state:
                state[key] = prior_state[key]

    return state


def seed_value_state(
    first_prompt: str,
    prior_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Initialize or update value evaluation state.

    Args:
        first_prompt: Initial question to ask the user
        prior_state: Optional prior state to preserve

    Returns:
        Initialized value evaluation state
    """
    state = {
        "status": "asking",
        "mode": "value_eval",
        "asked_questions": [first_prompt],
        "turn_history": [{"role": "frank", "content": first_prompt}],
        "intro_fee_cents": 999,  # Default fee ($9.99)
        "signals": {},
        "score_history": [],  # Track scores for each turn
        "extracted_claims": [],  # Claims they've made about their value
        "cumulative_score": 0,  # Running total for final fee determination
        "updated_at": datetime.utcnow().isoformat(),
    }

    # Preserve prior state fields if provided
    if prior_state:
        for key in [
            "user_value", "skills", "experience", "intro_fee_cents",
            "signals", "score_history", "extracted_claims", "cumulative_score"
        ]:
            if key in prior_state:
                state[key] = prior_state[key]

    return state


async def build_initial_need_prompt(
    user_profile: Dict[str, Any],
) -> str:
    """Build the initial need evaluation prompt.

    Args:
        user_profile: User's profile data

    Returns:
        Initial question to ask about needs
    """
    name = user_profile.get("name", "")
    name_part = f" {name.lower()}" if name else ""

    # Basic prompt - could be enhanced with email context
    return f"so{name_part} who are you trying to meet and what do you want from them"


async def build_initial_gate_prompt(
    phone_number: str,
    user_profile: Dict[str, Any],
) -> str:
    """Build the initial value evaluation prompt.

    Args:
        phone_number: User's phone number
        user_profile: User's profile data

    Returns:
        Initial question to ask about value
    """
    name = user_profile.get("name", "")
    name_part = f" {name.lower()}" if name else ""

    return f"got it{name_part}. now flip side - what can you actually offer? like if i intro you to someone, what are they getting"


async def score_value_response(
    user_message: str,
    question_type: str,
    turn_history: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Score the quality of user's value claim using LLM.

    Uses gpt-4o-mini to evaluate the credibility, clarity, and specificity
    of the user's response about their professional value.

    Args:
        user_message: The user's response about their value
        question_type: The type of question that was asked
        turn_history: Previous conversation turns for context

    Returns:
        {
            "clarity_score": 1-10,
            "credibility_score": 1-10,
            "specificity_score": 1-10,
            "overall_score": 1-10,
            "is_vague": bool,
            "extracted_claims": List[str],
            "follow_up_angle": str,
        }
    """
    # Build conversation context
    context_str = ""
    for turn in turn_history[-6:]:
        role = turn.get("role", "user")
        content = turn.get("content", "")
        context_str += f"{role}: {content}\n"

    prompt = f"""You are evaluating someone's professional value claim for a networking service.
BE GENEROUS with scoring - we want to reward people who share real info.

CONVERSATION SO FAR:
{context_str}

THEIR LATEST RESPONSE:
"{user_message}"

QUESTION TYPE THAT WAS ASKED: {question_type}

Score their response on these criteria (1-10 scale):

1. CLARITY (1-10): Can you understand what they're claiming?
   - 1-4: Confusing or nonsensical
   - 5-6: Understandable but vague
   - 7-10: Clear (MOST responses should be 7+)

2. CREDIBILITY (1-10): Does this sound real?
   - 1-4: Obviously fake or impossible
   - 5-6: Generic but plausible
   - 7-10: Sounds real (default to believing them)

3. SPECIFICITY (1-10): Are there concrete details?
   - 1-4: No specifics at all
   - 5-6: Some details (mentions a project, role, or context)
   - 7-10: Has numbers, names, or measurable outcomes

SCORING GUIDANCE - BE LENIENT:
- If they mention ANY numbers (users, revenue, team size, etc.) → minimum 6-7 overall
- If they describe a specific project or role → minimum 5-6 overall
- If they provide outcomes or impact → 7+ overall
- Only score below 5 for truly empty responses ("I work hard", "I'm good", etc.)

EXAMPLES:
- "I work at a startup" → overall: 4 (no details)
- "I built an AI agent" → overall: 5 (mentions project)
- "My app has 1000 users" → overall: 7 (has numbers!)
- "1000 users and helps find co-founders" → overall: 7-8 (numbers + use case)
- "87 connections made, people build things together" → overall: 8 (specific metrics + outcomes)

Respond with valid JSON only:
{{
    "clarity_score": <1-10>,
    "credibility_score": <1-10>,
    "specificity_score": <1-10>,
    "overall_score": <1-10>,
    "is_vague": <true if overall < 5>,
    "extracted_claims": ["list", "of", "specific", "claims", "they", "made"],
    "follow_up_angle": "suggested angle to probe next"
}}"""

    try:
        client = AzureOpenAIClient()
        response = await client.generate_response(
            system_prompt="You are a scoring assistant that evaluates professional value claims. Respond with valid JSON only.",
            user_prompt=prompt,
            model="gpt-4o-mini",
            temperature=0.3,  # Low temp for consistent scoring
        )

        # Parse JSON response
        response_text = response.strip()
        # Handle markdown code blocks
        if response_text.startswith("```"):
            response_text = response_text.split("```")[1]
            if response_text.startswith("json"):
                response_text = response_text[4:]
            response_text = response_text.strip()

        result = json.loads(response_text)

        # Validate and bound scores
        for key in ["clarity_score", "credibility_score", "specificity_score", "overall_score"]:
            if key in result:
                result[key] = max(1, min(10, int(result[key])))

        # Ensure required fields
        result.setdefault("is_vague", result.get("overall_score", 5) < 5)
        result.setdefault("extracted_claims", [])
        result.setdefault("follow_up_angle", "ask for more specifics")

        logger.info(f"Value response scored: overall={result.get('overall_score')}, vague={result.get('is_vague')}")
        return result

    except Exception as e:
        logger.error(f"Error scoring value response: {e}")
        # Fallback to heuristic scoring based on message length
        msg_len = len(user_message.strip())
        overall = min(10, max(1, msg_len // 15))
        return {
            "clarity_score": min(10, msg_len // 10),
            "credibility_score": min(10, msg_len // 15),
            "specificity_score": min(10, msg_len // 20),
            "overall_score": overall,
            "is_vague": overall < 5,
            "extracted_claims": [],
            "follow_up_angle": "ask for more specifics",
        }


async def evaluate_user_need(
    user_message: str,
    user_profile: Dict[str, Any],
    prior_state: Dict[str, Any],
) -> Dict[str, Any]:
    """Evaluate user's stated networking need.

    This function determines if the user has provided enough detail
    about WHO they want to meet and WHAT they want from those connections.

    Args:
        user_message: User's response
        user_profile: User profile data
        prior_state: Prior need evaluation state

    Returns:
        Evaluation result with decision and optional follow-up question
    """
    # Count turns
    turn_history = prior_state.get("turn_history", [])
    turn_count = len([t for t in turn_history if t.get("role") == "user"])

    # Max 1 follow-up question: accept after first follow-up regardless of content
    # Also accept immediately if very detailed first message (>50 chars)
    msg_len = len(user_message.strip())

    # Accept if: detailed first message OR already asked 1 follow-up
    if msg_len > 50 or turn_count >= 1:
        return {
            "decision": "accept",
            "user_need": {
                "raw_text": user_message,
                "targets": [],  # Would be extracted by LLM
                "goals": [],
            },
            "question": "",
            "question_type": "",
            "confidence": 0.8,
        }

    # First turn and not detailed enough - ask 1 follow-up
    return {
        "decision": "ask",
        "question": "be more specific - who exactly do you want to meet and what do you want from them",
        "question_type": "targets",
        "confidence": 0.4,
    }


async def evaluate_user_value(
    phone_number: str,
    user_message: str,
    user_profile: Dict[str, Any],
    prior_state: Dict[str, Any],
    score_result: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Evaluate user's stated professional value.

    Uses LLM-based scoring to determine if the user has demonstrated enough
    credibility, clarity, and specificity to be accepted. Fee drops are
    conditional on answer quality.

    Args:
        phone_number: User's phone number
        user_message: User's response
        user_profile: User profile data
        prior_state: Prior value evaluation state
        score_result: Pre-computed LLM scoring (if already called)

    Returns:
        Evaluation result with decision, fee, signals, and turn info
    """
    # Count turns (only user responses)
    turn_history = prior_state.get("turn_history", [])
    turn_count = len([t for t in turn_history if t.get("role") == "user"])

    # Get current fee and score history
    prior_fee = prior_state.get("intro_fee_cents", 999)  # Start at $9.99
    score_history = prior_state.get("score_history", [])
    cumulative_score = prior_state.get("cumulative_score", 0)
    extracted_claims = prior_state.get("extracted_claims", [])

    # Determine question type for this turn
    question_type_idx = min(turn_count, len(QUESTION_TYPES) - 1)
    current_question_type = QUESTION_TYPES[question_type_idx]

    # Score the response (use pre-computed if available)
    if score_result is None:
        score_result = await score_value_response(
            user_message=user_message,
            question_type=current_question_type,
            turn_history=turn_history,
        )

    overall_score = score_result.get("overall_score", 5)
    is_vague = score_result.get("is_vague", overall_score < 5)

    # Build signals from score result
    signals = {
        "clarity": score_result.get("clarity_score", 5),
        "credibility": score_result.get("credibility_score", 5),
        "specificity": score_result.get("specificity_score", 5),
        "overall": overall_score,
    }

    # Update cumulative score and claims
    new_cumulative_score = cumulative_score + overall_score
    new_extracted_claims = extracted_claims + score_result.get("extracted_claims", [])
    new_score_history = score_history + [overall_score]

    # Fee logic: exponential decay based on score
    # Only apply fee reduction if response is substantive (score >= 5)
    intro_fee_cents = prior_fee
    if not is_vague and overall_score >= 5:
        intro_fee_cents = calculate_fee_from_score(prior_fee, overall_score)

    # Ensure fee never increases (monotonically decreasing)
    intro_fee_cents = min(intro_fee_cents, prior_fee)

    # Count substantive turns (score >= 5)
    substantive_turns = len([s for s in new_score_history if s >= 5])
    has_strong_response = any(s >= 7 for s in new_score_history)

    # Determine next question type (cycle through types to avoid repetition)
    next_turn = turn_count + 1
    next_question_type_idx = next_turn % len(QUESTION_TYPES)
    next_question_type = QUESTION_TYPES[next_question_type_idx]

    # Acceptance criteria:
    # - Must have at least 2 turns (turn_count is 0-indexed, so turn_count >= 1 means 2+ turns)
    # - Fee must drop below $10 (any drop from starting fee) to accept
    # - Then: 3+ substantive turns, OR 2 substantive + strong response, OR max turns with fee dropped
    fee_below_threshold = intro_fee_cents < 999  # Fee must have dropped at all to accept
    minimum_turns_reached = turn_count >= 1  # At least 2 turns (0-indexed)

    should_accept = minimum_turns_reached and (
        (substantive_turns >= 3 and fee_below_threshold) or
        (substantive_turns >= 2 and has_strong_response and fee_below_threshold) or
        (turn_count >= 4 and fee_below_threshold)  # Max 5 turns, but only if fee dropped
    )

    if should_accept:
        # Use the per-turn calculated fee as final fee
        # (exponential decay already applied each turn)
        final_fee_cents = intro_fee_cents

        return {
            "decision": "accept",
            "user_value": {
                "raw_text": user_message,
                "skills": new_extracted_claims,
                "experience": [],
            },
            "signals": signals,
            "intro_fee_cents": final_fee_cents,
            "question": "",
            "question_type": "",
            "confidence": 0.85,
            "turn_number": turn_count + 1,
            "cumulative_score": new_cumulative_score,
            "score_history": new_score_history,
            "extracted_claims": new_extracted_claims,
        }

    # Generate follow-up question based on turn and score
    follow_up_angle = score_result.get("follow_up_angle", "")

    # Select question based on next turn type
    question_templates = {
        "concrete_example": "what's something you've actually built or shipped",
        "impact_probe": "what was the actual impact of that - numbers, outcomes, people helped",
        "credibility_challenge": "how would someone verify that? like if i looked you up, what would i see",
        "value_to_others": "ok but what's in it for them? if i intro you to someone, why would they want to meet you",
        "final_push": "last q - what's the ONE thing that makes you actually worth someone's time vs everyone else",
    }

    base_question = question_templates.get(next_question_type, "tell me more")

    # If they were vague, acknowledge it
    if is_vague:
        question = f"that's pretty vague ngl. {base_question}"
    else:
        question = base_question

    return {
        "decision": "ask",
        "question": question,
        "question_type": next_question_type,
        "signals": signals,
        "intro_fee_cents": intro_fee_cents,
        "confidence": 0.4 + (overall_score * 0.05),
        "turn_number": turn_count + 1,
        "last_response_score": overall_score,
        "cumulative_score": new_cumulative_score,
        "score_history": new_score_history,
        "extracted_claims": new_extracted_claims,
        "follow_up_angle": follow_up_angle,
    }
