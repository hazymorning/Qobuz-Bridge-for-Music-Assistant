"""Music Assistant backend: Qobuz Connect devices that play through Music Assistant."""


class LoginError(Exception):
    """A Music Assistant login problem; the message is plain language for the web UI."""
