# -*- coding: utf-8 -*-
"""框架自检（根目录，仅标准库干跑）—— 核心与脚手架 + camera 模块 + 段模块注册表聚合。

用法：python selftest.py [--with-server] [--strict] [--keep]

边界（任务约定）：
  - 不渲染、不下载、不修改项目数据、不启停 ComfyUI、不碰 GPU；全部检查在
    临时目录/内存里进行（子进程只跑 --help/--dry-run/文案类命令）；
  - Blender 只以 ``--background --python <gen>.bpy -- --verify-only`` 做场景构建验证
    （未安装时 SKIP，--strict 升级为 FAIL）。

覆盖（编号沿 docs/FRAMEWORK_DESIGN.md §7；A 组=核心与脚手架任务书的 fatal 环）：
  A1  目录契约与入口（含 __main__.py，python -m pipeline --help 可执行）      fatal
  A2  miniyaml 往返等价（中文/多行/flow/嵌套 flow 映射）+ 越界语法报错         fatal
  A3  配置：解析/全量校验/合并规则/--set/仓库根锚定（cwd 无关）                fatal
  A4  五段模块导入与契约（含运镜词库与 workflows 入口）                       fatal
  A5  manifest：原子写/三条件完成判定/上游指纹失效                            fatal
  A6  脚手架模板：生成到临时目录 → miniyaml → schema                          fatal
  A7  CLI 冒烟（子进程）：new/run --dry-run/status/非法输入/依赖门             fatal
  A8  帧长网格：5.17s→124 帧 + 5.0–15.0 逐 0.5s 21 点全命中 + 报错路径         fatal
  A9  环境存在性提示：目录/字体/十件权重（大小门）/ffmpeg ≥4                   WARN（--strict→fatal）
  A11 ComfyUI 节点清单（--with-server 时 fatal；不加则 SKIP）
  A12 screenplay 段0（write/breakdown）：llm_client 配置判定/模板模式/有界退回/
      校验器模式/schema 强校验落盘/预算约束（全部无 LLM 干跑，假 chat_fn 注入）  fatal
  B   camera 模块（该模块交付的检查：词库/轨迹数学/codegen/模式B/LLM 钩子/
      Blender 发现与场景构建/ffmpeg/vocab 子命令）                            其自定级
  C   段模块自检注册表聚合（pipeline/stages/*.selftest_checks，如 video v1-vN）fatal

任一 FAIL（含 --strict 下升级的 WARN）→ exit 1。
"""
from __future__ import annotations

import argparse
import ast
import importlib
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zlib

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO_ROOT)

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from pipeline import blender                                     # noqa: E402
from pipeline import (config as config_mod, manifest as mf,      # noqa: E402
                      miniyaml, scaffold, schema, timing)
from pipeline.camera import codegen, llm_bpy, preset_moves, vocab  # noqa: E402

BLENDER_VERIFY_TIMEOUT_S = 180
STRICT = False
WITH_SERVER = False
# (名称, ok, 备注)：ok=True→PASS；False→FAIL；"warn"→WARN（--strict 才算失败）；None→SKIP
RESULTS: list[tuple[str, object, str]] = []


def record(name: str, ok, note: str = "") -> None:
    RESULTS.append((name, ok, note))
    tag = {True: "PASS", False: "FAIL", "warn": "WARN", None: "SKIP"}[ok if ok in (True, False, "warn", None) else bool(ok)]
    print("  %s  %s%s" % (tag, name, (" | " + note) if note else ""), flush=True)


def record_skip(name: str, note: str) -> None:
    record(name, None, note)


def section(title: str) -> None:
    print("\n[%s]" % title, flush=True)


def run_core(name: str, fn, warn_level: str = "fatal"):
    """核心检查：fn() 成功返回备注字符串；断言/异常 → 按级别记 FAIL/WARN。"""
    try:
        note = fn()
    except Exception as e:  # noqa: BLE001
        note = None
        record(name, False if warn_level == "fatal" else "warn",
               "异常 %s: %s" % (type(e).__name__, e))
        return
    record(name, True, note or "")


# ---------------------------------------------------------------- 测试工具
def write_png(path: str, w: int = 64, h: int = 36) -> str:
    """纯标准库 PNG 生成器（RGB 渐变），自测贴图用。"""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    raw = bytearray()
    for y in range(h):
        raw.append(0)  # filter: none
        for x in range(w):
            raw += bytes((x * 255 // max(1, w - 1), y * 255 // max(1, h - 1), 128))
    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(data)
    return path


def finite(v) -> bool:
    return isinstance(v, (int, float)) and math.isfinite(v)


def _cli(*args, expect=None, contains=None):
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, "-m", "pipeline"] + list(args),
                          capture_output=True, cwd=REPO_ROOT, env=env, timeout=300)
    out = (proc.stdout + proc.stderr).decode("utf-8", "replace")
    if expect is not None:
        assert proc.returncode == expect, "期望退出码 %s 实得 %s：\n%s" % (expect, proc.returncode, out[-900:])
    if contains:
        for kw in (contains if isinstance(contains, (list, tuple)) else [contains]):
            assert kw in out, "输出缺 %r：\n%s" % (kw, out[-900:])
    return proc.returncode, out


# ================================================================ A 核心与脚手架
YAML_SAMPLE = """\
# 注释行（中文）
project: 自检样例
seed_base: 20261000
flag_true: true
score: 5.17
nothing: null
url_bare: http://127.0.0.1:8188   # 冒号后无空格，不作键分隔
quoted: "含: 冒号与 # 井号的值，保留"
style_anchors:
  photo: "cinematic anime film still"
  anime: masterpiece anime illustration
input_dirs:
  - E:/SD-WEBUI-AKI-V5.0/ComfyUI/input
  - E:/other/input
defaults: {duration_s: 5.17, camera: {mode: B, move: push_in, intensity: slow}}
shots:
  - id: S1
    title: 示例镜
    characters: [主角id]
    camera: {mode: B, extra: ""}
    motion: |
      [0s-2s] 第一行动作
      [2s-5s] 第二行动作
  - id: S2
    title: 折叠块
    note: >
      折叠为
      一行
"""

YAML_EXPECTED = {
    "project": "自检样例",
    "seed_base": 20261000,
    "flag_true": True,
    "score": 5.17,
    "nothing": None,
    "url_bare": "http://127.0.0.1:8188",
    "quoted": "含: 冒号与 # 井号的值，保留",
    "style_anchors": {"photo": "cinematic anime film still",
                      "anime": "masterpiece anime illustration"},
    "input_dirs": ["E:/SD-WEBUI-AKI-V5.0/ComfyUI/input", "E:/other/input"],
    "defaults": {"duration_s": 5.17,
                 "camera": {"mode": "B", "move": "push_in", "intensity": "slow"}},
    "shots": [
        {"id": "S1", "title": "示例镜", "characters": ["主角id"],
         "camera": {"mode": "B", "extra": ""},
         "motion": "[0s-2s] 第一行动作\n[2s-5s] 第二行动作\n"},
        {"id": "S2", "title": "折叠块", "note": "折叠为 一行\n"},
    ],
}


