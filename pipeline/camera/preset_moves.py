# -*- coding: utf-8 -*-
"""模式A 运动预设：推/拉/摇/移/跟/环绕/2.5D 视差的参数化相机轨迹（纯 Python，无 bpy 依赖）。

坐标系（与 codegen 生成的 bpy 脚本一致）：
  - 首帧图贴在 XY 平面（z=0，法线 +Z）的细分网格上，U→+X、V→+Y；
  - 相机位于 +Z，rotation_euler=(0,0,0) 时视向 (0,0,-1)（Blender 相机沿本地 -Z 看），
    屏幕右=+X、屏幕上=+Y，图像正立；
  - 到目标的 look-at 用 rx=acos(-dz), rz=atan2(-dx,dy)（XYZ 欧拉，已推导验证）。

对外入口：
  build_track(move, params, ...) -> {"move","params","keys"}     每帧相机位姿
  make_spec(...) -> dict                                          完整 spec JSON（codegen 输入）
  MOVES / DEFAULT_PARAMS                                          预设清单与默认参数

可离线单测：不 import bpy、不依赖 Blender 安装。
"""

from __future__ import annotations

import math
import os

# ---------------------------------------------------------------- 预设清单

#: 支持的运镜（模式A）。move 名与模式B词库 vocab.yaml 的 moves 键保持一致
MOVES = ("push_in", "pull_out", "pan", "truck", "follow", "orbit", "parallax")

DEFAULT_PARAMS: dict[str, dict] = {
    # 推（dolly in）：strength=距离缩短比例
    "push_in":   {"strength": 0.35},
    # 拉（dolly out）
    "pull_out":  {"strength": 0.35},
    # 摇（rotate in place）：axis=horizontal|vertical，yaw_deg=总摆角
    "pan":       {"yaw_deg": 12.0, "direction": 1, "axis": "horizontal"},
    # 移（lateral truck）：shift_frac=相对基准可视宽度的平移比例
    "truck":     {"shift_frac": 0.12, "direction": 1},
    # 跟（follow drift）：横漂+缓推+轻微回摆
    "follow":    {"shift_frac": 0.10, "push_frac": 0.15, "yaw_deg": 6.0, "direction": 1},
    # 环绕（orbit）：绕画面中心水平圆周，look-at 中心
    "orbit":     {"orbit_deg": 25.0, "radius_frac": 0.5, "direction": 1},
    # 2.5D 视差（双平面）：小横移 + 背景板深度分离（backdrop 参数在 make_spec 层）
    "parallax":  {"shift_frac": 0.08, "direction": 1},
}

EASINGS = ("linear", "smooth", "ease_in", "ease_out")

_INT_PARAMS = {"direction"}


def _ease(name: str, t: float) -> float:
    t = min(1.0, max(0.0, t))
    if name == "linear":
        return t
    if name == "ease_in":
        return t * t
    if name == "ease_out":
        return 1.0 - (1.0 - t) ** 2
    return t * t * (3.0 - 2.0 * t)  # smooth（默认，smoothstep）


def _apply_defaults(move: str, params: dict | None) -> dict:
    if move not in DEFAULT_PARAMS:
        raise ValueError("未知运镜 %r，可选：%s" % (move, ", ".join(MOVES)))
    merged = dict(DEFAULT_PARAMS[move])
    if params:
        unknown = set(params) - set(DEFAULT_PARAMS[move])
        if unknown:
            raise ValueError("运镜 %r 不接受参数：%s（可用：%s）"
                             % (move, sorted(unknown), sorted(DEFAULT_PARAMS[move])))
        merged.update(params)
    for k in merged:
        if k == "axis":
            if merged[k] not in ("horizontal", "vertical"):
                raise ValueError("pan.axis 只能是 horizontal|vertical")
        elif k in _INT_PARAMS:
            merged[k] = int(merged[k])
        else:
            merged[k] = float(merged[k])
    return merged


def _look_at_z_down(pos: tuple[float, float, float]) -> tuple[float, float, float]:
    """相机在 +Z 一侧看向原点的欧拉角（XYZ）。直视= (0,0,0)。"""
    dx, dy, dz = -pos[0], -pos[1], -pos[2]  # pos→(0,0,0) 方向
    norm = math.sqrt(dx * dx + dy * dy + dz * dz)
    if norm < 1e-9:
        return (0.0, 0.0, 0.0)
    dx, dy, dz = dx / norm, dy / norm, dz / norm
    rx = math.acos(min(1.0, max(-1.0, -dz)))
    rz = math.atan2(-dx, dy) if abs(dx) + abs(dy) > 1e-12 else 0.0
    return (rx, 0.0, rz)


