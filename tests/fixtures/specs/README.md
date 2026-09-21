# Spec fixtures

Small OpenAPI pairs used to pin down oasdiff's actual behaviour. Each pair
documents something that was **verified by running oasdiff**, not assumed — a
few of these contradicted what the documentation implied.

## What each pair proves

| Base | Revision | Result | Point |
|---|---|---|---|
| `base.yaml` | `safe-property-added.yaml` | clean | Additive changes must not fail the gate |
| `base.yaml` | `breaking-property-removed.yaml` | `response-required-property-removed` (error) | The core detection |
| `base-shifted.yaml` | `breaking-property-removed.yaml` | same `fingerprint` as above | **Fingerprints are location-independent** |
| `deprecated-sunset-past.yaml` | `deprecated-removed.yaml` | endpoint clean, field still flagged | Sunset covers endpoints, **not** fields |
| `deprecated-sunset-future.yaml` | `deprecated-removed.yaml` | `api-path-removed-before-sunset` | The sunset promise is enforced |
| `deprecated-optional-sunset-past.yaml` | `deprecated-removed.yaml` | clean | Removing an *optional* field is fine |
| `optional-no-deprecation.yaml` | `deprecated-removed.yaml` | clean | ...with or without deprecation metadata |
| `base.yaml` | `optional-no-deprecation.yaml` | `response-property-became-optional` (error) | **The real breaking moment for a field** |

## Findings that shaped the design

**1. `oasdiff breaking` exits 0 on breaking changes unless `--fail-on` is passed.**
It reports the change and returns success. A pipeline that trusted the exit code
without `--fail-on` would pass every build forever — a gate that always says yes.
`--fail-on` accepts only `ERR` or `WARN`, not `INFO`.

**2. `x-sunset` on a response property is ignored.**
Only optionality matters. Removing an optional field is clean whether or not it
was marked deprecated; demoting a required field to optional is `error`
regardless of any sunset date. So endpoints have a zero-exception retirement
path and response fields do not — field retirement pays its cost up front, at
the demotion.

**3. `fingerprint` identifies the change, not its location.**
Reformatting a spec so every definition lands on a different line produced the
identical fingerprint. That makes it a safe key for waiver matching, which is
why api-guard applies waivers itself against the JSON rather than using
oasdiff's text-matching `--err-ignore` file.

**4. Severity levels encode as integers:** `info` = 1, `warn` = 2, `error` = 3.

## Regenerating the evidence

```bash
docker run --rm -v "$PWD:/specs" tufin/oasdiff:latest \
  breaking /specs/base.yaml /specs/breaking-property-removed.yaml \
  --fail-on ERR --format json
```
