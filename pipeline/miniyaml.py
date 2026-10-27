# -*- coding: utf-8 -*-
"""受限子集 YAML 解析器（仅标准库，零第三方依赖）。

支持集（与 docs/FRAMEWORK_DESIGN.md §2.3 严格自洽，selftest 第 2 项会往返校验）：
  * 块风格嵌套映射、块序列（``- `` 项，含 ``- key: v`` 行内映射开头）
  * flow 序列 ``[a, b, c]``、flow 映射 ``{k: v}`` 及其嵌套（≥2 层）
  * ``|`` 与 ``>`` 多行块（默认 clip；``|-`` / ``>-`` strip；``+`` 按本实现等同 clip）
  * 单/双引号标量与裸标量、``#`` 注释、UTF-8
  * 类型推断：null / bool / int / float；引号标量一律是字符串

不支持（直接报 MiniYAMLError 并给出行号，提示改写）：
  * 锚点与别名（``&`` / ``*``）、多文档（``---``）、Tab 缩进
  * 跨行 flow（flow 必须在单行内闭合）
  * flow 内含冒号/逗号的裸标量（请加引号）
"""
from __future__ import annotations

import re

__all__ = ["MiniYAMLError", "parse", "loads", "load", "dumps"]


class MiniYAMLError(ValueError):
    """解析失败；message 带行号。"""

    def __init__(self, msg: str, line: int | None = None):
        if line is not None:
            msg = "line %d: %s" % (line, msg)
        super().__init__(msg)


_INT_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")
_KEY_RE = re.compile(r"^([^\s][^:]*?):(?:\s+(.*))?$")
_FLOW_STOP = ",}]:"


# --------------------------------------------------------------------------- #
# 词法辅助
# --------------------------------------------------------------------------- #
def _strip_comment(line: str) -> str:
    """去掉 ` # 注释`（# 前须为空白或行首；引号内的 # 不算）。

    双引号段内反斜杠转义下一字符（\\" 不闭引号，复审 #4：原实现只跳过反斜杠本身，
    被转义的 \\" 仍被当作闭引号，导致串内 # 被误剥成注释 → 引号未闭合报错）。"""
    in_s = in_d = False
    skip = False                       # 双引号段内被反斜杠转义的字符
    for i, ch in enumerate(line):
        if skip:
            skip = False
            continue
        if in_s:
            if ch == "'":
                in_s = False
        elif in_d:
            if ch == "\\":
                skip = True
            elif ch == '"':
                in_d = False
        elif ch == "'":
            in_s = True
        elif ch == '"':
            in_d = True
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i]
    return line


def _typed(s: str):
    """裸标量类型推断；引号由 _unquote 处理，不经过这里。"""
    if s in ("null", "Null", "NULL", "~", ""):
        return None
    if s in ("true", "True", "TRUE"):
        return True
    if s in ("false", "False", "FALSE"):
        return False
    if _INT_RE.match(s):
        return int(s)
    if _FLOAT_RE.match(s):
        return float(s)
    return s