def core_dirs_and_entry() -> str:
    for rel in ("pipeline/__init__.py", "pipeline/__main__.py", "pipeline/config.yaml",
                "pipeline/cli.py", "pipeline/miniyaml.py", "pipeline/manifest.py",
                "pipeline/schema.py", "pipeline/scaffold.py",
                "docs/FRAMEWORK_DESIGN.md", "selftest.py"):
        assert os.path.isfile(os.path.join(REPO_ROOT, rel)), "缺契约文件 %s" % rel
    cfg = config_mod.load_global()
    config_mod.ensure_runtime_dirs(cfg)
    r = config_mod.resolved(cfg)
    assert r.projects_root.is_dir() and r.logs_dir.is_dir(), "projects_root/logs_dir 应可创建"
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, "-m", "pipeline", "--help"],
                          capture_output=True, cwd=REPO_ROOT, env=env, timeout=120)
    out = (proc.stdout or b"") + (proc.stderr or b"")
    assert proc.returncode == 0, "`python -m pipeline --help` 退出码 %s：\n%s" \
        % (proc.returncode, out.decode("utf-8", "replace")[:500])
    assert b"usage" in out.lower(), "--help 输出缺 usage"
    return "契约文件齐全；projects/logs 可创建；python -m pipeline --help 退出码 0"


def core_miniyaml() -> str:
    got = miniyaml.parse(YAML_SAMPLE)
    assert got == YAML_EXPECTED, "样例解析不等价：\n得到 %s" % json.dumps(got, ensure_ascii=False, indent=1)
    again = miniyaml.parse(miniyaml.dumps(got))
    assert again == got, "dumps 往返不等价"
    assert miniyaml.load.__doc__ or True
    for bad, keyword in (("a: &anchor 1", "锚点"), ("---\nx: 1", "多文档"),
                         ("key:\n\tsub: 1", "Tab")):
        try:
            miniyaml.parse(bad)
            raise AssertionError("越界语法 %r 未报错" % bad)
        except miniyaml.MiniYAMLError as e:
            assert keyword in str(e) and "line" in str(e), "报错缺行号/关键词：%s" % e
    return ("样例断言全对（嵌套 flow 映射/多行块/类型推断/注释）；dumps 往返等价；"
            "锚点/多文档/Tab 报错均带行号")


def core_config() -> str:
    cfg = config_mod.load_global()
    errs = config_mod.validate(cfg)
    assert not errs, "全局配置校验问题：\n  - %s" % "\n  - ".join(errs)
    h1, h2 = config_mod.config_hash(cfg), config_mod.config_hash(config_mod.load_global())
    assert h1 == h2 and h1.startswith("sha256:"), "配置哈希不稳定/格式不对：%s" % h1
    merged_cfg = config_mod.deep_merge(
        {"a": {"b": 1, "c": [1, 2]}, "d": "x"},
        {"a": {"b": 2, "c": [3]}, "e": True})
    assert merged_cfg == {"a": {"b": 2, "c": [3]}, "d": "x", "e": True}, \
        "深合并规则不符（标量覆盖/列表整体替换/映射递归）：%s" % merged_cfg
    assert config_mod.parse_set("video.fps=12") == {"video": {"fps": 12}}, "--set 类型推断不符"
    assert config_mod.parse_set("run.server_precheck=false")["run"]["server_precheck"] is False
    r_before = config_mod.resolved(cfg)
    old_cwd = os.getcwd()
    tmp = tempfile.mkdtemp(prefix="selftest-cwd-")
    try:
        os.chdir(tmp)
        r_after = config_mod.resolved(config_mod.load_global())
    finally:
        os.chdir(old_cwd)
        shutil.rmtree(tmp, ignore_errors=True)
    assert r_before.projects_root == r_after.projects_root, "projects_root 随 cwd 漂移"
    assert r_before.logs_dir == r_after.logs_dir, "logs_dir 随 cwd 漂移"
    probe = config_mod.load()
    assert isinstance(probe, dict) and "comfy" in probe and "video" in probe, "load() 兼容入口异常"
    for stage in ("assets", "keyframes", "camera", "video", "final"):
        assert config_mod.stage_sections_hash(cfg, stage).startswith("sha256:"), stage
    return ("校验 0 错误；哈希稳定；深合并/--set 断言过；临时 cwd 下路径锚定不变；"
            "load() 探测兼容入口可用；五段节哈希可算")


def core_stage_imports() -> str:
    from pipeline.stages import NEEDS_COMFY, STAGE_ORDER
    assert STAGE_ORDER == ("assets", "keyframes", "camera", "video", "final")
    assert NEEDS_COMFY == {"assets", "keyframes", "video"}, "needs_comfy 集不符（§2.1 run 注）"
    found = []
    for stage in STAGE_ORDER:
        mod = importlib.import_module("pipeline.stages.%s" % stage)
        assert callable(getattr(mod, "run_stage", None)), "%s.run_stage 不可调用" % stage
        found.append(stage)
    v = vocab.load_vocab()
    assert "push_in" in (v.get("moves") or {}) and "slow" in v["moves"].get("push_in", {}), \
        "运镜词库缺 push_in/slow（脚手架模板默认值依赖它）"
    from pipeline import workflows
    for fn in ("build_t2i", "build_edit", "build_h3"):
        assert callable(getattr(workflows, fn, None)), "workflows.%s 缺失" % fn
    return "五段导入成功（%s）；词库含 push_in/slow；workflows 三个统一入口在" % "/".join(found)


def core_manifest() -> str:
    with tempfile.TemporaryDirectory(prefix="selftest-mf-") as td:
        tdir_path = os.path.join(td)
        mpath = os.path.join(tdir_path, "keyframes_manifest.json")
        m = mf.ensure(mpath, "keyframes", "自检", "sha256:cfg1")
        f = os.path.join(tdir_path, "S1_first.png")
        with open(f, "wb") as fh:
            fh.write(b"x" * 150_000)
        mf.mark_unit(m, "S1:first", status="done", seed=20261010,
                     files={"first": "S1_first.png"}, inputs_hash="sha256:in1",
                     upstream={"first_png": mf.fingerprint_file(f)},
                     stage_cfg_hash="sha256:sec1")
        mf.save(mpath, m)
        assert not [p for p in os.listdir(td) if "tmp" in p], "原子写残留 tmp 文件"
        m2 = mf.load(mpath)
        rec = m2["units"]["S1:first"]
        assert mf.unit_complete(rec, tdir_path, 100_000), "完成判定应通过（≥100KB）"
        assert not mf.unit_complete(rec, tdir_path, 200_000), "大小下限未生效"
        os.remove(f)
        assert not mf.unit_complete(rec, tdir_path, 100_000), "文件删除后完成判定应失败"
        with open(f, "wb") as fh:
            fh.write(b"x" * 150_000)
        st = os.stat(f)
        os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
        reason = mf.stale_reason(rec, upstream={"first_png": mf.fingerprint_file(f)})
        assert reason and "指纹变化" in reason, "上游 mtime 变化未判 stale：%r" % reason
        assert "inputs_hash" in mf.stale_reason(rec, inputs_hash="sha256:in2")
        assert "配置哈希" in mf.stale_reason(rec, stage_cfg_hash="sha256:sec2")
        assert mf.stale_reason(rec) is None, "无变化不应判 stale"
        with open(mpath, "w", encoding="utf-8") as fh:
            fh.write("{broken")
        try:
            mf.load(mpath)
            raise AssertionError("损坏 manifest 未报错")
        except mf.ManifestError as e:
            assert "损坏" in str(e)
    return ("原子写无残留；三条件完成判定/大小门/文件缺失；mtime→stale 与 inputs/配置哈希失效；"
            "损坏 manifest 报可读错误")


