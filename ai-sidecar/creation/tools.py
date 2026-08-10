"""创作 Tool 的稳定标识、兼容规则和调用意图判断。"""

from __future__ import annotations

from typing import Any, Iterable

INTERNET_SEARCH_TOOL_ID = "internet_search"
MEMORY_SEARCH_TOOL_ID = "memory_search"
DATA_SEARCH_TOOL_ID = "data_search"
WEBPAGE_SCRAPE_TOOL_ID = "webpage_scrape"
PLANTUML_DIAGRAM_TOOL_ID = "plantuml_diagram"
GITHUB_SEARCH_TOOL_ID = "github_search"

REQUIRED_CREATION_TOOL_IDS = (
    INTERNET_SEARCH_TOOL_ID,
    MEMORY_SEARCH_TOOL_ID,
    DATA_SEARCH_TOOL_ID,
    WEBPAGE_SCRAPE_TOOL_ID,
)
OPTIONAL_CREATION_TOOL_IDS = (
    PLANTUML_DIAGRAM_TOOL_ID,
    GITHUB_SEARCH_TOOL_ID,
)
KNOWN_CREATION_TOOL_IDS = (
    *REQUIRED_CREATION_TOOL_IDS,
    *OPTIONAL_CREATION_TOOL_IDS,
)

# 可由路由决策选择的 Agent（Agent as Tool）。
DATA_ANALYSIS_AGENT_ID = "data_analysis_agent"
INDUSTRY_RESEARCH_AGENT_ID = "industry_research_agent"
SOLUTION_DESIGN_AGENT_ID = "solution_design_agent"

# 每个可路由能力在自身定义处声明描述：解决什么问题、在什么目标下使用。
# 路由系统提示词只动态加载这些描述（渐进式披露），不内置硬编码倾向。
# memory_search / webpage_scrape 属于结构性能力，不参与路由选择：前者总是作为
# 证据探针执行，后者只由 Harness 依据 data_search 的反馈调度。
ROUTING_CAPABILITIES = (
    {
        "id": INTERNET_SEARCH_TOOL_ID,
        "kind": "tool",
        "name": "互联网检索 Tool",
        "description": (
            "解决需要获取本地资料之外的公开外部事实的问题：政策法规、行业标准、"
            "新闻动态、竞品动态、市场调研、价格、开源版本趋势等。"
            "当请求需要最新外部信息或公开资料时使用。"
        ),
    },
    {
        "id": DATA_SEARCH_TOOL_ID,
        "kind": "tool",
        "name": "数据检索 Tool",
        "description": (
            "解决需要获取用户自有业务或运营数据的问题：看板、报表、指标、用量、"
            "成本、利用率、QPS 等，以及日报、周报、月报、经营分析、数据分析类文档。"
            "当用户要求查看、展示或获取今天、本周等实时数值时必须使用；"
            "只要请求疑似涉及用户自己的数据、看板或报表，优先用本能力探测，"
            "宁可多探测，不能遗漏。"
        ),
    },
    {
        "id": GITHUB_SEARCH_TOOL_ID,
        "kind": "tool",
        "name": "GitHub 检索 Tool",
        "description": (
            "解决需要开源实现参考的问题：开源项目、代码仓库、框架或 SDK 选型、"
            "技术方案对比。"
        ),
    },
    {
        "id": PLANTUML_DIAGRAM_TOOL_ID,
        "kind": "tool",
        "name": "PlantUML 画图 Tool",
        "description": (
            "解决需要用图形表达结构与流程的问题：架构图、流程图、时序图、"
            "类图等图示。"
        ),
    },
    {
        "id": DATA_ANALYSIS_AGENT_ID,
        "kind": "agent",
        "name": "数据分析 Agent",
        "description": (
            "解决需要对检索到的数据做量化分析的问题：指标对比、趋势解读、"
            "变化归因。在数据检索有结果且交付物需要数据结论时使用。"
        ),
    },
    {
        "id": INDUSTRY_RESEARCH_AGENT_ID,
        "kind": "agent",
        "name": "行业调研 Agent",
        "description": (
            "解决需要深入外部调研的问题：行业与市场现状、竞争格局、政策环境、"
            "技术演进趋势，形成带来源的调研结论。"
        ),
    },
    {
        "id": SOLUTION_DESIGN_AGENT_ID,
        "kind": "agent",
        "name": "方案设计 Agent",
        "description": (
            "解决需要把需求与调研结论转成可落地设计的问题：交付物是方案、"
            "架构、规划、设计类文档时，用于产出方案结构与关键取舍。"
        ),
    },
)

