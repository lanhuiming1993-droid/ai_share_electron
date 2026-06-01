const { app, BrowserWindow, dialog, ipcMain, shell } = require("electron");
const { spawn } = require("child_process");
const fs = require("fs/promises");
const http = require("http");
const path = require("path");

let apiProcess;
let mainWindow;
const root = path.join(__dirname, "..");
const electronLogDir = path.join(root, "data", "logs");
const electronLogPath = path.join(electronLogDir, "electron.jsonl");
const electronLogMaxBytes = 2 * 1024 * 1024;
let electronLogQueue = Promise.resolve();

const hasInstanceLock = app.requestSingleInstanceLock();
if (!hasInstanceLock) app.quit();

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function redactLogValue(value, key = "") {
  const normalizedKey = key.toLowerCase().replaceAll("-", "_");
  if (/api.?key|authorization|cookie|password|secret|har.?text/i.test(normalizedKey) || normalizedKey === "token" || normalizedKey.endsWith("_token")) return "[REDACTED]";
  if (Array.isArray(value)) return value.map((item) => redactLogValue(item));
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([itemKey, itemValue]) => [itemKey, redactLogValue(itemValue, itemKey)]));
  if (typeof value === "string") return value.replace(/\bsk-[A-Za-z0-9_-]{10,}\b/gi, "[REDACTED]").slice(0, 4000);
  return value;
}

async function rotateElectronLog() {
  try {
    const stat = await fs.stat(electronLogPath);
    if (stat.size < electronLogMaxBytes) return;
    await fs.rm(`${electronLogPath}.4`, { force: true });
    for (let index = 3; index >= 1; index -= 1) {
      try { await fs.rename(`${electronLogPath}.${index}`, `${electronLogPath}.${index + 1}`); } catch {}
    }
    await fs.rename(electronLogPath, `${electronLogPath}.1`);
  } catch {}
}

function electronLog(level, event, fields = {}) {
  electronLogQueue = electronLogQueue.then(async () => {
    await fs.mkdir(electronLogDir, { recursive: true });
    await rotateElectronLog();
    await fs.appendFile(electronLogPath, `${JSON.stringify({ timestamp: new Date().toISOString(), level, component: "electron", event, fields: redactLogValue(fields) })}\n`, "utf8");
  }).catch(() => {});
}

function apiIsHealthy() {
  return new Promise((resolve) => {
    const request = http.get("http://127.0.0.1:8765/health", { timeout: 900 }, (response) => {
      let body = "";
      response.on("data", (chunk) => { body += chunk; });
      response.on("end", () => {
        try {
          const health = JSON.parse(body);
          resolve(response.statusCode === 200 && health.service === "alphadesk-local-api");
        } catch {
          resolve(false);
        }
      });
    });
    request.on("timeout", () => { request.destroy(); resolve(false); });
    request.on("error", () => resolve(false));
  });
}

function startApi() {
  const python = path.join(root, ".venv", "Scripts", "python.exe");
  electronLog("info", "api_process.starting", { python });
  apiProcess = spawn(python, ["-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", "8765"], {
    cwd: root,
    windowsHide: true,
    stdio: "inherit",
  });
  apiProcess.on("error", (error) => electronLog("error", "api_process.failed", { error: error.message }));
  apiProcess.on("exit", (code, signal) => electronLog(code ? "error" : "info", "api_process.exited", { code, signal }));
}

function stopApi() {
  if (!apiProcess) return;
  if (process.platform === "win32") {
    spawn("taskkill", ["/pid", String(apiProcess.pid), "/t", "/f"], { windowsHide: true, stdio: "ignore" });
  } else {
    apiProcess.kill();
  }
}

async function ensureApi() {
  if (await apiIsHealthy()) {
    electronLog("info", "api_process.reused");
    return;
  }
  startApi();
  for (let attempt = 0; attempt < 40; attempt += 1) {
    await sleep(250);
    if (await apiIsHealthy()) {
      electronLog("info", "api_process.healthy", { attempt });
      return;
    }
  }
  electronLog("error", "api_process.health_timeout");
  throw new Error("Local API did not become healthy");
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1480,
    height: 940,
    minWidth: 1180,
    minHeight: 760,
    backgroundColor: "#08111f",
    titleBarStyle: "hiddenInset",
    webPreferences: {
      contextIsolation: true,
      sandbox: true,
      preload: path.join(__dirname, "preload.cjs"),
    },
  });
  const devUrl = process.env.VITE_DEV_SERVER_URL;
  mainWindow.loadURL(devUrl || `file://${path.join(__dirname, "..", "frontend", "dist", "index.html")}`);
  electronLog("info", "window.created", { dev_url: devUrl || "" });
}

ipcMain.handle("alphadesk:save-html-report", async (_event, payload = {}) => {
  const html = typeof payload.html === "string" ? payload.html : "";
  if (!html.trim()) throw new Error("HTML report content is empty");
  const requestedName = typeof payload.filename === "string" ? payload.filename : "AlphaDesk-report.html";
  const safeName = requestedName.replace(/[<>:"/\\|?*\u0000-\u001f]/g, "_").slice(0, 120) || "AlphaDesk-report.html";
  const filename = safeName.toLowerCase().endsWith(".html") ? safeName : `${safeName}.html`;
  const result = await dialog.showSaveDialog(mainWindow, {
    title: "导出 HTML 报告",
    defaultPath: path.join(app.getPath("documents"), filename),
    filters: [{ name: "HTML 报告", extensions: ["html"] }],
  });
  if (result.canceled || !result.filePath) return { status: "cancelled" };
  await fs.writeFile(result.filePath, html, "utf8");
  electronLog("info", "report.exported", { file_path: result.filePath, html_chars: html.length });
  return { status: "saved", filePath: result.filePath };
});

ipcMain.handle("alphadesk:open-external-url", async (_event, payload = {}) => {
  const requestedUrl = typeof payload.url === "string" ? payload.url.trim() : "";
  const parsed = new URL(requestedUrl);
  if (!["http:", "https:"].includes(parsed.protocol)) throw new Error("Only http and https URLs can be opened");
  await shell.openExternal(parsed.toString());
  electronLog("info", "external_url.opened", { protocol: parsed.protocol, hostname: parsed.hostname });
  return { status: "opened" };
});

app.on("second-instance", () => {
  if (!mainWindow) return;
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.focus();
});

app.whenReady().then(async () => {
  electronLog("info", "application.ready");
  await ensureApi();
  createWindow();
}).catch((error) => {
  electronLog("error", "application.startup_failed", { error: error.message });
  dialog.showErrorBox("AlphaDesk 启动失败", error.message);
  app.quit();
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("before-quit", () => {
  electronLog("info", "application.before_quit");
  stopApi();
});
