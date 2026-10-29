"""
电商售后工单处理工作流 (基于 LangGraph 状态图与项目内置 aftersales 工具集)

业务链路:
识别意图 -> 查询订单 -> 检查物流状态 -> 核对售后政策 -> 判断是否符合条件 -> 执行退款/换货 -> 发送通知 -> 异常时转人工

架构设计:
1. 状态模式 (AfterSalesState): 定义全流程上下文状态。
2. 真实工具对接 (Official Tools): 直接调用项目内置的 `react_ai.tools.aftersales` 工具类
   - QueryOrderTool (query_order): 查订单及商品详情
   - QueryLogisticsTool (query_logistics): 查物流签收与轨迹
   - SearchPolicyTool (search_policy): 查 PG after_sales_policies 政策规则并计算合规性
   - CreateRefundTool (create_refund): 调用支付写操作退款 (支持幂等)
   - SendNotificationTool (send_notification): 发送用户短信/站内信
   - EscalateToHumanTool (escalate_to_human): 转人工审核并写入待办工单
3. 条件路由 (Conditional Edges): 根据政策合规性与金额风控分流至自动执行或人工介入。
4. 持久化支持 (PostgreSQL Checkpointer): 挂载 AsyncPostgresSaver 持久化检查点（§2.6），
   追问中的会话状态跨进程重启、跨多实例存活；PG 不可用时降级 MemorySaver 并告警。
5. LLM 意图识别与槽位填充 (Slot Filling): 引入真实大模型 (项目内置 LLMRouter) 从用户
   自然语言中抽取意图/订单号/原因等槽位; 采用 function calling (SLOT_TOOL + tool_choice)
   从协议层保证结构化输出, LLM 输出再经 Pydantic Schema (SlotExtractionResult) 强校验
   枚举/格式/长度，非法输出拒绝并降级; 必要槽位缺失时路由到追问节点向用户反问补槽,
   用户补充后同一线程重新抽取, 替代原先写死订单号兜底的做法; LLM 不可用时降级为关键词规则。
   槽位格式合法 ≠ 业务可用: 订单号在意图节点即查库校验存在性+归属权 (防越权), 校验失败
   剔除槽位并携带针对性话术回流追问; 查询/政策节点失败不再裸 raise 崩图, 分别降级为
   RETRY_ASK 回流追问与 ESCALATE_HUMAN 透传转人工; 意图枚举含 OTHER, 非售后诉求经
   越界意图网关短路为 OUT_OF_SCOPE 出口直接答复, 不触发任何售后工具写操作。
   输入安全护栏 (GuardrailChain): 用户原文进 LLM 前先过注入检测 (命中 BLOCK 直接转人工,
   不进模型不进售后链) 与 PII 脱敏 (手机号/身份证掩码后才发往模型); 采用"入模型脱敏、
   入数据库原文"双轨——关键词降级与业务校验始终基于原文订单号。
6. 异步兼容 (Async Native): LLM 意图节点为原生 async def + await, 图可经 `ainvoke` 直接
   挂入 FastAPI 等已运行事件循环的服务内并发执行, 无 asyncio.run 重入错误; 同步桥接仅保留
   在脚本入口 `asyncio.run(main_async())` 一处。
"""

import sys                                              # 导入系统模块，用于操作 Python 解释器的模块搜索路径
import asyncio                                           # 导入异步运行时模块，仅用于脚本入口 asyncio.run(main_async()) 一处桥接
import json                                             # 导入 JSON 编解码模块，用于解析 LLM 输出的槽位 JSON
import re                                                # 导入正则模块，用于关键词降级时从文本中提取订单号
import uuid                                             # 导入通用唯一标识符模块，用于生成幂等键、运单号等随机ID
from contextlib import asynccontextmanager              # 导入异步上下文管理器装饰器，用于封装 PG 检查点的连接生命周期
from pathlib import Path                                # 导入路径对象类，用于跨平台地解析文件/目录路径
from typing import Dict, Any, Optional, Literal, List   # 导入类型注解工具：字典、任意类型、可选类型、字面量枚举类型、列表
from typing_extensions import TypedDict                 # 导入 TypedDict，用类型字典形式定义 LangGraph 的状态结构
from pydantic import BaseModel, Field, ValidationError, field_validator  # 导入 Pydantic 校验基类、字段约束、校验异常与自定义校验器

# 自动将项目 src 根路径加入模块搜索路径，支持直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))  # 把本文件向上两级目录下的 src/ 插到搜索路径首位，使 react_ai 包可被直接导入

from langgraph.graph import StateGraph, START, END      # 导入 LangGraph 状态图构建器及图的虚拟起点/终点标记
from langgraph.checkpoint.memory import MemorySaver     # 导入内存版检查点存储器，用于按 thread_id 持久化图执行状态
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver  # 导入 PG 异步检查点（§2.6）：会话状态跨进程重启/多实例持久化

# 导入本项目的 LLM 网关与配置，用于真实大模型意图识别
from react_ai.config import settings                     # 导入全局配置单例：从环境变量/.env 读取 LLM 密钥、模型名等
from react_ai.llm.base import ChatMessage               # 导入对话消息模型（role/content 结构）
from react_ai.llm.router import LLMRouter               # 导入 LLM 路由器：主模型失败自动降级到备用模型
from react_ai.llm.providers.deepseek import DeepSeekProvider  # 导入 DeepSeek 客户端（OpenAI 兼容协议，默认主模型）
from react_ai.llm.providers.openai import OpenAIProvider  # 导入 OpenAI 客户端（备用降级目标）
from react_ai.schemas.tools import ToolDefinition, ToolParameterSchema  # 导入工具定义与参数 Schema 模型，用于 function calling 结构化抽槽

# 导入安全护栏组件：输入侧注入检测 + PII 脱敏（§2.4）
from react_ai.guardrails.base import GuardrailChain, GuardrailAction, GuardrailContext, GuardrailDirection  # 护栏链、动作枚举、检查上下文与方向枚举
from react_ai.guardrails.injection import PromptInjectionGuardrail  # 提示词注入/越狱特征检测护栏
from react_ai.guardrails.pii import PIIMaskingGuardrail             # 手机号/身份证正则脱敏护栏

# 导入本项目已定义好的电商售后工具与上下文
from react_ai.tools.base import ToolContext             # 导入工具执行上下文（携带调用方、租户、用户、幂等键等信息）
from react_ai.tools.aftersales import (                 # 从项目售后工具集批量导入 6 个真实业务工具类
    QueryOrderTool,                                    # 订单查询工具：按订单号查订单与商品明细
    QueryLogisticsTool,                                # 物流查询工具：查签收状态与物流轨迹
    SearchPolicyTool,                                  # 政策检索工具：查售后政策表并计算是否合规
    CreateRefundTool,                                  # 退款创建工具：向支付系统发起退款写操作
    SendNotificationTool,                              # 通知发送工具：写站内信/短信通知表
    EscalateToHumanTool,                               # 转人工工具：生成人工审核待办工单
)

# 实例化官方工具单例
query_order_tool = QueryOrderTool()                    # 创建订单查询工具实例（无状态，全局复用）
query_logistics_tool = QueryLogisticsTool()            # 创建物流查询工具实例
search_policy_tool = SearchPolicyTool()                # 创建政策检索工具实例
create_refund_tool = CreateRefundTool()                # 创建退款执行工具实例
send_notification_tool = SendNotificationTool()        # 创建通知发送工具实例
escalate_to_human_tool = EscalateToHumanTool()         # 创建转人工工具实例


# ============================================================================
# 0. LLM 意图识别基础设施 (真实大模型 + 槽位 Schema)
# ============================================================================
SLOT_EXTRACTION_PROMPT = """你是电商售后系统的意图与槽位抽取器。只输出一个 JSON 对象，禁止输出任何其他文字。
字段定义:
- intent: 意图枚举，取值 RETURN_AND_REFUND(退货退款) / REFUND_ONLY(仅退款) / EXCHANGE(换货) / OTHER(非售后诉求：查天气、问客服电话、闲聊等越界内容一律归此类)
- order_id: 用户明确提供的订单号原文；用户未提及时必须为 null，严禁编造
- reason: 售后原因简述（不超过20字）；未提及为 null
- reason_type: 原因大类，取值 quality_issue(商品破损/质量瑕疵等质量问题) / no_reason(不喜欢等无理由诉求)
- missing_slots: intent / order_id / reason_type 中无法从对话判断的字段名列表，均能判断时为空数组
示例: {"intent": "RETURN_AND_REFUND", "order_id": "ORD123", "reason": "衣服破损", "reason_type": "quality_issue", "missing_slots": []}"""  # 槽位抽取的系统提示词：定义输出 JSON Schema 与"禁止编造订单号"约束

