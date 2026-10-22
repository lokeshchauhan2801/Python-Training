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


        if "file" in self.fields:
            self.fields["file"].required = False


        if "title" in self.fields:
            self.fields["title"].required = False

    def clean(self):
        cleaned = super().clean()


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
