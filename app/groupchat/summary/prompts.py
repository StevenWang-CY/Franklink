from __future__ import annotations

from typing import Dict, List, Optional


def build_groupchat_summary_messages(
    *,
    chat_guid: str,
    participant_a_name: str,
    participant_b_name: str,
    segment_start_at: Optional[str],
    segment_end_at: str,
    transcript_lines: List[str],
) -> List[Dict[str, str]]:
    system = (
        "You are a careful group chat summarizer for Franklink.\n"
        "You must only use information present in the transcript lines.\n"
        "Do not invent facts. If uncertain, omit.\n"
        "Output Markdown only (no JSON, no code fences).\n"
        "\n"
        "If the transcript looks truncated (starts mid-topic or missing context), add this as the first line:\n"
        "NOTE: Transcript window may be incomplete.\n"
        "\n"
        "Use exactly this template:\n"
        "## Topics\n"
        "- ...\n"
        "\n"
        "## Each Person\n"
        f"### {participant_a_name}\n"
        "- ...\n"
        f"### {participant_b_name}\n"
        "- ...\n"
        "\n"
        "## Agreements\n"
        "- ...\n"
        "\n"
        "## Disagreements\n"
        "- ...\n"
        "\n"
        "## Decisions\n"
        "- ...\n"
        "\n"
        "## Action Items\n"
        "- ...\n"
        "\n"
        "## Open Questions\n"
        "- ...\n"
        "\n"
        "## One-line Summary\n"
        "...\n"
    )

    start_line = segment_start_at or "(first segment)"
    user = (
        f"Chat: {chat_guid}\n"
        f"Segment: {start_line} -> {segment_end_at}\n"
        f"Participants: {participant_a_name}, {participant_b_name}\n"
        "\n"
        "Transcript:\n"
        + "\n".join(transcript_lines)
    )

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

