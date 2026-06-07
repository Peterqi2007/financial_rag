# -*- coding: utf-8 -*-
"""
chat/services.py
============================================================
LLM 对话服务层（面向未来多厂商）
------------------------------------------------------------
本模块采用 "抽象基类 + 具体厂商类 + 工厂函数" 的三层设计：

    BaseLLMProvider        —— 抽象基类，定义统一对外接口
        └── QwenProvider   —— 阿里云 DashScope 千问（当前唯一实现）
        └── (未来) OpenAIProvider / AnthropicProvider / GeminiProvider ...

视图层只需依赖 get_llm_provider(user) 工厂与 BaseLLMProvider 接口，
对具体厂商完全无感。

新增一个厂商的步骤：
    1. 新建 class XxxProvider(BaseLLMProvider)，实现 _make_client /
       stream_chat / chat 三个方法（还可覆写 for_user 来自定义 API
       Key 读取逻辑）。
    2. 在 _PROVIDER_REGISTRY 里注册 {"xxx": XxxProvider}。
    3. 在 UserProfile 加一个 `llm_provider` 字段（未来），值为 "xxx"。
       在此之前 get_llm_provider 会自动 fallback 到 "qwen"。

本模块同时保留旧 API 的函数式入口（iter_qwen_stream_text /
chat_completion / get_qwen_client / stream_chat_completion），
底下全部委托给新的类实现，保证历史调用点零修改地继续工作。
"""

from abc import ABC, abstractmethod
from typing import Iterator, List, Dict, Tuple, Optional, Type

from openai import OpenAI
from openai import (
    APIError,
    AuthenticationError,
    RateLimitError,
    APIConnectionError,
    NotFoundError,
)

from django.conf import settings  # noqa: F401  # 可能在未来新增厂商时用到
from .models import UserProfile, ChatEntry, ChatMessage  # noqa: F401

import logging
import itertools
import sys
import time
import requests
import json

logger = logging.getLogger(__name__)


# ==============================================
# 统一异常体系（厂商无关）
# ----------------------------------------------
# 视图层只需 except LLMError 就能覆盖所有来自 LLM 服务的错误，
# 具体原因可通过 isinstance 细分，也可直接拿 .message 展示给用户。
# ==============================================
class LLMError(Exception):
    """LLM 服务层统一基类异常。所有厂商错误都归一到此体系。"""


class LLMAuthError(LLMError):
    """API Key 无效 / 权限不足 / 账号冻结等鉴权错误。"""


class LLMRateLimitError(LLMError):
    """触发频率限制 / 额度耗尽 / 并发超限。"""


class LLMConnectionError(LLMError):
    """网络层错误：DNS / TCP / TLS / 读超时等。"""


class LLMModelNotFoundError(LLMError):
    """模型不存在 / 未开通 / 路由错误。"""


class LLMConfigError(LLMError):
    """配置层错误：用户未填 Key、UserProfile 缺失等。"""


# ==============================================
# 诊断用：统计真正打到 LLM 服务器的 HTTP 请求次数
# （保留，便于以后继续排查"看起来被调了 2 次"这类怪事）
# ==============================================
_LLM_CREATE_COUNTER = itertools.count(1)



