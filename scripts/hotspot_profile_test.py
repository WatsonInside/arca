"""Synthetic large-corpus hotspot profiler contracts."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "src"))

from arca import engine, zipfix  # noqa: E402
import hotspot_profile  # noqa: E402


class LegacyZipFixtureTests(unittest.TestCase):
    def test_stored_gbk_names_are_real_zip_entries_and_need_decoding(self):
        with tempfile.TemporaryDirectory(prefix="arca-legacy-fixture-") as tmp:
            path = Path(tmp) / "legacy.zip"
            hotspot_profile.make_legacy_zip(path, ["目录/中文.txt", "plain.txt"])

            with zipfile.ZipFile(path) as zf:
                self.assertEqual(len(zf.infolist()), 2)
                self.assertEqual(zf.testzip(), None)
            plan = zipfix.plan(str(path))
            self.assertIsNotNone(plan)
            self.assertEqual(plan["total"], 2)
            self.assertEqual(plan["fixed"], 1)
            self.assertEqual(plan["encoding"], "gbk")
            self.assertIn("目录/中文.txt", plan["names"].values())


class ProfileCallTests(unittest.TestCase):
    def test_profiles_only_the_operation_and_preserves_its_result(self):
        text = "Path = file.txt\nSize = 1\nAttributes = A\n\n"
        with tempfile.TemporaryDirectory(prefix="arca-profile-call-") as tmp:
            output = Path(tmp) / "slt.pstats"
            report, value = hotspot_profile.profile_call(
                "slt", lambda: engine._parse_slt(text), output)

            self.assertEqual(value[1][0]["path"], "file.txt")
            self.assertEqual(report["case"], "slt")
            self.assertGreaterEqual(report["seconds"], 0)
            self.assertTrue(output.is_file())
            self.assertTrue(any(row["name"] == "_parse_slt" for row in report["top"]))

    def test_profile_rows_use_checkout_root_not_literal_arca_directory(self):
        from unittest import mock

        with tempfile.TemporaryDirectory(prefix="arca-profile-root-") as tmp:
            root = Path(tmp) / "release-clean"
            output = Path(tmp) / "profile.pstats"
            filename = str(root / "src" / "arca" / "engine.py")
            stats = {(filename, 123, "_parse_slt"): (1, 1, 0.001, 0.001, {})}
            with mock.patch.object(hotspot_profile, "ROOT", root), \
                    mock.patch.object(hotspot_profile.pstats, "Stats") as stats_type:
                stats_type.return_value.stats = stats
                report, _value = hotspot_profile.profile_call(
                    "slt", lambda: {"preserved": True}, output)

            self.assertEqual([row["name"] for row in report["top"]], ["_parse_slt"])
            self.assertEqual(report["top"][0]["file"], "engine.py")

    def test_small_run_covers_all_five_synthetic_hotspots(self):
        with tempfile.TemporaryDirectory(prefix="arca-hotspot-run-") as tmp:
            root = Path(tmp)
            result = hotspot_profile.run(
                root / "work", root / "pstats", entries=20, selection_count=4)

            self.assertEqual(set(result["cases"]), {
                "zip_inspect", "legacy_zip_plan", "slt_parse",
                "prune_covered", "selected_bytes",
            })
            self.assertEqual(result["parameters"]["entries"], 20)
            for case in result["cases"].values():
                self.assertTrue(Path(case["pstats"]).is_file())
                self.assertGreaterEqual(case["seconds"], 0)


if __name__ == "__main__":
    unittest.main()
