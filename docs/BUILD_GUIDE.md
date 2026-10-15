# BUILD GUIDE — 나만의 지식 파이프라인 구축기 (단계별)

> 이 문서는 osba-wiki를 처음부터 따라 하며 **개인 지식 → 콘텐츠 자동화 파이프라인**을 구축하는 가이드다.
> 각 단계는 독립적으로 커밋 가능한 마일스톤이며, 끝날 때마다 검증 커맨드로 동작을 확인한다.
> 전체를 다 완성하지 않아도 단계 1~4만으로 "수집→노트화"는 돌아간다.

**목표 아키텍처:**

```
수집(Ingest) → 클리핑(Clip) → 공부노트(Note) → 임베딩(Embed) → 발행(Publish)
     └───────────────── Obsidian Vault (Markdown) ─────────────────┘
```

**사전 요구사항:**
- Python 3.10+, Docker(선택 — pgvector/Neo4j용), Obsidian(보기 편하게)
- LLM API 키 없어도 시작 가능 (추출식 폴백 동작) — 단, 한국어 요약 품질은 키가 있을 때 가장 좋다

**소요 시간 감각:** 단계 1~4는 하루. 단계 5~7은 반나절. 단계 8~10은 운영하며 익힌다.

---

## 단계 1 — 볼트 골격 만들기 (폴더가 곧 DB)

가장 중요한 단계. 도구보다 구조가 먼저다.

```bash
mkdir -p my-vault/{raw,concepts,comparisons,queries,configs}
cd my-vault
```

**4계층 역할:**

| 폴더 | 역할 | 수정 규칙 |
|---|---|---|
| `raw/` | 원문 보존 (클리핑, RSS 원문, 전사) | **불변 — 절대 수정 금지** |
| `queries/` | 질문 결과, 공부노트 (가공 1차) | 자유롭게 추가 |
| `concepts/` | 2개 이상 출처에서 반복된 개념 (승격) | 주기적 정리 |
| `comparisons/` | 둘 이상 개념의 비교 분석 | 주기적 정리 |

**SCHEMA.md 작성** — 볼트 루트에 규칙 파일을 만든다. 최소 3가지:

1. **승격 규칙**: `queries/` 노트가 2개 이상 출처와 연결되면 `concepts/`로 승격
2. **태그 목록**: 10~20개 상위 태그를 미리 정의 (자유 태그는 금지 — 태그가 쓰레기가 된다)
3. **frontmatter 필수 필드**: `title`, `created`, `type`, `tags`, `sources`

> 💡 이 패턴의 원조는 Andrej Karpathy의 [LLM Wiki gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f).
> 차이점: Karpathy 패턴은 에이전트가 파일을 직접 읽는 수동 구조고, 이 가이드는 여기에 벡터 검색·그래프·크론을 얹어 무인 운영까지 간다.

**검증:**
```bash
ls raw concepts comparisons queries configs SCHEMA.md   # 전부 존재해야 함
```

---

## 단계 2 — 수집 자동화 (RSS → 한국어 노트)

`osba-wiki` 설치:

```bash
git clone https://github.com/KIMDDOL3/osba-wiki.git && cd osba-wiki
cp .env.example .env && $EDITOR .env   # OBSIDIAN_VAULT_PATH=위에서 만든 my-vault 경로
pip install -e .
```

RSS 피드 설정 (`my-vault/configs/rss_gemini_wiki.json`):

```json
{
  "feeds": [
    {"url": "https://example.com/feed.xml", "tag": "예시소스"}
  ]
}
```

실행:

```bash
python3 scripts/rss_gemini_wiki_note.py --config "$VAULT/configs/rss_gemini_wiki.json"
python3 scripts/ai_news_pipeline.py        # AI 뉴스 전용 큐레이션
```

- `GEMINI_API_KEY` 또는 `OPENAI_API_KEY`가 `.env`에 있으면 한국어 요약이 생성된다
- 키가 없어도 **추출식 폴백**으로 노트가 만들어진다 — 진입장벽 없음
- `surfit_rss_bridge.py`는 Surfit JSON을 RSS로 바꿔주는 브리지 (국내 큐레이션 서비스용)

