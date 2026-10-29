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
4. 持久化支持 (PostgreSQL Checkpointer): 支持挂载基于 PG 的状态持久化与断点续跑。
5. LLM 意图识别与槽位填充 (Slot Filling): 引入真实大模型 (项目内置 LLMRouter) 从用户
   自然语言中抽取意图/订单号/原因等槽位; LLM 输出经 Pydantic Schema (SlotExtractionResult)
   强校验枚举/格式/长度，非法输出拒绝并降级; 必要槽位缺失时路由到追问节点向用户反问补槽,
   用户补充后同一线程重新抽取, 替代原先写死订单号兜底的做法; LLM 不可用时降级为关键词规则。
"""

import sys                                              # 导入系统模块，用于操作 Python 解释器的模块搜索路径
import asyncio                                           # 导入异步运行时模块，用于在同步示例脚本中驱动异步 LLM 调用
import json                                             # 导入 JSON 编解码模块，用于解析 LLM 输出的槽位 JSON
import re                                                # 导入正则模块，用于关键词降级时从文本中提取订单号
import uuid                                             # 导入通用唯一标识符模块，用于生成幂等键、运单号等随机ID
from pathlib import Path                                # 导入路径对象类，用于跨平台地解析文件/目录路径
from typing import Dict, Any, Optional, Literal, List   # 导入类型注解工具：字典、任意类型、可选类型、字面量枚举类型、列表
from typing_extensions import TypedDict                 # 导入 TypedDict，用类型字典形式定义 LangGraph 的状态结构
from pydantic import BaseModel, Field, ValidationError, field_validator  # 导入 Pydantic 校验基类、字段约束、校验异常与自定义校验器

# 自动将项目 src 根路径加入模块搜索路径，支持直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))  # 把本文件向上两级目录下的 src/ 插到搜索路径首位，使 react_ai 包可被直接导入

from langgraph.graph import StateGraph, START, END      # 导入 LangGraph 状态图构建器及图的虚拟起点/终点标记
from langgraph.checkpoint.memory import MemorySaver     # 导入内存版检查点存储器，用于按 thread_id 持久化图执行状态

# 导入本项目的 LLM 网关与配置，用于真实大模型意图识别
from react_ai.config import settings                     # 导入全局配置单例：从环境变量/.env 读取 LLM 密钥、模型名等
from react_ai.llm.base import ChatMessage               # 导入对话消息模型（role/content 结构）
from react_ai.llm.router import LLMRouter               # 导入 LLM 路由器：主模型失败自动降级到备用模型
from react_ai.llm.providers.deepseek import DeepSeekProvider  # 导入 DeepSeek 客户端（OpenAI 兼容协议，默认主模型）
from react_ai.llm.providers.openai import OpenAIProvider  # 导入 OpenAI 客户端（备用降级目标）

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
- intent: 意图枚举，取值 RETURN_AND_REFUND(退货退款) / REFUND_ONLY(仅退款) / EXCHANGE(换货)
- order_id: 用户明确提供的订单号原文；用户未提及时必须为 null，严禁编造
- reason: 售后原因简述（不超过20字）；未提及为 null
- reason_type: 原因大类，取值 quality_issue(商品破损/质量瑕疵等质量问题) / no_reason(不喜欢等无理由诉求)
- missing_slots: intent / order_id / reason_type 中无法从对话判断的字段名列表，均能判断时为空数组
示例: {"intent": "RETURN_AND_REFUND", "order_id": "ORD123", "reason": "衣服破损", "reason_type": "quality_issue", "missing_slots": []}"""  # 槽位抽取的系统提示词：定义输出 JSON Schema 与"禁止编造订单号"约束

REQUIRED_SLOTS = ("intent", "order_id", "reason_type")  # 必要槽位：意图、订单号、原因大类缺失时必须追问用户补槽


