#!/usr/bin/env python3
"""
vault → Notion 자정 동기화.

두 데이터베이스를 채운다:
  1. AI 뉴스 아카이브  ← queries/RSS 기사 큐레이션/**.md
  2. Wiki Mirror       ← concepts/, comparisons/, queries/ (RSS 제외)

동기화 키는 둘 다 `Vault Path`다. 같은 경로가 이미 있으면 갱신하고,
없으면 새로 만든다. 그래서 몇 번을 돌려도 행이 불어나지 않는다.

주의: Vault Path가 키라서 vault에서 파일을 옮기면 노션에 중복 행이 생긴다.
      이동이 잦아지면 frontmatter에 불변 uid를 넣고 그걸 키로 바꾸는 게 낫다.

사용법:
    python3 sync_to_notion.py              # 증분 (state 이후 변경분만)
    python3 sync_to_notion.py --full       # 전량 재스캔
    python3 sync_to_notion.py --dry-run    # 노션에 쓰지 않고 계획만 출력
    python3 sync_to_notion.py --since 2026-09-01
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

VAULT = Path(os.environ.get("WIKI_VAULT", Path.home() / "Documents/wiki"))
HERE = Path(__file__).resolve().parent
STATE_PATH = HERE / "state.json"
LOG_PATH = HERE / "sync.log"
TOKEN_PATH = HERE / "token"

NOTION_VERSION = "2022-06-28"
API = "https://api.notion.com/v1"

# 노션 데이터소스 ID — 2026-09-19 생성분
DS_NEWS = "95c2dc71-3856-4ef5-8c4c-35b157d744b3"   # AI 뉴스 아카이브
DS_MIRROR = "11dd9354-c49b-46a4-8bfe-bb114479a9d6"  # Wiki Mirror

RSS_DIR = "queries/RSS 기사 큐레이션"

FEEDS = {
    "The Decoder", "TechCrunch AI", "The Verge AI", "MIT Technology Review AI",
    "AI타임스", "IT조선", "ZDNet Korea", "디지털데일리", "블로터", "한경 IT",
    "요즘IT", "Seth's Blog", "배움의 달인 Newsletter", "서핏 Surfit",
}
FEED_ALIAS = {"요즘IT » 피드": "요즘IT"}

# Wiki Mirror의 Area / Tier / Folder 는 신규 옵션을 못 만든다. 기존 값만 쓴다.
AREAS = [
    ("AI 자동화·에이전트", r"agent|에이전트|hermes|mcp|automation|자동화|claude|codex|hook|skill|워크플로"),
    ("지식관리·RAG", r"rag|obsidian|wiki|graph|zettel|제텔|노트|knowledge|memory|vault|notebooklm|온톨로지"),
    ("콘텐츠 제작", r"video|영상|블로그|콘텐츠|content|youtube|썸네일|글쓰기|humanize|번역|카드뉴스"),
    ("LLM 모델·인프라", r"llm|model|모델|gpu|토큰|api|비용|benchmark|벤치마크|local|로컬|quant|opencode"),
]
TIERS = {"evergreen", "concept", "episode", "capture"}


# ---------------------------------------------------------------- 유틸

def log(msg: str) -> None:
    """stdout 으로만 쓴다.

    run.sh 가 stdout 을 sync.log 로 리다이렉트하므로 여기서 파일에도 쓰면
    크론 실행 때 모든 줄이 두 번 기록된다. 직접 실행할 때만 파일로 넘긴다.
    """
    line = f"{datetime.now().isoformat(timespec='seconds')}  {msg}"
    print(line, flush=True)
    if sys.stdout.isatty():
        try:
            with LOG_PATH.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


def read_token() -> str:
    tok = os.environ.get("NOTION_TOKEN", "").strip()
    if tok:
        return tok
    if TOKEN_PATH.exists():
        tok = TOKEN_PATH.read_text(encoding="utf-8").strip()
        if tok:
            return tok
    log(
        "ERROR: Notion 토큰이 없습니다. "
        f"{TOKEN_PATH} 에 integration token 한 줄을 넣거나 NOTION_TOKEN 을 설정하세요."
    )
    sys.exit(2)


def api(method: str, path: str, token: str, body: dict | None = None) -> dict:
    """노션 API 호출. 429/5xx는 지수 백오프로 최대 5회 재시도."""
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(5):
        req = urllib.request.Request(
            API + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            payload = e.read().decode(errors="replace")
            if e.code in (429, 500, 502, 503, 504) and attempt < 4:
                wait = float(e.headers.get("Retry-After") or 2 ** attempt)
                log(f"  retry {attempt + 1}/4 ({e.code}) {wait}s")
                time.sleep(wait)
                continue
            raise RuntimeError(f"{method} {path} -> {e.code} {payload[:400]}") from None
        except urllib.error.URLError as e:
            if attempt < 4:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"{method} {path} -> {e}") from None
    raise RuntimeError("unreachable")


def frontmatter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    out: dict = {}
    for line in text[3:end].splitlines():
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip().strip("\"'")
        if val.startswith("[") and val.endswith("]"):
            val = [v.strip().strip("\"'") for v in val[1:-1].split(",") if v.strip()]
        out[key] = val
    return out


def body_of(text: str) -> str:
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    return text[end + 4:] if end >= 0 else text


def summarize(body: str, limit: int) -> str:
    b = re.sub(r"^#.*$", "", body, flags=re.M)
    b = re.sub(r"^>.*$", "", b, flags=re.M)
    return re.sub(r"\s+", " ", b).strip()[:limit]


def effective_date(fm: dict, path: Path) -> str:
    """증분 판단용 날짜. frontmatter created 가 없으면 파일 mtime 으로 대체한다.

    이 폴백이 없으면 created 없는 노트가 매 실행마다 전부 재전송된다.
    """
    created = str(fm.get("created", ""))[:10]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", created):
        return created
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")
    except OSError:
        return ""


def area_of(title: str, tags, path: str) -> str:
    hay = f"{title} {' '.join(tags) if isinstance(tags, list) else tags} {path}".lower()
    for name, pat in AREAS:
        if re.search(pat, hay):
            return name
    return "미분류"


# ------------------------------------------------- 노션 읽기 / 쓰기

def existing_rows(ds_id: str, token: str) -> dict[str, str]:
    """Vault Path -> page_id 맵. 데이터소스 전체를 페이지네이션으로 읽는다."""
    out: dict[str, str] = {}
    cursor = None
    while True:
        body: dict = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        res = api("POST", f"/data_sources/{ds_id}/query", token, body)
        for row in res.get("results", []):
            prop = row.get("properties", {}).get("Vault Path", {})
            rich = prop.get("rich_text") or []
            key = "".join(part.get("plain_text", "") for part in rich).strip()
            if key:
                out[key] = row["id"]
        if not res.get("has_more"):
            break
        cursor = res.get("next_cursor")
    return out


def txt(value: str, limit: int = 1900) -> dict:
    return {"rich_text": [{"type": "text", "text": {"content": value[:limit]}}]}


def title(value: str) -> dict:
    return {"title": [{"type": "text", "text": {"content": value[:200]}}]}


def upsert(ds_id: str, props: dict, page_id: str | None, token: str) -> str:
    if page_id:
        api("PATCH", f"/pages/{page_id}", token, {"properties": props})
        return "updated"
    api("POST", "/pages", token,
        {"parent": {"type": "data_source_id", "data_source_id": ds_id}, "properties": props})
    return "created"


# --------------------------------------------------------- 수집기

def collect_news(since: str | None) -> list[dict]:
    rows = []
    base = VAULT / RSS_DIR
    if not base.is_dir():
        return rows
    for path in base.rglob("*.md"):
        rel = str(path.relative_to(VAULT))
        text = path.read_text(encoding="utf-8", errors="replace")
        fm = frontmatter(text)
        if since and effective_date(fm, path) < since:
            continue
        created = str(fm.get("created", ""))[:10]
        m = re.search(r"## 2\) 기사 핵심 요약\n- (.*?)\n", text, re.S)
        summary = (m.group(1).strip() if m else summarize(body_of(text), 300))

        props: dict = {
            "제목": title(str(fm.get("title") or path.stem)),
            "Vault Path": txt(rel),
            "요약": txt(summary[:1800]),
            "상태": {"select": {"name": "신규"}},
        }
        if created:
            props["수집일"] = {"date": {"start": created}}
        pub = str(fm.get("published", ""))[:10]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", pub):
            props["발행일"] = {"date": {"start": pub}}
        url = str(fm.get("source_url", ""))
        if url.startswith("http"):
            props["URL"] = {"url": url}
        feed = FEED_ALIAS.get(str(fm.get("source_feed", "")), str(fm.get("source_feed", "")))
        if feed in FEEDS:
            props["피드"] = {"select": {"name": feed}}
        rows.append({"path": rel, "props": props})
    return rows


def collect_mirror(since: str | None) -> list[dict]:
    rows = []
    for folder in ("concepts", "comparisons", "queries"):
        base = VAULT / folder
        if not base.is_dir():
            continue
        for path in base.rglob("*.md"):
            rel = str(path.relative_to(VAULT))
            if rel.startswith(RSS_DIR):
                continue  # 뉴스는 별도 DB
            text = path.read_text(encoding="utf-8", errors="replace")
            fm = frontmatter(text)
            if since and effective_date(fm, path) < since:
                continue
            created = str(fm.get("created", ""))[:10]
            body = body_of(text)
            tags = fm.get("tags", [])
            if not isinstance(tags, list):
                tags = [tags] if tags else []
            name = str(fm.get("title") or path.stem)

            props: dict = {
                "Title": title(name),
                "Vault Path": txt(rel),
                "Summary": txt(summarize(body, 1800)),
                "Folder": {"select": {"name": folder}},
                "Status": {"select": {"name": "Reference" if folder != "queries" else "Inbox"}},
                "Area": {"select": {"name": area_of(name, tags, rel)}},
                "Words": {"number": len(body.split())},
            }
            tier = str(fm.get("memory_tier", ""))
            if tier in TIERS:
                props["Tier"] = {"select": {"name": tier}}
            typ = str(fm.get("type", ""))
            if typ:
                props["Type"] = txt(typ, 200)
            if created:
                props["Created"] = {"date": {"start": created}}
            src = str(fm.get("source", ""))
            if src.startswith("http"):
                props["Source"] = {"url": src}
            rows.append({"path": rel, "props": props})
    return rows


# ------------------------------------------------------------ main

def run(ds_id: str, label: str, rows: list[dict], token: str, dry: bool) -> tuple[int, int, int]:
    if not rows:
        log(f"[{label}] 대상 없음")
        return 0, 0, 0
    index = {} if dry else existing_rows(ds_id, token)
    log(f"[{label}] 대상 {len(rows)}건 / 노션 기존 {len(index)}행")
    created = updated = failed = 0
    for i, row in enumerate(rows, 1):
        if dry:
            log(f"  [dry] {row['path']}")
            created += 1
            continue
        try:
            result = upsert(ds_id, row["props"], index.get(row["path"]), token)
            created += result == "created"
            updated += result == "updated"
        except Exception as exc:  # noqa: BLE001 - 한 건 실패로 전체를 멈추지 않는다
            failed += 1
            log(f"  FAIL {row['path']}: {exc}")
        if i % 25 == 0:
            log(f"  ... {i}/{len(rows)}")
        time.sleep(0.34)  # 노션 레이트리밋 ~3 req/s
    log(f"[{label}] 생성 {created} / 갱신 {updated} / 실패 {failed}")
    return created, updated, failed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="전량 재스캔")
    ap.add_argument("--since", help="이 날짜(YYYY-MM-DD) 이후 created 만")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", choices=["news", "mirror"], help="한쪽만 동기화")
    args = ap.parse_args()

    state = {}
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            pass

    since = args.since
    if since is None and not args.full:
        since = state.get("last_since")
    if args.full:
        since = None

    token = "DRY" if args.dry_run else read_token()
    log(f"=== sync 시작 (since={since or '전량'}, dry={args.dry_run}) ===")

    totals = [0, 0, 0]
    if args.only != "mirror":
        c, u, f = run(DS_NEWS, "AI 뉴스", collect_news(since), token, args.dry_run)
        totals = [totals[0] + c, totals[1] + u, totals[2] + f]
    if args.only != "news":
        c, u, f = run(DS_MIRROR, "Wiki Mirror", collect_mirror(since), token, args.dry_run)
        totals = [totals[0] + c, totals[1] + u, totals[2] + f]

    log(f"=== 완료: 생성 {totals[0]} / 갱신 {totals[1]} / 실패 {totals[2]} ===")

    if not args.dry_run:
        # 다음 증분은 오늘 것부터 다시 본다 (당일 추가분을 놓치지 않도록 하루 겹침)
        STATE_PATH.write_text(json.dumps({
            "last_run": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "last_since": datetime.now().strftime("%Y-%m-%d"),
            "created": totals[0], "updated": totals[1], "failed": totals[2],
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    return 1 if totals[2] else 0


if __name__ == "__main__":
    sys.exit(main())
