"""
Management command: import_chaos_data

Reads all AFLOW-format data from the CHAOS SQLite database (256 sharded tables,
~211 k records, 230 columns) and upserts it into the MongoDB materials collection
as EmbeddedDFT documents.

Usage:
    python manage.py import_chaos_data                        # full upsert
    python manage.py import_chaos_data --incremental          # skip existing
    python manage.py import_chaos_data --dry-run --table auid_00 --limit 5

LOOP mirrors the CHAOS database: a run that reads the whole database also
removes the CHAOS records LOOP holds whose CHAOS auid is no longer in it
(an entry withdrawn or held back, or a record superseded by a recalculation
of the same run). A material that held nothing but such records
goes too, with its embeddings and unreviewed synthesis predictions; a
material with recipes, notes, files, reviewed predictions or any other user
data stays. Safety:
  * the removal is planned before anything is written, and a run that would
    remove more than --max-prune-fraction of LOOP's CHAOS records (default
    5%) stops with nothing written; a planned cut passes a larger fraction;
  * it runs only when every one of the 256 auid tables was read: a table that
    cannot be read (a file still being copied) stops it, and the run exits
    with an error;
  * the file is opened read-only, and one import runs at a time (a lock next
    to CHAOS_DB_PATH);
  * --dry-run reports the records and materials that would go; --no-prune
    skips the removal; --db reads another file (for a dry run against a new
    copy before it replaces the current one).

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

import collections
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

# A run removes at most this fraction of the CHAOS records LOOP holds, unless
# --max-prune-fraction says otherwise (see find_stale_chaos_records).
PRUNE_MAX_FRACTION = 0.05

# The CHAOS database is sharded by auid into this many tables (AFLOW's
# _N_AUID_TABLES_); a file with fewer is incomplete.
N_AUID_TABLES = 256

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


def find_stale_chaos_records(mat_coll, present_auids: set) -> Tuple[int, Dict[str, List[str]]]:
    """CHAOS records in LOOP whose CHAOS auid is not in ``present_auids``.

    Returns (number of CHAOS records LOOP holds, {material id: [comp_auid of
    each stale record]}). A CHAOS record is one this command wrote:
    uploaded_by "import", dft_source "S4E" and a CHAOS auid in extended_data.
    Records from uploads, the ChemScreen sync or any other source never match.
    """
    pipeline = [
        {"$match": {"dft_calculations.extended_data.auid": {"$exists": True}}},
        {"$unwind": "$dft_calculations"},
        {"$match": {
            "dft_calculations.uploaded_by": "import",
            "dft_calculations.dft_source": "S4E",
            "dft_calculations.extended_data.auid": {"$type": "string"},
        }},
        {"$project": {
            "_id": 1,
            "comp": "$dft_calculations.comp_auid",
            "auid": "$dft_calculations.extended_data.auid",
        }},
    ]
    total = 0
    stale: Dict[str, List[str]] = {}
    for row in mat_coll.aggregate(pipeline, allowDiskUse=True):
        total += 1
        if row.get("auid") not in present_auids:
            stale.setdefault(row["_id"], []).append(row["comp"])
    return total, stale


# Documents that hold user data about a material: a material any of these
# names is kept even when its CHAOS records are removed.
_USER_DATA_KINDS = (
    "Recipe", "SynthesisParseJob", "ModelFeedback", "XRDAnalysisJob", "XRDAnalysisReview",
)
_CURATION_FIELDS = ("display_name", "notes", "curator")


def materials_to_keep(mat_docs: List[dict], candidates: List[str]) -> Dict[str, str]:
    """{material id: why it stays} for materials that would hold no records.

    A material stays when someone curated it (notes, display name, curator,
    or visibility other than the import's ["S4E"]), when another document
    names it (recipes, parse jobs, feedback, XRD analyses, raw files), or when
    one of its synthesis predictions was reviewed.
    """
    from catalog import documents as docs
    from catalog.raw_db import RawFile

    keep: Dict[str, str] = {}
    for d in mat_docs:
        if any(str(d.get(f) or "").strip() for f in _CURATION_FIELDS):
            keep[d["_id"]] = "notes or name"
        elif (d.get("default_visibility_affiliations") or ["S4E"]) != ["S4E"]:
            keep[d["_id"]] = "visibility set by hand"
    rest = [m for m in candidates if m not in keep]
    for i in range(0, len(rest), BATCH_SIZE):
        chunk = rest[i:i + BATCH_SIZE]
        for kind in _USER_DATA_KINDS:
            for m in getattr(docs, kind).objects(material_auid__in=chunk).distinct("material_auid"):
                keep.setdefault(m, kind)
        for m in RawFile.objects(material_auid__in=chunk).distinct("material_auid"):
            keep.setdefault(m, "RawFile")
        for p in docs.SynthesisPrediction.objects(material_auid__in=chunk).only(
                "material_auid", "prediction_status", "validation"):
            if (p.prediction_status or "predicted") != "predicted" or p.validation:
                keep.setdefault(p.material_auid, "reviewed synthesis prediction")
    return keep


def _read_rows(conn: sqlite3.Connection, table: str) -> List[dict]:
    cur = conn.execute(f'SELECT * FROM "{table}"')  # noqa: S608
    return [dict(r) for r in cur.fetchall()]


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

    def _export_materials_to_archive(self) -> bool:
        """Re-emit every material into the JSON archive after a bulk import.

        Uses the shared export path so the files are byte-identical to what the
        signal hooks would have written. Returns False when the export failed;
        the caller decides whether that is fatal (it is after a removal, since
        the archive would still list removed records and a rebuild would bring
        them back). `manage.py loop_archive export` can be re-run by hand.
        """
        try:
            from catalog.archive import rebuild as archive_rebuild
            from catalog.archive import writer as archive_writer

            if archive_writer.archive_root() is None:
                return True
            self.stdout.write("  Mirroring materials into the JSON archive...")
            counts = archive_rebuild.export(["material"])["material"]
            self.stdout.write(
                f"  Archive: {counts.written} written, {counts.unchanged} unchanged, "
                f"{counts.failed} failed."
            )
            return counts.failed == 0
        except Exception as exc:
            self.stdout.write(
                self.style.WARNING(
                    f"  Archive mirror failed ({exc}). Run "
                    "`manage.py loop_archive export --kind material` to repair."
                )
            )
            return False

    def _lock(self, db_path: str):
        """One import at a time: the hourly watcher and a manual run share a
        lock file next to CHAOS_DB_PATH. Returns the open file (keep it)."""
        import errno
        import fcntl

        base = getattr(settings, "CHAOS_DB_PATH", "") or db_path
        lock_path = os.path.join(os.path.dirname(os.path.abspath(base)), ".import_chaos_data.lock")
        try:
            fh = open(lock_path, "a")
        except OSError as exc:
            self.stdout.write(self.style.WARNING(f"  No import lock ({exc}); continuing without it."))
            return None
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            if exc.errno in (errno.EAGAIN, errno.EACCES):
                raise CommandError(f"Another import_chaos_data run holds {lock_path}; nothing done.")
            raise
        return fh

    def _plan_prune(self, mat_coll, present_auids: set, max_fraction: float):
        """The removal of Pass 3, decided before anything is written. Returns
        {material id: [comp_auid, ...]} (possibly empty); raises when it would
        remove more than max_fraction of LOOP's CHAOS records."""
        total, stale = find_stale_chaos_records(mat_coll, present_auids)
        n_stale = sum(len(v) for v in stale.values())
        self.stdout.write(
            f"  Pass 3 plan: {n_stale:,} of LOOP's {total:,} CHAOS records are no longer "
            f"in the database ({len(stale):,} materials).")
        if total and n_stale > max_fraction * total:
            raise CommandError(
                f"Pass 3 would remove {n_stale:,} of {total:,} CHAOS records "
                f"({n_stale / total:.1%}), more than --max-prune-fraction={max_fraction}. "
                "Nothing was written. If the database really shrank that much, rerun "
                "with a larger --max-prune-fraction; otherwise check the database file.")
        return stale

    def _outcome(self, mat_coll, stale: Dict[str, List[str]], after_pull: bool):
        """(materials that would be deleted, {kept material: why}) for the
        materials of the stale records: those left with no records at all."""
        empty: List[dict] = []
        touched = list(stale)
        fields = {"_id": 1, "default_visibility_affiliations": 1, **{f: 1 for f in _CURATION_FIELDS}}
        for i in range(0, len(touched), BATCH_SIZE):
            chunk = touched[i:i + BATCH_SIZE]
            if after_pull:
                empty += list(mat_coll.find({"_id": {"$in": chunk}, "dft_calculations": {"$size": 0}}, fields))
            else:
                for d in mat_coll.aggregate([
                    {"$match": {"_id": {"$in": chunk}}},
                    {"$project": {**fields, "n": {"$size": {"$ifNull": ["$dft_calculations", []]}}}},
                ]):
                    if d["n"] == len(stale[d["_id"]]):
                        empty.append(d)
        keep = materials_to_keep(empty, [d["_id"] for d in empty])
        doomed = [d["_id"] for d in empty if d["_id"] not in keep]
        return doomed, keep

    def _apply_prune(self, mat_coll, stale: Dict[str, List[str]]) -> Tuple[int, int]:
        """Pass 3: pull the stale records and delete the materials that held
        nothing else. Returns (records, materials)."""
        from catalog import documents as docs

        now = datetime.now(tz=timezone.utc)
        ops: List[UpdateOne] = []
        pulled: List[str] = []
        for mat_auid, comps in stale.items():
            pulled += comps
            ops.append(UpdateOne(
                {"_id": mat_auid},
                {"$pull": {"dft_calculations": {
                    "comp_auid": {"$in": comps}, "uploaded_by": "import", "dft_source": "S4E"}},
                 "$set": {"updated_at": now}},
            ))
            if len(ops) >= BATCH_SIZE:
                mat_coll.bulk_write(ops, ordered=False)
                ops = []
        if ops:
            mat_coll.bulk_write(ops, ordered=False)
        # The removed records' own embeddings ("comp" scope), as when a record
        # is deleted by hand.
        for i in range(0, len(pulled), BATCH_SIZE):
            docs.MLEmbedding.objects(scope="comp", comp_auid__in=pulled[i:i + BATCH_SIZE]).delete()

        doomed, keep = self._outcome(mat_coll, stale, after_pull=True)
        deleted = 0
        for i in range(0, len(doomed), BATCH_SIZE):
            chunk = doomed[i:i + BATCH_SIZE]
            # The conditions are repeated in the delete, so a material someone
            # edited a moment ago is not taken. Through the documents (not the
            # collection), so the archive hooks write a tombstone for each.
            gone = [m.id for m in docs.Material.objects(
                id__in=chunk, dft_calculations__size=0,
                display_name__in=[None, ""], notes__in=[None, ""], curator__in=[None, ""]).only("id")]
            if gone:
                docs.MLEmbedding.objects(material_auid__in=gone).delete()
                docs.SynthesisPrediction.objects(
                    material_auid__in=gone, prediction_status__in=[None, "predicted"]).delete()
                deleted += docs.Material.objects(id__in=gone, dft_calculations__size=0).delete()
        n = sum(len(v) for v in stale.values())
        self.stdout.write(
            f"  Pass 3 done: {n:,} records removed; {deleted:,} materials that held only those "
            f"records deleted; {len(keep):,} left without records but kept "
            f"({dict(collections.Counter(keep.values()))}).")
        return (n, deleted)

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
            help="Parse and report what would be imported and removed; make no writes. "
                 "One line per record with -v 2.",
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
        parser.add_argument(
            "--db",
            default=None,
            help="Read this CHAOS file instead of CHAOS_DB_PATH (e.g. a new copy, with --dry-run).",
        )
        parser.add_argument(
            "--no-prune",
            action="store_true",
            default=False,
            help="Keep CHAOS records whose auid is no longer in the database.",
        )
        parser.add_argument(
            "--max-prune-fraction",
            type=float,
            default=PRUNE_MAX_FRACTION,
            help="Refuse to remove more than this fraction of LOOP's CHAOS records "
                 f"in one run (default {PRUNE_MAX_FRACTION}).",
        )

    def handle(self, *args, **options):
        from catalog import auid as auid_mod
        from catalog.documents import (
            Material,
            compute_material_auid,
            normalize_elements_payload,
        )
        from mongoengine.connection import get_db

        db_path = options["db"] or getattr(settings, "CHAOS_DB_PATH", "")
        if not db_path:
            raise CommandError("CHAOS_DB_PATH is not configured in settings.")
        if not os.path.exists(db_path):
            raise CommandError(f"CHAOS database not found at: {db_path}")

        incremental = options["incremental"]
        dry_run = options["dry_run"]
        only_table = options["table"]
        limit = options["limit"]
        prune = not options["no_prune"]
        max_prune_fraction = options["max_prune_fraction"]
        verbose = options.get("verbosity", 1) >= 2

        self.stdout.write(
            f"[import_chaos_data] db={db_path} incremental={incremental} dry_run={dry_run}"
        )
        start_time = time.monotonic()
        lock = None if dry_run else self._lock(db_path)  # noqa: F841 (held until the process ends)

        conn = sqlite3.connect(f"file:{os.path.realpath(db_path)}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row

        try:
            tables = _get_auid_tables(conn)
        except Exception as exc:
            conn.close()
            raise CommandError(f"Failed to list tables: {exc}") from exc
        n_tables = len(tables)

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
        # Every CHAOS auid in the database, imported or not, for the removal of
        # records that have left it (Pass 3).
        present_auids: set = set()
        read_everything = only_table is None and limit is None
        failed_tables: List[str] = []
        total_rows = 0
        total_skipped = 0
        total_errors = 0

        for table in tables:
            if limit is not None and total_rows >= limit:
                break

            try:
                rows = _read_rows(conn, table)
            except Exception as exc:
                self.stderr.write(f"  [WARN] Could not read {table}: {exc}")
                failed_tables.append(table)
                continue

            for row in rows:
                if limit is not None and total_rows >= limit:
                    break

                chaos_auid = (row.get("auid") or "").strip()
                if not chaos_auid:
                    total_errors += 1
                    continue
                present_auids.add(chaos_auid)

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
                    if verbose:
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

        # Plan Pass 3 before anything is written: a refused plan writes nothing.
        stale: Dict[str, List[str]] = {}
        blocked = ""
        if prune and not read_everything:
            self.stdout.write("  Pass 3 skipped: only part of the database was read (--table or --limit).")
        elif prune and (failed_tables or n_tables != N_AUID_TABLES):
            blocked = (f"{len(failed_tables)} of {n_tables} tables could not be read" if failed_tables
                       else f"the file has {n_tables} auid tables, not {N_AUID_TABLES}")
            self.stdout.write(self.style.WARNING(f"  Pass 3 skipped: {blocked}; nothing removed."))
        elif prune and not present_auids:
            self.stdout.write(self.style.WARNING("  Pass 3 skipped: the CHAOS database gave no auids."))
        elif prune:
            stale = self._plan_prune(mat_coll, present_auids, max_prune_fraction)

        if dry_run:
            if stale:
                doomed, keep = self._outcome(mat_coll, stale, after_pull=False)
                self.stdout.write(
                    f"  Pass 3 would delete {len(doomed):,} materials that would hold no records, "
                    f"and keep {len(keep):,} ({dict(collections.Counter(keep.values()))}).")
            self.stdout.write(self.style.SUCCESS("[DRY RUN] No writes performed."))
            if blocked:
                raise CommandError(f"Pass 3 could not run: {blocked}.")
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
        removed = (0, 0)

        def _flush(ops: List[UpdateOne]) -> None:
            if ops:
                mat_coll.bulk_write(ops, ordered=False)

        try:
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

            # Pass 3: remove the CHAOS records that have left the database.
            if stale:
                removed = self._apply_prune(mat_coll, stale)
        finally:
            # Mirror the imported materials into the JSON archive, also when a
            # step above failed, so the archive follows whatever reached Mongo.
            #
            # This is the one write path that archives *after* Mongo rather than
            # before. Two reasons it is the right trade here: the bulk_write is a
            # two-phase pull/push that has no single in-memory document to archive,
            # and CHAOS itself is an external source of truth that can simply be
            # re-imported. An operator-run bulk load is also not a user request that
            # could silently lose data. Everything else in LOOP is archive-first.
            archived = self._export_materials_to_archive()

        if removed[0] and not archived:
            raise CommandError(
                "Records were removed but the archive export failed: the archive still lists "
                "them. Run `manage.py loop_archive export --kind material`.")

        pass2_elapsed = time.monotonic() - t0_pass2
        total_elapsed = time.monotonic() - start_time

        self.stdout.write(
            self.style.SUCCESS(
                f"[import_chaos_data] Done in {total_elapsed:.0f}s. "
                f"rows_read={total_rows} imported={total_imported} "
                f"skipped={total_skipped} errors={total_errors} "
                f"removed_records={removed[0]} removed_materials={removed[1]}"
            )
        )
        if blocked:
            raise CommandError(f"Pass 3 could not run: {blocked}. Records read were imported.")
