## Tooling Preferences

- Treat a workspace `.venv` as Codex's project-local auxiliary toolbox, even when the project's main language is Vue, Java, Rust, Electron, Node, Go, or something else.
- If any non-primary-language auxiliary work is needed in a project, create a `.venv` in the workspace when one does not already exist, unless the user asks for a different location.
- When a workspace contains a `.venv`, use that environment for Python work instead of the global Python installation.
- On Windows, run project Python commands through `.venv\Scripts\python.exe` and project package installs through `.venv\Scripts\python.exe -m pip ...`.
- Do not install Python packages into the global interpreter for project work unless the user explicitly asks for that.
- For internet data collection, API calls, documentation lookup, financial data retrieval, data cleaning, structured parsing, report generation, one-off transformations, and other auxiliary automation, prefer Python scripts and Python libraries over raw terminal commands.
- For A-share and market-data tasks, prefer `akshare`, `requests`, `pandas`, and other Python data tools when they are available and appropriate.
- Default tool preference for auxiliary work: project `.venv` Python first, then Playwright/browser tools when a real browser is needed, then shell-native commands as a fallback.
- Use the project's native toolchain for primary project operations: for example npm/pnpm/yarn for Vue/Electron/Node work, Gradle/Maven for Java work, cargo for Rust work, and the repo's existing scripts for builds, tests, debugging, and dependency management.
- Use Playwright/browser tools when the task requires real page rendering, interaction, screenshots, authentication flows, local UI debugging, or browser-only behavior.
- Use shell-native commands such as `curl`, `Invoke-WebRequest`, or ad hoc PowerShell parsing only when Python is unavailable, when a simple shell command is clearly sufficient, when invoking the primary project toolchain, or when the user explicitly asks for terminal-level verification.
- Shell commands remain appropriate for environment inspection, dependency installation, running tests, starting local services, invoking build tools, git operations, and file-system operations.
- When using Python for live data, keep the script small, show the relevant data sources or package APIs used, and avoid hiding important assumptions in one-off parsing code.

## Cloud Agent Lite Context

- This workspace is the `codex/cloud-agent-lite` branch. Treat it as the cloud Hermes + AlphaDesk agent project, not the old desktop/Electron working context.
- The cloud profile keeps only three core AlphaDesk sources: WeRSS official-account RSS, ZSXQ MCP topics, and IMA OpenAPI knowledge base. Other market-data and desktop-browser workflows are not part of the cloud agent baseline unless explicitly reintroduced.
- Hermes owns all LLM usage: command understanding and industry-analysis/PDF generation. AlphaDesk backend and collectors only collect structured evidence, maintain watermarks, persist snapshots, expose evidence/report APIs, and provide PDF rendering helpers.
- Current Hermes-side assets live under `deploy/hermes/`:
  - `alphadesk-cloud-report/`: main Hermes skill for three-source collection, source auth, report rendering, and verification scripts.
  - `plugins/alphadesk-command/`: gateway plugin that rewrites WeChat/Lightclaw intents into AlphaDesk commands, invokes evidence collection, adds cross-validation evidence, and returns `MEDIA:/...pdf`.
  - `a-share-growth-hunter/`: company-level A-share growth-stock analysis framework. It is an analysis frame, not a source, and is injected only for stock/company/growth/Davis-double-play style queries.
- Cross-validation skills currently expected on the Hermes host include announcement search, report search, finance, event, business, industry, market, institutional research, and A-stock selector. They are supplemental evidence layers on top of AlphaDesk.
- Do not put API keys, MCP URLs containing keys, bot tokens, WeRSS passwords, cookies, or private paid content into committed files. Use env files or local-only notes.
- For server-specific operational notes, prefer a local untracked `LOCAL_CLOUD_AGENT_CONTEXT.md` when present.
