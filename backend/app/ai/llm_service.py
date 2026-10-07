"""LLM 统一入口（Phase 1 AI Chat MVP，Phase 3 加 bind_tools）。

为什么需要这一层：
    Phase 3 工具调用（bind_tools）、Phase 4 图节点、Phase 5 多 Agent 都要调模型。
    如果各模块自己 new ChatOpenAI，将来换模型 / 加重试 / 加观测（如 Langfuse）
    就要改 N 处。收敛成单一入口后，只改本文件。

约定（写实现时守住这三条）：
    1. 模型对象只在本文件创建，其他模块 import llm_service，不 import ChatOpenAI；
    2. 所有参数从 app.core.config.settings 读，不写死 key / 模型名；
    3. 实例只建一次（放 __init__），不要在方法内重复 new。
"""
from collections.abc import AsyncIterator
from dataclasses import dataclass

from langchain_core.messages import AIMessageChunk, BaseMessage
from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from app.ai.tools.base import ToolCallRequest
from app.core.config import settings


@dataclass
class StreamChunk:
    """流式输出的一块：文本增量 / 用量统计 / 模型要求调工具。

    为什么要有"类型"：usage 块的 content 是空的，靠 content 区分不出来，
    所以由本层翻译成显式的 type，调用方按 type 分派。

    type == "tool_calls" 是**一轮的结束信号**，不是增量：
        模型决定调工具时，这一轮不会产出面向用户的文字（content 为空），
        只会吐一串 tool_call 片段。片段是分多次到达的（参数一个 token
        一个 token 地流），本层用 LangChain 的 AIMessageChunk 相加把它拼完整，
        在流结束时一次性吐出。调用方拿到它就意味着：该执行工具了。
    """

    type: str  # "text" | "usage" | "tool_calls"
    text: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_calls: list[ToolCallRequest] | None = None


