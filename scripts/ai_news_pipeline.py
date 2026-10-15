#!/usr/bin/env python3
"""RSS 기반 AI 뉴스 한국어 요약·큐레이션 파이프라인.

- RSS/Atom 피드를 수집한다.
- 중복 URL을 제거하고 관심 키워드로 점수를 매긴다.
- GEMINI_API_KEY(또는 GOOGLE_API_KEY)가 있으면 Gemini API로 한국어 브리핑을 생성한다.
- Gemini API가 없거나 실패하면 추출식 한국어 큐레이션 문서를 생성한다.
- 결과를 Obsidian Markdown으로 저장한다.

민감정보 주의: API 키는 읽기만 하며 출력/저장하지 않는다.
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
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

VAULT = Path(os.environ.get("OBSIDIAN_VAULT_PATH") or os.environ.get("WIKI_PATH") or str(Path.home() / "Documents/wiki")).expanduser()
CONFIG_PATH = VAULT / "configs" / "ai_news_feeds.json"
RAW_DIR = VAULT / "raw" / "rss"
OUT_DIR = VAULT / "queries" / "AI 뉴스 큐레이션"
STATE_PATH = RAW_DIR / "ai-news-state.json"
LATEST_JSON = RAW_DIR / "ai-news-latest.json"
TIMEOUT = 25
USER_AGENT = "Mozilla/5.0 Hermes-Obsidian-AI-News-Pipeline/1.0"

TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")


def now_kst() -> dt.datetime:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=9), name="KST"))


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
        return parsed.astimezone(dt.timezone(dt.timedelta(hours=9))).date().isoformat()
    except Exception:
        return strip_html(value)[:40]


def fetch_url(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read(3_000_000)


def first_text(elem: ET.Element, names: list[str]) -> str:
    for name in names:
        found = elem.find(name)
        if found is not None and found.text:
            return found.text
        # namespace-insensitive fallback
        for child in list(elem):
            if child.tag.split("}")[-1].lower() == name.split(":")[-1].lower() and child.text:
                return child.text
    return ""


def parse_feed_xml(data: bytes, feed: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        text = data.decode("utf-8", "ignore")
        text = re.sub(r"&(?!amp;|lt;|gt;|quot;|apos;|#\d+;)", "&amp;", text)
        root = ET.fromstring(text.encode())

    root_name = root.tag.split("}")[-1].lower()
    entries: list[ET.Element] = []
    if root_name == "rss" or root.find("channel") is not None:
        channel = root.find("channel") or root
        entries = channel.findall("item")
    else:
        entries = [e for e in root.iter() if e.tag.split("}")[-1].lower() in {"entry", "item"}]

    items = []
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
        items.append({
            "title": title or "(제목 없음)",
            "url": url,
            "summary": summary[:900],
            "published": published,
            "source": feed["name"],
            "feed_url": feed["url"],
            "category": feed.get("category", ""),
            "trust": feed.get("trust", ""),
            "collected_at": now_kst().isoformat(timespec="seconds"),
        })
    return items


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
        qs = [(k, v) for k, v in qs if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}]
        return urllib.parse.urlunsplit((p.scheme, p.netloc.lower(), p.path.rstrip("/"), urllib.parse.urlencode(qs), ""))
    except Exception:
        return url.strip()


def score_item(item: dict[str, Any], config: dict[str, Any]) -> tuple[int, list[str]]:
    text = f"{item.get('title','')} {item.get('summary','')} {item.get('category','')}".lower()
    labels = []
    score = 0
    high = config.get("keywords", {}).get("high_priority", [])
    ko = config.get("keywords", {}).get("korean_labels", {})
    for kw in high:
        if kw.lower() in text:
            score += 3 if kw in {"agent", "rag", "graph", "policy", "university", "education"} else 1
            labels.append(ko.get(kw, kw))
    source_cat = item.get("category")
    if source_cat in {"frontier-lab", "research", "academic", "paper"}:
        score += 2
    trust = item.get("trust", "")
    if trust == "high":
        score += 2
    elif trust == "medium-high":
        score += 1
    return score, sorted(set(labels))[:6]


_TRANSLATION_CACHE: dict[str, str] = {}


def translate_en_ko(text: str) -> str:
    """Best-effort English→Korean translation for reader-friendly notes.

    Uses the public Google Translate web endpoint without an API key. If the
    request fails for any reason, the original text is returned unchanged.
    """
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


def call_gemini(prompt: str, model: str, retries: int = 3) -> str | None:
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
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


def _fetch_one_feed(feed: dict[str, Any]) -> tuple[dict[str, Any], bytes | None, str | None]:
    """Network fetch only — no parsing/scoring here, so results stay order-independent to gather."""
    try:
        return feed, fetch_url(feed["url"]), None
    except Exception as e:
        return feed, None, str(e)[:240]


def collect(config: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    errors = []
    seen = set()
    items = []
    feeds = config["feeds"]

    # Feeds are fetched concurrently (I/O-bound network calls) but then walked back
    # in the original feed-list order below, so dedup/scoring/output stay byte-identical
    # to the old sequential version — only wall-clock time changes.
    fetched: dict[int, tuple[bytes | None, str | None]] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(feeds)) or 1) as pool:
        futures = {pool.submit(_fetch_one_feed, feed): idx for idx, feed in enumerate(feeds)}
        for future in futures:
            idx = futures[future]
            _, data, err = future.result()
            fetched[idx] = (data, err)

    for idx, feed in enumerate(feeds):
        data, err = fetched[idx]
        if err is not None:
            errors.append({"feed": feed.get("name", "?"), "url": feed.get("url", ""), "error": err})
            continue
        try:
            parsed = parse_feed_xml(data, feed)
            for item in parsed:
                key = canonical_url(item.get("url", ""))
                if not key or key in seen:
                    continue
                seen.add(key)
                score, labels = score_item(item, config)
                item["canonical_url"] = key
                item["score"] = score
                item["labels"] = labels
                items.append(item)
        except Exception as e:
            errors.append({"feed": feed.get("name", "?"), "url": feed.get("url", ""), "error": str(e)[:240]})
    items.sort(key=lambda x: (x.get("score", 0), x.get("published", "")), reverse=True)
    return items, errors


def filter_recent_items(items: list[dict[str, Any]], lookback_days: int, today: dt.date) -> list[dict[str, Any]]:
    """Keep items published within the configured lookback window.

    Items with missing/unparseable dates are retained because some feeds omit
    dates even for fresh posts. Dated items older than the window are removed so
    a high keyword score does not surface stale evergreen posts in a "latest"
    briefing.
    """
    if lookback_days <= 0:
        return items
    cutoff = today - dt.timedelta(days=lookback_days)
    recent: list[dict[str, Any]] = []
    for item in items:
        published = str(item.get("published") or "").strip()
        if not published:
            recent.append(item)
            continue
        try:
            pub_date = dt.date.fromisoformat(published[:10])
        except ValueError:
            recent.append(item)
            continue
        if cutoff <= pub_date <= today:
            recent.append(item)
    return recent


def cluster_items_by_label(items: list[dict[str, Any]], max_clusters: int = 6) -> dict[str, list[dict[str, Any]]]:
    """Group curated items by their strongest score_item() label.

    Each item keeps only its first (highest-priority) label as its cluster key;
    unlabeled items fall into "기타". Clusters are capped at max_clusters by
    combined item score so a long tail of one-item topics doesn't turn into a
    long tail of near-empty Gemini calls — the smallest clusters beyond the cap
    are folded into "기타" instead of dropped.
    """
    buckets: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        labels = item.get("labels") or []
        key = labels[0] if labels else "기타"
        buckets.setdefault(key, []).append(item)

    if len(buckets) <= max_clusters:
        return buckets

    ranked = sorted(buckets.items(), key=lambda kv: sum(i.get("score", 0) for i in kv[1]), reverse=True)
    kept = dict(ranked[: max_clusters - 1])
    overflow: list[dict[str, Any]] = []
    for _, group in ranked[max_clusters - 1 :]:
        overflow.extend(group)
    if overflow:
        kept.setdefault("기타", []).extend(overflow)
    return kept


def make_cluster_prompt(label: str, items: list[dict[str, Any]], date_s: str) -> str:
    compact = [
        {
            "title": item["title"],
            "source": item["source"],
            "published": item.get("published", ""),
            "url": item["url"],
            "summary": item.get("summary", ""),
        }
        for item in items
    ]
    return f"""다음은 {date_s} 기준 "{label}" 주제로 묶인 AI 뉴스 후보입니다. 한국어로 짧은 섹션 하나를 작성하세요.

