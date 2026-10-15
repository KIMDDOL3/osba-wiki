#!/usr/bin/env python3
"""Obsidian AI search: Postgres/pgvector semantic search + optional Neo4j graph expansion.

Typical use from Obsidian Shell Commands (customize to your venv):
  python wiki_search.py --from-note "{{file_path:absolute}}" --graph'

It reads the question from the active note's `## 질문` section and saves a result note to
`queries/검색결과/YYYY-MM-DD-질문-slug.md`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import urllib.parse
from getpass import getuser
from datetime import datetime
from pathlib import Path
from typing import Any

from psycopg import connect
from pgvector import Vector
from pgvector.psycopg import register_vector

try:
    from neo4j import GraphDatabase
except Exception:  # Neo4j is optional; semantic search should still work.
    GraphDatabase = None  # type: ignore[assignment]

VAULT = Path(os.getenv("OBSIDIAN_VAULT_PATH") or os.getenv("WIKI_PATH") or str(Path.home() / "Documents/wiki")).expanduser().resolve()
RESULT_DIR = VAULT / "queries" / "검색결과"
DEFAULT_DB = "obsidian_rag"
DIM = 256
TOKEN_RE = re.compile(r"[A-Za-z0-9_가-힣]+")


def hash_embed(text: str, dim: int = DIM) -> list[float]:
    """Deterministic embedding compatible with the current local hash-indexed pgvector store."""
    vec = [0.0] * dim
    for token in TOKEN_RE.findall(text.lower()):
        h = int(hashlib.blake2b(token.encode("utf-8"), digest_size=8).hexdigest(), 16)
        idx = h % dim
        sign = 1.0 if ((h >> 8) & 1) else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def conninfo(dbname: str) -> str:
    host = os.getenv("PGHOST", "127.0.0.1")
    port = os.getenv("PGPORT", "5432")
    user = os.getenv("PGUSER") or getuser()
    password = os.getenv("PGPASSWORD", "")
    parts = [f"host={host}", f"port={port}", f"user={user}", f"dbname={dbname}"]
    if password:
        parts.append(f"password={password}")
    return " ".join(parts)


def extract_question_from_note(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    # Preferred: content between `## 질문` and the next heading.
    m = re.search(r"^##\s*질문\s*$\n(?P<body>.*?)(?=^##\s+|\Z)", text, flags=re.M | re.S)
    if m:
        body = m.group("body")
        lines = []
        for raw in body.splitlines():
            line = raw.strip()
            if not line or line.startswith("<!--") or line.startswith(">"):
                continue
            # Strip common checkbox/list prefixes in the search-note template.
            line = re.sub(r"^[-*]\s*", "", line)
            if line:
                lines.append(line)
        question = " ".join(lines).strip()
        if question:
            return question
    # Fallback: first non-empty non-frontmatter-ish line.
    for raw in text.splitlines():
        line = raw.strip().strip("# ")
        if line and not line.startswith("---") and ":" not in line[:30]:
            return line
    raise SystemExit(f"질문을 찾지 못했습니다. {path}의 `## 질문` 아래에 질문을 적어주세요.")


def sanitize_slug(text: str, max_len: int = 54) -> str:
    slug = re.sub(r"[^A-Za-z0-9가-힣]+", "-", text).strip("-")
    return (slug[:max_len].strip("-") or "wiki-search")


def obsidian_link(note_path: str, title: str | None = None) -> str:
    stem_path = note_path[:-3] if note_path.endswith(".md") else note_path
    label = title or Path(stem_path).name
    return f"[[{stem_path}|{label}]]"


def semantic_search(question: str, *, top_k: int, include_folders: str = "", note_path: str = "", audience: str = "") -> list[dict[str, Any]]:
    qvec = Vector(hash_embed(question))
    params: list[Any] = [qvec]
    sql = """
        SELECT
            c.id,
            c.note_path,
            c.note_title,
            c.note_folder,
            c.chunk_index,
            c.text,
            c.start_line,
            c.end_line,
            1 - (c.embedding <=> %s) AS score
        FROM obsidian_chunks c
        JOIN obsidian_notes n ON n.path = c.note_path
        WHERE 1 = 1
    """
    folders = [x.strip() for x in include_folders.split(",") if x.strip()]
    if folders:
        sql += " AND (" + " OR ".join(["n.folder = %s OR n.folder LIKE %s" for _ in folders]) + ")"
        for folder in folders:
            params.extend([folder, f"{folder}/%"])
    if audience:
        # audience filter: match frontmatter audience containing the mode (builder matches 'builder, ai-rag')
        sql += " AND n.frontmatter_json->>'audience' LIKE %s"
        params.append(f"%{audience}%")
    if note_path:
        sql += " AND c.note_path = %s"
        params.append(note_path)
    sql += " ORDER BY c.embedding <=> %s LIMIT %s"
    params.extend([qvec, top_k])

    dbname = os.getenv("PGDATABASE", DEFAULT_DB)
    with connect(conninfo(dbname)) as conn:
        register_vector(conn)
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [
        {
            "id": r[0],
            "note_path": r[1],
            "note_title": r[2],
            "note_folder": r[3],
            "chunk_index": r[4],
            "text": r[5],
            "start_line": r[6],
            "end_line": r[7],
            "score": float(r[8]),
        }
        for r in rows
    ]


def expand_graph(note_paths: list[str], *, limit: int = 160) -> dict[str, Any]:
    if not note_paths or GraphDatabase is None:
        return {"ok": False, "warning": "Neo4j driver 없음 또는 검색 결과 없음", "nodes": [], "relationships": []}
    password = os.getenv("NEO4J_PASSWORD", "")
    if not password:
        return {"ok": False, "warning": "NEO4J_PASSWORD 없음: semantic-search만 수행", "nodes": [], "relationships": []}
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    user = os.getenv("NEO4J_USER", "neo4j")
    database = os.getenv("NEO4J_DATABASE", "neo4j")
    driver = GraphDatabase.driver(uri, auth=(user, password), connection_timeout=8)
    nodes: dict[str, dict[str, Any]] = {}
    rels: list[dict[str, Any]] = []
    try:
        with driver.session(database=database) as session:
            result = session.run(
                """
                MATCH (n:Note)
                WHERE n.path IN $paths
                OPTIONAL MATCH (n)-[r]-(m)
                RETURN n.path AS source_path,
                       n.title AS source_title,
                       labels(n) AS source_labels,
                       type(r) AS rel_type,
                       coalesce(m.path, m.name) AS target_key,
                       coalesce(m.title, m.name, m.path) AS target_title,
                       labels(m) AS target_labels
                LIMIT $limit
                """,
                paths=note_paths,
                limit=limit,
            )
            for rec in result:
                src = rec["source_path"]
                nodes.setdefault(src, {"key": src, "title": rec["source_title"] or src, "labels": rec["source_labels"]})
                tgt = rec["target_key"]
                if tgt:
                    nodes.setdefault(tgt, {"key": tgt, "title": rec["target_title"] or tgt, "labels": rec["target_labels"]})
                    rels.append({"source": src, "target": tgt, "type": rec["rel_type"]})
        return {"ok": True, "nodes": list(nodes.values()), "relationships": rels}
    except Exception as exc:
        return {"ok": False, "warning": str(exc), "nodes": [], "relationships": []}
    finally:
        driver.close()


def render_markdown(question: str, hits: list[dict[str, Any]], graph: dict[str, Any], *, source_note: Path | None) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    unique_paths: list[str] = []
    seen = set()
    for hit in hits:
        if hit["note_path"] not in seen:
            seen.add(hit["note_path"])
            unique_paths.append(hit["note_path"])

    lines = [
        "---",
        f"title: AI 검색 결과 - {question[:48]}",
        "type: search-result",
        f"created: {now}",
        "tags: [ai-search, pgvector, semantic-search, neo4j, graphrag, obsidian]",
        f"question: {json.dumps(question, ensure_ascii=False)}",
        "---",
        "",
        f"# AI 검색 결과 - {question}",
        "",
        "## 실행 정보",
        f"- source: `semantic-search live` + `neo4j-traversal {'live' if graph.get('ok') else 'skipped'}`",
        f"- vault: `{VAULT}`",
        f"- Postgres DB: `{os.getenv('PGDATABASE', DEFAULT_DB)}`",
        f"- top_k: `{len(hits)}`",
    ]
    if source_note:
        try:
            rel = source_note.resolve().relative_to(VAULT).as_posix()
            lines.append(f"- 입력 노트: {obsidian_link(rel, source_note.stem)}")
        except Exception:
            lines.append(f"- 입력 노트: `{source_note}`")
    if not graph.get("ok") and graph.get("warning"):
        lines.append(f"- Neo4j 참고: `{graph['warning']}`")

    lines.extend(["", "## 질문", question, "", "## 관련 문단"])
    if not hits:
        lines.append("- 결과가 없습니다.")
    else:
        for i, hit in enumerate(hits, start=1):
            excerpt = " ".join(str(hit["text"]).split())[:520]
            lines.extend([
                f"### {i}. {obsidian_link(hit['note_path'], hit['note_title'])}",
                f"- source: `semantic-search`",
                f"- score: `{hit['score']:.3f}`",
                f"- folder: `{hit['note_folder'] or '.'}`",
                f"- chunk: `{hit['chunk_index']}` / lines `{hit['start_line']}-{hit['end_line']}`",
                f"- path: `{hit['note_path']}`",
                "",
                f"> {excerpt}",
                "",
            ])

    lines.extend(["", "## Neo4j 그래프 확장"])
    if not graph.get("ok"):
        lines.append(f"- 그래프 확장 생략: `{graph.get('warning', 'unknown')}`")
    else:
        note_nodes = [n for n in graph.get("nodes", []) if "Note" in (n.get("labels") or [])]
        tag_nodes = [n for n in graph.get("nodes", []) if "Tag" in (n.get("labels") or [])]
        folder_nodes = [n for n in graph.get("nodes", []) if "Folder" in (n.get("labels") or [])]
        rels = graph.get("relationships", [])
        lines.extend([
            f"- expanded nodes: `{len(graph.get('nodes', []))}`",
            f"- relationships: `{len(rels)}`",
            f"- Note nodes: `{len(note_nodes)}`, Tag nodes: `{len(tag_nodes)}`, Folder nodes: `{len(folder_nodes)}`",
            "",
            "### 연결 노트 후보",
        ])
        for n in note_nodes[:20]:
            key = n.get("key", "")
            title = n.get("title") or key
            if str(key).endswith(".md"):
                lines.append(f"- {obsidian_link(str(key), str(title))}")
            else:
                lines.append(f"- `{key}` — {title}")
        if tag_nodes:
            lines.extend(["", "### 관련 태그"])
            lines.append(", ".join(f"`#{n.get('key')}`" for n in tag_nodes[:40]))
        if folder_nodes:
            lines.extend(["", "### 관련 폴더"])
            for n in folder_nodes[:20]:
                lines.append(f"- `{n.get('key') or '.'}`")
        if rels:
            lines.extend(["", "### 관계 샘플"])
            for rel in rels[:40]:
                src = str(rel["source"])
                tgt = str(rel["target"])
                src_md = obsidian_link(src, Path(src).stem) if src.endswith(".md") else f"`{src}`"
                tgt_md = obsidian_link(tgt, Path(tgt).stem) if tgt.endswith(".md") else f"`{tgt}`"
                lines.append(f"- {src_md} -[{rel['type']}]- {tgt_md}")

    lines.extend([
        "",
        "## 다음 행동",
        "- 좋은 결과면 관련 문단을 기존 `queries/` 노트에 붙이거나 `concepts/`로 승격하세요.",
        "- 관계 구조를 더 보고 싶으면 결과 노트의 `연결 노트 후보`를 하나 골라 다시 검색하세요.",
        "- `raw/clippings`가 너무 많이 뜨면 입력 노트의 사용 예시처럼 `--include-folders queries,concepts,comparisons` 옵션으로 좁히세요.",
        "",
    ])
    return "\n".join(lines)


def write_result(question: str, markdown: str) -> Path:
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    day = datetime.now().strftime("%Y-%m-%d")
    slug = sanitize_slug(question)
    out = RESULT_DIR / f"{day}-{slug}.md"
    if out.exists():
        out = RESULT_DIR / f"{day}-{datetime.now().strftime('%H%M%S')}-{slug}.md"
    out.write_text(markdown, encoding="utf-8")
    return out


def append_recent_result(source_note: Path, result_note: Path, question: str) -> None:
    try:
        text = source_note.read_text(encoding="utf-8", errors="replace")
        rel = result_note.relative_to(VAULT).as_posix()
        line = f"- {datetime.now().strftime('%Y-%m-%d %H:%M')} — [[{rel[:-3]}|{question[:60]}]]"
        marker = "## 최근 결과"
        if marker in text:
            text = text.replace(marker, marker + "\n" + line, 1)
        else:
            text += "\n\n## 최근 결과\n" + line + "\n"
        source_note.write_text(text, encoding="utf-8")
    except Exception:
        pass


def open_in_obsidian(result_note: Path) -> None:
    """Best-effort open of the generated result note in the current vault."""
    try:
        rel = result_note.resolve().relative_to(VAULT).as_posix()
        vault_name = VAULT.name
        uri = "obsidian://open?" + urllib.parse.urlencode({"vault": vault_name, "file": rel})
        subprocess.run(["open", uri], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        # Fallback: ask macOS to open the file with the default Markdown app.
        try:
            subprocess.run(["open", str(result_note)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Postgres/pgvector + Neo4j Obsidian AI search")
    parser.add_argument("--question", default="", help="검색 질문. 생략 시 --from-note의 `## 질문` 섹션을 읽음")
    parser.add_argument("--from-note", default="", help="Obsidian active note path")
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--include-folders", default="", help="예: queries,concepts,comparisons")
    parser.add_argument("--note-path", default="")
    parser.add_argument("--audience", default="", help="beginner | builder | verifier 대상 필터")
    parser.add_argument("--graph", action="store_true", help="Neo4j graph expansion 포함")
    parser.add_argument("--graph-limit", type=int, default=160)
    parser.add_argument("--open", action="store_true", help="검색 결과 노트를 Obsidian에서 바로 열기")
    parser.add_argument("--stdout-only", action="store_true")
    args = parser.parse_args(argv)

    source_note = Path(args.from_note).expanduser() if args.from_note else None
    question = args.question.strip()
    if not question and source_note:
        question = extract_question_from_note(source_note)
    if not question:
        raise SystemExit("--question 또는 --from-note의 `## 질문`이 필요합니다.")

    hits = semantic_search(question, top_k=args.top_k, include_folders=args.include_folders, note_path=args.note_path, audience=args.audience)
    unique_paths = []
    for hit in hits:
        if hit["note_path"] not in unique_paths:
            unique_paths.append(hit["note_path"])
    graph = expand_graph(unique_paths, limit=args.graph_limit) if args.graph else {"ok": False, "warning": "--graph 미사용", "nodes": [], "relationships": []}
    md = render_markdown(question, hits, graph, source_note=source_note)

    if args.stdout_only:
        print(md)
        return 0

    out = write_result(question, md)
    if source_note:
        append_recent_result(source_note, out, question)
    if args.open:
        open_in_obsidian(out)
    print(f"AI 검색 완료: {out}")
    print(f"질문: {question}")
    print(f"semantic hits: {len(hits)} | graph: {'ok' if graph.get('ok') else 'skipped'} | graph nodes: {len(graph.get('nodes', []))} | relationships: {len(graph.get('relationships', []))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