def _unquote(s: str, line: int) -> str:
    """解析引号标量（单引号 '' 转义；双引号 \\\\ \" \\n \\t 转义）。"""
    if len(s) < 2 or s[0] not in "\"'" or s[-1] != s[0]:
        raise MiniYAMLError("引号未闭合: %r" % s, line)
    q = s[0]
    body = s[1:-1]
    if q == "'":
        if "'" in body.replace("''", ""):
            raise MiniYAMLError("单引号标量含未转义引号: %r" % s, line)
        return body.replace("''", "'")
    out = []
    i = 0
    while i < len(body):
        c = body[i]
        if c == "\\":
            if i + 1 >= len(body):
                raise MiniYAMLError("反斜杠转义悬空: %r" % s, line)
            nxt = body[i + 1]
            out.append({"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "'": "'"}.get(nxt, nxt))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _scalar(text: str, line: int):
    """一行上的标量值：引号 → str；裸 → 类型推断。"""
    t = text.strip()
    if not t:
        return None
    if t[0] in "\"'":
        return _unquote(t, line)
    if t[0] in "&*":
        raise MiniYAMLError("锚点/别名（%r…）不在支持集内，请改写" % t[:1], line)
    if t == "---" or t.startswith("--- "):
        raise MiniYAMLError("多文档 --- 不支持", line)
    return _typed(t)


# --------------------------------------------------------------------------- #
# flow 解析（单行内闭合）
# --------------------------------------------------------------------------- #
class _Flow:
    def __init__(self, s: str, line: int):
        self.s = s
        self.i = 0
        self.line = line

    def _ws(self):
        while self.i < len(self.s) and self.s[self.i] in " \t":
            self.i += 1

    def _peek(self):
        return self.s[self.i] if self.i < len(self.s) else ""

    def _quoted(self) -> str:
        j = self.i
        q = self.s[j]
        j += 1
        while j < len(self.s):
            if q == "'" and self.s[j] == "'":
                if j + 1 < len(self.s) and self.s[j + 1] == "'":
                    j += 2
                    continue
                break
            if q == '"' and self.s[j] == "\\":
                j += 2
                continue
            if self.s[j] == q:
                break
            j += 1
        if j >= len(self.s):
            raise MiniYAMLError("flow 内引号未闭合: %r" % self.s, self.line)
        val = _unquote(self.s[self.i : j + 1], self.line)
        self.i = j + 1
        return val

    def _bare(self, stops: str) -> str:
        j = self.i
        while j < len(self.s) and self.s[j] not in stops:
            j += 1
        val = self.s[self.i : j].strip()
        self.i = j
        return val

    def value(self):
        self._ws()
        c = self._peek()
        if c == "[":
            return self.sequence()
        if c == "{":
            return self.mapping()
        if c in "\"'":
            return self._quoted()
        raw = self._bare(",]}")
        if raw and raw[0] in "&*":
            raise MiniYAMLError("flow 内锚点/别名不支持: %r" % raw, self.line)
        return _typed(raw)

    def sequence(self):
        self.i += 1  # '['
        out = []
        self._ws()
        if self._peek() == "]":
            self.i += 1
            return out
        while True:
            out.append(self.value())
            self._ws()
            c = self._peek()
            if c == ",":
                self.i += 1
                continue
            if c == "]":
                self.i += 1
                return out
            raise MiniYAMLError("flow 序列语法错误（期望 , 或 ]）: %r" % self.s[self.i :], self.line)

    def mapping(self):
        self.i += 1  # '{'
        out = {}
        self._ws()
        if self._peek() == "}":
            self.i += 1
            return out
        while True:
            self._ws()
            if self._peek() in "\"'":
                key = self._quoted()
            else:
                key = self._bare(_FLOW_STOP)
            if not key:
                raise MiniYAMLError("flow 映射键为空: %r" % self.s, self.line)
            self._ws()
            if self._peek() != ":":
                raise MiniYAMLError("flow 映射缺少 ':'（含冒号的裸标量请加引号）: %r" % self.s[self.i :], self.line)
            self.i += 1
            out[key] = self.value()
            self._ws()
            c = self._peek()
            if c == ",":
                self.i += 1
                continue
            if c == "}":
                self.i += 1
                return out
            raise MiniYAMLError("flow 映射语法错误（期望 , 或 }）: %r" % self.s[self.i :], self.line)


def _parse_flow(text: str, line: int):
    f = _Flow(text, line)
    val = f.value()
    f._ws()
    if f.i != len(f.s):
        raise MiniYAMLError("flow 语法后有多余内容: %r" % f.s[f.i :], line)
    return val


# --------------------------------------------------------------------------- #
# 块解析
# --------------------------------------------------------------------------- #
class _Cur:
    """行游标。lines = [(行号, 原始行)]；结构性读取会跳过空行/整行注释。"""

    def __init__(self, lines: list):
        self.lines = lines
        self.i = 0

    def peek_struct(self):
        """返回 (行号, 缩进, 内容) 或 None（EOF）。空行/注释行被跳过（i 前移）。"""
        while self.i < len(self.lines):
            n, raw = self.lines[self.i]
            content = _strip_comment(raw).rstrip()
            if not content.strip():
                self.i += 1
                continue
            lead = content[:len(content) - len(content.lstrip(" \t"))]
            if "\t" in lead:
                raise MiniYAMLError("Tab 缩进不支持，请改用空格", n)
            indent = len(content) - len(content.lstrip(" "))
            return (n, indent, content.strip())
        return None


def _parse_block_scalar(cur: _Cur, parent_indent: int, header: str) -> str:
    style, chomp = header[0], header[1:]
    picked = []
    while cur.i < len(cur.lines):
        _n, raw = cur.lines[cur.i]
        if raw.strip() == "":
            picked.append("")
            cur.i += 1
            continue
        ind = len(raw) - len(raw.lstrip(" "))
        if ind <= parent_indent:
            break
        picked.append(raw)
        cur.i += 1
    while picked and picked[-1] == "":
        picked.pop()
    if not picked:
        return ""
    base = min(len(l) - len(l.lstrip(" ")) for l in picked)
    body = [l[base:] if l else "" for l in picked]
    if style == "|":
        text = "\n".join(body) + "\n"
    else:  # '>'
        chunks, buf = [], []
        for l in body:
            if l == "":
                chunks.append(" ".join(buf))
                buf = []
            else:
                buf.append(l)
        chunks.append(" ".join(buf))
        text = "\n".join(c for c in chunks) + "\n"
    if chomp == "-":
        return text.rstrip("\n")
    return text  # clip / keep（本实现统一 clip）


def _is_key_line(content: str):
    return _KEY_RE.match(content) is not None


def _map_step(result: dict, cur: _Cur, indent: int, content: str, n: int):
    m = _KEY_RE.match(content)
    if not m:
        raise MiniYAMLError("无法解析的映射行: %r" % content, n)
    key = m.group(1).strip()
    rest = (m.group(2) or "").strip()
    if key in result:
        raise MiniYAMLError("重复的键 %r" % key, n)
    cur.i += 1
    if rest in ("|", "|-", "|+", ">", ">-", ">+"):
        result[key] = _parse_block_scalar(cur, indent, rest)
        return
    if rest == "":
        nxt = cur.peek_struct()
        if nxt is not None and nxt[1] > indent:
            result[key] = _parse_node(cur, nxt[1])
        else:
            result[key] = None
        return
    if rest[0] in "[{":
        result[key] = _parse_flow(rest, n)
        return
    result[key] = _scalar(rest, n)


def _parse_map(cur: _Cur, indent: int) -> dict:
    result = {}
    while True:
        nxt = cur.peek_struct()
        if nxt is None:
            return result
        n, ind, content = nxt
        if ind < indent:
            return result
        if ind > indent:
            raise MiniYAMLError("缩进不一致（期望 %d，得到 %d）：%r" % (indent, ind, content), n)
        if content == "-" or content.startswith("- "):
            raise MiniYAMLError("映射处出现序列项 '- '（键下序列需缩进）", n)
        _map_step(result, cur, indent, content, n)


def _parse_seq(cur: _Cur, indent: int) -> list:
    out = []
    while True:
        nxt = cur.peek_struct()
        if nxt is None:
            return out
        n, ind, content = nxt
        if ind < indent:
            return out
        if ind > indent:
            raise MiniYAMLError("缩进不一致（期望 %d，得到 %d）：%r" % (indent, ind, content), n)
        if not (content == "-" or content.startswith("- ")):
            return out  # 同级出现映射键 → 序列结束
        rest = content[1:].strip()
        cur.i += 1
        if rest == "":
            nx = cur.peek_struct()
            if nx is not None and nx[1] > indent:
                out.append(_parse_node(cur, nx[1]))
            else:
                out.append(None)
        elif rest[0] in "[{":
            out.append(_parse_flow(rest, n))
        elif rest in ("|", "|-", "|+", ">", ">-", ">+"):
            out.append(_parse_block_scalar(cur, indent, rest))
        elif _is_key_line(rest):
            item = {}
            cur.i -= 1  # 回退到本行：_map_step 内部会自行前移游标（否则吞掉第 2 个键）
            _map_step(item, cur, indent + 2, rest, n)
            out.append(_parse_map_into(cur, indent + 2, item))
        else:
            out.append(_scalar(rest, n))


def _parse_map_into(cur: _Cur, indent: int, result: dict) -> dict:
    """继续解析 indent 级映射，并入 result（供 `- key: v` 行内映射续行）。"""
    while True:
        nxt = cur.peek_struct()
        if nxt is None:
            return result
        n, ind, content = nxt
        if ind < indent:
            return result
        if ind > indent:
            raise MiniYAMLError("缩进不一致（期望 %d，得到 %d）：%r" % (indent, ind, content), n)
        if content == "-" or content.startswith("- "):
            return result
        _map_step(result, cur, indent, content, n)


def _parse_node(cur: _Cur, indent: int):
    nxt = cur.peek_struct()
    if nxt is None:
        return None
    n, ind, content = nxt
    if content == "-" or content.startswith("- "):
        return _parse_seq(cur, ind)
    if _is_key_line(content):
        return _parse_map(cur, ind)
    cur.i += 1
    return _scalar(content, n)


def parse(text: str):
    """解析 YAML 文本（受限子集），返回 dict/list/标量。"""
    lines = [(n, raw.rstrip("\r")) for n, raw in enumerate(text.split("\n"), 1)]
    if lines and lines[0][1].startswith("\ufeff"):
        lines[0] = (lines[0][0], lines[0][1].lstrip("\ufeff"))
    cur = _Cur(lines)
    if cur.peek_struct() is None:
        return {}
    value = _parse_node(cur, 0)
    tail = cur.peek_struct()
    if tail is not None:
        raise MiniYAMLError("文档尾部有多余内容: %r" % tail[2], tail[0])
    return value


loads = parse


def load(path) :
    """从文件解析（UTF-8，容忍 BOM/CRLF）。"""
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return parse(f.read())


# --------------------------------------------------------------------------- #
# 序列化（用于往返等价校验与调试输出；人写文件请直接编辑模板）
# --------------------------------------------------------------------------- #
def _needs_quote(s: str) -> bool:
    if s == "" or s != s.strip():
        return True
    if s[0] in "-?[]{}#&*!|>'\"%@`,:" or "\t" in s or "\n" in s:
        return True
    t = _typed(s)
    if t is not None and not isinstance(t, str):
        return True
    if ": " in s or " #" in s or s.endswith(":"):
        return True
    return False


def _fmt_scalar(v) -> str:
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    s = str(v)
    if _needs_quote(s):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t") + '"'
    return s


def _fmt_key(k: str) -> str:
    return _fmt_scalar(k) if _needs_quote(str(k)) else str(k)


def _emit_kv(out: list, indent: int, key, value, prefix: str | None = None):
    pad = " " * indent
    head = (prefix if prefix is not None else pad) + _fmt_key(key) + ":"
    if isinstance(value, dict):
        if value:
            out.append(head)
            for k, v in value.items():
                _emit_kv(out, indent + 2, k, v)
        else:
            out.append(head + " {}")
    elif isinstance(value, list):
        if value:
            out.append(head)
            _emit_seq(out, indent + 2, value)
        else:
            out.append(head + " []")
    else:
        out.append(head + " " + _fmt_scalar(value))


def _emit_seq(out: list, indent: int, seq: list):
    pad = " " * indent
    for item in seq:
        if isinstance(item, dict) and item:
            # `- k: v` 行内开头，续键在 indent+2（与 _parse_seq 的行内映射约定一致）
            first = True
            for k, v in item.items():
                _emit_kv(out, indent + 2, k, v, prefix=(pad + "- ") if first else None)
                first = False
        elif isinstance(item, list) and item:
            out.append(pad + "-")
            _emit_seq(out, indent + 2, item)
        else:
            out.append(pad + "- " + (_fmt_scalar(item) if not isinstance(item, (dict, list)) else ("{}" if isinstance(item, dict) else "[]")))


def dumps(obj) -> str:
    """把 dict/list/标量序列化为受支持子集的 YAML 文本。"""
    out: list = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            _emit_kv(out, 0, k, v)
    elif isinstance(obj, list):
        _emit_seq(out, 0, obj)
    else:
        out.append(_fmt_scalar(obj))
    return "\n".join(out) + ("\n" if out else "")
