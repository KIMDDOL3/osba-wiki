#!/usr/bin/env python3
"""Audio/transcript -> Obsidian meeting minutes automation.

This script is designed for the user's Obsidian vault workflow:
- take a recorded meeting audio file or an existing transcript
- optionally transcribe audio with local Whisper if installed
- create a raw transcript note under raw/transcripts/
- create a polished meeting-minutes note under queries/
- keep the output Obsidian-friendly and linkable

It intentionally has a zero-dependency fallback path so the script can still
create useful Markdown even when transcription libraries are unavailable.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Iterable

DEFAULT_VAULT = Path(
    os.environ.get("OBSIDIAN_VAULT_PATH") or os.environ.get("WIKI_PATH") or str(Path.home() / "Documents/wiki")
 ).expanduser()
KST = dt.timezone(dt.timedelta(hours=9), name="KST")

NON_FILENAME = re.compile(r'[\\/:*?"<>|]+')
SPACE_RE = re.compile(r"\s+")
SPEAKER_RE = re.compile(r"^(?P<speaker>[A-Za-z가-힣0-9 _.-]{1,40})\s*[:：]\s*(?P<text>.+)$")
TIMESTAMP_RE = re.compile(r"^\s*(?:\[?\d{1,2}:\d{2}(?::\d{2})?(?:\s*[–—-]\s*\d{1,2}:\d{2}(?::\d{2})?)?\]?\s*)")
ACTION_RE = re.compile(r"(?:\b(?:action|todo|follow up|next step|assign|owner|담당|할 일|액션|조치)\b|해주세요|부탁|확인)", re.I)
DECISION_RE = re.compile(r"(?:\b(?:decid(?:e|ed|es)|approve(?:d|s)?|confirm(?:ed|s)?|agree(?:d|s)?|결정|확정|합의|승인|채택)\b)", re.I)
ISSUE_RE = re.compile(r"(?:\b(?:issue|risk|blocker|pending|open item|question|보류|미정|문제|리스크|확인 필요)\b)", re.I)


def now_kst() -> dt.datetime:
    return dt.datetime.now(KST)


def slugify(text: str, limit: int = 80) -> str:
    text = text.strip()
    text = NON_FILENAME.sub("-", text)
    text = SPACE_RE.sub(" ", text).strip(" ._-\t")
    return (text[:limit] or "meeting").strip()


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_text(path: Path, content: str) -> None:
    ensure_dir(path.parent)
    path.write_text(content, encoding="utf-8")


def copy_audio(source: Path, dest_dir: Path, prefix: str) -> Path:
    ensure_dir(dest_dir)
    dest = dest_dir / f"{prefix}{source.name}"
    if source.resolve() != dest.resolve():
        shutil.copy2(source, dest)
    return dest


def strip_markdown_metadata(text: str) -> str:
    text = text.lstrip("\ufeff")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[0].strip() == "---":
        end = None
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                end = i
                break
        if end is not None:
            lines = lines[end + 1 :]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and re.match(r"^#+\s+", lines[0].strip()):
        lines.pop(0)
    while lines and not lines[0].strip():
        lines.pop(0)
    return "\n".join(lines)


def normalize_lines(text: str) -> list[str]:
    lines = []
    for raw in strip_markdown_metadata(text).split("\n"):
        line = raw.strip()
        if not line:
            continue
        line = TIMESTAMP_RE.sub("", line).strip()
        line = line.lstrip("-•* ").strip()
        if line:
            lines.append(line)
    return lines


def split_sentences(text: str) -> list[str]:
    chunks = re.split(r"(?<=[.!?。！？])\s+|\n+", text)
    return [c.strip() for c in chunks if c.strip()]


def collect_bullets(lines: Iterable[str], patterns: tuple[re.Pattern[str], ...], limit: int = 8) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        if len(out) >= limit:
            break
        if any(p.search(line) for p in patterns):
            cleaned = line.lstrip("-•* ")
            if cleaned not in seen:
                out.append(cleaned)
                seen.add(cleaned)
    return out


def summarize_transcript(transcript: str) -> dict[str, list[str] | str]:
    lines = normalize_lines(transcript)
    if not lines:
        return {
            "summary": "전사 내용이 비어 있습니다.",
            "purpose": [],
            "key_points": [],
            "decisions": [],
            "issues": [],
            "actions": [],
            "next_steps": [],
            "speaker_notes": [],
        }

    speaker_notes: list[str] = []
    for line in lines:
        match = SPEAKER_RE.match(line)
        if match:
            speaker = match.group("speaker").strip()
            text = match.group("text").strip()
            speaker_notes.append(f"{speaker}: {text}")
        else:
            speaker_notes.append(line)
        if len(speaker_notes) >= 12:
            break

    actions = collect_bullets(lines, (ACTION_RE,), limit=8)
    decisions = collect_bullets(lines, (DECISION_RE,), limit=8)
    issues = collect_bullets(lines, (ISSUE_RE,), limit=8)

    next_steps: list[str] = []
    for line in lines:
        if re.search(r"(?:next|다음|후속|추후|일정|회의 때|follow up)", line, re.I):
            cleaned = line.lstrip("-•* ")
            if cleaned not in next_steps:
                next_steps.append(cleaned)
        if len(next_steps) >= 6:
            break

    purpose: list[str] = []
    for line in lines[:20]:
        if re.search(r"(?:purpose|goal|agenda|목적|안건|오늘은|회의 목적|논의 목적)", line, re.I):
            purpose.append(line)
    purpose = purpose[:4]

    cleaned_text = strip_markdown_metadata(transcript)
    top_sentences = split_sentences(cleaned_text.replace("\n", " "))[:4]
    summary = top_sentences[0] if top_sentences else lines[0]
    summary = summary.lstrip("-•* ").strip()
    summary = TIMESTAMP_RE.sub("", summary).strip()
    if len(summary) > 140:
        summary = summary[:137].rstrip() + "..."

    key_points: list[str] = []
    for line in lines:
        if line not in actions and line not in decisions and line not in issues:
            if len(line) > 18:
                key_points.append(line)
        if len(key_points) >= 10:
            break

    return {
        "summary": summary,
        "purpose": purpose,
        "key_points": key_points[:8],
        "decisions": decisions,
        "issues": issues,
        "actions": actions,
        "next_steps": next_steps,
        "speaker_notes": speaker_notes,
    }


def try_transcribe_with_faster_whisper(audio_path: Path, model_name: str, language: str | None) -> str | None:
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except Exception:
        return None

    device = os.environ.get("WHISPER_DEVICE", "auto")
    compute_type = os.environ.get("WHISPER_COMPUTE_TYPE", "int8")
    try:
        model = WhisperModel(model_name, device=device, compute_type=compute_type)
        segments, _info = model.transcribe(str(audio_path), language=language, vad_filter=True)
        return "\n".join(seg.text.strip() for seg in segments if seg.text.strip())
    except Exception:
        return None


def try_transcribe_with_cli(audio_path: Path, model_name: str, language: str | None) -> str | None:
    cli = shutil.which("whisper")
    if not cli:
        return None
    cmd = [cli, str(audio_path), "--model", model_name, "--output_format", "txt", "--output_dir", str(audio_path.parent)]
    if language:
        cmd += ["--language", language]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    txt_path = audio_path.parent / f"{audio_path.stem}.txt"
    if txt_path.exists():
        return txt_path.read_text(encoding="utf-8", errors="ignore")
    return None


def transcribe_audio(audio_path: Path, model_name: str, language: str | None) -> str:
    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    transcript = try_transcribe_with_faster_whisper(audio_path, model_name, language)
    if transcript:
        return transcript

    transcript = try_transcribe_with_cli(audio_path, model_name, language)
    if transcript:
        return transcript

    raise RuntimeError(
        "오디오 전사 도구를 찾지 못했습니다. faster-whisper 또는 whisper CLI를 설치하세요. "
        "예: pip install faster-whisper / pip install openai-whisper"
    )


def build_transcript_note(title: str, source_audio: Path | None, transcript: str, collected_at: dt.datetime) -> str:
    fm = [
        "---",
        f"title: {title} - 원문 전사",
        f"created: {collected_at.date().isoformat()}",
        f"updated: {collected_at.date().isoformat()}",
        "type: transcript",
        "tags: [meeting, transcript, audio, obsidian]",
    ]
    if source_audio is not None:
        fm.append(f'source_audio: "{source_audio.name}"')
    fm.append("---")
    body = [
        "",
        f"# {title} - 원문 전사",
        "",
        "```text",
        transcript.strip(),
        "```",
        "",
    ]
    return "\n".join(fm + body)


def build_minutes_note(title: str, source_audio: Path | None, source_transcript: Path | None, transcript: str, collected_at: dt.datetime) -> str:
    summary_raw = summarize_transcript(transcript)
    date_str = collected_at.date().isoformat()

    def as_list(value: list[str] | str) -> list[str]:
        return value if isinstance(value, list) else [value]

    def bullet_block(items: list[str], empty_text: str) -> str:
        if items:
            return "\n".join(f"- {item}" for item in items)
        return f"- {empty_text}"

    fm = [
        "---",
        f"title: {title}",
        f"created: {date_str}",
        f"updated: {date_str}",
        "type: meeting",
        "tags: [meeting, minutes, obsidian, automation]",
    ]
    if source_audio is not None:
        fm.append(f'source_audio: "{source_audio.name}"')
    if source_transcript is not None:
        fm.append(f'source_transcript: "{source_transcript.name}"')
    fm.append("---")

    lines = [
        "",
        f"# {title}",
        "",
        "## 한 줄 요약",
        f"- {summary_raw['summary']}",
        "",
        "## 회의 목적",
        bullet_block(as_list(summary_raw["purpose"]), "목적을 전사본에서 자동으로 추출하지 못했습니다. 수동 보강하세요."),
        "",
        "## 핵심 논의",
        bullet_block(as_list(summary_raw["key_points"]), "핵심 논의 항목을 충분히 추출하지 못했습니다."),
        "",
        "## 결정사항",
        bullet_block(as_list(summary_raw["decisions"]), "결정사항을 충분히 추출하지 못했습니다."),
        "",
        "## 보류 이슈",
        bullet_block(as_list(summary_raw["issues"]), "보류 이슈를 충분히 추출하지 못했습니다."),
        "",
        "## 담당자별 액션 아이템",
        bullet_block(as_list(summary_raw["actions"]), "액션 아이템을 충분히 추출하지 못했습니다."),
        "",
        "## 다음 회의 때 확인할 점",
        bullet_block(as_list(summary_raw["next_steps"]), "다음 회의 확인 포인트를 충분히 추출하지 못했습니다."),
        "",
        "## 발화 메모",
        bullet_block(as_list(summary_raw["speaker_notes"]), "발화 메모가 없습니다."),
        "",
        "## 원문 전사",
        "```text",
        transcript.strip(),
        "```",
        "",
        "## 연결 노트",
        "- [[LLM Hub]]",
        "- [[LLM Wiki Home]]",
        "- [[Obsidian Gemini 활용 워크플로]]",
        "- [[gemini-scribe/Prompts/Meeting Note]]",
        "",
    ]
    return "\n".join(fm + lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Turn a recorded meeting audio file or transcript into Obsidian meeting minutes."
    )
    parser.add_argument("--audio", type=Path, help="Path to the audio file (m4a/mp3/wav/etc.)")
    parser.add_argument("--transcript", type=Path, help="Path to an existing transcript text file")
    parser.add_argument("--title", help="Meeting title to use for the note")
    parser.add_argument("--vault", type=Path, default=DEFAULT_VAULT, help="Obsidian vault path")
    parser.add_argument("--output-dir", default="queries/회의록 자동화", help="Folder for finished minutes notes")
    parser.add_argument("--raw-dir", default="raw/transcripts", help="Folder for saved transcript notes")
    parser.add_argument("--archive-audio-dir", default="raw/audio", help="Folder where copied audio is stored")
    parser.add_argument("--model", default="base", help="Whisper model name when transcription is needed")
    parser.add_argument("--language", default="ko", help="Language hint for transcription (use 'auto' for none)")
    parser.add_argument("--prefix-date", action="store_true", help="Prefix output filenames with the current date")
    parser.add_argument("--write-transcript", action="store_true", help="Write the raw transcript note as well")
    parser.add_argument("--dry-run", action="store_true", help="Print output paths but do not write files")
    args = parser.parse_args()

    if not args.audio and not args.transcript:
        parser.error("--audio or --transcript is required")

    vault = args.vault.expanduser().resolve()
    if not vault.exists():
        raise SystemExit(f"Vault not found: {vault}")

    collected_at = now_kst()
    title = args.title
    source_audio: Path | None = None
    source_transcript: Path | None = None

    if args.audio:
        source_audio = args.audio.expanduser().resolve()
        if not title:
            title = source_audio.stem
    elif args.transcript:
        if not title:
            title = args.transcript.stem

    if not title:
        title = "회의록"

    title = title.strip()
    if source_audio is not None:
        audio_copy = copy_audio(source_audio, vault / args.archive_audio_dir, f"{collected_at.date().isoformat()} - ")
    else:
        audio_copy = None

    if args.transcript:
        assert args.transcript is not None
        source_transcript = args.transcript.expanduser().resolve()
        transcript = read_text(source_transcript)
    else:
        assert source_audio is not None
        transcript = transcribe_audio(source_audio, args.model, None if args.language == "auto" else args.language)

    minutes_note = build_minutes_note(title, audio_copy, source_transcript, transcript, collected_at)
    transcript_note = build_transcript_note(title, audio_copy, transcript, collected_at)

    if args.prefix_date:
        prefix = f"{collected_at.date().isoformat()} - "
    else:
        prefix = ""

    minutes_filename = f"{prefix}{slugify(title)}.md"
    transcript_filename = f"{prefix}{slugify(title)} - transcript.md"

    minutes_path = vault / args.output_dir / minutes_filename
    transcript_path = vault / args.raw_dir / transcript_filename

    if not args.dry_run:
        write_text(minutes_path, minutes_note)
        if args.write_transcript or not args.transcript:
            write_text(transcript_path, transcript_note)

    print(f"minutes: {minutes_path}")
    if args.write_transcript or not args.transcript:
        print(f"transcript: {transcript_path}")
    if audio_copy is not None:
        print(f"audio_copy: {audio_copy}")
    if args.dry_run:
        print("dry_run: true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
