"""
World Cup Pilot — local HTTP server.

Serves the single-page UI (worldcup.html) and a small JSON API that proxies
and caches Football-Data.org (https://docs.football-data.org/) so the native
window can render fixtures, group standings and team details.

Local / personal use only. The Football-Data.org token lives in config.json
next to this file (or next to the .app when bundled) and is read at startup.

API (all JSON unless noted):
  GET  /                      -> worldcup.html
  GET  /assets/<file>         -> static asset (background image, logo, ...)
  GET  /api/status            -> { token_set, mock, competition, season, dates }
  GET  /api/matches           -> { dates: [...], matches: [...], source }
  GET  /api/standings         -> { groups: [...], source }
  GET  /api/team?id=<id>      -> { team: {...}, source }
  POST /api/refresh           -> clears the on-disk cache
"""
import json
import os
import queue
import re
import threading
import time
import unicodedata

# global throttle for TheSportsDB (free tier rate-limits aggressively)
_tsdb_lock = threading.Lock()
_tsdb_last = [0.0]


def _tsdb_throttle():
    with _tsdb_lock:
        dt = time.time() - _tsdb_last[0]
        if dt < 0.5:
            time.sleep(0.5 - dt)
        _tsdb_last[0] = time.time()
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# ---- paths (overridable by the launcher when frozen) ------------------------
ROOT = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(ROOT, "worldcup.html")
ASSETS_DIR = os.path.join(ROOT, "assets")
CACHE_DIR = os.path.join(ROOT, "cache")
CONFIG_PATH = os.path.join(ROOT, "config.json")

API_BASE = "https://api.football-data.org/v4"

DEFAULTS = {
    "football_data_token": "",
    "competition": "WC",
    "season": 2026,
    "cache_ttl_seconds": 120,
    "use_mock_when_unavailable": True,
    # "현지시간" 토글이 쓰는 개최지 시간대 (football-data 가 경기장 정보를 주지 않으므로
    # 대회 개최 권역 기준 단일값. 2026 월드컵=북미, 기본 미 동부)
    "venue_timezone": "America/New_York",
}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[warn] bad config.json: {e}")
    tok = (cfg.get("football_data_token") or "").strip()
    if tok in ("", "PUT_YOUR_TOKEN_HERE"):
        cfg["football_data_token"] = ""
    return cfg


CONFIG = load_config()


def load_venues():
    """{cities: {city: tz}, matches: {matchId: city}} from assets/venues.json."""
    try:
        with open(os.path.join(ASSETS_DIR, "venues.json"), "r", encoding="utf-8") as f:
            v = json.load(f)
            return {"cities": v.get("cities", {}), "matches": v.get("matches", {})}
    except Exception:
        return {"cities": {}, "matches": {}}


VENUES = load_venues()


def load_ranking():
    """{country: rank} FIFA ranking snapshot from assets/fifa_ranking.json."""
    try:
        with open(os.path.join(ASSETS_DIR, "fifa_ranking.json"), "r", encoding="utf-8") as f:
            return json.load(f).get("ranks", {})
    except Exception:
        return {}


RANKING = load_ranking()


def load_ranking_history():
    """{year(str): {country: rank}} — FIFA ranking as of each past edition."""
    try:
        with open(os.path.join(ASSETS_DIR, "fifa_ranking_history.json"), "r", encoding="utf-8") as f:
            return json.load(f).get("byYear", {})
    except Exception:
        return {}


RANKING_HISTORY = load_ranking_history()


def load_country_info():
    try:
        with open(os.path.join(ASSETS_DIR, "country_info.json"), "r", encoding="utf-8") as f:
            return json.load(f).get("data", {})
    except Exception:
        return {}


COUNTRY = load_country_info()


def _country_norm():
    if getattr(_country_norm, "src", None) is not COUNTRY:
        _country_norm.cache = {_norm(k): v for k, v in COUNTRY.items()}
        _country_norm.src = COUNTRY
    return _country_norm.cache


def country_info(name):
    return _country_norm().get(_norm(name)) if name else None


def _ranking_norm():
    """Lazy normalized lookup (built after _norm exists; rebuilt if RANKING swaps)."""
    if getattr(_ranking_norm, "src", None) is not RANKING:
        _ranking_norm.cache = {_norm(k): v for k, v in RANKING.items()}
        _ranking_norm.src = RANKING
    return _ranking_norm.cache


def _ranking_hist_norm(year):
    """Normalized lookup for a given edition year, or None if no data for that year."""
    cache = getattr(_ranking_hist_norm, "cache", None)
    if cache is None:
        cache = _ranking_hist_norm.cache = {}
    key = str(year)
    if key not in cache:
        ranks = RANKING_HISTORY.get(key)
        cache[key] = {_norm(k): v for k, v in ranks.items()} if ranks else None
    return cache[key]


def rank_for(name, year=None):
    """FIFA ranking for a team. Current edition → current snapshot; past edition →
    that year's ranking if we have it, else None (ranking didn't exist / no data)."""
    if not name:
        return None
    if year is not None and str(year) != str(CONFIG.get("season")):
        hist = _ranking_hist_norm(year)
        return hist.get(_norm(name)) if hist else None
    return _ranking_norm().get(_norm(name))


def venue_for(match_id):
    """(city, tz) for a football-data match id, or (None, None)."""
    city = VENUES["matches"].get(str(match_id))
    if not city:
        return None, None
    return city, VENUES["cities"].get(city)


def token_ok():
    return bool(CONFIG.get("football_data_token"))


# ---- football-data.org client with disk cache -------------------------------
def _cache_file(key):
    return os.path.join(CACHE_DIR, f"{key}.json")


def _read_cache(key, ttl):
    path = _cache_file(key)
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None, None
    age = time.time() - st.st_mtime
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None, None
    fresh = age <= ttl
    return data, fresh


