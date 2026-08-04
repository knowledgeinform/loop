"""Tests for the idempotent Predictions-tab data bootstrap command."""
from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase


class PredictionBootstrapTests(SimpleTestCase):
    @patch("catalog.management.commands.ensure_prediction_data.call_command")
    @patch("catalog.management.commands.ensure_prediction_data.eligible_material_count")
    def test_skips_import_when_twenty_materials_exist(self, count, nested_call):
        count.return_value = 20
        call_command("ensure_prediction_data", minimum=20)
        nested_call.assert_not_called()

    @patch("catalog.management.commands.ensure_prediction_data.Path.is_file", return_value=True)
    @patch("catalog.management.commands.ensure_prediction_data.call_command")
    @patch("catalog.management.commands.ensure_prediction_data.eligible_material_count")
    def test_imports_and_backfills_when_data_is_sparse(self, count, nested_call, _is_file):
        count.side_effect = [1, 111]
        call_command("ensure_prediction_data", minimum=20, root="/tmp/ChemScreen")
        self.assertEqual(nested_call.call_count, 2)
        self.assertEqual(nested_call.call_args_list[0].args[0], "import_chemscreen")
        self.assertEqual(nested_call.call_args_list[1].args[0], "backfill_synthesis_predictions")