# ==============================================
# RAG 外部知识库检索服务（Coze Coding）
# ==============================================
class RAGService:
    """Coze 知识库检索增强服务。向云上 Coze API 发送用户问题，获取相关知识。"""

    def __init__(self, base_url, api_token, dataset_name="fin_reports", top_k=10, min_score=0.5):
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self.dataset_name = dataset_name
        self.top_k = top_k
        self.min_score = min_score

    def retrieve(self, query):
        """
        调用 Coze API 检索相关知识。
        统一返回格式：{"success": bool, "answer": str, "source": str, "raw": dict}
        失败返回 None——RAG 失败不应阻断对话。
        """
        payload = {
            "mode": "search",
            "documents": [],
            "dataset_name": self.dataset_name,
            "max_level": 3,
            "original_query": query,
            "top_k": self.top_k,
            "min_score": self.min_score,
        }
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
        }
        try:
            resp = requests.post(self.base_url, headers=headers, json=payload, timeout=60)
            if resp.status_code != 200:
                logger.warning(f"[RAG] Coze API returned {resp.status_code}: {resp.text[:200]}")
                return None
            data = resp.json()

            # 格式 1：reranked_results — Coze 重排序后的文档切片（当前主格式）
            reranked = data.get("reranked_results") if isinstance(data, dict) else None
            if isinstance(reranked, list) and reranked:
                answer_parts = []
                source_parts = []
                seen_sources = set()
                for i, doc in enumerate(reranked):
                    content = doc.get("content", "")
                    if not content:
                        continue
                    # 仅编号，不标注来源信息（避免 LLM 自行拼出来源段落）
                    label = f"[参考片段 {i+1}]"
                    answer_parts.append(f"{label}\n{content.strip()}")
                    # 从 source_file + hierarchy_path 构建来源
                    src_file = doc.get("source_file", "")
                    hierarchy = doc.get("hierarchy_path", "")
                    if src_file:
                        src_line = f"{src_file}：{hierarchy}" if hierarchy else src_file
                        if src_line not in seen_sources:
                            seen_sources.add(src_line)
                            source_parts.append(src_line)

                logger.info(
                    f"[RAG] reranked_results: {len(reranked)} docs, "
                    f"{len(source_parts)} unique sources"
                )
                sys.stdout.write(
                    f"[RAG] reranked_results: {len(reranked)} docs, "
                    f"{len(source_parts)} unique sources\n"
                ); sys.stdout.flush()
                return {
                    "success": data.get("success", True),
                    "answer": "\n\n".join(answer_parts),
                    "source": "\n".join(source_parts) if source_parts else "",
                    "raw": data,
                }

            # 格式 1-旧：Coze QA 工作流（extracted_answer + extracted_source，已废弃，保留兼容）
            if isinstance(data, dict) and "extracted_answer" in data:
                logger.info(f"[RAG] Coze QA (legacy) matched: {data.get('message', '')}")
                return {
                    "success": data.get("success", True),
                    "answer": data.get("extracted_answer", ""),
                    "source": data.get("extracted_source", ""),
                    "raw": data,
                }

            # 格式 2：传统检索结果（documents 数组）
            docs = []
            if isinstance(data, dict):
                if "data" in data and "documents" in data["data"]:
                    docs = data["data"]["documents"]
                elif "documents" in data:
                    docs = data["documents"]
                elif "results" in data:
                    docs = data["results"]
                elif "items" in data:
                    docs = data["items"]
            if isinstance(data, list):
                docs = data

            if docs:
                logger.info(f"[RAG] retrieved {len(docs)} documents")
                answer_parts = []
                sources = []
                for doc in docs:
                    content = doc.get("content") or doc.get("text") or str(doc)
                    title = doc.get("title") or doc.get("source") or ""
                    if content:
                        answer_parts.append(content)
                    if title:
                        sources.append(title)
                return {
                    "success": True,
                    "answer": "\n\n".join(answer_parts),
                    "source": ", ".join(sources) if sources else "",
                    "raw": data,
                }

            logger.warning(f"[RAG] unrecognized JSON structure: {list(data.keys()) if isinstance(data, dict) else type(data)}")
            return None

        except Exception as e:
            logger.warning(f"[RAG] Coze API call failed: {e}")
            return None

    def format_context(self, result):
        """
        将检索结果格式化为 LLM prompt 中的参考上下文。
        result: retrieve() 返回的统一格式 dict 或 None。

        在检索结果前添加指令，要求 LLM 优先据此回答。
        """
        if not result or not result.get("answer"):
            return ""
        parts = [
            "=== 以下是从知识库中检索到的参考资料 ===",
            "请优先根据以下资料回答问题。如果资料内容与你的已有知识冲突，以资料为准。",
            "【重要】不要在回复中提及任何文件名、章节标题或知识库来源，来源标注由系统自动附加。",
            "你可以引用资料中的事实和数据，但不要说明它们出自哪个文件。",
            "",
        ]
        parts.append(result["answer"])
        parts.append("=== 参考资料结束 ===")
        return "\n".join(parts)