# ---------------------------------------------------------------- 轨迹构建

def build_track(move: str, params: dict | None = None, *, frame_start: int = 1,
                frame_end: int = 124, lens: float = 50.0, sensor_width: float = 36.0,
                base_distance: float = 3.0, easing: str = "smooth") -> dict:
    """生成逐帧相机位姿序列。

    返回 {"move", "params", "keys"}；keys 每项
    {"frame","location":[x,y,z],"rotation":[rx,ry,rz],"lens"}（角度弧度，值保留 6 位）。
    平移量以“基准可视宽度”W0 = 2·d0·tan(hfov/2) 为尺度，跨分辨率/焦距可比。
    """
    if frame_end <= frame_start:
        raise ValueError("frame_end 必须大于 frame_start")
    if easing not in EASINGS:
        raise ValueError("未知缓动 %r，可选：%s" % (easing, ", ".join(EASINGS)))
    p = _apply_defaults(move, params)

    hfov = 2.0 * math.atan(sensor_width / (2.0 * lens))
    w0 = 2.0 * base_distance * math.tan(hfov / 2.0)
    d0 = float(base_distance)
    n_frames = frame_end - frame_start + 1
    keys = []

    for idx in range(n_frames):
        frame = frame_start + idx
        u_raw = idx / (n_frames - 1) if n_frames > 1 else 0.0
        u = _ease(easing, u_raw)

        if move == "push_in":
            dist = d0 * (1.0 - p["strength"] * u)
            loc, rot = (0.0, 0.0, dist), (0.0, 0.0, 0.0)

        elif move == "pull_out":
            dist = d0 * (1.0 - p["strength"] * (1.0 - u))
            loc, rot = (0.0, 0.0, dist), (0.0, 0.0, 0.0)

        elif move == "pan":
            a = math.radians(p["yaw_deg"]) * 0.5
            sweep = (-a + 2.0 * a * u) * p["direction"]
            loc = (0.0, 0.0, d0)
            if p["axis"] == "horizontal":   # 绕 Y 轴：画面横向扫
                rot = (0.0, sweep, 0.0)
            else:                            # 绕 X 轴：画面纵向扫
                rot = (sweep, 0.0, 0.0)

        elif move == "truck":
            s = p["shift_frac"] * w0
            x = (-s / 2.0 + s * u) * p["direction"]
            loc, rot = (x, 0.0, d0), (0.0, 0.0, 0.0)

        elif move == "follow":
            s = p["shift_frac"] * w0
            x = (-s / 2.0 + s * u) * p["direction"]
            z = d0 * (1.0 - p["push_frac"] * u)
            ry = -math.radians(p["yaw_deg"]) * u * p["direction"]
            loc, rot = (x, 0.0, z), (0.0, ry, 0.0)

        elif move == "orbit":
            radius = d0 * p["radius_frac"]
            hz = math.sqrt(max(d0 * d0 - radius * radius, (0.25 * d0) ** 2))
            half = math.radians(p["orbit_deg"]) * 0.5
            theta = -half + 2.0 * half * u
            if p["direction"] < 0:
                theta = -theta
            pos = (radius * math.sin(theta), radius * math.cos(theta), hz)
            loc, rot = pos, _look_at_z_down(pos)

        else:  # parallax（小横移；深度分离由场景层双平面承担）
            s = p["shift_frac"] * w0
            x = (-s / 2.0 + s * u) * p["direction"]
            loc, rot = (x, 0.0, d0), (0.0, 0.0, 0.0)

        keys.append({
            "frame": frame,
            "location": [round(v, 6) for v in loc],
            "rotation": [round(v, 6) for v in rot],
            "lens": round(float(lens), 4),
        })

    return {"move": move, "params": p, "keys": keys}


# ---------------------------------------------------------------- 平面包围盒

