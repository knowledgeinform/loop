import json

from django.conf import settings
from django.utils import timezone
from rest_framework import serializers

from catalog.auid import STRUCTURE_FAMILY_VALUES
from catalog.documents import normalize_elements_payload


API_KEY_SCOPES = (
    "data:read",
    "data:write",
    "files:write",
    "imports:write",
)

SYNTHESIS_STEP_TYPES = (
    "ball_milling",
    "weighing",
    "mixing",
    "pelletizing",
    "heat_treatment",
    "annealing",
    "arc_melting",
    "quenching",
    "cooling",
    "grinding",
    "xrd_measurement",
    "other",
    "unknown",
    "na",
)


class PrecursorSerializer(serializers.Serializer):
    cas_number = serializers.CharField(required=False, allow_blank=True)
    name = serializers.CharField(required=False, allow_blank=True)
    formula = serializers.CharField(required=False, allow_blank=True)
    purity = serializers.CharField(required=False, allow_blank=True)
    supplier = serializers.CharField(required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True)


class SynthesisStepSerializer(serializers.Serializer):
    step_type = serializers.ChoiceField(choices=SYNTHESIS_STEP_TYPES)
    notes = serializers.CharField(required=False, allow_blank=True)
    milling_time_hours = serializers.FloatField(required=False)
    milling_rpm = serializers.FloatField(required=False)
    ball_powder_ratio = serializers.CharField(required=False, allow_blank=True)
    atmosphere = serializers.CharField(required=False, allow_blank=True)
    jar_material = serializers.CharField(required=False, allow_blank=True)
    ball_material = serializers.CharField(required=False, allow_blank=True)
    process_control_agent = serializers.CharField(required=False, allow_blank=True)
    total_mass_g = serializers.FloatField(required=False)
    precursors = serializers.CharField(required=False, allow_blank=True)
    precursors_list = PrecursorSerializer(many=True, required=False)
    mixing_time_min = serializers.FloatField(required=False)
    mixing_method = serializers.CharField(required=False, allow_blank=True)
    pressure_mpa = serializers.FloatField(required=False)
    hold_time_min = serializers.FloatField(required=False)
    die_diameter_mm = serializers.FloatField(required=False)
    lubricant = serializers.CharField(required=False, allow_blank=True)
    max_temp_c = serializers.FloatField(required=False)
    ramp_rate_c_min = serializers.FloatField(required=False)
    hold_time_hours = serializers.FloatField(required=False)
    furnace_type = serializers.CharField(required=False, allow_blank=True)
    o2_partial_pressure_bar = serializers.FloatField(required=False)
    temperature_c = serializers.FloatField(required=False)
    duration_hours = serializers.FloatField(required=False)
    current_a = serializers.FloatField(required=False)
    number_of_remelts = serializers.IntegerField(required=False)
    hearth_material = serializers.CharField(required=False, allow_blank=True)
    quenching_medium = serializers.CharField(required=False, allow_blank=True)
    medium_temperature_c = serializers.FloatField(required=False)
    cooling_method = serializers.CharField(required=False, allow_blank=True)
    cooling_rate_c_min = serializers.FloatField(required=False)
    grinding_method = serializers.CharField(required=False, allow_blank=True)
    final_particle_size = serializers.CharField(required=False, allow_blank=True)
    radiation = serializers.CharField(required=False, allow_blank=True)
    two_theta_range = serializers.CharField(required=False, allow_blank=True)
    step_size_deg = serializers.FloatField(required=False)
    scan_speed_deg_min = serializers.FloatField(required=False)
    description = serializers.CharField(required=False, allow_blank=True)


class CompositionValidationMixin:
    def validate_elements(self, value):
        try:
            return normalize_elements_payload(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc


class APIKeyCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)
    scopes = serializers.ListField(
        child=serializers.ChoiceField(choices=API_KEY_SCOPES),
        allow_empty=False,
    )
    expires_at = serializers.DateTimeField(required=False, allow_null=True)

    def validate_scopes(self, value):
        return list(dict.fromkeys(value))

    def validate_expires_at(self, value):
        if value is not None and value <= timezone.now():
            raise serializers.ValidationError("Expiration must be in the future.")
        return value