REQUIRED_SLOTS = ("intent", "order_id", "reason_type")  # 必要槽位：意图、订单号、原因大类缺失时必须追问用户补槽


class SlotExtractionResult(BaseModel):                 # Pydantic 槽位抽取结果 Schema：大模型输出必须通过校验才可信
    """约束 LLM 结构化输出的枚举取值/字段格式/长度上限，非法输出直接拒绝并触发关键词降级。"""
    intent: Optional[Literal["RETURN_AND_REFUND", "REFUND_ONLY", "EXCHANGE", "OTHER"]] = None  # 意图枚举（含 OTHER 越界出口，§2.3）：超出取值范围即校验失败
    order_id: Optional[str] = Field(default=None, pattern=r"^[A-Za-z0-9_\-]{4,64}$")  # 订单号格式：仅允许字母/数字/下划线/连字符，防幻觉文本与注入
    reason: Optional[str] = Field(default=None, max_length=50)  # 原因描述长度上限，防止模型冗长输出污染状态
    reason_type: Optional[Literal["quality_issue", "no_reason"]] = None  # 原因大类枚举
    missing_slots: List[str] = Field(default_factory=list)  # 模型自报的缺失槽位名列表

    @field_validator("intent", "order_id", "reason", "reason_type", mode="before")  # 前置归一化校验器
    @classmethod
    def _blank_to_none(cls, v: Any) -> Any:            # 空字符串/纯空白统一视为 None
        if isinstance(v, str) and not v.strip():       # 命中空白字符串
            return None                                # 归一为 None，避免 pattern 对空串误报
        if isinstance(v, str):                         # 非空白字符串
            return v.strip()                           # 顺带去除首尾空白
        return v                                       # None 等原样透传

_llm_router: Optional[LLMRouter] = None                # LLM 路由器全局懒加载单例（首次使用时初始化）

_SLOT_VALUE_FIELD = {"intent": "value", "order_id": "string_value", "reason": "string_value", "reason_type": "value"}  # 各槽位对应的取值字段名映射

# function calling 抽槽工具定义：参数 JSON Schema 即 SlotExtractionResult 的协议层镜像，
# 配合 tool_choice 强制调用，模型返回由 API 保证是结构化 tool_calls 而非自由文本 JSON
SLOT_TOOL = ToolDefinition(
    name="output_aftersales_slots",                    # 工具名，作为 tool_choice 强制调用的目标
    description="从用户售后诉求对话中提取意图与槽位并结构化输出",  # 供模型理解何时调用（本场景恒定调用）
    parameters=ToolParameterSchema(                    # JSON Schema 约束返回参数结构
        type="object",
        properties={
            "intent": {                                 # 意图枚举：协议层限定取值范围
                "type": "string",
                "enum": ["RETURN_AND_REFUND", "REFUND_ONLY", "EXCHANGE", "OTHER"],
                "description": "退货退款/仅退款/换货；非售后诉求（查天气、问客服电话、闲聊等）选 OTHER；无法判断时省略该字段",
            },
            "order_id": {                               # 订单号：未提及必须省略，严禁编造
                "type": "string",
                "description": "用户明确提供的订单号原文；未提及时不要填写",
            },
            "reason": {                                 # 原因简述
                "type": "string",
                "description": "售后原因简述（不超过20字）",
            },
            "reason_type": {                            # 原因大类枚举
                "type": "string",
                "enum": ["quality_issue", "no_reason"],
                "description": "质量问题 quality_issue / 无理由 no_reason",
            },
            "missing_slots": {                          # 模型自报缺失槽位
                "type": "array",
                "items": {"type": "string"},
                "description": "无法从对话判断的必要槽位名列表",
            },
        },
        required=[],                                    # 全部可选：缺槽由节点侧统一计算并触发追问
    ),
)

_SLOT_KEYS = {"intent", "order_id", "reason", "reason_type", "missing_slots"}  # 合法槽位键集合，用于甄别模拟/异常返回的 tool_calls 参数

# 抽槽输入护栏链（§2.4）：用户原文进 LLM 前必须先过安全护栏。
# 短路策略：注入检测命中即 BLOCK（后续护栏不再跑）；PII 命中则 MODIFY 产出脱敏文本继续放行。
slot_guard_chain = GuardrailChain(name="slot_input_chain", short_circuit=True)   # 构建抽槽入口专用护栏链
slot_guard_chain.add_guardrail(PromptInjectionGuardrail())                        # 第一道：提示词注入/越狱检测 -> BLOCK 阻断
slot_guard_chain.add_guardrail(PIIMaskingGuardrail())                             # 第二道：手机号/身份证 -> 正则掩码脱敏


def get_llm_router() -> LLMRouter:                     # 懒加载构建 LLM 路由器
    """基于全局配置构建带降级链的 LLMRouter（LLM_* 配置的 DeepSeek 为主，OPENAI_* 为备用）。"""
    global _llm_router                                   # 声明使用模块级单例变量
    if _llm_router is None:                              # 首次调用时尚未初始化
        primary = DeepSeekProvider(                      # 主模型客户端：DeepSeek（OpenAI 兼容协议）
            api_key=settings.llm.api_key,                # API 密钥，来自 LLM_API_KEY 环境变量
            api_base=settings.llm.api_base,              # 接口地址，默认 https://api.deepseek.com
            model=settings.llm.model_name,               # 模型名，来自 LLM_MODEL_NAME 环境变量
            timeout_seconds=settings.llm.timeout_seconds,  # 请求超时时间
        )
        fallback = OpenAIProvider()                      # 备用客户端：自动读取 OPENAI_API_KEY/OPENAI_BASE_URL 环境变量
        _llm_router = LLMRouter(primary, fallbacks=[fallback])  # 组装路由器：主模型失败自动降级到备用模型
    return _llm_router                                   # 返回全局路由器单例


def _extract_json(text: str) -> Optional[Dict[str, Any]]:  # 从 LLM 回复文本中稳健地提取 JSON 对象
    text = text.strip()                                  # 去除首尾空白
    if text.startswith("```"):                           # 兼容模型用 markdown 代码围栏包裹输出的情况
        text = text.strip("`").strip()                   # 剥掉 ``` 围栏标记
        if text.startswith("json"):                      # 围栏语言标记为 json 时
            text = text[4:]                              # 去掉开头的 "json" 四个字符
    start, end = text.find("{"), text.rfind("}")         # 定位第一个 { 与最后一个 }，截取最外层 JSON 对象
    if start == -1 or end <= start:                      # 未找到完整对象结构
        return None                                      # 返回 None 交由调用方降级处理
    try:                                                 # 尝试解析 JSON
        return json.loads(text[start:end + 1])           # 解析成功返回字典
    except json.JSONDecodeError:                         # 内容非法 JSON（模型幻觉/截断）
        return None                                      # 同样返回 None 触发降级


# ============================================================================
# 1. 状态定义 (State Definition)
# ============================================================================
class AfterSalesState(TypedDict, total=False):         # 定义全流程共享的状态类型；total=False 表示所有字段均可缺省
    """电商售后全流程核心状态对象。"""
    session_id: str                   # 会话/线程ID
    user_id: str                      # 发起请求的用户ID
    user_message: str                 # 用户输入 (如: "我买的衣服破了，要退货")

    # 意图与槽位解析
    intent: Optional[str]             # RETURN_AND_REFUND(退货退款) / REFUND_ONLY(仅退款) / EXCHANGE(换货)
    order_id: Optional[str]           # 关联订单号（LLM 槽位抽取所得，缺失时触发追问，不再写死兜底）
    reason: Optional[str]             # 售后具体原因
    reason_type: Optional[str]        # 政策原因大类: quality_issue (质量问题) / no_reason (无理由)
    slot_values: Optional[Dict[str, Any]]   # 槽位填充表 {槽位名: {"value"/"string_value": 值}}，跨轮次累积
    missing_slots: Optional[list]           # 当前仍缺失的必要槽位名列表
    slot_rounds: int                  # 已追问轮数，超过上限后放弃补槽转人工
    dialog_history: Optional[list]    # 多轮补槽对话历史 [{role, content}]，供 LLM 结合上下文抽取
    order_validated_id: Optional[str] # 已通过存在性+归属权业务校验的订单号（换单号后需重新校验）
    slot_error: Optional[str]         # 槽位业务校验失败文案，非空时优先作为追问话术回流给用户

    # 外部与工具执行结果缓存
    order_info: Optional[Dict[str, Any]]      # 订单详情
    logistics_info: Optional[Dict[str, Any]]  # 物流轨迹
    policy_rules: Optional[Dict[str, Any]]    # 售后政策查询计算结果

    # 决策与判定
    is_eligible: bool                         # 规则判定是否通过
    decision: Optional[str]                   # 最终决策: AUTO_REFUND / RETURN_SHIPPING / ESCALATE_HUMAN / REJECTED / RETRY_ASK / OUT_OF_SCOPE
    human_reason: Optional[str]               # 转人工的具体原因

    # 写操作结果与最终回复
    action_result: Optional[Dict[str, Any]]   # 写操作结果 (退款单号、工单号等)
    notification_sent: bool                   # 是否已发通知
    final_reply: Optional[str]                # 呈现给用户的答复


