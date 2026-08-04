"""Batch literature upload: each manifest row is one paper, committed through
``persist_literature_entry`` so the batch path shares DOI dedup, recipe upsert,
and archiving with the manual form.

Two input shapes are accepted. A CSV/Excel manifest carries one paper per row
with a free-text ``Synthesis route``. A JSON/JSONL upload carries the richer
record shape the API's ``/imports/`` endpoint accepts -- an ``elements`` map and
a structured ``synthesis_steps`` list -- and those structured fields are passed
through intact rather than being flattened into text."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from typing import Any

from django.core.files.storage import default_storage

from catalog.auid import STRUCTURE_FAMILY_VALUES
from catalog.batch_upload import (
    _coerce_bool,
    _coerce_int,
    _normalize_authors,
    _text_or_na,
    normalize_synthesis_steps,
)
from catalog.documents import normalize_elements_payload
from catalog.services.batch_experiment_upload import (
    load_manifest_text,
    parse_composition_formula,
)

JSON_SUFFIXES = (".json",)
JSONL_SUFFIXES = (".jsonl", ".ndjson")
ALL_JSON_SUFFIXES = JSON_SUFFIXES + JSONL_SUFFIXES


MANIFEST_HEADERS = [
    "DOI",
    "Target composition",
    "Structure Family",
    "Synthesis successful",
    "Synthesis route",
    "Title",
    "Authors",
    "Journal",
    "Year",
    "Findings",
    "Spacegroup",
]


@dataclass
class LitManifestRow:
    doi: str = ""
    target_composition: str = ""
    structure_family: str = ""
    synthesis_successful: str = ""
    synthesis_route: str = ""
    title: str = ""
    authors: str = ""
    journal: str = ""
    year: str = ""
    findings: str = ""
    spacegroup: str = ""

    # Structured fields, populated only by JSON/JSONL uploads. When present they
    # take precedence over their flattened text equivalents above, which are kept
    # populated purely so the preview table renders something readable.
    elements_map: dict[str, Any] | None = None
    raw_synthesis_steps: list[Any] | None = None
    element_sites: dict[str, Any] | None = None
    authors_list: list[str] | None = None


@dataclass
class LitBatchItem:
    index: int
    row: LitManifestRow
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class LitBatchPreview:
    items: list[LitBatchItem]
    total_rows: int
    valid_count: int
    warning_count: int
    error_count: int
    # Set when a file parsed cleanly but yielded no rows at all, so the page can
    # explain why instead of telling the user to "fix errors" that do not exist.
    empty_reason: str = ""


def parse_literature_manifest_csv(file_obj) -> list[LitManifestRow]:
    text = load_manifest_text(file_obj, "DOI")

    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return []

    header_index = 0
    for index, line in enumerate(lines[:15]):
        if "DOI" in line:
            header_index = index
            break

    text = "\n".join(lines[header_index:])

    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel

    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    rows: list[LitManifestRow] = []

    for raw_row in reader:
        cleaned = {
            str(key or "").strip(): str(value or "").strip()
            for key, value in raw_row.items()
        }

        def pick(*names: str) -> str:
            for name in names:
                if cleaned.get(name):
                    return cleaned[name]
            return ""

        doi = pick("DOI", "doi")
        composition = pick("Target composition", "composition")
        if not doi and not composition:
            continue

        rows.append(
            LitManifestRow(
                doi=doi,
                target_composition=composition,
                structure_family=pick("Structure Family", "structure_family"),
                synthesis_successful=pick("Synthesis successful", "synthesis_successful"),
                synthesis_route=pick("Synthesis route", "synthesis_route"),
                title=pick("Title", "title"),
                authors=pick("Authors", "authors"),
                journal=pick("Journal", "journal"),
                year=pick("Year", "year"),
                findings=pick("Findings", "findings"),
                spacegroup=pick("Spacegroup", "spacegroup"),
            )
        )

    return rows


def _format_number(value: Any) -> str:
    """Render 7.0 as "7" and 0.4 as "0.4" for display formulas."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(number)) if number == int(number) else str(number)


def format_elements_formula(elements: dict[str, Any]) -> str:
    """Build a display-only formula string, e.g. ``Y2Ti0.4Zr0.4O7``.

    Cosmetic: validation and commit both read ``elements_map`` directly, so this
    string never has to survive a round trip through the formula parser.
    """
    parts = []
    for element, ratio in elements.items():
        rendered = _format_number(ratio)
        parts.append(f"{element}{'' if rendered == '1' else rendered}")
    return "".join(parts)


def summarize_synthesis_steps(steps: list[Any]) -> str:
    """One-line summary of structured steps for the preview's Synthesis column."""
    names = [
        str(step.get("step_type") or "?").strip()
        for step in steps
        if isinstance(step, dict)
    ]
    return " → ".join(names)


