#!/usr/bin/env python3
"""RSS -> Gemini Wiki Note automation.

Reads one or more RSS/Atom feeds and writes one Markdown note per item using a
Gemini-style wiki-note structure.

Design goals:
- stdlib-only so it works anywhere on the user's machine
- optional Gemini API / OpenAI API enhancement if keys are available
- safe fallback that still produces a useful note without any LLM key
- preserves raw RSS snapshots under raw/rss/
- uses Obsidian-friendly Markdown and wiki links
"""
from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import html
import json
import os
import re
import subprocess
import sys
import time
import textwrap
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

VAULT = Path(os.environ.get("OBSIDIAN_VAULT_PATH") or os.environ.get("WIKI_PATH") or str(Path.home() / "Documents/wiki")).expanduser()
DEFAULT_CONFIG = VAULT / "configs" / "rss_gemini_wiki.json"
DEFAULT_TEMPLATE = VAULT / "templates" / "RSS Gemini Wiki Note Template.md"
DEFAULT_RAW_DIR = VAULT / "raw" / "rss"
DEFAULT_OUT_DIR = VAULT / "queries" / "RSS 기사 큐레이션"
TIMEOUT = 30
USER_AGENT = "Mozilla/5.0 Hermes-RSS-Gemini-Wiki/1.0"
KST = dt.timezone(dt.timedelta(hours=9), name="KST")

TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")
NON_FILENAME = re.compile(r"[\\/:*?\"<>|]+")

_TRANSLATION_CACHE: dict[str, str] = {}


def now_kst() -> dt.datetime:
    return dt.datetime.now(KST)


def strip_html(value: str | None) -> str:
    if not value:
        return ""
    value = html.unescape(value)
    value = TAG_RE.sub(" ", value)
    value = SPACE_RE.sub(" ", value)
    return value.strip()


def parse_date(value: str | None) -> str:
    if not value:
        return ""
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(KST).date().isoformat()
    except Exception:
        cleaned = strip_html(value)
        if not cleaned:
            return ""
        return cleaned[:40]


def fetch_url(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read(4_000_000)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def canonical_url(url: str) -> str:
    try:
        p = urllib.parse.urlsplit(url)
        qs = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
        qs = [
            (k, v)
            for k, v in qs
            if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}
        ]
        return urllib.parse.urlunsplit(
            (p.scheme, p.netloc.lower(), p.path.rstrip("/"), urllib.parse.urlencode(qs), "")
        )
    except Exception:
        return url.strip()


def safe_filename(text: str, limit: int = 120) -> str:
    text = NON_FILENAME.sub("-", text)
    text = SPACE_RE.sub(" ", text).strip().strip(".")
    return text[:limit] or "note"


def first_text(elem: ET.Element, names: list[str]) -> str:
    for name in names:
        found = elem.find(name)
        if found is not None and found.text:
            return found.text
        for child in list(elem):
            tag = child.tag.split("}")[-1].lower()
            if tag == name.split(":")[-1].lower() and child.text:
                return child.text
    return ""


