# -*- coding: utf-8 -*-
"""便携 Blender：下载 / 解压 / 发现 / headless 执行（仅标准库）。

依据 docs/FRAMEWORK_DESIGN.md §2.1 camera.blender 与 §4.3 模式A：
  - 免管理员安装：Windows x64 zip → Python ``zipfile`` 解压（已实测 GNU tar 1.35
    不解 zip、tarfile 抛 ReadError，故不用 tar 系，见 §9 环境事实表）；
  - 下载源优先南京大学镜像 mirror.nju.edu.cn/blender/release/，回退
    download.blender.org/release/（两源 2026-10-26 curl -sI 均核实 200）；
  - 解压后 blender.exe 直接位于 install_dir 根（zip 内唯一顶层目录被剥掉），
    与设计文档 selftest 第 12 项 “install_dir/blender.exe” 检查一致；
  - 版本默认 4.2（LTS；4.2–4.5 引擎枚举为 BLENDER_EEVEE_NEXT，codegen 用双枚举
    try/except 兼容），具体小版本号自动解析镜像目录下最新 4.2.x。

对外入口：
  ensure_installed()   装+验一步到位（已装且 --version 通过则直接复用）
  install()            强制/全新安装
  find_blender()       发现（env 覆盖 > install_dir > PATH）
  run()                headless 执行（列表参数、超时杀进程、UTF-8 安全捕获）
  get_version()        解析 --version 首行
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile

# ---------------------------------------------------------------- 常量与路径

#: 镜像列表：优先南京大学镜像（国内带宽），官方源兜底
MIRRORS = [
    "https://mirror.nju.edu.cn/blender/release/",
    "https://download.blender.org/release/",
]

#: 默认大版本（LTS 线）。小版本号自动取该线下最新
DEFAULT_VERSION_LINE = "4.2"

#: 环境变量：显式指定 blender.exe（跳过发现/安装逻辑）
ENV_BLENDER = "PIPELINE_BLENDER"

_ZIP_RE = re.compile(r"blender-(\d+)\.(\d+)\.(\d+)-windows-x64\.zip")
_DOWNLOAD_CHUNK = 1 << 20          # 1 MiB
_PROGRESS_EVERY_MB = 32            # 每 32MB 打一行进度


def repo_root() -> str:
    """仓库根 = pipeline 包父目录（config.py 同款锚定法，§2.1 paths 注）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def repo_fallback_install_dir() -> str:
    """仓库内兜底安装目录 <仓库根>/_tools/blender（本模块任务指定位置；
    config 未声明 camera.blender.install_dir 时使用）。"""
    return os.path.join(repo_root(), "_tools", "blender")


#: config 解析结果缓存（None=无配置值或读取失败），避免每次调用重读 config.yaml
_config_install_dir_cache: str | None = None


def configured_install_dir() -> str | None:
    """读全局配置 pipeline/config.yaml 的 camera.blender.install_dir（§2.1 契约）。

    未声明/配置不可读一律返回 None（调用方落到仓库内兜底），不让配置故障
    阻塞 Blender 发现；结果缓存。显式路径优先级见 find_blender。
    """
    global _config_install_dir_cache
    if _config_install_dir_cache is not None:
        return _config_install_dir_cache
    value = None
    try:
        from pipeline import config as _config  # 局部导入：config 故障不拖垮本模块
        cfg = _config.load_global()
        v = ((cfg.get("camera") or {}).get("blender") or {}).get("install_dir")
        if isinstance(v, str) and v.strip():
            value = str(_config.resolve_path(v.strip()))
    except Exception:  # noqa: BLE001 - 配置缺失/坏格式 → 兜底，不阻塞
        value = None
    _config_install_dir_cache = value or ""
    return _config_install_dir_cache or None


def default_install_dir() -> str:
    """安装目标目录：config camera.blender.install_dir（有则用，框架契约）→
    仓库内兜底 <仓库根>/_tools/blender（任务指定）。发现逻辑会同时搜两处。"""
    return configured_install_dir() or repo_fallback_install_dir()


# ---------------------------------------------------------------- 下载

def _http_get_bytes(url: str, timeout_s: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "pipeline-blender-installer/0.1"})
    with urllib.request.urlopen(req, timeout=timeout_s) as r:
        return r.read()