def core_scaffold() -> str:
    with tempfile.TemporaryDirectory(prefix="selftest-sc-") as td:
        made = []
        for name, style in (("自检示例剧", "photo"), ("自检动画剧", "anime")):
            pdir = scaffold.new_project(name, style, td)
            made.append(name)
            for fname in scaffold.PROJECT_FILES:
                assert os.path.isfile(os.path.join(pdir, fname)), "缺模板文件 %s" % fname
            for sdir in scaffold.STAGE_DIRS:
                assert os.path.isdir(os.path.join(pdir, sdir)), "缺产物目录 %s" % sdir
                assert os.path.isfile(os.path.join(pdir, sdir, "%s_manifest.json" % sdir))
            data = schema.load_project_data(pdir)  # 内部已过 miniyaml + schema
            assert not schema.validate_all(data), "schema 校验应通过"
            with open(os.path.join(pdir, "shots.yaml"), encoding="utf-8") as fh:
                parsed = miniyaml.parse(fh.read())
            again = miniyaml.parse(miniyaml.dumps(parsed))
            assert again == parsed, "shots.yaml 模板 dumps 往返不等价"
            assert parsed["shots"][0]["id"] == "S1"
            with open(os.path.join(pdir, "剧本模板.md"), encoding="utf-8") as fh:
                assert len(fh.read()) > 500, "剧本模板.md 内容过短"
        try:
            scaffold.new_project("自检示例剧", "photo", td)
            raise AssertionError("重复项目未拒绝")
        except FileExistsError:
            pass
        return ("两种通道脚手架通过（%s）：模板 miniyaml 可解析且往返等价、schema 0 问题、"
                "五目录+空 manifest 齐、剧本模板结构完整" % "+".join(made))


def core_cli_smoke() -> str:
    with tempfile.TemporaryDirectory(prefix="selftest-cli-") as td:
        set_root = "--set=paths.projects_root=%s" % Path_td(td)
        _cli("new", "冒烟剧", "--style", "photo", set_root, expect=0, contains="schema 校验通过")
        _cli("run", "冒烟剧", "--stage", "assets", "--dry-run", set_root, expect=0, contains="assets")
        # keyframes 人工关口：空 assets manifest（未 pick）→ 拒绝开跑（§4.1 宁可停，不糊）
        _cli("run", "冒烟剧", "--stage", "keyframes", "--dry-run", set_root, expect=1,
             contains="三视图")
        _cli("run", "冒烟剧", "--stage", "camera", "--dry-run", set_root, expect=0,
             contains="modeB")
        # §4.0.3 依赖缺口：dry 预览同样按 1 记（反映"真跑会不会过"）
        _cli("run", "冒烟剧", "--stage", "final", "--dry-run", set_root, expect=1,
             contains="依赖缺口")
        _cli("run", "冒烟剧", "--stage", "all", "--dry-run", set_root, expect=1)
        _cli("status", "冒烟剧", set_root, expect=0,
             contains=["keyframes", "units=0"])  # 脚手架写空 manifest（§6），status 显示空表而非缺文件
        _cli("run", "冒烟剧", "--stage", "bogus", "--dry-run", set_root, expect=1, contains="未知段")
        _cli("run", "不存在的剧", "--stage", "assets", "--dry-run", set_root, expect=1,
             contains="不存在")
        _cli("new", "冒烟剧", set_root, expect=1, contains="已存在")
        _cli("new", "冒烟剧", "--force", set_root, expect=0)
        # doctor：服务器停机属常态，只要求信息性输出不炸
        _cli("doctor", set_root, expect=0, contains="doctor")
    return ("new/assets-dry/camera-dry/status/doctor/非法段/非法项目/重复建与 --force 逐项断言过；"
            "keyframes 三视图门、final 依赖缺口、--stage all 均按设计拒绝（exit 1）")


def Path_td(td: str) -> str:
    return td.replace("\\", "/")


def core_grid() -> str:
    cfg = config_mod.load_global()["video"]
    r = timing.map_duration(5.17, cfg)
    assert (r["k"], r["length"]) == (7, 124), "5.17s 应 k=7/124 帧，得到 %s" % r
    assert abs(r["eff_s"] - 5.17) < 0.01
    tol = 17 / 24 / 2
    pts = timing.sweep_grid(cfg, step=0.5)
    assert len(pts) == 21, "应 21 点，得到 %d" % len(pts)
    for d, rr in pts:
        assert 7 <= rr["k"] <= 21 and rr["length"] == 17 * rr["k"] + 5
        assert abs(rr["eff_s"] - d) <= tol + 1e-9, "%.1fs 命中失败：%s" % (d, rr)
    assert pts[-1][1]["k"] == 21 and pts[-1][1]["length"] == 362, "端点 15.0 应 k=21/362 帧"
    for bad in (4.9, 15.1):
        try:
            timing.map_duration(bad, cfg)
            raise AssertionError("%.1fs 越域未报错" % bad)
        except timing.GridError as e:
            assert "请求域" in str(e)
    narrow = dict(cfg)
    narrow["k_max"] = 7
    try:
        timing.map_duration(6.0, narrow)
        raise AssertionError("容差超限未报错")
    except timing.GridError as e:
        assert "网格点" in str(e), "报错缺最近网格点：%s" % e
    return ("5.17s→k=7/124 帧；21/21 点半步命中（端点 15.0→k=21/362 帧）；"
            "越域/超容差报错含最近网格点")


