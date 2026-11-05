from django.apps import AppConfig


class AgentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "agents"
    verbose_name = "Agents"

    def ready(self):
        from agents import signals  # noqa: F401  (registers the receivers)
