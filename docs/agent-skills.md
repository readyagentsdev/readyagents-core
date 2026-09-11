# Agent Skills interop

ReadyAgents consumes the open [Agent Skills](https://agentskills.io/specification)
folder format (`SKILL.md` YAML frontmatter plus Markdown body, optional
`scripts/`, `references/`, `assets/`) and can export a workflow as the same
format. This is not a hosted marketplace, and it does not implement any
vendor's private SKILL.md extensions.

A skill runs **only** when a workflow declares `type: skill`. There is no
implicit activation.

## Inbound

```bash
readyagents skills add examples/skills/house-writing-style
readyagents skills list
readyagents skills show house-writing-style
```

Install copies the folder into the local catalog under ReadyAgents home.
Only `name` and `description` are disclosed until a `type: skill` node
selects the skill. Then the body is injected as **untrusted, attributed**
text. Bundled scripts run only in the existing code sandbox. `allowed-tools`
is a request; the policy engine filters it and never auto-grants.

```yaml
- id: apply
  type: skill
  skill: house-writing-style
  inputs: {draft: "{{ draft }}"}
  output_key: styled
```

Install refuses zip-slip, symlinks, and oversized archives. Each skill is
digested (`sha256:…`). Optional `SKILL.md.sig` can pin the digest. A changed
folder after install is drift and the node refuses.

## Outbound

```bash
readyagents skills export examples/calc_pipeline.yaml --out ./exported
readyagents agents-md
```

Export writes a valid skill folder whose `name` matches the directory, with
`scripts/run.sh` shelling to `readyagents run workflow.yaml`. The description
names budget, approval, and audit when the workflow declares them. Export
refuses to embed secrets, absolute local paths, or an operator identity.

`agents-md` emits how to run, validate, and test workflows in this repository.

A workflow without a skill node is unchanged.