def _write_cache(key, data):
    os.makedirs(CACHE_DIR, exist_ok=True)
    try:
        with open(_cache_file(key), "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"[warn] cache write {key}: {e}")


def fd_get(path, cache_key, ttl=None):
    """GET {API_BASE}{path} with token, disk-cached. Returns (data, source).

    source: "live" | "cache" | "mock". Falls back to stale cache, then mock.
    """
    if ttl is None:
        ttl = int(CONFIG.get("cache_ttl_seconds", 120))
    cached, fresh = _read_cache(cache_key, ttl)
    if cached is not None and fresh:
        return cached, "cache"

    if not token_ok():
        if cached is not None:
            return cached, "cache"
        return None, "mock"

    url = f"{API_BASE}{path}"
    req = urllib.request.Request(url, headers={
        "X-Auth-Token": CONFIG["football_data_token"],
        "User-Agent": "WorldCupPilot/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.loads(r.read().decode("utf-8"))
        _write_cache(cache_key, data)
        return data, "live"
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
        print(f"[warn] football-data {path}: {e}")
        if cached is not None:
            return cached, "cache"
        return None, "mock"


# ---- normalization ----------------------------------------------------------
def _crest(team, area_flag=None):
    return team.get("crest") or area_flag or ""


def normalize_matches(raw):
    out = []
    for m in raw.get("matches", []):
        home, away = m.get("homeTeam", {}), m.get("awayTeam", {})
        score = m.get("score", {}) or {}
        ft = score.get("fullTime", {}) or {}
        city, vtz = venue_for(m.get("id"))
        out.append({
            "id": m.get("id"),
            "utcDate": m.get("utcDate"),
            "status": m.get("status"),
            "stage": m.get("stage"),
            "group": m.get("group"),
            "matchday": m.get("matchday"),
            "venueCity": city,
            "venueTz": vtz,
            "home": {"id": home.get("id"), "name": home.get("name"),
                     "tla": home.get("tla"), "crest": home.get("crest"),
                     "rank": rank_for(home.get("name"))},
            "away": {"id": away.get("id"), "name": away.get("name"),
                     "tla": away.get("tla"), "crest": away.get("crest"),
                     "rank": rank_for(away.get("name"))},
            "score": {"home": ft.get("home"), "away": ft.get("away"),
                      "winner": score.get("winner")},
        })
    out.sort(key=lambda x: (x["utcDate"] or ""))
    return out


def _stat_row(t):
    team = t.get("team", {}) or {}
    return {
        "team": {"id": team.get("id"), "name": team.get("name"),
                 "tla": team.get("tla"), "crest": team.get("crest"),
                 "rank": rank_for(team.get("name"))},
        "playedGames": t.get("playedGames") or 0,
        "won": t.get("won") or 0, "draw": t.get("draw") or 0, "lost": t.get("lost") or 0,
        "goalsFor": t.get("goalsFor") or 0, "goalsAgainst": t.get("goalsAgainst") or 0,
        "goalDifference": t.get("goalDifference") or 0, "points": t.get("points") or 0,
    }


def normalize_standings(raw, matches=None):
    """football-data returns one flat TOTAL table for the WC. Split it into the
    A..L group cards using group membership derived from the group-stage matches."""
    # team_id -> stat row (from the flat TOTAL table)
    stats = {}
    for s in raw.get("standings", []):
        if s.get("type") not in (None, "TOTAL"):
            continue
        for t in s.get("table", []):
            row = _stat_row(t)
            if row["team"]["id"] is not None:
                stats[row["team"]["id"]] = row

    # group -> {team_id -> team meta}, from matches
    membership = {}
    for m in (matches or []):
        grp = m.get("group")
        if not grp or (m.get("stage") and m["stage"] != "GROUP_STAGE"):
            continue
        bucket = membership.setdefault(grp, {})
        for side in ("home", "away"):
            tm = m.get(side) or {}
            if tm.get("id") is not None:
                bucket[tm["id"]] = tm

    if not membership:                      # no group info (e.g. mock/standings-only)
        rows = sorted(stats.values(), key=lambda r: r["points"], reverse=True)
        return [{"group": "STANDINGS", "table": rows}] if rows else []

    groups = []
    for grp in sorted(membership):
        rows = []
        for tid, meta in membership[grp].items():
            r = stats.get(tid) or _stat_row({"team": meta})
            r = dict(r)
            r["team"] = {"id": tid, "name": meta.get("name"),
                         "tla": meta.get("tla"), "crest": meta.get("crest"),
                         "rank": rank_for(meta.get("name"))}
            rows.append(r)
        rows.sort(key=lambda r: (r["points"], r["goalDifference"], r["goalsFor"]), reverse=True)
        for i, r in enumerate(rows):
            r["position"] = i + 1
        groups.append({"group": grp.replace("_", " ").title(), "table": rows})
    return groups


def normalize_team(raw):
    area = raw.get("area", {}) or {}
    squad = [{"id": p.get("id"), "name": p.get("name"),
              "position": p.get("position"), "nationality": p.get("nationality"),
              "dateOfBirth": p.get("dateOfBirth")} for p in (raw.get("squad") or [])]
    coach = raw.get("coach") or {}
    return {
        "id": raw.get("id"), "name": raw.get("name"), "tla": raw.get("tla"),
        "rank": rank_for(raw.get("name")),
        "crest": _crest(raw, area.get("flag")),
        "area": {"name": area.get("name"), "flag": area.get("flag")},
        "founded": raw.get("founded"), "address": raw.get("address"),
        "coach": {"name": coach.get("name"), "nationality": coach.get("nationality")},
        "squad": squad,
    }


def unique_dates(matches):
    return sorted({(m["utcDate"] or "")[:10] for m in matches if m.get("utcDate")})


# ---- mock data (used until a valid token / coverage is available) -----------
def _flag(code):
    return f"https://flagcdn.com/w160/{code}.png"


def mock_matches():
    teams = {
        "qat": {"id": 9001, "name": "Qatar", "tla": "QAT", "crest": _flag("qa")},
        "ecu": {"id": 9002, "name": "Ecuador", "tla": "ECU", "crest": _flag("ec")},
        "sen": {"id": 9003, "name": "Senegal", "tla": "SEN", "crest": _flag("sn")},
        "ned": {"id": 9004, "name": "Netherlands", "tla": "NED", "crest": _flag("nl")},
        "eng": {"id": 9005, "name": "England", "tla": "ENG", "crest": _flag("gb-eng")},
        "irn": {"id": 9006, "name": "Iran", "tla": "IRN", "crest": _flag("ir")},
        "usa": {"id": 9007, "name": "USA", "tla": "USA", "crest": _flag("us")},
        "kor": {"id": 9008, "name": "Korea Republic", "tla": "KOR", "crest": _flag("kr")},
    }
    def mk(mid, date, h, a, group, hs=None, asc=None, status="TIMED"):
        return {"id": mid, "utcDate": date, "status": status, "stage": "GROUP_STAGE",
                "group": group, "matchday": 1, "home": teams[h], "away": teams[a],
                "score": {"home": hs, "away": asc, "winner": None}}
    return {"matches": [
        mk(1, "2026-06-14T16:00:00Z", "qat", "ecu", "Group A", 0, 2, "FINISHED"),
        mk(2, "2026-06-14T19:00:00Z", "sen", "ned", "Group A", 0, 2, "FINISHED"),
        mk(3, "2026-06-14T22:00:00Z", "eng", "irn", "Group B", 6, 2, "FINISHED"),
        mk(4, "2026-06-15T13:00:00Z", "usa", "kor", "Group B"),
        mk(5, "2026-06-15T16:00:00Z", "qat", "sen", "Group A"),
        mk(6, "2026-06-15T19:00:00Z", "ned", "ecu", "Group A"),
        mk(7, "2026-06-16T19:00:00Z", "eng", "usa", "Group B"),
        mk(8, "2026-06-16T22:00:00Z", "irn", "kor", "Group B"),
    ]}


def mock_standings():
    def row(tid, name, code, p, w, d, l, gf, ga, pts):
        return {"team": {"id": tid, "name": name, "tla": code.upper()[:3], "crest": _flag(code)},
                "playedGames": p, "won": w, "draw": d, "lost": l,
                "goalsFor": gf, "goalsAgainst": ga, "goalDifference": gf - ga, "points": pts}
    # team ids align with mock_matches() so group bucketing finds their stats
    return {"standings": [{"type": "TOTAL", "table": [
        row(9004, "Netherlands", "nl", 1, 1, 0, 0, 2, 0, 3),
        row(9002, "Ecuador", "ec", 1, 1, 0, 0, 2, 0, 3),
        row(9003, "Senegal", "sn", 1, 0, 0, 1, 0, 2, 0),
        row(9001, "Qatar", "qa", 1, 0, 0, 1, 0, 2, 0),
        row(9005, "England", "gb-eng", 1, 1, 0, 0, 6, 2, 3),
        row(9007, "USA", "us", 0, 0, 0, 0, 0, 0, 0),
        row(9008, "Korea Republic", "kr", 0, 0, 0, 0, 0, 0, 0),
        row(9006, "Iran", "ir", 1, 0, 0, 1, 2, 6, 0)]}]}


def mock_team(team_id):
    names = {9004: ("Netherlands", "nl"), 9005: ("England", "gb-eng"),
             9008: ("Korea Republic", "kr")}
    name, code = names.get(int(team_id), ("Sample National Team", "un"))
    return {"id": int(team_id), "name": name, "tla": name[:3].upper(),
            "crest": _flag(code), "area": {"name": name, "flag": _flag(code)},
            "founded": 1889, "coach": {"name": "—", "nationality": name},
            "squad": [{"id": 1, "name": "Player One", "position": "Goalkeeper",
                       "nationality": name, "dateOfBirth": "1995-01-01"},
                      {"id": 2, "name": "Player Two", "position": "Defence",
                       "nationality": name, "dateOfBirth": "1997-03-12"},
                      {"id": 3, "name": "Player Three", "position": "Midfield",
                       "nationality": name, "dateOfBirth": "1998-07-20"},
                      {"id": 4, "name": "Player Four", "position": "Offence",
                       "nationality": name, "dateOfBirth": "2000-11-05"}]}


# ---- data assembly ----------------------------------------------------------
def get_matches():
    comp = CONFIG.get("competition", "WC")
    season = CONFIG.get("season")
    q = f"?season={season}" if season else ""
    raw, source = fd_get(f"/competitions/{comp}/matches{q}", "matches")
    if raw is None and CONFIG.get("use_mock_when_unavailable", True):
        raw, source = mock_matches(), "mock"
    matches = normalize_matches(raw or {"matches": []})
    return {"dates": unique_dates(matches), "matches": matches, "source": source}


def _espn_status(s):
    n = ((s or {}).get("type") or {}).get("name", "")
    if "FINAL" in n or n == "STATUS_FULL_TIME":
        return "FINISHED"
    if any(k in n for k in ("HALF", "IN_PROGRESS", "OVERTIME", "SHOOTOUT", "PROGRESS")):
        return "IN_PLAY"
    return "SCHEDULED"


_ESPN_STAGE = {"group-stage": "GROUP_STAGE", "round-of-32": "LAST_32", "round-of-16": "LAST_16",
               "quarterfinals": "QUARTER_FINALS", "semifinals": "SEMI_FINALS",
               "3rd-place-match": "THIRD_PLACE", "final": "FINAL"}


def get_matches_espn(year):
    """Past editions: full match list from ESPN (one call per year, permanent cache)."""
    data = http_json(f"{ESPN_BASE}/scoreboard?dates={year}", f"espn-year-{year}", ttl=10 ** 9)
    out = []
    for ev in (data or {}).get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        stage = _ESPN_STAGE.get((ev.get("season") or {}).get("slug"))
        cs = {c.get("homeAway"): c for c in comp.get("competitors", [])}
        h, a = cs.get("home", {}), cs.get("away", {})

        def team(c):
            t = c.get("team", {}) or {}
            return {"id": t.get("id"), "name": t.get("displayName"), "tla": t.get("abbreviation"),
                    "crest": t.get("logo"), "rank": rank_for(t.get("displayName"), year)}

        def sc(c):
            try:
                return int(c.get("score"))
            except (TypeError, ValueError):
                return None
        v = comp.get("venue", {}) or {}
        city = (v.get("address", {}) or {}).get("city")
        out.append({"id": ev.get("id"), "utcDate": ev.get("date"), "status": _espn_status(ev.get("status")),
                    "stage": stage, "group": None, "matchday": None,
                    "venueCity": city, "venueTz": VENUES["cities"].get(city),
                    "home": team(h), "away": team(a),
                    "score": {"home": sc(h), "away": sc(a), "winner": None}})
    out.sort(key=lambda x: x["utcDate"] or "")
    return {"dates": unique_dates(out), "matches": out, "source": "espn"}


def get_standings_espn(year):
    """Group standings for a past edition from ESPN (permanent cache)."""
    data = http_json(f"https://site.api.espn.com/apis/v2/sports/soccer/fifa.world/standings?season={year}",
                     f"espn-standings-{year}", ttl=10 ** 9)
    groups = []
    for ch in (data or {}).get("children", []):
        rows = []
        for e in (ch.get("standings") or {}).get("entries", []):
            t = e.get("team", {}) or {}
            st = {s.get("name"): s.get("value") for s in e.get("stats", [])}
            i = lambda k: int(st.get(k, 0) or 0)
            rows.append({"position": i("rank") or None,
                         "team": {"id": t.get("id"), "name": t.get("displayName"), "tla": t.get("abbreviation"),
                                  "crest": (t.get("logos") or [{}])[0].get("href"),
                                  "rank": rank_for(t.get("displayName"), year)},
                         "playedGames": i("gamesPlayed"), "won": i("wins"), "draw": i("ties"), "lost": i("losses"),
                         "goalsFor": i("pointsFor"), "goalsAgainst": i("pointsAgainst"),
                         "goalDifference": i("pointDifferential"), "points": i("points")})
        rows.sort(key=lambda r: r["position"] or 99)
        groups.append({"group": ch.get("name"), "table": rows})
    return {"groups": groups, "source": "espn"}


_saving = set()


def save_edition(year):
    """Past editions are fixed → persist the whole snapshot (matches, standings, every
    match detail) permanently in the background. Videos are NOT saved (fetched on click)."""
    marker = f"edition-saved-{year}"
    done, _ = _read_cache(marker, 10 ** 9)
    if done:
        return {"saved": True}
    if year in _saving:
        return {"saving": True}
    _saving.add(year)

    def run():
        try:
            data = get_matches_espn(year)          # cached permanently
            get_standings_espn(year)               # cached permanently
            for m in data.get("matches", []):
                get_match_espn(str(m["id"]))       # caches each match's detail (events/venue)
            _write_cache(marker, {"done": True, "matches": len(data.get("matches", []))})
            print(f"[info] edition {year} snapshot saved ({len(data.get('matches', []))} matches)")
        except Exception as e:
            print(f"[warn] save_edition {year}: {e}")
        finally:
            _saving.discard(year)
    threading.Thread(target=run, daemon=True).start()
    return {"saving": True}


def get_standings():
    comp = CONFIG.get("competition", "WC")
    season = CONFIG.get("season")
    q = f"?season={season}" if season else ""
    # standings change only as matches finish — cache longer to avoid slow cold fetches
    raw, source = fd_get(f"/competitions/{comp}/standings{q}", "standings", ttl=300)
    if raw is None and CONFIG.get("use_mock_when_unavailable", True):
        raw, source = mock_standings(), "mock"
    matches = get_matches()["matches"]      # group membership comes from fixtures
    return {"groups": normalize_standings(raw or {"standings": []}, matches), "source": source}


def get_team(team_id):
    raw, source = fd_get(f"/teams/{team_id}", f"team-{team_id}", ttl=10 ** 9)  # squad/coach static
    if raw is None and CONFIG.get("use_mock_when_unavailable", True):
        raw, source = mock_team(team_id), "mock"
    if raw is None:
        return {"team": None, "source": "mock"}
    team = normalize_team(raw)
    team["info"] = country_info(team.get("name"))   # capital/population/area + World Cup history
    # squad from ESPN (free): photo, height/weight, availability; AF fills face photos by jersey number
    try:
        roster = espn_roster(team.get("name"), season=CONFIG.get("season"))
    except Exception as e:
        print(f"[warn] espn roster: {e}")
        roster = None
    if roster:
        # photos: cache-only for a fast response; fetch the rest in the background for next time
        for pl in roster:
            if not pl.get("photo"):
                ph = tsdb_player_cached(pl.get("name"))
                if ph:
                    pl["photo"] = ph
        warm_photos_bg([pl.get("name") for pl in roster if not pl.get("photo")])
        team["squad"] = roster
        team["hasPhotos"] = True
    return {"team": team, "source": source}


# ---- match detail: ESPN (events/venue/live) + venue image + Open-Meteo ------
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world"

# team-name aliases so football-data names match ESPN's
_ALIAS = {
    "turkey": "turkiye", "southkorea": "korearepublic", "korea": "korearepublic",
    "republicofkorea": "korearepublic", "unitedstates": "usa", "us": "usa",
    "ivorycoast": "cotedivoire", "czechia": "czechrepublic", "congodr": "drcongo",
    "democraticrepublicofcongo": "drcongo", "capeverdeislands": "capeverde",
    "iranislamicrepublic": "iran", "bosniaherzegovina": "bosnia",
}


def _norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(ch for ch in s.lower() if ch.isalnum())


def _canon(name):
    n = _norm(name)
    return _ALIAS.get(n, n)


def http_json(url, cache_key, ttl, headers=None):
    cached, fresh = _read_cache(cache_key, ttl)
    if cached is not None and fresh:
        return cached
    if "thesportsdb.com" in url:   # respect the free tier's rate limit
        _tsdb_throttle()
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "WorldCupPilot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.loads(r.read().decode("utf-8"))
        _write_cache(cache_key, data)
        return data
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError) as e:
        print(f"[warn] {cache_key}: {e}")
        return cached


def espn_find(date_yyyymmdd, home, away):
    """Find the ESPN event on a date whose two teams match home/away."""
    data = http_json(f"{ESPN_BASE}/scoreboard?dates={date_yyyymmdd}",
                     f"espn-sb-{date_yyyymmdd}", ttl=30)
    if not data:
        return None
    want = {_canon(home), _canon(away)}
    for ev in data.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        names = {_canon(c.get("team", {}).get("displayName")) for c in comp.get("competitors", [])}
        if want & names == want:
            return ev
    return None


def _ml_to_decimal(ml):
    """American moneyline -> decimal odds (배당)."""
    if ml in (None, ""):
        return None
    try:
        ml = float(ml)
    except (TypeError, ValueError):
        return None
    return round((ml / 100 + 1) if ml > 0 else (100 / abs(ml) + 1), 2)


def espn_events(espn_id, home_team_id, away_team_id):
    data = http_json(f"{ESPN_BASE}/summary?event={espn_id}", f"espn-sum-{espn_id}", ttl=30)
    if not data:
        return [], None, None
    out = []
    for k in data.get("keyEvents", []):
        ttext = (k.get("type", {}) or {}).get("text", "")
        low = ttext.lower()
        if not any(w in low for w in ("goal", "card", "penalty")):
            continue
        tid = str((k.get("team", {}) or {}).get("id") or "")
        side = "home" if tid == str(home_team_id) else ("away" if tid == str(away_team_id) else None)
        players = [a.get("athlete", {}).get("displayName") for a in k.get("participants", [])]
        out.append({
            "minute": (k.get("clock", {}) or {}).get("displayValue", ""),
            "type": ttext, "side": side,
            "player": next((p for p in players if p), ""),
            "text": k.get("text", ""),
        })
    venue = (data.get("gameInfo", {}) or {}).get("venue", {})
    # 1X2 betting odds from the pickcenter (DraftKings etc.)
    odds = None
    pc = data.get("pickcenter") or []
    if pc:
        p = pc[0]
        home = _ml_to_decimal((p.get("homeTeamOdds") or {}).get("moneyLine"))
        draw = _ml_to_decimal((p.get("drawOdds") or {}).get("moneyLine"))
        away = _ml_to_decimal((p.get("awayTeamOdds") or {}).get("moneyLine"))
        if any(v is not None for v in (home, draw, away)):
            odds = {"provider": (p.get("provider") or {}).get("name"),
                    "home": home, "draw": draw, "away": away}
    return out, venue, odds


def wiki_image(name):
    """Stadium photo from the Wikipedia page summary (free, reliable for venues)."""
    if not name:
        return None
    title = urllib.parse.quote(name.replace(" ", "_"))
    data = http_json(f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}",
                     f"wiki-{_norm(name)}", ttl=10 ** 9)
    if not data:
        return None
    return ((data.get("originalimage") or {}).get("source")
            or (data.get("thumbnail") or {}).get("source"))


def geocode(city):
    if not city:
        return None
    url = f"https://geocoding-api.open-meteo.com/v1/search?name={urllib.parse.quote(city)}&count=1&language=en"
    data = http_json(url, f"geo-{_norm(city)}", ttl=10 ** 9)
    res = (data or {}).get("results") or []
    return (res[0]["latitude"], res[0]["longitude"]) if res else None


def weather_at(city, utc_iso):
    coord = geocode(city)
    if not coord:
        return {}
    lat, lon = coord
    day, hour = utc_iso[:10], utc_iso[11:13]
    base = ("https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            "&hourly=temperature_2m,relative_humidity_2m"
            f"&start_date={day}&end_date={day}&timezone=UTC")
    data = http_json(base, f"wx-{_norm(city)}-{day}", ttl=1800)
    h = (data or {}).get("hourly") or {}
    times = h.get("time") or []
    target = f"{day}T{hour}:00"
    idx = next((i for i, t in enumerate(times) if t == target), None)
    if idx is None and times:
        idx = 0
    if idx is None:
        return {}
    return {"temp": (h.get("temperature_2m") or [None])[idx],
            "humidity": (h.get("relative_humidity_2m") or [None])[idx]}


def youtube_first_video(query):
    """First YouTube videoId for a query (scraped from results HTML, no API key)."""
    key = "yt-" + _norm(query)[:60]
    cached, fresh = _read_cache(key, 86400)   # 1 day — FIFA may upload newer/better videos
    if cached and cached.get("videoId") and fresh:
        return cached["videoId"]
    url = ("https://www.youtube.com/results?search_query="
           + urllib.parse.quote(query) + "&hl=en&gl=US")
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            html = r.read().decode("utf-8", "ignore")
    except Exception as e:
        print(f"[warn] youtube search: {e}")
        return cached.get("videoId") if cached else None
    m = re.search(r'"videoId":"([A-Za-z0-9_-]{11})"', html)
    vid = m.group(1) if m else None
    if vid:
        _write_cache(key, {"videoId": vid})
    return vid


ESPN_POS = {"G": "Goalkeeper", "D": "Defence", "M": "Midfield", "F": "Offence",
            "Goalkeeper": "Goalkeeper", "Defender": "Defence",
            "Midfielder": "Midfield", "Forward": "Offence", "Attacker": "Offence"}


def _in_to_cm(v):
    try:
        return round(float(v) * 2.54)
    except (TypeError, ValueError):
        return None


def _lb_to_kg(v):
    try:
        return round(float(v) * 0.4536)
    except (TypeError, ValueError):
        return None


def espn_team_id(name):
    data = http_json(f"{ESPN_BASE}/teams", "espn-teams", ttl=30 * 86400)
    try:
        teams = data["sports"][0]["leagues"][0]["teams"]
    except (TypeError, KeyError, IndexError):
        return None
    want = _canon(name)
    for x in teams:
        t = x.get("team", {}) or {}
        if want in (_canon(t.get("displayName")), _canon(t.get("shortDisplayName")), _canon(t.get("name"))):
            return t.get("id")
    return None


def espn_roster(name, season=None):
    """Squad from ESPN for a given edition (season). dateOfBirth → age AT that tournament."""
    tid = espn_team_id(name)
    if not tid:
        return None
    qs = f"?season={season}" if season else ""
    data = http_json(f"{ESPN_BASE}/teams/{tid}/roster{qs}", f"espn-roster-{tid}-{season or 'cur'}", ttl=10 ** 9)
    if not data:
        return None
    # availability (injury/suspension) only matters for the current edition;
    # for past editions everyone listed actually took part — ignore today's status
    is_current = (not season) or str(season) == str(CONFIG.get("season"))
    out = []
    for a in data.get("athletes", []):
        pos = a.get("position", {}) or {}
        inj = a.get("injuries") or []
        st = a.get("status", {}) or {}
        if not is_current:
            avail, status_text = True, None
        elif inj:
            avail, status_text = False, "부상"
        elif st.get("type") and st.get("type") != "active":
            avail, status_text = False, st.get("name")
        else:
            avail, status_text = True, None
        bp = a.get("birthPlace") or {}
        photo = (a.get("headshot") or {}).get("href")   # ESPN soccer headshots are sparse; TheSportsDB fills most
        dob = (a.get("dateOfBirth") or "")[:10]
        age = a.get("age")
        if season and dob[:4].isdigit():        # age at the tournament, not today
            age = int(season) - int(dob[:4])
        out.append({
            "id": a.get("id"),
            "name": a.get("displayName") or a.get("fullName"),
            "number": a.get("jersey"),
            "position": ESPN_POS.get(pos.get("abbreviation")) or ESPN_POS.get(pos.get("name")) or "기타",
            "age": age,
            "dateOfBirth": dob,
            "photo": photo,
            "height": _in_to_cm(a.get("height")),
            "weight": _lb_to_kg(a.get("weight")),
            "birthPlace": ", ".join(x for x in (bp.get("city"), bp.get("country")) if x),
            "club": (a.get("defaultTeam") or {}).get("displayName"),
            "available": avail,
            "statusText": status_text,
        })
    return out or None


TSDB = "https://www.thesportsdb.com/api/v1/json/3"


def tsdb_team_id(name):
    data = http_json(f"{TSDB}/searchteams.php?t={urllib.parse.quote(name)}",
                     f"tsdb-t-{_norm(name)}", ttl=10 ** 9)
    teams = (data or {}).get("teams") or []
    want = _canon(name)
    for t in teams:
        if t.get("strSport") == "Soccer" and _canon(t.get("strTeam")) == want:
            return t.get("idTeam")
    for t in teams:
        if t.get("strSport") == "Soccer":
            return t.get("idTeam")
    return None


def tsdb_photos(name):
    """{normalized player name: photo url} from TheSportsDB (free, no daily cap)."""
    tid = tsdb_team_id(name)
    if not tid:
        return {}
    data = http_json(f"{TSDB}/lookup_all_players.php?id={tid}", f"tsdb-p-{tid}", ttl=14 * 86400)
    out = {}
    for p in (data or {}).get("player") or []:
        ph = p.get("strCutout") or p.get("strThumb")
        if ph and p.get("strPlayer"):
            out[_norm(p.get("strPlayer"))] = ph
    return out


def tsdb_player(name):
    """{photo, club} for a player by name from TheSportsDB (free, no daily cap), cached."""
    if not name:
        return {}
    data = http_json(f"{TSDB}/searchplayers.php?p={urllib.parse.quote(name)}",
                     f"tsdb-pl-{_norm(name)}", ttl=10 ** 9)
    for p in (data or {}).get("player") or []:
        return {"photo": p.get("strCutout") or p.get("strThumb"), "club": p.get("strTeam")}
    return {}


def tsdb_player_cached(name):
    """Cache-only (no network) photo lookup — keeps team detail fast."""
    if not name:
        return None
    cached, _ = _read_cache(f"tsdb-pl-{_norm(name)}", 10 ** 9)
    for p in (cached or {}).get("player") or []:
        return p.get("strCutout") or p.get("strThumb")
    return None


# single background worker drains a photo-warm queue (avoids a thread storm + dedupes)
_warm_q = queue.Queue()
_warm_seen = set()


def _warm_worker():
    while True:
        name = _warm_q.get()
        try:
            tsdb_player(name)   # throttled fetch + permanent cache
        except Exception:
            pass
        _warm_q.task_done()


threading.Thread(target=_warm_worker, daemon=True).start()


def warm_photos_bg(names):
    """Queue missing player photos for the single background worker (gentle, deduped)."""
    for n in names:
        if n and n not in _warm_seen:
            _warm_seen.add(n)
            _warm_q.put(n)


def tsdb_team_country(club):
    if not club:
        return None
    data = http_json(f"{TSDB}/searchteams.php?t={urllib.parse.quote(club)}",
                     f"tsdb-club-{_norm(club)}", ttl=10 ** 9)
    for t in (data or {}).get("teams") or []:
        if t.get("strSport") == "Soccer" and t.get("strCountry"):
            return t.get("strCountry")
    return None


def tsdb_player_clubinfo(name):
    club = tsdb_player(name).get("club")
    return {"club": club, "clubCountry": tsdb_team_country(club) if club else None}


def get_match_espn(espn_id):
    """Match detail for a past-edition match (ESPN event id): score, events, venue, weather."""
    saved, _ = _read_cache(f"match-final-{espn_id}", 10 ** 9)
    if saved is not None:
        return {"match": saved}
    data = http_json(f"{ESPN_BASE}/summary?event={espn_id}", f"espn-sum-{espn_id}", ttl=60)
    if not data:
        return {"match": None}
    comp = ((data.get("header") or {}).get("competitions") or [{}])[0]
    cs = {c.get("homeAway"): c for c in comp.get("competitors", [])}

    def team(c):
        t = c.get("team", {}) or {}
        return {"id": t.get("id"), "name": t.get("displayName") or t.get("name"),
                "tla": t.get("abbreviation"), "crest": (t.get("logos") or [{}])[0].get("href"), "rank": None}

    def sc(c):
        try:
            return int(c.get("score"))
        except (TypeError, ValueError):
            return None
    h, a = cs.get("home", {}), cs.get("away", {})
    status = _espn_status(comp.get("status"))
    events, venue, odds = espn_events(espn_id, (h.get("team") or {}).get("id"), (a.get("team") or {}).get("id"))
    vaddr = (venue or {}).get("address", {}) or {}
    vname, vcity = (venue or {}).get("fullName"), vaddr.get("city")
    utc = comp.get("date") or ""
    out = {"id": espn_id, "home": team(h), "away": team(a), "utcDate": utc, "status": status,
           "score": {"home": sc(h), "away": sc(a)}, "group": None, "espnMatched": True, "espnId": espn_id,
           "events": events, "odds": odds, "attendance": None,
           "venue": {"name": vname, "city": vcity, "country": vaddr.get("country"),
                     "image": wiki_image(vname) if vname else None, "capacity": None, "surface": None},
           "weather": weather_at(vcity.split(",")[0].strip(), utc) if (vcity and utc) else {}}
    if status == "FINISHED":
        _write_cache(f"match-final-{espn_id}", out)
    return {"match": out}


def get_team_by_name(name, year=None):
    """Past-edition team detail: country info + WC history + that edition's ESPN squad (당시 나이)."""
    iso = (country_info(name) or {}).get("iso2")
    team = {"id": name, "name": name, "tla": None, "rank": rank_for(name, year),
            "crest": f"https://flagcdn.com/w160/{iso}.png" if iso else None,
            "area": {"name": name, "flag": None}, "founded": None, "coach": {"name": None},
            "info": country_info(name), "squad": [], "hasPhotos": False}
    try:
        roster = espn_roster(name, season=year)
        if roster:
            for pl in roster:
                if not pl.get("photo"):
                    ph = tsdb_player_cached(pl.get("name"))
                    if ph:
                        pl["photo"] = ph
            warm_photos_bg([pl.get("name") for pl in roster if not pl.get("photo")])
            team["squad"] = roster
            team["hasPhotos"] = True
    except Exception as e:
        print(f"[warn] team_by_name roster: {e}")
    return {"team": team, "source": "espn"}


def get_match(fd_id):
    # finished matches never change → serve from permanent cache, skipping all live calls
    saved, _ = _read_cache(f"match-final-{fd_id}", 10 ** 9)
    if saved is not None:
        return {"match": saved}
    matches = get_matches()["matches"]
    m = next((x for x in matches if str(x.get("id")) == str(fd_id)), None)
    if not m:
        return get_match_espn(fd_id)   # past-edition match (ESPN id)
    home, away = m["home"]["name"], m["away"]["name"]
    utc = m.get("utcDate") or ""
    out = {"id": fd_id, "home": m["home"], "away": m["away"], "utcDate": utc,
           "status": m.get("status"), "score": m.get("score"), "group": m.get("group"),
           "venue": None, "weather": {}, "events": [], "attendance": None,
           "odds": None, "espnMatched": False}
    # ESPN lookup over the match's UTC date +/- 1 (scoreboard is by calendar day)
    base = utc[:10].replace("-", "")
    cands = [base]
    if utc:
        d = time.strptime(utc[:10], "%Y-%m-%d")
        epoch = time.mktime(d)
        cands += [time.strftime("%Y%m%d", time.localtime(epoch + 86400)),
                  time.strftime("%Y%m%d", time.localtime(epoch - 86400))]
    ev = None
    for dt in cands:
        ev = espn_find(dt, home, away)
        if ev:
            break
    venue_name = venue_city = venue_country = None
    if ev:
        out["espnMatched"] = True
        out["espnId"] = ev.get("id")
        comp = (ev.get("competitions") or [{}])[0]
        v = comp.get("venue", {}) or {}
        venue_name = v.get("fullName")
        venue_city = (v.get("address", {}) or {}).get("city")
        venue_country = (v.get("address", {}) or {}).get("country")
        out["attendance"] = comp.get("attendance")
        st = (ev.get("status", {}) or {}).get("type", {}) or {}
        out["liveStatus"] = st.get("description")
        ids = {c.get("homeAway"): c.get("team", {}).get("id") for c in comp.get("competitors", [])}
        out["events"], gv, out["odds"] = espn_events(ev.get("id"), ids.get("home"), ids.get("away"))
        if not venue_name and gv:
            venue_name = gv.get("fullName")
    # venue card: name/city from ESPN, image from Wikipedia (free), weather from Open-Meteo
    if venue_name:
        out["venue"] = {"name": venue_name, "city": venue_city, "country": venue_country,
                        "image": wiki_image(venue_name), "capacity": None, "surface": None}
    if venue_city and utc:
        out["weather"] = weather_at(venue_city.split(",")[0].strip(), utc)
    if out.get("status") == "FINISHED":   # save finished matches permanently
        _write_cache(f"match-final-{fd_id}", out)
    return {"match": out}


def _espn_event_for(m):
    """Find the ESPN event for a football-data match over its UTC date +/- 1."""
    utc = m.get("utcDate") or ""
    if not utc:
        return None
    base = utc[:10].replace("-", "")
    epoch = time.mktime(time.strptime(utc[:10], "%Y-%m-%d"))
    for dt in (base, time.strftime("%Y%m%d", time.localtime(epoch + 86400)),
               time.strftime("%Y%m%d", time.localtime(epoch - 86400))):
        ev = espn_find(dt, m["home"]["name"], m["away"]["name"])
        if ev:
            return ev
    return None


def build_venues():
    """Map every football-data match id -> host city (via ESPN) and persist it to
    assets/venues.json, so '현지' time becomes per-match accurate. Dev-time step;
    the generated file is then bundled. Returns stats incl. any unknown cities."""
    global VENUES
    path = os.path.join(ASSETS_DIR, "venues.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:
        doc = {"cities": VENUES.get("cities", {}), "matches": {}}
    cities = doc.get("cities", {})
    by_lower = {k.lower(): k for k in cities}

    mapping, unknown = {}, {}
    stats = {"total": 0, "mapped": 0, "no_event": 0, "unknown_city": 0}
    for m in get_matches()["matches"]:
        stats["total"] += 1
        ev = _espn_event_for(m)
        if not ev:
            stats["no_event"] += 1
            continue
        comp = (ev.get("competitions") or [{}])[0]
        city = ((comp.get("venue", {}) or {}).get("address", {}) or {}).get("city", "")
        key = (city or "").split(",")[0].strip()
        canon = by_lower.get(key.lower())
        if not canon:
            stats["unknown_city"] += 1
            unknown[key] = unknown.get(key, 0) + 1
            continue
        mapping[str(m["id"])] = canon
        stats["mapped"] += 1

    doc["matches"] = mapping
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    VENUES = load_venues()
    stats["unknown"] = unknown
    return stats


# ---- HTTP -------------------------------------------------------------------
_ASSET_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".gif": "image/gif", ".svg": "image/svg+xml", ".webp": "image/webp",
                ".ico": "image/x-icon"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            self._json(404, {"error": "not found"})
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html", "/worldcup.html"):
            return self._file(HTML, "text/html; charset=utf-8")
        if path.startswith("/assets/"):
            name = os.path.basename(path)
            full = os.path.join(ASSETS_DIR, name)
            ext = os.path.splitext(name)[1].lower()
            return self._file(full, _ASSET_TYPES.get(ext, "application/octet-stream"))
        if path == "/api/status":
            data = get_matches()
            return self._json(200, {
                "token_set": token_ok(),
                "mock": data["source"] == "mock",
                "competition": CONFIG.get("competition"),
                "season": CONFIG.get("season"),
                "venue_timezone": CONFIG.get("venue_timezone", "America/New_York"),
                "dates": data["dates"],
            })
        if path == "/api/matches":
            q = parse_qs(urlparse(self.path).query)
            year = (q.get("year") or [""])[0]
            if year and year != str(CONFIG.get("season")):
                return self._json(200, get_matches_espn(year))
            return self._json(200, get_matches())
        if path == "/api/standings":
            q = parse_qs(urlparse(self.path).query)
            year = (q.get("year") or [""])[0]
            if year and year != str(CONFIG.get("season")):
                return self._json(200, get_standings_espn(year))
            return self._json(200, get_standings())
        if path == "/api/team":
            q = parse_qs(urlparse(self.path).query)
            tid = (q.get("id") or [""])[0]
            name = (q.get("name") or [""])[0]
            year = (q.get("year") or [""])[0]
            if name:
                return self._json(200, get_team_by_name(name, year or None))
            if not tid:
                return self._json(400, {"error": "missing id"})
            return self._json(200, get_team(tid))
        if path == "/api/wiki-image":
            q = parse_qs(urlparse(self.path).query)
            title = (q.get("title") or [""])[0]
            return self._json(200, {"image": wiki_image(title) if title else None})
        if path == "/api/playerclub":
            q = parse_qs(urlparse(self.path).query)
            name = (q.get("name") or [""])[0]
            try:
                return self._json(200, tsdb_player_clubinfo(name))
            except Exception as e:
                print(f"[warn] playerclub: {e}")
                return self._json(200, {"club": None, "clubCountry": None})
        if path == "/api/match":
            q = parse_qs(urlparse(self.path).query)
            mid = (q.get("id") or [""])[0]
            if not mid:
                return self._json(400, {"error": "missing id"})
            return self._json(200, get_match(mid))
        if path == "/api/highlight":
            q = parse_qs(urlparse(self.path).query)
            query = (q.get("q") or [""])[0]
            if not query:
                return self._json(400, {"error": "missing q"})
            return self._json(200, {"videoId": youtube_first_video(query)})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/refresh":
            removed = 0
            try:
                for f in os.listdir(CACHE_DIR):
                    if f.endswith(".json"):
                        os.remove(os.path.join(CACHE_DIR, f))
                        removed += 1
            except FileNotFoundError:
                pass
            return self._json(200, {"ok": True, "removed": removed})
        if path == "/api/save-edition":
            q = parse_qs(urlparse(self.path).query)
            year = (q.get("year") or [""])[0]
            return self._json(200, save_edition(year) if year else {"error": "missing year"})
        if path == "/api/build-venues":
            try:
                return self._json(200, {"ok": True, "stats": build_venues()})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})
        return self._json(404, {"error": "not found"})


def main():
    os.makedirs(CACHE_DIR, exist_ok=True)
    host, port = "127.0.0.1", 8770
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"World Cup Pilot server on http://{host}:{port}  (token_set={token_ok()})")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
