# -*- coding: utf-8 -*-
"""段0 编剧（screenplay）：LLM（或模板/校验器降级）生成 剧本.md 与三份结构化 yaml。

CLI（经根 CLI 的 run 子命令，cli.py 按 SCREENPLAY_STAGES 分发到本包）：
    python -m pipeline run <名> --stage screenplay-write --premise "…" \
        [--style-note "…"] [--shots N] [--total-s S]
    python -m pipeline run <名> --stage breakdown [--shots N] [--total-s S]
    python -m pipeline run <名> --stage screenplay          # = write → breakdown

段属性：不进 STAGE_ORDER（--stage all 仍只含五段，保持向后兼容）；不依赖 ComfyUI；
不写各段 manifest（产物本身即状态：剧本.md / 三份 yaml，重跑前旧版自动备份 .bak）。

内容边界：模板与提示词默认中性叙事、全体角色成年人（18+）；项目尺度由
project.yaml 的 screenplay.content_rating 自行声明（common.content_boundary）。

子模块：
  llm_client  公共 OpenAI 兼容客户端（config ``llm:`` 段；运镜模式A 之后可复用，
              camera 模块现有 llm_bpy.py 保留不动，迁移路径见 llm_client.py 头注）
  common      时长网格边界/预算可行性/预算行解析/内容边界
  prompts     提示词构造（write/breakdown）
  write       创意/大纲 → 剧本.md（预算可行性 + 结构 lint + 有界退回）
  breakdown   剧本.md → shots/characters/subtitles.yaml（schema 强校验 + 有界退回；
              未配置 LLM 降级为三份人工输入的校验器）
"""
from __future__ import annotations

__all__ = ["SCREENPLAY_STAGES", "ALIAS_STAGES", "run_stage", "selftest_checks"]

#: 段名（cli.py 的 --stage 合法值；"screenplay" 是串联别名，见 ALIAS_STAGES）
SCREENPLAY_STAGES = ("screenplay-write", "breakdown")

ALIAS_STAGES = {"screenplay": ["screenplay-write", "breakdown"]}


def run_stage(stage: str, project_dir, cfg: dict, *, args=None, opts=None, log=print) -> int:
    """段分发。约定与 stages 包一致：返回退出码（0=成功/降级完成；1=失败）。"""
    if stage == "screenplay-write":
        from . import write as _write
        return _write.run(project_dir, cfg, args=args, opts=opts, log=log)
    if stage == "breakdown":
        from . import breakdown as _breakdown
        return _breakdown.run(project_dir, cfg, args=args, opts=opts, log=log)
    raise ValueError("未知 screenplay 段 %r（合法：%s）" % (stage, "|".join(SCREENPLAY_STAGES)))


