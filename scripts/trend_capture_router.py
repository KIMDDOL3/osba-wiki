#!/usr/bin/env python3
"""Classify a trend capture note into Obsidian vault lanes.

Usage examples:

  python3 scripts/trend_capture_router.py --input "/path/to/note.md"
  python3 scripts/trend_capture_router.py --input "/path/to/note.md" --write
  python3 scripts/trend_capture_router.py --input "/path/to/note.md" --write --dry-run

The script inspects the note content and chooses one of:
  - queries
  - clippings
  - concepts
  - comparisons

By default it prints a compact JSON summary. With --write it writes a routed
copy into the target lane and prints the absolute output path.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

VAULT_ROOT = Path(__file__).resolve().parents[1]
DATE_STR = datetime.now().strftime("%Y-%m-%d")


@dataclass
class Classification:
    bucket: str
    score: int
    reasons: List[str]


INVALID_FILENAME_CHARS = r'<>:"/\\|?*'


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def split_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text

    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text

    fm_text = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1 :])
    return parse_frontmatter(fm_text), body.lstrip("\n")


def parse_frontmatter(text: str) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            items = [v.strip().strip('"').strip("'") for v in value[1:-1].split(",") if v.strip()]
            data[key] = items
        else:
            value = value.strip('"').strip("'")
            data[key] = value
    return data


def serialize_frontmatter(data: Dict[str, Any]) -> str:
    lines = ["---"]
    for key, value in data.items():
        if value is None:
            continue
        if isinstance(value, list):
            rendered = ", ".join(str(v) for v in value)
            lines.append(f"{key}: [{rendered}]")
        else:
            text = str(value)
            if "\n" in text:
                escaped = text.replace('"', '\\"')
                lines.append(f'{key}: "{escaped}"')
            else:
                lines.append(f"{key}: {text}")
    lines.append("---")
    return "\n".join(lines)


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).lower()


def count_placeholders(text: str) -> int:
    return len(re.findall(r"\{\{[^}]+\}\}", text))


def is_clipping_like(meta: Dict[str, Any], title: str, body: str) -> bool:
    blob = normalize("\n".join([title, body, json.dumps(meta, ensure_ascii=False)]))
    signals = [
        "원문",
        "발췌",
        "스크랩",
        "자막",
        "transcript",
        "clip",
        "article",
        "기사",
        "영상",
        "youtube",
        "url",
        "링크",
        "source",
    ]
    score = sum(1 for s in signals if s in blob)
    return score >= 3 or str(meta.get("type", "")).lower() in {"clipping", "clip", "raw", "source"}


def score_bucket(meta: Dict[str, Any], title: str, body: str) -> Classification:
    blob = normalize("\n".join([title, body, json.dumps(meta, ensure_ascii=False)]))
    placeholder_count = count_placeholders(body)

    scores = {"queries": 0, "clippings": 0, "concepts": 0, "comparisons": 0}
    reasons: Dict[str, List[str]] = {k: [] for k in scores}

    if placeholder_count >= 4:
        scores["queries"] += 3
        reasons["queries"].append("템플릿 플레이스홀더가 많아 초안/질의 노트로 판단")

    if is_clipping_like(meta, title, body):
        scores["clippings"] += 5
        reasons["clippings"].append("원문/발췌/링크 중심의 클리핑 신호가 강함")

    concept_terms = ["개념", "정의", "원리", "구조", "아키텍처", "framework", "방법", "why", "how", "설명", "시스템"]
    comp_terms = ["비교", "vs", "장단점", "차이", "선택", "대안", "a/b", "장점", "단점", "판단"]
    query_terms = ["질문", "오늘", "트렌드", "저장", "관찰", "기록", "워크플로", "자동화"]

    for term in concept_terms:
        if term in blob:
            scores["concepts"] += 2
            reasons["concepts"].append(f"'{term}' 개념 신호")

    for term in comp_terms:
        if term in blob:
            scores["comparisons"] += 2
            reasons["comparisons"].append(f"'{term}' 비교 신호")

    for term in query_terms:
        if term in blob:
            scores["queries"] += 1
            reasons["queries"].append(f"'{term}' 질의/운영 신호")

    if str(meta.get("type", "")).lower() == "concept":
        scores["concepts"] += 4
        reasons["concepts"].append("frontmatter type=concept")
    if str(meta.get("type", "")).lower() in {"comparison", "compare"}:
        scores["comparisons"] += 4
        reasons["comparisons"].append("frontmatter type=comparison")
    if str(meta.get("type", "")).lower() in {"query", "workflow", "trend-capture"}:
        scores["queries"] += 2
        reasons["queries"].append("frontmatter type=query/workflow/capture")

    if title.startswith("[") and title.endswith("]"):
        scores["queries"] += 1
        reasons["queries"].append("질문형 제목")

    best_bucket = max(scores.items(), key=lambda kv: (kv[1], kv[0]))[0]
    if scores[best_bucket] <= 0:
        best_bucket = "queries"
        reasons["queries"].append("기본값")

    if best_bucket == "clippings" and scores["concepts"] >= 6:
        best_bucket = "concepts"
    if best_bucket == "clippings" and scores["comparisons"] >= 6:
        best_bucket = "comparisons"

    if best_bucket == "queries" and scores["concepts"] >= 6 and scores["concepts"] >= scores["queries"] + 2:
        best_bucket = "concepts"
    if best_bucket == "queries" and scores["comparisons"] >= 6 and scores["comparisons"] >= scores["queries"] + 2:
        best_bucket = "comparisons"

    final_reasons = reasons[best_bucket][:]
    if not final_reasons:
        final_reasons.append("기본 분류")

    return Classification(bucket=best_bucket, score=scores[best_bucket], reasons=final_reasons)


def bucket_to_folder(bucket: str) -> Path:
    if bucket == "queries":
        return VAULT_ROOT / "queries" / "트렌드 운영"
    if bucket == "clippings":
        return VAULT_ROOT / "raw/clippings" / "트렌드"
    if bucket == "concepts":
        return VAULT_ROOT / "concepts"
    if bucket == "comparisons":
        return VAULT_ROOT / "comparisons"
    return VAULT_ROOT / "queries" / "트렌드 운영"


def infer_title(meta: Dict[str, Any], body: str, fallback: str) -> str:
    for key in ("title", "name"):
        if meta.get(key):
            return str(meta[key]).strip()
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def infer_source_url(meta: Dict[str, Any], body: str) -> str:
    for key in ("url", "source_url", "sourceUrl"):
        if meta.get(key):
            return str(meta[key]).strip()
    for line in body.splitlines():
        if line.startswith("- http://") or line.startswith("- https://"):
            return line[2:].strip()
        if line.startswith("http://") or line.startswith("https://"):
            return line.strip()
    return ""


def slugify(value: str, max_len: int = 80) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = re.sub(rf"[{re.escape(INVALID_FILENAME_CHARS)}]", "", value)
    value = value.replace("/", " ").replace("\\", " ")
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) > max_len:
        value = value[:max_len].rstrip()
    return value or "untitled"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    for i in range(2, 100):
        candidate = parent / f"{stem} {i}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not find a unique path for {path}")


def strip_leading_heading(body: str, title: str) -> str:
    lines = body.splitlines()
    if not lines:
        return body
    first = lines[0].strip()
    if first == f"# {title}" or first.startswith("# "):
        return "\n".join(lines[1:]).lstrip("\n")
    return body


def render_output(meta: Dict[str, Any], body: str, source_path: Path, title: str, bucket: str, classification: Classification) -> str:
    body = strip_leading_heading(body, title)
    out_meta = dict(meta)
    out_meta["title"] = title
    out_meta["type"] = {
        "queries": "query",
        "clippings": "clipping",
        "concepts": "concept",
        "comparisons": "comparison",
    }.get(bucket, bucket)
    out_meta["trend_bucket"] = bucket
    out_meta["trend_router_version"] = "1"
    out_meta["trend_router_reason"] = "; ".join(classification.reasons)
    out_meta["source_note"] = str(source_path.relative_to(VAULT_ROOT)) if source_path.is_relative_to(VAULT_ROOT) else str(source_path)
    out_meta.setdefault("date", DATE_STR)
    out_meta.setdefault("captured_at", DATE_STR)
    source_url = infer_source_url(meta, body)
    if source_url:
        out_meta.setdefault("url", source_url)

    rendered = [serialize_frontmatter(out_meta), "", f"# {title}", "", f"> 자동 분류 결과: `{bucket}`", f"> 분류 근거: {', '.join(classification.reasons)}", ""]
    if body.strip():
        rendered.extend(["---", "", body.strip(), ""])
    return "\n".join(rendered).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Classify a trend capture note into an Obsidian lane")
    parser.add_argument("--input", required=True, help="Path to the source markdown note")
    parser.add_argument("--write", action="store_true", help="Write the routed copy to the target lane")
    parser.add_argument("--dry-run", action="store_true", help="Do not write files; only report the route")
    args = parser.parse_args()

    source_path = Path(args.input).expanduser().resolve()
    if not source_path.exists():
        print(json.dumps({"ok": False, "error": f"missing input: {source_path}"}, ensure_ascii=False), file=sys.stderr)
        return 2

    text = read_text(source_path)
    meta, body = split_frontmatter(text)
    title = infer_title(meta, body, source_path.stem)
    classification = score_bucket(meta, title, body)
    folder = bucket_to_folder(classification.bucket)
    filename = f"{DATE_STR} - {slugify(title)}.md"
    output_path = unique_path(folder / filename)

    result = {
        "ok": True,
        "source": str(source_path),
        "title": title,
        "bucket": classification.bucket,
        "score": classification.score,
        "reasons": classification.reasons,
        "output": str(output_path),
    }

    if args.write and not args.dry_run:
        folder.mkdir(parents=True, exist_ok=True)
        output_path.write_text(render_output(meta, body, source_path, title, classification.bucket, classification), encoding="utf-8")
        result["written"] = True
    else:
        result["written"] = False

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
