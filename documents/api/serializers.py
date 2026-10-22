"""Serializers for the documents API.

``DocumentSerializer`` reuses the chat required-company resolution rule (the
required ``company`` FK is resolved against request-user ownership) and marks
every server-controlled processing/audit field read-only, so a client can upload
a file + title but cannot forge status, chunk counts, or the uploader.
"""

from rest_framework import serializers

from basics.api.serializers import AuditReadOnlySerializerMixin
from chat.api.serializers import RequiredCompanyResolutionMixin

from ..models import Document, DocumentChunk

_SERVER_CONTROLLED_DOC_FIELDS = (
    "uploaded_by",
    "status",
    "processing_backend",
    "processing_stage",
    "processing_progress",
    "processing_task_id",
    "processing_error",
    "chunk_count",
    "processing_attempts",
    "processing_duration_ms",
    "file_size",
    "content_type",
    "file_type",
)


class DocumentSerializer(
    RequiredCompanyResolutionMixin,
    AuditReadOnlySerializerMixin,
    serializers.ModelSerializer,
):
    class Meta:
        model = Document
        fields = [
            "id",
            "title",
            "source",
            "file",
            "file_type",
            "content_type",
            "file_size",
            "status",
            "processing_backend",
            "processing_stage",
            "processing_progress",
            "processing_task_id",
            "processing_error",
            "chunk_count",
            "processing_attempts",
            "processing_duration_ms",
            "uploaded_by",
            "session",
            "company",
            "created_at",
            "updated_at",
            "created_by",
            "updated_by",
            "is_deleted",
            "deleted_at",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


        if self.instance is not None and "company" in self.fields:
            self.fields["company"].read_only = True
        if "company" in self.fields:
            self.fields["company"].required = False
        for name in _SERVER_CONTROLLED_DOC_FIELDS:
            if name in self.fields:
                self.fields[name].read_only = True

    def validate_company(self, value):
        return self._resolve_company(value)

    def validate(self, attrs):
        if self.instance is None and "company" not in attrs:
            attrs["company"] = self._resolve_company(None)
        return attrs


class DocumentChunkSerializer(serializers.ModelSerializer):
    class Meta:
        model = DocumentChunk
        fields = [
            "id",
            "document",
            "chunk_index",
            "page_number",
            "source_location",
            "content",
            "created_at",
        ]
        read_only_fields = fields
