"""Request-user context via contextvars.

The current acting user is stored in a ContextVar so that model/service code can
resolve an audit actor without threading ``request.user`` through every call.
``CurrentUserMiddleware`` sets it per request and always resets it afterwards.
"""

import contextvars

_current_user = contextvars.ContextVar("current_user", default=None)


def set_current_user(user):
    """Store the current user and return the reset token."""
    return _current_user.set(user)


def get_current_user():
    """Return the current user (or ``None`` when unset)."""
    return _current_user.get()


def reset_current_user(token):
    """Reset the context var to its previous value using ``token``."""
    _current_user.reset(token)
