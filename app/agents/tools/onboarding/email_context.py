"""
Email context utilities for Frank onboarding.

Fetches and processes user emails via Composio Gmail integration.
Now returns full email content (subject + body) without categorization.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import random
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.config import settings
from app.integrations.composio_client import ComposioClient

logger = logging.getLogger(__name__)


_DEFAULT_QUERY = "newer_than:90d"
_DEFAULT_RECEIVED_LIMIT = 50  # Fetch 50 received emails
_DEFAULT_SENT_LIMIT = 50  # Fetch 50 sent emails
_DEFAULT_MAX_EVIDENCE = 100  # Max evidence emails to include in signals (50 received + 50 sent)
_DEFAULT_REFRESH_DAYS = 14
_MAX_BODY_CHARS = 500  # 500 char limit per email body

# Comprehensive career interest to keyword mapping
CAREER_TO_KEYWORDS = {
    # Venture Capital & Investing
    "vc": ["investor", "funding", "pitch", "venture", "raise", "capital", "term sheet", "due diligence"],
    "venture capital": ["investor", "funding", "pitch", "venture", "raise", "capital", "portfolio"],
    "investing": ["investor", "portfolio", "fund", "returns", "equity", "valuation"],
    "angel investing": ["angel", "investor", "seed", "pre-seed", "funding", "pitch"],
    "private equity": ["pe", "buyout", "lbo", "portfolio", "fund", "acquisition"],

    # Startups & Entrepreneurship
    "startup": ["founder", "startup", "launch", "build", "ship", "product", "mvp", "pivot"],
    "startups": ["founder", "startup", "launch", "build", "ship", "product", "mvp"],
    "entrepreneurship": ["founder", "startup", "venture", "business", "launch", "bootstrap"],
    "founder": ["startup", "co-founder", "ceo", "build", "launch", "raise", "pitch"],

    # Software & Engineering
    "software engineering": ["github", "code", "deploy", "engineering", "developer", "pull request", "merge"],
    "software": ["github", "code", "deploy", "software", "developer", "tech", "api"],
    "engineering": ["engineer", "technical", "system", "architecture", "infrastructure"],
    "backend": ["api", "server", "database", "backend", "infrastructure", "microservices"],
    "frontend": ["react", "frontend", "ui", "ux", "javascript", "typescript", "web"],
    "fullstack": ["fullstack", "full-stack", "developer", "engineer", "web", "api"],
    "devops": ["devops", "ci/cd", "kubernetes", "docker", "aws", "cloud", "infrastructure"],
    "mobile": ["ios", "android", "mobile", "app", "swift", "kotlin", "react native"],

    # Product & Design
    "product": ["product", "launch", "feature", "roadmap", "user", "metrics", "prd"],
    "product management": ["product", "pm", "roadmap", "feature", "sprint", "backlog", "stakeholder"],
    "design": ["design", "figma", "ui", "ux", "prototype", "wireframe", "user research"],
    "ux": ["ux", "user experience", "research", "usability", "prototype", "design"],
    "ui": ["ui", "interface", "design", "figma", "mockup", "visual"],

    # Data & AI
    "ai": ["ai", "ml", "machine learning", "model", "openai", "anthropic", "llm", "gpt"],
    "artificial intelligence": ["ai", "ml", "machine learning", "neural", "deep learning", "model"],
    "machine learning": ["ml", "model", "training", "dataset", "neural", "tensorflow", "pytorch"],
    "data science": ["data", "analytics", "model", "python", "statistics", "visualization"],
    "data": ["data", "analytics", "sql", "dashboard", "metrics", "insights"],
    "analytics": ["analytics", "metrics", "dashboard", "data", "insights", "reporting"],

    # Finance & Banking
    "finance": ["finance", "banking", "trading", "investment", "analyst", "model", "valuation"],
    "banking": ["bank", "finance", "trading", "analyst", "deal", "m&a", "ipo"],
    "investment banking": ["ib", "deal", "m&a", "ipo", "pitchbook", "valuation", "transaction"],
    "trading": ["trading", "market", "equity", "fixed income", "derivatives", "quant"],
    "quant": ["quant", "quantitative", "algorithm", "trading", "model", "statistics"],

    # Consulting & Strategy
    "consulting": ["consulting", "strategy", "case", "engagement", "client", "deliverable"],
    "strategy": ["strategy", "strategic", "planning", "analysis", "market", "competitive"],
    "management consulting": ["mckinsey", "bain", "bcg", "consulting", "case", "engagement"],

    # Sales & Marketing
    "sales": ["sales", "revenue", "deal", "pipeline", "quota", "account", "customer"],
    "marketing": ["marketing", "campaign", "brand", "growth", "acquisition", "content"],
    "growth": ["growth", "acquisition", "retention", "funnel", "conversion", "metrics"],
    "content": ["content", "blog", "social", "copy", "writing", "marketing"],

    # Recruiting & HR
    "recruiting": ["recruiter", "interview", "job", "offer", "hiring", "candidate", "application"],
    "hr": ["hr", "human resources", "people", "talent", "hiring", "onboarding"],
    "talent": ["talent", "recruiting", "hiring", "candidate", "interview", "offer"],

    # Operations & Business
    "operations": ["operations", "ops", "process", "efficiency", "logistics", "supply chain"],
    "business": ["business", "strategy", "operations", "revenue", "growth", "market"],
    "business development": ["bd", "partnership", "deal", "relationship", "strategic"],

    # Legal & Policy
    "legal": ["legal", "law", "attorney", "contract", "compliance", "regulation"],
    "policy": ["policy", "regulation", "government", "compliance", "advocacy"],

    # Healthcare & Biotech
    "healthcare": ["healthcare", "health", "medical", "patient", "clinical", "hospital"],
    "biotech": ["biotech", "pharma", "clinical", "trial", "fda", "drug", "research"],
    "health tech": ["health tech", "digital health", "telehealth", "medical", "patient"],

    # Real Estate
    "real estate": ["real estate", "property", "investment", "development", "commercial", "residential"],

    # Media & Entertainment
    "media": ["media", "content", "entertainment", "streaming", "video", "production"],
    "entertainment": ["entertainment", "media", "content", "production", "creative"],

    # Education
    "education": ["education", "learning", "course", "student", "teaching", "edtech"],
    "edtech": ["edtech", "learning", "course", "education", "student", "platform"],

    # Crypto & Web3
    "crypto": ["crypto", "blockchain", "web3", "defi", "nft", "token", "ethereum"],
    "web3": ["web3", "blockchain", "crypto", "defi", "dao", "smart contract"],
    "blockchain": ["blockchain", "crypto", "decentralized", "web3", "smart contract"],

    # Research & Academia
    "research": ["research", "paper", "study", "academic", "publication", "phd"],
    "academia": ["academic", "professor", "research", "university", "publication", "phd"],

    # General Tech Industry
    "tech": ["tech", "technology", "startup", "software", "product", "engineering"],
    "technology": ["technology", "tech", "software", "digital", "innovation"],

    # Networking & Events
    "networking": ["network", "connect", "meetup", "event", "conference", "introduction"],
    "events": ["event", "conference", "meetup", "summit", "demo day", "hackathon"],
}

# Patterns to exclude from email selection (notifications, automated emails)
# Be careful not to exclude legitimate emails - only clearly automated/transactional ones
EXCLUDE_SENDER_PATTERNS = [
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "notifications@", "notification@",
    "mailer-daemon", "postmaster", "bounce",
    "billing@", "invoice@", "receipt@",
    "verify@", "confirm@", "security@",
]

# Semantic keyword expansions for better topical overlap detection
# Maps common terms to their semantic equivalents
SEMANTIC_EXPANSIONS = {
    "startup": ["founder", "vc", "venture", "funding", "raise", "seed", "series", "bootstrap", "launch"],
    "vc": ["investor", "fund", "partner", "portfolio", "pitch", "term", "raise", "capital", "sequoia", "a]16z", "andreessen"],
    "investor": ["vc", "fund", "funding", "raise", "pitch", "capital", "angel", "seed"],
    "funding": ["raise", "investor", "vc", "capital", "seed", "series", "round"],
    "engineering": ["developer", "software", "code", "github", "deploy", "ship", "engineer", "technical"],
    "product": ["pm", "roadmap", "feature", "launch", "user", "metrics", "ship", "build"],
    "finance": ["banking", "trading", "investment", "analyst", "deal", "valuation", "model"],
    "consulting": ["strategy", "mckinsey", "bain", "bcg", "case", "engagement", "client"],
    "ai": ["ml", "machine", "learning", "model", "openai", "anthropic", "llm", "gpt", "neural"],
    "crypto": ["blockchain", "web3", "defi", "token", "ethereum", "bitcoin", "nft"],
    "legal": ["law", "attorney", "contract", "compliance", "litigation", "counsel"],
    "recruiting": ["hiring", "interview", "job", "offer", "candidate", "talent", "application"],
    "growth": ["acquisition", "retention", "funnel", "conversion", "marketing", "revenue"],
    "sales": ["revenue", "deal", "pipeline", "account", "customer", "quota", "closing"],
}

# Notable company domains for proactive name-dropping
NOTABLE_COMPANY_DOMAINS = {
    # Top Tech Companies
    "google.com": "Google", "alphabet.com": "Google",
    "meta.com": "Meta", "facebook.com": "Meta",
    "apple.com": "Apple",
    "amazon.com": "Amazon", "aws.amazon.com": "AWS",
    "microsoft.com": "Microsoft",
    "netflix.com": "Netflix",
    # Top Startups & Unicorns
    "stripe.com": "Stripe",
    "openai.com": "OpenAI",
    "anthropic.com": "Anthropic",
    "coinbase.com": "Coinbase",
    "plaid.com": "Plaid",
    "figma.com": "Figma",
    "notion.so": "Notion", "notion.com": "Notion",
    "airtable.com": "Airtable",
    "linear.app": "Linear",
    "vercel.com": "Vercel",
    "datadog.com": "Datadog",
    "databricks.com": "Databricks",
    "snowflake.com": "Snowflake",
    "palantir.com": "Palantir",
    "robinhood.com": "Robinhood",
    "ramp.com": "Ramp",
    "brex.com": "Brex",
    "rippling.com": "Rippling",
    "scale.com": "Scale AI",
    "anduril.com": "Anduril",
    # VC Firms
    "a]16z.com": "a]16z", "andreessen.com": "a]16z",
    "sequoiacap.com": "Sequoia",
    "ycombinator.com": "Y Combinator", "yc.com": "Y Combinator",
    "greylock.com": "Greylock",
    "accel.com": "Accel",
    "benchmark.com": "Benchmark",
    "kpcb.com": "Kleiner Perkins",
    "indexventures.com": "Index Ventures",
    "lightspeed.com": "Lightspeed",
    "generalcatalyst.com": "General Catalyst",
    "foundersfd.com": "Founders Fund",
    # Consulting
    "mckinsey.com": "McKinsey",
    "bcg.com": "BCG",
    "bain.com": "Bain",
    "deloitte.com": "Deloitte",
    # Banks
    "goldmansachs.com": "Goldman Sachs", "gs.com": "Goldman Sachs",
    "jpmorgan.com": "JPMorgan", "jpmchase.com": "JPMorgan",
    "morganstanley.com": "Morgan Stanley",
    "blackstone.com": "Blackstone",
    "blackrock.com": "BlackRock",
    "citadel.com": "Citadel",
    "bridgewater.com": "Bridgewater",
}


def expand_keywords(words: set) -> set:
    """Expand word set with semantic equivalents for better overlap detection."""
    expanded = set(words)
    for word in list(words):
        word_lower = word.lower()
        # Check if this word is a key in semantic expansions
        if word_lower in SEMANTIC_EXPANSIONS:
            expanded.update(SEMANTIC_EXPANSIONS[word_lower])
        # Check if this word appears in any expansion list
        for key, synonyms in SEMANTIC_EXPANSIONS.items():
            if word_lower in synonyms:
                expanded.add(key)
                expanded.update(synonyms)
    return expanded


def extract_notable_companies(emails: List[Dict[str, Any]]) -> List[str]:
    """Extract notable company names from email senders for name-dropping."""
    companies = []
    seen = set()

    for email in emails:
        sender = _as_clean_str(email.get("sender", "")).lower()
        sender_domain = _as_clean_str(email.get("sender_domain", "")).lower()

        # Check sender domain directly
        if sender_domain in NOTABLE_COMPANY_DOMAINS:
            company = NOTABLE_COMPANY_DOMAINS[sender_domain]
            if company not in seen:
                companies.append(company)
                seen.add(company)
                continue

        # Check if sender email contains notable domain
        for domain, company in NOTABLE_COMPANY_DOMAINS.items():
            if domain in sender and company not in seen:
                companies.append(company)
                seen.add(company)
                break

    return companies[:5]  # Return top 5 notable companies


def generate_network_insights(emails: List[Dict[str, Any]], career_interests: List[str]) -> List[str]:
    """
    Generate proactive insights about user's network for Frank to reference.

    Returns natural-sounding observations like:
    - "you're in touch with Stripe, YC people"
    - "looks like you're in fundraising mode"
    """
    insights = []

    # 1. Notable company connections
    notable_companies = extract_notable_companies(emails)
    if notable_companies:
        if len(notable_companies) >= 3:
            insights.append(f"you're talking to {notable_companies[0]}, {notable_companies[1]}, {notable_companies[2]} people")
        elif len(notable_companies) == 2:
            insights.append(f"you're in touch with {notable_companies[0]} and {notable_companies[1]} folks")
        elif len(notable_companies) == 1:
            insights.append(f"you've got {notable_companies[0]} connections")

    # 2. Detect activity patterns from email content
    all_text = ""
    for email in emails[:10]:
        subject = _as_clean_str(email.get("subject", "")).lower()
        body = _as_clean_str(email.get("body", "")).lower()
        all_text += f" {subject} {body}"

    # Fundraising signals
    fundraising_keywords = ["funding", "raise", "investor", "pitch", "term sheet", "due diligence", "valuation", "cap table"]
    if sum(1 for kw in fundraising_keywords if kw in all_text) >= 2:
        insights.append("looks like you're in fundraising mode")

    # Recruiting signals
    recruiting_keywords = ["interview", "offer", "application", "hiring", "recruiter", "candidate", "position"]
    if sum(1 for kw in recruiting_keywords if kw in all_text) >= 2:
        if "fundraising mode" not in str(insights):  # Avoid conflicting signals
            insights.append("you're in the middle of recruiting cycles")

    # Launch/building signals
    launch_keywords = ["launch", "ship", "beta", "users", "product hunt", "demo day", "mvp"]
    if sum(1 for kw in launch_keywords if kw in all_text) >= 2:
        insights.append("you're in building mode")

    return insights[:2]  # Max 2 insights

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_URL_RE = re.compile(r"https?://\S+")
_LONG_TOKEN_RE = re.compile(r"\b[A-Za-z0-9]{16,}\b")
_PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,}\d")

# Keep PII filtering for safety - only truly sensitive patterns
# Be careful not to over-filter: "bank" alone would filter "investment bank", "banking industry"
_SENSITIVE_RE = re.compile(
    r"\b("
    # Security codes and passwords (high risk)
    r"one[- ]time\s+(code|password)|verification\s+code|security\s+code|otp\s*:|2fa\s+code|"
    r"reset\s+your\s+password|temporary\s+password|"
    # Personal identifiers (high risk)
    r"ssn[:\s]|social\s+security\s+(number|#)|passport\s+(number|#)|"
    # Medical records (HIPAA)
    r"lab\s+results|medical\s+records|patient\s+id|diagnosis[:\s]|"
    # Financial account numbers (specific patterns, not generic words)
    r"account[:\s#]*\d{8,}|"  # Account numbers with 8+ digits
    r"routing[:\s#]*\d{9}|"  # Routing numbers
    # API/Auth tokens
    r"Bearer\s+[A-Za-z0-9-_]{20,}|"  # Bearer tokens (20+ chars)
    r"api[_-]?key[:\s]*[A-Za-z0-9-_]{16,}"  # API keys
    r")\b",
    re.I,
)

# Credit card pattern (separate for clarity)
_CREDIT_CARD_RE = re.compile(r"\b(?:\d{4}[- ]?){3,4}\b")


def _scrub_pii(text: str) -> str:
    """Remove PII patterns from text while preserving readability."""
    if not text:
        return ""
    # Order matters: more specific patterns first
    # Remove credit card numbers (before phone, as they can overlap)
    text = _CREDIT_CARD_RE.sub("[card]", text)
    # Remove emails (but keep domain for context)
    text = _EMAIL_RE.sub("[email]", text)
    # Remove URLs (tracking tokens, sensitive links)
    text = _URL_RE.sub("[link]", text)
    # Remove phone numbers
    text = _PHONE_RE.sub("[phone]", text)
    # Remove long tokens (API keys, auth tokens)
    text = _LONG_TOKEN_RE.sub("[token]", text)
    return text


def _as_clean_str(value: Any) -> str:
    return str(value or "").strip()


def _truncate(text: str, *, max_len: int) -> str:
    s = _as_clean_str(text)
    if not s:
        return ""
    if len(s) <= max_len:
        return s
    return s[: max_len - 1].rstrip() + "…"


def _extract_header(item: Dict[str, Any], name: str) -> str:
    """Extract a header value from email payload."""
    payload = item.get("payload")
    if isinstance(payload, dict):
        headers = payload.get("headers")
        if isinstance(headers, list):
            for header in headers:
                if not isinstance(header, dict):
                    continue
                if str(header.get("name") or "").strip().lower() != name.lower():
                    continue
                val = _as_clean_str(header.get("value"))
                if val:
                    return val
        if isinstance(headers, dict):
            for k, v in headers.items():
                if str(k or "").strip().lower() == name.lower():
                    val = _as_clean_str(v)
                    if val:
                        return val
    meta = item.get("headers")
    if isinstance(meta, dict):
        for k, v in meta.items():
            if str(k or "").strip().lower() == name.lower():
                val = _as_clean_str(v)
                if val:
                    return val
    return ""


def _extract_subject(item: Dict[str, Any]) -> str:
    """Extract email subject."""
    return (
        _as_clean_str(item.get("subject"))
        or _as_clean_str(item.get("Subject"))
        or _extract_header(item, "Subject")
    )


def _extract_snippet(item: Dict[str, Any]) -> str:
    """Extract email snippet/preview."""
    return (
        _as_clean_str(item.get("snippet"))
        or _as_clean_str(item.get("snippetText"))
        or _as_clean_str(item.get("preview"))
    )


def _strip_html(text: str) -> str:
    """Strip HTML tags and clean up the result."""
    # Remove HTML tags
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    # Decode common HTML entities
    text = text.replace("&nbsp;", " ")
    text = text.replace("&amp;", "&")
    text = text.replace("&lt;", "<")
    text = text.replace("&gt;", ">")
    text = text.replace("&quot;", '"')
    # Clean up whitespace
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_body(item: Dict[str, Any]) -> str:
    """Extract full email body from payload, with base64 decoding."""
    # First try messageText which Composio provides
    message_text = _as_clean_str(item.get("messageText"))
    if message_text:
        # Check if it's HTML and strip if so
        if "<html" in message_text.lower() or "<!doctype" in message_text.lower():
            message_text = _strip_html(message_text)
        else:
            # Clean up the text - remove excessive whitespace but keep structure
            message_text = re.sub(r"\r\n", "\n", message_text)
            message_text = re.sub(r"\n{3,}", "\n\n", message_text)
            message_text = re.sub(r"[ \t]+", " ", message_text)
        return message_text.strip()

    # Try preview field (Composio's preview may be a dict or string)
    preview = item.get("preview")
    if isinstance(preview, dict):
        preview_body = _as_clean_str(preview.get("body"))
        if preview_body:
            return preview_body
    elif isinstance(preview, str) and preview.strip():
        return preview.strip()

    # Try to get body from payload
    payload = item.get("payload", {})
    if not isinstance(payload, dict):
        return _extract_snippet(item)

    def _decode_data(data: str) -> str:
        """Decode base64 URL-safe encoded data."""
        if not data:
            return ""
        try:
            decoded = base64.urlsafe_b64decode(data + "==")
            return decoded.decode("utf-8", errors="replace")
        except Exception:
            return ""

    # Try simple body first
    body_data = payload.get("body", {})
    if isinstance(body_data, dict) and body_data.get("data"):
        decoded = _decode_data(body_data["data"])
        if decoded:
            # If it looks like HTML, strip tags
            if "<html" in decoded.lower() or "<!doctype" in decoded.lower():
                return _strip_html(decoded)
            return decoded

    # Handle multipart messages
    parts = payload.get("parts", [])
    if isinstance(parts, list):
        for part in parts:
            if not isinstance(part, dict):
                continue
            mime_type = str(part.get("mimeType") or "").lower()
            if mime_type == "text/plain":
                part_body = part.get("body", {})
                if isinstance(part_body, dict) and part_body.get("data"):
                    decoded = _decode_data(part_body["data"])
                    if decoded:
                        return decoded
            nested_parts = part.get("parts", [])
            if isinstance(nested_parts, list):
                for nested in nested_parts:
                    if not isinstance(nested, dict):
                        continue
                    nested_mime = str(nested.get("mimeType") or "").lower()
                    if nested_mime == "text/plain":
                        nested_body = nested.get("body", {})
                        if isinstance(nested_body, dict) and nested_body.get("data"):
                            decoded = _decode_data(nested_body["data"])
                            if decoded:
                                return decoded

    # Fallback: try HTML if no plain text
    if isinstance(parts, list):
        for part in parts:
            if not isinstance(part, dict):
                continue
            mime_type = str(part.get("mimeType") or "").lower()
            if mime_type == "text/html":
                part_body = part.get("body", {})
                if isinstance(part_body, dict) and part_body.get("data"):
                    decoded = _decode_data(part_body["data"])
                    if decoded:
                        return _strip_html(decoded)

    # Final fallback to snippet
    return _extract_snippet(item)


def _extract_sender(item: Dict[str, Any]) -> str:
    """Extract full sender (name + email)."""
    return (
        _as_clean_str(item.get("from"))
        or _as_clean_str(item.get("From"))
        or _extract_header(item, "From")
    )


def _extract_from_domain(item: Dict[str, Any]) -> str:
    """Extract sender's domain."""
    raw_from = _extract_sender(item)
    if not raw_from:
        return ""
    emails = _EMAIL_RE.findall(raw_from)
    if emails:
        domain = emails[0].split("@", 1)[-1].lower().strip()
        return domain[:64]
    token = raw_from.strip().lower()
    token = re.sub(r"[^a-z0-9.-]+", "", token)
    if "." in token and len(token) <= 64:
        return token
    return ""