요구사항:
- 2~4개 bullet로, 이 주제에서 오늘 실제로 있었던 사실만 씁니다 (과장 금지, 요약/제목에서 확인 가능한 범위만).
- 각 bullet 끝에 원문 출처를 Markdown 링크로 답니다.
- 제목이나 머리말 없이 bullet 목록만 반환하세요 (섹션 제목은 호출하는 쪽에서 붙입니다).
- 코드블록으로 감싸지 마세요.

후보 (JSON):
{json.dumps(compact, ensure_ascii=False, indent=2)}
"""


def summarize_clusters_parallel(
    clusters: dict[str, list[dict[str, Any]]], model: str, date_s: str, max_workers: int = 4
) -> dict[str, str | None]:
    """Summarize each topic cluster with its own Gemini call, in parallel.

    This is the parallel replacement for one giant single-prompt summary:
    faster wall-clock time (concurrent calls instead of one large sequential
    one) and each topic gets focused attention instead of being diluted in a
    single mega-prompt. A cluster that fails just comes back None — the
    caller falls back to a deterministic bullet list for that section rather
    than aborting the whole run.
    """

    def _one(label: str, items: list[dict[str, Any]]) -> tuple[str, str | None]:
        prompt = make_cluster_prompt(label, items, date_s)
        return label, call_gemini(prompt, model)

    results: dict[str, str | None] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_one, label, items): label for label, items in clusters.items()}
        for future in futures:
            label, text = future.result()
            results[label] = text
    return results


def fallback_cluster_bullets(items: list[dict[str, Any]]) -> str:
    lines = []
    for item in items[:4]:
        title = item.get("title", "").strip() or "(제목 없음)"
        url = item.get("url", "")
        lines.append(f"- {title} — [{item.get('source','')}]({url})" if url else f"- {title}")
    return "\n".join(lines)


def make_overview_prompt(cluster_sections: dict[str, str], date_s: str) -> str:
    joined = "\n\n".join(f"### {label}\n{text}" for label, text in cluster_sections.items())
    return f"""아래는 {date_s} AI 뉴스를 주제별로 나눠 이미 요약한 내용입니다. 이걸 다시 읽고, 전체를 아우르는 "한눈에 보는 결론"만 3~5개 bullet로 뽑으세요.

