#!/usr/bin/env python3
"""Unified Obsidian RAG hub.

What it does
- Syncs Obsidian notes into Postgres + pgvector using Gemini embeddings
- Syncs the same vault into Neo4j for graph traversal
- Answers questions by combining pgvector semantic search + Neo4j expansion

Defaults
- Vault: ~/Documents/wiki
- Postgres: 127.0.0.1:5433 / obsidian_rag
- Neo4j: bolt://localhost:7687 / neo4j
- Gemini embedding model: gemini-embedding-2
- Output dimension: 256

Requirements
- python with psycopg[binary], pgvector, neo4j installed
- packages: psycopg[binary], pgvector, neo4j
- env: GOOGLE_API_KEY, NEO4J_PASSWORD
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict
from getpass import getuser
from pathlib import Path
from typing import Iterable

from psycopg import connect
from psycopg.sql import Identifier, SQL
from psycopg.types.json import Jsonb
from pgvector import Vector
from pgvector.psycopg import register_vector

SCRIPT_DIR = Path(__file__).resolve().parent
PROTOTYPE_PATH = Path(os.getenv("RAG_PROTOTYPE_SCRIPT", str(Path.home() / ".hermes/scripts/obsidian_vector_rag_prototype.py")))
NEO4J_SYNC_PATH = Path(os.getenv("NEO4J_SYNC_SCRIPT", str(Path.home() / ".hermes/scripts/obsidian_neo4j_sync.py")))
DEFAULT_VAULT = Path.home() / "Documents/wiki"
DEFAULT_DB = "obsidian_rag"
DEFAULT_DIM = 768
DEFAULT_EMBED_MODEL = "gemini-embedding-001"
DEFAULT_TASK_TYPE = "SEMANTIC_SIMILARITY"


def load_module(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"Cannot load module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


proto = load_module(PROTOTYPE_PATH, "obsidian_vector_rag_prototype")
neo_sync = load_module(NEO4J_SYNC_PATH, "obsidian_neo4j_sync")


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


def ensure_database(*, host: str, port: int, user: str, password: str, dbname: str) -> None:
    admin = conninfo(host=host, port=port, user=user, password=password, dbname="postgres")
    with connect(admin, autocommit=True) as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
        if cur.fetchone() is None:
            cur.execute(SQL("CREATE DATABASE {}").format(Identifier(dbname)))


def ensure_embedding_cache_schema(conn, dim: int) -> None:
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS embedding_cache (
            provider text NOT NULL,
            model text NOT NULL,
            dim integer NOT NULL,
            content_hash text NOT NULL,
            content text NOT NULL,
            embedding vector({dim}) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (provider, model, dim, content_hash)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_embedding_cache_hash ON embedding_cache(content_hash)")


def ensure_schema(conn, dim: int) -> None:
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    row = conn.execute(
        """
        SELECT format_type(a.atttypid, a.atttypmod) AS vector_type
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        WHERE c.relname = 'obsidian_chunks' AND a.attname = 'embedding' AND NOT a.attisdropped
        """
    ).fetchone()
    if row and row[0] != f"vector({dim})":
        conn.execute("DROP INDEX IF EXISTS idx_obsidian_chunks_embedding")
        conn.execute("DROP INDEX IF EXISTS idx_obsidian_chunks_note_path")
        conn.execute("DROP TABLE IF EXISTS obsidian_chunks")
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS obsidian_notes (
            path text PRIMARY KEY,
            title text NOT NULL,
            stem text NOT NULL,
            folder text NOT NULL DEFAULT '',
            type text NOT NULL DEFAULT 'note',
            tags text[] NOT NULL DEFAULT '{{}}',
            frontmatter_json jsonb NOT NULL DEFAULT '{{}}'::jsonb,
            size integer NOT NULL DEFAULT 0,
            modified_epoch bigint NOT NULL DEFAULT 0,
            excerpt text NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS obsidian_chunks (
            id text PRIMARY KEY,
            note_path text NOT NULL REFERENCES obsidian_notes(path) ON DELETE CASCADE,
            note_title text NOT NULL,
            note_folder text NOT NULL DEFAULT '',
            chunk_index integer NOT NULL,
            text text NOT NULL,
            start_line integer NOT NULL,
            end_line integer NOT NULL,
            embedding vector({dim}) NOT NULL,
            embedding_dim integer NOT NULL,
            synced_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS embedding_cache (
            provider text NOT NULL,
            model text NOT NULL,
            dim integer NOT NULL,
            content_hash text NOT NULL,
            content text NOT NULL,
            embedding vector({dim}) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (provider, model, dim, content_hash)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_embedding_cache_hash ON embedding_cache(content_hash)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_obsidian_chunks_note_path ON obsidian_chunks(note_path)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_obsidian_chunks_embedding ON obsidian_chunks USING hnsw (embedding vector_cosine_ops)")


def upsert_notes(conn, notes: list) -> None:
    rows = []
    for note in notes:
        rows.append(
            (
                note.path,
                note.title,
                note.stem,
                note.folder,
                note.type,
                note.tags,
                Jsonb(note.frontmatter),
                note.size,
                int(note.modified),
                note.excerpt,
            )
        )
    if not rows:
        return
    cur = conn.cursor()
    cur.executemany(
        """
        INSERT INTO obsidian_notes (
            path, title, stem, folder, type, tags, frontmatter_json, size, modified_epoch, excerpt
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (path) DO UPDATE SET
            title = EXCLUDED.title,
            stem = EXCLUDED.stem,
            folder = EXCLUDED.folder,
            type = EXCLUDED.type,
            tags = EXCLUDED.tags,
            frontmatter_json = EXCLUDED.frontmatter_json,
            size = EXCLUDED.size,
            modified_epoch = EXCLUDED.modified_epoch,
            excerpt = EXCLUDED.excerpt
        """,
        rows,
    )


def upsert_chunks(conn, chunks: list) -> None:
    rows = []
    for chunk in chunks:
        rows.append(
            (
                chunk.id,
                chunk.note_path,
                chunk.note_title,
                chunk.note_folder,
                chunk.chunk_index,
                chunk.text,
                chunk.start_line,
                chunk.end_line,
                chunk.embedding,
                len(chunk.embedding),
            )
        )
    if not rows:
        return
    cur = conn.cursor()
    cur.executemany(
        """
        INSERT INTO obsidian_chunks (
            id, note_path, note_title, note_folder, chunk_index, text,
            start_line, end_line, embedding, embedding_dim
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE SET
            note_path = EXCLUDED.note_path,
            note_title = EXCLUDED.note_title,
            note_folder = EXCLUDED.note_folder,
            chunk_index = EXCLUDED.chunk_index,
            text = EXCLUDED.text,
            start_line = EXCLUDED.start_line,
            end_line = EXCLUDED.end_line,
            embedding = EXCLUDED.embedding,
            embedding_dim = EXCLUDED.embedding_dim,
            synced_at = now()
        """,
        rows,
    )


def _post_json(url: str, payload: dict, headers: dict[str, str], retries: int = 4, sleep_s: float = 1.5) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers={**headers, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", "ignore")
            last_exc = RuntimeError(f"HTTP {exc.code}: {err_body}")
        except Exception as exc:
            last_exc = exc
        if attempt >= retries:
            break
        time.sleep(sleep_s * (2 ** attempt))
    raise RuntimeError(f"Gemini API request failed: {last_exc}")


def embedding_content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_vector_value(value) -> list[float]:
    if isinstance(value, list):
        return [float(v) for v in value]
    if isinstance(value, str):
        raw = value.strip().strip("[]")
        if not raw:
            return []
        return [float(part) for part in raw.split(",")]
    try:
        return [float(v) for v in value]
    except TypeError:
        return []


def vector_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{float(value):.8f}" for value in values) + "]"


def cached_embeddings(conn, texts: list[str], *, provider: str, model: str, dim: int) -> dict[str, list[float]]:
    if not texts:
        return {}
    hashes = [embedding_content_hash(text) for text in texts]
    cur = conn.cursor()
    cur.execute(
        """
        SELECT content, embedding
        FROM embedding_cache
        WHERE provider = %s AND model = %s AND dim = %s AND content_hash = ANY(%s)
        """,
        (provider, model, dim, hashes),
    )
    out: dict[str, list[float]] = {}
    for row in cur.fetchall():
        values = parse_vector_value(row[1])
        if len(values) >= dim:
            out[row[0]] = values[:dim]
    return out


def upsert_embedding_cache(conn, items: list[tuple[str, list[float]]], *, provider: str, model: str, dim: int) -> None:
    if not items:
        return
    cur = conn.cursor()
    cur.executemany(
        """
        INSERT INTO embedding_cache (provider, model, dim, content_hash, content, embedding)
        VALUES (%s, %s, %s, %s, %s, %s::vector)
        ON CONFLICT (provider, model, dim, content_hash) DO UPDATE SET
            content = EXCLUDED.content,
            embedding = EXCLUDED.embedding,
            updated_at = now()
        """,
        [
            (provider, model, dim, embedding_content_hash(text), text, vector_literal(values))
            for text, values in items
        ],
    )


def gemini_embed_texts(texts: list[str], *, api_key: str, dim: int, model: str = DEFAULT_EMBED_MODEL,
                       task_type: str = DEFAULT_TASK_TYPE, batch_size: int = 16) -> list[list[float]]:
    if not api_key:
        raise SystemExit("GOOGLE_API_KEY가 비어 있습니다. Gemini 임베딩을 쓰려면 키를 설정하세요.")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:embedContent"
    headers = {"x-goog-api-key": api_key}
    embeddings: list[list[float]] = []
    for text in texts:
        payload = {
            "model": f"models/{model}",
            "content": {"parts": [{"text": text}]},
        }
        data = _post_json(url, payload, headers)
        emb = data.get("embedding") or data.get("embeddings", [{}])[0]
        values = emb.get("values") if isinstance(emb, dict) else None
        if not isinstance(values, list):
            raise RuntimeError(f"Unexpected Gemini embedding response: {data}")
        if len(values) < dim:
            raise RuntimeError(f"Gemini returned only {len(values)} dims, expected at least {dim}")
        embeddings.append([float(v) for v in values[:dim]])
    return embeddings


def hash_embed_texts(texts: list[str], *, dim: int) -> list[list[float]]:
    return proto.embed_texts(texts, provider="hash", dim=dim, model=proto.DEFAULT_OPENAI_EMBED_MODEL)


def embed_texts(texts: list[str], *, provider: str, dim: int, api_key: str | None) -> list[list[float]]:
    if provider == "hash":
        return hash_embed_texts(texts, dim=dim)
    if provider == "gemini":
        return gemini_embed_texts(texts, api_key=api_key or "", dim=dim)
    raise SystemExit(f"Unknown embed provider: {provider}")


def sync_pgvector(*, vault: Path, include_folders: set[str] | None, exclude_folders: set[str] | None,
                 limit_notes: int | None, max_chars: int, overlap_chars: int, dim: int,
                 host: str, port: int, user: str, password: str, dbname: str,
                 dry_run: bool, embed_provider: str, api_key: str | None,
                 cache_dbname: str | None = None) -> dict:
    notes = proto.collect_notes(vault, include_folders=include_folders, exclude_folders=exclude_folders, limit_notes=limit_notes)
    chunks, _idf = proto.make_chunks(notes, provider="hash", dim=dim, model=proto.DEFAULT_OPENAI_EMBED_MODEL,
                                     max_chars=max_chars, overlap_chars=overlap_chars)

    cached_count = 0
    embedded_count = 0
    cache_dbname = cache_dbname or dbname

    if embed_provider != "hash" and not dry_run:
        ensure_database(host=host, port=port, user=user, password=password, dbname=dbname)
        existing_chunks: dict[str, tuple[str, list[float]]] = {}
        try:
            with connect(conninfo(host=host, port=port, user=user, password=password, dbname=dbname)) as conn:
                with conn.transaction():
                    ensure_schema(conn, dim=dim)
                register_vector(conn)
                cur = conn.cursor()
                cur.execute("SELECT id, text, embedding FROM obsidian_chunks WHERE embedding_dim = %s", (dim,))
                for row in cur.fetchall():
                    cid, ctext, cemb = row[0], row[1], row[2]
                    existing_chunks[cid] = (ctext, list(cemb) if cemb else [])
        except Exception:
            existing_chunks = {}

        chunks_to_embed = []
        for chunk in chunks:
            if chunk.id in existing_chunks and existing_chunks[chunk.id][0] == chunk.text and existing_chunks[chunk.id][1]:
                chunk.embedding = existing_chunks[chunk.id][1]
                cached_count += 1
            else:
                chunks_to_embed.append(chunk)

        if chunks_to_embed:
            texts = [c.text for c in chunks_to_embed]
            ensure_database(host=host, port=port, user=user, password=password, dbname=cache_dbname)
            with connect(conninfo(host=host, port=port, user=user, password=password, dbname=cache_dbname)) as cache_conn:
                register_vector(cache_conn)
                with cache_conn.transaction():
                    ensure_embedding_cache_schema(cache_conn, dim=dim)
                cache = cached_embeddings(cache_conn, texts, provider=embed_provider, model=DEFAULT_EMBED_MODEL, dim=dim)
                chunks_missing_cache = []
                for chunk in chunks_to_embed:
                    cached_vector = cache.get(chunk.text)
                    if cached_vector:
                        chunk.embedding = cached_vector
                        cached_count += 1
                    else:
                        chunks_missing_cache.append(chunk)

                if chunks_missing_cache:
                    missing_texts = [c.text for c in chunks_missing_cache]
                    vectors = embed_texts(missing_texts, provider=embed_provider, dim=dim, api_key=api_key)
                    for chunk, vec in zip(chunks_missing_cache, vectors):
                        chunk.embedding = vec
                    upsert_embedding_cache(
                        cache_conn,
                        list(zip(missing_texts, vectors)),
                        provider=embed_provider,
                        model=DEFAULT_EMBED_MODEL,
                        dim=dim,
                    )
                    cache_conn.commit()
                    embedded_count = len(chunks_missing_cache)
    elif embed_provider != "hash":
        texts = [c.text for c in chunks]
        vectors = embed_texts(texts, provider=embed_provider, dim=dim, api_key=api_key)
        for chunk, vec in zip(chunks, vectors):
            chunk.embedding = vec
        embedded_count = len(chunks)

    summary = {
        "vault": str(vault),
        "database": dbname,
        "embedding_cache_database": cache_dbname,
        "notes": len(notes),
        "chunks": len(chunks),
        "cached_chunks": cached_count,
        "embedded_chunks": embedded_count,
        "folders": sorted({n.folder or "." for n in notes}),
        "dim": dim,
        "embed_provider": embed_provider,
    }
    if dry_run:
        return summary

    ensure_database(host=host, port=port, user=user, password=password, dbname=dbname)
    with connect(conninfo(host=host, port=port, user=user, password=password, dbname=dbname)) as conn:
        with conn.transaction():
            ensure_schema(conn, dim=dim)
        register_vector(conn)
        with conn.transaction():
            upsert_notes(conn, notes)
            upsert_chunks(conn, chunks)
    return summary


def sync_neo4j(*, vault: Path, uri: str, user: str, password: str, database: str, clear: bool, limit_notes: int | None) -> dict:
    notes = neo_sync.collect_notes(vault)
    if limit_notes is not None:
        notes = notes[:limit_notes]
    if not password:
        raise SystemExit("NEO4J_PASSWORD가 비어 있습니다. Neo4j 동기화를 하려면 비밀번호를 설정하세요.")
    summary = neo_sync.build_summary(notes)
    neo_sync.sync_to_neo4j(notes, uri, user, password, database, clear=clear)
    summary.update({"vault": str(vault), "uri": uri, "database": database, "mode": "synced"})
    return summary


def search_pgvector(*, question: str, host: str, port: int, user: str, password: str, dbname: str,
                    top_k: int, dim: int, include_folders: set[str] | None, note_path: str | None,
                    embed_provider: str, api_key: str | None) -> list[dict]:
    qvec = embed_texts([question], provider=embed_provider, dim=dim, api_key=api_key)[0]
    qvec = Vector(qvec)
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
            1 - (c.embedding <=> %s) AS score
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
    sql += " ORDER BY c.embedding <=> %s LIMIT %s"
    params.extend([qvec, top_k])

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


def expand_graph(*, note_paths: list[str], uri: str, user: str, password: str, database: str, limit: int = 100) -> dict:
    if not note_paths:
        return {"nodes": [], "relationships": []}
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(uri, auth=(user, password))
    nodes: dict[str, dict] = {}
    rels: list[dict] = []
    with driver.session(database=database) as session:
        result = session.run(
            """
            MATCH (n:Note)
            WHERE n.path IN $paths
            OPTIONAL MATCH (n)-[r]-(m)
            RETURN n.path AS source_path,
                   n.title AS source_title,
                   type(r) AS rel_type,
                   m.path AS target_path,
                   coalesce(m.title, m.stem, m.name, m.path) AS target_title,
                   labels(m) AS target_labels
            LIMIT $limit
            """,
            paths=note_paths,
            limit=limit,
        )
        for rec in result:
            src_path = rec["source_path"]
            nodes.setdefault(src_path, {"path": src_path, "title": rec["source_title"] or src_path})
            if rec["target_path"]:
                tgt_path = rec["target_path"]
                nodes.setdefault(tgt_path, {
                    "path": tgt_path,
                    "title": rec["target_title"] or tgt_path,
                    "labels": rec["target_labels"],
                })
                rels.append({
                    "source": src_path,
                    "target": tgt_path,
                    "type": rec["rel_type"],
                })
    driver.close()
    return {"nodes": list(nodes.values()), "relationships": rels}


def render_answer(question: str, hits: list[dict], graph: dict) -> str:
    lines = [
        "---",
        f"title: GraphRAG answer - {question[:48]}",
        "type: answer",
        f"created: {proto.current_date()}",
        "tags: [graphrag, pgvector, neo4j, obsidian]",
        "---",
        "",
        f"# {question}",
        "",
        "## Semantic search",
    ]
    if not hits:
        lines.append("- 결과가 없습니다.")
    else:
        for hit in hits:
            excerpt = " ".join(str(hit["text"]).split())[:320]
            lines.extend([
                f"- **{hit['note_title']}**  ",
                f"  score={hit['score']:.3f} | chunk={hit['chunk_index']} | `{hit['note_path']}`  ",
                f"  {excerpt}",
            ])
    lines.extend(["", "## Graph expansion"])
    if not graph.get("nodes"):
        lines.append("- 그래프 확장 결과가 없습니다.")
    else:
        for node in graph["nodes"]:
            lines.append(f"- `{node['path']}` → {node.get('title', node['path'])}")
    if graph.get("relationships"):
        lines.append("")
        lines.append("### Relationships")
        for rel in graph["relationships"]:
            lines.append(f"- `{rel['source']}` -[{rel['type']}]-> `{rel['target']}`")
    return "\n".join(lines) + "\n"


def write_note_if_requested(markdown: str, output_note: str) -> None:
    if not output_note:
        return
    out = Path(output_note).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(markdown, encoding="utf-8")
    print(f"Wrote note: {out}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Unified Obsidian RAG hub: Gemini embeddings + pgvector + Neo4j")
    sub = p.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--vault", default=os.getenv("OBSIDIAN_VAULT_PATH") or os.getenv("WIKI_PATH") or str(DEFAULT_VAULT))
    common.add_argument("--host", default=os.getenv("PGHOST", "127.0.0.1"))
    common.add_argument("--port", type=int, default=int(os.getenv("PGPORT", "5432")))
    common.add_argument("--user", default=os.getenv("PGUSER", getuser()))
    common.add_argument("--password", default=os.getenv("PGPASSWORD", ""))
    common.add_argument("--database", default=os.getenv("PGDATABASE", DEFAULT_DB))
    common.add_argument("--cache-database", default=os.getenv("PGVECTOR_CACHE_DATABASE", "osba_workspace"))
    common.add_argument("--dim", type=int, default=DEFAULT_DIM)
    common.add_argument("--include-folders", default=os.getenv("PGVECTOR_INCLUDE_FOLDERS", ""))
    common.add_argument("--exclude-folders", default=os.getenv("PGVECTOR_EXCLUDE_FOLDERS", ""))
    common.add_argument("--limit-notes", type=int, default=os.getenv("PGVECTOR_LIMIT_NOTES"))
    common.add_argument("--max-chars", type=int, default=1400)
    common.add_argument("--overlap-chars", type=int, default=200)
    common.add_argument("--embed-provider", choices=["gemini", "hash"], default=os.getenv("PGVECTOR_EMBED_PROVIDER", "gemini"))
    common.add_argument("--output-note", default="")

    sync = sub.add_parser("sync", parents=[common], help="Sync vault into pgvector and Neo4j")
    sync.add_argument("--neo4j-uri", default=os.getenv("NEO4J_URI", "bolt://localhost:7687"))
    sync.add_argument("--neo4j-user", default=os.getenv("NEO4J_USER", "neo4j"))
    sync.add_argument("--neo4j-password", default=os.getenv("NEO4J_PASSWORD", ""))
    sync.add_argument("--neo4j-database", default=os.getenv("NEO4J_DATABASE", "neo4j"))
    sync.add_argument("--neo4j-clear", action="store_true")
    sync.add_argument("--skip-neo4j", action="store_true")
    sync.add_argument("--dry-run", action="store_true")

    ask = sub.add_parser("ask", parents=[common], help="Semantic search + Neo4j graph expansion")
    ask.add_argument("--question", required=True)
    ask.add_argument("--top-k", type=int, default=5)
    ask.add_argument("--note-path", default="")
    ask.add_argument("--neo4j-uri", default=os.getenv("NEO4J_URI", "bolt://localhost:7687"))
    ask.add_argument("--neo4j-user", default=os.getenv("NEO4J_USER", "neo4j"))
    ask.add_argument("--neo4j-password", default=os.getenv("NEO4J_PASSWORD", ""))
    ask.add_argument("--neo4j-database", default=os.getenv("NEO4J_DATABASE", "neo4j"))
    ask.add_argument("--graph-limit", type=int, default=120)

    status = sub.add_parser("status", parents=[common], help="Print graph/data progress summary")
    status.add_argument("--neo4j-uri", default=os.getenv("NEO4J_URI", "bolt://localhost:7687"))
    status.add_argument("--neo4j-user", default=os.getenv("NEO4J_USER", "neo4j"))
    status.add_argument("--neo4j-password", default=os.getenv("NEO4J_PASSWORD", ""))
    status.add_argument("--neo4j-database", default=os.getenv("NEO4J_DATABASE", "neo4j"))

    args = p.parse_args(argv)
    vault = Path(args.vault).expanduser().resolve()
    if not vault.exists():
        raise SystemExit(f"Vault path does not exist: {vault}")

    api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    include_folders = parse_csv_set(args.include_folders)
    exclude_folders = parse_csv_set(args.exclude_folders)

    if args.cmd == "sync":
        summary = sync_pgvector(
            vault=vault,
            include_folders=include_folders,
            exclude_folders=exclude_folders,
            limit_notes=int(args.limit_notes) if args.limit_notes not in (None, "", 0) else None,
            max_chars=args.max_chars,
            overlap_chars=args.overlap_chars,
            dim=args.dim,
            host=args.host,
            port=args.port,
            user=args.user,
            password=args.password,
            dbname=args.database,
            dry_run=args.dry_run,
            embed_provider=args.embed_provider,
            api_key=api_key,
            cache_dbname=args.cache_database,
        )
        if not args.skip_neo4j and not args.dry_run:
            try:
                neo_summary = sync_neo4j(
                    vault=vault,
                    uri=args.neo4j_uri,
                    user=args.neo4j_user,
                    password=args.neo4j_password,
                    database=args.neo4j_database,
                    clear=args.neo4j_clear,
                    limit_notes=int(args.limit_notes) if args.limit_notes not in (None, "", 0) else None,
                )
                summary["neo4j"] = neo_summary
            except Exception as exc:
                summary["neo4j_error"] = str(exc)
                print(f"[warn] Neo4j sync skipped: {exc}", file=sys.stderr)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        if args.output_note:
            write_note_if_requested(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", args.output_note)
        return 0

    if args.cmd == "ask":
        hits = search_pgvector(
            question=args.question,
            host=args.host,
            port=args.port,
            user=args.user,
            password=args.password,
            dbname=args.database,
            top_k=args.top_k,
            dim=args.dim,
            include_folders=include_folders,
            note_path=args.note_path or None,
            embed_provider=args.embed_provider,
            api_key=api_key,
        )
        graph = {"nodes": [], "relationships": []}
        if hits:
            note_paths = []
            seen = set()
            for hit in hits:
                if hit["note_path"] not in seen:
                    note_paths.append(hit["note_path"])
                    seen.add(hit["note_path"])
            try:
                graph = expand_graph(
                    note_paths=note_paths,
                    uri=args.neo4j_uri,
                    user=args.neo4j_user,
                    password=args.neo4j_password,
                    database=args.neo4j_database,
                    limit=args.graph_limit,
                )
            except Exception as exc:
                print(f"[warn] Neo4j graph expansion skipped: {exc}", file=sys.stderr)
        md = render_answer(args.question, hits, graph)
        print(json.dumps({"question": args.question, "hits": hits, "graph": graph}, ensure_ascii=False, indent=2))
        print(md)
        if args.output_note:
            write_note_if_requested(md, args.output_note)
        return 0

    if args.cmd == "status":
        notes = proto.collect_notes(vault, include_folders=include_folders, exclude_folders=exclude_folders, limit_notes=int(args.limit_notes) if args.limit_notes not in (None, "", 0) else None)
        graph_summary = neo_sync.build_summary(notes)
        graph_summary["vault"] = str(vault)
        graph_summary["pgvector_dim"] = args.dim
        graph_summary["embed_provider"] = args.embed_provider
        graph_summary["google_api_key_present"] = bool(api_key)
        print(json.dumps(graph_summary, ensure_ascii=False, indent=2))
        return 0

    raise SystemExit(f"Unknown command: {args.cmd}")


if __name__ == "__main__":
    raise SystemExit(main())