# --------------------------------------------------------------------------- #
# selftest 注册表（screenplay 不在 stages/ 下，根 selftest.py 的专项节直接调用；
# 检查项全部 dry：不联网（假 chat_fn 注入）、项目一律建在临时目录）
# --------------------------------------------------------------------------- #
def selftest_checks(with_server: bool = False, strict: bool = False) -> list:
    import json
    import os
    from tempfile import TemporaryDirectory
    from types import SimpleNamespace

    from pipeline import config as config_mod, scaffold, schema
    from pipeline.stages import StageOpts
    from . import breakdown as sp_breakdown
    from . import common, llm_client as sp_llm, write as sp_write

    # ------------------------------------------------------------------ #1
    def check_llm_client() -> str:
        assert sp_llm.is_configured({}) is False, "空配置不应判定为已配置"
        assert sp_llm.make_chat_fn({}) is None, "未配置应返回 None（调用方降级）"
        assert sp_llm.make_chat_fn({"llm": {"base_url": "http://x/v1", "model": "m"}}) is None, \
            "缺密钥应返回 None"
        assert sp_llm.api_key({"api_key": "direct", "api_key_env": ""}) == "direct"
        old = os.environ.get("SPTEST_KEY")
        os.environ["SPTEST_KEY"] = "k"
        try:
            fn = sp_llm.make_chat_fn({"llm": {"base_url": "http://127.0.0.1:9/v1",
                                              "model": "m", "api_key_env": "SPTEST_KEY"}})
            assert callable(fn), "三字段齐备 + 环境变量密钥应得到 chat 包装"
            assert sp_llm.api_key({"api_key": "direct", "api_key_env": "SPTEST_KEY"}) == "k", \
                "api_key_env 命中时应优先于直填"
        finally:
            if old is None:
                os.environ.pop("SPTEST_KEY", None)
            else:
                os.environ["SPTEST_KEY"] = old
        assert sp_llm.strip_think("<think>推理</think>\n正文") == "正文"
        assert sp_llm.strip_think("无标签正文") == "无标签正文"
        try:
            sp_llm.chat({}, [])  # 未配置必须抛 LLMError（不联网）
            raise AssertionError("未配置 chat 未抛 LLMError")
        except sp_llm.LLMError as e:
            assert "llm" in str(e)
        try:
            sp_llm._content({"choices": [{"message": {"content": None}}]})
            raise AssertionError("空 content 未报错")
        except sp_llm.LLMError:
            pass
        return "配置判定/密钥优先级/strip_think/未配置报错/空 content 报错（全程不联网）"

    def _cfg_no_llm(pdir):
        """直调用配置：强制关掉 llm 段——已配置 LLM 的机器上降级模式测试也不许触网。"""
        cfg = config_mod.load(str(pdir))
        cfg["llm"] = {"enabled": False}
        return cfg

    # ------------------------------------------------------------------ #2
    def check_write_degraded() -> str:
        with TemporaryDirectory(prefix="sp-selftest-w-") as td:
            pdir = scaffold.new_project("编剧自检剧", "photo", td)
            cfg = _cfg_no_llm(pdir)
            args = SimpleNamespace(premise="深夜便利店，两个陌生成年人因停电相遇",
                                   style_note="都市·微暖·克制", shots=4, total_s=30.0)
            rc = sp_write.run(pdir, cfg, args=args, opts=StageOpts(dry_run=False),
                              chat_fn=None, log=lambda m: None)
            assert rc == 0, "模板模式应成功（rc=0）"
            text = (pdir / "剧本.md").read_text(encoding="utf-8")
            assert "便利店" in text and "### S4" in text and "English anchor" in text
            assert "成年人" in text, "模板应带内容边界默认说明"
            n, t = common.parse_budget_line(text)
            assert (n, t) == (4, 30.0), "预算行应可机读：%r/%r" % (n, t)
            assert not sp_write.lint(text, 4, 30.0), "模板自身应过结构 lint"
            # 预算不可行：4 镜 × 12s < 4×5.17s → 一句报错退出
            bad = sp_write.run(pdir, cfg,
                               args=SimpleNamespace(premise="x", style_note="", shots=4,
                                                    total_s=12.0),
                               opts=StageOpts(dry_run=False), chat_fn=lambda *a, **k: "x",
                               log=lambda m: None)
            assert bad == 1
            return "剧本.md 生成（预算行=4镜×30s 可机读、lint 自洽）；不可行预算 rc=1"

    # ------------------------------------------------------------------ #3
    def check_write_bounded_retry() -> str:
        good = ("## 1. 元信息\n- 预算: 2 镜 × 12s\n\n## 2. 人物设定\n"
                "**甲（成年）** — 描述\nEnglish anchor: `a 30-year-old man`\n\n"
                "## 3. 三幕结构\n- 第一幕 S1–S2\n\n## 4. 分镜表\n"
                "### S1 雨夜（约 6s）\n- 首帧: a\n- 尾帧: b\n- H3: c\n"
                "- 运镜: push_in × slow\n- Audio: rain\n- 字幕：好\n\n"
                "### S2 天明（约 6s）\n- 首帧: a\n- 尾帧: b\n- H3: c\n"
                "- 运镜: locked × slow\n- Audio: birds\n- 字幕：好\n\n"
                "## 5. 技术流水线\n略\n")
        with TemporaryDirectory(prefix="sp-selftest-w2-") as td:
            pdir = scaffold.new_project("退回剧", "photo", td)
            cfg = config_mod.load(str(pdir))
            args = SimpleNamespace(premise="题材", style_note="", shots=2, total_s=12.0)
            calls = {"n": 0}

            def flaky(messages, temperature=None):
                calls["n"] += 1
                return "残缺稿：没有分镜表" if calls["n"] == 1 else good

            rc = sp_write.run(pdir, cfg, args=args, opts=StageOpts(dry_run=False),
                              chat_fn=flaky, log=lambda m: None)
            assert rc == 0 and calls["n"] == 2, "第 2 次应收敛（rc=%s calls=%s）" % (rc, calls["n"])
            assert (pdir / "剧本.md").read_text(encoding="utf-8").startswith("## 1.")

            def always_bad(messages, temperature=None):
                calls["n"] += 1
                return "仍是残稿"

            rc = sp_write.run(pdir, cfg, args=args, opts=StageOpts(dry_run=False),
                              chat_fn=always_bad, log=lambda m: None)
            assert rc == 1, "3 次全坏应非零退出"
            assert (pdir / "剧本.draft.md").is_file(), "最后产物应保留为 draft"
            assert (pdir / "screenplay_review.txt").is_file()
            assert (pdir / "剧本.md").read_text(encoding="utf-8").startswith("## 1."), \
                "失败不应破坏已成功的 剧本.md"
            return "坏→好第 2 次收敛；3 次全坏 rc=1、draft/review 保留、正文不被破坏"

    # ------------------------------------------------------------------ #4
    def check_breakdown_validator() -> str:
        with TemporaryDirectory(prefix="sp-selftest-b1-") as td:
            pdir = scaffold.new_project("校验剧", "photo", td)
            cfg = _cfg_no_llm(pdir)
            args = SimpleNamespace(shots=0, total_s=0.0)
            msgs: list[str] = []
            rc = sp_breakdown.run(pdir, cfg, args=args, opts=StageOpts(dry_run=False),
                                  chat_fn=None, log=msgs.append)
            assert rc == 0 and any("校验通过" in m for m in msgs), msgs
            shotspath = pdir / "shots.yaml"
            good = shotspath.read_text(encoding="utf-8")
            shotspath.write_text(good.replace("move: push_in", "move: warp9"),
                                 encoding="utf-8", newline="\n")
            msgs.clear()
            rc = sp_breakdown.run(pdir, cfg, args=args, opts=StageOpts(dry_run=False),
                                  chat_fn=None, log=msgs.append)
            assert rc == 1, "非法 move 应 rc=1"
            assert any(("camera.move" in m or "词库" in m) for m in msgs), msgs
            shotspath.write_text(good, encoding="utf-8", newline="\n")
            return "合法三件套 rc=0（附总时长汇总）；非法 move rc=1 且错误清单可读"

    # ------------------------------------------------------------------ #5
    def _good_payload() -> dict:
        return {
            "characters": [{
                "id": "chen_ye", "name": "陈野",
                "anchor_en": "a 33-year-old convenience store clerk, short black hair, tired eyes, thin build",
                "outfit_en": "green store apron over a grey hoodie",
                "negative_extra": "", "three_view": True,
                "expressions": ["worried", "smile"], "props": ["flashlight"]}],
            "props_catalog": {"flashlight": "a small orange LED flashlight"},
            "shots": [
                {"title": "停电的便利店", "characters": ["chen_ye"],
                 "scene": "a small convenience store at night, sudden blackout",
                 "scene_zh": "深夜便利店突然停电",
                 "first": "a clerk looks up behind the counter, shelves in darkness",
                 "first_zh": "店员在柜台后抬头，货架陷入黑暗",
                 "last": "he lights a small flashlight, beam cutting across the aisle",
                 "last_zh": "他打开手电，光束扫过过道", "last_mode": "edit",
                 "motion": "[0s-3s] the room goes dark, shelves rattle\n[3s-8s] he raises the flashlight and sweeps the beam",
                 "motion_zh": "[0-3s] 灯灭、货架轻响\n[3-8s] 抬手电扫过过道",
                 "camera_move": "push_in", "camera_intensity": "slow",
                 "audio_hint": "hum stops, plastic rustle", "duration_s": 8.0,
                 "subtitle": "停电了……先把手电找出来。"},
                {"title": "门口的身影", "characters": ["chen_ye"],
                 "scene": "store front door, rain outside", "scene_zh": "店门口，外头下着雨",
                 "first": "a silhouette stands at the glass door, rain streaks behind",
                 "first_zh": "玻璃门外站着一道身影，雨痕横流",
                 "last": "the silhouette steps inside, backlit by streetlight",
                 "last_zh": "身影走进店里，身后是街灯逆光", "last_mode": "edit",
                 "motion": "[0s-3s] rain runs down the glass door\n[3s-7s] the figure pushes the door and steps in",
                 "motion_zh": "[0-3s] 雨水顺玻璃门流下\n[3-7s] 推门进来",
                 "camera_move": "locked", "camera_intensity": "slow",
                 "audio_hint": "rain, door chime", "duration_s": 7.0,
                 "subtitle": "不好意思，能借个光吗？"},
                {"title": "两道光", "characters": ["chen_ye"],
                 "scene": "aisle lit by two flashlight beams", "scene_zh": "两条手电光交错的过道",
                 "first": "two flashlight beams cross in the narrow aisle",
                 "first_zh": "两条光束在窄过道里交错",
                 "last": "the beams settle on the same shelf, hands reaching",
                 "last_zh": "光停在同一排货架，两只手同时伸向它", "last_mode": "edit",
                 "motion": "[0s-3s] beams cross and sway\n[3s-7s] both settle on one shelf",
                 "motion_zh": "[0-3s] 光束交错摇晃\n[3-7s] 同落一排货架",
                 "camera_move": "pan", "camera_intensity": "medium",
                 "audio_hint": "footsteps, breathing", "duration_s": 7.0,
                 "subtitle": "你也来找这个？"},
                {"title": "来电", "characters": ["chen_ye"],
                 "scene": "store lights flicker back on", "scene_zh": "店内灯光重新亮起",
                 "first": "fluorescent lights flicker back to life over the aisle",
                 "first_zh": "荧光灯在过道上方闪着亮起",
                 "last": "two strangers blink at each other in full light",
                 "last_zh": "灯光大亮，两人对视眨眼", "last_mode": "edit",
                 "motion": "[0s-3s] lights flicker rapidly\n[3s-8s] steadying glow, both blink",
                 "motion_zh": "[0-3s] 灯光急闪\n[3-8s] 光稳定，两人眨眼",
                 "camera_move": "pull_out", "camera_intensity": "slow",
                 "audio_hint": "electric buzz returning", "duration_s": 8.0,
                 "subtitle": "……原来长这样。"}]}

    def _fence(payload) -> str:
        return "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"

    def _broken_payload() -> str:
        bad = _good_payload()
        bad["shots"][0]["camera_move"] = "warp9"     # 词库外（schema/build 双拦）
        bad["shots"][1]["motion"] = ""               # 缺 motion
        bad["shots"][2]["first_zh"] = ""             # 缺双语
        bad["characters"][0]["props"].append("not_in_catalog")  # 引用完整性
        return _fence(bad)

    def check_breakdown_llm() -> str:
        with TemporaryDirectory(prefix="sp-selftest-b2-") as td:
            pdir = scaffold.new_project("拆解剧", "photo", td)
            cfg = config_mod.load(str(pdir))
            (pdir / "剧本.md").write_text(
                "## 1. 元信息\n- 预算: 4 镜 × 30s\n\n## 4. 分镜表\n### S1\n### S2\n### S3\n### S4\n",
                encoding="utf-8", newline="\n")
            args = SimpleNamespace(shots=0, total_s=0.0)
            seq = ["not json at all", _broken_payload(), _fence(_good_payload())]
            calls = {"n": 0}

            def flaky(messages, temperature=None):
                i = min(calls["n"], len(seq) - 1)
                calls["n"] += 1
                return seq[i]

            msgs: list[str] = []
            rc = sp_breakdown.run(pdir, cfg, args=args, opts=StageOpts(dry_run=False),
                                  chat_fn=flaky, log=msgs.append)
            assert rc == 0, "第 3 次应收敛（rc=%s，日志尾部：%s）" % (rc, msgs[-3:])
            assert calls["n"] == 3, calls
            data = schema.load_project_data(pdir, cfg)  # 与磁盘终检同款装载
            shots = data["shots"]["shots"]
            assert [sh["id"] for sh in shots] == ["S1", "S2", "S3", "S4"]
            assert all(sh.get("first_zh") and sh.get("motion_zh") for sh in shots), "双语字段在"
            total = sum(schema.eff_duration(sh, data["shots"], cfg) for sh in shots)
            assert total <= 30.0 + 0.71, "总时长 %.2fs 应受预算 30s 约束" % total
            assert (pdir / "shots.yaml.bak").is_file(), "旧版应有 .bak 备份"
            chars = data["characters"]["characters"]
            assert len(chars) == 1 and chars[0]["assets"]["props"] == ["flashlight"]
            cues = data["subtitles"]["cues"]
            assert len(cues) == 4 and cues[0]["shot"] == "S1"
            for c, sh in zip(cues, shots):
                assert c["t_out"] < schema.eff_duration(sh, data["shots"], cfg), "cue 须在镜内"
            # 发射器往返：落盘 yaml 逐字段与构造 doc 等价（隐含于 parse==doc 守卫）
            return ("坏JSON→坏schema→好：第 3 次收敛落盘；schema/双语/预算 30s/备份/cue 镜内全过；"
                    "总时长 %.2fs" % total)

    # ------------------------------------------------------------------ #6
    def check_breakdown_exhausted() -> str:
        with TemporaryDirectory(prefix="sp-selftest-b3-") as td:
            pdir = scaffold.new_project("耗尽剧", "photo", td)
            cfg = config_mod.load(str(pdir))
            (pdir / "剧本.md").write_text("## 1. 元信息\n- 预算: 4 镜 × 30s\n",
                                          encoding="utf-8", newline="\n")
            before = (pdir / "shots.yaml").read_text(encoding="utf-8")
            args = SimpleNamespace(shots=0, total_s=0.0)

            def always_bad(messages, temperature=None):
                return "还是不是 JSON"

            rc = sp_breakdown.run(pdir, cfg, args=args, opts=StageOpts(dry_run=False),
                                  chat_fn=always_bad, log=lambda m: None)
            assert rc == 1, "3 次全坏应 rc=1"
            assert (pdir / "breakdown_last_output.txt").is_file()
            assert (pdir / "screenplay_review.txt").is_file()
            assert not (pdir / "shots.draft.yaml").is_file(), "无成型 docs 不写 draft"
            assert (pdir / "shots.yaml").read_text(encoding="utf-8") == before, "失败不得动正本"

            # 预算不可行（4 镜最短 ≈20.67s > 18s+容差）经有界退回进 review：
            # 剧本不写预算行、只给 --total-s=18（n_req=0 → 走 fit_durations 反馈路径）
            (pdir / "剧本.md").write_text("## 1. 元信息\n（无预算行）\n",
                                          encoding="utf-8", newline="\n")

            def always_over(messages, temperature=None):
                return _fence(_good_payload())  # 4 镜 × 15s，超预算

            msgs2: list[str] = []
            rc = sp_breakdown.run(pdir, cfg, args=SimpleNamespace(shots=0, total_s=18.0),
                                  opts=StageOpts(dry_run=False), chat_fn=always_over,
                                  log=msgs2.append)
            assert rc == 1
            review = (pdir / "screenplay_review.txt").read_text(encoding="utf-8")
            assert "预算不可行" in review, review
            return "3 次非 JSON：rc=1、raw/review 保留、正本不动；预算不可行进退回清单"

    return [
        {"id": "llm_client", "title": "配置判定/密钥优先级/未配置报错（不联网）",
         "level": "fatal", "run": check_llm_client},
        {"id": "write-degraded", "title": "模板模式生成 剧本.md（预算行可机读）",
         "level": "fatal", "run": check_write_degraded},
        {"id": "write-retry", "title": "结构 lint 有界退回（3 次上限，draft 保留）",
         "level": "fatal", "run": check_write_bounded_retry},
        {"id": "breakdown-validator", "title": "未配置 LLM 的三份输入校验器",
         "level": "fatal", "run": check_breakdown_validator},
        {"id": "breakdown-llm", "title": "schema 强校验 + 有界退回 + 落盘终检",
         "level": "fatal", "run": check_breakdown_llm},
        {"id": "breakdown-exhausted", "title": "耗尽路径与预算不可行反馈",
         "level": "fatal", "run": check_breakdown_exhausted},
    ]
