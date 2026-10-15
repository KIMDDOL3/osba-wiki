#!/usr/bin/env python3
"""Sync Obsidian Markdown notes into Postgres + pgvector.

This is a practical prototype for semantic search experiments:
- parses Obsidian notes from the vault
- chunks them
- builds deterministic hash embeddings for testing plumbing
- stores notes/chunks in Postgres with a pgvector column

Dependencies (recommended venv):
  python (with psycopg, pgvector installed)
  pip install psycopg[binary] pgvector python-dotenv

Usage examples:
  python obsidian_pgvector_sync.py --dry-run --limit-notes 20 --include-folders queries,concepts
  python obsidian_pgvector_sync.py --database obsidian_rag --port 5433
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from dataclasses import asdict
from getpass import getuser
from pathlib import Path
from typing import Iterable

from psycopg import connect
from psycopg.sql import Identifier, SQL
from psycopg.types.json import Jsonb
from pgvector.psycopg import register_vector

SCRIPT_DIR = Path(__file__).resolve().parent
PROTOTYPE_PATH = Path(os.getenv("RAG_PROTOTYPE_SCRIPT", str(Path.home() / ".hermes/scripts/obsidian_vector_rag_prototype.py")))


def load_proto():
    spec = importlib.util.spec_from_file_location("obsidian_vector_rag_prototype", PROTOTYPE_PATH)
    if spec is None or spec.loader is None:
        raise SystemExit(f"Cannot load prototype module: {PROTOTYPE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


proto = load_proto()

DEFAULT_VAULT = Path.home() / "Documents/wiki"
DEFAULT_DB = "obsidian_rag"
DEFAULT_DIM = 256


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


def ensure_schema(conn, dim: int) -> None:
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
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


def run_sync(*, vault: Path, include_folders: set[str] | None, exclude_folders: set[str] | None,
             limit_notes: int | None, max_chars: int, overlap_chars: int, dim: int,
             host: str, port: int, user: str, password: str, dbname: str, dry_run: bool,
             provider: str = "hash", model: str | None = None) -> dict:
    notes = proto.collect_notes(vault, include_folders=include_folders, exclude_folders=exclude_folders, limit_notes=limit_notes)
    chunks, _idf = proto.make_chunks(notes, provider=provider, dim=dim, model=model or proto.DEFAULT_OPENAI_EMBED_MODEL, max_chars=max_chars, overlap_chars=overlap_chars)

    summary = {
        "vault": str(vault),
        "database": dbname,
        "provider": provider,
        "notes": len(notes),
        "chunks": len(chunks),
        "folders": sorted({n.folder or "." for n in notes}),
        "dim": dim,
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync Obsidian notes into Postgres + pgvector")
    parser.add_argument("--vault", default=os.getenv("OBSIDIAN_VAULT_PATH") or os.getenv("WIKI_PATH") or str(DEFAULT_VAULT))
    parser.add_argument("--host", default=os.getenv("PGHOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PGPORT", "5433")))
    parser.add_argument("--user", default=os.getenv("PGUSER", getuser()))
    parser.add_argument("--password", default=os.getenv("PGPASSWORD", ""))
    parser.add_argument("--database", default=os.getenv("PGDATABASE", DEFAULT_DB))
    parser.add_argument("--include-folders", default=os.getenv("PGVECTOR_INCLUDE_FOLDERS", ""))
    parser.add_argument("--exclude-folders", default=os.getenv("PGVECTOR_EXCLUDE_FOLDERS", ""))
    parser.add_argument("--limit-notes", type=int, default=os.getenv("PGVECTOR_LIMIT_NOTES"))
    parser.add_argument("--max-chars", type=int, default=1400)
    parser.add_argument("--overlap-chars", type=int, default=200)
    parser.add_argument("--dim", type=int, default=DEFAULT_DIM)
    parser.add_argument("--provider", choices=["hash", "openai", "openrouter"], default=os.getenv("PGVECTOR_EMBED_PROVIDER", "hash").lower(), help="Embedding provider for pgvector chunks")
    parser.add_argument("--model", default=os.getenv("OPENROUTER_EMBED_MODEL"), help="Override embedding model (openrouter)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    vault = Path(args.vault).expanduser().resolve()
    if not vault.exists():
        raise SystemExit(f"Vault path does not exist: {vault}")

    summary = run_sync(
        vault=vault,
        include_folders=parse_csv_set(args.include_folders),
        exclude_folders=parse_csv_set(args.exclude_folders),
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
        provider=args.provider,
        model=args.model,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
