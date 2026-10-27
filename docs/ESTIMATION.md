# Estimation: human days vs AI-agent days

_Generated on 2026-10-27 from the backlog in the GitHub Project (97 stories, 23 epics, 15 repositories). Re-run after the first 15 stories with measured data._

## Summary

| | Optimistic | **Likely** | Pessimistic |
|---|---|---|---|
| Ideal engineering days (sum of PERT estimates) | 146 | **146** | 146 |
| **Human person-days** (with review, tests, docs, integration) | 193 | **218** | 264 |
| Human team of 3, calendar | 17 weeks | **22 weeks** | 32 weeks |
| One human alone, calendar | 51 weeks | **67 weeks** | 96 weeks |
| **AI agent compute** (4 agents, hours of agent work) | 134 h | **325 h** | 616 h |
| **Human supervision** (specs, reviews, takeovers, design) | 678 h | **1,134 h** | 2,317 h |
| Supervision as share of human effort | 44% | **65%** | 110% |
| **AI team calendar, part-time maintainer** (15 h/week + 4 agents) | 45 weeks | **76 weeks** | 154 weeks |
| AI team calendar, full-time supervisor (30 h/week + 4 agents) | 23 weeks | **38 weeks** | 77 weeks |

**How to read it**
- **"Human days"** = what a team of experienced developers would spend, everything included.
- **"AI days"** has two parts: agent compute is cheap and fast (about 32 agent-days of 10 h); **the human supervising is the bottleneck** (about 189 days of 6 productive hours).
- With one full-time supervisor, agents finish in about **38 weeks** what one human alone would do in **67 weeks** (1.8x faster), but a team of three humans is still faster on the calendar (22 weeks).
- The part-time scenario is the roadmap's basis: it is honest about evenings and weekends.

## Where the supervision goes (likely)

| Risk class | Stories | Human days | Supervision hours | Supervision / human effort |
|---|---|---|---|---|
| Security | 16 | 42.1 | 295 | 88% |
| Normal | 35 | 64.3 | 128 | 25% |
| External API | 8 | 19.4 | 100 | 64% |
| Core contract | 9 | 22.8 | 66 | 36% |
| Integration | 6 | 12.9 | 52 | 50% |
| Human-only | 7 | 6.3 | 39 | 77% |
| Design/prompt | 5 | 8.9 | 30 | 42% |
| Docs/CI | 11 | 14.8 | 24 | 20% |

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