class RecordValidationSerializer(serializers.Serializer):
    record_type = serializers.ChoiceField(
        choices=("experiment", "literature", "computational")
    )
    record = serializers.DictField()
    material_auid = serializers.CharField(required=False, allow_blank=True)


class CompositionNormalizeSerializer(CompositionValidationMixin, serializers.Serializer):
    elements = serializers.DictField(child=serializers.FloatField())
    structure_family = serializers.ChoiceField(choices=sorted(STRUCTURE_FAMILY_VALUES))


class PrecursorWriteSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200)
    formula = serializers.CharField(required=False, allow_blank=True)
    cas_number = serializers.CharField(required=False, allow_blank=True)
    purity = serializers.CharField(required=False, allow_blank=True)
    supplier = serializers.CharField(required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True)
    force = serializers.BooleanField(required=False, default=False, write_only=True)


class ProtocolWriteSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200)
    description = serializers.CharField(required=False, allow_blank=True)
    # Protocol templates are deliberately lossless because older deployments
    # may contain extra step fields that are still meaningful to the website.
    steps = serializers.ListField(child=serializers.DictField(), allow_empty=True)


class ImportSerializer(serializers.Serializer):
    record_type = serializers.ChoiceField(
        choices=("experiment", "literature", "computational")
    )
    records = serializers.ListField(
        child=serializers.DictField(), allow_empty=False, max_length=100, required=False
    )
    file = serializers.FileField(required=False, write_only=True)
    dry_run = serializers.BooleanField(required=False, default=False)

    def validate(self, attrs):
        upload = attrs.get("file")
        records = attrs.get("records")
        if upload and records:
            raise serializers.ValidationError(
                "Provide either records or a JSON/JSONL file, not both."
            )
        if upload:
            max_bytes = getattr(settings, "LOOP_API_IMPORT_MAX_BYTES", 10 * 1024 * 1024)
            if upload.size > max_bytes:
                raise serializers.ValidationError(
                    {"file": [f"File exceeds the {max_bytes}-byte import limit."]}
                )
            try:
                text = upload.read().decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise serializers.ValidationError({"file": ["File must be UTF-8."]}) from exc
            finally:
                upload.seek(0)

            try:
                if upload.name.lower().endswith((".jsonl", ".ndjson")):
                    records = []
                    for line_number, line in enumerate(text.splitlines(), start=1):
                        if not line.strip():
                            continue
                        value = json.loads(line)
                        if not isinstance(value, dict):
                            raise serializers.ValidationError(
                                {"file": [f"Line {line_number} must be a JSON object."]}
                            )
                        records.append(value)
                elif upload.name.lower().endswith(".json"):
                    value = json.loads(text)
                    if isinstance(value, dict) and isinstance(value.get("records"), list):
                        records = value["records"]
                    elif isinstance(value, dict):
                        records = [value]
                    elif isinstance(value, list):
                        records = value
                    else:
                        raise serializers.ValidationError(
                            {"file": ["JSON must contain an object or array of objects."]}
                        )
                else:
                    raise serializers.ValidationError(
                        {"file": ["Only .json, .jsonl, and .ndjson files are supported."]}
                    )
            except json.JSONDecodeError as exc:
                raise serializers.ValidationError(
                    {"file": [f"Invalid JSON at line {exc.lineno}: {exc.msg}"]}
                ) from exc

            if not records:
                raise serializers.ValidationError({"file": ["The import is empty."]})
            if len(records) > 100:
                raise serializers.ValidationError(
                    {"file": ["An import may contain at most 100 records."]}
                )
            if not all(isinstance(record, dict) for record in records):
                raise serializers.ValidationError(
                    {"file": ["Every imported record must be a JSON object."]}
                )
            attrs["records"] = records
        elif not records:
            raise serializers.ValidationError(
                {"records": ["Provide at least one record or upload a JSON/JSONL file."]}
            )
        return attrs


class ExperimentCreateSerializer(CompositionValidationMixin, serializers.Serializer):
    elements = serializers.DictField(child=serializers.FloatField())
    structure_family = serializers.ChoiceField(choices=sorted(STRUCTURE_FAMILY_VALUES))
    phase_status = serializers.ChoiceField(
        choices=("single_phase", "multi_phase", "not_confirmed")
    )
    synthesis_steps = SynthesisStepSerializer(many=True, required=False)
    spacegroup = serializers.CharField(required=False, default="unknown")
    element_sites = serializers.DictField(required=False)
    raw_data_type = serializers.ChoiceField(
        choices=("xrd", "sem", "tem", "eds", "other", "unknown", "na"),
        required=False,
        default="xrd",
    )
    comments = serializers.CharField(required=False, allow_blank=True, default="na")


