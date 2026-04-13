from __future__ import annotations

from dataclasses import dataclass

from app.database.client import DatabaseClient
from app.integrations.azure_openai_client import AzureOpenAIClient
from app.integrations.photon_client import PhotonClient
from app.groupchat.io.recorder import GroupChatRecorder
from app.groupchat.io.sender import GroupChatSender


@dataclass(frozen=True)
class GroupChatRuntimeDeps:
    db: DatabaseClient
    photon: PhotonClient
    openai: AzureOpenAIClient
    recorder: GroupChatRecorder
    sender: GroupChatSender
