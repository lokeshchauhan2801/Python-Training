"""Views for the company API."""

from basics.api.views import ScopedModelViewSet

from ..models import Company
from .serializers import CompanySerializer


class CompanyViewSet(ScopedModelViewSet):
    """List/create companies scoped to the request user.

    ``owner`` is forced to ``request.user`` on create (never client-settable);
    the base viewset forces ``created_by``/``updated_by``.
    """

    queryset = Company.objects.all()
    serializer_class = CompanySerializer

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user, **self._scope_kwargs(serializer))
