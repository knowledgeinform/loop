"""
MongoDB document models for LOOP (Learning and Optimization Platform)

Nested Mongo-native topology. Two user-facing collections own the bulk of the
data:

    materials   — one document per (composition, structure_family).
                  ``_id = material_auid`` (``M:...``). DFT calculations are
                  embedded here because a DFT run is a property of the
                  material class.
    recipes     — one document per unique methodology within a material.
                  ``_id = recipe_auid`` (``M:...:R:...``). Experimental
                  trials and literature reports with those exact synthesis
                  steps live as embedded arrays inside the recipe doc.

Supporting collections:

    ml_embeddings      — vectors for Atlas Vector Search.
    doi_mappings       — DOI -> [material_auid] dedup index.
    user_affiliations  — Mongo-backed affiliation profile for a Django user.

The raw-file backup collection (``raw_files``) lives in a separate Mongo
database (``loop_raw``) and is defined in :mod:`catalog.raw_db`.

Interior payloads (``synthesis_steps``, ``exp_condition.additional_params``,
``dft_metadata``) stay as nested ``DictField`` / ``ListField(DictField())`` so
new step types can ship without schema migrations.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from mongoengine import (
    BooleanField,
    DateTimeField,
    DictField,
    Document,
    EmbeddedDocument,
    EmbeddedDocumentField,
    FloatField,
    IntField,
    ListField,
    StringField,
    URLField,
)
from mongoengine.connection import get_db

from . import auid as auid_mod
from .auid import STRUCTURE_FAMILY_VALUES

AFFILIATION_VALUES = ("S4E", "APL", "Oak Ridge")
AFFILIATION_CHOICES = tuple((aff, aff) for aff in AFFILIATION_VALUES)

VISIBILITY_DEFAULT: List[str] = ["S4E"]

RAW_DATA_TYPE_CHOICES = ("xrd", "sem", "tem", "eds", "other", "unknown", "na")


def _utc_now() -> datetime:
    """Aware UTC timestamp for DateTimeField defaults and save() helpers.

    PyMongo is configured with ``tz_aware=True`` so reads come back as
    UTC-aware datetimes; writing aware UTC keeps the round-trip consistent
    and lets Django's ``USE_TZ`` machinery localize display correctly.
    """
    return datetime.now(timezone.utc)


def _clean_visibility(values: Optional[Iterable[str]]) -> List[str]:
    out: List[str] = []
    for item in values or []:
        if item in AFFILIATION_VALUES and item not in out:
            out.append(item)
    return out or list(VISIBILITY_DEFAULT)


# =============================================================================
# Interior embedded shapes
# =============================================================================

class ExpCondition(EmbeddedDocument):
    """Experimental synthesis condition payload for trials and literature."""

    milling_time_hours = FloatField()
    milling_rpm = FloatField()
    temp_profile = ListField(DictField())
    precursors = ListField(StringField())
    cooling_method = StringField(
        choices=["air_quench", "furnace_cool", "quench", "slow_cool", "other"]
    )
    pelletizing_pressure_mpa = FloatField()
    pelletizing_time_min = FloatField()
    atmosphere = StringField()
    oxygen_partial_pressure_bar = FloatField()

    # Multi-step synthesis payload and any extensible free-form parameters.
    # ``synthesis_steps`` used to live under ``additional_params`` for upload
    # compatibility; the recipe document still accepts that shape on input.
    additional_params = DictField()

    meta = {"strict": False}


class EmbeddedDFT(EmbeddedDocument):
    """A single DFT (or similar) calculation attached to a material."""

    comp_auid = StringField(required=True)

    dft_source = StringField()
    dft_formation_energy_ev = FloatField()
    dft_hull_distance_ev = FloatField()
    dft_bandgap_ev = FloatField()
    # Electronic properties
    bandgap_type = StringField()           # e.g. "metal", "insulator", "semiconductor"
    bandgap_fit_ev = FloatField()          # fitted bandgap [eV]

    # Elastic properties (Voigt-Reuss-Hill averages)
    bulk_modulus_vrh = FloatField()        # [GPa]
    shear_modulus_vrh = FloatField()       # [GPa]
    youngs_modulus_vrh = FloatField()      # [GPa]
    poisson_ratio = FloatField()
    elastic_anisotropy = FloatField()

    # Thermal / acoustic properties
    debye_temperature = FloatField()       # [K]
    thermal_conductivity_300k = FloatField()  # [W/m/K]
    gruneisen_parameter = FloatField()
    thermal_expansion_300k = FloatField()  # [1/K]

    # Structural
    pearson_symbol = StringField()         # e.g. "cF8"
    crystal_system = StringField()         # e.g. "cubic"
    crystal_family = StringField()         # e.g. "cubic"

    # Magnetic
    spin_atom = FloatField()               # net spin per atom [μB]

    dft_metadata = DictField()
    ml_predictions = DictField()

    # All remaining AFLOW-format properties stored verbatim (tensors, Bader charges,
    # Wyckoff data, thermodynamic tables, etc.)
    extended_data = DictField()

    spacegroup = StringField()
    element_sites = DictField()

    # Optional backlink for "this calculation models that sample".
    trial_recipe_auid = StringField()
    trial_id = StringField()

    uploaded_by = StringField()
    visibility_affiliations = ListField(StringField(choices=list(AFFILIATION_VALUES)))

    created_at = DateTimeField(default=_utc_now)
    updated_at = DateTimeField(default=_utc_now)

    meta = {"strict": False}


PHASE_STATUS_VALUES = ("single_phase", "multi_phase", "not_confirmed")


class EmbeddedTrial(EmbeddedDocument):
    """A specific experimental execution of a recipe."""

    trial_id = StringField(required=True)
    trial_date = DateTimeField(required=True)
    # Tri-state phase outcome. ``success`` is kept in sync (single_phase->True,
    # multi_phase->False, not_confirmed->None) so legacy templates and
    # aggregations keep working.
    phase_status = StringField(choices=list(PHASE_STATUS_VALUES), default="not_confirmed")
    success = BooleanField()

    exp_condition = EmbeddedDocumentField(ExpCondition, required=True)
    raw_data_link = URLField()
    raw_data_type = StringField(choices=list(RAW_DATA_TYPE_CHOICES))
    file_hash = StringField()
    # Deterministic hash of the trial's stored payload. This is a provenance
    # fingerprint; raw-file dedup happens on ``file_hash`` instead.
    content_hash = StringField()

    experimenter = StringField()
    notes = StringField()
    phases_detected = ListField(StringField())
    is_single_phase = BooleanField()

    spacegroup = StringField()
    element_sites = DictField()

    visibility_affiliations = ListField(StringField(choices=list(AFFILIATION_VALUES)))
    created_at = DateTimeField(default=_utc_now)
    updated_at = DateTimeField(default=_utc_now)

    meta = {"strict": False}


class EmbeddedLiterature(EmbeddedDocument):
    """A specific paper's report of the enclosing recipe."""

    lit_id = StringField()  # L:<12-hex hash of doi> — stable URL-safe key
    doi = StringField(required=True)
    title = StringField()
    authors = ListField(StringField())
    journal = StringField()
    year = IntField()

    synthesis_successful = BooleanField()
    exp_condition = EmbeddedDocumentField(ExpCondition)

    spacegroup = StringField()
    element_sites = DictField()

    notes = StringField()
    extracted_by = StringField()

    visibility_affiliations = ListField(StringField(choices=list(AFFILIATION_VALUES)))
    created_at = DateTimeField(default=_utc_now)
    updated_at = DateTimeField(default=_utc_now)

    meta = {"strict": False}