def parse_feed_xml(data: bytes, feed: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        text = data.decode("utf-8", "ignore")
        text = re.sub(r"&(?!amp;|lt;|gt;|quot;|apos;|#\d+;)", "&amp;", text)
        root = ET.fromstring(text.encode("utf-8"))

    root_name = root.tag.split("}")[-1].lower()
    if root_name == "rss" or root.find("channel") is not None:
        channel = root.find("channel") or root
        entries = channel.findall("item")
    else:
        entries = [e for e in root.iter() if e.tag.split("}")[-1].lower() in {"entry", "item"}]

    items: list[dict[str, Any]] = []
    for e in entries:
        title = strip_html(first_text(e, ["title"]))
        link = first_text(e, ["link"])
        if not link:
            for child in list(e):
                if child.tag.split("}")[-1].lower() == "link":
                    link = child.attrib.get("href", "") or (child.text or "")
                    if link:
                        break
        guid = first_text(e, ["guid", "id"])
        summary = strip_html(first_text(e, ["description", "summary", "content", "content:encoded"]))
        published = parse_date(first_text(e, ["pubDate", "published", "updated", "dc:date"]))
        if not title and not link:
            continue
        url = link.strip() or guid.strip() or title
        items.append(
            {
                "title": title or "(제목 없음)",
                "url": url,
                "summary": summary[:1500],
                "published": published,
                "source": feed.get("name", "RSS"),
                "feed_url": feed.get("url", ""),
                "category": feed.get("category", ""),
                "trust": feed.get("trust", ""),
                "output_subdir": feed.get("output_subdir", feed.get("name", "RSS")),
                "collected_at": now_kst().isoformat(timespec="seconds"),
            }
        )
    return items


def translate_en_ko(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    cached = _TRANSLATION_CACHE.get(text)
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
    _TRANSLATION_CACHE[text] = translated
    return translated


def detect_key_topics(text: str) -> list[str]:
    lowered = text.lower()
    mapping = [
        ("agent", "[[AI Agent]]"),
        ("agents", "[[AI Agent]]"),
        ("agentic", "[[AI Agent]]"),
        ("workflow", "[[AI Agent]]"),
        ("rag", "[[RAG]]"),
        ("graph", "[[지식그래프]]"),
        ("governance", "[[대학 행정 AX]]"),
        ("policy", "[[대학 행정 AX]]"),
        ("security", "[[생성형 AI]]"),
        ("privacy", "[[생성형 AI]]"),
        ("enterprise", "[[대학 행정 AX]]"),
        ("google", "[[생성형 AI]]"),
        ("openai", "[[생성형 AI]]"),
        ("anthropic", "[[생성형 AI]]"),
        ("meta", "[[생성형 AI]]"),
        ("llm", "[[생성형 AI]]"),
        ("model", "[[생성형 AI]]"),
        ("automation", "[[AI Agent]]"),
        ("x.com", "[[AI 뉴스]]"),
        ("twitter", "[[AI 뉴스]]"),
        ("threads", "[[AI 뉴스]]"),
    ]
    picked = []
    for needle, link in mapping:
        if needle in lowered:
            picked.append(link)
    # Deduplicate while preserving order
    out = []
    seen = set()
    for link in picked:
        if link not in seen:
            out.append(link)
            seen.add(link)
    return out[:5] or ["[[AI 뉴스]]", "[[생성형 AI]]"]


def build_prompt(item: dict[str, Any], template_text: str) -> str:
    return textwrap.dedent(
        f"""
        아래 RSS 기사 1개를 Obsidian 위키 노트로 정리해줘.

        작성 스타일:
        - 한국어로 작성
        - 읽기 쉬운 문장으로 짧고 선명하게 쓰기
        - 사실 / 해석 / 시사점을 분리
        - 원문 URL은 유지
        - 위키링크를 적극적으로 사용
        - 대학 행정 AX, AI Agent, RAG, 지식그래프 관점의 연결을 우선 검토
        - 너무 일반적인 문장은 줄이고 구체적 의미를 설명
        - 가능하면 제목을 더 자연스럽고 짧게 다듬기

        출력 형식:
        - 반드시 JSON만 반환
        - 코드블록 금지
        - 키는 아래 스키마만 사용

        JSON 스키마:
        {{
          "title_ko": "...",
          "one_line": "...",
          "summary_bullets": ["...", "...", "..."],
          "why_important": ["...", "...", "..."],
          "obsidian_links": ["[[AI Agent]]", "[[RAG]]"],
          "questions": ["...", "...", "..."],
          "tags": ["rss", "gemini", "wiki"],
          "notes": "..."
        }}

        템플릿 참고:
        {template_text}

        입력 데이터:
        - 제목: {item['title']}
        - 출처: {item['source']}
        - 발행일: {item.get('published') or ''}
        - URL: {item['url']}
        - RSS 요약: {item.get('summary') or ''}
        - 수집일: {item.get('collected_at') or ''}
        """
    ).strip()


def call_gemini(prompt: str, model: str, retries: int = 3) -> str | None:
    key = (
        os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or os.environ.get("GOOGLE_GENERATIVE_AI_API_KEY")
        or os.environ.get("GEMINI_DESCRIBE_API_KEY")
        or os.environ.get("GEMINI_DESCRIBE_KEY")
    )
    if not key:
        return None
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2},
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    data_bytes = json.dumps(payload).encode("utf-8")

    for attempt in range(retries + 1):
        req = urllib.request.Request(
            url,
            data=data_bytes,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            candidates = data.get("candidates") or []
            if not candidates:
                return None
            parts = candidates[0].get("content", {}).get("parts", [])
            return "".join(part.get("text", "") for part in parts).strip() or None
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                retry_after = exc.headers.get("Retry-After")
                sleep_time = float(retry_after) if retry_after and retry_after.isdigit() else (1.5 * (2 ** attempt))
                time.sleep(sleep_time)
                continue
            return None
        except Exception:
            if attempt < retries:
                time.sleep(1.5 * (2 ** attempt))
                continue
            return None
    return None


def call_openai(prompt: str, model: str) -> str | None:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return None
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a Korean Obsidian wiki-note curator."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"].strip()
    except Exception:
        return None


def parse_llm_json(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except Exception:
            return None


def heuristic_analysis(item: dict[str, Any]) -> dict[str, Any]:
    title = item.get("title", "")
    summary = item.get("summary", "")
    title_ko = translate_en_ko(title)
    summary_ko = translate_en_ko(summary) if summary else "RSS 요약이 없습니다."
    links = detect_key_topics(f"{title} {summary} {item.get('category','')}")
    if "[[AI Agent]]" in links:
        questions = [
            "이 기사의 에이전트 기능은 실제 업무 자동화에 연결되는가?",
            "사람의 승인 없이도 안전하게 사용할 수 있는 범위는 어디까지인가?",
            "대학 행정 업무에 적용할 때 어떤 리스크가 있는가?",
        ]
    elif "[[RAG]]" in links:
        questions = [
            "이 기사에서 검색/검색증강이 필요한 지점은 무엇인가?",
            "내부 지식베이스와 연결하면 어떤 가치가 생기는가?",
            "근거성 확인이 필요한 정보는 어디인가?",
        ]
    else:
        questions = [
            "이 변화가 실제 업무 흐름을 얼마나 바꾸는가?",
            "정책·보안·거버넌스 이슈는 무엇인가?",
            "관련 개념 노트로 승격할 만한가?",
        ]
    return {
        "title_ko": title_ko,
        "one_line": f"{title_ko} 관련 RSS 기사입니다.",
        "summary_bullets": [
            summary_ko[:280] or "원문 RSS 요약이 짧아 추가 해석이 어렵습니다.",
            f"출처: [{item.get('source','RSS')}]({item.get('url','')})",
            f"발행일: {item.get('published') or '미상'}",
        ],
        "why_important": [
            "기사를 원문 링크와 함께 보관해 나중에 다시 검증할 수 있습니다.",
            "위키링크로 연결해 같은 주제의 노트와 맥락을 묶을 수 있습니다.",
            "기계적 아카이브를 넘어서 지식화가 가능합니다.",
        ],
        "obsidian_links": links,
        "questions": questions,
        "tags": ["rss", "gemini", "wiki", "obsidian"],
        "notes": "LLM 키가 없어서 RSS 제목/요약 기반으로 생성된 fallback 노트입니다.",
    }


def render_note(item: dict[str, Any], analysis: dict[str, Any], template_text: str) -> str:
    date_s = item.get("published") or now_kst().date().isoformat()
    source = item.get("source", "RSS")
    title = analysis.get("title_ko") or item.get("title", "RSS 기사")
    links = analysis.get("obsidian_links") or ["[[AI 뉴스]]"]
    tags = analysis.get("tags") or ["rss", "wiki"]
    bullets = analysis.get("summary_bullets") or []
    importance = analysis.get("why_important") or []
    questions = analysis.get("questions") or []
    one_line = analysis.get("one_line") or ""
    notes = analysis.get("notes") or ""

    body = [
        "---",
        f"title: {title}",
        f"created: {now_kst().date().isoformat()}",
        "type: query",
        f"tags: [{', '.join(tags)}]",
        f"source_url: {item.get('url', '')}",
        f"source_feed: {source}",
        f"published: {date_s}",
        "---",
        "",
        f"# {title}",
        "",
        f"> {one_line}",
        "",
        "## 1) 메타데이터",
        "",
        f"- 출처: [{source}]({item.get('url', '')})",
        f"- 발행일: {item.get('published') or '미상'}",
        f"- 수집일: {item.get('collected_at') or now_kst().isoformat(timespec='seconds')}",
        f"- RSS 원문 제목: {item.get('title', '')}",
        "",
        "## 2) 기사 핵심 요약",
    ]
    if bullets:
        for b in bullets:
            body.append(f"- {b}")
    else:
        body.append("- 요약이 비어 있습니다.")
    body += [
        "",
        "## 3) 왜 중요한가",
    ]
    if importance:
        for b in importance:
            body.append(f"- {b}")
    else:
        body.append("- 별도의 중요성 해석이 없습니다.")
    body += [
        "",
        "## 4) Obsidian 연결 후보",
    ]
    for link in links:
        body.append(f"- {link}")
    body += [
        "",
        "## 5) 후속 질문",
    ]
    if questions:
        for q in questions:
            body.append(f"- {q}")
    else:
        body.append("- 이 기사에서 다음으로 확인할 질문은 무엇인가?")
    body += [
        "",
        "## 6) RSS/템플릿 메모",
        "",
        "- 원문 링크는 항상 유지합니다.",
        "- 사실과 해석을 분리해 나중에 재검증할 수 있게 둡니다.",
        f"- 템플릿 기반 생성: `templates/RSS Gemini Wiki Note Template.md`",
    ]
    if notes:
        body += ["- 메모: " + notes]
    body.append("")
    return "\n".join(body)


def write_note(item: dict[str, Any], note_text: str, out_dir: Path, feed_subdir: str) -> Path:
    date_s = item.get("published") or now_kst().date().isoformat()
    title = item.get("title", "RSS 기사")
    fname = f"{date_s} {safe_filename(translate_en_ko(title)[:90])}.md"
    target_dir = out_dir / safe_filename(feed_subdir or item.get("source", "RSS"))
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / fname
    path.write_text(note_text, encoding="utf-8")
    return path.resolve()


def update_index_and_log(note_paths: list[Path], date_s: str) -> None:
    index = VAULT / "index.md"
    if index.exists():
        text = index.read_text(encoding="utf-8")
        link = "[[queries/RSS 기사 → Gemini Wiki Note 자동화|RSS 기사 → Gemini Wiki Note 자동화]]"
        if link not in text:
            insertion = (
                "- 🧭 [[queries/RSS 기사 → Gemini Wiki Note 자동화|RSS 기사 → Gemini Wiki Note 자동화]]: "
                "RSS 기사를 Gemini 위키 노트로 바꾸는 자동화 문서.\n"
            )
            marker = "- 🧭 [[queries/README|Queries Hub]]: `queries/` 폴더의 Dataview 자동 목차 허브.\n"
            if marker in text:
                text = text.replace(marker, insertion + marker, 1)
            else:
                text = text.rstrip() + "\n\n" + insertion
            index.write_text(text, encoding="utf-8")
    log = VAULT / "log.md"
    if log.exists() and note_paths:
        text = log.read_text(encoding="utf-8")
        rels = ", ".join(f"`{p.relative_to(VAULT)}`" for p in note_paths[:5])
        entry = (
            f"\n## [{date_s}] automation | RSS 기사 → Gemini Wiki Note\n"
            f"- RSS 기사를 Gemini 위키 노트로 변환: {rels} 생성/갱신.\n"
        )
        if note_paths[0].name not in text:
            log.write_text(text.rstrip() + entry, encoding="utf-8")


def git_backup(date_s: str) -> None:
    try:
        subprocess.run(
            [
                "git",
                "add",
                "configs/rss_gemini_wiki.json",
                "scripts/rss_gemini_wiki_note.py",
                "templates/RSS Gemini Wiki Note Template.md",
                "queries/RSS 기사 → Gemini Wiki Note 자동화.md",
                "queries/RSS 기사 큐레이션",
                "index.md",
                "log.md",
                "raw/rss",
            ],
            cwd=VAULT,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=VAULT)
        if diff.returncode != 0:
            subprocess.run(["git", "commit", "-m", f"automation: rss gemini wiki note {date_s}"], cwd=VAULT, check=False)
            subprocess.run(["git", "push"], cwd=VAULT, check=False)
    except Exception:
        pass


def process_feed(feed: dict[str, Any], config: dict[str, Any], args: argparse.Namespace, template_text: str) -> list[Path]:
    items, errors = [], []
    try:
        data = fetch_url(feed["url"])
        items = parse_feed_xml(data, feed)
    except Exception as e:
        errors.append({"feed": feed.get("name", "?"), "url": feed.get("url", ""), "error": str(e)[:240]})

    limit = args.limit or int(feed.get("limit") or config.get("pipeline", {}).get("max_items", 1) or 1)
    items = items[:limit]
    collected = now_kst().date().isoformat()
    raw_snapshot = {
        "date": collected,
        "feed": feed,
        "items": items,
        "errors": errors,
    }
    raw_dir = Path(args.raw_dir or config.get("raw_dir") or DEFAULT_RAW_DIR)
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_slug = safe_filename(feed.get("output_subdir") or feed.get("name") or "rss")
    save_json(raw_dir / f"{collected}-{raw_slug}-items.json", raw_snapshot)

    out_dir = Path(args.output_dir or config.get("output_dir") or DEFAULT_OUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)

    note_paths: list[Path] = []
    provider = (args.provider or config.get("provider", {}).get("kind") or "gemini").lower()
    gemini_model = args.gemini_model or config.get("provider", {}).get("gemini_model") or os.environ.get("GEMINI_MODEL") or "gemini-1.5-flash"
    openai_model = args.openai_model or config.get("provider", {}).get("openai_model") or os.environ.get("OPENAI_MODEL") or "gpt-4o-mini"

    for item in items:
        prompt = build_prompt(item, template_text)
        llm_text = None
        if not args.no_llm:
            if provider in {"auto", "gemini"}:
                llm_text = call_gemini(prompt, gemini_model)
            if not llm_text and provider in {"auto", "openai"}:
                llm_text = call_openai(prompt, openai_model)
        analysis = parse_llm_json(llm_text) or heuristic_analysis(item)
        note_text = render_note(item, analysis, template_text)
        path = write_note(item, note_text, out_dir, str(feed.get("output_subdir") or feed.get("name") or "RSS"))
        note_paths.append(path)
    return note_paths


def main() -> int:
    ap = argparse.ArgumentParser(description="Create Obsidian wiki notes from RSS articles.")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG), help="JSON config file path")
    ap.add_argument("--feed", default="", help="Single feed URL to process instead of config feeds")
    ap.add_argument("--source-name", default="", help="Human-readable name for --feed")
    ap.add_argument("--limit", type=int, default=1, help="Maximum items per feed")
    ap.add_argument("--output-dir", default="", help="Override output directory")
    ap.add_argument("--raw-dir", default="", help="Override raw JSON directory")
    ap.add_argument("--provider", default="gemini", choices=["gemini", "auto", "openai", "none"], help="LLM provider preference")
    ap.add_argument("--gemini-model", default="", help="Gemini model name")
    ap.add_argument("--openai-model", default="", help="OpenAI model name")
    ap.add_argument("--no-llm", action="store_true", help="Skip LLM calls and use fallback analysis")
    ap.add_argument("--no-git", action="store_true", help="Skip Git backup")
    args = ap.parse_args()

    config = load_json(Path(args.config), {}) if args.config else {}
    if not config and not args.feed:
        print(f"Config not found: {args.config}", file=sys.stderr)
        return 2

    template_text = DEFAULT_TEMPLATE.read_text(encoding="utf-8") if DEFAULT_TEMPLATE.exists() else ""
    note_paths: list[Path] = []
    date_s = now_kst().date().isoformat()

    if args.feed:
        feed = {
            "name": args.source_name or urllib.parse.urlsplit(args.feed).netloc or "RSS",
            "url": args.feed,
            "output_subdir": args.source_name or urllib.parse.urlsplit(args.feed).netloc or "RSS",
        }
        note_paths.extend(process_feed(feed, config or {}, args, template_text))
    else:
        feeds = config.get("feeds") or []
        if not feeds:
            print("No feeds configured", file=sys.stderr)
            return 2
        max_workers = min(6, max(1, len(feeds)))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(process_feed, feed, config, args, template_text) for feed in feeds]
            for fut in as_completed(futures):
                note_paths.extend(fut.result())

    if note_paths:
        update_index_and_log(note_paths, date_s)
        if not args.no_git and config.get("auto_git_backup", True):
            git_backup(date_s)

    digest = build_digest(note_paths, date_s)
    print(digest)
    send_discord_message(DISCORD_MAIN_CHANNEL_ID, digest)
    return 0


