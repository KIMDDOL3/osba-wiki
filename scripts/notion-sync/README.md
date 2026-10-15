---
title: Notion 자정 동기화
created: 2026-09-19
type: query
memory_tier: concept
tags: [notion, 동기화, cron, 자동화, wiki]
---

# vault → Notion 자정 동기화

매일 **00:00**에 vault의 새 노트를 노션 두 DB로 올린다.

| vault | → | 노션 DB |
|---|---|---|
| `queries/RSS 기사 큐레이션/**` | → | **AI 뉴스 아카이브** |
| `concepts/`, `comparisons/`, `queries/` (RSS 제외) | → | **Wiki Mirror** |

동기화 키는 둘 다 `Vault Path`다. 같은 경로면 갱신, 없으면 신규 — 몇 번을 돌려도 행이 불어나지 않는다.

---

## ⚠️ 시작 전 필수: 토큰 한 번 넣기

스크립트는 **claude.ai 노션 커넥터를 쓸 수 없다.** 그건 대화 세션에 묶인 OAuth라 크론에서 접근이 안 된다. 별도의 integration 토큰이 필요하다.

1. https://www.notion.so/my-integrations → **New integration** 생성 (Internal)
2. Capabilities에서 **Read content / Update content / Insert content** 체크
3. 발급된 `ntn_...` 토큰 복사
4. 터미널에서:

```bash
cd ~/Documents/wiki/scripts/notion-sync
printf '%s' 'ntn_여기에_토큰' > token
chmod 600 token
```

5. **두 DB에 integration을 초대한다** (이걸 빼먹으면 404가 난다):
   - 노션에서 `AI 뉴스 아카이브` 열기 → 우측 상단 `...` → **연결** → 만든 integration 선택
   - `Wiki Mirror`도 동일하게

> `token` 파일은 git에 올리지 말 것. 이 디렉터리에 `.gitignore`를 같이 뒀다.

---

## 첫 실행 — 과거분 백필

크론은 오늘 이후만 본다. 과거 노트를 한 번에 올리려면:

```bash
cd ~/Documents/wiki/scripts/notion-sync
./run.sh --full          # 뉴스 750 + 미러 731 = 약 1,481건, 8~10분
tail -f sync.log         # 다른 탭에서 진행 상황 보기
```

범위를 좁히려면:

```bash
./run.sh --since 2026-09-01      # 9월 것만
./run.sh --only news --full      # 뉴스만
./run.sh --dry-run --full        # 노션에 쓰지 않고 계획만
```

---

## 크론 등록 상태

```
0 0 * * * /path/to/osba-wiki/scripts/notion-sync/run.sh
```

확인: `crontab -l` / 해제: `crontab -r`

### macOS 주의 — 전체 디스크 접근 권한

최신 macOS에서 cron은 기본적으로 `~/Documents`를 못 읽는다. 자정에 아무 일도 안 일어나고 로그도 안 생기면 십중팔구 이것이다.

**시스템 설정 → 개인정보 보호 및 보안 → 전체 디스크 접근 권한**에서 `+` → `Cmd+Shift+G` → `/usr/sbin/cron` 추가.

launchd로 바꾸는 편이 macOS에서는 더 안정적이다. 필요하면 plist로 전환할 수 있다.

---

## 동작 메모

- **증분 기준**: frontmatter `created`, 없으면 **파일 mtime**. mtime 폴백이 없으면 `created` 없는 노트가 매번 전부 재전송된다.
- **하루 겹침**: 매 실행 후 `state.json`에 그날 날짜를 적고 다음 실행은 그 날짜부터 다시 본다. 자정 직전에 추가된 노트를 놓치지 않으려는 의도적 중복이다(upsert라 안전).
- **레이트리밋**: 요청 사이 0.34초. 429/5xx는 지수 백오프 4회 재시도.
- **동시 실행 방지**: `.lock` 파일. 전량 백필이 도는 중에 자정이 와도 겹치지 않는다.
- **부분 실패 허용**: 한 건이 실패해도 나머지를 계속 처리하고, 실패 건수만큼 exit code 1.
- **로그**: `sync.log`, 최근 2000줄 유지.

## 알려진 한계

- `Vault Path`가 키라서 **vault에서 파일을 옮기면 노션에 중복 행이 생긴다.** 이동이 잦아지면 frontmatter에 불변 `uid`를 넣고 키를 바꾸는 편이 낫다.
- `Area` / `Tier` / `피드` 같은 select 속성은 **노션에 이미 있는 옵션만** 쓴다. 새 피드가 추가되면 노션에서 옵션을 먼저 만들어야 값이 들어간다.
- 본문은 올리지 않는다. 속성과 요약만 동기화한다. 본문 전문이 필요하면 건별로 처리한다.
- vault에서 노트를 **삭제해도 노션 행은 남는다.** 의도한 동작이다(미러는 지우지 않는다).
