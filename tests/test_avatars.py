"""Avatar catalog and public-payload safety tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from symposium.avatars import (
    AvatarProfile,
    avatar_asset_paths,
    avatar_for,
    avatar_for_agent,
    avatar_pool,
)


def test_builtin_avatar_payload_is_synthetic_and_package_local():
    payload = avatar_for("logician").viewer_payload()
    assert payload["synthetic"] is True
    assert payload["portrait_url"] == "/static/avatars/logician.webp"
    assert "Synthetic portrait" in payload["alt_text"]
    assert payload["voice"]["presentation"] == "masculine"


def test_coordinator_keeps_protocol_id_but_uses_sartori_display_name():
    profile = avatar_for("coordinator")
    assert profile.persona_id == "coordinator"
    assert profile.display_name == "Sartori"


def test_unknown_persona_has_explicit_no_image_fallback():
    profile = avatar_for("guest_security_lead")
    assert profile.display_name == "Guest Security Lead"
    assert profile.portrait_asset is None
    assert profile.viewer_payload()["portrait_url"] is None
    assert profile.viewer_payload()["voice"] is None


def test_reusable_pool_has_fifty_unique_faces_with_matching_voice_metadata():
    pool = avatar_pool()
    assert len(pool) == 50
    assert len({profile.persona_id for profile in pool}) == 50
    assert len({profile.portrait_asset for profile in pool}) == 50
    assert {profile.voice_presentation for profile in pool} == {
        "feminine", "masculine"
    }
    assert sum(profile.voice_presentation == "feminine" for profile in pool) == 25
    assert all(profile.voice_speaker == (
        "Julia" if profile.voice_presentation == "feminine" else "Richard"
    ) for profile in pool)
    assert len(avatar_asset_paths()) == 56


def test_agent_binding_keeps_face_voice_and_public_name_together():
    profile = avatar_for_agent("zeus-lead", "Responsabile Zeus", "pool-001")
    payload = profile.viewer_payload()
    assert payload["profile_id"] == "zeus-lead"
    assert payload["asset_id"] == "pool-001"
    assert payload["display_name"] == "Responsabile Zeus"
    assert payload["voice"]["presentation"] == "feminine"


@pytest.mark.parametrize(
    "persona_id",
    ["logician", "visionary", "researcher", "critic", "engineer", "coordinator"],
)
def test_builtin_portrait_asset_exists_in_package(persona_id):
    profile = avatar_for(persona_id)
    static_root = Path(__file__).parents[1] / "symposium" / "viewer" / "static"
    assert profile.portrait_asset is not None
    assert (static_root / profile.portrait_asset).is_file()


@pytest.mark.parametrize("asset", ["/tmp/face.webp", "../face.webp", "face.webp"])
def test_avatar_assets_are_confined_to_avatar_directory(asset):
    with pytest.raises(ValueError, match="under avatars"):
        AvatarProfile("x", "X", portrait_asset=asset)
