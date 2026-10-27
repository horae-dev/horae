"""Generate docs/ESTIMATION.md and docs/ROADMAP.md for the horae repo from results.json."""
import os as _os
_os.chdir(_os.path.dirname(_os.path.abspath(__file__)))  # paths below are relative to tools/planning

import json
from collections import defaultdict
from backlog import MILESTONES, RISKS, EPICS, REPOS

r = json.load(open("results.json"))
sc = r["scenarios"]
L, O, PS = sc["likely"], sc["optimistic"], sc["pessimistic"]
PT = "ai_weeks::part-time maintainer (15 h/week)"
FT = "ai_weeks::full-time supervisor (30 h/week)"


def f(x, n=0):
    return f"{x:,.{n}f}"


est = f"""# Estimation: human days vs AI-agent days

_Generated on 2026-10-27 from the backlog in the GitHub Project ({len(r['stories'])} stories, {len(EPICS)} epics, {len(REPOS)} repositories). Re-run after the first 15 stories with measured data._

## Summary

| | Optimistic | **Likely** | Pessimistic |
|---|---|---|---|
| Ideal engineering days (sum of PERT estimates) | {f(O['human_ideal_days'])} | **{f(L['human_ideal_days'])}** | {f(PS['human_ideal_days'])} |
| **Human person-days** (with review, tests, docs, integration) | {f(O['human_person_days'])} | **{f(L['human_person_days'])}** | {f(PS['human_person_days'])} |
| Human team of 3, calendar | {f(O['human_team_weeks'])} weeks | **{f(L['human_team_weeks'])} weeks** | {f(PS['human_team_weeks'])} weeks |
| One human alone, calendar | {f(O['solo_human_weeks'])} weeks | **{f(L['solo_human_weeks'])} weeks** | {f(PS['solo_human_weeks'])} weeks |
| **AI agent compute** (4 agents, hours of agent work) | {f(O['agent_hours'])} h | **{f(L['agent_hours'])} h** | {f(PS['agent_hours'])} h |
| **Human supervision** (specs, reviews, takeovers, design) | {f(O['supervision_hours'])} h | **{f(L['supervision_hours'])} h** | {f(PS['supervision_hours'])} h |
| Supervision as share of human effort | {f(O['compression']*100)}% | **{f(L['compression']*100)}%** | {f(PS['compression']*100)}% |
| **AI team calendar, part-time maintainer** (15 h/week + 4 agents) | {f(O[PT])} weeks | **{f(L[PT])} weeks** | {f(PS[PT])} weeks |
| AI team calendar, full-time supervisor (30 h/week + 4 agents) | {f(O[FT])} weeks | **{f(L[FT])} weeks** | {f(PS[FT])} weeks |

**How to read it**
- **"Human days"** = what a team of experienced developers would spend, everything included.
- **"AI days"** has two parts: agent compute is cheap and fast (about {f(L['agent_hours']/10)} agent-days of 10 h); **the human supervising is the bottleneck** (about {f(L['supervision_hours']/6)} days of 6 productive hours).
- With one full-time supervisor, agents finish in about **{f(L[FT])} weeks** what one human alone would do in **{f(L['solo_human_weeks'])} weeks** ({f(L['solo_human_weeks']/L[FT],1)}x faster), but a team of three humans is still faster on the calendar ({f(L['human_team_weeks'])} weeks).
- The part-time scenario is the roadmap's basis: it is honest about evenings and weekends.

## Where the supervision goes (likely)

| Risk class | Stories | Human days | Supervision hours | Supervision / human effort |
|---|---|---|---|---|
"""
d = defaultdict(lambda: [0, 0, 0])
for s in r["stories"]:
    x = d[s["risk_label"]]; x[0] += 1; x[1] += s["human_days"]; x[2] += s["supervision_hours"]
for k, v in sorted(d.items(), key=lambda kv: -kv[1][2]):
    est += f"| {k} | {v[0]} | {f(v[1],1)} | {f(v[2])} | {f(v[2]/(v[1]*8)*100)}% |\n"

