#!/bin/zsh
# 효규가영 배포 스크립트 — 항상 이 스크립트로 배포한다 (수동 docker cp 금지).
#
# 하는 일:
#   ① audit_core + daily_audit_pipe.py를 두 컨테이너에 반입(__pycache__ 제거)
#   ② Function 콘텐츠를 webui.db에 갱신
#   ③ DEPLOY_STAMP 갱신 — Function이 다음 요청에서 모듈 캐시를 스스로 비움
#      (실장애 2026-07-15: 장수 프로세스의 sys.modules에 구 모듈 잔존 → _QA_TRIGGER 오류)
#   ④ pipelines(구버전) 재기동 — 이쪽은 스탬프 훅이 없으므로 재기동이 리로드 수단
#   ⑤ 로더 경유 스모크(비LLM)
set -e
cd "$(dirname "$0")/.."

echo "① 코드 반입"
for SPEC in "open-webui:/app/backend/data/daily_audit" "open-webui-pipelines:/app/pipelines"; do
  N="${SPEC%%:*}"; P="${SPEC#*:}"
  docker exec "$N" rm -rf "$P/audit_core"
  docker cp audit_core "$N:$P/audit_core"
  docker exec "$N" sh -c "find $P/audit_core -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null; true"
done
sed 's/self.name = "일상감사 멀티 에이전트"/self.name = "일상감사 멀티 에이전트(구)"/' \
  pipelines/daily_audit_pipe.py > /tmp/daily_audit_pipe_legacy.py
docker cp /tmp/daily_audit_pipe_legacy.py open-webui-pipelines:/app/pipelines/daily_audit_pipe.py
docker cp pipelines/daily_audit_pipe.py open-webui:/app/backend/data/daily_audit/daily_audit_pipe.py

echo "② Function 갱신(콘텐츠+메타) + ③ 스탬프"
docker cp functions/daily_audit_function.py open-webui:/tmp/daily_audit_function.py
docker exec open-webui python -c "
import json, re, sqlite3, time
src = open('/tmp/daily_audit_function.py', encoding='utf-8').read()
# 파일 머리말(frontmatter)에서 메타를 읽어 UI 소개·버전까지 동기화
# (기존 결함: 콘텐츠만 갱신되어 모델 소개가 최초 등록값 0.1.0에 고정돼 있었음)
fm = dict(re.findall(r'^(title|author|version|description): (.+)$', src.split('\"\"\"')[1], re.M))
db = sqlite3.connect('/app/backend/data/webui.db')
meta = json.loads(db.execute(\"SELECT meta FROM function WHERE id='daily_audit_agent'\").fetchone()[0])
meta['description'] = fm.get('description', meta.get('description', ''))
meta.setdefault('manifest', {}).update(fm)
db.execute('UPDATE function SET content=?, meta=?, updated_at=? WHERE id=?',
           (src, json.dumps(meta, ensure_ascii=False), int(time.time()), 'daily_audit_agent'))
db.commit()
open('/app/backend/data/daily_audit/DEPLOY_STAMP', 'w').write(str(time.time()))
print('function+meta+stamp OK (v' + fm.get('version','?') + ')')
"

echo "④ pipelines 재기동"
docker restart open-webui-pipelines > /dev/null

echo "⑤ 스모크(로더 경유, 비LLM)"
docker exec -e WEBUI_SECRET_KEY=verify-only open-webui python -c "
import asyncio
from open_webui.utils.plugin import load_function_module_by_id
async def main():
    p, _, _ = await load_function_module_by_id('daily_audit_agent')
    out = await p.pipe({'messages':[{'role':'user','content':'대상? 협상용역 3억1천만원'}]})
    assert '일상감사 대상' in out, out[:200]
    out2 = await p.pipe({'messages':[{'role':'assistant','content':'x'},{'role':'user','content':'도움말'}]})
    assert '효규가영' in out2, out2[:200]
    print('스모크 통과 ✓')
asyncio.run(main())
"
echo "배포 완료 ✅ (실행 중인 open-webui 세션은 다음 요청부터 새 코드 사용)"
