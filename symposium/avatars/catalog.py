"""Packaged synthetic identities used by the static meeting viewer."""

from __future__ import annotations

from dataclasses import replace
from typing import Optional

from symposium.avatars.models import AvatarProfile, VoicePresentation


def _voice(
    presentation: VoicePresentation,
    character: str,
) -> tuple[str, str]:
    if presentation == "feminine":
        speaker = "Julia"
        pronoun = "She"
    else:
        speaker = "Richard"
        pronoun = "He"
    description = (
        f"{speaker}'s voice is {character}, with natural intonation. "
        f"{pronoun} speaks fluent Italian at a measured conversational pace. "
        "The recording is very clear, with no background noise."
    )
    return speaker, description


def _profile(
    persona_id: str,
    display_name: str,
    presentation: VoicePresentation,
    character: str,
    *,
    filename: Optional[str] = None,
) -> AvatarProfile:
    speaker, description = _voice(presentation, character)
    return AvatarProfile(
        persona_id=persona_id,
        display_name=display_name,
        portrait_asset=f"avatars/{filename or persona_id}.webp",
        synthetic=True,
        asset_id=persona_id,
        voice_presentation=presentation,
        voice_speaker=speaker,
        voice_description=description,
    )


_BUILT_INS = {
    "logician": _profile("logician", "Logician", "masculine", "calm, precise and adult"),
    "visionary": _profile("visionary", "Visionary", "feminine", "warm, imaginative and adult"),
    "researcher": _profile("researcher", "Researcher", "feminine", "composed, mature and clear"),
    "critic": _profile("critic", "Critic", "masculine", "firm, constructive and mature"),
    "engineer": _profile("engineer", "Engineer", "feminine", "pragmatic, confident and clear"),
    # The protocol id remains `coordinator`; Sartori is its product-facing
    # identity.  Keeping the ids separate avoids changing old run artifacts.
    "coordinator": _profile("coordinator", "Sartori", "masculine", "discreet, warm and mature"),
}


# Fifty reusable identities. Each asset is assigned at most once and the
# voice profile travels with the face, so visual and spoken identity remain
# stable across rooms and runs. The photographic subject directions are kept
# in docs/avatar-assets.md; this runtime catalog contains presentation data.
_POOL_SPECS: tuple[tuple[VoicePresentation, str], ...] = (
    ("feminine", "thoughtful, analytical and young-adult"),
    ("masculine", "friendly, pragmatic and adult"),
    ("feminine", "calm, authoritative and mature"),
    ("masculine", "energetic, clear and young-adult"),
    ("feminine", "warm, methodical and adult"),
    ("masculine", "measured, thoughtful and mature"),
    ("feminine", "confident, concise and adult"),
    ("masculine", "gentle, analytical and young-adult"),
    ("feminine", "observant, grounded and mature"),
    ("masculine", "direct, constructive and adult"),
    ("feminine", "bright, articulate and young-adult"),
    ("masculine", "calm, dependable and mature"),
    ("feminine", "precise, pragmatic and adult"),
    ("masculine", "warm, imaginative and adult"),
    ("feminine", "firm, thoughtful and mature"),
    ("masculine", "curious, clear and young-adult"),
    ("feminine", "measured, reassuring and adult"),
    ("masculine", "authoritative, composed and mature"),
    ("feminine", "energetic, practical and young-adult"),
    ("masculine", "attentive, concise and adult"),
    ("feminine", "calm, creative and mature"),
    ("masculine", "confident, analytical and adult"),
    ("feminine", "friendly, clear and young-adult"),
    ("masculine", "grounded, methodical and mature"),
    ("feminine", "direct, constructive and adult"),
    ("masculine", "bright, pragmatic and young-adult"),
    ("feminine", "warm, dependable and mature"),
    ("masculine", "precise, thoughtful and adult"),
    ("feminine", "gentle, imaginative and adult"),
    ("masculine", "firm, composed and mature"),
    ("feminine", "curious, articulate and young-adult"),
    ("masculine", "measured, reassuring and adult"),
    ("feminine", "authoritative, analytical and mature"),
    ("masculine", "energetic, practical and young-adult"),
    ("feminine", "attentive, concise and adult"),
    ("masculine", "calm, creative and mature"),
    ("feminine", "confident, methodical and adult"),
    ("masculine", "friendly, clear and young-adult"),
    ("feminine", "grounded, thoughtful and mature"),
    ("masculine", "direct, constructive and adult"),
    ("feminine", "bright, pragmatic and young-adult"),
    ("masculine", "warm, dependable and mature"),
    ("feminine", "precise, creative and adult"),
    ("masculine", "gentle, analytical and adult"),
    ("feminine", "firm, composed and mature"),
    ("masculine", "curious, articulate and young-adult"),
    ("feminine", "measured, reassuring and adult"),
    ("masculine", "authoritative, pragmatic and mature"),
    ("feminine", "energetic, clear and young-adult"),
    ("masculine", "attentive, thoughtful and adult"),
)

_POOL = {
    f"pool-{index:03d}": _profile(
        f"pool-{index:03d}",
        f"Identità {index:03d}",
        presentation,
        character,
    )
    for index, (presentation, character) in enumerate(_POOL_SPECS, start=1)
}

_ALL = {**_BUILT_INS, **_POOL}


def avatar_for(persona_id: str) -> AvatarProfile:
    """Return a built-in profile or a safe no-image fallback.

    Unknown/adaptive personas still appear in the meeting grid.  Their
    initial-based placeholder makes the missing onboarding asset explicit
    instead of assigning them another agent's face.
    """
    normalized = persona_id.strip() or "unknown"
    profile = _BUILT_INS.get(normalized)
    if profile is not None:
        return profile
    label = normalized.replace("_", " ").replace("-", " ").title()
    return AvatarProfile(persona_id=normalized, display_name=label)


def avatar_by_id(avatar_id: str) -> Optional[AvatarProfile]:
    """Return a registered immutable identity, if present."""
    return _ALL.get(avatar_id)


def avatar_for_agent(
    persona_id: str,
    display_name: str,
    avatar_id: Optional[str],
) -> AvatarProfile:
    """Bind a registered face and voice to an agent-facing display name."""
    base = _ALL.get(avatar_id or persona_id)
    if base is None:
        return AvatarProfile(persona_id=persona_id, display_name=display_name)
    return replace(
        base,
        persona_id=persona_id,
        display_name=display_name,
        alt_text=f"Ritratto sintetico di {display_name}",
    )


def avatar_pool() -> tuple[AvatarProfile, ...]:
    """Return the reusable catalog in stable order."""
    return tuple(_POOL.values())


def avatar_asset_paths() -> tuple[str, ...]:
    """Return the exact package-relative static paths safe to serve."""
    return tuple(
        profile.portrait_asset
        for profile in _ALL.values()
        if profile.portrait_asset is not None
    )