def resolve_zip_url(version_line: str = DEFAULT_VERSION_LINE) -> tuple[str, str]:
    """在镜像目录里解析该版本线最新的 Windows x64 zip。

    返回 (url, 版本号)；逐镜像尝试，全部失败抛 RuntimeError（带每个镜像的失败原因）。
    """
    errors = []
    for base in MIRRORS:
        listing_url = base + "Blender%s/" % version_line
        try:
            html = _http_get_bytes(listing_url).decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001 - 逐镜像收集失败原因
            errors.append("%s -> %s" % (listing_url, e))
            continue
        best = None  # (version_tuple, zipname)
        for m in _ZIP_RE.finditer(html):
            vt = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if best is None or vt > best[0]:
                best = (vt, m.group(0))
        if best:
            return listing_url + best[1], "%d.%d.%d" % best[0]
        errors.append("%s -> 目录里没有匹配的 windows-x64 zip" % listing_url)
    raise RuntimeError("所有镜像解析失败：\n  " + "\n  ".join(errors))


def _download_resume(url: str, dest: str, log=print) -> str:
    """流式下载，支持断点续传（HTTP Range）。完成后返回 dest。"""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    partial = os.path.getsize(dest) if os.path.exists(dest) else 0

    headers = {"User-Agent": "pipeline-blender-installer/0.1"}
    if partial:
        headers["Range"] = "bytes=%d-" % partial
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        status = getattr(r, "status", r.getcode())
        remaining = int(r.headers.get("Content-Length") or 0)
        resumed = partial and status == 206
        if partial and not resumed:
            partial = 0  # 服务器不支持 Range，重头来
        expect = (partial + remaining) if (resumed and remaining) else remaining
        if resumed:
            log("续传 %s：已有 %.1f MB" % (os.path.basename(dest), partial / 1e6))
        mode = "ab" if resumed else "wb"
        done_mb = partial / 1e6
        next_report = done_mb + _PROGRESS_EVERY_MB
        with open(dest, mode) as f:
            while True:
                chunk = r.read(_DOWNLOAD_CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                done_mb += len(chunk) / 1e6
                if done_mb >= next_report:
                    total_mb = expect / 1e6 if expect else float("inf")
                    log("下载中 %.0f/%.0f MB" % (done_mb, total_mb))
                    next_report = done_mb + _PROGRESS_EVERY_MB
    size = os.path.getsize(dest)
    if expect and size != expect:
        raise RuntimeError("下载不完整：%s 实际 %d 字节，期望 %d（重跑可断点续传）"
                           % (dest, size, expect))
    log("下载完成：%s（%.1f MB）" % (dest, size / 1e6))
    return dest


# ---------------------------------------------------------------- 解压

def _extract_zip_strip_top(zip_path: str, dest_dir: str, log=print) -> int:
    """zipfile 解压并剥掉唯一顶层目录，使 blender.exe 落在 dest_dir 根。

    返回解压文件数。zip 内顶层目录不唯一时抛 RuntimeError（防止解出一堆散文件）。
    """
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        tops = {n.split("/", 1)[0] for n in names if n.strip("/")}
        if len(tops) != 1:
            raise RuntimeError("zip 顶层目录不唯一，拒绝解压：%s" % sorted(tops)[:5])
        top = next(iter(tops)) + "/"
        n_files = 0
        t0 = time.time()
        for info in zf.infolist():
            if info.is_dir():
                continue
            rel = info.filename
            if rel.startswith(top):
                rel = rel[len(top):]
            if not rel or ".." in rel.replace("\\", "/").split("/"):
                continue  # 防路径穿越
            target = os.path.join(dest_dir, *rel.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, _DOWNLOAD_CHUNK)
            n_files += 1
            if n_files % 2000 == 0:
                log("解压中 %d 个文件…" % n_files)
        log("解压完成：%d 个文件，%.1fs" % (n_files, time.time() - t0))
    return n_files


# ---------------------------------------------------------------- 发现/校验/执行

def _search_root(root: str) -> str | None:
    """在单个安装根下找 blender.exe：根直下 → 一级子目录。"""
    direct = os.path.join(root, "blender.exe")
    if os.path.isfile(direct):
        return direct
    if os.path.isdir(root):
        for entry in sorted(os.listdir(root)):
            cand = os.path.join(root, entry, "blender.exe")
            if os.path.isfile(cand):
                return cand
    return None


def find_blender(install_dir: str | None = None) -> str | None:
    """发现 blender.exe：env 覆盖 > install_dir（显式传参时只搜该处）>
    [config 声明的 install_dir、仓库内兜底 _tools/blender] 逐处 > PATH。

    显式传 install_dir 语义不变（只搜该根）；不传时把 config 契约位置与
    本模块任务指定的仓库内位置都搜一遍——config 指到别处时不会漏掉已装副本。
    找不到返回 None。
    """
    explicit = os.environ.get(ENV_BLENDER)
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    if install_dir:
        return _search_root(install_dir)
    seen = set()
    for root in (default_install_dir(), repo_fallback_install_dir()):
        if root in seen:
            continue
        seen.add(root)
        found = _search_root(root)
        if found:
            return found
    return shutil.which("blender")


def run(args: list[str], blender_exe: str | None = None, timeout_s: int = 600,
        cwd: str | None = None) -> subprocess.CompletedProcess:
    """headless 执行 blender 子命令。

    例：run(["--background", "--python", "S1.bpy", "--", "--verify-only"])。
    超时杀进程并抛 RuntimeError；输出按 UTF-8（errors=replace）解码，Windows 安全。
    """
    exe = blender_exe or find_blender()
    if not exe:
        raise RuntimeError(
            "未找到 blender.exe：先 python -m pipeline.camera.run_camera install，"
            "或设 %s 指向现有 blender.exe" % ENV_BLENDER)
    cmd = [exe] + list(args)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, timeout=timeout_s, cwd=cwd,
            encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"")
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        raise RuntimeError(
            "blender 超时（%ss）：%s\n--- 尾部输出 ---\n%s"
            % (timeout_s, " ".join(cmd[:6]), out[-2000:])) from e
    return proc


