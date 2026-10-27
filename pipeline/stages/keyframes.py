# -*- coding: utf-8 -*-
"""keyframes 段（段2）：按 shots.yaml 逐镜渲染首帧（t2i）与末帧（保身份 edit / fresh）。

设计依据（docs/FRAMEWORK_DESIGN.md）：
- §4.2 段契约：每镜两 unit（``S#:first`` / ``S#:last``）；edit 前先把首帧复制为
  ``S#_ref.png`` 放进**两个** input_dirs，且**先 makedirs 再复制**（keyframes.log:4-12
  的翻车先例）；use_char_ref 默认关（B 级，仅 photo 通道，必须走 edit_wf 图形）。
- §3 规则 4 seed：首帧 = seed_base + 10×镜序（镜序 1 起，yaml 显式 seed 优先，0=派生）；
  末帧 edit = 首帧 seed + 5，fresh = +7（render_keyframes.py:133,140 A 级沿用）。
- §3 规则 5 提示词次序：镜头描述 + 角色锚点 + 道具锚点 + 画风锚点；负面同理；
  edit 负面追加 different person/face/hair/clothes/location/camera angle（render_keyframes.py:22）。
- §5 manifest：原子写（tmp+os.replace）、三条件完成判定（done ∧ 文件在 ∧ ≥100KB）、
  inputs_hash / upstream / config_hash 三路失效判定。
- §4.0 前置：服务器可达预检（不可达 → exit 2，不让 unit 空转）；孤儿 ``running`` unit
  收敛（history 命中→收割下载；queue 在列→等待收割绝不重提；都查无→置回 pending）。

批次/重试/续跑泛化自 _tools/render_keyframes.py（只读参考，A 级）：存在即跳过（:119-125）、
总尝试 2 次=1 次+1 重试（:88-102, run.attempts）、首帧失败整镜跳过（:122-123）、失败不阻塞
其它镜、段末汇总失败清单非零退出（:144-145）。

对外入口（stages 包约定）：
    run_stage(project_dir, config=None, args=None) -> int   # 供 pipeline.cli 调度
    main(argv=None) -> int     # python -m pipeline.stages.keyframes <项目> [--only S1,S3] [--force] [--dry-run]
退出码：0=全部成功或跳过；1=存在失败 unit 或输入错误；2=ComfyUI 不可达（§4.0.1）。

共享模块对接（延迟探测，均不阻塞 import；核心与脚手架模块负责这些文件）：
- ``pipeline.config``：存在则用其合并配置（探测 load/load_config/get_config/load_merged，
  取第一个可用的）；缺失时退回本文件 DEFAULTS（= 设计 §2.1 相关节的镜像）。
- ``pipeline.miniyaml``：存在则用于解析 shots/characters/project yaml（探测
  parse/parse_string/loads/safe_load）；缺失时 run 模式给出可读报错退出（不静默降级）。
- ComfyUI HTTP 网关为本模块内置（comfy_client.py:7-52 的参数化移植，A 级行为），
  不依赖 pipeline.comfy；集成阶段如需统一客户端可整体替换 ComfyGateway。
"""
from __future__ import annotations

import argparse
import copy
import dataclasses
import hashlib
import importlib
import json
import os
import re
import shutil
import sys
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from pipeline.workflows import build_edit, build_t2i

STAGE = "keyframes"
NEEDS_COMFY = True  # §4.0：服务器预检仅对 needs_comfy 段执行

#: 仓库根 = pipeline 包父目录（设计 §2.1 paths 注：与 cwd 无关）
REPO_ROOT = Path(__file__).resolve().parents[2]

#: edit 负面追加项 —— render_keyframes.py:22 NEGEDIT 相对 NEG 的增量，逐字沿用（A 级）。
NEGEDIT_TAIL = (
    "different person, different face, different hair, different clothes, "
    "different location, different camera angle"
)

#: 通用 edit 提示词模板 —— render_keyframes.py:26-28 E() 的按角色泛化（设计 §4.2 指定措辞；
#: girl→person、uniform→outfit 属泛化改写，首跑回归时对照验证）。
EDIT_TEMPLATE = (
    "Edit <image1>: keep exactly the same person, face, hairstyle, outfit, "
    "camera angle, framing, lighting and background; change only — {desc}"
)

#: §5 完成判定：图 ≥100KB（段配置下限）。
MIN_BYTES_DEFAULT = 100_000

#: 默认配置 = docs/FRAMEWORK_DESIGN.md §2.1 相关节的镜像（pipeline.config 落地前的回退，
#: 且作为其返回值的补全基底：核心配置缺键时由此兜底）。
DEFAULTS: Dict[str, Any] = {
    "comfy": {
        "server": "http://127.0.0.1:8188",
        "client_id": "pipeline",
        "portable_root": "E:/SD-WEBUI-AKI-V5.0/ComfyUI_windows_portable",
        "models_dir": "E:/SD-WEBUI-AKI-V5.0/ComfyUI_windows_portable/ComfyUI/models",
        "input_dirs": [
            "E:/SD-WEBUI-AKI-V5.0/ComfyUI_windows_portable/ComfyUI/user/default/input",
            "E:/SD-WEBUI-AKI-V5.0/ComfyUI_windows_portable/ComfyUI/input",
        ],
        "poll_interval_s": 3,
        "timeout_img_s": 900,
    },
    "channels": {
        "photo": {
            "unet_gguf": "diffusion_models/Sakura-Image-2.1-Turbo-Q5_K_M-4.60GiB.gguf",
            "clip": "text_encoders/Sakura-TextEncoder-Qwen3VL-8B-Uncensored-bf16.safetensors",
            "vae": "vae/qwen_image_2.1_vae_bf16.safetensors",
            "clip_type": "qwen_image",
            "resolution": 1024,
            "width": 1344,
            "height": 768,
            "sampler": "euler",
            "sigmas": "1.0, 0.978453, 0.95418, 0.926626, 0.89508, 0.845148, 0.704534, 0.414568, 0.0",
            "cfg": 1.0,
        },
        "anime": {
            "checkpoint": "checkpoints/waiIllustriousSDXL_v170.safetensors",
            "width": 1344,
            "height": 768,
            "steps": 28,
            "cfg": 5.0,
            "sampler": "euler_ancestral",
            "img2img_denoise": 0.55,
            "negative": "photo, 3d, realistic, text, watermark, bad anatomy, extra fingers, lowres",
        },
    },
    "run": {
        "attempts": 2,        # 总尝试次数（含首次）= render_keyframes.py:90 tries=2
        "retry_wait_s": 20,
        "quiet_first_s": 5,
        "server_precheck": True,
    },
    # schema 对 shots.yaml duration_s 的兜底取 video.default_duration_s（§4.0 第三优先级），
    # 域 [min_request_s, max_request_s]（§2.1 镜像；keyframes 自身不消费，仅供共享校验）
    "video": {"default_duration_s": 5.17, "min_request_s": 5.0, "max_request_s": 15.0},
    "paths": {"projects_root": "projects", "logs_dir": "logs"},
    "keyframes": {"min_bytes": MIN_BYTES_DEFAULT},
}

_SHOT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-.]*$")


# ----------------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_obj(obj: Any) -> str:
    return _sha256_text(json.dumps(obj, ensure_ascii=False, sort_keys=True))


def _deep_update(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """深合并：映射递归、标量覆盖、列表整体替换（设计 §2.2）。None 值跳过。"""
    for k, v in (override or {}).items():
        if v is None:
            continue
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)
        else:
            base[k] = v
    return base


def _resolve_path(value: str | Path, base: Path = REPO_ROOT) -> Path:
    p = Path(value)
    return p if p.is_absolute() else (base / p)


def _safe_print(line: str) -> None:
    """Windows 控制台（cp936）打印中文安全：失败时降级为转义 ASCII。"""
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "backslashreplace").decode("ascii"), flush=True)


class StageLog:
    """段运行日志：stdout + logs/<项目>_keyframes.log 追加（§5：含完整提示词，可复现）。"""

    def __init__(self, path: Optional[Path] = None):
        self.path = path
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, msg: str) -> None:
        line = "%s %s" % (time.strftime("%H:%M:%S"), msg)
        _safe_print(line)
        if self.path is not None:
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass  # 日志写失败不阻断渲染


def _noop_log(msg: str) -> None:  # 供测试注入
    _safe_print(msg)


# ----------------------------------------------------------------------------
# 共享模块探测（pipeline.config / pipeline.miniyaml —— 核心与脚手架模块负责）
# ----------------------------------------------------------------------------

