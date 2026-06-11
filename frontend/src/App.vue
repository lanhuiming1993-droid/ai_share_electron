<script setup>
import { computed, onMounted, onUnmounted, reactive, ref, watch } from "vue";

const API = import.meta.env.VITE_API_BASE_URL || "";
const webOrigin = window.location.origin;
const data = reactive({ tools: [], channels: [], skills: [], tasks: [], source_jobs: [], provider: null, providers: [], codex_policy: null, research_red_lines: null, source_weights: { configured: false, total_weight: 0, weights: [] }, audit: { jobs: [], watermarks: [], snapshots: [], normalized: [], events: [], inventory: { snapshot_count: 0, normalized_item_count: 0, source_report_count: 0, research_report_count: 0 } } });
const provider = reactive({ name: "", base_url: "", model: "", api_key: "", protocol: "openai_chat_completions", enabled: true, extra_body_text: "{}" });
const task = reactive({ title: "成长股六维研究", target: "", objective: "识别财务拐点、赛道卡位、客户订单、产能交付、机构变化和未来催化", skill_name: "a-share-growth-hunter", lookback_days: 30 });
const sourceJob = reactive({ action: "collect", channel_ids: [], lookback_days: 30, report_title: "近 30 天信源聚合报告", skill_name: "a-share-growth-hunter" });
const activePage = ref("dashboard");
const notice = ref("");
const selectedReport = ref("");
const selectedReportTitle = ref("");
const reportModal = ref(false);
const reportPdfExporting = ref(false);
const sourceJobSubmitting = ref(false);
const sourceJobFeedback = ref("");
const sourceJobFeedbackType = ref("info");
const snapshotModal = ref(false);
const snapshotDetail = reactive({ job: null, snapshots: [] });
const snapshotPreviewLoadingId = ref("");
const normalizedModal = ref(false);
const normalizedDetail = reactive({ title: "", items: [] });
const saving = ref(false);
const providerModal = ref(false);
const channelModal = ref(false);
const editingChannel = ref(null);
const editingProviderId = ref("");
const channelForm = reactive({ name: "", type: "", url: "", collection_mode: "playwright", status: "pending", notes: "", validation_url: "", success_url_contains: "", success_selector: "", group_ids: [], parsing_strategy: "hybrid", normalization_quality_threshold: 60, max_scrolls: 8, research_enabled: false });
const marketDataForm = reactive({ enable_akshare: true, enable_baostock: true, enable_tushare: true, tushare_token: "", tushare_token_configured: false, clear_tushare_token: false, component_timeout_seconds: 35 });
const imaForm = reactive({ client_id: "", api_key: "", api_key_configured: false, skill_download_url: "https://app-dl.ima.qq.com/skills/ima-skills-1.1.7.zip", clear_credentials: false });
const itickForm = reactive({ api_base: "https://api0.itick.org", api_key: "", api_key_configured: false, default_symbols_text: "HK:700\nUS:AAPL\nSH:600519", kline_type: 2, kline_limit: 60, timeout_seconds: 20, clear_credentials: false });
const xTwtApiForm = reactive({ api_base: "https://api.twtapi.com/api/v1/twitter", api_key: "", api_key_configured: false, default_queries_text: "A股\n半导体\n光伏\n机器人", tracked_users_text: "", result_type: "Latest", max_results: 20, timeout_seconds: 20, lang: "zh", clear_credentials: false });
const wechatRssForm = reactive({ base_url: "http://127.0.0.1:8001", feed_ids_text: "all", access_key: "", secret_key: "", credentials_configured: false, admin_username: "admin", admin_password: "", admin_password_configured: false, clear_credentials: false, timeout_seconds: 20, max_items_per_feed: 100 });
const wechatRssComponent = reactive({ status: "pending", message: "尚未检查", ready: false, service_online: false, rss_online: false, subscription_count: 0, subscriptions: [], subscription_error: "", docker_available: false, docker_engine_available: false, managed_setup_available: false, management_url: "", onboarding_steps: [] });
Object.assign(wechatRssComponent, { wechat_authorized: false, wechat_login_state: "unknown", wechat_message: "", admin_authorized: false, qr_available: false });
const wechatRssComponentLoading = ref(false);
const wechatRssStarting = ref(false);
const wechatRssLoginModal = ref(false);
const wechatRssLoginLoading = ref(false);
const wechatRssLogin = reactive({ login_state: "idle", message: "点击登录后获取微信二维码", qr_image_url: "", qr_base_url: "", qr_loaded: false, authorized: false });
const wechatRssSearch = reactive({ query: "", items: [], loading: false, adding_id: "", removing_id: "", backfilling_id: "", backfilling_all: false, adding_panel_open: false, backfill_start_page: 0, backfill_end_page: 1 });
const wechatRssQueueClearing = ref("");
const mxHarFile = ref(null);
const mxHarImporting = ref(false);
const inventoryCleanupSubmitting = ref(false);
const taskListCleanupSubmitting = ref(false);
const sourceWeightsSaving = ref(false);
const diagnosticLogs = ref([]);
const diagnosticConfig = reactive({ directory: "", active_file: "", max_file_mb: 0, backup_count: 0, files: [] });
const diagnosticFilters = reactive({ level: "", component: "", search: "" });
const diagnosticLoading = ref(false);
const frontendLogQueue = [];
let frontendLogFlushing = false;
let frontendLogFlushTimer;

const navItems = [
  { id: "dashboard", label: "仪表盘", hint: "Dashboard", icon: "grid" },
  { id: "tasks", label: "任务与报告", hint: "Research", icon: "file" },
  { id: "providers", label: "模型供应商", hint: "Providers", icon: "cpu" },
  { id: "channels", label: "信源渠道", hint: "Sources", icon: "radio" },
  { id: "audit", label: "采集审计", hint: "Audit", icon: "audit" },
  { id: "skills", label: "Skill 管理", hint: "Skills", icon: "spark" },
  { id: "settings", label: "系统设置", hint: "Settings", icon: "settings" },
];

const pageMeta = {
  dashboard: ["仪表盘", "模型服务、信源状态和研究任务概览"],
  tasks: ["任务与报告", "检查采集任务状态，审阅模型生成的研究计划和报告"],
  providers: ["模型供应商", "配置用于 Agent 编排和分析的 OpenAI-compatible 模型服务"],
  channels: ["信源渠道", "维护公开数据、登录态渠道和浏览器采集能力"],
  audit: ["采集审计", "检查采集窗口、水位、快照数量和 Agent 证据推进记录"],
  skills: ["Skill 管理", "管理 Agent 可加载的领域能力和研究方法"],
  settings: ["系统设置", "检查核心红线、本地服务和开发环境信息"],
};

const readyTools = computed(() => data.tools.filter((x) => x.status === "ready").length);
const pendingChannels = computed(() => data.channels.filter((x) => x.status === "pending").length);
const pageTitle = computed(() => pageMeta[activePage.value][0]);
const pageSubtitle = computed(() => pageMeta[activePage.value][1]);
const wechatRssAuthorized = computed(() => Boolean(wechatRssComponent.wechat_authorized || wechatRssLogin.authorized || wechatRssComponent.ready));
const sourceWeightTotal = computed(() => Number((data.source_weights?.weights || []).reduce((sum, item) => sum + Number(item.weight || 0), 0).toFixed(2)));
const sourceWeightsValid = computed(() => Math.abs(sourceWeightTotal.value - 100) <= 0.01 && (data.source_weights?.weights || []).some((item) => Number(item.weight || 0) > 0));
const canonicalChannelNames = {
  akshare: "AkShare 市场数据",
  itick: "iTick 行情 API",
  "x-twtapi": "X（TwtAPI）",
  "industry-news": "产业趋势公开资讯",
  "wechat-mp-rss": "微信公众号（WeRSS）",
  "ima-knowledge": "IMA 知识库",
  zsxq: "知识星球",
  "web-rumors": "MX 小作文频道",
  "146aa28e21": "TG 小作文频道",
};
function channelDisplayName(channelOrId) {
  const channelId = typeof channelOrId === "string" ? channelOrId : channelOrId?.id || channelOrId?.channel_id;
  const channel = typeof channelOrId === "object" ? channelOrId : data.channels.find((item) => item.id === channelId);
  if (channelId === "akshare") return "A股市场数据（AkShare / BaoStock / TuShare）";
  return canonicalChannelNames[channelId] || channel?.name || channelId || "未知信源";
}
function channelStatusDescription(channel) {
  if (channel.status === "online") {
    if (channel.id === "wechat-mp-rss") return "微信公众号快照采集可用";
    if (channel.id === "ima-knowledge") return "IMA 知识库检索可用";
    if (channel.id === "itick") return "iTick 行情 API 可用";
    if (channel.id === "x-twtapi") return "X/TwtAPI 检索可用";
    if (channel.collection_mode === "playwright") return "登录态可用";
    if (channel.id === "web-rumors") return "授权会话可用";
    return "公开采集可用";
  }
  if (channel.status === "offline") {
    if (channel.id === "wechat-mp-rss") return "微信公众号组件不可用，请重新登录或检查高级配置";
    if (channel.id === "ima-knowledge") return "IMA 凭证或知识库不可用，请检查渠道配置";
    if (channel.id === "itick") return "iTick 凭证或 API 不可用，请检查渠道配置";
    if (channel.id === "x-twtapi") return "TwtAPI API Key 或接口不可用，请检查渠道配置";
    if (channel.collection_mode === "playwright") return "登录态失效，请重新登录";
    if (channel.id === "web-rumors") return "授权会话失效，请重新导入 HAR";
    return "公开采集暂不可用";
  }
  return channel.collection_mode === "playwright" ? "等待登录配置" : "等待渠道配置";
}
function jobChannelNames(job) {
  return job.channel_names?.length ? job.channel_names : (job.channel_ids || []).map(channelDisplayName);
}
function sourceWeightItem(channelId) {
  return (data.source_weights?.weights || []).find((item) => item.channel_id === channelId);
}
function sourceWeightValue(channelId) {
  return sourceWeightItem(channelId)?.weight ?? 0;
}
function setSourceWeight(channelId, value) {
  const item = sourceWeightItem(channelId);
  if (!item) return;
  const weight = Number(value);
  item.weight = Number.isFinite(weight) ? Math.max(0, Math.min(100, Math.round(weight * 100) / 100)) : 0;
}
function resetSourceWeightsEvenly() {
  const channels = data.channels || [];
  if (!channels.length) return;
  const basisPoints = 10000;
  const base = Math.floor(basisPoints / channels.length);
  const remainder = basisPoints % channels.length;
  data.source_weights = {
    configured: false,
    updated_at: "",
    total_weight: 100,
    weights: channels.map((channel, index) => ({
      channel_id: channel.id,
      name: channelDisplayName(channel),
      weight: Number(((base + (index < remainder ? 1 : 0)) / 100).toFixed(2)),
    })),
  };
}
function sourceRunSummary(job) {
  return (job.runs || [])
    .map((run) => `${channelDisplayName(run.channel_id)}：${sourceJobStatusLabel(run)}`)
    .join(" · ");
}
const sourceActionOptions = [
  { value: "collect", label: "仅采集数据", hint: "保存原始快照，按渠道策略整理，不生成分析报告" },
  { value: "collect_report", label: "采集并生成报告", hint: "采集完成后交给 AI 分析" },
  { value: "report", label: "仅生成报告", hint: "只分析已有本地快照" },
];
const sourceJobSubmitLabel = computed(() => {
  if (sourceJobSubmitting.value) {
    return sourceJob.action === "report" ? "正在生成 HTML 报告..." : "正在提交任务...";
  }
  return {
    collect: "发起采集任务",
    collect_report: "发起采集并生成报告",
    report: "立即生成报告",
  }[sourceJob.action];
});
const reportPreviewDocument = computed(() => buildReportPreviewDocument(selectedReport.value));

watch(() => sourceJob.lookback_days, (days, previousDays) => {
  if (Number.isInteger(days) && sourceJob.report_title === `近 ${previousDays} 天信源聚合报告`) {
    sourceJob.report_title = `近 ${days} 天信源聚合报告`;
  }
});

watch(activePage, (page, previousPage) => {
  frontendLog("info", "navigation.changed", "", { from: previousPage, to: page });
  if (page === "audit") void loadDiagnosticLogs();
});

function clientRequestId() {
  return globalThis.crypto?.randomUUID?.().replaceAll("-", "").slice(0, 12) || `${Date.now()}${Math.random()}`.replace(".", "").slice(-12);
}

const beijingTimeFormatter = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hourCycle: "h23",
  hour12: false,
});

function formatBeijingTime(value) {
  if (!value) return "";
  if (value instanceof Date) {
    if (Number.isNaN(value.getTime())) return "";
    const parts = Object.fromEntries(beijingTimeFormatter.formatToParts(value).map((part) => [part.type, part.value]));
    return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`;
  }
  const text = String(value).trim();
  const normalized = /^\d{4}-\d{2}-\d{2} \d{2}:/.test(text) ? text.replace(" ", "T") : text;
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return text;
  const parts = Object.fromEntries(beijingTimeFormatter.formatToParts(date).map((part) => [part.type, part.value]));
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`;
}

const inlineTimestampPattern = /\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:?\d{2})?\b/g;

function localizeInlineTimestamps(value) {
  return String(value || "").replace(inlineTimestampPattern, (match) => {
    const date = new Date(match.replace(" ", "T"));
    if (Number.isNaN(date.getTime())) return match;
    return `${formatBeijingTime(date)} 北京时间`;
  });
}

function localizeReportTimestamps(report) {
  return localizeInlineTimestamps(report);
}

function displayMessage(value) {
  return localizeInlineTimestamps(value);
}

function sanitizeClientContext(value, key = "") {
  const normalizedKey = key.toLowerCase().replaceAll("-", "_");
  if (/api.?key|authorization|cookie|password|secret|har.?text/i.test(normalizedKey) || normalizedKey === "token" || normalizedKey.endsWith("_token")) return "[REDACTED]";
  if (Array.isArray(value)) return value.map((item) => sanitizeClientContext(item));
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([itemKey, itemValue]) => [itemKey, sanitizeClientContext(itemValue, itemKey)]));
  if (typeof value === "string") return value.replace(/\bsk-[A-Za-z0-9_-]{10,}\b/gi, "[REDACTED]").slice(0, 4000);
  return value;
}

function frontendLog(level, event, message = "", context = {}) {
  frontendLogQueue.push({
    timestamp: new Date().toISOString(),
    level,
    event,
    message,
    context: sanitizeClientContext({ page: activePage.value, ...context }),
  });
  if (frontendLogQueue.length > 200) frontendLogQueue.splice(0, frontendLogQueue.length - 200);
  window.clearTimeout(frontendLogFlushTimer);
  frontendLogFlushTimer = window.setTimeout(() => void flushFrontendLogs(), 500);
}

async function flushFrontendLogs() {
  if (frontendLogFlushing || !frontendLogQueue.length) return;
  frontendLogFlushing = true;
  const entries = frontendLogQueue.splice(0, 40);
  try {
    await fetch(`${API}/api/diagnostics/frontend-logs`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Request-ID": clientRequestId() },
      body: JSON.stringify({ entries }),
    });
  } catch {
    frontendLogQueue.unshift(...entries);
  } finally {
    frontendLogFlushing = false;
    if (frontendLogQueue.length) frontendLogFlushTimer = window.setTimeout(() => void flushFrontendLogs(), 2000);
  }
}

function formatApiErrorDetail(detail) {
  if (Array.isArray(detail)) {
    return localizeInlineTimestamps(detail.map((item) => {
      if (!item || typeof item !== "object") return String(item);
      const location = Array.isArray(item.loc) ? item.loc.join(".") : "";
      const message = item.msg || item.message || JSON.stringify(item);
      return location ? `${location}: ${message}` : message;
    }).join("；"));
  }
  if (detail && typeof detail === "object") {
    const nested = detail.detail ?? detail.message ?? detail.error;
    if (nested && nested !== detail) return formatApiErrorDetail(nested);
    try {
      return localizeInlineTimestamps(JSON.stringify(detail));
    } catch {
      return localizeInlineTimestamps(String(detail));
    }
  }
  return localizeInlineTimestamps(String(detail || "请求失败"));
}

async function request(path, options = {}) {
  const requestId = clientRequestId();
  const method = options.method || "GET";
  const startedAt = performance.now();
  try {
    const headers = { "Content-Type": "application/json", "X-Request-ID": requestId, ...(options.headers || {}) };
    const response = await fetch(API + path, { ...options, headers });
    const responseText = await response.text();
    let body = {};
    try {
      body = responseText ? JSON.parse(responseText) : {};
    } catch {
      const plainText = responseText.replace(/\s+/g, " ").trim();
      const readableText = plainText && !/<(?:!doctype|html|head|body)\b/i.test(plainText) ? `：${plainText.slice(0, 240)}` : "";
      body = { detail: `服务返回 HTTP ${response.status}，但响应格式无法识别${readableText}` };
    }
    if (response.status === 413 && (!body.detail || body.detail.startsWith("服务返回 HTTP"))) {
      body.detail = "上传内容超过服务限制（HTTP 413），请精简文件后重试";
    }
    const correlatedId = response.headers.get("X-Request-ID") || requestId;
    if (!response.ok) {
      const detail = formatApiErrorDetail(body.detail ?? body.message ?? body.error);
      frontendLog("error", "api.request.failed", detail, { method, path, status_code: response.status, request_id: correlatedId, latency_ms: Math.round(performance.now() - startedAt) });
      throw new Error(`${detail} · 请求 ID ${correlatedId}`);
    }
    if (method !== "GET" && path !== "/api/diagnostics/frontend-logs") {
      frontendLog("info", "api.request.completed", "", { method, path, status_code: response.status, request_id: correlatedId, latency_ms: Math.round(performance.now() - startedAt) });
    }
    return body;
  } catch (error) {
    if (!String(error.message).includes("请求 ID")) {
      const message = localizeInlineTimestamps(error.message);
      frontendLog("error", "api.request.network_failed", message, { method, path, request_id: requestId, latency_ms: Math.round(performance.now() - startedAt) });
      throw new Error(`${message} · 请求 ID ${requestId}`);
    }
    throw error;
  }
}

async function loadDiagnosticLogs() {
  diagnosticLoading.value = true;
  try {
    const params = new URLSearchParams({ limit: "240" });
    if (diagnosticFilters.level) params.set("level", diagnosticFilters.level);
    if (diagnosticFilters.component) params.set("component", diagnosticFilters.component);
    if (diagnosticFilters.search) params.set("search", diagnosticFilters.search);
    const result = await request(`/api/diagnostics/logs?${params}`);
    diagnosticLogs.value = result.logs;
    Object.assign(diagnosticConfig, result.config);
  } catch (error) {
    notice.value = error.message;
  } finally {
    diagnosticLoading.value = false;
  }
}

function exportDiagnosticLogs() {
  frontendLog("info", "diagnostics.export.clicked");
  window.open(`${API}/api/diagnostics/logs/export`, "_blank", "noopener,noreferrer");
}

function formatDiagnosticFields(fields) {
  return JSON.stringify(fields || {}, null, 2);
}

async function refresh() {
  const [dashboard, audit] = await Promise.all([request("/api/dashboard"), request("/api/audit")]);
  Object.assign(data, dashboard, { audit });
}

async function saveSourceWeights() {
  if (!sourceWeightsValid.value) {
    notice.value = `信源权重总和必须等于 100%，当前为 ${sourceWeightTotal.value}%`;
    return;
  }
  sourceWeightsSaving.value = true;
  try {
    const weights = (data.source_weights?.weights || []).map((item) => ({ channel_id: item.channel_id, weight: Number(item.weight || 0) }));
    data.source_weights = await request("/api/settings/source-weights", { method: "PUT", body: JSON.stringify({ weights }) });
    notice.value = "信源分析权重已保存；采集任务仍按原规则执行";
    await refresh();
  } catch (error) {
    notice.value = error.message;
  } finally {
    sourceWeightsSaving.value = false;
  }
}

