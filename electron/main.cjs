const { app, BrowserWindow } = require("electron");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

let apiProcess;
let mainWindow;

const hasInstanceLock = app.requestSingleInstanceLock();
if (!hasInstanceLock) app.quit();

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
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
  const root = path.join(__dirname, "..");
  const python = path.join(root, ".venv", "Scripts", "python.exe");
  apiProcess = spawn(python, ["-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", "8765"], {
    cwd: root,
    windowsHide: true,
    stdio: "inherit",
  });
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
  if (await apiIsHealthy()) return;
  startApi();
  for (let attempt = 0; attempt < 40; attempt += 1) {
    await sleep(250);
    if (await apiIsHealthy()) return;
  }
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
    webPreferences: { contextIsolation: true, sandbox: true },
  });
  const devUrl = process.env.VITE_DEV_SERVER_URL;
  mainWindow.loadURL(devUrl || `file://${path.join(__dirname, "..", "frontend", "dist", "index.html")}`);
}

app.on("second-instance", () => {
  if (!mainWindow) return;
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.focus();
});

app.whenReady().then(async () => {
  await ensureApi();
  createWindow();
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("before-quit", () => {
  stopApi();
});
