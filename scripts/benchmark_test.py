"""Development benchmark harness contracts: semantic replay and interleaved medians."""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import benchmark  # noqa: E402


def _reference(value):
    if value == "boom":
        raise ValueError("bad input")
    return value


class ReplayCallTests(unittest.TestCase):
    def test_matching_values_and_exceptions_pass(self):
        report = benchmark.replay_calls(_reference, _reference, [
            ("empty", ("",), {}),
            ("unicode", ("中文-😀",), {}),
            ("exception", ("boom",), {}),
        ])
        self.assertEqual(report["total"], 3)
        self.assertEqual(report["passed"], 3)
        self.assertEqual(report["failed"], 0)
        self.assertEqual(report["failures"], [])

    def test_value_mismatch_is_reported_without_running_later_cases(self):
        calls = []

        def candidate(value):
            calls.append(value)
            return value + "changed"

        report = benchmark.replay_calls(_reference, candidate, [
            ("first", ("a",), {}),
            ("second", ("b",), {}),
        ])
        self.assertEqual(report["passed"], 0)
        self.assertEqual(report["failed"], 2)
        self.assertEqual([f["case"] for f in report["failures"]], ["first", "second"])
        self.assertEqual(calls, ["a", "b"])

    def test_exception_type_and_message_must_match(self):
        def candidate(value):
            if value == "boom":
                raise RuntimeError("bad input")
            return value

        report = benchmark.replay_calls(_reference, candidate, [
            ("exception", ("boom",), {}),
        ])
        self.assertEqual(report["failed"], 1)
        self.assertIn("ValueError", report["failures"][0]["expected"])
        self.assertIn("RuntimeError", report["failures"][0]["actual"])

    def test_empty_replay_is_explicitly_empty(self):
        report = benchmark.replay_calls(_reference, _reference, [])
        self.assertEqual(report, {
            "total": 0, "passed": 0, "failed": 0, "failures": [],
        })


class InterleavedMedianTests(unittest.TestCase):
    def test_alternates_order_and_reports_sample_medians(self):
        order = []

        def reference():
            order.append("reference")

        def candidate():
            order.append("candidate")

        durations = iter([0.4, 0.1, 0.2, 0.3, 0.2, 0.2])

        def fake_timed(fn):
            seconds = next(durations)
            return {"seconds": seconds, "cpu_seconds": seconds,
                    "peak_memory_bytes": 0}, fn()

        with mock.patch.object(benchmark, "timed", side_effect=fake_timed):
            result = benchmark.interleaved_median(reference, candidate, rounds=3)

        self.assertEqual(order, [
            "reference", "candidate", "candidate", "reference",
            "reference", "candidate",
        ])
        self.assertEqual(result["reference"]["seconds"], 0.3)
        self.assertEqual(result["candidate"]["seconds"], 0.2)
        self.assertEqual(result["speedup"], 1.5)
        self.assertEqual(result["rounds"], 3)
        self.assertEqual(result["reference"]["samples_seconds"], [0.4, 0.3, 0.2])
        self.assertEqual(result["candidate"]["samples_seconds"], [0.1, 0.2, 0.2])

    def test_rejects_non_positive_round_count(self):
        with self.assertRaises(ValueError):
            benchmark.interleaved_median(lambda: None, lambda: None, rounds=0)


class ConcurrentInspectBenchmarkTests(unittest.TestCase):
    def test_replays_synthetic_edges_before_interleaved_timing(self):
        class FakeEngine:
            def inspect(self, path):
                if path == "broken.zip":
                    raise ValueError("bad archive")
                return {"entries": [] if path == "empty.zip" else [{"path": path}]}

        result = benchmark.concurrent_inspect(
            FakeEngine(), ["one.zip", "two.zip"],
            ["empty.zip", "中文-😀.zip", "broken.zip"], workers=2, rounds=3)

        self.assertEqual(result["replay"]["total"], 3)
        self.assertEqual(result["replay"]["passed"], 3)
        self.assertEqual(result["replay"]["failed"], 0)
        self.assertEqual(result["rounds"], 3)
        self.assertEqual(len(result["sequential"]["samples_seconds"]), 3)
        self.assertEqual(len(result["parallel"]["samples_seconds"]), 3)


class ReplayGateTests(unittest.TestCase):
    def test_semantic_mismatch_returns_nonzero_after_writing_json(self):
        import tempfile

        result = {"cases": {"concurrent_inspect": {"replay": {
            "failed": 1, "failures": [{"case": "synthetic"}],
        }}}}
        with tempfile.TemporaryDirectory(prefix="arca-benchmark-gate-") as tmp:
            output = os.path.join(tmp, "result.json")
            argv = ["benchmark.py", "--json", output]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(benchmark, "run", return_value=result), \
                    mock.patch("builtins.print"):
                code = benchmark.main()
            self.assertEqual(code, 1)
            self.assertTrue(os.path.isfile(output))


if __name__ == "__main__":
    unittest.main()
