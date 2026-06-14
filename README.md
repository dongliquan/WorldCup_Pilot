# World Cup Pilot (macOS)

로컬/개인용 FIFA 월드컵 뷰어. `pywebview` 네이티브 창 + 백그라운드 로컬 서버 + 단일 HTML UI +
PyInstaller `.app` 패턴입니다. **여러 무료 API를 조합**해 일정·결과·순위·대진·선수·영상까지 보여주며,
**역대 월드컵(1930~2026)** 을 회차 선택으로 볼 수 있습니다. **상용 배포 용도가 아닙니다.**

## 구성

| 파일 | 역할 |
|------|------|
| `worldcup.py`   | 런처 — 로컬 서버를 스레드로 띄우고 WebKit 창을 엶. Dock 아이콘 설정, 데스크톱 알림(osascript), 영상 팝업 창, 시작 시 정적 데이터 백그라운드 프리빌드 |
| `server.py`     | `127.0.0.1:8770` 로컬 서버 — `/` (UI) + `/api/*` (여러 소스 프록시·정규화·디스크 캐시) |
| `worldcup.html` | 단일 페이지 UI (블랙/라이트 테마, 일정·조별·대진·팀/선수/경기 상세, 영상, 알림) |
| `config.json`   | football-data 토큰·시즌·캐시 설정 |
| `assets/`       | 정적 데이터(JSON) + 아이콘/로고 |
| `worldcup.spec` / `build.sh` | `.app` 번들 빌드 |

### assets (정적 데이터 — 모두 편집 가능)
- `country_info.json` — 국가별 수도·인구·면적·ISO2(지도)·월드컵 역대 성적(우승/준우승/3위 연도)
- `fifa_ranking.json` — 현재(2026) FIFA 랭킹 스냅샷(국가→순위)
- `fifa_ranking_history.json` — 회차별 대회 당시 FIFA 랭킹(`byYear`: 연도→국가→순위). FIFA 랭킹은 1993년 시작 → 이전 대회는 없음
- `venues.json` — 개최 도시→시간대 + 경기 id→도시 매핑(현지시간용)
- `wc_editions.json` — 역대 대회(개최국·우승/준우승 국기·MVP·골든부트)
- `icon.png` / `icon_1024.png` — 앱/Dock 아이콘(생성한 축구장 이미지)
- `logo.png` — 헤더 공식 FIFA 엠블럼

## 데이터 소스 (전부 무료, API-Football 미사용)

| 데이터 | 소스 |
|------|------|
| 2026 일정·스코어·순위·팀(감독/창설)·공식 엠블럼 | **football-data.org** (토큰 필요, 무료) |
| 과거 대회 일정·결과·스테이지, 조별 순위 | **ESPN** (무료, 키 불필요) |
| 경기 이벤트(골·카드)·경기장 도시·LIVE·배당(DraftKings) | **ESPN** |
| 선수단(이름·나이·생일·신장·체중·부상) — 당시 시즌 기준 | **ESPN** roster (`?season=연도`) |
| 선수 사진·소속팀·소속팀 소재국 | **TheSportsDB** (무료, 전역 스로틀 적용) |
| 현재 FIFA 랭킹 | `fifa_ranking.json` (정적 스냅샷, 무료 실시간 API 없음) |
| 대회 당시 FIFA 랭킹 | `fifa_ranking_history.json` (회차별 정적, 편집 가능) |
| 경기장 이미지 | **Wikipedia** |
| 온도·습도 | **Open-Meteo** (무료, 키 불필요) |
| 국가 지도 윤곽 | **mapsicon** (GitHub, ISO2) |
| 개막식·결승·하이라이트 영상 | **YouTube**(공식 FIFA 검색 1순위) → 앱 내 팝업 재생 |

> **회차 선택 시 모든 정보가 당시 기준**: 선수 명단·나이(당시 시즌), 조별 순위·결과, FIFA 랭킹(데이터 있는 회차만).
> FIFA 랭킹은 1993년 시작 → 그 이전 대회는 랭킹 미표시. 국가정보·결과·순위·역대성적은 정확.

## 1) 설정

`config.example.json` 을 `config.json` 으로 복사하고 football-data 토큰을 넣으세요
(`config.json` 은 토큰 보호를 위해 git에서 제외됨):