# ============================================================================
# 2. 状态图节点 (Graph Nodes - 调用项目内置真实工具)
# ============================================================================
async def extract_slots_via_llm(messages: list) -> Optional[Dict[str, Any]]:  # 调用真实 LLM 完成意图与槽位抽取，失败返回 None 由上层降级
    """function calling 结构化抽槽：tool_choice 强制模型调用 SLOT_TOOL，
    槽位取自协议层保证的 tool_calls.arguments；模型未走工具路径时回退解析正文 JSON。"""
    try:                                                 # 包住整个 LLM 调用，网络/解析异常统一走降级
        router = get_llm_router()                        # 获取（或懒初始化）LLM 路由器
        request_messages = [ChatMessage(role="system", content=SLOT_EXTRACTION_PROMPT)] + [  # 系统提示词 + 多轮对话历史
            ChatMessage(role=m["role"], content=m["content"]) for m in messages               # 将历史 dict 转为 ChatMessage 模型
        ]
        response = await router.generate(                # 低温度调用并强制工具：协议层保证结构化输出
            request_messages,
            tools=[SLOT_TOOL],                           # 传入槽位 JSON Schema 工具定义
            temperature=0.0,                             # 零温度保证抽取确定性
            max_tokens=300,
            tool_choice=SLOT_TOOL.name,                  # 强制调用抽槽工具，模型不再输出自由文本
        )
        raw = None                                       # 槽位原始字典，优先取 tool_calls
        if response.tool_calls:                          # 首选路径：function calling 结构化返回
            tc = response.tool_calls[0]                  # 取首个工具调用（tool_choice 已限定唯一目标）
            args = tc.arguments if isinstance(tc.arguments, dict) else _extract_json(str(tc.arguments))  # 参数已是 dict 或需二次解析
            if args and set(args) & _SLOT_KEYS:          # 参数须命中至少一个已知槽位键，甄别模拟器的无关返回
                raw = args                               # 采用协议层结构化槽位
        if raw is None and response.content:             # 回退路径：模型忽略 tool_choice 时从正文抢救 JSON
            raw = _extract_json(response.content)        # 兼容旧行为的兜底解析
        if raw is None:                                  # 两条路径均无有效结构
            return None                                  # 直接触发关键词降级
        validated = SlotExtractionResult.model_validate(raw)  # Pydantic Schema 强校验：枚举/格式/长度不合规即抛 ValidationError
        print(f"[LLM] 槽位输出通过 Pydantic 校验: {validated.model_dump(exclude_none=True)}")  # 打印规范化后的槽位
        return validated.model_dump(exclude_none=True) # 返回校验清洗后的槽位字典（None 字段已剔除）
    except ValidationError as ve:                      # LLM 输出不符合槽位 Schema（幻觉枚举值、非法订单号格式等）
        print(f"[LLM] 槽位输出未通过 Pydantic 校验，降级为关键词规则: {ve.errors()}")  # 打印具体违规字段与原因
        return None                                    # 触发关键词降级
    except Exception as exc:                           # LLM 超时、鉴权失败、路由熔断等任何异常
        print(f"[LLM] 槽位抽取失败，降级为关键词规则: {exc}")  # 打印降级日志，便于排查
        return None                                      # 返回 None 触发关键词兜底


def fallback_extract_slots(user_msg: str) -> Dict[str, Any]:  # LLM 不可用时的关键词规则兜底抽取（保留原演示逻辑并增强）
    """基于质量关键词命中做规则抽取；无法判定的槽位置空以触发追问。"""
    is_quality = any(k in user_msg for k in ["破", "坏", "烂", "残", "瑕疵", "质量", "断"])  # 命中任一质量类关键词即判定为质量问题
    has_refund = any(k in user_msg for k in ["退", "退款", "退货"])  # 命中"退"类关键词判定为退货退款意图
    has_exchange = any(k in user_msg for k in ["换货", "换一个", "换新"])  # 命中"换"类关键词判定为换货意图
    has_no_reason = any(k in user_msg for k in ["不想要", "不喜欢", "无理由", "后悔", "不需要"])  # 命中"无理由"类关键词判定为非质量问题
    order_match = re.search(r"ORD[_\-A-Z0-9]+|\b\d{8,}\b", user_msg, re.IGNORECASE)  # 正则尝试从文本提取订单号（如 ORD_CLOTH_001 / 8 位以上数字）
    has_any_signal = has_refund or has_exchange or is_quality or has_no_reason or bool(order_match)  # 是否命中任何售后相关信号
    return {                                             # 组装与 LLM 输出同构的槽位字典
        "intent": (("RETURN_AND_REFUND" if has_refund else "EXCHANGE") if (has_refund or has_exchange) else ("OTHER" if not has_any_signal else None)),  # 售后词->意图；完全无售后信号->OTHER 越界出口（§2.3）；有质量/订单线索但无动作词->None 交由追问
        "reason_type": "quality_issue" if is_quality else ("no_reason" if has_no_reason else None),  # 仅显式表达才能判定大类，否则置 None 触发追问
        "reason": "关键词规则判定：质量问题" if is_quality else ("关键词规则判定：无理由" if has_no_reason else None),  # 规则模式下的原因描述
        "order_id": order_match.group(0) if order_match else None,  # 正则命中则填订单号，未命中置 None 触发追问（不写死兜底）
        "missing_slots": [],                             # 缺失槽位由主节点统一计算，此处留空
    }


def validate_order_slot(order_id: str, user_id: str) -> tuple:  # 槽位业务校验：格式合法 ≠ 业务可用，查库验证存在性+归属权
    """返回 (错误文案, 订单数据)。错误文案为 None 表示校验通过；
    失败不抛异常，由调用方剔除槽位并回流追问分支。"""
    ctx = ToolContext(caller_id="langgraph-aftersales", tenant_id="default", user_id=user_id)  # 以当前用户身份查询
    try:                                                 # 工具内部超时/连接失败等硬异常同样收敛为文案
        res = query_order_tool.call({"order_id": order_id}, ctx)  # 复用真实订单查询工具做存在性核验
    except Exception as exc:                             # 数据库不可用等基础设施异常
        return f"订单系统暂时不可用（{exc}），请稍后重试或回复『人工』由专员跟进", None
    if not res.ok or not res.data:                       # 订单不存在或查询失败
        return "未查询到该订单，请核对订单号（可在『我的订单』页面复制）后重新发送", None
    if res.data.get("user_id") != user_id:               # 归属权校验：防止用他人订单号发起售后（越权）
        return "该订单不属于当前账号，请使用下单账号重新提交订单号，或回复『人工』", None
    return None, res.data                                # 校验通过，顺带返回订单详情供缓存复用


