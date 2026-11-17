#!/usr/bin/env python3
"""llm_backends.py - one client for every LLM backend used by build_synthetic.py (stdlib only).

  anthropic   Anthropic Messages API
  openai      any OpenAI-compatible server (/v1/chat/completions)
  ollama      Ollama native API (/api/chat)
  llamacpp    llama.cpp `llama-server` (OpenAI-compatible /v1/chat/completions + llama.cpp extensions)

llama.cpp specifics used here
  * cache_prompt=true     the server re-uses the KV cache of the longest common prompt prefix (system prompt + the static
                          part of the request prompt), so only the variable tail is evaluated again. The response
                          `timings` (cache_n = tokens re-used, prompt_n = tokens evaluated) are aggregated by the caller.
  * top_k / min_p / repeat_penalty / seed   llama.cpp sampling extensions: min_p + repeat_penalty fight repetition,
                          a different seed per request makes identical prompts produce different texts.
  * /health, /props, /slots   readiness (model loading answers 503), number of parallel slots (-np) and the context
                          available per slot. Slots are the unit of parallelism: use as many workers as slots.
Server flags that matter on a single GPU: -ngl 99 (all layers on the GPU), -np N (slots; the -c context is split evenly
between them), --flash-attn, -ctk/-ctv q8_0 (quantised KV cache, needs flash attention), --jinja (chat template).
`python build_synthetic.py server-cmd ...` prints a launch command for your numbers.
"""
import json
import os
import time
import urllib.error
import urllib.request

ANTHROPIC_VERSION = "2023-06-01"


class LLMError(RuntimeError):
    pass


def http_json(url, headers=None, body=None, retries=0, timeout=600, method=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=headers or {}, method=method or ("POST" if data is not None else "GET"))
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8")
                return json.loads(raw) if raw.strip() else None
        except urllib.error.HTTPError as e:
            if e.code in (408, 429, 500, 502, 503, 529) and attempt < retries:
                time.sleep(min(30, 2 ** attempt))
                continue
            raise LLMError("HTTP %d: %s" % (e.code, e.read().decode("utf-8", "replace")[:300]))
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt < retries:
                time.sleep(min(30, 2 ** attempt))
                continue
            raise LLMError("network error: %s" % e)


def probe(url, timeout=5):
    """(status_code, json|None) without raising on HTTP errors; (0, None) when the server is not reachable."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            return r.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError):
        return 0, None


def normalize_ollama_base(url):
    """http://localhost:11434 | .../api | .../api/chat | .../v1  ->  http://localhost:11434"""
    url = (url or "http://localhost:11434").rstrip("/")
    for suffix in ("/api/chat", "/api/generate", "/api", "/v1/chat/completions", "/v1"):
        if url.endswith(suffix):
            return url[:-len(suffix)]
    return url


def normalize_llamacpp_base(url):
    """http://127.0.0.1:8080 | .../v1 | .../v1/chat/completions | .../completion | .../health  ->  http://127.0.0.1:8080"""
    url = (url or "http://127.0.0.1:8080").rstrip("/")
    for suffix in ("/v1/chat/completions", "/v1/completions", "/v1", "/completion", "/health", "/props", "/slots"):
        if url.endswith(suffix):
            return url[:-len(suffix)]
    return url