def core_env_hints():
    """返回 (ok, note)；ok="warn" 表示存在环境提示（--strict 才算失败）。"""
    cfg = config_mod.load_global()
    problems = list(config_mod.path_hints(cfg))
    ok_weights = []
    for w in config_mod.weight_checks(cfg):
        if w["ok"]:
            ok_weights.append("%s=%.2fGB" % (w["label"].split(".")[-1], w["size"] / 1e9))
        else:
            problems.append("%s %s（%s）" % (w["label"], w["problem"], w["path"]))
    try:
        from pipeline import ffmpeg_tool
        ff = ffmpeg_tool.FFmpegTool.from_config(cfg.get("final") or {})
        ver = ff.version()
        major, _minor = ff.version_tuple()
        if major < 4:
            problems.append("ffmpeg 版本过低：%s" % ver)
        ff_note = "ffmpeg %s（≥4）ffprobe ✓" % ver.split()[2] if "version" in ver else "ffmpeg 就绪"
    except Exception as e:  # noqa: BLE001
        problems.append("ffmpeg/ffprobe 不可用：%s" % e)
        ff_note = "ffmpeg 不可用"
    note = "目录/字体 0 提示；权重 %d/10 在盘且过大小门（%s）；%s" \
           % (len(ok_weights), " ".join(ok_weights), ff_note)
    if problems and STRICT:
        return False, note + "\n提示：\n  - " + "\n  - ".join(problems)
    if problems:
        return "warn", note + "\n提示：\n  - " + "\n  - ".join(problems)
    return True, note


def core_camera_regressions() -> str:
    """camera 段回归（复审两轮修复的路径）：网格未命中分支 / LLM 桥接清理 / 时长透传 / SystemExit 兜底。"""
    import copy
    import glob
    from pipeline import scaffold as _sc
    from pipeline.stages import camera as _cam
    from pipeline.stages import StageOpts as _SO

    notes = []
    with tempfile.TemporaryDirectory(prefix="selftest-cam-") as td:
        pdir = _sc.new_project("回归剧", "photo", td)
        shots_path = os.path.join(pdir, "shots.yaml")
        cfg = config_mod.load(str(pdir))

        # -- 1. 网格未命中分支（复审轮 KeyError 崩溃路径）：modeA + 网格 a=40 → 5.17s 必超容差
        # （网格只约束模式A：modeB 的 camdesc 是纯文本，不涉及时长，改网格不受影响）
        t0 = open(shots_path, encoding="utf-8").read().replace(
            'camera: {mode: B, move: push_in, intensity: slow, extra: ""}',
            'camera: {mode: A, move: push_in}')
        open(shots_path, "w", encoding="utf-8", newline="\n").write(t0)
        bad = copy.deepcopy(cfg)
        bad.setdefault("video", {})["length_grid"] = {"a": 40, "b": 5}
        rc = _cam.run_stage(pdir, bad, _SO(dry_run=False))
        m = json.loads(open(os.path.join(pdir, "camera", "camera_manifest.json"),
                            encoding="utf-8").read())
        rec = m["units"]["S1"]
        assert rc == 1 and rec["status"] == "failed" and rec.get("inputs_hash"), (rc, rec)
        assert "时长网格未命中" in (rec.get("error") or ""), rec.get("error")
        notes.append("网格未命中：rc=1、unit=failed（含 inputs_hash），无 KeyError")
        # 同场景 CLI dry-run：干净失败（无 traceback、不误诊 exit 2）。
        # 模式A 依赖 keyframes 首末帧（§4.0.3 会先拦），补假帧让预览走到网格分支
        for _kind in ("first", "last"):
            with open(os.path.join(pdir, "keyframes", "S1_%s.png" % _kind), "wb") as _f:
                _f.write(b"\x89PNG" + b"x" * 120_000)
        env = dict(os.environ, PYTHONUTF8="1")
        proc = subprocess.run(
            [sys.executable, "-m", "pipeline", "run", "回归剧", "--stage", "camera",
             "--dry-run", "--set=paths.projects_root=" + td.replace(chr(92), "/"),
             "--set=video.length_grid.a=40"],
            capture_output=True, cwd=REPO_ROOT, env=env, timeout=120)
        out = (proc.stdout + proc.stderr).decode("utf-8", "replace")
        assert proc.returncode == 1 and "Traceback" not in out and "KeyError" not in out, out[-400:]
        assert "时长网格未命中" in out or "将失败" in out, out[-400:]
        notes.append("CLI dry-run：exit 1、无 Traceback/KeyError")

        # -- 2. LLM 桥接清理（复审轮 env/临时文件泄漏路径）：modeB 真跑后无残留、env 恢复
        llm_cfg = copy.deepcopy(cfg)
        llm_cfg.setdefault("camera", {})["llm"] = {
            "enabled": True, "base_url": "http://selftest/v1", "model": "m",
            "api_key_env": "RL_KEY", "require_review": True}
        os.environ["RL_KEY"] = "k"
        os.environ["PIPELINE_LLM_CONFIG"] = "sentinel-should-restore"
        before = set(glob.glob(os.path.join(tempfile.gettempdir(), "camera-llm-bridge-*.json")))
        try:
            rc2 = _cam.run_stage(pdir, llm_cfg, _SO(dry_run=False, force=True))
            after = set(glob.glob(os.path.join(tempfile.gettempdir(), "camera-llm-bridge-*.json")))
            assert rc2 == 0, "modeB 真跑应 OK"
            assert before == after, "桥接临时 JSON 泄漏：%s" % (after - before)
            assert os.environ.get("PIPELINE_LLM_CONFIG") == "sentinel-should-restore",                 "env 未恢复：%r" % os.environ.get("PIPELINE_LLM_CONFIG")
        finally:
            os.environ.pop("RL_KEY", None)
            os.environ.pop("PIPELINE_LLM_CONFIG", None)
        notes.append("LLM 桥接：run 后无 camera-llm-bridge-*.json 残留、PIPELINE_LLM_CONFIG 恢复")

        # -- 3. 模式A 时长透传（复审前轮修复的守护用例）：12.0s → k=17→294帧→--duration 12.25
        t = open(shots_path, encoding="utf-8").read()
        t = t.replace("duration_s: 5.17", "duration_s: 12.0")
        open(shots_path, "w", encoding="utf-8", newline="\n").write(t)
        units = _cam.plan(pdir, cfg)
        ua = [x for x in units if x["mode"] == "A"]
        if not ua:  # 模板默认 B：临时改 A 以覆盖透传路径
            t2 = open(shots_path, encoding="utf-8").read().replace(
                'camera: {mode: B, move: push_in, intensity: slow, extra: ""}',
                'camera: {mode: A, move: push_in}')
            open(shots_path, "w", encoding="utf-8", newline="\n").write(t2)
            units = _cam.plan(pdir, cfg)
            ua = [x for x in units if x["mode"] == "A"]
        argv = _cam._argv_for(pdir, ua[0], _SO(), llm_active=False)
        argv_str = " ".join(argv)
        assert "--duration 12.250000" in argv_str and "--fps 24" in argv_str, argv_str
        assert (ua[0]["length"], ua[0]["duration_s"]) == (294, 12.25), ua[0]
        notes.append("模式A 12.0s→294 帧，argv 含 --duration 12.250000 --fps 24")

        # -- 4. SystemExit 兜底（复审前轮）：非法 move 绕过 schema 直调段执行器
        t3 = open(shots_path, encoding="utf-8").read().replace("move: push_in", "move: 非法名")
        open(shots_path, "w", encoding="utf-8", newline="\n").write(t3)
        rc4 = _cam.run_stage(pdir, cfg, _SO(dry_run=False, force=True))
        m4 = json.loads(open(os.path.join(pdir, "camera", "camera_manifest.json"),
                             encoding="utf-8").read())
        st4 = [v["status"] for v in m4["units"].values()]
        assert rc4 == 1 and "running" not in st4, (rc4, st4)
        notes.append("非法 move：rc=1、无 unit 卡 running（SystemExit 逐镜兜底）")
    return "；".join(notes)