async function saveProvider() {
  frontendLog("info", "provider.save.clicked", "", { editing_provider_id: editingProviderId.value, protocol: provider.protocol, model: provider.model });
  saving.value = true;
  try {
    const payload = { ...provider, extra_body: JSON.parse(provider.extra_body_text || "{}") };
    delete payload.extra_body_text;
    const path = editingProviderId.value ? `/api/providers/${editingProviderId.value}` : "/api/providers";
    await request(path, { method: editingProviderId.value ? "PUT" : "POST", body: JSON.stringify(payload) });
    notice.value = "模型通道已加密保存";
    closeProviderModal();
    await refresh();
  } catch (error) { notice.value = error instanceof SyntaxError ? "额外参数必须是合法 JSON" : error.message; }
  saving.value = false;
}

async function testProvider(id) {
  frontendLog("info", "provider.health_check.clicked", "", { provider_id: id });
  notice.value = "正在测试模型通道...";
  try {
    const result = await request(`/api/providers/${id}/test`, { method: "POST" });
    notice.value = `${result.message} · ${result.latency_ms} ms`;
    await refresh();
  } catch (error) { notice.value = error.message; }
}

function resetProviderForm() {
  editingProviderId.value = "";
  Object.assign(provider, { name: "", base_url: "", model: "", api_key: "", protocol: "openai_chat_completions", enabled: true, extra_body_text: "{}" });
}

function editProvider(item) {
  editingProviderId.value = item.id;
  Object.assign(provider, { ...item, extra_body_text: JSON.stringify(item.extra_body || {}, null, 2) });
}

function openProviderModal(item = null) {
  if (item) editProvider(item);
  else resetProviderForm();
  providerModal.value = true;
}

function closeProviderModal() {
  providerModal.value = false;
  resetProviderForm();
}

async function activateProvider(id) {
  try {
    await request(`/api/providers/${id}/activate`, { method: "POST" });
    notice.value = "默认研究模型已切换";
    await refresh();
  } catch (error) { notice.value = error.message; }
}

async function toggleProvider(item) {
  try {
    const payload = { ...item, enabled: !item.enabled, extra_body: item.extra_body || {} };
    await request(`/api/providers/${item.id}`, { method: "PUT", body: JSON.stringify(payload) });
    notice.value = payload.enabled ? "模型通道已启用" : "模型通道已停用";
    await refresh();
  } catch (error) { notice.value = error.message; }
}

async function deleteProvider(item) {
  if (!window.confirm(`确认删除模型通道“${item.name}”吗？`)) return;
  try {
    await request(`/api/providers/${item.id}`, { method: "DELETE" });
    if (editingProviderId.value === item.id) closeProviderModal();
    notice.value = "模型通道已删除";
    await refresh();
  } catch (error) { notice.value = error.message; }
}

async function createTask() {
  if (!task.target.trim()) return notice.value = "请先填写股票代码或标的名称";
  if (!Number.isInteger(task.lookback_days) || task.lookback_days < 1 || task.lookback_days > 30) {
    return notice.value = "个股研究时间窗口必须是 1-30 之间的整数";
  }
  try {
    frontendLog("info", "research_task.create.clicked", "", { target: task.target, skill_name: task.skill_name, lookback_days: task.lookback_days });
    await request("/api/tasks", { method: "POST", body: JSON.stringify(task) });
    notice.value = "研究任务已进入队列";
    task.target = "";
    await refresh();
  } catch (error) { notice.value = error.message; }
}

async function createSourceJob() {
  if (sourceJobSubmitting.value) return;
  if (!sourceJob.channel_ids.length) return notice.value = "请至少选择一个信源渠道";
  if (!Number.isInteger(sourceJob.lookback_days) || sourceJob.lookback_days < 1 || sourceJob.lookback_days > 30) {
    return notice.value = "信源时间窗口必须是 1-30 之间的整数";
  }
  sourceJobSubmitting.value = true;
  sourceJobFeedbackType.value = "info";
  sourceJobFeedback.value = sourceJob.action === "report"
    ? "报告任务已提交，AI 正在生成 HTML 报告。请稍候，不需要重复点击。"
    : "任务正在提交，请稍候。";
  notice.value = sourceJobFeedback.value;
  try {
    frontendLog("info", "source_job.create.clicked", "", { action: sourceJob.action, channel_ids: sourceJob.channel_ids, lookback_days: sourceJob.lookback_days });
    const result = await request("/api/source-jobs", { method: "POST", body: JSON.stringify(sourceJob) });
    const message = result.deduplicated && result.action === "report"
      ? result.status === "generating_report"
        ? "相同参数的报告任务正在生成中，已阻止重复提交。"
        : "已复用最近生成的同参数报告，没有重复创建任务。"
      : result.status === "deduplicated"
        ? "所选信源已存在覆盖当前时间段的采集水位或排队任务，本次未重复采集。"
        : result.action === "report"
          ? "HTML 报告任务已进入队列，生成完成后可在任务列表中查看。"
          : "信源采集任务已按精确时间窗口进入队列。";
    notice.value = message;
    sourceJobFeedback.value = message;
    sourceJobFeedbackType.value = result.status === "deduplicated" || result.deduplicated ? "warn" : "success";
    if (result.report) openReport(result.report, result.report_title);
    await refresh();
  } catch (error) {
    notice.value = error.message;
    sourceJobFeedback.value = error.message;
    sourceJobFeedbackType.value = "error";
  } finally {
    sourceJobSubmitting.value = false;
  }
}

async function retrySourceJob(job) {
  notice.value = "正在重新排队失败的信源任务...";
  try {
    const result = await request(`/api/source-jobs/${job.id}/retry`, { method: "POST" });
    notice.value = result.status === "review" ? "报告已重新生成，等待审查" : "信源任务已重新进入采集队列";
    if (result.report) openReport(result.report, result.report_title || job.report_title);
    await refresh();
  } catch (error) { notice.value = error.message; }
}

async function openSnapshots(job) {
  try {
    const result = await request(`/api/source-jobs/${job.id}/snapshots`);
    Object.assign(snapshotDetail, result);
    snapshotModal.value = true;
  } catch (error) { notice.value = error.message; }
}

async function openJobReport(job) {
  try {
    const result = await request(`/api/source-jobs/${job.id}/report`);
    openReport(result.report, result.report_title || job.report_title);
  } catch (error) { notice.value = error.message; }
}

async function exportJobReport(job) {
  try {
    const result = await request(`/api/source-jobs/${job.id}/report`);
    downloadReportHtml(result.report, result.report_title || job.report_title);
  } catch (error) { notice.value = error.message; }
}

async function exportJobReportPdf(job) {
  try {
    const result = await request(`/api/source-jobs/${job.id}/report`);
    await downloadReportPdf(result.report, result.report_title || job.report_title);
  } catch (error) { notice.value = error.message; }
}

function closeSnapshots() {
  snapshotModal.value = false;
  Object.assign(snapshotDetail, { job: null, snapshots: [] });
}

async function openNormalizedItems({ channelId = "", snapshotId = "", title = "结构化条目" } = {}) {
  try {
    const params = new URLSearchParams();
    if (channelId) params.set("channel_id", channelId);
    if (snapshotId) params.set("snapshot_id", snapshotId);
    const result = await request(`/api/normalized-items?${params.toString()}`);
    Object.assign(normalizedDetail, { title, items: result.items });
    normalizedModal.value = true;
  } catch (error) { notice.value = error.message; }
}

function closeNormalizedItems() {
  normalizedModal.value = false;
  Object.assign(normalizedDetail, { title: "", items: [] });
}

async function normalizeSnapshot(item) {
  notice.value = "正在整理已有原始快照，不会访问远端信源...";
  try {
    const result = await request(`/api/snapshots/${item.id}/normalize`, { method: "POST" });
    notice.value = `整理完成：${result.stored_item_count} 条结构化内容，状态 ${result.status}`;
    if (snapshotDetail.job) await openSnapshots(snapshotDetail.job);
    await refresh();
  } catch (error) { notice.value = error.message; }
}

async function normalizeExistingChannel(channel) {
  notice.value = `正在整理 ${channelDisplayName(channel)} 的最近原始快照，不会重新采集...`;
  try {
    const result = await request(`/api/channels/${channel.id}/normalize-existing`, { method: "POST" });
    notice.value = `${channelDisplayName(channel)} 已整理 ${result.snapshot_count} 份原始快照`;
    await refresh();
  } catch (error) { notice.value = error.message; }
}

function snapshotPreviewText(item) {
  return item.content_preview || "";
}

function snapshotDownloadUrl(item) {
  return `${API}/api/snapshots/${encodeURIComponent(item.id)}/content`;
}

async function loadLongerSnapshotPreview(item) {
  snapshotPreviewLoadingId.value = item.id;
  try {
    Object.assign(item, await request(`/api/snapshots/${encodeURIComponent(item.id)}?preview_chars=200000`));
  } catch (error) {
    notice.value = error.message;
  } finally {
    snapshotPreviewLoadingId.value = "";
  }
}

function formatSnapshotContent(content) {
  try { return JSON.stringify(JSON.parse(content), null, 2); }
  catch { return content; }
}

function toggleSourceChannel(channelId) {
  const index = sourceJob.channel_ids.indexOf(channelId);
  if (index >= 0) sourceJob.channel_ids.splice(index, 1);
  else sourceJob.channel_ids.push(channelId);
}

async function runTask(id) {
  notice.value = "模型正在编排研究计划...";
  try {
    frontendLog("info", "research_task.analyze.clicked", "", { task_id: id });
    const result = await request(`/api/tasks/${id}/analyze`, { method: "POST" });
    const taskItem = data.tasks.find((item) => item.id === id);
    if (result.report) openReport(result.report, taskItem ? `${taskItem.target} - ${taskItem.title}` : "个股研究报告");
    notice.value = result.status === "review"
      ? "AI 研究报告已生成，等待审查"
      : `Agent 已请求 ${result.evidence_layer} 证据，采集器正在执行`;
    await refresh();
  } catch (error) { notice.value = error.message; }
}

function canRunTask(item) {
  return ["queued", "evidence_ready", "agent_failed"].includes(item.status);
}

function taskActionLabel(item) {
  if (item.status === "agent_failed") return "重试编排";
  if (item.status === "evidence_ready") return "继续分析";
  if (item.status === "evidence_queued" || item.status === "analyzing") return "处理中";
  return "启动编排";
}

function sourceJobActionLabel(action) {
  return {
    collect: "仅采集",
    collect_report: "采集并生成报告",
    report: "仅生成报告",
  }[action] || action;
}

function sourceJobSnapshotLabel(job) {
  return job.action === "report" ? "使用已有本地快照" : `${job.snapshot_count} 条新增快照`;
}

function sourceJobStatusLabel(job) {
  return {
    queued: "排队中",
    running: "正在采集",
    generating_report: "正在生成 HTML 报告",
    partial_completed: "部分信源采集完成",
    partial_review: "部分信源可用，报告待审查",
    partial_coverage: "时间窗口覆盖不完整",
    completed: job.action === "collect_report" ? "采集完成，报告待恢复" : "采集完成",
    deduplicated: job.action === "collect_report" ? "无需重复采集，报告待生成" : "已跳过重复采集",
    snapshot_deleted: "快照已删除",
    report_deleted: "报告已删除",
    review: "待审查",
    report_failed: "报告生成失败",
    failed: "采集失败",
    cancelled: "已取消",
  }[job.status] || job.status;
}

function canRetrySourceJob(job) {
  return ["failed", "report_failed", "cancelled"].includes(job.status)
    || (["completed", "deduplicated"].includes(job.status) && job.action === "collect_report")
    || (job.status === "report_deleted" && ["report", "collect_report"].includes(job.action));
}

function retrySourceJobLabel(job) {
  return (["completed", "deduplicated"].includes(job.status) && job.action === "collect_report")
    || (job.status === "report_deleted" && ["report", "collect_report"].includes(job.action))
    ? "生成报告"
    : "重试";
}

async function clearAuditInventory(scope) {
  if (inventoryCleanupSubmitting.value) return;
  const descriptions = {
    snapshots: "全部原始快照、结构化条目和采集水位",
    reports: "全部 HTML 报告",
    all: "全部快照、结构化条目、采集水位和 HTML 报告",
  };
  if (!window.confirm(`确认删除${descriptions[scope]}吗？任务流水和审计记录会保留，此操作不可撤销。`)) return;
  inventoryCleanupSubmitting.value = true;
  try {
    frontendLog("warning", "audit.inventory.clear.confirmed", "", { scope });
    const result = await request(`/api/audit/inventory/${scope}`, { method: "DELETE" });
    notice.value = `库存清理完成：删除 ${result.deleted.snapshot_count} 份快照、${result.deleted.normalized_item_count} 条结构化内容、${result.deleted.source_report_count + result.deleted.research_report_count} 份报告`;
    await refresh();
  } catch (error) {
    notice.value = error.message;
  } finally {
    inventoryCleanupSubmitting.value = false;
  }
}

async function clearTaskList(scope) {
  if (taskListCleanupSubmitting.value) return;
  const descriptions = {
    research: "全部个股研究任务",
    "source-jobs": "全部信源采集与报告任务",
  };
  if (!window.confirm(`确认永久删除${descriptions[scope]}吗？任务记录、内嵌报告和关联映射会直接删除，此操作不可撤销。原始快照库存不受影响。`)) return;
  taskListCleanupSubmitting.value = true;
  try {
    frontendLog("warning", "audit.task_list.clear.confirmed", "", { scope });
    const result = await request(`/api/task-lists/${scope}`, { method: "DELETE" });
    notice.value = `任务列表已清理：删除 ${result.deleted_research_tasks} 个研究任务、${result.deleted_source_jobs} 个信源任务`;
    await refresh();
  } catch (error) {
    notice.value = error.message;
  } finally {
    taskListCleanupSubmitting.value = false;
  }
}

async function resetTask(item) {
  if (!window.confirm(`确认重置“${item.target} · ${item.title}”吗？已有报告和 Agent 进度会清空。`)) return;
  try {
    const result = await request(`/api/tasks/${item.id}/reset`, { method: "POST" });
    closeReport();
    notice.value = result.cancelled_jobs
      ? `任务已重置，并取消 ${result.cancelled_jobs} 个尚未执行的采集任务`
      : "任务已重置，可以重新启动编排";
    await refresh();
  } catch (error) { notice.value = error.message; }
}

async function deleteTask(item) {
  if (!window.confirm(`确认永久删除“${item.target} · ${item.title}”吗？关联子任务、内嵌报告和映射会直接删除，原始快照库存不受影响。`)) return;
  try {
    const result = await request(`/api/tasks/${item.id}`, { method: "DELETE" });
    closeReport();
    notice.value = result.deleted_source_jobs
      ? `任务已删除，同时删除 ${result.deleted_source_jobs} 个关联信源任务`
      : "任务已删除";
    await refresh();
  } catch (error) { notice.value = error.message; }
}

function openChannelModal(channel = null) {
  if (!channel) {
    notice.value = "新增信源入口已关闭；信源请通过后端内置适配后发布。";
    return;
  }
  editingChannel.value = channel;
  Object.assign(channelForm, channel ? { ...channel, research_enabled: Boolean(channel.research_enabled), group_ids: [...(channel.group_ids || [])] } : { name: "", type: "", url: "", collection_mode: "playwright", status: "pending", notes: "", validation_url: "", success_url_contains: "", success_selector: "", group_ids: [], parsing_strategy: "hybrid", normalization_quality_threshold: 60, max_scrolls: 8, research_enabled: false });
  Object.assign(marketDataForm, channel?.market_data_config || { enable_akshare: true, enable_baostock: true, enable_tushare: true, tushare_token: "", tushare_token_configured: false, clear_tushare_token: false, component_timeout_seconds: 35 });
  marketDataForm.clear_tushare_token = false;
  Object.assign(imaForm, channel?.ima_config || { client_id: "", api_key: "", api_key_configured: false, skill_download_url: "https://app-dl.ima.qq.com/skills/ima-skills-1.1.7.zip" });
  imaForm.clear_credentials = false;
  const itickConfig = channel?.itick_config || { api_base: "https://api0.itick.org", api_key: "", api_key_configured: false, default_symbols: ["HK:700", "US:AAPL", "SH:600519"], kline_type: 2, kline_limit: 60, timeout_seconds: 20 };
  Object.assign(itickForm, { ...itickConfig, api_key: "", default_symbols_text: (itickConfig.default_symbols || ["HK:700", "US:AAPL", "SH:600519"]).join("\n"), clear_credentials: false });
  const xTwtApiConfig = channel?.x_twtapi_config || { api_base: "https://api.twtapi.com/api/v1/twitter", api_key: "", api_key_configured: false, default_queries: ["A股", "半导体", "光伏", "机器人"], tracked_users: [], result_type: "Latest", max_results: 20, timeout_seconds: 20, lang: "zh" };
  Object.assign(xTwtApiForm, { ...xTwtApiConfig, api_key: "", default_queries_text: (xTwtApiConfig.default_queries || ["A股", "半导体", "光伏", "机器人"]).join("\n"), tracked_users_text: (xTwtApiConfig.tracked_users || []).join("\n"), clear_credentials: false });
  const wechatRssConfig = channel?.wechat_rss_config || { base_url: "http://127.0.0.1:8001", feed_ids: ["all"], access_key: "", secret_key: "", credentials_configured: false, admin_username: "admin", admin_password: "", admin_password_configured: false, timeout_seconds: 20, max_items_per_feed: 100 };
  Object.assign(wechatRssForm, { ...wechatRssConfig, feed_ids_text: (wechatRssConfig.feed_ids || ["all"]).join("\n"), clear_credentials: false });
  channelModal.value = true;
  if (channel?.id === "wechat-mp-rss") void refreshWechatRssComponentStatus();
}

function closeChannelModal() {
  closeWechatRssLogin();
  channelModal.value = false;
  editingChannel.value = null;
  mxHarFile.value = null;
}

async function saveWechatRssConfiguration() {
  const { feed_ids_text, ...wechatRssConfig } = wechatRssForm;
  return request("/api/channels/wechat-mp-rss/config", {
    method: "PUT",
    body: JSON.stringify({ ...wechatRssConfig, feed_ids: feed_ids_text.split(/\r?\n|,|，/).map((item) => item.trim()).filter(Boolean) }),
  });
}

async function saveImaConfiguration() {
  return request("/api/channels/ima-knowledge/config", {
    method: "PUT",
    body: JSON.stringify(imaForm),
  });
}

async function saveItickConfiguration() {
  const { default_symbols_text, ...config } = itickForm;
  return request("/api/channels/itick/config", {
    method: "PUT",
    body: JSON.stringify({ ...config, default_symbols: default_symbols_text.split(/\r?\n|,|，|;|；/).map((item) => item.trim()).filter(Boolean) }),
  });
}

async function saveXTwtApiConfiguration() {
  const { default_queries_text, tracked_users_text, ...config } = xTwtApiForm;
  const splitLines = (text) => text.split(/\r?\n|,|，|;|；/).map((item) => item.trim()).filter(Boolean);
  return request("/api/channels/x-twtapi/config", {
    method: "PUT",
    body: JSON.stringify({ ...config, default_queries: splitLines(default_queries_text), tracked_users: splitLines(tracked_users_text) }),
  });
}

