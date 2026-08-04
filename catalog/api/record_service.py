"""Deep catalog-record operations shared by HTTP adapters."""

from catalog import batch_upload as batch_upload_mod
from catalog import auid as auid_mod, signals
from catalog.documents import (
    EmbeddedDFT,
    Material,
    compute_material_auid,
    normalize_elements_payload,
)
from catalog.upload_archive import archive_upload
from catalog.views import (
    _get_visibility_affiliations_for_create,
    _normalize_structure_family,
    _upsert_material,
    DuplicateRecordError,
    persist_experimental_trial,
    persist_literature_entry,
)
from django.utils import timezone


def create_experiment(*, actor, request, payload, csv_file=None):
    normalized, errors = batch_upload_mod.normalize_record("experiment", payload)
    if errors:
        return None, errors
    result = persist_experimental_trial(
        user=actor,
        request=request,
        raw_elements=normalized["raw_elements"],
        structure_family=normalized["structure_family"],
        synthesis_steps=normalized["synthesis_steps"],
        phase_status=normalized["phase_status"],
        spacegroup=normalized["spacegroup"],
        element_sites=normalized["element_sites"],
        raw_data_type=normalized["raw_data_type"],
        notes=normalized["notes"],
        csv_file=csv_file,
        reject_duplicates=True,
    )
    return result, []


def update_experiment(*, actor, request, recipe, trial, payload, csv_file=None):
    normalized, errors = batch_upload_mod.normalize_record("experiment", payload)
    if errors:
        return None, errors
    result = persist_experimental_trial(
        user=actor,
        request=request,
        raw_elements=normalized["raw_elements"],
        structure_family=normalized["structure_family"],
        synthesis_steps=normalized["synthesis_steps"],
        phase_status=normalized["phase_status"],
        spacegroup=normalized["spacegroup"],
        element_sites=normalized["element_sites"],
        raw_data_type=normalized["raw_data_type"],
        notes=normalized["notes"],
        csv_file=csv_file,
        edit_recipe=recipe,
        edit_trial_record=trial,
        edit_recipe_id=recipe.id,
        edit_trial_id=trial.trial_id,
    )
    return result, []


def create_literature(*, actor, payload):
    normalized, errors = batch_upload_mod.normalize_record("literature", payload)
    if errors:
        return None, errors
    result = persist_literature_entry(
        user=actor,
        raw_elements=normalized["raw_elements"],
        structure_family=normalized["structure_family"],
        synthesis_steps=normalized["synthesis_steps"],
        doi=normalized["doi"],
        synthesis_successful=normalized["synthesis_successful"],
        title=normalized["title"],
        authors=normalized["authors"],
        journal=normalized["journal"],
        year=normalized["year"],
        findings=normalized["findings"],
        spacegroup=normalized["spacegroup"],
        element_sites=normalized["element_sites"],
        reject_duplicates=True,
    )
    return result, []


def update_literature(*, actor, recipe, literature, payload):
    normalized, errors = batch_upload_mod.normalize_record("literature", payload)
    if errors:
        return None, errors
    result = persist_literature_entry(
        user=actor,
        raw_elements=normalized["raw_elements"],
        structure_family=normalized["structure_family"],
        synthesis_steps=normalized["synthesis_steps"],
        doi=normalized["doi"],
        synthesis_successful=normalized["synthesis_successful"],
        title=normalized["title"],
        authors=normalized["authors"],
        journal=normalized["journal"],
        year=normalized["year"],
        findings=normalized["findings"],
        spacegroup=normalized["spacegroup"],
        element_sites=normalized["element_sites"],
        existing_recipe=recipe,
        existing_literature=literature,
        edit_lit_id=literature.lit_id,
        edit_doi=literature.doi,
    )
    return result, []


