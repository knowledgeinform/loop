from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.contrib.auth.models import User


class SignupForm(UserCreationForm):
    """Sign-up asks for no affiliation: an admin sets one when approving the
    account, and until then the account waits (access.policy.has_loop_access).
    The choice used to be open to anyone and named the partner groups."""

    first_name = forms.CharField(max_length=30, required=True, help_text="Required.")
    last_name = forms.CharField(max_length=30, required=True, help_text="Required.")

    class Meta:
        model = User
        fields = ("username", "first_name", "last_name", "email", "password1", "password2")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        field_attrs = {
            "username": {"class": "form-control", "autocomplete": "username"},
            "first_name": {"class": "form-control", "autocomplete": "given-name"},
            "last_name": {"class": "form-control", "autocomplete": "family-name"},
            "email": {"class": "form-control", "autocomplete": "email"},
            "password1": {"class": "form-control", "autocomplete": "new-password"},
            "password2": {"class": "form-control", "autocomplete": "new-password"},
        }
        for name, attrs in field_attrs.items():
            self.fields[name].widget.attrs.update(attrs)

    def clean_email(self):
        email = self.cleaned_data["email"]
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with this email already exists.")
        return email


class LoopAuthenticationForm(AuthenticationForm):
    """Styled authentication form for LOOP login page."""

    def __init__(self, request=None, *args, **kwargs):
        super().__init__(request=request, *args, **kwargs)
        self.fields["username"].widget.attrs.update(
            {"class": "form-control", "autocomplete": "username"}
        )
        self.fields["password"].widget.attrs.update(
            {"class": "form-control", "autocomplete": "current-password"}
        )


class LiteratureDataForm(forms.Form):
    doi = forms.CharField(max_length=255, required=True)
    synthesis_successful = forms.ChoiceField(
        required=True,
        choices=(("true", "Yes"), ("false", "No")),
        widget=forms.RadioSelect,
    )
    title = forms.CharField(max_length=500, required=False)
    authors = forms.CharField(
        max_length=1000,
        required=False,
        help_text="Comma-separated author names",
    )
    journal = forms.CharField(max_length=255, required=False)
    year = forms.IntegerField(required=False, min_value=1900, max_value=3000)
    structure_family = forms.ChoiceField(
        choices=(
            ("rocksalt", "Rocksalt"),
            ("pyrochlore", "Pyrochlore"),
            ("spinel", "Spinel"),
            ("perovskite", "Perovskite"),
            ("fluorite", "Fluorite"),
            ("other", "Other"),
        ),
        required=True,
    )
    element_order = forms.CharField(required=False, widget=forms.HiddenInput())
    findings = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 4}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        control_fields = ("doi", "title", "authors", "journal", "year", "structure_family", "findings")
        for name in control_fields:
            self.fields[name].widget.attrs.update({"class": "form-control"})

class BatchExperimentalUploadForm(forms.Form):
    MODE_WITH_MANIFEST = "with_manifest"
    MODE_ZIP_ONLY = "zip_only"
    MODE_WITH_EXISTING_ZIP = "with_existing_zip"

    upload_mode = forms.ChoiceField(
        required=True,
        choices=(
            (MODE_WITH_MANIFEST, "I have a manifest CSV and an XRD ZIP"),
            (MODE_ZIP_ONLY, "I only have an XRD ZIP"),
            (MODE_WITH_EXISTING_ZIP, "I have completed a generated manifest"),
        ),
        widget=forms.RadioSelect(attrs={"class": "btn-check", "autocomplete": "off"}),
        initial=MODE_WITH_MANIFEST,
    )

    manifest = forms.FileField(
        required=False,
        help_text="CSV or Excel (.xlsx) manifest with one row per experiment.",
        widget=forms.ClearableFileInput(attrs={"accept": ".csv,.xlsx"}),
    )

    archive = forms.FileField(
        required=False,
        help_text="ZIP file containing experiment folders.",
    )

    structure_family = forms.ChoiceField(
        required=True,
        choices=(
            ("unknown", "Unknown"),
            ("rocksalt", "Rocksalt"),
            ("pyrochlore", "Pyrochlore"),
            ("spinel", "Spinel"),
            ("perovskite", "Perovskite"),
            ("fluorite", "Fluorite"),
            ("other", "Other"),
        ),
        initial="unknown",
    )

    def clean(self):
        cleaned = super().clean()
        mode = cleaned.get("upload_mode")
        manifest = cleaned.get("manifest")
        archive = cleaned.get("archive")

        if mode == self.MODE_WITH_MANIFEST:
            if not manifest:
                self.add_error("manifest", "Manifest CSV is required.")
            if not archive:
                self.add_error("archive", "XRD ZIP is required.")

        elif mode == self.MODE_ZIP_ONLY:
            if not archive:
                self.add_error("archive", "XRD ZIP is required.")

        elif mode == self.MODE_WITH_EXISTING_ZIP:
            if not manifest:
                self.add_error("manifest", "Completed manifest CSV is required.")

        return cleaned


class BatchLiteratureUploadForm(forms.Form):
    manifest = forms.FileField(
        required=True,
        help_text=(
            "CSV or Excel (.xlsx) manifest with one row per paper, or a JSON / "
            "JSONL file with one record per paper."
        ),
        widget=forms.ClearableFileInput(
            attrs={"accept": ".csv,.xlsx,.json,.jsonl,.ndjson"}
        ),
    )

    structure_family = forms.ChoiceField(
        required=True,
        choices=(
            ("unknown", "Unknown"),
            ("rocksalt", "Rocksalt"),
            ("pyrochlore", "Pyrochlore"),
            ("spinel", "Spinel"),
            ("perovskite", "Perovskite"),
            ("fluorite", "Fluorite"),
            ("other", "Other"),
        ),
        initial="unknown",
    )