async function saveChannel(openLoginAfterSave = false, loginWindow = null) {
  if (!editingChannel.value) {
    notice.value = "新增信源入口已关闭；信源请通过后端内置适配后发布。";
    return;
  }
  if (!channelForm.name.trim() || !channelForm.type.trim()) {
    notice.value = "请填写渠道名称和渠道类型";
    return;
  }
  try {
    normalizeChannelForm();
    const path = editingChannel.value ? `/api/channels/${editingChannel.value.id}` : "/api/channels";
    const saved = await request(path, { method: editingChannel.value ? "PUT" : "POST", body: JSON.stringify(channelForm) });
    if (saved.id === "akshare") {
      await request("/api/channels/akshare/market-data-config", { method: "PUT", body: JSON.stringify(marketDataForm) });
    }
    if (saved.id === "wechat-mp-rss") {
      await saveWechatRssConfiguration();
    }
    if (saved.id === "ima-knowledge") {
      await saveImaConfiguration();
    }
    if (saved.id === "itick") {
      await saveItickConfiguration();
    }
    if (saved.id === "x-twtapi") {
      await saveXTwtApiConfiguration();
    }
    notice.value = editingChannel.value ? "渠道配置已更新" : "新渠道已添加";
    closeChannelModal();
    await refresh();
    if (openLoginAfterSave) {
      const result = await request(`/api/channels/${saved.id}/login`, { method: "POST" });
      notice.value = result.message;
      if (result.login_url) {
        if (loginWindow && !loginWindow.closed) loginWindow.location.href = result.login_url;
        else window.open(result.login_url, "_blank", "noopener,noreferrer");
      } else if (loginWindow && !loginWindow.closed) {
        loginWindow.close();
      }
    }
  } catch (error) {
    if (loginWindow && !loginWindow.closed) loginWindow.close();
    notice.value = error.message;
  }
}

async function deleteChannel() {
  if (!editingChannel.value || editingChannel.value.builtin) return;
  try {
    await request(`/api/channels/${editingChannel.value.id}`, { method: "DELETE" });
    notice.value = "渠道已删除";
    closeChannelModal();
    await refresh();
  } catch (error) { notice.value = error.message; }
}

async function openChannelLogin() {
  const loginWindow = window.open("", "_blank");
  await saveChannel(true, loginWindow);
}

async function checkChannel(channel) {
  notice.value = `正在检查 ${channelDisplayName(channel)} 登录状态...`;
  try {
    frontendLog("info", "channel.check.clicked", "", { channel_id: channel.id });
    const result = await request(`/api/channels/${channel.id}/check`, { method: "POST" });
    notice.value = `${channelDisplayName(channel)}: ${result.message}`;
    if (channel.id === "wechat-mp-rss") await refreshWechatRssComponentStatus();
    await refresh();
  } catch (error) { notice.value = error.message; }
}

async function checkAllChannels() {
  notice.value = "正在巡检已有信源状态...";
  try {
    const result = await request("/api/channels/check-all", { method: "POST" });
    notice.value = result.message;
    await refresh();
  } catch (error) { notice.value = error.message; }
}

function wechatRssConsoleUrl(path = "", baseUrlOverride = "") {
  const baseUrl = (baseUrlOverride || wechatRssComponent.management_url || "").replace(/\/+$/, "");
  if (!baseUrl) return "";
  return `${baseUrl}${path.startsWith("/") ? path : `/${path}`}`;
}

async function openWechatRssConsole(path = "", baseUrlOverride = "") {
  const url = wechatRssConsoleUrl(path, baseUrlOverride);
  if (!url) {
    notice.value = "WeRSS 原生管理台默认不对外暴露。如需排障，请使用运维 Compose override 临时启用本机管理端口。";
    return;
  }
  frontendLog("info", "channel.wechat_rss.console.opened", "", { path: path || "/" });
  try {
    window.open(url, "_blank", "noopener,noreferrer");
    notice.value = "已打开 WeRSS 原生管理台。这里仅用于高级排障和订阅维护；日常登录请使用 AlphaDesk 的扫码弹窗。";
  } catch (error) {
    notice.value = `打开 WeRSS 管理台失败：${error.message}`;
  }
}

async function refreshWechatRssComponentStatus() {
  wechatRssComponentLoading.value = true;
  try {
    Object.assign(wechatRssComponent, await request("/api/channels/wechat-mp-rss/component-status"));
  } catch (error) {
    Object.assign(wechatRssComponent, { status: "offline", message: error.message, service_online: false, rss_online: false, wechat_authorized: false, wechat_login_state: "failed", wechat_message: error.message });
  } finally {
    wechatRssComponentLoading.value = false;
  }
}

async function startWechatRssSidecar() {
  wechatRssStarting.value = true;
  notice.value = "正在启动本地 WeRSS 组件，首次使用可能需要拉取固定版本镜像...";
  try {
    Object.assign(wechatRssComponent, await request("/api/channels/wechat-mp-rss/start-sidecar", { method: "POST" }));
    notice.value = wechatRssComponent.service_online
      ? "WeRSS 已启动。点击“登录微信公众号”即可在 AlphaDesk 中扫码，并搜索加入公众号。"
      : "已提交 WeRSS 启动命令，但服务尚未就绪，请稍后重新检查。";
  } catch (error) {
    notice.value = error.message;
  } finally {
    wechatRssStarting.value = false;
  }
}

let wechatRssLoginPollTimer;
let wechatRssQrRefreshTimer;

function stopWechatRssLoginPolling() {
  window.clearInterval(wechatRssLoginPollTimer);
  wechatRssLoginPollTimer = undefined;
}

function stopWechatRssQrRefresh() {
  window.clearInterval(wechatRssQrRefreshTimer);
  wechatRssQrRefreshTimer = undefined;
}

function refreshWechatRssQrImage() {
  if (!wechatRssLogin.qr_base_url || wechatRssLogin.authorized || wechatRssLogin.qr_loaded) return;
  const separator = wechatRssLogin.qr_base_url.includes("?") ? "&" : "?";
  wechatRssLogin.qr_image_url = `${wechatRssLogin.qr_base_url}${separator}alphadesk=${Date.now()}`;
}

function markWechatRssQrLoaded() {
  wechatRssLogin.qr_loaded = true;
  stopWechatRssQrRefresh();
}

function closeWechatRssLogin() {
  stopWechatRssLoginPolling();
  stopWechatRssQrRefresh();
  wechatRssLoginModal.value = false;
}

async function syncWechatRssSubscriptions({ quiet = false } = {}) {
  try {
    const result = await request("/api/channels/wechat-mp-rss/subscriptions");
    Object.assign(wechatRssComponent, result);
    if (!quiet) {
      notice.value = result.ready
        ? `微信公众号信源可用，WeRSS 已加入 ${result.subscription_count} 个公众号`
        : "WeRSS 尚未加入公众号，请先扫码授权，再搜索并加入需要采集的公众号";
    }
    await refresh();
    return result;
  } catch (error) {
    if (!quiet) notice.value = `同步公众号失败：${error.message}`;
    throw error;
  }
}

async function searchWechatRssAccounts() {
  if (!wechatRssAuthorized.value) return notice.value = "微信授权无效，请先扫码登录微信公众号";
  wechatRssSearch.adding_panel_open = true;
  const query = wechatRssSearch.query.trim();
  if (!query) return notice.value = "请输入公众号名称或关键词";
  wechatRssSearch.loading = true;
  try {
    const result = await request(`/api/channels/wechat-mp-rss/subscriptions/search?q=${encodeURIComponent(query)}`);
    wechatRssSearch.items = result.items || [];
    notice.value = result.count ? `已找到 ${result.count} 个公众号候选` : "没有找到匹配的公众号，请换一个关键词";
  } catch (error) {
    notice.value = `搜索公众号失败：${error.message}`;
  } finally {
    wechatRssSearch.loading = false;
  }
}

function openWechatRssSubscriptionPanel() {
  if (!wechatRssAuthorized.value) {
    notice.value = "微信授权无效，请先扫码登录微信公众号";
    return;
  }
  wechatRssSearch.adding_panel_open = true;
}

async function addWechatRssSubscription(item) {
  wechatRssSearch.adding_id = item.id;
  try {
    const result = await request("/api/channels/wechat-mp-rss/subscriptions", {
      method: "POST",
      body: JSON.stringify({ id: item.id, name: item.name, avatar: item.avatar || "", intro: item.intro || "" }),
    });
    Object.assign(wechatRssComponent, {
      ready: Boolean(result.ready),
      subscriptions: result.subscriptions || [],
      subscription_count: result.subscription_count || 0,
      wechat_authorized: Boolean(result.wechat_authorized ?? wechatRssComponent.wechat_authorized),
      wechat_login_state: result.wechat_login_state || wechatRssComponent.wechat_login_state,
      wechat_message: result.wechat_message || wechatRssComponent.wechat_message,
    });
    wechatRssSearch.items = wechatRssSearch.items.filter((candidate) => candidate.id !== item.id);
    if (!wechatRssSearch.items.length) {
      wechatRssSearch.query = "";
      wechatRssSearch.adding_panel_open = false;
    }
    notice.value = `已加入公众号订阅：${item.name}`;
    await refresh();
  } catch (error) {
    notice.value = `加入公众号失败：${error.message}`;
  } finally {
    wechatRssSearch.adding_id = "";
  }
}

async function removeWechatRssSubscription(item) {
  if (!window.confirm(`确认移除公众号订阅“${item.name}”吗？后续任务将不再采集该公众号；已经保存的历史快照仍可在采集审计中清理。`)) return;
  wechatRssSearch.removing_id = item.id;
  try {
    const result = await request(`/api/channels/wechat-mp-rss/subscriptions/${encodeURIComponent(item.id)}`, { method: "DELETE" });
    Object.assign(wechatRssComponent, {
      ready: Boolean(result.ready),
      subscriptions: result.subscriptions || [],
      subscription_count: result.subscription_count || 0,
      wechat_authorized: Boolean(result.wechat_authorized ?? wechatRssComponent.wechat_authorized),
      wechat_login_state: result.wechat_login_state || wechatRssComponent.wechat_login_state,
      wechat_message: result.wechat_message || wechatRssComponent.wechat_message,
    });
    notice.value = `已移除公众号订阅：${item.name}`;
    await refresh();
  } catch (error) {
    notice.value = `移除公众号失败：${error.message}`;
  } finally {
    wechatRssSearch.removing_id = "";
  }
}

async function backfillWechatRssSubscriptions(item = null) {
  if (!wechatRssAuthorized.value) return notice.value = "微信授权无效，请先扫码登录微信公众号";
  const ids = item?.id
    ? [item.id]
    : (wechatRssComponent.subscriptions || []).filter((entry) => entry.enabled !== false).map((entry) => entry.id).filter(Boolean);
  if (!ids.length) return notice.value = "当前没有可补采的公众号订阅";
  if (item?.id) wechatRssSearch.backfilling_id = item.id;
  else wechatRssSearch.backfilling_all = true;
  const startPage = Math.min(Math.max(Number(wechatRssSearch.backfill_start_page || 0), 0), 100);
  const endPage = Math.min(Math.max(Number(wechatRssSearch.backfill_end_page || 1), 1), 100);
  wechatRssSearch.backfill_start_page = startPage;
  wechatRssSearch.backfill_end_page = endPage;
  try {
    const result = await request("/api/channels/wechat-mp-rss/subscriptions/backfill", {
      method: "POST",
      body: JSON.stringify({
        subscription_ids: ids,
        start_page: startPage,
        end_page: endPage,
      }),
    });
    Object.assign(wechatRssComponent, {
      ready: Boolean(result.ready),
      subscriptions: result.subscriptions || wechatRssComponent.subscriptions,
      subscription_count: result.subscription_count ?? wechatRssComponent.subscription_count,
      wechat_authorized: Boolean(result.wechat_authorized ?? wechatRssComponent.wechat_authorized),
      wechat_login_state: result.wechat_login_state || wechatRssComponent.wechat_login_state,
      wechat_message: result.wechat_message || wechatRssComponent.wechat_message,
    });
    notice.value = result.failed_count
      ? `已提交 ${result.submitted_count} 个公众号补采，${result.failed_count} 个失败；可稍后同步或查看 WeRSS 任务队列`
      : `已提交 ${result.submitted_count} 个公众号补采；可稍后同步公众号快照`;
  } catch (error) {
    notice.value = `公众号补采失败：${error.message}`;
  } finally {
    wechatRssSearch.backfilling_id = "";
    wechatRssSearch.backfilling_all = false;
  }
}

async function clearWechatRssTaskQueue(queueType = "main", clearHistory = false) {
  const queueLabel = queueType === "content" ? "内容补抓队列" : "文章采集队列";
  const targetLabel = clearHistory ? `${queueLabel}历史记录` : `${queueLabel}待执行任务`;
  if (!window.confirm(`确认清空 WeRSS ${targetLabel}吗？正在执行中的公众号任务不会被中断。`)) return;
  const clearingKey = `${queueType}:${clearHistory ? "history" : "queue"}`;
  wechatRssQueueClearing.value = clearingKey;
  try {
    const result = await request("/api/channels/wechat-mp-rss/task-queue/clear", {
      method: "POST",
      body: JSON.stringify({ queue_type: queueType, clear_history: clearHistory }),
    });
    Object.assign(wechatRssComponent, {
      ready: Boolean(result.ready),
      subscriptions: result.subscriptions || wechatRssComponent.subscriptions,
      subscription_count: result.subscription_count ?? wechatRssComponent.subscription_count,
      wechat_authorized: Boolean(result.wechat_authorized ?? wechatRssComponent.wechat_authorized),
      wechat_login_state: result.wechat_login_state || wechatRssComponent.wechat_login_state,
      wechat_message: result.wechat_message || wechatRssComponent.wechat_message,
    });
    notice.value = clearHistory ? `${result.message}，历史记录也已清空` : result.message;
  } catch (error) {
    notice.value = `WeRSS 队列清理失败：${error.message}`;
  } finally {
    wechatRssQueueClearing.value = "";
  }
}

async function pollWechatRssLoginStatus() {
  try {
    const result = await request("/api/channels/wechat-mp-rss/wechat-login/status");
    Object.assign(wechatRssLogin, result);
    if (result.authorized) {
      stopWechatRssLoginPolling();
      stopWechatRssQrRefresh();
      Object.assign(wechatRssComponent, {
        ready: Boolean(result.ready),
        subscriptions: result.subscriptions || [],
        subscription_count: result.subscription_count || 0,
        wechat_authorized: Boolean(result.wechat_authorized ?? result.authorized),
        wechat_login_state: result.wechat_login_state || result.login_state,
        wechat_message: result.wechat_message || result.message,
      });
      wechatRssLoginModal.value = false;
      notice.value = result.ready
        ? `微信扫码成功，已同步 ${result.subscription_count} 个公众号，信源可用`
        : "微信扫码成功。请在当前弹窗中搜索并加入需要采集的公众号。";
      await refresh();
    } else if (result.login_state === "expired") {
      stopWechatRssLoginPolling();
      stopWechatRssQrRefresh();
    }
  } catch (error) {
    stopWechatRssLoginPolling();
    stopWechatRssQrRefresh();
    wechatRssLogin.message = error.message;
    wechatRssLogin.login_state = "failed";
  }
}

async function beginWechatRssLogin() {
  wechatRssLoginModal.value = false;
  wechatRssLoginLoading.value = true;
  stopWechatRssLoginPolling();
  stopWechatRssQrRefresh();
  Object.assign(wechatRssLogin, { login_state: "starting", message: "正在准备微信扫码二维码...", qr_image_url: "", qr_base_url: "", qr_loaded: false, authorized: false });
  try {
    await saveWechatRssConfiguration();
    try {
      const current = await request("/api/channels/wechat-mp-rss/wechat-login/status");
      Object.assign(wechatRssLogin, current, { authorized: Boolean(current.authorized) });
      if (current.authorized) {
        Object.assign(wechatRssComponent, {
          ready: Boolean(current.ready),
          subscriptions: current.subscriptions || wechatRssComponent.subscriptions,
          subscription_count: current.subscription_count ?? wechatRssComponent.subscription_count,
          wechat_authorized: true,
          wechat_login_state: current.wechat_login_state || current.login_state,
          wechat_message: current.wechat_message || current.message,
        });
        await syncWechatRssSubscriptions({ quiet: true });
        notice.value = "微信授权仍然有效，可直接管理公众号订阅";
        return;
      }
    } catch {
      // If status probing fails, fall through and request a fresh QR code.
    }
    wechatRssLoginModal.value = true;
    const result = await request("/api/channels/wechat-mp-rss/wechat-login", { method: "POST" });
    Object.assign(wechatRssLogin, result, { qr_base_url: result.qr_image_url || "", qr_loaded: false, authorized: Boolean(result.authorized) });
    if (result.authorized) {
      wechatRssLoginModal.value = false;
      await syncWechatRssSubscriptions({ quiet: true });
      notice.value = "微信授权仍然有效，可直接管理公众号订阅";
      return;
    }
    refreshWechatRssQrImage();
    wechatRssQrRefreshTimer = window.setInterval(refreshWechatRssQrImage, 1200);
    wechatRssLoginPollTimer = window.setInterval(() => void pollWechatRssLoginStatus(), 3000);
  } catch (error) {
    Object.assign(wechatRssLogin, { login_state: "failed", message: error.message, qr_image_url: "", qr_base_url: "", qr_loaded: false, authorized: false });
    if (!wechatRssLoginModal.value) notice.value = error.message;
  } finally {
    wechatRssLoginLoading.value = false;
  }
}

function selectMxHar(event) {
  mxHarFile.value = event.target.files?.[0] || null;
}

async function importMxHar() {
  if (!editingChannel.value || editingChannel.value.id !== "web-rumors") return;
  if (!mxHarFile.value) return notice.value = "请先选择登录后导出的 MX HAR 文件";
  if (mxHarFile.value.size > 32 * 1024 * 1024) return notice.value = "HAR 文件过大，请只保留 MX 登录和消息请求";
  mxHarImporting.value = true;
  notice.value = "正在验证 MX HAR 并加密更新会话...";
  try {
    frontendLog("info", "channel.mx_har.import.clicked", "", { channel_id: editingChannel.value.id, file_size: mxHarFile.value.size });
    const result = await request(`/api/channels/${editingChannel.value.id}/import-mx-har`, {
      method: "POST",
      headers: { "Content-Type": "text/plain; charset=utf-8" },
      body: await mxHarFile.value.text(),
    });
    notice.value = `MX 会话已更新并通过验活，抽样读取 ${result.validated_snapshot_count} 条消息`;
    closeChannelModal();
    await refresh();
  } catch (error) {
    notice.value = `MX HAR 导入失败：${error.message}`;
  } finally {
    mxHarImporting.value = false;
  }
}

function addGroupId() {
  channelForm.group_ids.push("");
}

function removeGroupId(index) {
  channelForm.group_ids.splice(index, 1);
}

function normalizeChannelForm() {
  const groupMatch = channelForm.url.match(/\/group\/(\d+)/);
  if (groupMatch && !channelForm.group_ids.includes(groupMatch[1])) channelForm.group_ids.push(groupMatch[1]);
  channelForm.group_ids = channelForm.group_ids.map((id) => id.trim()).filter(Boolean);
  channelForm.normalization_quality_threshold = Number(channelForm.normalization_quality_threshold);
  channelForm.max_scrolls = Number(channelForm.max_scrolls);
}

function openReport(report, title = "AlphaDesk 报告") {
  frontendLog("info", "report.preview.opened", "", { title, report_chars: (report || "").length });
  selectedReport.value = report;
  selectedReportTitle.value = title || "AlphaDesk 报告";
  reportModal.value = true;
}

function closeReport() {
  reportModal.value = false;
  selectedReport.value = "";
  selectedReportTitle.value = "";
}

function escapeHtml(value) {
  return value.replace(/[&<>"']/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;",
  }[character]));
}

function buildReportPreviewDocument(report) {
  const text = localizeReportTimestamps(report || "").trim();
  const csp = `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; base-uri 'none'; form-action 'none';">`;
  if (/<html(?:\s|>)/i.test(text) && /<body(?:\s|>)/i.test(text)) {
    return /<head(?:\s|>)/i.test(text)
      ? text.replace(/<head([^>]*)>/i, `<head$1>${csp}`)
      : text.replace(/<html([^>]*)>/i, `<html$1><head>${csp}</head>`);
  }
  return `<!DOCTYPE html><html><head>${csp}<style>body{margin:0;padding:32px;background:#f8fafc;color:#1e293b;font-family:Inter,"Microsoft YaHei",sans-serif}.notice{margin-bottom:20px;padding:12px 14px;border:1px solid #f59e0b;border-radius:10px;background:#fffbeb;color:#92400e;font-size:13px}pre{white-space:pre-wrap;word-break:break-word;line-height:1.75;font-size:14px}</style></head><body><div class="notice">历史报告：生成于 HTML-only 红线启用之前，暂以纯文本兼容预览。重新生成后将使用 HTML 格式。</div><pre>${escapeHtml(text)}</pre></body></html>`;
}