# =============================================================================
# Top-level documents
# =============================================================================

class Material(Document):
    """One document per (composition, structure_family)."""

    id = StringField(primary_key=True)  # material_auid, e.g. "M:afc940abcdef1234"

    elements = DictField(required=True)
    element_symbols = ListField(StringField())
    num_elements = IntField()
    structure_family = StringField(required=True, choices=list(STRUCTURE_FAMILY_VALUES))

    display_name = StringField()
    notes = StringField()
    curator = StringField()
    default_visibility_affiliations = ListField(StringField(choices=list(AFFILIATION_VALUES)))

    dft_calculations = ListField(EmbeddedDocumentField(EmbeddedDFT))

    latest_trial_date = DateTimeField()

    created_at = DateTimeField(default=_utc_now)
    updated_at = DateTimeField(default=_utc_now)

    meta = {
        "collection": "materials",
        "indexes": [
            "element_symbols",
            "structure_family",
            "num_elements",
            "-created_at",
            "-latest_trial_date",
            "dft_calculations.comp_auid",
            {"fields": ["element_symbols", "-created_at"]},
            {"fields": ["structure_family", "-created_at"]},
            {"fields": ["element_symbols", "structure_family", "-created_at"]},
        ],
        "ordering": ["-created_at"],
        "strict": False,
    }

    @property
    def material_auid(self) -> str:
        return self.id

    def save(self, *args, **kwargs):
        if self.elements:
            self.element_symbols = sorted(str(k) for k in self.elements.keys())
            self.num_elements = len(self.element_symbols)
        self.default_visibility_affiliations = _clean_visibility(
            self.default_visibility_affiliations
        )
        now = _utc_now()
        if not self.created_at:
            self.created_at = now
        self.updated_at = now
        return super().save(*args, **kwargs)


