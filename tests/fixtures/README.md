# Vendored fixtures

## graft.v1.schema.json

- Source repository: https://github.com/mmornati/graft
- Source path: `schema/graft.v1.schema.json`
- Commit: `3f40dd101286bb95c4b356eaf10a9e163bf1903d`
- Vendored with the permission of its author: `mmornati/graft` is the maintainer's own project,
  and Graft is the review service used on this repository.

It is used only by `tests/test_graft_policy.py` to validate `graft.yaml` offline.

## instance/

A synthetic Horae instance (`horae.yaml`, `pipelines/`, `horae.lock`) for the configuration
loader and `horae doctor` tests. The pipelines and the lockfile are copies of examples in
`schemas/examples/`. It holds no secret values: tests create them in a temporary directory.
