"""Fail-closed, allowlisted source export. Never prints matched secret values."""

import argparse
import re
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOP = {'README.md', 'pyproject.toml', '.gitignore', 'MANIFEST.in', 'SECURITY.md', 'LICENSE'}
DIRS = {'dca_lab', 'tests', 'tools', 'docs'}
EXTENSIONS = {'.py', '.js', '.css', '.html', '.md', '.json', '.toml', '.in'}
PRIVATE_PARTS = {'data', 'reports', 'runtime', 'private', 'release', 'dist', 'build',
                 '.git', '.codex', '.agents', '.venv', '__pycache__', '.pytest_cache'}
PATTERNS = {
    'private-key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'),
    'credential-assignment': re.compile(
        r'''(?i)["']?(?:api[_-]?(?:key|secret)|password|access[_-]?token|client[_-]?secret)["']?\s*[:=]\s*["']([A-Za-z0-9_+/=.-]{24,})["']'''),
    'provider-token': re.compile(r'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|AKIA[A-Z0-9]{16}|sk-[A-Za-z0-9_-]{32,})'),
    'credential-url': re.compile(r'https?://[^\s/:]+:[^\s/@]+@'),
    'personal-home': re.compile(r'(?i)(?:[A-Z]:[\\/]Users[\\/][^\s"<>]+|/' + r'Users/[^\s/"<>]+|/' + r'home/[^\s/"<>]+)'),
}


def allowed(path):
    parts = path.parts
    if not parts or any(p in PRIVATE_PARTS for p in parts):
        return False
    if any(p.startswith('.env') for p in parts):
        return False
    if path.suffix == '.json' and path.as_posix() != 'dca_lab/vendor/source.json':
        return False
    if len(parts) == 1:
        return path.name in TOP
    return parts[0] in DIRS and (path.suffix in EXTENSIONS or path.name in {'LICENSE', 'NOTICE'})


def candidates(root):
    return sorted(p for p in root.rglob('*') if allowed(p.relative_to(root)) and not p.is_dir())


def scan(root, paths):
    findings = []
    for path in paths:
        relative = path.relative_to(root).as_posix()
        # Symlinks, including directory symlinks, must not import outside data.
        if any(p.is_symlink() for p in (path, *path.parents)):
            findings.append((relative, 0, 'symlink'))
            continue
        try:
            content = path.read_text(encoding='utf-8')
        except (OSError, UnicodeError):
            findings.append((relative, 0, 'unreadable-or-binary'))
            continue
        for line_number, line in enumerate(content.splitlines(), 1):
            for name, pattern in PATTERNS.items():
                if pattern.search(line):
                    findings.append((relative, line_number, name))
    return findings


def tracked_findings(root):
    if not (root / '.git').exists():
        return []
    result = subprocess.run(['git', 'ls-files', '-z'], cwd=root, capture_output=True, check=True)
    return [(name, 0, 'tracked-outside-release-allowlist')
            for name in result.stdout.decode('utf-8').split('\0') if name and not allowed(Path(name))]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Scan without exporting')
    args = parser.parse_args()
    paths = candidates(ROOT)
    findings = scan(ROOT, paths) + tracked_findings(ROOT)
    if findings:
        for name, line, rule in findings:
            print(f'{name}:{line}: {rule}')
        print('Release blocked. Matched values are intentionally not printed.')
        return 1
    print(f'Privacy checks passed: {len(paths)} source files. No private artifacts included.')
    if not args.check:
        output = ROOT / 'release' / 'dca-lab-source.zip'
        output.parent.mkdir(exist_ok=True)
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
            for path in paths:
                # Recheck before writing; never follow a newly introduced symlink.
                if scan(ROOT, [path]):
                    raise RuntimeError('Source changed during export; export rejected')
                archive.writestr(path.relative_to(ROOT).as_posix(), path.read_bytes())
        print('Exported release/dca-lab-source.zip')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
