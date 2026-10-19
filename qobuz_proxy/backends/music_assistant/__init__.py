"""
Music Assistant backend.

Plays Qobuz tracks on Music Assistant (MA) players and groups through MA's own
Qobuz provider, so MA shows the track, artwork, queue and progress like
anything else it plays. Needs the optional ``music-assistant`` extra
(music-assistant-client, Python 3.13+); ``settings``, ``supervisor`` and
``manager`` import without it so the web UI can explain what is missing.
"""

from typing import Optional


def ma_support_error() -> Optional[str]:
    """None when the MA client library is installed, else a plain explanation."""
    try:
        import music_assistant_client  # noqa: F401
        import music_assistant_models  # noqa: F401
    except ImportError as e:
        return (
            "Music Assistant support is not installed (needs Python 3.13+ and the "
            f"'music-assistant' extra): {e}"
        )
    return None


__all__ = ["ma_support_error"]