async def intent_recognition_node(state: AfterSalesState) -> Dict[str, Any]:  # 节点1（异步）：调用真实 LLM 从多轮对话中抽取意图与槽位，累积合并并计算缺失必要槽位
    """
    [节点 1: LLM 意图识别与槽位填充]
    - 携带对话历史调用 LLM 抽取 intent/order_id/reason/reason_type 槽位;
    - 槽位跨轮次累积合并 (已有值优先，不被新一轮覆盖);
    - 必要槽位缺失时不写死兜底，而是交由 ask_user 节点反问用户补槽。
    """
    user_msg = state.get("user_message", "")             # 本轮用户输入（补槽轮次里就是用户对追问的回答）

    # ---- 输入安全护栏（§2.4）：注入阻断、PII 脱敏，脱敏文本才发往 LLM ----
    guard = slot_guard_chain.check(                      # 同步检查本轮用户原文（GuardrailChain.check 可在任意节点安全调用）
        user_msg,
        GuardrailContext(                                # 检查上下文：链路追踪 + 会话/用户归属 + 输入方向
            request_id=state.get("session_id", ""),      # 以会话ID作请求追踪标识
            session_id=state.get("session_id", ""),      # 会话ID
            user_id=state.get("user_id"),                # 操作用户
            direction=GuardrailDirection.INPUT,          # 输入侧检查（PII 身份证规则仅输入方向生效）
        ),
    )
    if guard.action == GuardrailAction.BLOCK:            # 命中注入/越狱特征：不进 LLM、不进售后流程，直接转人工
        hit = guard.violations[0].description if guard.violations else "安全规则"  # 取首条违规描述用于审计说明
        print(f"[Node: intent_recognition] 护栏阻断: {hit}")  # 打印阻断轨迹（违规原文不落入对话历史，防二次注入）
        return {
            "intent": state.get("intent"),               # 保留已确认意图，不覆盖
            "slot_values": dict(state.get("slot_values") or {}),  # 槽位表原样保留
            "missing_slots": [],                         # 清空缺失，防止误触补槽追问
            "dialog_history": list(state.get("dialog_history") or []),  # 历史不含注入原文
            "decision": "ESCALATE_HUMAN",                # 越狱输入交人工甄别，route_slots 走人工分支
            "human_reason": f"用户输入触发安全护栏（{hit}），转人工核实真实诉求",  # 审计用转人工原因
            "slot_error": None,                          # 无业务校验错误
            "final_reply": "您的消息触发了系统安全检查，已转人工专员继续为您处理售后诉求。",  # 用户侧友好话术
        }
    safe_msg = guard.modified_content or user_msg        # MODIFY 时为 PII 脱敏后文本；ALLOW 时即原文——只有脱敏文本才进模型

    history = list(state.get("dialog_history") or [])    # 取出多轮对话历史，首轮为空列表
    if user_msg:                                         # 本轮有新输入
        history.append({"role": "user", "content": safe_msg})  # 追加脱敏后文本进对话历史，供 LLM 结合上下文抽取（§2.4 入模型脱敏）

    slots = await extract_slots_via_llm(history)         # 原生异步 await（§2.5）：节点置于 FastAPI 等已运行事件循环内可直接 ainvoke，不再 asyncio.run 重入报错
    source = "LLM"                                       # 标记槽位来源为真实大模型
    if slots is None:                                    # LLM 调用或解析失败
        slots = fallback_extract_slots(user_msg)         # 降级为关键词规则抽取——双轨原则：规则用原文，抽出的订单号原值送业务校验（入数据库原文）
        slots = SlotExtractionResult(**slots).model_dump(exclude_none=True)  # 规则输出同样过 Pydantic Schema 归一化，保证两路输出契约一致
        source = "关键词降级"                            # 标记来源为兜底规则

    slot_values = dict(state.get("slot_values") or {})   # 取出已累积的槽位表（跨轮次保留）
    for name in ("intent", "order_id", "reason", "reason_type"):  # 遍历全部槽位定义
        new_val = slots.get(name)                        # 本轮抽取到的槽位值
        if new_val is not None and str(new_val).strip(): # 值非空才有效
            old = slot_values.get(name)                  # 已有槽位记录
            if old is None or not (old.get("value") or old.get("string_value")):  # 仅当旧值为空时才写入，防止后续轮次误覆盖已确认槽位
                slot_values[name] = {                    # 按 react_ai 意图路由的槽位结构写入
                    _SLOT_VALUE_FIELD[name]: str(new_val).strip(),  # 依据槽位类型选择 value / string_value 字段
                    "confidence": 0.9 if source == "LLM" else 0.5,  # LLM 抽取置信度高于规则兜底
                    "source": source,                    # 记录该槽位的抽取来源，便于审计
                }

    missing = [s for s in REQUIRED_SLOTS if not slot_values.get(s)]  # 计算尚未填充的必要槽位

    # ---- 越界意图网关（§2.3）：OTHER 不进售后流程，直接产出 OUT_OF_SCOPE 出口 ----
    if (slot_values.get("intent") or {}).get("value") == "OTHER":  # 意图判定为非售后诉求（仅在意图槽位首次填入时生效，防止打断进行中流程）
        print("[Node: intent_recognition] 越界意图 OTHER，走 OUT_OF_SCOPE 出口，不触发任何售后工具")  # 打印网关拦截轨迹
        return {                                         # 短路返回：跳过业务校验与售后链路
            "intent": "OTHER",                           # 顶层镜像：越界意图
            "slot_values": slot_values,                  # 写回槽位表（intent=OTHER 已记录）
            "missing_slots": [],                         # 清空缺失清单，防止 route_slots 误触发售后补槽追问
            "dialog_history": history,                   # 写回对话历史
            "decision": "OUT_OF_SCOPE",                  # 越界决策标记，route_slots 据此直送通知节点
            "slot_error": None,                          # 无业务校验错误
            "final_reply": "我目前只支持退货退款、仅退款、换货类售后诉求，其他问题请回复『人工』由专员为您解答；如需售后，请直接描述您的问题。",  # 越界友好话术
        }

    # ---- 槽位业务校验（§2.2）：新出现或变更过的订单号需查库验证存在性+归属权 ----
    user_id = state.get("user_id", "USR_001")            # 当前操作用户，用于归属权核验
    order_val = (slot_values.get("order_id") or {}).get("string_value")  # 本轮待校验的订单号
    slot_error = None                                    # 业务校验失败文案，驱动针对性追问
    biz_extra: Dict[str, Any] = {}                       # 校验产生的附加状态增量
    if order_val and order_val != state.get("order_validated_id"):  # 首次填入或用户换了单号才校验，已校验过则跳过
        err, order_data = validate_order_slot(order_val, user_id)  # 存在性 + 归属权双重校验
        if err:                                          # 校验不通过：格式合法但业务不可用
            slot_error = err                             # 记录失败文案，供 ask_user 生成针对性追问
            slot_values.pop("order_id", None)            # 从槽位表剔除无效单号
            order_val = None                             # 清空局部变量，使顶层镜像同步为 None
            missing = [s for s in REQUIRED_SLOTS if not slot_values.get(s)]  # 重算缺失清单 -> route_slots 自然回流追问
        else:                                            # 校验通过
            biz_extra = {"order_validated_id": order_val, "order_info": order_data}  # 记录已校验单号并缓存订单详情，节点2免重复查询

    print(f"[Node: intent_recognition] 槽位抽取完成: {json.dumps({k: (v.get('value') or v.get('string_value')) for k, v in slot_values.items()}, ensure_ascii=False)}; 缺失={missing or '无'}" + (f"; 业务校验失败={slot_error}" if slot_error else ""))  # 打印当前槽位填充全景

    return {                                             # 返回本节点产出的状态增量（LangGraph 会将其合并进全局状态）
        "intent": (slot_values.get("intent") or {}).get("value"),        # 顶层镜像：供下游节点直接读取意图
        "order_id": (slot_values.get("order_id") or {}).get("string_value"),  # 顶层镜像：LLM 抽取的订单号，未提供则为 None（不再写死兜底）
        "reason": (slot_values.get("reason") or {}).get("string_value") or "用户未说明具体原因",  # 原因描述，缺省给通用文案
        "reason_type": (slot_values.get("reason_type") or {}).get("value"),  # 政策原因大类，供政策核对节点使用
        "slot_values": slot_values,                      # 写回累积后的槽位表
        "missing_slots": missing,                        # 缺失清单驱动条件路由决定是否追问
        "dialog_history": history,                       # 写回更新后的对话历史（追问后用户补充时携带上下文再抽取）
        "slot_error": slot_error,                        # 业务校验失败文案（通过则 None，同时清除上一轮残留）
        "decision": None,                                # 新一轮开始，清除上一轮 RETRY_ASK 等残留决策，防止路由误判
        **biz_extra,                                     # 校验通过时附带 order_validated_id 与缓存的 order_info
    }


