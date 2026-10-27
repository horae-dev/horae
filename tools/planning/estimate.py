"""Estimate the Horae backlog in human days and AI-agent days.

Model: see ESTIMATION.md (generated). Three parameter sets: optimistic, likely, pessimistic.
"""
import os as _os
_os.chdir(_os.path.dirname(_os.path.abspath(__file__)))  # paths below are relative to tools/planning

import json, math, datetime as dt
from backlog import S, EPICS, MILESTONES

EPIC = {e[0]: e for e in EPICS}

P = {
    "optimistic":  dict(p={"XS": .95, "S": .85, "M": .70, "L": .50}, rho=.08, W=.5,  t_take=.4, F=.10, b_ai=.20, o_h=.20, b_int=.10, f=.75),
    "likely":      dict(p={"XS": .85, "S": .70, "M": .50, "L": .30}, rho=.15, W=1.0, t_take=.6, F=.15, b_ai=.25, o_h=.30, b_int=.15, f=.65),
    "pessimistic": dict(p={"XS": .70, "S": .50, "M": .30, "L": .15}, rho=.30, W=1.5, t_take=1.0, F=.20, b_ai=.35, o_h=.45, b_int=.25, f=.55),
}
M_RISK = {"normal": 1.0, "docs": 1.15, "contract": .8, "sec": .5, "integ": .6, "ext": .5, "spec": .55, "human": 0.0}
SPEC_MULT = {"sec": 2, "spec": 2, "integ": 1.5, "ext": 1.5, "contract": 1.25}
REV_MULT = {"sec": 2, "integ": 1.5, "ext": 1.5, "contract": 1.25}
SPEC_H = {"XS": .25, "S": .5, "M": 1, "L": 2}
WALL_H = {"XS": .25, "S": .75, "M": 2, "L": 5}
EXECUTOR = {"normal": "Agent", "docs": "Agent", "contract": "Agent", "integ": "Agent + human check",
            "spec": "Agent + human check", "ext": "Agent + human check", "sec": "Human-led", "human": "Human-only"}
RISK_LABEL = {"normal": "Normal", "docs": "Docs/CI", "contract": "Core contract", "sec": "Security",
              "integ": "Integration", "ext": "External API", "spec": "Design/prompt", "human": "Human-only"}

N_DEV = 3            # human team size
N_AGENTS = 4         # parallel agents
AGENT_H_WEEK = N_AGENTS * 10 * 5
SUP_SCENARIOS = {"part-time maintainer (15 h/week)": 15, "full-time supervisor (30 h/week)": 30}
START = dt.date(2026, 11, 2)


def size_of(m):
    return "XS" if m <= .5 else "S" if m <= 1 else "M" if m <= 3 else "L"


def story_est(s, prm):
    o, m, p = s["est"]
    E = (o + 4 * m + p) / 6
    sig = (p - o) / 6
    size = size_of(m)
    cat = s["cat"]
    if cat == "human":
        return dict(E=E, sig=sig, size=size, p1=0.0, attempts=0.0, agent_h=0.0, sup_h=E * 8, takeover=1.0)
    p1 = min(.95, prm["p"][size] * M_RISK[cat])
    pr = min(.9, p1 + .2)
    attempts = 1 + (1 - p1) + (1 - p1) * (1 - pr)
    takeover = (1 - p1) * (1 - pr) ** 2
    agent_h = attempts * WALL_H[size] * prm["W"]
    spec = SPEC_H[size] * SPEC_MULT.get(cat, 1)
    review = max(.25, prm["rho"] * E * 8) * REV_MULT.get(cat, 1)
    sup = spec + attempts * review + takeover * prm["t_take"] * E * 8
    if cat == "sec":        # human designs the approach
        sup += .25 * E * 8
    if cat == "ext":        # live manual testing with real accounts
        sup += 2
    return dict(E=E, sig=sig, size=size, p1=p1, attempts=attempts, agent_h=agent_h, sup_h=sup, takeover=takeover)


