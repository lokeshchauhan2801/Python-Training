
from django.urls import path

from . import views

app_name = "chat"

urlpatterns = [
    path("", views.chat_home, name="home"),
    path("companies-json/", views.companies_json, name="companies_json"),
    path("models-json/", views.models_json, name="models_json"),
    path("sessions-json/", views.sessions_json, name="sessions_json"),
    path("new/", views.new_session, name="new_session"),
    path("<str:session_id>/messages-json/", views.messages_json, name="messages_json"),
    path("<str:session_id>/send/", views.send, name="send"),
    path("<str:session_id>/attach/", views.attach, name="attach"),
    path(
        "<str:session_id>/reprocess/",
        views.reprocess_document,
        name="reprocess_document",
    ),
    path(
        "<str:session_id>/doc-status-json/",
        views.document_status_json,
        name="document_status_json",
    ),
    path("<str:session_id>/delete/", views.delete_session, name="delete_session"),
    path("<str:session_id>/", views.chat_session, name="session"),
]
