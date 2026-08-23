"""
Management command: import_chaos_data

Reads all AFLOW-format data from the CHAOS SQLite database (256 sharded tables,
~211 k records, 230 columns) and upserts it into the MongoDB materials collection
as EmbeddedDFT documents.

Usage:
    python manage.py import_chaos_data                        # full upsert
    python manage.py import_chaos_data --incremental          # skip existing
    python manage.py import_chaos_data --dry-run --table auid_00 --limit 5

Performance notes
-----------------
The command does a two-pass design to avoid the O(N²) read-modify-write pattern
that grows exponentially as embedded arrays get larger:

  Pass 1 – Read all 256 SQLite tables into memory, grouping DFT dicts by
            material_auid.  No MongoDB reads.

  Pass 2 – One bulk_write per BATCH_SIZE materials.  Each material gets a
            single $set that replaces its dft_calculations array entirely.
            No document reads, no repeated growing-array writes.

This reduces MongoDB write volume from O(N × avg_array_size) to O(N).
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from pymongo import UpdateOne

logger = logging.getLogger(__name__)

BATCH_SIZE = 500  # materials per bulk_write call

# ---------------------------------------------------------------------------
# CHAOS column → EmbeddedDFT typed field mappings
# ---------------------------------------------------------------------------

TYPED_FIELD_MAP: Dict[str, str] = {
    "enthalpy_formation_atom": "dft_formation_energy_ev",
    "Egap":                    "dft_bandgap_ev",
    "Egap_type":               "bandgap_type",
    "Egap_fit":                "bandgap_fit_ev",
    "ael_bulk_modulus_vrh":    "bulk_modulus_vrh",
    "ael_shear_modulus_vrh":   "shear_modulus_vrh",
    "ael_youngs_modulus_vrh":  "youngs_modulus_vrh",
    "ael_poisson_ratio":       "poisson_ratio",
    "ael_elastic_anisotropy":  "elastic_anisotropy",
    "agl_debye":               "debye_temperature",
    "agl_thermal_conductivity_300K": "thermal_conductivity_300k",
    "agl_gruneisen":           "gruneisen_parameter",
    "agl_thermal_expansion_300K": "thermal_expansion_300k",
    "Pearson_symbol_relax":    "pearson_symbol",
    "crystal_system":          "crystal_system",
    "crystal_family":          "crystal_family",
    "spin_atom":               "spin_atom",
}

# Columns stored in dft_metadata (calculation provenance).
DFT_METADATA_COLS = frozenset({
    "code", "dft_type", "energy_cutoff", "kpoints", "kpoints_relax",
    "kpoints_static", "ldau_type", "ldau_u", "ldau_j", "ldau_l", "ldau_TLUJ",
    "species_pp", "species_pp_AUID", "species_pp_ZVAL", "species_pp_version",
    "calculation_cores", "calculation_memory", "calculation_time",
    "node_CPU_Model", "node_CPU_MHz", "node_CPU_Cores", "node_RAM_GB",
    "metagga", "aflow_version", "aflowlib_version", "aflowlib_date",
    "data_api", "data_source",
})

# Columns handled explicitly and therefore excluded from extended_data.
HANDLED_COLS = (
    frozenset(TYPED_FIELD_MAP.keys())
    | DFT_METADATA_COLS
    | frozenset({
        "auid", "aurl", "compound", "species", "stoichiometry",
        "spacegroup_relax",   # stored in spacegroup field
        "nspecies",           # derivable from elements
    })
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        f = float(value)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def _parse_json_col(value: Any) -> Any:
    """Try to parse a TEXT column that may be a JSON array/object."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return value
    return value


def _parse_elements(row: dict) -> Optional[Dict[str, float]]:
    """Build {symbol: ratio} from the species + stoichiometry columns."""
    species_raw = row.get("species")
    stoich_raw = row.get("stoichiometry")
    if species_raw and stoich_raw:
        try:
            species = json.loads(species_raw) if isinstance(species_raw, str) else species_raw
            stoich = json.loads(stoich_raw) if isinstance(stoich_raw, str) else stoich_raw
            if species and stoich and len(species) == len(stoich):
                elements = {str(sym).strip(): float(r) for sym, r in zip(species, stoich) if float(r) > 0}
                if elements:
                    return elements
        except Exception:
            pass
    # Fallback: parse compound formula "Mn1Ni1Ti1V1W1"
    compound = row.get("compound", "")
    if compound:
        matches = re.findall(r'([A-Z][a-z]?)(\d+)', compound)
        if matches:
            return {sym: float(n) for sym, n in matches}
    return None


