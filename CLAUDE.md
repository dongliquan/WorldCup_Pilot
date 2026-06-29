# World Cup Pilot — 작업 규약 (Claude)

개인용 Windows 데스크톱 FIFA 월드컵 뷰어 + 예측 엔진. `worldcup.py`(런처) + `server.py`(로컬 서버
`127.0.0.1:8770`) + `worldcup.html`(단일 UI) + PyInstaller 단일 exe(`worldcup_win.spec`). **Windows 전용.**

---

## 캐싱 / API 호출 원칙 (★ 최우선 — 항상 적용)

> **확정된 것은 캐시해서 쓴다. API는 꼭 필요할 때만 부른다.** 끝난 경기·확정 일정처럼 다시 안 바뀌는
> 데이터는 영구 캐시에서 즉시 서빙 → 빠르고, 외부 호출 0.

새 기능/엔드포인트를 추가하거나 데이터를 가져올 때 **반드시** 아래를 먼저 따진다.

1. **이 데이터가 바뀔 수 있나?**
   - **안 바뀜**(일정 골격, 끝난 경기 결과·이벤트·라인업, 과거 대회, 국가/선수 기본정보, AI 픽 등):
     **영구 캐시(`ttl=10**9`)**. 한 번 받으면 다시 호출하지 않는다.
   - **바뀜**(라이브 스코어, 진행 중 순위, 채워지는 녹아웃 대진): **꼭 변할 수 있는 시점에만** 짧게 호출.

2. **"꼭 필요할 때"의 기준 — 적응형 TTL (`_live_ttl`)**
   스코어보드/순위는 전역 짧은 TTL로 무조건 폴링하지 **않는다**. 대회 상태로 주기를 정한다:
   - 경기가 **라이브**(`state=="in"`) 또는 **킥오프 지났는데 아직 final 아님** → `cache_ttl_seconds`(짧게).
   - 경기가 **방금 끝남**(≤4h) → 300s 정도(녹아웃 대진 채워지는 것 포착).
   - 그 외 → **다음 킥오프 2분 전까지 캐시 유지**(최대 6h 캡). 경기일 사이엔 사실상 호출 0.
   - **전 경기 final** → 영구 캐시.

3. **끝난 경기는 개별 영구 저장.** `match-final-{id}` 패턴처럼, 경기가 FINISHED가 되는 순간
   필요한 정보(스코어·이벤트·스탯·라인업·폼·h2h)를 캐시에 박제하고, 이후엔 그 캐시만 읽는다.
   상세 summary를 매번 다시 받지 않는다.

4. **단일 호출로 묶어라.** ESPN 스코어보드는 한 번 호출로 104경기 전체를 준다. 경기마다 따로 부르지 말 것.
   - ⚠️ `scoreboard?dates=YYYY`에는 **`&limit=300` 필수**. 기본 100이라 2026 WC 104경기 중 마지막 4경기
     (준결승·3·4위전·결승)가 조용히 잘린다.

5. **확정 데이터는 `dist/cache/`에 커밋.** 신규 클론/exe가 네트워크 없이도 즉시 뜨고, 외부 소스가
   잘리거나 죽어도 안전망이 된다. 일정 골격(`espn-year-*.json`), 파생 계산(advtrend/advodds/accuracy),
   AI 픽(aipick/modelpick)은 커밋 대상. 큰 재취득 가능 blob(`img/`, `espn-sum-*`)은 gitignore.

**안티패턴**: 모든 호출에 똑같은 60초 TTL 박기 / 끝난 경기를 매번 다시 fetch / 화면 열 때마다 summary
재요청 / 경기마다 개별 API 호출 / 확정 데이터를 캐시 안 하고 항상 라이브로.

---

## 캐시 구조

- 런타임 캐시: `cache/`(gitignore). 커밋 데이터셋: `dist/cache/`. exe는 `dist/cache`를 읽고,
  `seed_cache()`가 커밋된 파생 캐시(aipick/modelpick/advtrend/advodds/accuracy)를 런타임 캐시에 시드.
- 2계층 TTL: 짧은 ttl(라이브) + 영구(완료/파생). 시그니처 키 재계산
  (`_group_stage_signature`는 조별 결과만 → 녹아웃 진행 중에도 안정).
- 원자적 쓰기(`_write_cache`: temp + `os.replace`), 요청 단위 메모이즈(`_req_memo`).

## 검증 워크플로

- `python -m py_compile server.py`
- 인라인 JS: `node -e`로 `<script>` 블록 신택스 체크(아래 한 줄).
- 서버 재시작 전 8770 점유 정리: `netstat -ano | grep 8770.*LISTEN` → `taskkill //F //PID <pid>`
  (SO_REUSEADDR로 두 인스턴스가 붙어 옛 HTML이 서빙되는 함정 주의).
- 엔드포인트 점검: `/api/matches`(104), `/api/standings`(12조), `/api/advtrend`, `/api/accuracy`.

```bash
node -e 'const fs=require("fs");const h=fs.readFileSync("worldcup.html","utf8");const re=/<script\b[^>]*>([\s\S]*?)<\/script>/gi;let m,i=0,b=0;while((m=re.exec(h))){i++;try{new Function(m[1])}catch(e){b++;console.log("#"+i,e.message)}}console.log("scripts",i,"errors",b)'
```

## 빌드

```
python -m PyInstaller --noconfirm worldcup_win.spec   # -> dist/WorldCupPilot.exe (단일 파일)
```
빌드 전 8770 리스너 정리. exe는 약 16~17MB.

## 데이터 소스 (전부 무료·토큰 불필요)

ESPN(일정·스코어·순위·대진·summary·roster) · TheSportsDB→Wikipedia(사진, 전역 스로틀) · flagcdn(국기) ·
Open-Meteo(날씨) · YouTube(영상) · Groq/OpenAI(AI 예측). football-data.org / API-Football **미사용**.
