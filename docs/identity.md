# Identity

ReadyAgents **verifies** approver assertions issued elsewhere. It is not an
identity provider, login page, or OAuth authorization server. It never issues
human identities.

`--actor NAME` is still the default. Without a trust-anchor file and without
`--token-file`, behaviour is unchanged.

## Signed vs identified

These are independent:

| Property | Meaning |
| --- | --- |
| **Signed** | HMAC of the decision body (`READYAGENTS_DECISION_SECRET`). Proves the payload was not altered. The actor may still be a typed name. |
| **Identified** | An OIDC ID token or JWT verified against a local trust anchor. Proves *who* made the decision (`subject`, `issuer`). |

A decision can be signed, identified, both, or neither. Docs and records keep
`signature_status` and `method` / `identified` as separate fields.

## Trust anchors

A local YAML file lists issuers, a **JWKS file path**, audience, clock skew,
and the claim used as the actor. Offline by default. Network JWKS fetch is not
enabled.

```yaml
version: 1
issuers:
  - issuer: "https://login.example.com/"
    audience: "readyagents"
    jwks_file: "jwks.json"
    actor_claim: "email"
    role_claims: ["groups"]
    role_map:
      approvers: approver
    max_skew_seconds: 60
```

Point at it with `--trust-anchors` or `READYAGENTS_TRUST_ANCHORS`. A missing,
malformed, or unreadable file **fails closed** when a token is presented —
never a silent `method: "none"` downgrade.

Verification uses the optional `jwt` extra (`pip install 'readyagentsdev[jwt]'`,
**not** in `all`). Core installs and runs without it. Do not hand-roll JOSE.

Refused explicitly: `alg: none`, HMAC-vs-RSA algorithm confusion, unknown
`kid`, wrong issuer or audience, expired / nbf, oversized or malformed tokens.

## Replay

Each verified assertion is bound to `run_id`, node, and decision. The same
token cannot be reused on a gate. Default replay policy is `never`.

## CLI

```bash
readyagents identity verify --token-file ./id.jwt --json
readyagents decide RUN_ID --node gate --decision approve --token-file ./id.jwt
readyagents identity whoami --json
readyagents identity trust list
```

## Workload identity

Optional. Operator-provided key pair and subject (`READYAGENTS_WORKLOAD_SUBJECT`,
`READYAGENTS_WORKLOAD_KEY`). `whoami` prints subject, key id, and a public
fingerprint — never the private key. When configured, pause-notify webhooks
(`post_json`) attach a short-lived `Authorization: Bearer` JWT identifying
this agent. Unconfigured, no outbound identity is attached.
