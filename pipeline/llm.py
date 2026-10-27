# -*- coding: utf-8 -*-
"""OpenAI 兼容 LLM 客户端（通用传输层；docs §2.1/§4.3/§10.6）。

现状说明（2026-10-26，避免接口与契约漂移）：
  * camera 模式A 的 bpy 生成走 pipeline/camera/llm_bpy.py（自带安全扫描与配置
    合成，配置源 = PIPELINE_LLM_CONFIG 指向的 JSON / pipeline/camera/llm_config.json
    + 环境变量），不 import 本模块；
  * config.yaml camera.llm.* 由 pipeline/stages/camera.py 的 _LLMBridge 桥接为
    PIPELINE_LLM_CONFIG 临时 JSON 后交给 run_camera（config 节不会被静默忽略）；
  * 本模块保留为设计 §2.1 规定的通用客户端（供后续模块/脚本直连 OpenAI 兼容
    接口用），语义与 llm_bpy 的配置键一致（enabled/base_url/api_key_env/model/
    timeout_s/retries）。

约束：
  * 密钥只从环境变量取（api_key_env），配置文件不落密钥；
  * 未配置/不可用 → 由调用方降级到预设模板（is_configured() 判定），模式A 永不因
    LLM 缺席而阻塞；
  * 仅标准库（urllib）。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

__all__ = ["LLMError", "is_configured", "chat"]


class LLMError(RuntimeError):
    pass


def _cfg(llm_cfg: dict) -> dict:
    base = {
        "enabled": False, "base_url": "", "api_key_env": "LLM_API_KEY",
        "model": "", "timeout_s": 90, "retries": 2,
    }
    base.update(llm_cfg or {})
    return base


def is_configured(llm_cfg: dict) -> bool:
    """enabled ∧ base_url ∧ model ∧ 环境变量里有密钥。"""
    c = _cfg(llm_cfg)
    return bool(c["enabled"] and c["base_url"] and c["model"] and os.environ.get(c["api_key_env"]))


def chat(llm_cfg: dict, messages: list, temperature: float = 0.0) -> str:
    """调 /v1/chat/completions，返回首个 choice 文本。失败重试 retries 次。"""
    c = _cfg(llm_cfg)
    if not is_configured(llm_cfg):
        raise LLMError("LLM 未配置或缺少密钥（enabled/base_url/model/%s）——将回退预设模板"
                       % c["api_key_env"])
    url = str(c["base_url"]).rstrip("/") + "/chat/completions"
    key = os.environ[c["api_key_env"]]
    payload = {"model": c["model"], "messages": messages, "temperature": temperature}
    last_err: Exception | None = None
    for attempt in range(int(c["retries"]) + 1):
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer %s" % key})
        try:
            with urllib.request.urlopen(req, timeout=float(c["timeout_s"])) as r:
                body = json.loads(r.read().decode("utf-8"))
            text = body["choices"][0]["message"]["content"]
            if not isinstance(text, str) or not text.strip():
                raise LLMError("LLM 返回空内容")
            return text
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            last_err = LLMError("HTTP %s: %s" % (e.code, detail))
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError, IndexError) as e:
            last_err = LLMError("请求失败: %s" % e)
        if attempt < int(c["retries"]):
            time.sleep(2 * (attempt + 1))
    raise LLMError("LLM 调用最终失败（重试 %s 次）：%s" % (c["retries"], last_err))
