import json
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

from dca_lab.report import render_report


class StructureParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.panels = {}
        self.ids = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'id' in attrs:
            self.ids.append(attrs['id'])
        if tag == 'section':
            self.stack.append(attrs.get('id'))
            if 'data-view' in attrs:
                self.panels[attrs['data-view']] = {'parent': self.stack[-2] if len(self.stack) > 1 else None, **attrs}

    def handle_endtag(self, tag):
        if tag == 'section':
            if not self.stack:
                raise AssertionError('Unbalanced dashboard section')
            self.stack.pop()


class DashboardTests(unittest.TestCase):
    @unittest.skipUnless(Path('reports/five-indicator-002/report.json').exists(), 'Generate report first')
    def test_four_independent_panels_and_unchanged_payload(self):
        source = Path('reports/five-indicator-002/report.json')
        before = source.read_bytes()
        report = json.loads(before)
        with tempfile.TemporaryDirectory(dir=Path(__file__).parents[1]) as directory:
            html = render_report(report, directory).read_text(encoding='utf-8')
        self.assertEqual(source.read_bytes(), before)
        self.assertNotIn('__PORTFOLIO_UI_', html)
        self.assertIn('window.DcaDashboard.init', html)
        self.assertIn('prefers-reduced-motion:reduce', html)
        parser = StructureParser()
        parser.feed(html)
        self.assertFalse(parser.stack)
        self.assertEqual(set(parser.panels), {'overview', 'chart', 'research', 'ledger'})
        self.assertTrue(all(panel['parent'] is None for panel in parser.panels.values()))
        self.assertEqual(len(parser.ids), len(set(parser.ids)))
        for view, panel in parser.panels.items():
            self.assertEqual(panel['role'], 'tabpanel')
            self.assertEqual(panel['aria-labelledby'], 'tab-' + view)
        start = html.index('<script id="report-data" type="application/json">') + len('<script id="report-data" type="application/json">')
        self.assertEqual(json.loads(html[start:html.index('</script>', start)]), report)

    def test_navigation_supports_keyboard_and_chart_visibility_gate(self):
        root = Path(__file__).parents[1] / 'dca_lab'
        script = (root / 'portfolio_dashboard.js').read_text(encoding='utf-8')
        template = (root / 'portfolio_template.html').read_text(encoding='utf-8')
        for key in ('ArrowLeft', 'ArrowRight', 'Home', 'End'):
            self.assertIn(key, script)
        self.assertIn("if(dashboard.view!=='chart')return", template)
        self.assertIn('跳到当前工作区', template)
        self.assertIn("popstate", script)

    @unittest.skipUnless(Path('reports/vwap-002/report.json').exists(), 'Generate VWAP report first')
    def test_vwap_candidate_count_does_not_rewrite_tier_amounts(self):
        report = json.loads(Path('reports/vwap-002/report.json').read_text(encoding='utf-8'))
        with tempfile.TemporaryDirectory(dir=Path(__file__).parents[1]) as directory:
            html = render_report(report, directory).read_text(encoding='utf-8')
        self.assertIn('6/12/18/24U', html)
        self.assertIn('6/12/24/36U', html)
        self.assertNotIn('6/5/18/24U', html)
        self.assertIn('id="chart-cost-rows"', html)