class LLMService:
    """模型调用统一入口。"""

    def __init__(self) -> None:
        self._llm = ChatOpenAI(
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
            stream_usage=True,  # 不设这个，流式就拿不到 token 统计
        )

    async def achat(self, messages: list[BaseMessage]) -> str:
        """非流式：给一串消息，一次性返回完整文本。"""
        resp = await self._llm.ainvoke(messages)
        return resp.content

    async def achat_stats(self, messages: list[BaseMessage]) -> tuple[str, int, int]:
        """非流式 + token 统计，返回 (全文, prompt_tokens, completion_tokens)。

        Phase 4 图节点用：既要全文也要 usage。不要"achat 拿文本再补一次带
        统计的调用"——那是两次计费；token 就在一次响应的 usage_metadata 里。
        """
        resp = await self._llm.ainvoke(messages)
        usage = resp.usage_metadata or {}
        return resp.content, usage.get("input_tokens", 0), usage.get("output_tokens", 0)

    async def astream(
        self,
        messages: list[BaseMessage],
        *,
        tools: list[dict] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """流式：逐块产出 StreamChunk（文本增量 + 末尾的 usage + 可能的 tool_calls）。

        tools 传的是 OpenAI function calling 格式的 Schema 列表
        （用 registry.to_openai_schema 生成）；不传就是普通对话。

        注意：本方法是异步生成器，实现体内必须出现 yield，
              否则它就变成了普通协程，调用方 async for 会报错。

        为什么用 usage_metadata 判断、而不是"最后一个 chunk"：
            实测一轮流式是 [空帧] + [N 个文本帧] + [usage 帧] + [空帧]，
            usage 帧后面还有空帧，靠位置判断会漏。

        为什么文本帧和 usage 帧要**各判各的**、不能用 elif 串起来：
            同一次流里，最后一个文本块完全可能同时也带 usage（模型一个字
            一个字出，usage 常挂在倒数第二个有内容的块上）。用 elif 的话
            usage 会被静默丢掉 —— 表现是"偶尔一次对话 token 统计是 0"，
            金额对不上却查不出原因。
        """
        # 不传 tools 就别 bind：绑一个空列表在某些实现里会被当成
        # "本次不允许调用任何工具"，语义上反而更容易出偏差
        bound = self._llm.bind_tools(tools) if tools else self._llm

        # 累积整轮的 chunk。tool_call 片段是分多次到达的，只有相加后才能得到
        # 完整可解析的 tool_calls（LangChain 会按 index 把 name / args 拼起来）
        merged: AIMessageChunk | None = None

        async for chunk in bound.astream(messages):
            merged = chunk if merged is None else merged + chunk

            if chunk.content:
                yield StreamChunk(type="text", text=chunk.content)
            if chunk.usage_metadata:
                # 命名映射：LangChain 叫 input/output，OpenAI 叫 prompt/completion。
                # 统一成后者的叫法，和 messages 表的列名保持一致。
                yield StreamChunk(
                    type="usage",
                    prompt_tokens=chunk.usage_metadata["input_tokens"],
                    completion_tokens=chunk.usage_metadata["output_tokens"],
                )

        # 一轮结束：模型如果要调工具，在这里一次性交出去。
        # 必须等整轮流完才能解析 —— tool_calls 里的 args 是流式拼出来的，
        # 中途取到的永远是半截 JSON，直接 json.loads 会炸。
        #
        # 为什么要并入 invalid_tool_calls：模型偶尔会把工具参数生成到超长、撞上
        # max_tokens，args JSON 被截断 → LangChain 把它丢进 invalid_tool_calls、
        # tool_calls 留空。若只认 tool_calls，这一轮就被误判成"模型不再调工具 = 最终
        # 回答"，落一条空 AIMessage 当答案（无错误、脏历史）。这里把截断的调用以空 args
        # 交出去，让 registry 的 Pydantic 校验把它当"参数不合法"回喂模型 → 它自纠重试。
        if merged is not None:
            pending = [
                ToolCallRequest(id=tc["id"] or "", name=tc["name"], args=tc["args"])
                for tc in merged.tool_calls
            ]
            pending += [
                ToolCallRequest(id=(itc.get("id") or ""), name=itc["name"], args={})
                for itc in (merged.invalid_tool_calls or [])
                if (itc.get("name") or "").strip()  # 无名 = 无从调起，跳过（否则会带空 tool_call_id 400）
            ]
            if pending:
                yield StreamChunk(type="tool_calls", tool_calls=pending)

    def bind_structured(
        self, schema: type[BaseModel]
    ) -> Runnable[list[BaseMessage], dict]:
        """把模型绑定到一个结构化输出 schema（Phase 3）。

        为什么这件事必须写在 llm_service 里、而不是 structured_output.py 里：
            它是**在模型对象上调用的**（with_structured_output 是 ChatOpenAI 的方法）。
            本文件的约定是"模型实例只在这里出现"，所以绑定动作也留在这里，
            structured_output.py 只负责"绑定之后怎么校验、失败怎么办"。

        为什么显式指定 method="function_calling"，而不用它默认的 json_schema：
            默认的 json_schema 走 HTTP 的 response_format 参数，要求服务端实现
            结构化输出协议；DeepSeek 目前不支持，会直接报错。function_calling
            走的是工具调用通道 —— 和 tool loop 用的是同一条链路，
            在本项目已经实测可用。

        为什么 include_raw=True：
            解析失败时要拿到**模型原始输出**和**解析报错**才能告诉模型"你哪里不对"。
            不开这个开关，LangChain 在解析失败时直接抛异常，错误信息里没有原始内容，
            我们只能重试同一句话 —— 模型大概率还是会错第二遍。
        """
        return self._llm.with_structured_output(
            schema, method="function_calling", include_raw=True
        )


# 模块级单例：其他模块 from app.ai.llm_service import llm_service 直接用
llm_service = LLMService()