function buildReportExportDocument(report) {
  const text = localizeReportTimestamps(report || "").trim();
  return /<html(?:\s|>)/i.test(text) && /<body(?:\s|>)/i.test(text)
    ? text
    : buildReportPreviewDocument(text);
}

function reportExportFilename(title = selectedReportTitle.value, extension = "html") {
  const stem = (title || "AlphaDesk 报告")
    .replace(/[<>:"/\\|?*\u0000-\u001f]/g, "_")
    .trim()
    .slice(0, 100) || "AlphaDesk 报告";
  const suffix = String(extension || "html").replace(/^\.+/, "") || "html";
  return `${stem}.${suffix}`;
}

function downloadReportHtml(report, title = "AlphaDesk 报告") {
  const filename = reportExportFilename(title, "html");
  const html = buildReportExportDocument(report);
  try {
    frontendLog("info", "report.export.clicked", "", { filename, report_chars: html.length });
    const url = URL.createObjectURL(new Blob([html], { type: "text/html;charset=utf-8" }));
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    notice.value = `HTML 报告已下载：${filename}`;
  } catch (error) {
    notice.value = `HTML 报告导出失败：${error.message}`;
  }
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function apiErrorMessageFromText(text, fallback = "请求失败") {
  try {
    const parsed = JSON.parse(text);
    return parsed.detail || parsed.message || fallback;
  } catch {
    return text?.trim()?.slice(0, 500) || fallback;
  }
}

async function downloadReportPdf(report, title = "AlphaDesk 报告") {
  if (!report?.trim()) {
    notice.value = "没有可导出的报告";
    return;
  }
  const filename = reportExportFilename(title, "pdf");
  const html = buildReportExportDocument(report);
  reportPdfExporting.value = true;
  try {
    frontendLog("info", "report.pdf_export.clicked", "", { filename, report_chars: html.length });
    const response = await fetch(`${API}/api/reports/export/pdf`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Request-ID": clientRequestId() },
      body: JSON.stringify({ title, html }),
    });
    if (!response.ok) {
      throw new Error(apiErrorMessageFromText(await response.text(), `PDF 导出失败：HTTP ${response.status}`));
    }
    downloadBlob(await response.blob(), filename);
    notice.value = `PDF 报告已下载：${filename}`;
  } catch (error) {
    notice.value = `PDF 报告导出失败：${error.message}`;
  } finally {
    reportPdfExporting.value = false;
  }
}

function exportReportHtml() {
  downloadReportHtml(selectedReport.value, selectedReportTitle.value);
}

async function exportReportPdf() {
  await downloadReportPdf(selectedReport.value, selectedReportTitle.value);
}

function exportTaskReport(item) {
  downloadReportHtml(item.report, `${item.target} - ${item.title}`);
}

async function exportTaskReportPdf(item) {
  await downloadReportPdf(item.report, `${item.target} - ${item.title}`);
}

function closeTopmostModal(event) {
  if (event.key !== "Escape" || event.defaultPrevented) return;
  if (wechatRssLoginModal.value) closeWechatRssLogin();
  else if (reportModal.value) closeReport();
  else if (normalizedModal.value) closeNormalizedItems();
  else if (snapshotModal.value) closeSnapshots();
  else if (channelModal.value) closeChannelModal();
  else if (providerModal.value) closeProviderModal();
  else return;
  event.preventDefault();
}

function captureWindowError(event) {
  frontendLog("error", "frontend.window.error", event.message || "Unknown window error", { filename: event.filename, lineno: event.lineno, colno: event.colno });
}

function captureUnhandledRejection(event) {
  frontendLog("error", "frontend.unhandled_rejection", event.reason?.message || String(event.reason || "Unknown rejection"));
}

let refreshTimer;
onMounted(async () => {
  window.addEventListener("keydown", closeTopmostModal);
  window.addEventListener("error", captureWindowError);
  window.addEventListener("unhandledrejection", captureUnhandledRejection);
  frontendLog("info", "frontend.mounted");
  await refresh();
  refreshTimer = setInterval(() => refresh().catch(() => {}), 5000);
});
onUnmounted(() => {
  frontendLog("info", "frontend.unmounted");
  void flushFrontendLogs();
  window.removeEventListener("keydown", closeTopmostModal);
  window.removeEventListener("error", captureWindowError);
  window.removeEventListener("unhandledrejection", captureUnhandledRejection);
  window.clearTimeout(frontendLogFlushTimer);
  clearInterval(refreshTimer);
  stopWechatRssLoginPolling();
  stopWechatRssQrRefresh();
});
</script>

<template>
  <div class="flex min-h-screen bg-[#080d16] text-slate-200">
    <aside class="fixed inset-y-0 left-0 z-20 flex w-64 flex-col border-r border-white/[.06] bg-[#0b111c]/95 px-3 py-5 backdrop-blur-2xl">
      <div class="flex items-center gap-3 px-3 pb-7">
        <div class="flex h-11 w-11 items-center justify-center rounded-2xl bg-gradient-to-br from-teal-300 to-cyan-500 text-lg font-bold text-slate-950 shadow-[0_0_28px_rgba(45,212,191,.22)]">A</div>
        <div>
          <p class="font-semibold tracking-wide text-white">AlphaDesk</p>
          <p class="mt-0.5 text-xs text-teal-300">A-Share Agent</p>
        </div>
      </div>

      <nav class="space-y-1">
        <button v-for="item in navItems" :key="item.id" @click="activePage=item.id" class="group flex w-full items-center gap-3 rounded-xl px-3 py-3 text-left transition" :class="activePage===item.id?'bg-white/[.08] text-white shadow-inner shadow-white/[.025]':'text-slate-500 hover:bg-white/[.035] hover:text-slate-300'">
          <svg class="h-5 w-5 shrink-0" :class="activePage===item.id?'text-teal-300':'text-slate-500 group-hover:text-slate-300'" fill="none" stroke="currentColor" stroke-width="1.7" viewBox="0 0 24 24">
            <path v-if="item.icon==='grid'" d="M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM14 14h6v6h-6z"/>
            <path v-else-if="item.icon==='file'" d="M6 3h9l3 3v15H6zM9 11h6M9 15h6"/>
            <path v-else-if="item.icon==='cpu'" d="M8 8h8v8H8zM9 1v3m6-3v3M9 20v3m6-3v3M20 9h3m-3 6h3M1 9h3m-3 6h3"/>
            <path v-else-if="item.icon==='radio'" d="M12 12h.01M8.5 8.5a5 5 0 0 0 0 7m7-7a5 5 0 0 1 0 7M5 5a10 10 0 0 0 0 14m14-14a10 10 0 0 1 0 14"/>
            <path v-else-if="item.icon==='audit'" d="M5 4h14v16H5zM8 8h8M8 12h8M8 16h5"/>
            <path v-else-if="item.icon==='spark'" d="m12 2 1.8 6.2L20 10l-6.2 1.8L12 18l-1.8-6.2L4 10l6.2-1.8zM19 17l.7 2.3L22 20l-2.3.7L19 23l-.7-2.3L16 20l2.3-.7z"/>
            <path v-else d="M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7zM19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.5v.1h-4v-.1a1.7 1.7 0 0 0-1-1.5 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.5-1H3v-4h.1a1.7 1.7 0 0 0 1.5-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1a1.7 1.7 0 0 0 1.9.3 1.7 1.7 0 0 0 1-1.5V3h4v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.5 1h.1v4h-.1a1.7 1.7 0 0 0-1.5 1z"/>
          </svg>
          <div>
            <p class="text-sm font-medium">{{ item.label }}</p>
            <p class="mt-0.5 text-[10px] uppercase tracking-[.14em] text-slate-600">{{ item.hint }}</p>
          </div>
        </button>
      </nav>

      <div class="mt-auto rounded-2xl border border-white/[.06] bg-white/[.025] p-3">
        <div class="flex items-center gap-2">
          <span class="h-2 w-2 rounded-full bg-emerald-400 shadow-[0_0_12px_#34d399]"></span>
          <p class="text-xs text-slate-300">本地服务运行中</p>
        </div>
        <p class="mt-2 text-[10px] tracking-wide text-slate-600">{{ webOrigin }}</p>
      </div>
    </aside>

    <main class="ml-64 min-h-screen flex-1">
      <header class="sticky top-0 z-10 flex h-[82px] items-center justify-between border-b border-white/[.06] bg-[#080d16]/80 px-8 backdrop-blur-2xl">
        <div>
          <h1 class="text-xl font-semibold text-white">{{ pageTitle }}</h1>
          <p class="mt-1 text-xs text-slate-500">{{ pageSubtitle }}</p>
        </div>
        <div class="flex items-center gap-3">
          <span class="rounded-full border border-teal-400/15 bg-teal-400/[.06] px-3 py-1.5 text-xs text-teal-200">Agent Ready</span>
          <div class="flex h-9 w-9 items-center justify-center rounded-full bg-gradient-to-br from-slate-700 to-slate-800 text-xs font-semibold text-white">AD</div>
        </div>
      </header>

      <div class="mx-auto max-w-[1480px] p-8">
        <div v-if="notice" class="mb-5 rounded-2xl border border-teal-400/20 bg-teal-400/[.08] px-4 py-3 text-sm text-teal-100">{{ displayMessage(notice) }}</div>

        <template v-if="activePage==='dashboard'">
          <section class="mb-5 grid grid-cols-4 gap-4">
            <div v-for="[label,value,detail] in [['工具就绪', readyTools + '/' + data.tools.length, '按采集优先级编排'],['待配置渠道', pendingChannels, '浏览器登录态渠道'],['已加载 Skills', data.skills.length, '领域能力可持续扩展'],['审查队列', data.tasks.filter(x=>x.status==='review').length, '等待人工复核']]" :key="label" class="panel p-5">
              <p class="text-sm text-slate-500">{{ label }}</p>
              <p class="mt-3 text-3xl font-semibold text-white">{{ value }}</p>
              <p class="mt-2 text-xs text-slate-600">{{ detail }}</p>
            </div>
          </section>

          <section class="grid grid-cols-[1.3fr_.7fr] gap-5">
            <div class="space-y-5">
              <div class="panel p-5">
                <div class="mb-4">
                  <h2 class="section-title">个股研究任务</h2>
                  <p class="mt-1 text-xs text-slate-500">实时交给大模型分析。本地端只聚合最近信源快照，不执行本地分析。</p>
                </div>
                <div class="grid grid-cols-[1fr_1.2fr] gap-3">
                  <input v-model="task.target" placeholder="股票代码 / 公司名称，例如 300308 中际旭创" class="field" />
                  <input v-model="task.title" class="field" />
                </div>
                <textarea v-model="task.objective" rows="3" class="field mt-3 w-full"></textarea>
                <div class="mt-3 grid grid-cols-2 gap-3">
                  <select v-model="task.skill_name" class="field">
                    <option v-for="skill in data.skills" :key="skill.name" :value="skill.name">{{ skill.name }}</option>
                  </select>
                  <label class="relative">
                    <input v-model.number="task.lookback_days" type="number" min="1" max="30" step="1" class="field pr-12" />
                    <span class="pointer-events-none absolute right-4 top-1/2 -translate-y-1/2 text-xs text-slate-500">天</span>
                  </label>
                </div>
                <button @click="createTask" class="primary mt-3">加入研究队列</button>
              </div>

              <div class="panel p-5">
                <div class="mb-4 flex items-center justify-between">
                  <h2 class="section-title">最近任务</h2>
                  <button @click="activePage='tasks'" class="text-xs text-teal-300 hover:text-teal-200">查看全部</button>
                </div>
                <div v-if="!data.tasks.length" class="empty">还没有研究任务</div>
                <div v-for="item in data.tasks.slice(0,4)" :key="item.id" class="list-row">
                  <div>
                    <p class="text-sm font-medium text-white">{{ item.target }} · {{ item.title }}</p>
                    <p class="mt-1 text-xs text-slate-600">{{ formatBeijingTime(item.created_at) }} · {{ item.status }}</p>
                  </div>
                  <div v-if="item.report" class="flex shrink-0 gap-2">
                    <button @click="openReport(item.report, `${item.target} - ${item.title}`)" class="report-action">查看报告</button>
                    <div class="group relative inline-flex shrink-0">
                      <button type="button" class="report-action">导出报告</button>
                      <div class="invisible absolute right-0 top-[calc(100%-1px)] z-30 min-w-32 rounded-xl border border-white/[.12] bg-[#101a2a] p-1 opacity-0 shadow-xl shadow-black/30 transition group-hover:visible group-hover:opacity-100 group-focus-within:visible group-focus-within:opacity-100">
                        <button type="button" @click="exportTaskReport(item)" class="block w-full rounded-lg px-3 py-2 text-left text-xs font-semibold text-slate-200 hover:bg-white/[.07]">导出 HTML</button>
                        <button type="button" @click="exportTaskReportPdf(item)" :disabled="reportPdfExporting" class="block w-full rounded-lg px-3 py-2 text-left text-xs font-semibold text-slate-200 hover:bg-white/[.07] disabled:cursor-wait disabled:opacity-60">{{ reportPdfExporting ? 'PDF 生成中' : '导出 PDF' }}</button>
                      </div>
                    </div>
                  </div>
                  <button v-else-if="canRunTask(item)" @click="runTask(item.id)" class="secondary">{{ taskActionLabel(item) }}</button>
                  <span v-else class="status-warn">{{ taskActionLabel(item) }}</span>
                </div>
              </div>
            </div>

            <div class="space-y-5">
              <div class="panel p-5">
                <div class="mb-4 flex items-center justify-between">
                  <h2 class="section-title">系统状态</h2>
                  <span class="status-good">运行中</span>
                </div>
                <div class="space-y-3">
                  <div class="metric"><span>模型供应商</span><strong :class="data.provider?.configured?'text-emerald-300':'text-amber-300'">{{ data.provider?.configured ? data.provider.name + ' · 已配置' : '待配置' }}</strong></div>
                  <div class="metric"><span>Browser 插件</span><strong>{{ data.codex_policy?.browser_enabled ? 'enabled' : 'disabled' }}</strong></div>
                  <div class="metric"><span>本地分析</span><strong class="text-emerald-300">{{ data.research_red_lines?.analysis?.local_analysis_enabled ? 'enabled' : 'disabled · AI only' }}</strong></div>
                  <div class="metric"><span>在线信源</span><strong>{{ data.channels.filter(x=>x.status==='online').length }}/{{ data.channels.length }}</strong></div>
                  <div class="metric"><span>Python 工具箱</span><strong>{{ data.codex_policy?.python_toolbox || '-' }}</strong></div>
                  <div class="metric"><span>默认推理模型</span><strong>{{ data.codex_policy?.model || '-' }}</strong></div>
                </div>
              </div>
              <div class="panel p-5">
                <h2 class="section-title mb-4">采集优先级</h2>
                <div v-for="tool in data.tools" :key="tool.id" class="mb-3 flex items-center gap-3 last:mb-0">
                  <div class="flex h-7 w-7 items-center justify-center rounded-lg bg-white/[.05] text-xs text-teal-300">{{ tool.priority }}</div>
                  <div class="min-w-0">
                    <p class="text-sm text-slate-200">{{ tool.name }}</p>
                    <p class="truncate text-xs text-slate-600">{{ tool.detail }}</p>
                  </div>
                </div>
              </div>
              <div class="panel p-5">
                <div class="mb-4 flex items-center justify-between">
                  <h2 class="section-title">信源状态</h2>
                  <button @click="activePage='channels'" class="text-xs text-teal-300 hover:text-teal-200">管理渠道</button>
                </div>
                <div v-for="channel in data.channels" :key="channel.id" class="mb-3 flex items-center justify-between text-xs last:mb-0">
                  <span class="text-slate-400">{{ channelDisplayName(channel) }}</span>
                  <span :class="channel.status==='online'?'status-good':'status-warn'">{{ channel.status }}</span>
                </div>
              </div>
            </div>
          </section>
        </template>

        <template v-else-if="activePage==='tasks'">
          <section class="panel p-5">
            <div class="mb-5 flex items-center justify-between">
              <h2 class="section-title">研究任务队列</h2>
              <div class="flex items-center gap-2">
                <button v-if="data.tasks.length" @click="clearTaskList('research')" :disabled="taskListCleanupSubmitting" class="rounded-xl border border-rose-400/35 bg-rose-400/[.08] px-3 py-2 text-xs font-semibold text-rose-200 transition hover:bg-rose-400/[.16] disabled:cursor-wait disabled:opacity-60">清空研究任务</button>
                <button @click="activePage='dashboard'" class="primary">新建研究任务</button>
              </div>
            </div>
            <div v-if="!data.tasks.length" class="empty">还没有研究任务</div>
            <div v-for="item in data.tasks" :key="item.id" class="list-row">
              <div>
                <p class="font-medium text-white">{{ item.target }} · {{ item.title }}</p>
                <p class="mt-1 text-xs text-slate-600">{{ formatBeijingTime(item.created_at) }} · {{ item.status }}</p>
                <p class="mt-2 text-xs text-slate-500">{{ item.objective }}</p>
                <p v-if="item.agent_error" class="mt-2 text-xs text-rose-300">{{ displayMessage(item.agent_error) }}</p>
              </div>
              <div class="flex shrink-0 items-center gap-2">
                <button v-if="item.report" @click="openReport(item.report, `${item.target} - ${item.title}`)" class="report-action">查看报告</button>
                <div v-if="item.report" class="group relative inline-flex shrink-0">
                  <button type="button" class="report-action">导出报告</button>
                  <div class="invisible absolute right-0 top-[calc(100%-1px)] z-30 min-w-32 rounded-xl border border-white/[.12] bg-[#101a2a] p-1 opacity-0 shadow-xl shadow-black/30 transition group-hover:visible group-hover:opacity-100 group-focus-within:visible group-focus-within:opacity-100">
                    <button type="button" @click="exportTaskReport(item)" class="block w-full rounded-lg px-3 py-2 text-left text-xs font-semibold text-slate-200 hover:bg-white/[.07]">导出 HTML</button>
                    <button type="button" @click="exportTaskReportPdf(item)" :disabled="reportPdfExporting" class="block w-full rounded-lg px-3 py-2 text-left text-xs font-semibold text-slate-200 hover:bg-white/[.07] disabled:cursor-wait disabled:opacity-60">{{ reportPdfExporting ? 'PDF 生成中' : '导出 PDF' }}</button>
                  </div>
                </div>
                <button v-if="canRunTask(item)" @click="runTask(item.id)" class="secondary">{{ taskActionLabel(item) }}</button>
                <span v-else-if="!item.report" class="status-warn">{{ taskActionLabel(item) }}</span>
                <button @click="resetTask(item)" class="secondary">重置</button>
                <button @click="deleteTask(item)" class="rounded-xl px-3 py-2 text-xs font-semibold text-rose-300 transition hover:bg-rose-400/10">删除</button>
              </div>
            </div>
          </section>
          <section class="panel mt-5 p-5">
            <div class="mb-4">
              <h2 class="section-title">信源数据采集与报告</h2>
              <p class="mt-1 text-xs text-slate-500">严格按信源时间戳创建采集窗口。仅生成报告不会触碰远端信源。</p>
            </div>
            <div class="mb-3 grid grid-cols-3 gap-2">
              <button v-for="option in sourceActionOptions" :key="option.value" @click="sourceJob.action=option.value" :disabled="sourceJobSubmitting" class="rounded-2xl border px-4 py-3 text-left transition disabled:cursor-wait disabled:opacity-60" :class="sourceJob.action===option.value?'border-teal-400/50 bg-teal-400/10 text-teal-100':'border-white/[.08] bg-black/10 text-slate-400 hover:border-white/[.16] hover:text-slate-200'">
                <span class="block text-sm font-semibold">{{ option.label }}</span>
                <span class="mt-1 block text-xs opacity-70">{{ option.hint }}</span>
              </button>
            </div>
            <div class="flex items-center gap-4">
              <label class="flex shrink-0 items-center gap-2">
                <span class="text-xs text-slate-500">窗口天数</span>
                <input v-model.number="sourceJob.lookback_days" :disabled="sourceJobSubmitting" type="number" min="1" max="30" step="1" class="field w-20 disabled:cursor-wait disabled:opacity-60" placeholder="1-30" />
                <span class="text-xs text-slate-500">天</span>
              </label>
              <div class="min-w-0 flex-1 rounded-xl border border-teal-400/15 bg-teal-400/[.05] px-4 py-2">
                <p class="text-xs font-semibold text-teal-200">独立信源聚合</p>
                <p class="mt-1 text-xs text-slate-500">仅使用通用信源快照，不继承个股研究目标、Skill 或临时补采证据。</p>
              </div>
            </div>
            <input v-model="sourceJob.report_title" :disabled="sourceJobSubmitting" class="field mt-3 w-full disabled:cursor-wait disabled:opacity-60" placeholder="报告标题" />
            <div class="mt-4 flex flex-wrap gap-2">
              <button v-for="channel in data.channels" :key="channel.id" @click="toggleSourceChannel(channel.id)" :disabled="sourceJobSubmitting" class="rounded-xl border px-3 py-2 text-xs transition disabled:cursor-wait disabled:opacity-60" :class="sourceJob.channel_ids.includes(channel.id)?'border-teal-400/50 bg-teal-400/10 text-teal-200':'border-white/[.08] text-slate-500 hover:text-slate-300'">
                {{ channelDisplayName(channel) }} · {{ channel.status }}
              </button>
            </div>
            <div class="mt-4 flex items-center gap-3">
              <button @click="createSourceJob" :disabled="sourceJobSubmitting" class="primary flex items-center gap-2">
                <span v-if="sourceJobSubmitting" class="h-3.5 w-3.5 animate-spin rounded-full border-2 border-teal-950/30 border-t-teal-950"></span>
                {{ sourceJobSubmitLabel }}
              </button>
              <p v-if="sourceJobSubmitting" class="text-xs text-teal-200">请求处理中，请勿重复点击</p>
            </div>
            <div v-if="sourceJobFeedback" class="mt-3 rounded-xl border px-3 py-2 text-xs leading-5" :class="sourceJobFeedbackType==='error'?'border-rose-400/25 bg-rose-400/[.08] text-rose-200':sourceJobFeedbackType==='warn'?'border-amber-400/25 bg-amber-400/[.08] text-amber-200':sourceJobFeedbackType==='success'?'border-emerald-400/25 bg-emerald-400/[.08] text-emerald-200':'border-teal-400/20 bg-teal-400/[.08] text-teal-100'">
              {{ displayMessage(sourceJobFeedback) }}
            </div>
          </section>
          <section class="panel mt-5 p-5">
            <div class="mb-4 flex items-center justify-between">
              <h2 class="section-title">信源采集与报告任务队列</h2>
              <button v-if="data.source_jobs.length" @click="clearTaskList('source-jobs')" :disabled="taskListCleanupSubmitting" class="rounded-xl border border-rose-400/35 bg-rose-400/[.08] px-3 py-2 text-xs font-semibold text-rose-200 transition hover:bg-rose-400/[.16] disabled:cursor-wait disabled:opacity-60">清空信源任务</button>
            </div>
            <div v-if="!data.source_jobs.length" class="empty">还没有信源采集任务</div>
            <div v-for="job in data.source_jobs" :key="job.id" class="list-row items-start">
              <div class="min-w-0">
                <p class="text-sm font-medium text-white">{{ job.report_title }}</p>
                <p class="mt-1 text-xs text-slate-600">{{ formatBeijingTime(job.created_at) }} · {{ sourceJobActionLabel(job.action) }} · {{ sourceJobStatusLabel(job) }} · {{ sourceJobSnapshotLabel(job) }}</p>
                <p class="mt-1 text-xs text-slate-600">信源：{{ jobChannelNames(job).join('、') }} · 窗口：{{ job.lookback_days }} 天</p>
                <p v-if="job.runs?.length" class="mt-1 text-xs text-slate-600">逐信源：{{ sourceRunSummary(job) }}</p>
                <p v-if="job.report_anchor" class="mt-1 text-xs text-slate-600">报告数据锚点：{{ formatBeijingTime(job.report_anchor) }}</p>
                <p v-if="job.error" class="mt-2 max-w-4xl break-all text-xs leading-5 text-rose-300">{{ displayMessage(job.error) }}</p>
              </div>
              <div class="flex shrink-0 gap-2">
                <button v-if="job.snapshot_count" @click="openSnapshots(job)" class="snapshot-action">查看快照</button>
                <button v-if="job.has_report || job.report" @click="openJobReport(job)" class="report-action">查看报告</button>
                <div v-if="job.has_report || job.report" class="group relative inline-flex shrink-0">
                  <button type="button" class="report-action">导出报告</button>
                  <div class="invisible absolute right-0 top-[calc(100%-1px)] z-30 min-w-32 rounded-xl border border-white/[.12] bg-[#101a2a] p-1 opacity-0 shadow-xl shadow-black/30 transition group-hover:visible group-hover:opacity-100 group-focus-within:visible group-focus-within:opacity-100">
                    <button type="button" @click="exportJobReport(job)" class="block w-full rounded-lg px-3 py-2 text-left text-xs font-semibold text-slate-200 hover:bg-white/[.07]">导出 HTML</button>
                    <button type="button" @click="exportJobReportPdf(job)" :disabled="reportPdfExporting" class="block w-full rounded-lg px-3 py-2 text-left text-xs font-semibold text-slate-200 hover:bg-white/[.07] disabled:cursor-wait disabled:opacity-60">{{ reportPdfExporting ? 'PDF 生成中' : '导出 PDF' }}</button>
                  </div>
                </div>
                <span v-if="job.status==='generating_report'" class="status-warn">报告生成中</span>
                <button v-if="canRetrySourceJob(job)" @click="retrySourceJob(job)" class="secondary">{{ retrySourceJobLabel(job) }}</button>
              </div>
            </div>
          </section>
        </template>

        <template v-else-if="activePage==='providers'">
          <section class="panel overflow-hidden">
            <div class="flex items-center justify-between border-b border-white/[.06] p-5">
              <div>
                <h2 class="section-title">模型供应商</h2>
                <p class="mt-1 text-xs text-slate-500">每个供应商独立维护模型、协议和加密 API Key。默认供应商用于 Agent 编排与报告分析。</p>
              </div>
              <button @click="openProviderModal()" class="primary">添加供应商</button>
            </div>
            <div v-if="!data.providers.length" class="empty m-5">尚未配置模型供应商</div>
            <div v-for="item in data.providers" :key="item.id" class="setting-row px-5 py-4">
              <span class="min-w-0">
                <span class="flex items-center gap-2">
                  <strong>{{ item.name }}</strong>
                  <b v-if="item.is_default" class="status-good">默认</b>
                  <b :class="item.status==='online'?'status-good':item.status==='failed'?'status-warn':'text-slate-600'" class="text-xs">{{ item.status }}</b>
                  <b v-if="item.latency_ms" class="text-xs text-amber-300">{{ item.latency_ms }} ms</b>
                </span>
                <small class="truncate">{{ item.base_url }} · {{ item.model }} · {{ item.protocol === 'openai_responses' ? 'Responses API' : 'Chat Completions' }}</small>
              </span>
              <div class="flex shrink-0 items-center gap-2">
                <button v-if="!item.is_default" @click="activateProvider(item.id)" class="secondary">设为默认</button>
                <button @click="testProvider(item.id)" class="secondary">测试</button>
                <button @click="openProviderModal(item)" class="secondary">配置</button>
                <button @click="deleteProvider(item)" class="rounded-xl px-3 py-2 text-xs font-semibold text-rose-300 transition hover:bg-rose-400/10">删除</button>
                <button @click="toggleProvider(item)" class="relative h-6 w-11 rounded-full transition" :class="item.enabled?'bg-teal-400':'bg-white/[.1]'">
                  <span class="absolute top-1 h-4 w-4 rounded-full bg-slate-950 transition" :class="item.enabled?'left-6':'left-1'"></span>
                </button>
              </div>
            </div>
          </section>
        </template>

        <template v-else-if="activePage==='channels'">
          <section class="panel mb-5 p-5">
            <div class="mb-4 flex items-center justify-between gap-4">
              <div>
                <h2 class="section-title">信源分析权重</h2>
                <p class="mt-1 text-xs text-slate-500">百分比只在大模型分析阶段生效；采集、去重、水位和原始快照保存不按权重处理。</p>
              </div>
              <div class="flex items-center gap-2">
                <span class="text-xs" :class="sourceWeightsValid ? 'text-emerald-300' : 'text-amber-300'">合计 {{ sourceWeightTotal }}%</span>
                <button @click="resetSourceWeightsEvenly" :disabled="sourceWeightsSaving" class="secondary disabled:cursor-wait disabled:opacity-60">均分</button>
                <button @click="saveSourceWeights" :disabled="sourceWeightsSaving || !sourceWeightsValid" class="primary disabled:cursor-wait disabled:opacity-60">{{ sourceWeightsSaving ? '保存中...' : '保存权重' }}</button>
              </div>
            </div>
            <div v-if="!(data.source_weights?.weights || []).length" class="empty">尚无可配置信源</div>
            <div v-else class="grid grid-cols-2 gap-3">
              <label v-for="item in data.source_weights.weights" :key="item.channel_id" class="flex items-center justify-between gap-3 rounded-xl border border-white/[.06] bg-black/10 px-4 py-3">
                <span class="min-w-0">
                  <strong class="block truncate text-sm text-slate-200">{{ channelDisplayName({ id: item.channel_id, name: item.name }) }}</strong>
                  <small class="text-slate-600">{{ item.channel_id }}</small>
                </span>
                <span class="flex shrink-0 items-center gap-2">
                  <input :value="sourceWeightValue(item.channel_id)" @input="setSourceWeight(item.channel_id, $event.target.value)" :disabled="sourceWeightsSaving" type="number" min="0" max="100" step="0.01" class="field w-24 text-right disabled:cursor-wait disabled:opacity-60" />
                  <span class="text-xs text-slate-500">%</span>
                </span>
              </label>
            </div>
          </section>
          <section class="panel overflow-hidden">
            <div class="flex items-center justify-between border-b border-white/[.06] p-5">
              <div>
                <h2 class="section-title">信源渠道配置</h2>
                <p class="mt-1 text-xs text-slate-500">公开数据优先，登录态和强反爬页面由持久化浏览器处理</p>
              </div>
              <div class="flex gap-2">
                <button @click="checkAllChannels" class="secondary">巡检状态</button>
              </div>
            </div>
            <div v-for="channel in data.channels" :key="channel.id" class="setting-row px-5 py-4">
              <span>
                <strong>{{ channelDisplayName(channel) }}</strong>
                <small>{{ channel.type }} · {{ channelStatusDescription(channel) }}</small>
                <small v-if="channel.group_ids?.length">星球 ID：{{ channel.group_ids.join('、') }}</small>
                <small v-if="channel.id==='akshare'">组件：AkShare · BaoStock · TuShare{{ channel.market_data_config?.tushare_token_configured ? '（token 已加密保存）' : '（等待 token）' }}</small>
                <small v-if="channel.id==='itick'">iTick 行情 API：配置中维护 API Base、API Key 和默认代码；API Key 仅本地加密保存</small>
                <small v-if="channel.id==='x-twtapi'">X/TwtAPI：配置中维护 API Base、API Key、默认搜索词和指定博主；个股补证会按标的实时搜索</small>
                <small v-if="channel.id==='wechat-mp-rss'">微信扫码登录后搜索并加入公众号；采集按严格时间窗读取文章快照</small>
                <small v-if="channel.id==='ima-knowledge'">在配置中维护 ClientID、API Key 和 IMA Skill 下载地址；API Key 仅本地加密保存</small>
                <small>整理策略：{{ channel.parsing_strategy }} · 质量阈值 {{ channel.normalization_quality_threshold }} · 最大滚动 {{ channel.max_scrolls }}</small>
                <small>个股补证：{{ channel.research_enabled ? '允许' : '关闭' }}</small>
                <small v-if="channel.last_check">上次检查：{{ formatBeijingTime(channel.last_check) }}</small>
              </span>
              <div class="flex items-center gap-3">
                <span :class="channel.status==='online'?'status-good':'status-warn'">{{ channel.status }}</span>
                <button @click="normalizeExistingChannel(channel)" class="secondary">整理已有快照</button>
                <button v-if="channel.collection_mode==='playwright' || ['web-rumors','akshare','industry-news','wechat-mp-rss','ima-knowledge','itick','x-twtapi'].includes(channel.id)" @click="checkChannel(channel)" class="secondary">检查状态</button>
                <button @click="openChannelModal(channel)" class="secondary">配置</button>
              </div>
            </div>
          </section>
          <section class="panel mt-5 p-5">
            <h2 class="section-title mb-4">采集工具注册表</h2>
            <div class="grid grid-cols-2 gap-3">
              <div v-for="tool in data.tools" :key="tool.id" class="rounded-2xl border border-white/[.06] bg-black/10 p-4">
                <div class="flex items-center justify-between">
                  <p class="font-medium text-slate-100">{{ tool.priority }}. {{ tool.name }}</p>
                  <span :class="tool.status==='ready'?'status-good':'status-warn'">{{ tool.status }}</span>
                </div>
                <p class="mt-2 text-xs leading-5 text-slate-500">{{ tool.detail }}</p>
              </div>
            </div>
          </section>
        </template>

        <template v-else-if="activePage==='audit'">
          <section class="panel mb-5 p-5">
            <div class="flex items-start justify-between gap-6">
              <div>
                <h2 class="section-title">库存管理</h2>
                <p class="mt-1 text-xs leading-5 text-slate-500">集中管理本地快照和 HTML 报告。清空快照会同步删除结构化内容并重置采集水位，任务流水和审计记录仍会保留。</p>
              </div>
              <div class="flex shrink-0 gap-2">
                <button @click="clearAuditInventory('snapshots')" :disabled="inventoryCleanupSubmitting" class="snapshot-action disabled:cursor-wait disabled:opacity-60">清空全部快照</button>
                <button @click="clearAuditInventory('reports')" :disabled="inventoryCleanupSubmitting" class="report-action disabled:cursor-wait disabled:opacity-60">清空全部报告</button>
                <button @click="clearAuditInventory('all')" :disabled="inventoryCleanupSubmitting" class="rounded-xl border border-rose-400/35 bg-rose-400/[.08] px-3 py-2 text-xs font-semibold text-rose-200 transition hover:bg-rose-400/[.16] disabled:cursor-wait disabled:opacity-60">清空快照与报告</button>
              </div>
            </div>
            <div class="mt-4 grid grid-cols-4 gap-3">
              <div class="rounded-xl border border-blue-400/15 bg-blue-400/[.05] px-4 py-3"><p class="text-xs text-slate-500">原始快照</p><p class="mt-1 text-xl font-semibold text-blue-200">{{ data.audit.inventory?.snapshot_count || 0 }}</p></div>
              <div class="rounded-xl border border-teal-400/15 bg-teal-400/[.05] px-4 py-3"><p class="text-xs text-slate-500">结构化内容</p><p class="mt-1 text-xl font-semibold text-teal-200">{{ data.audit.inventory?.normalized_item_count || 0 }}</p></div>
              <div class="rounded-xl border border-violet-400/15 bg-violet-400/[.05] px-4 py-3"><p class="text-xs text-slate-500">信源报告</p><p class="mt-1 text-xl font-semibold text-violet-200">{{ data.audit.inventory?.source_report_count || 0 }}</p></div>
              <div class="rounded-xl border border-fuchsia-400/15 bg-fuchsia-400/[.05] px-4 py-3"><p class="text-xs text-slate-500">个股研究报告</p><p class="mt-1 text-xl font-semibold text-fuchsia-200">{{ data.audit.inventory?.research_report_count || 0 }}</p></div>
            </div>
          </section>
          <section class="grid grid-cols-3 gap-5">
            <div class="panel p-5">
              <h2 class="section-title mb-4">信源水位</h2>
              <div v-if="!data.audit.watermarks.length" class="empty">尚无成功采集水位</div>
              <div v-for="item in data.audit.watermarks" :key="item.channel_id" class="list-row">
                <div>
                  <p class="text-sm font-medium text-white">{{ channelDisplayName(item) }}</p>
                  <p v-if="item.scope_key" class="mt-1 text-xs text-slate-600">范围：{{ item.scope_key }}</p>
                </div>
                <p class="text-xs text-slate-400">{{ formatBeijingTime(item.last_success_at) }}</p>
              </div>
            </div>
            <div class="panel p-5">
              <h2 class="section-title mb-4">快照库存</h2>
              <div v-if="!data.audit.snapshots.length" class="empty">尚无本地信源快照</div>
              <div v-for="item in data.audit.snapshots" :key="item.channel_id" class="list-row">
                <div>
                  <p class="text-sm font-medium text-white">{{ channelDisplayName(item) }}</p>
                  <p class="mt-1 text-xs text-slate-600">最近聚合：{{ formatBeijingTime(item.last_collected_at) }}</p>
                </div>
                <span class="status-good">{{ item.snapshot_count }} 条</span>
              </div>
            </div>
            <div class="panel p-5">
              <h2 class="section-title mb-4">结构化库存</h2>
              <div v-if="!data.audit.normalized.length" class="empty">尚无整理后的结构化条目</div>
              <div v-for="item in data.audit.normalized" :key="item.channel_id" class="list-row">
                <div>
                  <p class="text-sm font-medium text-white">{{ channelDisplayName(item) }}</p>
                  <p class="mt-1 text-xs text-slate-600">平均质量：{{ item.average_quality ?? '-' }} · {{ formatBeijingTime(item.last_normalized_at) }}</p>
                </div>
                <button @click="openNormalizedItems({ channelId: item.channel_id, title: `${channelDisplayName(item)} · 结构化条目` })" class="secondary">{{ item.item_count }} 条</button>
              </div>
            </div>
          </section>
          <section class="panel mt-5 p-5">
            <h2 class="section-title mb-4">采集任务流水</h2>
            <div v-if="!data.audit.jobs.length" class="empty">尚无采集任务</div>
            <div v-for="job in data.audit.jobs" :key="job.id" class="list-row items-start">
              <div class="min-w-0">
                <p class="text-sm font-medium text-white">{{ job.report_title }}</p>
                <p class="mt-1 text-xs text-slate-600">{{ formatBeijingTime(job.created_at) }} · {{ job.action }} · {{ sourceJobStatusLabel(job) }} · {{ job.snapshot_count }} 条快照</p>
                <p class="mt-1 text-xs text-slate-600">信源：{{ jobChannelNames(job).join('、') }}<span v-if="job.evidence_layer"> · Agent 层：{{ job.evidence_layer }}</span></p>
                <p v-if="job.runs?.length" class="mt-1 text-xs text-slate-600">逐信源：{{ sourceRunSummary(job) }}</p>
                <p v-if="job.error" class="mt-2 text-xs text-rose-300">{{ displayMessage(job.error) }}</p>
              </div>
              <div class="flex shrink-0 gap-2">
                <button v-if="job.snapshot_count" @click="openSnapshots(job)" class="snapshot-action">查看快照</button>
                <button v-if="job.has_report || job.report" @click="openJobReport(job)" class="report-action">查看报告</button>
                <div v-if="job.has_report || job.report" class="group relative inline-flex shrink-0">
                  <button type="button" class="report-action">导出报告</button>
                  <div class="invisible absolute right-0 top-[calc(100%-1px)] z-30 min-w-32 rounded-xl border border-white/[.12] bg-[#101a2a] p-1 opacity-0 shadow-xl shadow-black/30 transition group-hover:visible group-hover:opacity-100 group-focus-within:visible group-focus-within:opacity-100">
                    <button type="button" @click="exportJobReport(job)" class="block w-full rounded-lg px-3 py-2 text-left text-xs font-semibold text-slate-200 hover:bg-white/[.07]">导出 HTML</button>
                    <button type="button" @click="exportJobReportPdf(job)" :disabled="reportPdfExporting" class="block w-full rounded-lg px-3 py-2 text-left text-xs font-semibold text-slate-200 hover:bg-white/[.07] disabled:cursor-wait disabled:opacity-60">{{ reportPdfExporting ? 'PDF 生成中' : '导出 PDF' }}</button>
                  </div>
                </div>
                <span v-if="job.status==='generating_report'" class="status-warn">报告生成中</span>
                <button v-if="canRetrySourceJob(job)" @click="retrySourceJob(job)" class="secondary">{{ retrySourceJobLabel(job) }}</button>
              </div>
            </div>
          </section>
          <section class="panel mt-5 p-5">
            <h2 class="section-title mb-4">Agent 证据推进</h2>
            <div v-if="!data.audit.events.length" class="empty">尚无 Agent 证据事件</div>
            <div v-for="event in data.audit.events" :key="event.id" class="list-row">
              <div>
                <p class="text-sm font-medium text-white">{{ event.event_type }} · {{ event.task_id }}</p>
                <p class="mt-1 max-w-5xl break-all text-xs leading-5 text-slate-600">{{ displayMessage(event.detail) }}</p>
              </div>
              <p class="whitespace-nowrap text-xs text-slate-500">{{ formatBeijingTime(event.created_at) }}</p>
            </div>
          </section>
          <section class="panel mt-5 p-5">
            <div class="flex items-start justify-between gap-5">
              <div>
                <h2 class="section-title">运行诊断日志</h2>
                <p class="mt-1 text-xs leading-5 text-slate-500">后端、模型网关、采集 worker 和前端交互统一写入脱敏 JSONL。接口失败提示中的请求 ID 可直接用于检索。</p>
                <p class="mt-1 text-xs text-slate-600">滚动策略：单文件 {{ diagnosticConfig.max_file_mb || 8 }} MB，保留 {{ diagnosticConfig.backup_count || 12 }} 份 · {{ diagnosticConfig.directory || 'data/logs' }}</p>
              </div>
              <div class="flex shrink-0 gap-2">
                <button @click="loadDiagnosticLogs" :disabled="diagnosticLoading" class="secondary disabled:cursor-wait disabled:opacity-60">{{ diagnosticLoading ? '刷新中...' : '刷新日志' }}</button>
                <button @click="exportDiagnosticLogs" class="report-action">导出诊断包</button>
              </div>
            </div>
            <div class="mt-4 grid grid-cols-[.55fr_.8fr_1.65fr_auto] gap-3">
              <select v-model="diagnosticFilters.level" class="field">
                <option value="">全部级别</option>
                <option value="error">error</option>
                <option value="warning">warning</option>
                <option value="info">info</option>
              </select>
              <input v-model="diagnosticFilters.component" class="field" placeholder="组件，例如 model_gateway" />
              <input v-model="diagnosticFilters.search" class="field" placeholder="事件、任务 ID、信源 ID 或请求 ID" @keyup.enter="loadDiagnosticLogs" />
              <button @click="loadDiagnosticLogs" class="primary">筛选</button>
            </div>
            <div class="mt-4 space-y-2">
              <div v-if="!diagnosticLogs.length" class="empty">尚无匹配的运行日志</div>
              <article v-for="(entry,index) in diagnosticLogs" :key="`${entry.timestamp}-${index}`" class="rounded-xl border border-white/[.07] bg-black/15 px-4 py-3">
                <div class="flex flex-wrap items-center gap-2 text-xs">
                  <span class="rounded-full px-2 py-0.5 font-semibold uppercase" :class="entry.level==='error'?'bg-rose-400/10 text-rose-300':entry.level==='warning'?'bg-amber-400/10 text-amber-300':'bg-teal-400/10 text-teal-300'">{{ entry.level }}</span>
                  <strong class="text-slate-200">{{ entry.event }}</strong>
                  <span class="text-slate-500">{{ entry.component }}</span>
                  <span v-if="entry.request_id" class="font-mono text-blue-300">{{ entry.request_id }}</span>
                  <span class="ml-auto text-slate-600">{{ formatBeijingTime(entry.timestamp) }}</span>
                </div>
                <pre v-if="Object.keys(entry.fields || {}).length" class="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-black/20 p-3 text-[11px] leading-5 text-slate-500">{{ formatDiagnosticFields(entry.fields) }}</pre>
              </article>
            </div>
          </section>
        </template>

        <template v-else-if="activePage==='skills'">
          <section class="panel overflow-hidden">
            <div class="flex items-center justify-between border-b border-white/[.06] p-5">
              <div>
                <h2 class="section-title">已加载 Skills</h2>
                <p class="mt-1 text-xs text-slate-500">领域能力从项目 skills 目录加载，后续可持续扩展</p>
              </div>
              <button class="primary">导入 Skill</button>
            </div>
            <div v-for="skill in data.skills" :key="skill.path" class="setting-row px-5 py-4">
              <span>
                <strong>{{ skill.name }}</strong>
                <small>{{ skill.path }}</small>
              </span>
              <div class="flex items-center gap-3">
                <span class="status-good">{{ skill.status }}</span>
                <button class="secondary">查看详情</button>
              </div>
            </div>
          </section>
        </template>

        <template v-else>
          <section class="mb-5">
            <h2 class="mb-3 text-sm font-semibold text-rose-300">核心红线</h2>
            <div class="panel overflow-hidden border-rose-400/15">
              <div class="setting-row px-5 py-4"><span><strong>禁止本地分析</strong><small>本地程序仅执行聚合、去重、时间窗口控制和证据传递。所有分析必须交给大模型。</small></span><b class="text-emerald-300">{{ data.research_red_lines?.analysis?.local_analysis_enabled ? '未生效' : '已强制生效' }}</b></div>
              <div class="setting-row px-5 py-4"><span><strong>采集阶段 AI 仅允许整理</strong><small>原始快照先落库。模型仅可提取、拆分、去重、标注和评分，严禁在采集阶段分析，也严禁触发新的网络访问。</small></span><b class="text-emerald-300">{{ data.research_red_lines?.collection_normalization?.analysis_forbidden && data.research_red_lines?.collection_normalization?.preserve_raw_snapshot ? '已强制生效' : '未生效' }}</b></div>
              <div class="setting-row px-5 py-4"><span><strong>信源报告与个股研究隔离</strong><small>通用信源报告仅读取 general 快照，不继承股票代码、个股研究目标、Skill 或临时补采证据。个股研究仍可读取通用快照并按证据链补采。</small></span><b class="text-emerald-300">{{ data.research_red_lines?.workflow_isolation?.source_reports_use_general_snapshots_only && data.research_red_lines?.workflow_isolation?.source_reports_forbid_research_task_context ? '已强制生效' : '未生效' }}</b></div>
              <div class="setting-row px-5 py-4"><span><strong>个股分析前置全量刷新</strong><small>个股研究会读取全部渠道的历史快照和已有通用报告。任一在线自动信源超过 15 分钟未刷新时，会先按精确窗口补采全部在线信源，再继续模型分析。</small></span><b class="text-emerald-300">{{ data.research_red_lines?.workflow_isolation?.research_tasks_refresh_stale_general_sources ? '已强制生效' : '未生效' }}</b></div>
              <div class="setting-row px-5 py-4"><span><strong>模型知识库最后使用</strong><small>仅当外部证据链无法确认时允许使用，并强制标记为低置信推断。</small></span><b class="text-emerald-300">{{ data.research_red_lines?.analysis?.model_knowledge_last_resort ? '已强制生效' : '未生效' }}</b></div>
              <div class="setting-row px-5 py-4"><span><strong>报告必须使用 HTML</strong><small>模型必须生成完整 HTML 文档。严禁 Markdown；非 HTML 报告会被服务端拒绝保存。</small></span><b class="text-emerald-300">{{ data.research_red_lines?.report_output?.format === 'html' && !data.research_red_lines?.report_output?.markdown_allowed ? '已强制生效' : '未生效' }}</b></div>
              <div class="px-5 py-4">
                <strong class="block text-sm text-slate-200">固定证据升级顺序</strong>
                <div class="mt-3 flex flex-wrap items-center gap-2 text-xs">
                  <template v-for="(source,index) in data.research_red_lines?.evidence_escalation?.ordered_sources || []" :key="source">
                    <span class="rounded-lg border border-white/[.08] bg-black/10 px-3 py-2 text-slate-300">{{ source }}</span>
                    <span v-if="index < data.research_red_lines.evidence_escalation.ordered_sources.length-1" class="text-slate-600">→</span>
                  </template>
                </div>
              </div>
            </div>
          </section>
          <section class="mb-5">
            <div class="mb-3">
              <h2 class="text-sm font-semibold text-slate-500">开发环境信息（只读）</h2>
              <p class="mt-1 text-xs text-slate-600">来自项目配置，仅说明本地开发和工具环境，不参与研究 Agent 的供应商配置或分析决策。</p>
            </div>
            <div class="panel overflow-hidden">
              <div class="setting-row px-5 py-4"><span><strong>Codex 开发辅助模型</strong><small>仅用于开发本工作台，不是业务研究模型。研究模型请在“模型供应商”中配置。</small></span><b>{{ data.codex_policy?.model || '-' }}</b></div>
              <div class="setting-row px-5 py-4"><span><strong>Codex 推理强度</strong><small>仅影响开发辅助过程，不影响业务报告分析。</small></span><b>{{ data.codex_policy?.reasoning_effort || '-' }}</b></div>
              <div class="setting-row px-5 py-4"><span><strong>Browser 开发插件</strong><small>用于开发调试和页面验证；业务登录态采集由本地 Playwright 渠道配置控制。</small></span><b class="text-emerald-300">{{ data.codex_policy?.browser_enabled ? 'enabled' : 'disabled' }}</b></div>
              <div class="setting-row px-5 py-4"><span><strong>Python 工具箱</strong><small>辅助采集和结构化数据处理使用项目本地环境</small></span><b>{{ data.codex_policy?.python_toolbox || '-' }}</b></div>
              <div class="setting-row px-5 py-4"><span><strong>Windows sandbox</strong><small>开发辅助过程的本地执行偏好</small></span><b>{{ data.codex_policy?.sandbox_preference || '-' }}</b></div>
            </div>
          </section>
          <section>
            <h2 class="mb-3 text-sm font-semibold text-slate-500">关于</h2>
            <div class="panel overflow-hidden">
              <div class="setting-row px-5 py-4"><span><strong>应用版本</strong><small>AShareHunter</small></span><b>0.1.0</b></div>
              <div class="setting-row px-5 py-4"><span><strong>Web 服务</strong><small>Nginx 同源代理 FastAPI Agent 编排服务</small></span><b class="text-emerald-300">{{ webOrigin }}</b></div>
            </div>
          </section>
        </template>
      </div>
    </main>

    <div v-if="reportModal" class="fixed inset-0 z-[60] flex items-center justify-center bg-black/75 p-6 backdrop-blur-sm" @click.self="closeReport">
      <section class="flex max-h-[94vh] w-full max-w-6xl flex-col overflow-hidden rounded-3xl border border-white/[.12] bg-[#101a2a] shadow-2xl shadow-black/50">
        <header class="flex items-center justify-between border-b border-white/[.08] px-6 py-5">
          <div>
            <h2 class="text-lg font-semibold text-white">HTML 报告预览</h2>
            <p class="mt-1 text-xs text-slate-500">隔离预览 · 禁止脚本执行 · 新生成报告严禁 Markdown</p>
          </div>
          <div class="flex items-center gap-2">
            <div class="group relative inline-flex shrink-0">
              <button type="button" class="report-action">导出报告</button>
              <div class="invisible absolute right-0 top-[calc(100%-1px)] z-30 min-w-32 rounded-xl border border-white/[.12] bg-[#101a2a] p-1 opacity-0 shadow-xl shadow-black/30 transition group-hover:visible group-hover:opacity-100 group-focus-within:visible group-focus-within:opacity-100">
                <button type="button" @click="exportReportHtml" class="block w-full rounded-lg px-3 py-2 text-left text-xs font-semibold text-slate-200 hover:bg-white/[.07]">导出 HTML</button>
                <button type="button" @click="exportReportPdf" :disabled="reportPdfExporting" class="block w-full rounded-lg px-3 py-2 text-left text-xs font-semibold text-slate-200 hover:bg-white/[.07] disabled:cursor-wait disabled:opacity-60">{{ reportPdfExporting ? 'PDF 生成中' : '导出 PDF' }}</button>
              </div>
            </div>
            <button @click="closeReport" class="flex h-8 w-8 items-center justify-center rounded-full text-lg text-slate-500 transition hover:bg-white/[.06] hover:text-white">×</button>
          </div>
        </header>
        <div class="bg-slate-100 p-3">
          <iframe :srcdoc="reportPreviewDocument" sandbox="" referrerpolicy="no-referrer" title="HTML 报告预览" class="h-[78vh] w-full rounded-xl border-0 bg-white"></iframe>
        </div>
      </section>
    </div>

    <div v-if="snapshotModal" class="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-6 backdrop-blur-sm" @click.self="closeSnapshots">
      <section class="flex max-h-[90vh] w-full max-w-5xl flex-col overflow-hidden rounded-3xl border border-white/[.12] bg-[#101a2a] shadow-2xl shadow-black/40">
        <header class="flex items-center justify-between border-b border-white/[.08] px-6 py-5">
          <div>
            <h2 class="text-lg font-semibold text-white">信源快照内容</h2>
            <p class="mt-1 text-xs text-slate-500">{{ snapshotDetail.job?.report_title }} · {{ snapshotDetail.snapshots.length }} 条快照</p>
          </div>
          <button @click="closeSnapshots" class="flex h-8 w-8 items-center justify-center rounded-full text-lg text-slate-500 transition hover:bg-white/[.06] hover:text-white">×</button>
        </header>
        <div class="space-y-4 overflow-y-auto p-6">
          <div v-if="!snapshotDetail.snapshots.length" class="empty">该任务没有关联到可查看的本地快照</div>
          <article v-for="item in snapshotDetail.snapshots" :key="item.id" class="rounded-2xl border border-white/[.08] bg-black/15 p-4">
            <div class="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p class="text-sm font-semibold text-teal-200">{{ channelDisplayName({ id: item.channel_id, name: item.channel_name }) }}</p>
                <p class="mt-1 text-xs text-slate-500">发生时间：{{ formatBeijingTime(item.occurred_at) }} · 聚合时间：{{ formatBeijingTime(item.collected_at) }}</p>
                <p class="mt-1 text-xs text-slate-500">整理状态：{{ item.normalization_status }} · {{ item.normalized_item_count }} 条结构化内容</p>
              </div>
              <a :href="item.source_url" target="_blank" class="max-w-xl break-all text-right text-xs text-teal-300 hover:text-teal-200">{{ item.source_url }}</a>
            </div>
            <div class="mt-3 flex gap-2">
              <button @click="normalizeSnapshot(item)" class="secondary">重新整理</button>
              <button v-if="item.normalized_item_count" @click="openNormalizedItems({ snapshotId: item.id, title: `${channelDisplayName({ id: item.channel_id, name: item.channel_name })} · 结构化条目` })" class="secondary">查看结构化内容</button>
              <button v-if="item.content_truncated && item.content_preview.length < 200000" @click="loadLongerSnapshotPreview(item)" :disabled="snapshotPreviewLoadingId===item.id" class="secondary disabled:cursor-wait disabled:opacity-60">{{ snapshotPreviewLoadingId===item.id ? '加载中...' : '加载更多预览' }}</button>
              <a :href="snapshotDownloadUrl(item)" class="secondary">下载完整原文</a>
            </div>
            <p v-if="item.normalization_error" class="mt-3 break-all text-xs leading-5 text-amber-300">{{ displayMessage(item.normalization_error) }}</p>
            <p v-if="item.content_truncated" class="mt-3 text-xs text-amber-300">当前仅展示 {{ item.content_preview.length }} / {{ item.content_length }} 字符预览。完整内容请下载原文。</p>
            <pre class="mt-4 max-h-[440px] overflow-auto whitespace-pre-wrap break-words rounded-xl bg-black/25 p-4 text-xs leading-6 text-slate-300">{{ formatSnapshotContent(snapshotPreviewText(item)) }}</pre>
          </article>
        </div>
      </section>
    </div>

    <div v-if="normalizedModal" class="fixed inset-0 z-[55] flex items-center justify-center bg-black/70 p-6 backdrop-blur-sm" @click.self="closeNormalizedItems">
      <section class="flex max-h-[90vh] w-full max-w-5xl flex-col overflow-hidden rounded-3xl border border-white/[.12] bg-[#101a2a] shadow-2xl shadow-black/40">
        <header class="flex items-center justify-between border-b border-white/[.08] px-6 py-5">
          <div>
            <h2 class="text-lg font-semibold text-white">{{ normalizedDetail.title }}</h2>
            <p class="mt-1 text-xs text-slate-500">{{ normalizedDetail.items.length }} 条 · 仅展示字段提取和原文拆分结果，不包含本地分析</p>
          </div>
          <button @click="closeNormalizedItems" class="flex h-8 w-8 items-center justify-center rounded-full text-lg text-slate-500 transition hover:bg-white/[.06] hover:text-white">×</button>
        </header>
        <div class="space-y-4 overflow-y-auto p-6">
          <div v-if="!normalizedDetail.items.length" class="empty">尚无结构化条目</div>
          <article v-for="item in normalizedDetail.items" :key="item.id" class="rounded-2xl border border-white/[.08] bg-black/15 p-4">
            <div class="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p class="text-sm font-semibold text-teal-200">{{ item.title || channelDisplayName({ id: item.channel_id, name: item.channel_name }) }}</p>
                <p class="mt-1 text-xs text-slate-500">{{ formatBeijingTime(item.occurred_at) }} · {{ item.author || '未标注作者' }} · {{ item.normalization_mode }}</p>
              </div>
              <span class="status-good">质量 {{ item.quality_score }}</span>
            </div>
            <a v-if="item.source_url" :href="item.source_url" target="_blank" class="mt-3 block break-all text-xs text-teal-300 hover:text-teal-200">{{ item.source_url }}</a>
            <pre class="mt-4 max-h-[360px] overflow-auto whitespace-pre-wrap break-words rounded-xl bg-black/25 p-4 text-xs leading-6 text-slate-300">{{ item.content }}</pre>
          </article>
        </div>
      </section>
    </div>

    <div v-if="wechatRssLoginModal" class="fixed inset-0 z-[70] flex items-center justify-center bg-black/75 p-6 backdrop-blur-sm" @click.self="closeWechatRssLogin">
      <section class="flex max-h-[90vh] w-full max-w-2xl flex-col overflow-hidden rounded-3xl border border-white/[.12] bg-[#101a2a] shadow-2xl shadow-black/40">
        <header class="flex items-center justify-between border-b border-white/[.08] px-6 py-5">
          <div>
            <h2 class="text-lg font-semibold text-white">登录微信公众号</h2>
            <p class="mt-1 text-xs text-slate-500">微信扫码授权后，可在 AlphaDesk 内搜索并加入需要采集的公众号</p>
          </div>
          <button @click="closeWechatRssLogin" class="flex h-8 w-8 items-center justify-center rounded-full text-lg text-slate-500 transition hover:bg-white/[.06] hover:text-white">×</button>
        </header>
        <div class="overflow-y-auto p-6">
          <div class="flex min-h-64 items-center justify-center rounded-2xl border border-white/[.08] bg-white p-4">
            <img v-if="wechatRssLogin.qr_image_url && !wechatRssLogin.authorized" :src="wechatRssLogin.qr_image_url" alt="微信公众号登录二维码" class="h-56 w-56 object-contain" @load="markWechatRssQrLoaded" @error="wechatRssLogin.qr_loaded=false" />
            <div v-else-if="wechatRssLogin.authorized" class="text-center">
              <p class="text-base font-semibold text-emerald-600">微信扫码授权成功</p>
              <p class="mt-2 text-sm text-slate-500">WeRSS 已加入 {{ wechatRssComponent.subscription_count || 0 }} 个公众号</p>
            </div>
            <div v-else class="text-center">
              <p class="text-sm text-slate-500">{{ wechatRssLoginLoading ? '正在准备二维码...' : '二维码暂不可用' }}</p>
            </div>
          </div>
          <p class="mt-4 text-center text-sm" :class="wechatRssLogin.authorized ? 'text-emerald-300' : wechatRssLogin.login_state==='failed' || wechatRssLogin.login_state==='expired' ? 'text-amber-300' : 'text-slate-300'">{{ wechatRssLogin.message }}</p>
          <div v-if="false && wechatRssLogin.authorized" class="mt-4 rounded-xl border border-teal-400/20 bg-teal-400/[.04] p-3">
            <p class="text-xs leading-5 text-slate-400">WeRSS 无法枚举个人微信的全部关注列表。请搜索需要采集的公众号并加入订阅；已加入项会自动进入后续快照采集。</p>
            <div class="mt-3 flex gap-2">
              <input v-model="wechatRssSearch.query" @keyup.enter="searchWechatRssAccounts" placeholder="搜索公众号，例如 半导体、证券时报" class="field min-w-0 flex-1" />
              <button type="button" @click="searchWechatRssAccounts" :disabled="wechatRssSearch.loading" class="primary disabled:cursor-wait disabled:opacity-60">{{ wechatRssSearch.loading ? '搜索中...' : '搜索' }}</button>
            </div>
            <div v-if="wechatRssSearch.items.length" class="mt-3 max-h-48 space-y-2 overflow-y-auto">
              <div v-for="item in wechatRssSearch.items" :key="item.id" class="flex items-center justify-between gap-3 rounded-lg border border-white/[.07] bg-black/10 px-3 py-2">
                <div class="min-w-0">
                  <p class="truncate text-xs font-semibold text-slate-200">{{ item.name }}</p>
                  <p class="truncate text-[11px] text-slate-500">{{ item.alias || item.intro || '未提供简介' }}</p>
                </div>
                <button type="button" @click="addWechatRssSubscription(item)" :disabled="wechatRssSearch.adding_id===item.id" class="secondary shrink-0 disabled:cursor-wait disabled:opacity-60">{{ wechatRssSearch.adding_id===item.id ? '加入中...' : '加入订阅' }}</button>
              </div>
            </div>
          </div>
          <div v-if="false && wechatRssComponent.subscriptions?.length" class="mt-4 max-h-36 space-y-2 overflow-y-auto rounded-xl border border-white/[.07] bg-black/10 p-3">
            <div v-for="item in wechatRssComponent.subscriptions" :key="item.id" class="flex items-center justify-between gap-3 text-xs">
              <span class="truncate text-slate-200">{{ item.name }}</span>
              <div class="flex shrink-0 items-center gap-2">
                <span :class="item.enabled ? 'status-good' : 'status-warn'">{{ item.enabled ? '已启用' : '已停用' }}</span>
                <button type="button" @click="removeWechatRssSubscription(item)" :disabled="wechatRssSearch.removing_id===item.id" class="rounded-lg px-2 py-1 font-semibold text-rose-300 transition hover:bg-rose-400/10 disabled:cursor-wait disabled:opacity-60">{{ wechatRssSearch.removing_id===item.id ? '移除中...' : '移除' }}</button>
              </div>
            </div>
          </div>
          <div class="mt-4 flex justify-center gap-2">
            <button v-if="wechatRssLogin.login_state==='failed' || wechatRssLogin.login_state==='expired'" type="button" @click="beginWechatRssLogin" class="primary">重新获取二维码</button>
            <button v-if="wechatRssLogin.authorized" type="button" @click="syncWechatRssSubscriptions()" class="secondary">重新同步公众号</button>
            <button type="button" @click="closeWechatRssLogin" class="secondary">{{ wechatRssLogin.authorized ? '完成' : '取消' }}</button>
          </div>
        </div>
      </section>
    </div>

    <div v-if="providerModal" class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-6 backdrop-blur-sm" @click.self="closeProviderModal">
      <section class="flex max-h-[94vh] w-full max-w-xl flex-col overflow-hidden rounded-3xl border border-white/[.12] bg-[#101a2a] shadow-2xl shadow-black/40">
        <header class="flex items-center justify-between border-b border-white/[.08] px-6 py-5">
          <div>
            <h2 class="text-lg font-semibold text-white">{{ editingProviderId ? '配置模型供应商' : '添加模型供应商' }}</h2>
            <p class="mt-1 text-xs text-slate-500">独立维护当前供应商的模型服务地址、协议和加密凭据</p>
          </div>
          <button @click="closeProviderModal" class="flex h-8 w-8 items-center justify-center rounded-full text-lg text-slate-500 transition hover:bg-white/[.06] hover:text-white">×</button>
        </header>

        <div class="space-y-4 overflow-y-auto p-6">
          <label class="block"><span class="form-label">供应商名称</span><input v-model="provider.name" placeholder="例如 DeepSeek" class="field mt-2 w-full" /></label>
          <label class="block"><span class="form-label">Base URL</span><input v-model="provider.base_url" placeholder="https://api.deepseek.com" class="field mt-2 w-full" /></label>
          <label class="block"><span class="form-label">API Key（密文占位）</span><input v-model="provider.api_key" type="password" autocomplete="new-password" placeholder="输入新的 API Key" class="field mt-2 w-full" /></label>
          <label class="block"><span class="form-label">模型名称</span><input v-model="provider.model" placeholder="例如 deepseek-chat" class="field mt-2 w-full" /></label>
          <label class="block"><span class="form-label">协议</span>
            <select v-model="provider.protocol" class="field mt-2 w-full">
              <option value="openai_chat_completions">OpenAI Chat Completions</option>
              <option value="openai_responses">OpenAI Responses API</option>
            </select>
          </label>
          <label class="block"><span class="form-label">额外参数 JSON</span><textarea v-model="provider.extra_body_text" rows="4" class="field mt-2 w-full"></textarea></label>
          <label class="flex items-center gap-2 text-xs text-slate-400"><input v-model="provider.enabled" type="checkbox" /> 启用该模型通道</label>
          <p class="text-xs leading-5 text-slate-600">API Key 仅在本机加密保存，页面读取时始终显示密文占位。</p>
        </div>

        <footer class="flex justify-end gap-3 border-t border-white/[.08] px-6 py-4">
          <button @click="closeProviderModal" class="secondary">取消</button>
          <button @click="saveProvider" :disabled="saving" class="primary disabled:cursor-wait disabled:opacity-60">{{ saving ? '保存中...' : '加密保存' }}</button>
        </footer>
      </section>
    </div>

    <div v-if="channelModal" class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-6 backdrop-blur-sm" @click.self="closeChannelModal">
      <section class="flex max-h-[94vh] w-full max-w-2xl flex-col overflow-hidden rounded-3xl border border-white/[.12] bg-[#101a2a] shadow-2xl shadow-black/40">
        <header class="flex items-center justify-between border-b border-white/[.08] px-6 py-5">
          <div>
            <h2 class="text-lg font-semibold text-white">配置渠道</h2>
            <p class="mt-1 text-xs text-slate-500">保存渠道入口、采集方式和当前可用状态</p>
          </div>
          <button @click="closeChannelModal" class="flex h-8 w-8 items-center justify-center rounded-full text-lg text-slate-500 transition hover:bg-white/[.06] hover:text-white">×</button>
        </header>

        <div class="grid grid-cols-2 gap-4 overflow-y-auto p-6">
          <label>
            <span class="form-label">渠道名称</span>
            <input v-model="channelForm.name" placeholder="例如 知识星球" class="field mt-2 w-full" />
          </label>
          <label>
            <span class="form-label">渠道类型</span>
            <input v-model="channelForm.type" placeholder="例如 登录态信息差" class="field mt-2 w-full" />
          </label>
          <label class="col-span-2">
            <span class="form-label">入口 URL</span>
            <input v-model="channelForm.url" placeholder="https://...；个股补证入口可使用 {query}" class="field mt-2 w-full" />
            <small class="mt-1 block text-xs leading-5 text-slate-600">需要按股票动态检索时，可在 URL 中使用 <code>{query}</code>。HTTP requests 和 Playwright 都会在个股补证时替换为股票代码或名称。</small>
          </label>
          <label v-if="channelForm.collection_mode==='playwright'" class="col-span-2">
            <span class="form-label">登录态检查 URL</span>
            <input v-model="channelForm.validation_url" placeholder="留空时使用入口 URL" class="field mt-2 w-full" />
          </label>
          <label>
            <span class="form-label">采集方式</span>
            <select v-model="channelForm.collection_mode" class="field mt-2 w-full">
              <option value="akshare">AkShare 模块</option>
              <option value="itick_market_data">iTick 行情 API</option>
              <option value="x_twtapi">X / Twitter（TwtAPI）</option>
              <option value="industry_news">产业趋势公开资讯</option>
              <option value="wechat_rss">微信公众号 WeRSS RSS</option>
              <option value="ima_knowledge_base">IMA 知识库 OpenAPI</option>
              <option value="requests">HTTP requests</option>
              <option value="playwright">Playwright 持久化浏览器</option>
              <option value="manual">人工补充</option>
            </select>
          </label>
          <label>
            <span class="form-label">渠道状态</span>
            <select v-model="channelForm.status" class="field mt-2 w-full">
              <option value="online">可用 online</option>
              <option value="pending">待配置 pending</option>
              <option value="offline">离线 offline</option>
            </select>
          </label>
          <label>
            <span class="form-label">快照整理策略</span>
            <select v-model="channelForm.parsing_strategy" class="field mt-2 w-full">
              <option value="fixed">固定规则 fixed</option>
              <option value="hybrid">AI 整理失败时降级 hybrid</option>
              <option value="ai">仅 AI 整理 ai</option>
            </select>
          </label>
          <label>
            <span class="form-label">整理质量阈值</span>
            <input v-model.number="channelForm.normalization_quality_threshold" type="number" min="0" max="100" class="field mt-2 w-full" />
          </label>
          <div v-if="editingChannel?.id==='akshare'" class="col-span-2 rounded-2xl border border-teal-400/20 bg-teal-400/[.04] p-4">
            <span class="form-label">市场数据组件</span>
            <p class="mt-1 text-xs leading-5 text-slate-500">AkShare 优先，BaoStock 和 TuShare 作为补充来源。组件并行执行且独立限时，单个上游异常不会阻塞全部市场数据。</p>
            <div class="mt-3 grid grid-cols-3 gap-3">
              <label class="rounded-xl border border-white/[.07] bg-black/10 px-3 py-3 text-xs text-slate-300"><input v-model="marketDataForm.enable_akshare" type="checkbox" class="mr-2" />AkShare</label>
              <label class="rounded-xl border border-white/[.07] bg-black/10 px-3 py-3 text-xs text-slate-300"><input v-model="marketDataForm.enable_baostock" type="checkbox" class="mr-2" />BaoStock</label>
              <label class="rounded-xl border border-white/[.07] bg-black/10 px-3 py-3 text-xs text-slate-300"><input v-model="marketDataForm.enable_tushare" type="checkbox" class="mr-2" />TuShare</label>
            </div>
            <label class="mt-3 block">
              <span class="form-label">TuShare token</span>
              <input v-model="marketDataForm.tushare_token" type="password" :placeholder="marketDataForm.tushare_token_configured ? '已加密保存；保留密文即可' : '填写 TuShare token；仅在本机加密保存'" class="field mt-2 w-full" />
              <small class="mt-1 block text-xs leading-5 text-slate-600">接口只回显掩码，不会把 token 明文发送回页面。未配置 token 时，TuShare 自动跳过。</small>
            </label>
            <div class="mt-3 flex items-center gap-4">
              <label class="text-xs text-slate-400"><input v-model="marketDataForm.clear_tushare_token" type="checkbox" class="mr-2" />清除已保存 token</label>
              <label class="flex items-center gap-2 text-xs text-slate-400">单组件超时
                <input v-model.number="marketDataForm.component_timeout_seconds" type="number" min="5" max="120" class="field w-20" />
                秒
              </label>
            </div>
          </div>
          <div v-if="editingChannel?.id==='itick'" class="col-span-2 rounded-2xl border border-teal-400/20 bg-teal-400/[.04] p-4">
            <span class="form-label">iTick 行情 API</span>
            <p class="mt-1 text-xs leading-5 text-slate-500">按官方 REST 语义读取股票实时 quote 与 K 线。API Key 只在本机加密保存，页面接口只回传掩码。</p>
            <div class="mt-3 grid grid-cols-2 gap-3">
              <label>
                <span class="form-label">API Base</span>
                <input v-model="itickForm.api_base" autocomplete="off" placeholder="https://api0.itick.org" class="field mt-2 w-full" />
              </label>
              <label>
                <span class="form-label">API Key</span>
                <input v-model="itickForm.api_key" type="password" autocomplete="new-password" :placeholder="itickForm.api_key_configured ? '已加密保存；不填则保留原密钥' : 'iTick API Key'" class="field mt-2 w-full" />
              </label>
            </div>
            <label class="mt-3 block">
              <span class="form-label">默认代码</span>
              <textarea v-model="itickForm.default_symbols_text" rows="4" placeholder="HK:700&#10;US:AAPL&#10;SH:600519" class="field mt-2 w-full"></textarea>
              <small class="mt-1 block text-xs leading-5 text-slate-600">每行一个 <code>REGION:CODE</code>，例如港股 <code>HK:700</code>、美股 <code>US:AAPL</code>、A 股 <code>SH:600519</code>。个股研究时也会从查询词中自动识别 6 位 A 股代码。</small>
            </label>
            <div class="mt-3 grid grid-cols-3 gap-3">
              <label>
                <span class="form-label">K 线类型</span>
                <input v-model.number="itickForm.kline_type" type="number" min="1" max="10" class="field mt-2 w-full" />
              </label>
              <label>
                <span class="form-label">K 线条数</span>
                <input v-model.number="itickForm.kline_limit" type="number" min="1" max="300" class="field mt-2 w-full" />
              </label>
              <label>
                <span class="form-label">请求超时秒数</span>
                <input v-model.number="itickForm.timeout_seconds" type="number" min="3" max="60" class="field mt-2 w-full" />
              </label>
            </div>
            <div class="mt-3 flex flex-wrap items-center gap-4">
              <label class="text-xs text-slate-400"><input v-model="itickForm.clear_credentials" type="checkbox" class="mr-2" />清除已保存 API Key</label>
              <small class="text-xs leading-5 text-slate-600">保存后点击“检查状态”即可用第一条默认代码验证当前 iTick 凭证。</small>
            </div>
          </div>
          <div v-if="editingChannel?.id==='x-twtapi'" class="col-span-2 rounded-2xl border border-teal-400/20 bg-teal-400/[.04] p-4">
            <span class="form-label">X / Twitter（TwtAPI）</span>
            <p class="mt-1 text-xs leading-5 text-slate-500">按 TwtAPI 文档用 <code>X-API-Key</code> 调用 Search 和用户时间线。普通信源采集使用默认搜索词与指定博主，个股补证会用股票标的实时检索。</p>
            <div class="mt-3 grid grid-cols-2 gap-3">
              <label>
                <span class="form-label">API Base</span>
                <input v-model="xTwtApiForm.api_base" autocomplete="off" placeholder="https://api.twtapi.com/api/v1/twitter" class="field mt-2 w-full" />
              </label>
              <label>
                <span class="form-label">API Key</span>
                <input v-model="xTwtApiForm.api_key" type="password" autocomplete="new-password" :placeholder="xTwtApiForm.api_key_configured ? '已加密保存；不填则保留原密钥' : 'TwtAPI API Key'" class="field mt-2 w-full" />
              </label>
            </div>
            <label class="mt-3 block">
              <span class="form-label">默认搜索词</span>
              <textarea v-model="xTwtApiForm.default_queries_text" rows="4" placeholder="A股&#10;半导体&#10;光伏&#10;机器人" class="field mt-2 w-full"></textarea>
              <small class="mt-1 block text-xs leading-5 text-slate-600">每行一个 X 搜索表达式；个股研究时会优先使用任务标的，不受这里的默认搜索词限制。</small>
            </label>
            <label class="mt-3 block">
              <span class="form-label">指定博主</span>
              <textarea v-model="xTwtApiForm.tracked_users_text" rows="3" placeholder="https://x.com/aleabitoreddit&#10;或 @aleabitoreddit" class="field mt-2 w-full"></textarea>
              <small class="mt-1 block text-xs leading-5 text-slate-600">每行一个 X 用户名或主页 URL。采集器会先解析用户 ID，再读取该账号最新 tweets。</small>
            </label>
            <div class="mt-3 grid grid-cols-4 gap-3">
              <label>
                <span class="form-label">结果类型</span>
                <select v-model="xTwtApiForm.result_type" class="field mt-2 w-full">
                  <option value="Latest">Latest</option>
                  <option value="Top">Top</option>
                  <option value="User">User</option>
                  <option value="Image">Image</option>
                  <option value="Video">Video</option>
                </select>
              </label>
              <label>
                <span class="form-label">单词条数</span>
                <input v-model.number="xTwtApiForm.max_results" type="number" min="1" max="100" class="field mt-2 w-full" />
              </label>
              <label>
                <span class="form-label">语言</span>
                <input v-model="xTwtApiForm.lang" maxlength="10" placeholder="zh" class="field mt-2 w-full" />
              </label>
              <label>
                <span class="form-label">超时秒数</span>
                <input v-model.number="xTwtApiForm.timeout_seconds" type="number" min="3" max="60" class="field mt-2 w-full" />
              </label>
            </div>
            <div class="mt-3 flex flex-wrap items-center gap-4">
              <label class="text-xs text-slate-400"><input v-model="xTwtApiForm.clear_credentials" type="checkbox" class="mr-2" />清除已保存 API Key</label>
              <small class="text-xs leading-5 text-slate-600">保存后点击“检查状态”会用第一条默认搜索词调用 Search 验证凭证。</small>
            </div>
          </div>
          <div v-if="editingChannel?.id==='wechat-mp-rss'" class="col-span-2 rounded-2xl border border-teal-400/20 bg-teal-400/[.04] p-4">
            <div class="flex flex-wrap items-start justify-between gap-3">
              <div>
                <span class="form-label">微信公众号</span>
                <p class="mt-1 text-xs leading-5 text-slate-500">微信扫码一次，在 AlphaDesk 内搜索并加入需要采集的公众号。后续任务会按严格时间窗保存文章快照。</p>
              </div>
              <span :class="wechatRssAuthorized ? 'status-good' : 'status-warn'">{{ wechatRssComponentLoading ? '正在检查' : (wechatRssAuthorized ? '授权有效' : '等待登录') }}</span>
            </div>
            <div class="mt-4 grid grid-cols-4 gap-2">
              <div class="rounded-xl border border-white/[.07] bg-black/10 p-3"><p class="text-[11px] text-slate-500">组件</p><p class="mt-1 text-xs font-semibold" :class="wechatRssComponent.service_online?'text-emerald-300':'text-amber-300'">{{ wechatRssComponent.service_online ? '已连接' : '待启动' }}</p></div>
              <div class="rounded-xl border border-white/[.07] bg-black/10 p-3"><p class="text-[11px] text-slate-500">微信登录</p><p class="mt-1 text-xs font-semibold" :class="wechatRssAuthorized ? 'text-emerald-300' : 'text-slate-300'">{{ wechatRssAuthorized ? '已授权' : '等待扫码' }}</p></div>
              <div class="rounded-xl border border-white/[.07] bg-black/10 p-3"><p class="text-[11px] text-slate-500">已订阅公众号</p><p class="mt-1 text-xs font-semibold text-slate-200">{{ wechatRssComponent.subscription_count || 0 }} 个</p></div>
              <div class="rounded-xl border border-white/[.07] bg-black/10 p-3"><p class="text-[11px] text-slate-500">采集状态</p><p class="mt-1 text-xs font-semibold" :class="wechatRssComponent.ready?'text-emerald-300':'text-amber-300'">{{ wechatRssComponent.ready ? '可用' : '待配置' }}</p></div>
            </div>
            <div class="mt-3 flex flex-wrap gap-2">
              <button v-if="!wechatRssAuthorized" type="button" @click="beginWechatRssLogin" :disabled="wechatRssLoginLoading" class="primary disabled:cursor-wait disabled:opacity-60">{{ wechatRssLoginLoading ? '正在准备...' : '扫码登录微信公众号' }}</button>
              <button v-else type="button" @click="beginWechatRssLogin" :disabled="wechatRssLoginLoading" class="secondary disabled:cursor-wait disabled:opacity-60">{{ wechatRssLoginLoading ? '正在检查...' : '重新扫码授权' }}</button>
              <button v-if="wechatRssAuthorized" type="button" @click="openWechatRssSubscriptionPanel" class="primary">添加订阅</button>
              <button type="button" @click="syncWechatRssSubscriptions()" :disabled="wechatRssComponentLoading" class="secondary">{{ wechatRssComponentLoading ? '正在同步...' : '同步已订阅公众号' }}</button>
            </div>
            <p class="mt-3 text-xs leading-5" :class="wechatRssAuthorized ? 'text-emerald-300' : 'text-slate-500'">{{ wechatRssAuthorized ? `微信授权有效，WeRSS 已加入 ${wechatRssComponent.subscription_count || 0} 个公众号` : '微信授权无效或尚未登录，请扫码后再搜索并加入需要采集的公众号。' }}</p>
            <div v-if="false && wechatRssComponent.subscriptions?.length" class="mt-3 flex max-h-28 flex-wrap gap-2 overflow-y-auto">
              <span v-for="item in wechatRssComponent.subscriptions" :key="item.id" class="inline-flex items-center gap-1 rounded-full border border-teal-400/20 bg-teal-400/[.06] py-1 pl-3 pr-1 text-xs text-teal-100">
                {{ item.name }}
                <button type="button" @click="removeWechatRssSubscription(item)" :disabled="wechatRssSearch.removing_id===item.id" :title="`移除公众号订阅：${item.name}`" class="flex h-5 w-5 items-center justify-center rounded-full text-sm text-rose-300 transition hover:bg-rose-400/15 disabled:cursor-wait disabled:opacity-60">×</button>
              </span>
            </div>
            <div v-if="wechatRssAuthorized" class="mt-4 space-y-4 rounded-xl border border-teal-400/20 bg-teal-400/[.04] p-4">
              <div>
                <div class="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <p class="text-sm font-semibold text-teal-100">公众号订阅管理</p>
                    <p class="mt-1 text-xs leading-5 text-slate-500">授权有效时可直接搜索、加入订阅、移除订阅或触发文章补采，无需重新扫码。</p>
                  </div>
                  <div class="flex flex-wrap gap-2">
                    <button type="button" @click="openWechatRssSubscriptionPanel" class="primary">添加订阅</button>
                    <button type="button" @click="syncWechatRssSubscriptions()" :disabled="wechatRssComponentLoading" class="secondary disabled:cursor-wait disabled:opacity-60">{{ wechatRssComponentLoading ? '同步中...' : '刷新订阅状态' }}</button>
                  </div>
                </div>
                <div v-if="wechatRssSearch.adding_panel_open || wechatRssSearch.items.length" class="mt-3 rounded-xl border border-white/[.07] bg-black/10 p-3">
                  <div class="flex gap-2">
                    <input v-model="wechatRssSearch.query" @keyup.enter="searchWechatRssAccounts" placeholder="搜索公众号，例如 半导体、证券时报" class="field min-w-0 flex-1" />
                    <button type="button" @click="searchWechatRssAccounts" :disabled="wechatRssSearch.loading" class="primary disabled:cursor-wait disabled:opacity-60">{{ wechatRssSearch.loading ? '搜索中...' : '搜索' }}</button>
                    <button type="button" @click="wechatRssSearch.adding_panel_open=false; wechatRssSearch.items=[]; wechatRssSearch.query=''" class="secondary">收起</button>
                  </div>
                  <p class="mt-2 text-xs leading-5 text-slate-500">输入公众号名称或关键词，搜索后点击“加入订阅”。授权有效时不需要重新扫码。</p>
                  <div v-if="wechatRssSearch.items.length" class="mt-3 max-h-48 space-y-2 overflow-y-auto">
                    <div v-for="item in wechatRssSearch.items" :key="item.id" class="flex items-center justify-between gap-3 rounded-lg border border-white/[.07] bg-black/10 px-3 py-2">
                      <div class="min-w-0">
                        <p class="truncate text-xs font-semibold text-slate-200">{{ item.name }}</p>
                        <p class="truncate text-[11px] text-slate-500">{{ item.alias || item.intro || '未提供简介' }}</p>
                      </div>
                      <button type="button" @click="addWechatRssSubscription(item)" :disabled="wechatRssSearch.adding_id===item.id" class="secondary shrink-0 disabled:cursor-wait disabled:opacity-60">{{ wechatRssSearch.adding_id===item.id ? '加入中...' : '加入订阅' }}</button>
                    </div>
                  </div>
                </div>
              </div>

              <div class="rounded-xl border border-white/[.07] bg-black/10 p-3">
                <div class="flex flex-wrap items-end justify-between gap-3">
                  <div>
                    <p class="text-sm font-semibold text-slate-200">公众号文章补采</p>
                    <p class="mt-1 text-xs leading-5 text-slate-500">调用 WeRSS 原生更新接口，适合发现某个公众号漏抓或需要补齐最近文章时手动重试。</p>
                  </div>
                  <div class="flex flex-wrap items-center gap-2 text-xs text-slate-400">
                    <label class="flex items-center gap-2">起始页
                      <input v-model.number="wechatRssSearch.backfill_start_page" type="number" min="0" max="100" class="field w-20" />
                    </label>
                    <label class="flex items-center gap-2">页数
                      <input v-model.number="wechatRssSearch.backfill_end_page" type="number" min="1" max="100" class="field w-20" />
                    </label>
                    <button type="button" @click="backfillWechatRssSubscriptions()" :disabled="wechatRssSearch.backfilling_all || !(wechatRssComponent.subscriptions || []).length" class="primary disabled:cursor-wait disabled:opacity-60">{{ wechatRssSearch.backfilling_all ? '提交中...' : '补采全部订阅' }}</button>
                  </div>
                </div>
                <div class="mt-3 flex flex-wrap items-center gap-2 border-t border-white/[.06] pt-3 text-xs text-slate-500">
                  <span>队列维护：清队列只移除待执行任务，不会中断当前正在抓取的公众号。</span>
                  <button type="button" @click="clearWechatRssTaskQueue('main', false)" :disabled="Boolean(wechatRssQueueClearing)" class="secondary disabled:cursor-wait disabled:opacity-60">{{ wechatRssQueueClearing==='main:queue' ? '清理中...' : '清文章队列' }}</button>
                  <button type="button" @click="clearWechatRssTaskQueue('main', true)" :disabled="Boolean(wechatRssQueueClearing)" class="secondary disabled:cursor-wait disabled:opacity-60">{{ wechatRssQueueClearing==='main:history' ? '清理中...' : '清文章历史' }}</button>
                  <button type="button" @click="clearWechatRssTaskQueue('content', false)" :disabled="Boolean(wechatRssQueueClearing)" class="secondary disabled:cursor-wait disabled:opacity-60">{{ wechatRssQueueClearing==='content:queue' ? '清理中...' : '清内容队列' }}</button>
                  <button type="button" @click="clearWechatRssTaskQueue('content', true)" :disabled="Boolean(wechatRssQueueClearing)" class="secondary disabled:cursor-wait disabled:opacity-60">{{ wechatRssQueueClearing==='content:history' ? '清理中...' : '清内容历史' }}</button>
                </div>
              </div>

              <div v-if="wechatRssComponent.subscriptions?.length" class="max-h-72 space-y-2 overflow-y-auto">
                <div v-for="item in wechatRssComponent.subscriptions" :key="item.id" class="flex items-center justify-between gap-3 rounded-xl border border-white/[.07] bg-black/10 px-3 py-2">
                  <div class="min-w-0">
                    <p class="truncate text-sm font-semibold text-slate-200">{{ item.name }}</p>
                    <p class="truncate text-[11px] text-slate-500">{{ item.id }}</p>
                  </div>
                  <div class="flex shrink-0 items-center gap-2">
                    <span :class="item.enabled ? 'status-good' : 'status-warn'">{{ item.enabled ? '已启用' : '已停用' }}</span>
                    <button type="button" @click="backfillWechatRssSubscriptions(item)" :disabled="wechatRssSearch.backfilling_id===item.id || wechatRssSearch.backfilling_all" class="secondary disabled:cursor-wait disabled:opacity-60">{{ wechatRssSearch.backfilling_id===item.id ? '提交中...' : '补采' }}</button>
                    <button type="button" @click="removeWechatRssSubscription(item)" :disabled="wechatRssSearch.removing_id===item.id" class="rounded-lg px-2 py-1 font-semibold text-rose-300 transition hover:bg-rose-400/10 disabled:cursor-wait disabled:opacity-60">{{ wechatRssSearch.removing_id===item.id ? '移除中...' : '移除' }}</button>
                  </div>
                </div>
              </div>
              <p v-else class="rounded-xl border border-dashed border-white/[.08] p-4 text-center text-xs text-slate-500">还没有已订阅公众号，可先搜索并加入订阅。</p>
            </div>
            <div v-else class="mt-4 rounded-xl border border-amber-400/20 bg-amber-400/[.05] p-4 text-xs leading-5 text-amber-100">
              微信授权无效或尚未完成扫码。只有在授权无效时才需要点击“扫码登录微信公众号”，授权有效后可直接在这里搜索、加入订阅和补采文章。
            </div>
            <details class="mt-3 rounded-xl border border-white/[.07] bg-black/10 p-3">
              <summary class="cursor-pointer text-xs font-semibold text-slate-300">高级配置与维护</summary>
              <div class="mt-3 flex flex-wrap gap-2">
                <button v-if="wechatRssComponent.managed_setup_available" type="button" @click="startWechatRssSidecar" :disabled="wechatRssStarting" class="secondary disabled:cursor-wait disabled:opacity-60">{{ wechatRssStarting ? '正在启动...' : '手工启动本地组件' }}</button>
                <button type="button" @click="refreshWechatRssComponentStatus" :disabled="wechatRssComponentLoading" class="secondary">{{ wechatRssComponentLoading ? '正在检查...' : '检查组件状态' }}</button>
                <button v-if="wechatRssComponent.management_url" type="button" @click="openWechatRssConsole()" class="secondary">打开原生管理台</button>
              </div>
              <p class="mt-2 text-xs leading-5 text-slate-500">{{ wechatRssComponent.message }}</p>
              <label class="mt-3 block">
                <span class="form-label">WeRSS 服务地址</span>
                <input v-model="wechatRssForm.base_url" placeholder="http://127.0.0.1:8001" class="field mt-2 w-full" />
              </label>
              <div class="mt-3 grid grid-cols-2 gap-3">
                <label><span class="form-label">管理账号</span><input v-model="wechatRssForm.admin_username" placeholder="admin" class="field mt-2 w-full" /></label>
                <label><span class="form-label">管理密码</span><input v-model="wechatRssForm.admin_password" type="password" :placeholder="wechatRssForm.admin_password_configured ? '已加密保存；无需重复填写' : 'WeRSS 管理密码'" class="field mt-2 w-full" /></label>
              </div>
              <label class="mt-3 block">
                <span class="form-label">Feed ID 列表</span>
                <textarea v-model="wechatRssForm.feed_ids_text" rows="3" placeholder="all&#10;或每行填写一个 Feed ID" class="field mt-2 w-full"></textarea>
                <small class="mt-1 block text-xs leading-5 text-slate-600">默认 <code>all</code> 读取 WeRSS 中全部已订阅公众号；仅在需要限制范围时填写具体 Feed ID。</small>
              </label>
              <div class="mt-3 grid grid-cols-2 gap-3">
                <label>
                  <span class="form-label">Access Key（可选）</span>
                  <input v-model="wechatRssForm.access_key" type="password" :placeholder="wechatRssForm.credentials_configured ? '已加密保存；保留密文即可' : 'WeRSS AK'" class="field mt-2 w-full" />
                </label>
                <label>
                  <span class="form-label">Secret Key（可选）</span>
                  <input v-model="wechatRssForm.secret_key" type="password" :placeholder="wechatRssForm.credentials_configured ? '已加密保存；保留密文即可' : 'WeRSS SK'" class="field mt-2 w-full" />
                </label>
              </div>
              <small class="mt-2 block text-xs leading-5 text-slate-600">AK/SK 必须同时填写或同时留空。凭据只在本机加密保存，页面接口只回显掩码。</small>
              <div class="mt-3 flex flex-wrap items-center gap-4">
                <label class="text-xs text-slate-400"><input v-model="wechatRssForm.clear_credentials" type="checkbox" class="mr-2" />清除已保存 AK/SK</label>
                <label class="flex items-center gap-2 text-xs text-slate-400">请求超时
                  <input v-model.number="wechatRssForm.timeout_seconds" type="number" min="3" max="120" class="field w-20" />
                  秒
                </label>
                <label class="flex items-center gap-2 text-xs text-slate-400">单 Feed 上限
                  <input v-model.number="wechatRssForm.max_items_per_feed" type="number" min="1" max="500" class="field w-20" />
                  条
                </label>
              </div>
            </details>
          </div>
          <div v-if="editingChannel?.id==='ima-knowledge'" class="col-span-2 rounded-2xl border border-teal-400/20 bg-teal-400/[.04] p-4">
            <span class="form-label">IMA 知识库 OpenAPI</span>
            <p class="mt-1 text-xs leading-5 text-slate-500">在这里维护 IMA 用户鉴权和 Skill 下载地址，不需要修改源码或 .env。API Key 仅在本地加密保存，接口只回传掩码。</p>
            <div class="mt-3 grid grid-cols-2 gap-3">
              <label>
                <span class="form-label">ClientID</span>
                <input v-model="imaForm.client_id" autocomplete="off" placeholder="IMA OpenAPI ClientID" class="field mt-2 w-full" />
              </label>
              <label>
                <span class="form-label">API Key</span>
                <input v-model="imaForm.api_key" type="password" autocomplete="new-password" :placeholder="imaForm.api_key_configured ? '已加密保存；不填则保留原密钥' : 'IMA OpenAPI API Key'" class="field mt-2 w-full" />
              </label>
            </div>
            <label class="mt-3 block">
              <span class="form-label">IMA Skill 下载地址</span>
              <input v-model="imaForm.skill_download_url" autocomplete="off" placeholder="https://app-dl.ima.qq.com/skills/ima-skills-1.1.7.zip" class="field mt-2 w-full" />
            </label>
            <div class="mt-3 flex flex-wrap items-center gap-4">
              <label class="text-xs text-slate-400"><input v-model="imaForm.clear_credentials" type="checkbox" class="mr-2" />清除已保存 API Key</label>
              <small class="text-xs leading-5 text-slate-600">保存后点击“检查状态”即可验证当前 IMA 用户可访问的知识库。</small>
            </div>
          </div>
          <label class="flex items-center gap-3 rounded-2xl border border-white/[.07] bg-black/10 px-4 py-3">
            <input v-model="channelForm.research_enabled" type="checkbox" />
            <span>
              <strong class="block text-sm text-slate-200">允许用于个股补证</strong>
              <small class="mt-1 block text-xs leading-5 text-slate-600">仅对个股研究 Agent 生效。通用 TG、MX、知识星球渠道通常保持关闭，避免重复采集。</small>
            </span>
          </label>
          <label v-if="channelForm.collection_mode==='playwright'">
            <span class="form-label">最大滚动次数</span>
            <input v-model.number="channelForm.max_scrolls" type="number" min="1" max="30" class="field mt-2 w-full" />
          </label>
          <div class="rounded-2xl border border-white/[.07] bg-black/10 p-3 text-xs leading-5 text-slate-500">
            原始快照始终先落库。AI 仅整理字段和拆分条目，不允许分析，也不会触发新的远端采集。
          </div>
          <label v-if="channelForm.collection_mode==='playwright'" class="col-span-2">
            <span class="form-label">登录后 URL 包含</span>
            <input v-model="channelForm.success_url_contains" placeholder="例如 /group/ 或 dashboard" class="field mt-2 w-full" />
          </label>
          <label v-if="channelForm.collection_mode==='playwright'" class="col-span-2">
            <span class="form-label">登录后页面选择器（可选，优先级更高）</span>
            <input v-model="channelForm.success_selector" placeholder="例如 .user-avatar 或 [data-testid=user-menu]" class="field mt-2 w-full" />
          </label>
          <div v-if="editingChannel?.id==='zsxq' || channelForm.name.includes('知识星球')" class="col-span-2 rounded-2xl border border-white/[.07] bg-black/10 p-4">
            <div class="flex items-center justify-between">
              <div>
                <span class="form-label">星球 ID 列表</span>
                <p class="mt-1 text-xs text-slate-600">支持配置一个或多个星球，采集器将逐个访问对应 `/group/&lt;id&gt;` 页面。</p>
              </div>
              <button @click="addGroupId" type="button" class="secondary">添加 ID</button>
            </div>
            <div v-if="!channelForm.group_ids.length" class="mt-3 rounded-xl border border-dashed border-white/[.1] px-3 py-4 text-center text-xs text-slate-600">尚未配置星球 ID</div>
            <div v-for="(_, index) in channelForm.group_ids" :key="index" class="mt-3 flex gap-2">
              <input v-model="channelForm.group_ids[index]" placeholder="例如 28888222124181" class="field flex-1" />
              <button @click="removeGroupId(index)" type="button" class="rounded-xl px-3 text-xs font-semibold text-rose-300 transition hover:bg-rose-400/10">移除</button>
            </div>
          </div>
          <div v-if="editingChannel?.id==='web-rumors'" class="col-span-2 rounded-2xl border border-teal-400/20 bg-teal-400/[.04] p-4">
            <span class="form-label">MX 登录会话 HAR 导入</span>
            <p class="mt-1 text-xs leading-5 text-slate-500">MX 掉线后，在浏览器重新登录并导出 HAR。这里会先实时验活，再加密替换本地会话；失败文件不会覆盖当前配置。支持最大 32 MB HAR。</p>
            <div class="mt-3 flex flex-wrap items-center gap-3">
              <label class="secondary cursor-pointer">
                <input type="file" accept=".har,application/json" class="hidden" @change="selectMxHar" />
                选择 HAR 文件
              </label>
              <span class="max-w-sm truncate text-xs text-slate-400">{{ mxHarFile?.name || '尚未选择文件' }}</span>
              <button type="button" class="primary" :disabled="mxHarImporting" @click="importMxHar">{{ mxHarImporting ? '正在验证...' : '验证并导入 HAR' }}</button>
            </div>
          </div>
          <label class="col-span-2">
            <span class="form-label">备注</span>
            <textarea v-model="channelForm.notes" rows="3" placeholder="登录状态、采集规则、待处理事项..." class="field mt-2 w-full"></textarea>
          </label>
        </div>

        <footer class="flex items-center justify-between border-t border-white/[.08] px-6 py-4">
          <button v-if="editingChannel && !editingChannel.builtin" @click="deleteChannel" class="rounded-xl px-3 py-2 text-xs font-semibold text-rose-300 transition hover:bg-rose-400/10">删除渠道</button>
          <span v-else class="text-xs text-slate-600">{{ editingChannel?.builtin ? '内置渠道不可删除' : '' }}</span>
          <div class="flex gap-3">
            <button v-if="editingChannel && channelForm.collection_mode==='playwright'" @click="openChannelLogin" class="secondary">保存并打开登录窗口</button>
            <button @click="closeChannelModal" class="secondary">取消</button>
            <button @click="saveChannel()" class="primary">保存配置</button>
          </div>
        </footer>
      </section>
    </div>
  </div>
</template>
