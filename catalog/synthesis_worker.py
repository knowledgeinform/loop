"""Background worker that discretizes batch synthesis routes via the LLM and
re-keys recipes through the manual edit path. Jobs are claimed atomically;
any failure leaves the original single-``other`` recipe untouched."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from django.conf import settings
from django.contrib.auth import get_user_model

from .batch_upload import normalize_synthesis_steps
from .documents import (
    MLEmbedding,
    Recipe,
    SynthesisParseJob,
    find_embedded_literature,
    find_embedded_trial,
    get_recipe,
)
from .llm_synthesis import SynthesisParseUnavailable, is_enabled, parse_synthesis_route

logger = logging.getLogger(__name__)


def enqueue_synthesis_job(
    *,
    kind: str,
    recipe_auid: str,
    material_auid: Optional[str],
    route_text: Optional[str],
    username: Optional[str],
    trial_id: Optional[str] = None,
    lit_id: Optional[str] = None,
) -> Optional[SynthesisParseJob]:
    """Enqueue a background discretization job for one imported record.

    No-op (returns ``None``) when there is no free-text route or the LLM feature
    is disabled, so shipping dark never creates orphaned pending jobs.
    """
    text = (route_text or "").strip()
    if not text or not is_enabled():
        return None
    job = SynthesisParseJob(
        kind=kind,
        recipe_auid=recipe_auid,
        material_auid=material_auid,
        route_text=text,
        username=username,
        trial_id=trial_id,
        lit_id=lit_id,
        status="pending",
    )
    job.save()
    return job


class _TerminalJobError(Exception):
    """A job that cannot succeed on retry (missing record, empty parse, …)."""


def _now():
    from .documents import _utc_now

    return _utc_now()


def _resolve_user(username: Optional[str]):
    if not username:
        return None
    return get_user_model().objects.filter(username=username).first()


def _steps_without_other(recipe: Recipe) -> List[Dict[str, Any]]:
    """The recipe's structured steps, dropping the single free-text ``other`` step."""
    return [
        step
        for step in (recipe.synthesis_steps or [])
        if str(step.get("step_type") or "").strip().lower() != "other"
    ]


def _cleanup_old_recipe(old_recipe_id: str) -> None:
    """Delete the old recipe + its embedding if the re-key left it empty."""
    old = Recipe.objects(id=old_recipe_id).first()
    if old is None:
        return
    if (old.trials or []) or (old.literature or []):
        return
    MLEmbedding.objects(scope="recipe", recipe_auid=old_recipe_id).delete()
    old.delete()


def _process_experiment(job: SynthesisParseJob) -> str:
    from .views import persist_experimental_trial

    old_recipe = get_recipe(job.recipe_auid)
    if old_recipe is None:
        raise _TerminalJobError(f"recipe {job.recipe_auid} not found")
    trial = find_embedded_trial(old_recipe, job.trial_id)
    if trial is None:
        raise _TerminalJobError(f"trial {job.trial_id} not found in {job.recipe_auid}")

    user = _resolve_user(job.username)
    if user is None:
        raise _TerminalJobError(f"uploader {job.username!r} not found")

    discretized = parse_synthesis_route(job.route_text)
    if not discretized:
        raise _TerminalJobError("model produced no steps")

    new_steps, _ = normalize_synthesis_steps(_steps_without_other(old_recipe) + discretized)

    prev_additional = getattr(getattr(trial, "exp_condition", None), "additional_params", {}) or {}
    result = persist_experimental_trial(
        user=user,
        request=None,
        raw_elements=old_recipe.elements,
        structure_family=old_recipe.structure_family,
        synthesis_steps=new_steps,
        phase_status=trial.phase_status,
        spacegroup=trial.spacegroup or "unknown",
        element_sites=trial.element_sites or {},
        raw_data_type=trial.raw_data_type,
        notes=trial.notes,
        csv_file=None,
        edit_recipe=old_recipe,
        edit_recipe_id=old_recipe.id,
        edit_trial_record=trial,
        edit_trial_id=trial.trial_id,
        source_batch_id=prev_additional.get("source_batch_id"),
    )

    new_recipe_id = result["recipe_auid"]
    if new_recipe_id != old_recipe.id:
        _cleanup_old_recipe(old_recipe.id)
    return new_recipe_id


