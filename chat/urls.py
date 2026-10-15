"""URLs for the top-level ``/chat/`` surface (option B).

Mounted at ``/chat/`` in ``base.urls`` BEFORE the admin include so the chat
page lives outside ``/admin/`` while still rendering inside the admin shell.
"""

from django.urls import path

from . import views

app_name = "chat"

urlpatterns = [
    path("", views.chat_home, name="home"),
    path("companies-json/", views.companies_json, name="companies_json"),
    path("sessions-json/", views.sessions_json, name="sessions_json"),
    path("new/", views.new_session, name="new_session"),
    path("<str:session_id>/messages-json/", views.messages_json, name="messages_json"),
    path("<str:session_id>/send/", views.send, name="send"),
    path("<str:session_id>/attach/", views.attach, name="attach"),
    path("<str:session_id>/delete/", views.delete_session, name="delete_session"),
    path("<str:session_id>/", views.chat_session, name="session"),
]
