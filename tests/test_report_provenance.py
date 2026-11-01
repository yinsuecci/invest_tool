import hashlib
import json
import unittest
from pathlib import Path

REPORTS = Path("reports")
DATA = Path("data/five-assets-2026-10-31")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def protocol_field(report, dotted):
    node = load(report)
    for part in dotted.split("."):
        node = node[part]
    return node


class ReportProvenanceTest(unittest.TestCase):
    """Every derived report must point at the exact bytes it was built from."""

    @classmethod
    def setUpClass(cls):
        if not (REPORTS / "smart-003" / "report.json").is_file():
            raise unittest.SkipTest("generated reports are not present")

    def test_source_report_hash_chain(self):
        chain = [
            ("smart-003", "protocol.protocol.source_report_sha256", "smart-002/report.json"),
            ("smart-002", "protocol.protocol.source_sha256", "vwap-007/report.json"),
            ("vwap-007", "protocol.protocol.source_report_sha256", "vwap-005/report.json"),
            ("vwap-006", "protocol.protocol.source_report_sha256", "vwap-005/report.json"),
        ]
        for report, field, source in chain:
            expected = protocol_field(REPORTS / report / "report.json", field)
            self.assertEqual(expected, digest(REPORTS / source), f"{report} <- {source}")

    def test_strategy_protocol_digest(self):
        expected = protocol_field(
            REPORTS / "smart-003" / "report.json",
            "protocol.protocol.strategy_protocol_sha256",
        )
        stored = load(REPORTS / "smart-002" / "adaptive-protocol.json")["sha256"]
        self.assertEqual(expected, stored)

    def test_ma_tiers_dataset_hashes(self):
        report_path = REPORTS / "ma-tiers-001" / "report.json"
        data = Path("data/seven-assets-2026-11-01")
        if not report_path.is_file() or not (data / "manifest.json").is_file():
            self.skipTest("ma-tiers report or dataset is not present")
        report = load(report_path)
        for key, meta in report["manifest"]["assets"].items():
            self.assertEqual(meta["sha256"], digest(data / meta["file"]), key)

    def test_dataset_hashes_match_manifest(self):
        if not (DATA / "manifest.json").is_file():
            self.skipTest("dataset snapshot is not present")
        manifest = load(DATA / "manifest.json")
        report = load(REPORTS / "smart-003" / "report.json")
        self.assertEqual(
            {key: value["sha256"] for key, value in manifest["assets"].items()},
            {key: value["sha256"] for key, value in report["manifest"]["assets"].items()},
        )
        for key, value in manifest["assets"].items():
            self.assertEqual(value["sha256"], digest(DATA / value["file"]), key)


if __name__ == "__main__":
    unittest.main()