class LLM:
    def __init__(self, backend, model="", endpoint="", api_key_env="", system="", temperature=0.9, top_p=0.95, top_k=40,
                 min_p=0.05, repeat_penalty=1.1, max_tokens=600, max_retries=3, timeout=600, num_ctx=8192,
                 keep_alive="30m", no_think=False, cache_prompt=True):
        self.backend, self.model, self.endpoint, self.api_key_env, self.system = backend, model, endpoint, api_key_env, system
        self.temperature, self.top_p, self.top_k, self.min_p, self.repeat_penalty = temperature, top_p, top_k, min_p, repeat_penalty
        self.max_tokens, self.max_retries, self.timeout, self.num_ctx = max_tokens, max_retries, timeout, num_ctx
        self.keep_alive, self.no_think, self.cache_prompt = keep_alive, no_think, cache_prompt

    # ------------------------------------------------------------- readiness
    def ready(self, wait=0):
        """llamacpp: wait for /health, read slots / context. Other backends: {}."""
        if self.backend != "llamacpp":
            return {}
        base = normalize_llamacpp_base(self.endpoint)
        deadline, status = time.time() + wait, 0
        while True:
            status, _ = probe(base + "/health")
            if status == 200 or time.time() >= deadline:
                break
            time.sleep(2)
        if status != 200:
            raise LLMError("llama-server at %s is not ready (health status %s). Start it first, e.g. the command printed by "
                           "`build_synthetic.py server-cmd`." % (base, status))
        info = {"slots": 0, "n_ctx_slot": 0}
        _, slots = probe(base + "/slots")
        if isinstance(slots, list) and slots:
            info["slots"] = len(slots)
            info["n_ctx_slot"] = min(int(s.get("n_ctx", 0) or 0) for s in slots)
        _, props = probe(base + "/props")
        if isinstance(props, dict):
            info["slots"] = info["slots"] or int(props.get("total_slots", 0) or 0)
            if not info["n_ctx_slot"]:
                info["n_ctx_slot"] = int((props.get("default_generation_settings") or {}).get("n_ctx", 0) or 0)
        return info

    # ------------------------------------------------------------- completion
    def complete(self, prompt, seed=None, temperature=None, system=None, max_tokens=None):
        """-> (text, info). info may contain prompt_n / cache_n / predicted_n / predicted_per_second (llama.cpp)."""
        t = self.temperature if temperature is None else temperature
        sysmsg = self.system if system is None else system
        mt = max_tokens or self.max_tokens
        msgs = ([{"role": "system", "content": sysmsg}] if sysmsg else []) + [{"role": "user", "content": prompt}]
        b = self.backend
        if b == "anthropic":
            key = os.environ.get(self.api_key_env or "ANTHROPIC_API_KEY", "")
            d = http_json(self.endpoint or "https://api.anthropic.com/v1/messages",
                          {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"},
                          {"model": self.model or "claude-sonnet-5-5", "max_tokens": mt, "temperature": min(1.0, t),
                           **({"system": sysmsg} if sysmsg else {}), "messages": [{"role": "user", "content": prompt}]},
                          self.max_retries, self.timeout)
            return "".join(x.get("text", "") for x in d.get("content", []) if x.get("type") == "text"), {}
        if b == "openai":
            key = os.environ.get(self.api_key_env or "OPENAI_API_KEY", "") or "local"
            base = (self.endpoint or "https://api.openai.com/v1").rstrip("/")
            body = {"model": self.model, "max_tokens": mt, "temperature": t, "top_p": self.top_p, "messages": msgs}
            if seed is not None:
                body["seed"] = seed
            d = http_json(base + "/chat/completions", {"Authorization": "Bearer " + key, "content-type": "application/json"},
                          body, self.max_retries, self.timeout)
            return d["choices"][0]["message"]["content"], {}
        if b == "ollama":
            opts = {"temperature": t, "top_p": self.top_p, "top_k": self.top_k, "min_p": self.min_p,
                    "repeat_penalty": self.repeat_penalty, "num_predict": mt, "num_ctx": self.num_ctx}
            if seed is not None:
                opts["seed"] = seed
            body = {"model": self.model, "stream": False, "messages": msgs, "options": opts}
            if self.keep_alive:
                body["keep_alive"] = self.keep_alive
            if self.no_think:
                body["think"] = False
            d = http_json(normalize_ollama_base(self.endpoint) + "/api/chat", {"content-type": "application/json"}, body,
                          self.max_retries, self.timeout)
            return d["message"]["content"], {}
        if b == "llamacpp":
            body = {"model": self.model or "local", "messages": msgs, "temperature": t, "top_p": self.top_p,
                    "top_k": self.top_k, "min_p": self.min_p, "repeat_penalty": self.repeat_penalty, "max_tokens": mt,
                    "cache_prompt": self.cache_prompt, "stream": False}
            if seed is not None:
                body["seed"] = seed
            headers = {"content-type": "application/json"}
            key = os.environ.get(self.api_key_env or "LLAMA_API_KEY", "")
            if key:
                headers["Authorization"] = "Bearer " + key
            d = http_json(normalize_llamacpp_base(self.endpoint) + "/v1/chat/completions", headers, body, self.max_retries, self.timeout)
            tm = d.get("timings") or {}
            use = d.get("usage") or {}
            info = {"prompt_n": tm.get("prompt_n", use.get("prompt_tokens")), "cache_n": tm.get("cache_n"),
                    "predicted_n": tm.get("predicted_n", use.get("completion_tokens")),
                    "predicted_per_second": tm.get("predicted_per_second")}
            return d["choices"][0]["message"]["content"], info
        raise LLMError("unknown backend " + b)
