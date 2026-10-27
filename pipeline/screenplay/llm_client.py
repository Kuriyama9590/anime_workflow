# -*- coding: utf-8 -*-
"""公共 OpenAI 兼容 LLM 客户端（段0 编剧起用；运镜模式A 之后可复用）。

配置源：全局 pipeline/config.yaml 顶层 ``llm:`` 段，核心三字段：
  base_url   OpenAI 兼容服务根地址（填到 /v1 为止；本模块自动补 /chat/completions）
  api_key    密钥。本地服务可任意非空串；真实密钥建议改走 api_key_env 环境变量
  model      模型名
可选字段：enabled（默认 true）/ api_key_env（非空则优先于 api_key 取环境变量）/
timeout_s / retries / temperature。

两个已核实的本机示例（config.yaml ``llm:`` 段注释同款）：
  1) 本地 llama.cpp：``E:/projects/推理/llama/start-llama-bonsai.bat`` 拉起 llama-server
     （Bonsai2-27B，监听 127.0.0.1:8010，OpenAI 兼容，无鉴权）：
       base_url: "http://127.0.0.1:8010/v1"   model: "Bonsai2-27B"   api_key: "no-key"
  2) DeepSeek：``E:/projects/推理/deepseek-api`` 项目（网页版逆向代理，OpenAI 兼容，
     默认 127.0.0.1:8317，PROXY_API_KEY 鉴权；模型 deepseek-chat / deepseek-reasoner）：
       base_url: "http://127.0.0.1:8317/v1"   model: "deepseek-chat"
       api_key_env: PROXY_API_KEY

与既有实现的关系（迁移路径，不强行重构）：
  - pipeline/camera/llm_bpy.py 自带客户端（配置源 = PIPELINE_LLM_CONFIG JSON /
    pipeline/camera/llm_config.json + 环境变量）与 bpy 安全扫描，camera 模式A 继续
    用它，本模块不改它；
  - pipeline/llm.py 是设计 §2.1 的通用客户端（仅支持 api_key_env 取密钥）；
  - 本模块是新的公共入口（支持 ask 要求的 api_key 直填 + api_key_env 优先）。
    运镜模式A 之后如需统一：给 llm_bpy.load_config 增加本模块的 config llm 段
    作为候选配置源，再把 chat 调用换成本模块（retries/timeout_s 语义同名兼容）。

行为约束：仅标准库（urllib）；失败重试 retries 次（线性退避 2s/4s）；
自动剥除 <think>…</think> 内联推理残留（llama.cpp --reasoning-format deepseek /
deepseek-reasoner 常见）；未配置由调用方降级（is_configured 判定，不在此抛错）。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request

__all__ = ["LLMError", "DEFAULTS", "section", "api_key", "is_configured",
           "not_configured_hint", "strip_think", "chat", "make_chat_fn"]

DEFAULTS = {
    "enabled": True,
    "base_url": "",
    "api_key": "",
    "api_key_env": "",
    "model": "",
    "timeout_s": 120,
    "retries": 2,
    "temperature": 0.7,
}

_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.S | re.I)


class LLMError(RuntimeError):
    """请求失败/响应不合法（消息面向用户可读，含修复提示）。"""


def section(cfg: dict) -> dict:
    """合并 DEFAULTS 与 cfg 的 llm 段（容忍 cfg=None/缺节/非映射）。"""
    sec = dict(DEFAULTS)
    raw = (cfg or {}).get("llm")
    if isinstance(raw, dict):
        sec.update(raw)
    return sec


def api_key(sec: dict) -> str:
    """api_key_env 命中的环境变量优先；否则用 api_key 字段直填值。"""
    env_name = str(sec.get("api_key_env") or "").strip()
    if env_name:
        val = os.environ.get(env_name, "")
        if val:
            return val.strip()
    return str(sec.get("api_key") or "").strip()


def is_configured(cfg: dict) -> bool:
    """enabled ∧ base_url ∧ model ∧ 密钥可得（环境变量或直填）。"""
    sec = section(cfg)
    return (bool(sec.get("enabled"))
            and bool(str(sec.get("base_url") or "").strip())
            and bool(str(sec.get("model") or "").strip())
            and bool(api_key(sec)))


def not_configured_hint(cfg: dict) -> str:
    """未配置时的友好提示（列出缺口 + 两个已核实示例的拉起方式）。"""
    sec = section(cfg)
    missing = []
    if not sec.get("enabled"):
        missing.append("llm.enabled=true")
    if not str(sec.get("base_url") or "").strip():
        missing.append("llm.base_url")
    if not str(sec.get("model") or "").strip():
        missing.append("llm.model")
    if not api_key(sec):
        missing.append("llm.api_key（或 api_key_env 指向的环境变量）")
    return ("LLM 未配置（缺：%s）。pipeline/config.yaml 的 llm: 段给两个注释示例："
            "① 本地 llama.cpp——运行 E:/projects/推理/llama/start-llama-bonsai.bat 拉起 "
            "llama-server（Bonsai2-27B，127.0.0.1:8010）；② DeepSeek——启动 "
            "E:/projects/推理/deepseek-api 项目（OpenAI 兼容代理，127.0.0.1:8317）。"
            % "、".join(missing))


def strip_think(text: str) -> str:
    """剥除内联 <think>…</think> 推理残留（reasoning 模型常见）。"""
    return _THINK_RE.sub("", text or "").strip()


def _content(body: dict) -> str:
    try:
        msg = body["choices"][0]["message"]
        content = msg.get("content")
    except (KeyError, IndexError, TypeError):
        raise LLMError("响应结构异常（缺 choices[0].message.content）：%s"
                       % json.dumps(body, ensure_ascii=False)[:300])
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # 多模态分段 content
        parts = [str(p.get("text") or "") for p in content if isinstance(p, dict)]
        joined = "\n".join(parts).strip()
        if joined:
            return joined
    raise LLMError("message.content 为空（reasoning 模型请确认正文落在 content 而非 "
                   "reasoning_content）")


def chat(cfg: dict, messages: list, temperature: float | None = None) -> str:
    """POST /v1/chat/completions，返回首个 choice 的正文（已剥 think 残留）。"""
    sec = section(cfg)
    if not is_configured(cfg):
        raise LLMError(not_configured_hint(cfg))
    url = str(sec["base_url"]).strip().rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    key = api_key(sec)
    payload = {
        "model": str(sec["model"]),
        "messages": list(messages),
        "temperature": sec["temperature"] if temperature is None else float(temperature),
    }
    last: Exception | None = None
    for attempt in range(int(sec["retries"]) + 1):
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     **({"Authorization": "Bearer %s" % key} if key else {})})
        try:
            with urllib.request.urlopen(req, timeout=float(sec["timeout_s"])) as r:
                body = json.loads(r.read().decode("utf-8"))
            text = strip_think(_content(body))
            if not text:
                raise LLMError("LLM 返回空内容")
            return text
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", "replace")[:400]
            except Exception:  # noqa: BLE001 —— 读错误体失败不掩盖原错误
                detail = ""
            last = LLMError("HTTP %s: %s" % (e.code, detail))
        except LLMError as e:
            last = e
        except (urllib.error.URLError, TimeoutError, OSError, ValueError,
                KeyError, IndexError) as e:
            last = LLMError("请求失败: %s" % e)
        if attempt < int(sec["retries"]):
            time.sleep(2 * (attempt + 1))
    raise LLMError("LLM 调用失败（已重试 %s 次）：%s。检查 base_url 与服务是否拉起："
                   "llama.cpp=start-llama-bonsai.bat（8010）；DeepSeek=deepseek-api 项目（8317）"
                   % (sec["retries"], last))


def make_chat_fn(cfg: dict):
    """已配置 → 返回 chat 包装（messages, temperature=None) -> str；未配置 → None。"""
    if not is_configured(cfg):
        return None
    return lambda messages, temperature=None: chat(cfg, messages, temperature)