class Recipe(Document):
    """One document per unique methodology inside a material."""

    id = StringField(primary_key=True)  # recipe_auid, e.g. "M:...:R:..."
    material_auid = StringField(required=True)

    elements = DictField(required=True)
    element_symbols = ListField(StringField())
    num_elements = IntField()
    structure_family = StringField(required=True, choices=list(STRUCTURE_FAMILY_VALUES))

    synthesis_steps = ListField(DictField())

    trials = ListField(EmbeddedDocumentField(EmbeddedTrial))
    literature = ListField(EmbeddedDocumentField(EmbeddedLiterature))

    visibility_affiliations = ListField(StringField(choices=list(AFFILIATION_VALUES)))

    created_at = DateTimeField(default=_utc_now)
    updated_at = DateTimeField(default=_utc_now)

    meta = {
        "collection": "recipes",
        "indexes": [
            "material_auid",
            "element_symbols",
            "structure_family",
            "num_elements",
            "-created_at",
            "trials.trial_id",
            "trials.file_hash",
            "literature.doi",
            {"fields": ["material_auid", "-created_at"]},
        ],
        "ordering": ["-created_at"],
        "strict": False,
    }

    @property
    def recipe_auid(self) -> str:
        return self.id

    def save(self, *args, **kwargs):
        if self.elements:
            self.element_symbols = sorted(str(k) for k in self.elements.keys())
            self.num_elements = len(self.element_symbols)
        self.visibility_affiliations = _clean_visibility(self.visibility_affiliations)
        now = _utc_now()
        if not self.created_at:
            self.created_at = now
        self.updated_at = now
        result = super().save(*args, **kwargs)
        trial_dates = [t.trial_date for t in (self.trials or []) if t.trial_date]
        if trial_dates:
            get_db()["materials"].update_one(
                {"_id": self.material_auid},
                {"$max": {"latest_trial_date": max(trial_dates)}},
            )
        return result


class SynthesisPrediction(Document):
    """Versioned, auditable synthesis recommendation for one material.

    These records are deliberately separate from :class:`Recipe`: a generated
    route is useful pseudo-evidence for the composition model, but it is not an
    experiment or a literature result until somebody validates it.
    """

    id = StringField(primary_key=True)  # material_auid
    material_auid = StringField(required=True)
    elements = DictField(required=True)
    element_symbols = ListField(StringField())
    structure_family = StringField(required=True)

    methodology = StringField()
    precursors = ListField(DictField())
    route_steps = ListField(DictField())
    temperature = DictField()
    atmosphere = StringField()
    cooling_method = StringField()

    evidence = ListField(DictField())
    assumptions = ListField(StringField())
    confidence = FloatField(default=0.0)
    prediction_status = StringField(
        default="predicted",
        choices=["predicted", "verified", "rejected"],
    )
    training_eligible = BooleanField(default=True)
    training_weight = FloatField(default=0.2)
    model_version = StringField()
    source_hash = StringField()
    validation = DictField()

    created_at = DateTimeField(default=_utc_now)
    updated_at = DateTimeField(default=_utc_now)

    meta = {
        "collection": "synthesis_predictions",
        "indexes": [
            "material_auid",
            "element_symbols",
            "structure_family",
            "prediction_status",
            "training_eligible",
            "-updated_at",
        ],
        "strict": False,
    }

    def save(self, *args, **kwargs):
        self.element_symbols = sorted(str(key) for key in (self.elements or {}))
        now = _utc_now()
        if not self.created_at:
            self.created_at = now
        self.updated_at = now
        return super().save(*args, **kwargs)


