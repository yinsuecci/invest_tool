import io
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from dca_lab.pipeline import generate


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.output = self.root / "output"

    def stages(self):
        events = []

        def assets(data, output, daily_total=30.):
            self.assertEqual(data, self.data)
            self.assertEqual(daily_total, 30.)
            output.mkdir()
            (output / "report.json").write_text("assets-fixture", encoding="utf-8")
            events.append("assets")

        def app(source, output):
            self.assertEqual(source.read_text(encoding="utf-8"), "assets-fixture")
            output.mkdir()
            (output / "index.html").write_text("page-fixture", encoding="utf-8")
            events.append("app")

        return events, assets, app

    def test_all_stages_run_in_order_with_explicit_sources(self):
        events, assets, app = self.stages()
        with patch("dca_lab.pipeline.fit_assets", side_effect=assets), patch(
            "dca_lab.pipeline.build", side_effect=app
        ):
            result = generate(self.data, self.output)
        self.assertEqual(events, ["assets", "app"])
        self.assertEqual(result["report"], self.output / "independent" / "report.json")
        self.assertTrue((result["app"] / "index.html").is_file())

    def test_existing_output_is_not_overwritten(self):
        self.output.mkdir()
        marker = self.output / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        with patch("dca_lab.pipeline.fit_assets") as assets:
            with self.assertRaises(FileExistsError):
                generate(self.data, self.output)
        assets.assert_not_called()
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_missing_data_creates_no_output(self):
        with self.assertRaises(ValueError):
            generate(self.root / "missing", self.output)
        self.assertFalse(self.output.exists())

    def test_failed_first_stage_never_builds_page(self):
        with patch("dca_lab.pipeline.fit_assets", side_effect=ValueError("fixture failure")), patch(
            "dca_lab.pipeline.build"
        ) as app:
            with self.assertRaises(ValueError):
                generate(self.data, self.output)
        app.assert_not_called()

    def test_missing_intermediate_report_is_an_error(self):
        with patch("dca_lab.pipeline.fit_assets"), patch("dca_lab.pipeline.build") as app:
            with self.assertRaises(RuntimeError):
                generate(self.data, self.output)
        app.assert_not_called()

    def test_missing_page_is_an_error(self):
        _, assets, _ = self.stages()
        with patch("dca_lab.pipeline.fit_assets", side_effect=assets), patch("dca_lab.pipeline.build"):
            with self.assertRaises(RuntimeError):
                generate(self.data, self.output)

    def test_cli_dispatches_to_pipeline_and_prints_start_command(self):
        from dca_lab.__main__ import main
        result = {"report": self.output / "independent" / "report.json", "app": self.output / "app"}
        output = io.StringIO()
        with patch("sys.argv", ["dca-lab", "generate", "--data", str(self.data),
                               "--output", str(self.output), "--daily-total", "42"]), patch(
            "dca_lab.pipeline.generate", return_value=result
        ) as pipeline, redirect_stdout(output):
            main()
        pipeline.assert_called_once_with(str(self.data), str(self.output), daily_total=42.)
        self.assertIn("python -m dca_lab.compact_app --source", output.getvalue())

    def test_cli_existing_output_has_nonzero_exit(self):
        from dca_lab.__main__ import main
        with patch("sys.argv", ["dca-lab", "generate"]), patch(
            "dca_lab.pipeline.generate", side_effect=FileExistsError("existing output")
        ), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                main()
        self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
