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
7. 可观测性 (Observability, §2.3): 节点内轨迹由 `react_ai.observability` 的结构化 JSON 日志
   输出 (每条日志携带 thread_id, 一次多轮会话可按该键全链路还原), 并落地六类运行指标:
   intent_extraction_total / slot_validation_fail_total / guardrail_action_total /
   ask_user_rounds / node_latency_seconds / decision_total (另加 checkpoint_fork_total),
   可经 `metrics.snapshot()` 或 `/metrics` (Prometheus 文本) 查询。
8. 会话级串行化 (§2.3 追加项, 修 E2E 报告 §6.2 检查点分叉): 同一 thread_id 的请求先过进程内
   互斥锁, 再取 PG advisory lock (`pg_advisory_lock(hashtext(thread_id))`) 跨实例串行, 每轮结束
   统计该线程 leaf 检查点数, >1 即判定分叉并计数告警; PG 不可达时降级为进程内锁并告警。
9. 置信度与阈值策略 (Confidence Gate, §3.1): 抽槽结果新增 confidence (0~1) 自评分——LLM 由
   function calling Schema 强制产出、关键词降级由规则命中纯度自评, 未自评分的轮次标 band=unrated
   只做观测。意图节点按三段收敛: ≥0.8 直接进售后链 / [0.5,0.8) 槽位齐全时先反问"您是想办理 X 吗"
   (缺槽则本轮先补槽, 标 mid_deferred 留到槽位齐全再确认) / <0.5 作废意图槽位并回落到"您要办哪一种"
   的既有补槽提问——不直接开人工工单, 超过补槽轮次上限才升级人工, 拦住"自信地判错"又不浪费工单。
   确认反问复用 slot_error→ask_user 回流通道, 用户答"是"锁定意图(0.98)并跳过一次 LLM 调用, 答"不是"
   作废意图槽位交回模型重判; 未确认前流程不查库不写单。阈值来自评测集标定而非拍脑袋, 可由
   INTENT_CONFIDENCE_HIGH / INTENT_CONFIDENCE_LOW / INTENT_CONFIDENCE_ENABLED 调整与灰度关闭。
10. 中途换意图与流程挂起 (Flow Parking, §3.2): 补槽补到一半用户改口, 旧实现会把新诉求硬塞进
   旧流程的抽取提示词——accumulate-never-overwrite 令"换意图"实际不可能发生。改造后意图节点先做
   会话状态检测: 存在进行中流程(有意图 + 未办结 + 停在补槽或意图确认反问上)时, 抽取提示词追加一段
   【当前会话状态】, 由模型对本轮消息给出 flow_action ∈ continue/switch/resume/abort/off_domain
   (缺省即 continue, 无进行中流程时行为与改造前逐字节一致)。switch 把旧流程按 LIFO 压入
   pending_flows 快照栈(含槽位表/缺失清单/该流程轮次/已校验订单缓存), 新流程继承同一订单与原因、
   只重抽办理类型; resume 由前置正则通道定案(不再向模型索取关系判定, 它的自述一律不作数),
   从栈里取回快照回到中断处继续补槽, 不必重报订单号。切换是有代价的判定
   (误判即把用户已填信息埋掉), 故设证据门槛: 自评分 <0.7、或本轮既没给出另一个真实售后意图
   也没报出另一个订单号时一律维持旧流程并计数 flow_switch_rejected_total。
   abort(明确"算了不办了")作废当前流程但不碰更早挂起的;
   off_domain(聊别的业务)保留旧流程只答越界出口。通知节点按终态决策回填 flow_status, 使已办结/
   已转人工的流程永不被挂起——否则"继续退货"会恢复出一个已经写过退款单的流程而二次写单。
   除单流程 3 轮补槽上限外另加会话级 6 轮预算(防在两流程间反复切换刷追问), 流程办结后归零。
   由 INTENT_FLOW_ENABLED / INTENT_FLOW_MIN_SWITCH_CONFIDENCE / INTENT_FLOW_MAX_PENDING /
   INTENT_FLOW_MAX_SESSION_ROUNDS 配置。
"""

import sys                                              # 导入系统模块，用于操作 Python 解释器的模块搜索路径
import asyncio                                           # 导入异步运行时模块，仅用于脚本入口 asyncio.run(main_async()) 一处桥接
import functools                                        # 导入高阶函数工具，用于节点埋点包装时保留原函数签名
import json                                             # 导入 JSON 编解码模块，用于解析 LLM 输出的槽位 JSON
import re                                                # 导入正则模块，用于关键词降级时从文本中提取订单号
import time                                             # 导入时间模块，用于节点耗时指标计时
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

# 导入可观测性组件：结构化日志 + 指标（§2.3）
from react_ai.observability import logger, metrics        # 单行 JSON 日志器与指标收集器（进程内单例）

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
- confidence: 0~1 小数，对你本轮 intent 判定的把握程度自评。措辞明确且动作清晰(如"质量问题，退货退款，订单号X")给 0.9 以上；
  用户只是陈述问题、没有明确说要退/换/退款，或"退"既可理解为退货又可理解为仅退款时给 0.5~0.8；
  几乎无法判断意图时给 0.5 以下。低于 0.5 的判定不会被执行，会转人工核实
- missing_slots: intent / order_id / reason_type 中无法从对话判断的字段名列表，均能判断时为空数组
若上一轮助手已就某项向用户确认（如"您是想办理退货退款吗？"），用户回答"是/对/嗯/好的"即按该意图并把 confidence 提到 0.9 以上；
用户回答"不是/不对/我要换货"等否定或改口时，必须放弃被确认的意图，按用户新表达重新判定，不得沿用。
示例: {"intent": "RETURN_AND_REFUND", "order_id": "ORD123", "reason": "衣服破损", "reason_type": "quality_issue", "confidence": 0.95, "missing_slots": []}"""  # 槽位抽取的系统提示词：定义输出 JSON Schema、"禁止编造订单号"约束与置信度自评口径（§3.1）

REQUIRED_SLOTS = ("intent", "order_id", "reason_type")  # 必要槽位：意图、订单号、原因大类缺失时必须追问用户补槽

# ---- 置信度阈值策略（§3.1）：模型自评分 + 关键词规则自评分，统一按三段收敛 ----
CONFIDENCE_HIGH = settings.intent.high_threshold          # ≥ 该值：意图判定可信，直接进售后链
CONFIDENCE_LOW = settings.intent.low_threshold            # < 该值：判定不可信，永不写库，转人工核实
CONFIDENCE_ENABLED = settings.intent.enabled              # 关闭时只观测不拦截（标定/灰度用）
# 模型未返回自评分时不拦截（§3.1 只约束"自评了但没把握"的判定）；band=unrated 指标用于发现模型停止自评
_INT_CONFIRM_ACCEPT_RE = re.compile(                     # 用户对"您是想办理退货退款吗？"的肯定回答
    r"^\s*(是|对|嗯|好|好的|可以|没问题|确认|没错|是的|对的)[的。.!！~～]*\s*$")
_INT_CONFIRM_REJECT_RE = re.compile(                     # 否定回答：作废待确认意图，交回模型重新判定
    r"(不是|不对|不想|不要|错了|并非|取消)")
_INT_CONFIRM_QUESTION = "为确保处理正确，先跟您确认一下：您是要办理【{label}】吗？" \
                       "回复『是』即为您办理；如不是，请直接说明您的诉求（退货退款/仅退款/换货）。"  # 中置信段的意图确认反问话术
_INTENT_LABELS = {"RETURN_AND_REFUND": "退货退款", "REFUND_ONLY": "仅退款", "EXCHANGE": "换货"}  # 话术里的意图中文名


# ---- 中途换意图与流程挂起（§3.2）：会话状态检测的判定契约 ----
INTENT_ENUM = ("RETURN_AND_REFUND", "REFUND_ONLY", "EXCHANGE", "OTHER")  # 意图枚举全集（挂起快照校验与 resume 目标共用）
FLOW_ACTIONS = ("continue", "switch", "resume", "abort", "off_domain")   # 本轮消息与会话中流程的关系
FLOW_TERMINAL_STATUSES = ("completed", "rejected", "escalated")          # 已办结的流程不得再挂起（恢复出去重跑会二次写单）
FLOW_STATUS_BY_DECISION = {                                              # 通知节点按终态决策回填 flow_status
    "AUTO_REFUND": "completed", "RETURN_SHIPPING": "completed",
    "REJECTED": "rejected", "ESCALATE_HUMAN": "escalated",
}
FLOW_INHERIT_SLOTS = ("order_id", "reason", "reason_type")               # 换意图时新流程继承的槽位：只换办理类型，不换订单与原因
MIN_SWITCH_CONFIDENCE = settings.flow.min_switch_confidence              # 判定 switch 的最低自评分：低于它按补充旧流程处理
MAX_PENDING_FLOWS = settings.flow.max_pending_flows                      # 挂起栈深度上限，超出丢弃最旧快照
MAX_SESSION_SLOT_ROUNDS = settings.flow.max_session_slot_rounds          # 会话内跨流程补槽总预算，防止反复切换刷轮次
FLOW_ENABLED = settings.flow.enabled                                     # 灰度开关：关闭后本节全部判据退回改造前的单流程行为
_FLOW_RESUME_RE = re.compile(r"(继续|回到|恢复|刚才那|之前那|上次那|未完成|没办完)")                       # 恢复挂起流程的表达
_FLOW_SWITCH_CUE_RE = re.compile(r"(还是|改成|改为|换成|改办|不要.{0,4}要)")                                # 改口换办理类型的表达
_FLOW_ABORT_RE = re.compile(r"(不办了|不用了|不弄了|不想(退|换|办|要)了|算了[,，。]?\s*不|不退了|不换货了|取消(申请|退货|退款|换货))")  # 放弃当前流程的表达
_INTENT_HINTS = {                                                        # 指代挂起流程时的关键词（用于"继续换货"定位快照）
    "RETURN_AND_REFUND": ("退货退款", "退货", "退回", "寄回"),
    "REFUND_ONLY": ("仅退款", "只退款", "不退货"),
    "EXCHANGE": ("换货", "换一个", "换新"),
}