class ExperimentMultipartSerializer(serializers.Serializer):
    record = serializers.JSONField(
        help_text="The same JSON object accepted by application/json requests."
    )
    csv_file = serializers.FileField(help_text="Optional XRD CSV attachment.")


class LiteratureCreateSerializer(CompositionValidationMixin, serializers.Serializer):
    doi = serializers.CharField()
    synthesis_successful = serializers.BooleanField()
    elements = serializers.DictField(child=serializers.FloatField())
    structure_family = serializers.ChoiceField(choices=sorted(STRUCTURE_FAMILY_VALUES))
    title = serializers.CharField(required=False, allow_blank=True, default="na")
    authors = serializers.ListField(child=serializers.CharField(), required=False)
    journal = serializers.CharField(required=False, allow_blank=True, default="na")
    year = serializers.IntegerField(required=False, allow_null=True)
    findings = serializers.CharField(required=False, allow_blank=True, default="na")
    synthesis_steps = SynthesisStepSerializer(many=True, required=False)
    spacegroup = serializers.CharField(required=False, default="unknown")
    element_sites = serializers.DictField(required=False)


class ComputationalCreateSerializer(CompositionValidationMixin, serializers.Serializer):
    elements = serializers.DictField(child=serializers.FloatField())
    structure_family = serializers.ChoiceField(choices=sorted(STRUCTURE_FAMILY_VALUES))
    dft_source = serializers.CharField(required=False, allow_blank=True, default="")
    calculation_method = serializers.CharField(required=False, allow_blank=True, default="")
    functional = serializers.CharField(required=False, allow_blank=True, default="")
    pseudopotential = serializers.CharField(required=False, allow_blank=True, default="")
    k_points = serializers.CharField(required=False, allow_blank=True, default="")
    cutoff_energy = serializers.FloatField(required=False, allow_null=True)
    formation_energy_ev = serializers.FloatField(required=False, allow_null=True)
    hull_distance_ev = serializers.FloatField(required=False, allow_null=True)
    bandgap_ev = serializers.FloatField(required=False, allow_null=True)
    bandgap_type = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    bandgap_fit_ev = serializers.FloatField(required=False, allow_null=True)
    bulk_modulus_vrh = serializers.FloatField(required=False, allow_null=True)
    shear_modulus_vrh = serializers.FloatField(required=False, allow_null=True)
    youngs_modulus_vrh = serializers.FloatField(required=False, allow_null=True)
    poisson_ratio = serializers.FloatField(required=False, allow_null=True)
    elastic_anisotropy = serializers.FloatField(required=False, allow_null=True)
    debye_temperature = serializers.FloatField(required=False, allow_null=True)
    thermal_conductivity_300k = serializers.FloatField(required=False, allow_null=True)
    gruneisen_parameter = serializers.FloatField(required=False, allow_null=True)
    thermal_expansion_300k = serializers.FloatField(required=False, allow_null=True)
    pearson_symbol = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    crystal_system = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    crystal_family = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    spin_atom = serializers.FloatField(required=False, allow_null=True)
    ml_predictions = serializers.DictField(required=False)
    extended_data = serializers.DictField(required=False)
    spacegroup = serializers.CharField(required=False, default="unknown")
    element_sites = serializers.DictField(required=False)


class MaterialSerializer(serializers.Serializer):
    material_auid = serializers.CharField()
    elements = serializers.DictField(child=serializers.FloatField())
    element_symbols = serializers.ListField(child=serializers.CharField())
    num_elements = serializers.IntegerField()
    structure_family = serializers.CharField()
    display_name = serializers.CharField(allow_blank=True, allow_null=True)
    notes = serializers.CharField(allow_blank=True, allow_null=True)
    curator = serializers.CharField(allow_blank=True, allow_null=True)
    visibility_affiliations = serializers.ListField(child=serializers.CharField())
    created_at = serializers.DateTimeField(allow_null=True)
    updated_at = serializers.DateTimeField(allow_null=True)
