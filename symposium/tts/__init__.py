"""Optional, local-only speech synthesis for the Symposium viewer."""

from symposium.tts.local import LocalTTSManager, LocalTTSUnavailable

__all__ = ["LocalTTSManager", "LocalTTSUnavailable"]
