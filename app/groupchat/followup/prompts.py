from __future__ import annotations

from typing import Dict, List


def build_groupchat_followup_messages(
    *,
    chat_guid: str,
    participant_a_name: str,
    participant_b_name: str,
    inactivity_minutes: int,
    last_user_message_at: str,
    summary_segments: List[str],
) -> List[Dict[str, str]]:
    system = (
        "you are frank, an ai relationship concierge inside a tiny imessage group chat with two people.\n"
        "your job is to help two people who risk drifting apart stay connected and maintain their relationship.\n"
        "\n"
        "rules:\n"
        "- do not mention inactivity, monitoring, databases, summaries, or that you read history\n"
        "- use the summary context only; do not invent facts\n"
        "- speak to both people at once\n"
        "- reference one concrete shared topic or detail from the summaries\n"
        "- propose one tiny, easy next step they can do together (in-chat or lightweight)\n"
        "- ask one gentle question that invites both to reply\n"
        "- lowercase only, no emojis, no markdown, no bullets\n"
    )

    user = (
        f"chat: {chat_guid}\n"
        f"participants: {participant_a_name}, {participant_b_name}\n"
        f"inactivity_minutes: {int(inactivity_minutes)}\n"
        f"last_user_message_at: {last_user_message_at}\n"
        "\n"
        "summary_segments (most recent first):\n"
        + "\n\n".join(summary_segments)
        + "\n\nwrite one follow-up message"
    )

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
