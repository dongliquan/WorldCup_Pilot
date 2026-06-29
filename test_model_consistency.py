"""Golden test: the Elo x Score prediction math is duplicated across four places that must
agree (the JS runtime serves what the Python tuner optimised):

  • worldcup.html   computePrediction / teamRating   — the runtime users actually see
  • server.py       _scoreline / _score_params        — what tune_model() grid-searches
  • backtest.py     probs / init_elo                  — offline outcome-weight search
  • learn.py        lambdas / init_elo                — offline scoreline-param search

There is no compiler to catch drift between them. This test re-derives the runtime scoreline
from the constants declared in worldcup.html and asserts server._scoreline produces the same
expected goals and modal scoreline — so editing PRED_K / PRED_DEFAULT (or the hardcoded
0.75/30/60 in _scoreline) without updating the other side fails CI instead of silently
shipping a tuner that optimises a different model than the one being served.

Run:  python test_model_consistency.py      (also works under pytest)
"""
import math
import os
import re

import server

HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "worldcup.html")


def _parse_js_obj(name):
    """Pull `const <name> = { k:v, ... };` out of worldcup.html into a Python dict of floats."""
    src = open(HTML, encoding="utf-8").read()
    m = re.search(r"const\s+" + re.escape(name) + r"\s*=\s*\{([^}]*)\}", src)
    if not m:
        raise AssertionError(f"{name} not found in worldcup.html")
    out = {}
    for k, v in re.findall(r"(\w+)\s*:\s*([-\d.]+)", m.group(1)):
        out[k] = float(v)
    return out


PRED_K = _parse_js_obj("PRED_K")
PRED_DEFAULT = _parse_js_obj("PRED_DEFAULT")


def runtime_scoreline(eh, ea, host, fh, fa, P):
    """Python mirror of computePrediction (worldcup.html) restricted to the same factors
    _scoreline models: Elo strength + home + host + shrinkage-blended form. Injuries,
    suspensions, calibration, midfield, rest, travel, weather and the keeper multiplier are
    neutral here (their slider/None defaults drop out), isolating the shared core."""
    elo_w, home_w = PRED_DEFAULT["elo"], PRED_DEFAULT["home"]
    host_k, home_base = PRED_K["host"], PRED_K["homeBase"]
    A, TS, TC, K = P["avg"], P["tiltScale"], P["tiltCap"], P["formK"]

    rh = elo_w * ((eh - 1500) / 8)           # teamRating with inj=susp=cal=0
    ra = elo_w * ((ea - 1500) / 8)
    dr = rh - ra + home_w * home_base + (host_k if host else 0)
    tilt = max(-TC, min(TC, dr / TS))

    def eg(tourn, n):                         # atk/dfn prior is the global mean (atk/dfn=null)
        return (tourn + A * K) / (n + K)
    atkh, dfnh = eg(fh["gf"], fh["p"]), eg(fh["ga"], fh["p"])
    atka, dfna = eg(fa["gf"], fa["p"]), eg(fa["ga"], fa["p"])
    lh = max(0.2, ((atkh + dfna) / 2) * (1 + tilt))   # damp=1, gkMul=1
    la = max(0.2, ((atka + dfnh) / 2) * (1 - tilt))

    best, pH, pA = -1, 1, 1
    for i in range(7):
        pi = math.exp(-lh) * lh ** i / math.factorial(i)
        for j in range(7):
            pr = pi * (math.exp(-la) * la ** j / math.factorial(j))
            if pr > best + 1e-9 or (pr > best - 1e-9 and i + j > pH + pA):
                best = max(best, pr); pH, pA = i, j
    return lh, la, pH, pA