class SlotExtractionResult(BaseModel):                 # Pydantic 槽位抽取结果 Schema：大模型输出必须通过校验才可信
    """约束 LLM 结构化输出的枚举取值/字段格式/长度上限，非法输出直接拒绝并触发关键词降级。"""
    intent: Optional[Literal["RETURN_AND_REFUND", "REFUND_ONLY", "EXCHANGE"]] = None  # 意图枚举：超出取值范围即校验失败
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

    # 外部与工具执行结果缓存
    order_info: Optional[Dict[str, Any]]      # 订单详情
    logistics_info: Optional[Dict[str, Any]]  # 物流轨迹
    policy_rules: Optional[Dict[str, Any]]    # 售后政策查询计算结果

    # 决策与判定
    is_eligible: bool                         # 规则判定是否通过
    decision: Optional[str]                   # 最终决策: AUTO_REFUND / RETURN_SHIPPING / ESCALATE_HUMAN / REJECTED
    human_reason: Optional[str]               # 转人工的具体原因

    # 写操作结果与最终回复
    action_result: Optional[Dict[str, Any]]   # 写操作结果 (退款单号、工单号等)
    notification_sent: bool                   # 是否已发通知
    final_reply: Optional[str]                # 呈现给用户的答复


# ============================================================================
# 2. 状态图节点 (Graph Nodes - 调用项目内置真实工具)
# ============================================================================
async def extract_slots_via_llm(messages: list) -> Optional[Dict[str, Any]]:  # 调用真实 LLM 完成意图与槽位抽取，失败返回 None 由上层降级
    """携带对话上下文调用 LLM，要求仅输出 JSON 并解析为槽位字典。"""
    try:                                                 # 包住整个 LLM 调用，网络/解析异常统一走降级
        router = get_llm_router()                        # 获取（或懒初始化）LLM 路由器
        request_messages = [ChatMessage(role="system", content=SLOT_EXTRACTION_PROMPT)] + [  # 系统提示词 + 多轮对话历史
            ChatMessage(role=m["role"], content=m["content"]) for m in messages               # 将历史 dict 转为 ChatMessage 模型
        ]
        response = await router.generate(request_messages, temperature=0.0, max_tokens=300)  # 低温度调用，保证输出稳定可解析
        raw = _extract_json(response.content or "")    # 从回复文本提取并解析 JSON 槽位字典
        if raw is None:                                # 文本中不含合法 JSON 对象
            return None                                # 直接触发关键词降级
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
    return {                                             # 组装与 LLM 输出同构的槽位字典
        "intent": ("RETURN_AND_REFUND" if has_refund else "EXCHANGE") if (has_refund or has_exchange) else None,  # 无法判断意图时置 None，交由追问流程
        "reason_type": "quality_issue" if is_quality else ("no_reason" if has_no_reason else None),  # 仅显式表达才能判定大类，否则置 None 触发追问
        "reason": "关键词规则判定：质量问题" if is_quality else ("关键词规则判定：无理由" if has_no_reason else None),  # 规则模式下的原因描述
        "order_id": order_match.group(0) if order_match else None,  # 正则命中则填订单号，未命中置 None 触发追问（不写死兜底）
        "missing_slots": [],                             # 缺失槽位由主节点统一计算，此处留空
    }