def _build_dft_metadata(row: dict) -> Dict[str, Any]:
    meta: Dict[str, Any] = {}
    for col in DFT_METADATA_COLS:
        val = row.get(col)
        if val is None or val == "":
            continue
        parsed = _parse_json_col(val)
        if parsed is not None and parsed != "":
            meta[col] = parsed
    return meta


def _build_extended_data(row: dict, chaos_auid: str) -> Dict[str, Any]:
    """All non-handled, non-null columns + the CHAOS auid for dedup."""
    out: Dict[str, Any] = {"auid": chaos_auid}
    for col, val in row.items():
        if col in HANDLED_COLS or col.startswith("_"):
            continue
        if val is None or val == "":
            continue
        parsed = _parse_json_col(val)
        if parsed is not None and parsed != "":
            out[col] = parsed
    return out


def _get_auid_tables(conn: sqlite3.Connection) -> List[str]:
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    return [r[0] for r in cur.fetchall() if re.match(r'^auid_[0-9a-f]{2}$', r[0])]


def _build_dft_dict(row: dict, mat_auid: str, comp_auid: str, chaos_auid: str) -> dict:
    """Serialize a CHAOS row into a raw dict matching the EmbeddedDFT schema."""
    sg_raw = row.get("spacegroup_relax")
    spacegroup_str = str(int(float(sg_raw))) if sg_raw else "unknown"

    return {
        "comp_auid": comp_auid,
        "dft_source": "S4E",
        "dft_formation_energy_ev": _safe_float(row.get("enthalpy_formation_atom")),
        "dft_bandgap_ev": _safe_float(row.get("Egap")),
        "bandgap_type": row.get("Egap_type") or None,
        "bandgap_fit_ev": _safe_float(row.get("Egap_fit")),
        "bulk_modulus_vrh": _safe_float(row.get("ael_bulk_modulus_vrh")),
        "shear_modulus_vrh": _safe_float(row.get("ael_shear_modulus_vrh")),
        "youngs_modulus_vrh": _safe_float(row.get("ael_youngs_modulus_vrh")),
        "poisson_ratio": _safe_float(row.get("ael_poisson_ratio")),
        "elastic_anisotropy": _safe_float(row.get("ael_elastic_anisotropy")),
        "debye_temperature": _safe_float(row.get("agl_debye")),
        "thermal_conductivity_300k": _safe_float(row.get("agl_thermal_conductivity_300K")),
        "gruneisen_parameter": _safe_float(row.get("agl_gruneisen")),
        "thermal_expansion_300k": _safe_float(row.get("agl_thermal_expansion_300K")),
        "pearson_symbol": row.get("Pearson_symbol_relax") or None,
        "crystal_system": row.get("crystal_system") or None,
        "crystal_family": row.get("crystal_family") or None,
        "spin_atom": _safe_float(row.get("spin_atom")),
        "spacegroup": spacegroup_str,
        "element_sites": {},
        "dft_metadata": _build_dft_metadata(row),
        "ml_predictions": {},
        "extended_data": _build_extended_data(row, chaos_auid),
        "uploaded_by": "import",
        "visibility_affiliations": ["S4E"],
    }


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------

