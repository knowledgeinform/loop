"""import_chaos_data mirrors the CHAOS database: records that leave it leave LOOP.

CHAOS publishes views of its database; LOOP copies one and imports it. When
an entry leaves that view (withdrawn, held back, or superseded by a
recalculation of the same run), LOOP must drop its copy too. These tests build a small CHAOS file in the published
layout (256 auid tables), import it, shrink it, and import again.

They use made-up compositions of technetium, promethium and rhenium oxides so
that nothing here can meet another test's materials in CI's shared MongoDB,
and they clean up after themselves.
"""

from __future__ import annotations

import io
import os
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from catalog.archive import registry
from catalog.documents import (
    EmbeddedDFT,
    MLEmbedding,
    Material,
    SynthesisPrediction,
    compute_material_auid,
    normalize_elements_payload,
)
from catalog.management.commands import import_chaos_data as command
from .mongo_guard import mongo_reachable

COLUMNS = ("auid", "aurl", "prototype", "compound", "species", "stoichiometry",
           "enthalpy_formation_atom", "spacegroup_relax", "pocc_parameters")


def _row(auid, aurl, prototype, species, stoich, hf="-1.0"):
    compound = "".join(f"{s}{n}" for s, n in zip(species, stoich))
    return {
        "auid": auid, "aurl": aurl, "prototype": prototype, "compound": compound,
        "species": "[" + ",".join(f'"{s}"' for s in species) + "]",
        "stoichiometry": "[" + ",".join(str(n) for n in stoich) + "]",
        "enthalpy_formation_atom": hf, "spacegroup_relax": "225",
    }


def _material_of(row):
    return compute_material_auid(normalize_elements_payload(command._parse_elements(row)), "unknown")