def get_version(blender_exe: str | None = None) -> str | None:
    """`blender --version` 首行解析出版本号（如 "4.2.23"）；失败返回 None。"""
    try:
        proc = run(["--version"], blender_exe=blender_exe, timeout_s=60)
    except (RuntimeError, OSError):
        return None
    if proc.returncode != 0:
        return None
    m = re.search(r"Blender\s+(\d+\.\d+\.\d+)", proc.stdout or "")
    return m.group(1) if m else None


# ---------------------------------------------------------------- 安装编排

def install(version_line: str = DEFAULT_VERSION_LINE, install_dir: str | None = None,
            keep_zip: bool = False, log=print) -> str:
    """下载并安装便携 Blender，返回 blender.exe 路径。

    幂等：已有可用安装（--version 通过）时直接返回。
    原子性：解压到 <install_dir>.tmp-<pid> 成功后 os.replace 到位；
    zip 缓存在 <仓库根>/_tools/.downloads/，失败保留供续传。
    """
    root = install_dir or default_install_dir()
    # 先全局发现（config 位置 + 仓库内兜底 + PATH）：任何一处有可用安装就复用，
    # 不因目标目录不同而重复下载 383MB
    existing = find_blender() if install_dir is None else find_blender(root)
    if existing:
        ver = get_version(existing)
        if ver:
            log("Blender %s 已安装：%s（复用）" % (ver, existing))
            return existing
        log("发现 %s 但 --version 未通过，重装" % existing)

    url, ver = resolve_zip_url(version_line)
    log("目标 Blender %s（版本线 %s）\n  %s" % (ver, version_line, url))
    zip_path = os.path.join(repo_root(), "_tools", ".downloads",
                            os.path.basename(url))
    _download_resume(url, zip_path, log=log)

    tmp = root + ".tmp-%d" % os.getpid()
    if os.path.exists(tmp):
        shutil.rmtree(tmp)
    os.makedirs(tmp)
    try:
        _extract_zip_strip_top(zip_path, tmp, log=log)
        if os.path.exists(root):
            bak = root + ".old-%d" % int(time.time())
            log("旧目录存在，移 aside：%s" % bak)
            os.replace(root, bak)
        os.replace(tmp, root)
    finally:
        if os.path.exists(tmp):
            shutil.rmtree(tmp, ignore_errors=True)

    exe = os.path.join(root, "blender.exe")
    if not os.path.isfile(exe):
        raise RuntimeError("解压完成但 %s 不存在" % exe)
    got = get_version(exe)
    if got != ver:
        raise RuntimeError("--version 验证失败：期望 %s，实际 %s" % (ver, got))
    log("Blender %s 安装并验证通过：%s" % (got, exe))
    if not keep_zip:
        try:
            os.remove(zip_path)
        except OSError:
            pass
    return exe


def ensure_installed(version_line: str = DEFAULT_VERSION_LINE,
                     install_dir: str | None = None, log=print) -> str:
    """find→install→verify 一步到位，返回 blender.exe。"""
    exe = find_blender(install_dir)
    if exe and get_version(exe):
        return exe
    return install(version_line=version_line, install_dir=install_dir, log=log)


if __name__ == "__main__":  # 直接运行：python pipeline/blender.py [版本线]
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_VERSION_LINE
    print(install(version_line=target))
