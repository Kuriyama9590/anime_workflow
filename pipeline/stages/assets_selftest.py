# -*- coding: utf-8 -*-
"""assets 段干跑自检（仅标准库；不碰 GPU、不启停 ComfyUI、不访问 8188）。

运行：python -m pipeline.stages.assets --selftest   （或 python selftest.py 转发）
任何一项 FAIL ⇒ 总退出码 1。覆盖面：
  1  全模块 py_compile（assets 及其依赖链：workflows/comfy/config/manifest/miniyaml）
  2  miniyaml 解析真实 pipeline/config.yaml + 自检夹具（characters/project yaml）
  3  characters.yaml 规范化 + 全量校验（好夹具通过 / 坏夹具一次性列出全部错误）
  4  seed 派生公式（§3 角则 4：1_000_000 + 1000×角色序 + 10×资产序 + 候选 +(i-1)；显式 seed 优先）
  5  提示词拼装：设计 §4.1 措辞 + 内容边界措辞 + §3 规则 5 次序；负面永远以边界负面收尾
  6  双通道工作流图合法（workflows.build_t2i + validate_api_workflow；参数注入断言）
  7  dry-run：打印全部 unit 计划且零写盘
  8  假渲染全量：产物/meta/manifest 齐全且内容正确（模型/种子/提示词）
  9  断点续跑幂等：重跑零提交；--force 全量重渲；--only 过滤
 10  失败路径：注入失败 → 重试 attempts 次后 failed、退出码 1、其余 unit 不受累
 11  --auto-pick / --pick：定稿文件 + picked 标志 + keyframes 门禁字段（files.threeview）
 12  失效判定：characters.yaml 编辑 → 该角色 stale 重渲；段配置变化 → 全量 stale；--keep-stale 保留
 13  孤儿收敛（§4.0.2）：history 命中收割 / queue 等待收割 / 查无置 pending / 等待超时人工处置
 14  服务器不可达 → 退出码 2（连本地保留端口 127.0.0.1:9，不碰 8188）
 15  缺权重 → 退出码 1 且列出缺口
 16  CLI：--help 退出 0；缺 project 退出 2；未知项目退出 1
 17  pick 自愈：定稿文件被误删后自动从记录候选补拷

测试注入：monkeypatch pipeline.comfy.build_client 返回 FakeComfy（实现
assets 段用到的窄接口：reachable/queue_prompt/wait/history/download_images/prompt_in_queue）。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import py_compile
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

from pipeline import miniyaml
from pipeline.stages import assets as A

REPO_ROOT = Path(__file__).resolve().parents[2]

PROJECT_YAML = """\
# 自检夹具：内容中性、全身着装（模块内容边界）
name: demo_asset
style: photo
seed_base: 20261000
style_anchors:
  photo: "cinematic film still, cel-shaded with soft lighting, muted palette"
  anime: "masterpiece anime illustration, cel shading, film grain"
negative_anchors:
  photo: "photorealistic, 3d render, text, watermark, lowres"
  anime: "photo, 3d render, text, watermark, lowres"
overrides:
  assets: {candidates: 2}
"""

CHARACTERS_YAML = """\
# 角色一致性锚点（自检夹具）
characters:
  - id: detective
    name: 沉舟
    anchor_en: "a 35-year-old male detective, short black hair, tired sharp eyes, tall lean build"
    outfit_en: "beige trench coat over a white shirt, dark trousers, brown leather shoes"
    negative_extra: "child, teenager, blurry, lowres"
    assets:
      three_view: true
      expressions: [neutral, smile, worried]
      props: [briefcase]
  - id: clerk
    name: 店员
    anchor_en: "a 28-year-old female convenience store clerk, ponytail, friendly round face"
    outfit_en: "green store uniform vest over a white polo shirt, black slacks"
    seed: 999000
    assets:
      three_view: true
      expressions: [neutral]
      props: []
props_catalog:
  briefcase: "worn brown leather briefcase with brass clasps"
"""

BAD_CHARACTERS_YAML = """\
characters:
  - id: bad_anchor
    name: 甲
    anchor_en: ""
    assets:
      three_view: true
      props: [ghost_prop]
  - id: "坏 id!"
    name: 乙
    anchor_en: "ok"
    assets:
      three_view: true
  - id: detective
    anchor_en: "anchor ok"
    seed: true
    assets:
      three_view: yes_not_bool
      expressions: "not-a-list"
  - id: detective
    name: 丙
    anchor_en: "dup id"
    assets:
      three_view: true
props_catalog:
  briefcase: "ok anchor"