def _probe_module(name: str):
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def parse_yaml(text: str):
    """经 pipeline.miniyaml 解析；未构建则抛 RuntimeError（不静默降级）。"""
    mod = _probe_module("pipeline.miniyaml")
    if mod is not None:
        for fn_name in ("parse", "parse_string", "loads", "safe_load"):
            fn = getattr(mod, fn_name, None)
            if callable(fn):
                try:
                    return fn(text)
                except TypeError:
                    continue  # 名字存在但签名不同，试下一个
    raise RuntimeError(
        "无法解析 YAML：pipeline/miniyaml.py 未构建（核心与脚手架模块负责，设计 §2.3）。"
        "keyframes 段 run 模式依赖它解析 shots.yaml/characters.yaml/project.yaml；"
        "纯逻辑与工作流构造不受影响。"
    )


def load_merged_config(project_dir: Optional[Path] = None, log: Callable[[str], None] = _noop_log) -> Dict[str, Any]:
    """优先 pipeline.config 的合并结果；缺失时用 DEFAULTS（并打告警）。

    返回值恒为完整配置：核心返回值会 _deep_update 到 DEFAULTS 之上（缺键兜底）。
    """
    mod = _probe_module("pipeline.config")
    merged: Optional[Dict[str, Any]] = None
    if mod is not None:
        for fn_name in ("load", "load_config", "get_config", "load_merged"):
            fn = getattr(mod, fn_name, None)
            if not callable(fn):
                continue
            for call in (
                lambda: fn(str(project_dir)) if project_dir else fn(),
                lambda: fn(),
                lambda: fn(project_dir=str(project_dir)) if project_dir else fn(),
            ):
                try:
                    result = call()
                except TypeError:
                    continue
                if isinstance(result, dict):
                    merged = result
                    break
            if merged is not None:
                break
        if merged is None:
            log("警告：pipeline.config 存在但未找到可用入口（load/load_config/get_config/load_merged），退回内置默认配置")
    cfg = copy.deepcopy(DEFAULTS)
    if merged is not None:
        _deep_update(cfg, merged)
    else:
        cfg["_defaults_only"] = True
        log("警告：pipeline/config 模块未构建，使用内置默认配置（= 设计 §2.1 镜像，含 8188/双 input 目录/1344x768）")
    return cfg


# ----------------------------------------------------------------------------
# ComfyUI HTTP 网关（_tools/comfy_client.py:7-52 的参数化移植，A 级回路）
# ----------------------------------------------------------------------------

class ComfyGateway:
    """queue → 轮询 history → /view 下载。只依赖标准库；异常向上抛（重试由段层负责）。"""

    def __init__(self, server: str, client_id: str = "pipeline", poll_interval_s: float = 3.0):
        self.server = str(server).rstrip("/")
        self.client_id = client_id
        self.poll_interval_s = max(0.05, float(poll_interval_s))

    # -- 底层 -------------------------------------------------------------
    def _get(self, path: str, timeout: float = 300.0) -> bytes:
        with urllib.request.urlopen(self.server + path, timeout=timeout) as r:
            return r.read()

    def _post_json(self, path: str, payload: dict, timeout: float = 60.0) -> dict:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.server + path, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    # -- API --------------------------------------------------------------
    def system_stats(self) -> dict:
        return json.loads(self._get("/system_stats", timeout=10.0).decode("utf-8"))

    def queue_prompt(self, prompt_ws: dict) -> dict:
        return self._post_json("/prompt", {"prompt": prompt_ws, "client_id": self.client_id})

    def history(self, prompt_id: str) -> dict:
        raw = self._get("/history/" + urllib.parse.quote(str(prompt_id))).decode("utf-8")
        return json.loads(raw) if raw else {}

    def queue_state(self) -> Optional[dict]:
        try:
            return json.loads(self._get("/queue").decode("utf-8"))
        except Exception:
            return None

    @staticmethod
    def queued_pids(queue: Optional[dict]) -> set:
        if not queue:
            return set()
        pids = set()
        for key in ("queue_running", "queue_pending"):
            for item in queue.get(key) or []:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    pids.add(str(item[1]))
        return pids

    def wait_for(self, prompt_id: str, timeout_s: float = 900.0, quiet_first_s: float = 5.0) -> dict:
        """轮询 history 至完成（comfy_client.py:23-38 移植）。error → RuntimeError；超时 → TimeoutError。"""
        t0 = time.monotonic()
        if quiet_first_s > 0:
            time.sleep(quiet_first_s)
        while True:
            h = self.history(prompt_id)
            if prompt_id in h:
                entry = h[prompt_id]
                status = entry.get("status") or {}
                s = status.get("status_str")
                if status.get("completed") is True or s == "success":
                    return entry
                if s == "error":
                    raise RuntimeError(
                        "execution error: " + json.dumps(status, ensure_ascii=False)[:4000])
            if time.monotonic() - t0 >= timeout_s:
                raise TimeoutError("prompt %s timed out (%.0fs)" % (prompt_id, timeout_s))
            time.sleep(self.poll_interval_s)

    def download_images(self, history_entry: dict, dest_dir: Path | str, prefix: str) -> List[Path]:
        """从 history entry 的 outputs 下载全部图像（comfy_client.py:40-52 移植）。"""
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        out: List[Path] = []
        for node in (history_entry.get("outputs") or {}).values():
            for img in node.get("images") or []:
                q = urllib.parse.urlencode({
                    "filename": img["filename"],
                    "subfolder": img.get("subfolder", ""),
                    "type": img.get("type", ""),
                })
                raw = self._get("/view?" + q)
                name = "%s_%02d.%s" % (prefix, len(out) + 1, str(img["filename"]).rsplit(".", 1)[-1])
                p = dest_dir / name
                with open(p, "wb") as f:
                    f.write(raw)
                out.append(p)
        return out


class _SharedComfyAdapter:
    """把 pipeline.comfy.ComfyClient 适配成本段网关协议（queue/wait/history/download）。

    核心客户端未落地或组装失败时，run_stage 回退到内置 ComfyGateway（A 级移植）。
    """

    def __init__(self, client):
        self._c = client

    def system_stats(self) -> dict:
        return self._c.system_stats()

    def queue_prompt(self, prompt_ws: dict) -> dict:
        pid = self._c.queue_prompt(prompt_ws)  # 返回 pid 字符串
        return {"prompt_id": pid}

    def history(self, prompt_id: str) -> dict:
        return self._c.history(prompt_id)

    def queue_state(self):
        try:
            return self._c.queue_snapshot()
        except Exception:  # noqa: BLE001 —— 与内置网关同语义：不可达返回 None
            return None

    def wait_for(self, prompt_id: str, timeout_s: float = 900.0,
                 quiet_first_s: float = 5.0) -> dict:
        return self._c.wait(prompt_id, timeout_s=timeout_s, quiet_first_s=quiet_first_s)

    def download_images(self, history_entry: dict, dest_dir, prefix: str):
        return [Path(p) for p in self._c.download_images(history_entry, dest_dir, prefix)]


def build_gateway(cfg: Dict[str, Any]):
    """优先 pipeline.comfy.build_client（共享客户端）；失败回退内置网关。"""
    try:
        from pipeline import comfy as _comfy
        return _SharedComfyAdapter(_comfy.build_client(cfg))
    except Exception:  # noqa: BLE001 —— ImportError/参数不匹配均回退，保证段可运行
        comfy_cfg = cfg.get("comfy") or {}
        return ComfyGateway(
            server=comfy_cfg.get("server", "http://127.0.0.1:8188"),
            client_id=str(comfy_cfg.get("client_id", "pipeline")),
            poll_interval_s=float(comfy_cfg.get("poll_interval_s", 3)))


# ----------------------------------------------------------------------------
# 纯逻辑：提示词 / seed / 指纹
# ----------------------------------------------------------------------------

def compose_first_prompt(desc: str, char_anchors: List[str], prop_anchors: List[str],
                         style_anchor: str) -> str:
    """§3 规则 5 次序：镜头描述 + 角色锚点 + 道具锚点 + 画风锚点（空段自动省略）。"""
    parts = [desc] + [a for a in char_anchors if a and a.strip()] + \
            [a for a in prop_anchors if a and a.strip()]
    if style_anchor and style_anchor.strip():
        parts.append(style_anchor)
    return ", ".join(str(p).strip() for p in parts if p and str(p).strip())


#: anime 通道 img2img 的保身份措辞：与 photo 同指令但去掉 "Edit <image1>:" 前缀——
#: 该 token 是 Sakura/Qwen edit 编码的语义标记，对 SDXL CLIPTextEncode 无意义（C 级设计值）。
EDIT_TEMPLATE_ANIME = (
    "keep exactly the same person, face, hairstyle, outfit, camera angle, "
    "framing, lighting and background; change only — {desc}"
)


