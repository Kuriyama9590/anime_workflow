# -*- coding: utf-8 -*-
"""video 段执行器 —— 按 shots.yaml 逐镜提交本地 MiniMax H3 首尾帧图生视频（fl2v）。

泛化自 _tools/render_videos.py（B 级证据：H3 全链零运行记录，设计文档 §0/§4.4，
结构照抄、参数全部待首跑标定）。设计文档 docs/FRAMEWORK_DESIGN.md §4.4。

要点（设计文档条目化）：
  - 时长三步命中 §4.4：由 pipeline/timing.py 权威实现（请求域 [5.0,15.0]；
    k=round((d×fps−b)/a) 夹取 [7,21]，length=17k+5 → 124..362 帧 @24fps，
    即"帧数≈时长×24"，可达 5.167–15.083s；半步容差 0.354s，超限列最近网格点）。
  - seed 派生 §3 规则 4：由 pipeline/seeds.py 权威实现（seed_base+2_000_000+镜序；
    yaml 显式 seed>0 优先）。
  - 断点续跑 §5：manifest（pipeline/manifest.py）done ∧ 文件存在 ∧ ≥min_bytes(200KB)
    → 跳过；inputs_hash / upstream 指纹 / 段配置哈希任一变化 → stale 默认重渲。
  - 失败重试：总尝试 run.attempts（默认 2 = 1 次 + 1 次重试，不是重试 2 次）。
  - 日志 §5：logs/<项目>_video.log 追加，含每次提交的完整提示词与节点参数。
  - 权重门 §4.4：unet/te ≥5GB、所选 LoRA ≥1.5GB（.truncated.bak 残件按大小门排除），
    立即失败并提示；--wait-weights 才轮询。
  - 服务器预检 §4.0：GET /system_stats 不可达 → 一句提示 + exit 2（区别于渲染失败 1）。
  - 孤儿收敛 §4.0：running 单元查 history（收割，不重提）/ queue（等待上限=段超时，
    绝不重提——双 H3 job 同跑必 OOM）/ 均查无 → pending 重跑；--abandon-running 强制置 pending。
  - 串行约束 §4.3/§10.7：每次提交前确认 /queue 为空（一次一个 ComfyUI job）。
  - 输入图复制进两个 input_dirs 且先 makedirs（render_keyframes.py:136-139 教训）。
  - 产物：projects/<名>/video/S#.mp4 + video_manifest.json（manifest.STAGE_DIRS 权威布局；
    camera 模式A full_video 的镜跳过 H3，其 camera/S#.mp4 由 final 段直收）。

共享模块（核心与脚手架，已落地）：pipeline.config（合并/校验/路径锚定）、
pipeline.miniyaml、pipeline.comfy（ComfyClient）、pipeline.manifest、
pipeline.seeds、pipeline.timing。本段不复制这些实现，直接复用——与本仓库
keyframes 段的 run_stage(project_dir, config, args) -> int 约定一致。

用法（cwd=仓库根，包发现依赖 cwd/PYTHONPATH，设计 §2.1 边界说明）：
  python -m pipeline.stages.video <项目> --dry-run
  python -m pipeline.stages.video <项目> [--only S1,S3] [--force]
退出码：0=全部成功/跳过；1=存在失败镜或输入错误；2=ComfyUI 不可达（§4.0）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from pipeline import config as pconfig
from pipeline import manifest as pmanifest
from pipeline import miniyaml
from pipeline import seeds as pseeds
from pipeline import timing as ptiming
from pipeline.comfy import ComfyClient, ComfyError, build_client
from pipeline.workflows.h3_fl2v import build_h3_fl2v

STAGE = "video"
NEEDS_COMFY = True  # §4.0：服务器预检仅对 needs_comfy 段执行

#: 仓库根 = pipeline 包父目录（设计 §2.1 paths 注：与 cwd 无关）
REPO_ROOT = Path(__file__).resolve().parents[2]

SHOT_ID_RE = re.compile(r"^S\d+$")

#: 提示词尾行（设计 §4.4 固定文案）
PROMPT_TAIL = "No text, no subtitles, no watermark. Keep the character design consistent."

#: 首末帧查找目录（项目内相对；设计 §1 布局优先，output/keyframes 为 keyframes 段
#: 任务书旧版布局的兼容回退）
KEYFRAME_DIRS = ("keyframes", "output/keyframes")
CAMERA_DIR = "camera"


class ConfigError(RuntimeError):
    """本段输入/配置错误（shots.yaml、网格参数、--set 键等）。"""


class WeightsNotReady(RuntimeError):
    """权重未就绪（大小门未过，路径+大小在消息里列全）。"""


# ---------------------------------------------------------------------------
# 配置装配：核心 pipeline.config 原语组合（全局 < 项目 overrides < CLI --set）
# ---------------------------------------------------------------------------

def load_stage_config(project_dir, sets=None, config_path=None):
    """→ (merged_cfg, project_meta)。语义同 config.load_merged，但接受任意项目路径。

    返回的 merged_cfg 顶层仍是全局配置结构（comfy/run/video/…）。
    """
    cfg = pconfig.load_global(config_path)
    pj = Path(project_dir)
    meta_doc = pconfig.load_project_file(pj)  # project.yaml 缺失 → ConfigError（可读提示）
    project_meta = {k: v for k, v in meta_doc.items() if k != "overrides"}
    ov = meta_doc.get("overrides")
    if ov is not None:
        if not isinstance(ov, dict):
            raise ConfigError("project.yaml 的 overrides 必须是映射")
        cfg = pconfig.deep_merge(cfg, ov)
    for item in sets or []:
        cfg = pconfig.deep_merge(cfg, pconfig.parse_set(item))
    return cfg, project_meta


def load_shots(project_dir) -> tuple:
    """读项目 shots.yaml → (doc, 按 S# 数排序的镜列表)。结构见设计 §6 模板。"""
    p = Path(project_dir) / "shots.yaml"
    if not p.is_file():
        raise ConfigError("未找到 %s（video 段必需，设计 §6；先 `python -m pipeline new <名>`）" % p)
    try:
        doc = miniyaml.load(p)
    except miniyaml.MiniYAMLError as e:
        raise ConfigError("shots.yaml 解析失败：%s" % e) from e
    if not isinstance(doc, dict):
        raise ConfigError("shots.yaml 顶层必须是映射: %s" % p)
    shots = doc.get("shots")
    if not isinstance(shots, list) or not shots:
        raise ConfigError("%s 缺少非空 shots 列表" % p)
    seen = set()
    for s in shots:
        if not isinstance(s, dict) or not SHOT_ID_RE.match(str(s.get("id", ""))):
            raise ConfigError("shots 内每镜须为含 id（形如 S1）的映射；问题行：%s"
                              % json.dumps(s, ensure_ascii=False)[:200])
        if s["id"] in seen:
            raise ConfigError("镜号重复：%s" % s["id"])
        seen.add(s["id"])
    return doc, sorted(shots, key=lambda s: int(s["id"][1:]))


def shot_field(shot: dict, doc: dict, key: str, default=None):
    """取值优先级：单镜字段 > shots.defaults: 节 > default（设计 §4.0）。"""
    v = shot.get(key)
    if v is not None:
        return v
    defaults = doc.get("defaults")
    if isinstance(defaults, dict):
        v = defaults.get(key)
        if v is not None:
            return v
    return default


def resolve_project_path(raw: str) -> Path:
    """项目目录解析：绝对路径；相对则依次试 仓库根/、仓库根/projects/、cwd/。"""
    r = raw.replace("\\", "/")
    cands = ([Path(os.path.normpath(r))] if os.path.isabs(r) else
             [REPO_ROOT / r, REPO_ROOT / "projects" / r,
              Path(os.path.normpath(os.path.join(os.getcwd(), r)))])
    for c in cands:
        if c.is_dir():
            return c
    raise ConfigError("项目目录不存在，尝试过：%s" % "；".join(str(c) for c in cands))


# ---------------------------------------------------------------------------
# 权重门（§4.4：立即失败并提示；--wait-weights 才轮询）
# ---------------------------------------------------------------------------

def _models_path(models_dir, rel: str) -> Path:
    p = Path(str(rel))
    return p if p.is_absolute() else Path(models_dir) / p


def weight_report(cfg: dict) -> dict:
    """六件套在盘情况（只读）。workflow 节点入参取 basename，盘上检查取全路径。"""
    r = pconfig.resolved(cfg)
    v = cfg["video"]
    items = {}
    for key in ("unet", "text_encoder", "lora_4step", "lora_8step", "vae_video", "vae_audio"):
        p = _models_path(r.models_dir, v[key])
        size = p.stat().st_size if p.is_file() else None
        items[key] = {"rel": v[key], "path": str(p), "exists": size is not None,
                      "bytes": size, "basename": p.name}
    return items


def check_weights(cfg: dict) -> list:
    """unet/te ≥ weight_min_bytes；双 VAE 在盘。返回问题清单（空=就绪）。"""
    rep = weight_report(cfg)
    v = cfg["video"]
    issues = []
    for key in ("unet", "text_encoder"):
        it = rep[key]
        if not it["exists"]:
            issues.append("%s 不存在：%s" % (key, it["path"]))
        elif it["bytes"] < int(v["weight_min_bytes"]):
            issues.append("%s 过小：%.3fGB < 下限 %.3fGB（%s）"
                          % (key, it["bytes"] / 1e9, int(v["weight_min_bytes"]) / 1e9, it["path"]))
    for key in ("vae_video", "vae_audio"):
        if not rep[key]["exists"]:
            issues.append("%s 不存在：%s" % (key, rep[key]["path"]))
    return issues


def pick_lora(cfg: dict) -> dict:
    """双 LoRA 按大小择优且步数随档位（render_videos.py:32-35 泛化）。

    4-step ≥ lora_min_bytes → (4step, steps=4)；否则 8-step 达标 → (8step, steps=8)；
    都不达标 → WeightsNotReady（列两个路径+大小；防 .truncated.bak 残件被误用）。
    """
    r = pconfig.resolved(cfg)
    v = cfg["video"]
    lmin = int(v["lora_min_bytes"])
    cands = []
    for key, steps in (("lora_4step", 4), ("lora_8step", 8)):
        p = _models_path(r.models_dir, v[key])
        size = p.stat().st_size if p.is_file() else -1
        cands.append({"name": p.name, "path": str(p), "bytes": size,
                      "steps": steps, "ok": size >= lmin})
    chosen = next((c for c in cands if c["ok"]), None)
    if chosen is None:
        detail = "；".join("%s=%dB（需≥%dB）" % (c["path"], c["bytes"], lmin) for c in cands)
        raise WeightsNotReady("无达标 turbo LoRA：" + detail)
    if v.get("steps_follow_lora", True):
        steps, src = chosen["steps"], "lora"
    else:
        steps, src = int(v.get("steps") or 0), "config"
        if steps <= 0:
            raise ConfigError("video.steps_follow_lora=false 时必须显式配置 video.steps")
    return {"lora": chosen["name"], "lora_path": chosen["path"], "steps": steps,
            "steps_source": src}


# ---------------------------------------------------------------------------
# 提示词组装 / 首末帧定位（§4.4 输入 = keyframes 或 camera endpoints）
# ---------------------------------------------------------------------------

def _camdesc_text(project_dir, sid: str) -> str | None:
    """读 camera/S#_camdesc.txt（camera 段模式B 产物，§4.3）；无则 None。"""
    p = Path(project_dir) / CAMERA_DIR / ("%s_camdesc.txt" % sid)
    if p.is_file():
        text = p.read_text(encoding="utf-8").strip()
        return text or None
    return None


def load_project_meta(project_dir) -> dict:
    """project.yaml 元数据（name/style/seed_base/style_anchors；不含 overrides）。"""
    doc = pconfig.load_project_file(project_dir)
    return {k: v for k, v in doc.items() if k != "overrides"}


def build_shot_prompt(shot: dict, doc: dict, meta: dict, project_dir) -> tuple:
    """§4.4 版式：<场景段> \\n\\n Timeline:\\n<motion> \\n\\n Camera:… Audio:… 尾行。

    场景段来源（写入 manifest 供审计）：单镜 scene > first + style_anchors[style] 拼接。
    Camera 行来源：camera/S#_camdesc.txt（camera 段产物，容忍自带 Camera: 前缀）>
    单镜 camdesc > camera.line > camera 节 move/intensity/extra 原样拼（记 warn）> 省略。
    返回 (prompt, info)。
    """
    sid = shot["id"]
    notes: list = []
    style = shot.get("style") or meta.get("style") or "photo"
    anchor = str((meta.get("style_anchors") or {}).get(style) or "").strip()
    scene = str(shot.get("scene") or "").strip()
    if scene:
        scene_source = "scene"
    else:
        first_desc = str(shot.get("first") or "").strip()
        if not first_desc:
            raise ConfigError("镜 %s 既无 scene 也无 first，无法组装 H3 提示词首段" % sid)
        scene = "%s %s" % (first_desc, anchor) if anchor else first_desc
        scene_source = "fallback:first+style_anchor"
        notes.append("场景段回退：%s（style_anchors[%s]%s）"
                     % (scene_source, style, " 拼接" if anchor else " 未配置，仅 first"))
    motion = str(shot_field(shot, doc, "motion") or "").strip()
    if not motion:
        raise ConfigError("镜 %s 缺 motion（H3 Timeline 段必填，shots.yaml 模板 §6）" % sid)

    def as_cam_line(text: str) -> str:
        text = text.strip()
        return text if text.lower().startswith("camera:") else "Camera: " + text

    cam_line, cam_source = None, None
    camdesc = _camdesc_text(project_dir, sid)
    if camdesc:
        cam_line, cam_source = as_cam_line(camdesc), "camdesc_file"
    else:
        inline = str(shot.get("camdesc") or "").strip()
        if inline:
            cam_line, cam_source = as_cam_line(inline), "shot.camdesc"
        else:
            cam_node = shot.get("camera")
            if isinstance(cam_node, dict):
                inline2 = str(cam_node.get("line") or "").strip()
                if inline2:
                    cam_line, cam_source = as_cam_line(inline2), "shot.camera.line"
                else:
                    raw = " ".join(x for x in (str(cam_node.get("move") or ""),
                                               str(cam_node.get("intensity") or ""),
                                               str(cam_node.get("extra") or "")) if x).strip()
                    if raw:
                        cam_line = "Camera: " + raw
                        cam_source = "shot.camera.raw"
                        notes.append("镜 %s 无 camdesc 文件，Camera 行由 move/intensity/extra "
                                     "原样拼成（未经 camera 段词库加工）" % sid)
    if cam_line is None:
        notes.append("镜 %s 无任何 Camera 来源，提示词省略 Camera 段" % sid)

    audio = str(shot_field(shot, doc, "audio_hint") or "").strip()
    if not audio:
        notes.append("镜 %s audio_hint 为空，提示词省略 Audio 行" % sid)

    tail_lines = [x for x in (cam_line, "Audio: " + audio if audio else None, PROMPT_TAIL) if x]
    prompt = "\n\n".join([scene, "Timeline:\n" + motion, "\n".join(tail_lines)])
    info = {"scene_source": scene_source, "camera_line_source": cam_source,
            "audio_included": bool(audio), "style": style, "notes": notes}
    return prompt, info


def derive_seed(meta: dict, shot: dict, index_1based: int) -> tuple:
    """§3 规则 4（pipeline/seeds.py 权威实现）：显式 seed>0 优先，否则派生。"""
    derived = pseeds.video_seed(int(meta.get("seed_base") or 0), index_1based)
    seed = pseeds.resolve_seed(shot.get("seed"), derived)
    return seed, seed == derived


def resolve_frames(project_dir, sid: str) -> tuple:
    """首/末帧定位（§4.4）：camera endpoints（模式A）优先，其次 keyframes 目录表。

    返回 (first|None, last|None)。设计 §1 布局优先，keyframes 段任务书旧版
    output/keyframes 布局作兼容回退。
    """
    pj = Path(project_dir)

    def find(cands):
        for c in cands:
            if c.is_file():
                return str(c)
        return None

    first = find([pj / CAMERA_DIR / ("%s_camfirst.png" % sid)]
                 + [pj / d / ("%s_first.png" % sid) for d in KEYFRAME_DIRS])
    last = find([pj / CAMERA_DIR / ("%s_camlast.png" % sid)]
                + [pj / d / ("%s_last.png" % sid) for d in KEYFRAME_DIRS])
    return first, last


# ---------------------------------------------------------------------------
# 日志（§5：logs/<项目>_video.log 追加；dry-run 只打印不落盘）
# ---------------------------------------------------------------------------

class StageLog:
    def __init__(self, cfg: dict, project_name: str, enabled: bool = True):
        self.path = Path(pconfig.resolved(cfg).logs_dir) / ("%s_video.log" % project_name)
        self.enabled = enabled

    def __call__(self, msg: str) -> None:
        line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        print(line, flush=True)
        if self.enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8", newline="\n") as f:
                f.write(line + "\n")


# ---------------------------------------------------------------------------
# 计划（plan）—— 纯盘上/纯计算，dry-run 与真实运行共用
# ---------------------------------------------------------------------------

def plan_stage(project_dir, cfg: dict, doc: dict, shots: list, meta: dict, *,
               only=None, force=False, keep_stale=False):
    """逐镜决策：render / skip(done|stale+keep_stale|camera_full_video) / blocked。

    只读不写（dry-run 安全）。真实运行先 converge_orphans 再 plan（running 收敛后
    状态才准确）。manifest schema = pipeline/manifest.py（§5）。
    """
    pj = Path(project_dir)
    v = cfg["video"]
    stage_dir = pmanifest.stage_dir_for(pj, STAGE)
    stage_cfg_hash = pconfig.stage_sections_hash(cfg, STAGE)  # §5：只看 video 节
    mpath = pmanifest.manifest_path_for(pj, STAGE)
    manifest = pmanifest.ensure(mpath, STAGE, meta.get("name") or pj.name, stage_cfg_hash)

    weight_issues = check_weights(cfg)
    lora_info, lora_err = None, None
    try:
        lora_info = pick_lora(cfg)
    except WeightsNotReady as e:
        lora_err = str(e)

    def default_duration():
        d = v.get("default_duration_s")
        return float(d) if d is not None else None

    notes: list = []
    actions = []
    for real_index, shot in enumerate(shots, 1):
        sid = shot["id"]
        if only and sid not in only:
            continue
        act = {"sid": sid, "action": "render", "reason": ""}
        cam_node = shot.get("camera") if isinstance(shot.get("camera"), dict) else {}
        cam_mp4 = pj / CAMERA_DIR / ("%s.mp4" % sid)
        if cam_node.get("output") == "full_video" or cam_mp4.is_file():
            act.update(action="skip",
                       reason="camera 模式A full_video（%s；video 段跳过 H3，final 段直收）"
                              % ("camera/%s.mp4 在盘" % sid if cam_mp4.is_file()
                                 else "shots.yaml camera.output=full_video"))
            actions.append(act)
            continue
        try:
            d = shot_field(shot, doc, "duration_s", default_duration())
            if d is None:
                raise ConfigError("镜 %s 未配置 duration_s（单镜/defaults/config 均无）" % sid)
            mapping = ptiming.map_duration(float(d), v)  # 三步规则，GridError → blocked
            first, last = resolve_frames(pj, sid)
            missing = [n for n, p in (("first", first), ("last", last)) if not p]
            if missing:
                raise ConfigError("缺上游首/末帧（keyframes/camera 段产物）：%s —— 先跑上游段"
                                  % "、".join("%s_%s.png" % (sid, n) for n in missing))
            prompt, pinfo = build_shot_prompt(shot, doc, meta, pj)
            seed, seed_derived = derive_seed(meta, shot, real_index)
            upstream = {"first_png": pmanifest.fingerprint_file(first),
                        "last_png": pmanifest.fingerprint_file(last)}
            camdesc_p = pj / CAMERA_DIR / ("%s_camdesc.txt" % sid)
            if camdesc_p.is_file():
                upstream["camdesc"] = pmanifest.hash_file_content(camdesc_p)
            inputs_hash = pmanifest.hash_fields(shot)
        except (ptiming.GridError, ConfigError) as e:
            act.update(action="blocked", reason=str(e))
            actions.append(act)
            continue
        act.update(mapping=mapping, duration_s=float(d), seed=seed, seed_derived=seed_derived,
                   prompt=prompt, prompt_info=pinfo, first=first, last=last,
                   upstream=upstream, inputs_hash=inputs_hash)
        mp4 = stage_dir / ("%s.mp4" % sid)
        act["mp4"] = str(mp4)
        unit = manifest["units"].get(sid)
        if unit and not force:
            if unit.get("status") == "done":
                if not pmanifest.unit_complete(unit, stage_dir, int(v["min_bytes"])):
                    act["reason"] = ("manifest=done 但产物缺失/过小（%s）——按未完成重渲"
                                     % ("%dB" % mp4.stat().st_size if mp4.is_file() else "不存在"))
                else:
                    stale = pmanifest.stale_reason(unit, inputs_hash=inputs_hash,
                                                   upstream=upstream,
                                                   stage_cfg_hash=stage_cfg_hash)
                    if stale:
                        if keep_stale:
                            act.update(action="skip", reason="stale 但 --keep-stale 保留：%s" % stale)
                            notes.append("镜 %s 已 stale，按 --keep-stale 保留旧产物（注意可能与其他镜不一致）" % sid)
                        else:
                            act["reason"] = "stale 重渲：%s" % stale
                    else:
                        act.update(action="skip",
                                   reason="done（%s，%dB ≥ 下限 %dB）"
                                          % (mp4.name, mp4.stat().st_size, int(v["min_bytes"])))
            elif unit.get("status") == "running":
                notes.append("镜 %s manifest=running —— 真实运行时先孤儿收敛（history 收割/queue 等待，不重提）" % sid)
        elif unit and force:
            act["reason"] = "--force 重渲"
        actions.append(act)
    return {"project": meta.get("name") or pj.name, "project_dir": str(pj),
            "stage_dir": str(stage_dir), "manifest_path": str(mpath), "manifest": manifest,
            "stage_cfg_hash": stage_cfg_hash, "weight_issues": weight_issues,
            "lora": lora_info, "lora_error": lora_err, "actions": actions, "notes": notes,
            "video_cfg": v}


def print_plan(plan: dict, dry_run: bool = False) -> None:
    print("=" * 78)
    print("%s video 段计划 —— 项目 %r（产物目录 %s/video）"
          % ("[DRY-RUN]" if dry_run else "[RUN]", plan["project"], plan["project_dir"]))
    lora = plan["lora"]
    if lora:
        print("LoRA 档位：%s → steps=%d（%s）" % (lora["lora"], lora["steps"], lora["lora_path"]))
    if plan["lora_error"]:
        print("LoRA 未就绪：%s" % plan["lora_error"])
    for issue in plan["weight_issues"]:
        print("  [权重门] %s" % issue)
    if not plan["weight_issues"] and not plan["lora_error"]:
        print("权重门：六件套就绪（unet/te ≥5GB，LoRA ≥1.5GB）")
    print("-" * 78)
    for act in plan["actions"]:
        head = "%-4s %-7s" % (act["sid"], act["action"].upper())
        if act["action"] == "render":
            m = act["mapping"]
            print("%s %gs→%d帧(k=%d, eff %.3fs, err %+.3fs) seed=%s%s"
                  % (head, act.get("duration_s", m["eff_s"]), m["length"], m["k"],
                     m["eff_s"], m["eff_s"] - act.get("duration_s", m["eff_s"]),
                     act["seed"], "" if act["seed_derived"] else "(显式)"))
            pj = plan["project_dir"]
            print("      帧源：%s + %s"
                  % (os.path.relpath(act["first"], pj).replace("\\", "/"),
                     os.path.relpath(act["last"], pj).replace("\\", "/")))
            pi = act["prompt_info"]
            print("      提示词源：%s%s | %s"
                  % (pi["scene_source"], "" if pi["audio_included"] else " (无Audio行)",
                     pi["camera_line_source"] or "无Camera行"))
            if dry_run:
                preview = act["prompt"].replace("\n", " ⏎ ")
                print("      提示词预览：%s" % (preview[:150] + ("…" if len(preview) > 150 else "")))
        else:
            print("%s %s" % (head, act["reason"]))
    if plan["notes"]:
        print("-" * 78)
        for n in plan["notes"]:
            print("  [注] %s" % n)
    n_render = sum(1 for a in plan["actions"] if a["action"] == "render")
    n_skip = sum(1 for a in plan["actions"] if a["action"] == "skip")
    n_block = sum(1 for a in plan["actions"] if a["action"] == "blocked")
    print("-" * 78)
    print("共 %d 镜：render=%d skip=%d blocked=%d%s"
          % (len(plan["actions"]), n_render, n_skip, n_block,
             "（blocked=缺上游/配置错，上行 reason 有明细）" if n_block else ""))
    print("=" * 78)


# ---------------------------------------------------------------------------
# 孤儿收敛（§4.0：history 收割 / queue 等待（不重提）/ 均无 → pending）
# ---------------------------------------------------------------------------

def converge_orphans(project_dir, cfg: dict, manifest: dict, client: ComfyClient, log,
                     abandon: bool = False) -> list:
    v = cfg["video"]
    stage_dir = pmanifest.stage_dir_for(project_dir, STAGE)
    mpath = pmanifest.manifest_path_for(project_dir, STAGE)
    notes, dirty = [], False
    for sid, unit in list((manifest.get("units") or {}).items()):
        if unit.get("status") != "running":
            continue
        pid = unit.get("prompt_id")
        mp4 = stage_dir / ("%s.mp4" % sid)

        def harvest(entry: dict) -> None:
            got = client.download_media(entry, str(mp4))
            if got and mp4.is_file() and mp4.stat().st_size >= int(v["min_bytes"]):
                pmanifest.mark_unit(manifest, sid, status="done",
                                    files={"mp4": mp4.name}, error=None)
                notes.append("镜 %s：孤儿 job 收割 → %s" % (sid, mp4))
            else:
                pmanifest.mark_unit(manifest, sid, status="failed",
                                    error="孤儿 history 有记录但下载失败/过小")
                notes.append("镜 %s：孤儿收割失败 → failed" % sid)

        if not pid:
            unit.update(status="pending", error="running 但无 prompt_id（历史遗留），置回 pending")
            notes.append("镜 %s：running 无 prompt_id → pending 重跑" % sid)
            dirty = True
            continue
        if abandon:
            unit.update(status="pending", error=None)
            notes.append("镜 %s：--abandon-running，running→pending 重提（操作者已确认服务器侧无此 job）" % sid)
            dirty = True
            continue
        h = client.history(pid)
        if pid in h:
            entry = h[pid]
            status = entry.get("status") or {}
            if status.get("completed") is True or status.get("status_str") == "success":
                harvest(entry)
            else:
                unit.update(status="failed",
                            error=str(status.get("status_str") or json.dumps(status, ensure_ascii=False))[:1000])
                notes.append("镜 %s：孤儿 job 执行错误 → failed" % sid)
            dirty = True
            continue
        if client.prompt_in_queue(pid):
            log("镜 %s：prompt %s 仍在 ComfyUI 队列——等待并收割，绝不重提（串行/OOM 约束）"
                % (sid, pid))
            try:
                entry = client.wait(pid, timeout_s=float(cfg["comfy"]["timeout_video_s"]),
                                    quiet_first_s=0.0)
                harvest(entry)
            except (TimeoutError, ComfyError) as e:
                unit.update(status="failed", error=str(e)[:1000])
                notes.append("镜 %s：孤儿等待超上限 → failed，需人工处置（ComfyUI 端取消该 job 或重启；"
                             "确认后 --abandon-running 重提）：%s" % (sid, e))
            dirty = True
        else:
            unit.update(status="pending", error=None)
            notes.append("镜 %s：prompt %s 在 history/queue 均查无（服务器或已重启）→ pending 重跑" % (sid, pid))
            dirty = True
    if dirty:
        pmanifest.save(mpath, manifest)
        for n in notes:
            log("孤儿收敛：" + n)
    return notes


# ---------------------------------------------------------------------------
# 渲染一镜（提交 → 轮询 → 下载 → manifest；重试 run.attempts 次）
# ---------------------------------------------------------------------------

def _put_inputs(cfg: dict, first: str, last: str, sid: str, log) -> None:
    """首/末帧复制进两个 input_dirs，先 makedirs 再复制（§4.4/关键帧翻车先例）。"""
    r = pconfig.resolved(cfg)
    names = (("%s_in.png" % sid, first), ("%s_in_last.png" % sid, last))
    for name, src in names:
        for d in r.input_dirs:
            os.makedirs(d, exist_ok=True)
            shutil.copyfile(src, os.path.join(str(d), name))
    log("镜 %s：首/末帧已复制进 %d 个 input_dirs（%s / %s）"
        % (sid, len(r.input_dirs), names[0][0], names[1][0]))


def render_one(act: dict, plan: dict, cfg: dict, manifest: dict, meta: dict,
               client: ComfyClient, log) -> bool:
    sid = act["sid"]
    v = cfg["video"]
    mp4 = Path(act["mp4"])
    mp4.parent.mkdir(parents=True, exist_ok=True)
    lora = plan["lora"]
    wf = build_h3_fl2v(
        prompt=act["prompt"], seed=act["seed"],
        first_image="%s_in.png" % sid, last_image="%s_in_last.png" % sid,
        length=act["mapping"]["length"], width=int(v["width"]), height=int(v["height"]),
        fps=float(v["fps"]), unet=v["unet"], text_encoder=v["text_encoder"],
        clip_type=v["clip_type"], lora=lora["lora"], vae_video=v["vae_video"],
        vae_audio=v["vae_audio"], sampler=v["sampler"], scheduler=v["scheduler"],
        steps=lora["steps"],
        filename_prefix="%s/video/%s" % (plan["project"], sid))
    _put_inputs(cfg, act["first"], act["last"], sid, log)
    log("镜 %s 提示词（完整，供复现）：\n%s" % (sid, act["prompt"]))
    log("镜 %s 节点参数：%s" % (sid, json.dumps(wf, ensure_ascii=False, sort_keys=True)))

    unit = pmanifest.mark_unit(manifest, sid,
                               status="running", files={"mp4": mp4.name}, seed=act["seed"],
                               seed_derived=act["seed_derived"], length=act["mapping"]["length"],
                               duration_s=act.get("duration_s"), eff_s=act["mapping"]["eff_s"],
                               width=int(v["width"]), height=int(v["height"]), fps=int(v["fps"]),
                               lora=lora["lora"], steps=lora["steps"], prompt=act["prompt"],
                               prompt_source=act["prompt_info"], inputs_hash=act["inputs_hash"],
                               upstream=act["upstream"], stage_cfg_hash=plan["stage_cfg_hash"],
                               error=None)
    attempts = int(cfg["run"]["attempts"])
    timeout_s = float(cfg["comfy"]["timeout_video_s"])
    ok = False
    for attempt in range(1, attempts + 1):
        try:
            _wait_server_idle(client, timeout_s, float(cfg["comfy"]["poll_interval_s"]), log)
            log("镜 %s 渲染 attempt %d/%d（length=%d steps=%d seed=%s lora=%s）..."
                % (sid, attempt, attempts, act["mapping"]["length"], lora["steps"],
                   act["seed"], lora["lora"]))
            t0 = time.time()
            pid = client.queue_prompt(wf)
            unit.update(prompt_id=pid, attempts=attempt)
            pmanifest.save(plan["manifest_path"], manifest)  # 先落 pid 再等（孤儿收敛依据）
            entry = client.wait(pid, timeout_s=timeout_s,
                                quiet_first_s=float(cfg["run"]["quiet_first_s"]))
            got = client.download_media(entry, str(mp4))
            if not got:
                raise RuntimeError("outputs 无视频媒体：%s"
                                   % json.dumps(entry.get("outputs", {}), ensure_ascii=False)[:300])
            size = mp4.stat().st_size
            if size < int(v["min_bytes"]):
                raise RuntimeError("成片过小：%dB < 下限 %dB（%s）" % (size, v["min_bytes"], mp4))
            elapsed_min = (time.time() - t0) / 60.0
            unit.update(status="done", attempts=attempt, error=None,
                        render_elapsed_s=round(time.time() - t0, 1),
                        render_minutes=round(elapsed_min, 2))
            pmanifest.save(plan["manifest_path"], manifest)
            log("镜 %s OK：%.1f 分钟 → %s（%dB）" % (sid, elapsed_min, mp4, size))
            ok = True
            break
        except Exception as e:  # noqa: BLE001 —— 单镜失败不阻塞其它镜
            if attempt >= attempts:
                unit.update(status="failed", attempts=attempt, error=str(e)[:1000])
            else:
                unit.update(attempts=attempt, error=str(e)[:1000])  # 中间失败保持 running（还要重试）
            pmanifest.save(plan["manifest_path"], manifest)
            log("镜 %s attempt %d 失败：%s" % (sid, attempt, e))
            if attempt < attempts:
                time.sleep(float(cfg["run"]["retry_wait_s"]))
    if not ok:
        unit.update(status="failed")
        pmanifest.save(plan["manifest_path"], manifest)
    return ok


def _wait_server_idle(client: ComfyClient, timeout_s: float, poll_s: float, log) -> None:
    """串行约束（§4.3/§10.7）：提交任何新 job 前确认 /queue 为空。

    也兜住"提交后、写 manifest 前崩溃丢失 prompt_id"的窗口——服务器仍在跑旧 job
    时绝不叠加新 H3 job（双 20.8G unet 同跑必 OOM 双杀）。
    """
    t0 = time.time()
    while True:
        snap = client.queue_snapshot()
        busy = [it for key in ("queue_running", "queue_pending") for it in (snap.get(key) or [])]
        if not busy:
            return
        if time.time() - t0 >= timeout_s:
            pids = sorted({str(it[1]) if isinstance(it, (list, tuple)) and len(it) > 1
                           else str(it) for it in busy})
            raise RuntimeError("ComfyUI 队列非空且等待超时（%.0fs），串行约束拒绝叠加提交。"
                               "在队列中的 prompt_id：%s —— 人工处置（ComfyUI 端取消或重启）后重跑"
                               % (timeout_s, ", ".join(pids)))
        log("等待 ComfyUI 队列排空（串行约束）：%d 个 job 在队列" % len(busy))
        time.sleep(max(0.2, poll_s))


# ---------------------------------------------------------------------------
# 段入口（pipeline.cli 与 python -m pipeline.stages.video 共用）
# ---------------------------------------------------------------------------

def run_stage(project_dir, config=None, args=None) -> int:
    """执行 video 段（stages 包约定签名）。

    :param project_dir: 项目目录（projects/<名>）
    :param config: 合并后配置（pipeline.config 结构）；None → 本段自行装配
                   （全局 config.yaml + 项目 overrides + args.sets）
    :param args: SimpleNamespace（only/force/dry_run/keep_stale/wait_weights/
                 abandon_running/sets/config_path…），None → 全缺省
    退出码：0 全成；1 渲染失败/输入错误；2 服务器预检不可达（§4.0）。
    """
    a = args or SimpleNamespace()
    only = getattr(a, "only", None)
    force = bool(getattr(a, "force", False))
    dry_run = bool(getattr(a, "dry_run", False))
    keep_stale = bool(getattr(a, "keep_stale", False))
    wait_weights = bool(getattr(a, "wait_weights", False))
    abandon = bool(getattr(a, "abandon_running", False))

    if config is None:
        cfg, meta = load_stage_config(project_dir, sets=getattr(a, "sets", None),
                                      config_path=getattr(a, "config_path", None))
    else:
        cfg, meta = config, load_project_meta(project_dir)
    if only is not None and not isinstance(only, (set, frozenset, list, tuple)):
        only = {x.strip() for x in str(only).split(",") if x.strip()}

    doc, shots = load_shots(project_dir)
    if only:
        known = {s["id"] for s in shots}
        unknown = set(only) - known
        if unknown:
            raise ConfigError("--only 引用不存在的镜：%s（现有：%s）"
                              % (", ".join(sorted(unknown)), ", ".join(sorted(known))))

    log = StageLog(cfg, meta.get("name") or Path(project_dir).name, enabled=not dry_run)

    # dry-run：只算计划 + 打印，不触网不写盘
    plan = plan_stage(project_dir, cfg, doc, shots, meta,
                      only=only, force=force, keep_stale=keep_stale)
    print_plan(plan, dry_run=dry_run)
    if dry_run:
        return 0

    # 权重门：立即失败并提示（--wait-weights 才轮询，§4.4）
    while plan["weight_issues"] or plan["lora_error"]:
        if not wait_weights:
            for issue in plan["weight_issues"]:
                log("权重未就绪：%s" % issue)
            if plan["lora_error"]:
                log(plan["lora_error"])
            log("权重门未过（立即失败，不加 --wait-weights 不死等）。修复后重跑。")
            return 1
        log("权重未就绪，--wait-weights 轮询中（每 30s）...")
        time.sleep(30)
        plan = plan_stage(project_dir, cfg, doc, shots, meta,
                          only=only, force=force, keep_stale=keep_stale)

    # 服务器可达预检（§4.0：不可达 → exit 2，不让 unit 空转被误标 failed）
    client = build_client(cfg)
    if cfg.get("run", {}).get("server_precheck", True):
        try:
            stats = client.system_stats()
            log("ComfyUI 可达：%s（ComfyUI ver %s）"
                % (client.server, (stats.get("system") or {}).get("comfyui_version", "?")))
        except Exception as e:
            msg = ("ComfyUI 未启动（当前 8188 停机即此情形）：请先运行 run_nvidia_gpu.bat。（%s）" % e)
            print(msg, flush=True)
            log(msg)
            return 2

    # 孤儿收敛（§4.0）→ 重新计划（running 收敛后的 done/pending 才准确）
    converge_orphans(project_dir, cfg, plan["manifest"], client, log, abandon=abandon)
    plan = plan_stage(project_dir, cfg, doc, shots, meta,
                      only=only, force=force, keep_stale=keep_stale)
    print_plan(plan)
    Path(plan["stage_dir"]).mkdir(parents=True, exist_ok=True)

    failures, skips = [], 0
    for act in plan["actions"]:
        if act["action"] == "render":
            if not render_one(act, plan, cfg, plan["manifest"], meta, client, log):
                failures.append(act["sid"])
        elif act["action"] == "skip":
            skips += 1
            log("镜 %s 跳过：%s" % (act["sid"], act["reason"]))
        else:  # blocked
            failures.append(act["sid"])
            log("镜 %s 无法渲染（blocked）：%s" % (act["sid"], act["reason"]))
    log("VIDEO 段结束：render %d 镜，跳过 %d 镜，失败/blocked %d 镜%s"
        % (len(plan["actions"]) - len(failures) - skips, skips, len(failures),
           ("——失败清单：%s" % ", ".join(failures)) if failures else ""))
    return 1 if failures else 0


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    ap = argparse.ArgumentParser(
        prog="python -m pipeline.stages.video",
        description="video 段：按 shots.yaml 逐镜提交本地 MiniMax H3 fl2v（首尾帧图生视频）",
        epilog="示例：python -m pipeline.stages.video projects/我的剧 --dry-run")
    ap.add_argument("project", nargs="?", default=None,
                    help="项目目录/名（绝对路径，或 仓库根[projects/]下相对名）")
    ap.add_argument("--project", dest="project_opt", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--only", default=None, help="只跑指定镜，如 S1,S3")
    ap.add_argument("--force", action="store_true", help="忽略完成判定强制重渲")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不触网不写盘")
    ap.add_argument("--set", dest="sets", action="append", default=[], metavar="a.b=v",
                    help="配置覆盖（最高优先级），如 --set video.default_duration_s=6")
    ap.add_argument("--wait-weights", action="store_true", help="权重未就绪时轮询等待（默认立即失败）")
    ap.add_argument("--keep-stale", action="store_true", help="stale 单元保留旧产物不重渲（默认 stale 即重跑）")
    ap.add_argument("--abandon-running", action="store_true",
                    help="把 manifest 中 running 单元强制置 pending 重提（须先确认服务器侧无此 job）")
    ap.add_argument("--selftest", action="store_true", help="运行本模块自检（dry），等价 selftest.py 的 video 段")
    args = ap.parse_args(argv)

    if args.selftest:
        return run_selftest()

    raw = args.project or args.project_opt
    if not raw:
        ap.error("需要项目参数：python -m pipeline.stages.video <项目> […]")
    try:
        project_dir = resolve_project_path(raw)
        ns = SimpleNamespace(only=args.only, force=args.force, dry_run=args.dry_run,
                             keep_stale=args.keep_stale, wait_weights=args.wait_weights,
                             abandon_running=args.abandon_running, sets=args.sets,
                             config_path=None)
        return run_stage(project_dir, config=None, args=ns)
    except (ConfigError, pconfig.ConfigError, miniyaml.MiniYAMLError) as e:
        print("配置/输入错误：%s" % e, flush=True)
        return 1
    except KeyboardInterrupt:
        print("中断（manifest 已记录进行中状态，重跑时孤儿收敛接管）", flush=True)
        return 130


# ---------------------------------------------------------------------------
# selftest 检查注册表（设计 §7；根 selftest.py 与 --selftest 聚合执行，全部 dry）
# ---------------------------------------------------------------------------

def run_selftest(with_server: bool = False) -> int:
    """独立跑本模块检查（根 selftest.py 亦调用 selftest_checks 聚合）。"""
    results = []
    for c in selftest_checks(with_server=with_server):
        try:
            detail = c["run"]()
            results.append((c, True, detail))
            print("  PASS  %s %s | %s" % (c["id"], c["title"], detail), flush=True)
        except Exception as e:  # noqa: BLE001
            results.append((c, False, str(e)))
            print("  FAIL  %s %s | %s" % (c["id"], c["title"], e), flush=True)
    n_fail = sum(1 for _, ok, _ in results if not ok)
    print("video 段自检：%d 项，FAIL=%d" % (len(results), n_fail), flush=True)
    return 1 if n_fail else 0


def selftest_checks(with_server: bool = False, strict: bool = False) -> list:
    """返回 [{id,title,level,run}]；run() 成功返回明细字符串，失败 raise。

    覆盖设计 §7 与本模块相关项：#7（h3_fl2v 构造器合法性）、#8（帧长网格全扫，
    经 pipeline.timing 权威实现）、模块特有：LoRA 择优与权重门、manifest 完成判定/
    stale（经 pipeline.manifest）、提示词组装、seed 派生（pipeline.seeds）、断点续跑
    计划、CLI dry-run、H3 权重在盘（真实盘只读）、（--with-server）H3 节点清单。
    """
    import subprocess
    import tempfile

    checks = []

    def check(cid, title, level="fatal"):
        def deco(fn):
            checks.append({"id": cid, "title": title, "level": level, "run": fn})
            return fn
        return deco

    def _real_video_cfg() -> dict:
        return pconfig.load_global()["video"]

    def _mk_project(td: str) -> Path:
        """临时项目（miniyaml 支持集内的 yaml）：3 镜 + keyframes 产物 + done 的 S1。"""
        pj = Path(td) / "proj"
        (pj / "keyframes").mkdir(parents=True, exist_ok=True)
        (pj / "camera").mkdir(parents=True, exist_ok=True)
        (pj / "video").mkdir(parents=True, exist_ok=True)
        (pj / "project.yaml").write_text(
            "name: 测试剧\nstyle: photo\nseed_base: 20261000\n"
            "style_anchors:\n  photo: cinematic anime film still\n", encoding="utf-8")
        (pj / "shots.yaml").write_text(
            "project: 测试剧\n"
            "defaults: {duration_s: 5.17}\n"
            "shots:\n"
            "  - id: S1\n"
            "    scene: Bus stop night rain.\n"
            "    motion: |\n"
            "      [0s-2s] She sits alone.\n"
            "    audio_hint: steady rain\n"
            "  - id: S2\n"
            "    first: Alley neon.\n"
            "    motion: |\n"
            "      [0s-2s] She walks.\n"
            "    duration_s: 10.0\n"
            "    camera: {move: push_in, intensity: slow}\n"
            "  - id: S3\n"
            "    scene: Full video shot.\n"
            "    motion: |\n"
            "      [0s-2s] x\n"
            "    camera: {mode: A, output: full_video}\n",
            encoding="utf-8")
        for sid in ("S1", "S2"):
            for kind in ("first", "last"):
                (pj / "keyframes" / ("%s_%s.png" % (sid, kind))).write_bytes(b"\x89PNG" + b"\0" * 64)
        (pj / "camera" / "S1_camdesc.txt").write_text(
            "Camera: very slow push-in, breathing hand-held stillness", encoding="utf-8")
        mp4 = pj / "video" / "S1.mp4"
        mp4.write_bytes(b"\0" * 300_000)
        # S1 的 manifest 记录与当前状态一致的指纹（v8 首次 plan 应判"干净跳过"，
        # 之后 utime 上游文件才会触发 stale）
        _, shots = load_shots(pj)
        m = pmanifest.ensure(pmanifest.manifest_path_for(pj, STAGE), STAGE, "测试剧",
                             pconfig.stage_sections_hash(pconfig.load_global(), STAGE))
        m["units"]["S1"] = {
            "status": "done", "files": {"mp4": "S1.mp4"},
            "inputs_hash": pmanifest.hash_fields(shots[0]),
            "upstream": {"first_png": pmanifest.fingerprint_file(pj / "keyframes" / "S1_first.png"),
                         "last_png": pmanifest.fingerprint_file(pj / "keyframes" / "S1_last.png"),
                         "camdesc": pmanifest.hash_file_content(pj / "camera" / "S1_camdesc.txt")},
            "attempts": 1, "error": None, "prompt_id": None, "seed": 22261001,
            "stage_cfg_hash": pconfig.stage_sections_hash(pconfig.load_global(), STAGE)}
        pmanifest.save(pmanifest.manifest_path_for(pj, STAGE), m)
        return pj

    # ---- #7 h3_fl2v 工作流构造器（假参数出合法 API 图）----
    @check("v1", "h3_fl2v 构造器：假参数产出合法 API 图，结构与 render_videos.py:37-58 一致")
    def _v1():
        from pipeline.workflows.h3_fl2v import validate_api_workflow
        wf = build_h3_fl2v(
            prompt="test prompt", seed=12345, first_image="S1_in.png",
            last_image="S1_in_last.png", length=97, steps=8,
            unet="diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors",
            text_encoder="text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
            lora="loras/whatever_lora.safetensors",
            vae_video="vae/a.safetensors", vae_audio="vae/b.safetensors",
            filename_prefix="测试/video/S1")
        validate_api_workflow(wf)
        assert len(wf) == 17, "应 17 节点，got %d" % len(wf)
        expect = ["UNETLoader", "LoraLoaderModelOnly", "CLIPLoader", "VAELoader",
                  "VAELoader", "LoadImage", "LoadImage", "MiniMaxH3ImageToVideo",
                  "BasicGuider", "KSamplerSelect", "BasicScheduler", "RandomNoise",
                  "SamplerCustomAdvanced", "VAEDecode", "VAEDecodeAudio",
                  "CreateVideo", "SaveVideo"]
        got = [wf[str(i)]["class_type"] for i in range(1, 18)]
        assert got == expect, "class_type 序列与参考实现不一致：%s" % got
        # 相对子路径入参自动取 basename（设计 §2.1 video 节注）
        assert wf["1"]["inputs"]["unet_name"] == "minimax_h3_fl2va_pruned_int8_convrot.safetensors"
        assert wf["3"]["inputs"]["clip_name"] == "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
        assert wf["2"]["inputs"]["lora_name"] == "whatever_lora.safetensors"
        assert wf["8"]["inputs"]["length"] == 97 and wf["8"]["inputs"]["width"] == 1344 \
            and wf["8"]["inputs"]["height"] == 768
        assert wf["11"]["inputs"]["steps"] == 8 and wf["11"]["inputs"]["scheduler"] == "simple"
        assert wf["10"]["inputs"]["sampler_name"] == "res_multistep"
        assert wf["12"]["inputs"]["noise_seed"] == 12345
        assert wf["16"]["inputs"]["fps"] == 24.0
        assert wf["17"]["inputs"]["filename_prefix"] == "测试/video/S1"
        for bad, why in (({"1": {"class_type": "X", "inputs": {"m": ["9", 0]}}}, "悬空引用"),
                         ({"nodes": [], "links": []}, "UI 格式残留")):
            try:
                validate_api_workflow(bad)
                raise AssertionError("%s未被查出" % why)
            except ValueError:
                pass
        return "17 节点 API 图合法；注入/basename/校验器均通过"

    # ---- #8 帧长网格（pipeline.timing 权威实现 + 真实 config 的 video 节）----
    @check("v2", "帧长网格：default_duration 命中 + 5.0–15.0 逐 0.5s 共 21 点全部半步命中")
    def _v2():
        v = _real_video_cfg()
        m = ptiming.map_duration(float(v["default_duration_s"]), v)
        assert (m["k"], m["length"]) == (7, 124), "5.17s 应 k=7/124帧，got %s" % m
        assert abs(m["eff_s"] - 5.17) <= m["tolerance_s"] + 1e-9
        sweep = ptiming.sweep_grid(v, step=0.5)
        assert len(sweep) == 21, "应 21 点，got %d" % len(sweep)
        for d, r in sweep:
            assert 7 <= r["k"] <= 21 and r["length"] == 17 * r["k"] + 5
            assert abs(r["eff_s"] - d) <= r["tolerance_s"] + 1e-9, "d=%.1f 命中失败：%s" % (d, r)
        d, r = sweep[-1]
        assert (d, r["k"], r["length"]) == (15.0, 21, 362), \
            "端点 15.0 应 k=21/362帧，got %s" % ((d, r),)
        return "21/21 点命中；端点 15.0→k=21→362帧=15.083s（误差 0.083s ≤ 半步 0.354s）"

    # ---- 时长域/容差报错 ----
    @check("v3", "时长三步规则的报错路径：请求域越界 + 容差超限列最近网格点")
    def _v3():
        v = _real_video_cfg()
        for bad in (4.9, 15.1):
            try:
                ptiming.map_duration(bad, v)
                raise AssertionError("%.1fs 越域未报错" % bad)
            except ptiming.GridError as e:
                assert "请求域" in str(e), str(e)
        narrow = dict(v)
        narrow["k_max"] = 7  # 可达域截到 5.167s 单点，6.0s 必超容差
        try:
            ptiming.map_duration(6.0, narrow)
            raise AssertionError("容差超限未报错")
        except ptiming.GridError as e:
            msg = str(e)
            assert "网格点" in msg and "k=7" in msg and "容差" in msg, msg
        return "越域/超容差均显式报错，消息含网格点与修复指引"

    # ---- LoRA 择优 + 权重门（临时目录，阈值调小避免真实体积）----
    @check("v4", "LoRA 双档择优（步数随档位）+ 权重大小门（.truncated.bak 防御语义）")
    def _v4():
        with tempfile.TemporaryDirectory() as td:
            md = os.path.join(td, "models")
            os.makedirs(md, exist_ok=True)

            def put(rel, size):
                p = os.path.join(md, *rel.split("/"))
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "wb") as f:
                    f.write(b"\0" * size)
                return p

            cfg = pconfig.load_global()
            cfg["comfy"]["models_dir"] = md
            v = cfg["video"]
            v["lora_min_bytes"], v["weight_min_bytes"] = 1500, 1000
            put("loras/l4.safetensors", 1000)   # 不达标（<1500）
            put("loras/l8.safetensors", 2000)   # 达标
            v["lora_4step"], v["lora_8step"] = "loras/l4.safetensors", "loras/l8.safetensors"
            put("text_encoders/te.safetensors", 2000)
            put("vae/vv.safetensors", 10)
            put("vae/va.safetensors", 10)
            v["text_encoder"], v["vae_video"], v["vae_audio"] = (
                "text_encoders/te.safetensors", "vae/vv.safetensors", "vae/va.safetensors")
            picked = pick_lora(cfg)
            assert picked["steps"] == 8 and picked["lora"] == "l8.safetensors", picked
            v["steps_follow_lora"] = False
            v["steps"] = 6
            assert pick_lora(cfg)["steps"] == 6
            v["steps_follow_lora"] = True
            put("loras/l4.safetensors", 1400)   # 双档都不达标
            put("loras/l8.safetensors", 1400)
            try:
                pick_lora(cfg)
                raise AssertionError("双 LoRA 不达标未报错")
            except WeightsNotReady as e:
                assert "l4.safetensors" in str(e) and "1400" in str(e), str(e)
            put("diffusion_models/u.safetensors", 500)
            v["unet"] = "diffusion_models/u.safetensors"
            issues = check_weights(cfg)
            assert len(issues) == 1 and "u.safetensors" in issues[0], issues
            with open(os.path.join(md, "diffusion_models", "u.safetensors"), "wb") as f:
                f.write(b"\0" * 2000)
            assert check_weights(cfg) == [], "达标后应无 issue"
        return "4step<门 回退 8step/8 步；显式 steps 生效；大小门列路径与大小"

    # ---- manifest 完成判定 / stale（pipeline.manifest 权威实现）----
    @check("v5", "manifest：完成判定三条件 + upstream/inputs_hash/段哈希三路失效")
    def _v5():
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td)
            mp4 = sd / "S1.mp4"
            mp4.write_bytes(b"\0" * 300_000)
            rec = {"status": "done", "files": {"mp4": "S1.mp4"},
                   "inputs_hash": "sha256:in1",
                   "upstream": {"first_png": "S1_first.png|100|1"},
                   "stage_cfg_hash": "sha256:cfg1"}
            assert pmanifest.unit_complete(rec, sd, 200_000)
            assert not pmanifest.unit_complete(rec, sd, 400_000), "阈值高于文件大小应判未完成"
            rec2 = dict(rec, status="failed")
            assert not pmanifest.unit_complete(rec2, sd, 200_000)
            mp4.write_bytes(b"\0" * 10)
            assert not pmanifest.unit_complete(rec, sd, 200_000), "过小不得判完成"
            mp4.write_bytes(b"\0" * 300_000)
            r = pmanifest.stale_reason(rec, inputs_hash="sha256:in2",
                                       upstream=rec["upstream"], stage_cfg_hash="sha256:cfg1")
            assert r and "inputs_hash" in r, r
            r = pmanifest.stale_reason(rec, inputs_hash="sha256:in1",
                                       upstream={"first_png": "S1_first.png|100|2",
                                                 "last_png": "S1_last.png|10|3"},
                                       stage_cfg_hash="sha256:cfg1")
            assert r and "上游" in r, r
            r = pmanifest.stale_reason(rec, inputs_hash="sha256:in1",
                                       upstream=rec["upstream"], stage_cfg_hash="sha256:cfg2")
            assert r and "配置" in r, r
            r = pmanifest.stale_reason(rec, inputs_hash="sha256:in1",
                                       upstream=rec["upstream"], stage_cfg_hash="sha256:cfg1")
            assert r is None, r
        return "三条件判定 + 三路失效 + 未变不 stale"

    # ---- 提示词组装 ----
    @check("v6", "提示词组装：scene/回退、camdesc 前缀容忍、Audio 省略、缺 motion 报错")
    def _v6():
        with tempfile.TemporaryDirectory() as td:
            pj = Path(td)
            (pj / "camera").mkdir(exist_ok=True)
            meta = {"style": "photo", "style_anchors": {"photo": "cinematic anime film still"},
                    "seed_base": 20261000}
            shot = {"id": "S1", "scene": "Rain bus stop.", "motion": "[0s-2s] She sits.",
                    "audio_hint": "steady rain"}
            prompt, info = build_shot_prompt(shot, {}, meta, pj)
            assert info["scene_source"] == "scene" and info["camera_line_source"] is None
            (pj / "camera" / "S1_camdesc.txt").write_text(
                "very slow push-in, breathing hand-held stillness", encoding="utf-8")
            prompt, info = build_shot_prompt(shot, {}, meta, pj)
            assert prompt.startswith("Rain bus stop.\n\nTimeline:\n[0s-2s] She sits.")
            assert "\nCamera: very slow push-in" in prompt and prompt.count("Camera:") == 1
            assert "\nAudio: steady rain" in prompt and prompt.endswith(PROMPT_TAIL)
            assert info["camera_line_source"] == "camdesc_file"
            (pj / "camera" / "S1_camdesc.txt").write_text("Camera: locked-off.", encoding="utf-8")
            prompt2, _ = build_shot_prompt(shot, {}, meta, pj)
            assert "\nCamera: locked-off." in prompt2 and "Camera: Camera:" not in prompt2
            shot2 = {"id": "S2", "first": "Girl at bus stop.", "motion": "m",
                     "audio_hint": "", "camera": {"move": "push_in", "intensity": "slow"}}
            p2, i2 = build_shot_prompt(shot2, {}, meta, pj)
            assert i2["scene_source"] == "fallback:first+style_anchor"
            assert p2.startswith("Girl at bus stop. cinematic anime film still")
            assert "Audio:" not in p2 and "Camera: push_in slow" in p2
            assert i2["camera_line_source"] == "shot.camera.raw"
            try:
                build_shot_prompt({"id": "S3", "scene": "x"}, {}, meta, pj)
                raise AssertionError("缺 motion 未报错")
            except ConfigError as e:
                assert "motion" in str(e)
        return "版式=§4.4；回退/前缀容忍/省略/必填报错全符合"

    # ---- seed 派生（pipeline.seeds 权威实现）----
    @check("v7", "seed 派生：seed_base+2_000_000+镜序（seeds.video_seed），显式 seed 优先")
    def _v7():
        assert pseeds.video_seed(20261000, 1) == 22261001
        assert derive_seed({"seed_base": 20261000}, {}, 2) == (22261002, True)
        assert derive_seed({"seed_base": 20261000}, {"seed": 99}, 5) == (99, False)
        assert derive_seed({"seed_base": 20261000}, {"seed": 0}, 1) == (22261001, True)
        assert derive_seed({"seed_base": 0}, {}, 1) == (2000001, True)
        return "S1=22261001/S2=22261002/显式99 直用/0=派生"

    # ---- 计划/断点续跑/full_video 跳过/stale/blocked（纯 plan 不触网）----
    @check("v8", "计划与断点续跑：done跳过/force重渲/stale默认重跑与keep-stale/full_video跳过/缺上游blocked")
    def _v8():
        with tempfile.TemporaryDirectory() as td:
            pj = _mk_project(td)
            cfg = pconfig.load_global()
            cfg = pconfig.deep_merge(cfg, {"comfy": {"models_dir": os.path.join(td, "models")}})
            meta = {"name": "测试剧", "style": "photo", "seed_base": 20261000,
                    "style_anchors": {"photo": "cinematic anime film still"}}
            doc, shots = load_shots(pj)
            plan = plan_stage(pj, cfg, doc, shots, meta)
            acts = {a["sid"]: a for a in plan["actions"]}
            assert acts["S1"]["action"] == "skip" and "done" in acts["S1"]["reason"], acts["S1"]
            assert acts["S2"]["action"] == "render" and acts["S2"]["mapping"]["length"] == 243, \
                "10.0s 应 k=14/243帧，got %s" % acts["S2"]["mapping"]
            assert acts["S2"]["prompt"].startswith("Alley neon. cinematic anime film still")
            assert acts["S2"]["prompt_info"]["camera_line_source"] == "shot.camera.raw"
            assert acts["S1"]["seed"] == 22261001 and acts["S2"]["seed"] == 22261002
            assert acts["S3"]["action"] == "skip" and "full_video" in acts["S3"]["reason"]
            plan_f = plan_stage(pj, cfg, doc, shots, meta, force=True)
            assert {a["sid"]: a for a in plan_f["actions"]}["S1"]["action"] == "render"
            # stale：S1 记录的 stage_cfg_hash/upstream 与当前不同 → 默认重渲；keep_stale → 保留
            os.utime(pj / "keyframes" / "S1_first.png", ns=(0, 10 ** 9))
            a1 = {a["sid"]: a for a in plan_stage(pj, cfg, doc, shots, meta)["actions"]}["S1"]
            assert a1["action"] == "render" and "stale" in a1["reason"], a1
            a1k = {a["sid"]: a for a in
                   plan_stage(pj, cfg, doc, shots, meta, keep_stale=True)["actions"]}["S1"]
            assert a1k["action"] == "skip" and "keep-stale" in a1k["reason"], a1k
            # 缺上游 → blocked 且不阻塞其它镜
            (pj / "keyframes" / "S2_last.png").unlink()
            ab = {a["sid"]: a for a in plan_stage(pj, cfg, doc, shots, meta)["actions"]}
            assert ab["S2"]["action"] == "blocked" and "S2_last.png" in ab["S2"]["reason"]
            assert ab["S1"]["action"] in ("skip", "render")
        return "done跳过/force全重渲/stale重跑+keep-stale保留/full_video跳过/缺上游blocked"

    @check("v9", "CLI 端到端 dry-run：python -m pipeline.stages.video <临时项目> --dry-run")
    def _v9():
        with tempfile.TemporaryDirectory() as td:
            pj = _mk_project(td)
            r = subprocess.run(
                [sys.executable, "-m", "pipeline.stages.video", str(pj), "--dry-run"],
                cwd=str(REPO_ROOT), capture_output=True, encoding="utf-8", errors="replace",
                timeout=120)
            out = (r.stdout or "") + (r.stderr or "")
            assert r.returncode == 0, "exit=%d\n%s" % (r.returncode, out[-2000:])
            assert "DRY-RUN" in out and "S1" in out and "S2" in out and "S3" in out
            assert "RENDER" in out and "SKIP" in out and "243" in out and "done" in out, \
                "应有 S2=243帧 的网格映射、S1 done 跳过与 S3 full_video 跳过"
            # dry-run 不写日志/不写 manifest/不留 .part（不修改项目数据）
            assert not (pj / "video" / "S2.mp4").exists()
            assert not any(f.endswith(".part") for f in os.listdir(pj / "video"))
        return "exit=0；计划含 S2=243帧映射、S1 done 跳过、S3 full_video 跳过；无产物写入"

    # ---- §4.0 退出码语义：服务器不可达 → exit 2（用死端口注入，不依赖真实服务器状态）----
    @check("v12", "服务器预检不可达 → exit 2 + 提示文案（--set 注入死端口，不触真实服务器）")
    def _v12():
        with tempfile.TemporaryDirectory() as td:
            pj = _mk_project(td)
            log_path = Path(pconfig.resolved(pconfig.load_global()).logs_dir) / "测试剧_video.log"
            had_log = log_path.exists()
            backup = log_path.read_bytes() if had_log else None
            r = subprocess.run(
                [sys.executable, "-m", "pipeline.stages.video", str(pj),
                 "--set", "comfy.server=http://127.0.0.1:9"],
                cwd=str(REPO_ROOT), capture_output=True, encoding="utf-8", errors="replace",
                timeout=120)
            out = (r.stdout or "") + (r.stderr or "")
            assert r.returncode == 2, "不可达应 exit 2，got %d\n%s" % (r.returncode, out[-1500:])
            assert "run_nvidia_gpu.bat" in out and "ComfyUI 未启动" in out, out[-800:]
            # 单元不得被误标 failed（§4.0：不让 unit 空转尝试后被误标）
            m = pmanifest.load(pmanifest.manifest_path_for(pj, STAGE))
            assert m["units"].get("S1", {}).get("status") in ("done", None), m["units"].get("S1")
            # 清理本次运行写入的日志（不动并行代理可能写的其他日志）
            if backup is not None:
                log_path.write_bytes(backup)
            elif log_path.exists():
                log_path.unlink()
        return "exit=2 + 文案正确；manifest 未被误标"

    # ---- H3 权重在盘（真实盘只读；设计 §7 #6 的 H3 六件套部分）----
    @check("v10", "H3 六件套在盘（models_dir 相对子路径拼全路径 + 大小下限；truncated.bak 不计）")
    def _v10():
        cfg = pconfig.load_global()
        rep = weight_report(cfg)
        v = cfg["video"]
        problems = []
        for key in ("unet", "text_encoder"):
            it = rep[key]
            if not it["exists"] or it["bytes"] < int(v["weight_min_bytes"]):
                problems.append("%s: %s (%dB)" % (key, it["path"], it["bytes"] or -1))
        for key in ("lora_4step", "lora_8step"):
            it = rep[key]
            if not it["exists"] or it["bytes"] < int(v["lora_min_bytes"]):
                problems.append("%s: %s (%dB)" % (key, it["path"], it["bytes"] or -1))
        for key in ("vae_video", "vae_audio"):
            if not rep[key]["exists"]:
                problems.append("%s: %s 不存在" % (key, rep[key]["path"]))
        assert not problems, "；".join(problems)
        sizes = {k: "%.2fGB" % (x["bytes"] / 1e9) for k, x in rep.items()}
        return "六件套就绪：%s" % json.dumps(sizes)

    if with_server:
        @check("v11", "ComfyUI 节点清单（--with-server）：MiniMax H3 链节点在 /object_info")
        def _v11():
            cfg = pconfig.load_global()
            client = build_client(cfg)
            try:
                stats = client.system_stats()
            except Exception as e:
                raise AssertionError(
                    "ComfyUI 不可达（%s）——请先运行 run_nvidia_gpu.bat 后再 --with-server。%s"
                    % (client.server, e))
            ver = (stats.get("system") or {}).get("comfyui_version", "?")
            info = client.object_info()
            need = ["MiniMaxH3ImageToVideo", "CreateVideo", "SaveVideo", "VAEDecodeAudio",
                    "UNETLoader", "LoraLoaderModelOnly"]
            missing = [n for n in need if n not in info]
            assert not missing, (
                "ComfyUI %s 缺节点：%s —— MiniMax H3 支持包未装（值班工程师负责安装，"
                "GitHub 不通走镜像；装好前 video 段无法首跑）" % (ver, ", ".join(missing)))
            return "ComfyUI %s：H3 链节点齐全（%d 个节点在列）" % (ver, len(info))

    return checks


if __name__ == "__main__":
    sys.exit(main())