요구사항:
- 이미 있는 세부 내용을 반복하지 말고, 오늘 전체에서 가장 중요한 흐름/시사점만 압축합니다.
- 대학 행정 AX, AI Agent, RAG/GraphRAG, 생성형 AI 정책, 공공부문/교육 적용 관점을 우선합니다.
- bullet 목록만 반환하세요 (제목, 코드블록 없이).

주제별 요약:
{joined}
"""


def assemble_clustered_markdown(
    overview: str | None, cluster_sections: dict[str, str], date_s: str
) -> str:
    parts = [f"# {date_s} AI 뉴스 큐레이션\n"]
    parts.append("## 한눈에 보는 결론\n")
    parts.append((overview or "- (오늘은 요약을 만들지 못했습니다 — 아래 주제별 섹션을 참고하세요.)").strip() + "\n")
    for label, text in cluster_sections.items():
        parts.append(f"\n## {label}\n")
        parts.append(text.strip() + "\n")
    return "\n".join(parts)


def send_discord_message(channel_id: str, text: str) -> bool:
    """Post a plain message to a Discord channel via the bot REST API.

    Standalone HTTP call (no MCP/Claude Code dependency) so this script keeps
    working unattended from cron/launchd. Best-effort: any failure (missing
    token, network error, channel not allowlisted) is swallowed — publishing
    to Discord is a nice-to-have, never a reason to fail the whole pipeline
    run after the note has already been written.
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