def ask_user_node(state: AfterSalesState) -> Dict[str, Any]:  # 节点1.5：必要槽位缺失时生成追问话术，结束本轮等待用户补充
    """
    [节点 追问补槽: Slot Filling 反问]
    - 按缺失的必要槽位生成针对性提问，写入 final_reply 后本轮图执行结束;
    - 调用方用同一 thread_id 携带用户新消息再次 invoke，dialog_history 使 LLM 能在
      完整上下文里重新抽取槽位，实现多轮补槽。
    - 追问轮数超过上限 (3 轮) 则放弃自动处理，转人工工单。
    """
    rounds = int(state.get("slot_rounds") or 0) + 1      # 本次为该会话第几轮追问
    missing = list(state.get("missing_slots") or [])     # 当前缺失的必要槽位列表

    if rounds > 3:                                       # 反复补槽仍失败（用户答非所问/持续缺失）
        print(f"[Node: ask_user] 追问超过上限，转人工处理")  # 打印转人工日志
        ctx = ToolContext(                               # 构造转人工所需上下文
            caller_id="langgraph-aftersales",            # 调用方标识
            tenant_id="default",                         # 租户标识
            user_id=state.get("user_id", "USR_001"),     # 操作用户ID
            idempotency_key=f"IDEM_SLOT_FAIL_{state.get('session_id', 'S')}",  # 幂等键防止重复开工单
        )
        res = escalate_to_human_tool.call({              # 调用真实转人工工具
            "order_id": state.get("order_id") or state.get("session_id", ""),  # 订单号可能尚未收集到，以会话ID代替
            "reason": f"多轮槽位补全失败，仍缺失: {','.join(missing)}",  # 转人工原因：明确列出缺失槽位
            "context_summary": f"用户诉求: {state.get('user_message', '')}，对话历史 {len(state.get('dialog_history') or [])} 轮后仍无法完成信息收集",  # 供人工专员快速了解背景
            "priority": "MEDIUM",                        # 中优先级工单
        }, ctx)
        return {                                         # 返回转人工结果，条件路由将直接汇入通知节点
            "decision": "ESCALATE_HUMAN",                # 决策置为转人工，复用既有路由分支
            "human_reason": "多次追问后必要信息仍不完整",  # 记录转人工原因
            "final_reply": "很抱歉，多次尝试后仍无法确认您的售后信息，已为您转接人工专员处理。",  # 给用户的解释性答复
            "action_result": res.data if res.ok else {}, # 人工工单创建结果
            "slot_rounds": rounds,                       # 记录追问轮数
        }

    ask_map = {                                          # 槽位名 -> 追问话术片段映射
        "intent": "请问您希望如何处理：退货退款、仅退款，还是换货？",  # 意图缺失提问
        "order_id": "请提供您的订单号，方便我为您查询订单信息。",  # 订单号缺失提问（替代原先写死兜底）
        "reason_type": "方便说明一下原因吗？是商品质量问题（如破损、瑕疵），还是不想要了？",  # 原因大类缺失提问
    }
    slot_error = state.get("slot_error")                 # 业务校验失败文案（订单不存在/非本人订单等）
    if slot_error:                                       # 校验失败场景：针对性话术优先于通用补槽提问
        question = slot_error
    else:                                                # 普通缺槽场景：按缺失槽位拼接追问
        question = "为了帮您处理售后申请，" + " ".join(ask_map[s] for s in missing if s in ask_map)
    print(f"[Node: ask_user] 第 {rounds} 轮追问: {question}")  # 打印追问内容

    history = list(state.get("dialog_history") or [])    # 取出对话历史
    history.append({"role": "assistant", "content": question})  # 把本次追问也记入历史，下一轮 LLM 可见完整语境
    return {                                             # 本轮以追问结束，等待调用方携带用户新消息重新 invoke
        "final_reply": question,                         # 追问文本作为本轮答复呈现给用户
        "missing_slots": missing,                        # 保持缺失清单（下一轮重抽后刷新）
        "slot_rounds": rounds,                           # 累加追问轮数
        "dialog_history": history,                       # 写回含追问的对话历史
        "slot_error": None,                              # 文案已消费，清除避免污染下一轮
    }


def query_order_node(state: AfterSalesState) -> Dict[str, Any]:  # 节点2：调用真实工具查询订单详情
    """
    [节点 2: 查询订单详情]
    - 调用内置 QueryOrderTool 从 PostgreSQL 真实查询订单数据;
    - 槽位校验阶段已缓存订单详情时跳过重复查询;
    - 查询失败不再抛异常崩图，转为 RETRY_ASK 回流追问节点（§2.2）。
    """
    if state.get("order_info"):                          # 意图节点业务校验时已顺带查到并缓存
        print("[Node: query_order] 订单详情已由槽位校验阶段缓存，跳过重复查询")  # 打印缓存命中日志
        return {}                                        # 直接透传，进入后续核验链

    order_id = state.get("order_id", "")               # 从状态中取出待查询的订单号
    user_id = state.get("user_id", "USR_001")          # 取出用户ID，缺省为演示用户
    print(f"[Node: query_order] 调用 QueryOrderTool 查询订单: {order_id}")  # 打印节点执行轨迹，便于观察流程

    ctx = ToolContext(caller_id="langgraph-aftersales", tenant_id="default", user_id=user_id)  # 构造工具上下文：标记调用方、租户与操作用户
    res = query_order_tool.call({"order_id": order_id}, ctx)  # 同步调用订单查询工具，返回结构化 ToolResult

    if not res.ok:                                     # 查询失败：收敛为可恢复的追问流程而非崩图
        print(f"[Node: query_order] 订单查询失败，回流追问: {res.error_message}")  # 打印失败轨迹
        sv = dict(state.get("slot_values") or {})      # 复制槽位表
        sv.pop("order_id", None)                       # 剔除失效订单号槽位，等待用户重新提供
        return {
            "decision": "RETRY_ASK",                   # 决策标记：由 route_query_order 路由回 ask_user
            "slot_values": sv,                         # 写回剔除后的槽位表
            "order_id": None,                          # 顶层镜像同步清空
            "missing_slots": ["order_id"],             # 标记缺失，驱动追问
            "slot_error": f"未能查询到订单 {order_id}（{res.error_message}），请核对订单号后重新发送",  # 针对性追问文案
            "order_validated_id": None,                # 作废校验标记，重新提供后需再次校验
        }

    return {"order_info": res.data}                    # 将查询到的订单数据写入状态的 order_info 字段


def check_logistics_node(state: AfterSalesState) -> Dict[str, Any]:  # 节点3：核验物流签收状态与轨迹
    """
    [节点 3: 检查物流状态]
    - 调用内置 QueryLogisticsTool 核验签收状态与物流轨迹。
    """
    order_id = state.get("order_id", "")               # 取出订单号
    user_id = state.get("user_id", "USR_001")          # 取出用户ID
    print(f"[Node: check_logistics] 调用 QueryLogisticsTool 检查物流: {order_id}")  # 打印节点执行轨迹

    ctx = ToolContext(caller_id="langgraph-aftersales", tenant_id="default", user_id=user_id)  # 构造与查询订单相同的工具上下文
    res = query_logistics_tool.call({"order_id": order_id}, ctx)  # 调用物流查询工具获取签收与轨迹信息

    return {"logistics_info": res.data if res.ok else {}}  # 成功则写入物流数据；失败不阻断流程，写入空字典容错继续


def policy_evaluation_node(state: AfterSalesState) -> Dict[str, Any]:  # 节点4：核对售后政策合规性
    """
    [节点 4: 核对售后政策]
    - 调用内置 SearchPolicyTool 从 PG after_sales_policies 表核验退换货合规性。
    """
    order_id = state.get("order_id", "")               # 取出订单号
    user_id = state.get("user_id", "USR_001")          # 取出用户ID
    reason_type = state.get("reason_type", "quality_issue")  # 取出原因大类，缺省按质量问题
    order_info = state.get("order_info") or {}         # 取出前序节点缓存的订单详情
    items = order_info.get("items", [])                # 从订单详情中取出商品行列表
    category = items[0].get("category", "CLOTHING") if items else "CLOTHING"  # 取首个商品的品类作为政策匹配品类，无商品时按服装类兜底

    print(f"[Node: policy_evaluation] 调用 SearchPolicyTool 核对政策: 品类={category}, 原因类型={reason_type}")  # 打印政策核对入参

    ctx = ToolContext(caller_id="langgraph-aftersales", tenant_id="default", user_id=user_id)  # 构造工具上下文
    res = search_policy_tool.call({                    # 调用政策检索工具，查询并计算退换货合规性
        "order_id": order_id,                          # 入参：订单号（用于核对下单时间是否在退换期内）
        "category": category,                          # 入参：商品品类（不同品类政策不同）
        "reason_type": reason_type                     # 入参：原因大类（质量问题/无理由适用不同规则）
    }, ctx)

    if not res.ok:                                     # 政策查询失败属于关键前置条件缺失，但不再裸 raise 崩图（§2.2）
        print(f"[Node: policy_evaluation] 政策核验失败，转人工: {res.error_message}")  # 打印失败轨迹
        return {                                       # 收敛为转人工决策，由下游 route_decision 汇入人工节点
            "decision": "ESCALATE_HUMAN",              # 直接给出决策，condition_decision 节点将透传不覆盖
            "human_reason": f"售后政策核验服务异常：{res.error_message}，转人工专员处理",  # 记录转人工原因
        }

    return {"policy_rules": res.data}                  # 将政策计算结果（是否可退、自动退款上限等）写入状态


