"""Reusable DRF viewset bases."""

from rest_framework import viewsets


class ScopedModelViewSet(viewsets.ModelViewSet):
    """Enforce owner/company scope on queries and writes.

    Superusers see everything. Everyone else sees rows whose ``company.owner`` is
    the request user (for company-scoped models) or whose ``owner`` is the request
    user (for owner-scoped models). ``perform_create``/``perform_update`` force
    the server-controlled relations so a client cannot set another company/user.
    """

    def _model_field_names(self):
        model = self.queryset.model if self.queryset is not None else self.get_serializer().Meta.model
        return {f.name for f in model._meta.get_fields()}

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if user.is_superuser:
            return qs
        field_names = {f.name for f in qs.model._meta.get_fields()}
        if "company" in field_names:
            return qs.filter(company__owner=user)
        if "owner" in field_names:
            return qs.filter(owner=user)
        return qs

    def _scope_kwargs(self, serializer):
        model = serializer.Meta.model
        field_names = {f.name for f in model._meta.get_fields()}
        kwargs = {}
        if "created_by" in field_names:
            kwargs["created_by"] = self.request.user
        if "updated_by" in field_names:
            kwargs["updated_by"] = self.request.user
        if "user" in field_names:
            kwargs["user"] = self.request.user
        return kwargs

    def perform_create(self, serializer):
        serializer.save(**self._scope_kwargs(serializer))

    def perform_update(self, serializer):
        model = serializer.Meta.model
        field_names = {f.name for f in model._meta.get_fields()}
        kwargs = {}
        if "updated_by" in field_names:
            kwargs["updated_by"] = self.request.user
        serializer.save(**kwargs)
