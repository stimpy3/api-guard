# api-guard

Blocks **unacknowledged** breaking changes to an OpenAPI contract in CI.

It reads your spec, never your source code, so it works on any codebase in any
language. The only requirement is that your project can produce an OpenAPI
document.

## What it checks

| Check | Question | Fails when |
|---|---|---|
| **freshness** | Does the committed spec still match the code? | Somebody changed the code and forgot to regenerate the spec |
| **breaking** | Would this change break existing consumers? | A field or endpoint is removed, or a parameter becomes required |
| **conformance** | Does the running API still honour the spec? | The spec is unchanged but the implementation drifted from it |

Freshness exists because without it the other two are worthless: they would
both pass against a stale contract and report a green build on a broken API.

## Add it to your project

**1.** Write `api-guard.yaml` next to your spec:

```yaml
spec:
  path: openapi.yaml
  base: "git:origin/main"
  generate_cmd: "python scripts/export_openapi.py --stdout"   # omit if hand-written
runtime:
  url: http://localhost:8000                                  # omit to skip conformance
```

**2.** Run it:

```bash
docker run --rm -v "$PWD:/work" -w /work <user>/api-guard:1 check
```

**3.** Wire it into CI. Exit codes are the whole integration:

| Code | Meaning |
|---|---|
| `0` | Contract intact, or every breach waived |
| `1` | Contract would break consumers |
| `2` | api-guard could not reach a conclusion (bad config, missing tool) |

`1` and `2` are deliberately distinct. A typo'd URL reported as "breaking change
detected" sends people hunting for a change that does not exist, and after that
happens twice they stop believing the gate.

## When the change is intentional

Requirements change; the gate is not a wall. Its job is to stop breaking changes
nobody *noticed*, not every breaking change. Two ways through:

### Retiring an endpoint — no exception needed

Mark it deprecated with a sunset date, ship that, and delete it after the date
passes:

```yaml
paths:
  /users/search:
    get:
      deprecated: true
      x-sunset: '2027-03-01'
```

Removing it before the sunset date is reported as
`api-path-removed-before-sunset`. Removing it after is clean — the promise was
kept.

### Everything else — an expiring waiver

`x-sunset` only applies to endpoints. For a response **field**, the breaking
moment is demoting it from required to optional; once optional, deleting it is
free. That demotion needs acknowledging in `waivers.yaml`:

```yaml
- fingerprint: "3c11fcf1ab0e"        # printed in the failure output
  id: response-property-became-optional
  path: /users
  reason: "PROD-142 - email superseded by phone, both consumers migrated."
  approved_by: sohan
  expires: 2026-12-31
```

Waivers match on oasdiff's `fingerprint`, which identifies the change rather
than its location — reformatting a spec does not invalidate them.

Waivers **expire**, and an expired one fails the build. An ignore list that
lives forever silently swallows future breakages on the same endpoint, which is
how a gate quietly stops being one. Because `waivers.yaml` is committed, every
waiver is reviewed in a pull request and preserved in git history: the
acknowledgement becomes an auditable artifact, which is the entire point.

Applied waivers are printed on every run, including passing ones.

## Configuration

```yaml
spec:
  path: openapi.yaml            # required
  base: "git:origin/main"       # git:<ref> | ./path.yaml | https://...
  generate_cmd: null            # enables the freshness check

runtime:                        # omit the section to skip conformance
  url: http://localhost:8000
  checks: [not_a_server_error, response_schema_conformance, status_code_conformance]
  max_examples: 50
  wait_for_schema: 30

policy:
  fail_on: ERR                  # ERR | WARN
  deprecation_days_stable: 180
  deprecation_days_beta: 30
  severity_levels: null         # oasdiff severity-levels file, for org-wide tuning
  waivers: waivers.yaml

report:
  dir: api-guard-report
  formats: [markdown, json, junit]
```

`result.json` is the machine-readable interface — PR comments, dashboards and
the MCP server all read it rather than scraping console output. It carries a
`schema_version`.

## Notes for CI

- **GitHub Actions needs `fetch-depth: 0`.** A shallow clone has no
  `origin/main`, so there is nothing to compare against.
- **Run the gate before publishing the image.** A broken contract should never
  produce a deployable artifact.
