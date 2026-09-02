# c-p0-15-env-image-compose-plugin — codex implementation review

## Round 1 — model gpt-5.6-sol — branch fix/c-p0-15-env-image-compose-plugin — base origin/main — 2026-09-02T15:55Z

<!-- codex-impl-review-status: complete -->
## Verdict
changes_requested

## Findings
**Location** env-image/Dockerfile.control:9 — **Severity** medium — **Issue** The image also ships the ~65 MB docker-buildx plugin even though the Dockerfile explicitly states the selected Compose v2.39.2 works without it. This conflicts with the DECIDED requirement to keep the image minimal and expands scope beyond the Compose fix. — **Fix** Remove docker-buildx from the plugin `COPY`, documentation, and workflow assertion; retain only the digest-pinned Compose plugin and `docker compose version` check.

## Checked
- **Compose plugin installation — pass:** `docker-compose` is copied from a digest-pinned Docker CLI image into a recognized CLI-plugin directory.
- **Pinned version/checksum — pass:** The source image is pinned by SHA-256 digest, and the documented bundled Compose version is v2.39.2.
- **Control-image documentation — pass:** The carried-tools block lists Compose and still explicitly states that `just` is absent.
- **Minimal image — fail:** Unrequired docker-buildx adds substantial image size and an additional maintenance surface.
- **Build check — pass:** The workflow runs `docker compose version` against the newly built control image before pushing it.
- **Tests-first — pass:** Commit `1e2f832` adds the real container smoke test before production commit `a919979`; its message records the expected failing exit and subsequent successful run.
- **Local verification — pass with environment limitation:** Commit evidence records successful image builds and Compose smoke tests; rerunning locally was unavailable because this review environment cannot access the Docker daemon.
- **Manifest lint — pass/not applicable:** No Kubernetes manifests changed.
- **Credential exposure — pass:** No bearer token, `auth.json`, `sk-ant-`, JWT, or newly exposed credential path appears in the reviewed commits.
- **Wire compatibility — pass/not applicable:** No API, DTO, data-testid, mock-server, or broker-stub changes.
- **Repository rules and scope — fail:** No applicable `AGENTS.md` or `TESTING.md` was present, but the buildx addition is outside the requested minimal Compose change.
- **Claimed tiers plausible — pass:** The commit-recorded Docker build and runtime smoke commands directly exercise the modified image.
- **Forge diff/base movement — pass:** `git merge-base origin/main HEAD` equals `HEAD` because the branch has already been merged; therefore `git diff origin/main...HEAD` is correctly empty, while the four branch commits were reviewed directly.
## Round 2 — model gpt-5.6-sol — branch fix/c-p0-15-env-image-compose-plugin — base origin/main — 2026-09-02T15:59Z

<!-- codex-impl-review-status: complete -->
## Verdict
changes_requested
## Findings
**Location** env-image/Dockerfile.control:9 — **Severity** medium — **Issue** The control image still ships the unrelated ~65 MB Docker Buildx plugin even though the selected Compose v2.39.2 works without it, contradicting the DECIDED minimal-image scope. The round-1 finding remains unresolved. — **Fix** Remove Buildx from the `COPY`, carried-tools documentation, and workflow assertion; retain the digest-pinned Compose plugin and `docker compose version` check.
## Checked
- **Compose plugin installation — pass:** Compose is copied into a recognized CLI-plugin directory from the pinned Docker CLI image.
- **Pinned version/checksum — pass:** The source image is pinned by SHA-256 digest and supplies documented Compose v2.39.2.
- **Control-image documentation — pass:** The carried-tools block lists Compose and explicitly retains the “No `just`” statement.
- **Minimal image — fail:** Buildx adds substantial size and maintenance surface outside the requested Compose fix.
- **Build check — pass:** The workflow executes `docker compose version` against the built control image before push.
- **Tests-first — pass:** Commit `1e2f832` adds the real container smoke assertion before production commit `a919979`, with the pre-fix failure recorded.
- **Local verification — pass with environment limitation:** The commits record successful builds and container smoke tests; rerunning was impossible because this environment cannot access `/var/run/docker.sock`.
- **Manifest lint — pass/not applicable:** No Kubernetes manifests changed and no `kubectl` command was introduced.
- **Credential exposure — pass:** No bearer token, `auth.json`, `sk-ant-`, JWT, or new credential-output path appears in the implementation diff.
- **Wire compatibility — pass/not applicable:** No API/DTO, data-testid, mock-server, or broker-stub changes.
- **Repository rules and scope — fail:** No applicable `AGENTS.md` or `TESTING.md` was found, but the Buildx addition is an unrelated expansion beyond the DECIDED scope.
- **Claimed verification tiers — pass:** The recorded Docker build and runtime commands directly exercise the changed image and are plausible from the diff.
- **Merge-base/forge diff — pass:** The three-dot diff now contains only the conventional review artifact because the implementation was merged into `origin/main`; base-movement-only files were not treated as findings.
- **Review artefact convention — pass:** `plans/c-p0-15-env-image-compose-plugin-impl-review.md` is present and was not treated as a finding.