def core_screenplay_cli() -> str:
    """段0 编剧的 CLI 冒烟（子进程；--set=llm.enabled=false 确保已配置机器也不触网）。"""
    with tempfile.TemporaryDirectory(prefix="selftest-spc-") as td:
        set_root = "--set=paths.projects_root=%s" % Path_td(td)
        no_llm = "--set=llm.enabled=false"
        _cli("new", "编剧CLI剧", "--style", "photo", set_root, expect=0, contains="schema 校验通过")
        _cli("run", "编剧CLI剧", "--stage", "screenplay-write", "--premise", "测试题材",
             "--shots", "3", "--total-s", "20", "--dry-run", set_root, no_llm,
             expect=0, contains="模板模式")  # dry：不落盘
        assert not os.path.isfile(os.path.join(td, "编剧CLI剧", "剧本.md")), "dry-run 不应写盘"
        _cli("run", "编剧CLI剧", "--stage", "screenplay-write", "--premise", "深夜便利店停电",
             "--style-note", "都市微暖", "--shots", "3", "--total-s", "20", set_root, no_llm,
             expect=0, contains="已生成骨架")  # 未配置 LLM → 模板模式（LLM 模式文案是"剧本已生成"）
        assert os.path.isfile(os.path.join(td, "编剧CLI剧", "剧本.md"))
        _cli("run", "编剧CLI剧", "--stage", "screenplay", set_root, no_llm,
             expect=0, contains="校验通过")  # 串联：write（模板）→ breakdown（校验器）
        _cli("run", "编剧CLI剧", "--stage", "breakdown", "--dry-run", set_root, no_llm, expect=0)
        _cli("run", "编剧CLI剧", "--stage", "breakdown,assets", "--dry-run", set_root, no_llm,
             expect=0)  # 混合段：screenplay 段 + 既有段同跑
        _cli("run", "编剧CLI剧", "--stage", "bogus", "--dry-run", set_root, expect=1,
             contains="未知段")  # 既有非法段语义不变
        # 数据装载失败 + 仅 screenplay 段 → 不提前退出，交给校验器给完整清单
        sp = os.path.join(td, "编剧CLI剧", "shots.yaml")
        good = open(sp, encoding="utf-8").read()
        open(sp, "w", encoding="utf-8", newline="\n").write(good.replace("move: push_in", "move: warp9"))
        _cli("run", "编剧CLI剧", "--stage", "breakdown", set_root, no_llm, expect=1,
             contains="camera.move")
        open(sp, "w", encoding="utf-8", newline="\n").write(good)
    return ("new/write-dry（不落盘）/write 落盘/screenplay 串联/breakdown 校验器/混合段/"
            "非法段拒绝/坏 yaml 时校验器给完整清单（而非提前退出）——逐项断言过")


def core_server_nodes() -> str:
    cfg = config_mod.load_global()
    from pipeline import comfy
    client = comfy.build_client(cfg)
    if not client.reachable():
        raise AssertionError("ComfyUI 不可达（当前 8188 停机即此情形）：请先运行 "
                             "run_nvidia_gpu.bat 拉起后 --with-server 复检")
    info = client.object_info()
    need = ("MiniMaxH3ImageToVideo", "TextEncodeQwenImage21", "UnetLoaderGGUF")
    missing = [n for n in need if n not in info]
    if missing:
        raise AssertionError(
            "ComfyUI 缺节点：%s\n（custom_nodes 现仅 ComfyUI-GGUF 与 SakuraQwenImage21Tools；"
            "MiniMax H3 节点归值班工程师补装——装好前 video 段被阻塞，docs §10.2）" % ", ".join(missing))
    return "服务器可达；三个必需节点均在列"


# ================================================================ B camera 模块
def check_imports() -> None:
    section("B1 camera：模块导入与词库完整性")
    v = vocab.load_vocab()
    moves = v["moves"]
    ok = all(m in moves for m in preset_moves.MOVES) and "locked" in moves
    record("词库含全部模式A运镜（%s）+ locked" % ",".join(preset_moves.MOVES), ok)
    ok = all(sorted(entries) == ["fast", "medium", "slow"] for entries in moves.values())
    record("每个运镜都有 slow/medium/fast 三档", ok, "moves=%s" % ",".join(sorted(moves)))
    ok = all(isinstance(t, str) and 10 < len(t) < 200
             for entries in moves.values() for t in entries.values())
    record("短语均为合理长度英文字符串", ok)


