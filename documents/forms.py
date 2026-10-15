"""Admin ModelForm for Document — surfaces the file-OR-URL rule as a field error.

The model ``clean()`` (documents.models.Document.clean) is the backstop; this
form repeats the exactly-one-of file/source_url check for library documents so
the admin add/change page shows a clear validation error instead of a 500 or a
silent save. ``file`` is no longer required at the form level because a library
document may instead supply a media URL.
"""
from django import forms

from .models import Document


class DocumentAdminForm(forms.ModelForm):
    class Meta:
        model = Document
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # A library document may supply a URL instead of a file, so the file
        # field is optional at the form level (the clean() rule enforces that
        # exactly one is present).
        if "file" in self.fields:
            self.fields["file"].required = False
        # The minimal add form hides the title; it is derived from the document
        # content at process time (with a filename/URL no-key fallback), so the
        # title is never required at the form level.
        if "title" in self.fields:
            self.fields["title"].required = False

    def clean(self):
        cleaned = super().clean()
        # The minimal add form omits the ``source`` field entirely; such a
        # submission is always a library upload, so run the file-OR-URL rule.
        if "source" in self.fields:
            source = cleaned.get("source")
        else:
            source = Document.SOURCE_LIBRARY
        if source != Document.SOURCE_LIBRARY:
            return cleaned
        has_file = bool(cleaned.get("file"))
        has_url = bool(cleaned.get("source_url"))
        if has_file and has_url:
            raise forms.ValidationError(
                "Provide EITHER a file OR a media URL, not both."
            )
        if not has_file and not has_url:
            raise forms.ValidationError(
                "A library document needs a file upload or a media URL."
            )
        return cleaned