# ==============================================
# 抽象基类
# ==============================================
class BaseLLMProvider(ABC):
    """
    所有 LLM 厂商客户端的基类。

    约束子类必须实现：
        - _make_client()            构造底层 SDK 客户端
        - stream_chat(chat_entry, user_message)
                                    生成器：yield event_type, payload
                                    event_type ∈ {"delta","error","done"}
                                    绝不 raise
        - chat(chat_entry, user_message)  -> str
                                    一次性返回完整回复，可抛 LLMError 子类

    可选覆写：
        - for_user(user)            从 UserProfile 读取凭据并构造实例
    """

    name: str = "base"  # 子类必须设置一个唯一短名（qwen / openai / ...）

    # 子类可覆盖默认值；也可由 __init__ 参数覆盖
    DEFAULT_BASE_URL: str = ""
    DEFAULT_MODEL: str = ""

    def __init__(
        self,
        user,
        *,
        api_key: str,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        rag_service = None,
    ):
        self.user = user
        self._api_key = api_key
        self._base_url = base_url or self.DEFAULT_BASE_URL
        self._model = model or self.DEFAULT_MODEL
        self._rag_service = rag_service  # RAG 检索服务实例（可选）
        self._rag_last_source = None  # 最近一次 RAG 检索的来源（供视图层落库）
        self._client = self._make_client()

    # ---------- 钩子 ----------
    @classmethod
    def for_user(cls, user) -> "BaseLLMProvider":
        """
        从 UserProfile 读取该用户的 API Key 并构造 Provider。
        当前所有厂商都共用 UserProfile.api_key 字段；未来若要支持多
        Key（不同厂商各存一把），可在 UserProfile 增加 JSON/多字段，
        并在具体子类里覆写本方法。
        """
        try:
            profile = UserProfile.objects.get(user=user)
        except UserProfile.DoesNotExist:
            msg = f"用户资料不存在：用户ID {user.id} 未创建个人资料，请先完成资料创建"
            logger.error(f"[LLM 客户端初始化失败] {msg}")
            raise LLMConfigError(msg)

        if not profile.api_key:
            msg = "用户未配置 API Key：请前往个人资料页填写有效的 API Key"
            logger.error(f"[LLM 客户端初始化失败] {msg} | 用户ID: {user.id}")
            raise LLMConfigError(msg)

        if len(profile.api_key.strip()) < 10:
            msg = "API Key 格式无效：请检查是否复制完整的 Key（长度过短）"
            logger.error(f"[LLM 客户端初始化失败] {msg} | 用户ID: {user.id}")
            raise LLMConfigError(msg)

        # 读取用户默认模型（若未设置则使用 Provider 类自身的 DEFAULT_MODEL）
        default_model = profile.default_model or cls.DEFAULT_MODEL

        # 构造 RAG 知识库检索服务（若用户启用）
        rag_service = None
        if profile.rag_enabled and profile.rag_api_token:
            rag_service = RAGService(
                base_url=profile.rag_base_url,
                api_token=profile.rag_api_token,
                dataset_name=profile.rag_dataset_name or "fin_reports",
                top_k=profile.rag_top_k,
                min_score=profile.rag_min_score,
            )
            logger.info(f"[RAG] enabled for user_id={user.id}")
            sys.stdout.write(f"\n[RAG] enabled for user_id={user.id}\n"); sys.stdout.flush()

        return cls(user, api_key=profile.api_key, model=default_model, rag_service=rag_service)

    @abstractmethod
    def _make_client(self):
        """构造底层 SDK 客户端实例（OpenAI / Anthropic / ...）。"""

    @abstractmethod
    def stream_chat(
        self, chat_entry: "ChatEntry", user_message: str
    ) -> Iterator[Tuple[str, str]]:
        """
        流式对话生成器。无论任何异常，都在内部吞掉并以
        ("error", msg) 事件 yield 给上层，最后必 yield 一个
        ("done", full_text)，**绝不向外 raise**——这样
        StreamingHttpResponse 才不会吞异常导致前端看到空响应。
        """

    @abstractmethod
    def chat(self, chat_entry: "ChatEntry", user_message: str) -> str:
        """非流式一次性对话。失败时抛 LLMError 子类。"""

    # ---------- 共享工具 ----------
    def _build_messages(
        self,
        chat_entry: "ChatEntry",
        *,
        append_user: bool = False,
        user_message: str = "",
        user_query: str = "",
    ) -> List[Dict[str, str]]:
        """
        把 ChatEntry 的 system_prompt + 历史 ChatMessage 组装成
        OpenAI 兼容格式（role / content）。

        若用户启用了 RAG 知识库且当前对话 use_rag=True，
        则先调用 Coze API 检索相关文档，将检索结果拼入 system prompt。

        :param append_user: 是否在末尾追加一条 user_message。
        :param user_query:  用于 RAG 检索的查询文本（通常即用户原始问题）。
        """
        # 构建 system prompt，可能包含 RAG 检索结果
        system_content = chat_entry.system_prompt

        # 每次调用时重置，避免跨对话残留旧来源
        self._rag_last_source = None

        # RAG 检索增强：Provider 持 RAGService 且对话级开关开启时触发
        if self._rag_service is not None and chat_entry.use_rag:
            query = user_query or user_message
            if query:
                try:
                    sys.stdout.write(f"\n[RAG] querying: {query[:80]}...\n"); sys.stdout.flush()
                    result = self._rag_service.retrieve(query)
                    if result and result.get("answer"):
                        rag_context = self._rag_service.format_context(result)
                        system_content = system_content + "\n\n" + rag_context
                        self._rag_last_source = result.get("source", "")  # 供视图层落库
                        logger.info(f'[RAG] context injected, chat_id={chat_entry.id} source={result.get("source","")}')
                        sys.stdout.write(f"\n[RAG] context injected, chat_id={chat_entry.id}\n"); sys.stdout.flush()
                except Exception as e:
                    logger.warning(f"[RAG] skipped (error): {e}")
                    sys.stdout.write(f"\n[RAG] error: {e}\n"); sys.stdout.flush()

        messages: List[Dict[str, str]] = [
            {"role": "system", "content": system_content}
        ]
        for msg in chat_entry.messages.all():
            messages.append({"role": msg.role, "content": msg.content})
        if append_user and user_message:
            messages.append({"role": "user", "content": user_message})
        return messages