def _is_sensitive(text: str) -> bool:
    """Check if text contains sensitive PII content."""
    return bool(_SENSITIVE_RE.search(str(text or "").lower()))


def build_email_signals(
    *,
    threads: List[Dict[str, Any]],
    query: str,
    max_evidence: int,
    updated_at: str,
) -> Dict[str, Any]:
    """
    Build email signals from fetched threads.

    Returns structured email data with full content (no categorization).
    Processes sent and received emails separately to ensure we get up to 50 of each.
    """
    received_emails: List[Dict[str, Any]] = []
    sent_emails: List[Dict[str, Any]] = []
    domains: Dict[str, int] = {}

    # Calculate per-type limits (half of max_evidence each, defaulting to 50)
    per_type_limit = max(0, max_evidence) // 2 or 50

    # Track filtering stats for debugging
    sensitive_filtered = 0
    received_limit_filtered = 0
    sent_limit_filtered = 0

    for item in threads or []:
        if not isinstance(item, dict):
            continue

        subject = _extract_subject(item)
        body = _extract_body(item)
        sender = _extract_sender(item)
        from_domain = _extract_from_domain(item)

        # Check for sensitive content - skip if found
        combined_text = f"{subject} {body}".lower()
        if _is_sensitive(combined_text):
            sensitive_filtered += 1
            continue

        # Track domains
        if from_domain:
            domains[from_domain] = domains.get(from_domain, 0) + 1

        is_sent = item.get("is_sent", False)

        # Check per-type limits separately
        if is_sent and len(sent_emails) >= per_type_limit:
            sent_limit_filtered += 1
            continue
        if not is_sent and len(received_emails) >= per_type_limit:
            received_limit_filtered += 1
            continue

        # Build email entry with full content (truncated body)
        # Extract message_id for deduplication in database storage
        message_id = _as_clean_str(item.get("id")) or _as_clean_str(item.get("message_id")) or _as_clean_str(item.get("threadId"))

        # Scrub PII from body before storing
        scrubbed_body = _scrub_pii(body)

        email_entry = {
            "message_id": message_id,
            "sender": sender,
            "subject": subject,
            "body": _truncate(scrubbed_body, max_len=_MAX_BODY_CHARS),
            "is_sent": is_sent,
        }

        if is_sent:
            sent_emails.append(email_entry)
        else:
            received_emails.append(email_entry)

    # Combine: received first, then sent
    emails = received_emails + sent_emails

    # Log filtering stats for debugging
    logger.info(
        "[EMAIL_SIGNALS] processed %d threads: %d received + %d sent stored, "
        "filtered out: %d sensitive, %d over received limit, %d over sent limit",
        len(threads or []),
        len(received_emails),
        len(sent_emails),
        sensitive_filtered,
        received_limit_filtered,
        sent_limit_filtered,
    )

    # Build summary
    if emails:
        summary = f"found {len(received_emails)} received + {len(sent_emails)} sent emails"
        status = "ready"
    else:
        summary = "no recent emails found"
        status = "empty"

    # Sort domains by count
    top_domains = sorted(domains.items(), key=lambda x: -x[1])[:5]

    return {
        "status": status,
        "summary": summary,
        "emails": emails,  # Full email content (replaces old "evidence" format)
        "query": query,
        "threads_sampled": len(threads or []),
        "top_from_domains": [d for d, _ in top_domains],
        "updated_at": updated_at,
    }


