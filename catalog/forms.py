from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.contrib.auth.models import User

from .documents import AFFILIATION_CHOICES, AFFILIATION_VALUES, upsert_user_affiliations


class SignupForm(UserCreationForm):
    first_name = forms.CharField(max_length=30, required=True, help_text="Required.")
    last_name = forms.CharField(max_length=30, required=True, help_text="Required.")
    affiliations = forms.MultipleChoiceField(
        choices=AFFILIATION_CHOICES,
        required=True,
        widget=forms.CheckboxSelectMultiple,
        help_text="Select one or more affiliations.",
    )

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

    def clean_affiliations(self):
        selected = self.cleaned_data.get("affiliations", [])
        invalid = [a for a in selected if a not in AFFILIATION_VALUES]
        if invalid:
            raise forms.ValidationError("Invalid affiliation selection.")
        return selected

    def save(self, commit=True):
        user = super().save(commit=commit)
        if commit:
            selected = self.cleaned_data.get("affiliations", [])
            upsert_user_affiliations(user, selected)
        return user


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

