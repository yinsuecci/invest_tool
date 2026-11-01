"""Combine verified public datasets without rewriting any prior research inputs."""

import argparse
import json
import shutil
from pathlib import Path

from dca_lab.market import digest, load_dataset


def combine(sources, output):
    output = Path(output)
    if output.exists():
        raise ValueError("Use a new output directory; preserve existing datasets")
    checked = []
    for source in sources:
        path = Path(source)
        meta = json.loads((path / 'manifest.json').read_text(encoding='utf-8'))
        manifest, info, _ = load_dataset(path, tuple(meta['assets']))
        checked.append((path, manifest, info))
    if len({m['end_exclusive'] for _, m, _ in checked}) != 1:
        raise ValueError("Dataset cutoffs must match")
    keys = [k for _, m, _ in checked for k in m['assets']]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate assets")
    output.mkdir(parents=True)
    assets, symbols = {}, {}
    for path, manifest, info in checked:
        for key, meta in manifest['assets'].items():
            shutil.copyfile(path / meta['file'], output / meta['file'])
            assets[key] = meta
            symbols[key] = info['symbols'][key]
    info = {'symbols': symbols, 'sources': [{'directory': str(p), 'exchange_info_sha256': m['exchange_info_sha256'],
                                            'downloaded_at': m['downloaded_at']} for p, m, _ in checked]}
    (output / 'exchange_info.json').write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding='utf-8')
    manifest = {**checked[0][1], 'assets': assets, 'exchange_info_sha256': digest(output / 'exchange_info.json'),
                'combined_sources': info['sources'], 'downloaded_at': max(m['downloaded_at'] for _, m, _ in checked),
                'requested_start': min(m['requested_start'] for _, m, _ in checked)}
    (output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    load_dataset(output, keys)
    print(f"Verified combined dataset: {output}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--sources', nargs='+', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    combine(args.sources, args.output)