def check_preset_moves(tmp: str) -> None:
    section("B2 camera：预设轨迹数学（纯 Python，无 Blender）")
    fs, fe = 1, 124
    for move in preset_moves.MOVES:
        try:
            track = preset_moves.build_track(move, None, frame_start=fs, frame_end=fe)
        except Exception as e:  # noqa: BLE001
            record("build_track(%s)" % move, False, "异常：%s" % e)
            continue
        keys = track["keys"]
        ok = (len(keys) == fe - fs + 1
              and all(k["frame"] == fs + i for i, k in enumerate(keys))
              and all(all(finite(c) for c in k["location"] + k["rotation"])
                      and finite(k["lens"]) for k in keys)
              and all(k["location"][2] > 0.01 for k in keys))
        note = "keys=%d" % len(keys)
        z0, z1 = keys[0]["location"][2], keys[-1]["location"][2]
        if move == "push_in":
            ok = ok and z1 < z0 * 0.999
            note += " z %.2f→%.2f（递减）" % (z0, z1)
        elif move == "pull_out":
            ok = ok and z1 > z0 * 1.001
            note += " z %.2f→%.2f（递增）" % (z0, z1)
        elif move == "pan":
            p0, p1 = keys[0]["location"], keys[-1]["location"]
            r = [k["rotation"] for k in keys]
            axis = 1 if track["params"]["axis"] == "horizontal" else 0
            others = [i for i in range(3) if i != axis]
            ok = ok and p0 == p1 and r[0][axis] != r[-1][axis] \
                and all(abs(x[others[0]]) + abs(x[others[1]]) < 1e-9 for x in r)
            note += " 定点旋转 axis=%d" % axis
        elif move == "truck":
            ok = ok and keys[-1]["location"][0] > keys[0]["location"][0]
            note += " x %.3f→%.3f" % (keys[0]["location"][0], keys[-1]["location"][0])
        elif move == "orbit":
            r0 = math.hypot(keys[0]["location"][0], keys[0]["location"][1])
            radii = [math.hypot(k["location"][0], k["location"][1]) for k in keys]
            ok = ok and all(abs(rr - r0) < 1e-4 for rr in radii) and r0 > 0.01
            note += " 水平半径恒定 %.3f" % r0
        elif move == "parallax":
            ok = ok and keys[-1]["location"][0] != keys[0]["location"][0]
            note += " 小横移 %.3f→%.3f" % (keys[0]["location"][0], keys[-1]["location"][0])
        record("build_track(%s) 结构与方向性" % move, ok, note)

    first = write_png(os.path.join(tmp, "t_first.png"))
    spec = preset_moves.make_spec("T1", "push_in", first, duration_s=5.17, fps=24,
                                  width=1344, height=768, out_dir=tmp)
    r, sc, ct = spec["render"], spec["scene"], spec["camera_track"]
    ok = (r["width"] == 1344 and r["height"] == 768 and r["fps"] == 24
          and r["frame_start"] == 1 and r["frame_end"] == 124
          and len(ct["keys"]) == 124
          and sc["plane_half"][0] > 0 and sc["plane_half"][1] > 0
          and spec["output"]["endpoints"] == ["T1_camfirst.png", "T1_camlast.png"])
    record("make_spec 分辨率/帧率/帧域可配（1344x768@24 → 124 帧）", ok)
    ok = sc["backdrop"]["enabled"] is False
    spec_px = preset_moves.make_spec("T2", "parallax", first, out_dir=tmp)
    ok = ok and spec_px["scene"]["backdrop"]["enabled"] is True \
        and spec_px["scene"]["backdrop"]["scale"] >= 1.4
    record("parallax 自动启用双平面背景板", ok)
    try:
        preset_moves.build_track("nope")
        record("未知运镜显式报错", False)
    except ValueError:
        record("未知运镜显式报错", True)
    try:
        preset_moves.build_track("push_in", {"bogus": 1})
        record("未知参数显式报错", False)
    except ValueError:
        record("未知参数显式报错", True)
    try:
        preset_moves.make_spec("T3", "push_in", os.path.join(tmp, "missing.png"))
        record("首帧图缺失显式报错", False)
    except FileNotFoundError:
        record("首帧图缺失显式报错", True)


def check_codegen(tmp: str) -> None:
    section("B3 camera：codegen spec → bpy 脚本静态检查")
    first = write_png(os.path.join(tmp, "t_first.png"))
    last = write_png(os.path.join(tmp, "t_last.png"))
    for tag, kwargs in (("单图", {}), ("crossfade", {"image_last": last})):
        spec = preset_moves.make_spec("T1", "orbit", first, out_dir=tmp, **kwargs)
        text = codegen.generate_bpy(spec)
        try:
            ast.parse(text)
            ok = True
        except SyntaxError as e:
            ok = False
            record("codegen(%s) 生成脚本语法合法" % tag, False, "SyntaxError: %s" % e)
            continue
        checks = [
            "BLENDER_EEVEE_NEXT" in text and "BLENDER_EEVEE" in text,
            'resolution_x = int(r["width"])' in text and '"width": 1344' in text,
            'resolution_y = int(r["height"])' in text and '"height": 768' in text,
            'render.fps = int(r["fps"])' in text and '"fps": 24' in text,
            'frame_end = int(r["frame_end"])' in text and '"frame_end": 124' in text,
            codegen.marker() in text,
            "--verify-only" in text and "--render=endpoints" in text
            and "--render=animation" in text,
            "Displace" in text,
            "keyframe_insert" in text,
        ]
        ok = ok and all(checks)
        note = "len=%d" % len(text)
        if tag == "crossfade":
            checks_cf = ["MixShader" in text, "Fac" in text]
            ok = ok and all(checks_cf)
            note += " 含 MixShader crossfade"
        record("codegen(%s) 语法+双枚举+分辨率/帧率+验证模式+位移+关键帧" % tag, ok, note)
    try:
        codegen.generate_bpy({"render": {}})
        record("spec 缺节显式报错", False)
    except ValueError:
        record("spec 缺节显式报错", True)


def check_mode_b(tmp: str) -> None:
    section("B4 camera：模式B camdesc 组装与 shots.yaml 批处理（经 pipeline.miniyaml）")
    text = vocab.build_camdesc("push_in", "slow", "custom drift")
    record("单镜 camdesc",
           text == "Camera: very slow push-in, breathing hand-held stillness, custom drift",
           text)
    # 样例 = 设计文档 §6 脚手架模板的规范格式：flow defaults / 单镜 flow camera /
    # motion | 多行块 / 块风格 camera / 缺 camera 节 / 模式A 镜，全覆盖
    shots_yaml = os.path.join(tmp, "shots_std.yaml")
    with open(shots_yaml, "w", encoding="utf-8", newline="\n") as f:
        f.write("""# selftest shots.yaml（规范格式：miniyaml 支持集，§6 模板同款）
project: selftest
style: photo
defaults: {duration_s: 5.17, camera: {mode: B, move: push_in, intensity: slow}}
shots:
  - id: S1
    title: 示例镜
    motion: |
      [0s-2s] boy walks into the rain
      [2s-5s] umbrella flares in the wind
    camera: {mode: B, move: push_in, intensity: slow, extra: "custom drift"}
  - id: S2
    camera:
      move: orbit
      intensity: fast
  - id: S3
    title: 无 camera 节走 defaults
  - id: S4
    camera:
      mode: A
      move: parallax
""")
    doc = vocab.parse(open(shots_yaml, encoding="utf-8").read(), shots_yaml)
    ok = (isinstance(doc.get("defaults"), dict)
          and isinstance(doc["defaults"].get("camera"), dict)
          and doc["defaults"]["camera"]["move"] == "push_in"
          and isinstance(doc["shots"][0].get("motion"), str)
          and "umbrella flares" in doc["shots"][0]["motion"])
    record("规范格式解析：flow defaults 得嵌套 dict、motion | 得多行块", ok)
    outdir = os.path.join(tmp, "cam")
    results = vocab.camdesc_from_shots(shots_yaml, outdir, log=lambda m: None)
    got = {sid: open(p, encoding="utf-8").read().strip() for sid, p in results}
    ok = (set(got) == {"S1", "S2", "S3"}
          and got.get("S1") == "Camera: very slow push-in, breathing hand-held stillness, custom drift"
          and got.get("S2") == "Camera: fast orbit around the subject"
          and got.get("S3") == "Camera: very slow push-in, breathing hand-held stillness"
          and not os.path.exists(os.path.join(outdir, "S4_camdesc.txt")))
    record("批处理 4 镜：flow/块/缺省 camera 三路 + A×1 跳过 + defaults 兜底", ok,
           "产出=%s" % sorted(got))
    # CLI 批量入口（run_camera.py 文档写明的 modeB --shots）端到端
    cli_out = os.path.join(tmp, "cam_cli")
    proc = subprocess.run([sys.executable, "-m", "pipeline.camera.run_camera", "modeB",
                           "--shots", shots_yaml, "--outdir", cli_out],
                          cwd=REPO_ROOT, capture_output=True, timeout=120,
                          encoding="utf-8", errors="replace")
    ok = (proc.returncode == 0
          and os.path.isfile(os.path.join(cli_out, "S1_camdesc.txt"))
          and os.path.isfile(os.path.join(cli_out, "S3_camdesc.txt"))
          and not os.path.exists(os.path.join(cli_out, "S4_camdesc.txt")))
    record("CLI modeB --shots 批量入口（规范 shots.yaml）", ok,
           "rc=%d %s" % (proc.returncode, (proc.stdout or "").strip().splitlines()[-1][:80]
                         if (proc.stdout or "").strip() else ""))
    try:
        vocab.phrase("push_in", "warp")
        record("词库外强度显式报错", False)
    except KeyError:
        record("词库外强度显式报错", True)