# ==============================================
# 具体实现：阿里云 DashScope 千问（OpenAI 兼容模式）
# ==============================================
class QwenProvider(BaseLLMProvider):
    """
    通过 DashScope 的 "OpenAI 兼容模式" 调用千问系列模型。
    因此底层直接复用 `openai` Python SDK。
    """

    name = "qwen"
    DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    DEFAULT_MODEL = "qwen-plus"

    # ---------- 客户端构造 ----------
    def _make_client(self):
        # ⚠️ max_retries=0 的说明：OpenAI Python SDK 2.x 默认 max_retries=2，
        # 遇到网络抖动 / 5xx / 408 / 429 会静默重试。流式连接尤其敏感，
        # 会导致 Django 视图只跑一次但 DashScope 那边被计为多次调用。
        # 这里显式关闭 SDK 层重试，所有重试策略交给上层业务显式决定。
        client = OpenAI(
            api_key=self._api_key,
            base_url=self._base_url,
            max_retries=0,
        )
        logger.info(f"[Qwen 客户端初始化成功] 用户ID: {self.user.id}")
        return client

    # ---------- 公共参数打包 ----------
    def _completion_kwargs(self, chat_entry: "ChatEntry", messages, *, stream: bool):
        return dict(
            model=self._model,
            messages=messages,
            temperature=chat_entry.temperature,
            top_p=chat_entry.top_p,
            max_tokens=chat_entry.max_tokens,
            stream=stream,
        )

    # ---------- 异常转译：厂商异常 → 统一 LLMError ----------
    @staticmethod
    def _translate_exception(e: Exception) -> LLMError:
        if isinstance(e, AuthenticationError):
            return LLMAuthError(f"API Key 认证失败：{e}")
        if isinstance(e, RateLimitError):
            return LLMRateLimitError(f"触发频率/额度限制：{e}")
        if isinstance(e, APIConnectionError):
            return LLMConnectionError(f"网络连接失败：{e}")
        if isinstance(e, NotFoundError):
            return LLMModelNotFoundError(f"模型/接口不存在：{e}")
        if isinstance(e, APIError):
            return LLMError(f"千问服务异常：{e}")
        return LLMError(f"未知错误：{e}")

    # ---------- 流式 ----------
    def stream_chat(self, chat_entry: "ChatEntry", user_message: str):
        chat_entry_id = chat_entry.id
        user_id = chat_entry.user.id
        logger.info(f"[Qwen 流式对话开始] chat_id={chat_entry_id} user_id={user_id}")

        # ⚠️ chat_stream 视图已经在调用本方法前把 user_message 落库了，
        # 这里 append_user=False，避免在发给模型的 messages 里重复追加，
        # 那会污染上下文，并让 DashScope 日志看起来"被调了 2 次"。
        messages = self._build_messages(chat_entry, append_user=False, user_query=user_message)

        # RAG 检索完成 → 通知前端切换气泡
        if self._rag_service is not None and chat_entry.use_rag:
            yield "status", "rag_done"

        full_text_parts: List[str] = []
        stream = None
        try:
            create_seq = next(_LLM_CREATE_COUNTER)
            sys.stdout.write(
                f"===== [llm.create #{create_seq}] provider={self.name} "
                f"chat_id={chat_entry_id} user_id={user_id} "
                f"ts={time.strftime('%H:%M:%S')} "
                f"msgs_len={len(messages)} max_retries=0\n"
            )
            sys.stdout.flush()
            stream = self._client.chat.completions.create(
                **self._completion_kwargs(chat_entry, messages, stream=True)
            )
        except Exception as e:
            err = self._translate_exception(e)
            logger.error(
                f"[Qwen 流式 建立连接失败] {err} | chat_id={chat_entry_id} user_id={user_id}"
            )
            yield "error", str(err)
            yield "done", ""
            return

        try:
            for chunk in stream:
                try:
                    choice = chunk.choices[0] if chunk.choices else None
                    if not choice:
                        continue
                    delta = getattr(choice, "delta", None)
                    piece = getattr(delta, "content", None) if delta else None
                    if piece:
                        full_text_parts.append(piece)
                        yield "delta", piece
                except Exception as inner:
                    logger.warning(f"[Qwen 流式 chunk 解析异常] {inner}")
                    continue
        except Exception as e:
            yield "error", f"读取流中断：{e}"
        finally:
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass

        yield "done", "".join(full_text_parts)

    # ---------- 非流式 ----------
    def chat(self, chat_entry: "ChatEntry", user_message: str) -> str:
        chat_entry_id = chat_entry.id
        user_id = chat_entry.user.id
        logger.info(f"[Qwen 非流式对话开始] chat_id={chat_entry_id} user_id={user_id}")

        # 非流式入口：视图只保存用户消息后立即调用本方法，且不会重复构造上下文；
        # 为了与旧 chat_completion 行为一致，显式 append_user=True。
        messages = self._build_messages(
            chat_entry, append_user=True, user_message=user_message, user_query=user_message
        )

        try:
            completion = self._client.chat.completions.create(
                **self._completion_kwargs(chat_entry, messages, stream=False)
            )
            return completion.choices[0].message.content
        except Exception as e:
            err = self._translate_exception(e)
            logger.error(
                f"[Qwen 非流式 调用失败] {err} | chat_id={chat_entry_id} user_id={user_id}"
            )
            raise err