**검증:**
```bash
ls "$VAULT/raw" | head    # 오늘 날짜 노트가 생겼으면 성공
```

---

## 단계 3 — 클리핑 라우팅 (URL → raw/ 보존)

웹페이지·유튜브 URL을 노트로:

```bash
python3 scripts/trend_capture_router.py <URL>       # 웹페이지 → raw/clippings/
python3 scripts/youtube_clip_router.py <YouTubeURL> # 영상 → 전사 → raw/
```

라우터가 하는 일:
1. URL 형태 판별 (웹/유튜브/RSS)
2. 원문 전문을 `raw/`에 저장 (불변 계층)
3. frontmatter에 `source_url`, `ingested` 기록 — 나중에 재수집 시 변경 감지 가능

> ⚠️ **규칙**: 원문은 `raw/`에만. 요약·비평은 `queries/`에. 이 분리가 나중에 "내 노트가 원문을 벗어났는지" 대조 가능하게 한다 (환각 억제의 기초).

**검증:**
```bash
head -10 "$VAULT/raw/clippings/"최신파일.md   # frontmatter에 source_url 있으면 성공
```

---

## 단계 4 — 공부노트 태깅과 승격

쌓인 노트를 지식 계층으로 올린다:

```bash
python3 scripts/apply_study_note_tags.py            # concepts/comparisons/queries에 #공부노트 태그 부착
bash scripts/agent-os-auto-promote.sh --dry-run     # 승격 후보 미리보기
bash scripts/agent-os-auto-promote.sh --apply       # 실제 승격
```

**승격 판단 기준 (내가 운영하며 정한 것):**
- 같은 주제가 2개 이상 출처에서 반복되면 → `concepts/`로
- 두 개념을 비교하는 노트가 생기면 → `comparisons/`로
- 반복돼도 개인 기록이면 → `queries/`에 유지 (억지 승격 금지)

**운영 리듬**: 주 1회 `--dry-run` 돌려서 후보를 눈으로 확인 후 `--apply`. 완전 자동은 추천하지 않는다 — 승격은 지식 구조를 바꾸는 작업이라 사람 눈이 필요하다.

**검증:**
```bash
bash scripts/agent-os-auto-promote.sh --dry-run   # 후보 리스트가 뜨면 파이프라인이 산다
```

---

## 단계 5 — Postgres + pgvector 임베딩 (의미 검색의 시작)

여기서부터 "검색"이 "의미 검색"으로 바뀐다.

```bash
docker compose up -d postgres          # pgvector 포함 Postgres 기동
python3 scripts/obsidian_pgvector_sync.py            # 전체 노트 청크·임베딩
```

- 임베딩 제공자: `GOOGLE_API_KEY`가 있으면 Gemini, 없으면 `--embed-provider hash` 로컬 해시 폴백
- 해시 폴백은 **공짜지만 유사어 검색이 약하다** — 의미 검색 품질이 만족스지 않으면 API로 업그레이드

> ⚠️ **비용 경고 (내 실화)**: 초반에 2시간 주기로 전체 재호출 크론을 돌렸다가 한 달에 10만 원+이 소진됐다. 해결책은 단가가 아니라 **증분 동기화** — 바뀐 청크만 다시 임베딩하면 월 1만 원 이하로 유지된다. `obsidian_pgvector_sync.py`는 증분을 기본 동작으로 지원한다.

**검증:**
```bash
python3 -c "
import psycopg2, os
from dotenv import load_dotenv; load_dotenv()
conn = psycopg2.connect(os.environ['DATABASE_URL'])
cur = conn.cursor(); cur.execute('SELECT count(*) FROM obsidian_chunks')
print('chunks:', cur.fetchone()[0])"
```

---

## 단계 6 — 의미 검색 CLI

