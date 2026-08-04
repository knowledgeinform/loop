"""Parity tests between the API serializers and ``STEP_FIELD_SCHEMA``.

``catalog/batch_upload.py`` is the single source of truth for the stored
``synthesis_steps`` shape. The web batch-upload path reaches it through
``normalize_synthesis_steps``; the API path first runs the payload through
``SynthesisStepSerializer``, and DRF drops undeclared fields silently. A field
added to ``STEP_FIELD_SCHEMA`` but not to the serializer would therefore make
the same record store differently depending on how it was submitted, with no
error on either path. These tests fail loudly on that drift instead.
"""

from django.test import SimpleTestCase
from rest_framework import serializers as drf

from catalog.api.serializers import (
    SYNTHESIS_STEP_TYPES,
    PrecursorSerializer,
    SynthesisStepSerializer,
)
from catalog.batch_upload import (
    _PRECURSOR_KEYS,
    STEP_FIELD_SCHEMA,
    STEP_TYPE_VALUES,
    normalize_synthesis_steps,
)


# Schema ``kind`` -> the DRF field class the serializer must use for it. A
# ``ratio`` arrives as the string "10:1" and is canonicalized downstream by
# ``normalize_synthesis_steps``, so the serializer carries it as text.
KIND_TO_DRF_FIELD = {
    "float": drf.FloatField,
    "int": drf.IntegerField,
    "ratio": drf.CharField,
    "str": drf.CharField,
    "list": drf.ListSerializer,
}

# One representative value per schema kind, used to populate a step densely.
SAMPLE_BY_KIND = {
    "float": 12.5,
    "int": 3,
    "ratio": "10:1",
    "str": "sample-text",
    "list": [{
        "name": "Y2O3",
        "cas_number": "1314-36-9",
        "formula": "Y2O3",
        "purity": "99.99%",
        "supplier": "Sigma",
        "notes": "dried at 200C",
    }],
}


def _schema_fields_by_name():
    """``field_name -> {kind}`` across every step type in the schema."""
    merged = {}
    for fields in STEP_FIELD_SCHEMA.values():
        for name, kind in fields.items():
            merged.setdefault(name, set()).add(kind)
    return merged


def _fully_populated_step(step_type):
    step = {"step_type": step_type, "notes": f"notes for {step_type}"}
    for name, kind in STEP_FIELD_SCHEMA[step_type].items():
        step[name] = SAMPLE_BY_KIND[kind]
    return step


class SynthesisStepSerializerSchemaParityTests(SimpleTestCase):
    def test_serializer_declares_every_schema_field(self):
        declared = set(SynthesisStepSerializer().fields)
        missing = sorted(set(_schema_fields_by_name()) - declared)
        self.assertEqual(
            missing,
            [],
            "STEP_FIELD_SCHEMA fields absent from SynthesisStepSerializer; DRF "
            f"would drop these from API submissions without an error: {missing}",
        )

    def test_serializer_declares_no_fields_outside_schema(self):
        declared = set(SynthesisStepSerializer().fields)
        extra = sorted(declared - set(_schema_fields_by_name()) - {"step_type", "notes"})
        self.assertEqual(
            extra,
            [],
            "Serializer accepts fields no step type stores, so they are "
            f"discarded later by normalize_synthesis_steps: {extra}",
        )

    def test_field_types_match_schema_kinds(self):
        fields = SynthesisStepSerializer().fields
        for name, kinds in sorted(_schema_fields_by_name().items()):
            for kind in sorted(kinds):
                with self.subTest(field=name, kind=kind):
                    self.assertIsInstance(fields[name], KIND_TO_DRF_FIELD[kind])

    def test_step_type_choices_match_schema(self):
        self.assertEqual(set(SYNTHESIS_STEP_TYPES), set(STEP_TYPE_VALUES))

    def test_precursor_serializer_matches_precursor_keys(self):
        self.assertEqual(set(PrecursorSerializer().fields), set(_PRECURSOR_KEYS))


class SynthesisStepSerializerRoundTripTests(SimpleTestCase):
    def test_cooling_step_survives_the_serializer(self):
        step = {
            "step_type": "cooling",
            "cooling_method": "furnace_cool",
            "cooling_rate_c_min": 5.0,
        }
        serializer = SynthesisStepSerializer(data=step)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["cooling_method"], "furnace_cool")
        self.assertEqual(serializer.validated_data["cooling_rate_c_min"], 5.0)

    def test_every_declared_field_survives_the_serializer(self):
        for step_type in STEP_TYPE_VALUES:
            with self.subTest(step_type=step_type):
                step = _fully_populated_step(step_type)
                serializer = SynthesisStepSerializer(data=step)
                self.assertTrue(serializer.is_valid(), serializer.errors)
                dropped = sorted(set(step) - set(serializer.validated_data))
                self.assertEqual(dropped, [], f"silently dropped: {dropped}")

    def test_api_and_web_paths_store_identical_steps(self):
        """The same record submitted either way must normalize identically."""
        raw_steps = [_fully_populated_step(st) for st in STEP_TYPE_VALUES]

        via_web, web_errors = normalize_synthesis_steps(raw_steps)
        self.assertEqual(web_errors, [])

        serializer = SynthesisStepSerializer(data=raw_steps, many=True)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        via_api, api_errors = normalize_synthesis_steps(
            [dict(step) for step in serializer.validated_data]
        )
        self.assertEqual(api_errors, [])

        self.assertEqual(via_api, via_web)

    def test_precursors_list_entries_survive_the_serializer(self):
        step = _fully_populated_step("weighing")
        serializer = SynthesisStepSerializer(data=step)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        precursor = serializer.validated_data["precursors_list"][0]
        self.assertEqual(dict(precursor), step["precursors_list"][0])