# ==============================================
# 具体实现：DeepSeek API（OpenAI 兼容模式）
# ----------------------------------------------
# DeepSeek V4 模型存在「思考内容」(reasoning_content)，
# 即模型在输出最终回复前会先输出一段内部推理文本。
# 当前策略：抛弃所有思考内容，仅保留最终回复 (content)。
# ==============================================
class DeepSeekProvider(BaseLLMProvider):
    """
    通过 DeepSeek 官方 API 调用 deepseek-v4-pro 等模型。
    DeepSeek API 兼容 OpenAI 接口格式，因此底层复用 `openai` Python SDK。

    与标准 OpenAI 的关键区别：
        - 响应中可能包含 reasoning_content（思考链），本 Provider 会主动过滤
        - 流式 delta 中可能出现仅含 reasoning_content 不含 content 的 chunk
    """

    name = "deepseek"
    # DeepSeek API 基础地址（OpenAI 兼容端点）
    DEFAULT_BASE_URL = "https://api.deepseek.com"
    # 默认模型：deepseek-v4-pro（支持深度推理）
    DEFAULT_MODEL = "deepseek-v4-pro"

    # ---------- 客户端构造 ----------
    def _make_client(self):
        """构造 OpenAI 兼容客户端，指向 DeepSeek API。"""
        # max_retries=0：与 QwenProvider 一致，关闭 SDK 层自动重试，
        # 避免流式连接中断后被静默重连导致重复计费。
        client = OpenAI(
            api_key=self._api_key,
            base_url=self._base_url,
            max_retries=0,
        )
        logger.info(f"[DeepSeek 客户端初始化成功] 用户ID: {self.user.id}")
        return client

    # ---------- 公共参数打包 ----------
    def _completion_kwargs(self, chat_entry, messages, *, stream):
        """打包 DeepSeek API 调用参数。"""
        return dict(
            model=self._model,
            messages=messages,
            temperature=chat_entry.temperature,
            top_p=chat_entry.top_p,
            max_tokens=chat_entry.max_tokens,
            stream=stream,
        )

    # ---------- 异常转译：DeepSeek 异常 → 统一 LLMError ----------
    @staticmethod
    def _translate_exception(e):
        if isinstance(e, AuthenticationError):
            return LLMAuthError(f"DeepSeek API Key 认证失败：{e}")
        if isinstance(e, RateLimitError):
            return LLMRateLimitError(f"DeepSeek 触发频率/额度限制：{e}")
        if isinstance(e, APIConnectionError):
            return LLMConnectionError(f"DeepSeek 网络连接失败：{e}")
        if isinstance(e, NotFoundError):
            return LLMModelNotFoundError(f"DeepSeek 模型/接口不存在：{e}")
        if isinstance(e, APIError):
            return LLMError(f"DeepSeek 服务异常：{e}")
        return LLMError(f"DeepSeek 未知错误：{e}")

    # ---------- 流式对话 ----------
    def stream_chat(self, chat_entry, user_message):
        chat_entry_id = chat_entry.id
        user_id = chat_entry.user.id
        logger.info(f"[DeepSeek 流式对话开始] chat_id={chat_entry_id} user_id={user_id}")

        messages = self._build_messages(chat_entry, append_user=False, user_query=user_message)

        # RAG 检索完成 → 通知前端切换气泡
        if self._rag_service is not None and chat_entry.use_rag:
            yield "status", "rag_done"

        full_text_parts = []
        stream = None
        try:
            create_seq = next(_LLM_CREATE_COUNTER)
            kwargs = self._completion_kwargs(chat_entry, messages, stream=True)
            sys.stdout.write(
                f"===== [llm.create #{create_seq}] provider={self.name} "
                f"model={kwargs['model']} "
                f"chat_id={chat_entry_id} user_id={user_id} "
                f"ts={time.strftime('%H:%M:%S')} "
                f"msgs_len={len(messages)} max_retries=0\n"
            )
            sys.stdout.flush()
            stream = self._client.chat.completions.create(**kwargs)
        except Exception as e:
            err = self._translate_exception(e)
            logger.error(f"[DeepSeek 流式 建立连接失败] {err}")
            yield "error", str(err)
            yield "done", ""
            return

        try:
            for chunk in stream:
                try:
                    choice = chunk.choices[0] if chunk.choices else None
                    if not choice:
                        continue
                    delta = getattr(choice, "delta", None)
                    if delta is None:
                        continue
                    # ===== 关键：过滤思考内容，仅取实际回复 =====
                    # DeepSeek V4 会在 delta 中同时返回 reasoning_content（思考链）
                    # 和 content（最终回复）。这里只取 content，丢弃 reasoning_content。
                    piece = getattr(delta, "content", None)
                    if piece:
                        full_text_parts.append(piece)
                        yield "delta", piece
                    # 如果 chunk 仅包含 reasoning_content 而无 content，直接跳过
                except Exception as inner:
                    logger.warning(f"[DeepSeek 流式 chunk 解析异常] {inner}")
                    continue
        except Exception as e:
            yield "error", f"DeepSeek 读取流中断：{e}"
        finally:
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass

        yield "done", "".join(full_text_parts)

    # ---------- 非流式对话 ----------
    def chat(self, chat_entry, user_message):
        chat_entry_id = chat_entry.id
        user_id = chat_entry.user.id
        logger.info(f"[DeepSeek 非流式对话开始] chat_id={chat_entry_id} user_id={user_id}")

        messages = self._build_messages(
            chat_entry, append_user=True, user_message=user_message, user_query=user_message
        )

        try:
            kwargs = self._completion_kwargs(chat_entry, messages, stream=False)
            sys.stdout.write(
                f"===== [llm.chat] provider={self.name} "
                f"model={kwargs['model']} "
                f"chat_id={chat_entry_id} user_id={user_id} "
                f"ts={time.strftime('%H:%M:%S')} "
                f"msgs_len={len(messages)}\n"
            )
            sys.stdout.flush()
            completion = self._client.chat.completions.create(**kwargs)
            # 非流式响应：message 中可能同时包含 content 和 reasoning_content，
            # 只返回 content 部分，抛弃思考内容。
            return completion.choices[0].message.content
        except Exception as e:
            err = self._translate_exception(e)
            logger.error(f"[DeepSeek 非流式 调用失败] {err}")
            raise err


