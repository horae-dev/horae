# Planning tools

These scripts hold the Horae backlog and the estimation model, and they generated:
- the repositories
- the issues
- the [Horae Roadmap](https://github.com/orgs/horae-dev/projects/1) project
- [`docs/ESTIMATION.md`](../../docs/ESTIMATION.md) and [`docs/ROADMAP.md`](../../docs/ROADMAP.md)

Use them to **re-forecast** once real data comes in.

| File | What it does |
|---|---|
| `backlog.py` | The source of truth for repositories, milestones, epics, stories (with O/M/P estimates and a risk category), human tasks and risks |
| `estimate.py` | The human vs AI-agent model; writes `results.json` (per-story estimates and milestone dates) |
| `gen_docs.py` | Writes `docs/ESTIMATION.md` and `docs/ROADMAP.md` from `results.json` |
| `sync_dates.py` | Pushes milestone Start/Target dates from `results.json` to the project (dry run by default) |
| `create.py` | One-off bootstrap that created the GitHub setup. It is idempotent through `state.json` (git-ignored, holds GitHub IDs). Kept for reference; don't re-run it without that file, or it creates duplicates. |
| `gen_repos.py` | Initial files of each repository (README, AGENTS.md, graft.yaml, LICENSE). Used by `create.py`. |
| `results.json` | Output of `estimate.py`, committed so that a docs change is reviewable |

Requirements: Python 3.12+, standard library only. `sync_dates.py` and `create.py` also need the `gh` CLI, logged in with the `project` scope. Every script can be run from any directory.

## Regenerate the documents

```bash
python3 tools/planning/estimate.py
python3 tools/planning/gen_docs.py
git diff docs/ tools/planning/results.json
```

With no parameter change, the diff is empty.

## Recalibrate from issue actuals

Every story has an **Actuals** section: agent attempts, agent wall-clock, human review minutes, human takeover. After about 15 merged stories, and again at each milestone, compare those numbers with the model in `estimate.py` and update its parameters.

| Parameter (in `P["likely"]` and its ranges) | Measure from actuals | How to update |
|---|---|---|
| `p[size]`: first-pass success by size | Share of stories of that size merged with **1 attempt and no takeover** | Divide by the story's risk multiplier (`M_RISK`) to get the base value. Use at least 5 stories per size; otherwise only move it halfway. |
| `M_RISK[category]` | First-pass rate of a category ÷ the base `p` of the same sizes | Change only categories with 4 or more data points (security and external API will be the slowest to fill). |
| `rho`: review hours per human-day of code | Σ review minutes ÷ 60 ÷ Σ (story human estimate E × 8 h × attempts) | The minimum of 0.25 h per review stays. |
| `t_take`: takeover effort | For taken-over stories: human hours spent ÷ (E × 8 h) | |
| `W`: agent wall-clock scale | Median measured agent wall-clock ÷ the size table `WALL_H` | |
| `o_h`, `b_int`, `f` (human side) | Only if you have human-team data | Otherwise leave them. |

Then:
1. Move the **optimistic** and **pessimistic** sets so that the measured value sits inside the range.
2. Re-run the regeneration commands above.
3. Note the change in the PR description (old → new value, sample size).

Other settings live at the top of `estimate.py`:
- `N_DEV` (human team size), `N_AGENTS` and `AGENT_H_WEEK` (agent capacity)
- `SUP_SCENARIOS` (the supervision hours shown in ESTIMATION.md)
- `START` (first day of M0)

**The roadmap dates use 15 hours of supervision per week**: the `sup / 15` in `estimate.py`'s milestone loop. Change it there if the maintainer's availability changes.

The "Generated on" date in the ESTIMATION.md header is written in `gen_docs.py`. Update it when you regenerate with new data.

### Change the backlog

- **Add, split or re-estimate a story:** edit `backlog.py` and re-run the regeneration commands.
- **New stories:** `create.py` does not create them on GitHub (it needs `state.json`). Create the issue by hand, or with `gh issue create`, using the same body template (see `story_body` in `create.py`), then add it to the project.

## Push updated dates to the project

```bash
python3 tools/planning/sync_dates.py           # shows the changes
python3 tools/planning/sync_dates.py --apply   # applies them
```

- The script reads each item's milestone (the built-in Milestone field, e.g. `M3 Dogfood`) and sets **Start date** and **Target date** to that milestone's window in `results.json`.
- Items without a milestone (the risks) are left alone.
- It does not change GitHub milestone due dates. Update those in each repository's Milestones page, or with:
  ```bash
  gh api -X PATCH repos/horae-dev/<repo>/milestones/<n> -f due_on=<date>T23:59:59Z
  ```