# =============================================================================
# Vector embeddings (Atlas Vector Search)
# =============================================================================

class MLEmbedding(Document):
    """Vector embedding for a material, recipe, or computational record.

    Indexed by Atlas Vector Search; see ``mongo_admin ensure-vector-indexes``.
    Declared Mongo-specific boundary; the migration escape hatch is pgvector.
    """

    scope = StringField(required=True, choices=["material", "recipe", "comp"])
    material_auid = StringField(required=True)
    recipe_auid = StringField()
    comp_auid = StringField()

    composition_embedding = ListField(FloatField())
    structure_embedding = ListField(FloatField())
    synthesis_embedding = ListField(FloatField())

    model_version = StringField()
    created_at = DateTimeField(default=_utc_now)

    meta = {
        "collection": "ml_embeddings",
        "indexes": [
            "scope",
            "material_auid",
            "recipe_auid",
            "comp_auid",
            "-created_at",
        ],
        "strict": False,
    }


# =============================================================================
# Supporting collections
# =============================================================================

SYNTHESIS_JOB_STATUS_VALUES = (
    "pending",
    "processing",
    "done",
    "failed",
    "skipped",
)

XRD_ANALYSIS_JOB_STATUS_VALUES = (
    "queued",
    "running",
    "succeeded",
    "failed",
)

XRD_ANALYSIS_REVIEW_STATUS_VALUES = (
    "confirmed",
    "corrected",
    "unresolved",
    "needs-more-data",
)

XRD_ANALYSIS_PHASE_STATE_VALUES = (
    "likely single-phase",
    "likely multiphase",
    "unresolved",
    "insufficient-quality data",
)


class SynthesisParseJob(Document):
    """Queue entry for background LLM discretization of a batch synthesis route.

    Batch uploads store the free-form "Synthesis route" as a single ``other``
    step and enqueue one of these per imported trial/literature record. The
    worker (:mod:`catalog.synthesis_worker`) claims ``pending`` jobs atomically,
    calls the LLM to split ``route_text`` into typed steps, and re-keys the
    recipe. ``recipe_auid`` is updated to the new (discretized) id once done.
    """

    kind = StringField(required=True, choices=["experiment", "literature"])
    recipe_auid = StringField(required=True)
    material_auid = StringField()
    trial_id = StringField()  # experiment jobs
    lit_id = StringField()    # literature jobs
    route_text = StringField(required=True)
    username = StringField()  # uploader, so the worker preserves visibility/experimenter

    status = StringField(default="pending", choices=list(SYNTHESIS_JOB_STATUS_VALUES))
    attempts = IntField(default=0)
    last_error = StringField()

    created_at = DateTimeField(default=_utc_now)
    updated_at = DateTimeField(default=_utc_now)

    meta = {
        "collection": "synthesis_parse_jobs",
        "indexes": [
            "status",
            "recipe_auid",
            {"fields": ["status", "created_at"]},
        ],
        "ordering": ["created_at"],
        "strict": False,
    }

    def save(self, *args, **kwargs):
        self.updated_at = _utc_now()
        return super().save(*args, **kwargs)


class SynthesisParseCache(Document):
    """Cache of LLM synthesis-route parses, keyed by a hash of (model, prompt, text).

    Guarantees the same route text yields the same discrete steps across retries
    and re-imports (so content-addressable recipe AUIDs stay stable), and avoids
    paying for the same parse twice. ``_id`` is the cache key from
    :func:`catalog.llm_synthesis._cache_key`.
    """

    id = StringField(primary_key=True)
    steps = ListField(DictField())
    model = StringField()
    prompt_version = StringField()
    created_at = DateTimeField(default=_utc_now)

    meta = {
        "collection": "synthesis_parse_cache",
        "strict": False,
    }


