"""법령 탐색 레인 (C3, 선택적). 기존 law-mcp를 mcpo REST(8002)로 재사용.

결정론 검증(law_fetcher)이 '아는 조문을 정확히 가져오는' 레인이라면, 이쪽은
'관련 조례·훈령·판례를 찾는' 탐색 레인이다. law_fetcher가 못 하는 두 가지를 담당:
검색(키워드→후보) + 자치법규/행정규칙/판례(국가법령 외).

원칙:
- **선택적 의존성**: LAW_MCP_URL 미설정·접속 불가 시 예외 없이 빈 결과 반환.
  파이프라인은 탐색 레인 없이도 정상 동작한다(orchestrator가 '탐색 미수행' 표기).
- **후보 발굴까지만**: 여기서 찾은 조문은 의견서에 바로 인용하지 않는다.
  orchestrator가 law_fetcher로 재검증(verified=True)한 것만 인용 가능(A3 정합).

연동은 MCP 프로토콜이 아니라 mcpo가 노출하는 OpenAPI REST(POST /{tool})를 직접 호출.
컨테이너 내부에서는 host.docker.internal:8002로 접근(LAW_MCP_URL).
"""

import json
import urllib.error
import urllib.request
from typing import Callable

from audit_core.agents.schemas import LawSearchHit
from audit_core.config import get_settings


def _default_post(url: str, payload: dict, timeout_s: int) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return json.loads(resp.read().decode("utf-8"))


class LawSearchClient:
    def __init__(
        self,
        base_url: str | None = None,
        timeout_s: int | None = None,
        default_org: str | None = None,
        post_fn: Callable[[str, dict, int], dict] | None = None,  # 테스트 주입용
    ):
        s = get_settings()
        self.base_url = (base_url if base_url is not None else s.LAW_MCP_URL).rstrip("/")
        self.timeout_s = timeout_s or s.LAW_MCP_TIMEOUT_S
        self.default_org = default_org if default_org is not None else s.LAW_MCP_DEFAULT_ORG
        self._post = post_fn or _default_post

    @property
    def enabled(self) -> bool:
        return bool(self.base_url)

    def _call(self, tool: str, params: dict) -> list[dict]:
        """mcpo 도구 1개 호출 → 결과 리스트. 어떤 오류든 빈 리스트로 강등."""
        if not self.enabled:
            return []
        try:
            data = self._post(f"{self.base_url}/{tool}", params, self.timeout_s)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            return []
        if not isinstance(data, dict):
            return []
        return data.get("결과") or []

    def search_law(self, query: str, display: int = 3) -> list[LawSearchHit]:
        """법률·시행령·시행규칙 검색 (law-mcp /search_law)."""
        rows = self._call("search_law", {"query": query, "display": display})
        return [
            LawSearchHit(
                target="law",
                title=r.get("제목") or "",
                ref=str(r.get("종류") or ""),
                snippet=f"{r.get('소관') or ''} 시행 {r.get('시행일자') or ''}".strip(),
            )
            for r in rows
            if r.get("제목")
        ]

    def search_ordinance(self, query: str, display: int = 3, org: str | None = None) -> list[LawSearchHit]:
        org = self.default_org if org is None else org
        rows = self._call("search_ordinance", {"query": query, "display": display, "org": org})
        return [
            LawSearchHit(
                target="ordin",
                title=r.get("제목") or "",
                ref=str(r.get("종류") or ""),
                snippet=f"{r.get('소관') or ''} 시행 {r.get('시행일자') or ''}".strip(),
            )
            for r in rows
            if r.get("제목")
        ]

    def search_admrule(self, query: str, display: int = 3) -> list[LawSearchHit]:
        rows = self._call("search_admrule", {"query": query, "display": display})
        return [
            LawSearchHit(
                target="admrul",
                title=r.get("제목") or "",
                ref=str(r.get("종류") or ""),
                snippet=f"{r.get('소관') or ''} 시행 {r.get('시행일자') or ''}".strip(),
            )
            for r in rows
            if r.get("제목")
        ]

    def search_precedent(self, query: str, display: int = 3) -> list[LawSearchHit]:
        rows = self._call("search_precedent", {"query": query, "display": display})
        return [
            LawSearchHit(
                target="prec",
                title=r.get("제목") or "",
                ref=str(r.get("사건번호") or ""),
                snippet=f"{r.get('소관') or ''} 선고 {r.get('선고일자') or ''}".strip(),
            )
            for r in rows
            if r.get("제목")
        ]