# ==============================================
# 厂商注册表 + 工厂
# ----------------------------------------------
# 新增厂商只需在此登记：_PROVIDER_REGISTRY["openai"] = OpenAIProvider
# ==============================================
_PROVIDER_REGISTRY: Dict[str, Type[BaseLLMProvider]] = {
    QwenProvider.name: QwenProvider,
    DeepSeekProvider.name: DeepSeekProvider,
    # 未来可继续注册其他厂商：
    # "openai": OpenAIProvider,
    # "anthropic": AnthropicProvider,
    # "gemini": GeminiProvider,
}

DEFAULT_PROVIDER_NAME = "qwen"


def get_llm_provider(user, provider_name: Optional[str] = None) -> BaseLLMProvider:
    """
    根据用户 / 显式名称选出合适的 LLM Provider 实例。

    解析优先级：
        1. 显式 provider_name 参数
        2. UserProfile.llm_provider 字段（若未来添加）
        3. DEFAULT_PROVIDER_NAME（当前为 "qwen"）

    所有配置 / 鉴权错误都转成 LLMError 子类抛出；调用方可以用
    except LLMError 统一处理。
    """
    name = provider_name
    if not name:
        # 优先读取 UserProfile.llm_provider 字段，用户可自主选择厂商
        try:
            profile = UserProfile.objects.get(user=user)
            name = profile.llm_provider or DEFAULT_PROVIDER_NAME
        except UserProfile.DoesNotExist:
            name = DEFAULT_PROVIDER_NAME

    cls = _PROVIDER_REGISTRY.get(name)
    if cls is None:
        raise LLMConfigError(
            f"不支持的 LLM 厂商：{name}。可用厂商：{list(_PROVIDER_REGISTRY)}"
        )
    return cls.for_user(user)