def compose_edit_prompt(last_desc: str, style_anchor: str, channel: str = "photo") -> str:
    """末帧 edit 提示词（photo = E() 泛化模板 + 画风锚点，结构同 render_keyframes.py:26-28）。"""
    tmpl = EDIT_TEMPLATE if channel == "photo" else EDIT_TEMPLATE_ANIME
    p = tmpl.format(desc=str(last_desc).strip())
    if style_anchor and style_anchor.strip():
        p += ". " + style_anchor.strip()
    return p


def compose_negative(base: str, char_negatives: List[str], shot_negative: str,
                     edit: bool = False) -> str:
    parts = [base] + [n for n in char_negatives if n and n.strip()]
    if shot_negative and shot_negative.strip():
        parts.append(shot_negative)
    if edit:
        parts.append(NEGEDIT_TAIL)
    return ", ".join(str(p).strip().strip(",") for p in parts if p and str(p).strip().strip(","))


def first_seed(seed_base: Optional[int], shot_index_1based: int, explicit) -> int:
    """§3 规则 4：显式 seed 优先（0/None = 派生）；派生 = seed_base + 10×镜序。

    委托 pipeline.seeds（全段统一实现，杜绝各自发明）；未落地时用同式回退。
    """
    if explicit is not None and not isinstance(explicit, bool) and int(explicit) != 0:
        return int(explicit)
    if seed_base is None:
        raise ValueError("项目未配置 seed_base 且本镜未写显式 seed（设计 §3 规则 4）")
    try:
        from pipeline import seeds as _seeds
        return _seeds.kf_first_seed(seed_base, shot_index_1based)
    except ImportError:
        return int(seed_base) + 10 * shot_index_1based


def last_seed(first_seed_value: int, mode: str) -> int:
    """edit=+5 / fresh=+7（render_keyframes.py:133,140 A 级沿用；委托 pipeline.seeds）。"""
    delta = 7 if mode == "fresh" else 5
    try:
        from pipeline import seeds as _seeds
        delta = _seeds.kf_last_seed(0, 0, mode) - _seeds.kf_first_seed(0, 0)
    except ImportError:
        pass
    return int(first_seed_value) + delta


def _fingerprint(path: Optional[Path]) -> str:
    """§5 上游指纹 = 文件名|字节大小|mtime_ns（O(1)，不哈希内容）；委托 pipeline.manifest。"""
    if path is None:
        return "missing"
    try:
        from pipeline import manifest as _m
        return _m.fingerprint_file(path)
    except ImportError:
        pass
    try:
        st = Path(path).stat()
    except OSError:
        return "missing"
    return "%s|%d|%d" % (Path(path).name, st.st_size, st.st_mtime_ns)


def _file_ok(path: Path, min_bytes: int) -> bool:
    try:
        return path.stat().st_size >= min_bytes
    except OSError:
        return False


# ----------------------------------------------------------------------------
# manifest（§5 schema：原子写 / 三条件完成判定 / 三路失效）
# ----------------------------------------------------------------------------

def stage_dir(project_dir: Path | str) -> Path:
    try:
        from pipeline import manifest as _m
        return _m.stage_dir_for(project_dir, STAGE)
    except ImportError:
        return Path(project_dir) / "keyframes"


def manifest_path(project_dir: Path | str) -> Path:
    try:
        from pipeline import manifest as _m
        return _m.manifest_path_for(project_dir, STAGE)
    except ImportError:
        return Path(project_dir) / "keyframes" / "keyframes_manifest.json"


def new_manifest(project_name: str = "") -> Dict[str, Any]:
    return {"stage": STAGE, "project": project_name, "schema": 1,
            "config_hash": "", "updated_at": _now_iso(), "units": {}}


def load_manifest(path: Path | str) -> Dict[str, Any]:
    try:
        from pipeline import manifest as _m
        data = _m.load(path)
        if isinstance(data, dict):
            # 核心对缺失文件按路径猜测 stage/project（unknown/Temp）——归一化为本段骨架
            if not data.get("stage") or data.get("stage") == "unknown":
                data["stage"] = STAGE
            if not isinstance(data.get("units"), dict):
                data["units"] = {}
            return data
    except ImportError:
        pass
    p = Path(path)
    if not p.exists():
        return new_manifest()
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return new_manifest()
    if not isinstance(data, dict) or not isinstance(data.get("units"), dict):
        mf = new_manifest()
        mf["units"] = data.get("units") if isinstance(data.get("units"), dict) else {}
        return mf
    return data


