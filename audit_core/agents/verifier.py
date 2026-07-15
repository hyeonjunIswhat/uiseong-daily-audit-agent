"""검증 1차: 결정론적 대조 (SPEC §3.3). LLM 아님.

- 금액 산식 검증: 문서에서 'A × B = C' / '소계·합계' 형태의 수식을 추출해 재계산,
  기재값과 불일치 시 FLAG. (샘플의 합계 오류 같은 케이스 검출)
- FP(기능점수) 검산: SW개발사업 대가 구조 대응 —
  ① 곱셈 체인에 소수 보정계수·퍼센트·인수 N개 허용
     (예: "500FP × 553,114원 × 0.83 × 1.2 = …", "개발원가 × 25% = …")
  ② 'SW개발비 = 개발원가 + 직접경비 + 이윤' 3자 합산 검산 (KOSA 대가산정 가이드 구조)
- 조문 실존 검증: law_fetcher.exists()로 인용 조문 실재 확인.

허용오차 설계: 정수 인수만의 곱셈은 정확 일치를 요구한다(기존 동작). 소수·퍼센트가
끼면 중간 반올림 관행(원단위 절사 등)으로 최종값이 미세하게 어긋나므로,
max(1,000원, 0.5%)를 허용해 오탐(허위 지적)을 막는다 — 오탐을 미탐보다 위험한 것으로
보는 설계 판단(DESIGN.md 8장). 실제 결함(계수 오적용·전기 오류)은 수 % 이상 어긋난다.
2차 LLM 문맥 검증은 서면검토(의견서) 파이프에서 synthesizer 뒤에 붙는다.
"""

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

# "라벨: 12,345,678원" 또는 "= 12,345,678원"
_MONEY = re.compile(r"([0-9][0-9,]*)\s*원")

# 곱셈 체인 "A단위 × B단위 × … = D" — 인수 N개, 소수·%·영문 단위(FP 등) 허용
_NUM_PAT = r"[0-9][0-9,]*(?:\.[0-9]+)?"
_FACTOR = _NUM_PAT + r"\s*%?\s*[A-Za-z가-힣]*"
_CHAIN = re.compile(
    r"((?:" + _FACTOR + r"\s*[×xX*]\s*)+" + _FACTOR + r")\s*=\s*(" + _NUM_PAT + r")"
)
_FACTOR_NUM = re.compile(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*(%?)")

# 소수·퍼센트 체인 허용오차 — 중간 반올림 관행 흡수(절대 1,000원 또는 상대 0.5%)
_FRACTION_TOL_ABS = 1_000
_FRACTION_TOL_REL = Decimal("0.005")


def _num(s: str) -> int:
    return int(s.replace(",", ""))


@dataclass
class NumericCheck:
    kind: str        # "mult" | "sum"
    expr: str        # 원문 표현
    expected: int    # 재계산 값
    claimed: int     # 문서 기재값
    match: bool


def check_arithmetic(doc_text: str) -> list[NumericCheck]:
    """문서 내 곱셈·합산 산식을 재계산해 기재값과 대조."""
    checks: list[NumericCheck] = []

    for m in _CHAIN.finditer(doc_text):
        factors = _FACTOR_NUM.findall(m.group(1))
        expected = Decimal(1)
        fractional = False
        for num, pct in factors:
            val = Decimal(num.replace(",", ""))
            if pct:
                val /= 100
            if pct or "." in num:
                fractional = True
            expected *= val
        expected_i = int(expected.to_integral_value(rounding=ROUND_HALF_UP))
        claimed_v = _num(m.group(2).split(".")[0])
        if fractional:
            tol = max(_FRACTION_TOL_ABS, int(expected * _FRACTION_TOL_REL))
            match = abs(expected_i - claimed_v) <= tol
        else:
            match = expected_i == claimed_v
        checks.append(NumericCheck("mult", m.group(0).strip(), expected_i, claimed_v, match))

    checks.extend(_check_sums(doc_text))
    checks.extend(_check_fp_sums(doc_text))
    return checks


_LABELED = re.compile(r"(소계|부가가치세|부가세|합계|총계|총액)\s*[:：]?[^0-9]*([0-9][0-9,]*)\s*원")


def _check_sums(doc_text: str) -> list[NumericCheck]:
    """원가계산서의 '소계 + 부가세 = 합계' 관계만 보수적으로 검산.

    계층 내역(직접인건비=하위 항목 합)을 임의로 재구성하면 오탐이 잦으므로,
    라벨이 명시된 소계·부가세·합계 3자 관계만 검증한다. 오탐(허위 지적)을
    미탐보다 위험한 것으로 본 설계 판단(자동화 편향 대응, DESIGN.md 8장).
    """
    labeled: dict[str, int] = {}
    total_line = ""
    for label, val in _LABELED.findall(doc_text):
        key = "부가세" if label in ("부가가치세", "부가세") else label
        key = "합계" if key in ("총계", "총액") else key
        labeled.setdefault(key, _num(val))
    for line in doc_text.splitlines():
        if any(k in line for k in ("합계", "총계", "총액")):
            total_line = line.strip()
            break

    if {"소계", "부가세", "합계"} <= labeled.keys():
        expected = labeled["소계"] + labeled["부가세"]
        claimed = labeled["합계"]
        return [NumericCheck("sum", total_line or "합계 = 소계 + 부가세",
                             expected, claimed, expected == claimed)]
    return []


# SW개발비 = 개발원가 + 직접경비 + 이윤 (KOSA SW사업 대가산정 가이드 구조)
_FP_LABELED = re.compile(
    r"(개발원가|직접경비|이윤|SW\s*개발비|소프트웨어\s*개발비)\s*[:：]?[^0-9]*([0-9][0-9,]*)\s*원"
)


def _check_fp_sums(doc_text: str) -> list[NumericCheck]:
    """'SW개발비 = 개발원가 + 직접경비 + 이윤' 4자 라벨이 모두 명시된 경우만 검산.

    _check_sums와 같은 보수 원칙 — 라벨 없는 금액으로 산식을 재구성하지 않는다.
    각 라벨은 첫 등장 값을 취한다(요약표 우선 관행)."""
    labeled: dict[str, int] = {}
    total_line = ""
    for label, val in _FP_LABELED.findall(doc_text):
        key = "SW개발비" if label.replace(" ", "") in ("SW개발비", "소프트웨어개발비") else label
        labeled.setdefault(key, _num(val))
    for line in doc_text.splitlines():
        if "SW개발비" in line.replace(" ", "") or "소프트웨어개발비" in line.replace(" ", ""):
            total_line = line.strip()
            break

    # 실물 산출내역서 교훈(2026 생성형AI플랫폼): 'SW개발비(부가세 포함, 십만단위 절사)'
    # 표기가 흔함 — 부가세·절사가 끼면 3자 합산이 성립하지 않으므로 검산하지 않는다(오탐 방지)
    if total_line and "부가세" in total_line:
        return []

    if {"개발원가", "직접경비", "이윤", "SW개발비"} <= labeled.keys():
        expected = labeled["개발원가"] + labeled["직접경비"] + labeled["이윤"]
        claimed = labeled["SW개발비"]
        return [NumericCheck("fp_sum", total_line or "SW개발비 = 개발원가 + 직접경비 + 이윤",
                             expected, claimed, expected == claimed)]
    return []


def arithmetic_flags(doc_text: str) -> list[NumericCheck]:
    return [c for c in check_arithmetic(doc_text) if not c.match]