# ==============================================
# 向后兼容：旧函数式 API
# ----------------------------------------------
# 现在全部委托给新类。保留这些名字是为了让历史调用点零修改地工作，
# 新代码请直接用 get_llm_provider(user) 拿到 Provider 对象。
# ==============================================
def get_qwen_client(user):
    """兼容旧接口：返回底层 OpenAI 客户端。新代码请用 get_llm_provider。"""
    provider = QwenProvider.for_user(user)
    return provider._client  # noqa: SLF001


def iter_qwen_stream_text(chat_entry: "ChatEntry", user_message: str):
    """
    兼容旧接口：生成器 yield event_type, payload。
    新代码请用 `provider = get_llm_provider(user); provider.stream_chat(...)`。
    """
    try:
        provider = get_llm_provider(chat_entry.user)
    except LLMError as e:
        yield "error", str(e)
        yield "done", ""
        return
    yield from provider.stream_chat(chat_entry, user_message)


def chat_completion(chat_entry: "ChatEntry", user_message: str) -> str:
    """兼容旧接口：非流式对话。新代码请用 provider.chat(...)。"""
    try:
        provider = get_llm_provider(chat_entry.user)
        return provider.chat(chat_entry, user_message)
    except LLMError:
        raise
    except Exception as e:
        raise LLMError(f"客户端初始化失败：{e}")


