#!/usr/bin/env python3
"""Semantic search over Obsidian notes stored in Postgres + pgvector.

Example:
  python obsidian_pgvector_search.py --question "GraphRAG가 뭐야?" --top-k 8
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from getpass import getuser
from pathlib import Path

from psycopg import connect
from pgvector import Vector
from pgvector.psycopg import register_vector

SCRIPT_DIR = Path(__file__).resolve().parent
PROTOTYPE_PATH = Path(os.getenv("RAG_PROTOTYPE_SCRIPT", str(Path.home() / ".hermes/scripts/obsidian_vector_rag_prototype.py")))
DEFAULT_DB = "obsidian_rag"


def load_proto():
    spec = importlib.util.spec_from_file_location("obsidian_vector_rag_prototype", PROTOTYPE_PATH)
    if spec is None or spec.loader is None:
        raise SystemExit(f"Cannot load prototype module: {PROTOTYPE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


proto = load_proto()


def parse_csv_set(value: str | None) -> set[str] | None:
    if not value:
        return None
    items = {v.strip() for v in value.split(",") if v.strip()}
    return items or None


def conninfo(*, host: str, port: int, user: str, password: str, dbname: str) -> str:
    parts = [f"host={host}", f"port={port}", f"user={user}", f"dbname={dbname}"]
    if password:
        parts.append(f"password={password}")
    return " ".join(parts)


def search(*, question: str, host: str, port: int, user: str, password: str, dbname: str,
           top_k: int, dim: int, include_folders: set[str] | None, note_path: str | None,
           max_chars: int, overlap_chars: int, provider: str = "hash") -> list[dict]:
    qvec = proto.embed_texts([question], provider=provider, dim=dim, model=proto.DEFAULT_OPENAI_EMBED_MODEL)[0]
    qvec = Vector(qvec[:dim] if len(qvec) > dim else qvec)

    params = [qvec]
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
            1 - (c.embedding <=> %s::vector) AS score
        FROM obsidian_chunks c
        JOIN obsidian_notes n ON n.path = c.note_path
        WHERE 1 = 1
    """
    if include_folders:
        folders = sorted(include_folders)
        sql += " AND n.folder = ANY(%s)"
        params.append(folders)
    if note_path:
        sql += " AND c.note_path = %s"
        params.append(note_path)
    sql += " ORDER BY score DESC LIMIT %s"
    params.append(top_k)

    with connect(conninfo(host=host, port=port, user=user, password=password, dbname=dbname)) as conn:
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


def render_markdown(question: str, hits: list[dict]) -> str:
    lines = [
        "---",
        f"title: pgvector semantic search - {question[:48]}",
        "type: search",
        f"created: {proto.current_date()}",
        f"tags: [pgvector, semantic-search, obsidian, rag]",
        "---",
        "",
        f"# {question}",
        "",
        "## 검색 결과",
    ]
    if not hits:
        lines.append("- 결과가 없습니다.")
    else:
        for hit in hits:
            excerpt = " ".join(str(hit["text"]).split())[:300]
            lines.extend([
                f"- **{hit['note_title']}**  ",
                f"  score={hit['score']:.3f} | chunk={hit['chunk_index']} | `{hit['note_path']}`  ",
                f"  {excerpt}",
            ])
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Semantic search over Obsidian notes in Postgres + pgvector")
    parser.add_argument("--question", required=True)
    parser.add_argument("--vault", default=os.getenv("OBSIDIAN_VAULT_PATH") or os.getenv("WIKI_PATH") or str(Path.home() / "Documents/wiki"))
    parser.add_argument("--host", default=os.getenv("PGHOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PGPORT", "5433")))
    parser.add_argument("--user", default=os.getenv("PGUSER", getuser()))
    parser.add_argument("--password", default=os.getenv("PGPASSWORD", ""))
    parser.add_argument("--database", default=os.getenv("PGDATABASE", DEFAULT_DB))
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--dim", type=int, default=256)
    parser.add_argument("--provider", choices=["hash", "openai", "openrouter"], default=os.getenv("PGVECTOR_SEARCH_PROVIDER", "hash").lower())
    parser.add_argument("--include-folders", default="")
    parser.add_argument("--note-path", default="")
    parser.add_argument("--output-note", default="")
    args = parser.parse_args(argv)

    hits = search(
        question=args.question,
        host=args.host,
        port=args.port,
        user=args.user,
        password=args.password,
        dbname=args.database,
        top_k=args.top_k,
        dim=args.dim,
        provider=args.provider,
        include_folders=parse_csv_set(args.include_folders),
        note_path=args.note_path or None,
        max_chars=1400,
        overlap_chars=200,
    )
    print(json.dumps({"question": args.question, "hits": hits}, ensure_ascii=False, indent=2))
    md = render_markdown(args.question, hits)
    print(md)

    if args.output_note:
        out = Path(args.output_note).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
        print(f"Wrote search note: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