est += """
**Takeaway:** security stories (permissions, sandboxing, signed links, secrets) are 16% of the stories but about a quarter of supervision. Agents are close to "free" on normal and docs work (supervision about 20-25% of human effort) and barely help on security and unofficial external APIs. That is why those stories are **human-led** in the project.

## The model

Every story has a three-point estimate in ideal days (optimistic O, most likely M, pessimistic P), a size (XS ≤ 0.5 d, S ≤ 1 d, M ≤ 3 d, L ≤ 5 d) and a risk class.

**Human effort**
- Expected days `E = (O + 4M + P) / 6`, spread `σ = (P − O) / 6`.
- Person-days = `ΣE × (1 + overhead) × (1 + integration buffer)`; overhead 0.20 / **0.30** / 0.45 (review, tests, docs), buffer 0.10 / **0.15** / 0.25.
- Calendar = person-days / (developers × focus factor); focus 0.75 / **0.65** / 0.55.

**AI-agent effort** (per story)
- First-pass success `p` (a mergeable PR without human code changes) by size: XS 0.95 / **0.85** / 0.70, S 0.85 / **0.70** / 0.50, M 0.70 / **0.50** / 0.30, L 0.50 / **0.30** / 0.15, times a risk multiplier: docs 1.15, normal 1.0, core contract 0.8, integration 0.6, design/prompt 0.55, security 0.5, external API 0.5.
- Up to 3 attempts; retries succeed more often (`p + 0.2`, max 0.9). Expected attempts `A = 1 + (1−p) + (1−p)(1−p_retry)`. If all fail, a human takes over at 0.6 × the human effort (reusing partial work).
- Agent wall-clock per attempt: XS 0.25 h, S 0.75 h, M 2 h, L 5 h.
- Supervision per story = spec (XS 0.25 h … L 2 h, ×2 for security and design, ×1.5 for integration and external APIs) + `A ×` review (max(0.25 h, 0.15 × E × 8 h), ×2 security, ×1.5 integration/external) + takeover; security stories add 0.25 × E for the human design; external-API stories add 2 h of manual testing with real accounts.
- Plus fixed human work (architecture, releases, final security review) of 0.15 × ΣE, and a hardening buffer of 0.25 for agent-written code.
- Calendar = max(supervision ÷ supervisor hours per week, agent compute ÷ (4 agents × 50 h/week)). Supervision always dominates here.

## Evidence behind the parameters (2025-2026)

| Finding | Number | Source |
|---|---|---|
| METR time horizon (50% success), frontier models | hours to ~half a day; 80% horizon about 10x shorter | [METR TH 1.1](https://metr.org/blog/2026-1-29-time-horizon-1-1/), [limitations](https://metr.org/notes/2026-01-22-time-horizon-limitations/) |
| Tests-passing agent PRs that maintainers would merge | about half | [METR, Mar 2026](https://metr.org/notes/2026-03-10-many-swe-bench-passing-prs-would-not-be-merged-into-main/) |
| Experienced OSS developers with AI (RCT) | 19% slower (2025); ~18% faster in the weak 2026 follow-up | [METR uplift update](https://metr.org/blog/2026-02-24-uplift-update/) |
| Anthropic engineers | +50% self-reported productivity; "70%+ of my work is review" | [Anthropic research](https://www.anthropic.com/research/how-ai-is-transforming-work-at-anthropic) |
| Telemetry across companies | throughput up, review time 2-5x, more bugs per developer | [Faros AI](https://faros.ai/research/ai-acceleration-whiplash), [DORA 2025](https://dora.dev/insights/dora-2025-year-in-review/) |
| AI-co-authored PRs | 1.7x issues; security issues 1.57x | [CodeRabbit](https://www.coderabbit.ai/blog/state-of-ai-vs-human-code-generation-report) |
| AI-written code with security flaws | 45% of samples | [Veracode](https://www.veracode.com/press-release/ai-generated-code-poses-major-security-risks-in-nearly-half-of-all-development-tasks-veracode-research-reveals/) |
| Agent PR acceptance | 50-80% depending on agent and task type; docs 82%, features 66% | [AIDev](https://arxiv.org/html/2602.08915v1) |
| Net gain after rework | 15-20% overall; greenfield simple 30-40%; complex brownfield 0-10% | [Stanford talk](https://www.ai.engineer/talks/ai-developer-productivity) |

**Caveats.** No validated method exists to convert human estimates into agent effort; the AI parameters are a synthesis of the evidence above, deliberately wide. Benchmarks overstate real-world autonomy; vendor reports may overstate gains. Models change every few months: recalibrate `p`, review ratio and takeover rate from the project's own data (log spec time, review minutes, iterations and takeovers per story; Graft records most of it).

## Assumptions to confirm
- Supervisor time: **15 h/week** (evenings and weekends). Change it and dates move proportionally.
- 4 agents in parallel, each up to 10 h of work per day when stories are ready.
- Human team: 3 experienced Python developers, full time.
"""

road = f"""# Roadmap

_Dates assume the **likely** estimate with a part-time maintainer (15 h/week) supervising 4 agents. See [ESTIMATION.md](ESTIMATION.md). They will be re-forecast after M1._

| Milestone | Goal | Start | Target | Weeks | Supervision h | Agent h | Human team (3 devs) weeks |
|---|---|---|---|---|---|---|---|
"""
for ms, (name, goal) in MILESTONES.items():
    m = r["milestones"][ms]
    road += f"| **{ms} {name}** | {goal} | {m['start']} | {m['end']} | {m['weeks']} | {m['supervision_hours']} | {m['agent_hours']} | {m['human_team_weeks']} |\n"
road += """
**Critical path:** M0 → M1 (engine, plugin host) → M2 (LLM step) → M3 (dogfood). M4-M8 build on a working dogfood instance. M6 and M7 plugins can start in parallel once M3 is done if supervisor time allows.

## Burning points

The hot topics that can stall the project. Each one is tracked as a **Risk** issue in `horae` and visible in the project's "Burning points" view.

| Risk | Severity | When | Mitigation |
|---|---|---|---|
"""
for t, sev, when, desc, mit in RISKS:
    road += f"| **{t}** — {desc} | {sev} | {when} | {mit} |\n"
road += """
## How the work flows (AI-first)

1. **Ready:** a story has acceptance criteria, an estimate, an executor (Agent, Agent + human check, Human-led, Human-only) and its dependencies done.
2. **Agent works:** one agent per story, on its own branch, following the repository's `AGENTS.md`. Commits carry Graft trailers (`Graft-Agent`, `Graft-Model`, `Graft-Task`).
3. **Review:** Graft writes an intent card, checks it against the diff and applies `graft.yaml`: trivial and small low-risk changes can be approved without a human; security paths, core contracts and large changes require the maintainer.
4. **Done:** merged with CI green, acceptance criteria checked, docs updated, estimate actuals recorded on the issue.
"""
open("../../docs/ESTIMATION.md", "w").write(est)
open("../../docs/ROADMAP.md", "w").write(road)
print("ok")