# 白名单由能力自描述派生，路由决策校验与提示词组装共用同一来源。
ROUTABLE_TOOL_IDS = tuple(
    item["id"] for item in ROUTING_CAPABILITIES if item["kind"] == "tool"
)
ROUTABLE_AGENT_IDS = tuple(
    item["id"] for item in ROUTING_CAPABILITIES if item["kind"] == "agent"
)


def routing_capability_lines(extra_lines: Iterable[str] = ()) -> list[str]:
    """渐进式披露：每个能力以自己的名称向路由模型呈现自己的自描述。"""
    lines: list[str] = []
    for item in ROUTING_CAPABILITIES:
        label = "Tool" if item["kind"] == "tool" else "Agent"
        lines.append(
            "- {id} ({label} · {name}): {description}".format(
                id=item["id"],
                label=label,
                name=item["name"],
                description=item["description"],
            )
        )
    lines.extend(extra_lines)
    return lines


class CreationToolExecutionError(RuntimeError):
    """携带稳定错误码的 Tool 失败；消息不得包含正文、URL 或凭据。"""

    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code


def normalize_creation_tool_ids(value: Any) -> tuple[str, ...]:
    """强制保留必备 Tool，同时对可选和未来 Tool ID 做稳定去重。"""
    normalized = list(REQUIRED_CREATION_TOOL_IDS)
    candidates: Iterable[Any] = value if isinstance(value, (list, tuple, set)) else ()
    for candidate in candidates:
        tool_id = str(candidate or "").strip()
        if tool_id and tool_id not in normalized:
            normalized.append(tool_id)
    return tuple(normalized)


def should_use_internet_search(text: str, requirement: dict[str, Any]) -> bool:
    if requirement.get("needs_latest"):
        return True
    return _contains_any(
        text,
        (
            "互联网",
            "联网",
            "网上",
            "搜索",
            "检索",
            "最新",
            "近期",
            "新闻",
            "政策",
            "法规",
            "标准",
            "行业调研",
            "市场调研",
            "竞品",
            "趋势",
            "价格",
        ),
    )


def should_use_data_tools(text: str, requirement: dict[str, Any]) -> bool:
    doc_type = str(requirement.get("doc_type") or "")
    evidence = f"{text}\n{doc_type}".lower()
    document_markers = (
        "日报",
        "周报",
        "月报",
        "季报",
        "年报",
        "项目总结",
        "工作总结",
        "经营分析",
        "数据分析",
        "指标分析",
        "数据报告",
        "业绩报告",
        "运营报告",
        "daily report",
        "weekly report",
        "project summary",
        "data analysis",
    )
    if any(marker in evidence for marker in document_markers):
        return True

    # 方案类任务只要明确围绕可量化对象，也需要先探测本地数据源。否则像
    # “治理 GPU 利用率”这样的请求会只命中旧文档，即使其中引用了可实时
    # 刷新的运营看板，也不会进入 data_search -> webpage_scrape 反馈链路。
    metric_markers = (
        "gpu",
        "利用率",
        "使用率",
        "成本",
        "用量",
        "资源池",
        "吞吐",
        "延迟",
        "可用性",
        "qps",
        "token",
        "指标",
        "看板",
        "报表",
    )
    evidence_intent_markers = (
        "方案",
        "治理",
        "分析",
        "复盘",
        "总结",
        "报告",
        "现状",
        "基线",
        "目标",
        "优化",
        "plan",
        "analysis",
        "review",
        "baseline",
    )
    if any(marker in evidence for marker in metric_markers) and any(
        marker in evidence for marker in evidence_intent_markers
    ):
        return True

    # 查询类请求（查看/展示/获取某看板或指标的实时数值）即使不产出方案、
    # 总结类文档，也必须先走 data_search，才能经由看板刷新进入浏览器抓取
    # 和数据分析链路，而不是直接凭记忆生成文档。
    query_intent_markers = (
        "查看",
        "查询",
        "查一下",
        "查下",
        "查查",
        "看看",
        "看下",
        "看一下",
        "展示",
        "显示",
        "获取",
        "拉取",
        "抓取",
        "取数",
        "核对",
        "check",
        "show",
        "fetch",
        "query",
    )
    freshness_markers = (
        "今天",
        "今日",
        "昨天",
        "本周",
        "这周",
        "本月",
        "当天",
        "当前",
        "实时",
        "现在",
        "最新",
        "today",
        "latest",
        "real-time",
        "realtime",
    )
    has_metric_object = any(marker in evidence for marker in metric_markers)
    has_query_intent = any(marker in evidence for marker in query_intent_markers)
    has_freshness_intent = any(marker in evidence for marker in freshness_markers)
    return has_metric_object and (has_query_intent or has_freshness_intent)


