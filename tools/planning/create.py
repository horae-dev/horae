"""Create the horae-dev GitHub setup: issue types, project, repos, labels, milestones, issues,
sub-issues, dependencies, project fields, items, values and views. Idempotent via state.json."""
import os as _os
_os.chdir(_os.path.dirname(_os.path.abspath(__file__)))  # paths below are relative to tools/planning

import json, os, subprocess, sys, time
from backlog import REPOS, EPICS, S, RISKS, MILESTONES
from estimate import story_est, P, EXECUTOR, RISK_LABEL

ORG = "horae-dev"
STATE = "state.json"
st = json.load(open(STATE)) if os.path.exists(STATE) else {}
r = json.load(open("results.json"))
MS = r["milestones"]
EPIC = {e[0]: e for e in EPICS}
LIKELY = P["likely"]


def save():
    json.dump(st, open(STATE, "w"), indent=1)


def gh(args, inp=None, ok_fail=False, tries=6):
    for i in range(tries):
        p = subprocess.run(["gh"] + args, input=inp, capture_output=True, text=True)
        if p.returncode == 0:
            return p.stdout
        err = p.stderr + p.stdout
        if "secondary rate limit" in err.lower() or "abuse" in err.lower() or "502" in err or "504" in err or "Something went wrong" in err:
            time.sleep(10 * (i + 1)); continue
        if ok_fail:
            return None
        raise RuntimeError(f"gh {' '.join(args[:4])} failed: {err[:800]}")
    raise RuntimeError("rate limited too long")


def api(method, path, body=None, ok_fail=False):
    args = ["api", "-X", method, path, "-H", "Accept: application/vnd.github+json"]
    if body is not None:
        args += ["--input", "-"]
    out = gh(args, json.dumps(body) if body is not None else None, ok_fail=ok_fail)
    return json.loads(out) if out and out.strip() else {}


def gql(query, **vars):
    body = {"query": query, "variables": vars}
    out = gh(["api", "graphql", "--input", "-"], json.dumps(body))
    res = json.loads(out)
    if res.get("errors"):
        raise RuntimeError(json.dumps(res["errors"])[:1500])
    return res["data"]


def step(name):
    print(f"== {name}", flush=True)


# ---------------------------------------------------------------- issue types
def issue_types():
    step("issue types")
    existing = {t["name"]: t for t in api("GET", f"orgs/{ORG}/issue-types")}
    want = [("Epic", "A large body of work broken into stories", "purple"),
            ("Story", "A user-visible slice of an epic, sized for one PR", "blue"),
            ("Risk", "A burning point: something that can stall the project", "red")]
    for name, desc, color in want:
        if name not in existing:
            api("POST", f"orgs/{ORG}/issue-types", {"name": name, "description": desc, "color": color, "is_enabled": True})
    st["types"] = sorted(n for n, *_ in want)
    save()


# ---------------------------------------------------------------- project
def project():
    step("project")
    if "project" in st:
        return
    out = json.loads(gh(["project", "create", "--owner", ORG, "--title", "Horae Roadmap", "--format", "json"]))
    st["project"] = {"number": out["number"], "id": out["id"], "url": out["url"]}
    save()