def _row_from_json_record(record: dict[str, Any]) -> LitManifestRow:
    def text(*keys: str) -> str:
        for key in keys:
            if key in record:
                value = record[key]
                if value is None:
                    continue
                rendered = str(value).strip()
                if rendered:
                    return rendered
        return ""

    elements = record.get("elements")
    elements_map = elements if isinstance(elements, dict) and elements else None

    raw_steps = record.get("synthesis_steps")
    raw_synthesis_steps = raw_steps if isinstance(raw_steps, list) and raw_steps else None

    sites = record.get("element_sites")
    element_sites = sites if isinstance(sites, dict) else None

    authors = record.get("authors")
    authors_list = (
        [str(a).strip() for a in authors if str(a).strip()]
        if isinstance(authors, (list, tuple))
        else None
    )

    composition = text("target_composition", "composition", "formula")
    if not composition and elements_map:
        composition = format_elements_formula(elements_map)

    route = text("synthesis_route", "route")
    if not route and raw_synthesis_steps:
        route = summarize_synthesis_steps(raw_synthesis_steps)

    return LitManifestRow(
        doi=text("doi", "DOI"),
        target_composition=composition,
        structure_family=text("structure_family", "Structure Family"),
        synthesis_successful=text("synthesis_successful", "Synthesis successful"),
        synthesis_route=route,
        title=text("title"),
        authors="; ".join(authors_list) if authors_list else text("authors"),
        journal=text("journal"),
        year=text("year"),
        findings=text("findings"),
        spacegroup=text("spacegroup"),
        elements_map=elements_map,
        raw_synthesis_steps=raw_synthesis_steps,
        element_sites=element_sites,
        authors_list=authors_list,
    )


def _records_from_json_text(text: str, *, is_lines: bool) -> list[Any]:
    """Extract the record list from JSON or JSONL text.

    Mirrors the shapes ``ImportSerializer`` accepts so the page and the API agree
    on what a valid upload looks like: a bare object, an array, or ``{"records":
    [...]}``.
    """
    if is_lines:
        records: list[Any] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on line {line_number}: {exc.msg}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"Line {line_number} must be a JSON object.")
            records.append(value)
        return records

    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON at line {exc.lineno}: {exc.msg}") from exc

    if isinstance(value, dict) and isinstance(value.get("records"), list):
        return value["records"]
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return value
    raise ValueError("JSON must contain an object or an array of objects.")


def parse_literature_json(file_obj, *, filename: str = "") -> list[LitManifestRow]:
    raw = file_obj.read()
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("JSON uploads must be UTF-8 encoded.") from exc
    else:
        text = raw

    name = (filename or getattr(file_obj, "name", "") or "").lower()
    records = _records_from_json_text(text, is_lines=name.endswith(JSONL_SUFFIXES))

    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            raise ValueError(f"Record {index} must be a JSON object.")

    return [_row_from_json_record(record) for record in records]


def is_json_upload(filename: str) -> bool:
    return (filename or "").lower().endswith(ALL_JSON_SUFFIXES)


def parse_literature_manifest(file_obj, *, filename: str = "") -> list[LitManifestRow]:
    """Dispatch on file extension: JSON/JSONL keep structure, CSV/XLSX do not."""
    name = filename or getattr(file_obj, "name", "") or ""
    if is_json_upload(name):
        return parse_literature_json(file_obj, filename=name)
    return parse_literature_manifest_csv(file_obj)


def resolve_structure_family(row: LitManifestRow, default_structure_family: str) -> str:
    return (row.structure_family or default_structure_family or "unknown").strip().lower()


def build_synthesis_steps(row: LitManifestRow) -> tuple[list[dict[str, Any]], list[str]]:
    # Structured steps win: a JSON upload carrying real step objects must not be
    # collapsed into the single free-text step the CSV path produces.
    if row.raw_synthesis_steps:
        return normalize_synthesis_steps(row.raw_synthesis_steps)
    if not row.synthesis_route:
        return [], []
    return normalize_synthesis_steps(
        [{"step_type": "other", "description": row.synthesis_route}]
    )


def resolve_elements(row: LitManifestRow) -> dict[str, float]:
    """Elements for a row, from the structured map when present, else the formula."""
    if row.elements_map is not None:
        return normalize_elements_payload(row.elements_map)
    return parse_composition_formula(row.target_composition)


def validate_item(item: LitBatchItem) -> None:
    row = item.row

    if not row.doi:
        item.errors.append("Missing DOI.")

    if row.elements_map is not None:
        try:
            normalize_elements_payload(row.elements_map)
        except ValueError as exc:
            item.errors.append(f"Invalid elements: {exc}")
    elif not row.target_composition:
        item.errors.append("Missing target composition.")
    else:
        try:
            parse_composition_formula(row.target_composition)
        except ValueError as exc:
            item.errors.append(str(exc))

    if row.structure_family and resolve_structure_family(row, "unknown") not in STRUCTURE_FAMILY_VALUES:
        item.errors.append(f"Invalid Structure Family: {row.structure_family!r}")

    if not row.synthesis_successful:
        item.warnings.append("Missing 'Synthesis successful'; defaulting to false.")

    _, step_errors = build_synthesis_steps(row)
    item.errors.extend(step_errors)