def make_prompt(items: list[dict[str, Any]], errors: list[dict[str, str]], config: dict[str, Any], date_s: str) -> str:
    compact = []
    for i, item in enumerate(items, 1):
        compact.append({
            "rank": i,
            "title": item["title"],
            "source": item["source"],
            "published": item.get("published", ""),
            "url": item["url"],
            "summary": item.get("summary", ""),
            "score": item.get("score", 0),
            "labels": item.get("labels", []),
            "trust": item.get("trust", ""),
        })
    return f"""다음 RSS 기반 AI 뉴스 후보를 한국어 Obsidian 큐레이션 노트로 작성하세요.

요구사항:
- 한국어로 간결하지만 실무적으로 큐레이션합니다.
- 사실과 시사점을 구분합니다.
- 모든 주요 항목에 원문 출처 URL을 Markdown 링크로 연결합니다.
- 대학 행정 AX, AI Agent, RAG/GraphRAG, 생성형 AI 정책, 공공부문/교육 적용 관점으로 중요도를 판단합니다.
- 과장하지 말고 RSS 제목/요약에서 확인 가능한 범위만 사실로 씁니다.
- 출력은 완성된 Markdown 문서만 반환합니다. 코드블록으로 감싸지 마세요.

문서 구조:
# {date_s} AI 뉴스 큐레이션

## 한눈에 보는 결론
- 3~5개 bullet

## 오늘의 Top Picks
각 항목마다:
### N. 제목
- 출처/게시일/신뢰도
- 핵심 요약
- 왜 중요한가
- Obsidian 연결 후보: [[AI Agent]], [[대학 행정 AX]] 등 2~4개

## 대학 행정 AX 관점 시사점

## 더 지켜볼 질문

## 수집 메타데이터
- 수집일: {date_s}
- 후보 기사 수: {len(items)}
- 수집 오류: {len(errors)}건

큐레이션 초점: {json.dumps(config.get('curation_focus', []), ensure_ascii=False)}
수집 오류: {json.dumps(errors, ensure_ascii=False)}
뉴스 후보 JSON:
{json.dumps(compact, ensure_ascii=False, indent=2)}
"""


def call_openai(prompt: str, model: str) -> str | None:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return None
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a Korean AI news curator for an Obsidian knowledge vault."},
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
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return None