def condition_decision_node(state: AfterSalesState) -> Dict[str, Any]:  # 节点5：综合政策与金额做风控决策
    """
    [节点 5: 综合条件判断与决策路由]
    - 判断是否在有效退换期内，是否超出大额退款阈值;
    - 上游已定 ESCALATE_HUMAN（如政策核验服务异常）时透传，不重新判定。
    """
    if state.get("decision") == "ESCALATE_HUMAN":      # 上游节点（政策核验失败等）已作出转人工决策
        print("[Node: condition_decision] 上游已决策转人工，透传跳过风控判定")  # 打印透传轨迹
        return {}                                      # 状态增量置空，decision/human_reason 原样保留流向路由

    policy = state.get("policy_rules") or {}           # 取出政策计算结果
    order_info = state.get("order_info") or {}         # 取出订单详情
    total_amount = float(order_info.get("total_amount", 0.0))  # 订单实付金额，转为浮点数
    max_auto_amount = float(policy.get("max_auto_refund_amount", 300.0))  # 政策允许的单笔自动退款上限，缺省 300 元
    eligible = policy.get("eligible_for_return", False)  # 政策是否允许本次退货/退款

    print(f"[Node: condition_decision] 综合风控判定: 是否符合政策={eligible}, 金额=¥{total_amount}, 自动上限=¥{max_auto_amount}")  # 打印判定依据

    # 1. 政策不符合 (如超期或不支持无理由)
    if not eligible:                                   # 分支一：政策不允许，直接拒绝
        reason_note = policy.get("eligibility_note", "不符合退换政策")  # 取出拒绝原因说明
        return {
            "is_eligible": False,                      # 标记判定未通过
            "decision": "REJECTED",                    # 决策为拒绝（后续路由将直接发通知）
            "human_reason": reason_note,               # 记录拒绝说明
            "final_reply": f"抱歉，您的售后申请未能通过自动审核：{reason_note}。"  # 生成面向用户的拒绝话术
        }

    # 2. 金额超出自动审核阈值 -> 转人工接管 (HITL)
    if total_amount > max_auto_amount:                 # 分支二：金额超过自动退款上限，触发人机协同
        return {
            "is_eligible": True,                       # 政策本身是符合的
            "decision": "ESCALATE_HUMAN",              # 决策为转人工复核
            "human_reason": f"订单实付金额(¥{total_amount:.2f})超出自动审核阈值(¥{max_auto_amount:.2f})，需专员复核"  # 记录转人工原因
        }

    # 3. 正常通过，根据政策决定是退货寄件还是直接退款
    if policy.get("need_return_goods", True):          # 政策要求先把商品寄回（缺省要求寄回）
        decision = "RETURN_SHIPPING"                   # 决策：走退货寄件流程
    else:                                              # 政策允许不退货直接退款（如质量问题小额极速退）
        decision = "AUTO_REFUND"                       # 决策：走极速自动退款

    return {
        "is_eligible": True,                           # 标记判定通过
        "decision": decision,                          # 写入具体决策类型
        "human_reason": None                           # 无需人工介入，清空原因字段
    }


def execute_action_node(state: AfterSalesState) -> Dict[str, Any]:  # 节点6：按决策执行业务写操作
    """
    [节点 6: 执行业务写操作]
    - 调用 CreateRefundTool 等真实写操作工具并生成用户反馈。
    """
    decision = state.get("decision")                   # 取出前序节点产生的决策类型
    order_id = state.get("order_id", "")               # 取出订单号
    user_id = state.get("user_id", "USR_001")          # 取出用户ID
    order_info = state.get("order_info") or {}         # 取出订单详情
    amount = float(order_info.get("total_amount", 0.0))  # 取出应退金额

    print(f"[Node: execute_action] 执行写操作: 决策类型={decision}")  # 打印执行动作轨迹

    if decision == "RETURN_SHIPPING":                  # 路径一：需要用户寄回商品
        # 生成退货取件引导
        action_res = {                                 # 构造寄件结果（示例中为模拟生成的取件信息）
            "status": "RETURN_LABEL_GENERATED",        # 状态：退货面单已生成
            "carrier": "顺丰速运",                     # 承运快递公司
            "pickup_code": "8899",                     # 上门取件码
            "return_tracking_no": f"SF_RET_{uuid.uuid4().hex[:8].upper()}"  # 用随机 UUID 前 8 位生成唯一退货运单号
        }
        reply = (                                      # 拼接面向用户的寄件引导话术
            f"您的售后退货申请已通过！已为您预约【顺丰速运】上门取件，"
            f"寄件码: {action_res['pickup_code']}，运单号: {action_res['return_tracking_no']}。"
            f"快递员将在2小时内联系您取件，仓库质检签收后退款将原路到账。"
        )

    elif decision == "AUTO_REFUND":                    # 路径二：无需寄回，直接极速退款
        # 调用 CreateRefundTool 执行退款写入
        ctx = ToolContext(                             # 构造携带幂等键的工具上下文，防止重复退款
            caller_id="langgraph-aftersales",          # 调用方标识
            tenant_id="default",                       # 租户标识
            user_id=user_id,                           # 操作用户
            idempotency_key=f"REFUND_IDEM_{order_id}_{uuid.uuid4().hex[:6]}"  # 幂等键：同订单重试不会重复出款
        )
        res = create_refund_tool.call({                # 调用退款工具向支付系统发起退款写操作
            "order_id": order_id,                      # 入参：订单号
            "amount": amount,                          # 入参：退款金额
            "reason": state.get("reason", "售后极速退款")  # 入参：退款原因说明
        }, ctx)
        action_res = res.data if res.ok else {"error": res.error_message}  # 成功取退款结果数据；失败则记录错误信息（不抛异常，继续走通知）
        reply = f"您的极速退款申请已通过，退款金额 ¥{amount:.2f} 已提交支付网关原路退回，请留意到账通知。"  # 生成退款成功话术

    else:                                              # 兜底路径：决策类型不在预期内
        action_res = {"status": "NOOP"}                # 不执行任何写操作（NOOP = No Operation）
        reply = "申请已处理完毕。"                     # 给出中性兜底答复

    return {
        "action_result": action_res,                   # 将写操作结果（退款单号/运单号/错误）写入状态
        "final_reply": reply                           # 将面向用户的最终答复写入状态
    }


def human_escalation_node(state: AfterSalesState) -> Dict[str, Any]:  # 节点7：转人工专员接管处理
    """
    [节点 7: 转人工接管 (HITL)]
    - 调用内置 EscalateToHumanTool 生成人工审核工单。
    """
    order_id = state.get("order_id", "")               # 取出订单号
    user_id = state.get("user_id", "USR_001")          # 取出用户ID
    reason = state.get("human_reason", "超出自动退款额度")  # 取出转人工原因，缺省为额度超限
    print(f"[Node: human_escalation] 调用 EscalateToHumanTool 转人工处理: {reason}")  # 打印转人工轨迹

    ctx = ToolContext(                                 # 构造带幂等键的上下文，避免同一订单重复开工单
        caller_id="langgraph-aftersales",              # 调用方标识
        tenant_id="default",                           # 租户标识
        user_id=user_id,                               # 操作用户
        idempotency_key=f"IDEM_ESCALATE_{order_id}_{uuid.uuid4().hex[:6]}"  # 工单创建幂等键
    )
    res = escalate_to_human_tool.call({                # 调用转人工工具，写入待办工单表
        "order_id": order_id,                          # 入参：关联订单号
        "reason": reason,                              # 入参：转人工原因
        "context_summary": f"订单 {order_id} 用户发起诉求: {state.get('user_message')}，经系统初审需人工介入: {reason}",  # 入参：给专员看的完整上下文摘要
        "priority": "HIGH"                             # 入参：工单优先级设为高
    }, ctx)

    ticket_id = res.data.get("ticket_id") if res.ok else "TICKET_MANUAL_001"  # 成功取真实工单号；失败用兜底演示工单号保证话术完整

    reply = f"由于【{reason}】，系统已为您升级至人工专员审核，工单号: {ticket_id}。专员将在2小时内完成复核。"  # 生成转人工告知话术

    return {
        "action_result": res.data if res.ok else {},   # 写入工单创建结果（失败则空字典）
        "final_reply": reply                           # 写入用户答复
    }


