"""Views for the documents API.

``DocumentViewSet`` is owner/company scoped (via ``ScopedModelViewSet``). A
library upload sets ``uploaded_by`` server-side; the ``chunks`` action lists a
document's chunks; the ``process`` action re-runs synchronous extraction. Only
library documents can be processed (chat attachments are Phase 3) — a non-library
document returns HTTP 409.
"""

from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from basics.api.views import ScopedModelViewSet

from ..models import Document
from ..services import DocumentIndexError, DocumentValidationError, process_document
from .serializers import DocumentChunkSerializer, DocumentSerializer


class DocumentViewSet(ScopedModelViewSet):
    queryset = Document.objects.all()
    serializer_class = DocumentSerializer

    def perform_create(self, serializer):
        kwargs = self._scope_kwargs(serializer)
        kwargs["uploaded_by"] = self.request.user
        serializer.save(**kwargs)

    @action(detail=True, methods=["get"])
    def chunks(self, request, pk=None):
        document = self.get_object()
        chunks = document.chunks.order_by("chunk_index")
        return Response(
            DocumentChunkSerializer(chunks, many=True).data,
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"])
    def process(self, request, pk=None):
        document = self.get_object()
        if document.source != Document.SOURCE_LIBRARY:
            return Response(
                {"detail": "Only library documents can be processed."},
                status=status.HTTP_409_CONFLICT,
            )
        try:
            process_document(document)
        except (DocumentValidationError, DocumentIndexError):
            # The service persisted status=failed with a user-facing message.
            document.refresh_from_db()
        return Response(
            self.get_serializer(document).data, status=status.HTTP_200_OK
        )