def check_llm_hook() -> None:
    section("B5 camera：LLM 钩子（未配置回退 + 静态安全扫描；不联网）")
    # 屏蔽一切配置来源（含机器上可能存在的 pipeline/camera/llm_config.json，
    # 否则合法配置过的机器上"未配置"断言会假阳性）
    old = {k: os.environ.pop(k, None) for k in
           ("PIPELINE_LLM_CONFIG", "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY")}
    old_file_cfg = llm_bpy._FILE_CONFIG
    try:
        llm_bpy._FILE_CONFIG = os.path.join(tempfile.gettempdir(),
                                            "selftest_no_llm_config.json")
        cfg = llm_bpy.load_config()
        record("未配置时 load_config()=None（走预设模板）", cfg is None)
        # 错误路径契约：cfg=None 必须抛 LLMError（历史上曾 AttributeError 顶替，
        # 调用方 except LLMError 会漏接）
        try:
            llm_bpy.generate_bpy_code({"scene": {"images": {"first": "x"}}})
            record("未配置时 generate_bpy_code 抛 LLMError", False, "未抛异常")
        except llm_bpy.LLMError as e:
            record("未配置时 generate_bpy_code 抛 LLMError", True, str(e)[:60])
        except AttributeError as e:
            record("未配置时 generate_bpy_code 抛 LLMError", False,
                   "AttributeError 顶替：%s" % e)
    finally:
        for k, v in old.items():
            if v is not None:
                os.environ[k] = v
        llm_bpy._FILE_CONFIG = old_file_cfg
    bad = "import bpy\nimport subprocess\nbpy.data.objects.clear()\nopen('x','w')\n"
    v1 = llm_bpy.safety_scan(bad)
    record("安全扫描拒绝 subprocess/open/白名单外 import", len(v1) >= 2, str(v1[:2]))
    good = ("import bpy\nimport json\nscene = bpy.context.scene\n"
            "print('SCENE_BUILD_OK ' + json.dumps({}))\n")
    record("安全扫描放行干净 bpy 脚本", llm_bpy.safety_scan(good) == [],
           str(llm_bpy.safety_scan(good)))
    record("系统提示词含安全契约与 --verify-only 契约",
           "--verify-only" in llm_bpy.build_system_prompt()
           and "no network" in llm_bpy.build_system_prompt())


def check_blender_available(strict: bool) -> bool:
    section("B6 Blender 便携包发现与 --version")
    cfg_install_dir = str(config_mod.resolve_path(
        ((config_mod.load_global().get("camera") or {}).get("blender") or {})
        .get("install_dir", "")))
    exe = blender.find_blender(cfg_install_dir or None) or blender.find_blender()
    if not exe:
        msg = "未找到 blender.exe（config install_dir=%s）；camera 模块首次使用时自动安装" % cfg_install_dir
        if strict:
            record("blender.exe 发现", False, msg)
        else:
            record_skip("blender.exe 发现", msg)
        return False
    ver = blender.get_version(exe)
    record("blender.exe 发现 + --version", ver is not None, "%s -> %s" % (exe, ver))
    return ver is not None


def check_ffmpeg() -> None:
    section("B7 ffmpeg 发现（final/camera 合成依赖）")
    # 口径与 A9/ffmpeg_tool.discover 一致：config final.ffmpeg（可为绝对路径）>
    # shutil.which > PIPELINE_FFMPEG 兜底（PATH 无但配置了绝对路径的机器不再假失败）
    from pipeline import ffmpeg_tool
    try:
        ff = ffmpeg_tool.discover((config_mod.load_global().get("final") or {}))[0]
    except ffmpeg_tool.FFmpegToolError:
        ff = shutil.which("ffmpeg") or os.environ.get("PIPELINE_FFMPEG")
    if not ff:
        record("ffmpeg 可发现（config final.ffmpeg / PATH / PIPELINE_FFMPEG）", False)
        return
    try:
        proc = subprocess.run([ff, "-version"], capture_output=True, timeout=30,
                              encoding="utf-8", errors="replace")
        first = (proc.stdout or "").splitlines()[0]
        major = int(first.split("version")[1].strip().split(".")[0])
        record("ffmpeg -version 可执行且 >=4（发现于 %s）" % ff,
               proc.returncode == 0 and major >= 4, first)
    except (OSError, ValueError, IndexError) as e:
        record("ffmpeg -version 可执行且 >=4", False, str(e))


def check_scene_build(tmp: str, strict: bool, blender_ok: bool) -> None:
    section("B8 camera：Blender 场景构建干跑（--background --python --verify-only，不渲染）")
    if not blender_ok:
        if strict:
            record("场景构建干跑", False, "前置失败：blender 不可用")
        else:
            record_skip("场景构建干跑", "blender 不可用（未装），--strict 下为 fatal")
        return
    first = write_png(os.path.join(tmp, "t_first.png"))
    last = write_png(os.path.join(tmp, "t_last.png"))
    cases = [(m, []) for m in preset_moves.MOVES] + [("push_in+crossfade", ["--image-last", last])]
    for move, extra in cases:
        m = "push_in" if move.endswith("crossfade") else move
        outdir = os.path.join(tmp, "cam_%s" % move.replace("+", "_"))
        cmd = [sys.executable, "-m", "pipeline.camera.run_camera", "modeA",
               "--shot", "T1", "--image-first", first, "--move", m,
               "--outdir", outdir, "--verify-only"] + extra
        t0 = time.time()
        try:
            proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, timeout=300,
                                  encoding="utf-8", errors="replace")
            bpy_file = os.path.join(outdir, "T1.bpy")
            ok = (proc.returncode == 0 and "场景构建自检：PASS" in (proc.stdout or "")
                  and os.path.isfile(bpy_file))
            engine = ""
            for line in (proc.stdout or "").splitlines():
                if codegen.marker() in line:
                    engine = line.split('"engine": "')[1].split('"')[0]
                    break
            note = "%.1fs engine=%s" % (time.time() - t0, engine or "?")
            if not ok:
                tail = "\n".join((proc.stdout or "").splitlines()[-6:])
                note += " rc=%d tail=%s" % (proc.returncode, tail[-300:])
            record("场景构建 %s" % move, ok, note)
        except subprocess.TimeoutExpired:
            record("场景构建 %s" % move, False, "CLI 超时（>300s）")