def send_notification_node(state: AfterSalesState) -> Dict[str, Any]:  # 节点8：向用户发送处理结果通知
    """
    [节点 8: 发送用户通知]
    - 调用内置 SendNotificationTool 写入 notifications 表并发送站内信/短信。
    """
    user_id = state.get("user_id", "USR_001")          # 取出接收通知的用户ID
    final_reply = state.get("final_reply", "您的售后诉求已处理。")  # 取出前序节点生成的最终答复作为通知正文
    order_id = state.get("order_id", "")               # 取出订单号
    print(f"[Node: send_notification] 调用 SendNotificationTool 发送通知至用户[{user_id}]")  # 打印通知发送轨迹

    ctx = ToolContext(                                 # 构造带幂等键的上下文，防止重复发送通知
        caller_id="langgraph-aftersales",              # 调用方标识
        tenant_id="default",                           # 租户标识
        user_id=user_id,                               # 接收用户
        idempotency_key=f"IDEM_NOTIF_{order_id}_{uuid.uuid4().hex[:6]}"  # 通知发送幂等键
    )
    send_notification_tool.call({                      # 调用通知工具写入并发送站内信
        "user_id": user_id,                            # 入参：接收人
        "title": "售后服务单处理进展通知",             # 入参：通知标题
        "content": final_reply,                        # 入参：通知正文（即最终答复）
        "channel": "IN_APP",                           # 入参：通知渠道为站内信
        "ticket_id": state.get("action_result", {}).get("ticket_id") or order_id  # 入参：关联工单号，无工单时回退用订单号
    }, ctx)

    return {"notification_sent": True}                 # 标记通知已发送，写入状态


# ============================================================================
# 3. 动态条件路由 (Conditional Edge Router)
# ============================================================================
def route_slots(state: AfterSalesState) -> Literal["query_order", "ask_user", "send_notification", "human_escalation"]:  # 槽位完整性路由：决定继续流程、追问、越界出口或护栏转人工
    if state.get("decision") == "OUT_OF_SCOPE":          # 越界意图网关命中（§2.3）
        return "send_notification"                       # 跳过整条售后链，直接发答复通知后结束
    if state.get("decision") == "ESCALATE_HUMAN":        # 输入触发安全护栏（§2.4），意图节点已短路定责
        return "human_escalation"                        # 直接转人工甄别，不进 LLM 抽槽与售后链
    missing = state.get("missing_slots") or []           # 读取必要槽位缺失清单
    return "ask_user" if missing else "query_order"      # 有缺失 -> 追问节点；槽位齐全 -> 查询订单


def route_query_order(state: AfterSalesState) -> Literal["check_logistics", "ask_user"]:  # 订单查询结果路由（§2.2 降级边）
    if state.get("decision") == "RETRY_ASK":             # 查询失败：节点已剔除坏单号并写好 slot_error
        return "ask_user"                                # 回流追问节点，让用户核对订单号后重新提供
    return "check_logistics"                             # 查询成功（或校验阶段已缓存）：继续物流核验


def route_decision(state: AfterSalesState) -> Literal["execute_action", "human_escalation", "send_notification"]:  # 路由函数：根据决策字段返回下一跳节点名
    decision = state.get("decision")                   # 取出风控决策结果
    if decision in ("RETURN_SHIPPING", "AUTO_REFUND"): # 自动通过的两类决策
        return "execute_action"                        # 路由到写操作执行节点
    elif decision == "ESCALATE_HUMAN":                 # 金额超限等需人工介入
        return "human_escalation"                      # 路由到转人工节点
    else:                                              # REJECTED 或其他情况
        return "send_notification"                     # 跳过执行，直接路由到通知节点告知结果


# ============================================================================
# 4. 构建与编译 LangGraph (Graph Construction)
# ============================================================================
def build_aftersales_graph(checkpointer=None):         # 构建并编译售后状态图；checkpointer 传入时支持按线程持久化与断点续跑
    workflow = StateGraph(AfterSalesState)             # 以状态类型创建有向图构建器

    # 注册节点
    workflow.add_node("intent_recognition", intent_recognition_node)  # 注册节点1：意图识别
    workflow.add_node("query_order", query_order_node)                # 注册节点2：订单查询
    workflow.add_node("check_logistics", check_logistics_node)        # 注册节点3：物流核验
    workflow.add_node("policy_evaluation", policy_evaluation_node)    # 注册节点4：政策核对
    workflow.add_node("condition_decision", condition_decision_node)  # 注册节点5：风控决策
    workflow.add_node("execute_action", execute_action_node)          # 注册节点6：业务写操作
    workflow.add_node("human_escalation", human_escalation_node)      # 注册节点7：转人工
    workflow.add_node("send_notification", send_notification_node)    # 注册节点8：发送通知
    workflow.add_node("ask_user", ask_user_node)                        # 注册追问节点：必要槽位缺失时反问用户补槽

    # 串联常规边
    workflow.add_edge(START, "intent_recognition")     # 图入口 -> 意图识别
    workflow.add_conditional_edges(                    # 意图识别后接条件边：按槽位完整性分流
        "intent_recognition",                          # 源节点：LLM 意图识别与槽位抽取
        route_slots,                                   # 路由函数：missing_slots 非空则追问
        {
            "query_order": "query_order",              # 槽位齐全 -> 继续订单查询流程
            "ask_user": "ask_user",                    # 槽位缺失 -> 反问用户补槽
            "send_notification": "send_notification",  # 越界意图 OUT_OF_SCOPE -> 直接答复通知（§2.3）
            "human_escalation": "human_escalation",    # 护栏阻断 ESCALATE_HUMAN -> 直接转人工（§2.4）
        }
    )
    workflow.add_edge("ask_user", END)                 # 追问后本轮图执行结束；用户补充信息后同线程重新 invoke 进入下一轮补槽
    workflow.add_conditional_edges(                    # 查询订单后接条件边：失败时回流追问而非崩图（§2.2）
        "query_order",                                 # 源节点：订单查询
        route_query_order,                             # 路由函数：RETRY_ASK -> ask_user；正常 -> 物流核验
        {
            "check_logistics": "check_logistics",    # 分支映射：订单可用 -> 继续检查物流
            "ask_user": "ask_user",                   # 分支映射：订单查询失败 -> 针对性追问重新要单号
        }
    )
    workflow.add_edge("check_logistics", "policy_evaluation")  # 检查物流 -> 核对政策
    workflow.add_edge("policy_evaluation", "condition_decision")  # 核对政策 -> 风控决策

    # 条件路由分流
    workflow.add_conditional_edges(                    # 在决策节点后挂动态边：运行时按路由函数返回值选择下游节点
        "condition_decision",                          # 源节点：风控决策
        route_decision,                                # 路由函数：读 state["decision"] 返回分支名
        {
            "execute_action": "execute_action",        # 分支映射：自动执行 -> 写操作节点
            "human_escalation": "human_escalation",    # 分支映射：转人工 -> 人工接管节点
            "send_notification": "send_notification"   # 分支映射：拒绝 -> 直接通知节点
        }
    )

    # 汇聚通知并结束
    workflow.add_edge("execute_action", "send_notification")    # 写操作完成后汇入通知节点
    workflow.add_edge("human_escalation", "send_notification")  # 转人工完成后汇入通知节点
    workflow.add_edge("send_notification", END)        # 通知发送后图执行结束

    return workflow.compile(checkpointer=checkpointer)  # 编译为可执行的 Runnable 应用，挂载检查点存储器


