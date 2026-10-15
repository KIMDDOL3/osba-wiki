#!/usr/bin/env python3
"""9:40am combined digest delivery for both RSS curation pipelines.

Reads today's already-generated notes from queries/RSS 기사 큐레이션/ (written
by both rss_gemini_wiki_note.py and rss_articles_to_korean_wiki.py — they
share the same output folder) and delivers one readable digest to Telegram
(via cron stdout capture) and Discord (direct bot API call).

Does NOT re-fetch RSS or call any LLM — pure delivery step, meant to run
after both generation jobs (08:00 / 08:20) have already produced today's
notes.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rss_gemini_wiki_note import build_digest, send_discord_message, DISCORD_MAIN_CHANNEL_ID  # noqa: E402

VAULT = Path(os.environ.get("OBSIDIAN_VAULT_PATH") or os.environ.get("WIKI_PATH") or str(Path.home() / "Documents/wiki")).expanduser()
OUT_DIR = VAULT / "queries" / "RSS 기사 큐레이션"


def main() -> int:
    date_s = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).date().isoformat()
    note_paths = sorted(OUT_DIR.glob(f"*/{date_s}*.md"))
    if not note_paths:
        print(f"오늘({date_s}) 생성된 RSS 큐레이션 노트가 없습니다.")
        return 0
    digest = build_digest(note_paths, date_s)
    print(digest)
    send_discord_message(DISCORD_MAIN_CHANNEL_ID, digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