def fallback_markdown(items: list[dict[str, Any]], errors: list[dict[str, str]], date_s: str) -> str:
    lines = [
        f"# {date_s} AI 뉴스 큐레이션 (한글 요약)",
        "",
        "> GEMINI_API_KEY가 없거나 Gemini API 호출이 실패해, 영어 원문을 한국어로 번역한 추출식 큐레이션으로 생성했습니다.",
        "",
        "## 한눈에 보는 결론",
    ]
    for item in items[:5]:
        title_ko = translate_en_ko(item['title'])
        lines.append(
            f"- **{title_ko}** — {item['source']} 기준 RSS 요약에서 확인. "
            f"원문 제목: {item['title']}. 관련 키워드: {', '.join(item.get('labels') or ['일반 AI'])}."
        )
    lines += ["", "## 오늘의 Top Picks"]
    for n, item in enumerate(items[:12], 1):
        title_ko = translate_en_ko(item['title'])
        summary_ko = translate_en_ko((item.get('summary') or 'RSS 요약 없음')[:450])
        lines += [
            "",
            f"### {n}. {title_ko}",
            f"- 원문 제목: {item['title']}",
            f"- 출처: [{item['source']}]({item['url']})",
            f"- 게시일: {item.get('published') or '미상'}",
            f"- 신뢰도: {item.get('trust') or '미상'}",
            f"- 점수/태그: {item.get('score', 0)} / {', '.join(item.get('labels') or ['미분류'])}",
            f"- 핵심 요약: {summary_ko}",
            "- Obsidian 연결 후보: [[AI 뉴스]], [[생성형 AI]], [[AI Agent]], [[대학 행정 AX]]",
        ]
    lines += [
        "",
        "## 대학 행정 AX 관점 시사점",
        "- AI Agent, RAG, 지식그래프, 정책/보안 관련 항목은 대학 행정 자동화와 지식관리 설계에 우선 검토할 가치가 있습니다.",
        "- 개별 뉴스의 실제 적용 가능성은 원문 전문 확인 후 별도 노트로 분리하세요.",
        "",
        "## 더 지켜볼 질문",
        "- 어떤 기술 변화가 대학 행정 업무흐름을 실제로 줄이는가?",
        "- 보안·개인정보·거버넌스 요구사항은 무엇인가?",
        "- RAG/GraphRAG 기반 내부 지식관리와 연결할 수 있는가?",
        "",
        "## 수집 메타데이터",
        f"- 수집일: {date_s}",
        f"- 후보 기사 수: {len(items)}",
        f"- 수집 오류: {len(errors)}건",
    ]
    if errors:
        lines.append("- 오류 피드:")
        for e in errors:
            lines.append(f"  - {e['feed']}: {e['error']}")
    return "\n".join(lines) + "\n"


def write_dashboard(note_path: Path, latest_items: list[dict[str, Any]], date_s: str) -> None:
    dash = OUT_DIR / "AI 뉴스 큐레이션 대시보드.md"
    top_links = "\n".join([f"- [[{note_path.stem}]] — 최신 자동 큐레이션" ])
    source_lines = "\n".join(
        f"- {item['source']}: [{translate_en_ko(item['title'])}]({item['url']})"
        for item in latest_items[:8]
    )
    content = f"""# AI 뉴스 큐레이션 대시보드

## 최신 브리핑
{top_links}

## 최근 Top 기사
{source_lines}

## 자동화 파이프라인
- 설정: `configs/ai_news_feeds.json`
- 스크립트: `scripts/ai_news_pipeline.py`
- 원자료: `raw/rss/ai-news-latest.json`
- 결과 폴더: `queries/AI 뉴스 큐레이션/`
- 마지막 갱신: {date_s}

## 운영 규칙
- 원문 출처 URL을 유지합니다.
- 한국어 요약은 RSS 제목/요약을 기반으로 하며, 중요한 의사결정 전 원문을 확인합니다.
- 대학 행정 AX, AI Agent, RAG/GraphRAG, 정책·보안 항목을 우선 큐레이션합니다.
"""
    dash.write_text(content, encoding="utf-8")


def update_index_and_log(note_path: Path, date_s: str) -> None:
    index = VAULT / "index.md"
    if index.exists():
        text = index.read_text(encoding="utf-8")
        link = "[[AI 뉴스 큐레이션 대시보드]]"
        if link not in text:
            index.write_text(text.rstrip() + f"\n\n- {link}: RSS 기반 AI 뉴스 한국어 요약·큐레이션 자동화 대시보드.\n", encoding="utf-8")
    log = VAULT / "log.md"
    entry = f"\n## [{date_s}] automation | AI 뉴스 큐레이션\n- RSS 기반 AI 뉴스 수집·한국어 요약 자동화 실행: `{note_path.relative_to(VAULT)}` 생성/갱신.\n"
    if log.exists():
        text = log.read_text(encoding="utf-8")
        if str(note_path.relative_to(VAULT)) not in text:
            log.write_text(text.rstrip() + entry, encoding="utf-8")


