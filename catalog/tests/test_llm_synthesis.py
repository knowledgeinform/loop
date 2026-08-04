"""Unit tests for the OpenRouter synthesis-route parser (no network, no Mongo).

The parse cache is best-effort and swallows errors, so these run under
SimpleTestCase whether or not MongoDB is reachable. Each test uses distinct
route text so a warm cache (in a dev environment with Mongo up) can't leak
canned results between assertions.
"""
import io
import json
import urllib.error
from unittest import mock

from django.test import SimpleTestCase, override_settings

from catalog import llm_synthesis
from catalog.llm_synthesis import SynthesisParseUnavailable, parse_synthesis_route

_ENABLED = dict(
    SYNTHESIS_LLM_ENABLED=True,
    OPENROUTER_API_KEY="test-key",
    OPENROUTER_MODEL="test/model",
)


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


def _openrouter_response(content_obj) -> _FakeResp:
    envelope = {"choices": [{"message": {"content": json.dumps(content_obj)}}]}
    return _FakeResp(json.dumps(envelope).encode())


class LLMSynthesisTests(SimpleTestCase):
    def test_empty_text_returns_empty_without_calling_api(self):
        with mock.patch.object(llm_synthesis.urllib.request, "urlopen") as urlopen:
            self.assertEqual(parse_synthesis_route("   "), [])
            urlopen.assert_not_called()

    def test_disabled_raises(self):
        # Default settings ship the feature off.
        with self.assertRaises(SynthesisParseUnavailable):
            parse_synthesis_route("Ball milled then fired at 900C")

    @override_settings(**_ENABLED)
    def test_valid_response_is_parsed_and_filtered(self):
        content = {
            "steps": [
                {"step_type": "Ball_Milling", "milling_time_hours": 12, "milling_rpm": 300},
                {"step_type": "bogus_type", "foo": "bar"},
                {"not": "a step"},
                {"step_type": "heat_treatment", "max_temp_c": 900},
            ]
        }
        with mock.patch.object(
            llm_synthesis.urllib.request, "urlopen",
            return_value=_openrouter_response(content),
        ):
            steps = parse_synthesis_route("Ball milled 12h at 300rpm, fired 900C #a")

        self.assertEqual([s["step_type"] for s in steps], ["ball_milling", "heat_treatment"])
        self.assertEqual(steps[0]["milling_time_hours"], 12)
        self.assertEqual(steps[1]["max_temp_c"], 900)

    @override_settings(**_ENABLED)
    def test_http_error_raises_unavailable(self):
        err = urllib.error.HTTPError(
            "http://x", 500, "err", hdrs=None, fp=io.BytesIO(b'{"error":"boom"}')
        )
        with mock.patch.object(llm_synthesis.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(SynthesisParseUnavailable):
                parse_synthesis_route("Fired at 900C for 6h #b")

    @override_settings(**_ENABLED)
    def test_non_json_content_raises_unavailable(self):
        resp = _FakeResp(
            json.dumps({"choices": [{"message": {"content": "not json at all"}}]}).encode()
        )
        with mock.patch.object(llm_synthesis.urllib.request, "urlopen", return_value=resp):
            with self.assertRaises(SynthesisParseUnavailable):
                parse_synthesis_route("Annealed in air #c")

    @override_settings(**_ENABLED)
    def test_missing_steps_list_raises_unavailable(self):
        with mock.patch.object(
            llm_synthesis.urllib.request, "urlopen",
            return_value=_openrouter_response({"result": "no steps key"}),
        ):
            with self.assertRaises(SynthesisParseUnavailable):
                parse_synthesis_route("Quenched in water #d")