```bash
cp config.example.json config.json   # 그런 다음 football_data_token 값 입력
```

- 토큰이 없거나 시즌 미지원이면 자동으로 **SAMPLE DATA(mock)** 로 동작(`use_mock_when_unavailable`).
- `cache_ttl_seconds`(기본 60초): 라이브 스코어·이벤트용 짧은 캐시. **정적 데이터는 영구 캐시**.
- `venue_timezone`: "현지시간" 토글의 개최 권역 기본값(2026=미 동부).

## 2) 개발 모드 실행

```
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
./dashboard.sh            # == .venv/bin/python worldcup.py
```

서버만 띄워 브라우저로 보려면: `.venv/bin/python server.py` → http://127.0.0.1:8770

> 시스템 Python 3.9는 pyobjc 빌드가 안 돼 **brew Python 3.12** 로 `.venv` 사용.

## 3) .app 번들 빌드

```
./build.sh                # -> dist/World Cup Pilot.app  (icon.icns 자동 포함)
open "dist/World Cup Pilot.app"
```

## 주요 기능

- **회차 선택(역순)**: 2026 → 1930. 현재(2026)는 라이브, 과거는 ESPN 스냅샷. 선택 시 회차 명칭(title)·로고도 해당 연도로 전환, **모든 정보가 당시 기준**.
- **일정**: 좌우 날짜 스크롤 + 시간순. 🇰🇷한국/📍현지 시간대 토글(경기별 시간대), Today 버튼(현재 회차만).
  - 팀(국기·국가명·당시 FIFA 랭킹) 클릭 → 팀 상세 / 경기 중앙 클릭 → 경기 상세 아코디언(경기장 이미지·날씨·관중·골/카드 타임라인·배당)
  - 골 칩 클릭 → 해당 골 하이라이트(YouTube, 앱 내 재생)
  - LIVE 경기: 초록 테두리 + **30초 자동 갱신**(해당 영역만, 깜빡임 없음)
  - 헤더 **LIVE 배지는 항상 표시**, 실제 진행 경기가 있을 때만 초록색 점등
- **조별**: A~L 조 순위표(상위 2팀 강조). 과거 대회는 ESPN 조별 순위.
- **대진표**: 결승을 가운데 둔 대칭 브래킷(32강~결승). 팀 미정은 슬롯 표시.
- **팀 상세**(해당 연도 기준): 헤더(국기 색 배경 + 국가지도 + 당시 FIFA 랭킹) + 국가정보(수도(영문)·인구·면적·대륙) + 월드컵 역대성적(연도) + 일정 + 선수단(당시 시즌·등번호순, 사진·나이(당시), 클릭 시 상세: 소속팀·소재국·신장·체중·생일·출생지, 부상/정지 표시)
- **대회 요약 배너**(과거): 🏆우승·🥈준우승(국기+영어) · ⭐MVP · 👟골든부트, **개막식/결승 풀경기/하이라이트** 영상 버튼
- **알림 🔔**: 팀·경기 구독 → 경기 시작·골·종료 시 macOS 데스크톱 알림(앱 실행 중)
- **테마**: 라이트(기본)/다크 토글
- **저장**: 정적 정보(국가·팀·선수·사진·경기장)는 영구 파일 캐시. **완료 경기·과거 대회 전체**는 선택 시 영구 스냅샷 저장. **영상만** 매번 새로 조회.

## API (로컬)

GET: `/api/status`, `/api/matches[?year=]`, `/api/standings[?year=]`, `/api/team?id=|name=[&year=]`,
`/api/match?id=`, `/api/playerclub?name=`, `/api/highlight?q=`, `/api/wiki-image?title=`
POST: `/api/refresh`(캐시 비우기), `/api/save-edition?year=`(과거 대회 전체 저장), `/api/build-venues`(경기→도시 매핑 생성)

## 캐시/유지보수

- 캐시: `cache/*.json`. 강제 새로고침은 헤더 ↻(POST /api/refresh).
- 정적 JSON(`assets/*.json`)의 값이 틀리면 직접 편집 → 재시작 시 반영.
- TheSportsDB는 무료 한도가 빡빡해 **요청 간 0.5초 전역 스로틀** + 사진은 백그라운드 워밍(받는 즉시 영구 저장).