def plane_half_bounds(keys: list[dict], *, width: int, height: int, lens: float,
                      sensor_width: float, overscan: float) -> tuple[float, float]:
    """按逐帧视锥保守估计画面平面所需半宽/半高（含 overscan 余量）。

    对每帧：half_w = |px| + pz·tan(hfov/2 + |ry|)，half_h = |py| + pz·tan(vfov/2 + |rx|)；
    相机倾斜角（来自 look-at/pan）一并计入，防止摇/环绕时看到平面边缘（穿帮）。
    """
    aspect = float(height) / float(width)
    hfov = 2.0 * math.atan(sensor_width / (2.0 * lens))
    vfov = 2.0 * math.atan(math.tan(hfov / 2.0) * aspect)
    max_w = max_h = 1e-6
    for k in keys:
        px, py, pz = k["location"]
        rx, ry, _ = k["rotation"]
        if pz <= 0.01:
            raise ValueError("相机 z 必须 > 0.01（平面在 z=0，相机在其上方）")
        ang_w = hfov / 2.0 + min(abs(ry), 1.2)
        ang_h = vfov / 2.0 + min(abs(rx), 1.2)
        hw = abs(px) + pz * math.tan(ang_w)
        hh = abs(py) + pz * math.tan(ang_h)
        max_w, max_h = max(max_w, hw), max(max_h, hh)
    return round(max_w * overscan, 6), round(max_h * overscan, 6)


# ---------------------------------------------------------------- spec 组装

def make_spec(shot: str, move: str, image_first: str, image_last: str | None = None, *,
              params: dict | None = None, duration_s: float = 5.17, fps: int = 24,
              width: int = 1344, height: int = 768, frame_start: int = 1,
              samples: int = 16, overscan: float = 1.08, subdivisions: int = 96,
              displacement_strength: float = 0.012, easing: str = "smooth",
              lens: float = 50.0, sensor_width: float = 36.0, base_distance: float = 3.0,
              out_dir: str = ".", frame_prefix: str = "frame") -> dict:
    """组装完整 spec JSON（codegen.generate_bpy 的输入）。

    分辨率/帧率/采样/细分/位移强度全部可配（任务要求“分辨率帧率可配”）。
    image_last 提供时：近平面材质做 first→last 逐帧 crossfade（Ken Burns 式端点过渡），
    endpoints 模式即“首帧图起幅、末帧图落幅”的两张端点图。
    """
    if width <= 0 or height <= 0 or fps <= 0:
        raise ValueError("width/height/fps 必须为正")
    if not image_first or not os.path.isfile(image_first):
        raise FileNotFoundError("首帧图不存在：%r" % image_first)
    if image_last and not os.path.isfile(image_last):
        raise FileNotFoundError("末帧图不存在：%r" % image_last)

    n_frames = max(2, round(float(duration_s) * fps))
    frame_end = frame_start + n_frames - 1
    track = build_track(move, params, frame_start=frame_start, frame_end=frame_end,
                        lens=lens, sensor_width=sensor_width,
                        base_distance=base_distance, easing=easing)
    half_x, half_y = plane_half_bounds(
        track["keys"], width=width, height=height, lens=lens,
        sensor_width=sensor_width, overscan=overscan)

    # 背景板（双平面 2.5D）：parallax 必开；其余 move 关（crossfade 与 backdrop 互斥使用）
    backdrop = {"enabled": move == "parallax", "distance": 1.8, "scale": 1.4, "tint": 0.35}
    if backdrop["enabled"]:
        need = (base_distance + backdrop["distance"]) / base_distance * 1.05
        backdrop["scale"] = max(backdrop["scale"], need)

    def fwd(p: str | None) -> str | None:
        return os.path.abspath(p).replace("\\", "/") if p else None

    return {
        "shot": str(shot),
        "move": move,
        "params": track["params"],
        "generator": "pipeline.camera.preset_moves",
        "render": {
            "engine": "eevee", "samples": int(samples),
            "width": int(width), "height": int(height), "fps": int(fps),
            "frame_start": int(frame_start), "frame_end": int(frame_end),
            "overscan": float(overscan),
        },
        "scene": {
            "images": {"first": fwd(image_first), "last": fwd(image_last)},
            "plane_half": [half_x, half_y],
            "subdivisions": int(subdivisions),
            "displacement_strength": float(displacement_strength),
            "backdrop": backdrop,
        },
        "camera_track": {
            "easing": easing, "lens": float(lens),
            "sensor_width": float(sensor_width),
            "keys": track["keys"],
        },
        "output": {
            "dir": fwd(out_dir),
            "endpoints": ["%s_camfirst.png" % shot, "%s_camlast.png" % shot],
            "frame_prefix": frame_prefix,
        },
    }
