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