def _process_literature(job: SynthesisParseJob) -> str:
    from .views import persist_literature_entry

    old_recipe = get_recipe(job.recipe_auid)
    if old_recipe is None:
        raise _TerminalJobError(f"recipe {job.recipe_auid} not found")
    lit = find_embedded_literature(old_recipe, job.lit_id)
    if lit is None:
        raise _TerminalJobError(f"literature {job.lit_id} not found in {job.recipe_auid}")

    user = _resolve_user(job.username)
    if user is None:
        raise _TerminalJobError(f"uploader {job.username!r} not found")

    discretized = parse_synthesis_route(job.route_text)
    if not discretized:
        raise _TerminalJobError("model produced no steps")

    new_steps, _ = normalize_synthesis_steps(_steps_without_other(old_recipe) + discretized)

    result = persist_literature_entry(
        user=user,
        raw_elements=old_recipe.elements,
        structure_family=old_recipe.structure_family,
        synthesis_steps=new_steps,
        doi=lit.doi,
        synthesis_successful=lit.synthesis_successful,
        title=lit.title,
        authors=lit.authors,
        journal=lit.journal,
        year=lit.year,
        findings=lit.notes,
        spacegroup=lit.spacegroup or "unknown",
        element_sites=lit.element_sites or {},
        existing_recipe=old_recipe,
        existing_literature=lit,
        edit_lit_id=lit.lit_id,
        edit_doi=lit.doi,
        reject_duplicates=False,
    )

    new_recipe_id = result["recipe_auid"]
    if new_recipe_id != old_recipe.id:
        _cleanup_old_recipe(old_recipe.id)
    return new_recipe_id


def _claim_next_job() -> Optional[SynthesisParseJob]:
    """Atomically claim the oldest pending job (find-one-and-update)."""
    return SynthesisParseJob.objects(status="pending").order_by("created_at").modify(
        new=True,
        set__status="processing",
        inc__attempts=1,
        set__updated_at=_now(),
    )


def process_one_job(job: SynthesisParseJob) -> str:
    """Process a claimed job, updating its status. Returns the final status."""
    try:
        if job.kind == "literature":
            new_recipe_id = _process_literature(job)
        else:
            new_recipe_id = _process_experiment(job)
    except _TerminalJobError as exc:
        job.status = "skipped"
        job.last_error = str(exc)
        job.save()
        logger.info("synthesis job %s skipped: %s", job.id, exc)
        return job.status
    except SynthesisParseUnavailable as exc:
        # Transient (API/network). Retry until the attempt budget is spent.
        max_attempts = int(getattr(settings, "SYNTHESIS_LLM_MAX_ATTEMPTS", 3))
        job.status = "pending" if job.attempts < max_attempts else "failed"
        job.last_error = str(exc)
        job.save()
        logger.warning(
            "synthesis job %s %s (attempt %s): %s",
            job.id, job.status, job.attempts, exc,
        )
        return job.status
    except Exception as exc:  # unexpected — do not lose the row, mark failed
        job.status = "failed"
        job.last_error = repr(exc)
        job.save()
        logger.exception("synthesis job %s failed unexpectedly", job.id)
        return job.status

    job.recipe_auid = new_recipe_id
    job.status = "done"
    job.last_error = None
    job.save()
    logger.info("synthesis job %s done -> %s", job.id, new_recipe_id)
    return job.status


def process_pending_jobs(limit: int = 25) -> Dict[str, int]:
    """Claim and process up to ``limit`` pending jobs. Returns status counts."""
    counts: Dict[str, int] = {"done": 0, "skipped": 0, "failed": 0, "pending": 0}
    for _ in range(max(1, limit)):
        job = _claim_next_job()
        if job is None:
            break
        status = process_one_job(job)
        counts[status] = counts.get(status, 0) + 1
    return counts
