"""Static local identities for the Symposium 2.x viewer.

The v1 deliberation protocol and its persisted artifacts intentionally do
not know about presentation. This package maps persona IDs to packaged
portraits or to a zero-cost local fallback rendered by the browser.
"""

from symposium.avatars.catalog import (
    avatar_asset_paths,
    avatar_by_id,
    avatar_for,
    avatar_for_agent,
    avatar_pool,
)
from symposium.avatars.models import AvatarProfile, VoicePresentation

__all__ = [
    "AvatarProfile",
    "VoicePresentation",
    "avatar_asset_paths",
    "avatar_by_id",
    "avatar_for",
    "avatar_for_agent",
    "avatar_pool",
]