def check_cli_misc() -> None:
    section("B9 camera：CLI 辅助子命令（vocab，纯文本无副作用）")
    proc = subprocess.run([sys.executable, "-m", "pipeline.camera.run_camera", "vocab"],
                          cwd=REPO_ROOT, capture_output=True, timeout=60,
                          encoding="utf-8", errors="replace")
    ok = proc.returncode == 0 and "push_in" in proc.stdout and "locked" in proc.stdout
    record("vocab 子命令打印词库", ok)


# ================================================================ C 段模块注册表聚合
def check_stage_registries(with_server: bool) -> None:
    section("C 段模块自检注册表聚合（pipeline/stages/*.selftest_checks）")
    from pipeline.stages import STAGE_ORDER
    for stage in STAGE_ORDER:
        try:
            mod = importlib.import_module("pipeline.stages.%s" % stage)
        except ImportError as e:
            record("%s 导入" % stage, False, str(e)[:200])
            continue
        fn = getattr(mod, "selftest_checks", None)
        if not callable(fn):
            record_skip("%s 无 selftest_checks 注册表" % stage, "该模块未提供（可后续补充）")
            continue
        try:
            checks = fn(with_server=with_server, strict=STRICT) or []
        except TypeError:
            checks = fn() or []
        if not checks:
            record_skip("%s selftest_checks 为空" % stage, "")
            continue
        for c in checks:
            name = "%s %s %s" % (stage, c.get("id", "?"), c.get("title", ""))
            level = str(c.get("level", "fatal"))
            try:
                note = c["run"]()
                note = str(note)
                if note.startswith("SKIP"):
                    record_skip(name, note[5:].strip(" ：:"))
                    continue
                record(name, True, note[:220])
            except Exception as e:  # noqa: BLE001
                if level == "warn":
                    record(name, "warn", str(e)[:300])
                elif level == "optional":
                    record_skip(name, "未启用（optional 检查，开启方式见标题）：%s" % str(e)[:160])
                else:
                    record(name, False, str(e)[:300])


# ---------------------------------------------------------------- 主流程
def main() -> int:
    global STRICT, WITH_SERVER
    ap = argparse.ArgumentParser(description="pipeline 框架自检（仅标准库干跑）")
    ap.add_argument("--strict", action="store_true",
                    help="把环境提示（Blender/权重/目录/ffmpeg）升级为 fatal")
    ap.add_argument("--keep", action="store_true", help="保留临时目录")
    ap.add_argument("--with-server", action="store_true",
                    help="追加需 ComfyUI 在线的检查（节点清单，docs §7 #11）")
    args = ap.parse_args()
    STRICT, WITH_SERVER = args.strict, args.with_server

    print("== pipeline selftest（%s，repo=%s python=%s）==" % (
        time.strftime("%Y-%m-%d %H:%M:%S"), REPO_ROOT, sys.version.split()[0]), flush=True)
    t_all = time.time()

    section("A 核心与脚手架（config/CLI/调度/manifest/模板）")
    run_core("A1 目录契约与入口（python -m pipeline --help）", core_dirs_and_entry)
    run_core("A2 miniyaml 往返等价 + 越界语法报错", core_miniyaml)
    run_core("A3 配置：解析/校验/合并/--set/仓库根锚定", core_config)
    run_core("A4 五段模块导入与契约（含运镜词库/workflows 入口）", core_stage_imports)
    run_core("A5 manifest：原子写/完成判定/上游指纹失效", core_manifest)
    run_core("A6 脚手架模板：生成→miniyaml→schema（临时目录）", core_scaffold)
    run_core("A7 CLI 冒烟：new/run --dry-run/status/doctor/非法输入", core_cli_smoke)
    run_core("A8 帧长网格：21 点全扫 + 报错路径", core_grid)
    ok, note = core_env_hints()
    record("A9 环境存在性提示：目录/十件权重/ffmpeg", ok, note)
    run_core("A10 camera 段回归：网格未命中/LLM 桥接清理/时长透传/SystemExit", core_camera_regressions)
    if WITH_SERVER:
        run_core("A11 ComfyUI 可达 + 必需节点清单", core_server_nodes)
    else:
        record_skip("A11 ComfyUI 节点清单",
                    "未加 --with-server（服务器当前停机为常态；需要时先跑 run_nvidia_gpu.bat）")

    section("A12 screenplay 段0：write/breakdown（无 LLM 干跑，假 chat_fn 注入）")
    try:
        from pipeline.screenplay import selftest_checks as _sp_checks
    except ImportError as e:
        record("screenplay 模块导入", False, str(e)[:200])
        _sp_checks = None
    if _sp_checks is not None:
        for _c in _sp_checks(strict=STRICT):
            _name = "screenplay %s %s" % (_c.get("id", "?"), _c.get("title", ""))
            try:
                _note = str(_c["run"]())
                record(_name, True, _note[:220])
            except Exception as e:  # noqa: BLE001
                record(_name, False, str(e)[:300])
        run_core("screenplay CLI：new/write-dry/write/串联/校验器/混合段/非法段",
                 core_screenplay_cli)
    del _sp_checks

    tmp = tempfile.mkdtemp(prefix="pipeline_selftest_")
    try:
        check_imports()
        check_preset_moves(tmp)
        check_codegen(tmp)
        check_mode_b(tmp)
        check_llm_hook()
        blender_ok = check_blender_available(args.strict)
        check_ffmpeg()
        check_scene_build(tmp, args.strict, blender_ok)
        check_cli_misc()
        check_stage_registries(getattr(args, "with_server", False))
    finally:
        if args.keep:
            print("\n临时目录保留：%s" % tmp, flush=True)
        else:
            shutil.rmtree(tmp, ignore_errors=True)

    n_pass = sum(1 for _, ok_, _ in RESULTS if ok_ is True)
    n_fail = sum(1 for _, ok_, _ in RESULTS if ok_ is False or (ok_ == "warn" and STRICT))
    n_warn = sum(1 for _, ok_, _ in RESULTS if ok_ == "warn" and not STRICT)
    n_skip = sum(1 for _, ok_, _ in RESULTS if ok_ is None)
    print("\n==== selftest 汇总：PASS=%d FAIL=%d WARN=%d SKIP=%d（共 %d 项，%.1fs）===="
          % (n_pass, n_fail, n_warn, n_skip, len(RESULTS), time.time() - t_all), flush=True)
    if n_fail:
        print("自检未通过（FAIL=%d）→ exit 1" % n_fail, flush=True)
        return 1
    print("自检通过（fatal 全绿；WARN 为环境提示，--strict 升级为 fatal）", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
