# Live command-only UI regression

Operator-only, real browser test against the existing `http://127.0.0.1:3080` GUI. No response interception, no alternate server, no ordinary chat inference, and no conductor start/pause/resume calls.

Prerequisites: Node24, normal authorized in-cluster kubectl access, Playwright Chromium and native libraries, `/workspace` writable. The test reads the current pod's normal launch URL in memory, signs in normally, and never persists auth storage, raw transport frames, trace, video or cookie headers. It refuses execution unless explicitly opted in. No credential-file access or auth weakening.

```sh
npm ci --ignore-scripts
# On a normal development runner, install the browser/native dependencies first.
npx playwright install chromium
DSH_RUN_LIVE_UI_TEST=1 npm test
```

For this deployment's pre-extracted native libraries, set `DSH_BROWSER_LIBRARY_PATH` to the two library directories under `/workspace/conductor-ui-e2e/native-deps/root` (`usr/lib/x86_64-linux-gnu` and `lib/x86_64-linux-gnu`, colon-separated). This environment override affects only the browser child. Use an init/subreaper when running repeatedly in containers whose PID1 does not reap Chromium descendants.

Assertions: first `/conductor status` visibly renders in a newly created workspace without a model turn; zero runs; output remains visible after refresh; bare `/conductor` aliases status; unsupported argument yields a visible usage error. The empty-argument command is submitted using Send because Enter can select an autocomplete item rather than execute it.

A finally block deletes only the test's unique workspace registration via the actual GUI and verifies absence after refresh. It removes the directory only if empty. Session audit logs are intentionally preserved by Harness's workspace-delete semantics. Any cleanup failure fails the run; manually remove the exact generated name if needed.

Artifacts: ignored `report.json` and `test-results/`, with safe command results and screenshots. Auth tokens/cookies are excluded. Screenshots contain the visible application, so treat them as operator evidence, not public assets.

Before deployment, this checked-in test failed on the first visibility assertion (12seconds); cleanup succeeded after refresh. After the GitOps rollout the same test must pass without any browser patch.