MODEL_TRAINING_STATUS_VALUES = (
    "pending",
    "processing",
    "done",
    "failed",
    "skipped",
)


class ModelRetrainJob(Document):
    """Coalesced background request to refresh the EFA/DEED models."""

    reason = StringField()
    material_auids = ListField(StringField())
    trigger_count = IntField(default=1)
    status = StringField(default="pending", choices=list(MODEL_TRAINING_STATUS_VALUES))
    attempts = IntField(default=0)
    model_version = StringField()
    result_summary = DictField()
    last_error = StringField()
    created_at = DateTimeField(default=_utc_now)
    requested_at = DateTimeField(default=_utc_now)
    started_at = DateTimeField()
    completed_at = DateTimeField()
    updated_at = DateTimeField(default=_utc_now)

    meta = {
        "collection": "model_retrain_jobs",
        "indexes": ["status", {"fields": ["status", "requested_at"]}, "-created_at"],
        "ordering": ["created_at"],
        "strict": False,
    }

    def save(self, *args, **kwargs):
        self.updated_at = _utc_now()
        return super().save(*args, **kwargs)


class ModelVersion(Document):
    """Immutable metadata for one trained EFA/DEED model artifact."""

    id = StringField(primary_key=True)
    model_name = StringField(default="LOOP ChemScreen RF")
    artifact_path = StringField(required=True)
    targets = ListField(StringField())
    feature_names = ListField(StringField())
    training_counts = DictField()
    metrics = DictField()
    source_data_hash = StringField()
    chemscreen_commit = StringField()
    aflow_enabled = BooleanField(default=False)
    active = BooleanField(default=False)
    promoted = BooleanField(default=False)
    promotion_reason = StringField()
    created_at = DateTimeField(default=_utc_now)

    meta = {
        "collection": "model_versions",
        "indexes": ["active", "-created_at", "source_data_hash"],
        "ordering": ["-created_at"],
        "strict": False,
    }


class ModelFeedback(Document):
    """Prediction-versus-truth audit record used for reward/flag weighting."""

    id = StringField(primary_key=True)
    material_auid = StringField(required=True)
    comp_auid = StringField()
    model_version = StringField()
    outcome = StringField(required=True, choices=["reward", "flag", "unscored"])
    results = DictField()
    sample_weight = FloatField(default=1.0)
    reason = StringField()
    created_at = DateTimeField(default=_utc_now)

    meta = {
        "collection": "model_feedback",
        "indexes": ["material_auid", "model_version", "outcome", "-created_at"],
        "ordering": ["-created_at"],
        "strict": False,
    }


class AFLOWCache(Document):
    """Cached exact-species AFLUX response and aggregate model features."""

    id = StringField(primary_key=True)
    species = ListField(StringField())
    query = StringField()
    status = StringField(choices=["ok", "empty", "error"])
    records = ListField(DictField())
    summary = DictField()
    error = StringField()
    fetched_at = DateTimeField(default=_utc_now)

    meta = {
        "collection": "aflow_cache",
        "indexes": ["status", "-fetched_at"],
        "strict": False,
    }


class XRDAnalysisJob(Document):
    """Persistent background execution record for one content-addressed XRD analysis."""

    analysis_id = StringField(required=True, unique=True)

    material_auid = StringField(required=True)
    recipe_auid = StringField(required=True)
    trial_id = StringField(required=True)
    raw_file_hash = StringField()
    algorithm_version = StringField(required=True)
    configuration_version = StringField(required=True)

    status = StringField(
        required=True,
        default="queued",
        choices=list(XRD_ANALYSIS_JOB_STATUS_VALUES),
    )
    cache_hit = BooleanField(default=False)
    attempt_count = IntField(default=0)
    maximum_attempts = IntField(default=3)
    worker_identifier = StringField()
    progress_stage = StringField(default="queued")
    progress_message = StringField()
    lease_claimed_at = DateTimeField()
    lease_expires_at = DateTimeField()
    last_heartbeat_at = DateTimeField()

    created_at = DateTimeField(default=_utc_now)
    queued_at = DateTimeField(default=_utc_now)
    started_at = DateTimeField()
    completed_at = DateTimeField()

    result_manifest_relative_path = StringField()
    automated_summary = DictField()
    warnings = ListField(DictField())
    failure_codes = ListField(StringField())
    error_summary = StringField()
    diagnostic_metadata = DictField()

    meta = {
        "collection": "xrd_analysis_jobs",
        "indexes": [
            "analysis_id",
            "recipe_auid",
            "trial_id",
            "status",
            {"fields": ["status", "queued_at"]},
            {"fields": ["status", "lease_expires_at"]},
        ],
        "ordering": ["queued_at"],
        "strict": False,
    }