# ---------------------------------------------------------------- repos
def repos():
    step("repos")
    subprocess.run([sys.executable, "gen_repos.py", st["project"]["url"]], check=True)
    st.setdefault("repos", [])
    for name, desc in REPOS.items():
        if name in st["repos"]:
            continue
        d = os.path.abspath(f"repos/{name}")
        gh(["repo", "create", f"{ORG}/{name}", "--public", "--description", desc, "--disable-wiki"], ok_fail=True)
        env = dict(os.environ, GIT_AUTHOR_NAME="", GIT_COMMITTER_NAME="")
        env.pop("GIT_AUTHOR_NAME"); env.pop("GIT_COMMITTER_NAME")
        cmds = [["git", "init", "-q", "-b", "main"], ["git", "add", "-A"],
                ["git", "commit", "-q", "-m", "Initial README, agent guide, Graft policy and license\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"],
                ["git", "remote", "add", "origin", f"git@github.com:{ORG}/{name}.git"],
                ["git", "push", "-q", "-u", "origin", "main"]]
        for c in cmds:
            subprocess.run(c, cwd=d, check=True, capture_output=True)
        topics = ["horae", "personal-agent", "self-hosted", "llm"] + (["horae-plugin"] if name.startswith(("plugin-", "pack-")) else [])
        api("PUT", f"repos/{ORG}/{name}/topics", {"names": topics})
        api("PUT", f"repos/{ORG}/{name}/private-vulnerability-reporting", ok_fail=True)
        gh(["project", "link", str(st["project"]["number"]), "--owner", ORG, "--repo", f"{ORG}/{name}"], ok_fail=True)
        st["repos"].append(name); save()
        time.sleep(2)


# ---------------------------------------------------------------- labels & milestones
LABELS = [("agent-ready", "0e8a16", "Spec complete: an agent can pick this up"),
          ("needs-spec", "fbca04", "Acceptance criteria or design still to clarify"),
          ("executor:agent", "1d76db", "Done by an agent; Graft decides if a human must review"),
          ("executor:agent+check", "5319e7", "Done by an agent; the maintainer checks the result"),
          ("executor:human-led", "b60205", "Security-critical: human designs, agent assists"),
          ("executor:human-only", "000000", "Only a human can do this"),
          ("risk", "d93f0b", "Burning point")]
EXEC_LABEL = {"Agent": "executor:agent", "Agent + human check": "executor:agent+check",
              "Human-led": "executor:human-led", "Human-only": "executor:human-only"}


def labels_milestones():
    step("labels and milestones")
    st.setdefault("labels", []); st.setdefault("milestones", {})
    used = {}
    for s in S:
        used.setdefault(EPIC[s["epic"]][1], set()).add(s["ms"] or EPIC[s["epic"]][2])
    for e in EPICS:
        used.setdefault(e[1], set()).add(e[2])
    for repo in REPOS:
        if repo not in st["labels"]:
            for n, c, d in LABELS:
                if n == "risk" and repo != "horae":
                    continue
                api("POST", f"repos/{ORG}/{repo}/labels", {"name": n, "color": c, "description": d}, ok_fail=True)
            st["labels"].append(repo); save()
        st["milestones"].setdefault(repo, {})
        for ms in sorted(used.get(repo, [])):
            if ms in st["milestones"][repo]:
                continue
            name, goal = MILESTONES[ms]
            m = api("POST", f"repos/{ORG}/{repo}/milestones",
                    {"title": f"{ms} {name}", "description": goal, "due_on": MS[ms]["end"] + "T23:59:59Z"})
            st["milestones"][repo][ms] = m["number"]; save()