def stream_chat_completion(chat_entry: "ChatEntry", user_message: str):
    """
    兼容旧接口：返回原始 SDK stream 对象（不是生成器）。
    **该接口已废弃**，保留仅为向后兼容——视图层现在走
    iter_qwen_stream_text / provider.stream_chat 的事件流。
    """
    provider = get_llm_provider(chat_entry.user)
    if not isinstance(provider, QwenProvider):
        raise LLMError(
            "stream_chat_completion 只能用于 QwenProvider；"
            "其它厂商请改用 provider.stream_chat() 的事件生成器接口。"
        )
    messages = provider._build_messages(  # noqa: SLF001
        chat_entry, append_user=True, user_message=user_message, user_query=user_message
    )
    try:
        return provider._client.chat.completions.create(  # noqa: SLF001
            **provider._completion_kwargs(chat_entry, messages, stream=True)  # noqa: SLF001
        )
    except Exception as e:
        raise QwenProvider._translate_exception(e)  # noqa: SLF001


# 便于外部 import：暴露统一异常与常用符号
__all__ = [
    # 类
    "BaseLLMProvider",
    "QwenProvider",
    "DeepSeekProvider",
    "RAGService",
    # 工厂
    "get_llm_provider",
    "DEFAULT_PROVIDER_NAME",
    # 异常
    "LLMError",
    "LLMAuthError",
    "LLMRateLimitError",
    "LLMConnectionError",
    "LLMModelNotFoundError",
    "LLMConfigError",
    # 向后兼容函数
    "get_qwen_client",
    "iter_qwen_stream_text",
    "chat_completion",
    "stream_chat_completion",
]
