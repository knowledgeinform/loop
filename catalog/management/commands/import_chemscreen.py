"""Import ChemScreen observed data and precomputed model predictions into LOOP."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from mongoengine.connection import get_db
from pymongo import UpdateOne

from catalog import auid as auid_mod
from catalog.chemscreen import (
    ChemScreenImportError,
    ChemScreenRecord,
    iter_candidate_records,
    iter_observed_records,
    iter_prediction_records,
)
from catalog.documents import Material

_3D_TRANSITION_METALS = frozenset("Sc Ti V Cr Mn Fe Co Ni Cu Zn".split())


class Command(BaseCommand):
    help = (
        "Sync the ChemScreen generated candidate pool, EFA/DEED/d2h JSON, "
        "and optional all_predictions CSV/JSON into LOOP."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--root",
            default=getattr(settings, "CHEMSCREEN_ROOT", ""),
            help="ChemScreen checkout root (defaults to CHEMSCREEN_ROOT).",
        )
        parser.add_argument(
            "--predictions",
            default=getattr(settings, "CHEMSCREEN_PREDICTIONS_PATH", ""),
            help="ChemScreen all_predictions CSV, JSON, or JSONL artifact.",
        )
        parser.add_argument(
            "--metric",
            default=getattr(settings, "CHEMSCREEN_PREDICTION_METRIC", "ML_Predicted"),
            help="Prediction value column/key and stored metric name.",
        )
        parser.add_argument(
            "--model-name",
            default=getattr(settings, "CHEMSCREEN_MODEL_NAME", "ChemScreen"),
            help="Model provenance label stored with predictions.",
        )
        parser.add_argument(
            "--skip-candidates",
            action="store_true",
            help="Do not import the five-cation 3d oxide candidate pool.",
        )
        parser.add_argument("--skip-observed", action="store_true")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        root = str(options["root"] or "").strip()
        predictions = str(options["predictions"] or "").strip()
        if not options["skip_observed"] and not root:
            raise CommandError("Provide --root or configure CHEMSCREEN_ROOT.")
        if (
            options["skip_observed"]
            and not predictions
            and (not root or options["skip_candidates"])
        ):
            raise CommandError(
                "--skip-observed requires candidates from --root or --predictions."
            )

        records: List[ChemScreenRecord] = []
        try:
            if not options["skip_observed"]:
                records.extend(iter_observed_records(root))
            if root and not options["skip_candidates"]:
                records.extend(
                    iter_candidate_records(
                        root,
                        allowed_elements=_3D_TRANSITION_METALS,
                        cation_count=5,
                    )
                )
            if predictions:
                records.extend(
                    iter_prediction_records(
                        predictions,
                        metric_name=options["metric"],
                        model_name=options["model_name"],
                    )
                )
        except ChemScreenImportError as exc:
            raise CommandError(str(exc)) from exc

        grouped: Dict[str, Dict[str, Any]] = {}
        labeled_material_auids: set[str] = set()
        for record in records:
            material_auid = auid_mod.material_auid(record.elements, "rocksalt")
            if record.kind == "observed" and record.values:
                labeled_material_auids.add(material_auid)
            bucket = grouped.setdefault(
                material_auid,
                {"elements": record.elements, "calculations": {}},
            )
            dft = self._calculation_dict(material_auid, record, options["metric"])
            bucket["calculations"][dft["comp_auid"]] = dft

        observed_count = sum(record.kind == "observed" for record in records)
        prediction_count = sum(record.kind == "prediction" for record in records)
        candidate_count = sum(record.kind == "candidate" for record in records)
        if options["dry_run"]:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Validated {observed_count} observed, {prediction_count} predicted, "
                    f"and {candidate_count} candidate ChemScreen records across "
                    f"{len(grouped)} materials."
                )
            )
            return

        operations: List[UpdateOne] = []
        now = datetime.now(timezone.utc)
        for material_auid, bucket in grouped.items():
            calculations = list(bucket["calculations"].values())
            comp_auids = [calculation["comp_auid"] for calculation in calculations]
            elements = dict(bucket["elements"])
            operations.extend(
                (
                    UpdateOne(
                        {"_id": material_auid},
                        {
                            "$set": {
                                "elements": elements,
                                "element_symbols": sorted(elements),
                                "num_elements": len(elements),
                                "structure_family": "rocksalt",
                                "updated_at": now,
                            },
                            "$setOnInsert": {
                                "created_at": now,
                                "default_visibility_affiliations": ["S4E"],
                                "dft_calculations": [],
                            },
                        },
                        upsert=True,
                    ),
                    UpdateOne(
                        {"_id": material_auid},
                        {"$pull": {"dft_calculations": {"comp_auid": {"$in": comp_auids}}}},
                    ),
                    UpdateOne(
                        {"_id": material_auid},
                        {"$push": {"dft_calculations": {"$each": calculations}}},
                    ),
                )
            )

        if operations:
            get_db()[Material._meta["collection"]].bulk_write(operations, ordered=True)
            from catalog.model_training import enqueue_model_retraining

            enqueue_model_retraining(
                material_auids=sorted(labeled_material_auids),
                reason="ChemScreen observed-data import",
            )

        # The prediction table reads materials live; embeddings are optional
        # for this path and can be rebuilt in one efficient pass afterward.
        self.stdout.write(
            self.style.SUCCESS(
                f"Imported {observed_count} observed, {prediction_count} predicted, "
                f"and {candidate_count} candidate ChemScreen records into "
                f"{len(grouped)} LOOP materials."
            )
        )

    @staticmethod
    def _calculation_dict(
        material_auid: str,
        record: ChemScreenRecord,
        metric_name: str,
    ) -> Dict[str, Any]:
        identity = {
            "source": "ChemScreen",
            "chem_id": record.chem_id,
            "kind": record.kind,
            "model": record.model_name,
            "metric": metric_name if record.kind == "prediction" else "",
        }
        comp_auid = auid_mod.comp_auid(material_auid, identity)
        extended_data: Dict[str, Any] = {
            "chem_id": record.chem_id,
            "DFT": record.dft,
            "EXP": record.exp,
        }
        if record.single_phase:
            extended_data["single_phase"] = record.single_phase
        if record.experimental_source:
            extended_data["experimental_source"] = record.experimental_source
        if record.calculation_method:
            extended_data["calculation_method"] = record.calculation_method
        if record.calculation_inputs:
            extended_data["calculation_inputs"] = record.calculation_inputs
        ml_predictions: Dict[str, Any] = {}
        if record.kind == "observed":
            extended_data.update(record.values)
            dft_source = "ChemScreen DFT"
        elif record.kind == "prediction":
            ml_predictions = {
                "model": record.model_name or "ChemScreen",
                **record.values,
            }
            dft_source = "ChemScreen model"
        else:
            dft_source = "ChemScreen candidate pool"
        now = datetime.now(timezone.utc)
        return {
            "comp_auid": comp_auid,
            "dft_source": dft_source,
            "dft_metadata": identity,
            "ml_predictions": ml_predictions,
            "extended_data": extended_data,
            "spacegroup": "225",
            "element_sites": {},
            "uploaded_by": "ChemScreen sync",
            "visibility_affiliations": ["S4E"],
            "created_at": now,
            "updated_at": now,
        }