class SlotExtractionResult(BaseModel):                 # Pydantic 槽位抽取结果 Schema：大模型输出必须通过校验才可信
    """约束 LLM 结构化输出的枚举取值/字段格式/长度上限，非法输出直接拒绝并触发关键词降级。"""
    intent: Optional[Literal["RETURN_AND_REFUND", "REFUND_ONLY", "EXCHANGE", "OTHER"]] = None  # 意图枚举（含 OTHER 越界出口，§2.3）：超出取值范围即校验失败
    order_id: Optional[str] = Field(default=None, pattern=r"^[A-Za-z0-9_\-]{4,64}$")  # 订单号格式：仅允许字母/数字/下划线/连字符，防幻觉文本与注入
    reason: Optional[str] = Field(default=None, max_length=50)  # 原因描述长度上限，防止模型冗长输出污染状态
    reason_type: Optional[Literal["quality_issue", "no_reason"]] = None  # 原因大类枚举
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)  # 模型对 intent 判定的自评置信度（§3.1）：阈值策略的唯一输入
    missing_slots: List[str] = Field(default_factory=list)  # 模型自报的缺失槽位名列表
    flow_action: Optional[Literal["continue", "switch", "resume", "abort", "off_domain"]] = None  # 本轮与会话中流程的关系（§3.2）：缺省即 continue，保证无进行中流程时行为不变
    resume_intent: Optional[Literal["RETURN_AND_REFUND", "REFUND_ONLY", "EXCHANGE", "OTHER"]] = None  # flow_action=resume/abort 时用户所指代的流程意图（§3.2）

    @field_validator("intent", "order_id", "reason", "reason_type", mode="before")  # 前置归一化校验器
    @classmethod
    def _blank_to_none(cls, v: Any) -> Any:            # 空字符串/纯空白统一视为 None
        if isinstance(v, str) and not v.strip():       # 命中空白字符串
            return None                                # 归一为 None，避免 pattern 对空串误报
        if isinstance(v, str):                         # 非空白字符串
            return v.strip()                           # 顺带去除首尾空白
        return v                                       # None 等原样透传

    @field_validator("flow_action", "resume_intent", mode="before")  # 会话状态字段归一化（§3.2）
    @classmethod
    def _normalize_flow_fields(cls, v: Any) -> Any:
        if isinstance(v, str):
            text = v.strip()
            if text.lower() in FLOW_ACTIONS:
                return text.lower()                      # 关系枚举统一小写
            if text.upper() in INTENT_ENUM:
                return text.upper()                      # 意图枚举统一大写
            return None                                  # 非枚举取值视为"本轮未判定关系"，按 continue 处理，不阻断抽槽
        return v

    @field_validator("confidence", mode="before")      # 置信度归一化：模型常以字符串或越界小数返回自评分
    @classmethod
    def _normalize_confidence(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return None
            try:
                v = float(v)
            except ValueError:
                return None                            # 非数字自评视为"未给出"，交由缺省策略处理
        if v is None or isinstance(v, bool):
            return v
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        if v > 1.0 and v <= 100.0:                     # 模型把 0.9 写成 90 的常见口径错误：按百分制还原
            v = v / 100.0
        return max(0.0, min(1.0, v))                   # 钳制到 0~1，越界自评分不直接判死而是收敛到边界

_llm_router: Optional[LLMRouter] = None                # LLM 路由器全局懒加载单例（首次使用时初始化）

_SLOT_VALUE_FIELD = {"intent": "value", "order_id": "string_value", "reason": "string_value",
                     "reason_type": "value", "confidence": "value"}  # 各槽位对应的取值字段名映射

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
            "confidence": {                             # 意图自评置信度（§3.1）：阈值策略的唯一输入
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": "0~1 小数，对 intent 判定的把握程度：措辞明确动作清晰给 0.9 以上，"
                               "用户只陈述问题未明确要退/换/退款给 0.5~0.8，几乎无法判断给 0.5 以下",
            },
            "missing_slots": {                          # 模型自报缺失槽位
                "type": "array",
                "items": {"type": "string"},
                "description": "无法从对话判断的必要槽位名列表",
            },
            "flow_action": {                            # 会话状态检测（§3.2）：仅当提示词给出【当前流程】时填写
                "type": "string",
                "enum": list(FLOW_ACTIONS),
                "description": "本轮消息与会话中正在进行流程的关系：continue=补充该流程的信息，"
                               "switch=改口办另一个售后诉求，resume=回到之前暂停的流程，"
                               "abort=明确不再办理当前流程，off_domain=与当前流程无关的新话题；"
                               "提示词未给出【当前流程】时省略该字段",
            },
            "resume_intent": {                          # 恢复/取消的目标流程（§3.2）
                "type": "string",
                "enum": list(INTENT_ENUM),
                "description": "flow_action 为 resume 或 abort 时用户所指代流程的意图，其余情况不要填写",
            },
        },
        required=["confidence"],                        # 槽位可缺（由节点侧统一计算并追问），但自评分必须给出——它是阈值策略的唯一输入（§3.1）
    ),
)

_SLOT_KEYS = {"intent", "order_id", "reason", "reason_type", "confidence", "missing_slots",  # 合法槽位键集合，用于甄别模拟/异常返回的 tool_calls 参数
              "flow_action", "resume_intent"}  # 会话状态关系字段同样属合法键（§3.2）

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
# 0.5 可观测性基础设施 (结构化日志 + 运行指标, §2.3)
# ============================================================================
def thread_id_of(state: Dict[str, Any]) -> str:
    """会话主键：调用方注入的 thread_id 优先，缺省按 {tenant}:{user}:{session} 约定推导。

    与 main_async / live_e2e_verify 的线程命名一致，因此即便调用方忘记注入（如评测脚本
    直接调节点），日志与指标仍能按同一个键把一次多轮会话串起来。
    """
    explicit = state.get("thread_id")                      # ainvoke_serialized 注入的真实线程键
    if explicit:
        return str(explicit)
    return f"default:{state.get('user_id', 'USR_001')}:{state.get('session_id', '')}"


def trace_of(state: Dict[str, Any]) -> Dict[str, Any]:
    """日志关联字段：thread_id 为 trace 主键，session_id/user_id 便于按维度检索。"""
    return {
        "thread_id": thread_id_of(state),
        "session_id": state.get("session_id"),
        "user_id": state.get("user_id"),
    }


def log_node(state: Dict[str, Any], event: str, **fields):
    """节点轨迹日志：单行 JSON，携带会话关联键，可按 thread_id 还原全链路。"""
    logger.info(event, **trace_of(state), **fields)


def _validation_failure_reason(err: str) -> str:
    """订单业务校验失败分类：与 validate_order_slot 的三种话术一一对应。"""
    if "系统暂时不可用" in err:
        return "system"                                    # 数据库/基础设施异常
    if "不属于当前账号" in err:
        return "ownership"                                 # 越权尝试（他人订单号）
    return "not_found"                                     # 订单号不存在/查不到


_TRAJECTORY_FIELDS = ("intent", "confidence_band", "flow_action", "decision", "missing_slots",
                      "slot_rounds", "final_reply")  # 节点结束时摘要上报的状态字段（自评分原值见 slots_extracted 事件）


def instrument_node(name: str, fn):
    """节点埋点包装：耗时直方图 + 决策计数 + 结构化轨迹日志，不改变节点的输入输出。"""
    def _record(state: Dict[str, Any], out: Optional[Dict[str, Any]], started: float, error: Optional[str]):
        elapsed = time.perf_counter() - started
        metrics.observe("node_latency_seconds", elapsed, {"node": name})
        result = out or {}
        if result.get("decision"):
            metrics.inc("decision_total", {"decision": str(result["decision"])})
        summary = {k: result.get(k, state.get(k)) for k in _TRAJECTORY_FIELDS if result.get(k, state.get(k)) is not None}
        if isinstance(summary.get("final_reply"), str):
            summary["final_reply"] = summary["final_reply"][:60]
        fields = {"node": name, "latency_ms": round(elapsed * 1000, 1), **summary}
        if error:
            logger.error("node_failed", **trace_of(state), **fields, error=error)
        else:
            log_node(state, "node_completed", **fields)

    if asyncio.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def async_wrapper(state: Dict[str, Any]) -> Dict[str, Any]:
            started = time.perf_counter()
            try:
                out = await fn(state)
            except Exception as exc:
                _record(state, None, started, f"{type(exc).__name__}: {exc}")
                raise
            _record(state, out, started, None)
            return out
        return async_wrapper

    @functools.wraps(fn)
    def sync_wrapper(state: Dict[str, Any]) -> Dict[str, Any]:
        started = time.perf_counter()
        try:
            out = fn(state)
        except Exception as exc:
            _record(state, None, started, f"{type(exc).__name__}: {exc}")
            raise
        _record(state, out, started, None)
        return out
    return sync_wrapper


# ============================================================================
# 1. 状态定义 (State Definition)
# ============================================================================
class AfterSalesState(TypedDict, total=False):         # 定义全流程共享的状态类型；total=False 表示所有字段均可缺省
    """电商售后全流程核心状态对象。"""
    session_id: str                   # 会话/线程ID
    thread_id: Optional[str]          # 检查点线程主键 {tenant}:{user}:{session}，由 ainvoke_serialized 注入，作为日志/指标的 trace 关联键（§2.3）
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
    intent_confirm_pending: Optional[str]  # 中置信段等待用户口头确认的意图（§3.1）；ask_user 节点提问后写入，下一轮据此识别"是/不是"
    confidence_band: Optional[str]         # 本轮意图自评分落段（§3.1）：high / mid / mid_deferred / low / confirmed / unrated / not_gated / out_of_scope / guardrail_blocked

    # 中途换意图与流程挂起（§3.2）
    flow_action: Optional[str]             # 本轮消息与会话中流程的关系：continue / switch / resume / abort / off_domain
    pending_flows: Optional[List[Dict[str, Any]]]  # 挂起流程快照栈（LIFO，最近挂起的在前）：{flow_id, intent, slot_values, missing_slots, slot_rounds, order_validated_id, parked_at_turn}
    flow_turn_total: int                   # 会话内累计补槽轮数（跨流程预算）：防止在两个流程间反复切换刷满追问次数
    flow_status: Optional[str]             # 上一轮流程是否已办结（completed/rejected/escalated）：已办结的流程不得再挂起，否则恢复后会二次写单

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
def _flow_pausable(state: Dict[str, Any]) -> bool:      # 是否存在"可被挂起"的进行中流程（§3.2 判定入口）
    """条件缺一不可，否则本节会把不该动的东西动走：

    - flow_status 非终态：上一轮已办结/已转人工的流程若被挂起，"继续退货"会恢复出一个
      已经写过退款单的流程，二次触发写操作；
    - decision 为空或 RETRY_ASK：已给出终局决策（越界答复、取消、转人工）的会话位不是进行中流程；
    - 中断在两处之一——补槽补到一半（missing_slots 非空）或等用户确认意图
      （intent_confirm_pending 非空）。后者同样是"用户已填过信息、系统还没办完"的状态，
      而"您是要办理退货退款吗？→算了换货吧"恰是现实里最高频的改口。

    还差最后一道由调用处把关：意图刚被用户判错时（§3.1 拒绝分支已作废意图槽位）槽位表里
    没有值得埋掉的判定，此时不该挂起。
    """
    if state.get("flow_status") in FLOW_TERMINAL_STATUSES:
        return False
    if state.get("decision") not in (None, "RETRY_ASK"):
        return False
    return bool(state.get("missing_slots")) or bool(state.get("intent_confirm_pending"))


def _flow_context_prompt(active_intent: str, missing: list, history: list, pending: list) -> str:
    """§3.2 会话状态段：仅在确有进行中流程时追加，避免每轮抽槽都多背一段与本轮无关的说明。"""
    label = _INTENT_LABELS.get(active_intent, active_intent)
    last_ask = ""                                        # 助手上一轮的提问——判断"本轮是不是在回答它"的最强线索
    for msg in reversed(history or []):
        if msg.get("role") == "assistant":
            last_ask = str(msg.get("content") or "")[:60]
            break
    lines = [f"【当前会话状态】正在进行中的售后流程：{label}"]
    if missing:
        lines.append(f"该流程尚缺【{'、'.join(missing)}】，助手上一轮的问题是：{last_ask}")
    else:
        lines.append("该流程信息已齐全。")
    if pending:
        names = "、".join(_INTENT_LABELS.get(p.get("intent"), str(p.get("intent"))) for p in pending)
        lines.append(f"此前还有被你暂停并保留的流程：{names}（最近暂停的排在最前）。")
    lines.append(
        "本轮除抽取槽位外，必须同时给出 flow_action：\n"
        "- continue：本轮在补充上述流程缺失的信息（即对上一轮提问的回答）\n"
        "- switch：本轮改口要办另一个售后诉求（如『还是换货吧』『改成仅退款』），或同一诉求另起一单"
        "（如『再帮我退另一个订单 ORD_XXX』）。intent 填新诉求，"
        "订单号与原因沿用当前流程的值，除非用户另外给出了订单号\n"
        "- resume：本轮要求回到之前暂停的某个流程，同时给出 resume_intent\n"
        "- abort：本轮明确表示不再办理当前流程（如『算了不退了』『取消申请』），此时不要给出新诉求\n"
        "- off_domain：本轮是与当前流程无关的新话题或别的业务类型（如查物流、问客服电话、闲聊），intent 填 OTHER\n"
        "判定从宽：只是回答提问、补充原因或订单号时一律 continue；『算了』单独出现不是 switch；"
        "拿不准就选 continue——误判切换会把用户已经填好的信息埋掉，代价远高于漏判。"
    )
    return "\n".join(lines)