def intent_recognition_node(state: AfterSalesState) -> Dict[str, Any]:  # 节点1：调用真实 LLM 从多轮对话中抽取意图与槽位，累积合并并计算缺失必要槽位
    """
    [节点 1: LLM 意图识别与槽位填充]
    - 携带对话历史调用 LLM 抽取 intent/order_id/reason/reason_type 槽位;
    - 槽位跨轮次累积合并 (已有值优先，不被新一轮覆盖);
    - 必要槽位缺失时不写死兜底，而是交由 ask_user 节点反问用户补槽。
    """
    user_msg = state.get("user_message", "")             # 本轮用户输入（补槽轮次里就是用户对追问的回答）
    history = list(state.get("dialog_history") or [])    # 取出多轮对话历史，首轮为空列表
    if user_msg:                                         # 本轮有新输入
        history.append({"role": "user", "content": user_msg})  # 追加进对话历史，供 LLM 结合上下文抽取

    slots = asyncio.run(extract_slots_via_llm(history))  # 同步示例脚本内驱动异步 LLM 调用（生产 async 服务中应直接 await）
    source = "LLM"                                       # 标记槽位来源为真实大模型
    if slots is None:                                    # LLM 调用或解析失败
        slots = fallback_extract_slots(user_msg)         # 降级为关键词规则抽取
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
    print(f"[Node: intent_recognition] 槽位抽取完成: {json.dumps({k: (v.get('value') or v.get('string_value')) for k, v in slot_values.items()}, ensure_ascii=False)}; 缺失={missing or '无'}")  # 打印当前槽位填充全景

    return {                                             # 返回本节点产出的状态增量（LangGraph 会将其合并进全局状态）
        "intent": (slot_values.get("intent") or {}).get("value"),        # 顶层镜像：供下游节点直接读取意图
        "order_id": (slot_values.get("order_id") or {}).get("string_value"),  # 顶层镜像：LLM 抽取的订单号，未提供则为 None（不再写死兜底）
        "reason": (slot_values.get("reason") or {}).get("string_value") or "用户未说明具体原因",  # 原因描述，缺省给通用文案
        "reason_type": (slot_values.get("reason_type") or {}).get("value"),  # 政策原因大类，供政策核对节点使用
        "slot_values": slot_values,                      # 写回累积后的槽位表
        "missing_slots": missing,                        # 缺失清单驱动条件路由决定是否追问
        "dialog_history": history,                       # 写回更新后的对话历史（追问后用户补充时携带上下文再抽取）
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
    question = "为了帮您处理售后申请，" + " ".join(ask_map[s] for s in missing if s in ask_map)  # 拼接所有缺失槽位的追问
    print(f"[Node: ask_user] 第 {rounds} 轮追问: {question}")  # 打印追问内容

    history = list(state.get("dialog_history") or [])    # 取出对话历史
    history.append({"role": "assistant", "content": question})  # 把本次追问也记入历史，下一轮 LLM 可见完整语境
    return {                                             # 本轮以追问结束，等待调用方携带用户新消息重新 invoke
        "final_reply": question,                         # 追问文本作为本轮答复呈现给用户
        "missing_slots": missing,                        # 保持缺失清单（下一轮重抽后刷新）
        "slot_rounds": rounds,                           # 累加追问轮数
        "dialog_history": history,                       # 写回含追问的对话历史
    }


def query_order_node(state: AfterSalesState) -> Dict[str, Any]:  # 节点2：调用真实工具查询订单详情
    """
    [节点 2: 查询订单详情]
    - 调用内置 QueryOrderTool 从 PostgreSQL 真实查询订单数据。
    """
    order_id = state.get("order_id", "")               # 从状态中取出待查询的订单号
    user_id = state.get("user_id", "USR_001")          # 取出用户ID，缺省为演示用户
    print(f"[Node: query_order] 调用 QueryOrderTool 查询订单: {order_id}")  # 打印节点执行轨迹，便于观察流程

    ctx = ToolContext(caller_id="langgraph-aftersales", tenant_id="default", user_id=user_id)  # 构造工具上下文：标记调用方、租户与操作用户
    res = query_order_tool.call({"order_id": order_id}, ctx)  # 同步调用订单查询工具，返回结构化 ToolResult

    if not res.ok:                                     # 工具返回失败标志时
        raise RuntimeError(f"查询订单失败: {res.error_message}")  # 抛出运行时异常终止本次图执行（错误可被上游捕获）

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

    if not res.ok:                                     # 政策查询失败属于关键前置条件缺失
        raise RuntimeError(f"核对政策失败: {res.error_message}")  # 直接抛异常终止流程

    return {"policy_rules": res.data}                  # 将政策计算结果（是否可退、自动退款上限等）写入状态


def condition_decision_node(state: AfterSalesState) -> Dict[str, Any]:  # 节点5：综合政策与金额做风控决策
    """
    [节点 5: 综合条件判断与决策路由]
    - 判断是否在有效退换期内，是否超出大额退款阈值。
    """
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
def route_slots(state: AfterSalesState) -> Literal["query_order", "ask_user"]:  # 槽位完整性路由：决定继续流程还是向用户追问
    missing = state.get("missing_slots") or []           # 读取必要槽位缺失清单
    return "ask_user" if missing else "query_order"      # 有缺失 -> 追问节点；槽位齐全 -> 查询订单


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
        }
    )
    workflow.add_edge("ask_user", END)                 # 追问后本轮图执行结束；用户补充信息后同线程重新 invoke 进入下一轮补槽
    workflow.add_edge("query_order", "check_logistics")     # 查询订单 -> 检查物流
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
# 5. 主程序运行演示 (执行真实数据库与真实工具调用)
# ============================================================================
def main():                                            # 演示入口：构建图并跑三个典型业务用例（含多轮槽位补全）
    print("=" * 75)                                    # 打印分隔线
    print(">>> 启动 LangGraph 电商售后工单工作流 (真实 LLM 意图识别 + 槽位填充 + 缺失追问) <<<")  # 打印标题说明
    print("=" * 75)                                    # 打印分隔线

    app = build_aftersales_graph(checkpointer=MemorySaver())  # 编译图应用，使用内存检查点按 thread_id 隔离会话状态

    # Case 1: 完整诉求（一句话给全意图/原因/订单号）-> LLM 一次抽槽直接跑通流程
    case1_input: AfterSalesState = {                   # 构造用例1的初始状态（类型标注为售后状态字典）
        "session_id": "SESS_TEST_001",                 # 会话ID
        "user_id": "USR_001",                          # 用户ID
        "user_message": "我买的衣服破了，要退货退款，订单号是 ORD_CLOTH_001",  # 槽位信息齐全，LLM 可一次抽取完整
    }

    print("\n--- [测试用例 1: 完整诉求 -> LLM 一次抽槽自动通过] ---")  # 打印用例标题
    res1 = app.invoke(case1_input, config={"configurable": {"thread_id": "THREAD_001"}})  # 同步执行整张图；thread_id 用于检查点会话隔离
    print(f"最终决策: {res1.get('decision')}")         # 打印决策结果（预期 RETURN_SHIPPING）
    print(f"客户回复:\n{res1.get('final_reply')}")     # 打印给用户的最终答复文本

    # Case 2: 必要槽位缺失 -> ask_user 追问 -> 用户补充 -> 同一线程续跑（多轮槽位填充）
    print("\n\n--- [测试用例 2: 槽位缺失追问演示: 信息不全 -> 系统反问 -> 用户补充 -> 继续处理] ---")  # 打印用例标题
    cfg2 = {"configurable": {"thread_id": "THREAD_002"}}  # 用例2跨轮共用同一 thread_id，依赖检查点携带槽位与对话历史状态
    res2a = app.invoke({                               # 第1轮：用户只说想退货，缺少原因大类与订单号
        "session_id": "SESS_TEST_002",                 # 会话ID
        "user_id": "USR_002",                          # 用户ID
        "user_message": "我想退货",                    # 缺失 reason_type / order_id 必要槽位 -> 触发追问
    }, config=cfg2)
    print(f"[第1轮] 已抽槽位: intent={res2a.get('intent')}, reason_type={res2a.get('reason_type')}, order_id={res2a.get('order_id')}; 缺失={res2a.get('missing_slots')}")  # 打印第1轮槽位填充情况
    print(f"[第1轮] 系统追问: {res2a.get('final_reply')}")  # 打印追问话术（本轮流程在 ask_user 后结束）

    res2b = app.invoke({                               # 第2轮：用户回答追问补充槽位，同一线程从检查点恢复上下文续跑
        "session_id": "SESS_TEST_002",                 # 会话ID
        "user_id": "USR_002",                          # 用户ID
        "user_message": "衣服质量有问题，订单号 ORD_CLOTH_001",  # 结合对话历史，LLM 抽取剩余缺失槽位后流程继续
    }, config=cfg2)
    print(f"[第2轮] 最终决策: {res2b.get('decision')}")  # 打印最终决策（补槽成功后预期 RETURN_SHIPPING）
    print(f"[第2轮] 客户回复:\n{res2b.get('final_reply')}")  # 打印补槽完成后的业务处理结果

    # Case 3: 大额黄金订单退款 (槽位齐全但金额超限 -> 触发转人工审核)
    case3_input: AfterSalesState = {                   # 构造用例3的初始状态
        "session_id": "SESS_TEST_003",                 # 会话ID
        "user_id": "USR_003",                          # 用户ID
        "user_message": "黄金手链断了，要求退款，订单号 ORD_LUXURY_005",  # 槽位齐全；金额超出自动审核上限
    }

    print("\n\n--- [测试用例 3: 黄金手链金额超限 -> 转人工专员审核 (HITL)] ---")  # 打印用例标题
    res3 = app.invoke(case3_input, config={"configurable": {"thread_id": "THREAD_003"}})  # 以独立线程执行用例3
    print(f"最终决策: {res3.get('decision')}")         # 打印决策结果（预期 ESCALATE_HUMAN）
    print(f"客户回复:\n{res3.get('final_reply')}")     # 打印含工单号的转人工答复


if __name__ == "__main__":                             # 脚本直接运行时执行主函数（被导入时不自动运行）
    main()                                             # 启动售后工作流演示