def save_manifest(path: Path | str, manifest: Dict[str, Any]) -> None:
    """原子写（§5）；委托 pipeline.manifest，未落地时用同式回退。"""
    try:
        from pipeline import manifest as _m
        _m.save(path, manifest)
        return
    except ImportError:
        pass
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    manifest["updated_at"] = _now_iso()
    tmp = p.with_name(p.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def unit_done(entry: Optional[dict], file_path: Path, min_bytes: int = MIN_BYTES_DEFAULT) -> bool:
    """§5 完成判定：status==done ∧ 文件存在 ∧ 大小 ≥ 下限，三条件同时满足。"""
    if not entry or entry.get("status") != "done":
        return False
    return _file_ok(Path(file_path), min_bytes)


# ----------------------------------------------------------------------------
# 输入装载（shots.yaml / characters.yaml / project.yaml）
# ----------------------------------------------------------------------------

def _read_text(path: Path) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _read_project_overrides(project_dir: Path) -> Dict[str, Any]:
    """提前读 project.yaml 的 overrides（§2.2）——schema 校验（duration_s 兜底等）
    需要完整合并后的 cfg；解析问题交由 schema 稍后权威报告，这里失败只回 {}。"""
    try:
        p = Path(project_dir) / "project.yaml"
        if not p.exists():
            return {}
        doc = parse_yaml(_read_text(p))
        ov = doc.get("overrides") if isinstance(doc, dict) else None
        return ov if isinstance(ov, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def load_stage_inputs(project_dir: Path, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """项目输入装载：优先 pipeline.schema.load_project_data（读+全量校验，§6），
    其返回结构适配为本段形状；schema 未落地时回退 load_project_inputs（自研宽松装载）。

    cfg 必传合并后的完整配置：schema 对 shots.duration_s 的第三优先级兜底取
    cfg video.default_duration_s（§4.0）——不传会把"cli 过、段内拒"的合法项目错杀。
    schema 严格模式：任何结构性问题 → SchemaError（全量问题清单），run_stage 汇报后退出，
    与 §4.0.3"列出缺口、非零退出，不自动越级"一致。
    """
    try:
        from pipeline import schema as _schema
    except ImportError:
        return load_project_inputs(project_dir)
    data = _schema.load_project_data(project_dir, cfg)  # SchemaError(ValueError) 向上抛
    shots_doc = data.get("shots") or {}
    chars_doc = data.get("characters") or {}
    shots = [s for s in (shots_doc.get("shots") or []) if isinstance(s, dict)]
    chars_by_id = {c["id"]: c for c in (chars_doc.get("characters") or [])
                   if isinstance(c, dict) and isinstance(c.get("id"), str)}
    return {"shots_doc": shots_doc, "shots": shots, "chars_by_id": chars_by_id,
            "props_catalog": chars_doc.get("props_catalog") or {},
            "project_doc": data.get("project") or {}, "errors": []}


def load_project_inputs(project_dir: Path) -> Dict[str, Any]:
    """解析三份项目 yaml。shots.yaml 必需；characters.yaml / project.yaml 可选（缺文件=空）。

    返回 {"shots_doc", "shots", "chars_by_id", "props_catalog", "project_doc", "errors"}；
    结构性错误进 errors（逐镜列出，不整体崩溃），由 run_stage 汇总退出。
    """
    project_dir = Path(project_dir)
    errors: List[str] = []
    shots_path = project_dir / "shots.yaml"
    if not shots_path.exists():
        raise FileNotFoundError("未找到 %s（keyframes 段必需，设计 §6）" % shots_path)
    shots_doc = parse_yaml(_read_text(shots_path))
    if not isinstance(shots_doc, dict):
        errors.append("shots.yaml 顶层必须是映射，得到 %s" % type(shots_doc).__name__)
        shots_doc, shots = {}, []
    shots = shots_doc.get("shots")
    if not isinstance(shots, list) or not shots:
        errors.append("shots.yaml 缺少非空 shots 列表（设计 §6 模板）")
        shots = []
    shots = [s for s in shots if isinstance(s, dict)]
    for i, s in enumerate(shots, 1):
        if not isinstance(s.get("id"), str):
            errors.append("第 %d 镜缺少字符串 id" % i)

    chars_by_id: Dict[str, dict] = {}
    props_catalog: Dict[str, str] = {}
    chars_path = project_dir / "characters.yaml"
    if chars_path.exists():
        doc = parse_yaml(_read_text(chars_path))
        if isinstance(doc, dict):
            for c in doc.get("characters") or []:
                if isinstance(c, dict) and isinstance(c.get("id"), str):
                    chars_by_id[c["id"]] = c
            pc = doc.get("props_catalog")
            if isinstance(pc, dict):
                props_catalog.update(pc)

    project_doc: Dict[str, Any] = {}
    proj_path = project_dir / "project.yaml"
    if proj_path.exists():
        doc = parse_yaml(_read_text(proj_path))
        if isinstance(doc, dict):
            project_doc = doc

    return {"shots_doc": shots_doc, "shots": shots, "chars_by_id": chars_by_id,
            "props_catalog": props_catalog, "project_doc": project_doc, "errors": errors}


# ----------------------------------------------------------------------------
# unit 计划
# ----------------------------------------------------------------------------

@dataclasses.dataclass
class Unit:
    key: str                 # "S1:first"
    sid: str                 # "S1"
    kind: str                # "first" | "last"
    index: int               # 镜序，1 起
    channel: str             # "photo" | "anime"
    last_mode: str           # "edit" | "fresh"（first unit 记 shot 的模式仅供审计）
    use_char_ref: bool
    dest_name: str           # "S1_first.png"
    prompt: str
    negative: str
    seed: int
    filename_prefix: str
    inputs_hash: str
    needs_first_png: bool = False    # last 且 edit
    charref_char_id: Optional[str] = None
    charref_name: str = ""


@dataclasses.dataclass
class StageOptions:
    only: Tuple[str, ...] = ()
    force: bool = False
    dry_run: bool = False
    keep_stale: bool = False
    abandon_running: bool = False

    def match(self, unit: Unit) -> bool:
        if not self.only:
            return True
        return unit.sid in self.only or unit.key in self.only


def _coerce_args(args) -> StageOptions:
    """兼容 StageOptions / argparse.Namespace / dict / None。"""
    if isinstance(args, StageOptions):
        return args
    if args is None:
        return StageOptions()
    if isinstance(args, dict):
        opts = StageOptions()
        for k in ("force", "dry_run", "keep_stale", "abandon_running"):
            if k in args:
                setattr(opts, k, bool(args[k]))
        only = args.get("only")
        if isinstance(only, str):
            opts.only = tuple(x.strip() for x in only.split(",") if x.strip())
        elif isinstance(only, (list, tuple)):
            opts.only = tuple(str(x).strip() for x in only if str(x).strip())
        return opts
    # argparse.Namespace 等：按属性名取，缺省容忍
    opts = StageOptions()
    for k in ("force", "dry_run", "keep_stale", "abandon_running"):
        setattr(opts, k, bool(getattr(args, k, False)))
    only = getattr(args, "only", None)
    if isinstance(only, str):
        opts.only = tuple(x.strip() for x in only.split(",") if x.strip())
    elif isinstance(only, (list, tuple)):
        opts.only = tuple(str(x).strip() for x in only if str(x).strip())
    return opts


def _ascii_prefix(project_name: str, sid: str, kind: str) -> str:
    """SaveImage filename_prefix：ComfyUI 输出子路径，非 ASCII 一律替换为 _。"""
    proj = re.sub(r"[^A-Za-z0-9_\-]", "_", project_name) or "project"
    return "%s/kf/%s_%s" % (proj, sid, kind)


def _shot_field(shot: dict, shots_doc: dict, key: str, default=None):
    """§4.0 取值优先级：单镜字段 > shots.defaults > default（不合并，整体覆盖）。"""
    v = shot.get(key)
    if v is not None:
        return v
    d = shots_doc.get("defaults") or {}
    return d.get(key, default)


def char_anchor_text(char: dict) -> str:
    """角色锚点 = anchor_en（+ outfit_en，逗号衔接；参考实现 CHAR 内联着装的做法泛化）。"""
    parts = [str(char.get("anchor_en") or "").strip(),
             str(char.get("outfit_en") or "").strip()]
    return ", ".join(p for p in parts if p)


def plan_units(data: Dict[str, Any], cfg: Dict[str, Any], project_name: str
               ) -> Tuple[List[Unit], List[str], List[str], List[str]]:
    """由项目输入计算全部 unit（顺序 = 镜序 × first→last，主循环直接按序消费）。

    返回 (units, errors, needed_char_ids, warnings)。坏镜进 errors 不产生 unit，
    不阻塞好镜；warnings 是不致命问题（如道具不在词库，锚点跳过但镜照常渲染）。
    """
    shots_doc = data["shots_doc"]
    shots = data["shots"]
    chars_by_id = data["chars_by_id"]
    props_catalog = data["props_catalog"]
    project_doc = data["project_doc"]
    channels = cfg.get("channels") or {}
    errors: List[str] = list(data["errors"])
    warnings: List[str] = []
    units: List[Unit] = []
    needed: List[str] = []
    seen_ids = set()

    seed_base = project_doc.get("seed_base")
    if seed_base is not None:
        try:
            seed_base = int(seed_base)
        except (TypeError, ValueError):
            errors.append("project.yaml seed_base=%r 不是整数" % (seed_base,))
            seed_base = None
    style_anchors = project_doc.get("style_anchors") or {}
    neg_anchors = project_doc.get("negative_anchors") or {}

    for i, shot in enumerate(shots, 1):
        sid = shot.get("id")
        if not isinstance(sid, str) or not sid:
            errors.append("第 %d 镜缺少 id，跳过" % i)
            continue
        if not _SHOT_ID_RE.match(sid):
            errors.append("%s: id 含文件名不安全字符（限 [A-Za-z0-9_-.]），跳过" % sid)
            continue
        if sid in seen_ids:
            errors.append("%s: 镜号重复，跳过后者" % sid)
            continue
        seen_ids.add(sid)

        # -- 通道 ------------------------------------------------------
        style = _shot_field(shot, shots_doc, "style") or shots_doc.get("style") \
            or project_doc.get("style") or "photo"
        if style not in channels:
            errors.append("%s: 未知通道 %r（config channels 现有 %s）"
                          % (sid, style, sorted(channels)))
            continue
        # -- 角色与道具锚点 ---------------------------------------------
        raw_chars = _shot_field(shot, shots_doc, "characters") or []
        if isinstance(raw_chars, str):
            raw_chars = [raw_chars]
        char_ids: List[str] = []
        bad = False
        for cid in raw_chars:
            cid = str(cid)
            if cid not in chars_by_id:
                errors.append("%s: 引用了 characters.yaml 不存在的角色 %r" % (sid, cid))
                bad = True
            elif cid not in char_ids:
                char_ids.append(cid)
        if bad:
            continue
        for cid in char_ids:
            if cid not in needed:
                needed.append(cid)
        char_anchors = [char_anchor_text(chars_by_id[c]) for c in char_ids]
        char_negs = [str(chars_by_id[c].get("negative_extra") or "").strip()
                     for c in char_ids]
        char_negs = [n for n in char_negs if n]

        raw_props = _shot_field(shot, shots_doc, "props") or []
        if isinstance(raw_props, str):
            raw_props = [raw_props]
        prop_anchors = []
        for prop in raw_props:
            prop = str(prop)
            anchor = props_catalog.get(prop)
            if anchor is None:
                warnings.append("%s: 道具 %r 不在 characters.yaml props_catalog，忽略该道具锚点" % (sid, prop))
                continue
            prop_anchors.append(str(anchor))

        # -- 提示词各段 -------------------------------------------------
        style_anchor = str(style_anchors.get(style) or "").strip()
        neg_base = str(neg_anchors.get(style) or "").strip() \
            or str((channels.get(style) or {}).get("negative") or "")
        shot_neg = str(_shot_field(shot, shots_doc, "negative_extra") or "").strip()

        first_desc = str(shot.get("first") or "").strip()
        if not first_desc:
            errors.append("%s: 缺少 first 描述（首帧无从生成）" % sid)
            continue

        last_mode = str(_shot_field(shot, shots_doc, "last_mode", "edit") or "edit").lower()
        if last_mode not in ("edit", "fresh"):
            errors.append("%s: last_mode=%r 无效（edit|fresh）" % (sid, last_mode))
            continue

        use_char_ref = bool(_shot_field(shot, shots_doc, "use_char_ref", False))
        charref_char_id: Optional[str] = None
        if use_char_ref:
            if style != "photo":
                errors.append("%s: use_char_ref 仅支持 photo 通道（设计 §4.2，B 级）" % sid)
                continue
            if not char_ids:
                errors.append("%s: use_char_ref 需要镜内至少一个角色（三视图来源）" % sid)
                continue
            charref_char_id = char_ids[0]

        # -- seed --------------------------------------------------------
        explicit = _shot_field(shot, shots_doc, "seed")
        try:
            f_seed = first_seed(seed_base, i, explicit)
        except ValueError as e:
            errors.append("%s: %s" % (sid, e))
            continue
        l_seed = last_seed(f_seed, last_mode)

        # -- 末帧描述 ----------------------------------------------------
        if last_mode == "fresh":
            last_desc = str(shot.get("last_fresh") or shot.get("last") or "").strip()
            last_prompt = compose_first_prompt(last_desc, char_anchors, prop_anchors, style_anchor)
            last_negative = compose_negative(neg_base, char_negs, shot_neg, edit=False)
        else:
            last_desc = str(shot.get("last") or "").strip()
            last_prompt = compose_edit_prompt(last_desc, style_anchor, channel=style)
            last_negative = compose_negative(neg_base, char_negs, shot_neg, edit=True)
        if not last_desc:
            errors.append("%s: last_mode=%s 但缺少 last/last_fresh 描述" % (sid, last_mode))
            continue

        common = {"sid": sid, "index": i, "channel": style, "last_mode": last_mode}

        first_hash = _sha256_obj({
            "sid": sid, "kind": "first", "index": i, "channel": style, "seed": f_seed,
            "use_char_ref": use_char_ref, "desc": first_desc,
            "char_anchors": char_anchors, "prop_anchors": prop_anchors,
            "style_anchor": style_anchor, "negative_base": neg_base,
            "char_negatives": char_negs, "shot_negative_extra": shot_neg,
        })
        units.append(Unit(
            key="%s:first" % sid, kind="first", dest_name="%s_first.png" % sid,
            prompt=compose_first_prompt(first_desc, char_anchors, prop_anchors, style_anchor),
            negative=compose_negative(neg_base, char_negs, shot_neg, edit=False),
            seed=f_seed, filename_prefix=_ascii_prefix(project_name, sid, "first"),
            inputs_hash=first_hash, needs_first_png=False,
            use_char_ref=use_char_ref,
            charref_char_id=charref_char_id,
            charref_name=("%s_charref.png" % sid) if use_char_ref else "",
            **common))

        last_hash = _sha256_obj({
            "sid": sid, "kind": "last", "index": i, "channel": style, "seed": l_seed,
            "use_char_ref": False, "last_mode": last_mode, "desc": last_desc,
            "char_anchors": char_anchors, "prop_anchors": prop_anchors,
            "style_anchor": style_anchor, "negative_base": neg_base,
            "char_negatives": char_negs, "shot_negative_extra": shot_neg,
        })
        units.append(Unit(
            key="%s:last" % sid, kind="last", dest_name="%s_last.png" % sid,
            prompt=last_prompt, negative=last_negative, seed=l_seed,
            filename_prefix=_ascii_prefix(project_name, sid, "last"),
            inputs_hash=last_hash,
            needs_first_png=(last_mode == "edit"),
            use_char_ref=False, charref_char_id=None, charref_name="",
            **common))

    return units, errors, needed, warnings


# ----------------------------------------------------------------------------
# assets pick 门（§4.1：未 pick 拒开跑，宁可停不糊）
# ----------------------------------------------------------------------------

def check_assets_gate(project_dir: Path, needed_char_ids: List[str],
                      chars_by_id: Dict[str, dict], charref_char_ids: List[str],
                      any_char_ref: bool, log: Callable[[str], None]
                      ) -> Dict[str, Path]:
    """返回 {char_id: 已 pick 三视图路径}；门禁不过时抛 RuntimeError。

    与 schema/assets/cli 同语义（schema.py:141 允许 three_view:false —— 该类角色不产
    三视图、门禁跳过；assets.py plan_units 对 false 不建 threeview unit；cli
    dependency_gaps 亦跳过）。use_char_ref 引用的角色无论 flag 一律需要已 pick 三视图
    （B 级参考图路径硬依赖）。无 assets manifest：纯锚点路径放行，但 use_char_ref 不可用。
    """
    project_dir = Path(project_dir)
    mf_path = project_dir / "assets" / "assets_manifest.json"
    charref_ids = [c for c in charref_char_ids if c]
    if not mf_path.exists():
        if any_char_ref:
            raise RuntimeError(
                "use_char_ref 需要 assets 段产物：未找到 %s（先跑 assets 段并 pick 三视图，设计 §4.1/§4.2）" % mf_path)
        log("提示：未发现 assets/assets_manifest.json —— 按纯锚点提示词渲染（A 级验证路径，设计 §4.2）")
        return {}
    try:
        with open(mf_path, "r", encoding="utf-8") as f:
            mf = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        raise RuntimeError("assets manifest 不可读（%s）：%s" % (mf_path, e))
    units = mf.get("units") or {}
    picked: Dict[str, Path] = {}
    problems: List[str] = []
    gate_ids: List[str] = []
    for cid in needed_char_ids:
        assets = (chars_by_id.get(cid) or {}).get("assets") or {}
        want_ref = cid in charref_ids
        if not assets.get("three_view", True):
            if want_ref:
                problems.append("%s: use_char_ref 需要三视图，但 characters.yaml 声明 "
                                "assets.three_view: false（两者矛盾，改 yaml 或关 use_char_ref）" % cid)
                gate_ids.append(cid)
            continue  # three_view:false 且不作参考图 → 不设三视图，门禁跳过
        gate_ids.append(cid)
    for cid in gate_ids:
        entry = units.get("%s:threeview" % cid)
        if not entry or entry.get("status") != "done":
            problems.append("%s: 三视图未完成（先跑 assets 段）" % cid)
            continue
        if not entry.get("picked"):
            problems.append("%s: 三视图未 pick（assets 段人工挑选或 --auto-pick，设计 §4.1）" % cid)
            continue
        fname = (entry.get("files") or {}).get("threeview") or ("%s_threeview.png" % cid)
        p = project_dir / "assets" / fname
        if not p.exists():
            problems.append("%s: 三视图文件缺失：%s" % (cid, p))
            continue
        picked[cid] = p
    if problems:
        for line in problems:
            log("assets 门禁：" + line)
        raise RuntimeError("存在未就绪角色锚点，keyframes 拒绝开跑（宁可停，不糊——设计 §4.1）："
                           + "；".join(problems))
    return picked


# ----------------------------------------------------------------------------
# 执行
# ----------------------------------------------------------------------------

@dataclasses.dataclass
class RunContext:
    project_dir: Path
    project_name: str
    kf_dir: Path
    raw_dir: Path
    cfg: Dict[str, Any]
    opts: StageOptions
    log: Callable[[str], None]
    mf_path: Optional[Path] = None
    gateway: Optional[ComfyGateway] = None
    input_dirs: List[Path] = dataclasses.field(default_factory=list)
    min_bytes: int = MIN_BYTES_DEFAULT
    attempts: int = 2
    retry_wait_s: float = 20.0
    quiet_first_s: float = 5.0
    timeout_s: float = 900.0
    config_hash: str = ""
    charref_files: Dict[str, Path] = dataclasses.field(default_factory=dict)
    first_failed: set = dataclasses.field(default_factory=set)


def _current_upstream(unit: Unit, ctx: RunContext) -> Dict[str, str]:
    up: Dict[str, str] = {}
    if unit.kind == "last" and unit.needs_first_png:
        up["first_png"] = _fingerprint(ctx.kf_dir / ("%s_first.png" % unit.sid))
    if unit.kind == "first" and unit.use_char_ref and unit.charref_name:
        up["charref_png"] = _fingerprint(ctx.charref_files.get(unit.charref_char_id or ""))
    return up


def _is_stale(unit: Unit, entry: dict, ctx: RunContext) -> bool:
    """§5 失效判定：inputs_hash ∨ upstream 指纹 ∨ 段配置哈希任一变化（委托 pipeline.manifest）。"""
    try:
        from pipeline import manifest as _m
        reason = _m.stale_reason(
            entry, inputs_hash=unit.inputs_hash,
            upstream=_current_upstream(unit, ctx), stage_cfg_hash=ctx.config_hash)
        return reason is not None
    except ImportError:
        pass
    if entry.get("inputs_hash") != unit.inputs_hash:
        return True
    if ctx.config_hash and entry.get("stage_cfg_hash", entry.get("config_hash")) \
            not in ("", ctx.config_hash):
        return True
    old = entry.get("upstream") or {}
    for k, v in _current_upstream(unit, ctx).items():
        if old.get(k) != v:
            return True
    return False


def classify_unit(unit: Unit, mf: Dict[str, Any], ctx: RunContext) -> Tuple[str, str]:
    """单一决策源：dry-run 打印与真实执行共用（保证两者一致）。"""
    if ctx.opts.only and not ctx.opts.match(unit):
        return ("skip", "--only 未选中")
    if unit.kind == "last" and unit.sid in ctx.first_failed:
        return ("skip", "首帧本次失败，整镜跳过（render_keyframes.py:122-123 同策略）")
    entry = (mf.get("units") or {}).get(unit.key) or {}
    dest = ctx.kf_dir / unit.dest_name
    file_ok = _file_ok(dest, ctx.min_bytes)
    done = entry.get("status") == "done"
    if file_ok and done and not ctx.opts.force:
        if _is_stale(unit, entry, ctx):
            if ctx.opts.keep_stale:
                return ("skip", "已 stale 但 --keep-stale 保留")
            return ("run", "stale：输入/上游/配置指纹变化（§5 失效判定）")
        return ("skip", "done（断点续跑跳过）")
    if file_ok and not done and not ctx.opts.force:
        return ("skip", "adopt（在盘且达标，收编为 done——A 级存在即跳过的泛化）")
    if done and not file_ok and not ctx.opts.force:
        return ("run", "manifest 标 done 但文件缺失/过小（§5 完成判定不过，重跑）")
    return ("run", "" if not ctx.opts.force else "--force 强制重跑")


def _stage_ref_into_inputs(src_png: Path, ref_name: str, ctx: RunContext) -> None:
    """把参考图复制进两个 input_dirs —— 先 makedirs 再复制（keyframes.log:4-12 教训）。"""
    for d in ctx.input_dirs:
        d.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src_png, d / ref_name)


def _build_workflow(unit: Unit, ctx: RunContext, ref_name: Optional[str]) -> dict:
    params = (ctx.cfg.get("channels") or {}).get(unit.channel) or {}
    resolution = params.get("resolution")
    fresh_t2i = (unit.kind == "first" and not unit.use_char_ref) or \
                (unit.kind == "last" and unit.last_mode == "fresh")
    if fresh_t2i:
        return build_t2i(
            channel=unit.channel, prompt=unit.prompt, negative=unit.negative,
            seed=unit.seed, width=int(params.get("width", 1344)),
            height=int(params.get("height", 768)),
            filename_prefix=unit.filename_prefix, params=params, resolution=resolution)
    # 保身份改画（edit）/ use_char_ref 参考图：一律 edit 图形（设计 §4.2）
    if not ref_name:
        raise RuntimeError("%s: edit 模式缺少参考图文件名（内部错误）" % unit.key)
    return build_edit(
        channel=unit.channel, ref_image_name=ref_name, prompt=unit.prompt,
        negative=unit.negative, seed=unit.seed, filename_prefix=unit.filename_prefix,
        params=params, resolution=resolution)


def _render_unit(unit: Unit, ctx: RunContext, mf: Dict[str, Any]) -> bool:
    """渲染单个 unit：staging → 提交/等待/下载 × attempts → manifest 落账。"""
    entry = mf.setdefault("units", {}).setdefault(unit.key, {})
    dest = ctx.kf_dir / unit.dest_name
    ref_name: Optional[str] = None

    try:
        if unit.kind == "last" and unit.needs_first_png:
            first_png = ctx.kf_dir / ("%s_first.png" % unit.sid)
            if not first_png.exists():
                raise RuntimeError("首帧 %s 不存在（先渲 first）" % first_png.name)
            ref_name = "%s_ref.png" % unit.sid
            _stage_ref_into_inputs(first_png, ref_name, ctx)
        elif unit.kind == "first" and unit.use_char_ref:
            src = ctx.charref_files.get(unit.charref_char_id or "")
            if src is None:
                raise RuntimeError(
                    "use_char_ref：角色 %s 的已 pick 三视图不可用" % unit.charref_char_id)
            ref_name = unit.charref_name
            _stage_ref_into_inputs(src, ref_name, ctx)
    except Exception as e:
        entry.update(status="failed", error="%s: %s" % (type(e).__name__, e),
                     attempts=0, updated_at=_now_iso())
        if ctx.mf_path:
            save_manifest(ctx.mf_path, mf)
        ctx.log("%s: staging 失败：%s" % (unit.key, e))
        return False

    raw_prefix = unit.key.replace(":", "_") + "_raw"
    last_err = "unknown"
    for attempt in range(1, ctx.attempts + 1):
        try:
            wf = _build_workflow(unit, ctx, ref_name)
            resp = ctx.gateway.queue_prompt(wf)
            pid = str(resp.get("prompt_id") or "")
            if not pid:
                raise RuntimeError("ComfyUI 未返回 prompt_id：%r" % (resp,))
            entry.update(status="running", prompt_id=pid, seed=unit.seed,
                         channel=unit.channel, attempts=attempt, error=None,
                         files={unit.kind: unit.dest_name}, updated_at=_now_iso())
            if ctx.mf_path:
                save_manifest(ctx.mf_path, mf)  # kill-safe：running+pid 先落盘（§4.0 孤儿收敛依据）
            ctx.log("%s: 提交 attempt %d/%d seed=%s pid=%s" %
                    (unit.key, attempt, ctx.attempts, unit.seed, pid))
            ctx.log("%s: prompt=%s" % (unit.key, unit.prompt))
            ctx.log("%s: negative=%s" % (unit.key, unit.negative))
            ctx.log("%s: workflow=%s" % (unit.key, json.dumps(wf, ensure_ascii=False)))
            hist = ctx.gateway.wait_for(pid, timeout_s=ctx.timeout_s,
                                        quiet_first_s=ctx.quiet_first_s)
            files = ctx.gateway.download_images(hist, ctx.raw_dir, raw_prefix)
            if not files:
                raise RuntimeError("ComfyUI 历史无图像输出（outputs.images 为空）")
            shutil.copyfile(files[0], dest)
            size = dest.stat().st_size
            if size < ctx.min_bytes:
                raise RuntimeError("产物仅 %dB < 下限 %dB（§5 完成判定）" % (size, ctx.min_bytes))
            entry.update(status="done", error=None, attempts=attempt,
                         inputs_hash=unit.inputs_hash, config_hash=ctx.config_hash,
                         stage_cfg_hash=ctx.config_hash,
                         upstream=_current_upstream(unit, ctx), updated_at=_now_iso())
            if ctx.mf_path:
                save_manifest(ctx.mf_path, mf)
            ctx.log("%s: OK（%s，%dB）" % (unit.key, dest.name, size))
            return True
        except Exception as e:  # noqa: BLE001 —— 单 unit 失败不阻塞其它镜（A 级策略）
            last_err = "%s: %s" % (type(e).__name__, e)
            ctx.log("%s: attempt %d/%d 失败：%s" % (unit.key, attempt, ctx.attempts, last_err))
            if attempt < ctx.attempts and ctx.retry_wait_s > 0:
                time.sleep(ctx.retry_wait_s)
    entry.update(status="failed", error=last_err, updated_at=_now_iso())
    if ctx.mf_path:
        save_manifest(ctx.mf_path, mf)
    return False


def _harvest_history(key: str, dest_name: str, entry: dict, hist: dict,
                     ctx: RunContext, mf: Dict[str, Any],
                     unit: Optional[Unit] = None) -> bool:
    """孤儿收敛收割：按 history entry 下载产物并登记（§4.0）。

    必须同时补齐 inputs_hash/upstream/stage_cfg_hash——否则同轮 classify_unit 判 stale
    会重提全新 job，违反 §4.0.2「收割置 done（不重提）」。unit 缺失（镜已不在
    shots.yaml）时保留旧字段，仅置 done。
    """
    try:
        files = ctx.gateway.download_images(hist, ctx.raw_dir, key.replace(":", "_") + "_orphan")
        if not files:
            raise RuntimeError("历史记录无图像输出")
        dest = ctx.kf_dir / dest_name
        shutil.copyfile(files[0], dest)
        size = dest.stat().st_size
        if size < ctx.min_bytes:
            raise RuntimeError("收割产物仅 %dB < %dB" % (size, ctx.min_bytes))
        fields: Dict[str, Any] = {"status": "done", "error": None, "updated_at": _now_iso()}
        if unit is not None:
            fields.update(seed=unit.seed, channel=unit.channel,
                          files={unit.kind: unit.dest_name},
                          inputs_hash=unit.inputs_hash, config_hash=ctx.config_hash,
                          stage_cfg_hash=ctx.config_hash,
                          upstream=_current_upstream(unit, ctx))
        entry.update(fields)
        if ctx.mf_path:
            save_manifest(ctx.mf_path, mf)
        ctx.log("%s: 孤儿收敛收割完成（%s）%s" % (
            key, dest_name, "" if unit is not None else "（注意：不在当前 shots.yaml 计划）"))
        return True
    except Exception as e:  # noqa: BLE001
        entry.update(status="failed", error="孤儿收割失败：%s" % e, updated_at=_now_iso())
        if ctx.mf_path:
            save_manifest(ctx.mf_path, mf)
        ctx.log("%s: 孤儿收割失败：%s" % (key, e))
        return False


def converge_orphans(mf: Dict[str, Any], ctx: RunContext,
                     units_by_key: Optional[Dict[str, Unit]] = None) -> None:
    """§4.0.2：上次进程死亡遗留的 running unit 收敛。

    history 命中 → 直接收割；仍在 queue → 等待收割（绝不重提，防双 H3 job OOM）；
    两者皆无 → 置回 pending 重跑。等待上限 = 段超时，超时置 failed 提示人工处置。
    单个孤儿收敛异常不中断循环（后续孤儿照常收敛，避免遗留 running 被主循环重提
    形成双 job——§10.7）。
    """
    units_by_key = units_by_key or {}
    for key, entry in list((mf.get("units") or {}).items()):
        if entry.get("status") != "running":
            continue
        unit = units_by_key.get(key)
        pid = entry.get("prompt_id")
        ctx.log("孤儿 unit %s（pid=%s）收敛中…" % (key, pid))
        if ctx.opts.abandon_running:
            entry.update(status="pending", updated_at=_now_iso())
            ctx.log("%s: --abandon-running → 置回 pending 重提" % key)
            continue
        if not pid:
            entry.update(status="pending", updated_at=_now_iso())
            ctx.log("%s: 无 prompt_id → 置回 pending" % key)
            continue
        dest_name = (entry.get("files") or {}).get(
            "last" if key.endswith(":last") else "first") or key.replace(":", "_") + ".png"
        try:
            h = ctx.gateway.history(pid)
        except Exception as e:  # noqa: BLE001 —— 服务器中途不可达：置 failed 提示人工
            entry.update(status="failed", error="孤儿收敛查询 history 失败：%s" % e,
                         updated_at=_now_iso())
            ctx.log("%s: history 查询失败（%s）——请确认 ComfyUI 存活后重跑本段" % (key, e))
            continue
        if pid in h:
            _harvest_history(key, dest_name, entry, h[pid], ctx, mf, unit)
            continue
        if pid in ComfyGateway.queued_pids(ctx.gateway.queue_state()):
            ctx.log("%s: 仍在 ComfyUI 队列/执行中 —— 等待收割（绝不重提，§4.0）" % key)
            try:
                hist = ctx.gateway.wait_for(pid, timeout_s=ctx.timeout_s, quiet_first_s=0.0)
            except TimeoutError:
                entry.update(status="failed", updated_at=_now_iso(),
                             error="队列等待超时（%.0fs）；请人工在 ComfyUI 端取消该 job "
                                   "或重启服务后 --abandon-running 重跑" % ctx.timeout_s)
                if ctx.mf_path:
                    save_manifest(ctx.mf_path, mf)
                ctx.log("%s: 队列等待超时 → failed（人工处置）" % key)
                continue
            except Exception as e:  # noqa: BLE001 —— 执行错误/网络异常：置 failed，继续收敛其余孤儿
                entry.update(status="failed", updated_at=_now_iso(),
                             error="队列等待异常（%s: %s）；确认服务器侧该 job 已结束后 "
                                   "--abandon-running 重跑" % (type(e).__name__, e))
                if ctx.mf_path:
                    save_manifest(ctx.mf_path, mf)
                ctx.log("%s: 队列等待异常 → failed（不中断其余孤儿收敛）：%s" % (key, e))
                continue
            _harvest_history(key, dest_name, entry, hist, ctx, mf, unit)
            continue
        entry.update(status="pending", updated_at=_now_iso())
        ctx.log("%s: history 与 queue 均查无（服务器或已重启）→ 置回 pending 重跑" % key)
    if ctx.mf_path:
        save_manifest(ctx.mf_path, mf)


def describe_status(mf: Dict[str, Any]) -> List[str]:
    """供 `pipeline status` 汇总：每 unit 一行。"""
    lines = []
    for key, entry in sorted((mf.get("units") or {}).items()):
        lines.append("%-14s %-8s attempts=%s seed=%s %s" % (
            key, entry.get("status", "?"), entry.get("attempts", "?"),
            entry.get("seed", "?"), entry.get("error") or ""))
    return lines


# ----------------------------------------------------------------------------
# 段入口
# ----------------------------------------------------------------------------

def _resolve_project_dir(project: str | Path, cfg: Dict[str, Any]) -> Path:
    p = Path(project)
    if p.is_dir():
        return p.resolve()
    resolved = _resolve_path((cfg.get("paths") or {}).get("projects_root", "projects")) / str(project)
    if resolved.is_dir():
        return resolved.resolve()
    return resolved  # 不存在也返回，让上游报"未找到 shots.yaml"这类可读错误


def run_stage(project_dir, config=None, args=None) -> int:
    """keyframes 段主入口（stages 包约定签名）。

    :param project_dir: 项目目录（或项目名——按 cfg paths.projects_root 解析）
    :param config: 合并后配置 dict；None 时探测 pipeline.config / 回退内置默认
    :param args: StageOptions / argparse.Namespace / dict（only/force/dry_run/keep_stale/abandon_running）
    :return: 0 成功；1 失败或输入错误；2 ComfyUI 不可达
    """
    opts = _coerce_args(args)
    raw_dir_arg = Path(str(project_dir))
    guess = raw_dir_arg.resolve() if raw_dir_arg.is_dir() else None
    cfg = copy.deepcopy(config) if isinstance(config, dict) else None
    if cfg is None:
        try:
            cfg = load_merged_config(guess)
        except ValueError as e:  # 含核心 ConfigError（缺全局配置/项目目录等）
            print("keyframes 段中止（配置装载失败）：%s" % e, file=sys.stderr, flush=True)
            return 1
    cfg.pop("_defaults_only", None)

    project_dir = _resolve_project_dir(project_dir, cfg)
    project_name = Path(project_dir).name

    logs_dir = _resolve_path((cfg.get("paths") or {}).get("logs_dir", "logs"))
    log = StageLog(logs_dir / ("%s_%s.log" % (project_name, STAGE)))

    # -- 项目输入（schema 优先；缺配置/校验失败 → 列出问题退出） -----------------
    # project.yaml overrides 先并入 cfg（§2.2）——schema 的 duration_s 兜底等需要
    # 完整合并值（cli 传入的 merged_cfg 已含 overrides，此处再并一次幂等）
    ov = _read_project_overrides(project_dir)
    if ov:
        _deep_update(cfg, ov)
    try:
        data = load_stage_inputs(project_dir, cfg)
    except FileNotFoundError as e:
        log("keyframes 段中止：%s" % e)
        return 1
    except ValueError as e:  # 含 ConfigError / SchemaError / MiniYAMLError（全量问题清单）
        log("keyframes 段中止（项目输入未过校验）：")
        if isinstance(e, (list, tuple)):
            for line in e:
                log("  - %s" % line)
        else:
            for line in str(e).splitlines() or [str(e)]:
                log("  %s" % line)
        return 1
    project_doc = data["project_doc"]
    # §5 config_hash = 该段相关配置节（channels）；委托 config.stage_sections_hash
    config_hash = ""
    try:
        from pipeline.config import stage_sections_hash
        config_hash = stage_sections_hash(cfg, STAGE)
    except Exception:  # noqa: BLE001
        config_hash = str(cfg.get("config_hash") or "") or _sha256_obj(cfg)

    ctx = RunContext(
        project_dir=project_dir, project_name=project_name,
        kf_dir=stage_dir(project_dir), raw_dir=stage_dir(project_dir) / "raw",
        cfg=cfg, opts=opts, log=log, config_hash=config_hash)
    ctx.min_bytes = int((cfg.get("keyframes") or {}).get("min_bytes", MIN_BYTES_DEFAULT))
    run_cfg = cfg.get("run") or {}
    ctx.attempts = max(1, int(run_cfg.get("attempts", 2)))
    ctx.retry_wait_s = float(run_cfg.get("retry_wait_s", 20))
    ctx.quiet_first_s = float(run_cfg.get("quiet_first_s", 5))
    comfy_cfg = cfg.get("comfy") or {}
    ctx.timeout_s = float(comfy_cfg.get("timeout_img_s", 900))
    ctx.input_dirs = [_resolve_path(d) for d in (comfy_cfg.get("input_dirs") or [])]

    log("== keyframes 段开始：项目=%s dry_run=%s only=%s force=%s config_hash=%s ==" % (
        project_name, opts.dry_run, ",".join(opts.only) or "-", opts.force, config_hash[:19]))

    units, plan_errors, needed_chars, plan_warnings = plan_units(data, cfg, project_name)
    for err in plan_errors:
        log("计划错误：%s" % err)
    for warn in plan_warnings:
        log("计划警告：%s" % warn)

    mf_path = manifest_path(project_dir)
    try:
        mf = load_manifest(mf_path)
    except (RuntimeError, ValueError) as e:  # 含核心 ManifestError（损坏/结构不合法）
        log("keyframes 段中止：manifest 不可读：%s" % e)
        return 1
    mf.update(stage=STAGE, project=project_name, schema=1, config_hash=config_hash)

    # -- assets pick 门（§4.1；three_view:false 角色不设三视图，跳过门禁） --------
    any_char_ref = any(u.use_char_ref for u in units)
    charref_ids = [u.charref_char_id for u in units if u.use_char_ref]
    try:
        ctx.charref_files = check_assets_gate(
            project_dir, needed_chars, data["chars_by_id"], charref_ids, any_char_ref, log)
    except RuntimeError as e:
        log("keyframes 段中止：%s" % e)
        return 1

    # -- dry-run：打印决策即止（不触网/不写盘） ------------------------------
    if opts.dry_run:
        actions = [classify_unit(u, mf, ctx) for u in units]
        for unit, (action, reason) in zip(units, actions):
            log("dry-run %s: %s%s" % (
                unit.key, action, ("（%s）" % reason) if reason else ""))
            if action == "run":
                log("dry-run %s: seed=%s channel=%s last_mode=%s" % (
                    unit.key, unit.seed, unit.channel, unit.last_mode))
                log("dry-run %s: prompt=%s" % (unit.key, unit.prompt))
                log("dry-run %s: negative=%s" % (unit.key, unit.negative))
        log("dry-run 结束：%d unit（RUN=%d SKIP=%d）；计划错误 %d 条" % (
            len(units),
            sum(1 for a, _ in actions if a == "run"),
            sum(1 for a, _ in actions if a == "skip"),
            len(plan_errors)))
        for k, e in sorted((mf.get("units") or {}).items()):
            if e.get("status") == "failed" and k not in {u.key for u in units}:
                log("历史遗留 failed（不在当前 shots.yaml，不计入退出码，可手工清理 manifest）：%s" % k)
        return 1 if plan_errors else 0

    # -- 服务器预检（§4.0.1） ----------------------------------------------
    ctx.gateway = build_gateway(cfg)
    if run_cfg.get("server_precheck", True):
        try:
            ctx.gateway.system_stats()
        except Exception as e:  # noqa: BLE001
            log("ComfyUI 不可达（%s）——请先运行 run_nvidia_gpu.bat 拉起 8188（设计 §4.0.1）" % e)
            return 2

    # -- 孤儿收敛（§4.0.2；传入本轮计划，收割置 done 时补齐指纹防 stale 重提） ----
    ctx.mf_path = mf_path
    try:
        converge_orphans(mf, ctx, {u.key: u for u in units})
    except Exception as e:  # noqa: BLE001
        log("孤儿收敛异常（继续主循环）：%s" % e)

    # -- 主循环（串行一次一个 job，§4.3 资源时序） -----------------------------
    ctx.kf_dir.mkdir(parents=True, exist_ok=True)
    ctx.raw_dir.mkdir(parents=True, exist_ok=True)
    for unit in units:
        action, reason = classify_unit(unit, mf, ctx)
        if action == "skip":
            if reason.startswith("adopt"):
                entry = mf["units"].setdefault(unit.key, {})
                entry.update(status="done", seed=unit.seed, channel=unit.channel,
                             files={unit.kind: unit.dest_name},
                             inputs_hash=unit.inputs_hash, config_hash=ctx.config_hash,
                             stage_cfg_hash=ctx.config_hash,
                             upstream=_current_upstream(unit, ctx), error=None,
                             attempts=0, updated_at=_now_iso())
                save_manifest(mf_path, mf)
            log("%s: 跳过（%s）" % (unit.key, reason))
            continue
        if reason:
            log("%s: 重跑（%s）" % (unit.key, reason))
        ok = _render_unit(unit, ctx, mf)
        if not ok and unit.kind == "first":
            ctx.first_failed.add(unit.sid)
    save_manifest(mf_path, mf)

    # 退出码只看**本轮计划内**的 unit——历史遗留 failed（镜已从 shots.yaml 删除）只提示
    planned_keys = {u.key for u in units}
    failed = sorted(k for k in planned_keys
                    if (mf.get("units", {}).get(k) or {}).get("status") == "failed")
    historical = sorted(k for k, e in (mf.get("units") or {}).items()
                        if e.get("status") == "failed" and k not in planned_keys)
    for k in historical:
        log("历史遗留 failed（不在当前 shots.yaml，不计入退出码，可手工清理 manifest）：%s" % k)
    log("== keyframes 段结束：unit=%d done=%d failed=%d ==" % (
        len(units),
        sum(1 for u in units
            if (mf.get("units", {}).get(u.key) or {}).get("status") == "done"),
        len(failed)))
    if failed or plan_errors:
        for k in failed:
            log("失败清单：%s —— %s" % (k, (mf["units"][k].get("error") or "")[:200]))
        return 1
    return 0


def selftest_checks(with_server: bool = False, strict: bool = False) -> List[Dict[str, Any]]:
    """根 selftest.py C 段聚合入口：复用 pipeline/selftest_keyframes.py 的干跑用例。

    全部 dry（不触网/不碰 GPU）；with_server/strict 不改变本段检查集。
    run() 约定：AssertionError → FAIL；返回字符串 → PASS（带 note）。
    """
    from pipeline import selftest_keyframes as _st
    titles = {
        "test_photo_t2i_graph": "photo t2i 图（img_wf 参数化，禁 ref 分支）",
        "test_photo_edit_graph": "photo edit 图（edit_wf，latent slot 2）",
        "test_anime_graphs": "anime t2i/img2img 图（SDXL，denoise 设计值）",
        "test_dispatch_entry": "workflows 统一分发 + 参数校验",
        "test_prompt_composition": "提示词次序/E() 模板/NEGEDIT 尾/双通道措辞",
        "test_seed_rules": "seed 派生（+10×镜序、+5/+7、显式优先）",
        "test_manifest_roundtrip_and_done": "manifest 原子写/完成判定三条件",
        "test_staleness": "失效判定三路（inputs/upstream/段配置哈希）",
        "test_gateway_wait_for": "wait_for 轮询（success/error/timeout）",
        "test_render_retry": "渲染重试（1+1 次、产物大小门）",
        "test_edit_staging_and_input_dirs": "edit 前置：双 input 目录先建后拷",
        "test_orphan_convergence": "孤儿收敛三分支（收割/等待/置 pending）",
        "test_orphan_harvest_no_requeue": "孤儿收割补指纹不重提（§4.0.2 回归）",
        "test_schema_cfg_duration_fallback": "schema 带 cfg（duration_s 兜底）",
        "test_assets_gate": "assets pick 门（three_view:false 跳过）",
        "test_plan_units": "unit 计划（通道/模式/锚点/报错不阻塞）",
        "test_classify_and_only": "skip/run 决策（done/stale/adopt/--only/连坐）",
        "test_run_stage_dry_run": "run_stage 干跑端到端（schema 全链）",
        "test_run_stage_missing_yaml_parser": "yaml 解析器缺失可读报错",
    }
    checks: List[Dict[str, Any]] = []
    for i, fn in enumerate(_st._RESULTS, 1):
        checks.append({
            "id": "k%02d" % i,
            "title": titles.get(fn.__name__, fn.__name__),
            "level": "fatal",
            "run": (lambda f: (lambda: (f(), "dry 通过")[1]))(fn),
        })
    return checks


def main(argv=None) -> int:
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(
        prog="python -m pipeline.stages.keyframes",
        description="keyframes 段：按 shots.yaml 逐镜渲染首/末帧（docs/FRAMEWORK_DESIGN.md §4.2）")
    ap.add_argument("project", help="项目目录或项目名（projects/ 下）")
    ap.add_argument("--only", default="", help="只处理指定 unit，如 S1,S3 或 S1:first")
    ap.add_argument("--force", action="store_true", help="完成也重跑")
    ap.add_argument("--dry-run", action="store_true", help="只打印决策，不渲染不触网")
    ap.add_argument("--keep-stale", action="store_true", help="stale 也不重跑（默认 stale 即重跑）")
    ap.add_argument("--abandon-running", action="store_true",
                    help="把遗留 running unit 强制置回 pending 重提（确认服务器侧已无该 job 后用）")
    args = ap.parse_args(argv)
    opts = _coerce_args(args)
    try:
        return run_stage(args.project, config=None, args=opts)
    except Exception:  # noqa: BLE001 —— 顶层兜底：完整栈写日志语义（对齐 render_keyframes.py:147-152）
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