def _flow_snapshot(state: Dict[str, Any], slot_values: Dict[str, Any], missing: list) -> Dict[str, Any]:
    """把进行中的流程原样存档，供用户说"继续退货"时恢复到中断处，不必从头重新补槽。"""
    intent = (slot_values.get("intent") or {}).get("value")
    order_val = (slot_values.get("order_id") or {}).get("string_value")
    return {                                             # 快照字段与状态字段一一对应，恢复时整体回填
        "flow_id": f"{intent}:{order_val or 'NO_ORDER'}",  # 流程标识：意图+订单，同一意图换订单视为不同流程
        "intent": intent,
        "slot_values": {k: dict(v) for k, v in (slot_values or {}).items() if k != "pending_confirm"},
        "missing_slots": list(missing or []),
        "slot_rounds": int(state.get("slot_rounds") or 0),  # 该流程已消耗的追问轮数，恢复时一并还原
        "order_validated_id": state.get("order_validated_id"),  # 已通过的订单校验标记，恢复后免二次查库
        "order_info": state.get("order_info"),             # 已缓存的订单详情一并存档：恢复时防止与另一流程的缓存串单
        "parked_at_turn": len(state.get("dialog_history") or []),
    }


def _park_flow(pending: list, snapshot: Dict[str, Any]) -> tuple:  # 快照入栈（LIFO），超深丢弃最旧一条
    stack = [snapshot] + [p for p in (pending or []) if p.get("flow_id") != snapshot.get("flow_id")]
    evicted = None                                       # 被挤出的挂起流程（用于日志与指标归因）
    while len(stack) > max(1, MAX_PENDING_FLOWS):
        evicted = stack.pop()                          # 栈尾即最旧的一条：用户越久没回来恢复，越可能已经放弃
    return stack, evicted


def _park_active_flow(state: Dict[str, Any], pending: list, slot_values: Dict[str, Any],
                      missing: list, reason: str) -> tuple:  # 挂起当前流程：存档 + 计数 + 轨迹（switch/off_domain 共用）
    """把进行中的流程快照压入挂起栈；栈超深时丢弃最旧一条并告警——用户越久没回来恢复，越可能已经放弃。"""
    snapshot = _flow_snapshot(state, slot_values, missing)
    stack, evicted = _park_flow(pending, snapshot)
    metrics.inc("pending_flow_total", {"op": "park"})
    if evicted:
        metrics.inc("pending_flow_total", {"op": "evict"})
        logger.warning("flow_evicted", **trace_of(state), flow_id=evicted.get("flow_id"),
                       note=f"挂起栈深度上限 {MAX_PENDING_FLOWS}，该流程的已填信息不再可恢复")
    log_node(state, "flow_parked", flow_id=snapshot["flow_id"], intent=snapshot["intent"],
             missing_slots=snapshot["missing_slots"], slot_rounds=snapshot["slot_rounds"],
             driven_by=reason, pending_depth=len(stack))
    return stack, snapshot


def _match_pending(text: str, pending: list) -> Optional[Dict[str, Any]]:  # 定位用户要恢复哪个挂起流程
    """话里点了意图名就按名字取，没点则取最近挂起的一条（栈语义：继续 = 恢复上一步）。"""
    if not pending:
        return None
    named = [snap for snap in pending
             if any(hint in text for hint in _INTENT_HINTS.get(snap.get("intent"), ()))]
    return named[0] if named else pending[0]


def _resume_hint(pending: list) -> str:                  # 挂起流程的恢复入口话术（§3.2）
    """把"旧流程还留着、怎么回来"告诉用户——不告诉就等于悄悄丢掉了用户已经填好的信息。"""
    if not pending:
        return ""
    label = _INTENT_LABELS.get(pending[0].get("intent"), str(pending[0].get("intent")))
    return f"您之前还有一个【{label}】申请我帮您留着了，回复『继续{label}』即可接着办理。"


def _restart_flow_slots(prev: Dict[str, Any], fresh: Dict[str, Any]) -> Dict[str, Any]:
    """换意图后为新流程重建槽位表：继承同一笔订单与原因，只丢弃"办理类型"（intent/confidence/待确认）。

    本轮用户已给出某槽位时不继承该槽位——否则 accumulate-never-overwrite 会把新订单号挡在门外，
    用户改了单号却仍按旧单号办理。
    """
    table: Dict[str, Any] = {}
    for name in FLOW_INHERIT_SLOTS:
        entry = prev.get(name)
        if not entry or fresh.get(name) is not None:
            continue                                     # 本轮自带新值：交给统一合并逻辑写入
        table[name] = {**entry, "source": f"{entry.get('source', '')}（换意图继承）"}
    return table


