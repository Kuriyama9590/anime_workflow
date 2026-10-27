# -*- coding: utf-8 -*-
"""ComfyUI HTTP 客户端（仅标准库）。

升级自 _tools/comfy_client.py（docs §0 映射表 A 级：queue → 轮询 history → /view 下载
回路经 test_s1.py 与关键帧批次实际使用）。语义保持一致，仅做：
  * server/client_id/轮询间隔/超时可配置（来自 config.yaml）
  * 提交失败的错误信息带 /prompt 返回的 error + node_errors
  * 新增 system_stats（预检）/ object_info（doctor/节点检查）/ queue 快照（孤儿收敛）
不改动 A 级回路的行为（wait 的完成/出错判定与 comfy_client.py:23-38 一致）。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

__all__ = ["ComfyError", "ComfySubmissionError", "ComfyClient", "build_client"]


class ComfyError(RuntimeError):
    pass


class ComfySubmissionError(ComfyError):
    pass


class ComfyClient:
    def __init__(self, server: str, client_id: str = "pipeline",
                 poll_interval_s: float = 3, timeout_img_s: float = 900,
                 timeout_video_s: float = 3600):
        self.server = server.rstrip("/")
        self.client_id = client_id
        self.poll_interval_s = max(0.2, float(poll_interval_s))
        self.timeout_img_s = float(timeout_img_s)
        self.timeout_video_s = float(timeout_video_s)

    # ---- 基础 HTTP ------------------------------------------------------- #
    def get_bytes(self, path: str, timeout: float = 300) -> bytes:
        url = self.server + path
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read()

    def get_json(self, path: str, timeout: float = 300):
        return json.loads(self.get_bytes(path, timeout).decode("utf-8"))

    def post_json(self, path: str, payload: dict, timeout: float = 60):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.server + path, data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    # ---- 状态端点 --------------------------------------------------------- #
    def system_stats(self) -> dict:
        return self.get_json("/system_stats", timeout=5)

    def reachable(self) -> bool:
        try:
            self.system_stats()
            return True
        except Exception:
            return False

    def object_info(self) -> dict:
        return self.get_json("/object_info", timeout=30)

    def queue_snapshot(self) -> dict:
        """GET /queue → {"queue_running": [[n, pid, …]], "queue_pending": […]}。"""
        return self.get_json("/queue", timeout=10)

    def prompt_in_queue(self, pid: str) -> bool:
        try:
            snap = self.queue_snapshot()
        except Exception:
            return False
        for key in ("queue_running", "queue_pending"):
            for item in snap.get(key) or []:
                if isinstance(item, (list, tuple)) and len(item) > 1 and item[1] == pid:
                    return True
        return False

    # ---- 提交与轮询 ------------------------------------------------------- #
    def queue_prompt(self, prompt_graph: dict) -> str:
        """POST /prompt；失败时抛 ComfySubmissionError（含 error/node_errors 摘要）。"""
        try:
            resp = self.post_json("/prompt", {"prompt": prompt_graph, "client_id": self.client_id})
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:2000]
            raise ComfySubmissionError("提交失败 HTTP %s: %s" % (e.code, body)) from e
        except urllib.error.URLError as e:
            raise ComfySubmissionError("无法连接 %s：%s" % (self.server, e.reason)) from e
        if "error" in resp:
            node_errors = resp.get("node_errors") or {}
            nodes = ",".join(str(k) for k in node_errors)[:200]
            raise ComfySubmissionError("ComfyUI 拒绝工作流: %s%s"
                                       % (json.dumps(resp["error"], ensure_ascii=False)[:800],
                                          ("（节点错误: %s…）" % nodes) if node_errors else ""))
        pid = resp.get("prompt_id")
        if not pid:
            raise ComfySubmissionError("/prompt 未返回 prompt_id: %r" % resp)
        return pid

    def history(self, pid: str) -> dict:
        return self.get_json("/history/" + urllib.parse.quote(pid))

    def wait(self, pid: str, timeout_s: float = 900, quiet_first_s: float = 5) -> dict:
        """轮询 history 直到完成；执行错误/超时抛异常（语义同 comfy_client.wait）。"""
        t0 = time.time()
        if quiet_first_s:
            time.sleep(quiet_first_s)
        while time.time() - t0 < timeout_s:
            try:
                h = self.history(pid)
            except Exception:
                h = {}
            if pid in h:
                entry = h[pid]
                status = entry.get("status", {})
                if status.get("completed") is True or status.get("status_str") == "success":
                    return entry
                if status.get("status_str") == "error":
                    raise ComfyError("执行错误: " + json.dumps(status, ensure_ascii=False)[:4000])
            time.sleep(self.poll_interval_s)
        raise TimeoutError("prompt %s 超时（%ss）" % (pid, timeout_s))

    # ---- 下载 ------------------------------------------------------------- #
    def _view_url(self, item: dict) -> str:
        q = urllib.parse.urlencode({"filename": item.get("filename", ""),
                                    "subfolder": item.get("subfolder", ""),
                                    "type": item.get("type", "output")})
        return "/view?" + q

    def download_images(self, history_entry: dict, dest_dir, prefix: str | None = None,
                        dest_name: str | None = None) -> list:
        """下载 outputs 里的全部图片到 dest_dir。

        prefix 模式命名 `<prefix>_%02d.<ext>`（comfy_client.download_images 同款）；
        dest_name 模式取第一张按精确文件名落盘（render_keyframes.run 的用法）。
        """
        import os
        os.makedirs(dest_dir, exist_ok=True)
        out = []
        for _nid, node in (history_entry.get("outputs") or {}).items():
            for img in node.get("images") or []:
                raw = self.get_bytes(self._view_url(img))
                if dest_name is not None and not out:
                    name = dest_name
                else:
                    name = "%s_%02d.%s" % (prefix or "img", len(out) + 1,
                                           str(img.get("filename", "")).rsplit(".", 1)[-1])
                p = os.path.join(str(dest_dir), name)
                with open(p, "wb") as f:
                    f.write(raw)
                out.append(p)
        return out

    def download_media(self, history_entry: dict, dest) -> str | None:
        """下载 outputs 里第一个视频类条目到 dest（render_videos.download_media 同款）。"""
        import os
        for _nid, node in (history_entry.get("outputs") or {}).items():
            for key in ("videos", "images", "gifs", "audio"):
                for item in node.get(key) or []:
                    if isinstance(item, dict) and "filename" in item and \
                            str(item["filename"]).lower().endswith((".mp4", ".webm", ".mkv", ".mov")):
                        raw = self.get_bytes(self._view_url(item))
                        with open(str(dest), "wb") as f:
                            f.write(raw)
                        return str(dest)
        return None


def build_client(cfg: dict) -> ComfyClient:
    """从合并后配置构造客户端。"""
    comfy = cfg.get("comfy") or {}
    return ComfyClient(
        server=str(comfy.get("server", "http://127.0.0.1:8188")),
        client_id=str(comfy.get("client_id", "pipeline")),
        poll_interval_s=float(comfy.get("poll_interval_s", 3)),
        timeout_img_s=float(comfy.get("timeout_img_s", 900)),
        timeout_video_s=float(comfy.get("timeout_video_s", 3600)),
    )
