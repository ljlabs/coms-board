---
name: playwright
description: "Drive a real browser from an agent using the Docker image's preinstalled Playwright CLI and Chromium. Keep browser automation CLI-first; use Playwright test files only when requested."
trigger_words: ["playwright", "browser automation", "browser test", "test website", "automate browser"]
---

Use the `playwright-cli` command directly. The Swarm Agent Docker image includes a pinned CLI, its matching Chromium browser, and Linux browser dependencies. Do not install packages with npm or npx from an agent session: the image root is read-only, and runtime downloads are not a durable setup.

## Check the runtime

```bash
command -v playwright-cli
playwright-cli --help
```

If the command is missing, ask the operator to rebuild the Docker image with `docker compose up --build -d`; do not try to repair the running container with npm or npx.

The Docker CLI config selects Chromium and writes browser output to `/home/board/playwright-output`. That path and Playwright's cache/profile directory under `/home/board/.cache` are backed by the persistent `agent-state` volume. The browser executable itself is baked into the image at `/ms-playwright`, so it remains available after container recreation. Rebuilding the image replaces the CLI/browser versions; the agent-state volume and browser profiles remain.

The agent workspace may be mounted read-only. Keep screenshots, PDFs, snapshots, traces, and other generated browser artifacts in `/home/board/playwright-output`, not in the workspace. The container has no display; run headless and inspect screenshots or snapshots instead of passing `--headed`.

## Quick start

```bash
playwright-cli open https://playwright.dev
playwright-cli snapshot
playwright-cli click e15
playwright-cli type "Playwright"
playwright-cli press Enter
playwright-cli screenshot
```

The CLI returns the current page snapshot after each action. Automatic output files go to the configured output directory. To name an artifact explicitly, give a path in that directory:

```bash
playwright-cli screenshot --filename=/home/board/playwright-output/result.png
playwright-cli snapshot --filename=/home/board/playwright-output/result.yml
```

## Core workflow

1. Open the page.
2. Snapshot to get current element references.
3. Interact with references from the latest snapshot.
4. Take a new snapshot after navigation or significant page changes.
5. Capture screenshots, PDFs, or traces when they help verify the behavior.

For a reusable browser profile across browser restarts, use `--persistent`. The profile is stored beneath the persistent Playwright cache:

```bash
playwright-cli -s=swarm-check open https://example.com --persistent
playwright-cli -s=swarm-check snapshot
playwright-cli -s=swarm-check close
```

## Guardrails

- Always snapshot before using element references like `e12`; take another snapshot when refs become stale.
- Prefer explicit CLI commands over `eval` and `run-code` unless needed.
- Use headless mode in Docker. A browser window cannot be shown from the container.
- Close sessions when finished. Use `playwright-cli close-all` if a session becomes stale.
- Default to CLI commands and workflows, not Playwright test specs.
