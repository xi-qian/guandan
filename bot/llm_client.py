"""OpenAI 兼容的聊天补全客户端（无第三方依赖，纯 urllib）。

只要服务实现了 `POST {api_base}/chat/completions`，就能接入：
  OpenAI / DeepSeek / Moonshot / 智谱 / Ollama / vLLM / 任意兼容网关。
"""
from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from typing import Any

DEFAULT_API_BASE = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"

# 思考型模型（带 reasoning_content）会把推理算进 max_tokens 配额。
# 配额太小会出现「推理完没剩 token 写答案」→ content 为空、finish_reason=length。
# 输入窗口可达 1M，实际输出上限 128k。思考型模型的推理吃这个配额，
# 正文只是一个 JSON 对象，开销可忽略——所以直接给满，别让推理被截断。
DEFAULT_MAX_TOKENS = 128_000


class LLMError(Exception):
    pass


def _env_int(name: str) -> int | None:
    v = os.environ.get(name)
    if not v:
        return None
    try:
        return int(v)
    except ValueError:
        return None


class LLMClient:
    def __init__(
        self,
        api_base: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        temperature: float = 0.2,
        timeout: float = 60.0,
        max_tokens: int | None = None,
        thinking: str | None = None,
    ) -> None:
        self.api_base = (
            api_base
            or os.environ.get("OPENAI_BASE_URL")
            or os.environ.get("OPENAI_API_BASE")
            or DEFAULT_API_BASE
        ).rstrip("/")
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY") or ""
        self.model = model or os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL
        self.temperature = temperature
        self.timeout = timeout
        self.max_tokens = (
            max_tokens
            or _env_int("OPENAI_MAX_TOKENS")
            or DEFAULT_MAX_TOKENS
        )
        # 思考模式：None/"on"=全开；"off"=关闭；数字=推理 token 预算
        self.thinking = thinking if thinking is not None else os.environ.get("OPENAI_THINKING")
        self.last_meta: dict[str, Any] = {}
        self.last_reasoning: str = ""

        if not self.api_key:
            raise LLMError(
                "缺少 API key：用 --api-key 传入，或设置环境变量 OPENAI_API_KEY"
            )

    def complete(self, system: str, user: str) -> str:
        """一次对话补全，返回助手正文。

        对思考型模型会自动处理 reasoning_content 吃掉配额导致的空回复：
        空回复或被截断时放大 max_tokens 重试一次。
        """
        last_err: Exception | None = None
        budget = self.max_tokens
        for attempt in range(2):
            body = self._post(system, user, max_tokens=budget)
            content, meta = self._extract(body)
            self.last_meta = meta
            self.last_reasoning = meta.get("reasoning") or ""

            if content.strip():
                return content

            # 空正文：多半是 reasoning 吃光了配额
            if meta.get("finish_reason") == "length" and attempt == 0:
                budget = budget * 4
                continue
            if not content.strip() and attempt == 0:
                budget = budget * 4
                continue
            return content
        return ""

    def _post(self, system: str, user: str, max_tokens: int) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        payload.update(self._thinking_payload())
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{self.api_base}/chat/completions",
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            try:
                msg = json.loads(detail).get("error", {}).get("message", detail)
            except Exception:
                msg = detail
            raise LLMError(f"LLM 请求失败 {e.code}: {msg}") from None
        except urllib.error.URLError as e:
            raise LLMError(f"LLM 连接失败: {e.reason}") from None
        # socket.timeout / TimeoutError / 其它 OSError 会从 ssl.read 直接抛出，
        # 不会被包成 URLError。漏掉就会把 bot 进程打死。
        except (socket.timeout, TimeoutError) as e:
            raise LLMError(f"LLM 读超时（{self.timeout}s）：{e}") from None
        except OSError as e:
            raise LLMError(f"LLM 网络异常：{e}") from None

    def _thinking_payload(self) -> dict[str, Any]:
        """按 self.thinking 生成思考控制字段。

        实测 mimo 系接口：
          thinking={"type":"disabled"}                    → 关闭推理，约 12 倍提速
          chat_template_kwargs.thinking_budget=<n>        → 限制推理预算，效果有限
          reasoning_effort                                 → 被忽略
        """
        t = (self.thinking or "").strip().lower()
        if not t or t == "on":
            return {}
        if t == "off":
            return {"thinking": {"type": "disabled"}}
        if t.isdigit():
            return {"chat_template_kwargs": {"thinking_budget": int(t)}}
        return {}

    @staticmethod
    def _extract(body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """兼容 OpenAI 与思考型模型的响应结构。"""
        try:
            choice = body["choices"][0]
            msg = choice.get("message") or {}
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"LLM 响应结构异常: {body}") from e

        content = msg.get("content") or ""
        if isinstance(content, list):  # 多模态格式
            content = "".join(
                seg.get("text", "") for seg in content if isinstance(seg, dict)
            )
        meta = {
            "finish_reason": choice.get("finish_reason"),
            "usage": body.get("usage"),
            "reasoning": msg.get("reasoning_content") or msg.get("reasoning") or "",
            "model": body.get("model"),
        }
        return content, meta
