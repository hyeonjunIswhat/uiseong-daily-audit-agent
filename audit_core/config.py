"""환경변수 로딩 (SPEC §4).

파이프라인용 .env만 로딩한다. .env.batch(외부 LLM 키)는 batch/ 프로세스 전용이며
이 모듈은 절대 참조하지 않는다 — 파이프라인 프로세스가 외부 키를 알 수 없게 하는
물리 분리 원칙(SPEC §1-4).
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── LLM (폐쇄형) ──────────────────────────────
    OLLAMA_BASE_URL: str = "http://host.docker.internal:11434"
    AUDIT_MODEL_REVIEW: str = "qwen3:14b"
    AUDIT_MODEL_LIGHT: str = "qwen3:8b"  # v0.2: qwen3:4b 미설치 → 8b
    AUDIT_MODEL_SYNTH: str = "qwen3:14b"  # 의견서 초안·2차 문맥검증(서면검토 전용)
    AUDIT_LLM_TIMEOUT_S: int = 180
    AUDIT_LLM_NUM_CTX: int = 16384
    # 단계별 시간 예산(2026-07-15 성능 규율) — 초과 시 남은 LLM 항목은
    # '확인 필요'로 넘기고 결정론 결과는 그대로 반환(부분 결과 우선)
    AUDIT_BUDGET_SELF_S: int = 120      # 자가점검 최대(목표 60초)
    AUDIT_BUDGET_WRITTEN_S: int = 300   # 서면검토 최대(목표 180초)
    # 호출별 HTTP 타임아웃(용도별) — 검토/검증/합성. 타임아웃은 재시도하지 않고
    # 부분 결과로 처리한다(base.chat_json 규율).
    AUDIT_TIMEOUT_REVIEW_S: int = 90
    AUDIT_TIMEOUT_VERIFY_S: int = 45
    AUDIT_TIMEOUT_SYNTH_S: int = 90
    # 검토 입력 다이제스트 상한(자) — 초과분은 결정론 발췌(digest.py)로 축약
    AUDIT_DIGEST_CAP: int = 8000

    # ── 문서 파서 (kordoc) ───────────────────────
    KORDOC_PARSE_URL: str = "http://host.docker.internal:8001/parse_document"
    KORDOC_TIMEOUT_S: int = 120

    # ── 국가법령정보 공동활용 API ─────────────────
    LAW_API_BASE: str = "http://www.law.go.kr/DRF"
    LAW_API_OC: str = ""  # 미설정 시 캐시 전용 모드 강등 (SPEC §4.3)
    LAW_API_TIMEOUT_S: int = 15
    LAW_CACHE_DIR: str = str(PROJECT_ROOT / "audit_core/storage/law_cache")
    LAW_CACHE_TTL_DAYS: int = 30

    # ── 법령 탐색 레인 (law-mcp/mcpo, 선택적) ─────
    # 미설정 시 탐색 레인 비활성 — 결정론 검증(law_fetcher)만으로 동작 (C3).
    # 예: http://host.docker.internal:8002 (mcpo OpenAPI 프록시)
    LAW_MCP_URL: str = ""
    LAW_MCP_TIMEOUT_S: int = 15
    LAW_MCP_DEFAULT_ORG: str = "의성군"

    # ── 루브릭·규칙 ──────────────────────────────
    # v0.3: 규정 §7④ 8축 재편 초안(협의 대상 표시 포함). v0_2·v0_1은 보존 — 롤백용
    RUBRIC_PATH: str = str(PROJECT_ROOT / "audit_core/rubric/rubric_v0_3.json")
    # 계약방법 오버레이 모듈(협상·수의) — 분야가 아니라 셀 위에 겹치는 공통 체크리스트 (REBUILD 회차 1)
    OVERLAY_PATH: str = str(PROJECT_ROOT / "audit_core/rubric/overlay_contract_method.json")
    TARGET_RULES_PATH: str = str(PROJECT_ROOT / "audit_core/rules/target_rules.yaml")
    DOC_PROFILES_PATH: str = str(PROJECT_ROOT / "audit_core/rules/doc_profiles.yaml")
    # 근거 태그(인용 권한)·서류 완결성 — 마스터 SOP 파생 데이터 (REBUILD 회차 1)
    CITATION_TAGS_PATH: str = str(PROJECT_ROOT / "audit_core/rules/citation_tags.yaml")
    REQUIRED_DOCS_PATH: str = str(PROJECT_ROOT / "audit_core/rules/required_docs.yaml")
    # 사업성격 신호 사전(BusinessClassifier — 현실 표현→법정 유형, 회차 3)
    BIZ_SIGNALS_PATH: str = str(PROJECT_ROOT / "audit_core/rules/biz_signals.yaml")
    GATES_PATH: str = str(PROJECT_ROOT / "audit_core/rules/gates.yaml")
    HOLIDAYS_PATH: str = str(PROJECT_ROOT / "audit_core/rules/holidays_kr.yaml")
    DEADLINE_NOTIFY: str = "7,7,14"  # 결과통보/재검토/조치결과(일). 세부규정 확정 시 교체
    DEADLINE_MODE: str = "calendar_roll"  # calendar_roll | business

    # ── 민감도 라우팅(설계서 §3.1) — 외부 LLM은 기관 승인 전 고정 OFF ──
    # Valve로 노출하지 않는다(운영 중 완화 방지). 승인 시 .env에서만 전환.
    EXTERNAL_LLM_ENABLED: bool = False

    # 축 트리아지(사전 선별) — 경량 모델 1콜로 관련 축만 가동(전 축 상시 가동
    # 구조의 시간·노이즈 한계 보완, 2026-07-15). false면 기존 전축 동작.
    AUDIT_TRIAGE: bool = True

    # ── 처리대장·이력 ────────────────────────────
    LEDGER_DIR: str = str(PROJECT_ROOT / "audit_core/storage/ledger_data")
    # AUDIT_TRAIL_LEVEL은 고정값 — 환경변수·Valve로 완화 불가 (SPEC §4.3)
    AUDIT_TRAIL_LEVEL: str = "code_only"

    # ── 등급1 파기 (purge_job 전용 참조) ─────────
    OPENWEBUI_DB_URL: str = ""
    OPENWEBUI_DATA_DIR: str = ""
    PURGE_MODEL_NAME: str = "일상감사 멀티 에이전트"  # 파이프 name과 일치 유지
    PURGE_RETENTION_HOURS: int = 24

    def deadline_days(self) -> tuple[int, int, int]:
        """DEADLINE_NOTIFY '7,7,14' → (결과통보, 재검토, 조치결과)."""
        parts = [int(p.strip()) for p in self.DEADLINE_NOTIFY.split(",")]
        if len(parts) != 3:
            raise ValueError(f"DEADLINE_NOTIFY는 '결과통보,재검토,조치결과' 3개 값이어야 함: {self.DEADLINE_NOTIFY!r}")
        return parts[0], parts[1], parts[2]


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
