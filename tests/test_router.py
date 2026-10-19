"""Unit tests for the request rewriting that does not need a model or a GPU."""

import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def load_router():
    """Import router.py from a temporary directory with the example config beside it."""
    temp = tempfile.mkdtemp(prefix="router-test-")
    shutil.copy(ROOT / "router.py", Path(temp) / "router.py")
    shutil.copy(ROOT / "router.example.json", Path(temp) / "router.json")
    spec = importlib.util.spec_from_file_location("router_under_test", Path(temp) / "router.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ROUTER = load_router()


class PathTests(unittest.TestCase):
    def test_missing_v1_is_added(self):
        self.assertEqual(ROUTER.backend_path("/responses"), "/v1/responses")
        self.assertEqual(ROUTER.backend_path("/messages"), "/v1/messages")
        self.assertEqual(ROUTER.backend_path("/chat/completions"),
                         "/v1/chat/completions")

    def test_duplicate_v1_is_collapsed(self):
        self.assertEqual(ROUTER.backend_path("/v1/v1/messages"), "/v1/messages")

    def test_correct_and_router_paths_are_untouched(self):
        for path in ("/v1/messages", "/v1", "/health", "/router/status", "/models"):
            self.assertEqual(ROUTER.backend_path(path), path)


class RewriteTests(unittest.TestCase):
    def test_effort_is_mapped_in_all_three_protocols(self):
        model = ROUTER.MODELS["coder390"]
        for body in (
            {"model": "high-alias", "reasoning_effort": "high"},
            {"model": "high-alias", "reasoning": {"effort": "high"}},
            {"model": "high-alias", "output_config": {"effort": "high"}},
        ):
            rewritten = json.loads(ROUTER.rewrite_body(json.dumps(body).encode(), model))
            self.assertEqual(rewritten["model"], "coder390")
            effort = (rewritten.get("reasoning_effort")
                      or rewritten.get("reasoning", {}).get("effort")
                      or rewritten.get("output_config", {}).get("effort"))
            self.assertEqual(effort, "xhigh")

    def test_function_call_output_parts_become_a_string(self):
        body = {
            "model": "coder390",
            "input": [{
                "type": "function_call_output",
                "call_id": "call_1",
                "output": [{"type": "input_text", "text": "a"}, {"text": "b"}],
            }],
        }
        rewritten = json.loads(ROUTER.rewrite_body(json.dumps(body).encode(),
                                                   ROUTER.MODELS["coder390"]))
        self.assertEqual(rewritten["input"][0]["output"], "a\nb")


class StripTests(unittest.TestCase):
    def strip(self, body, error):
        return ROUTER.drop_rejected(
            json.dumps(body).encode(),
            json.dumps({"error": error}).encode(),
        )

    def test_named_top_level_field_is_removed(self):
        body = {"model": "coder390", "client_metadata": {"x": 1}, "input": "hi"}
        rewritten, what = self.strip(body, {
            "code": "unknown_parameter",
            "param": "client_metadata",
            "message": "unknown parameter: client_metadata",
        })
        self.assertEqual(json.loads(rewritten), {"model": "coder390", "input": "hi"})
        self.assertEqual(what, "client_metadata")

    def test_nested_field_is_removed_with_its_empty_parent(self):
        body = {"model": "coder390", "text": {"verbosity": "high"}}
        rewritten, what = self.strip(body, {
            "code": "text_option_not_supported",
            "param": "text",
            "message": "text.verbosity is not supported",
        })
        self.assertEqual(json.loads(rewritten), {"model": "coder390"})
        self.assertEqual(what, "text")

    def test_tools_are_filtered_and_never_dropped_whole(self):
        body = {"model": "coder390", "tools": [
            {"type": "function", "name": "Bash", "strict": True},
            {"type": "custom", "name": "Patch"},
        ]}
        rewritten, _ = self.strip(body, {
            "code": "tool_type_not_supported",
            "param": "tools",
            "message": "only function tools are supported",
        })
        tools = json.loads(rewritten)["tools"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["name"], "Bash")
        self.assertIs(tools[0]["strict"], False)

    def test_value_errors_are_passed_through(self):
        result = self.strip({"max_output_tokens": 99999}, {
            "code": "invalid_value",
            "param": "max_output_tokens",
            "message": "max_output_tokens is too large",
        })
        self.assertIsNone(result)


class ResolveTests(unittest.TestCase):
    def test_alias_is_case_insensitive(self):
        self.assertEqual(ROUTER.resolve_model("  DFlash "), "coder390")
        self.assertEqual(ROUTER.resolve_model("IQ3_S"), "strata")
        self.assertIsNone(ROUTER.resolve_model("no-such-model"))


if __name__ == "__main__":
    unittest.main()
