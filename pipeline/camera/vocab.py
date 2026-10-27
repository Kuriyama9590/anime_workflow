# -*- coding: utf-8 -*-
"""模式B 运镜提示词词库：vocab.yaml 加载 + camdesc 组装 + shots.yaml 批处理。

词库（camera/vocab.yaml）按 move × intensity 存英文短语；组装规则：
  Camera: <短语>[, <extra>]           # extra 为 shots.yaml 单镜自由补充
产物一行文本写 <outdir>/<shot>_camdesc.txt，由 video 段拼进 H3 提示词
（设计文档 §4.3 模式B：零 Blender 依赖，首尾帧直通 H3）。

YAML 解析**一律走框架级 pipeline/miniyaml**（§2.3 受限子集：块/flow 风格、
``|``/``>`` 多行块、引号标量、注释），与脚手架生成的规范 shots.yaml 严格兼容。
历史备注：本模块早期自带一个更窄的私有解析器（不支持 ``|`` 块与 flow 映射），
在规范格式上必崩，复审后已删除并统一到 miniyaml——不要回退。
"""

from __future__ import annotations

import os

from pipeline import miniyaml

_HERE = os.path.dirname(os.path.abspath(__file__))
VOCAB_PATH = os.path.join(_HERE, "vocab.yaml")


class YamlError(ValueError):
    """YAML 读取/结构错误（message 带文件与行号，若有）。"""


def _wrap(err: Exception, source: str) -> YamlError:
    return YamlError("%s: %s" % (source, err))


def parse(text: str, source: str = "<yaml>"):
    """解析 YAML 文本（经 miniyaml，支持脚手架规范格式的全部语法）。"""
    try:
        return miniyaml.parse(text)
    except miniyaml.MiniYAMLError as e:
        raise _wrap(e, source) from e


# ---------------------------------------------------------------- 词库

def load_vocab(path: str | None = None) -> dict:
    p = path or VOCAB_PATH
    try:
        data = miniyaml.load(p)
    except miniyaml.MiniYAMLError as e:
        raise _wrap(e, p) from e
    if not isinstance(data, dict) or not isinstance(data.get("moves"), dict):
        raise YamlError("%s: 缺 moves 映射" % p)
    return data


def phrase(move: str, intensity: str = "medium", vocab: dict | None = None) -> str:
    v = vocab or load_vocab()
    moves = v["moves"]
    if move not in moves:
        raise KeyError("词库无运镜 %r；可用：%s" % (move, ", ".join(sorted(moves))))
    entries = moves[move]
    if intensity not in entries:
        raise KeyError("运镜 %r 无强度 %r；可用：%s"
                       % (move, intensity, ", ".join(sorted(entries))))
    return entries[intensity]


def build_camdesc(move: str, intensity: str = "medium", extra: str = "",
                  vocab: dict | None = None) -> str:
    """组装一行 Camera 提示词（模式B 唯一产物内容）。"""
    text = "Camera: " + phrase(move, intensity, vocab)
    if extra and extra.strip():
        text += ", " + extra.strip()
    return text


# ---------------------------------------------------------------- shots.yaml 批处理

def _as_map(value, where: str) -> dict:
    """防御：规范 shots.yaml 里该处应是映射；给可读错误而不是裸 AttributeError。"""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise YamlError("%s 应为映射，实得 %s：%r" % (where, type(value).__name__, value))
    return value


def camdesc_from_shots(shots_yaml: str, out_dir: str, log=print) -> list[tuple[str, str]]:
    """按 shots.yaml 批量生成 <out_dir>/<shot>_camdesc.txt。

    只处理 camera.mode 为 B 或缺省（缺省=defaults.camera.mode 或 "B"）的镜；
    单镜字段 > shots.defaults.camera > 词库默认（与框架取值优先级一致，§4.0）。
    返回 [(shot_id, camdesc_path), ...]。
    """
    try:
        data = miniyaml.load(shots_yaml)
    except miniyaml.MiniYAMLError as e:
        raise _wrap(e, shots_yaml) from e
    data = _as_map(data, shots_yaml)
    shots = data.get("shots")
    if not isinstance(shots, list) or not shots:
        raise YamlError("%s: 缺非空 shots 列表" % shots_yaml)
    defaults_cam = _as_map(_as_map(data.get("defaults"), "%s: defaults" % shots_yaml)
                           .get("camera"), "%s: defaults.camera" % shots_yaml)
    vocab_data = load_vocab()
    os.makedirs(out_dir, exist_ok=True)
    results = []
    for index, entry in enumerate(shots):
        entry = _as_map(entry, "%s: shots[%d]" % (shots_yaml, index))
        if "id" not in entry:
            raise YamlError("%s: shots[%d] 缺 id：%r" % (shots_yaml, index, entry))
        sid = str(entry["id"])
        cam = _as_map(entry.get("camera"), "%s: %s.camera" % (shots_yaml, sid))
        mode = cam.get("mode", defaults_cam.get("mode", "B"))
        if str(mode).upper() != "B":
            log("%s camera.mode=%s 跳过（模式A 由 Blender 流程处理）" % (sid, mode))
            continue
        move = cam.get("move", defaults_cam.get("move", "push_in"))
        intensity = cam.get("intensity",
                            defaults_cam.get("intensity",
                                             vocab_data.get("default_intensity", "medium")))
        extra = str(cam.get("extra", defaults_cam.get("extra", "")) or "")
        text = build_camdesc(str(move), str(intensity), extra, vocab_data)
        path = os.path.join(out_dir, "%s_camdesc.txt" % sid)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text + "\n")
        results.append((sid, path))
        log("%s -> %s | %s" % (sid, path, text))
    return results
