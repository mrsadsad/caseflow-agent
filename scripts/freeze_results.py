"""Copy a completed evaluation's JSON and source snapshots into versionable evidence."""
import argparse
from pathlib import Path
import shutil


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    if not (args.source / 'report.json').is_file():
        parser.error('source must contain completed report.json')
    args.destination.mkdir(parents=True, exist_ok=False)
    for file in args.source.glob('*.json'):
        shutil.copy2(file, args.destination / file.name)
    shutil.copytree(args.source / 'source', args.destination / 'source')
    print(args.destination.resolve())


if __name__ == '__main__':
    main()