def create_computational(*, actor, payload, existing_material=None, existing_comp=None):
    if existing_material is not None:
        raw_elements = normalize_elements_payload(existing_material.elements)
        structure_family = _normalize_structure_family(existing_material.structure_family)
    else:
        raw_elements = normalize_elements_payload(payload["elements"])
        structure_family = _normalize_structure_family(payload["structure_family"])
    material_auid = compute_material_auid(raw_elements, structure_family)
    dft_inputs = {
        "calculation_method": payload.get("calculation_method", ""),
        "functional": payload.get("functional", ""),
        "pseudopotential": payload.get("pseudopotential", ""),
        "k_points": payload.get("k_points", ""),
        "cutoff_energy": payload.get("cutoff_energy"),
        "dft_source": payload.get("dft_source", ""),
    }
    comp_auid = auid_mod.comp_auid(material_auid, dft_inputs)
    if existing_comp is None:
        current_material = Material.objects(id=material_auid).only("dft_calculations").first()
        if current_material is not None and any(
            item.comp_auid == comp_auid
            for item in (current_material.dft_calculations or [])
        ):
            raise DuplicateRecordError(
                f"Computational record {comp_auid} already exists for {material_auid}."
            )
    visibility = _get_visibility_affiliations_for_create(actor)
    material = _upsert_material(
        material_auid=material_auid,
        elements=raw_elements,
        structure_family=structure_family,
        default_visibility_affiliations=visibility,
    )
    dft = EmbeddedDFT(
        comp_auid=comp_auid,
        dft_source=payload.get("dft_source", ""),
        dft_formation_energy_ev=payload.get("formation_energy_ev"),
        dft_hull_distance_ev=payload.get("hull_distance_ev"),
        dft_bandgap_ev=payload.get("bandgap_ev"),
        bandgap_type=payload.get("bandgap_type") or None,
        bandgap_fit_ev=payload.get("bandgap_fit_ev"),
        bulk_modulus_vrh=payload.get("bulk_modulus_vrh"),
        shear_modulus_vrh=payload.get("shear_modulus_vrh"),
        youngs_modulus_vrh=payload.get("youngs_modulus_vrh"),
        poisson_ratio=payload.get("poisson_ratio"),
        elastic_anisotropy=payload.get("elastic_anisotropy"),
        debye_temperature=payload.get("debye_temperature"),
        thermal_conductivity_300k=payload.get("thermal_conductivity_300k"),
        gruneisen_parameter=payload.get("gruneisen_parameter"),
        thermal_expansion_300k=payload.get("thermal_expansion_300k"),
        pearson_symbol=payload.get("pearson_symbol") or None,
        crystal_system=payload.get("crystal_system") or None,
        crystal_family=payload.get("crystal_family") or None,
        spin_atom=payload.get("spin_atom"),
        dft_metadata={k: v for k, v in dft_inputs.items() if k != "dft_source" and v not in (None, "")},
        ml_predictions=payload.get("ml_predictions") or {},
        extended_data=payload.get("extended_data") or {},
        spacegroup=payload.get("spacegroup") or "unknown",
        element_sites=payload.get("element_sites") or {},
        uploaded_by=actor.username,
        visibility_affiliations=visibility,
    )
    material.dft_calculations = [
        existing for existing in (material.dft_calculations or [])
        if existing.comp_auid != comp_auid
        and not (existing_comp is not None and existing.comp_auid == existing_comp.comp_auid)
    ] + [dft]
    material.save()
    if existing_comp is not None and existing_comp.comp_auid != comp_auid:
        signals.delete_comp_embedding(existing_comp.comp_auid)
    signals.refresh_comp_embedding(material, dft)
    archive_upload(
        upload_type="computational",
        username=actor.username,
        timestamp=timezone.localtime(timezone.now()),
        metadata={
            "type": "computational",
            "comp_auid": comp_auid,
            "material_auid": material_auid,
            "uploaded_by": actor.username,
            "elements": raw_elements,
            "structure_family": structure_family,
            **{k: v for k, v in payload.items() if k not in {"elements", "structure_family"}},
        },
    )
    return {"material_auid": material_auid, "comp_auid": comp_auid}


def update_computational(*, actor, material, computation, payload):
    return create_computational(
        actor=actor,
        payload=payload,
        existing_material=material,
        existing_comp=computation,
    )


def delete_experiment(*, recipe, trial_id):
    recipe.trials = [
        trial for trial in (recipe.trials or []) if trial.trial_id != trial_id
    ]
    recipe.save()


def delete_literature(*, recipe, lit_id):
    recipe.literature = [
        item for item in (recipe.literature or []) if item.lit_id != lit_id
    ]
    recipe.save()


def delete_computational(*, material, comp_auid):
    material.dft_calculations = [
        item
        for item in (material.dft_calculations or [])
        if item.comp_auid != comp_auid
    ]
    material.save()
    signals.delete_comp_embedding(comp_auid)