def git_backup(date_s: str) -> None:
    try:
        subprocess.run(["git", "add", "configs/ai_news_feeds.json", "scripts/ai_news_pipeline.py", "raw/rss", "queries/AI 뉴스 큐레이션", "index.md", "log.md"], cwd=VAULT, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=VAULT)
        if diff.returncode != 0:
            subprocess.run(["git", "commit", "-m", f"automation: AI news curation {date_s}"], cwd=VAULT, check=False)
            subprocess.run(["git", "push"], cwd=VAULT, check=False)
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true", help="Gemini API 호출 없이 추출식 큐레이션만 생성")
    ap.add_argument("--no-git", action="store_true", help="Git 백업 생략")
    ap.add_argument("--max-items", type=int, default=None)
    args = ap.parse_args()

    config = load_json(CONFIG_PATH, {})
    if not config:
        print(f"Config not found: {CONFIG_PATH}", file=sys.stderr)
        return 2
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    today = now_kst().date()
    date_s = today.isoformat()
    items, errors = collect(config)
    lookback_days = int(config.get("pipeline", {}).get("lookback_days", 0) or 0)
    filtered_items = filter_recent_items(items, lookback_days, today)
    # If a feed outage or overly narrow window leaves too few items, fall back
    # to the full ranked list rather than producing an empty note.
    if len(filtered_items) >= 3:
        items = filtered_items
    max_items = args.max_items or int(config.get("pipeline", {}).get("max_items", 30))
    curated = int(config.get("pipeline", {}).get("curated_items", 12))
    selected = items[:max_items]
    save_json(LATEST_JSON, {"date": date_s, "items": selected, "errors": errors})
    save_json(RAW_DIR / f"{date_s}-items.json", {"date": date_s, "items": selected, "errors": errors})

    model = config.get("pipeline", {}).get("gemini_model", config.get("pipeline", {}).get("openai_model", "gemini-1.5-flash"))
    summarize_mode = config.get("pipeline", {}).get("summarize_mode", "clustered")
    llm_used = False
    md = None
    overview_text = None

    if not args.no_llm and summarize_mode == "clustered":
        clusters = cluster_items_by_label(selected[:curated])
        cluster_results = summarize_clusters_parallel(clusters, model, date_s)
        cluster_sections: dict[str, str] = {}
        for label, items_in_cluster in clusters.items():
            text = cluster_results.get(label)
            if text:
                llm_used = True
            cluster_sections[label] = text or fallback_cluster_bullets(items_in_cluster)
        overview_text = call_gemini(make_overview_prompt(cluster_sections, date_s), model) if llm_used else None
        if llm_used:
            md = assemble_clustered_markdown(overview_text, cluster_sections, date_s)
    elif not args.no_llm:
        # Legacy single-prompt mode — kept for comparison / rollback via config.
        prompt = make_prompt(selected[:curated], errors, config, date_s)
        md = call_gemini(prompt, model)
        if md:
            llm_used = True
            overview_text = None

    if not md:
        md = fallback_markdown(selected[:curated], errors, date_s)
    if not md.endswith("\n"):
        md += "\n"
    note_path = OUT_DIR / f"{date_s} AI 뉴스 큐레이션.md"
    note_path.write_text(md, encoding="utf-8")
    write_dashboard(note_path, selected, date_s)
    update_index_and_log(note_path, date_s)
    if config.get("pipeline", {}).get("auto_git_backup", True) and not args.no_git:
        git_backup(date_s)

    published_discord = False
    if config.get("pipeline", {}).get("publish_discord", False):
        channel_id = config.get("pipeline", {}).get("discord_channel_id", "")
        if channel_id:
            digest = overview_text or "(오늘은 요약을 만들지 못했습니다.)"
            message = f"🗞️ {date_s} AI 뉴스 큐레이션\n\n{digest.strip()}\n\n📄 {note_path.relative_to(VAULT)}"
            published_discord = send_discord_message(channel_id, message)

    print(json.dumps({
        "ok": True,
        "date": date_s,
        "items_collected": len(items),
        "items_selected": len(selected),
        "errors": errors,
        "note": str(note_path),
        "dashboard": str(OUT_DIR / "AI 뉴스 큐레이션 대시보드.md"),
        "used_llm": llm_used,
        "llm_provider": "gemini" if llm_used else "fallback",
        "summarize_mode": summarize_mode,
        "published_discord": published_discord,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
