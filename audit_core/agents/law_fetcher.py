"""법령 조회 에이전트 (SPEC §5). 국가법령정보 공동활용 API + 조문 단위 캐시.

- 기존 law-mcp(mcp/law-mcp/server.py)와 동일한 DRF 엔드포인트·OC 사용
- 캐시: {법령ID}_{조번호}.json, TTL 경과 시 시행일 재검증, 개정 감지 시
  구버전은 _archived 보관(과거 의견서 근거 추적용)
- LAW_API_OC 미설정 시 캐시 전용 모드로 강등 (SPEC §4.3)
- exists()는 verifier 1차 결정론 검증(인용 조문 실존 확인)에 사용 (SPEC §3.3)
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from audit_core.config import get_settings

KST = timezone(timedelta(hours=9))

# 약칭 → 법제처 정식 법령명 (루브릭 law_refs의 법령ID로 사용)
LAW_ALIASES = {
    "지방계약법": "지방자치단체를 당사자로 하는 계약에 관한 법률",
    "지방계약법시행령": "지방자치단체를 당사자로 하는 계약에 관한 법률 시행령",
    "지방계약법시행규칙": "지방자치단체를 당사자로 하는 계약에 관한 법률 시행규칙",
    "공공감사법": "공공감사에 관한 법률",
    "지방재정법": "지방재정법",
    "지방회계법": "지방회계법",
    "지방보조금법": "지방자치단체 보조금 관리에 관한 법률",
    "보조금법": "보조금 관리에 관한 법률",
}

_ARTICLE_RE = re.compile(r"제?\s*(\d+)조(?:의(\d+))?")


class LawFetchError(Exception):
    pass


@dataclass
class LawArticle:
    law_id: str          # 약칭 (캐시 파일명 구성)
    law_name: str        # 정식 법령명
    article: str         # "제22조", "제25조의2"
    effective_date: str  # YYYY-MM-DD
    fetched_at: str      # ISO(KST)
    hit_count: int
    text: str
    source: str = "api"  # api | cache | cache_stale
    mst: str = ""        # 법령일련번호 (재검증용)


def _default_get(url: str, timeout_s: int) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise LawFetchError(f"법령 API 호출 실패: {e}") from e


def _collect_text(node) -> list[str]:
    """조문단위 트리(항·호·목)에서 '…내용' 값을 문서 순서대로 수집."""
    out: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k.endswith("내용") and isinstance(v, str):
                out.append(v.strip())
            elif isinstance(v, (dict, list)):
                out.extend(_collect_text(v))
    elif isinstance(node, list):
        for item in node:
            out.extend(_collect_text(item))
    return out


def normalize_article(article: str) -> tuple[str, str, str]:
    """'22조'/'제22조'/'제22조의2' → (표준표기, 조번호, 가지번호)."""
    m = _ARTICLE_RE.search(article.replace(" ", ""))
    if not m:
        raise ValueError(f"조문 표기 해석 불가: {article!r}")
    num, branch = m.group(1), m.group(2) or ""
    std = f"제{num}조" + (f"의{branch}" if branch else "")
    return std, num, branch


class LawFetcher:
    def __init__(
        self,
        oc: str | None = None,
        base_url: str | None = None,
        cache_dir: str | Path | None = None,
        ttl_days: int | None = None,
        timeout_s: int | None = None,
        get_fn: Callable[[str, int], dict] | None = None,  # 테스트 주입용
    ):
        s = get_settings()
        self.oc = s.LAW_API_OC if oc is None else oc
        self.base_url = (base_url or s.LAW_API_BASE).rstrip("/")
        self.cache_dir = Path(cache_dir or s.LAW_CACHE_DIR)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl = timedelta(days=ttl_days if ttl_days is not None else s.LAW_CACHE_TTL_DAYS)
        self.timeout_s = timeout_s or s.LAW_API_TIMEOUT_S
        self._get = get_fn or _default_get
        self._law_json_memo: dict[str, dict] = {}  # MST → 법령 전문 (세션 내 재사용)

    @property
    def cache_only(self) -> bool:
        return not self.oc

    # ── 캐시 ────────────────────────────────────────────

    def _cache_path(self, law_id: str, article_std: str) -> Path:
        return self.cache_dir / f"{law_id}_{article_std}.json"

    def _read_cache(self, law_id: str, article_std: str) -> LawArticle | None:
        p = self._cache_path(law_id, article_std)
        if not p.exists():
            return None
        with open(p, encoding="utf-8") as f:
            return LawArticle(**json.load(f))

    def _write_cache(self, art: LawArticle) -> None:
        p = self._cache_path(art.law_id, art.article)
        d = asdict(art)
        d["source"] = "cache"  # 파일에는 캐시로 기록 (source는 반환 시점 표기)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)

    def _archive(self, art: LawArticle) -> None:
        """개정 감지 시 구버전 보관 — 과거 의견서 근거 추적용 (SPEC §5)."""
        src = self._cache_path(art.law_id, art.article)
        if src.exists():
            src.rename(src.with_name(f"{src.stem}_archived_{art.effective_date}.json"))

    def _fresh(self, art: LawArticle) -> bool:
        fetched = datetime.fromisoformat(art.fetched_at)
        return datetime.now(KST) - fetched < self.ttl

    # ── API ─────────────────────────────────────────────

    def _api_url(self, endpoint: str, **params) -> str:
        q = urllib.parse.urlencode({"OC": self.oc, "target": "law", "type": "JSON", **params})
        return f"{self.base_url}/{endpoint}?{q}"

    def _find_mst(self, law_name: str) -> str:
        data = self._get(self._api_url("lawSearch.do", query=law_name, display=10), self.timeout_s)
        laws = (data.get("LawSearch") or {}).get("law") or []
        if isinstance(laws, dict):
            laws = [laws]
        for law in laws:
            if law.get("법령명한글") == law_name and law.get("현행연혁코드") == "현행":
                return str(law["법령일련번호"])
        raise LawFetchError(f"현행 법령을 찾지 못함: {law_name!r}")

    def _fetch_law_json(self, mst: str) -> dict:
        if mst not in self._law_json_memo:
            data = self._get(self._api_url("lawService.do", MST=mst), self.timeout_s)
            self._law_json_memo[mst] = data.get("법령") or data
        return self._law_json_memo[mst]

    def _extract_article(self, law_json: dict, num: str, branch: str) -> str:
        units = (law_json.get("조문") or {}).get("조문단위") or []
        if isinstance(units, dict):
            units = [units]
        for u in units:
            if (
                u.get("조문여부") == "조문"
                and str(u.get("조문번호", "")) == num
                and str(u.get("조문가지번호", "") or "") == branch
            ):
                return "\n".join(t for t in _collect_text(u) if t)
        raise LawFetchError(f"조문 없음: 제{num}조{'의' + branch if branch else ''}")

    # ── 공개 API ─────────────────────────────────────────

    def fetch(self, law_id: str, article: str) -> LawArticle:
        """조문 원문 확보. 캐시 신선 → 캐시, 아니면 API 재검증. 실패 시 LawFetchError.

        캐시 전용 모드(OC 미설정)나 API 장애 시에는 만료 캐시라도 source='cache_stale'로
        반환한다(없으면 예외).
        """
        law_id = law_id.strip()
        law_name = LAW_ALIASES.get(law_id, law_id)
        article_std, num, branch = normalize_article(article)
        cached = self._read_cache(law_id, article_std)

        if cached and (self._fresh(cached) or self.cache_only):
            cached.hit_count += 1
            cached.source = "cache" if self._fresh(cached) else "cache_stale"
            self._write_cache(cached)
            return cached

        if self.cache_only:
            raise LawFetchError(f"캐시 전용 모드(OC 미설정) — 캐시에 없음: {law_id} {article_std}")

        try:
            mst = self._find_mst(law_name)
            law_json = self._fetch_law_json(mst)
        except LawFetchError:
            if cached:  # API 장애 시 만료 캐시로 강등 반환
                cached.hit_count += 1
                cached.source = "cache_stale"
                self._write_cache(cached)
                return cached
            raise

        basic = law_json.get("기본정보") or {}
        eff_raw = str(basic.get("시행일자", ""))
        eff = f"{eff_raw[:4]}-{eff_raw[4:6]}-{eff_raw[6:8]}" if len(eff_raw) == 8 else eff_raw
        text = self._extract_article(law_json, num, branch)

        if cached and cached.effective_date and cached.effective_date != eff:
            self._archive(cached)  # 개정 감지 → 구버전 보관

        art = LawArticle(
            law_id=law_id,
            law_name=str(basic.get("법령명_한글", law_name)),
            article=article_std,
            effective_date=eff,
            fetched_at=datetime.now(KST).isoformat(),
            hit_count=(cached.hit_count + 1) if cached else 1,
            text=text,
            source="api",
            mst=mst,
        )
        self._write_cache(art)
        return art

    def fetch_ref(self, ref: str) -> LawArticle:
        """'지방계약법-제22조' 형식(루브릭 law_refs·축 출력)을 해석해 조회."""
        law_id, _, article = ref.partition("-")
        if not article:
            raise ValueError(f"참조 형식 오류(법령ID-제N조): {ref!r}")
        return self.fetch(law_id, article)

    def exists(self, ref: str) -> bool:
        """verifier 1차 결정론 검증용 — 인용 조문의 실존 여부."""
        try:
            self.fetch_ref(ref)
            return True
        except (LawFetchError, ValueError):
            return False
