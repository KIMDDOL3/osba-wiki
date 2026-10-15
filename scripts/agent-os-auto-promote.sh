#!/usr/bin/env bash
set -euo pipefail

VAULT_ROOT="${VAULT_ROOT:-$HOME/Documents/wiki}"
MODE="dry-run"
ONLY_FILE=""
REPORT_ONLY=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) MODE="apply"; REPORT_ONLY=0 ;;
    --report) MODE="dry-run"; REPORT_ONLY=1 ;;
    --only-file)
      shift
      ONLY_FILE="${1:-}"
      ;;
    --help|-h)
      cat <<'EOF'
Usage: agent-os-auto-promote.sh [--dry-run|--apply] [--only-file PATH]

Scans queries/ for promotion candidates.
Rules:
- notes with comparison cues -> comparisons/
- notes with concept cues -> concepts/
- otherwise stay in queries/

--only-file limits the scan to a single note path for safe testing.
EOF
      exit 0
      ;;
  esac
  shift
done

python3 - "$VAULT_ROOT" "$MODE" "$ONLY_FILE" <<'PY'
import sys, re, shutil
from pathlib import Path
from datetime import datetime

vault = Path(sys.argv[1]).expanduser().resolve()
mode = sys.argv[2]
only_file = Path(sys.argv[3]).expanduser().resolve() if len(sys.argv) > 3 and sys.argv[3] else None
queries = vault / 'queries'
concepts = vault / 'concepts'
comparisons = vault / 'comparisons'

comparison_words = ['비교', '장단점', 'vs', '대조', '차이', '선택', '추천 상황', 'A/B']
concept_words = ['한 줄 정의', '구성 요소', '개념', '정의', '원리', '핵심', '설명', '구조', '운영체제']


def frontmatter(text):
    if not text.startswith('---\n'):
        return {}, text
    parts = text.split('---\n', 2)
    if len(parts) < 3:
        return {}, text
    fm_text = parts[1]
    body = parts[2]
    fm = {}
    for line in fm_text.splitlines():
        if ':' in line:
            k, v = line.split(':', 1)
            fm[k.strip()] = v.strip().strip('"')
    return fm, body


def decide(title, text, fm):
    low = f"{title}\n{text}".lower()
    if any(w.lower() in low for w in comparison_words):
        return comparisons, 'comparison cues'
    if fm.get('type') == 'concept' or any(w.lower() in low for w in concept_words):
        return concepts, 'concept cues'
    return None, None

reports = []
for path in sorted(queries.rglob('*.md')):
    if '프로젝트' not in path.parts:
        continue
    if only_file and path.resolve() != only_file:
        continue
    text = path.read_text(encoding='utf-8')
    fm, body = frontmatter(text)
    title = fm.get('title', path.stem)
    scope_text = f"{title}\n{body}"
    tags = fm.get('tags', '')
    if '에이전트 운영체제' not in scope_text and 'agent-os' not in tags.lower():
        continue
    if path.name.startswith('에이전트 운영체제 ') and 'MVP' in path.name:
        continue
    dest, reason = decide(title, body, fm)
    if not dest:
        continue
    rel = path.relative_to(queries)
    target = dest / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    new_text = text
    new_text = re.sub(r'^type:\s*query\s*$', f'type: {"comparison" if dest == comparisons else "concept"}', new_text, flags=re.M)
    new_text = re.sub(r'^updated:\s*.*$', f'updated: {datetime.now().strftime("%Y-%m-%d")}', new_text, flags=re.M)
    reports.append((path, target, reason))
    if mode == 'apply':
        shutil.move(str(path), str(target))
        target.write_text(new_text, encoding='utf-8')

if not reports:
    print('No promotion candidates found.')
else:
    for src, dst, reason in reports:
        print(f'{src} -> {dst} ({reason})')
PY
