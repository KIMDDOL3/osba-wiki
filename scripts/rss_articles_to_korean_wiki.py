#!/usr/bin/env python3
"""Watch `RSS articles/` for new Markdown files and convert them to Korean wiki notes.

This is a polling watcher, designed to be run by cron every minute.
It is stdlib-only and keeps a small state file so already-processed source
notes are not converted again.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import sys
import textwrap
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

VAULT = Path(os.environ.get("OBSIDIAN_VAULT_PATH") or os.environ.get("WIKI_PATH") or str(Path.home() / "Documents/wiki")).expanduser()
SOURCE_DIR = VAULT / "RSS articles"
OUT_DIR = VAULT / "queries" / "RSS 기사 큐레이션"
STATE_PATH = VAULT / "raw" / "rss" / ".rss_articles_watcher_state.json"
TEMPLATE_PATH = VAULT / "templates" / "RSS Gemini Wiki Note Template.md"
USER_AGENT = "Mozilla/5.0 Hermes-RSS-Articles-Watcher/1.0"
KST = dt.timezone(dt.timedelta(hours=9), name="KST")
NON_FILENAME = re.compile(r'[\\/:*?"<>|]+')
SPACE_RE = re.compile(r"\s+")
TAG_RE = re.compile(r"<[^>]+>")

TRANSLATION_CACHE: dict[str, str] = {}


def now_kst() -> dt.datetime:
    return dt.datetime.now(KST)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def strip_html(value: str | None) -> str:
    if not value:
        return ""
    value = html.unescape(value)
    value = TAG_RE.sub(" ", value)
    value = SPACE_RE.sub(" ", value)
    return value.strip()


def canonical_url(url: str) -> str:
    try:
        p = urllib.parse.urlsplit(url)
        qs = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
        qs = [(k, v) for k, v in qs if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}]
        return urllib.parse.urlunsplit((p.scheme, p.netloc.lower(), p.path.rstrip("/"), urllib.parse.urlencode(qs), ""))
    except Exception:
        return url.strip()


def safe_filename(text: str, limit: int = 120) -> str:
    text = NON_FILENAME.sub("-", text)
    text = SPACE_RE.sub(" ", text).strip().strip(".")
    return text[:limit] or "note"


def parse_frontmatter(path: Path) -> tuple[dict[str, str], str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    if len(lines) < 3:
        return {}, text
    fm: dict[str, str] = {}
    i = 1
    while i < len(lines):
        line = lines[i]
        if line.strip() == "---":
            return fm, "\n".join(lines[i + 1 :])
        if ":" in line:
            key, value = line.split(":", 1)
            fm[key.strip()] = value.strip().strip('"').strip("'")
        i += 1
    return fm, text


def parse_article_file(path: Path) -> dict[str, Any]:
    fm, body = parse_frontmatter(path)
    title = fm.get("title") or path.stem
    link = fm.get("link") or fm.get("url") or ""
    source = fm.get("source") or fm.get("feedTitle") or "RSS"
    summary = strip_html(fm.get("summary") or "")
    published = fm.get("date") or fm.get("published") or ""
    author = fm.get("author") or ""

    body_lines = []
    for raw_line in body.splitlines():
        line = strip_html(raw_line)
        if not line:
            continue
        lowered = line.lower()
        if lowered.startswith(("welcome back", "have you thought", "subscribe", "source")):
            continue
        if line.startswith("#"):
            continue
        if line == "*":
            continue
        if re.fullmatch(r"\d{4}(?:년|-)?.*", line):
            continue
        body_lines.append(line)
    body_text = " ".join(body_lines).strip()
    snippet = body_text[:1500]
    source_key = canonical_url(link) if link else str(path.resolve())
    return {
        "path": path,
        "title": title,
        "link": link,
        "source": source,
        "summary": summary,
        "published": published,
        "author": author,
        "body_text": body_text,
        "snippet": snippet,
        "source_key": source_key,
    }


def translate_text(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    cached = TRANSLATION_CACHE.get(text)
    if cached is not None:
        return cached
    try:
        url = (
            "https://translate.googleapis.com/translate_a/single?client=gtx"
            "&sl=en&tl=ko&dt=t&q=" + urllib.parse.quote(text)
        )
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        raw = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
        data = json.loads(raw)
        parts = []
        for chunk in data[0]:
            if chunk and chunk[0]:
                parts.append(chunk[0])
        translated = "".join(parts).strip() or text
    except Exception:
        translated = text
    TRANSLATION_CACHE[text] = translated
    return translated


def detect_links(text: str) -> list[str]:
    lowered = text.lower()
    mapping = [
        ("urgency", "[[시간 관리]]"),
        ("priority", "[[업무 우선순위]]"),
        ("focus", "[[집중]]"),
        ("productivity", "[[생산성]]"),
        ("agent", "[[AI Agent]]"),
        ("rag", "[[RAG]]"),
        ("graph", "[[지식그래프]]"),
        ("policy", "[[대학 행정 AX]]"),
        ("governance", "[[대학 행정 AX]]"),
        ("llm", "[[생성형 AI]]"),
        ("ai", "[[생성형 AI]]"),
    ]
    out: list[str] = []
    seen = set()
    for needle, link in mapping:
        if needle in lowered and link not in seen:
            out.append(link)
            seen.add(link)
    return out[:5] or ["[[AI 뉴스]]", "[[생산성]]"]


def render_note(article: dict[str, Any]) -> tuple[str, str]:
    title_ko = translate_text(article["title"])
    summary_ko = translate_text(article["summary"]) if article["summary"] else ""
    snippet_ko = translate_text(article["snippet"][:700]) if article["snippet"] else ""
    links = detect_links(f"{article['title']} {article['summary']} {article['body_text'][:500]}")
    date_s = article["published"] or now_kst().date().isoformat()
    note_title = title_ko or article["title"]
    source = article["source"] or "RSS"
    source_url = article["link"]
    one_line = summary_ko or snippet_ko or f"{note_title} 관련 RSS 기사입니다."
    is_short = len(article["body_text"]) < 350 and len(article["summary"]) < 120

    lines = [
        "---",
        f"title: {note_title}",
        f"created: {now_kst().date().isoformat()}",
        "type: query",
        "tags: [rss, wiki, korean]",
        f"source_url: {source_url}",
        f"source_feed: {source}",
        f"original_title: {article['title']}",
        f"published: {date_s}",
        "---",
        "",
        f"# {note_title}",
        "",
        f"> {one_line}",
        "",
        "## 1) 한눈에 보는 결론",
        "",
        one_line,
        "",
        "## 2) 기사 핵심 요약",
    ]
    if summary_ko:
        lines.append(f"- {summary_ko}")
    if snippet_ko and snippet_ko != summary_ko:
        lines.append(f"- {snippet_ko}")
    lines += [
        f"- 원문 제목: {article['title']}",
        f"- 원문 링크: {source_url}",
        "",
    ]

    if is_short:
        lines += [
            "## 3) Obsidian 연결 후보",
        ]
        for link in links:
            lines.append(f"- {link}")
        lines += [
            "",
            "## 4) RSS/자동화 메모",
            "",
            "- 이 노트는 `RSS articles/`의 새 Markdown 파일을 감시해 만든 결과다.",
            "- 짧은 글이라면 `왜 중요한가`, 후속 질문, 파이만 기법은 생략할 수 있다.",
            "- 템플릿 기반 생성: `templates/RSS Gemini Wiki Note Template.md`",
            "",
        ]
    else:
        lines += [
            "## 3) 왜 중요한가",
            "",
            "- 원문 파일이 `RSS articles/`에 있어도 한국어 위키 노트로 재정리됩니다.",
            "- 같은 주제를 나중에 다시 찾기 쉽게 `queries/`에 저장합니다.",
            "- 원문 URL과 원문 제목을 함께 남겨 검증 가능성을 유지합니다.",
            "",
            "## 4) Obsidian 연결 후보",
        ]
        for link in links:
            lines.append(f"- {link}")
        lines += [
            "",
            "## 5) 후속 질문",
            "",
            "- 이 글의 핵심 메시지를 한 문장으로 말하면 무엇인가?",
            "- 이 내용이 실제 업무나 생활 습관에 어떤 영향을 주는가?",
            "- 다른 노트와 연결할 만한 개념은 무엇인가?",
            "",
            "## 6) RSS/자동화 메모",
            "",
            "- 이 노트는 `RSS articles/`의 새 Markdown 파일을 감시해 만든 결과다.",
            "- 번역은 우선 Google Translate 엔드포인트를 사용하고, 실패하면 원문을 유지한다.",
            "- 템플릿 기반 생성: `templates/RSS Gemini Wiki Note Template.md`",
            "",
            "## 파이만 기법",
            "",
            "- 이 글은 아주 쉽게 말하면 `RSS articles/`에 저장된 영어 글을 한국어 위키 노트로 바꾸는 자동 변환이다.",
            "- 어려운 말 없이 말하면, 새 글이 들어오면 사람이 손으로 옮기지 않아도 한국어 요약본이 생긴다.",
            "- 내가 이해한 한 줄: **새 RSS 원문 파일이 생기면 자동으로 한국어 위키 노트가 만들어진다.**",
            "",
        ]
    return note_title, "\n".join(lines)


def note_path_for(article: dict[str, Any], title_ko: str) -> Path:
    source = safe_filename(article["source"] or "RSS")
    date_s = article["published"] or now_kst().date().isoformat()
    folder = OUT_DIR / source
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{date_s} {safe_filename(title_ko)[:90]}.md"


def process_new_articles() -> list[Path]:
    state = load_json(STATE_PATH, {"processed": {}, "last_run": ""})
    processed: dict[str, str] = state.get("processed", {}) or {}
    created: list[Path] = []
    if not SOURCE_DIR.exists():
        return []

    candidates = sorted(SOURCE_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime)
    for path in candidates:
        key = str(path.resolve())
        mtime = str(int(path.stat().st_mtime))
        if processed.get(key) == mtime:
            continue
        article = parse_article_file(path)
        title_ko, note_text = render_note(article)
        out_path = note_path_for(article, title_ko)
        out_path.write_text(note_text, encoding="utf-8")
        processed[key] = mtime
        created.append(out_path.resolve())

    state = {
        "last_run": now_kst().isoformat(timespec="seconds"),
        "processed": processed,
    }
    save_json(STATE_PATH, state)
    return created


def main() -> int:
    created = process_new_articles()
    if created:
        print(json.dumps({"ok": True, "count": len(created), "notes": [str(p) for p in created]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