def should_use_github_search(text: str) -> bool:
    return _contains_any(
        text,
        (
            "github",
            "开源",
            "代码仓库",
            "仓库",
            "repository",
            "repo",
            "sdk",
            "框架选型",
            "技术选型",
        ),
    )


def should_use_plantuml(text: str, requirement: dict[str, Any]) -> bool:
    if requirement.get("needs_images"):
        return True
    return _contains_any(
        text,
        (
            "plantuml",
            "画图",
            "图示",
            "架构图",
            "流程图",
            "时序图",
            "组件图",
            "关系图",
            "部署图",
            "活动图",
        ),
    )


def build_plantuml_context(text: str) -> dict[str, str]:
    lowered = text.lower()
    if "时序" in text or "sequence" in lowered:
        diagram_type = "sequence"
        starter = "@startuml\nactor 用户\nparticipant 系统\n用户 -> 系统: 发起请求\n系统 --> 用户: 返回结果\n@enduml"
    elif "流程" in text or "活动" in text or "activity" in lowered:
        diagram_type = "activity"
        starter = "@startuml\nstart\n:接收输入;\n:执行处理;\n:输出结果;\nstop\n@enduml"
    elif "部署" in text or "deployment" in lowered:
        diagram_type = "deployment"
        starter = "@startuml\nnode 客户端\nnode 服务端\n客户端 --> 服务端: 请求\n@enduml"
    else:
        diagram_type = "component"
        starter = "@startuml\nleft to right direction\ncomponent 客户端\ncomponent 核心服务\n客户端 --> 核心服务: 调用\n@enduml"
    return {
        "diagram_type": diagram_type,
        "language": "plantuml",
        "starter": starter,
        "instruction": (
            "在正文最适合的位置输出一段 ```plantuml 代码块；"
            "基于正文真实对象替换示例节点，保持图中术语与正文一致，"
            "只绘制已经说明的边界和关系。"
        ),
    }


def validate_routing_decision(raw: Any) -> dict[str, list[str]]:
    """推理后的代码只做校验：过滤白名单外的能力 ID、去重并保持顺序。"""

    def clean(values: Any, allowed: tuple[str, ...]) -> list[str]:
        cleaned: list[str] = []
        if isinstance(values, (list, tuple, set)):
            for value in values:
                item = str(value or "").strip()
                if item in allowed and item not in cleaned:
                    cleaned.append(item)
        return cleaned

    tools_raw: Any = ()
    agents_raw: Any = ()
    if isinstance(raw, dict):
        tools_raw = raw.get("tools") or ()
        agents_raw = raw.get("agents") or ()
    return {
        "tools": clean(tools_raw, ROUTABLE_TOOL_IDS),
        "agents": clean(agents_raw, ROUTABLE_AGENT_IDS),
    }


def fallback_routing_decision(
    text: str,
    requirement: dict[str, Any],
) -> dict[str, Any]:
    """模型不可用或输出无法解析时的降级路由，沿用探针优先的保守策略。"""
    normalized = text.lower()
    tools: list[str] = []
    if should_use_internet_search(text, requirement):
        tools.append(INTERNET_SEARCH_TOOL_ID)
    if should_use_github_search(text):
        tools.append(GITHUB_SEARCH_TOOL_ID)
    if should_use_plantuml(text, requirement):
        tools.append(PLANTUML_DIAGRAM_TOOL_ID)
    if should_use_data_tools(text, requirement):
        tools.append(DATA_SEARCH_TOOL_ID)
    agents: list[str] = []
    if any(
        marker in normalized
        for marker in ("数据", "指标", "分析", "统计", "趋势", "成本", "收益")
    ):
        agents.append(DATA_ANALYSIS_AGENT_ID)
    if should_use_internet_search(text, requirement):
        agents.append(INDUSTRY_RESEARCH_AGENT_ID)
    if any(
        marker in f"{text} {requirement.get('doc_type', '')}"
        for marker in ("方案", "架构", "PRD", "设计", "规划", "建设")
    ):
        agents.append(SOLUTION_DESIGN_AGENT_ID)
    return {"tools": tools, "agents": agents, "source": "fallback"}


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    normalized = text.lower()
    return any(marker.lower() in normalized for marker in markers)
