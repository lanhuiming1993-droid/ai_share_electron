const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("alphadesk", {
  saveHtmlReport: ({ filename, html }) => ipcRenderer.invoke("alphadesk:save-html-report", { filename, html }),
});