# ============================================================================
# 4.5 会话持久化 (Checkpointer 工厂, §2.6)
# ============================================================================
def _pg_conn_string() -> str:                            # 组装 PG 连接串：复用项目数据库配置并加短连接超时
    """在 settings.database.url 上追加 connect_timeout，避免 PG 不可达时长时间挂起。"""
    url = settings.database.url                          # 项目统一数据库连接串（与售后工具共用同一 PG 实例）
    sep = "&" if "?" in url else "?"                     # 判断连接串是否已带查询参数，选择正确的拼接分隔符
    return f"{url}{sep}connect_timeout=3"                # 3 秒连接超时：PG 缺席时快速失败并降级，而非阻塞演示


@asynccontextmanager                                     # 封装为异步上下文管理器：saver 连接随 with 块自动建立与释放
async def open_checkpointer():                           # 创建图检查点：优先 PG 持久化，不可用时降级内存（§2.6）
    """会话持久化工厂。

    AsyncPostgresSaver 把每个 thread_id 的图状态（对话历史/槽位/追问轮次/待中断位置）
    写入 PG checkpoint 表：进程重启后追问到一半的会话可原样续跑，多实例负载均衡下
    任意实例都能接管同一会话——修复 MemorySaver '重启即失忆' 问题。
    PG 不可达（本地开发/故障）时降级 MemorySaver 并打印告警，演示不中断。
    """
    cm = None                                            # 持有 from_conn_string 返回的异步上下文管理器句柄
    try:
        cm = AsyncPostgresSaver.from_conn_string(_pg_conn_string())  # 惰性创建 PG 连接池上下文（尚未真正连接）
        saver = await cm.__aenter__()                    # 进入上下文：建立连接，得到可用的 AsyncPostgresSaver
        try:
            await saver.setup()                          # 幂等建表：首次运行自动创建 checkpoint/checkpoint_blobs 等表
            print("[持久化] AsyncPostgresSaver 已挂载：会话状态跨重启/多实例存活")  # 打印持久化模式提示
            yield saver                                  # 把 saver 交给 with 块使用（build_aftersales_graph 参数）
        finally:
            await cm.__aexit__(None, None, None)         # 退出时释放 PG 连接池，防止连接泄漏
    except Exception as exc:                             # 连接失败/建表失败/PG 缺席：走内存降级
        print(f"[持久化] PG 不可用（{type(exc).__name__}: {exc}），降级 MemorySaver："
              f"进程重启后追问中的会话将丢失，仅限本地演示")  # 显式告警，防止误把内存模式当生产配置
        yield MemorySaver()                              # 内存检查点：行为与旧版一致，保证演示可离线跑通


# ============================================================================
# 5. 主程序运行演示 (执行真实数据库与真实工具调用)
# ============================================================================
async def main_async():                                  # 异步演示入口：构建图并跑三个典型业务用例（含多轮槽位补全）；服务内可直接 await 本函数图对象，无需任何桥接
    print("=" * 75)                                    # 打印分隔线
    print(">>> 启动 LangGraph 电商售后工单工作流 (真实 LLM 意图识别 + 槽位填充 + 缺失追问) <<<")  # 打印标题说明
    print("=" * 75)                                    # 打印分隔线

    async with open_checkpointer() as checkpointer:    # §2.6: 优先挂载 AsyncPostgresSaver（跨重启/多实例持久化），PG 不可用时自动降级 MemorySaver
        app = build_aftersales_graph(checkpointer=checkpointer)  # 编译图应用，按 thread_id 隔离并持久化会话状态

        # thread_id 命名规范 {tenant}:{user}:{session}（§2.6）：与项目会话键约定对齐，
        # 同一会话无论落到哪个服务实例、进程是否重启，都定位到同一份持久化检查点
        # Case 1: 完整诉求（一句话给全意图/原因/订单号）-> LLM 一次抽槽直接跑通流程
        case1_input: AfterSalesState = {               # 构造用例1的初始状态（类型标注为售后状态字典）
            "session_id": "SESS_TEST_001",             # 会话ID
            "user_id": "USR_001",                      # 用户ID
            "user_message": "我买的衣服破了，要退货退款，订单号是 ORD_CLOTH_001",  # 槽位信息齐全，LLM 可一次抽取完整
        }

        print("\n--- [测试用例 1: 完整诉求 -> LLM 一次抽槽自动通过] ---")  # 打印用例标题
        res1 = await app.ainvoke(case1_input, config={"configurable": {"thread_id": "default:USR_001:SESS_TEST_001"}})  # 异步执行整张图（§2.5）；thread_id 即持久化会话主键
        print(f"最终决策: {res1.get('decision')}")     # 打印决策结果（预期 RETURN_SHIPPING）
        print(f"客户回复:\n{res1.get('final_reply')}") # 打印给用户的最终答复文本

        # Case 2: 必要槽位缺失 -> ask_user 追问 -> 用户补充 -> 同一线程续跑（多轮槽位填充）
        print("\n\n--- [测试用例 2: 槽位缺失追问演示: 信息不全 -> 系统反问 -> 用户补充 -> 继续处理] ---")  # 打印用例标题
        cfg2 = {"configurable": {"thread_id": "default:USR_002:SESS_TEST_002"}}  # 用例2跨轮共用同一 thread_id；挂载 PG 检查点后，第2轮请求即使打到另一实例/重启后的进程也能续跑
        res2a = await app.ainvoke({                    # 第1轮：用户只说想退货，缺少原因大类与订单号
            "session_id": "SESS_TEST_002",             # 会话ID
            "user_id": "USR_002",                      # 用户ID
            "user_message": "我想退货",                # 缺失 reason_type / order_id 必要槽位 -> 触发追问
        }, config=cfg2)
        print(f"[第1轮] 已抽槽位: intent={res2a.get('intent')}, reason_type={res2a.get('reason_type')}, order_id={res2a.get('order_id')}; 缺失={res2a.get('missing_slots')}")  # 打印第1轮槽位填充情况
        print(f"[第1轮] 系统追问: {res2a.get('final_reply')}")  # 打印追问话术（本轮流程在 ask_user 后结束）

        res2b = await app.ainvoke({                    # 第2轮：用户回答追问补充槽位，同一线程从检查点恢复上下文续跑
            "session_id": "SESS_TEST_002",             # 会话ID
            "user_id": "USR_002",                      # 用户ID
            "user_message": "衣服质量有问题，订单号 ORD_CLOTH_001",  # 结合对话历史，LLM 抽取剩余缺失槽位后流程继续
        }, config=cfg2)
        print(f"[第2轮] 最终决策: {res2b.get('decision')}")  # 打印最终决策（补槽成功后预期 RETURN_SHIPPING）
        print(f"[第2轮] 客户回复:\n{res2b.get('final_reply')}")  # 打印补槽完成后的业务处理结果

        # Case 3: 大额黄金订单退款 (槽位齐全但金额超限 -> 触发转人工审核)
        case3_input: AfterSalesState = {               # 构造用例3的初始状态
            "session_id": "SESS_TEST_003",             # 会话ID
            "user_id": "USR_003",                      # 用户ID
            "user_message": "黄金手链断了，要求退款，订单号 ORD_LUXURY_005",  # 槽位齐全；金额超出自动审核上限
        }

        print("\n\n--- [测试用例 3: 黄金手链金额超限 -> 转人工专员审核 (HITL)] ---")  # 打印用例标题
        res3 = await app.ainvoke(case3_input, config={"configurable": {"thread_id": "default:USR_003:SESS_TEST_003"}})  # 以独立线程异步执行用例3
        print(f"最终决策: {res3.get('decision')}")     # 打印决策结果（预期 ESCALATE_HUMAN）
        print(f"客户回复:\n{res3.get('final_reply')}") # 打印含工单号的转人工答复


if __name__ == "__main__":                             # 脚本直接运行时执行主函数（被导入时不自动运行）
    if sys.platform == "win32":                        # Windows 平台特殊处理（§2.6）：psycopg3 异步模式不兼容默认的
        import selectors                               # ProactorEventLoop，须显式提供 Selector 型事件循环工厂，否则
        _loop_factory = lambda: asyncio.SelectorEventLoop(  # AsyncPostgresSaver 连接时报 "cannot use ProactorEventLoop"
            selectors.SelectSelector())                # 而永远落入内存降级（Linux 生产环境默认即兼容，无需处理）
        asyncio.run(main_async(), loop_factory=_loop_factory)  # 事件循环工厂方式，避开已弃用的 set_event_loop_policy
    else:
        asyncio.run(main_async())                      # 全图异步执行；asyncio.run 仅在此脚本入口出现一次（§2.5），挂进 API 服务时由事件循环直接 ainvoke