@unittest.skipUnless(mongo_reachable(), "MongoDB not reachable")
class ChaosImportMirrorTests(TestCase):
    databases = {"default"}

    def setUp(self):
        from catalog import signals

        signals.connect_document_signals()
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "chaos.db")
        self.archive = Path(self._tmp.name) / "archive"
        self._settings = override_settings(
            CHAOS_DB_PATH=self.db, ARCHIVE_ROOT=str(self.archive), ARCHIVE_ENABLED=True,
            ARCHIVE_REQUIRED=True, ARCHIVE_FSYNC=False, EMBEDDINGS_ON_WRITE=False)
        self._settings.enable()

        s = uuid.uuid4().hex[:10]
        base = f"s4e.ai:AFLOWDATA/LIB3_RAW/PmTcO_{s}/POCC_P0"
        # One disordered system, two of its supercells, one ordered compound.
        self.system = _row(f"s4e:t{s}01", base, "POCC_P0", ["Pm", "Tc", "O"], [1, 1, 2])
        self.system["pocc_parameters"] = '"S0-1xA_S1-0.5xB-0.5xC"'
        self.sc1 = _row(f"s4e:t{s}02", base + "/ARUN.POCC_1_H0", "ARUN.POCC_1_H0", ["Pm", "Tc", "O"], [1, 3, 4])
        self.sc2 = _row(f"s4e:t{s}03", base + "/ARUN.POCC_2_H0", "ARUN.POCC_2_H0", ["Pm", "Tc", "O"], [3, 1, 4])
        self.ordered = _row(f"s4e:t{s}04", f"s4e.ai:AFLOWDATA/LIB2_RAW/ReO_{s}/AB", "AB_cF8_225_a_b",
                            ["Re", "O"], [1, 1])
        self.extra = _row(f"s4e:t{s}05", f"s4e.ai:AFLOWDATA/LIB2_RAW/ReO_{s}/A2B", "A2B_cF12",
                          ["Re", "O"], [1, 2])
        self.rows = [self.system, self.sc1, self.sc2, self.ordered]
        self.materials = {_material_of(r) for r in self.rows + [self.extra]}
        self._cleanup()

    def tearDown(self):
        self._cleanup()
        self._settings.disable()
        self._tmp.cleanup()

    def _cleanup(self):
        ids = list(self.materials)
        MLEmbedding.objects(material_auid__in=ids).delete()
        SynthesisPrediction.objects(material_auid__in=ids).delete()
        Material.objects(id__in=ids).delete()

    def _write_db(self, rows):
        if os.path.exists(self.db):
            os.remove(self.db)
        con = sqlite3.connect(self.db)
        for t in range(command.N_AUID_TABLES):
            con.execute(f'CREATE TABLE "auid_{t:02x}" ({", ".join(c + " TEXT" for c in COLUMNS)})')
        for i, r in enumerate(rows):
            con.execute(f'INSERT INTO "auid_{i:02x}" VALUES ({",".join("?" * len(COLUMNS))})',
                        [r.get(c) for c in COLUMNS])
        con.commit()
        con.close()

    def _import(self, *args):
        out = io.StringIO()
        call_command("import_chaos_data", *args, stdout=out, stderr=io.StringIO())
        return out.getvalue()

    def _chaos_auids(self):
        found = set()
        for m in Material.objects(id__in=list(self.materials)):
            for d in m.dft_calculations or []:
                if d.uploaded_by == "import":
                    found.add((d.extended_data or {}).get("auid"))
        return found

    def _comp_of(self, row):
        m = Material.objects(id=_material_of(row)).first()
        return next(d.comp_auid for d in m.dft_calculations if (d.extended_data or {}).get("auid") == row["auid"])

    def _archived(self, material_auid):
        return (self.archive / "records" / registry.MATERIAL.path_of({"_id": material_auid})).exists()

    def test_records_that_leave_the_database_leave_loop(self):
        self._write_db(self.rows)
        self._import()
        self.assertEqual(self._chaos_auids(), {r["auid"] for r in self.rows})

        # sc1's material also holds a record someone uploaded: it stays, with
        # that record. sc2's material carries a curator's note: it stays.
        m1 = Material.objects(id=_material_of(self.sc1)).first()
        m1.dft_calculations.append(EmbeddedDFT(comp_auid=f"{m1.id}:C:upload", dft_source="DFT",
                                               uploaded_by="someone"))
        m1.save()
        m2 = Material.objects(id=_material_of(self.sc2)).first()
        m2.notes = "keep"
        m2.save()
        # The system's own material holds only CHAOS records and goes when
        # they go, with its embedding; sc1's record has an embedding of its own.
        sys_mat = _material_of(self.system)
        MLEmbedding(scope="material", material_auid=sys_mat, composition_embedding=[0.0]).save()
        sc1_comp = self._comp_of(self.sc1)
        MLEmbedding(scope="comp", material_auid=m1.id, comp_auid=sc1_comp, composition_embedding=[0.0]).save()

        # The supercells and the system leave the database (as a held system
        # would); the ordered compound stays. Incremental, as the watcher runs.
        self._write_db([self.ordered])
        with self.assertRaises(CommandError):
            self._import("--incremental")  # 3 of 4 is more than the default 5%: refused
        self.assertEqual(self._chaos_auids(), {r["auid"] for r in self.rows})

        out = self._import("--incremental", "--max-prune-fraction", "0.9")
        self.assertIn("3 of LOOP's 4 CHAOS records are no longer in the database", out)
        self.assertEqual(self._chaos_auids(), {self.ordered["auid"]})

        kept1 = Material.objects(id=_material_of(self.sc1)).first()
        self.assertIsNotNone(kept1)
        self.assertEqual([d.uploaded_by for d in kept1.dft_calculations], ["someone"])
        self.assertEqual(MLEmbedding.objects(scope="comp", comp_auid=sc1_comp).count(), 0)
        self.assertIsNotNone(Material.objects(id=_material_of(self.sc2)).first())
        self.assertIsNone(Material.objects(id=sys_mat).first())
        self.assertEqual(MLEmbedding.objects(material_auid=sys_mat).count(), 0)
        self.assertFalse(self._archived(sys_mat))
        self.assertTrue(self._archived(_material_of(self.ordered)))

    def test_a_reviewed_prediction_keeps_a_material(self):
        self._write_db(self.rows)
        self._import()
        sys_mat = _material_of(self.system)
        SynthesisPrediction(id=sys_mat, material_auid=sys_mat, elements={"Pm": 1, "Tc": 1, "O": 2},
                            structure_family="unknown", prediction_status="verified").save()
        self._write_db([self.sc1, self.sc2, self.ordered])
        out = self._import("--max-prune-fraction", "0.3")
        self.assertIn("reviewed synthesis prediction", out)
        self.assertIsNotNone(Material.objects(id=sys_mat).first())
        self.assertEqual(SynthesisPrediction.objects(material_auid=sys_mat).count(), 1)

    def test_a_refused_plan_writes_nothing(self):
        self._write_db(self.rows)
        self._import()
        # a new entry arrives while three leave: refused before Pass 2
        self._write_db([self.ordered, self.extra])
        with self.assertRaises(CommandError):
            self._import("--incremental")
        self.assertNotIn(self.extra["auid"], self._chaos_auids())
        self.assertEqual(len(self._chaos_auids()), 4)

    def test_an_unreadable_table_removes_nothing(self):
        self._write_db(self.rows)
        self._import()
        self._write_db([self.ordered])
        real = command._read_rows

        def flaky(conn, table):
            if table == "auid_01":
                raise sqlite3.DatabaseError("database disk image is malformed")
            return real(conn, table)

        with mock.patch.object(command, "_read_rows", side_effect=flaky):
            with self.assertRaises(CommandError) as ctx:
                self._import("--incremental", "--max-prune-fraction", "1.0")
        self.assertIn("could not be read", str(ctx.exception))
        self.assertEqual(len(self._chaos_auids()), 4)

    def test_dry_run_table_and_no_prune_remove_nothing(self):
        self._write_db(self.rows)
        self._import()
        self._write_db([self.system, self.ordered])

        out = self._import("--dry-run", "--max-prune-fraction", "0.6")
        self.assertIn("2 of LOOP's 4 CHAOS records are no longer in the database", out)
        self.assertIn("Pass 3 would delete", out)
        self.assertEqual(len(self._chaos_auids()), 4)

        self._import("--table", "auid_00", "--max-prune-fraction", "0.6")
        self.assertEqual(len(self._chaos_auids()), 4)

        self._import("--no-prune")
        self.assertEqual(len(self._chaos_auids()), 4)

        self._import("--max-prune-fraction", "0.6")
        self.assertEqual(self._chaos_auids(), {self.system["auid"], self.ordered["auid"]})

    def test_empty_database_removes_nothing(self):
        self._write_db(self.rows)
        self._import()
        self._write_db([])
        out = self._import("--max-prune-fraction", "1.0")
        self.assertIn("Pass 3 skipped: the CHAOS database gave no auids", out)
        self.assertEqual(len(self._chaos_auids()), 4)