DISCORD_MAIN_CHANNEL_ID = os.environ.get("DISCORD_MAIN_CHANNEL_ID", "")  # your main bot channel


def _first_note_field(text: str, field_pattern: str) -> str:
    m = re.search(field_pattern, text, re.MULTILINE)
    return m.group(1).strip() if m else ""


def build_digest(note_paths: "list[Path]", date_s: str) -> str:
    """Human-readable digest of today's run, for Telegram/Discord delivery.

    Replaces the old raw-JSON stdout (which cron delivery forwarded verbatim
    to Telegram as an unreadable code block). Parses each generated note's
    title / first summary bullet / source_url instead of dumping file paths.
    """
    lines = [f"📰 RSS → Wiki 큐레이션 — {date_s}", f"총 {len(note_paths)}건 수집", ""]

    for p in note_paths:
        try:
            text = Path(p).read_text(encoding="utf-8")
        except Exception:
            continue
        title = _first_note_field(text, r"^title:\s*(.+)$") or Path(p).stem
        source_url = _first_note_field(text, r"^source_url:\s*(.+)$")
        source_feed = _first_note_field(text, r"^source_feed:\s*(.+)$")
        summary_match = re.search(r"기사 핵심 요약\s*\n-\s*(.+)", text)
        summary = summary_match.group(1).strip() if summary_match else ""
        if len(summary) > 160:
            summary = summary[:157] + "..."
        line = f"• {title}"
        if source_feed:
            line += f" ({source_feed})"
        lines.append(line)
        if summary:
            lines.append(f"  {summary}")
        if source_url:
            lines.append(f"  {source_url}")
        lines.append("")

    daily_curation = Path(f"queries/AI 뉴스 큐레이션/{date_s} AI 뉴스 큐레이션.md")
    if daily_curation.exists():
        try:
            curation_text = daily_curation.read_text(encoding="utf-8")
            body = re.sub(r"^---.*?---\s*", "", curation_text, flags=re.DOTALL)
            snippet = body.strip()[:600]
            lines.append(f"📌 {date_s} AI 뉴스 큐레이션 요약")
            lines.append(snippet)
            lines.append("")
        except Exception:
            pass

    return "\n".join(lines).strip()


def send_discord_message(channel_id: str, text: str) -> bool:
    """Post a plain message to a Discord channel via the bot REST API.

    Standalone HTTP call (no MCP/Claude Code dependency) so this keeps working
    unattended from cron. Best-effort: any failure is swallowed — Discord
    delivery is a nice-to-have, never a reason to fail the pipeline after
    notes have already been written.
    """
    env_path = Path.home() / ".claude" / "channels" / "discord" / ".env"
    token = None
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("DISCORD_BOT_TOKEN="):
                token = line.split("=", 1)[1].strip()
                break
    if not token:
        return False
    try:
        req = urllib.request.Request(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            data=json.dumps({"content": text[:1900]}).encode("utf-8"),
            headers={
                "Authorization": f"Bot {token}",
                "Content-Type": "application/json",
                "User-Agent": "DiscordBot (https://github.com/anthropics, 1.0)",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
