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
import re
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

    def __init__(self, url: str = "http://127.0.0.1:11434", model: str = "qwen3:8b", num_ctx: int = 12288, timeout: float = 900.0) -> None:
        self.url = url.rstrip("/")
        self.model, self.num_ctx, self.timeout = model, num_ctx, timeout

    _think_cache: dict[str, bool] = {}

    def _supports_thinking(self) -> bool:
        """Ollama rejects the 'think' flag for models without the thinking capability; ask once per model."""
        hit = self._think_cache.get(self.model)
        if hit is not None:
            return hit
        ok = False
        try:
            req = urllib.request.Request(self.url + "/api/show", data=json.dumps({"model": self.model}).encode("utf-8"),
                                         headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=10) as r:
                ok = "thinking" in (json.loads(r.read()).get("capabilities") or [])
        except (OSError, ValueError):
            ok = "qwen3" in self.model or "deepseek-r1" in self.model
        self._think_cache[self.model] = ok
        return ok

    def _base_body(self, num_predict: int | None = None) -> dict:
        body: dict = {"model": self.model, "stream": True, "keep_alive": "60m",
                      "options": {"num_ctx": self.num_ctx, "temperature": 0.1}}
        if num_predict:
            body["options"]["num_predict"] = num_predict
        if self._supports_thinking():
            body["think"] = False  # thinking models (Qwen3...) would spend minutes on CPU before answering
        return body

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

    _SQL_FENCE = re.compile(r"```(?:sql|oracle|plsql)?\s*((?:select|with)\b.*?)```", re.I | re.S)

    _CALL_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```|<tool_call>\s*(\{.*?\})\s*</tool_call>|(\{\s*\"name\"\s*:.*\})", re.S)

    @classmethod
    def _recover_tool_calls(cls, text: str, tool_names: set[str]) -> tuple[str, list[dict]]:
        """Small models sometimes print the call as JSON text; turn that back into tool calls."""
        calls: list[dict] = []
        rest = text
        for m in cls._CALL_RE.finditer(text):
            raw = next(g for g in m.groups() if g)
            obj = None
            for candidate in (raw, raw + "}", raw + "}}", raw + '"}}'):  # small models often drop the closing braces
                try:
                    obj = json.loads(candidate)
                    break
                except ValueError:
                    continue
            if obj is None:
                continue
            if isinstance(obj, dict) and obj.get("name") in tool_names:
                args = obj.get("arguments") or obj.get("parameters") or obj.get("input") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {"_raw": args}
                calls.append({"name": obj["name"], "input": args if isinstance(args, dict) else {}})
                rest = rest.replace(m.group(0), "", 1)
        if not calls and "run_sql" in tool_names:  # SQL written as prose instead of a tool call: run it
            for m in cls._SQL_FENCE.finditer(text):
                sql = m.group(1).strip().rstrip(";").strip()
                if sql:
                    calls.append({"name": "run_sql", "input": {"sql": sql, "purpose": "query written in the reply"}})
                    rest = rest.replace(m.group(0), "", 1)
                    break  # one statement per round keeps the loop predictable
        return rest.strip(), calls

    # -- call ----------------------------------------------------------------
    def stream(self, system_blocks: list[dict], tools: list[dict], messages: list[dict], on_text: OnText) -> Reply:
        body = self._base_body(num_predict=1200)  # an answer never needs more; runaway generation is what eats minutes
        body["messages"] = self._to_ollama(system_blocks, messages)
        body["tools"] = self._tools(tools)
        req = urllib.request.Request(self.url + "/api/chat", data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        text_parts: list[str] = []
        calls: list[dict] = []
        held: bool | None = None  # True while the reply looks like a JSON tool call written as text
        tool_names = {t["name"] for t in tools}
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
                        if held is None:  # decide once we can see how the reply starts
                            head = "".join(text_parts).lstrip()
                            if len(head) >= 3 or chunk.get("done"):
                                held = head[:1] in ("`", "{", "<")
                                if not held:
                                    on_text("".join(text_parts))
                        elif not held:
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
        if text and not calls:
            text, calls = self._recover_tool_calls(text, tool_names)
        if held and text and not calls:  # it looked like a call but was a normal answer: show it now
            on_text(text)
        if text:
            content.append({"type": "text", "text": text})
        for i, c in enumerate(calls):
            content.append({"type": "tool_use", "id": f"call_{int(time.time() * 1000)}_{i}", "name": c["name"], "input": c["input"]})
        return Reply(content=content, stop_reason="tool_use" if calls else "end_turn", usage=usage, model=self.model)

    def warm(self, system_blocks: list[dict], tools: list[dict]) -> None:
        """Send the exact prefix (system + tools) once so Ollama caches its processed form."""
        body = self._base_body(num_predict=3)
        body["stream"] = False
        body["messages"] = self._to_ollama(system_blocks, [{"role": "user", "content": "Reply with the single word OK."}])
        body["tools"] = self._tools(tools)
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
