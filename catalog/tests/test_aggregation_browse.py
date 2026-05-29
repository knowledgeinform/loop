"""Mongo aggregation parity tests for browse (requires live MongoDB)."""

from __future__ import annotations

import os
import unittest
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from django.test import SimpleTestCase, tag

from catalog import aggregation as aggregation_mod
from catalog.documents import ExpCondition, Material, Recipe, EmbeddedTrial


def _mongo_reachable() -> bool:
    if os.environ.get("SKIP_MONGO_TESTS", "").lower() in ("1", "true", "yes"):
        return False
    try:
        from mongoengine.connection import get_db

        get_db().command("ping")
        return True
    except Exception:
        return False


def _embedded_item_visible_mongo_rules(
    item_tags: Optional[List[str]], user_tags: List[str]
) -> bool:
    """Match ``_mongo_embedded_visible_cond`` (trial/literature subdocs in browse)."""
    it = list(item_tags or [])
    if "S4E" in it:
        return True
    ut = list(user_tags or [])
    return bool(set(it) & set(ut))


def _reference_material_rollups(
    material_auid: str, user_tags: List[str]
) -> Dict[str, Any]:
    is_public = "S4E" in user_tags or not user_tags
    trial_count = 0
    lit_count = 0
    dates: List[datetime] = []
    for recipe in Recipe.objects(material_auid=material_auid):
        for t in recipe.trials or []:
            tags = getattr(t, "visibility_affiliations", None) or []
            if is_public or _embedded_item_visible_mongo_rules(list(tags), user_tags):
                trial_count += 1
                td = getattr(t, "trial_date", None)
                if td is not None:
                    dates.append(td)
        for lit in recipe.literature or []:
            tags = getattr(lit, "visibility_affiliations", None) or []
            if is_public or _embedded_item_visible_mongo_rules(list(tags), user_tags):
                lit_count += 1
    latest = max(dates) if dates else None
    return {
        "trial_count": trial_count,
        "literature_count": lit_count,
        "latest_trial_date": latest,
    }


@tag("mongo")
@unittest.skipUnless(_mongo_reachable(), "MongoDB not reachable (set SKIP_MONGO_TESTS=1 to skip)")
class BrowseMaterialsAggregationParityTests(SimpleTestCase):
    databases = {}  # only Mongo, not Django DB

    def setUp(self):
        suffix = uuid.uuid4().hex[:10]
        self.material_auid = f"M:testAggBrowse{suffix}"
        self.recipe_auid = f"{self.material_auid}:R:test{suffix}"
        Material(
            id=self.material_auid,
            elements={"La": 1, "Co": 1, "O": 3},
            structure_family="perovskite",
        ).save()
        d1 = datetime(2024, 1, 10, tzinfo=timezone.utc)
        d2 = datetime(2024, 6, 1, tzinfo=timezone.utc)
        Recipe(
            id=self.recipe_auid,
            material_auid=self.material_auid,
            elements={"La": 1, "Co": 1, "O": 3},
            structure_family="perovskite",
            synthesis_steps=[{"step_type": "grind"}],
            trials=[
                EmbeddedTrial(
                    trial_id="t1",
                    trial_date=d1,
                    exp_condition=ExpCondition(),
                    visibility_affiliations=["Oak Ridge"],
                ),
                EmbeddedTrial(
                    trial_id="t2",
                    trial_date=d2,
                    exp_condition=ExpCondition(),
                    visibility_affiliations=["APL"],
                ),
            ],
            literature=[],
            visibility_affiliations=["S4E"],
        ).save()

    def tearDown(self):
        Recipe.objects(id=self.recipe_auid).delete()
        Material.objects(id=self.material_auid).delete()

    def test_public_user_counts_match_reference(self):
        ref = _reference_material_rollups(self.material_auid, [])
        bm = aggregation_mod.browse_materials(material_auid_in=[self.material_auid])
        self.assertEqual(len(bm.rows), 1)
        row = bm.rows[0]
        self.assertEqual(row["trial_count"], ref["trial_count"])
        self.assertEqual(row["literature_count"], ref["literature_count"])
        self.assertEqual(row["latest_trial_date"], ref["latest_trial_date"])

    def test_org_only_user_sees_tagged_trials(self):
        ref = _reference_material_rollups(self.material_auid, ["APL"])
        bm = aggregation_mod.browse_materials(
            material_auid_in=[self.material_auid],
            user_affiliations=["APL"],
        )
        row = bm.rows[0]
        self.assertEqual(row["trial_count"], ref["trial_count"])
        self.assertEqual(ref["trial_count"], 1)
        self.assertEqual(row["latest_trial_date"], ref["latest_trial_date"])

    def test_facet_pagination_total(self):
        bm = aggregation_mod.browse_materials(
            material_auid_query=self.material_auid,
            skip=0,
            limit=10,
        )
        self.assertGreaterEqual(bm.total_count, 1)
        self.assertGreaterEqual(len(bm.rows), 1)
