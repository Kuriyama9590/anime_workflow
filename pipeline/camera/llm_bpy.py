# -*- coding: utf-8 -*-
"""模式A 可选 LLM API 钩子：OpenAI 兼容 /v1/chat/completions 生成 bpy 脚本。

策略（设计文档 §2.1 camera.llm 与 §10 风险 6）：
  - **未配置/任何失败一律回退预设模板**（pipeline.camera.codegen），模式A 永不因
    LLM 缺席而阻塞；
  - 配置来源（优先级高→低）：
      1. 环境变量 PIPELINE_LLM_CONFIG 指向的 JSON 文件；
      2. pipeline/camera/llm_config.json（如存在；**密钥不落配置文件**，只写
         api_key_env 指定环境变量名）；
      3. 纯环境变量 LLM_BASE_URL / LLM_MODEL / LLM_API_KEY；
  - 安全：生成代码过静态扫描（import 白名单 + 危险调用黑名单，ast 解析）；
    `require_review`（默认 true）时**只落盘不执行**，人工过目后 --trust-llm 才跑；
  - 请求 temperature=0（确定性），响应与生成代码的 sha256 记入 meta 供 manifest 审计。
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_FILE_CONFIG = os.path.join(_HERE, "llm_config.json")

#: import 白名单：headless bpy 脚本需要的都允许，其余（网络/进程/文件系统工具）拒绝
IMPORT_WHITELIST = {"bpy", "mathutils", "math", "json", "os", "sys", "time", "re"}

#: 危险调用黑名单（子串匹配，命中即拒）
DENY_PATTERNS = (
    "subprocess", "os.system", "os.popen", "socket", "ctypes", "urllib",
    "requests", "http.client", "shutil", "rmtree", "webbrowser", "ftplib",
    "pickle", "eval(", "exec(", "__import__", "globals(", "locals(",
    "compile(", "open(",
)

DEFAULTS = {
    "enabled": False,
    "base_url": "",
    "api_key_env": "LLM_API_KEY",
    "model": "",
    "timeout_s": 90,
    "retries": 2,
    "require_review": True,
}


class LLMError(RuntimeError):
    pass


# ---------------------------------------------------------------- 配置

def load_config() -> dict | None:
    """合成 LLM 配置；未启用/缺关键项返回 None（调用方走预设模板）。"""
    cfg = dict(DEFAULTS)
    path = os.environ.get("PIPELINE_LLM_CONFIG") or (
        _FILE_CONFIG if os.path.isfile(_FILE_CONFIG) else None)
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except (OSError, ValueError) as e:
            raise LLMError("LLM 配置文件不可读（%s）：%s" % (path, e))
    # 环境变量兜底（不覆盖显式文件配置的非空值）
    cfg["base_url"] = cfg.get("base_url") or os.environ.get("LLM_BASE_URL", "")
    cfg["model"] = cfg.get("model") or os.environ.get("LLM_MODEL", "")
    if not cfg.get("api_key_env"):
        cfg["api_key_env"] = "LLM_API_KEY"
    if os.environ.get("LLM_TIMEOUT_S", "").isdigit():
        cfg["timeout_s"] = int(os.environ["LLM_TIMEOUT_S"])
    enabled = bool(cfg.get("enabled")) or bool(
        os.environ.get("PIPELINE_LLM_CONFIG"))  # 显式指向配置文件即视为启用
    key = os.environ.get(cfg["api_key_env"], "")
    if not (enabled and cfg["base_url"] and cfg["model"] and key):
        return None
    return cfg


# ---------------------------------------------------------------- 静态安全扫描

def safety_scan(code: str) -> list[str]:
    """返回违规清单（空列表=通过）。ast 解析失败本身也是违规。"""
    violations = []
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return ["ast 解析失败（语法错误）：%s" % e]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module.split(".")[0]] if node.module else []
        else:
            continue
        for n in names:
            if n not in IMPORT_WHITELIST:
                violations.append("import %s 不在白名单 %s" % (n, sorted(IMPORT_WHITELIST)))
    for pat in DENY_PATTERNS:
        if pat in code:
            violations.append("代码含禁止模式 %r" % pat)
    if "import bpy" not in code:
        violations.append("缺 import bpy（不是 bpy 脚本）")
    return violations


# ---------------------------------------------------------------- 请求

def build_system_prompt() -> str:
    return (
        "You write standalone Blender bpy scripts for a headless camera-move renderer. "
        "Contract: the script must NOT import anything beyond bpy, mathutils, math, json, os, sys, time, re; "
        "no network, no subprocess, no file writes (Blender writes renders via bpy). "
        "It receives a JSON spec embedded as the module-level variable SPEC (same schema as the user message). "
        "It must build: a subdivided displaced image plane in the XY plane (z=0, normal +Z) textured with "
        "SPEC['scene']['images']['first'] (optional 'last' crossfade), an animated camera following "
        "SPEC['camera_track']['keys'] (location/rotation_euler/lens keyframes per frame), "
        "resolution/fps/frame range from SPEC['render'], EEVEE via dual-enum try/except "
        "(BLENDER_EEVEE_NEXT then BLENDER_EEVEE). "
        "It must support argv after '--': --verify-only (build scene, assert objects/camera/keys, "
        "print 'SCENE_BUILD_OK ' + json.dumps(info) WITHOUT rendering), --render=endpoints "
        "(render frame_start and frame_end to SPEC['output']['endpoints'] paths), "
        "--render=animation (PNG sequence into SPEC['output']['dir'] using SPEC['output']['frame_prefix']). "
        "Coordinate convention: plane in XY at z=0 textured U->+X V->+Y, camera above at +Z with "
        "rotation_euler (0,0,0) looking down -Z so the image appears upright. "
        "Output ONLY one fenced python code block."
    )


def _extract_code(content: str) -> str:
    m = re.search(r"```(?:python|py)\s*\n(.*?)```", content, re.S)
    return (m.group(1) if m else content).strip() + "\n"


def _post_chat(cfg: dict, messages: list[dict]) -> dict:
    url = cfg["base_url"].rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    payload = json.dumps({
        "model": cfg["model"],
        "messages": messages,
        "temperature": 0,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer %s" % os.environ.get(cfg["api_key_env"], ""),
    })
    with urllib.request.urlopen(req, timeout=int(cfg["timeout_s"])) as r:
        return json.loads(r.read().decode("utf-8"))


def generate_bpy_code(spec: dict, cfg: dict | None = None) -> tuple[str, dict]:
    """调 LLM 生成 bpy 脚本。返回 (code, meta)；失败抛 LLMError（调用方回退预设）。

    meta 含 model、响应与代码 sha256、耗时——供 manifest 审计（设计 §3 规则 4）。
    """
    cfg = cfg or load_config()
    if not cfg:
        # 注意：cfg 为 None 时不能用 cfg.get（历史缺陷：AttributeError 顶替了
        # 本应抛出的 LLMError，调用方 except LLMError 会漏接）——取 DEFAULTS 兜底
        raise LLMError("LLM 未配置（enabled/base_url/model/%s 缺一）"
                       % DEFAULTS["api_key_env"])
    if "first" not in (spec.get("scene", {}).get("images") or {}):
        raise LLMError("spec.scene.images.first 缺失，无法构造 LLM 提示")

    messages = [
        {"role": "system", "content": build_system_prompt()},
        {"role": "user", "content": json.dumps(spec, ensure_ascii=True)},
    ]
    last_err = None
    for attempt in range(int(cfg["retries"]) + 1):
        try:
            t0 = time.time()
            resp = _post_chat(cfg, messages)
            content = resp["choices"][0]["message"]["content"]
            code = _extract_code(content)
            if not code.strip():
                raise LLMError("LLM 返回空代码")
            meta = {
                "model": cfg["model"],
                "attempt": attempt + 1,
                "elapsed_s": round(time.time() - t0, 2),
                "response_sha256": hashlib.sha256(
                    content.encode("utf-8")).hexdigest(),
                "code_sha256": hashlib.sha256(code.encode("utf-8")).hexdigest(),
                "require_review": bool(cfg.get("require_review", True)),
            }
            return code, meta
        except (urllib.error.URLError, KeyError, IndexError, ValueError,
                OSError, LLMError) as e:
            last_err = e
    raise LLMError("LLM 生成失败（%d 次尝试）：%s" % (int(cfg["retries"]) + 1, last_err))