def select_email_context_for_prompt(
    *,
    signals: Any,
    user_message: Optional[str],
    stage: str,
    max_evidence: int = 8,
) -> Dict[str, Any]:
    """
    Select and format email context for LLM prompts.

    Returns email data ready for prompt injection.
    """
    if not isinstance(signals, dict):
        return {}

    status = _as_clean_str(signals.get("status")).lower()
    if status != "ready":
        return {}

    emails = signals.get("emails") if isinstance(signals.get("emails"), list) else []

    # Shuffle emails to provide variety across turns
    if emails:
        emails = list(emails)  # Copy to avoid mutating original
        random.shuffle(emails)

    # Select top N emails (now randomized)
    selected_emails = emails[:max(0, int(max_evidence or 0))]

    return {
        "status": status,
        "summary": _as_clean_str(signals.get("summary")),
        "emails": selected_emails,
        "updated_at": _as_clean_str(signals.get("updated_at")),
    }


def _extract_keywords_from_context(
    career_interests: List[str],
    user_need: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """
    Extract relevant search keywords from user's stated interests and needs.

    Maps career interests to relevant email keywords and includes
    targets from user_need if available.

    Args:
        career_interests: List of user's career interests
        user_need: Optional dict with targets, outcomes from need stage

    Returns:
        List of unique keywords to search for in emails
    """
    keywords = set()

    # Map career interests to keywords
    for interest in (career_interests or []):
        interest_lower = str(interest).lower().strip()
        if not interest_lower:
            continue

        # Direct match
        if interest_lower in CAREER_TO_KEYWORDS:
            keywords.update(CAREER_TO_KEYWORDS[interest_lower])
            continue

        # Partial match - check if interest contains or is contained in a key
        for key, kw_list in CAREER_TO_KEYWORDS.items():
            if key in interest_lower or interest_lower in key:
                keywords.update(kw_list)
                break
        else:
            # No match found - use the interest itself as a keyword
            keywords.add(interest_lower)

    # Extract keywords from user_need targets and outcomes
    if isinstance(user_need, dict):
        targets = user_need.get("targets") if isinstance(user_need.get("targets"), list) else []
        for target in targets:
            target_str = str(target).lower().strip()
            if target_str:
                # Check if target maps to a career category
                for key, kw_list in CAREER_TO_KEYWORDS.items():
                    if key in target_str or target_str in key:
                        keywords.update(kw_list[:3])  # Add top 3 keywords
                        break
                else:
                    # Add target as keyword directly
                    keywords.add(target_str)

        outcomes = user_need.get("outcomes") if isinstance(user_need.get("outcomes"), list) else []
        for outcome in outcomes:
            outcome_str = str(outcome).lower().strip()
            # Extract meaningful words from outcomes
            for word in outcome_str.split():
                if len(word) > 3 and word not in {"want", "need", "like", "with", "from", "that", "this", "them"}:
                    keywords.add(word)

    # Return as sorted list, limited to avoid overly broad searches
    return sorted(list(keywords))[:20]


async def select_relevant_emails(
    *,
    user_id: str,
    career_interests: List[str],
    user_need: Optional[Dict[str, Any]] = None,
    stage: str,
    max_emails: int = 8,
) -> Dict[str, Any]:
    """
    Select emails most relevant to user's context.

    Uses Zep graph search for semantic relevance (primary),
    falls back to keyword-based Supabase filtering if Zep unavailable.

    Args:
        user_id: The user's ID
        career_interests: User's stated career interests
        user_need: Optional dict with targets/outcomes from need stage
        stage: Current stage ("need" or "value")
        max_emails: Maximum emails to return

    Returns:
        Dict with status, summary, emails, and keywords_used
    """
    now = datetime.utcnow().isoformat()

    if not str(user_id or "").strip():
        return {
            "status": "empty",
            "summary": "no email context available",
            "emails": [],
            "keywords_used": [],
            "updated_at": now,
        }

    # Extract keywords from user context (used for both Zep query and Supabase fallback)
    keywords = _extract_keywords_from_context(career_interests, user_need)

    # Try Zep graph search first if enabled
    if settings.zep_graph_enabled:
        zep_result = await _search_emails_via_zep(
            user_id=user_id,
            career_interests=career_interests,
            user_need=user_need,
            keywords=keywords,
            max_emails=max_emails,
        )
        if zep_result and zep_result.get("status") == "ready" and zep_result.get("emails"):
            logger.debug(
                "[EMAIL_CONTEXT] Zep graph search found %d emails for user=%s",
                len(zep_result.get("emails", [])),
                user_id[:8] if user_id else "?",
            )
            return zep_result

    # Fallback to Supabase keyword-based filtering
    return await _select_emails_via_supabase(
        user_id=user_id,
        keywords=keywords,
        max_emails=max_emails,
        now=now,
    )


async def _search_emails_via_zep(
    *,
    user_id: str,
    career_interests: List[str],
    user_need: Optional[Dict[str, Any]],
    keywords: List[str],
    max_emails: int,
) -> Optional[Dict[str, Any]]:
    """
    Search user's Zep knowledge graph for relevant email context.

    Args:
        user_id: User identifier
        career_interests: User's career interests
        user_need: User's stated need/goal
        keywords: Extracted keywords from context
        max_emails: Maximum emails to return

    Returns:
        Dict with emails and metadata, or None if unavailable
    """
    try:
        from app.integrations.zep_graph_client import get_zep_graph_client

        zep = get_zep_graph_client()
        if not zep.is_graph_enabled():
            return None

        # Build semantic query from user context
        query_parts = []

        if career_interests:
            query_parts.append(f"Professional interests: {', '.join(career_interests[:5])}")

        if user_need:
            targets = user_need.get("targets") or []
            outcomes = user_need.get("outcomes") or []
            if targets:
                query_parts.append(f"Looking for: {', '.join(targets[:3])}")
            if outcomes:
                query_parts.append(f"Goals: {', '.join(outcomes[:3])}")

        if not query_parts and keywords:
            query_parts.append(f"Topics: {', '.join(keywords[:8])}")

        if not query_parts:
            return None

        query = ". ".join(query_parts)

        # Search the graph
        results = await zep.search_graph(
            user_id=user_id,
            query=query,
            scope="edges",
            limit=max_emails * 2,  # Over-fetch for filtering
        )

        if not results:
            return None

        # Transform graph edges to email-like format
        emails = []
        for result in results:
            fact = result.fact
            if not fact:
                continue

            # Parse email information from fact if it looks like email content
            if "email" in fact.lower() or "subject:" in fact.lower():
                email_dict = _parse_email_from_fact(fact, result)
                if email_dict:
                    emails.append(email_dict)

        if not emails:
            return None

        # Shuffle for variety
        random.shuffle(emails)
        emails = emails[:max_emails]

        return {
            "status": "ready",
            "summary": f"found {len(emails)} relevant emails via semantic search",
            "emails": emails,
            "keywords_used": keywords[:10],
            "source": "zep_graph",
            "updated_at": datetime.utcnow().isoformat(),
        }

    except Exception as e:
        logger.debug("[EMAIL_CONTEXT] Zep graph search failed: %s", e, exc_info=True)
        return None


def _parse_email_from_fact(fact: str, result: Any) -> Optional[Dict[str, Any]]:
    """
    Parse email information from a Zep graph fact.

    Args:
        fact: The fact string from the graph
        result: The GraphSearchResult object

    Returns:
        Email dict or None if not parseable as email
    """
    lines = fact.strip().split("\n")

    email_dict: Dict[str, Any] = {
        "body": "",
        "subject": "",
        "sender": "",
        "sender_domain": "",
        "is_sent": False,
        "relevance_score": getattr(result, "score", 0.5),
    }

    for line in lines:
        line = line.strip()
        lower = line.lower()

        if lower.startswith("email") and ("from" in lower or "sent" in lower):
            # Parse "Email (Received) from domain.com on 2024-01-15:"
            if "sent" in lower:
                email_dict["is_sent"] = True
            # Extract domain
            if "from" in lower:
                parts = line.split("from")
                if len(parts) > 1:
                    domain_part = parts[1].strip().split()[0] if parts[1].strip() else ""
                    email_dict["sender_domain"] = domain_part.rstrip(":").strip()
                    email_dict["sender"] = domain_part.rstrip(":").strip()

        elif lower.startswith("subject:"):
            email_dict["subject"] = line[8:].strip()

        elif lower.startswith("content:"):
            email_dict["body"] = line[8:].strip()

    # Only return if we have meaningful content
    if email_dict.get("subject") or email_dict.get("body"):
        return email_dict

    return None


async def _select_emails_via_supabase(
    *,
    user_id: str,
    keywords: List[str],
    max_emails: int,
    now: str,
) -> Dict[str, Any]:
    """
    Fallback: Select emails using Supabase keyword-based filtering.

    Args:
        user_id: User identifier
        keywords: Keywords for filtering
        max_emails: Maximum emails to return
        now: Current timestamp

    Returns:
        Dict with emails and metadata
    """
    from app.database.client import DatabaseClient

    try:
        db = DatabaseClient()

        # Use filtered query if we have keywords, otherwise fall back to regular query
        if keywords:
            emails = await db.get_filtered_user_emails(
                user_id,
                keywords=keywords,
                exclude_sender_patterns=EXCLUDE_SENDER_PATTERNS,
                limit=max_emails,
            )
        else:
            # Fall back to regular query with exclusion filtering
            emails = await db.get_user_emails(user_id, limit=max_emails * 2)
            # Filter out notification emails
            filtered = []
            for email in emails:
                sender = (email.get("sender") or "").lower()
                sender_domain = (email.get("sender_domain") or "").lower()
                exclude = False
                for pattern in EXCLUDE_SENDER_PATTERNS:
                    if pattern.lower() in sender or pattern.lower() in sender_domain:
                        exclude = True
                        break
                if not exclude:
                    filtered.append(email)
            emails = filtered[:max_emails]

        if not emails:
            return {
                "status": "empty",
                "summary": "no relevant emails found",
                "emails": [],
                "keywords_used": keywords,
                "updated_at": now,
            }

        # Shuffle emails to ensure variety across turns
        emails_copy = list(emails)
        random.shuffle(emails_copy)

        return {
            "status": "ready",
            "summary": f"found {len(emails_copy)} relevant emails matching interests",
            "emails": emails_copy,
            "keywords_used": keywords[:10],
            "source": "supabase",
            "updated_at": now,
        }

    except Exception as exc:
        logger.debug("[EMAIL_CONTEXT] Supabase email selection failed: %s", exc, exc_info=True)
        return {
            "status": "error",
            "summary": "couldn't load emails",
            "emails": [],
            "keywords_used": keywords,
            "updated_at": now,
        }


def _parse_iso8601(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def should_refresh_email_signals(existing: Any, *, refresh_days: int) -> bool:
    if not isinstance(existing, dict):
        return True
    status = _as_clean_str(existing.get("status")).lower()
    if status in {"error", "unavailable"}:
        return True
    updated_at = _as_clean_str(existing.get("updated_at"))
    if not updated_at:
        return True
    parsed = _parse_iso8601(updated_at)
    if not parsed:
        return True
    age = datetime.utcnow() - parsed
    return age > timedelta(days=max(1, int(refresh_days or _DEFAULT_REFRESH_DAYS)))


async def fetch_email_signals(*, user_id: str, query: Optional[str] = None) -> Dict[str, Any]:
    """
    Fetch email signals from Composio Gmail.

    Fetches both received and sent emails in parallel for comprehensive context.

    Args:
        user_id: The user's ID for Composio
        query: Optional Gmail search query (defaults to newer_than:90d)
    """
    final_query = query or _as_clean_str(getattr(settings, "email_context_query", "")) or _DEFAULT_QUERY
    max_evidence_raw = getattr(settings, "email_context_max_evidence", None)

    # Use new separate limits for received and sent emails
    received_limit = _DEFAULT_RECEIVED_LIMIT  # 50
    sent_limit = _DEFAULT_SENT_LIMIT  # 50

    try:
        max_evidence = int(max_evidence_raw) if max_evidence_raw is not None else _DEFAULT_MAX_EVIDENCE
    except Exception:
        max_evidence = _DEFAULT_MAX_EVIDENCE

    max_evidence = max(0, min(100, max_evidence))  # Allow up to 100 evidence emails (50 received + 50 sent)

    now = datetime.utcnow().isoformat()

    if not str(user_id or "").strip():
        return {
            "status": "error",
            "summary": "can't load emails yet",
            "emails": [],
            "query": final_query,
            "threads_sampled": 0,
            "top_from_domains": [],
            "updated_at": now,
            "error": "missing_user_id",
        }

    composio = ComposioClient()
    if not composio.is_available():
        logger.warning("[EMAIL_CONTEXT] Composio client not available")
        return {
            "status": "unavailable",
            "summary": "email context unavailable right now",
            "emails": [],
            "query": final_query,
            "threads_sampled": 0,
            "top_from_domains": [],
            "updated_at": now,
            "error": "composio_unavailable",
        }

    try:
        # Get connected account ID (required for SDK v0.10+)
        logger.info("[EMAIL_CONTEXT] Fetching connected account ID for user %s", user_id)
        connected_account_id = await composio.get_connected_account_id(user_id=user_id)
        logger.info("[EMAIL_CONTEXT] Got connected account ID: %s", connected_account_id)

        if not connected_account_id:
            logger.warning("[EMAIL_CONTEXT] No connected account found for user %s", user_id)
            return {
                "status": "error",
                "summary": "gmail not connected",
                "emails": [],
                "query": final_query,
                "threads_sampled": 0,
                "top_from_domains": [],
                "updated_at": now,
                "error": "no_connected_account",
            }

        # Build queries for received and sent emails
        received_query = final_query  # e.g., "newer_than:90d"
        sent_query = f"in:sent {final_query}"  # e.g., "in:sent newer_than:90d"

        # Fetch received and sent emails in parallel
        received_task = asyncio.create_task(
            composio.fetch_recent_threads(
                user_id=user_id,
                connected_account_id=connected_account_id,
                query=received_query,
                limit=received_limit,
            )
        )
        sent_task = asyncio.create_task(
            composio.fetch_recent_threads(
                user_id=user_id,
                connected_account_id=connected_account_id,
                query=sent_query,
                limit=sent_limit,
            )
        )

        # Wait for both with timeout
        results = await asyncio.wait_for(
            asyncio.gather(received_task, sent_task, return_exceptions=True),
            timeout=20.0,
        )

        received_emails = results[0] if not isinstance(results[0], Exception) else []
        sent_emails = results[1] if not isinstance(results[1], Exception) else []

        if isinstance(results[0], Exception):
            logger.warning("[EMAIL_CONTEXT] received emails fetch failed: %s", results[0])
        if isinstance(results[1], Exception):
            logger.warning("[EMAIL_CONTEXT] sent emails fetch failed: %s", results[1])

        # Tag sent emails with is_sent=True for storage differentiation
        for email in (sent_emails or []):
            email["is_sent"] = True

        # Combine and deduplicate by message_id
        all_threads = []
        seen_ids = set()
        for thread in list(received_emails or []) + list(sent_emails or []):
            msg_id = thread.get("id") or thread.get("message_id")
            if msg_id and msg_id not in seen_ids:
                seen_ids.add(msg_id)
                all_threads.append(thread)
            elif not msg_id:
                # Include emails without IDs (shouldn't happen often)
                all_threads.append(thread)

        logger.info(
            "[EMAIL_CONTEXT] fetched %d received + %d sent = %d unique emails for user %s",
            len(received_emails or []),
            len(sent_emails or []),
            len(all_threads),
            user_id,
        )

    except asyncio.TimeoutError:
        logger.warning("[EMAIL_CONTEXT] fetch threads timed out for user %s", user_id)
        return {
            "status": "error",
            "summary": "couldn't read emails yet",
            "emails": [],
            "query": final_query,
            "threads_sampled": 0,
            "top_from_domains": [],
            "updated_at": now,
            "error": "fetch_timeout",
        }
    except Exception as exc:
        logger.warning("[EMAIL_CONTEXT] fetch threads failed: %s", exc, exc_info=True)
        return {
            "status": "error",
            "summary": "couldn't read emails yet",
            "emails": [],
            "query": final_query,
            "threads_sampled": 0,
            "top_from_domains": [],
            "updated_at": now,
            "error": "fetch_failed",
        }

    return build_email_signals(
        threads=all_threads,
        query=final_query,
        max_evidence=max_evidence,
        updated_at=now,
    )


def has_topical_overlap(email: Dict[str, str], user_message: str) -> bool:
    """
    Check if email content relates to user's current message.

    Uses semantic keyword expansion to catch related concepts
    (e.g., "investors" matches emails about "funding", "vc", "raise").

    Args:
        email: Dict with sender, subject, body fields
        user_message: The user's current message

    Returns:
        True if meaningful overlap exists between email and message
    """
    msg_lower = user_message.lower()
    # Extract meaningful words (3+ chars) from user message - reduced from 4 for better matching
    msg_words = set(word for word in msg_lower.split() if len(word) >= 3)

    # Common words to exclude from overlap detection
    stopwords = {
        'that', 'this', 'with', 'from', 'have', 'been', 'what', 'your',
        'they', 'them', 'their', 'there', 'here', 'where', 'when', 'will',
        'would', 'could', 'should', 'about', 'just', 'like', 'know', 'want',
        'some', 'also', 'more', 'very', 'really', 'looking', 'trying',
        'people', 'thing', 'things', 'work', 'working', 'help', 'helping'
    }
    msg_words = msg_words - stopwords

    # Expand message words with semantic equivalents
    msg_words_expanded = expand_keywords(msg_words)

    # Extract words from email fields
    sender = _as_clean_str(email.get("sender", "")).lower()
    subject = _as_clean_str(email.get("subject", "")).lower()
    body = _as_clean_str(email.get("body", "")).lower()
    sender_domain = _as_clean_str(email.get("sender_domain", "")).lower()

    email_words = set()
    for text in [sender, subject, body]:
        email_words.update(word for word in text.split() if len(word) >= 3)
    email_words = email_words - stopwords

    # Expand email words with semantic equivalents
    email_words_expanded = expand_keywords(email_words)

    # Check for meaningful overlap using expanded sets
    overlap = msg_words_expanded & email_words_expanded

    # Also check if email is from a notable company - always relevant for name-dropping
    if sender_domain in NOTABLE_COMPANY_DOMAINS:
        return True

    # Check if any notable company domain appears in sender
    for domain in NOTABLE_COMPANY_DOMAINS:
        if domain in sender:
            return True

    # Require at least 1 meaningful word overlap
    return len(overlap) >= 1


def check_user_message_specificity(user_message: str) -> bool:
    """
    Check if user's message contains specific, concrete details.

    Used to determine if we should skip email context and focus
    on acknowledging the user's specific answer instead.

    Args:
        user_message: The user's current message

    Returns:
        True if the message contains specific details (numbers, action verbs, etc.)
    """
    import re

    msg_lower = user_message.lower()

    # Strong specificity indicators (any 1 of these = specific)
    strong_indicators = [
        bool(re.search(r'\d+', user_message)),  # Contains numbers (users, revenue, etc.)
        'http' in msg_lower or 'https' in msg_lower,  # Contains URL
    ]

    # If any strong indicator, check for action verb to confirm
    action_verbs = [
        'built', 'shipped', 'launched', 'created', 'founded', 'started',
        'developed', 'designed', 'led', 'managed', 'grew', 'raised',
        'sold', 'acquired', 'published', 'presented', 'worked'
    ]
    has_action_verb = any(word in msg_lower for word in action_verbs)

    # Strong indicator + action verb = definitely specific
    if any(strong_indicators) and has_action_verb:
        return True

    # Number alone with reasonable length = specific
    if bool(re.search(r'\d+', user_message)) and len(user_message) > 30:
        return True

    # Multiple action verbs + decent length = specific
    action_count = sum(1 for word in action_verbs if word in msg_lower)
    if action_count >= 2 and len(user_message) > 50:
        return True

    return False


async def load_email_signals_from_db(user_id: str) -> Dict[str, Any]:
    """
    Load email signals from user_emails database table.

    This is a fast alternative to ensure_email_signals() that reads from the
    database instead of fetching from Composio. Use this in need/value stages
    to avoid the 15s timeout on every turn.

    Args:
        user_id: The user's ID

    Returns:
        Dict with status, summary, and emails list (same format as build_email_signals)
    """
    from app.database.client import DatabaseClient

    now = datetime.utcnow().isoformat()

    if not str(user_id or "").strip():
        return {
            "status": "empty",
            "summary": "no email context available",
            "emails": [],
            "updated_at": now,
        }

    try:
        db = DatabaseClient()
        emails = await db.get_user_emails(user_id, limit=20)

        if not emails:
            return {
                "status": "empty",
                "summary": "no emails stored",
                "emails": [],
                "updated_at": now,
            }

        return {
            "status": "ready",
            "summary": f"found {len(emails)} emails from user's inbox",
            "emails": emails,
            "updated_at": now,
        }

    except Exception as exc:
        logger.debug("[EMAIL_CONTEXT] load from DB failed: %s", exc, exc_info=True)
        return {
            "status": "error",
            "summary": "couldn't load emails",
            "emails": [],
            "updated_at": now,
        }


async def ensure_email_signals(
    *,
    personal_facts: Dict[str, Any],
    user_id: str,
    query: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Ensure email signals are fresh, fetching if needed.

    Args:
        personal_facts: User's personal facts dict (will be updated)
        user_id: The user's ID for Composio
        query: Optional Gmail search query for contextual search
    """
    refresh_days_raw = getattr(settings, "email_context_refresh_days", None)
    try:
        refresh_days = int(refresh_days_raw) if refresh_days_raw is not None else _DEFAULT_REFRESH_DAYS
    except Exception:
        refresh_days = _DEFAULT_REFRESH_DAYS

    existing = personal_facts.get("email_signals") if isinstance(personal_facts, dict) else None

    # Always refresh if a custom query is provided (contextual search)
    if query:
        signals = await fetch_email_signals(user_id=user_id, query=query)
        personal_facts["email_signals"] = signals
        return signals

    if not should_refresh_email_signals(existing, refresh_days=refresh_days):
        return existing

    signals = await fetch_email_signals(user_id=user_id)
    personal_facts["email_signals"] = signals
    return signals


# =============================================================================
# SENT EMAIL ANALYSIS FOR PROFESSIONAL NEEDS & VALUE
# =============================================================================

# Professional needs detection patterns (signals from user's SENT emails)
NEED_PATTERNS = {
    "seeking_investors": {
        "keywords": ["pitch", "deck", "raising", "funding", "investor", "vc", "venture", "seed", "series", "term sheet", "valuation", "cap table"],
        "phrases": [
            r"would love to (pitch|share|show) you",
            r"raising (a |our )?(\$?\d+[mk]?|seed|series)",
            r"looking for (investors|funding|capital)",
            r"intro to.*investors?",
            r"warm intro to",
        ],
        "hook": "you've been reaching out to investors",
    },
    "seeking_cofounders": {
        "keywords": ["cofounder", "co-founder", "founding team", "join me", "building something", "partner up", "technical founder"],
        "phrases": [
            r"looking for (a )?co-?founder",
            r"join (me|us) as",
            r"building something.*together",
            r"need a technical.*founder",
        ],
        "hook": "you're looking for a co-founder",
    },
    "job_hunting": {
        "keywords": ["application", "applying", "resume", "cv", "job", "position", "role", "opportunity", "interview"],
        "phrases": [
            r"applying (for|to)",
            r"interested in (the|this) (role|position)",
            r"attached (is )?my resume",
            r"available for.*interview",
            r"looking for (a )?(new )?(role|job|opportunity)",
        ],
        "hook": "you're looking for a new role",
    },
    "seeking_mentorship": {
        "keywords": ["advice", "guidance", "mentor", "learn from", "pick your brain", "coffee chat", "office hours"],
        "phrases": [
            r"would love (your|to get your) (advice|thoughts|input)",
            r"could (you|i) pick your brain",
            r"learn from (you|your experience)",
            r"seeking (guidance|mentorship)",
        ],
        "hook": "you've been seeking advice from people",
    },
    "seeking_clients": {
        "keywords": ["proposal", "quote", "pricing", "services", "deliverables", "scope", "engagement"],
        "phrases": [
            r"attached.*proposal",
            r"happy to (discuss|share).*pricing",
            r"(our|my) services can help",
            r"looking for (new )?clients",
        ],
        "hook": "you're looking for clients",
    },
}

# Professional value detection patterns (what user offers based on SENT emails)
VALUE_PATTERNS = {
    "gives_advice": {
        "keywords": ["suggestion", "recommend", "my advice", "from my experience", "tip", "here's what"],
        "phrases": [
            r"my (suggestion|recommendation) would be",
            r"(i |i'd )suggest",
            r"from my experience",
            r"here's what (i think|worked for me)",
            r"you (should|might want to)",
        ],
        "hook": "you give advice to people",
    },
    "makes_intros": {
        "keywords": ["intro", "introduce", "connect you", "loop in", "cc'ing", "meet"],
        "phrases": [
            r"happy to intro(duce)?",
            r"(i'll |let me )connect you",
            r"loop(ing)? (you|them) in",
            r"cc'ing.*you should",
            r"you (two|both) should (meet|connect)",
        ],
        "hook": "you make intros for people",
    },
    "technical_expertise": {
        "keywords": ["architecture", "implementation", "code", "system", "algorithm", "infrastructure", "debugging", "deploy", "built"],
        "phrases": [
            r"the (architecture|approach) (i|we) (used|recommend)",
            r"(here's|this is) how (i|we) (built|implemented|solved)",
            r"technical(ly)?.*you (should|could)",
        ],
        "hook": "you have technical expertise",
    },
    "industry_knowledge": {
        "keywords": ["market", "industry", "trend", "landscape", "competitive", "benchmark", "insight"],
        "phrases": [
            r"in (my|the) experience.*(industry|market)",
            r"the (market|landscape) (is|has been)",
            r"from what (i've|i) (seen|observed)",
            r"(key|major) (trend|shift|change)",
        ],
        "hook": "you have industry knowledge",
    },
}


def _match_patterns(text: str, patterns: Dict[str, Any]) -> tuple[int, List[str]]:
    """
    Match text against keyword and phrase patterns.

    Returns:
        Tuple of (score, list of evidence snippets)
    """
    import re

    text_lower = text.lower()
    score = 0
    evidence = []

    # Check keywords (2+ hits = signal)
    keyword_hits = sum(1 for kw in patterns.get("keywords", []) if kw in text_lower)
    if keyword_hits >= 2:
        score += keyword_hits

    # Check phrase patterns (1 hit = strong signal)
    for phrase in patterns.get("phrases", []):
        match = re.search(phrase, text_lower, re.IGNORECASE)
        if match:
            score += 3  # Phrase match is stronger
            # Extract context around match for evidence
            start = max(0, match.start() - 30)
            end = min(len(text), match.end() + 30)
            snippet = text[start:end].strip()
            if snippet and len(snippet) > 10:
                evidence.append(f"...{snippet}...")

    return score, evidence[:2]  # Max 2 evidence snippets


async def analyze_sent_emails_for_signals(user_id: str) -> Dict[str, Any]:
    """
    Analyze user's SENT emails to detect professional needs and value signals.

    This function retrieves sent emails from the database and runs fast
    rule-based pattern matching to identify what the user needs and what
    value they offer to others.

    Args:
        user_id: The user's ID

    Returns:
        Dict with:
            - top_needs: List of top detected needs (e.g., ["seeking_investors"])
            - top_values: List of top detected values (e.g., ["makes_intros"])
            - conversation_hooks: Natural phrases Frank can use to reference insights
            - need_evidence: Dict mapping need types to evidence snippets
            - value_evidence: Dict mapping value types to evidence snippets
    """
    from app.database.client import DatabaseClient

    empty_result = {
        "top_needs": [],
        "top_values": [],
        "conversation_hooks": [],
        "need_evidence": {},
        "value_evidence": {},
    }

    if not str(user_id or "").strip():
        return empty_result

    try:
        db = DatabaseClient()
        sent_emails = await db.get_user_sent_emails(user_id, limit=30)

        if not sent_emails or len(sent_emails) < 2:
            logger.debug("[SENT_ANALYSIS] Insufficient sent emails for user %s: %d", user_id, len(sent_emails or []))
            return empty_result

        # Analyze needs
        need_scores: Dict[str, int] = {}
        need_evidence: Dict[str, List[str]] = {}

        for email in sent_emails:
            subject = _as_clean_str(email.get("subject", ""))
            body = _as_clean_str(email.get("body", ""))
            combined = f"{subject} {body}"

            for need_type, patterns in NEED_PATTERNS.items():
                score, evidence = _match_patterns(combined, patterns)
                if score > 0:
                    need_scores[need_type] = need_scores.get(need_type, 0) + score
                    if evidence and need_type not in need_evidence:
                        need_evidence[need_type] = evidence

        # Analyze values
        value_scores: Dict[str, int] = {}
        value_evidence: Dict[str, List[str]] = {}

        for email in sent_emails:
            subject = _as_clean_str(email.get("subject", ""))
            body = _as_clean_str(email.get("body", ""))
            combined = f"{subject} {body}"

            for value_type, patterns in VALUE_PATTERNS.items():
                score, evidence = _match_patterns(combined, patterns)
                if score > 0:
                    value_scores[value_type] = value_scores.get(value_type, 0) + score
                    if evidence and value_type not in value_evidence:
                        value_evidence[value_type] = evidence

        # Get top needs and values (sorted by score, min threshold of 3)
        top_needs = sorted(
            [n for n, s in need_scores.items() if s >= 3],
            key=lambda x: -need_scores[x]
        )[:2]

        top_values = sorted(
            [v for v, s in value_scores.items() if s >= 3],
            key=lambda x: -value_scores[x]
        )[:2]

        # Generate conversation hooks
        conversation_hooks = []
        for need in top_needs:
            hook = NEED_PATTERNS.get(need, {}).get("hook")
            if hook:
                conversation_hooks.append(hook)
        for value in top_values:
            hook = VALUE_PATTERNS.get(value, {}).get("hook")
            if hook:
                conversation_hooks.append(hook)

        logger.info(
            "[SENT_ANALYSIS] user %s: needs=%s, values=%s",
            user_id,
            top_needs,
            top_values
        )

        return {
            "top_needs": top_needs,
            "top_values": top_values,
            "conversation_hooks": conversation_hooks[:3],
            "need_evidence": {k: v for k, v in need_evidence.items() if k in top_needs},
            "value_evidence": {k: v for k, v in value_evidence.items() if k in top_values},
        }

    except Exception as exc:
        logger.warning("[SENT_ANALYSIS] Failed for user %s: %s", user_id, exc, exc_info=True)
        return empty_result


# ---------------------------------------------------------------------------
# LLM-Based Email Analysis
# ---------------------------------------------------------------------------

_LLM_ANALYSIS_SYSTEM_PROMPT = """You are analyzing a professional's sent emails to understand who they are and what they're working on.

Your goal is to extract SPECIFIC, CONCRETE details that show you actually understand this person - not generic summaries.

Look for:
- What company/project are they working on? What does it do?
- Who are they reaching out to and why? (investors, recruiters, clients, etc.)
- What specific expertise do they demonstrate? (technologies, industries, skills)
- What stage are they at? (fundraising, job hunting, scaling, launching, etc.)
- What relationships do they have? (do they make intros, give advice, mentor others?)

Be SPECIFIC. Extract names of companies, technologies, industries, roles - anything that shows real understanding.
Never quote email content directly. Summarize and synthesize.
Return valid JSON only."""

_LLM_ANALYSIS_USER_PROMPT = """Here are {email_count} recent SENT emails from this person:

{formatted_emails}

Analyze these emails and return a JSON object with exactly these fields:
{{
  "primary_need": "specific description of what they need - include details like industry, stage, type of people they're seeking. e.g. 'looking for Series A investors in the fintech space' not just 'seeking funding'",
  "need_specific_details": ["2-3 facts about their NEEDS/GOALS - what they're seeking, who they're reaching out to, what they want. Focus on their outreach patterns and asks."],
  "secondary_needs": ["other specific needs, up to 2"],
  "primary_value": "specific description of what they offer - include their expertise area, what they've built, who they know. e.g. 'has deep connections in the YC network and makes warm intros to founders' not just 'makes intros'",
  "value_specific_details": ["2-3 facts about their VALUE/ACHIEVEMENTS - what they've built, shipped, accomplished, their skills. Focus on their experience and results."],
  "secondary_values": ["other specific values, up to 2"],
  "professional_context": "specific details about who they are - their role, company, industry, what they're building. e.g. 'founder of a B2B payments startup, previously at Stripe, currently raising seed round'",
  "specific_details": ["3-5 specific facts you learned - company names, technologies, industries, people types they interact with. These should be things that would make them feel understood."],
  "conversation_hooks": ["2-3 casual ways to reference what you learned WITHOUT quoting emails. e.g. 'looks like you're deep in the payments space' or 'seems like you know a lot of folks in the YC world'"],
  "confidence": "high/medium/low"
}}

CRITICAL: need_specific_details and value_specific_details MUST be DIFFERENT facts:
- need_specific_details = their GOALS, what they're SEEKING, who they're REACHING OUT TO
- value_specific_details = their ACHIEVEMENTS, what they've BUILT, their SKILLS/EXPERIENCE

Don't put the same facts in both. If they "closed partnerships with Penn and Duke" that's a VALUE (achievement). If they "want to meet VCs" that's a NEED (goal).

Be specific enough that the person would think "wow, they actually looked at my emails and understand what I do" - not generic labels."""


def _format_emails_for_llm(emails: List[Dict[str, Any]], max_emails: int = 15) -> str:
    """Format emails for LLM consumption, respecting token limits."""
    formatted = []
    for i, email in enumerate(emails[:max_emails]):
        subject = _as_clean_str(email.get("subject", ""))[:100]
        body = _as_clean_str(email.get("body", ""))[:400]
        recipient = _as_clean_str(email.get("recipient", email.get("sender", "")))[:50]
        formatted.append(f"Email {i+1}:\nTo: {recipient}\nSubject: {subject}\nBody: {body}")
    return "\n\n---\n\n".join(formatted)


async def analyze_sent_emails_with_llm(user_id: str) -> Dict[str, Any]:
    """
    Analyze user's SENT emails using LLM to detect professional needs and values.

    Uses gpt-4o-mini for fast, cost-effective analysis (~$0.001 per call).
    Falls back to pattern-based analysis on error.

    Args:
        user_id: The user's ID

    Returns:
        Dict with:
            - primary_need: Main thing they're seeking
            - secondary_needs: Other needs detected
            - primary_value: Main value they offer
            - secondary_values: Other values they offer
            - professional_context: Brief professional description
            - conversation_hooks: Natural ways to reference insights
            - confidence: high/medium/low
            - top_needs, top_values, need_evidence, value_evidence (backward compatible)
    """
    from app.database.client import DatabaseClient
    from app.integrations.azure_openai_client import AzureOpenAIClient
    import json

    empty_result = {
        "primary_need": "",
        "need_specific_details": [],  # Stage-specific: for needs_eval
        "secondary_needs": [],
        "primary_value": "",
        "value_specific_details": [],  # Stage-specific: for value_eval
        "secondary_values": [],
        "professional_context": "",
        "specific_details": [],  # Kept for backward compatibility
        "conversation_hooks": [],
        "confidence": "none",
        # Backward compatible fields
        "top_needs": [],
        "top_values": [],
        "need_evidence": {},
        "value_evidence": {},
    }

    if not str(user_id or "").strip():
        return empty_result

    try:
        db = DatabaseClient()
        sent_emails = await db.get_user_sent_emails(user_id, limit=30)

        if not sent_emails or len(sent_emails) < 2:
            logger.debug("[LLM_ANALYSIS] Insufficient sent emails for user %s: %d", user_id, len(sent_emails or []))
            return empty_result

        # Format emails for LLM (limit to 15 for token efficiency)
        formatted_emails = _format_emails_for_llm(sent_emails, max_emails=15)
        email_count = min(len(sent_emails), 15)

        user_prompt = _LLM_ANALYSIS_USER_PROMPT.format(
            email_count=email_count,
            formatted_emails=formatted_emails,
        )

        # Call LLM
        async with AzureOpenAIClient() as client:
            response = await client.generate_response(
                system_prompt=_LLM_ANALYSIS_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                model="gpt-4o-mini",
                temperature=0.3,  # Lower temperature for more consistent JSON output
                trace_label="email_llm_analysis",
            )

        # Parse JSON response
        # Handle potential markdown code blocks
        response_text = response.strip()
        if response_text.startswith("```"):
            # Remove markdown code block
            lines = response_text.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            response_text = "\n".join(lines)

        result = json.loads(response_text)

        # Ensure all expected fields exist with defaults
        final_result = {
            "primary_need": result.get("primary_need", ""),
            "need_specific_details": result.get("need_specific_details", [])[:3],  # Stage-specific for needs_eval
            "secondary_needs": result.get("secondary_needs", [])[:2],
            "primary_value": result.get("primary_value", ""),
            "value_specific_details": result.get("value_specific_details", [])[:3],  # Stage-specific for value_eval
            "secondary_values": result.get("secondary_values", [])[:2],
            "professional_context": result.get("professional_context", ""),
            "specific_details": result.get("specific_details", [])[:5],  # Keep for backward compatibility
            "conversation_hooks": result.get("conversation_hooks", [])[:3],
            "confidence": result.get("confidence", "medium"),
            # Backward compatible empty fields
            "top_needs": [],
            "top_values": [],
            "need_evidence": {},
            "value_evidence": {},
        }

        logger.info(
            "[LLM_ANALYSIS] user %s: need=%s, value=%s, confidence=%s",
            user_id,
            final_result["primary_need"][:50] if final_result["primary_need"] else "none",
            final_result["primary_value"][:50] if final_result["primary_value"] else "none",
            final_result["confidence"],
        )

        return final_result

    except json.JSONDecodeError as e:
        logger.warning("[LLM_ANALYSIS] JSON parse error for user %s: %s", user_id, e)
        # Fall back to pattern matching
        return await analyze_sent_emails_for_signals(user_id)

    except Exception as exc:
        logger.warning("[LLM_ANALYSIS] Failed for user %s, falling back to pattern matching: %s", user_id, exc)
        # Fall back to pattern matching
        return await analyze_sent_emails_for_signals(user_id)
