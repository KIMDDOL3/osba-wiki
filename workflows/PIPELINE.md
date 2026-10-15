# Workflows — 수집 → 클리핑 → 공부노트 → 임베딩 → 발행

이 문서는 osba-wiki 스크립트들을 조합해 개인 지식 파이프라인을 구축하는 방법론을 설명합니다.
This document describes the methodology for combining osba-wiki scripts into a personal knowledge pipeline.

## 전체 파이프라인 (Overall pipeline)

```
 ┌──────────┐   ┌──────────┐   ┌───────────┐   ┌───────────┐   ┌──────────┐
 │  수집     │ → │  클리핑   │ → │  공부노트  │ → │  임베딩    │ → │  발행     │
 │ Ingest   │   │ Clip     │   │ Study note│   │ Embed     │   │ Publish  │
 └──────────┘   └──────────┘   └───────────┘   └───────────┘   └──────────┘
 rss_*          trend_capture   apply_study_    obsidian_       rss_daily_
 ai_news_       youtube_clip    note_tags       pgvector_sync   digest
 surfit_        _router         agent-os-auto   wiki_search     notion-sync/
 _rss_bridge                    _promote.sh     _*.py           sync_to_notion
```

## 1. 수집 (Ingest)

**스크립트**: `rss_gemini_wiki_note.py`, `rss_articles_to_korean_wiki.py`, `ai_news_pipeline.py`, `surfit_rss_bridge.py`

- RSS/Atom 피드를 수집해 Obsidian Markdown 노트로 변환합니다.
- LLM 키(`GEMINI_API_KEY`, `OPENAI_API_KEY`)가 있으면 한국어 요약을 생성하고, 없으면 추출식 큐레이션으로 폴백합니다.
- `surfit_rss_bridge.py`는 Surfit JSON 피드를 RSS 2.0 XML로 변환하는 브리지입니다.
- 피드 목록은 `configs/*.json` 설정 파일로 관리합니다 (vault 의 `configs/` 폴더).

실행 예:
```bash
python3 scripts/rss_gemini_wiki_note.py --config "$VAULT/configs/rss_gemini_wiki.json"
python3 scripts/ai_news_pipeline.py
```

## 2. 클리핑 (Clip / Route)

**스크립트**: `trend_capture_router.py`, `youtube_clip_router.py`

- 웹페이지·YouTube URL 등 입력 소스를 분류해 vault 의 적절한 폴더(`raw/clippings/` 등)로 라우팅합니다.
- 원문은 `raw/` 아래에 보존하고, 가공 노트는 `concepts/`, `comparisons/`, `queries/`로 분리합니다.

## 3. 공부노트 (Study notes)

**스크립트**: `apply_study_note_tags.py`, `agent-os-auto-promote.sh`, `meeting_to_minutes.py`

- `apply_study_note_tags.py`: `concepts/`, `comparisons/`, `queries/` 노트에 `#공부노트` 태그를 부착합니다.
- `agent-os-auto-promote.sh`: `queries/`에서 비교·개념 큐를 가진 노트를 찾아 상위 폴더로 승격할 후보를 리포트합니다 (`--apply`로 실제 이동).
- `meeting_to_minutes.py`: 회의 음성/전사 텍스트를 회의록 노트로 변환합니다 (선택: 로컬 Whisper).

```bash
python3 scripts/apply_study_note_tags.py
bash scripts/agent-os-auto-promote.sh --dry-run   # 후보 확인
bash scripts/agent-os-auto-promote.sh --apply     # 실제 승격
```

## 4. 임베딩 (Embed & index)

**스크립트**: `obsidian_pgvector_sync.py`, `obsidian_rag_hub.py`, `obsidian_pgvector_search.py`, `wiki_search.py`, `wiki_graphrag_visualize.py`, `wiki_graphrag_atlas.py`, `graphify_atlas_dashboard.py`

- Postgres + pgvector에 노트를 청크·임베딩해 적재합니다 (`obsidian_rag` DB 기본값).
- 임베딩 제공자는 Gemini(GOOGLE_API_KEY) 또는 로컬 해시 폴백(`--embed-provider hash`)을 지원합니다.
- `wiki_search.py`는 의미 검색 + (선택) Neo4j 1~2hop 그래프 확장으로 질문에 답하고 결과 노트를 저장합니다.
- `wiki_graphrag_visualize.py` / `wiki_graphrag_atlas.py`는 vault 전체를 인터랙티브 HTML 그래프로 시각화합니다.

```bash
docker compose up -d postgres                 # pgvector 준비
python3 scripts/obsidian_rag_hub.py sync      # 동기화 + 임베딩
python3 scripts/wiki_search.py --question "질문" --graph
python3 scripts/wiki_graphrag_atlas.py        # 오프라인 아틀라스 생성
```

## 5. 발행 (Publish / Deliver)

**스크립트**: `rss_daily_digest.py`, `notion-sync/sync_to_notion.py`

- `rss_daily_digest.py`: 오늘 생성된 큐레이션 노트를 하나의 다이제스트로 묶어 콘솔(→ cron 메일/Telegram)과 Discord로 전달합니다.
- `notion-sync/sync_to_notion.py`: vault 노트를 Notion 데이터베이스로 업서트합니다. 토큰은 `NOTION_TOKEN` 환경변수 또는 `notion-sync/token` 파일(커밋 금지)로 공급합니다.

```bash
python3 scripts/rss_daily_digest.py
python3 scripts/notion-sync/sync_to_notion.py
```

## Cron 구성 예 (Example cron)

```cron
# 08:00 RSS 수집·요약
0 8 * * * cd /path/to/osba-wiki && python3 scripts/rss_gemini_wiki_note.py
# 08:20 AI 뉴스 파이프라인
20 8 * * * cd /path/to/osba-wiki && python3 scripts/ai_news_pipeline.py
# 09:40 다이제스트 발행
40 9 * * * cd /path/to/osba-wiki && python3 scripts/rss_daily_digest.py
# 00:00 Notion 동기화
0 0 * * * /path/to/osba-wiki/scripts/notion-sync/run.sh
```

## 보안 원칙 (Security principles)

1. 모든 키는 환경변수로만 공급합니다. `.env` 파일은 절대 커밋하지 않습니다.
2. 스크립트는 키를 읽기만 하고 로그·노트에 출력하지 않습니다.
3. 클리핑 원문(`raw/clippings/`)과 개인 노트는 이 저장소에 포함되지 않습니다 — 스크립트만 발행됩니다.
4. 개인 경로는 `OBSIDIAN_VAULT_PATH` / `WIKI_PATH` 환경변수로 치환되어 있습니다.
