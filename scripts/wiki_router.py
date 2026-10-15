#!/usr/bin/env python3
"""Route a natural-language question to the right Obsidian/Wiki retrieval path.

Design goals:
- classify intent (general, wiki search, semantic search, graph RAG, compare, report, save)
- search the current vault when Wiki-related
- expand local wikilink graph for 1-hop / 2-hop evidence
- clearly label whether a live semantic index or Neo4j was actually used
- never write files unless the caller explicitly asks for a save workflow

This is a lightweight, dependency-free router that can run from Obsidian shell commands,
Hermes terminal calls, or a standalone CLI.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_VAULT = Path(os.getenv("OBSIDIAN_VAULT_PATH") or os.getenv("WIKI_PATH") or str(Path.home() / "Documents/wiki")).expanduser()
NEO_SYNC = Path(os.getenv("NEO4J_SYNC_SCRIPT", str(Path.home() / ".hermes/scripts/obsidian_neo4j_sync.py")))
RAG_HUB = SCRIPT_DIR / "obsidian_rag_hub.py"
PGVECTOR_HOST = os.getenv("PGHOST", "127.0.0.1")
PGVECTOR_PORT_CANDIDATES = [int(p) for p in os.getenv("PGPORT", "5432,5433").split(",") if p.strip().isdigit()]
NEO4J_HOST = os.getenv("NEO4J_HOST", "127.0.0.1")
NEO4J_PORT = int(os.getenv("NEO4J_PORT", "7687"))
VENV_PY = Path(os.getenv("RAG_HUB_PYTHON", sys.executable))

STOPWORDS = {
    "내", "너", "너가", "저", "이", "그", "저기", "것", "수", "좀", "좀더", "더", "좀요",
    "질문", "대답", "답변", "해줘", "해주세요", "해주세요", "해주세요.", "주세요", "주세요.",
    "찾아", "찾아줘", "검색", "보고서", "정리", "리포트", "위키", "wiki", "노트", "vault",
    "semantic", "search", "graph", "rag", "comparison", "비교", "저장", "수정", "작성",
}

COMPARE_PATTERNS = re.compile(r"(비교|차이|대비|vs\b|vs\.|대조|구분)", re.I)
GRAPH_PATTERNS = re.compile(r"(graph\s*rag|graphrag|wikilink|wikilinks?|1-hop|2-hop|링크\s*확장|연결\s*구조|연결망)", re.I)
SEMANTIC_PATTERNS = re.compile(r"(semantic|의미\s*유사|유사한|가까운\s*노트|벡터|embedding|embeddings?|pgvector)", re.I)
WIKI_PATTERNS = re.compile(r"(wiki|vault|옵시디언|obsidian|노트|노트를|검색해|찾아|관련\s*노트)", re.I)
REPORT_PATTERNS = re.compile(r"(보고서|리포트|정리해|요약해|구조|근거|출력형식)", re.I)
SAVE_PATTERNS = re.compile(r"(저장|기록|반영|반영해|파일\s*수정|노트\s*수정|스킬\s*수정|설정\s*수정)", re.I)


def read_question(raw_question: str | None) -> str:
    if raw_question:
        return raw_question.strip()
    stdin = sys.stdin.read().strip()
    return stdin


def normalize(text: str) -> list[str]:
    tokens = re.findall(r"[0-9A-Za-z가-힣_\-]+", text.lower())
    return [t for t in tokens if t not in STOPWORDS and len(t) > 1]


def classify_intent(question: str) -> tuple[str, list[str]]:
    tags: list[str] = []
    if SAVE_PATTERNS.search(question):
        return "save_request", ["save_request"]
    if GRAPH_PATTERNS.search(question):
        tags.append("graph_rag")
    if SEMANTIC_PATTERNS.search(question):
        tags.append("semantic_search")
    if COMPARE_PATTERNS.search(question):
        tags.append("comparison")
    if REPORT_PATTERNS.search(question):
        tags.append("report")
    if WIKI_PATTERNS.search(question):
        tags.append("wiki_search")
    if not tags:
        return "general_answer", ["general_answer"]

    # priority order
    for candidate in ["graph_rag", "semantic_search", "comparison", "report", "wiki_search"]:
        if candidate in tags:
            return candidate, tags
    return tags[0], tags


@dataclass
class Note:
    path: str
    title: str
    stem: str
    folder: str
    links: list[str]
    excerpt: str


def collect_notes(vault: Path) -> list[Note]:
    try:
        from obsidian_neo4j_sync import collect_notes as neo_collect_notes  # type: ignore
    except Exception:
        sys.path.insert(0, str(Path(os.getenv("RAG_SCRIPTS_DIR", str(Path.home() / ".hermes/scripts")))))
        from obsidian_neo4j_sync import collect_notes as neo_collect_notes  # type: ignore

    raw_notes = neo_collect_notes(vault)
    notes: list[Note] = []
    for n in raw_notes:
        notes.append(Note(
            path=n.path,
            title=n.title,
            stem=n.stem,
            folder=n.folder,
            links=list(n.links),
            excerpt=n.excerpt,
        ))
    return notes


def note_display_path(note: Note) -> str:
    rel = note.path[:-3] if note.path.endswith(".md") else note.path
    return rel


def build_note_index(notes: list[Note]) -> dict[str, list[Note]]:
    index: dict[str, list[Note]] = {}
    for note in notes:
        for key in {note.title, note.stem, note.path, note_display_path(note)}:
            index.setdefault(key.lower(), []).append(note)
    return index


def resolve_link_targets(link: str, index: dict[str, list[Note]]) -> list[Note]:
    candidates = []
    for key in {link, link.rstrip(".md"), link.split("#", 1)[0], Path(link).stem}:
        candidates.extend(index.get(key.lower(), []))
    seen = set()
    unique: list[Note] = []
    for note in candidates:
        if note.path not in seen:
            seen.add(note.path)
            unique.append(note)
    return unique


def score_notes(question: str, notes: list[Note]) -> list[tuple[float, Note, list[str]]]:
    q_tokens = normalize(question)
    scored: list[tuple[float, Note, list[str]]] = []
    for note in notes:
        hay = f"{note.title} {note.stem} {note.path} {note.excerpt}".lower()
        score = 0.0
        reasons: list[str] = []
        for tok in q_tokens:
            if tok in note.title.lower():
                score += 4.0
                reasons.append(f"title:{tok}")
            elif tok in note.stem.lower():
                score += 3.0
                reasons.append(f"stem:{tok}")
            elif tok in hay:
                score += 1.0
                reasons.append(f"body:{tok}")
        # Boost explicit wikilink-like overlap
        for link in note.links:
            if link.lower() in question.lower():
                score += 2.5
                reasons.append(f"link:{link}")
        if score > 0:
            scored.append((score, note, reasons))
    scored.sort(key=lambda x: (-x[0], x[1].path))
    return scored


def expand_link_graph(top_notes: list[Note], index: dict[str, list[Note]]) -> dict:
    one_hop: dict[str, list[Note]] = {}
    two_hop: dict[str, list[Note]] = {}
    rels_1: list[tuple[str, str]] = []
    rels_2: list[tuple[str, str]] = []

    frontier_1: list[Note] = []
    for note in top_notes:
        for link in note.links:
            targets = resolve_link_targets(link, index)
            if targets:
                one_hop.setdefault(note.path, [])
                for t in targets:
                    if t.path not in {n.path for n in one_hop[note.path]}:
                        one_hop[note.path].append(t)
                    rels_1.append((note.path, t.path))
                    frontier_1.append(t)

    seen_two = {n.path for n in top_notes}
    for note in frontier_1:
        for link in note.links:
            targets = resolve_link_targets(link, index)
            if targets:
                for t in targets:
                    if t.path in seen_two:
                        continue
                    seen_two.add(t.path)
                    two_hop.setdefault(note.path, [])
                    if t.path not in {n.path for n in two_hop[note.path]}:
                        two_hop[note.path].append(t)
                    rels_2.append((note.path, t.path))

    return {
        "one_hop": one_hop,
        "two_hop": two_hop,
        "relationships_1": rels_1,
        "relationships_2": rels_2,
        "mode": "wikilink-link-expansion",
    }


def can_connect(host: str, port: int, timeout: float = 0.8) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def resolve_pgvector_port() -> int | None:
    for port in PGVECTOR_PORT_CANDIDATES:
        if can_connect(PGVECTOR_HOST, port):
            return port
    return None


def run_rag_hub(question: str) -> tuple[bool, str]:
    if not RAG_HUB.exists():
        return False, "rag_hub_missing"
    if not VENV_PY.exists():
        return False, "pgvector_venv_missing"
    port = resolve_pgvector_port()
    if port is None:
        return False, "pgvector_port_closed"
    env = os.environ.copy()
    env["PGPORT"] = str(port)
    cmd = [str(VENV_PY), str(RAG_HUB), "ask", "--question", question, "--embed-provider", "hash"]
    # Let the hub decide whether Neo4j expansion can happen; if credentials are missing,
    # it will fall back and print a warning.
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        return False, proc.stderr.strip() or proc.stdout.strip() or "rag_hub_failed"
    return True, proc.stdout.strip()


def render_report(question: str, intent: str, tags: list[str], notes: list[Note], scored: list[tuple[float, Note, list[str]]], graph: dict, rag_status: str) -> str:
    top_notes = [item[1] for item in scored[:5]]
    lines: list[str] = []
    lines += ["---", f"title: Wiki router report - {question[:50]}", "type: router-report", f"created: {datetime.now().isoformat(timespec='seconds')}", "tags: [wiki-router, obsidian, hermes]", "---", "", f"# {question}"]
    lines += ["", "## 의도 분류", f"- primary: `{intent}`", f"- secondary: `{', '.join(tags)}`"]
    lines += ["", "## 실행 요약"]
    lines.append(f"- vault file search: {'실행' if scored else '실행 안 함'}")
    lines.append(f"- semantic search: {'실행' if rag_status == 'ok' else '미실행'} ({rag_status})")
    lines.append(f"- graph expansion: {'링크 확장만 수행' if graph.get('mode') == 'wikilink-link-expansion' else '미실행'}")
    lines.append("- neo4j live query: 미실행" if graph.get('mode') == 'wikilink-link-expansion' else "- neo4j live query: 확인 필요")
    lines.append("- vector index live query: 미실행" if rag_status != 'ok' else "- vector index live query: 실행")

    lines += ["", "## 핵심 결론"]
    if top_notes:
        best = top_notes[0]
        lines.append(f"- 질문과 가장 가까운 후보: [[{best.path[:-3] if best.path.endswith('.md') else best.path}]]")
        lines.append(f"- 판단: vault에 관련 노트가 있습니다. 아래 근거를 확인하세요.")
    else:
        lines.append("- 현재 질문에 대한 강한 근거를 찾지 못했습니다. 확인 필요입니다.")

    lines += ["", "## 관련 노트 링크"]
    if top_notes:
        for note in top_notes:
            lines.append(f"- [[{note.path[:-3] if note.path.endswith('.md') else note.path}]]")
    else:
        lines.append("- 없음")

    lines += ["", "## 근거"]
    if scored:
        for score, note, reasons in scored[:5]:
            excerpt = " ".join(note.excerpt.split())[:220]
            lines.append(f"- **{note.title}** (`{note.path}`) · score={score:.1f} · {', '.join(reasons[:6])}")
            lines.append(f"  - {excerpt}")
    else:
        lines.append("- 파일 검색 기준으로도 충분한 근거를 찾지 못했습니다.")

    lines += ["", "## 연결 구조"]
    if graph.get("relationships_1"):
        for src, dst in graph["relationships_1"][:20]:
            lines.append(f"- 1-hop: `{src}` -> `{dst}`")
    else:
        lines.append("- 1-hop: 없음")
    if graph.get("relationships_2"):
        for src, dst in graph["relationships_2"][:20]:
            lines.append(f"- 2-hop: `{src}` -> `{dst}`")
    else:
        lines.append("- 2-hop: 없음")

    lines += ["", "## 확인 필요"]
    if rag_status != "ok":
        lines.append("- Semantic Search는 현재 live index 확인이 필요합니다. Postgres/pgvector가 떠 있어야 실제 실행됩니다.")
    if not can_connect(NEO4J_HOST, NEO4J_PORT):
        lines.append("- Neo4j 포트에 연결되지 않아 live Graph RAG는 확인되지 않았습니다.")
    if not scored:
        lines.append("- 파일/링크 근거가 약하면 추측하지 말고 사용자의 추가 질문이 필요합니다.")
    lines.append("- 사용자가 저장을 명시하지 않았으므로 파일/노트/설정은 변경하지 않았습니다.")

    lines += ["", "## 사용자의 다음 행동"]
    lines.append("- 질문이 길면 핵심 키워드 3개만 남겨 다시 보내 주세요.")
    lines.append("- 저장이 필요하면 마지막에 '저장해줘'라고 명시해 주세요.")
    lines.append("- semantic/graph를 실제로 쓰려면 Postgres(pgvector)와 Neo4j 인증 상태를 맞춰야 합니다.")

    lines += ["", "## 데이터 소스 / 실행 여부"]
    lines.append(f"- source=file-search: {'yes' if scored else 'no'}")
    lines.append(f"- source=wikilink-link-expansion: {'yes' if graph.get('mode') == 'wikilink-link-expansion' else 'no'}")
    lines.append(f"- source=pgvector-live: {'yes' if rag_status == 'ok' else 'no'}")
    lines.append(f"- source=neo4j-live: {'yes' if rag_status == 'ok' and graph.get('mode') != 'wikilink-link-expansion' else 'no'}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Route a natural-language Wiki question to search/report paths")
    parser.add_argument("--question", default="")
    parser.add_argument("--vault", default=os.getenv("OBSIDIAN_VAULT_PATH") or os.getenv("WIKI_PATH") or str(DEFAULT_VAULT))
    parser.add_argument("--max-results", type=int, default=5)
    parser.add_argument("--dump-json", action="store_true")
    args = parser.parse_args(argv)

    question = read_question(args.question)
    if not question:
        raise SystemExit("No question provided. Pass --question or pipe text via stdin.")

    intent, tags = classify_intent(question)
    vault = Path(args.vault).expanduser().resolve()
    if not vault.exists():
        raise SystemExit(f"Vault path does not exist: {vault}")

    notes = collect_notes(vault)
    index = build_note_index(notes)
    scored = score_notes(question, notes)
    top_notes = [item[1] for item in scored[: args.max_results]]
    graph = expand_link_graph(top_notes, index)

    rag_ok, rag_payload = run_rag_hub(question) if intent in {"semantic_search", "graph_rag", "report", "comparison", "wiki_search"} else (False, "not_requested")
    rag_status = "ok" if rag_ok else rag_payload

    if args.dump_json:
        payload = {
            "question": question,
            "intent": intent,
            "tags": tags,
            "top_notes": [{"path": n.path, "title": n.title} for n in top_notes],
            "graph": {
                "mode": graph.get("mode"),
                "relationships_1": graph.get("relationships_1", []),
                "relationships_2": graph.get("relationships_2", []),
                "one_hop": {k: [n.path for n in v] for k, v in graph.get("one_hop", {}).items()},
                "two_hop": {k: [n.path for n in v] for k, v in graph.get("two_hop", {}).items()},
            },
            "rag_status": rag_status,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    report = render_report(question, intent, tags, notes, scored, graph, rag_status)
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
