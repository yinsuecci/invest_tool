import json
import tempfile
import unittest
from pathlib import Path

from dca_lab.report_index import build_index, summarize


def write_report(root, name, payload):
    directory = Path(root) / name
    directory.mkdir(parents=True)
    (directory / "report.json").write_text(json.dumps(payload), encoding="utf-8")
    (directory / "index.html").write_text("<html></html>", encoding="utf-8")
    return directory


class ReportIndexTest(unittest.TestCase):
    def test_missing_results_renders_dash(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = write_report(tmp, "old-001", {"version": "0.1.0", "family": "vwap"})
            row = summarize(directory)
            self.assertEqual(row["equity"], None)
            self.assertEqual(row["default"], "—")
            output = build_index(tmp, Path(tmp) / "index.html")
            html = output.read_text(encoding="utf-8")
            self.assertIn("old-001", html)
            self.assertIn("—", html)
            self.assertNotIn("None", html)

    def test_rows_and_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "version": "0.15.0",
                "family": "vwap",
                "default_policy": "vwap_fitted",
                "selection": {"selected_policy": "vwap_fitted", "status": "开发段首选"},
                "decisions": {"vwap_fitted": {"status": "研究", "live": False}},
                "protocol": {"frozen_at": "2026-11-01T03:45:34+00:00"},
                "results": {"full": {"vwap_fitted": {
                    "current": {"equity": 2326.16, "twr_return": 0.0808, "execution_rate": 0.9942,
                                "max_drawdown": -0.117},
                    "vs_four": {"equity_delta": -10.8868, "daily_return_gain_bps": -2.1},
                }}},
            }
            write_report(tmp, "vwap-006", payload)
            output = build_index(tmp, Path(tmp) / "index.html")
            html = output.read_text(encoding="utf-8")
            self.assertIn('href="vwap-006/index.html"', html)
            self.assertIn('href="vwap-006/report.json"', html)
            self.assertIn("2,326.16", html)
            self.assertIn("开发段首选", html)
            self.assertIn("0 个", html)
            self.assertIn('id="filter-text"', html)
            self.assertIn('data-family="vwap"', html)
            self.assertIn('aria-live="polite"', html)

    def test_cache_reuses_unchanged_and_refreshes_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "version": "0.15.0",
                "family": "vwap",
                "default_policy": "vwap_fitted",
                "protocol": {"frozen_at": "2026-11-01T03:45:34+00:00"},
                "results": {"full": {"vwap_fitted": {
                    "current": {"equity": 2326.16},
                    "vs_four": {"equity_delta": -10.89},
                }}},
            }
            write_report(tmp, "vwap-006", payload)
            first = build_index(tmp, Path(tmp) / "a.html").read_text(encoding="utf-8")
            cache = Path(tmp) / ".index-cache.json"
            self.assertTrue(cache.is_file())
            second = build_index(tmp, Path(tmp) / "b.html").read_text(encoding="utf-8")
            self.assertEqual(first, second)
            payload["results"]["full"]["vwap_fitted"]["current"]["equity"] = 9999.99
            (Path(tmp) / "vwap-006" / "report.json").write_text(json.dumps(payload), encoding="utf-8")
            third = build_index(tmp, Path(tmp) / "c.html").read_text(encoding="utf-8")
            self.assertIn("9,999.99", third)

    def test_no_cache_writes_no_cache_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_report(tmp, "old-001", {"version": "0.1.0", "family": "vwap"})
            build_index(tmp, Path(tmp) / "index.html", use_cache=False)
            self.assertFalse((Path(tmp) / ".index-cache.json").exists())

    def test_real_reports_index(self):
        reports = Path("reports")
        if not (reports / "smart-003" / "report.json").is_file():
            self.skipTest("real reports are not present")
        with tempfile.TemporaryDirectory() as tmp:
            output = build_index(reports, Path(tmp) / "index.html")
            html = output.read_text(encoding="utf-8")
            self.assertIn("smart-003", html)
            self.assertIn("vwap-006", html)
            self.assertLess(html.index("smart-003"), html.index("vwap-006"))
            self.assertIn("全部回测报告索引", html)
            self.assertIn("早期单标的/组合实验", html)
            self.assertNotIn("None", html)


if __name__ == "__main__":
    unittest.main()
