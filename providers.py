"""Model providers behind one small interface.

The agent keeps its history in Anthropic-shaped content blocks (plain dicts):
  assistant: [{"type": "text", "text": ...}, {"type": "tool_use", "id", "name", "input"}, ...]
  user:      "question" | [{"type": "tool_result", "tool_use_id", "content", "is_error"?}, ...]
Each provider converts that to its own wire format and returns a `Reply` in the same shape,
so roles, allow-lists, learning and the page work identically whichever model answers.

- AnthropicProvider: Claude via the official SDK (streaming, prompt caching, adaptive thinking).
- OllamaProvider: a model running locally in Ollama (http://127.0.0.1:11434). Nothing leaves
  the machine; cost is zero; speed depends entirely on the hardware.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

import anthropic

OnText = Callable[[str], None]


@dataclass
class Reply:
    content: list[dict]
    stop_reason: str
    usage: dict = field(default_factory=lambda: {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0})
    model: str = ""
    stop_details: str | None = None


# ---------------------------------------------------------------- Anthropic ----
class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, model: str, effort: str = "low") -> None:
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set in .env")
        self.client = anthropic.Anthropic(api_key=api_key, max_retries=3)
        self.model, self.effort = model, effort

    @property
    def label(self) -> str:
        return self.model.replace("claude-", "") + " · " + self.effort

    def stream(self, system_blocks: list[dict], tools: list[dict], messages: list[dict], on_text: OnText) -> Reply:
        kw: dict = {"model": self.model, "max_tokens": 8000, "system": system_blocks, "tools": tools, "messages": messages}
        m = self.model
        if not (m.startswith("claude-haiku") or m.startswith("claude-sonnet-4-5") or m.startswith("claude-opus-4-5")):
            kw["thinking"] = {"type": "adaptive"}
            kw["output_config"] = {"effort": self.effort}
        with self.client.messages.stream(**kw) as stream:
            for ev in stream:
                if ev.type == "content_block_delta" and ev.delta.type == "text_delta":
                    on_text(ev.delta.text)
            resp = stream.get_final_message()
        u = resp.usage
        det = getattr(resp, "stop_details", None)
        return Reply(
            content=[b.model_dump(exclude_none=True) for b in resp.content],  # thinking blocks keep their signature for replay
            stop_reason=resp.stop_reason or "end_turn",
            usage={"input": u.input_tokens or 0, "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0,
                   "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0, "output": u.output_tokens or 0},
            model=resp.model,
            stop_details=getattr(det, "category", None) if det else None,
        )


# ------------------------------------------------------------------- Ollama ----
class OllamaProvider:
    name = "ollama"

    def __init__(self, url: str = "http://127.0.0.1:11434", model: str = "qwen3:8b", num_ctx: int = 16384, timeout: float = 900.0) -> None:
        self.url = url.rstrip("/")
        self.model, self.num_ctx, self.timeout = model, num_ctx, timeout

    @property
    def label(self) -> str:
        return "local · " + self.model

    # -- conversion --------------------------------------------------------
    @staticmethod
    def _to_ollama(system_blocks: list[dict], messages: list[dict]) -> list[dict]:
        out = [{"role": "system", "content": "\n\n".join(b.get("text", "") for b in system_blocks if b.get("type") == "text")}]
        names: dict[str, str] = {}  # tool_use id -> tool name (Ollama tool results carry the name, not an id)
        for m in messages:
            role, content = m["role"], m["content"]
            if role == "user":
                if isinstance(content, str):
                    out.append({"role": "user", "content": content})
                else:
                    for b in content:
                        if b.get("type") == "tool_result":
                            c = b.get("content")
                            text = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
                            out.append({"role": "tool", "content": text, "tool_name": names.get(b.get("tool_use_id", ""), "tool")})
            elif role == "assistant":
                if isinstance(content, str):
                    out.append({"role": "assistant", "content": content})
                    continue
                text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
                calls = []
                for b in content:
                    if b.get("type") == "tool_use":
                        names[b.get("id", "")] = b.get("name", "tool")
                        calls.append({"function": {"name": b.get("name"), "arguments": b.get("input") or {}}})
                msg: dict = {"role": "assistant", "content": text}
                if calls:
                    msg["tool_calls"] = calls
                out.append(msg)
        return out

    @staticmethod
    def _tools(tools: list[dict]) -> list[dict]:
        out = []
        for t in tools:
            schema = {k: v for k, v in t["input_schema"].items() if k != "additionalProperties"}
            out.append({"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": schema}})
        return out

    # -- call ----------------------------------------------------------------
    def stream(self, system_blocks: list[dict], tools: list[dict], messages: list[dict], on_text: OnText) -> Reply:
        body = {
            "model": self.model,
            "messages": self._to_ollama(system_blocks, messages),
            "tools": self._tools(tools),
            "stream": True,
            "think": False,  # thinking models (Qwen3...) would spend minutes on CPU before answering
            "keep_alive": "60m",
            "options": {"num_ctx": self.num_ctx, "temperature": 0.1},
        }
        req = urllib.request.Request(self.url + "/api/chat", data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        text_parts: list[str] = []
        calls: list[dict] = []
        usage = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                for raw in r:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    chunk = json.loads(line)
                    if chunk.get("error"):
                        raise RuntimeError(chunk["error"])
                    msg = chunk.get("message") or {}
                    piece = msg.get("content") or ""
                    if piece:
                        text_parts.append(piece)
                        on_text(piece)
                    for tc in msg.get("tool_calls") or []:
                        fn = tc.get("function") or {}
                        args = fn.get("arguments")
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except ValueError:
                                args = {"_raw": args}
                        calls.append({"name": fn.get("name", ""), "input": args or {}})
                    if chunk.get("done"):
                        usage["input"] = int(chunk.get("prompt_eval_count") or 0)
                        usage["output"] = int(chunk.get("eval_count") or 0)
        except urllib.error.URLError as e:
            raise RuntimeError(f"Ollama is not reachable at {self.url} ({e.reason}). Start Ollama and pull the model.") from e
        except urllib.error.HTTPError as e:  # pragma: no cover - URLError subclass, kept for clarity
            raise RuntimeError(f"Ollama error {e.code}: {e.read().decode('utf-8', 'replace')[:200]}") from e

        content: list[dict] = []
        text = "".join(text_parts).strip()
        if text:
            content.append({"type": "text", "text": text})
        for i, c in enumerate(calls):
            content.append({"type": "tool_use", "id": f"call_{int(time.time() * 1000)}_{i}", "name": c["name"], "input": c["input"]})
        return Reply(content=content, stop_reason="tool_use" if calls else "end_turn", usage=usage, model=self.model)

    def warm(self, system_blocks: list[dict], tools: list[dict]) -> None:
        """Send the exact prefix (system + tools) once so Ollama caches its processed form."""
        body = {
            "model": self.model,
            "messages": self._to_ollama(system_blocks, [{"role": "user", "content": "Reply with the single word OK."}]),
            "tools": self._tools(tools),
            "stream": False, "think": False, "keep_alive": "60m",
            "options": {"num_ctx": self.num_ctx, "temperature": 0.1, "num_predict": 3},
        }
        req = urllib.request.Request(self.url + "/api/chat", data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            r.read()

    def available_models(self) -> list[str]:
        try:
            with urllib.request.urlopen(self.url + "/api/tags", timeout=5) as r:
                return sorted(m["name"] for m in json.loads(r.read().decode("utf-8")).get("models", []))
        except (urllib.error.URLError, ValueError, KeyError):
            return []

    def alive(self) -> bool:
        try:
            with urllib.request.urlopen(self.url + "/api/version", timeout=3) as r:
                return r.status == 200
        except urllib.error.URLError:
            return False