def build_preview(rows: list[LitManifestRow]) -> LitBatchPreview:
    items: list[LitBatchItem] = []
    for index, row in enumerate(rows):
        item = LitBatchItem(index=index, row=row)
        validate_item(item)
        items.append(item)

    return LitBatchPreview(
        items=items,
        total_rows=len(rows),
        valid_count=len([i for i in items if not i.errors]),
        warning_count=len([i for i in items if i.warnings]),
        error_count=len([i for i in items if i.errors]),
    )


def create_preview(*, manifest_file, filename: str = "") -> LitBatchPreview:
    name = filename or getattr(manifest_file, "name", "") or ""
    rows = parse_literature_manifest(manifest_file, filename=name)
    preview = build_preview(rows)

    if preview.total_rows == 0:
        # Without this the page renders 0/0/0/0 and then tells the user to fix
        # errors that were never generated -- the failure mode that sent JSON
        # uploads into a dead end.
        preview.empty_reason = (
            "The file contained no records."
            if is_json_upload(name)
            else (
                "No data rows were found. This looks like it is not a CSV or Excel "
                "manifest -- a manifest needs a header row containing a 'DOI' column. "
                "If you are uploading JSON, save it with a .json, .jsonl, or .ndjson "
                "extension and upload it again."
            )
        )

    return preview


def generate_manifest_template_csv() -> str:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=MANIFEST_HEADERS)
    writer.writeheader()
    return output.getvalue()


def commit_batch_literature(
    *,
    manifest_path: str,
    user,
    structure_family: str,
) -> dict[str, Any]:
    """Import a literature batch via the shared ``persist_literature_entry`` path."""
    # Lazy import to avoid the views <-> services import cycle.
    from catalog.synthesis_worker import enqueue_synthesis_job
    from catalog.views import DuplicateRecordError, persist_literature_entry

    with default_storage.open(manifest_path, "rb") as manifest_file:
        # persist_uploaded_file keeps the original filename (and extension) in the
        # stored path, so the same dispatch works on the commit leg.
        rows = parse_literature_manifest(manifest_file, filename=manifest_path)

    preview = build_preview(rows)

    created_records = 0
    skipped = 0
    skipped_details: list[str] = []

    for item in preview.items:
        if item.errors:
            skipped += 1
            skipped_details.append(f"row {item.index + 1}: {'; '.join(item.errors)}")
            continue

        row = item.row
        row_structure_family = resolve_structure_family(row, structure_family)
        elements = resolve_elements(row)
        synthesis_steps, _ = build_synthesis_steps(row)

        try:
            result = persist_literature_entry(
                user=user,
                raw_elements=elements,
                structure_family=row_structure_family,
                synthesis_steps=synthesis_steps,
                doi=row.doi,
                synthesis_successful=_coerce_bool(row.synthesis_successful),
                title=_text_or_na(row.title),
                # Pass the list through when we have one: splitting a JSON author
                # list on commas would mangle "Liu, Hu-Lin"-style names.
                authors=_normalize_authors(
                    row.authors_list if row.authors_list is not None else row.authors
                ),
                journal=_text_or_na(row.journal),
                year=_coerce_int(row.year),
                findings=_text_or_na(row.findings),
                spacegroup=(row.spacegroup or "").strip() or "unknown",
                element_sites=row.element_sites or {},
                reject_duplicates=True,
            )
        except DuplicateRecordError as exc:
            skipped += 1
            skipped_details.append(f"row {item.index + 1} ({row.doi}): {exc}")
            continue

        created_records += 1

        # Queue background AI discretization of the free-text route (no-op unless
        # the LLM feature is enabled). See catalog/synthesis_worker.py.
        #
        # Rows that arrived with structured steps are skipped: their
        # ``synthesis_route`` is only a display summary ("weighing -> ball_milling
        # -> ..."), and the worker merges its output with the steps already on the
        # recipe, so discretizing it would append LLM-invented duplicates of steps
        # we were handed correctly in the first place.
        enqueue_synthesis_job(
            kind="literature",
            recipe_auid=result["recipe_auid"],
            material_auid=result["material_auid"],
            route_text="" if row.raw_synthesis_steps else row.synthesis_route,
            username=user.get_username() if user is not None else None,
            lit_id=result["lit_id"],
        )

    return {
        "created_records": created_records,
        "skipped": skipped,
        "skipped_details": skipped_details,
    }