def run():
    out = {"params": P, "scenarios": {}, "stories": [], "milestones": {}}
    for name, prm in P.items():
        rows = [story_est(s, prm) for s in S]
        sumE = sum(r["E"] for r in rows)
        sig = math.sqrt(sum(r["sig"] ** 2 for r in rows))
        h_eng = sumE * (1 + prm["o_h"])
        h_proj = h_eng * (1 + prm["b_int"])
        h_p85 = (sumE + 1.04 * sig) * (1 + prm["o_h"]) * (1 + prm["b_int"])
        team_cal = h_proj / (N_DEV * prm["f"])
        sup_h = (sum(r["sup_h"] for r in rows) + prm["F"] * sumE * 8) * (1 + prm["b_ai"])
        agent_h = sum(r["agent_h"] for r in rows) * (1 + prm["b_ai"])
        sc = dict(human_ideal_days=sumE, human_person_days=h_proj, human_person_days_p85=h_p85,
                  human_team_calendar_days=team_cal, human_team_weeks=team_cal / 5,
                  supervision_hours=sup_h, supervision_person_days=sup_h / 6, agent_hours=agent_h,
                  compression=sup_h / (h_proj * 8),
                  solo_human_weeks=h_proj / prm['f'] / 5)
        for label, hw in SUP_SCENARIOS.items():
            weeks = max(sup_h / hw, agent_h / AGENT_H_WEEK)
            sc[f"ai_weeks::{label}"] = weeks
        out["scenarios"][name] = sc

    # per-story (likely) + milestone schedule for the part-time scenario
    prm = P["likely"]
    ms_sup, ms_agent = {}, {}
    for s in S:
        r = story_est(s, prm)
        ms = s["ms"] or EPIC[s["epic"]][2]
        ms_sup[ms] = ms_sup.get(ms, 0) + r["sup_h"]
        ms_agent[ms] = ms_agent.get(ms, 0) + r["agent_h"]
        out["stories"].append(dict(title=s["title"], epic=s["epic"], repo=EPIC[s["epic"]][1], milestone=ms,
                                   size=r["size"], category=s["cat"], risk_label=RISK_LABEL[s["cat"]],
                                   executor=EXECUTOR[s["cat"]], human_days=round(r["E"] * (1 + prm["o_h"]), 1),
                                   pert=s["est"], ai_agent_hours=round(r["agent_h"], 1),
                                   supervision_hours=round(r["sup_h"], 1), first_pass=round(r["p1"] * 100)))
    # fixed human work spread by share of supervision
    tot = sum(ms_sup.values())
    fixed = prm["F"] * sum(story_est(s, prm)["E"] for s in S) * 8
    d = START
    for ms in MILESTONES:
        sup = (ms_sup.get(ms, 0) + fixed * ms_sup.get(ms, 0) / tot) * (1 + prm["b_ai"])
        ag = ms_agent.get(ms, 0) * (1 + prm["b_ai"])
        weeks = max(sup / 15, ag / AGENT_H_WEEK, .5)
        end = d + dt.timedelta(days=round(weeks * 7) - 1)
        hum = sum(x["human_days"] for x in out["stories"] if x["milestone"] == ms) * (1 + prm["b_int"])
        out["milestones"][ms] = dict(start=d.isoformat(), end=end.isoformat(), weeks=round(weeks, 1),
                                     supervision_hours=round(sup, 1), agent_hours=round(ag, 1),
                                     human_person_days=round(hum, 1),
                                     human_team_weeks=round(hum / (N_DEV * prm["f"]) / 5, 1))
        d = end + dt.timedelta(days=1)
    json.dump(out, open("results.json", "w"), indent=1, default=str)
    return out


if __name__ == "__main__":
    o = run()
    for k, v in o["scenarios"].items():
        print(k, {a: round(b, 1) for a, b in v.items()})
    for k, v in o["milestones"].items():
        print(k, v)
    print(len(o["stories"]), "stories")
