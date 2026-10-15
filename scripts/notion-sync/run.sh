#!/bin/zsh
# vault → Notion 자정 동기화 래퍼.
# cron 은 로그인 셸의 PATH/환경을 물려받지 않으므로 여기서 전부 고정한다.

set -u

export PATH="/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:$HOME/.local/bin"
export WIKI_VAULT="${WIKI_VAULT:-$HOME/Documents/wiki}"
export LANG="ko_KR.UTF-8"
export PYTHONIOENCODING="utf-8"

HERE="$(cd "$(dirname "$0")" && pwd)"
LOCK="$HERE/.lock"

# 앞선 실행이 아직 돌고 있으면 건너뛴다 (전량 백필은 8분 넘게 걸린다).
if [ -e "$LOCK" ]; then
  if kill -0 "$(cat "$LOCK" 2>/dev/null)" 2>/dev/null; then
    echo "$(date '+%F %T')  이전 실행이 진행 중이라 건너뜀" >> "$HERE/sync.log"
    exit 0
  fi
  rm -f "$LOCK"
fi
echo $$ > "$LOCK"
trap 'rm -f "$LOCK"' EXIT INT TERM

# zsh 에서 `status` 는 $? 의 읽기 전용 별칭이다. 절대 대입하면 안 된다.
/usr/bin/python3 "$HERE/sync_to_notion.py" "$@" >> "$HERE/sync.log" 2>&1
rc=$?

# 로그가 무한정 자라지 않게 최근 2000줄만 남긴다.
if [ -f "$HERE/sync.log" ]; then
  tail -n 2000 "$HERE/sync.log" > "$HERE/sync.log.tmp" && mv "$HERE/sync.log.tmp" "$HERE/sync.log"
fi

exit $rc
