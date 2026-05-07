# Research Digest

매일 아침 8시 (KST, 월–금) Europe PMC에서 키워드(Somatic mosaicism, mosaic, mosaic variant, methylation, PacBio) + 핀 저널(Cell/Nature/Science) 신규 논문을 가져와 한 페이지짜리 HTML 다이제스트를 만든다. 월요일은 지난 금/토/일 3일치를 함께 처리한다.

**Live site:** <https://genomicdiversitylab.github.io/research-digest/>

## 디렉터리

```
.
├─ index.html               ← 일자별 디지스트 인덱스 (Pages 첫 화면)
├─ digests/
│  └─ YYYY-MM-DD.html       ← 하루치 다이제스트 (필터/검색/IF 태그)
├─ data/
│  ├─ YYYY-MM-DD.json       ← 하루치 메타데이터
│  ├─ _summaries_high.json  ← IF≥10 한글 구조화 요약 (수동 backfill)
│  └─ _summaries_low.json   ← 그 외 한글 2문장 요약 (수동 backfill)
├─ paper_digest.py          ← 메인 스크립트 (cron 호출 대상)
├─ config.json              ← 키워드 / 핀 저널 / 페이지 사이즈
├─ journal_if.json          ← JCR 2024 IF lookup (curated)
└─ README.md
```

## 어떻게 동작하나

하나의 endpoint만 호출: **`https://www.ebi.ac.uk/europepmc/webservices/rest/search`**.
Europe PMC가 PubMed, bioRxiv, medRxiv, PMC 등을 인덱싱하므로 호출 한 번으로 preprint + 정식 논문을 모두 커버.

각 키워드는:
```
(TITLE:"<keyword>" OR ABSTRACT:"<keyword>") AND CREATION_DATE:[D TO D]
```
각 핀 저널은:
```
JOURNAL:"<journal>" AND CREATION_DATE:[D TO D]
```

`CREATION_DATE`는 EPMC 인덱스에 추가된 날짜라서 "오늘 새로 들어온 거"에 가장 가까움 (`FIRST_PDATE`는 backdate되어 누락 발생 가능).

## 자동화

- 스케줄: `0 8 * * 1-5` (Mon–Fri 08:00 KST), Cowork scheduled task
- 월요일 자동 백필: `paper_digest.py`가 자동으로 지난 금/토/일 3일치 처리 (`resolve_target_dates`)
- Slack 알림: `#research-digest` (private), `<@U09DS8JQ476>` 멘션 포함
- Drive 아카이브: `~/Library/CloudStorage/GoogleDrive-yoojinha@hanyang.ac.kr/내 드라이브/연구/Digest/`
- GitHub Pages: `main` 브랜치 push 시 즉시 반영

## 수동 실행

```bash
# 어제 1일치 (Mon이면 자동 3일 백필)
python3 paper_digest.py

# 특정 날짜
python3 paper_digest.py 2026-05-06

# 여러 날짜
python3 paper_digest.py 2026-05-04 2026-05-05 2026-05-06

# 오늘
python3 paper_digest.py --today
```

## 키워드 편집

`config.json`을 수정. 세 가지 노브:

```json
{
  "keywords": ["Somatic mosaicism", "mosaic", "..."],
  "always_include_journals": ["Cell", "Nature", "Science"],
  "keyword_overrides": {
    "mosaic": "(TITLE:\"mosaic\" AND (ABSTRACT:genome OR ABSTRACT:variant OR ABSTRACT:cell OR ABSTRACT:brain))"
  }
}
```

- `keywords` — 멀티워드는 정확한 phrase로 보내짐. 대소문자 무관.
- `always_include_journals` — 키워드 hit 0이라도 무조건 포함. 정확한 저널명 사용 (`Nature Genetics` ✓, `Nat Genet` ✗).
- `keyword_overrides` — 기본 `(TITLE OR ABSTRACT)` 래퍼를 직접 짠 EPMC 쿼리로 대체. `CREATION_DATE`는 자동 추가.

EPMC search syntax: <https://europepmc.org/Help#searchcommands>

## 한글 요약 정책

- **IF ≥ 10 (JCR 2024)**: 배경 / 결과별 핵심 메시지 / 결론 / 의의 구조화 (`data/_summaries_high.json`)
- **IF < 10 또는 unknown**: 2문장 (`data/_summaries_low.json`)

요약은 abstract 기반 (full-text 접근 안 함). 매일 자동 실행에서는 abstract excerpt만 표시되고, 한글 요약은 수동 backfill로 추가됨.

## 네트워크 요구

`www.ebi.ac.uk`가 Cowork 네트워크 allowlist에 있어야 함 (Settings → Capabilities). 없으면 모든 키워드 fetch가 실패해 exit code 2.

GitHub Pages auto-publish를 위해 `github.com`도 allowlist 필요 (이미 OK).

## 일시 정지 / 재개

Cowork "Scheduled tasks" 패널에서 토글 또는:
```
mcp__scheduled-tasks__update_scheduled_task(taskId="daily-paper-digest", enabled=false)
```

## 의도적으로 다루지 않는 것

- 이메일 발송 (Slack 알림으로 대체)
- Google Scholar (public API 없음)
- Persistent dedup (`CREATION_DATE` 윈도우 기반만)
- Full-text 자동 다운로드 (구독 / 저작권 이슈)
