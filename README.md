# osba-wiki

**한국어** | [English](#english)

Obsidian 볼트를 중심으로 한 개인 지식 파이프라인 도구 모음 — RSS 수집·한국어 큐레이션, pgvector 의미 검색, GraphRAG 시각화, Notion/Discord 발행.

> ⚠️ 이 저장소는 **스크립트만** 포함합니다. 클리핑 원문·개인 노트·API 키는 포함되지 않습니다.

## 설치 (3줄)

```bash
git clone https://github.com/KIMDDOL3/osba-wiki.git && cd osba-wiki
cp .env.example .env && $EDITOR .env   # 최소한 OBSIDIAN_VAULT_PATH 만 설정
pip install -e ".[graph]" && docker compose up -d postgres
```

## 아키텍처

```
                        ┌─────────────────────────────────────────┐
   RSS / Surfit ──────▶ │  수집 Ingest                            │
   YouTube / Web ─────▶ │  rss_gemini_wiki_note · ai_news_pipeline│
                        │  surfit_rss_bridge · *_router           │
                        └────────────────┬────────────────────────┘
                                         ▼
                        ┌─────────────────────────────────────────┐
                        │  Obsidian Vault (Markdown)              │
                        │  raw/ · concepts/ · comparisons/ ·      │
                        │  queries/                               │
                        └──────┬──────────────────┬───────────────┘
                               ▼                  ▼
              ┌────────────────────────┐  ┌──────────────────────┐
              │ Postgres + pgvector    │  │ Neo4j (선택 graph)    │
              │ 의미 검색 · RAG        │  │ 그래프 확장 · 시각화   │
              │ wiki_search · rag_hub  │  │ graphrag_atlas       │
              └────────────────────────┘  └──────────────────────┘
                               │                  │
                               ▼                  ▼
                        ┌─────────────────────────────────────────┐
                        │  발행 Publish                            │
                        │  rss_daily_digest → Discord/Telegram    │
                        │  notion-sync/sync_to_notion → Notion    │
                        └─────────────────────────────────────────┘
```

## 구성

| 경로 | 설명 |
|---|---|
| `scripts/` | 파이프라인 스크립트 (Python 3.10+, 대부분 표준 라이브러리만 사용) |
| `scripts/notion-sync/` | vault → Notion 데이터베이스 동기화 |
| `workflows/PIPELINE.md` | 수집→클리핑→공부노트→임베딩→발행 방법론 |
| `docker-compose.yml` | Postgres+pgvector (기본) / Neo4j (`--profile graph`) |

주요 스크립트: `rss_gemini_wiki_note.py`(RSS→위키노트), `ai_news_pipeline.py`(AI뉴스 큐레이션), `obsidian_rag_hub.py`(pgvector+Neo4j RAG 허브), `wiki_search.py`(의미검색 CLI), `obsidian_pgvector_sync.py`(임베딩 동기화), `wiki_graphrag_atlas.py`(오프라인 그래프 아틀라스), `meeting_to_minutes.py`(회의록 자동화).

## 환경변수

`.env.example` 참고. 모든 키는 선택 사항이며, LLM 키가 없으면 추출식 폴백으로 동작합니다.
볼트 위치는 `OBSIDIAN_VAULT_PATH`(또는 `WIKI_PATH`) 하나로 제어합니다.

## 라이선스

[Apache License 2.0](LICENSE) — [NOTICE](NOTICE) 참고.

---

<a name="english"></a>
# osba-wiki (English)

A collection of personal knowledge-pipeline tools centered on an Obsidian vault — RSS ingestion with Korean curation, pgvector semantic search, GraphRAG visualization, and publishing to Notion/Discord.

> ⚠️ This repository contains **scripts only**. No clippings, personal notes, or API keys are included.

## Install (3 lines)

```bash
git clone https://github.com/KIMDDOL3/osba-wiki.git && cd osba-wiki
cp .env.example .env && $EDITOR .env   # at minimum, set OBSIDIAN_VAULT_PATH
pip install -e ".[graph]" && docker compose up -d postgres
```

## Architecture

```
  RSS / YouTube / Web
        │  ingest (rss_gemini_wiki_note, ai_news_pipeline, routers)
        ▼
  Obsidian Vault (Markdown: raw/ concepts/ comparisons/ queries/)
        │                         │
        ▼                         ▼
  Postgres + pgvector        Neo4j (optional)
  semantic search / RAG      graph expansion + atlas visualization
        │                         │
        └───────────┬─────────────┘
                    ▼
  publish: rss_daily_digest → Discord/Telegram
           notion-sync → Notion database
```

## Layout

| Path | Description |
|---|---|
| `scripts/` | Pipeline scripts (Python 3.10+, mostly stdlib-only) |
| `scripts/notion-sync/` | Vault → Notion database sync |
| `workflows/PIPELINE.md` | Ingest → clip → study-note → embed → publish methodology |
| `docker-compose.yml` | Postgres+pgvector (default) / Neo4j (`--profile graph`) |

Key scripts: `rss_gemini_wiki_note.py` (RSS→wiki notes), `ai_news_pipeline.py` (AI news curation), `obsidian_rag_hub.py` (pgvector+Neo4j RAG hub), `wiki_search.py` (semantic search CLI), `obsidian_pgvector_sync.py` (embedding sync), `wiki_graphrag_atlas.py` (offline graph atlas), `meeting_to_minutes.py` (meeting minutes automation).

## Environment

See `.env.example`. All keys are optional — pipelines fall back to extractive curation and local hash embeddings without LLM keys. The vault location is controlled by `OBSIDIAN_VAULT_PATH` (or `WIKI_PATH`).

## Security

- Keys are supplied via environment variables only; `.env` is git-ignored.
- Scripts read keys but never log or store them.
- Only scripts are published here — never vault content.

## License

[Apache License 2.0](LICENSE) — see [NOTICE](NOTICE).