# ---------------------------------------------------------------- issues
def story_body(s, idx):
    e = story_est(s, LIKELY)
    epic = EPIC[s["epic"]]
    ms = s["ms"] or epic[2]
    ex = EXECUTOR[s["cat"]]
    repo = epic[1]
    o, m, p = s["est"]
    ac = "\n".join(f"- [ ] {a}" for a in s["ac"])
    note = {
        "Human-led": "> [!IMPORTANT]\n> **Human-led (security-critical).** Agents: open a **draft PR with a short design note** first and wait for the maintainer before implementing. Graft treats these paths as `sensitive`.\n",
        "Human-only": "> [!NOTE]\n> **Human-only.** Agents must not pick this up.\n",
        "Agent + human check": "> [!NOTE]\n> **Agent + human check.** The maintainer verifies the result (integration, external accounts or design quality).\n",
        "Agent": "",
    }[ex]
    return f"""{note}
## Summary

{s['summary']}

Part of **{epic[3]}** ({ms} {MILESTONES[ms][0]}).

## Acceptance criteria

{ac}

## Estimate

| | |
|---|---|
| Size | {e['size']} |
| Human estimate (ideal days, O / M / P) | {o} / {m} / {p} → **{e['E']:.1f} d**; with review and tests **{e['E']*(1+LIKELY['o_h']):.1f} person-days** |
| Executor | {ex} |
| Risk class | {RISK_LABEL[s['cat']]} |
| Expected first-pass success (agent) | {e['p1']*100:.0f}% |
| Expected agent wall-clock | {e['agent_h']:.1f} h |
| Expected human supervision | {e['sup_h']:.1f} h |

Model and assumptions: [ESTIMATION.md](https://github.com/horae-dev/horae/blob/main/docs/ESTIMATION.md).

## For agents

- Follow [`AGENTS.md`](https://github.com/horae-dev/{repo}/blob/main/AGENTS.md). Branch `story/<this issue number>-<slug>`, one PR.
- Commit trailers: `Graft-Agent`, `Graft-Model`, `Graft-Task: #<this issue number>`.
- Context: [PLAN.md](https://github.com/horae-dev/horae/blob/main/docs/PLAN.md).

## Actuals (fill in when done)

- Agent attempts:
- Agent wall-clock:
- Human review minutes:
- Human takeover: yes / no
"""


def epic_body(e):
    key, repo, ms, title, summary = e
    rows = [x for x in r["stories"] if x["epic"] == key]
    hd = sum(x["human_days"] for x in rows); sh = sum(x["supervision_hours"] for x in rows); ah = sum(x["ai_agent_hours"] for x in rows)
    return f"""## Goal

{summary}

Milestone **{ms} {MILESTONES[ms][0]}**: {MILESTONES[ms][1]}.

## Stories

See the sub-issues below ({len(rows)} stories).

## Estimate (likely)

| Human person-days | Agent wall-clock | Human supervision |
|---|---|---|
| {hd:.1f} d | {ah:.1f} h | {sh:.1f} h |

Roadmap and model: [ROADMAP.md](https://github.com/horae-dev/horae/blob/main/docs/ROADMAP.md) · [ESTIMATION.md](https://github.com/horae-dev/horae/blob/main/docs/ESTIMATION.md)
"""


def risk_body(rk):
    t, sev, when, desc, mit = rk
    return f"""## Burning point

{desc}

| Severity | When |
|---|---|
| **{sev}** | {when} |

## Mitigation

{mit}

## Watch for

Update this issue when the risk materializes, changes severity, or is retired. Close it when it no longer applies.
"""


def create_issue(key, repo, title, body, type_, ms, labels):
    st.setdefault("issues", {})
    if key in st["issues"]:
        return st["issues"][key]
    payload = {"title": title, "body": body, "type": type_, "labels": labels}
    if ms:
        payload["milestone"] = st["milestones"][repo][ms]
    i = api("POST", f"repos/{ORG}/{repo}/issues", payload)
    st["issues"][key] = {"repo": repo, "number": i["number"], "id": i["id"], "node_id": i["node_id"], "url": i["html_url"]}
    save(); time.sleep(1.2)
    return st["issues"][key]


def issues():
    step("issues")
    for e in EPICS:
        create_issue(e[0], e[1], e[3], epic_body(e), "Epic", e[2], [])
    for idx, s in enumerate(S):
        epic = EPIC[s["epic"]]; repo = epic[1]; ms = s["ms"] or epic[2]
        ex = EXECUTOR[s["cat"]]
        labels = [EXEC_LABEL[ex]]
        if ex in ("Agent", "Agent + human check") and ms in ("M0", "M1"):
            labels.append("agent-ready")
        type_ = "Task" if s["cat"] == "human" else "Story"
        create_issue(f"S{idx}", repo, s["title"], story_body(s, idx), type_, ms, labels)
    for idx, rk in enumerate(RISKS):
        create_issue(f"R{idx}", "horae", rk[0], risk_body(rk), "Risk", None, ["risk"])


