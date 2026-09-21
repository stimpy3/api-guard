# api-guard: the contract gate, packaged so a team can adopt it without
# installing Python, Go, oasdiff or Schemathesis.
#
#   docker run --rm -v "$PWD:/work" -w /work <user>/api-guard:1 check
#
# Two stages because the tools have different runtimes: oasdiff is a Go binary,
# Schemathesis is Python. Copying the binary out of the official image beats
# re-downloading a release tarball - it is the artifact its maintainers publish.

# Pinned by digest, not tag. `tufin/oasdiff:latest` reports its version as a git
# sha rather than a semver, so the tag alone is not reproducible - a rebuild
# months from now would silently pick up different breaking-change rules and the
# demo would stop matching the report. To upgrade deliberately:
#   docker pull tufin/oasdiff:latest
#   docker inspect tufin/oasdiff:latest --format '{{index .RepoDigests 0}}'
FROM tufin/oasdiff@sha256:0286f138545a39010525df6c1bea67ffafacb384ef800effffa63bbd04718ce5 AS oasdiff

FROM python:3.11-slim

# git is not optional: `spec.base: "git:origin/main"` shells out to `git show`
# to read the previous contract.
RUN apt-get update \
    && apt-get install --no-install-recommends -y git \
    && rm -rf /var/lib/apt/lists/*

# The mounted repo is owned by the host user, not by the container's. Without
# this git refuses to read it ("detected dubious ownership"), which shows up as
# a confusing "no spec at origin/main" rather than a permissions message. The
# container is short-lived and only reads the repo it was handed.
RUN git config --global --add safe.directory '*'

COPY --from=oasdiff /usr/bin/oasdiff /usr/local/bin/oasdiff

WORKDIR /src
COPY pyproject.toml README.md ./
COPY src/ ./src/
# [cli] pulls in Schemathesis for the conformance check. The base install
# deliberately omits it so a team that only wants breaking-change detection is
# not made to carry a property-testing framework.
#
# EXTRAS=cli,ai builds the :N-ai variant, which adds langchain and langgraph
# for `--explain`. That is roughly a hundred megabytes of machine-learning
# dependencies for a feature which by design cannot change a build result, so
# the default image does without it.
ARG EXTRAS=cli
RUN pip install --no-cache-dir ".[${EXTRAS}]" && rm -rf /root/.cache

# Where the caller's repository gets mounted.
WORKDIR /work

LABEL org.opencontainers.image.title="api-guard" \
      org.opencontainers.image.description="Block unacknowledged breaking changes to an OpenAPI contract." \
      org.opencontainers.image.licenses="MIT"

ENTRYPOINT ["api-guard"]
CMD ["check"]
