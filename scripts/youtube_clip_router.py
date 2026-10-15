#!/usr/bin/env python3
"""Save YouTube clip markdown from clipboard into the vault and route it.

Workflow:
1) Use a browser bookmarklet to copy a structured markdown snippet from a YouTube page.
2) Run this script from Obsidian (hotkey/command).
3) The script reads clipboard text (or --input) and writes a routed note.

Targets:
- raw/clippings/YouTube/Transcripts for transcript-like clips
- raw/clippings/YouTube/Live for live-stream clips
- raw/clippings/YouTube for regular video clips
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

VAULT_ROOT = Path(__file__).resolve().parents[1]
DATE_STR = datetime.now().strftime("%Y-%m-%d")
INVALID_FILENAME_CHARS = r'<>:"/\\|?*'


@dataclass
class Classification:
    bucket: str
    score: int
    reasons: List[str]


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).lower()


def read_clipboard() -> str:
    try:
        proc = subprocess.run(["pbpaste"], check=True, capture_output=True, text=True)
        return proc.stdout
    except Exception as exc:  # pragma: no cover - platform specific
        raise RuntimeError(f"failed to read clipboard: {exc}") from exc


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
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            items = [v.strip().strip('"').strip("'") for v in value[1:-1].split(",") if v.strip()]
            data[key] = items
        else:
            data[key] = value.strip('"').strip("'")
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


def infer_title(meta: Dict[str, Any], body: str, fallback: str) -> str:
    for key in ("title", "name"):
        if meta.get(key):
            return str(meta[key]).strip()
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def infer_channel(meta: Dict[str, Any], body: str) -> str:
    for key in ("author", "channel", "channel_name"):
        if meta.get(key):
            return str(meta[key]).strip()
    match = re.search(r"(?:채널|Channel)\s*[:：]\s*(.+)", body, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return ""


def infer_source_url(meta: Dict[str, Any], body: str) -> str:
    for key in ("url", "source_url", "sourceUrl"):
        if meta.get(key):
            return str(meta[key]).strip()
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("- http://") or stripped.startswith("- https://"):
            return stripped[2:].strip()
        if stripped.startswith("http://") or stripped.startswith("https://"):
            return stripped
    return ""


def infer_publication_date(meta: Dict[str, Any], body: str) -> str:
    for key in ("published", "uploadDate", "date"):
        if meta.get(key):
            return str(meta[key]).strip()
    match = re.search(r"(?:게시일|업로드|Published)\s*[:：]\s*(\d{4}-\d{2}-\d{2})", body, re.IGNORECASE)
    if match:
        return match.group(1)
    return DATE_STR


def is_transcript_like(meta: Dict[str, Any], title: str, body: str) -> bool:
    blob = normalize("\n".join([title, body, json.dumps(meta, ensure_ascii=False)]))
    if any(term in blob for term in ["전사본", "transcript", "자막", "subtitle", "caption", "captioned"]):
        return True
    body_lines = [line.strip() for line in body.splitlines() if line.strip()]
    return len(body_lines) >= 25


def is_live_like(meta: Dict[str, Any], title: str, body: str) -> bool:
    blob = normalize("\n".join([title, body, json.dumps(meta, ensure_ascii=False)]))
    return any(term in blob for term in ["live", "라이브", "stream", "생방송"])


def classify(meta: Dict[str, Any], title: str, body: str) -> Classification:
    scores = {"transcripts": 0, "live": 0, "videos": 0}
    reasons = {k: [] for k in scores}

    if is_transcript_like(meta, title, body):
        scores["transcripts"] += 5
        reasons["transcripts"].append("전사/자막/긴 본문 신호")
    if is_live_like(meta, title, body):
        scores["live"] += 4
        reasons["live"].append("라이브/스트림 신호")

    blob = normalize("\n".join([title, body, json.dumps(meta, ensure_ascii=False)]))
    video_terms = ["youtube", "영상", "video", "유튜브", "채널", "구독", "조회수"]
    for term in video_terms:
        if term in blob:
            scores["videos"] += 1
            reasons["videos"].append(f"'{term}' 영상 신호")

    best = max(scores.items(), key=lambda kv: (kv[1], kv[0]))[0]
    if scores[best] <= 0:
        best = "videos"
        reasons["videos"].append("기본값")
    return Classification(best, scores[best], reasons[best] or ["기본 분류"])


def bucket_to_folder(bucket: str) -> Path:
    if bucket == "transcripts":
        return VAULT_ROOT / "raw/clippings" / "YouTube" / "Transcripts"
    if bucket == "live":
        return VAULT_ROOT / "raw/clippings" / "YouTube" / "Live"
    return VAULT_ROOT / "raw/clippings" / "YouTube"


def slugify(value: str, max_len: int = 90) -> str:
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


def render_output(meta: Dict[str, Any], body: str, source_path: Optional[Path], title: str, bucket: str, classification: Classification) -> str:
    body = strip_leading_heading(body, title)
    out_meta = dict(meta)
    out_meta["title"] = title
    out_meta["type"] = bucket[:-1] if bucket.endswith("s") else bucket
    out_meta["youtube_bucket"] = bucket
    out_meta["youtube_router_version"] = "1"
    out_meta["youtube_router_reason"] = "; ".join(classification.reasons)
    out_meta.setdefault("date", DATE_STR)
    out_meta.setdefault("captured_at", DATE_STR)
    if source_path is not None:
        try:
            out_meta["source_note"] = str(source_path.relative_to(VAULT_ROOT))
        except ValueError:
            out_meta["source_note"] = str(source_path)
    source_url = infer_source_url(meta, body)
    if source_url:
        out_meta.setdefault("url", source_url)

    rendered = [serialize_frontmatter(out_meta), "", f"# {title}"]
    channel = infer_channel(meta, body)
    published = infer_publication_date(meta, body)
    if channel or source_url or published:
        rendered.extend(["", "## 메타데이터"])
        if channel:
            rendered.append(f"- 채널: {channel}")
        if source_url:
            rendered.append(f"- URL: {source_url}")
        if published:
            rendered.append(f"- 게시일: {published}")
    rendered.extend(["", f"> 자동 분류 결과: `{bucket}`", f"> 분류 근거: {', '.join(classification.reasons)}", ""])
    if body.strip():
        rendered.extend(["---", "", body.strip(), ""])
    return "\n".join(rendered).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Save YouTube clip markdown from clipboard into the vault")
    parser.add_argument("--input", help="Optional path to a markdown clip file instead of clipboard")
    parser.add_argument("--write", action="store_true", help="Write the routed note to disk")
    parser.add_argument("--dry-run", action="store_true", help="Do not write files; only report the route")
    args = parser.parse_args()

    if args.input:
        source_path = Path(args.input).expanduser().resolve()
        if not source_path.exists():
            print(json.dumps({"ok": False, "error": f"missing input: {source_path}"}, ensure_ascii=False), file=sys.stderr)
            return 2
        text = read_text(source_path)
    else:
        source_path = None
        text = read_clipboard()

    meta, body = split_frontmatter(text)
    title = infer_title(meta, body, "YouTube clip")
    classification = classify(meta, title, body)
    folder = bucket_to_folder(classification.bucket)
    filename = f"{DATE_STR} - {slugify(title)}.md"
    output_path = unique_path(folder / filename)

    result = {
        "ok": True,
        "source": str(source_path) if source_path else "clipboard",
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
