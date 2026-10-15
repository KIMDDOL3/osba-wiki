#!/usr/bin/env python3
"""Apply study-note metadata to selected Obsidian notes.

Policy for 2026-08-05:
- concepts/**.md except README.md: add #공부노트
- comparisons/**.md except README.md: add #공부노트
- queries/**.md except README.md: add #공부노트 because the user confirmed AI-generated/study materials are also OK
- raw/clippings is intentionally not bulk-tagged here; tag only after selection/promotion.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

VAULT = Path(os.environ.get("OBSIDIAN_VAULT_PATH") or os.environ.get("WIKI_PATH") or str(Path.home() / "Documents/wiki")).expanduser()
TARGET_DIRS = ['concepts', 'comparisons', 'queries']
SKIP_NAMES = {'README.md'}
TAG = '공부노트'


def split_fm(text: str) -> Tuple[Dict[str, str], str, str, bool]:
    """Return (kv map, frontmatter body, markdown body, has_frontmatter)."""
    if text.startswith('---\n'):
        end = text.find('\n---\n', 4)
        if end != -1:
            fm = text[4:end]
            body = text[end + len('\n---\n'):]
            kv = {}
            for line in fm.splitlines():
                if ':' in line and not line.startswith((' ', '-')):
                    k, v = line.split(':', 1)
                    kv[k.strip()] = v.strip()
            return kv, fm, body, True
    return {}, '', text, False


def has_tag_in_fm(fm: str, tag: str) -> bool:
    # handles inline tags: [a, 공부노트] and block tags:
    if re.search(r'(^|[\[\s,])' + re.escape(tag) + r'($|[\]\s,])', fm, re.M):
        # avoid body false positives; fm only
        return True
    return False


def add_tag_to_fm(fm: str, tag: str) -> str:
    lines = fm.splitlines()
    # Find top-level tags key.
    for i, line in enumerate(lines):
        if re.match(r'^tags\s*:', line):
            # inline list
            m = re.match(r'^(tags\s*:\s*)\[(.*)\]\s*$', line)
            if m:
                raw = m.group(2).strip()
                items = [x.strip().strip('"\'') for x in raw.split(',') if x.strip()]
                if tag not in items:
                    items.append(tag)
                lines[i] = m.group(1) + '[' + ', '.join(items) + ']'
                return '\n'.join(lines)
            # tags: value single-line
            after = line.split(':', 1)[1].strip()
            if after:
                items = [x.strip().strip('"\'') for x in after.split(',') if x.strip()]
                if tag not in items:
                    items.append(tag)
                lines[i] = 'tags: [' + ', '.join(items) + ']'
                return '\n'.join(lines)
            # block list: insert under tags if not present
            j = i + 1
            while j < len(lines) and (lines[j].startswith('  - ') or lines[j].strip() == ''):
                j += 1
            lines.insert(j, f'  - {tag}')
            return '\n'.join(lines)
    # no tags key: insert after title if possible, otherwise at end of fm
    insert_at = 1 if lines and lines[0].startswith('title:') else len(lines)
    lines.insert(insert_at, f'tags: [{tag}]')
    return '\n'.join(lines)


def add_or_keep_scalar(fm: str, key: str, value: str) -> str:
    if re.search(r'^' + re.escape(key) + r'\s*:', fm, re.M):
        return fm
    lines = fm.splitlines()
    # Put study metadata near tags when possible.
    insert_at = len(lines)
    for i, line in enumerate(lines):
        if re.match(r'^tags\s*:', line):
            insert_at = i + 1
            # If block tags, skip tag list lines.
            while insert_at < len(lines) and lines[insert_at].startswith('  - '):
                insert_at += 1
            break
    lines.insert(insert_at, f'{key}: {value}')
    return '\n'.join(lines)


def desired_type_for(path: Path) -> str:
    rel = path.relative_to(VAULT).as_posix()
    if rel.startswith('concepts/'):
        return 'concept'
    if rel.startswith('comparisons/'):
        return 'comparison'
    return 'query'


def process(path: Path, dry_run: bool) -> Dict[str, Any]:
    text = path.read_text(encoding='utf-8', errors='ignore')
    kv, fm, body, has_fm = split_fm(text)
    old = text
    changed_fields: List[str] = []
    if not has_fm:
        title = path.stem
        fm = f'title: {title}\ntags: [{TAG}]\ntype: {desired_type_for(path)}\nstudy_status: to-study\nreview: true'
        text = f'---\n{fm}\n---\n\n{body}'
        changed_fields = ['frontmatter', 'tags', 'type', 'study_status', 'review']
    else:
        if not has_tag_in_fm(fm, TAG):
            fm = add_tag_to_fm(fm, TAG)
            changed_fields.append('tags')
        if 'study_status' not in kv:
            fm = add_or_keep_scalar(fm, 'study_status', 'to-study')
            changed_fields.append('study_status')
        if 'review' not in kv:
            fm = add_or_keep_scalar(fm, 'review', 'true')
            changed_fields.append('review')
        # Keep existing type, but add it if absent for clean Dataview filtering.
        if 'type' not in kv:
            fm = add_or_keep_scalar(fm, 'type', desired_type_for(path))
            changed_fields.append('type')
        text = f'---\n{fm}\n---\n{body}'
    if text != old and not dry_run:
        path.write_text(text, encoding='utf-8')
    return {
        'path': str(path.relative_to(VAULT)),
        'changed': text != old,
        'fields': changed_fields,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--report', default='')
    args = ap.parse_args()
    dry_run = not args.apply
    files: List[Path] = []
    for d in TARGET_DIRS:
        base = VAULT / d
        files.extend(p for p in sorted(base.rglob('*.md')) if p.name not in SKIP_NAMES)
    results = [process(p, dry_run=dry_run) for p in files]
    summary = {
        'mode': 'apply' if args.apply else 'dry-run',
        'target_files': len(results),
        'changed_files': sum(1 for r in results if r['changed']),
        'unchanged_files': sum(1 for r in results if not r['changed']),
        'by_top_folder': {},
        'sample_changed': [r for r in results if r['changed']][:20],
    }
    for r in results:
        top = r['path'].split('/', 1)[0]
        summary['by_top_folder'].setdefault(top, {'target': 0, 'changed': 0})
        summary['by_top_folder'][top]['target'] += 1
        if r['changed']:
            summary['by_top_folder'][top]['changed'] += 1
    out = json.dumps(summary, ensure_ascii=False, indent=2)
    print(out)
    if args.report:
        Path(args.report).write_text(out + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