def sub_issues():
    step("sub-issues")
    st.setdefault("subs", [])
    for idx, s in enumerate(S):
        k = f"S{idx}"
        if k in st["subs"]:
            continue
        ep = st["issues"][s["epic"]]; ch = st["issues"][k]
        api("POST", f"repos/{ORG}/{ep['repo']}/issues/{ep['number']}/sub_issues", {"sub_issue_id": ch["id"]}, ok_fail=True)
        st["subs"].append(k); save(); time.sleep(0.6)


def find_story(title):
    for idx, s in enumerate(S):
        if s["title"] == title:
            return f"S{idx}"
    raise KeyError(title)


DEPS = [  # (blocked, blocking)
    ("E-core-engine", find_story("JSON Schemas for pipeline, plugin manifest, intents, lockfile and run record")),
    ("E-core-llm", "E-core-engine"),
    ("E-core-runtime", "E-core-engine"),
    ("E-core-market", "E-core-runtime"),
    ("E-core-isolation", find_story("Plugin host v1 (MCP stdio, declared secrets only)")),
    ("E-core-mcp", "E-core-runtime"),
    ("E-core-release", "E-core-isolation"),
    ("E-template", find_story("Plugin SDK (Python)")),
    ("E-market", find_story("Install from git with pinned commit")),
    ("E-brief", "E-core-llm"),
] + [(e[0], find_story("Plugin SDK (Python)")) for e in EPICS if e[1].startswith(("plugin-", "pack-"))]


def deps():
    step("dependencies")
    st.setdefault("deps", [])
    for blocked, blocking in DEPS:
        k = f"{blocked}<{blocking}"
        if k in st["deps"]:
            continue
        b = st["issues"][blocked]; bl = st["issues"][blocking]
        api("POST", f"repos/{ORG}/{b['repo']}/issues/{b['number']}/dependencies/blocked_by", {"issue_id": bl["id"]}, ok_fail=True)
        st["deps"].append(k); save(); time.sleep(0.6)


# ---------------------------------------------------------------- project fields, items, views
FIELDS = [
    ("Milestone", "SINGLE_SELECT", [(f"{k} {v[0]}", "GRAY") for k, v in MILESTONES.items()]),
    ("Executor", "SINGLE_SELECT", [("Agent", "GREEN"), ("Agent + human check", "BLUE"), ("Human-led", "ORANGE"), ("Human-only", "RED")]),
    ("Size", "SINGLE_SELECT", [("XS", "GRAY"), ("S", "BLUE"), ("M", "YELLOW"), ("L", "ORANGE")]),
    ("Risk class", "SINGLE_SELECT", [(v, "GRAY") for v in dict.fromkeys(RISK_LABEL.values())]),
    ("Priority", "SINGLE_SELECT", [("P0", "RED"), ("P1", "ORANGE"), ("P2", "YELLOW")]),
    ("Severity", "SINGLE_SELECT", [("high", "RED"), ("medium", "ORANGE"), ("low", "YELLOW")]),
    ("Human days", "NUMBER", None),
    ("Agent hours", "NUMBER", None),
    ("Supervision hours", "NUMBER", None),
    ("First-pass %", "NUMBER", None),
    ("Start date", "DATE", None),
    ("Target date", "DATE", None),
]
STATUS = [("Backlog", "GRAY", "Not ready yet"), ("Ready", "BLUE", "Spec complete, can start"),
          ("In progress", "YELLOW", "An agent or human is working on it"), ("In review", "PURPLE", "PR open, Graft and/or maintainer reviewing"),
          ("Human needed", "ORANGE", "Blocked on the maintainer"), ("Done", "GREEN", "Merged and accepted")]