"""

FAKE_WEIGHTS = [
    "diffusion_models/Sakura-Image-2.1-Turbo-Q5_K_M-4.60GiB.gguf",
    "text_encoders/Sakura-TextEncoder-Qwen3VL-8B-Uncensored-bf16.safetensors",
    "vae/qwen_image_2.1_vae_bf16.safetensors",
    "checkpoints/waiIllustriousSDXL_v170.safetensors",
]

#: 全部 unit 键（detective 5 + clerk 2）
UNIT_KEYS = ["detective:threeview", "detective:expr:neutral", "detective:expr:smile",
             "detective:expr:worried", "detective:prop:briefcase",
             "clerk:threeview", "clerk:expr:neutral"]
#: 全部 png（detective 6 + clerk 3）
ALL_PNGS = ["detective_threeview_c1.png", "detective_threeview_c2.png",
            "detective_expr_neutral.png", "detective_expr_smile.png",
            "detective_expr_worried.png", "detective_prop_briefcase.png",
            "clerk_threeview_c1.png", "clerk_threeview_c2.png",
            "clerk_expr_neutral.png"]


# --------------------------------------------------------------------------- #
# FakeComfy：与 pipeline.comfy.ComfyClient 同窄接口（assets 段实际用到的部分）
# --------------------------------------------------------------------------- #
class FakeComfy:
    def __init__(self, *, fail=False, fail_on=None, wait_error=None, queue_pids=(),
                 extra_history=None, blob_size=120_000):
        self.submitted = []                      # [(pid, wf)]
        self.fail = fail
        self.fail_on = fail_on                   # callable(wf)->bool，选择性注入失败
        self.wait_error = wait_error
        self.queue_pids = set(queue_pids)
        self.extra_history = dict(extra_history or {})
        self.blob_size = blob_size
        self._seq = 0

    def reachable(self):
        return True

    def system_stats(self):
        return {"system": {"fake": True}}

    def _entry(self, pid):
        return {"outputs": {"11": {"images": [{"filename": "%s.png" % pid,
                                               "subfolder": "", "type": "output"}]}},
                "status": {"completed": True, "status_str": "success"}}

    def queue_prompt(self, wf):
        if self.fail or (self.fail_on is not None and self.fail_on(wf)):
            raise RuntimeError("fake queue failure (injected)")
        self._seq += 1
        pid = "fake-%d" % self._seq
        self.submitted.append((pid, wf))
        return pid

    def history(self, pid):
        h = dict(self.extra_history)
        for p, _ in self.submitted:
            h.setdefault(p, self._entry(p))
        return h

    def wait(self, pid, timeout_s=900, quiet_first_s=0):
        if self.wait_error is not None:
            raise self.wait_error
        if pid in self.queue_pids and pid not in self.extra_history \
                and not any(p == pid for p, _ in self.submitted):
            self.queue_pids.discard(pid)         # 模拟"排队片刻后完成"
            self.extra_history[pid] = self._entry(pid)
        if pid in self.extra_history or any(p == pid for p, _ in self.submitted):
            return self.extra_history.get(pid, self._entry(pid))
        raise TimeoutError("fake wait timeout: %s" % pid)

    def prompt_in_queue(self, pid):
        return pid in self.queue_pids

    def download_images(self, entry, dest_dir, dest_name=None, prefix=None):
        os.makedirs(str(dest_dir), exist_ok=True)
        p = os.path.join(str(dest_dir), dest_name or "fake_01.png")
        with open(p, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\nFAKEASSET-" + b"\x00" * self.blob_size)
        return [p]


@contextlib.contextmanager
def fake_client(fake):
    orig = A.comfy_mod.build_client
    A.comfy_mod.build_client = lambda cfg: fake
    try:
        yield fake
    finally:
        A.comfy_mod.build_client = orig


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
class Fixture:
    """临时项目 + 临时 models_dir（哑权重文件）+ 注入用 cfg。"""

    def __init__(self, root: Path):
        self.root = root
        self.proj = root / "projects" / "demo_asset"
        self.proj.mkdir(parents=True, exist_ok=True)
        (self.proj / "project.yaml").write_text(PROJECT_YAML, encoding="utf-8")
        (self.proj / "characters.yaml").write_text(CHARACTERS_YAML, encoding="utf-8")
        self.models = root / "models"
        for rel in FAKE_WEIGHTS:
            p = self.models / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"FAKEWEIGHT" * 100)
        cfg, _doc, _dir = A._load_stage_config(self.proj)
        cfg["comfy"]["models_dir"] = str(self.models)
        cfg["run"]["retry_wait_s"] = 0
        cfg["run"]["quiet_first_s"] = 0
        cfg["paths"]["logs_dir"] = str(root / "logs")
        self.cfg = cfg
        self.assets_dir = self.proj / "assets"

    def mf(self) -> dict:
        return json.loads((self.assets_dir / "assets_manifest.json").read_text(encoding="utf-8"))

    def png(self, name) -> Path:
        return self.assets_dir / name


def run(fx: Fixture, fake: FakeComfy = None, *, inject: bool = True, **kw) -> tuple:
    """跑 run_stage（注入 cfg；inject=True 时以 FakeComfy 替换 build_client）。

    返回 (退出码, fake, 捕获的 stdout)。
    """
    opts = A.StageOptions(**kw)
    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        if inject and fake is not None:
            with fake_client(fake):
                rc = A.run_stage(fx.proj, config=fx.cfg, args=opts)
        else:
            rc = A.run_stage(fx.proj, config=fx.cfg, args=opts)
    finally:
        sys.stdout = old_stdout
    return rc, fake, buf.getvalue()


# --------------------------------------------------------------------------- #
# 检查项
# --------------------------------------------------------------------------- #
def check_compile() -> tuple:
    files = [REPO_ROOT / "pipeline" / "stages" / "assets.py",
             REPO_ROOT / "pipeline" / "stages" / "assets_selftest.py",
             REPO_ROOT / "pipeline" / "workflows" / "__init__.py",
             REPO_ROOT / "pipeline" / "workflows" / "photo_sakura.py",
             REPO_ROOT / "pipeline" / "workflows" / "anime_sdxl.py",
             REPO_ROOT / "pipeline" / "comfy.py",
             REPO_ROOT / "pipeline" / "config.py",
             REPO_ROOT / "pipeline" / "manifest.py",
             REPO_ROOT / "pipeline" / "miniyaml.py"]
    for f in files:
        py_compile.compile(str(f), doraise=True)
    return True, "py_compile %d 个依赖链文件全部通过" % len(files)


def check_miniyaml() -> tuple:
    cfg_doc = miniyaml.load(REPO_ROOT / "pipeline" / "config.yaml")
    ok = (isinstance(cfg_doc.get("comfy"), dict)
          and cfg_doc["comfy"].get("server") == "http://127.0.0.1:8188"
          and cfg_doc["channels"]["photo"]["unet_gguf"].endswith(".gguf")
          and cfg_doc["channels"]["anime"]["steps"] == 28
          and cfg_doc["assets"]["candidates"] == 2
          and cfg_doc["video"]["length_grid"]["a"] == 17
          and isinstance(cfg_doc["channels"]["photo"]["sigmas"], str))
    proj = miniyaml.loads(PROJECT_YAML)
    chars = miniyaml.loads(CHARACTERS_YAML)
    ok = ok and proj["seed_base"] == 20261000 and proj["style"] == "photo"
    ok = ok and proj["overrides"]["assets"]["candidates"] == 2   # 嵌套 flow 映射
    ok = ok and isinstance(chars["characters"], list) and len(chars["characters"]) == 2
    ok = ok and chars["characters"][0]["assets"]["expressions"] == ["neutral", "smile", "worried"]
    ok = ok and chars["characters"][1]["seed"] == 999000
    ok = ok and chars["props_catalog"]["briefcase"].startswith("worn brown")
    return bool(ok), "真实 config.yaml + 夹具解析/类型断言通过"


def check_validate() -> tuple:
    good_doc = miniyaml.loads(CHARACTERS_YAML)
    chars, catalog, errs = A.validate_characters(good_doc)
    if errs:
        return False, "好夹具不应有错误，得到: %s" % errs
    ok = (len(chars) == 2 and chars[0]["id"] == "detective"
          and chars[0]["expr_files"] == ["neutral", "smile", "worried"]
          and chars[1]["seed"] == 999000 and catalog["briefcase"])
    bad_doc = miniyaml.loads(BAD_CHARACTERS_YAML)
    _c, _p, bad_errs = A.validate_characters(bad_doc)
    # 全量列出：id 非法 / anchor_en 空 / name 缺失 / three_view 非布尔 / expressions 非列表 /
    # ghost_prop 不在 catalog / id 重复 / seed 为 bool —— 全部一次给出
    joined = "\n".join(bad_errs)
    for frag in ("ASCII", "anchor_en", "three_view", "expressions", "ghost_prop", "重复", "seed"):
        if frag not in joined:
            return False, "坏夹具错误清单缺少 %r；实际: %s" % (frag, bad_errs)
    return bool(ok), "好夹具规范化通过；坏夹具一次性列出 %d 条错误" % len(bad_errs)


def check_seeds() -> tuple:
    chars, _cat, errs = A.validate_characters(miniyaml.loads(CHARACTERS_YAML))
    if errs:
        return False, str(errs)
    units = {u.key: u for u in A.plan_units(chars, 20261000, 2)}
    # pipeline/seeds.py 公式（角色序/资产序/候选序均从 1 起）：
    #   sb + 1_000_000 + 1000×ci + 10×ai + (cand-1)；detective ci=1 ⇒ 基 21262000
    expect = {
        "detective:threeview": [21262010, 21262011],
        "detective:expr:neutral": [21262020],
        "detective:expr:smile": [21262030],
        "detective:expr:worried": [21262040],
        "detective:prop:briefcase": [21262050],
        "clerk:threeview": [999000, 999001],           # 角色级显式 seed = 基整体替换
        "clerk:expr:neutral": [999010],
    }
    got = {k: u.seeds for k, u in units.items()}
    if got != expect:
        return False, "seed 派生不一致: %r" % (got,)
    # 直接对照 pipeline.seeds.asset_seed（若共享模块在）防双实现漂移
    try:
        from pipeline import seeds
        ref = [seeds.asset_seed(20261000, 1, 1, k) for k in (1, 2)]
        if ref != expect["detective:threeview"]:
            return False, "与 pipeline.seeds.asset_seed 漂移: %r vs %r" % (ref, expect["detective:threeview"])
    except ImportError:
        pass
    return True, "与 pipeline/seeds.py 一致（1-based；显式 seed=基替换）"


def check_prompts() -> tuple:
    chars, catalog, errs = A.validate_characters(miniyaml.loads(CHARACTERS_YAML))
    if errs:
        return False, str(errs)
    units = {u.key: u for u in A.plan_units(chars, 20261000, 2)}
    cfg = miniyaml.load(REPO_ROOT / "pipeline" / "config.yaml")
    style_anchor = miniyaml.loads(PROJECT_YAML)["style_anchors"]["photo"]

    tv = A.compose_prompt(units["detective:threeview"], style_anchor, "")
    ex = A.compose_prompt(units["detective:expr:smile"], style_anchor, "")
    pr = A.compose_prompt(units["detective:prop:briefcase"], style_anchor,
                          catalog["briefcase"])
    # 设计 §4.1 指定措辞逐一在场
    for frag in ("character reference sheet", "front view / side view / back view",
                 "standing full body", "standard outfit", "neutral flat background"):
        if frag not in tv:
            return False, "三视图缺设计措辞 %r" % frag
    for frag in ("expression sheet", "single portrait bust", "smile expression",
                 "same face same hair"):
        if frag not in ex:
            return False, "表情差分缺设计措辞 %r" % frag
    for frag in ("prop sheet", "single object on plain background"):
        if frag not in pr:
            return False, "道具缺设计措辞 %r" % frag
    # 内容边界：三处模板全部携带边界措辞
    for name, p in (("threeview", tv), ("expr", ex), ("prop", pr)):
        for frag in ("fully clothed", "modest", "safe for work"):
            if frag not in p:
                return False, "%s 提示词缺边界措辞 %r" % (name, frag)
    # §3 规则 5 次序：模板 < 角色锚点 < 服装锚点 < 画风锚点
    order = [tv.index("character reference sheet"), tv.index("a 35-year-old male detective"),
             tv.index("beige trench coat"), tv.index("cinematic film still")]
    if order != sorted(order):
        return False, "三视图拼装次序错误: %r" % order
    if "worn brown leather briefcase" not in pr or "a 35-year-old" in pr:
        return False, "道具图锚点错误（应只有道具锚点，无角色锚点）"

    neg = A.compose_negative("photo", cfg, "photorealistic, lowres", "child, blurry, lowres")
    tail = neg.rsplit(",", 1)[-1].strip()
    # 负面永远以边界负面收尾（不可关闭）；去重（lowres 只出现一次）
    if tail != A.BOUNDARY_NEGATIVE.rsplit(",", 1)[-1].strip() or "nude" not in neg:
        return False, "负面词未以边界负面收尾: …%s" % neg[-80:]
    if neg.count("lowres") != 1:
        return False, "负面词去重失效: %s" % neg
    neg_anime = A.compose_negative("anime", cfg, "", "")
    # anime 通道基础负面（channels.anime.negative）应拼入：token 级断言（重组后逗号间隔）
    for tok in ("photo", "3d", "realistic", "nude", "nsfw"):
        if tok not in neg_anime:
            return False, "anime 通道负面缺 token %r: %s" % (tok, neg_anime[:120])
    return True, "三类模板措辞/边界/次序/负面收尾与去重全部通过"


def check_workflows() -> tuple:
    from pipeline.workflows import build_t2i, validate_api_workflow
    cfg = miniyaml.load(REPO_ROOT / "pipeline" / "config.yaml")
    photo = build_t2i("photo", prompt="p", negative="n", seed=7, width=1024, height=1024,
                      filename_prefix="pipeline_assets/x/y", params=cfg["channels"]["photo"])
    anime = build_t2i("anime", prompt="p", negative="n", seed=8, width=1024, height=1024,
                      filename_prefix="pipeline_assets/x/y", params=cfg["channels"]["anime"])
    for name, wf in (("photo", photo), ("anime", anime)):
        validate_api_workflow(wf)                 # 引用闭合/class_type/无 UI 残留
    te = photo["4"]["inputs"]
    ok = (te["prompt"] == "p" and te["negative_prompt"] == "n" and te["resolution"] == 1024
          and photo["7"]["inputs"]["noise_seed"] == 7
          and photo["13"]["inputs"] == {"width": 1024, "height": 1024, "batch_size": 1}
          and "/" not in photo["1"]["inputs"]["unet_name"]
          and photo["1"]["inputs"]["unet_name"].endswith(".gguf")
          and photo["8"]["inputs"]["cfg"] == 1.0
          and photo["5"]["inputs"]["sigmas"].startswith("1.0,"))
    ks = anime["5"]["inputs"]
    ok = ok and (ks["seed"] == 8 and ks["steps"] == 28 and ks["cfg"] == 5.0
                 and ks["sampler_name"] == "euler_ancestral" and ks["scheduler"] == "normal"
                 and ks["denoise"] == 1.0
                 and anime["1"]["inputs"]["ckpt_name"] == "waiIllustriousSDXL_v170.safetensors"
                 and anime["2"]["inputs"]["text"] == "p")
    try:
        build_t2i("gelato", prompt="p", negative="n", seed=1, width=8, height=8,
                  filename_prefix="x", params={})
        return False, "未知通道应抛 ValueError"
    except ValueError:
        pass
    return bool(ok), "photo/anime t2i 图合法，参数注入（seed/分辨率/模型裸名/采样）断言通过"


def check_dry_run(root: Path) -> tuple:
    fx = Fixture(root)
    before = sorted(str(p.relative_to(root)) for p in root.rglob("*") if ".tmp" not in p.name)
    rc, _fake, out = run(fx, FakeComfy(), dry_run=True)
    after = sorted(str(p.relative_to(root)) for p in root.rglob("*") if ".tmp" not in p.name)
    if rc != 0:
        return False, "dry-run 退出码 %d\n%s" % (rc, out[-500:])
    if before != after:
        return False, "dry-run 写盘了：%s" % sorted(set(after) - set(before))
    missing = [k for k in UNIT_KEYS if k not in out]
    if missing:
        return False, "dry-run 计划缺 unit：%s" % missing
    for frag in ("21262010", "待渲染 7", "pick"):
        if frag not in out:
            return False, "dry-run 输出缺 %r" % frag
    return True, "计划打印 7 个 unit、零写盘"


def check_render_full(root: Path) -> tuple:
    fx = Fixture(root)
    fake = FakeComfy()
    rc, fake, _out = run(fx, fake)
    if rc != 0:
        return False, "全量渲染退出码 %d" % rc
    missing = [n for n in ALL_PNGS if not fx.png(n).is_file()]
    if missing:
        return False, "缺产物：%s" % missing
    m = fx.mf()
    for k in UNIT_KEYS:
        if m["units"][k]["status"] != "done":
            return False, "unit %s 状态 %r" % (k, m["units"][k].get("status"))
    if len(fake.submitted) != len(ALL_PNGS):
        return False, "提交数 %d ≠ 产物数 %d" % (len(fake.submitted), len(ALL_PNGS))
    # SaveImage filename_prefix 槽级一致：每个提交的 prefix 与其目标 dest stem 一致
    # （复审：c2 曾误用 dests[0] 的 stem → 服务端 output 审计标签全错）
    prefixes = sorted(wf["11"]["inputs"]["filename_prefix"] for _pid, wf in fake.submitted)
    expect_prefixes = sorted("pipeline_assets/%s/%s" % (n.split("_", 1)[0], Path(n).stem)
                             for n in ALL_PNGS)
    if prefixes != expect_prefixes:
        return False, "filename_prefix 槽级不一致：%r vs %r" % (prefixes[:4], expect_prefixes[:4])
    # 提交的工作流 = 拼装后的完整提示词（photo 通道节点 4）
    pid0, wf0 = fake.submitted[0]
    if wf0["4"]["inputs"]["prompt"].endswith("cinematic film still, cel-shaded with soft lighting, muted palette"):
        pass
    else:
        return False, "提交的提示词未包含画风锚点尾段"
    if wf0["1"]["inputs"]["unet_name"] != "Sakura-Image-2.1-Turbo-Q5_K_M-4.60GiB.gguf":
        return False, "unet 裸名错误: %r" % wf0["1"]["inputs"]["unet_name"]
    if wf0["7"]["inputs"]["noise_seed"] != 21262010:
        return False, "首个提交 seed 应为 21262010，得到 %r" % wf0["7"]["inputs"]["noise_seed"]
    # meta 伴生：模型/种子/提示词
    meta = json.loads(fx.png("detective_threeview_c1.png.meta.json").read_text(encoding="utf-8"))
    for field in ("model", "seed", "prompt", "negative", "sampler", "prompt_id", "channel"):
        if field not in meta:
            return False, "meta 缺字段 %r" % field
    if meta["seed"] != 21262010 or not str(meta["model"]["unet_gguf"]).endswith(".gguf"):
        return False, "meta 内容错误: %s" % json.dumps(meta)[:200]
    if "fully clothed" not in meta["prompt"] or "nude" not in meta["negative"]:
        return False, "meta 提示词/负面缺边界内容"
    # manifest 记录 prompt_id/inputs_hash/plan
    u0 = m["units"]["detective:threeview"]
    if not u0.get("prompt_id") or not u0.get("inputs_hash", "").startswith("sha256:") \
            or "prompt" not in (u0.get("plan") or {}):
        return False, "manifest unit 缺 prompt_id/inputs_hash/plan"
    # 段配置哈希 = channels+assets 节
    if not m.get("config_hash", "").startswith("sha256:"):
        return False, "manifest 缺段配置哈希"
    return True, "9 png + 9 meta + manifest（prompt_id/inputs_hash/plan/段配置哈希）齐全"


def check_resume_force_only(root: Path) -> tuple:
    fx = Fixture(root)
    rc, f1, _ = run(fx, FakeComfy())
    if rc != 0:
        return False, "首次渲染失败 rc=%d" % rc
    rc2, f2, out2 = run(fx, FakeComfy())
    if rc2 != 0 or f2.submitted:
        return False, "幂等续跑失败：rc=%d 新提交=%d" % (rc2, len(f2.submitted))
    if "跳过" not in out2:
        return False, "续跑未打印跳过"
    rc3, f3, _ = run(fx, FakeComfy(), force=True)
    if rc3 != 0 or len(f3.submitted) != len(ALL_PNGS):
        return False, "--force 应全量重渲 9 张：rc=%d 提交=%d" % (rc3, len(f3.submitted))
    # --only：新夹具只渲一个道具 unit
    fx2 = Fixture(root / "only")
    rc4, f4, out4 = run(fx2, FakeComfy(), only=["detective:prop:briefcase"])
    pngs = [p.name for p in fx2.assets_dir.glob("*.png")]
    if rc4 != 0 or len(f4.submitted) != 1 or pngs != ["detective_prop_briefcase.png"]:
        return False, "--only 过滤失效：rc=%d 提交=%d 产物=%s" % (rc4, len(f4.submitted), pngs)
    rc5, _f5, out5 = run(fx2, FakeComfy(), only=["noexist"])
    if rc5 == 0:
        return False, "--only 无匹配应非零退出"
    return True, "幂等（0 新提交）/ --force（9 重渲）/ --only（1 提交）通过"


def check_failure(root: Path) -> tuple:
    fx = Fixture(root)
    rc, fake, out = run(fx, FakeComfy(fail=True))
    if rc != 1:
        return False, "全失败应退出 1，得到 %d" % rc
    m = fx.mf()
    for k in UNIT_KEYS:
        u = m["units"].get(k) or {}
        if u.get("status") != "failed" or u.get("attempts") != 2:
            return False, "unit %s 应 failed 且 attempts=2，得到 %r" % (k, u.get("status"))
        if not u.get("error"):
            return False, "unit %s 未记录 error" % k
    if "fake queue failure" not in out:
        return False, "日志缺注入错误"
    # 修复后重跑：pending/failed → 正常渲染（断点续跑语义）
    rc2, f2, _ = run(fx, FakeComfy())
    if rc2 != 0 or len(f2.submitted) != len(ALL_PNGS):
        return False, "失败后重跑应全部补渲：rc=%d 提交=%d" % (rc2, len(f2.submitted))
    return True, "失败→attempts=2→failed→exit 1；修复后全量补渲通过"


def check_pick(root: Path) -> tuple:
    fx = Fixture(root)
    rc, _f, _ = run(fx, FakeComfy())
    if rc != 0:
        return False, "前置渲染失败"
    rc2, _f2, _ = run(fx, FakeComfy(), auto_pick=True)
    if rc2 != 0:
        return False, "--auto-pick 退出码 %d" % rc2
    m = fx.mf()
    for cid in ("detective", "clerk"):
        u = m["units"]["%s:threeview" % cid]
        if not u.get("picked") or u.get("picked_candidate") != 1:
            return False, "%s 未 auto-pick: %r" % (cid, u.get("picked"))
        if u["files"].get("threeview") != "%s_threeview.png" % cid:
            return False, "%s files.threeview 缺失（keyframes 门禁字段）" % cid
        if not fx.png("%s_threeview.png" % cid).is_file():
            return False, "%s 定稿文件缺失" % cid
    meta = json.loads(fx.png("detective_threeview.png.meta.json").read_text(encoding="utf-8"))
    if meta.get("picked") is not True or meta.get("picked_from") != "detective_threeview_c1.png":
        return False, "定稿 meta 未记录 pick 来源"
    # --pick c2（在独立夹具上）覆盖 auto-pick 语义
    fx2 = Fixture(root / "pick2")
    run(fx2, FakeComfy())
    rc3, _f3, out3 = run(fx2, FakeComfy(), pick="detective:c2")
    if rc3 != 0:
        return False, "--pick 退出码 %d\n%s" % (rc3, out3[-300:])
    if fx2.png("detective_threeview.png").read_bytes() != fx2.png("detective_threeview_c2.png").read_bytes():
        return False, "--pick c2 定稿内容 ≠ c2 候选"
    if (fx2.mf()["units"]["detective:threeview"].get("picked_candidate")) != 2:
        return False, "picked_candidate 应为 2"
    return True, "--auto-pick / --pick（含 keyframes 门禁字段 files.threeview）通过"


def check_stale(root: Path) -> tuple:
    fx = Fixture(root)
    run(fx, FakeComfy())
    # (a) characters.yaml 编辑 → 该角色全部 unit stale 重渲，另一角色不受影响
    (fx.proj / "characters.yaml").write_text(
        CHARACTERS_YAML.replace("tired sharp eyes", "calm piercing eyes"), encoding="utf-8")
    rc, f2, out = run(fx, FakeComfy())
    if rc != 0:
        return False, "stale 重渲退出码 %d" % rc
    if len(f2.submitted) != 6:  # detective: 2 cand + 3 expr + 1 prop
        return False, "detective stale 应重渲 6 张，提交 %d" % len(f2.submitted)
    if "stale" not in out:
        return False, "未打印 stale 原因"
    # (b) 段配置变化（channels+assets 节）→ 全量 stale
    fx2 = Fixture(root / "stale2")
    run(fx2, FakeComfy())
    cfg2 = json.loads(json.dumps(fx2.cfg))
    cfg2["channels"]["photo"]["cfg"] = 1.1
    fx2.cfg = cfg2
    rc, f3, out = run(fx2, FakeComfy())
    if rc != 0 or len(f3.submitted) != len(ALL_PNGS):
        return False, "段配置变化应全量重渲：rc=%d 提交=%d" % (rc, len(f3.submitted))
    # (c) --keep-stale：同样变化但保留旧产物
    fx3 = Fixture(root / "stale3")
    run(fx3, FakeComfy())
    cfg3 = json.loads(json.dumps(fx3.cfg))
    cfg3["channels"]["photo"]["cfg"] = 1.2
    fx3.cfg = cfg3
    rc, f4, out = run(fx3, FakeComfy(), keep_stale=True)
    if rc != 0 or f4.submitted:
        return False, "--keep-stale 应零提交：rc=%d 提交=%d" % (rc, len(f4.submitted))
    return True, "inputs_hash（角色级）/段配置哈希/--keep-stale 三路失效判定通过"


def check_orphans(root: Path) -> tuple:
    # A: history 命中 → 槽级收割不重提（prompt_ids 记录 c1 的 job）
    fx = Fixture(root / "a")
    run(fx, FakeComfy())
    fx.png("detective_threeview_c1.png").unlink()
    fx.png("detective_threeview_c1.png.meta.json").unlink()
    m = fx.mf()
    m["units"]["detective:threeview"]["status"] = "running"
    (fx.assets_dir / "assets_manifest.json").write_text(
        json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    fakeA = FakeComfy(extra_history={"fake-1": FakeComfy()._entry("fake-1")})
    rc, fA, out = run(fx, fakeA)
    if rc != 0 or fA.submitted:
        return False, "孤儿A：应收割不重提（rc=%d 新提交=%d）\n%s" % (rc, len(fA.submitted), out[-300:])
    if not fx.png("detective_threeview_c1.png").is_file():
        return False, "孤儿A：收割未恢复文件"
    if fx.mf()["units"]["detective:threeview"]["status"] != "done":
        return False, "孤儿A：收割后状态非 done"
    # B: queue 在列 → 等待收割（绝不重提）
    fx = Fixture(root / "b")
    run(fx, FakeComfy())
    fx.png("clerk_expr_neutral.png").unlink()
    m = fx.mf()
    m["units"]["clerk:expr:neutral"]["status"] = "running"
    m["units"]["clerk:expr:neutral"]["prompt_ids"] = {"img": "ghost-queued"}
    m["units"]["clerk:expr:neutral"]["prompt_id"] = "ghost-queued"
    (fx.assets_dir / "assets_manifest.json").write_text(
        json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    fakeB = FakeComfy(queue_pids={"ghost-queued"})
    rc, fB, out = run(fx, fakeB)
    if rc != 0 or fB.submitted:
        return False, "孤儿B：应等待收割不重提（rc=%d 新提交=%d）\n%s" % (rc, len(fB.submitted), out[-400:])
    if not fx.png("clerk_expr_neutral.png").is_file():
        return False, "孤儿B：等待收割未恢复文件"
    # C: 查无（服务器重启过）→ pending 重跑（删掉盘上候选，验证真的重提）
    fx = Fixture(root / "c")
    run(fx, FakeComfy())
    fx.png("clerk_threeview_c1.png").unlink()
    fx.png("clerk_threeview_c2.png").unlink()
    m = fx.mf()
    m["units"]["clerk:threeview"]["status"] = "running"
    m["units"]["clerk:threeview"]["prompt_id"] = "ghost-gone"
    m["units"]["clerk:threeview"]["prompt_ids"] = {}
    (fx.assets_dir / "assets_manifest.json").write_text(
        json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    fakeC = FakeComfy()
    rc, fC, out = run(fx, fakeC)
    if rc != 0 or len(fC.submitted) != 2:
        return False, "孤儿C：查无应置 pending 重渲 2 候选（提交 %d）\n%s" % (len(fC.submitted), out[-300:])
    # D: 等待超时 → failed + 人工处置提示 + 本轮封锁（主循环不得重提，§4.0.2）
    fx = Fixture(root / "d")
    run(fx, FakeComfy())
    fx.png("clerk_threeview_c1.png").unlink()
    fx.png("clerk_threeview_c2.png").unlink()
    m = fx.mf()
    m["units"]["clerk:threeview"]["status"] = "running"
    m["units"]["clerk:threeview"]["prompt_id"] = "ghost-stuck"
    m["units"]["clerk:threeview"]["prompt_ids"] = {}
    (fx.assets_dir / "assets_manifest.json").write_text(
        json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    fakeD = FakeComfy(queue_pids={"ghost-stuck"},
                      wait_error=TimeoutError("fake stuck"))
    rc, fD, out = run(fx, fakeD)
    u = fx.mf()["units"]["clerk:threeview"]
    if rc != 1 or u["status"] != "failed" or "人工处置" not in (u.get("error") or ""):
        return False, "孤儿D：超时应 failed+人工处置（rc=%d status=%r error=%r）" % (
            rc, u["status"], (u.get("error") or "")[:80])
    if fD.submitted:
        return False, "孤儿D：封锁 unit 本轮不得重提（新提交 %d）" % len(fD.submitted)
    # E: --abandon-running 直接重置重提（删掉候选避免断点快路径吞掉重提）
    fx = Fixture(root / "e")
    run(fx, FakeComfy())
    fx.png("clerk_threeview_c1.png").unlink()
    fx.png("clerk_threeview_c2.png").unlink()
    m = fx.mf()
    m["units"]["clerk:threeview"]["status"] = "running"
    m["units"]["clerk:threeview"]["prompt_id"] = "ghost-x"
    m["units"]["clerk:threeview"]["prompt_ids"] = {}
    (fx.assets_dir / "assets_manifest.json").write_text(
        json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    fakeE = FakeComfy()
    rc, fE, out = run(fx, fakeE, abandon_running=True)
    if rc != 0 or len(fE.submitted) != 2 or "abandon" not in out:
        return False, "--abandon-running 失效（提交 %d）" % len(fE.submitted)
    return True, "孤儿收敛 A(槽级history收割)/B(队列等待)/C(查无重跑)/D(超时人工处置)/E(abandon) 通过"


def check_unreachable(root: Path) -> tuple:
    fx = Fixture(root)
    cfg = json.loads(json.dumps(fx.cfg))
    cfg["comfy"]["server"] = "http://127.0.0.1:9"   # 保留端口，本机必拒连；不碰 8188
    fx.cfg = cfg
    # inject=False：run_stage 内部 build_client 构造**真实** client → 预检失败 exit 2
    rc, _f, out = run(fx, None, inject=False)
    if rc == 2 and "run_nvidia_gpu.bat" in out:
        return True, "服务器不可达 → exit 2 且提示 run_nvidia_gpu.bat"
    return False, "不可达应 exit 2（得到 %d）\n%s" % (rc, out[-300:])


def check_missing_weights(root: Path) -> tuple:
    fx = Fixture(root)
    (fx.models / "diffusion_models" / "Sakura-Image-2.1-Turbo-Q5_K_M-4.60GiB.gguf").unlink()
    rc, _f, out = run(fx, FakeComfy())
    if rc == 1 and "channels.photo.unet_gguf" in out and "不在盘" in out:
        return True, "缺权重 → exit 1 且列出缺口路径"
    return False, "缺权重应 exit 1 并列缺口（得到 %d）\n%s" % (rc, out[-300:])


def check_cli(root: Path) -> tuple:
    try:
        A.main(["--help"])
        return False, "--help 应 SystemExit(0)"
    except SystemExit as e:
        if e.code != 0:
            return False, "--help 退出码 %r" % (e.code,)
    try:
        A.main([])
        return False, "缺 project 应 SystemExit(2)"
    except SystemExit as e:
        if e.code != 2:
            return False, "缺 project 退出码 %r" % (e.code,)
    rc = A.run_stage("definitely_no_such_project_xyz")
    if rc != 1:
        return False, "未知项目应退出 1，得到 %d" % rc
    return True, "--help=0 / 缺参=2 / 未知项目=1"


def check_selfheal(root: Path) -> tuple:
    fx = Fixture(root)
    run(fx, FakeComfy(), auto_pick=True)
    fx.png("detective_threeview.png").unlink()
    rc, f, out = run(fx, FakeComfy())
    if rc != 0 or f.submitted:
        return False, "自愈不应重渲（提交 %d）" % len(f.submitted)
    if not fx.png("detective_threeview.png").is_file():
        return False, "自愈未恢复定稿文件"
    if not fx.mf()["units"]["detective:threeview"].get("picked"):
        return False, "自愈丢失 picked 标志"
    return True, "定稿误删 → 从记录候选自动补拷，零重渲"


def check_unified_cli_autopick(root: Path) -> tuple:
    """复审阻塞项回归：统一 CLI（stages.invoke_stage + StageOpts）驱动 assets 段。

    原缺陷：模块曾公开 load_stage_config，触发 invoke_stage 的 video 式分派
    （SimpleNamespace 无 auto_pick、--set 被过滤），`run X --stage assets --auto-pick`
    渲染成功却永不 pick → keyframes 门禁拒绝 → 无人值守流水线断裂。
    """
    from pipeline.stages import StageOpts, invoke_stage
    fx = Fixture(root)
    fake = FakeComfy()
    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        with fake_client(fake):
            rc = invoke_stage("assets", fx.proj, fx.cfg, opts=StageOpts(auto_pick=True))
    finally:
        sys.stdout = old_stdout
    if rc != 0:
        return False, "invoke_stage auto_pick 退出码 %d\n%s" % (rc, buf.getvalue()[-300:])
    m = fx.mf()
    for cid in ("detective", "clerk"):
        u = m["units"].get("%s:threeview" % cid) or {}
        if not u.get("picked") or not u.get("files", {}).get("threeview"):
            return False, "统一 CLI 下 %s 未 pick（picked=%r files=%r）——auto_pick 被 dispatch 丢弃" % (
                cid, u.get("picked"), u.get("files"))
    return True, "invoke_stage+StageOpts(auto_pick) → 两个三视图均 picked（标准分派生效）"


def check_corrupt_manifest(root: Path) -> tuple:
    """复审中项回归：损坏 manifest → 可读错误 + rc=1（不裸抛 ManifestError）。"""
    fx = Fixture(root)
    fx.assets_dir.mkdir(parents=True, exist_ok=True)
    (fx.assets_dir / "assets_manifest.json").write_text("{ 截断的垃圾", encoding="utf-8")
    rc, _f, out = run(fx, FakeComfy())
    if rc != 1:
        return False, "损坏 manifest 应 rc=1，得到 %d\n%s" % (rc, out[-300:])
    if "人工检查或删除重建" not in out:
        return False, "损坏 manifest 错误应可读（含处置指引），实际尾段：%s" % out[-200:]
    return True, "损坏 manifest → rc=1 + 可读处置指引（无裸 traceback）"


def check_mixed_params_guard(root: Path) -> tuple:
    """复审小项回归：失败后改配置重跑，不得复用旧采样参数的盘上文件（混参防御）。

    场景：选择性失败（仅 c2 提交失败）→ threeview unit failed 且 c1 已在盘；
    用户改 channels.photo.cfg → 修复后重跑 → c1 必须一并重提（hash 已变，
    allow_slot_skip 关闭），提交数=全部 9 槽。
    另含 _ascii_prefix 单元断言：c2 槽级后缀 + 中文 stem 的 ASCII 化。
    """
    from pipeline.stages import assets as AA
    fx = Fixture(root)
    fake1 = FakeComfy(fail_on=lambda wf: wf["11"]["inputs"]["filename_prefix"].endswith("_c2"))
    rc1, _f1, out1 = run(fx, fake1)
    if rc1 != 1:
        return False, "前置：c2 选择性失败应 rc=1，得到 %d\n%s" % (rc1, out1[-300:])
    m1 = fx.mf()
    if m1["units"]["detective:threeview"]["status"] != "failed":
        return False, "前置：threeview 应 failed"
    if not fx.png("detective_threeview_c1.png").is_file():
        return False, "前置：c1 应已在盘（失败前的成功槽）"
    cfg2 = json.loads(json.dumps(fx.cfg))
    cfg2["channels"]["photo"]["cfg"] = 1.1
    fx.cfg = cfg2
    rc2, f2, _out2 = run(fx, FakeComfy())
    if rc2 != 0:
        return False, "修复重跑 rc=%d" % rc2
    if len(f2.submitted) != len(ALL_PNGS):
        return False, "改配置后应整组重渲 %d 槽（含在盘 c1），提交 %d" % (
            len(ALL_PNGS), len(f2.submitted))
    # _ascii_prefix 单元断言
    chars, _cat, _e = A.validate_characters(miniyaml.loads(CHARACTERS_YAML))
    units = {u.key: u for u in A.plan_units(chars, 20261000, 2)}
    tv = units["detective:threeview"]
    if not A._ascii_prefix(tv, "detective_threeview_c2.png").endswith("detective_threeview_c2"):
        return False, "_ascii_prefix 应槽级取 dest stem（c2 不再误用 c1）"
    cjk = A._ascii_prefix(tv, "detective_threeview_微笑.png")
    if not cjk.isascii() or "detective" not in cjk:
        return False, "中文 stem 应 ASCII 化：%r" % cjk
    return True, "失败+改配置+重跑 → 9 槽全重提（无混参）；prefix 槽级/中文 ASCII 化通过"


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #
CHECKS = [
    ("1  py_compile 依赖链", check_compile),
    ("2  miniyaml 解析", check_miniyaml),
    ("3  characters 校验", check_validate),
    ("4  seed 派生", check_seeds),
    ("5  提示词与内容边界", check_prompts),
    ("6  双通道工作流", check_workflows),
    ("7  dry-run 零写盘", check_dry_run),
    ("8  假渲染全量产物", check_render_full),
    ("9  续跑/force/only", check_resume_force_only),
    ("10 失败重试与退出码", check_failure),
    ("11 pick / auto-pick", check_pick),
    ("12 三路失效判定", check_stale),
    ("13 孤儿收敛", check_orphans),
    ("14 服务器不可达 exit 2", check_unreachable),
    ("15 缺权重 exit 1", check_missing_weights),
    ("16 CLI 行为", check_cli),
    ("17 pick 自愈", check_selfheal),
    ("18 统一 CLI auto_pick（分派回归）", check_unified_cli_autopick),
    ("19 损坏 manifest 可读错误", check_corrupt_manifest),
    ("20 混参防御 + prefix 槽级", check_mixed_params_guard),
]


def _run_one(fn):
    """执行单个检查（需要 root 的自动配独立临时目录）。返回 (ok, detail)。"""
    import inspect
    if "root" in inspect.signature(fn).parameters:
        with tempfile.TemporaryDirectory(prefix="assets_chk_") as td:
            return fn(Path(td))
    return fn()


def selftest_checks(with_server: bool = False, strict: bool = False) -> list:
    """供根 selftest.py 加性聚合（与 video 段同款注册表约定）。

    返回 [{"id","title","level","run"}]；run() 成功返回明细字符串，失败 raise。
    with_server/strict 仅为签名兼容：assets 段无服务器侧自检项（ComfyUI 在线时的
    首轮渲染/1024 方图标定属首跑冒烟职责，不属 dry 自检）。
    """
    checks = []
    for idx, (name, fn) in enumerate(CHECKS, 1):
        def _run(fn=fn):
            ok, detail = _run_one(fn)
            if not ok:
                raise AssertionError(detail)
            return detail
        checks.append({"id": "A%02d" % idx, "title": name, "level": "fatal", "run": _run})
    return checks


def run_selftest() -> bool:
    """逐项打印 PASS/FAIL；任一 FAIL → False（总退出码 1）。"""
    A._safe_print("== pipeline.stages.assets 干跑自检（不碰 GPU/ComfyUI/8188）==")
    failures = []
    for name, fn in CHECKS:
        try:
            ok, detail = _run_one(fn)
        except Exception as e:  # noqa: BLE001 - 单项崩溃不拖垮整场自检
            ok, detail = False, "%s: %s\n%s" % (type(e).__name__, e,
                                                traceback.format_exc(limit=4))
        mark = "PASS" if ok else "FAIL"
        A._safe_print("[%s] %s — %s" % (mark, name, detail))
        if not ok:
            failures.append(name)
    if failures:
        A._safe_print("== 自检失败 %d 项：%s ==" % (len(failures), ", ".join(failures)))
        return False
    A._safe_print("== 自检全部通过（%d 项）==" % len(CHECKS))
    return True


if __name__ == "__main__":
    sys.exit(0 if run_selftest() else 1)
