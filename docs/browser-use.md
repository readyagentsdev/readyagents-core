# Governed browser use

**Unreleased on this checkout — not on the 1.9.0 tag.** `type: browser` is a
declared, policed, recorded node. It is not a free-running browser agent.

A workflow without a browser node is unchanged. Core installs with **no**
Playwright, Chromium, or Selenium dependency. The driver lives in an
**optional pack**. Tests and the example pack use an in-process stub.

```yaml
- id: fetch_statement
  type: browser
  allow: ["https://bank.example.com/*"]
  credentials: {host: "bank.example.com", secrets: [BANK_USER, BANK_PASS]}
  session: ephemeral
  limits: {wall_seconds: 120, pages: 10, download_bytes: 5000000}
  actions:
    - navigate: "https://bank.example.com/statements"
    - wait_for: {selector: "table.statements"}
    - extract: {schema: {rows: [{date: "td:nth-child(1)", amount: "td:nth-child(2)"}]}}
    - click: {selector: "a.download-latest", side_effecting: false}
  output_key: statement
```

Run the keyless stub fixture:

```bash
readyagents run examples/browser_statement.yaml --pack examples/packs/browser_pack.py
```

## Declared actions only

Allowed operations: `navigate`, `read`, `click`, `type`, `select`,
`wait_for` (YAML `wait-for` is an alias), `screenshot`, `download`,
`extract`. A model may choose among **declared** steps. It cannot synthesise
a new action. That constraint is the product, not a missing feature.

## Allowlist and SSRF

`allow` is a host and path-prefix list. It is enforced on navigation,
followed links, redirects, and sub-resource requests. Off-allowlist is a
typed error (`BrowserAllowlist`), not a warning. Redirects to private,
loopback, or metadata addresses are refused (`BrowserSSRF`), including
IPv4-mapped and decimal forms. The pin reuses the existing `http_get` IP
block. DNS failure on a public hostname does not bypass an allowlist
hostname match.

## Taint

Everything read from a page — visible text, hidden text, alt attributes,
DOM extracts, screenshots — is untrusted and attributed to the source URL.
Existing firewall rules (`on_tainted: deny` / `gate`) apply. Injected
instructions cannot cause a denied tool to run.

## Credentials

`credentials.host` plus `secrets` are brokered per step for that host only,
then scrubbed. Values never appear in the node output, the cassette, or
screenshot bytes. Typing a secret on another host is refused.

## Sessions

Default is `ephemeral`. `session: persist` requires that explicit
declaration **and logs a warning**. Core does not keep a cookie jar unless
the driver pack honours persist, and even then the warning is mandatory.

## Side-effecting clicks

A click whose selector or text looks like submit / purchase / delete / send
requires `side_effecting: false` (a declared allowance) or a human
approval. The approval prompt shows the target element and page context as
**untrusted**. An approval sourced from an attacker’s page is itself a
phishing surface.

## Downloads, extraction, bounds

Downloads are confined under the workspace, size-capped, and never
executed. Extraction is typed (selector or schema) and fails typed on
mismatch (`BrowserExtract`). Per-step wall-clock, page count, download
size, screenshot size, and memory each raise a **distinct** error.

## Trace and replay

Every action (operation, target, resulting URL, timing) is recorded into
the cassette. Screenshots are redacted by default. `readyagents runs replay
--offline` reproduces the step from the trace and **does not launch a
browser** or construct a driver.

## What this is not

- **No CAPTCHA solving**, ever.
- No claim of undetectability or stealth.
- No scraping-at-scale tooling.
- No headful automation of the operator’s personal browser profile.
- No credential entry beyond a brokered, declared, host-scoped fill.
- No browser engine bundled in core.

A live Playwright/Chromium pack is optional and out of tree. Until one is
installed, a missing driver is a typed `BrowserRefused` (`reason=driver`).