def project_fields():
    step("project fields")
    pid = st["project"]["id"]
    data = gql("""query($id:ID!){ node(id:$id){ ... on ProjectV2 { fields(first:50){ nodes{
        ... on ProjectV2FieldCommon { id name dataType }
        ... on ProjectV2SingleSelectField { options { id name } } } } } } }""", id=pid)
    have = {f["name"]: f for f in data["node"]["fields"]["nodes"] if f}
    # Status options
    stf = have["Status"]
    if {o["name"] for o in stf["options"]} != {s[0] for s in STATUS}:
        gql("""mutation($f:ID!,$o:[ProjectV2SingleSelectFieldOptionInput!]){ updateProjectV2Field(input:{fieldId:$f, singleSelectOptions:$o}){ clientMutationId } }""",
            f=stf["id"], o=[{"name": n, "color": c, "description": d} for n, c, d in STATUS])
    for name, typ, opts in FIELDS:
        if name in have:
            continue
        inp = {"projectId": pid, "dataType": typ, "name": name}
        if opts:
            inp["singleSelectOptions"] = [{"name": n, "color": c, "description": ""} for n, c in opts]
        gql("mutation($i:CreateProjectV2FieldInput!){ createProjectV2Field(input:$i){ clientMutationId } }", i=inp)
    data = gql("""query($id:ID!){ node(id:$id){ ... on ProjectV2 { fields(first:50){ nodes{
        ... on ProjectV2FieldCommon { id name dataType }
        ... on ProjectV2SingleSelectField { options { id name } } } } } } }""", id=pid)
    st["fields"] = {f["name"]: {"id": f["id"], "options": {o["name"]: o["id"] for o in f.get("options", [])}}
                    for f in data["node"]["fields"]["nodes"] if f}
    save()


def items():
    step("project items")
    pid = st["project"]["id"]
    st.setdefault("items", {})
    keys = [k for k in st["issues"] if k not in st["items"]]
    for i in range(0, len(keys), 5):
        chunk = keys[i:i + 5]
        q = "mutation {" + " ".join(
            f'a{j}: addProjectV2ItemById(input:{{projectId:"{pid}", contentId:"{st["issues"][k]["node_id"]}"}}){{ item {{ id }} }}'
            for j, k in enumerate(chunk)) + "}"
        d = gql(q)
        for j, k in enumerate(chunk):
            st["items"][k] = d[f"a{j}"]["item"]["id"]
        save()


def values_for(key):
    F = st["fields"]
    def ss(field, opt): return (F[field]["id"], {"singleSelectOptionId": F[field]["options"][opt]})
    def num(field, v): return (F[field]["id"], {"number": float(v)})
    def date(field, v): return (F[field]["id"], {"date": v})
    out = []
    if key.startswith("S"):
        s = S[int(key[1:])]; epic = EPIC[s["epic"]]; ms = s["ms"] or epic[2]
        x = r["stories"][int(key[1:])]
        out += [ss("Executor", x["executor"]), ss("Size", x["size"]),
                ss("Risk class", x["risk_label"]), num("Human days", x["human_days"]), num("Agent hours", x["ai_agent_hours"]),
                num("Supervision hours", x["supervision_hours"]), num("First-pass %", x["first_pass"]),
                date("Start date", MS[ms]["start"]), date("Target date", MS[ms]["end"]),
                ss("Priority", "P0" if ms in ("M0", "M1", "M2") else "P1" if ms in ("M3", "M4", "M5") else "P2"),
                ss("Status", "Ready" if ms in ("M0",) and x["executor"] != "Human-only" else "Backlog")]
    elif key.startswith("R"):
        t, sev, when, *_ = RISKS[int(key[1:])]
        out += [ss("Severity", sev), ss("Status", "Backlog"), ss("Priority", "P0" if sev == "high" else "P1" if sev == "medium" else "P2")]
    else:
        e = EPIC[key]; ms = e[2]
        rows = [x for x in r["stories"] if x["epic"] == key]
        out += [date("Start date", MS[ms]["start"]), date("Target date", MS[ms]["end"]),
                num("Human days", round(sum(x["human_days"] for x in rows), 1)),
                num("Agent hours", round(sum(x["ai_agent_hours"] for x in rows), 1)),
                num("Supervision hours", round(sum(x["supervision_hours"] for x in rows), 1)),
                ss("Priority", "P0" if ms in ("M0", "M1", "M2") else "P1" if ms in ("M3", "M4", "M5") else "P2"),
                ss("Status", "Ready" if ms == "M0" else "Backlog")]
    return out


