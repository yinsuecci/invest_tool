"""Release guard tests use synthetic secrets only."""

import tempfile
import unittest
import zipfile
from pathlib import Path

from tools.release_source import ROOT, allowed, candidates, scan, tracked_findings


class ReleasePrivacyTests(unittest.TestCase):
    def test_excludes_private_artifacts(self):
        for name in ('reports/latest/index.html', 'data/prices.json', 'runtime/orders.sqlite',
                     '.env', '.env.production', '.codex/settings.json', '_ma.md',
                     'dca_lab/credentials.json', 'dca_lab/private/key.py',
                     'dca_lab/account.json', 'tests/export.har', 'release/source.zip'):
            self.assertFalse(allowed(Path(name)), name)
        for name in ('README.md', 'SECURITY.md', 'dca_lab/compact.js',
                     'dca_lab/vendor/source.json', 'tests/test_gateway.py'):
            self.assertTrue(allowed(Path(name)), name)

    def test_redacted_scan_detects_synthetic_secrets_and_personal_paths(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            path = root / 'fixture.py'
            # These values are fabricated, not usable credentials.
            payload = 'api_secret = "' + 'A' * 64 + '"\n'
            payload += 'https://' + 'user:fake@' + 'example.invalid\n'
            payload += 'C:' + chr(92) + 'Users' + chr(92) + 'Example' + chr(92) + 'file\n'
            path.write_text(payload, encoding='utf-8')
            findings = scan(root, [path])
            self.assertEqual({f[2] for f in findings},
                             {'credential-assignment', 'credential-url', 'personal-home'})
            self.assertNotIn('A' * 64, str(findings))

    def test_source_tree_passes(self):
        self.assertEqual(scan(ROOT, candidates(ROOT)), [])
        self.assertEqual(tracked_findings(ROOT), [])

    def test_export_has_no_private_artifacts(self):
        archive = ROOT / 'release' / 'dca-lab-source.zip'
        if not archive.exists():
            self.skipTest('Run exporter to validate archive contents')
        with zipfile.ZipFile(archive) as bundle:
            self.assertTrue(bundle.namelist())
            self.assertTrue(all(allowed(Path(name)) for name in bundle.namelist()))


if __name__ == '__main__':
    unittest.main()