class XRDAnalysisReview(Document):
    """Expert review record for one persisted automated XRD analysis."""

    analysis_id = StringField(required=True)
    material_auid = StringField(required=True)
    recipe_auid = StringField(required=True)
    trial_id = StringField(required=True)

    reviewer_username = StringField(required=True)
    reviewer_display_name = StringField()
    reviewer_organization = StringField()

    review_status = StringField(
        required=True,
        choices=list(XRD_ANALYSIS_REVIEW_STATUS_VALUES),
    )
    reviewed_phase_state = StringField(
        choices=list(XRD_ANALYSIS_PHASE_STATE_VALUES),
        null=True,
    )
    selected_hypothesis_id = StringField()
    added_candidate_identifiers = ListField(StringField())
    confidence = StringField()
    notes = StringField()

    supersedes_review_id = StringField()
    is_active = BooleanField(default=True)

    created_at = DateTimeField(default=_utc_now)
    updated_at = DateTimeField(default=_utc_now)

    meta = {
        "collection": "xrd_analysis_reviews",
        "indexes": [
            "analysis_id",
            "recipe_auid",
            "trial_id",
            "review_status",
            {"fields": ["analysis_id", "-created_at"]},
            {"fields": ["analysis_id", "is_active"]},
            {"fields": ["recipe_auid", "trial_id", "-created_at"]},
        ],
        "ordering": ["-created_at"],
        "strict": False,
    }

    def save(self, *args, **kwargs):
        now = _utc_now()
        if not self.created_at:
            self.created_at = now
        self.updated_at = now
        return super().save(*args, **kwargs)


class DOIMapping(Document):
    """DOI -> list of material_auids referenced by that paper."""

    doi = StringField(required=True, unique=True)
    material_auids = ListField(StringField())
    title = StringField()
    created_at = DateTimeField(default=_utc_now)

    meta = {
        "collection": "doi_mappings",
        "indexes": ["doi"],
        "strict": False,
    }

    def __str__(self):
        return f"{self.doi} -> {len(self.material_auids or [])} materials"


class UserPrecursor(Document):
    """A saved precursor/reagent entry.

    Powers the "quick insert" dropdown on the Weighing step of the
    experimental upload form. Users can also hydrate entries from a
    CAS-number lookup against PubChem (see the ``/api/precursors/cas-lookup``
    endpoint in :mod:`catalog.views`).

    Visibility is affiliation-scoped: an entry is visible to any user who
    shares at least one affiliation with ``visibility_affiliations``. The
    ``user_id`` field records the uploader (for edit/delete authorization
    and display). The collection name is kept for backward compatibility.
    """

    user_id = IntField(required=True)
    uploaded_by_username = StringField()
    visibility_affiliations = ListField(StringField(), default=list)
    name = StringField(required=True)
    formula = StringField()
    cas_number = StringField()
    purity = StringField()
    supplier = StringField()
    notes = StringField()
    created_at = DateTimeField(default=_utc_now)
    updated_at = DateTimeField(default=_utc_now)

    meta = {
        "collection": "user_precursors",
        "indexes": [
            "user_id",
            "visibility_affiliations",
            ("user_id", "cas_number"),
            ("user_id", "name"),
            "-created_at",
        ],
        "ordering": ["-updated_at"],
        "strict": False,
    }

    def save(self, *args, **kwargs):
        self.updated_at = _utc_now()
        return super().save(*args, **kwargs)

    def to_public_dict(self, *, viewer_user_id: Optional[int] = None) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "name": self.name or "",
            "formula": self.formula or "",
            "cas_number": self.cas_number or "",
            "purity": self.purity or "",
            "supplier": self.supplier or "",
            "notes": self.notes or "",
            "uploaded_by": self.user_id,
            "uploaded_by_username": self.uploaded_by_username or "",
            "visibility_affiliations": list(self.visibility_affiliations or []),
            "is_mine": (viewer_user_id is not None and viewer_user_id == self.user_id),
        }