# representative pre-kickoff states: (eloH, eloA, host, formH, formA)
SAMPLES = [
    (1700, 1500, 0, {"p": 0, "gf": 0, "ga": 0}, {"p": 0, "gf": 0, "ga": 0}),   # opener, no form yet
    (1500, 1500, 0, {"p": 2, "gf": 3, "ga": 2}, {"p": 2, "gf": 1, "ga": 1}),   # even Elo, some form
    (1820, 1460, 1, {"p": 3, "gf": 7, "ga": 1}, {"p": 3, "gf": 2, "ga": 4}),   # host blowout favorite
    (1480, 1760, 0, {"p": 1, "gf": 0, "ga": 3}, {"p": 1, "gf": 3, "ga": 0}),   # underdog at home side
    (1600, 1605, 0, {"p": 3, "gf": 4, "ga": 4}, {"p": 3, "gf": 5, "ga": 5}),   # near tie, leaky both
    (1560, 1500, 1, {"p": 2, "gf": 3, "ga": 2}, {"p": 2, "gf": 2, "ga": 2}),   # mild host edge (tilt below cap → host bonus is observable)
]

PARAM_SETS = [
    server._MODEL_DEFAULTS,
    {"avg": 1.25, "tiltScale": 200, "tiltCap": 0.7, "formK": 1.0},
    {"avg": 1.45, "tiltScale": 280, "tiltCap": 1.0, "formK": 2.0},
]


def test_runtime_matches_tuner():
    """server._scoreline (what tune_model optimises) == the worldcup.html runtime scoreline."""
    for P in PARAM_SETS:
        for eh, ea, host, fh, fa in SAMPLES:
            lh, la, pH, pA = server._scoreline(eh, ea, host, fh, fa, P)
            rlh, rla, rpH, rpA = runtime_scoreline(eh, ea, host, fh, fa, P)
            assert abs(lh - rlh) < 1e-9 and abs(la - rla) < 1e-9, (
                f"expected-goals drift P={P} in={(eh, ea, host)}: "
                f"tuner=({lh:.6f},{la:.6f}) runtime=({rlh:.6f},{rla:.6f})")
            assert (pH, pA) == (rpH, rpA), (
                f"modal scoreline drift P={P} in={(eh, ea, host)}: "
                f"tuner={pH}-{pA} runtime={rpH}-{rpA}")


def test_structural_weights_unchanged():
    """_scoreline hardcodes 0.75*(eh-ea)+30+60host, derived from these runtime weights.
    If the runtime weights move, that hardcode (and this assumption) must be revisited."""
    assert PRED_DEFAULT["elo"] == 6, "elo weight changed → _scoreline's 0.75 (=elo/8) is stale"
    assert PRED_DEFAULT["home"] == 3, "home weight changed → _scoreline's +30 (=home*homeBase) is stale"
    assert PRED_K["homeBase"] == 10, "homeBase changed → _scoreline's +30 is stale"
    assert PRED_K["host"] == 60, "host bonus changed → _scoreline's +60 is stale"


def test_elo_seed_consistent_across_files():
    """All four implementations seed Elo from FIFA rank identically."""
    import backtest
    import learn
    for rank in (1, 7, 25, 60, 130, 200, None):
        s = server._init_elo(rank)
        assert s == backtest.init_elo(rank), f"backtest Elo seed differs at rank={rank}"
        assert s == learn.init_elo(rank), f"learn Elo seed differs at rank={rank}"
    # the offline scoreline tuner (learn.py) must use the same outcome weights as the runtime
    assert learn.W["elo"] == PRED_DEFAULT["elo"] and learn.W["home"] == PRED_DEFAULT["home"], \
        "learn.py outcome weights drifted from worldcup.html PRED_DEFAULT"
    assert learn.HOMEBASE == PRED_K["homeBase"], "learn.py HOMEBASE drifted from PRED_K.homeBase"


if __name__ == "__main__":
    test_runtime_matches_tuner()
    test_structural_weights_unchanged()
    test_elo_seed_consistent_across_files()
    print("OK - model math consistent across worldcup.html / server.py / backtest.py / learn.py")