class Command(BaseCommand):
    help = "Import AFLOW-format computational data from the CHAOS SQLite database into MongoDB."

    def _export_materials_to_archive(self) -> None:
        """Re-emit every material into the JSON archive after a bulk import.

        Uses the shared export path so the files are byte-identical to what the
        signal hooks would have written. Non-fatal: the import has already
        succeeded, and `manage.py loop_archive export` can be re-run by hand.
        """
        try:
            from catalog.archive import rebuild as archive_rebuild
            from catalog.archive import writer as archive_writer

            if archive_writer.archive_root() is None:
                return
            self.stdout.write("  Mirroring materials into the JSON archive...")
            counts = archive_rebuild.export(["material"])["material"]
            self.stdout.write(
                f"  Archive: {counts.written} written, {counts.unchanged} unchanged, "
                f"{counts.failed} failed."
            )
        except Exception as exc:
            self.stdout.write(
                self.style.WARNING(
                    f"  Archive mirror failed ({exc}). Run "
                    "`manage.py loop_archive export --kind material` to repair."
                )
            )

    def add_arguments(self, parser):
        parser.add_argument(
            "--incremental",
            action="store_true",
            default=False,
            help="Skip records whose CHAOS auid already exists in MongoDB.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Parse and report what would be imported; make no writes.",
        )
        parser.add_argument(
            "--table",
            metavar="auid_XX",
            default=None,
            help="Process only this table (e.g. auid_00). Useful for debugging.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Stop after processing this many rows total.",
        )

    def handle(self, *args, **options):
        from catalog import auid as auid_mod
        from catalog.documents import (
            Material,
            compute_material_auid,
            normalize_elements_payload,
        )
        from mongoengine.connection import get_db

        db_path = getattr(settings, "CHAOS_DB_PATH", "")
        if not db_path:
            raise CommandError("CHAOS_DB_PATH is not configured in settings.")
        if not os.path.exists(db_path):
            raise CommandError(f"CHAOS database not found at: {db_path}")

        incremental = options["incremental"]
        dry_run = options["dry_run"]
        only_table = options["table"]
        limit = options["limit"]

        self.stdout.write(
            f"[import_chaos_data] db={db_path} incremental={incremental} dry_run={dry_run}"
        )
        start_time = time.monotonic()

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        try:
            tables = _get_auid_tables(conn)
        except Exception as exc:
            conn.close()
            raise CommandError(f"Failed to list tables: {exc}") from exc

        if only_table:
            if only_table not in tables:
                conn.close()
                raise CommandError(f"Table '{only_table}' not found.")
            tables = [only_table]

        db = get_db()
        mat_coll = db[Material._meta["collection"]]
        now = datetime.now(tz=timezone.utc)

        # ------------------------------------------------------------------
        # Incremental mode: pre-scan all existing CHAOS AUIDs in one pass.
        # This replaces the per-material document read in the old approach.
        # ------------------------------------------------------------------
        existing_chaos_auids: set = set()
        if incremental:
            self.stdout.write("  [incremental] Scanning existing CHAOS AUIDs...")
            t_scan = time.monotonic()
            pipeline = [
                {"$unwind": "$dft_calculations"},
                {"$project": {"_id": 0, "auid": "$dft_calculations.extended_data.auid"}},
                {"$match": {"auid": {"$ne": None}}},
            ]
            for row in mat_coll.aggregate(pipeline, allowDiskUse=True):
                if row.get("auid"):
                    existing_chaos_auids.add(row["auid"])
            self.stdout.write(
                f"  [incremental] Found {len(existing_chaos_auids):,} existing records "
                f"({time.monotonic() - t_scan:.1f}s)"
            )

        # ------------------------------------------------------------------
        # Pass 1: Read all SQLite tables into memory.
        # Builds: mat_data[mat_auid] = {"elements": ..., "symbols": ..., "dft_dicts": [...]}
        # ------------------------------------------------------------------
        self.stdout.write("Pass 1: reading SQLite tables...")
        t0_pass1 = time.monotonic()

        # mat_data[mat_auid] = (normalized_elements, dft_dicts_list)
        mat_data: Dict[str, Tuple[Dict[str, float], List[dict]]] = {}
        total_rows = 0
        total_skipped = 0
        total_errors = 0

        for table in tables:
            if limit is not None and total_rows >= limit:
                break

            try:
                cur = conn.execute(f'SELECT * FROM "{table}"')  # noqa: S608
                rows = [dict(r) for r in cur.fetchall()]
            except Exception as exc:
                self.stderr.write(f"  [WARN] Could not read {table}: {exc}")
                continue

            for row in rows:
                if limit is not None and total_rows >= limit:
                    break

                chaos_auid = (row.get("auid") or "").strip()
                if not chaos_auid:
                    total_errors += 1
                    continue

                if incremental and chaos_auid in existing_chaos_auids:
                    total_skipped += 1
                    continue

                elements = _parse_elements(row)
                if not elements:
                    total_errors += 1
                    continue

                try:
                    normalized = normalize_elements_payload(elements)
                    mat_auid = compute_material_auid(normalized, "unknown")
                    comp_auid = auid_mod.comp_auid(mat_auid, {"auid": chaos_auid})
                except Exception:
                    total_errors += 1
                    continue

                if dry_run:
                    self.stdout.write(
                        f"    [DRY RUN] auid={chaos_auid} compound={row.get('compound')} "
                        f"mat_auid={mat_auid}"
                    )
                    total_rows += 1
                    continue

                dft_dict = _build_dft_dict(row, mat_auid, comp_auid, chaos_auid)

                if mat_auid not in mat_data:
                    mat_data[mat_auid] = (normalized, [])
                mat_data[mat_auid][1].append(dft_dict)
                total_rows += 1

            self.stdout.write(
                f"  {table}: {len(rows)} rows read "
                f"(running total: {total_rows:,} kept, {total_skipped:,} skipped, "
                f"{total_errors} errors)"
            )

        conn.close()
        pass1_elapsed = time.monotonic() - t0_pass1
        self.stdout.write(
            f"Pass 1 done in {pass1_elapsed:.1f}s. "
            f"{len(mat_data):,} unique materials, {total_rows:,} DFT records."
        )

        if dry_run:
            self.stdout.write(self.style.SUCCESS("[DRY RUN] No writes performed."))
            return

        # ------------------------------------------------------------------
        # Pass 2: Bulk-write to MongoDB.
        #
        # For non-incremental: two phases so $pull and $push are never mixed
        # in the same unordered batch (ordering within a document must be
        # guaranteed):
        #   Phase 2a — $pull any existing CHAOS records for each material so
        #              a reimport doesn't create duplicates.  Preserves records
        #              from other sources (e.g. user uploads).
        #   Phase 2b — $push the new records + $setOnInsert base material fields.
        #
        # For incremental: single phase — $push only, new records guaranteed
        # distinct by the upfront chaos_auid pre-scan.
        # ------------------------------------------------------------------
        self.stdout.write(f"Pass 2: writing {len(mat_data):,} materials to MongoDB...")
        t0_pass2 = time.monotonic()

        total_imported = 0
        mat_items = list(mat_data.items())

        def _flush(ops: List[UpdateOne]) -> None:
            if ops:
                mat_coll.bulk_write(ops, ordered=False)

        if not incremental:
            # Phase 2a: pull stale CHAOS-sourced records so reimport is idempotent.
            self.stdout.write("  Phase 2a: removing stale CHAOS records...")
            pull_ops: List[UpdateOne] = []
            for mat_auid, (_, dft_dicts) in mat_items:
                comp_auids = [d["comp_auid"] for d in dft_dicts]
                pull_ops.append(UpdateOne(
                    {"_id": mat_auid},
                    {"$pull": {"dft_calculations": {"comp_auid": {"$in": comp_auids}}}},
                ))
                if len(pull_ops) >= BATCH_SIZE:
                    _flush(pull_ops)
                    pull_ops = []
            _flush(pull_ops)
            self.stdout.write("  Phase 2a done.")

        # Phase 2b: upsert base material doc + push DFT records.
        self.stdout.write("  Phase 2b: pushing DFT records...")
        push_ops: List[UpdateOne] = []
        for i, (mat_auid, (normalized, dft_dicts)) in enumerate(mat_items):
            symbols = sorted(str(k) for k in normalized.keys())
            push_ops.append(UpdateOne(
                {"_id": mat_auid},
                {
                    "$setOnInsert": {
                        "elements": normalized,
                        "element_symbols": symbols,
                        "num_elements": len(symbols),
                        "structure_family": "unknown",
                        "default_visibility_affiliations": ["S4E"],
                        "created_at": now,
                    },
                    "$push": {"dft_calculations": {"$each": dft_dicts}},
                    "$set": {"updated_at": now},
                },
                upsert=True,
            ))
            total_imported += len(dft_dicts)

            if len(push_ops) >= BATCH_SIZE:
                _flush(push_ops)
                push_ops = []
                pct = (i + 1) / len(mat_items) * 100
                self.stdout.write(
                    f"  {i + 1:,}/{len(mat_items):,} materials written ({pct:.0f}%)..."
                )

        _flush(push_ops)

        # Mirror the imported materials into the JSON archive.
        #
        # This is the one write path that archives *after* Mongo rather than
        # before. Two reasons it is the right trade here: the bulk_write is a
        # two-phase pull/push that has no single in-memory document to archive,
        # and CHAOS itself is an external source of truth that can simply be
        # re-imported. An operator-run bulk load is also not a user request that
        # could silently lose data. Everything else in LOOP is archive-first.
        self._export_materials_to_archive()

        pass2_elapsed = time.monotonic() - t0_pass2
        total_elapsed = time.monotonic() - start_time

        self.stdout.write(
            self.style.SUCCESS(
                f"[import_chaos_data] Done in {total_elapsed:.0f}s. "
                f"rows_read={total_rows} imported={total_imported} "
                f"skipped={total_skipped} errors={total_errors}"
            )
        )