```bash
python3 scripts/wiki_search.py "하이브리드 RAG 장단점"
python3 scripts/obsidian_rag_hub.py --question "내 노트에서 RAG 실패 사례 찾아줘"
```

스코어 읽는 법:
- **0.8+**: 거의 같은 주제 — 답변 근거로 사용
- **0.6~0.8**: 관련 있음 — 보조 근거
- **0.4~0.6**: 주의. 관련 키워드만 맞은 경우가 많다
- **0.4 미만**: 버려라. 억지로 연결하면 그게 환각의 씨앗이다

**검증:** 검색 결과에 익숙한 노트 제목이 뜨면 성공.

---

## 단계 7 — 그래프 확장 (선택 — 관계형 질문용)

Neo4j는 필수가 아니다. 켜야 하는 때는 명확하다: **"A와 B의 관계"를 묻는 질문이 많아질 때.**

```bash
docker compose --profile graph up -d neo4j
pip install -e ".[graph]"
python3 scripts/wiki_graphrag_visualize.py    # 노트 링크 → 그래프 노드/엣지
python3 scripts/wiki_graphrag_atlas.py        # 오프라인 아틀라스 시각화
```

내 실측: 그래프 구조를 **갖추는 것**과 그래프 질의를 **쓰는 것**은 다른 문제였다. 의미 질문(벡터로 충분)과 관계 질문(그래프 필요)을 구분하는 라우팅이 있어야 그래프가 돈 값을 한다. 이 라우팅 없이 그래프를 상시 켜두면 느리고 비싸다.

**검증:**
```bash
python3 scripts/wiki_graphrag_visualize.py --stats   # 노드/엣지 카운트 출력
```

---

## 단계 8 — 발행 연결 (Discord · Telegram · Notion)

```bash
python3 scripts/rss_daily_digest.py        # 오늘 수집 요약 → Discord/Telegram
python3 scripts/notion-sync/sync_to_notion # 볼트 → Notion 데이터베이스 동기화
```

블로그(티스토리)·스레드 자동 발행은 브라우저 자동화가 필요해 이 저장소 범위 밖이다 — 대신 이 채널의 시즌 영상에서 파이프라인 전체를 공개한다.

---

## 단계 9 — 에이전트 연결 (질의 → 발행 루프)

에이전트(Claude Code, Hermes 등)에게 볼트를 도구로 주면, "질문 → 근거 검색 → 초안 작성" 루프가 만들어진다:

1. 에이전트 시스템 프롬프트에 볼트 경로 + `wiki_search.py` 실행법을 알려준다
2. 규칙을 심는다: **"답의 근거가 될 노트 경로를 반드시 인용할 것. 근거가 없으면 '노트에 없음'이라고 답할 것"**
3. 초안은 `queries/`에 저장하고, 사람이 검토 후 발행한다

이 "근거 인용 강제 + 거절 허용" 규칙 두 줄이, 에이전트를 그럴득한 환각 기계에서 내 지식으로 말하는 비서로 바꾼다.

---

## 단계 10 — 운영 리듬 (구축은 3일, 운영이 3개월)

- **주간**: `--dry-run` 승격 리뷰, 검색 실패 질문 기록 (golden set 재료)
- **월간**: 태그 감사 (SCHEMA 목록 밖 태그 정리), 고아 노트/깨진 링크 점검
- **측정 (가장 중요)**: 검색 성공률, 답변 근거율, 임베딩 비용을 숫자로 남긴다. 측정 없는 자동화는 "돌아간다"와 "제대로 돌아간다"를 구분할 수 없게 만든다 — 내 시즌 1의 가장 큰 교훈이다.

---

## 다음 읽을거리

- [`workflows/PIPELINE.md`](../workflows/PIPELINE.md) — 스크립트 조합 방법론
- [Karpathy's LLM Wiki gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) — 이 구조의 원조
- 라이선스: Apache-2.0 (스크립트 자유롭게 수정·배포 가능, 개인 노트·키는 저장소에 없음)
