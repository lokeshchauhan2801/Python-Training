"""Middleware that captures the authenticated user into a contextvar.

Must be placed AFTER ``AuthenticationMiddleware`` so ``request.user`` is
populated. The contextvar is always reset in a ``finally`` block so the value
never leaks across requests on a reused thread/async task.
"""

from .context import reset_current_user, set_current_user


class CurrentUserMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and not getattr(user, "is_authenticated", False):
            user = None
        token = set_current_user(user)
        try:
            return self.get_response(request)
        finally:
            reset_current_user(token)
