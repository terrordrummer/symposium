"""Local metadata for static visual agent identities.

These models are runtime/UI extensions, not additions to the frozen v1
JSON schemas. The viewer resolves a packaged portrait or renders a deterministic
initial-based identity card; it does not open remote rendering sessions.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal, Optional

VoicePresentation = Literal["feminine", "masculine"]


@dataclass(frozen=True, slots=True)
class AvatarProfile:
    """Public, non-secret visual identity metadata for one persona.

    ``portrait_asset`` is deliberately a package-relative path. Missing assets
    are represented locally by initials and a stable identity color.
    """

    persona_id: str
    display_name: str
    portrait_asset: Optional[str] = None
    synthetic: bool = True
    alt_text: Optional[str] = None
    asset_id: Optional[str] = None
    voice_presentation: Optional[VoicePresentation] = None
    voice_speaker: Optional[str] = None
    voice_description: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.persona_id.strip() or not self.display_name.strip():
            raise ValueError("persona_id and display_name must be non-empty")
        if self.portrait_asset is None:
            if self.asset_id is not None:
                raise ValueError("asset_id requires portrait_asset")
        else:
            asset = PurePosixPath(self.portrait_asset)
            if asset.is_absolute() or ".." in asset.parts or asset.parts[:1] != ("avatars",):
                raise ValueError("portrait_asset must be a relative path under avatars/")
        voice_fields = (
            self.voice_presentation,
            self.voice_speaker,
            self.voice_description,
        )
        if any(value is not None for value in voice_fields) and not all(
            value is not None for value in voice_fields
        ):
            raise ValueError("voice metadata must be provided together")

    def viewer_payload(self) -> dict[str, object]:
        """Return the safe metadata sent to the read-only browser viewer."""
        payload: dict[str, object] = {
            "profile_id": self.persona_id,
            "asset_id": self.asset_id,
            "display_name": self.display_name,
            "portrait_url": (
                f"/static/{self.portrait_asset}" if self.portrait_asset else None
            ),
            "synthetic": self.synthetic,
            "alt_text": self.alt_text or f"Synthetic portrait of {self.display_name}",
        }
        if self.voice_presentation is not None:
            payload["voice"] = {
                "engine": "parler-tts-mini-multilingual-v1.1",
                "language": "it-IT",
                "presentation": self.voice_presentation,
                "speaker": self.voice_speaker,
                "description": self.voice_description,
            }
        else:
            payload["voice"] = None
        return payload
