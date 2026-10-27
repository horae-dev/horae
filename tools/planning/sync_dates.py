"""Push the milestone dates from results.json to the "Start date" and "Target date" fields of the
Horae Roadmap project. Reads items straight from GitHub, so it does not need state.json.

    python3 sync_dates.py            # dry run: show what would change
    python3 sync_dates.py --apply    # update the project
"""
import os as _os
_os.chdir(_os.path.dirname(_os.path.abspath(__file__)))  # paths below are relative to tools/planning

import json, subprocess, sys

ORG, NUMBER = "horae-dev", 1
APPLY = "--apply" in sys.argv


def gql(query, **variables):
    out = subprocess.run(["gh", "api", "graphql", "--input", "-"], input=json.dumps({"query": query, "variables": variables}),
                         capture_output=True, text=True, check=True).stdout
    res = json.loads(out)
    if res.get("errors"):
        raise RuntimeError(res["errors"])
    return res["data"]


def main():
    ms = json.load(open("results.json"))["milestones"]
    meta = gql("""query($o:String!,$n:Int!){ organization(login:$o){ projectV2(number:$n){ id
        fields(first:50){ nodes{ ... on ProjectV2FieldCommon { id name } } } } } }""", o=ORG, n=NUMBER)
    proj = meta["organization"]["projectV2"]
    fid = {f["name"]: f["id"] for f in proj["fields"]["nodes"] if f}
    items, cursor = [], None
    while True:
        d = gql("""query($id:ID!,$c:String){ node(id:$id){ ... on ProjectV2 { items(first:100, after:$c){
            pageInfo{ hasNextPage endCursor }
            nodes{ id
              content{ ... on Issue { number repository{ name } milestone{ title } } }
              start: fieldValueByName(name:"Start date"){ ... on ProjectV2ItemFieldDateValue { date } }
              target: fieldValueByName(name:"Target date"){ ... on ProjectV2ItemFieldDateValue { date } } } } } } }""",
                id=proj["id"], c=cursor)
        page = d["node"]["items"]
        items += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]

    changes = []
    for it in items:
        c = it.get("content") or {}
        title = (c.get("milestone") or {}).get("title")
        if not title:
            continue  # risks and items without a milestone keep no dates
        key = title.split()[0]
        if key not in ms:
            continue
        want = {"Start date": ms[key]["start"], "Target date": ms[key]["end"]}
        have = {"Start date": (it.get("start") or {}).get("date"), "Target date": (it.get("target") or {}).get("date")}
        for field, value in want.items():
            if have[field] != value:
                changes.append((it["id"], field, value, f'{c["repository"]["name"]}#{c["number"]}', have[field]))

    for _, field, value, ref, old in changes:
        print(f"{ref:28} {field:12} {old} -> {value}")
    print(f"{len(changes)} change(s) on {len(items)} items" + ("" if APPLY else " (dry run, use --apply)"))
    if not APPLY:
        return
    for i in range(0, len(changes), 10):
        chunk = changes[i:i + 10]
        decl = ", ".join(f"$v{j}: ProjectV2FieldValue!" for j in range(len(chunk)))
        body = " ".join(f'm{j}: updateProjectV2ItemFieldValue(input:{{projectId:"{proj["id"]}", itemId:"{it}", '
                        f'fieldId:"{fid[field]}", value:$v{j}}}){{ clientMutationId }}'
                        for j, (it, field, *_rest) in enumerate(chunk))
        gql(f"mutation({decl}) {{ {body} }}", **{f"v{j}": {"date": value} for j, (_i, _f, value, *_r) in enumerate(chunk)})
    print("applied")


if __name__ == "__main__":
    main()