class UserProtocol(Document):
    """A saved multi-step synthesis protocol template.

    Mirrors :class:`UserPrecursor` in structure and visibility rules.
    ``steps`` stores the verbatim synthesis-step dicts produced by
    ``_parse_synthesis_steps_from_request``, including full precursor detail
    on weighing steps, so a protocol can be loaded back into the upload form
    without any external lookups.

    Visibility is affiliation-scoped: an entry is visible to any user who
    shares at least one affiliation with ``visibility_affiliations``.
    """

    user_id = IntField(required=True)
    uploaded_by_username = StringField()
    visibility_affiliations = ListField(StringField(), default=list)
    name = StringField(required=True)
    description = StringField()
    steps = ListField(DictField())
    created_at = DateTimeField(default=_utc_now)
    updated_at = DateTimeField(default=_utc_now)

    meta = {
        "collection": "user_protocols",
        "indexes": [
            "user_id",
            "visibility_affiliations",
            ("user_id", "name"),
            "-updated_at",
        ],
        "ordering": ["-updated_at"],
        "strict": False,
    }

    def save(self, *args, **kwargs):
        self.updated_at = _utc_now()
        return super().save(*args, **kwargs)

    def to_public_dict(self, *, viewer_user_id: Optional[int] = None) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "name": self.name or "",
            "description": self.description or "",
            "steps": list(self.steps or []),
            "uploaded_by": self.user_id,
            "uploaded_by_username": self.uploaded_by_username or "",
            "visibility_affiliations": list(self.visibility_affiliations or []),
            "is_mine": (viewer_user_id is not None and viewer_user_id == self.user_id),
        }


class UserAffiliation(Document):
    """Mongo-backed affiliation profile for a Django user."""

    user_id = IntField(required=True, unique=True)
    username = StringField(required=True)
    affiliations = ListField(
        StringField(choices=list(AFFILIATION_VALUES)),
        required=True,
        default=lambda: ["S4E"],
    )
    created_at = DateTimeField(default=_utc_now)
    updated_at = DateTimeField(default=_utc_now)

    meta = {
        "collection": "user_affiliations",
        "indexes": ["user_id", "username", "affiliations"],
        "strict": False,
    }

    def save(self, *args, **kwargs):
        self.updated_at = _utc_now()
        return super().save(*args, **kwargs)


# =============================================================================
# User affiliation helpers
# =============================================================================

def get_user_affiliations(user) -> List[str]:
    if not getattr(user, "is_authenticated", False):
        return []
    profile = UserAffiliation.objects(user_id=user.id).first()
    if profile and profile.affiliations:
        return list(profile.affiliations)
    return list(VISIBILITY_DEFAULT)


def upsert_user_affiliations(user, affiliations: List[str]) -> None:
    normalized: List[str] = []
    for aff in affiliations or []:
        value = str(aff).strip()
        if value in AFFILIATION_VALUES and value not in normalized:
            normalized.append(value)
    if not normalized:
        normalized = list(VISIBILITY_DEFAULT)

    UserAffiliation.objects(user_id=user.id).update_one(
        set__username=user.get_username(),
        set__affiliations=normalized,
        upsert=True,
    )


# =============================================================================
# Input normalization helpers shared by views + admin tooling
# =============================================================================

