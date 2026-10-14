"""URLConf for the auth (``accounts``) API, mounted at ``api/v1/auth/``."""

from django.urls import path

from .views import CurrentUserView

urlpatterns = [
    path("user/me/", CurrentUserView.as_view(), name="auth-user-me"),
]
