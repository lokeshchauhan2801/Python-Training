"""Views for the scrapper API."""

from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from basics.api.views import ScopedModelViewSet

from ..models import ScrapeJob
from ..services import ScrapeService
from .serializers import ScrapeJobSerializer


class ScrapeJobViewSet(ScopedModelViewSet):
    """List/create scrape jobs and run one via ``<uuid>/run/``.

    ``company`` is resolved by the serializer's required-company rule; audit is
    forced by the base viewset. ``run`` invokes the SSRF-safe ``ScrapeService``
    and returns the job with its resulting status (expected failures surface as
    an ``ERROR`` job, HTTP 200).
    """

    queryset = ScrapeJob.objects.all()
    serializer_class = ScrapeJobSerializer

    @action(detail=True, methods=["post"])
    def run(self, request, pk=None):
        job = self.get_object()
        ScrapeService().run(job, actor=request.user)
        return Response(self.get_serializer(job).data, status=status.HTTP_200_OK)