def normalize_elements_payload(elements) -> Dict[str, float]:
    """Parse whatever shape the form hands us into an ``{element: ratio}`` dict."""
    if isinstance(elements, dict):
        out: Dict[str, float] = {}
        for el, ratio in elements.items():
            try:
                value = float(ratio)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Non-numeric ratio for {el}: {ratio!r}") from exc
            if value <= 0:
                raise ValueError("Element ratios must be greater than 0")
            out[str(el)] = value
        if not out:
            raise ValueError("Elements dict cannot be empty")
        return out

    if isinstance(elements, list):
        out = {}
        for item in elements:
            if isinstance(item, (list, tuple)):
                if not item:
                    continue
                symbol = item[0]
                ratio = item[1] if len(item) > 1 else None
            elif isinstance(item, dict):
                symbol = item.get("symbol") or item.get("element")
                ratio = item.get("ratio") or item.get("value")
            else:
                continue
            if not symbol:
                continue
            if ratio is None or ratio == "":
                raise ValueError(f"Missing ratio for element {symbol}")
            value = float(ratio)
            if value <= 0:
                raise ValueError(f"Ratio for element {symbol} must be greater than 0")
            out[str(symbol)] = value
        if not out:
            raise ValueError("Elements dict cannot be empty")
        return out

    raise ValueError("Invalid composition payload")


def _normalize_structure_family(structure_family: str) -> str:
    return auid_mod.normalize_structure_family(structure_family)


# =============================================================================
# AUID + lookup helpers
# =============================================================================

def compute_material_auid(elements: Dict[str, float], structure_family: str) -> str:
    return auid_mod.material_auid(elements, structure_family)


def compute_recipe_auid(material: str, synthesis_steps: Optional[List[Dict[str, Any]]]) -> str:
    return auid_mod.recipe_auid(material, synthesis_steps or [])


def get_material(material_auid: str) -> Optional[Material]:
    return Material.objects(id=material_auid).first()


def get_recipe(recipe_auid: str) -> Optional[Recipe]:
    return Recipe.objects(id=recipe_auid).first()


def get_recipes_for_material(material_auid: str):
    return Recipe.objects(material_auid=material_auid).order_by("-created_at")


def find_embedded_trial(recipe: Recipe, trial_id: str) -> Optional[EmbeddedTrial]:
    if not recipe or not recipe.trials:
        return None
    for trial in recipe.trials:
        if trial.trial_id == trial_id:
            return trial
    return None


def find_embedded_literature(recipe: Recipe, lit_id: str) -> Optional[EmbeddedLiterature]:
    """Look up a literature entry by its lit_id (``L:...``).

    Falls back to a DOI case-insensitive match for records created before
    lit_id was introduced, so old admin links still resolve.
    """
    if not recipe or not recipe.literature:
        return None
    for lit in recipe.literature:
        if getattr(lit, "lit_id", None) == lit_id:
            return lit
    # Backward-compat: lit_id field absent on old embedded docs
    key_norm = (lit_id or "").strip().lower()
    for lit in recipe.literature:
        if (lit.doi or "").strip().lower() == key_norm:
            return lit
    return None


def find_embedded_dft(material: Material, comp_auid: str) -> Optional[EmbeddedDFT]:
    if not material or not material.dft_calculations:
        return None
    for dft in material.dft_calculations:
        if dft.comp_auid == comp_auid:
            return dft
    return None


__all__ = [
    "AFFILIATION_VALUES",
    "AFFILIATION_CHOICES",
    "VISIBILITY_DEFAULT",
    "RAW_DATA_TYPE_CHOICES",
    "STRUCTURE_FAMILY_VALUES",
    "PHASE_STATUS_VALUES",
    "ExpCondition",
    "EmbeddedDFT",
    "EmbeddedTrial",
    "EmbeddedLiterature",
    "Material",
    "Recipe",
    "SynthesisPrediction",
    "MLEmbedding",
    "MODEL_TRAINING_STATUS_VALUES",
    "ModelRetrainJob",
    "ModelVersion",
    "ModelFeedback",
    "AFLOWCache",
    "DOIMapping",
    "SynthesisParseJob",
    "SynthesisParseCache",
    "XRDAnalysisJob",
    "XRDAnalysisReview",
    "UserAffiliation",
    "UserPrecursor",
    "UserProtocol",
    "get_user_affiliations",
    "upsert_user_affiliations",
    "normalize_elements_payload",
    "_normalize_structure_family",
    "compute_material_auid",
    "compute_recipe_auid",
    "get_material",
    "get_recipe",
    "get_recipes_for_material",
    "find_embedded_trial",
    "find_embedded_literature",
    "find_embedded_dft",
]
