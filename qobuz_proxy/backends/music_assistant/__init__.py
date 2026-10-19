"""Music Assistant backend: Qobuz Connect speakers that play through Music Assistant (MA)."""


class LoginError(Exception):
    """A Music Assistant login problem; the message is plain language for the web UI."""