def item_values():
    step("project values")
    pid = st["project"]["id"]
    st.setdefault("valued", [])
    muts = []
    for key, item in st["items"].items():
        if key in st["valued"]:
            continue
        for fid, val in values_for(key):
            muts.append((key, item, fid, val))
    done_keys = set()
    for i in range(0, len(muts), 10):
        chunk = muts[i:i + 10]
        decl = ", ".join(f"$v{j}: ProjectV2FieldValue!" for j in range(len(chunk)))
        body = " ".join(
            f'm{j}: updateProjectV2ItemFieldValue(input:{{projectId:"{pid}", itemId:"{it}", fieldId:"{fid}", value:$v{j}}}){{ clientMutationId }}'
            for j, (k, it, fid, val) in enumerate(chunk))
        gql(f"mutation({decl}) {{ {body} }}", **{f"v{j}": val for j, (k, it, fid, val) in enumerate(chunk)})
        done_keys |= {k for k, *_ in chunk}
    st["valued"] = list(set(st["valued"]) | done_keys); save()


VIEWS = [
    ("Roadmap", "ROADMAP_LAYOUT", "-label:risk", ["Title", "Status", "Milestone", "Executor"]),
    ("Board", "BOARD_LAYOUT", "-label:risk", ["Title", "Assignees", "Milestone", "Executor", "Size"]),
    ("Backlog by milestone", "TABLE_LAYOUT", "-label:risk", ["Title", "Status", "Repository", "Milestone", "Executor", "Size", "Human days", "Agent hours", "Supervision hours", "First-pass %", "Target date"]),
    ("Agent-ready", "TABLE_LAYOUT", "label:agent-ready status:Ready", ["Title", "Repository", "Milestone", "Executor", "Size", "Agent hours"]),
    ("Human work", "TABLE_LAYOUT", 'executor:"Human-led","Human-only"', ["Title", "Status", "Repository", "Milestone", "Executor", "Supervision hours"]),
    ("Burning points", "TABLE_LAYOUT", "label:risk", ["Title", "Status", "Severity", "Priority"]),
]


def views():
    step("views")
    pid = st["project"]["id"]
    st.setdefault("views", {})
    data = gql("""query($id:ID!){ node(id:$id){ ... on ProjectV2 { fields(first:50){ nodes{ ... on ProjectV2FieldCommon { id name } } } } } }""", id=pid)
    fid = {f["name"]: f["id"] for f in data["node"]["fields"]["nodes"] if f}
    for name, layout, flt, cols in VIEWS:
        if name in st["views"]:
            continue
        vis = [fid[c] for c in cols if c in fid]
        d = gql("mutation($i:CreateProjectV2ViewInput!){ createProjectV2View(input:$i){ projectV2View { id } } }",
                i={"projectId": pid, "name": name, "layout": layout, **({"configuration": {"visibleFieldIds": vis}} if layout == "TABLE_LAYOUT" else {})})
        vid = d["createProjectV2View"]["projectV2View"]["id"]
        gql("mutation($i:UpdateProjectV2ViewInput!){ updateProjectV2View(input:$i){ clientMutationId } }",
            i={"viewId": vid, "filter": flt})
        st["views"][name] = vid; save()


if __name__ == "__main__":
    steps = sys.argv[1:] or ["issue_types", "project", "repos", "labels_milestones", "issues", "sub_issues", "deps",
                             "project_fields", "items", "item_values", "views"]
    for s_ in steps:
        globals()[s_]()
    print("DONE", flush=True)
