from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class SourceDefinition:
    id: str
    display_name: str
    category: str
    collection_mode: str
    capabilities: tuple[str, ...]
    credential_mode: str
    risk_level: str
    detail: str


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    id: str
    name: str
    kind: str
    priority: int
    status: str
    detail: str


SOURCES = (
    SourceDefinition(
        id="akshare",
        display_name="A股市场数据（AkShare / BaoStock / TuShare）",
        category="market_data",
        collection_mode="akshare",
        capabilities=("quotes", "company_profile", "fundamentals", "market_snapshot"),
        credential_mode="optional_token",
        risk_level="low",
        detail="结构化市场数据聚合；单个上游异常时保留其他组件结果。",
    ),
    SourceDefinition(
        id="industry-news",
        display_name="产业趋势公开资讯",
        category="public_industry_news",
        collection_mode="industry_news",
        capabilities=("industry_rankings", "industry_news", "company_news", "announcements"),
        credential_mode="none",
        risk_level="low",
        detail="东方财富行业排名与资讯、个股资料和巨潮公告。",
    ),
    SourceDefinition(
        id="wechat-mp-rss",
        display_name="微信公众号（WeRSS）",
        category="public_wechat_articles",
        collection_mode="wechat_rss",
        capabilities=("wechat_qr_login", "wechat_official_accounts", "subscription_management", "rss_feed", "article_snapshot"),
        credential_mode="optional_access_key",
        risk_level="medium",
        detail="通过 AlphaDesk 弹窗完成微信扫码授权，搜索并加入公众号；后台读取严格时间窗内的 RSS 快照。",
    ),
    SourceDefinition(
        id="ima-knowledge",
        display_name="IMA 知识库",
        category="private_knowledge_base",
        collection_mode="ima_knowledge_base",
        capabilities=("knowledge_base_search", "knowledge_base_browse", "private_knowledge_retrieval"),
        credential_mode="openapi_key",
        risk_level="medium",
        detail="通过 IMA OpenAPI 搜索可访问知识库，默认覆盖全部可访问知识库，也可用环境变量限定范围。",
    ),
    SourceDefinition(
        id="zsxq",
        display_name="知识星球",
        category="authenticated_community",
        collection_mode="playwright",
        capabilities=("authenticated_snapshot", "group_feed"),
        credential_mode="persistent_browser_profile",
        risk_level="high",
        detail="登录态社区信源；严格按时间窗增量采集。",
    ),
    SourceDefinition(
        id="web-rumors",
        display_name="MX 小作文频道",
        category="authenticated_rumors",
        collection_mode="requests",
        capabilities=("authorized_request_replay", "room_feed"),
        credential_mode="encrypted_session_config",
        risk_level="high",
        detail="授权会话信源；严格按房间白名单和时间窗采集。",
    ),
    SourceDefinition(
        id="tg-public",
        display_name="TG 小作文频道",
        category="public_rumors",
        collection_mode="requests",
        capabilities=("public_preview", "channel_feed"),
        credential_mode="none",
        risk_level="medium",
        detail="Telegram 公开预览页面；按游标向前翻页至时间窗边界。",
    ),
)

TOOLS = (
    ToolDefinition(
        id="akshare",
        name="A股市场数据聚合",
        kind="python",
        priority=1,
        status="ready",
        detail="AkShare 优先，BaoStock 自动后备，TuShare 配置 token 后参与；组件独立限时。",
    ),
    ToolDefinition(
        id="requests",
        name="HTTP 请求与产业资讯采集",
        kind="python",
        priority=2,
        status="ready",
        detail="公开接口、结构化网页与产业趋势公开资讯；所有自建请求统一使用浏览器 UA。",
    ),
    ToolDefinition(
        id="ima_openapi",
        name="IMA 知识库 OpenAPI",
        kind="python_http",
        priority=3,
        status="ready",
        detail="搜索个人与共享 IMA 知识库，作为报告和个股研究的私有知识补充。",
    ),
    ToolDefinition(
        id="playwright",
        name="Playwright 持久化浏览器",
        kind="browser",
        priority=4,
        status="setup",
        detail="登录态渠道、动态网页与强反爬页面。",
    ),
    ToolDefinition(
        id="manual",
        name="其他人工补充渠道",
        kind="fallback",
        priority=5,
        status="standby",
        detail="保留来源说明，进入报告审查。",
    ),
)

SOURCE_ALIASES = {
    "146aa28e21": "tg-public",
}

SOURCE_BY_ID = {source.id: source for source in SOURCES}
CANONICAL_CHANNEL_NAMES = {
    **{source.id: source.display_name for source in SOURCES},
    **{alias: SOURCE_BY_ID[source_id].display_name for alias, source_id in SOURCE_ALIASES.items()},
}


def source_catalog() -> list[dict]:
    return [asdict(source) for source in SOURCES]


def tool_catalog() -> list[dict]:
    return [asdict(tool) for tool in TOOLS]