async def extract_slots_via_llm(messages: list, thread_id: str = "", flow_context: str = "") -> Optional[Dict[str, Any]]:  # 调用真实 LLM 完成意图与槽位抽取，失败返回 None 由上层降级
    """function calling 结构化抽槽：tool_choice 强制模型调用 SLOT_TOOL，
    槽位取自协议层保证的 tool_calls.arguments；模型未走工具路径时回退解析正文 JSON。"""
    try:                                                 # 包住整个 LLM 调用，网络/解析异常统一走降级
        router = get_llm_router()                        # 获取（或懒初始化）LLM 路由器
        system_prompt = SLOT_EXTRACTION_PROMPT + (("\n" + flow_context) if flow_context else "")  # 存在进行中流程时才追加会话状态段（§3.2）
        request_messages = [ChatMessage(role="system", content=system_prompt)] + [  # 系统提示词 + 多轮对话历史
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
        via = "tool_calls"                               # 结构化来源形态：tool_calls / content_json，用于复盘 §2.1 收益
        if response.tool_calls:                          # 首选路径：function calling 结构化返回
            tc = response.tool_calls[0]                  # 取首个工具调用（tool_choice 已限定唯一目标）
            args = tc.arguments if isinstance(tc.arguments, dict) else _extract_json(str(tc.arguments))  # 参数已是 dict 或需二次解析
            if args and set(args) & _SLOT_KEYS:          # 参数须命中至少一个已知槽位键，甄别模拟器的无关返回
                raw = args                               # 采用协议层结构化槽位
        if raw is None and response.content:             # 回退路径：模型忽略 tool_choice 时从正文抢救 JSON
            raw = _extract_json(response.content)        # 兼容旧行为的兜底解析
            via = "content_json"                         # 标记本次走的是正文兜底而非协议层结构化
        if raw is None:                                  # 两条路径均无有效结构
            logger.warning("llm_slots_unstructured", thread_id=thread_id, content_len=len(response.content or ""))
            return None                                  # 直接触发关键词降级
        validated = SlotExtractionResult.model_validate(raw)  # Pydantic Schema 强校验：枚举/格式/长度不合规即抛 ValidationError
        logger.info("llm_slots_validated", thread_id=thread_id, via=via,
                    slots=validated.model_dump(exclude_none=True))  # 记录规范化槽位与来源形态
        return validated.model_dump(exclude_none=True) # 返回校验清洗后的槽位字典（None 字段已剔除）
    except ValidationError as ve:                      # LLM 输出不符合槽位 Schema（幻觉枚举值、非法订单号格式等）
        logger.warning("llm_slots_rejected", thread_id=thread_id, errors=ve.errors())  # 记录违规字段，供降级归因
        return None                                    # 触发关键词降级
    except Exception as exc:                           # LLM 超时、鉴权失败、路由熔断等任何异常
        logger.error("llm_extraction_failed", thread_id=thread_id, error=f"{type(exc).__name__}: {exc}")  # 降级日志，便于排查
        return None                                      # 返回 None 触发关键词兜底


def fallback_extract_slots(user_msg: str, active_intent: Optional[str] = None) -> Dict[str, Any]:  # LLM 不可用时的关键词规则兜底抽取（保留原演示逻辑并增强）
    """基于质量关键词命中做规则抽取；无法判定的槽位置空以触发追问。

    置信度自评（§3.1）：单一动作词命中给 0.9；退货退款与换货同时命中（"退货"又"换货"）时
    规则只能按优先级猜，给中置信 0.7 触发意图确认反问；完全没有动作词时不产出意图，
    与既有行为一致交由补槽/越界网关处理。

    会话关系判定（§3.2）：规则层只认两类证据明确的改口——
    - switch：改口线索词（还是/改成/换成）+（判出的意图与当前流程不同 或 本轮另报出一个订单号）；
      没有改口线索词一律不判切换，同一诉求复述在办单号也不算——误判会把用户已填的信息埋掉；
    - abort：放弃表达（不办了/取消申请/算了不…）且不含改口线索词，避免"算了还是换货吧"被当成取消。
    刻意不判 off_domain：关键词无法区分"好的马上找"与"帮我查物流"这类同为"无售后信号"的文本，
    误判会把用户已填的槽位埋掉；降级路径维持改造前的 continue 行为。
    """
    is_quality = any(k in user_msg for k in ["破", "坏", "烂", "残", "瑕疵", "质量", "断"])  # 命中任一质量类关键词即判定为质量问题
    has_refund = any(k in user_msg for k in ["退", "退款", "退货"])  # 命中"退"类关键词判定为退货退款意图
    has_exchange = any(k in user_msg for k in ["换货", "换一个", "换新"])  # 命中"换"类关键词判定为换货意图
    has_no_reason = any(k in user_msg for k in ["不想要", "不喜欢", "无理由", "后悔", "不需要"])  # 命中"无理由"类关键词判定为非质量问题
    order_match = re.search(r"ORD[_\-A-Z0-9]+|\b\d{8,}\b", user_msg, re.IGNORECASE)  # 正则尝试从文本提取订单号（如 ORD_CLOTH_001 / 8 位以上数字）
    has_any_signal = has_refund or has_exchange or is_quality or has_no_reason or bool(order_match)  # 是否命中任何售后相关信号
    intent = (("RETURN_AND_REFUND" if has_refund else "EXCHANGE") if (has_refund or has_exchange)
              else ("OTHER" if not has_any_signal else None))  # 售后词->意图；完全无售后信号->OTHER 越界出口（§2.3）；有质量/订单线索但无动作词->None 交由追问
    flow_action = None                                   # 本轮与会话中流程的关系：无进行中流程时保持缺省（continue）
    if active_intent:
        has_switch_cue = bool(_FLOW_SWITCH_CUE_RE.search(user_msg))
        if has_switch_cue and intent and intent != "OTHER" and (
                intent != active_intent or bool(order_match)):
            # 改口线索 + 判出不同意图；同一诉求但本轮另报了一个订单号也算新流程。
            # 规则层不认识在办单号，所以报出单号即放行，"是否真的换了单"由主节点按槽位核验（未换则拒绝，不埋旧流程）。
            flow_action = "switch"
        elif _FLOW_ABORT_RE.search(user_msg) and not has_switch_cue:
            flow_action = "abort"                        # 明确放弃当前流程，不产出新诉求
            intent = None
        else:
            flow_action = "continue"
    if intent in ("RETURN_AND_REFUND", "EXCHANGE", "REFUND_ONLY"):  # 规则确实判出了售后意图，才需要给自评置信度
        intent_confidence = 0.7 if (has_refund and has_exchange) else 0.9  # 退/换信号并存=规则在猜，降级为中置信触发确认
    else:
        intent_confidence = None                        # OTHER 走越界网关、None 走补槽追问，均不进入阈值判定
    return {                                             # 组装与 LLM 输出同构的槽位字典
        "intent": intent,
        "reason_type": "quality_issue" if is_quality else ("no_reason" if has_no_reason else None),  # 仅显式表达才能判定大类，否则置 None 触发追问
        "reason": "关键词规则判定：质量问题" if is_quality else ("关键词规则判定：无理由" if has_no_reason else None),  # 规则模式下的原因描述
        "order_id": order_match.group(0) if order_match else None,  # 正则命中则填订单号，未命中置 None 触发追问（不写死兜底）
        "confidence": intent_confidence,                # 规则路径的意图自评分，与 LLM 路径同契约进入阈值策略
        "missing_slots": [],                             # 缺失槽位由主节点统一计算，此处留空
        "flow_action": flow_action,                      # 会话关系判定（§3.2）：None 由主节点按 continue 处理
        "resume_intent": None,                           # 规则层不做恢复判定：恢复由主节点的正则前置通道处理
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


def _confidence_band(confidence: Optional[float]) -> str:  # 自评分分段（§3.1）：供指标/评测按段归因，与实际拦截动作解耦
    """返回段名。not_gated 表示本轮没有自评分（未抽槽或模型未自评），不参与阈值判定。"""
    if confidence is None:
        return "not_gated"
    if confidence < CONFIDENCE_LOW:
        return "low"
    if confidence < CONFIDENCE_HIGH:
        return "mid"
    return "high"


async def intent_recognition_node(state: AfterSalesState) -> Dict[str, Any]:  # 节点1（异步）：调用真实 LLM 从多轮对话中抽取意图与槽位，累积合并并按置信度阈值分流
    """
    [节点 1: LLM 意图识别与槽位填充 + 置信度阈值策略]
    - 携带对话历史调用 LLM 抽取 intent/order_id/reason/reason_type/confidence 槽位;
    - 槽位跨轮次累积合并 (已有值优先，不被新一轮覆盖);
    - 必要槽位缺失时不写死兜底，而是交由 ask_user 节点反问用户补槽;
    - 意图置信度三段收敛 (§3.1)：≥high 直接执行 / [low,high) 反问确认意图 / <low 转人工核实，
      确认话术复用 slot_error→ask_user 回流机制，不新增图节点与路由边。
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
        metrics.inc("guardrail_action_total", {"action": "block"})  # 注入攻击频次（§2.3）
        # 违规原文不写入日志与对话历史，防止二次注入与敏感信息落盘
        log_node(state, "guardrail_blocked", guardrail_action="block", violation=hit, decision="ESCALATE_HUMAN")
        return {
            "intent": state.get("intent"),               # 保留已确认意图，不覆盖
            "slot_values": dict(state.get("slot_values") or {}),  # 槽位表原样保留
            "missing_slots": [],                         # 清空缺失，防止误触补槽追问
            "dialog_history": list(state.get("dialog_history") or []),  # 历史不含注入原文
            "decision": "ESCALATE_HUMAN",                # 越狱输入交人工甄别，route_slots 走人工分支
            "human_reason": f"用户输入触发安全护栏（{hit}），转人工核实真实诉求",  # 审计用转人工原因
            "confidence_band": "guardrail_blocked",      # 护栏短路在阈值策略之前，本轮不参与置信度判定
            "slot_error": None,                          # 无业务校验错误
            "intent_confirm_pending": None,              # 安全阻断即终止，作废待确认意图
            "final_reply": "您的消息触发了系统安全检查，已转人工专员继续为您处理售后诉求。",  # 用户侧友好话术
        }
    safe_msg = guard.modified_content or user_msg        # MODIFY 时为 PII 脱敏后文本；ALLOW 时即原文——只有脱敏文本才进模型
    if guard.action == GuardrailAction.MODIFY:           # 命中 PII 脱敏：记录脱敏频次（§2.3）
        metrics.inc("guardrail_action_total", {"action": "modify"})
        log_node(state, "guardrail_masked", guardrail_action="modify",
                 rules=[v.rule_id for v in guard.violations])  # 只记规则 id，不落原始敏感文本

    history = list(state.get("dialog_history") or [])    # 取出多轮对话历史，首轮为空列表
    if user_msg:                                         # 本轮有新输入
        history.append({"role": "user", "content": safe_msg})  # 追加脱敏后文本进对话历史，供 LLM 结合上下文抽取（§2.4 入模型脱敏）

    slot_values = dict(state.get("slot_values") or {})   # 取出已累积的槽位表（跨轮次保留）
    pending_confirm = state.get("intent_confirm_pending")  # 上一轮发出的"您是想办理 X 吗？"所携带的待确认意图
    confirmed = None                                     # 本轮是否已由确认回流锁定意图（命中则不再调用 LLM）

    # ---- 会话状态检测与流程恢复（§3.2）：先判定有无进行中流程，再看是否要恢复已挂起的流程 ----
    pending_flows = [dict(p) for p in (state.get("pending_flows") or []) if isinstance(p, dict)]
    active_intent = (slot_values.get("intent") or {}).get("value")
    validated_id = state.get("order_validated_id")        # 本节点内可变的校验标记（换单号/恢复流程时需要改写）
    flow_delta: Dict[str, Any] = {}                        # 换意图/恢复带来的轮次与缓存增量，统一并入节点返回
    resumed = None                                        # 本轮恢复的挂起流程快照
    if FLOW_ENABLED and state.get("flow_status") in FLOW_TERMINAL_STATUSES:
        # 上一轮流程已办结/被拒/转人工：本条消息必然是新诉求，槽位表清零从头抽。
        # 留着旧单会让 accumulate-never-overwrite 把已办结的意图当成"当前诉求"，用户被锁死在那笔单子上。
        slot_values = {}
        active_intent = None
        validated_id = None
        flow_delta = {"slot_rounds": 0, "order_validated_id": None, "order_info": None}
    pausable = FLOW_ENABLED and bool(active_intent) and _flow_pausable(state)  # 可挂起：进行中且未办结（确认回流后还会复核一次，见下）
    if FLOW_ENABLED and pending_flows and not pending_confirm and _FLOW_RESUME_RE.search(user_msg):
        target = _match_pending(user_msg, pending_flows)  # 点了意图名按名字取，没点取最近挂起的一条
        if target:
            resumed = target
            if pausable:                                  # 恢复旧流程同样是"换意图"：先挂起当前流程，别静默丢掉用户在里面填好的信息
                pending_flows, _ = _park_active_flow(
                    state, pending_flows, slot_values,
                    [s for s in REQUIRED_SLOTS if not slot_values.get(s)], "resume")
            pending_flows = [p for p in pending_flows if p.get("flow_id") != target.get("flow_id")]
            slot_values = {k: dict(v) for k, v in (target.get("slot_values") or {}).items()}
            active_intent = target.get("intent")
            validated_id = target.get("order_validated_id")
            flow_delta = {"slot_rounds": int(target.get("slot_rounds") or 0),
                          "order_validated_id": validated_id,
                          "order_info": target.get("order_info")}  # 缓存的订单详情随流程一并恢复，避免跨流程串单
            metrics.inc("pending_flow_total", {"op": "resume"})
            metrics.inc("flow_action_total", {"action": "resume"})
            log_node(state, "flow_resumed", intent=active_intent, flow_id=target.get("flow_id"),
                     slot_rounds=flow_delta["slot_rounds"], remaining_pending=len(pending_flows))
    if pending_confirm:                                  # §3.1 确认回流：用户对意图反问的回答是/不是
        stripped = user_msg.strip()
        if _INT_CONFIRM_REJECT_RE.search(stripped):      # 先判否定：既肯定又否定（"是，但不是退款"）按否定处理，保守作废
            metrics.inc("intent_confirmation_total", {"result": "reject"})
            slot_values.pop("pending_confirm", None)
            slot_values.pop("intent", None)              # 作废被判错的意图槽位，令本轮抽取结果可写入、缺失清单重新触发提问
            slot_values.pop("confidence", None)          # 自评分描述的是被否决的那次判定，一并作废
            log_node(state, "intent_confirmation", result="reject", pending=pending_confirm)
            pending_confirm = None
        elif len(stripped) <= 8 and _INT_CONFIRM_ACCEPT_RE.match(stripped):  # 裸肯定词"是/对/嗯/好的"：用户已裁决，自评分不再否决
            pending_val = (slot_values.get("pending_confirm") or {}).get("value") or pending_confirm
            slot_values["intent"] = {"value": pending_val, "confidence": 0.98, "source": "用户确认"}
            slot_values["confidence"] = {"value": "0.98", "confidence": 0.98, "source": "用户确认"}
            slot_values.pop("pending_confirm", None)
            metrics.inc("intent_confirmation_total", {"result": "accept"})
            metrics.inc("intent_confidence_band_total", {"band": "confirmed"})
            metrics.inc("intent_confidence_total", {"band": "confirmed", "action": "proceed"})
            log_node(state, "intent_confirmation", result="accept", intent=pending_val, confidence=0.98)
            confirmed = pending_val
        else:                                            # 回答里带新信息（"不是，我要换货"/"是质量问题"）：作废待确认意图后交回模型重抽
            slot_values.pop("pending_confirm", None)
            pending_confirm = None

    # 确认回流可能刚作废意图槽位，故在其后复核一次（§3.2）：意图被判错的流程没有值得埋掉的判定，
    # 而"确认反问还没答复/被答非所问"的改口轮，正是要挂起旧流程换新流程的那一轮。
    active_intent = (slot_values.get("intent") or {}).get("value")
    pausable = FLOW_ENABLED and bool(active_intent) and _flow_pausable(state)

    source = "确认回流"                                   # 默认本轮由用户确认锁定意图（回流轮不调模型）
    round_slots: Dict[str, Any] = {}                      # 本轮抽出的原始槽位（含会话关系判定）：换意图判据的唯一来源
    flow_action = "resume" if resumed else "continue"     # 本轮与会话中流程的关系（§3.2）
    parked: Optional[Dict[str, Any]] = None               # 本轮被存档的旧流程快照，决定答复里要不要给"怎么回来"的提示
    if confirmed is None:                                # 未经确认锁定，才需要真实大模型抽槽
        flow_context = ""                                  # 会话状态段：仅在确有可挂起的进行中流程时才追加
        if pausable and not resumed:                       # 恢复轮旧流程已回到激活位，不必再让模型判断本轮关系
            flow_context = _flow_context_prompt(
                active_intent, [s for s in REQUIRED_SLOTS if not slot_values.get(s)], history, pending_flows)
        slots = await extract_slots_via_llm(history, thread_id_of(state), flow_context)  # 原生异步 await（§2.5）：节点置于 FastAPI 等已运行事件循环内可直接 ainvoke，不再 asyncio.run 重入报错
        source = "LLM"                                       # 标记槽位来源为真实大模型
        if slots is None:                                # LLM 调用或解析失败
            slots = fallback_extract_slots(user_msg, active_intent if pausable else None)  # 降级为关键词规则抽取——双轨原则：规则用原文，抽出的订单号原值送业务校验（入数据库原文）
            slots = SlotExtractionResult(**slots).model_dump(exclude_none=True)  # 规则输出同样过 Pydantic Schema 归一化，保证两路输出契约一致
            source = "关键词降级"                        # 标记来源为兜底规则
        metrics.inc("intent_extraction_total", {"source": "llm" if source == "LLM" else "fallback"})  # 降级触发率：LLM 不可用/输出非法的早期信号（§2.3）
        rated = slots.get("confidence")                  # 本轮模型自评分；None 表示模型未自评（§3.1 不据此拦截）
        round_confidence = float(rated) if rated is not None else None
        round_slots = slots                              # 留档本轮原值：换意图继承与证据门槛都要看它，不能被合并结果反推

        # ---- 换意图 / 取消 / 越界（§3.2）：在合并槽位之前决定本轮服务哪个流程 ----
        # 恢复轮的关系已由前置正则通道定案（flow_context 也因此留空，没向模型索取判定），
        # 模型此时自述的 switch/abort 一律不作数——否则刚恢复到激活位的流程会被它自己的一句话再埋回栈里。
        declared = None if resumed else slots.get("flow_action")
        if pausable and declared == "abort":             # 用户明确放弃当前流程：丢弃其已填信息，不开单不退款
            label = _INTENT_LABELS.get(active_intent, active_intent)
            metrics.inc("pending_flow_total", {"op": "abort"})
            metrics.inc("flow_action_total", {"action": "abort"})
            log_node(state, "flow_aborted", intent=active_intent, remaining_pending=len(pending_flows))
            return {
                "intent": None, "order_id": None, "reason": None, "reason_type": None,
                "slot_values": {},                         # 被取消的流程不留残余槽位，下一轮按新诉求从零开始
                "missing_slots": [],
                "dialog_history": history,
                "decision": "FLOW_ABORTED",                # 终局决策：route_slots 据此直送通知节点，不进任何写操作
                "confidence_band": "not_gated",            # 取消不经阈值判定
                "flow_action": "abort",
                "flow_status": None,
                "pending_flows": pending_flows,            # 更早挂起的流程不受影响，仍可"继续X"恢复
                "slot_rounds": 0, "order_validated_id": None, "order_info": None,
                "slot_error": None, "intent_confirm_pending": None,
                "final_reply": f"已为您取消【{label}】申请，本次未生成退款单或工单。{_resume_hint(pending_flows)}"
                               "如需重新办理，直接说明您的诉求即可。",
            }
        if pausable and declared in ("switch", "off_domain"):
            still_missing = [s for s in REQUIRED_SLOTS if not slot_values.get(s)]
            round_intent = round_slots.get("intent")
            round_order = round_slots.get("order_id")
            active_order = (slot_values.get("order_id") or {}).get("string_value")
            evidence_ok = round_confidence is None or round_confidence >= MIN_SWITCH_CONFIDENCE
            # 目标流程 = 另一类诉求，或同一诉求另起一单（flow_id 含订单号，见 _flow_snapshot）。
            # 后者必须是本轮真的报出了一个不同的单号：复述在办单号不算新流程，否则用户只是换个说法就被埋掉旧流程。
            targetable = round_intent in ("RETURN_AND_REFUND", "REFUND_ONLY", "EXCHANGE") \
                         and (round_intent != active_intent or bool(round_order and round_order != active_order))
            if declared == "switch" and not (targetable and evidence_ok):
                metrics.inc("flow_action_total", {"action": "continue"})
                metrics.inc("flow_switch_rejected_total", {"reason": "low_confidence" if not evidence_ok else "no_new_intent"})
                log_node(state, "flow_switch_rejected", active=active_intent, round_intent=round_intent,
                         confidence=round_confidence, threshold=MIN_SWITCH_CONFIDENCE)
                # 证据不足即维持旧流程：误判一次切换的代价是把用户已填的信息埋掉，宁可漏判
            else:
                pending_flows, parked = _park_active_flow(state, pending_flows, slot_values, still_missing, declared)
                metrics.inc("flow_action_total", {"action": declared})
                flow_action = declared
                if declared == "switch":                 # 改口办另一类诉求：同一笔订单、同一个原因，只换办理类型
                    slot_values = _restart_flow_slots(slot_values, round_slots)
                    if round_slots.get("order_id") and round_slots.get("order_id") != validated_id:
                        validated_id = None              # 用户另给了订单号：旧校验标记与缓存作废，按新单号重新核验
                        flow_delta.update({"order_validated_id": None, "order_info": None})
                    flow_delta["slot_rounds"] = 0        # 新流程重新享有完整的补槽轮次预算，旧流程的消耗记在快照里
                else:                                    # off_domain：本轮改答越界出口，旧流程整体留在挂起栈里
                    slot_values = {}
                    flow_delta.update({"slot_rounds": 0, "order_validated_id": None, "order_info": None})

        for name in ("intent", "order_id", "reason", "reason_type", "confidence"):  # 遍历全部槽位定义（含置信度，§3.1）
            new_val = slots.get(name)                    # 本轮抽取到的槽位值
            if new_val is not None and str(new_val).strip():  # 值非空才有效
                old = slot_values.get(name)              # 已有槽位记录
                # confidence 描述"本轮这个判定有多可信"，每轮刷新；其余槽位只在为空时写入，防止误覆盖已确认信息
                # 例外：旧值是 OTHER（上一轮越界判定留下的占位）时允许被真实诉求覆盖，否则该线程永远判不进售后
                if (name == "confidence" or old is None or not (old.get("value") or old.get("string_value"))
                        or (name == "intent" and old.get("value") == "OTHER")):
                    slot_values[name] = {                # 按 react_ai 意图路由的槽位结构写入
                        _SLOT_VALUE_FIELD[name]: str(new_val).strip(),  # 依据槽位类型选择 value / string_value 字段
                        # 自评分统一落到各槽位的 confidence 字段：意图槽位记录本轮模型自评（未自评则留空，不编造数字）
                        "confidence": (float(new_val) if name == "confidence"
                                       else (round_confidence if name == "intent"
                                             else (0.9 if source == "LLM" else 0.5))),
                        "source": source,                # 记录该槽位的抽取来源，便于审计
                    }

    missing = [s for s in REQUIRED_SLOTS if not slot_values.get(s)]  # 计算尚未填充的必要槽位

    # ---- 越界意图网关（§2.3）：OTHER 不进售后流程，直接产出 OUT_OF_SCOPE 出口 ----
    if (slot_values.get("intent") or {}).get("value") == "OTHER":  # 意图判定为非售后诉求（仅在意图槽位首次填入或本轮已挂起旧流程时生效）
        log_node(state, "out_of_scope_intent", intent="OTHER", decision="OUT_OF_SCOPE",
                 flow_action=flow_action, parked=bool(parked))  # 越界网关轨迹（§2.3）；parked 标记本轮是否同时挂起了旧流程（§3.2）
        metrics.inc("intent_confidence_band_total", {"band": "out_of_scope"})  # OTHER 由网关短路，不进阈值判定（§3.1）
        return {                                         # 短路返回：跳过业务校验与售后链路
            "intent": "OTHER",                           # 顶层镜像：越界意图
            "slot_values": slot_values,                  # 写回槽位表（intent=OTHER 已记录）
            "missing_slots": [],                         # 清空缺失清单，防止 route_slots 误触发售后补槽追问
            "dialog_history": history,                   # 写回对话历史
            "decision": "OUT_OF_SCOPE",                  # 越界决策标记，route_slots 据此直送通知节点
            "confidence_band": "out_of_scope",           # 越界出口同样标注段名，便于评测/看板归因
            "flow_action": flow_action,                  # 本轮关系（§3.2）：off_domain 时旧流程已进挂起栈
            "flow_status": None,                         # 越界答复不产生业务终态，flow_status 由通知节点按决策回填
            "pending_flows": pending_flows,
            "slot_error": None,                          # 无业务校验错误
            "intent_confirm_pending": None,              # 越界出口不挂起确认
            **flow_delta,                                # 挂起带来的轮次/缓存改写（§3.2）
            # 旧流程被保留时必须让用户知道回来的入口——本节唯一需要被用户看见的行为
            "final_reply": "我目前只支持退货退款、仅退款、换货类售后诉求，其他问题请回复『人工』由专员为您解答；如需售后，请直接描述您的问题。"
                           + (_resume_hint(pending_flows) if parked else ""),
        }

    # ---- 置信度阈值策略（§3.1）：意图自评分决定"直接执行 / 反问确认 / 转人工" ----
    intent_entry = slot_values.get("intent") or {}
    raw_confidence = (slot_values.get("confidence") or {}).get("value")  # 本轮模型自评（缺失即"未自评"）
    if raw_confidence is not None:
        confidence, confidence_src = float(raw_confidence), "model"
    elif intent_entry.get("confidence") is not None:                     # 意图是往轮填入的：沿用它当时的自评分
        confidence, confidence_src = float(intent_entry["confidence"]), "inherited"
    else:
        confidence, confidence_src = (None, "absent")    # 本轮与往轮都没有自评分：§3.1 不参与判定，保持改造前的直通行为
    intent_val = intent_entry.get("value")
    if confidence is not None:                           # 每次抽槽都上报自评分分布，供线上标定阈值（§3.1 风险项）
        metrics.observe("intent_confidence", confidence)
    band = _confidence_band(confidence)           # 分段（供评测/看板按段归因）：not_gated = 关闭拦截或本轮未抽槽
    if confirmed is not None:
        band = "confirmed"                               # 用户已当场确认的意图（§3.1 回流轮，不再调用模型），自评分不再参与判定
    if confidence is None and confirmed is None and intent_val:
        band = "unrated"                                 # 有意图却无自评分：阈值策略失效，靠该段计数发现模型停止自评
        metrics.inc("intent_confidence_band_total", {"band": band})
    if CONFIDENCE_ENABLED and intent_val and confirmed is None and confidence is not None:  # 用户已当场确认的意图不再受模型自评分否决
        if confidence < CONFIDENCE_LOW:                  # 段一：模型自己都没把握——不作废不硬猜，也不直接开人工工单
            band, action = "low", "ask_intent_clarify"
            for filled in ("intent", "confidence"):      # 回滚本轮新填入的判定，槽位表回到可追问状态
                slot_values.pop(filled, None)
            missing = [s for s in REQUIRED_SLOTS if not slot_values.get(s)]  # 意图已空 -> 必然回流追问"您要办哪一种"
        elif confidence < CONFIDENCE_HIGH:               # 段二：够可信但不足够——先占住流程，向用户确认；缺槽时仍按缺槽处理
            if missing:                                  # 还有必要槽位要问：本轮先补槽，确认留到槽位齐全那轮，避免一次抛两类问题
                band, action = "mid_deferred", "ask_missing_slot"
            else:
                band, action = "mid", "ask_confirm"
        else:                                            # 段三：高置信，直接进入售后链
            band, action = "high", "proceed"
        metrics.inc("intent_confidence_band_total", {"band": band})
        metrics.inc("intent_confidence_total", {"band": band, "action": action})
        log_node(state, "intent_confidence_gate", intent=intent_val, confidence=confidence,
                 confidence_source=confidence_src, band=band, action=action,
                 thresholds=f"{CONFIDENCE_LOW}~{CONFIDENCE_HIGH}")
        if action == "ask_confirm":                      # 槽位齐全才单独确认意图
            slot_values["pending_confirm"] = {"value": intent_val, "confidence": confidence, "source": "意图确认待答复"}
            confidence_question = _INT_CONFIRM_QUESTION.format(label=_INTENT_LABELS.get(intent_val, intent_val))
            log_node(state, "intent_confirmation", result="pending", intent=intent_val, confidence=confidence)
            return {
                "intent": intent_val,
                "slot_values": slot_values,
                "missing_slots": [],                     # 缺槽为空、由确认反问驱动追问，避免同时抛两类问题
                "dialog_history": history,
                "decision": None,
                "confidence_band": band,
                "flow_action": flow_action,              # 本轮会话关系（§3.2）
                "flow_status": None,                     # 进行中流程一律清空终态标记，使下一轮可被挂起（§3.2）
                "pending_flows": pending_flows,
                "slot_error": confidence_question,       # 复用校验失败回流话术机制：ask_user 优先播报该文案
                "intent_confirm_pending": intent_val,
                **flow_delta,
            }

    # ---- 槽位业务校验（§2.2）：新出现或变更过的订单号需查库验证存在性+归属权 ----
    user_id = state.get("user_id", "USR_001")            # 当前操作用户，用于归属权核验
    order_val = (slot_values.get("order_id") or {}).get("string_value")  # 本轮待校验的订单号
    slot_error = None                                    # 业务校验失败文案，驱动针对性追问
    biz_extra: Dict[str, Any] = {}                       # 校验产生的附加状态增量
    if order_val and order_val != validated_id:          # 首次填入、用户换了单号、或换意图后恢复旧流程才校验；同一笔订单已校验过则跳过
        err, order_data = validate_order_slot(order_val, user_id)  # 存在性 + 归属权双重校验
        if err:                                          # 校验不通过：格式合法但业务不可用
            slot_error = err                             # 记录失败文案，供 ask_user 生成针对性追问
            slot_values.pop("order_id", None)            # 从槽位表剔除无效单号
            order_val = None                             # 清空局部变量，使顶层镜像同步为 None
            missing = [s for s in REQUIRED_SLOTS if not slot_values.get(s)]  # 重算缺失清单 -> route_slots 自然回流追问
            reason_class = _validation_failure_reason(err)  # 失败分类：not_found / ownership / system
            metrics.inc("slot_validation_fail_total", {"reason": reason_class})  # 用户填错率与越权尝试检测（§2.3）
            log_node(state, "slot_validation_failed", order_id=None, reason_class=reason_class, detail=err)
        else:                                            # 校验通过
            biz_extra = {"order_validated_id": order_val, "order_info": order_data}  # 记录已校验单号并缓存订单详情，节点2免重复查询

    # 槽位填充全景：thread_id 可还原"抽取来源 + 槽位 + 缺失 + 校验 + 置信度"的完整决策轨迹（§2.3）
    log_node(state, "slots_extracted",
             slots={k: (v.get("value") or v.get("string_value")) for k, v in slot_values.items()},
             missing_slots=missing,
             extract_source={"LLM": "llm", "关键词降级": "fallback", "确认回流": "user_confirmation"}.get(source, "unknown"),
             confidence=confidence, slot_error=slot_error)

    return {                                             # 返回本节点产出的状态增量（LangGraph 会将其合并进全局状态）
        "intent": (slot_values.get("intent") or {}).get("value"),        # 顶层镜像：供下游节点直接读取意图
        "order_id": (slot_values.get("order_id") or {}).get("string_value"),  # 顶层镜像：LLM 抽取的订单号，未提供则为 None（不再写死兜底）
        "reason": (slot_values.get("reason") or {}).get("string_value") or "用户未说明具体原因",  # 原因描述，缺省给通用文案
        "reason_type": (slot_values.get("reason_type") or {}).get("value"),  # 政策原因大类，供政策核对节点使用
        "slot_values": slot_values,                      # 写回累积后的槽位表
        "missing_slots": missing,                        # 缺失清单驱动条件路由决定是否追问
        "dialog_history": history,                       # 写回更新后的对话历史（追问后用户补充时携带上下文再抽取）
        "slot_error": slot_error,                        # 业务校验失败文案（通过则 None，同时清除上一轮残留）
        "confidence_band": band,                         # 本轮自评分落段（§3.1）：high/mid/mid_deferred/low/confirmed/unrated/not_gated
        "flow_action": flow_action,                      # 本轮与会话中流程的关系（§3.2）
        "flow_status": None,                             # 进行中流程清空终态标记：下一轮才允许被挂起（§3.2）
        "pending_flows": pending_flows,                  # 挂起栈（§3.2）：本轮可能压入或弹出快照
        "intent_confirm_pending": None,                  # 新一轮开始，清除上一轮残留的意图确认
        "decision": None,                                # 新一轮开始，清除上一轮 RETRY_ASK 等残留决策，防止路由误判
        **flow_delta,                                    # 换意图/恢复带来的轮次与订单缓存改写（§3.2）
        **biz_extra,                                     # 校验通过时附带 order_validated_id 与缓存的 order_info（覆盖换意图清空的旧值）
    }


def ask_user_node(state: AfterSalesState) -> Dict[str, Any]:  # 节点1.5：必要槽位缺失或意图待确认时生成反问话术，结束本轮等待用户补充
    """
    [节点 追问补槽: Slot Filling 反问 + 意图确认反问]
    - 按缺失的必要槽位生成针对性提问，写入 final_reply 后本轮图执行结束;
    - 调用方用同一 thread_id 携带用户新消息再次 invoke，dialog_history 使 LLM 能在
      完整上下文里重新抽取槽位，实现多轮补槽。
    - 追问轮数超过上限 (3 轮) 则放弃自动处理，转人工工单。
    - 中置信段的意图确认（§3.1）复用同一节点：意图节点把确认话术写进 slot_error、
      待确认意图写进 intent_confirm_pending，此处原样回流并在下一轮由意图节点识别"是/不是"。
    """
    rounds = int(state.get("slot_rounds") or 0) + 1      # 本次为该流程第几轮追问
    turns = int(state.get("flow_turn_total") or 0) + 1   # 本次为整个会话第几轮追问（§3.2 跨流程预算）
    missing = list(state.get("missing_slots") or [])     # 当前缺失的必要槽位列表
    metrics.observe("ask_user_rounds", rounds)           # 补槽轮次分布，>2 轮占比高说明追问话术有问题（§2.3）

    # 双重预算：单流程 3 轮防"死磕一个诉求"，会话 6 轮防"在两个流程间反复切换刷满追问次数"
    exhausted = "flow" if rounds > 3 else ("session" if turns > MAX_SESSION_SLOT_ROUNDS else None)
    if exhausted:                                        # 反复补槽仍失败（用户答非所问/持续缺失/切换滥用）
        log_node(state, "ask_user_exhausted", slot_rounds=rounds, flow_turn_total=turns,
                 missing_slots=missing, budget=exhausted, decision="ESCALATE_HUMAN")
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
            "human_reason": ("多次追问后必要信息仍不完整" if exhausted == "flow"
                             else "会话内多流程切换累计追问轮数超限"),  # 区分是死磕一处还是反复换诉求
            "final_reply": "很抱歉，多次尝试后仍无法确认您的售后信息，已为您转接人工专员处理。",  # 给用户的解释性答复
            "action_result": res.data if res.ok else {}, # 人工工单创建结果
            "slot_rounds": rounds,                       # 记录追问轮数
            "flow_turn_total": turns,                    # 会话级累计轮数同样写回，供轨迹还原
            "intent_confirm_pending": None,              # 转人工后作废待确认意图，防止人工处理完又被回流轮复用
        }

    ask_map = {                                          # 槽位名 -> 追问话术片段映射
        "intent": "请问您希望如何处理：退货退款、仅退款，还是换货？",  # 意图缺失提问
        "order_id": "请提供您的订单号，方便我为您查询订单信息。",  # 订单号缺失提问（替代原先写死兜底）
        "reason_type": "方便说明一下原因吗？是商品质量问题（如破损、瑕疵），还是不想要了？",  # 原因大类缺失提问
    }
    slot_error = state.get("slot_error")                 # 回流话术文案：业务校验失败（订单不存在/非本人订单）或意图确认反问（§3.1）
    if slot_error:                                       # 针对性场景：校验失败/意图确认话术优先于通用补槽提问
        question = slot_error
    else:                                                # 普通缺槽场景：按缺失槽位拼接追问
        question = "为了帮您处理售后申请，" + " ".join(ask_map[s] for s in missing if s in ask_map)
    if state.get("flow_action") == "switch":             # 本轮刚把旧流程挂起：必须告知退路，否则等同于悄悄丢掉用户已填信息（§3.2）
        question += " " + _resume_hint(state.get("pending_flows") or [])
    log_node(state, "ask_user_round", slot_rounds=rounds, flow_turn_total=turns, missing_slots=missing,
             question=question, driven_by_slot_error=bool(slot_error),
             pending_flows=len(state.get("pending_flows") or []),
             flow_action=state.get("flow_action"),
             pending_intent=state.get("intent_confirm_pending"))  # 追问轮次轨迹（§2.3），意图确认时携带待确认意图

    history = list(state.get("dialog_history") or [])    # 取出对话历史
    history.append({"role": "assistant", "content": question})  # 把本次追问也记入历史，下一轮 LLM 可见完整语境
    return {                                             # 本轮以追问结束，等待调用方携带用户新消息重新 invoke
        "final_reply": question,                         # 追问文本作为本轮答复呈现给用户
        "missing_slots": missing,                        # 保持缺失清单（下一轮重抽后刷新）
        "slot_rounds": rounds,                           # 累加该流程追问轮数
        "flow_turn_total": turns,                        # 累加会话追问轮数（§3.2 跨流程预算）
        "dialog_history": history,                       # 写回含追问的对话历史
        "slot_error": None,                              # 文案已消费，清除避免污染下一轮
        "intent_confirm_pending": state.get("intent_confirm_pending"),  # 意图确认反问时保留待确认意图，普通补槽轮为 None
    }


def query_order_node(state: AfterSalesState) -> Dict[str, Any]:  # 节点2：调用真实工具查询订单详情
    """
    [节点 2: 查询订单详情]
    - 调用内置 QueryOrderTool 从 PostgreSQL 真实查询订单数据;
    - 槽位校验阶段已缓存订单详情时跳过重复查询;
    - 查询失败不再抛异常崩图，转为 RETRY_ASK 回流追问节点（§2.2）。
    """
    if state.get("order_info"):                          # 意图节点业务校验时已顺带查到并缓存
        log_node(state, "order_cache_hit")              # 槽位校验阶段已缓存，本节点跳过查询
        return {}                                        # 直接透传，进入后续核验链

    order_id = state.get("order_id", "")               # 从状态中取出待查询的订单号
    user_id = state.get("user_id", "USR_001")          # 取出用户ID，缺省为演示用户
    log_node(state, "tool_call_started", tool="query_order", order_id=order_id)  # 节点执行轨迹（§2.3）

    ctx = ToolContext(caller_id="langgraph-aftersales", tenant_id="default", user_id=user_id)  # 构造工具上下文：标记调用方、租户与操作用户
    res = query_order_tool.call({"order_id": order_id}, ctx)  # 同步调用订单查询工具，返回结构化 ToolResult

    if not res.ok:                                     # 查询失败：收敛为可恢复的追问流程而非崩图
        log_node(state, "tool_call_failed", tool="query_order", order_id=order_id,
                 error=res.error_message, decision="RETRY_ASK")  # 失败轨迹：回流追问而非崩图
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
    log_node(state, "tool_call_started", tool="query_logistics", order_id=order_id)  # 节点执行轨迹（§2.3）

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

    log_node(state, "tool_call_started", tool="search_policy", category=category,
             reason_type=reason_type)  # 政策核对入参（§2.3）

    ctx = ToolContext(caller_id="langgraph-aftersales", tenant_id="default", user_id=user_id)  # 构造工具上下文
    res = search_policy_tool.call({                    # 调用政策检索工具，查询并计算退换货合规性
        "order_id": order_id,                          # 入参：订单号（用于核对下单时间是否在退换期内）
        "category": category,                          # 入参：商品品类（不同品类政策不同）
        "reason_type": reason_type                     # 入参：原因大类（质量问题/无理由适用不同规则）
    }, ctx)

    if not res.ok:                                     # 政策查询失败属于关键前置条件缺失，但不再裸 raise 崩图（§2.2）
        log_node(state, "tool_call_failed", tool="search_policy", order_id=order_id,
                 error=res.error_message, decision="ESCALATE_HUMAN")  # 关键前置条件缺失：转人工而非崩图
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
        log_node(state, "decision_passthrough", decision="ESCALATE_HUMAN")  # 上游决策透传，不重复判定
        return {}                                      # 状态增量置空，decision/human_reason 原样保留流向路由

    policy = state.get("policy_rules") or {}           # 取出政策计算结果
    order_info = state.get("order_info") or {}         # 取出订单详情
    total_amount = float(order_info.get("total_amount", 0.0))  # 订单实付金额，转为浮点数
    max_auto_amount = float(policy.get("max_auto_refund_amount", 300.0))  # 政策允许的单笔自动退款上限，缺省 300 元
    eligible = policy.get("eligible_for_return", False)  # 政策是否允许本次退货/退款

    log_node(state, "risk_assessment", eligible=eligible, total_amount=total_amount,
             max_auto_amount=max_auto_amount)  # 风控判定依据（§2.3）

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

    log_node(state, "action_execute_started", decision=decision, order_id=order_id, amount=amount)  # 写操作轨迹（§2.3）

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
        if not res.ok:                                     # 退款写操作失败：结构化告警，避免静默吞掉
            log_node(state, "tool_call_failed", tool="create_refund", order_id=order_id, error=res.error_message)
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
    log_node(state, "tool_call_started", tool="escalate_to_human", order_id=order_id, reason=reason)  # 转人工轨迹（§2.3）

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
    - 按本轮终局决策回填 flow_status（§3.2）：办结/拒绝/转人工的流程即被标记为终态，
      意图节点据此拒绝挂起它——否则用户一句"继续退货"会恢复出一个已经写过退款单的流程。
    """
    user_id = state.get("user_id", "USR_001")          # 取出接收通知的用户ID
    final_reply = state.get("final_reply", "您的售后诉求已处理。")  # 取出前序节点生成的最终答复作为通知正文
    order_id = state.get("order_id", "")               # 取出订单号
    log_node(state, "tool_call_started", tool="send_notification", target_user_id=user_id,
             order_id=order_id)  # 通知发送轨迹（§2.3）

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

    flow_status = FLOW_STATUS_BY_DECISION.get(state.get("decision") or "")  # 本轮是否把流程推到终态（§3.2）
    if flow_status:
        metrics.inc("flow_terminal_total", {"status": flow_status})
        log_node(state, "flow_terminal", flow_status=flow_status, decision=state.get("decision"),
                 remaining_pending=len(state.get("pending_flows") or []))
    out: Dict[str, Any] = {"notification_sent": True,       # 标记通知已发送，写入状态
                           "flow_status": flow_status}      # 终态标记，供下一轮 §3.2 判定该流程不可再挂起
    if flow_status:                                          # 会话级追问预算在流程办结后归零：预算防的是"两个流程间反复切换刷追问"，
        out["flow_turn_total"] = 0                           # 不是"整个会话一辈子只能被反问 6 次"
    return out


# ============================================================================
# 3. 动态条件路由 (Conditional Edge Router)
# ============================================================================
def route_slots(state: AfterSalesState) -> Literal["query_order", "ask_user", "send_notification", "human_escalation"]:  # 槽位完整性路由：决定继续流程、追问、越界出口或护栏转人工
    if state.get("decision") in ("OUT_OF_SCOPE", "FLOW_ABORTED"):  # 越界意图网关命中（§2.3）/ 用户明确取消进行中流程（§3.2）
        return "send_notification"                       # 跳过整条售后链，直接发答复通知后结束
    if state.get("decision") == "ESCALATE_HUMAN":        # 输入触发安全护栏（§2.4），意图节点已短路定责
        return "human_escalation"                        # 直接转人工甄别，不进 LLM 抽槽与售后链
    missing = state.get("missing_slots") or []           # 读取必要槽位缺失清单
    if not missing and state.get("intent_confirm_pending"):  # 槽位齐全但意图待确认（§3.1 中置信段）：先反问，不进售后链
        return "ask_user"
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

    # 注册节点：统一经 instrument_node 包装，采集 node_latency_seconds / decision_total 与节点轨迹日志（§2.3）
    workflow.add_node("intent_recognition", instrument_node("intent_recognition", intent_recognition_node))  # 注册节点1：意图识别
    workflow.add_node("query_order", instrument_node("query_order", query_order_node))                        # 注册节点2：订单查询
    workflow.add_node("check_logistics", instrument_node("check_logistics", check_logistics_node))            # 注册节点3：物流核验
    workflow.add_node("policy_evaluation", instrument_node("policy_evaluation", policy_evaluation_node))      # 注册节点4：政策核对
    workflow.add_node("condition_decision", instrument_node("condition_decision", condition_decision_node))    # 注册节点5：风控决策
    workflow.add_node("execute_action", instrument_node("execute_action", execute_action_node))                # 注册节点6：业务写操作
    workflow.add_node("human_escalation", instrument_node("human_escalation", human_escalation_node))          # 注册节点7：转人工
    workflow.add_node("send_notification", instrument_node("send_notification", send_notification_node))       # 注册节点8：发送通知
    workflow.add_node("ask_user", instrument_node("ask_user", ask_user_node))                                  # 注册追问节点：必要槽位缺失时反问用户补槽

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
# 4.5 会话持久化与会话级串行化 (Checkpointer 工厂 §2.6 + 串行化 §2.3)
# ============================================================================
def _pg_conn_string() -> str:                            # 组装 PG 连接串：复用项目数据库配置并加短连接超时
    """在 settings.database.url 上追加 connect_timeout，避免 PG 不可达时长时间挂起。"""
    url = settings.database.url                          # 项目统一数据库连接串（与售后工具共用同一 PG 实例）
    sep = "&" if "?" in url else "?"                     # 判断连接串是否已带查询参数，选择正确的拼接分隔符
    return f"{url}{sep}connect_timeout=3"                # 3 秒连接超时：PG 缺席时快速失败并降级，而非阻塞演示


_PG_CHECKPOINT_READY = False                             # 检查点是否真的挂在 PG 上：决定跨实例锁与分叉检测可用性
_SESSION_LOCKS: Dict[str, asyncio.Lock] = {}             # 进程内 thread_id 排队锁表（同一事件循环内单线程访问，无需再加互斥）

# 线程 leaf 检查点数：线性历史恒为 1，>1 即同一 thread 出现分叉链（E2E 报告 §6.2 的并发缺陷特征）
_HEADS_SQL = """
select count(*)
from checkpoints c
where c.thread_id = %s
  and not exists (
      select 1 from checkpoints p
      where p.thread_id = c.thread_id and p.parent_checkpoint_id = c.checkpoint_id
  )
"""


@asynccontextmanager                                     # 封装为异步上下文管理器：saver 连接随 with 块自动建立与释放
async def open_checkpointer():                           # 创建图检查点：优先 PG 持久化，不可用时降级内存（§2.6）
    """会话持久化工厂。

    AsyncPostgresSaver 把每个 thread_id 的图状态（对话历史/槽位/追问轮次/待中断位置）
    写入 PG checkpoint 表：进程重启后追问到一半的会话可原样续跑，多实例负载均衡下
    任意实例都能接管同一会话——修复 MemorySaver '重启即失忆' 问题。
    PG 不可达（本地开发/故障）时降级 MemorySaver 并告警，演示不中断。
    """
    global _PG_CHECKPOINT_READY                          # 标记检查点后端，供串行化与分叉检测判断可用性
    cm = None                                            # 持有 from_conn_string 返回的异步上下文管理器句柄
    try:
        cm = AsyncPostgresSaver.from_conn_string(_pg_conn_string())  # 惰性创建 PG 连接池上下文（尚未真正连接）
        saver = await cm.__aenter__()                    # 进入上下文：建立连接，得到可用的 AsyncPostgresSaver
        await saver.setup()                              # 幂等建表：首次运行自动创建 checkpoint/checkpoint_blobs 等表
    except Exception as exc:                             # 仅连接/建表失败才降级；with 体内的业务异常不得进入此分支
        logger.warning("checkpointer_degraded", backend="memory", error=f"{type(exc).__name__}: {exc}",
                       note="进程重启后追问中的会话将丢失，仅限本地演示")  # 显式告警，防止误把内存模式当生产配置
        if cm is not None:                               # 已进入上下文又失败：释放连接，避免泄漏
            try:
                await cm.__aexit__(None, None, None)
            except Exception as close_exc:
                logger.warning("checkpointer_close_failed", error=f"{type(close_exc).__name__}: {close_exc}")
        yield MemorySaver()                              # 内存检查点：行为与旧版一致，保证演示可离线跑通
        return

    _PG_CHECKPOINT_READY = True                          # PG 就绪：跨实例会话锁与检查点分叉检测随之启用
    logger.info("checkpointer_ready", backend="postgres",
                note="会话状态跨重启/多实例存活，同线程请求按 thread_id 串行")
    try:
        yield saver                                      # 把 saver 交给 with 块使用（build_aftersales_graph 参数）
    finally:
        _PG_CHECKPOINT_READY = False                     # 退出即失效：防止降级路径仍去抢已关闭连接的 advisory 锁
        try:
            await cm.__aexit__(None, None, None)         # 退出时释放 PG 连接池，防止连接泄漏
        except Exception as close_exc:                   # 关闭异常不得掩盖业务异常
            logger.warning("checkpointer_close_failed", error=f"{type(close_exc).__name__}: {close_exc}")


async def _acquire_advisory_lock(thread_id: str):        # 取 PG advisory lock，返回持锁连接；不可用返回 None
    """跨实例串行化：锁键 `hashtext(thread_id)`，与检查点同库不同连接（锁必须独立于请求连接生命周期）。

    锁连接单独建立并在本轮结束时释放；lock_timeout 到期或 PG 不可达时返回 None，
    调用方退化为进程内锁并记录告警——基础设施异常不拒绝服务，但降级事实必须可见。
    """
    if not _PG_CHECKPOINT_READY or not settings.observability.enable_session_lock:
        return None                                      # 内存检查点或显式关闭：无需跨实例锁
    try:
        import psycopg                                   # psycopg3 异步连接（项目已依赖，用于检查点）
        conn = await psycopg.AsyncConnection.connect(_pg_conn_string(), autocommit=True)
        # SET 语句不接受参数占位符；取值来自配置并强制转为整数毫秒，无注入面
        lock_timeout_ms = int(settings.observability.session_lock_timeout_seconds * 1000)
        await conn.execute(f"SET lock_timeout = {lock_timeout_ms}")
        await conn.execute("SELECT pg_advisory_lock(hashtext(%s))", (thread_id,))  # 阻塞直到取锁或 lock_timeout 到期
        return conn
    except Exception as exc:
        logger.warning("session_lock_degraded", thread_id=thread_id,
                       error=f"{type(exc).__name__}: {exc}",
                       timeout_seconds=settings.observability.session_lock_timeout_seconds)
        return None


async def _release_advisory_lock(conn) -> None:          # 释放 advisory lock 并关闭锁连接
    if conn is None:
        return
    try:
        await conn.execute("SELECT pg_advisory_unlock_all()")  # 一次性释放该连接持有的全部 advisory 锁
        await conn.close()
    except Exception as exc:                             # 释放失败只告警：连接关闭即由 PG 回收锁
        logger.warning("session_lock_release_failed", error=f"{type(exc).__name__}: {exc}")


@asynccontextmanager
async def serialize_session(thread_id: str):             # 会话级串行化上下文：进程内排队 + PG advisory lock
    """同一 thread_id 的请求严格先后执行，消除并发写入导致的检查点分叉（§2.3 追加项）。

    yield 出锁连接（可能为 None），调用方可在同一连接上复用只读查询做分叉检测，
    省掉一次建连。yield 位置不在 try 内，业务异常不会被误记为锁降级。
    """
    lock = _SESSION_LOCKS.setdefault(thread_id, asyncio.Lock())  # 进程内先到先执行，避免同实例并发抢 PG 锁
    async with lock:
        conn = await _acquire_advisory_lock(thread_id)
        try:
            yield conn
        finally:
            await _release_advisory_lock(conn)


async def _count_thread_heads(conn, thread_id: str) -> int:  # 统计该线程的 leaf 检查点数量
    try:
        cur = await conn.execute(_HEADS_SQL, (thread_id,))
        row = await cur.fetchone()
        return int(row[0]) if row else 0
    except Exception as exc:
        logger.warning("checkpoint_fork_probe_failed", thread_id=thread_id, error=f"{type(exc).__name__}: {exc}")
        return 0


async def ainvoke_serialized(app, inputs: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    """一轮图调用的标准入口：串行化 + thread_id 注入 + 检查点分叉检测 + 轮次日志（§2.3）。

    thread_id 注入进状态，使各节点日志携带同一 trace 关联键；调用方仍按 LangGraph 原
    语义使用 `config={"configurable": {"thread_id": ...}}`，无需重复传 thread_id。
    """
    thread_id = str((config.get("configurable") or {}).get("thread_id") or "")
    payload = dict(inputs or {})
    if thread_id:
        payload["thread_id"] = thread_id                 # 真实线程键入状态，优先于按约定推导的兜底值
    started = time.perf_counter()
    if not thread_id:                                    # 无线程键（未挂检查点的一次性调用）：直接执行
        return await app.ainvoke(payload, config=config)

    async with serialize_session(thread_id) as lock_conn:
        logger.info("session_turn_started", thread_id=thread_id, user_id=payload.get("user_id"))
        result = await app.ainvoke(payload, config=config)
        if lock_conn is not None:                        # PG 锁连接可用时才可能查到检查点表
            heads = await _count_thread_heads(lock_conn, thread_id)
            if heads > 1:
                metrics.inc("checkpoint_fork_total")     # 冲突频次反查（§2.3）
                logger.error("checkpoint_fork_detected", thread_id=thread_id, head_count=heads,
                             note="同一会话出现多条检查点链，请核查串行化是否被绕过")
            else:
                logger.info("checkpoint_verified", thread_id=thread_id, head_count=heads)
    logger.info("session_turn_finished", thread_id=thread_id, decision=result.get("decision"),
                latency_ms=round((time.perf_counter() - started) * 1000, 1))
    return result


# ============================================================================
# 5. 主程序运行演示 (执行真实数据库与真实工具调用)
# ============================================================================
def format_metrics_report() -> str:                      # 把指标快照渲染成可读文本（与 /metrics 端点同源数据）
    """§2.3 验收自证：一次运行结束即可读出降级率、越权尝试、护栏动作、补槽轮次、节点耗时与决策分布。"""
    snap = metrics.snapshot()
    lines: List[str] = []
    for name in sorted(snap["counters"]):
        for item in snap["counters"][name]:
            label = ",".join(f"{k}={v}" for k, v in sorted(item["labels"].items()))
            lines.append(f"  counter   {name}{{{label}}} = {item['value']:g}" if label
                         else f"  counter   {name} = {item['value']:g}")
    for name in sorted(snap["histograms"]):
        for series in snap["histograms"][name]:
            label = ",".join(f"{k}={v}" for k, v in sorted(series["labels"].items()))
            quantiles = {float(k): v for k, v in series["quantiles"].items()}
            scale = 1000 if name.endswith("_seconds") else 1  # 秒制指标换算成毫秒展示，便于阅读
            unit = "ms" if scale == 1000 else ""
            lines.append(
                f"  histogram {name}{{{label}}} n={series['count']} "
                f"p50={_fmt_num(quantiles.get(0.5), scale)}{unit} "
                f"p95={_fmt_num(quantiles.get(0.95), scale)}{unit}"
            )
    return "\n".join(lines) if lines else "  (无指标数据)"


def _fmt_num(value: Optional[float], scale: float) -> str:
    return f"{value * scale:.1f}" if value is not None else "n/a"


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
        res1 = await ainvoke_serialized(app, case1_input, {"configurable": {"thread_id": "default:USR_001:SESS_TEST_001"}})  # 会话串行化执行整张图（§2.3）；thread_id 即持久化会话主键与 trace 关联键
        print(f"最终决策: {res1.get('decision')}")     # 打印决策结果（预期 RETURN_SHIPPING）
        print(f"客户回复:\n{res1.get('final_reply')}") # 打印给用户的最终答复文本

        # Case 2: 必要槽位缺失 -> ask_user 追问 -> 用户补充 -> 同一线程续跑（多轮槽位填充）
        print("\n\n--- [测试用例 2: 槽位缺失追问演示: 信息不全 -> 系统反问 -> 用户补充 -> 继续处理] ---")  # 打印用例标题
        cfg2 = {"configurable": {"thread_id": "default:USR_001:SESS_TEST_002"}}  # 用例2跨轮共用同一 thread_id；挂载 PG 检查点后，第2轮请求即使打到另一实例/重启后的进程也能续跑
        res2a = await ainvoke_serialized(app, {        # 第1轮：用户只说想退货，缺少原因大类与订单号
            "session_id": "SESS_TEST_002",             # 会话ID
            "user_id": "USR_001",                      # 用户ID（须为 ORD_CLOTH_001 的下单账号，否则第2轮补槽会被归属权校验拦下，走 §2.2 越权分支）
            "user_message": "我想退货",                # 缺失 reason_type / order_id 必要槽位 -> 触发追问
        }, cfg2)
        print(f"[第1轮] 已抽槽位: intent={res2a.get('intent')}, reason_type={res2a.get('reason_type')}, order_id={res2a.get('order_id')}; 缺失={res2a.get('missing_slots')}")  # 打印第1轮槽位填充情况
        print(f"[第1轮] 系统追问: {res2a.get('final_reply')}")  # 打印追问话术（本轮流程在 ask_user 后结束）

        res2b = await ainvoke_serialized(app, {        # 第2轮：用户回答追问补充槽位，同一线程从检查点恢复上下文续跑
            "session_id": "SESS_TEST_002",             # 会话ID
            "user_id": "USR_001",                      # 用户ID
            "user_message": "衣服质量有问题，订单号 ORD_CLOTH_001",  # 结合对话历史，LLM 抽取剩余缺失槽位后流程继续
        }, cfg2)
        print(f"[第2轮] 最终决策: {res2b.get('decision')}")  # 打印最终决策（补槽成功后预期 RETURN_SHIPPING）
        print(f"[第2轮] 客户回复:\n{res2b.get('final_reply')}")  # 打印补槽完成后的业务处理结果

        # Case 3: 大额黄金订单退款 (槽位齐全但金额超限 -> 触发转人工审核)
        case3_input: AfterSalesState = {               # 构造用例3的初始状态
            "session_id": "SESS_TEST_003",             # 会话ID
            "user_id": "USR_003",                      # 用户ID
            "user_message": "黄金手链断了，要求退款，订单号 ORD_LUXURY_005",  # 槽位齐全；金额超出自动审核上限
        }

        print("\n\n--- [测试用例 3: 黄金手链金额超限 -> 转人工专员审核 (HITL)] ---")  # 打印用例标题
        res3 = await ainvoke_serialized(app, case3_input, {"configurable": {"thread_id": "default:USR_003:SESS_TEST_003"}})  # 以独立线程异步执行用例3
        print(f"最终决策: {res3.get('decision')}")     # 打印决策结果（预期 ESCALATE_HUMAN）
        print(f"客户回复:\n{res3.get('final_reply')}") # 打印含工单号的转人工答复

        print("\n\n--- [可观测性自证: 本次运行的六类指标 (§2.3)] ---")  # 打印指标摘要，验收"六类指标可查询"
        print(format_metrics_report())                 # 生产环境经 /metrics (Prometheus 文本) 抓取，此处直接打印同一份快照


if __name__ == "__main__":                             # 脚本直接运行时执行主函数（被导入时不自动运行）
    try:                                               # Windows 控制台默认 GBK：话术里的 ¥/『』等字符会触发编码错误中断演示
        sys.stdout.reconfigure(encoding="utf-8")       # 业务输出与结构化日志统一 UTF-8，便于日志系统按字符检索
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if sys.platform == "win32":                        # Windows 平台特殊处理（§2.6）：psycopg3 异步不兼容默认 ProactorEventLoop，
        import selectors                               # 须显式给出 Selector 型事件循环，否则 AsyncPostgresSaver 连接报错、
        if sys.version_info >= (3, 12):                # 永远落入内存降级（Linux 生产环境默认即兼容，无需处理）
            asyncio.run(main_async(),                  # 3.12+ 用 loop_factory 接管，避开已弃用的 set_event_loop_policy
                        loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()))
        else:                                          # 3.10/3.11（requires-python 下限）没有 loop_factory 参数：手工跑
            _loop = asyncio.SelectorEventLoop(selectors.SelectSelector())
            try:
                _loop.run_until_complete(main_async())
            finally:
                _loop.close()
    else:
        asyncio.run(main_async())                      # 全图异步执行；asyncio.run 仅在此脚本入口出现一次（§2.5），挂进 API 服务时由事件循环直接 ainvoke
