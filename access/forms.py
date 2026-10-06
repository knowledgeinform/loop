from django import forms


class ChaosAccessRequestForm(forms.Form):
    affiliation = forms.CharField(
        max_length=200,
        help_text="Institution or company, and your role.",
        widget=forms.TextInput(attrs={"class": "form-control", "autocomplete": "organization"}),
    )
    purpose = forms.CharField(
        max_length=2000,
        help_text="What you plan to use CHAOS for, in a sentence or two.",
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 4}),
    )
    accept_terms = forms.BooleanField(
        required=True,
        label="I accept the CHAOS terms of use shown on this page.",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )


class ChaosTermsForm(forms.Form):
    """Accepting a new terms version, for an account that already has access."""

    accept_terms = forms.BooleanField(
        required=True,
        label="I accept the CHAOS terms of use shown on this page.",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )


# --- profile -------------------------------------------------------------------

import re

from access.countries import COUNTRIES
from access.models import Profile

_ROR = re.compile(r"^https://ror\.org/0[a-z0-9]{8}$")
_ORCID = re.compile(r"^(?:https?://orcid\.org/)?(\d{4}-\d{4}-\d{4}-\d{3}[\dX])$")


def _orcid_checksum_ok(orcid):
    """ISO 7064 11,2 check digit, as ORCID defines it."""
    digits = orcid.replace("-", "")
    total = 0
    for ch in digits[:-1]:
        total = (total + int(ch)) * 2
    result = (12 - total % 11) % 11
    check = "X" if result == 10 else str(result)
    return digits[-1] == check


class ProfileForm(forms.ModelForm):
    """Asked once of every account (access.models.Profile), with the usage notice."""

    country = forms.ChoiceField(
        choices=[("", "Choose a country")] + list(COUNTRIES),
        widget=forms.Select(attrs={"class": "form-select", "autocomplete": "country"}),
    )
    research_areas = forms.MultipleChoiceField(
        choices=Profile.AREA_CHOICES,
        widget=forms.CheckboxSelectMultiple,
        label="Research areas",
        help_text="Choose one or more.",
    )
    accept_usage_notice = forms.BooleanField(
        required=True,
        label="I have read the notice above describing what is recorded.",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )
    # Longer than the stored 19 characters, so a pasted https://orcid.org/...
    # address reaches clean_orcid, which keeps the 19-character iD.
    orcid = forms.CharField(
        max_length=64,
        required=False,
        label="ORCID iD (optional)",
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "0000-0000-0000-0000"}),
    )

    class Meta:
        model = Profile
        fields = [
            "institution",
            "ror_id",
            "ror_type",
            "country",
            "role",
            "sector",
            "research_areas",
            "intended_use",
            "orcid",
            "heard_from",
            "contact_ok",
        ]
        labels = {
            "institution": "Institution or company",
            "role": "Your role",
            "sector": "Kind of institution",
            "intended_use": "What will you use LOOP or CHAOS for?",
            "orcid": "ORCID iD (optional)",
            "heard_from": "How did you hear about LOOP or CHAOS? (optional)",
            "contact_ok": "The S4E Laboratory may email me about updates and occasional short surveys. (optional)",
        }
        help_texts = {
            "institution": "Start typing and choose from the list, or enter the full name.",
            "intended_use": "A sentence or two is enough.",
        }
        widgets = {
            "institution": forms.TextInput(attrs={"class": "form-control", "autocomplete": "organization", "list": "ror-suggestions"}),
            "ror_id": forms.HiddenInput(),
            "ror_type": forms.HiddenInput(),
            "role": forms.Select(attrs={"class": "form-select"}),
            "sector": forms.Select(attrs={"class": "form-select"}),
            "intended_use": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "orcid": forms.TextInput(attrs={"class": "form-control", "placeholder": "0000-0000-0000-0000"}),
            "heard_from": forms.Select(attrs={"class": "form-select"}),
            "contact_ok": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def clean_institution(self):
        value = " ".join(self.cleaned_data["institution"].split())
        if len(value) < 2:
            raise forms.ValidationError("Please give the name of your institution or company.")
        return value

    def clean_ror_id(self):
        value = (self.cleaned_data.get("ror_id") or "").strip()
        return value if _ROR.match(value) else ""

    def clean_ror_type(self):
        value = (self.cleaned_data.get("ror_type") or "").strip().lower()
        return value[:40] if re.fullmatch(r"[a-z ]*", value) else ""

    def clean_orcid(self):
        value = re.sub(r"(?i)^https?://(www\.)?orcid\.org/", "", (self.cleaned_data.get("orcid") or "").strip()).upper()
        if not value:
            return ""
        match = _ORCID.match(value)
        if not match or not _orcid_checksum_ok(match.group(1)):
            raise forms.ValidationError("This is not a valid ORCID iD (0000-0000-0000-0000).")
        return match.group(1)

    def clean_intended_use(self):
        value = self.cleaned_data["intended_use"].strip()
        if len(value) < 10:
            raise forms.ValidationError("Please say in a sentence what you will use the tools for.")
        return value

    def clean(self):
        cleaned = super().clean()
        # A ROR id belongs to the name it was chosen with; if the name was
        # edited afterwards, the id and type no longer apply.
        if not cleaned.get("ror_id"):
            cleaned["ror_type"] = ""
        return cleaned
