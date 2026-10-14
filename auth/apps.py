from django.apps import AppConfig


class AuthConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "auth"
    label = "accounts"  # avoids clash with django.contrib.auth
    verbose_name = "Auth"
