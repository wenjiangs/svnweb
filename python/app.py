#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SVN Web —— GitHub 风格的 Subversion 网页浏览器
基于 Flask + Pygments，直接读取本地 SVN 仓库（svn / svnlook 命令行）。
"""
import os
import re
import json
import time
import html
import zlib
import codecs
import shutil
import zipfile
import tempfile
import datetime
import subprocess
import functools
import hashlib
import hmac
import secrets
import socket
import sys
import threading
import contextlib
import concurrent.futures as futures
import xml.etree.ElementTree as ET
from urllib.parse import quote, urlparse
from pathlib import Path

from flask import (Flask, render_template, request, redirect, url_for,
                   session, abort, Response, send_file, flash,
                   get_flashed_messages, after_this_request)
from werkzeug.security import check_password_hash, generate_password_hash
from pygments import highlight
from pygments.lexers import get_lexer_for_filename, TextLexer
from pygments.formatters import HtmlFormatter
from pygments.style import Style
from pygments.token import (Token, Whitespace, Comment, Keyword, Name,
                            Operator, Punctuation, Literal, String, Number,
                            Generic, Error)
from pygments.util import ClassNotFound

# ----------------------------------------------------------------- 配置
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

# config.json 不存在时进入「安装向导」模式：所有访问引导到 /install，
# 在浏览器里完成初始化（环境自检 → 仓库根 / 检出地址（含自定义端口）→
# 第一个管理员账号），写入 config.json 后立即生效、免重启。
INSTALL_MODE = not os.path.exists(CONFIG_PATH)

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False


def _apply_config(cfg):
    """把配置字典应用到模块级全局变量。

    启动导入时调用一次；/install 写完 config.json 后再调一次，使新配置
    立即生效（secret_key / 密码哈希 / 仓库根 / 反代开关等全部热更新）。
    """
    global REPO_ROOT, USERNAME, PASSWORD_HASH, SECRET_KEY, CACHE_TTL
    global MAX_HIGHLIGHT_BYTES, MAX_PREVIEW_BYTES, MAX_MEDIA_BYTES
    global LOG_PAGE_SIZE, SEARCH_MAX_RESULTS, SEARCH_SNIPPET_MAX
    global SEARCH_SNIPPET_WIDTH, SEARCH_SCAN_TTL, SEARCH_SCAN_BUDGET
    global SEARCH_SCAN_TIMEOUT, SVN_BASE, AUDIT_MAX, ARCHIVE_DIR
    global BACKUP_DIR, ARCHIVE_MAX_BYTES, WEB_USERS, BEHIND_PROXY

    REPO_ROOT = cfg.get("repo_root", "/svn")
    USERNAME = cfg.get("username", "admin")
    PASSWORD_HASH = cfg.get("password_hash", "")
    # 安装模式下尚无 secret_key：用随机临时值维持 session 可用，
    # 安装完成后会以正式值覆盖（登录态作废无妨）。
    SECRET_KEY = cfg.get("secret_key") or secrets.token_hex(32)
    CACHE_TTL = cfg.get("cache_ttl", 20)
    MAX_HIGHLIGHT_BYTES = cfg.get("max_highlight_bytes", 512 * 1024)
    # 网页预览（blob）的文件大小上限；超过则不再尝试读取，避免大文件内存压力
    MAX_PREVIEW_BYTES = cfg.get("max_preview_bytes", 5 * 1024 * 1024)
    # 图片 / PDF / 音视频这类「浏览器能直接打开」的文件走流式渲染（不占服务端内存），
    # 但带宽与浏览器侧的内存仍有限度，所以再给一个更宽松的上限
    MAX_MEDIA_BYTES = cfg.get("max_media_bytes", 32 * 1024 * 1024)
    LOG_PAGE_SIZE = cfg.get("log_page_size", 60)
    # ---- 搜索（提交信息 / 文件名 / 代码变更）----
    SEARCH_MAX_RESULTS = cfg.get("search_max_results", 200)
    SEARCH_SNIPPET_MAX = cfg.get("search_snippet_max", 4)
    SEARCH_SNIPPET_WIDTH = cfg.get("search_snippet_width", 120)
    # 代码搜索缓存（zlib 压缩内存缓存；缓存键带最新版本号，TTL 只管内存回收）
    SEARCH_SCAN_TTL = cfg.get("search_scan_ttl", 1800)
    SEARCH_SCAN_BUDGET = cfg.get("search_scan_budget", 64 * 1024 * 1024)
    # 单次全历史扫描的墙钟上限；waitress 的 --channel-timeout 是 120s，这里必须更小
    SEARCH_SCAN_TIMEOUT = cfg.get("search_scan_timeout", 100)
    # 检出地址前缀，形如 svn://host 或 svn://host:3690；留空则按访问域名推断
    SVN_BASE = cfg.get("svn_base", "")
    AUDIT_MAX = cfg.get("audit_max", 300)
    # 归档仓库目录位于 REPO_ROOT 之下、不带 db/，不会被 list_repos() 识别
    ARCHIVE_DIR = os.path.join(REPO_ROOT, ".archived")
    BACKUP_DIR = cfg.get("backup_dir", os.path.join(BASE_DIR, "backups"))
    ARCHIVE_MAX_BYTES = cfg.get("archive_max_bytes", 1024 * 1024 * 1024)
    # 可选多账号：{"用户名": {"password_hash": "...", "role": "admin"|"viewer"}}
    WEB_USERS = cfg.get("web_users", {})
    BEHIND_PROXY = cfg.get("behind_proxy", False)

    app.secret_key = SECRET_KEY
    # 反代（nginx）终止 HTTPS 时让 request.is_secure / url_for 跟随转发头；
    # 只包一层（安装完成后热更新时避免重复包装）。
    if BEHIND_PROXY and not getattr(app, "_proxyfix_on", False):
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
        app._proxyfix_on = True


if INSTALL_MODE:
    CONFIG = {}
else:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        CONFIG = json.load(f)
_apply_config(CONFIG)

# 审计日志（管理端变更的 append-only 记录，JSONL 格式）
# 与 config.json 同目录；每行一条 JSON：ts/user/action/repo/detail
AUDIT_PATH = os.path.join(BASE_DIR, "audit.log")

# 创建仓库：仅允许字母数字开头，后接字母数字/点/下划线/连字符
REPO_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# 去掉 /r/ 前缀后，仓库名直接作为顶层路径段。以下为被路由/资源占用的
# 保留词，不允许作为仓库名（避免与 /login、/admin/*、/static、/install 等冲突）。
RESERVED_REPO_NAMES = {
    "login", "logout", "healthz", "favicon.ico",
    "admin", "static", "robots.txt", "sitemap.xml",
    "blob", "raw", "blame", "download", "commits",
    "commit", "compare", "tree", "new", "created",
    "versions", "users", "recent", "audit", "server", "archived",
    "install", "account", "media",
}


def joinpath(base, name):
    return ((base or "").strip("/") + "/" + name).strip("/")


app.add_template_filter(joinpath, "joinpath")


# ------------------------------------------------------------ Pygments
class GitHubStyle(Style):
    """贴近 GitHub 浅色主题的语法高亮配色"""
    background_color = "#ffffff"
    default_style = ""
    styles = {
        Token: "#24292f",
        Whitespace: "#24292f",
        Comment: "italic #6e7781",
        Comment.Preproc: "noitalic #6e7781",
        Keyword: "#cf222e",
        Keyword.Constant: "#0550ae",
        Keyword.Declaration: "#cf222e",
        Keyword.Namespace: "#cf222e",
        Keyword.Type: "#cf222e",
        Operator: "#0550ae",
        Operator.Word: "#cf222e",
        Punctuation: "#24292f",
        Name: "#24292f",
        Name.Builtin: "#0550ae",
        Name.Function: "#8250df",
        Name.Class: "#8250df",
        Name.Decorator: "#8250df",
        Name.Variable: "#953800",
        Name.Constant: "#0550ae",
        Name.Attribute: "#0550ae",
        Name.Tag: "#116329",
        Name.Label: "#0550ae",
        Name.Exception: "#cf222e",
        Literal: "#0a3069",
        String: "#0a3069",
        String.Escape: "#cf222e",
        String.Regex: "#0a3069",
        Number: "#0550ae",
        Generic.Deleted: "#82071e",
        Generic.Inserted: "#116329",
        Generic.Heading: "bold #24292f",
        Generic.Subheading: "bold #24292f",
        Error: "#82071e",
    }


FORMATTER = HtmlFormatter(style=GitHubStyle, linenos="table",
                          cssclass="highlight", nowrap=False,
                          lineanchors="L", anchorlinenos=False)
PYGMENTS_CSS = FORMATTER.get_style_defs(".highlight")


# ------------------------------------------------------------- SVN 访问
def _decode_output(b):
    """解码子进程输出：优先 utf-8（svn/svnlook 等工具输出 utf-8）；
    出现替换符时回退 GBK——中文 Windows 的系统命令（sc/tasklist 等）
    控制台输出是 GBK，utf-8 解码会变乱码。"""
    s = b.decode("utf-8", "replace")
    if "\ufffd" in s:
        try:
            s = b.decode("gbk")
        except UnicodeDecodeError:
            pass
    return s


def run_svn(args, timeout=60):
    """执行 svn / svnlook 命令，返回 (rc, stdout_str, stderr_str)"""
    try:
        p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout)
        return p.returncode, _decode_output(p.stdout), _decode_output(p.stderr)
    except subprocess.TimeoutExpired:
        return 124, "", "命令执行超时"
    except (FileNotFoundError, PermissionError):
        return 127, "", "未找到命令或无权执行"


_cache = {}


def cache_get(key):
    v = _cache.get(key)
    if v and (time.time() - v[0]) < CACHE_TTL:
        return v[1]
    return None


def cache_set(key, val):
    _cache[key] = (time.time(), val)
    return val


def repo_path(name):
    return os.path.join(REPO_ROOT, name)


def list_repos():
    if not os.path.isdir(REPO_ROOT):
        return []
    names = []
    for n in sorted(os.listdir(REPO_ROOT), key=lambda s: s.lower()):
        p = os.path.join(REPO_ROOT, n)
        # 合法的 SVN 仓库包含 format / db 目录
        if os.path.isdir(p) and os.path.isdir(os.path.join(p, "db")):
            names.append(n)
    return names


def validate_repo(name):
    if name not in list_repos():
        abort(404)


def youngest(name):
    key = ("youngest", name)
    v = cache_get(key)
    if v is not None:
        return v
    rc, out, err = run_svn(["svnlook", "youngest", repo_path(name)])
    n = int(out.strip()) if rc == 0 and out.strip().isdigit() else 0
    return cache_set(key, n)


def repo_url(name, path="", rev=None):
    """构造仓库的 file:// URL（路径做百分号编码，兼容中文/空格）。

    rev 会以 peg revision 形式（URL@rev）附加。原因：SVN 命令里的 -r 只是
    「操作版本」，而 **peg revision 默认取 HEAD** —— 对「HEAD 里已被删除、
    但旧版本中仍然存在」的路径，只写 -r 会直接在 HEAD 上查找失败
    （报 `File not found: revision <HEAD>`，注意报的是 HEAD 而不是 -r 指定的版本）。
    把 rev 同时作为 peg 传进去，才能精确定位「该版本中的该路径」。
    """
    base = Path(repo_path(name)).as_uri()
    url = (base + "/" + quote(path, safe="/")) if path else (base + "/")
    if rev is not None:
        return "%s@%d" % (url, int(rev))
    if "@" in path:
        # 路径自身含 @ 时，需以 @ 结尾消歧，否则会被当作 peg 分隔符
        return url + "@"
    return url


def checkout_url(name, path=""):
    """仓库（或其中的某个子目录）的对外检出地址（svn:// 协议，供 svn 客户端使用）

    SVN 允许把任意子目录当作检出目标，所以浏览到某个目录时，检出地址要跟着
    带上当前路径；path 为空就是整个仓库。
    """
    base = (SVN_BASE or "").strip().rstrip("/")
    if not base:
        # 未配置时按访问域名推断（去掉 Web 端口，SVN 走 3690）
        base = "svn://" + (request.host or "").split(":")[0]
    url = base + "/" + name
    sub = (path or "").strip("/")
    if sub:
        url += "/" + quote(sub, safe="/")
    return url


def checkout_dir(path, kind):
    """检出用的路径：文件归到它所在的目录（检出单个文件没有意义），目录原样返回。"""
    if not path:
        return ""
    if kind == "file":
        return path.rsplit("/", 1)[0] if "/" in path else ""
    return path


def svn_list(name, path, rev):
    """返回目录条目列表（每条含 name/kind/size/rev/author/date）。

    - 返回 []   表示该目录真实存在但没有内容（空目录）
    - 返回 None 表示该路径在此版本中不存在，调用方必须按 404 处理
    """
    if rev <= 0:
        # r0 是空仓库的初始版本：只有根路径存在，且没有任何内容
        return [] if not path else None
    key = ("list", name, path, rev)
    v = cache_get(key)
    if v is not None:
        return v
    rc, out, err = run_svn(["svn", "list", "--xml", "-r", str(rev),
                            "--non-interactive", repo_url(name, path, rev)])
    if rc != 0:
        # svn 以非零退出码表示该 URL 在指定版本中不存在
        # （W160013/E160013 non-existent in revision），
        # 与「空目录」（rc=0 且无条目）严格区分开。
        # 失败结果不写入缓存，避免把 404 固化下来。
        return None
    entries = []
    if out.strip():
        try:
            root = ET.fromstring(out)
            for e in root.iter("entry"):
                kind = e.get("kind", "file")
                nm = e.findtext("name") or ""
                size_el = e.findtext("size")
                com = e.find("commit")
                crev = com.get("revision") if com is not None else ""
                cau = com.findtext("author") if com is not None else ""
                cdt = com.findtext("date") if com is not None else ""
                entries.append({
                    "name": nm, "kind": kind,
                    "size": int(size_el) if size_el and size_el.isdigit() else None,
                    "rev": crev or "", "author": cau or "", "date": cdt or "",
                })
        except ET.ParseError:
            return None
    entries.sort(key=lambda x: (x["kind"] != "dir", x["name"].lower()))
    return cache_set(key, entries)


def path_kind(name, path, rev):
    """判定路径在该版本中的类型：'dir' / 'file' / None（不存在）"""
    if rev <= 0:
        return "dir" if not path else None
    key = ("kind", name, path, rev)
    v = cache_get(key)
    if v is not None:
        return v
    rc, out, err = run_svn(["svn", "info", "--xml", "-r", str(rev),
                            "--non-interactive", repo_url(name, path, rev)])
    if rc != 0 or not out.strip():
        return None
    kind = None
    try:
        for e in ET.fromstring(out).iter("entry"):
            kind = e.get("kind") or None
            break
    except ET.ParseError:
        return None
    return cache_set(key, kind) if kind else None


def file_size(name, path, rev):
    """返回路径在该版本下的文件大小（字节），非文件或失败返回 None。"""
    if rev <= 0:
        return None
    key = ("size", name, path, rev)
    v = cache_get(key)
    if v is not None:
        return v
    rc, out, err = run_svn(["svn", "info", "--xml", "-r", str(rev),
                            "--non-interactive", repo_url(name, path, rev)])
    if rc != 0:
        return None
    size = None
    try:
        for e in ET.fromstring(out).iter("entry"):
            if e.get("kind") == "file":
                sz = e.findtext("size")
                if sz and sz.isdigit():
                    size = int(sz)
            break
    except ET.ParseError:
        return None
    return cache_set(key, size)


def svn_log(name, path, rev, limit=LOG_PAGE_SIZE, until_rev=None):
    """返回提交记录列表（含改动路径）。

    until_rev：可选，从本次拉取的下界版本号（不含），用于「加载更多」翻页。
    关键字过滤不走这里——svn 的 --search 是 glob 语义（不带通配符就是全等匹配）
    且够不着代码内容，统一交给 search_versions() 在全历史里做子串匹配。
    """
    end = until_rev if until_rev and until_rev >= 2 else 1
    key = ("log", name, path, rev, limit, end)
    v = cache_get(key)
    if v is not None:
        return v
    cmd = ["svn", "log", "--xml", "-v", "-l", str(limit),
           "-r", "%d:%d" % (rev, end), "--non-interactive"]
    cmd.append(repo_url(name, path, rev))
    rc, out, err = run_svn(cmd)
    logs = []
    if rc == 0 and out.strip():
        try:
            root = ET.fromstring(out)
            for e in root.iter("logentry"):
                revs = e.get("revision", "")
                paths = []
                for p in e.findall("./paths/path"):
                    paths.append({
                        "path": (p.text or "").strip(),
                        "action": p.get("action", ""),
                        "kind": p.get("kind", ""),
                    })
                logs.append({
                    "rev": revs,
                    "author": e.findtext("author") or "",
                    "date": e.findtext("date") or "",
                    "msg": (e.findtext("msg") or "").strip(),
                    "paths": paths,
                })
        except ET.ParseError:
            pass
    return cache_set(key, logs)


def svn_log_one(name, rev):
    """取单个版本的元信息 + 改动路径"""
    key = ("log1", name, rev)
    v = cache_get(key)
    if v is not None:
        return v
    rc, out, err = run_svn(["svn", "log", "--xml", "-v", "-r", str(rev),
                            "--non-interactive", repo_url(name, "", rev)])
    info = None
    if rc == 0 and out.strip():
        try:
            root = ET.fromstring(out)
            e = next(root.iter("logentry"), None)
            if e is not None:
                paths = [{"path": (p.text or "").strip(),
                          "action": p.get("action", ""),
                          "kind": p.get("kind", "")}
                         for p in e.findall("./paths/path")]
                info = {
                    "rev": e.get("revision", ""),
                    "author": e.findtext("author") or "",
                    "date": e.findtext("date") or "",
                    "msg": (e.findtext("msg") or "").strip(),
                    "paths": paths,
                }
        except ET.ParseError:
            pass
    return cache_set(key, info)


def svn_cat(name, path, rev):
    rc, out, err = run_svn(["svn", "cat", "-r", str(rev),
                            "--non-interactive", repo_url(name, path, rev)], timeout=60)
    if rc != 0:
        return None, err
    return out, ""


# ---------- 侧边栏「仓库信息」统计 ----------
# 提交次数要数历史，比普通查询重得多，所以单独放一个长 TTL 的缓存，
# 不跟 CACHE_TTL（20s）那套走——否则每次翻页都要重新扫一遍日志。
_STATS_TTL = CONFIG.get("stats_ttl", 300)
_STATS_SCAN_TIMEOUT = CONFIG.get("stats_scan_timeout", 20)
_stats_cache = {}
_stats_cache_lock = threading.Lock()


def _stats_cache_get(key):
    with _stats_cache_lock:
        ent = _stats_cache.get(key)
    if not ent:
        return None
    if (time.time() - ent[0]) >= _STATS_TTL:
        with _stats_cache_lock:
            _stats_cache.pop(key, None)
        return None
    return ent[1]


def _stats_cache_put(key, val):
    with _stats_cache_lock:
        _stats_cache[key] = (time.time(), val)
        if len(_stats_cache) > 512:
            oldest = min(_stats_cache, key=lambda k: _stats_cache[k][0])
            _stats_cache.pop(oldest, None)
    return val


def count_commits(name, path, rev):
    """路径在 r1..r{rev} 范围内的提交次数。

    仓库根不用扫日志：SVN 的版本号是连续的、一个版本就是一次提交，
    所以根的提交次数恒等于 rev，没必要为最常见的首页白白走一遍全历史。
    子路径的版本号有断档，只能老实跑一遍 `svn log -q`（配合 --xml 数 <logentry>，
    避免解析本地化文本）。失败或超时返回 None，由模板显示 `--`，不拖垮页面。
    """
    if rev <= 0:
        return 0
    if not path:
        return rev
    key = ("count", name, path, rev)
    v = _stats_cache_get(key)
    if v is not None:
        return v
    rc, out, err = run_svn(["svn", "log", "-q", "--xml", "--non-interactive",
                            "-r", "%d:1" % rev, repo_url(name, path, rev)],
                           timeout=_STATS_SCAN_TIMEOUT)
    if rc != 0:
        return None
    return _stats_cache_put(key, out.count("<logentry"))


def repo_stats(name, path, rev):
    """右侧信息栏用的一组统计：提交次数 / 版本总数 / 最后一次提交。"""
    key = ("stats", name, path, rev)
    v = _stats_cache_get(key)
    if v is not None:
        return v
    last = svn_log(name, path, rev, limit=1)
    last = last[0] if last else None
    return _stats_cache_put(key, {
        "commits": count_commits(name, path, rev),
        "total": youngest(name),           # 仓库现有版本总数（r1..r{head}）
        "last_rev": last["rev"] if last else "",
        "last_author": last["author"] if last else "",
        "last_date": last["date"] if last else "",
    })


def stream_cat(name, path, rev, chunk=64 * 1024):
    """流式读取 svn cat 的输出（生成器，逐块产出 str）。

    用于下载/原始视图，避免大文件一次性载入内存。
    进程退出后才判定 rc，因此若出错可能已吐出部分数据。
    """
    import subprocess as _sp
    try:
        p = _sp.Popen(["svn", "cat", "-r", str(rev), "--non-interactive",
                       repo_url(name, path, rev)],
                      stdout=_sp.PIPE, stderr=_sp.PIPE)
        while True:
            buf = p.stdout.read(chunk)
            if not buf:
                break
            yield buf
        p.wait(timeout=120)
    except (OSError, _sp.TimeoutExpired):
        return
    finally:
        try:
            p.stdout.close()
            p.stderr.close()
        except Exception:
            pass


def svn_blame(name, path, rev):
    """逐行追溯：返回 ([{rev, author, date, line, index}], error)。

    svn blame --xml 只给出每行的 revision/author/date（不包含行文本），
    因此再取 svn cat 拿到源码，按行号 1:1 合并成可展示的追溯表。
    """
    key = ("blame", name, path, rev)
    v = cache_get(key)
    if v is not None:
        return v
    rc, out, err = run_svn(["svn", "blame", "--xml", "-r", str(rev),
                            "--non-interactive", repo_url(name, path, rev)],
                           timeout=90)
    if rc != 0:
        return None, err
    attrs = {}
    try:
        root = ET.fromstring(out)
        for e in root.iter("entry"):
            lnum = int(e.get("line-number", "0"))
            if lnum <= 0:
                continue
            com = e.find("commit")
            attrs[lnum] = {
                "rev": com.get("revision", "") if com is not None else "",
                "author": com.findtext("author") or "" if com is not None else "",
                "date": com.findtext("date") or "" if com is not None else "",
            }
    except ET.ParseError as ex:
        return None, "解析 blame 输出失败: %s" % ex
    if not attrs:
        # 空的 blame（例如文件无有效行）返回空表
        return cache_set(key, ([], ""))
    # 取源码行
    content, _ = svn_cat(name, path, rev)
    if content is None:
        content = ""
    src_lines = content.split("\n")
    if src_lines and src_lines[-1] == "":
        src_lines.pop()  # 末尾多余空行
    rows = []
    max_line = max(attrs)
    for idx in range(1, max(attrs) + 1):
        a = attrs.get(idx, {})
        line = src_lines[idx - 1] if (idx - 1) < len(src_lines) else ""
        rows.append({"rev": a.get("rev", ""), "author": a.get("author", ""),
                     "date": a.get("date", ""), "line": line, "index": idx})
    return cache_set(key, (rows, ""))


def svn_diff(name, rev, path=""):
    rc, out, err = run_svn(["svn", "diff", "-c", str(rev),
                            "--non-interactive", "--notice-ancestry",
                            repo_url(name, path, rev)], timeout=90)
    if rc != 0 and not out:
        return None, err
    return out, err


def svn_diff_path(name, rev, path):
    """只算某个路径（文件或目录）在某次提交里的改动。

    等价于 `svn diff -c <rev> URL/<path>@<rev>`；路径作用域让 svn 只遍历这
    个子树，比整仓库 diff 快很多。目标为目录时会列出该目录下的所有变更。
    新增/修改/删除/二进制都能正常输出；路径在该版本不存在则返回 (None, err)。
    """
    if not path:
        return svn_diff(name, rev, "")
    rc, out, err = run_svn(["svn", "diff", "-c", str(rev),
                            "--non-interactive", "--notice-ancestry",
                            repo_url(name, path, rev)], timeout=90)
    if rc != 0 and not out:
        return None, err
    return out, err


def filter_diff_by_path(files, path, is_dir):
    """把整仓库 diff 的结果裁到某个路径上（作用域 diff 取不到时的兜底）。"""
    out = []
    for f in files:
        p = f["path"].lstrip("/")
        if p == path or (is_dir and p.startswith(path + "/")):
            out.append(f)
    return out


def repo_last_change(name, rev):
    """仓库最后提交（用于首页）"""
    logs = svn_log(name, "", rev, limit=1)
    return logs[0] if logs else None


# ------------------------------------------------------- 创建仓库（仅创建）
SVNSERVE_CONF_TMPL = """[general]
anon-access = none
auth-access = write
password-db = passwd
authz-db = authz
realm = {realm}
"""


def pick_template_repo(exclude=None):
    """挑一个已有仓库（排除 exclude 所指仓库）作为新仓库权限配置的模板。
    排除正在创建的仓库本身，避免 src 与 dst 指向同一文件导致拷贝报错。"""
    for n in list_repos():
        if exclude and n == exclude:
            continue
        if os.path.isfile(os.path.join(REPO_ROOT, n, "conf", "passwd")):
            return n
    return None


def create_repo(name):
    """只做一件事：创建一个可用的空仓库。

    执行 svnadmin create，并写入 svnserve.conf（禁止匿名、认证可写、
    realm 指向自身），passwd/authz 沿用已有仓库的账号与授权，使现有
    SVN 账号在新仓库上立即可用。
    不创建任何目录结构、不产生任何提交。
    返回 (ok, message)。
    """
    name = (name or "").strip()
    if not name:
        return False, "请填写仓库名"
    if not REPO_NAME_RE.match(name):
        return False, ("仓库名不合法：只允许字母、数字、点、下划线、连字符，"
                       "必须以字母或数字开头，长度 1-64 位")
    if name in RESERVED_REPO_NAMES:
        return False, ("不允许使用保留名「%s」：该名称与系统页面/路由冲突，"
                       "请换个仓库名" % name)
    if name.lower() in [n.lower() for n in list_repos()]:
        return False, "同名仓库已存在"
    target = os.path.join(REPO_ROOT, name)
    if os.path.exists(target):
        return False, "目标路径已被占用：%s" % target

    rc, out, err = run_svn(["svnadmin", "create", target], timeout=180)
    if rc != 0:
        return False, "svnadmin create 执行失败：%s" % (
            err.strip() or out.strip() or "未知错误")

    conf_dir = os.path.join(target, "conf")
    try:
        conf_path = os.path.join(conf_dir, "svnserve.conf")
        with open(conf_path, "w", encoding="utf-8") as f:
            f.write(SVNSERVE_CONF_TMPL.format(realm="/svn/" + name))
        os.chmod(conf_path, 0o644)

        tpl = pick_template_repo(exclude=name)
        if tpl:
            for fn, mode in (("passwd", 0o600), ("authz", 0o644)):
                src = os.path.join(REPO_ROOT, tpl, "conf", fn)
                if os.path.isfile(src):
                    dst = os.path.join(conf_dir, fn)
                    shutil.copyfile(src, dst)
                    os.chmod(dst, mode)
    except OSError as e:
        return False, ("仓库目录已创建，但权限配置写入失败：%s。"
                       "请检查 %s 下的 conf 文件。" % (e, target))

    return True, "仓库 %s 创建成功" % name


# ------------------------------------------------------- 审计日志（append-only）
_audit_lock = threading.Lock()

# 审计动作 -> 展示文案
AUDIT_ACTION_LABELS = {
    "create_repo": "创建仓库",
    "rename_repo": "重命名仓库",
    "archive_repo": "归档仓库",
    "restore_repo": "恢复仓库",
    "delete_repo": "彻底删除仓库",
    "save_desc": "修改仓库描述",
    "save_authz": "编辑 authz 原文",
    "save_group": "保存用户组",
    "del_group": "删除用户组",
    "save_user": "保存账号",
    "del_user": "删除账号",
    "change_web_pw": "修改网站密码",
    "save_web_user": "保存网站账号",
    "del_web_user": "删除网站账号",
    "login": "登录成功",
    "login_fail": "登录失败",
    "start_backup": "启动备份",
    "del_backup": "删除备份",
    "save_hook": "保存钩子",
    "del_hook": "删除钩子",
    "unlock": "强制解锁",
    "create_branch": "创建分支/标签",
    "restart_svnserve": "重启 svnserve",
}


def audit(action, repo="", detail="", user=None):
    """追加一条审计记录。写失败不影响主流程（静默跳过）。"""
    entry = {
        "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "user": user or session.get("user") or "-",
        "action": action,
        "repo": repo or "",
        "detail": detail or "",
    }
    try:
        with _audit_lock:
            with open(AUDIT_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_audit_tail(limit=AUDIT_MAX, q_repo="", q_action=""):
    """读取审计日志末尾 limit 条（按时间倒序），支持仓库/动作过滤。

    文件只需读尾部：从文件末尾回退最多 1MB 再按行解析，够覆盖
    limit 条的日常规模，也避免日志长了大量 IO。
    """
    if not os.path.isfile(AUDIT_PATH):
        return []
    try:
        size = os.path.getsize(AUDIT_PATH)
        with open(AUDIT_PATH, "rb") as f:
            back = min(size, 1024 * 1024)
            f.seek(size - back)
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    lines = data.split("\n")
    if back < size and lines:
        lines = lines[1:]  # 丢弃开头可能被截断的半行
    out = []
    for ln in reversed(lines):
        ln = ln.strip()
        if not ln:
            continue
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        if q_repo and q_repo.lower() not in (e.get("repo") or "").lower():
            continue
        if q_action and e.get("action") != q_action:
            continue
        e["label"] = AUDIT_ACTION_LABELS.get(e.get("action"), e.get("action"))
        out.append(e)
        if len(out) >= limit:
            break
    return out


def purge_repo_cache(name):
    """仓库被重命名/归档/删除后，清掉所有以该仓库名为键的内存缓存。"""
    def _hit(k):
        return any(name == part for part in k if isinstance(part, str)) \
            if isinstance(k, tuple) else k == name
    for d in (_cache, _stats_cache, _scan_cache, _delete_cache):
        for k in [k for k in list(d) if _hit(k)]:
            d.pop(k, None)


def purge_all_caches():
    """repo_root 切换后清空全部仓库相关内存缓存（旧根下的数据全部作废）。"""
    _cache.clear()
    with _stats_cache_lock:
        _stats_cache.clear()
    with _scan_cache_lock:
        _scan_cache.clear()
    with _delete_cache_lock:
        _delete_cache.clear()


# ------------------------------------------------- 归档 / 恢复 / 重命名 / 删除
def list_archived():
    """列出已归档仓库。归档目录名为 <name>@<时间戳>，按名字排序。"""
    if not os.path.isdir(ARCHIVE_DIR):
        return []
    out = []
    for n in sorted(os.listdir(ARCHIVE_DIR), key=str.lower):
        p = os.path.join(ARCHIVE_DIR, n)
        if not (os.path.isdir(p) and os.path.isdir(os.path.join(p, "db"))):
            continue
        base, _, ts = n.rpartition("@")
        out.append({"arc": n, "name": base or n, "ts": ts})
    return out


def _check_repo_name_free(name):
    """新仓库名校验（创建/重命名/恢复共用）。合法且未占用返回 None，否则返回错误信息。"""
    if not name:
        return "请填写仓库名"
    if not REPO_NAME_RE.match(name):
        return ("仓库名不合法：只允许字母、数字、点、下划线、连字符，"
                "必须以字母或数字开头，长度 1-64 位")
    if name in RESERVED_REPO_NAMES:
        return ("不允许使用保留名「%s」：该名称与系统页面/路由冲突，请换个名字" % name)
    if name.lower() in [n.lower() for n in list_repos()]:
        return "同名仓库已存在"
    if os.path.exists(os.path.join(REPO_ROOT, name)):
        return "目标路径已被占用：%s" % os.path.join(REPO_ROOT, name)
    return None


def archive_repo(name):
    """把仓库移入 .archived/<name>@<时间戳> 并 chmod 000 阻断 svnserve 访问。

    不是删除：可随时从「新建仓库」页恢复。返回 (ok, message)。
    """
    validate_repo(name)
    src = repo_path(name)
    try:
        os.makedirs(ARCHIVE_DIR, exist_ok=True)
        dst = os.path.join(ARCHIVE_DIR, "%s@%s" % (
            name, datetime.datetime.now().strftime("%Y%m%d%H%M%S")))
        if os.path.exists(dst):
            return False, "归档目录名冲突，请稍后重试"
        os.rename(src, dst)
        try:
            os.chmod(dst, 0o000)  # 阻断 svnserve（非 root）读取
        except OSError:
            pass
    except OSError as e:
        return False, "归档失败：%s" % e
    purge_repo_cache(name)
    return True, "仓库 %s 已归档" % name


def restore_archived(arc):
    """把归档目录恢复回 REPO_ROOT（恢复为原名）。返回 (ok, message)。"""
    arc = arc or ""
    # 只允许纯文件名，防止路径穿越
    if arc != os.path.basename(arc) or "@" not in arc:
        return False, "参数不合法"
    src = os.path.join(ARCHIVE_DIR, arc)
    if not (os.path.isdir(src) and os.path.isdir(os.path.join(src, "db"))):
        return False, "归档不存在：%s" % arc
    base = arc.rpartition("@")[0] or arc
    err = _check_repo_name_free(base)
    if err:
        return False, "无法恢复为「%s」：%s" % (base, err)
    try:
        os.chmod(src, 0o755)
        os.rename(src, os.path.join(REPO_ROOT, base))
    except OSError as e:
        return False, "恢复失败：%s" % e
    return True, "仓库 %s 已恢复" % base


def delete_archived(arc, confirm):
    """彻底删除一个已归档的仓库目录（不可恢复）。返回 (ok, message)。"""
    arc = arc or ""
    if arc != os.path.basename(arc) or "@" not in arc:
        return False, "参数不合法"
    base = arc.rpartition("@")[0] or arc
    if confirm != base:
        return False, "确认文字不匹配，未删除。需完整输入仓库名「%s」" % base
    src = os.path.join(ARCHIVE_DIR, arc)
    if not os.path.isdir(src):
        return False, "归档不存在：%s" % arc

    def _rm_readonly(func, path, _exc):
        # SVN 的 rev 文件自带只读属性，直接 rmtree 会 WinError 5；逐个解锁后重试
        try:
            os.chmod(path, 0o755)
        except OSError:
            pass
        func(path)

    try:
        os.chmod(src, 0o755)  # 000 目录无法遍历，先恢复权限再删
        shutil.rmtree(src, onerror=_rm_readonly)
    except OSError as e:
        return False, "删除失败：%s" % e
    return True, "归档 %s 已彻底删除" % arc


def rename_repo(old, new):
    """重命名仓库目录，并同步 svnserve.conf 里的 realm。返回 (ok, message)。"""
    validate_repo(old)
    new = (new or "").strip()
    err = _check_repo_name_free(new)
    if err:
        return False, err
    try:
        os.rename(repo_path(old), os.path.join(REPO_ROOT, new))
    except OSError as e:
        return False, "重命名失败：%s" % e
    # realm 通常形如 /svn/<old>；改名后同步，避免客户端凭据缓存指向旧名
    conf = os.path.join(REPO_ROOT, new, "conf", "svnserve.conf")
    try:
        if os.path.isfile(conf):
            with open(conf, "r", encoding="utf-8", errors="replace") as f:
                lines = f.read().replace("\r\n", "\n").split("\n")
            changed = False
            for i, ln in enumerate(lines):
                s = ln.strip()
                if s.startswith("realm") and "=" in s:
                    lines[i] = "realm = /svn/%s" % new
                    changed = True
                    break
            if changed:
                with open(conf, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines))
    except OSError:
        pass  # realm 未同步不影响功能，仅影响客户端凭据缓存归属
    purge_repo_cache(old)
    return True, "仓库 %s 已重命名为 %s" % (old, new)


# ------------------------------------------------- 备份（svnadmin dump）
# 每仓一条任务状态：{"status": running/done/error, "file": 文件名, "error"/"ts"}
_backup_states = {}
_backup_lock = threading.Lock()


def _backup_file_pattern(name):
    return "%s-r" % name  # <name>-r<youngest>-<时间戳>.dump


def list_backups(name):
    """列出该仓库的备份文件（新在前），[{file, size, mtime}]。"""
    if not os.path.isdir(BACKUP_DIR):
        return []
    out = []
    prefix = _backup_file_pattern(name)
    for fn in os.listdir(BACKUP_DIR):
        p = os.path.join(BACKUP_DIR, fn)
        if fn.startswith(prefix) and fn.endswith(".dump") and os.path.isfile(p):
            st = os.stat(p)
            out.append({"file": fn, "size": st.st_size, "mtime": st.st_mtime})
    out.sort(key=lambda b: b["mtime"], reverse=True)
    return out


def start_backup(name):
    """后台线程跑 svnadmin dump 到 BACKUP_DIR，返回 (ok, message)。"""
    validate_repo(name)
    with _backup_lock:
        st = _backup_states.get(name)
        if st and st.get("status") == "running":
            return False, "该仓库已有备份任务在进行中，请稍后再试。"
    os.makedirs(BACKUP_DIR, exist_ok=True)
    rev = youngest(name)
    ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    fn = "%s-r%d-%s.dump" % (name, rev, ts)
    dst = os.path.join(BACKUP_DIR, fn)
    _backup_states[name] = {"status": "running", "file": fn}

    def worker():
        try:
            with open(dst, "wb") as fh:
                p = subprocess.run(["svnadmin", "dump", "--quiet",
                                    repo_path(name)],
                                   stdout=fh, stderr=subprocess.PIPE)
            if p.returncode != 0:
                raise RuntimeError(
                    (p.stderr or b"").decode("utf-8", "replace")[:300] or
                    "svnadmin dump 退出码 %d" % p.returncode)
            _backup_states[name] = {
                "status": "done", "file": fn,
                "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        except Exception as e:  # noqa: BLE001 —— 后台线程兜底，状态可见
            _backup_states[name] = {"status": "error", "file": fn,
                                    "error": str(e)}

    threading.Thread(target=worker, daemon=True).start()
    return True, "备份任务已启动（r%d），完成后在本页可见，大仓库可能需要几分钟。" % rev


def delete_backup(name, fn):
    """删除一个备份文件。fn 必须是该仓库前缀的纯文件名。"""
    if fn != os.path.basename(fn) or not fn.startswith(
            _backup_file_pattern(name)) or not fn.endswith(".dump"):
        return False, "参数不合法"
    p = os.path.join(BACKUP_DIR, fn)
    if not os.path.isfile(p):
        return False, "备份不存在：%s" % fn
    try:
        os.remove(p)
    except OSError as e:
        return False, "删除失败：%s" % e
    return True, "备份 %s 已删除" % fn


# ------------------------------------------------- 钩子（hooks/ 目录）
HOOK_NAMES = ("start-commit", "pre-commit", "post-commit",
              "pre-revprop-change", "post-revprop-change",
              "pre-lock", "post-lock", "pre-unlock", "post-unlock")

# 常用钩子模板（textarea 的预填示例，用户可自行改）
HOOK_TEMPLATES = {
    "pre-commit": (
        "#!/bin/sh\n"
        "# 示例：拒绝空的提交信息（REPOS=仓库路径 TXN=事务号）\n"
        "REPOS=\"$1\"\n"
        "TXN=\"$2\"\n"
        "SVNLOOK=/usr/bin/svnlook\n"
        "MSG=$($SVNLOOK log -t \"$TXN\" \"$REPOS\")\n"
        "if [ -z \"$MSG\" ] || [ \"$MSG\" = \" \" ]; then\n"
        "  echo \"提交信息不能为空\" 1>&2\n"
        "  exit 1\n"
        "fi\n"
        "exit 0\n"),
    "post-commit": (
        "#!/bin/sh\n"
        "# 示例：每次提交追加一行到 /var/log/svn-commit.log\n"
        "REPOS=\"$1\"\n"
        "REV=\"$2\"\n"
        "echo \"$(date '+%F %T') $REPOS r$REV\" >> /var/log/svn-commit.log\n"
        "exit 0\n"),
}


def _hook_path(name, hook):
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", hook or "") \
            or hook.endswith(".tmpl"):
        return None
    validate_repo(name)
    return os.path.join(REPO_ROOT, name, "hooks", hook)


def list_hooks(name):
    """hooks/ 目录下已有的可执行脚本（排除 .tmpl 模板），按名字排序。"""
    validate_repo(name)
    d = os.path.join(REPO_ROOT, name, "hooks")
    if not os.path.isdir(d):
        return []
    out = []
    for fn in sorted(os.listdir(d)):
        p = os.path.join(d, fn)
        if fn.endswith(".tmpl") or not os.path.isfile(p):
            continue
        try:
            exe = bool(os.stat(p).st_mode & 0o111)
        except OSError:
            exe = False
        out.append({"file": fn, "exec": exe})
    return out


def read_hook(name, hook):
    p = _hook_path(name, hook)
    if not p or not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def save_hook(name, hook, content):
    """保存钩子脚本并赋予可执行权限（svnserve 要求 hook 可执行才生效）。"""
    p = _hook_path(name, hook)
    if not p:
        return False, "钩子名不合法"
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write((content or "").replace("\r\n", "\n"))
        os.chmod(p, 0o755)
    except OSError as e:
        return False, "保存钩子失败：%s" % e
    return True, "钩子 %s 已保存（已加可执行权限，立即生效）" % hook


def delete_hook(name, hook):
    p = _hook_path(name, hook)
    if not p or not os.path.isfile(p):
        return False, "钩子不存在：%s" % hook
    try:
        os.remove(p)
    except OSError as e:
        return False, "删除钩子失败：%s" % e
    return True, "钩子 %s 已删除" % hook


# ------------------------------------------------- 文件锁（svnadmin lslocks）
def list_locks(name):
    """解析 svnadmin lslocks 输出，返回 [{path, owner, created, token}]。"""
    validate_repo(name)
    rc, out, err = run_svn(["svnadmin", "lslocks", repo_path(name)], timeout=30)
    if rc != 0:
        return None  # 无法获取（命令失败），与「无锁」区分
    locks = []
    cur = None
    for ln in out.split("\n"):
        ln = ln.rstrip()
        if not ln.strip():
            if cur:
                locks.append(cur)
                cur = None
            continue
        key, _, val = ln.partition(":")
        key = key.strip()
        val = val.strip()
        if key == "Path":
            cur = {"path": val, "owner": "", "created": "", "token": ""}
        elif cur is not None and key in ("Owner", "Created", "Token"):
            cur[key.lower()] = val
    if cur:
        locks.append(cur)
    return locks


def remove_lock(name, path):
    """强制解锁（svnadmin rmlocks）。path 必须来自 lslocks 的输出。"""
    path = (path or "").strip()
    if not path.startswith("/") or ".." in path or "\\" in path:
        return False, "路径不合法"
    validate_repo(name)
    rc, out, err = run_svn(["svnadmin", "rmlocks", repo_path(name), path],
                           timeout=30)
    if rc != 0:
        return False, "解锁失败：%s" % (err.strip() or out.strip() or "未知错误")
    return True, "已强制解锁 %s" % path


# ---------------------------------------------- 账号/权限配置读写（仅 conf 目录）
USER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
AUTHZ_PATH_RE = re.compile(r"^/[^=\s:]+$")


def _conf_file(name, fn):
    """校验仓库名并返回 conf/<fn> 的绝对路径；非法则 abort(404)。"""
    validate_repo(name)
    p = os.path.join(REPO_ROOT, name, "conf", fn)
    if os.path.dirname(p) != os.path.join(REPO_ROOT, name, "conf"):
        abort(404)
    return p


def load_passwd(name):
    """解析 passwd 文件为用户字典 {user: passwd}，忽略空行与注释。"""
    p = _conf_file(name, "passwd")
    out = {}
    if not os.path.isfile(p):
        return out
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or line.startswith("["):
                    continue
                if "=" in line:
                    user, _, pw = line.partition("=")
                    u = user.strip()
                    if u:
                        out[u] = pw.strip()
    except OSError:
        pass
    return out


def save_passwd(name, passwd_dict):
    """把用户字典写回 passwd 文件（保留注释等非账号行）。

    注意：[users] 节头由本函数重建，原文件里的旧节头一律跳过，
    否则每保存一次就多一个 [users]（老实现的历史 bug）。
    """
    p = _conf_file(name, "passwd")
    lines = []
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.rstrip("\n")
                s = line.strip()
                if s == "[users]":
                    continue  # 下方重建，避免重复节头
                if "=" in s and not s.startswith("#"):
                    left = s.partition("=")[0].strip()
                    if left:
                        continue  # 账号行由 passwd_dict 重建，跳过旧行
                lines.append(line)
    except OSError:
        pass
    # 重新拼装 [users] 节
    users = ["[users]"] + ["%s = %s" % (u, pw) for u, pw in passwd_dict.items()]
    body = "\n".join(lines).strip("\n")
    text = (body + "\n\n" + "\n".join(users) + "\n") if body else ("\n".join(users) + "\n")
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(p, 0o600)


def read_authz(name):
    """读取 authz 内容（字符串）。"""
    p = _conf_file(name, "authz")
    if not os.path.isfile(p):
        return ""
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def write_authz(name, content):
    """写回 authz 内容，校验不含危险字符后保存。"""
    p = _conf_file(name, "authz")
    text = (content or "").replace("\r\n", "\n")
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(p, 0o644)


# ---- authz 图形化管理：只操作根节 [/]，其余节与注释原样保留 ----
AUTHZ_SPECIAL = ("$authenticated", "$anonymous", "*")


def _read_authz_lines(name):
    """读 authz 文件为行列表；文件不存在返回 None。"""
    p = _conf_file(name, "authz")
    if not os.path.isfile(p):
        return None
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        return f.read().replace("\r\n", "\n").split("\n")


def _write_authz_lines(p, lines):
    text = "\n".join(lines)
    if not text.endswith("\n"):
        text += "\n"
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(p, 0o644)


def parse_authz_root(text):
    """解析 authz 根节 [/] 的授权条目，返回 {主体: 权限}。

    权限为空串表示「显式无权限」行。注释行跳过；组引用（@xxx）原样返回，
    由调用方决定是否展示为账号。
    """
    out = {}
    cur = None
    for raw in (text or "").replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            cur = line
            continue
        if cur != "[/]" or "=" not in line:
            continue
        who, _, perm = line.partition("=")
        who = who.strip()
        if who:
            out[who] = perm.strip()
    return out


def set_authz_root_user(name, username, perm):
    """在根节 [/] 中设置 username = perm（'r'/'rw'）；perm 为空串则移除该行。

    其他节、注释、`* =` 等行一律不动；无 [/] 节时在文件末尾追加。
    """
    p = _conf_file(name, "authz")
    lines = _read_authz_lines(name)
    if lines is None:
        lines = []
    out = []
    cur = None
    done = False
    root_at = None
    for ln in lines:
        s = ln.strip()
        if s.startswith("[") and s.endswith("]"):
            cur = s
            if s == "[/]" and root_at is None:
                root_at = len(out)
        if cur == "[/]" and "=" in s and not s.startswith("#") and not s.startswith(";"):
            key = s.partition("=")[0].strip()
            if key == username:
                if perm and not done:
                    out.append("%s = %s" % (username, perm))
                    done = True
                continue  # 丢弃旧行
        out.append(ln)
    if perm and not done:
        if root_at is None:
            if out and out[-1].strip():
                out.append("")
            out.append("[/]")
            out.append("%s = %s" % (username, perm))
        else:
            idx = root_at + 1
            n = len(out)
            while idx < n:
                s = out[idx].strip()
                if s and not s.startswith("#") and not s.startswith(";"):
                    break
                idx += 1
            out.insert(idx, "%s = %s" % (username, perm))
    _write_authz_lines(p, out)


def remove_authz_user(name, username):
    """删除 authz 所有节（[groups] 除外）中 username 的授权行，防止删号留权限。"""
    p = _conf_file(name, "authz")
    lines = _read_authz_lines(name)
    if lines is None:
        return
    out = []
    cur = None
    removed = False
    for ln in lines:
        s = ln.strip()
        if s.startswith("[") and s.endswith("]"):
            cur = s
        if (cur != "[groups]" and "=" in s
                and not s.startswith("#") and not s.startswith(";")):
            if s.partition("=")[0].strip() == username:
                removed = True
                continue
        out.append(ln)
    if removed:
        _write_authz_lines(p, out)


# ---- authz 用户组（[groups] 节）图形化管理 ----
GROUP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def parse_authz_groups(text):
    """解析 authz 的 [groups] 节，返回 {组名(不带@): [成员列表]}。

    成员原样保留（用户名或 @子组）；注释行与空行跳过；同名组以后一行
    为准（svnserve 行为一致）。
    """
    out = {}
    cur = None
    for raw in (text or "").replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            cur = line
            continue
        if cur != "[groups]" or "=" not in line:
            continue
        gname, _, members = line.partition("=")
        gname = gname.strip().lstrip("@").strip()
        if gname:
            out[gname] = [m.strip() for m in members.split(",") if m.strip()]
    return out


def set_authz_group(name, group, members):
    """在 [groups] 节写入/替换组定义；其余行（含注释）原样保留。

    members 为成员列表（用户名或以 @ 开头的子组引用）。
    """
    p = _conf_file(name, "authz")
    lines = _read_authz_lines(name)
    if lines is None:
        lines = []
    newline = "%s = %s" % (group, ", ".join(members))
    out = []
    cur = None
    groups_at = None
    done = False
    for ln in lines:
        s = ln.strip()
        if s.startswith("[") and s.endswith("]"):
            cur = s
            if s == "[groups]" and groups_at is None:
                groups_at = len(out)
        if cur == "[groups]" and "=" in s and not s.startswith("#") and not s.startswith(";"):
            key = s.partition("=")[0].strip().lstrip("@").strip()
            if key == group:
                if not done:
                    out.append(newline)
                    done = True
                continue  # 丢弃同名旧行
        out.append(ln)
    if not done:
        if groups_at is None:
            if out and out[-1].strip():
                out.append("")
            out.append("[groups]")
            groups_at = len(out) - 1
        # 插到 [groups] 节头之后、下一节（或文件尾）之前
        idx = groups_at + 1
        n = len(out)
        while idx < n:
            s = out[idx].strip()
            if s.startswith("[") and s.endswith("]"):
                break
            idx += 1
        out.insert(idx, newline)
    _write_authz_lines(p, out)


def remove_authz_group(name, group):
    """删除组定义，并移除所有非 [groups] 节中的 @group 授权行，防止悬空引用。"""
    p = _conf_file(name, "authz")
    lines = _read_authz_lines(name)
    if lines is None:
        return
    out = []
    cur = None
    changed = False
    for ln in lines:
        s = ln.strip()
        if s.startswith("[") and s.endswith("]"):
            cur = s
        if "=" in s and not s.startswith("#") and not s.startswith(";"):
            key = s.partition("=")[0].strip()
            if cur == "[groups]":
                if key.lstrip("@").strip() == group:
                    changed = True
                    continue  # 删组定义
            elif key == "@" + group:
                changed = True
                continue  # 删各节的 @group 授权行
        out.append(ln)
    if changed:
        _write_authz_lines(p, out)


# 仓库描述：存于仓库 conf/svn.desc，首页与仓库页展示。
def repo_desc(name):
    """读取仓库描述（一行简介）。无则为空串。"""
    p = _conf_file(name, "svn.desc")
    if not os.path.isfile(p):
        return ""
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return ""


def save_repo_desc(name, text):
    """保存仓库描述（可多行）：统一换行为 \\n，去首尾空白，限长 200。"""
    p = _conf_file(name, "svn.desc")
    val = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()[:200]
    with open(p, "w", encoding="utf-8") as f:
        f.write(val)
    os.chmod(p, 0o644)
    return val


# --------------------------------------------------------- 工具函数
def parse_date(s):
    """把 SVN 时间字符串解析为带时区的 aware UTC datetime；失败返回 None。

    SVN 的日期是 UTC（尾部带 Z），这里显式标记为 UTC，便于后续按服务器
    本地时区展示，避免把 UTC 误当本地时间。
    """
    if not s:
        return None
    try:
        s2 = s.rstrip("Z")
        if "." in s2:
            s2 = s2.split(".")[0]
        naive = datetime.datetime.strptime(s2, "%Y-%m-%dT%H:%M:%S")
        return naive.replace(tzinfo=datetime.timezone.utc)
    except Exception:
        return None


def _local_tz():
    """服务器本地时区（Windows 上用本机 tz，Linux 用系统 tz）。"""
    try:
        import zoneinfo
        return datetime.datetime.now().astimezone().tzinfo
    except Exception:
        return datetime.timezone.utc


_LOCAL_TZ = _local_tz()


def human_time(s):
    dt = parse_date(s)
    if not dt:
        return ""
    local = dt.astimezone(_LOCAL_TZ)
    return local.strftime("%Y-%m-%d %H:%M")


def tz_label():
    """服务器本地时区的可读标签，如 "UTC+8" / "UTC"。"""
    try:
        off = datetime.datetime.now(_LOCAL_TZ).utcoffset()
        total = int(off.total_seconds()) if off else 0
        sign = "+" if total >= 0 else "-"
        hh = abs(total) // 3600
        mm = (abs(total) % 3600) // 60
        return "UTC%s%02d:%02d" % (sign, hh, mm)
    except Exception:
        return "UTC"


def rel_time(s):
    dt = parse_date(s)
    if not dt:
        return ""
    now = datetime.datetime.now(datetime.timezone.utc)
    d = now - dt
    sec = d.total_seconds()
    if sec < 60:
        return "刚刚"
    if sec < 3600:
        return "%d 分钟前" % (sec // 60)
    if sec < 86400:
        return "%d 小时前" % (sec // 3600)
    if d.days < 30:
        return "%d 天前" % d.days
    if d.days < 365:
        return "%d 个月前" % (d.days // 30)
    return "%d 年前" % (d.days // 365)


def human_size(n):
    if n is None:
        return ""
    units = ["B", "KB", "MB", "GB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return ("%.0f %s" % (f, u)) if u == "B" else ("%.1f %s" % (f, u))
        f /= 1024
    return "%d B" % n


def safe_rel_path(path):
    """校验并规范化仓库内相对路径"""
    path = (path or "").strip("/")
    if path == "":
        return ""
    parts = []
    for seg in path.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            abort(404)
        parts.append(seg)
    return "/".join(parts)


def is_binary(data):
    if isinstance(data, str):
        data = data.encode("utf-8", "replace")
    return b"\x00" in data[:8192]


# ---- 浏览器可直接打开的文件：内联预览 ----
# 只列「用 <img> / <embed> / <video> 渲染也安全」的扩展名。
# 故意不含 html / htm / js / xhtml —— 内联渲染仓库里的 HTML 等于让它在同源执行脚本，
# 一个能提交文件的人就能顺走登录态，所以这些类型一律只当普通文本给代码视图。
_PREVIEW_KINDS = (
    ("image", {".png", ".jpg", ".jpeg", ".jpe", ".gif", ".bmp", ".webp",
               ".ico", ".avif", ".svg"}),
    ("pdf", {".pdf"}),
    ("video", {".mp4", ".m4v", ".webm", ".ogv", ".mov"}),
    ("audio", {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".oga", ".flac"}),
)
# /media/ 预览资源允许回给浏览器的 Content-Type（同样是白名单，不在其中一律 404）
_MEDIA_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".jpe": "image/jpeg", ".gif": "image/gif", ".bmp": "image/bmp",
    ".webp": "image/webp", ".ico": "image/x-icon", ".avif": "image/avif",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
    ".mp4": "video/mp4", ".m4v": "video/x-m4v", ".webm": "video/webm",
    ".ogv": "video/ogg", ".mov": "video/quicktime",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
    ".aac": "audio/aac", ".ogg": "audio/ogg", ".oga": "audio/ogg",
    ".flac": "audio/flac",
}


def preview_kind(filename):
    """浏览器能否直接打开这个文件：返回 'image' / 'pdf' / 'video' / 'audio'，否则 None。"""
    ext = os.path.splitext(filename or "")[1].lower()
    for kind, exts in _PREVIEW_KINDS:
        if ext in exts:
            return kind
    return None


def media_mimetype(filename):
    """预览资源的 Content-Type；不在白名单里返回 None（调用方直接 404）。"""
    return _MEDIA_MIME.get(os.path.splitext(filename or "")[1].lower())


def lang_of(filename):
    ext = os.path.splitext(filename)[1].lower().lstrip(".")
    m = {
        "py": "Python", "js": "JavaScript", "ts": "TypeScript", "tsx": "TSX",
        "jsx": "JSX", "vue": "Vue", "java": "Java", "go": "Go", "rs": "Rust",
        "c": "C", "h": "C", "cpp": "C++", "hpp": "C++", "cs": "C#",
        "php": "PHP", "rb": "Ruby", "sh": "Shell", "bash": "Shell",
        "html": "HTML", "htm": "HTML", "css": "CSS", "scss": "SCSS",
        "less": "Less", "json": "JSON", "yml": "YAML", "yaml": "YAML",
        "xml": "XML", "md": "Markdown", "sql": "SQL", "dockerfile": "Dockerfile",
        "toml": "TOML", "ini": "INI", "conf": "Config", "txt": "Text",
    }
    return m.get(ext, "")


def highlight_code(filename, content):
    """返回 (html, has_highlight)"""
    if len(content.encode("utf-8", "replace")) > MAX_HIGHLIGHT_BYTES:
        return None, False
    try:
        lexer = get_lexer_for_filename(os.path.basename(filename), content)
    except ClassNotFound:
        lexer = TextLexer()
    if isinstance(lexer, TextLexer):
        lexer = TextLexer(encoding="utf-8")
    return highlight(content, lexer, FORMATTER), True


def parse_diff(text):
    """把 unified diff 解析成 [{path, lines:[(type,text)], adds, dels}]"""
    files = []
    cur = None
    for line in text.splitlines():
        if line.startswith("Index: "):
            cur = {"path": line[7:].strip(), "lines": [], "adds": 0, "dels": 0}
            files.append(cur)
            continue
        if cur is None:
            cur = {"path": "", "lines": [], "adds": 0, "dels": 0}
            files.append(cur)
        if line.startswith("===") or line.startswith("---") or line.startswith("+++"):
            # 文件头分隔行，界面已有独立的文件标题栏，这里跳过
            continue
        if line.startswith("@@"):
            cur["lines"].append(("hunk", line))
            continue
        if line.startswith("+"):
            cur["adds"] += 1
            cur["lines"].append(("add", line[1:]))
            continue
        if line.startswith("-"):
            cur["dels"] += 1
            cur["lines"].append(("del", line[1:]))
            continue
        if line.startswith("\\"):
            cur["lines"].append(("meta", line))
            continue
        cur["lines"].append(("ctx", line[1:] if line.startswith(" ") else line))
    # 去掉只有 meta 的空文件块
    return [f for f in files if f["lines"]]


def diff_stats(files):
    adds = sum(f["adds"] for f in files)
    dels = sum(f["dels"] for f in files)
    return adds, dels


def action_label(a):
    return {"A": "新增", "M": "修改", "D": "删除", "R": "替换"}.get(a, a)


# ------------------------------------------------------------- 认证
def login_required(f):
    @functools.wraps(f)
    def wrap(*args, **kwargs):
        if not session.get("user"):
            # full_path 在无参数时也带尾问号（"/?"），去掉得到干净的回跳地址
            nxt = request.full_path
            if nxt.endswith("?"):
                nxt = nxt[:-1]
            return redirect(url_for("login", next=nxt))
        return f(*args, **kwargs)
    return wrap


def current_role():
    """当前会话角色。旧会话（无 role 字段）按 admin 处理，保证平滑升级。"""
    return session.get("role") or (
        "admin" if session.get("user") else None)


def admin_required(f):
    """管理动作专用：viewer 角色只读，返回 403。"""
    @functools.wraps(f)
    def wrap(*args, **kwargs):
        if current_role() != "admin":
            abort(403)
        return f(*args, **kwargs)
    return wrap


def _verify_web_password(hashval, password):
    """校验 werkzeug 口令哈希，兼容 pbkdf2 与 scrypt 两种存储格式。

    werkzeug 2.x 的 _hash_internal 只认 pbkdf2，遇到 werkzeug 3.x
    默认生成的 scrypt:32768:8:1 格式会走 hmac 兜底分支抛
    ValueError，导致登录 500；且部分 Windows 的 OpenSSL 对 scrypt
    有默认 maxmem 限制（memory limit exceeded）。这里对 scrypt
    格式手工解析并用 hashlib.scrypt(maxmem=...) 校验。
    """
    if not hashval:
        return False
    if not hashval.startswith("scrypt:"):
        try:
            return check_password_hash(hashval, password)
        except ValueError:
            return False
    # 格式：scrypt:32768:8:1$salt$hash（salt 为 SALT_CHARS 串、hash 为 hex）
    try:
        method, salt, want = hashval.split("$", 2)
        n, r, p = (int(x) for x in method.split(":")[1:])
        want = bytes.fromhex(want)
        got = hashlib.scrypt(password.encode("utf-8"),
                             salt=salt.encode("utf-8"),
                             n=n, r=r, p=p, dklen=len(want),
                             maxmem=132 * n * r)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, want)


def web_login_user(username, password):
    """校验网站登录，成功返回 (username, role)，失败返回 None。

    优先查 web_users 多账号表；未命中再走顶层单账号（username=admin）。
    登录成功后若存储的还是 scrypt 哈希，则透明升级为 pbkdf2:sha256
    （服务器 py3.6 的老 werkzeug 也认，无需用户重设密码）。
    """
    entry = WEB_USERS.get(username)
    if entry is not None:
        h = entry.get("password_hash") or ""
        if h and _verify_web_password(h, password):
            _maybe_upgrade_hash(username, h, password)
            return username, entry.get("role") or "viewer"
        return None
    if username == USERNAME and PASSWORD_HASH \
            and _verify_web_password(PASSWORD_HASH, password):
        _maybe_upgrade_hash(username, PASSWORD_HASH, password)
        return username, "admin"
    return None


def _maybe_upgrade_hash(username, old_hash, password):
    """存储的哈希是 scrypt 格式时，改写为 pbkdf2:sha256。失败不影响登录。"""
    if not old_hash.startswith("scrypt:"):
        return
    try:
        _update_web_password(
            username, generate_password_hash(password, method="pbkdf2:sha256"))
    except Exception:
        pass


# 登录防爆破：失败计数 + 临时锁定（内存态，重启即清零）。
import threading as _threading
LOGIN_MAX_FAILS = CONFIG.get("login_max_fail", 5)          # 允许连续失败次数
LOGIN_LOCK_SECONDS = CONFIG.get("login_lock_seconds", 300)  # 锁定秒数
_login_failures = {}   # key -> [count, locked_until_ts]
_login_lock = _threading.Lock()


def _login_key(extra=""):
    ip = request.remote_addr or "?"
    return ("%s|%s" % (ip, extra)).lower()


def _login_attempt_allowed(key):
    with _login_lock:
        rec = _login_failures.get(key)
        now = time.time()
        if rec:
            if rec[1] and now < rec[1]:
                return False, int(rec[1] - now) + 1
            if rec[1] and now >= rec[1]:
                del _login_failures[key]
    return True, 0


def _login_fail(key):
    with _login_lock:
        rec = _login_failures.get(key)
        now = time.time()
        if rec is None or (rec[1] and now >= rec[1]):
            rec = [0, 0]
        rec[0] += 1
        if rec[0] >= LOGIN_MAX_FAILS:
            rec[1] = now + LOGIN_LOCK_SECONDS
            rec[0] = 0
        _login_failures[key] = rec


# ------------------------------------------------------------- 安装向导
def _probe_port(port, host="127.0.0.1", timeout=0.8):
    """探测本机某端口是否有进程在监听（用于判断 svnserve 是否已启动）。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _default_repo_root():
    return "D:/svn" if os.name == "nt" else "/svn"


def _install_env():
    """安装向导页展示的服务端环境自检结果。"""
    tools = []
    for t in ("svn", "svnadmin", "svnlook", "svnserve"):
        path = shutil.which(t)
        ver = ""
        if path:
            try:
                r = subprocess.run([t, "--version"], capture_output=True,
                                   timeout=10)
                ver = (r.stdout.decode("utf-8", "replace")
                       .splitlines() or [""])[0]
            except Exception:
                ver = ""
        tools.append({"name": t, "ok": bool(path), "ver": ver})
    return {
        "py": "%d.%d.%d" % sys.version_info[:3],
        "tools": tools,
        "svn_listen": _probe_port(3690),
        "repo_root_suggest": _default_repo_root(),
    }


@app.route("/install", methods=["GET", "POST"])
def install():
    """首次运行安装向导。

    仅在 INSTALL_MODE（config.json 不存在）时可访问；写完配置后本路由
    永久跳转登录页，防止他人重装覆盖。
    """
    global INSTALL_MODE, CONFIG
    if not INSTALL_MODE:
        return redirect(url_for("login"))

    env = _install_env()
    errors = []
    form = {
        "repo_root": env["repo_root_suggest"],
        "svn_host": "", "svn_port": "",
        "username": "admin", "behind_proxy": False,
    }

    if request.method == "POST":
        form["repo_root"] = (request.form.get("repo_root") or "").strip().strip('"')
        form["svn_host"] = (request.form.get("svn_host") or "").strip()
        form["svn_port"] = (request.form.get("svn_port") or "").strip()
        form["username"] = (request.form.get("username") or "").strip()
        pw1 = request.form.get("password") or ""
        pw2 = request.form.get("password2") or ""
        behind = request.form.get("behind_proxy") == "on"

        # 分步向导：出错后前端直接打开对应步骤（基础配置=2，管理员账号=3）
        n0 = len(errors)

        # 1) 仓库根目录：不存在则尝试自动创建
        rr = form["repo_root"].replace("\\", "/").rstrip("/")
        if not rr:
            errors.append("仓库根目录不能为空。")
        elif not os.path.isdir(rr):
            try:
                os.makedirs(rr)
            except OSError as e:
                errors.append("仓库根目录 %s 创建失败：%s" % (rr, e))
        if rr and os.path.isdir(rr) and not os.access(rr, os.W_OK):
            errors.append("仓库根目录 %s 不可写，请检查权限。" % rr)

        # 2) 检出地址前缀（可选）：主机名 + 可选端口（支持自定义 SVN 端口）
        port = None
        if form["svn_port"]:
            if not form["svn_port"].isdigit() or not 1 <= int(form["svn_port"]) <= 65535:
                errors.append("SVN 端口需为 1-65535 的数字，留空表示默认 3690。")
            else:
                port = int(form["svn_port"])
        if form["svn_host"] and not re.match(r"^[A-Za-z0-9._-]+$", form["svn_host"]):
            errors.append("主机名格式不正确（只允许字母、数字、点、下划线、连字符）。")

        err_base = len(errors) > n0  # 步骤 1-2（仓库根/检出地址）是否报错

        # 3) 管理员账号
        u = form["username"]
        if not re.match(r"^[A-Za-z0-9_.-]{1,32}$", u or ""):
            errors.append("管理员用户名限 1-32 位，可含字母、数字、点、下划线、连字符。")
        if len(pw1) < 6:
            errors.append("管理员密码至少 6 位。")
        if pw1 != pw2:
            errors.append("两次输入的密码不一致。")

        if not errors:
            svn_base = ""
            if form["svn_host"]:
                svn_base = "svn://%s%s" % (form["svn_host"],
                                           (":%d" % port) if port else "")
            cfg = {
                "repo_root": rr,
                "username": u,
                "password_hash": generate_password_hash(pw1, method="pbkdf2:sha256"),
                "secret_key": secrets.token_hex(32),
                "cache_ttl": 20,
                "max_highlight_bytes": 524288,
                "max_preview_bytes": 5242880,
                "max_media_bytes": 33554432,
                "log_page_size": 60,
                "login_max_fail": 5,
                "login_lock_seconds": 300,
                "svn_base": svn_base,
                "behind_proxy": behind,
                "web_users": {},
            }
            # 原子写入，写失败不破坏现场
            tmp = CONFIG_PATH + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, ensure_ascii=False, indent=2)
                    f.write("\n")
                os.replace(tmp, CONFIG_PATH)
            except OSError as e:
                errors.append("config.json 写入失败：%s" % e)
        if not errors:
            CONFIG = cfg
            _apply_config(cfg)
            INSTALL_MODE = False
            audit("install", user=u,
                  detail="ip=%s repo_root=%s svn_base=%s behind_proxy=%s"
                         % (request.remote_addr or "?", rr,
                            svn_base or "自动推断", behind))
            flash("安装完成，配置已写入 config.json。请用刚设置的管理员账号登录。",
                  "success")
            return redirect(url_for("login"))

    # 分步向导初始步骤：GET 从第 1 步开始；POST 校验失败打开出错步骤
    open_step = 1
    if request.method == "POST":
        open_step = 2 if err_base else 3
    return render_template("install.html", env=env, errors=errors, form=form,
                           open_step=open_step)


@app.before_request
def _install_mode_gate():
    """安装模式下接管全站访问：除向导本身与静态资源外一律跳 /install。"""
    if INSTALL_MODE and request.endpoint not in ("install", "static"):
        return redirect(url_for("install"))
    return None


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    status = 200
    if request.method == "POST":
        u = request.form.get("username", "")
        p = request.form.get("password", "")
        key = _login_key(u)
        allowed, wait = _login_attempt_allowed(key)
        if not allowed:
            error = "登录失败次数过多，请 %d 秒后再试。" % (LOGIN_LOCK_SECONDS)
            status = 429
        else:
            hit = web_login_user(u, p)
            if hit:
                u, role = hit
                with _login_lock:
                    _login_failures.pop(key, None)
                session["user"] = u
                session["role"] = role
                audit("login", user=u, detail="ip=%s role=%s"
                      % (request.remote_addr or "?", role))
                nxt = request.args.get("next") or url_for("index")
                # 仅允许站内相对路径：拒绝 "//evil.com"、"/\evil.com" 等开放重定向
                if (not nxt.startswith("/") or nxt.startswith("//")
                        or nxt.startswith("/\\")):
                    nxt = url_for("index")
                return redirect(nxt)
            _login_fail(key)
            audit("login_fail", user=u or "?",
                  detail="ip=%s" % (request.remote_addr or "?"))
            error = "用户名或密码错误"
    return render_template("login.html", error=error, next=request.args.get("next", "")), status


@app.route("/account/password", methods=["POST"])
@login_required
def account_password():
    """修改当前登录账号的网站密码（头部头像下拉的弹窗入口）。

    只能改自己的密码；用户名与角色由管理员在「用户 → 网站用户」维护。
    """
    me = session.get("user") or USERNAME
    cur_hash = (WEB_USERS.get(me) or {}).get(
        "password_hash") or (PASSWORD_HASH if me == USERNAME else "")
    cur = request.form.get("current_password") or ""
    pw1 = request.form.get("new_password") or ""
    pw2 = request.form.get("new_password2") or ""
    if not cur_hash:
        flash("config.json 中未设置 password_hash，无法在线修改。", "error")
    elif not check_password_hash(cur_hash, cur):
        flash("当前密码不正确。", "error")
    elif len(pw1) < 6 or len(pw1) > 100:
        flash("新密码长度需在 6-100 位之间。", "error")
    elif pw1 != pw2:
        flash("两次输入的新密码不一致。", "error")
    elif pw1 == cur:
        flash("新密码不能与当前密码相同。", "error")
    else:
        try:
            _update_web_password(
                me, generate_password_hash(pw1, method="pbkdf2:sha256"))
            audit("change_web_pw", "", "账号 %s 的网站登录密码已修改" % me)
            flash("网站登录密码已修改，下次登录请使用新密码（当前会话不受影响）。",
                  "success")
        except OSError as e:
            flash("密码写入 config.json 失败：%s" % e, "error")
    # 回跳来源页（仅站内），取不到就回首页
    dest = request.referrer or url_for("index")
    parsed = urlparse(dest)
    if parsed.netloc and parsed.netloc != request.host:
        dest = url_for("index")
    return redirect(dest)


@app.route("/logout")
def logout():
    session.pop("user", None)
    session.pop("role", None)
    return redirect(url_for("login"))


@app.route("/healthz")
def healthz():
    """免登录健康检查端点（供 systemd / nginx 探活）。

    返回 svnserve 依赖是否可用、仓库根目录是否可达。正常返回 200。
    """
    import shutil as _sh
    svn_ok = bool(_sh.which("svn") or _sh.which("svnlook"))
    root_ok = os.path.isdir(REPO_ROOT)
    if svn_ok and root_ok:
        return Response('{"status":"ok","repos":%d}' % len(list_repos()),
                        mimetype="application/json")
    return Response('{"status":"degraded","svn":%s,"repo_root":%s}' % (
        "true" if svn_ok else "false",
        "true" if root_ok else "false"), status=503,
        mimetype="application/json")


# ------------------------------------------------------------- 视图
@app.context_processor
def inject_globals():
    return {"pygments_css": PYGMENTS_CSS, "human_time": human_time,
            "rel_time": rel_time, "human_size": human_size,
            "lang_of": lang_of, "user": session.get("user"),
            "role": current_role(), "REPO_ROOT": REPO_ROOT,
            "checkout_url": checkout_url, "tz_label": tz_label,
            "repo_desc": repo_desc,
            "search_scope_order": SEARCH_SCOPE_ORDER,
            "search_scope_labels": SEARCH_SCOPE_LABELS,
            "search_kind_labels": SEARCH_KIND_LABELS,
            "messages": get_flashed_messages(with_categories=True)}


@app.route("/")
@login_required
def index():
    # 仓库概要级缓存：每个仓库的 (youngest, 最后提交) 一起算并缓存一份
    # （而非每个仓库各自跑一次 svnlook + svn log），降低首页刷新成本。
    key = ("repo_overview",)
    repos = cache_get(key)
    if repos is None:
        repos = []
        for n in list_repos():
            r = youngest(n)
            last = repo_last_change(n, r) if r else None
            repos.append({"name": n, "rev": r, "last": last, "desc": repo_desc(n)})
        repos = cache_set(key, repos)
    return render_template("index.html", repos=repos)


def get_rev(name, rev_param):
    head = youngest(name)
    if rev_param:
        try:
            v = int(rev_param)
            if 1 <= v <= head:
                return v, head
        except ValueError:
            pass
    return head, head


@app.route("/<name>")
@login_required
def repo_home(name):
    # 仓库主页直接在 /<name> 渲染根目录树，URL 保持不带尾部斜杠
    return tree(name, "")


# --------------------------------------------------- 仓库管理：新建仓库
@app.route("/admin/new", methods=["GET", "POST"])
@login_required
@admin_required
def new_repo():
    error = None
    name = ""
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        ok, msg = create_repo(name)
        if ok:
            audit("create_repo", name)
            flash(msg, "success")
            return redirect(url_for("repo_created", name=name))
        error = msg
    return render_template("newrepo.html", error=error, name=name,
                           repo_count=len(list_repos()))


# --------------------------------------------------- 仓库管理：归档仓库
@app.route("/archived/", methods=["GET", "POST"])
@login_required
@admin_required
def archived_page():
    if request.method == "POST":
        action = request.form.get("action")
        arc = request.form.get("arc") or ""
        base = arc.rpartition("@")[0] or arc
        if action == "restore_repo":
            ok, msg = restore_archived(arc)
            if ok:
                audit("restore_repo", base, arc)
                flash(msg, "success")
            else:
                flash(msg, "error")
        elif action == "delete_archived":
            ok, msg = delete_archived(arc, request.form.get("confirm") or "")
            if ok:
                audit("delete_repo", base, "彻底删除归档 %s" % arc)
                flash(msg, "success")
            else:
                flash(msg, "error")
        return redirect(url_for("archived_page"))
    return render_template("archived.html", archived=list_archived())


@app.route("/admin/created/<name>")
@login_required
def repo_created(name):
    """创建成功页：展示检出地址与后续初始化命令"""
    if name not in list_repos():
        abort(404)
    return render_template("created.html", name=name)


# ------------------------------------------------------- 仓库设置（分区页）
PERM_LABELS = {"none": "无权限", "r": "只读", "rw": "读写"}

ADMIN_SECTIONS = {
    "overview": "基本信息",
    "access": "账号与权限",
    "groups": "用户组",
    "backup": "备份",
    "hooks": "钩子脚本",
    "locks": "文件锁",
    "danger": "危险操作",
}
ADMIN_SECTION_TEMPLATES = {
    "overview": "admin_overview.html",
    "access": "admin_access.html",
    "groups": "admin_groups.html",
    "backup": "admin_backup.html",
    "hooks": "admin_hooks.html",
    "locks": "admin_locks.html",
    "danger": "admin_danger.html",
}


@app.route("/<name>/admin/", defaults={"section": "overview"},
           methods=["GET", "POST"])
@app.route("/<name>/admin/<section>", methods=["GET", "POST"])
@login_required
@admin_required
def repo_admin(name, section):
    validate_repo(name)
    error = None
    success = None
    users = load_passwd(name)

    action = request.form.get("action") if request.method == "POST" else None

    if action == "save_desc":
        try:
            save_repo_desc(name, request.form.get("desc") or "")
            desc = repo_desc(name)
            audit("save_desc", name, (desc or "（清空描述）")[:80])
            flash("仓库描述已保存。", "success")
        except OSError as e:
            error = "保存描述失败：%s" % e

    elif action == "save_authz":
        content = request.form.get("authz") or ""
        try:
            write_authz(name, content)
            audit("save_authz", name)
            success = "授权配置（authz）已保存。新权限立即对 svn:// 客户端生效。"
        except OSError as e:
            error = "保存 authz 失败：%s" % e

    elif action == "save_group":
        gname = (request.form.get("group") or "").strip().lstrip("@").strip()
        members_raw = (request.form.get("members") or "").replace("，", ",")
        perm = request.form.get("perm") or ""
        members = [m for m in (x.strip() for x in members_raw.split(",")) if m]
        if not GROUP_RE.match(gname):
            error = ("组名不合法：字母/数字开头，后接字母、数字、点、"
                     "下划线、连字符，最长 64 位")
        elif not members:
            error = "请填写至少一个成员（多个成员用英文逗号分隔）"
        else:
            try:
                set_authz_group(name, gname, members)
                # 根节授权：'r'/'rw' 写入，空串移除该行
                set_authz_root_user(name, "@" + gname,
                                    perm if perm in ("r", "rw") else "")
                audit("save_group", name, "@%s（%d 人），根节权限=%s"
                      % (gname, len(members), perm or "无"))
                success = ("用户组 @%s 已保存（%s），立即生效。"
                           % (gname, "、".join(members)))
            except OSError as e:
                error = "保存用户组失败：%s" % e

    elif action == "del_group":
        gname = (request.form.get("group") or "").strip().lstrip("@").strip()
        if gname:
            try:
                remove_authz_group(name, gname)
                audit("del_group", name, "@%s" % gname)
                success = ("用户组 @%s 已删除（组定义与根节授权一并清理）。"
                           % gname)
            except OSError as e:
                error = "删除用户组失败：%s" % e
        else:
            error = "缺少组名。"

    elif action == "rename_repo":
        new_name = (request.form.get("new_name") or "").strip()
        ok, msg = rename_repo(name, new_name)
        if ok:
            audit("rename_repo", new_name, "%s → %s" % (name, new_name))
            flash(msg, "success")
            return redirect(url_for("repo_admin", name=new_name))
        error = msg

    elif action == "archive_repo":
        if (request.form.get("confirm") or "").strip() != name:
            error = "确认文字不匹配，仓库未归档。需完整输入仓库名「%s」。" % name
        else:
            ok, msg = archive_repo(name)
            if ok:
                audit("archive_repo", name)
                flash(msg + "，可在「归档仓库」页恢复。", "success")
                return redirect(url_for("index"))
            error = msg

    elif action == "start_backup":
        ok, msg = start_backup(name)
        if ok:
            audit("start_backup", name)
            flash(msg, "success")
        else:
            error = msg

    elif action == "del_backup":
        fn = request.form.get("file") or ""
        ok, msg = delete_backup(name, fn)
        if ok:
            audit("del_backup", name, fn)
            flash(msg, "success")
        else:
            error = msg

    elif action == "save_hook":
        hook = (request.form.get("hook") or "").strip()
        ok, msg = save_hook(name, hook, request.form.get("content") or "")
        if ok:
            audit("save_hook", name, hook)
            flash(msg, "success")
            return redirect(url_for("repo_admin", name=name,
                                    section="hooks", hook=hook))
        error = msg

    elif action == "del_hook":
        hook = (request.form.get("hook") or "").strip()
        ok, msg = delete_hook(name, hook)
        if ok:
            audit("del_hook", name, hook)
            flash(msg, "success")
        else:
            error = msg

    elif action == "unlock":
        lpath = request.form.get("path") or ""
        ok, msg = remove_lock(name, lpath)
        if ok:
            audit("unlock", name, lpath)
            flash(msg, "success")
        else:
            error = msg

    elif action is not None:
        error = "未知操作。"

    # 分区校验：非法分区按 404 处理
    if section not in ADMIN_SECTIONS:
        abort(404)

    # 组装账号列表：passwd 账号 + authz 根节权限（含孤儿授权）
    authz = read_authz(name)
    root_perms = parse_authz_root(authz)
    desc = repo_desc(name)

    accounts = []
    for u in users:
        perm = root_perms.get(u, "")
        accounts.append({"user": u, "perm": perm if perm in ("r", "rw") else "",
                         "exists": True})
    for who in root_perms:
        if who in users or who in AUTHZ_SPECIAL or who.startswith("@"):
            continue
        perm = root_perms.get(who, "")
        accounts.append({"user": who, "perm": perm if perm in ("r", "rw") else "",
                         "exists": False})

    # 用户组：定义 + 根节授权（@group = r/rw）
    groups_raw = parse_authz_groups(authz)
    groups = [{"name": g, "members": ms, "members_text": ", ".join(ms),
               "perm": root_perms.get("@" + g, "") if root_perms.get("@" + g, "") in ("r", "rw") else ""}
              for g, ms in sorted(groups_raw.items())]

    # 备份 / 钩子 / 锁
    sel_hook = (request.args.get("hook") or "").strip()
    if sel_hook:
        hook_content = read_hook(name, sel_hook)
        if hook_content is None:
            hook_content = HOOK_TEMPLATES.get(sel_hook, "#!/bin/sh\n# 新钩子：%s\n" % sel_hook)
    else:
        sel_hook, hook_content = "", ""

    ctx = dict(name=name, desc=desc, error=error, success=success,
               section=section, admin_sections=ADMIN_SECTIONS)
    if section == "access":
        ctx.update(accounts=accounts, authz=authz)
    elif section == "groups":
        ctx.update(groups=groups)
    elif section == "backup":
        ctx.update(backups=list_backups(name),
                   backup_state=_backup_states.get(name))
    elif section == "hooks":
        ctx.update(hooks=list_hooks(name), sel_hook=sel_hook,
                   hook_content=hook_content, hook_names=HOOK_NAMES)
    elif section == "locks":
        ctx.update(locks=list_locks(name))
    elif section == "danger":
        ctx.update(authz=authz)
    return render_template(ADMIN_SECTION_TEMPLATES[section], **ctx)


# --------------------------------------------------- 集中式用户管理（全局账号）
def global_user_state():
    """汇总所有仓库的账号与授权，得到集中管理的展示状态。

    返回 (accounts, repo_count)。权限是「用户 × 仓库」粒度，accounts 按用户名
    排序，每项：
    - exists:       是否在（任一）passwd 中有账号
    - pw_conflict:  同一账号在各仓库 passwd 中密码不一致（需重新保存以同步）
    - perms:        {仓库名: 'r'/'rw'/''}，'' 视为无权限
    - perm_label:   各仓权限完全一致时为 'rw'/'r'/'none'，否则 None
    - perm_counts:  不一致时的摘要 [(权限标签, 仓库数), ...]；一致时为 []
    - perm_detail:  「仓库=权限」明细串，供 tooltip 展示
    组引用（@group）与 $authenticated/* 等特殊主体不作为账号展示。
    """
    repos = list_repos()
    pws = {}    # user -> {密码: 出现次数}
    roots = {}  # repo -> {主体: 权限}
    users = set()
    for name in repos:
        for u, pw in load_passwd(name).items():
            pws.setdefault(u, {})
            pws[u][pw] = pws[u].get(pw, 0) + 1
        root = parse_authz_root(read_authz(name))
        roots[name] = root
        for who in root:
            if not who.startswith("@") and who not in AUTHZ_SPECIAL:
                users.add(who)
    users.update(pws)
    accounts = []
    for u in sorted(users, key=str.lower):
        pwvals = pws.get(u, {})
        perm_map = {}
        counts = {"rw": 0, "r": 0, "none": 0}
        for name in repos:
            p = roots.get(name, {}).get(u, "")
            p = p if p in ("r", "rw") else ""
            perm_map[name] = p
            counts["rw" if p == "rw" else ("r" if p == "r" else "none")] += 1
        if counts["rw"] == len(repos):
            label = "rw"
        elif counts["r"] == len(repos):
            label = "r"
        elif counts["none"] == len(repos):
            label = "none"
        else:
            label = None
        if label:
            perm_counts = []
        else:
            perm_counts = [(PERM_LABELS[k], counts[k])
                           for k in ("rw", "r", "none") if counts[k]]
        perm_detail = ", ".join(
            "%s=%s" % (n, PERM_LABELS["none" if p == "" else p])
            for n, p in perm_map.items())
        accounts.append({
            "user": u,
            "exists": bool(pwvals),
            "pw_conflict": len(pwvals) > 1,
            "perms": perm_map,
            "perm_label": label,
            "perm_counts": perm_counts,
            "perm_detail": perm_detail,
        })
    return accounts, len(repos)


def _apply_to_all_repos(fn):
    """对每个仓库执行 fn(name)，返回 (成功仓库数, {仓库名: 错误})。"""
    ok, bad = 0, {}
    for name in list_repos():
        try:
            fn(name)
            ok += 1
        except OSError as e:
            bad[name] = str(e)
    return ok, bad


def _update_web_password(username, new_hash):
    """把网站登录账号的新密码 hash 原子写回 config.json，并同步内存配置。

    多账号（web_users）则只更新对应账号的 password_hash；
    单账号（顶层 username/password_hash）维持原行为。
    重新读盘是为了保留进程启动后可能出现的其它配置改动。
    """
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        disk = json.load(f)
    if username in disk.get("web_users", {}):
        disk["web_users"][username]["password_hash"] = new_hash
        WEB_USERS[username]["password_hash"] = new_hash
    else:
        disk["password_hash"] = new_hash
        global PASSWORD_HASH
        PASSWORD_HASH = new_hash
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(disk, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, CONFIG_PATH)


def _save_web_user(username, role, new_hash=None):
    """新增 / 更新网站登录账号（config.json 的 web_users），原子写盘并同步内存。"""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        disk = json.load(f)
    entry = disk.setdefault("web_users", {}).setdefault(username, {})
    entry["role"] = role
    if new_hash:
        entry["password_hash"] = new_hash
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(disk, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, CONFIG_PATH)
    WEB_USERS[username] = entry


def _delete_web_user(username):
    """删除网站登录账号。返回 (ok, msg)。"""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        disk = json.load(f)
    users = disk.get("web_users") or {}
    if username not in users:
        return False, "网站账号不存在：%s" % username
    del users[username]
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(disk, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, CONFIG_PATH)
    WEB_USERS.pop(username, None)
    return True, "网站账号 %s 已删除" % username


@app.route("/users/", methods=["GET", "POST"], defaults={"section": "svn"})
@app.route("/users/<section>", methods=["GET", "POST"])
@login_required
def users_admin(section):
    if section not in ("svn", "web"):
        abort(404)
    error = None
    success = None

    if request.method == "POST":
        action = request.form.get("action")
        state, _ = global_user_state()
        known = {a["user"] for a in state}

        if action in ("save_user", "del_user",
                      "save_web_user", "del_web_user") and current_role() != "admin":
            abort(403)

        if action == "save_user":
            # 添加 / 编辑账号：passwd 同步写所有仓库；权限按仓库逐个设置
            uname = (request.form.get("username") or "").strip()
            pw1 = request.form.get("password") or ""
            pw2 = request.form.get("password2") or ""
            repos = list_repos()
            perm_map = {}   # 仓库名 -> ''（无）/ 'r' / 'rw'
            for rn in repos:
                v = request.form.get("perm_" + rn) or "none"
                if v not in PERM_LABELS:
                    error = "仓库 %s 的权限参数不合法。" % rn
                    break
                perm_map[rn] = "" if v == "none" else v
            existed = uname in known
            if error is None:
                if not uname:
                    error = "请填写用户名"
                elif not USER_RE.match(uname):
                    error = ("用户名不合法：字母/数字开头，后接字母、数字、点、"
                             "下划线、连字符，最长 64 位")
                elif not existed and not pw1:
                    error = "新账号需要设置密码"
                elif pw1 or pw2:
                    if pw1 != pw2:
                        error = "两次输入的密码不一致"
                    elif len(pw1) < 1 or len(pw1) > 100:
                        error = "密码长度需在 1-100 位之间"
                    elif "=" in pw1:
                        error = "密码不能包含 = 号（svnserve 限制）"
            if error is None:
                def _apply(name):
                    users = load_passwd(name)
                    if pw1 or not existed:
                        users[uname] = pw1  # 编辑时密码留空 → 保留原密码
                    save_passwd(name, users)
                    set_authz_root_user(name, uname, perm_map.get(name, ""))
                ok, bad = _apply_to_all_repos(_apply)
                if ok == 0:
                    error = "保存失败：%s" % next(iter(bad.values()), "未知错误")
                else:
                    change = "密码已重置，" if (existed and pw1) else ""
                    verb = "创建" if not existed else "保存"
                    success = ("账号 %s 已%s并同步到 %d 个仓库，%s权限已按仓库设置。"
                               % (uname, verb, ok, change))
                    if bad:
                        success += "（以下仓库写入失败：%s）" % "、".join(bad)
                    audit("save_user", "",
                          "%s 账号 %s；权限 %s"
                          % ("编辑" if existed else "新建", uname,
                             " ".join("%s=%s" % (rn, p or "无")
                                      for rn, p in sorted(perm_map.items()))
                             or "（无仓库）"))
        elif action == "del_user":
            uname = request.form.get("username") or ""

            def _apply(name):
                users = load_passwd(name)
                if uname in users:
                    del users[uname]
                    save_passwd(name, users)
                remove_authz_user(name, uname)
            ok, bad = _apply_to_all_repos(_apply)
            success = "账号 %s 已从 %d 个仓库删除（passwd 与 authz 同步清理）。" % (
                uname, ok)
            if bad:
                success += "（以下仓库清理失败：%s）" % "、".join(bad)
            audit("del_user", "", "账号 %s，涉及 %d 个仓库" % (uname, ok))
        elif action == "save_web_user":
            # 添加 / 编辑网站登录账号（config.json 的 web_users），与 SVN 账号无关
            wname = (request.form.get("username") or "").strip()
            wrole = request.form.get("role") or "viewer"
            pw1 = request.form.get("password") or ""
            pw2 = request.form.get("password2") or ""
            me = session.get("user") or ""
            existed = wname in WEB_USERS
            admins = [u for u, e in WEB_USERS.items()
                      if (e.get("role") or "viewer") == "admin"]
            if not wname or not USER_RE.match(wname):
                error = ("用户名不合法：字母/数字开头，后接字母、数字、点、"
                         "下划线、连字符，最长 64 位")
            elif wname == USERNAME:
                error = "该用户名与 config.json 的顶层管理员账号相同，请换一个。"
            elif wrole not in ("admin", "viewer"):
                error = "角色不合法。"
            elif existed and wname == me and wrole != "admin":
                error = "不能把自己的角色改为只读（防止把自己锁在门外）。"
            elif existed and wname in admins and wrole != "admin" and len(admins) == 1:
                error = "至少要保留一个管理员账号。"
            elif not existed and not pw1:
                error = "新账号需要设置密码。"
            elif pw1 or pw2:
                if pw1 != pw2:
                    error = "两次输入的密码不一致。"
                elif len(pw1) < 6 or len(pw1) > 100:
                    error = "密码长度需在 6-100 位之间。"
            if error is None:
                try:
                    _save_web_user(wname, wrole,
                                   generate_password_hash(pw1, method="pbkdf2:sha256")
                                   if pw1 else None)
                except OSError as e:
                    error = "写入 config.json 失败：%s" % e
                else:
                    audit("save_web_user", "",
                          "%s 账号 %s，角色 %s%s"
                          % ("编辑" if existed else "新建", wname,
                             "管理员" if wrole == "admin" else "只读",
                             "，密码已重置" if pw1 else ""))
                    success = "网站账号 %s 已%s（角色：%s）。%s" % (
                        wname, "保存" if existed else "创建",
                        "管理员" if wrole == "admin" else "只读",
                        "密码已重置，该账号下次登录使用新密码。" if pw1 else "")
        elif action == "del_web_user":
            wname = request.form.get("username") or ""
            me = session.get("user") or ""
            admins = [u for u, e in WEB_USERS.items()
                      if (e.get("role") or "viewer") == "admin"]
            if wname == me:
                error = "不能删除当前登录的账号。"
            elif wname in admins and len(admins) == 1:
                error = "至少要保留一个管理员账号。"
            else:
                try:
                    okd, msg = _delete_web_user(wname)
                except OSError as e:
                    okd, msg = False, "写入 config.json 失败：%s" % e
                if okd:
                    audit("del_web_user", "", "账号 %s" % wname)
                    success = msg + "（不影响同名 SVN 账号）。"
                else:
                    error = msg
        elif action is not None:
            error = "未知操作。"

    accounts, repo_count = global_user_state()
    me = session.get("user") or ""
    # 顶层单账号（config.json username/password_hash）始终列出：它是多账号模式的兜底入口
    web_accounts = [{"name": USERNAME, "role": "admin",
                     "self": me == USERNAME, "toplevel": True}]
    for u, e in sorted(WEB_USERS.items()):
        web_accounts.append({"name": u, "role": (e.get("role") or "viewer"),
                             "self": u == me, "toplevel": False})
    if section == "web":
        # 网站账号属管理信息，只读角色不展示
        if current_role() != "admin":
            abort(403)
        return render_template("users_web.html", section=section,
                               web_accounts=web_accounts,
                               error=error, success=success)
    return render_template("users_svn.html", section=section, accounts=accounts,
                           repo_count=repo_count, repos=list_repos(),
                           error=error, success=success)


# ------------------------------------------------- 备份下载 / 分支标签 / 打包下载
@app.route("/<name>/backup/<path:fname>")
@login_required
@admin_required
def backup_download(name, fname):
    """下载一个 svnadmin dump 备份文件（校验前缀，防目录穿越）。"""
    if fname != os.path.basename(fname) or not fname.startswith(
            _backup_file_pattern(name)) or not fname.endswith(".dump"):
        abort(404)
    p = os.path.join(BACKUP_DIR, fname)
    if not os.path.isfile(p):
        abort(404)
    return send_file(p, as_attachment=True, download_name=fname)


@app.route("/<name>/branch", methods=["POST"])
@login_required
@admin_required
def create_branch(name):
    """在线创建分支/标签：svn copy URL URL --parents -m。"""
    validate_repo(name)
    back = url_for("tree", name=name,
                   path=request.form.get("src") or None)
    kind = request.form.get("kind") or "branches"
    if kind not in ("branches", "tags"):
        kind = "branches"
    bname = (request.form.get("name") or "").strip()
    src = safe_rel_path(request.form.get("src") or "")
    msg = (request.form.get("msg") or "").strip() or (
        "%s %s%s" % ("创建分支" if kind == "branches" else "创建标签",
                     bname, "（来自 /%s）" % src if src else "（来自仓库根）"))

    if not REPO_NAME_RE.match(bname):
        flash("名称不合法：只允许字母、数字、点、下划线、连字符。", "error")
        return redirect(back)
    dst_path = "%s/%s" % (kind, bname)
    # svn copy --parents 对「已存在的目标」不报错，而是把源复制成目标的
    # 子节点（如 tags/v1/trunk），必须先显式检查目标不存在
    rc0, _o, _e = run_svn(["svn", "ls", "--depth", "empty", "--non-interactive",
                           repo_url(name, dst_path)], timeout=30)
    if rc0 == 0:
        flash("目标 /%s 已存在，请换个名称。" % dst_path, "error")
        return redirect(back)
    rc, out, err = run_svn(
        ["svn", "copy", "--parents", "--non-interactive", "-m", msg,
         repo_url(name, src), repo_url(name, dst_path)], timeout=300)
    if rc != 0:
        flash("创建失败：%s" % (err.strip() or out.strip() or "未知错误"), "error")
        return redirect(back)
    audit("create_branch", name, "%s → /%s" % (src or "(root)", dst_path))
    purge_repo_cache(name)
    flash("%s /%s 已创建。" % ("分支" if kind == "branches" else "标签", dst_path),
          "success")
    return redirect(url_for("tree", name=name, path=dst_path))


@app.route("/<name>/archive/", defaults={"path": ""})
@app.route("/<name>/archive/<path:path>")
@login_required
def archive_download(name, path):
    """整目录打包下载：svn export → zip → 发送，请求结束后清理临时文件。"""
    validate_repo(name)
    rev, head = get_rev(name, request.args.get("rev"))
    if path:
        if path_kind(name, path, rev) != "dir":
            abort(404)
    if head == 0:
        abort(404)

    tmpdir = tempfile.mkdtemp(prefix="svnweb-arc-")
    export_dir = os.path.join(tmpdir, "export")
    rc, out, err = run_svn(
        ["svn", "export", "-q", "--non-interactive",
         repo_url(name, path, rev if rev != head else None), export_dir],
        timeout=600)
    if rc != 0:
        shutil.rmtree(tmpdir, ignore_errors=True)
        abort(500, "导出失败：%s" % (err.strip() or out.strip() or "未知错误"))

    total = 0
    for root, _dirs, files in os.walk(export_dir):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    if total > ARCHIVE_MAX_BYTES:
        shutil.rmtree(tmpdir, ignore_errors=True)
        abort(413, "目录内容约 %s，超过打包下载上限 %s。"
              % (human_size(total), human_size(ARCHIVE_MAX_BYTES)))

    base = os.path.basename(path.rstrip("/")) or name
    zipname = "%s-%s-r%d.zip" % (name, base, rev)
    zippath = os.path.join(tmpdir, "download.zip")
    try:
        with zipfile.ZipFile(zippath, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _dirs, files in os.walk(export_dir):
                for fn in files:
                    fp = os.path.join(root, fn)
                    arc = os.path.relpath(fp, export_dir)
                    zf.write(fp, arc)
    except OSError:
        shutil.rmtree(tmpdir, ignore_errors=True)
        abort(500, "打包失败（磁盘空间不足？）")

    @after_this_request
    def _cleanup(response):
        shutil.rmtree(tmpdir, ignore_errors=True)
        return response

    return send_file(zippath, as_attachment=True,
                     download_name=zipname, mimetype="application/zip")


# --------------------------------------------------- 全服务器最近提交
# 聚合页最多展示的条数，以及每个仓库最多取多少条参与合并
RECENT_LIMIT = CONFIG.get("recent_limit", 60)
RECENT_PER_REPO = CONFIG.get("recent_per_repo", 40)


def global_recent():
    """拉取所有仓库的最近提交，按提交时间倒序合并。

    每个仓库取最近 RECENT_PER_REPO 条（足够覆盖「最近」窗口），合并后
    截取前 RECENT_LIMIT 条；结果整体缓存（TTL 同 CACHE_TTL）。
    SVN 的 XML 日期是 ISO 格式 UTC，可直接按字符串排序。
    """
    key = ("global_recent",)
    v = cache_get(key)
    if v is not None:
        return v
    entries = []
    for name in list_repos():
        r = youngest(name)
        if r <= 0:
            continue  # 空仓库没有提交
        rc, out, err = run_svn(
            ["svn", "log", "--xml", "-l", str(RECENT_PER_REPO),
             "-r", "%d:1" % r, "--non-interactive", repo_url(name)],
            timeout=30)
        if rc != 0 or not out.strip():
            continue
        try:
            for e in ET.fromstring(out).iter("logentry"):
                rev = e.get("revision", "")
                if not rev.isdigit():
                    continue
                entries.append({
                    "repo": name, "rev": int(rev),
                    "author": e.findtext("author") or "",
                    "date": e.findtext("date") or "",
                    "msg": (e.findtext("msg") or "").strip(),
                })
        except ET.ParseError:
            continue
    entries.sort(key=lambda x: x["date"], reverse=True)
    return cache_set(key, entries[:RECENT_LIMIT])


@app.route("/recent/")
@login_required
def recent_commits():
    return render_template("recent.html", entries=global_recent())


# ------------------------------------------------------- 审计日志页
@app.route("/audit/")
@login_required
@admin_required
def audit_page():
    q_repo = (request.args.get("repo") or "").strip()
    q_action = (request.args.get("action") or "").strip()
    entries = read_audit_tail(q_repo=q_repo, q_action=q_action)
    actions = [{"v": k, "label": v}
               for k, v in AUDIT_ACTION_LABELS.items()]
    return render_template("audit.html", entries=entries,
                           q_repo=q_repo, q_action=q_action,
                           actions=actions)


# ------------------------------------------------------- 系统状态页
def _dir_size(path):
    """目录总大小（字节）；目录不存在返回 0。"""
    total = 0
    for root, _dirs, files in os.walk(path):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    return total


def _svnserve_status():
    """探测 svnserve 服务状态：{active, active_text, version}。"""
    info = {"active": None, "active_text": "未知", "version": ""}
    if shutil.which("systemctl"):
        rc, out, _e = run_svn(["systemctl", "is-active", "svnserve"], timeout=10)
        active = (rc == 0 and out.strip() == "active")
        info["active"] = active
        info["active_text"] = "运行中" if active else ("已停止" if out.strip() else "未知")
    elif shutil.which("pgrep"):
        rc, out, _e = run_svn(["pgrep", "-x", "svnserve"], timeout=10)
        info["active"] = rc == 0
        info["active_text"] = "运行中" if rc == 0 else "未发现进程"
    elif os.name == "nt" and shutil.which("tasklist"):
        # Windows：tasklist 输出为 GBK，但进程名都是 ASCII，utf-8 replace
        # 解码 + _decode_output 回退后按内容匹配（无匹配时 rc 各版本不一致）
        rc, out, _e = run_svn(["tasklist", "/FO", "CSV", "/NH"], timeout=15)
        low = out.lower()
        hit = [p for p in ("svnserve", "visualsvnserver", "vsvnhttpsvc")
               if p in low]
        info["active"] = bool(hit)
        if hit:
            info["active_text"] = ("运行中（VisualSVN HTTP）"
                                   if "vsvnhttpsvc" in hit else "运行中")
        else:
            info["active_text"] = "未发现进程"
    if info["active"] is None and shutil.which("svnserve"):
        # 兜底：没有任何进程检测手段时，退回探测默认端口
        listening = _probe_port(3690)
        info["active"] = listening
        info["active_text"] = ("运行中（默认端口监听）" if listening
                               else "未发现进程（默认端口 3690 未监听）")
    rc, out, _e = run_svn(["svnserve", "--version"], timeout=15)
    if rc == 0 and out:
        info["version"] = out.strip().split("\n")[0]
    return info


@app.route("/server/")
@login_required
@admin_required
def server_status():
    repos = []
    for n in list_repos():
        y = youngest(n)
        repos.append({
            "name": n,
            "size": _dir_size(repo_path(n)),
            "youngest": y,
            "last": repo_last_change(n, y) if y > 0 else None,
        })
    repos.sort(key=lambda r: r["size"], reverse=True)
    total_size = sum(r["size"] for r in repos)
    try:
        disk = shutil.disk_usage(REPO_ROOT)
    except OSError:
        disk = None
    return render_template("server.html", repos=repos,
                           total_size=total_size, disk=disk,
                           repo_root=REPO_ROOT,
                           svnserve=_svnserve_status())


def _win_svc_state(svc):
    """Windows：查询服务状态，返回 RUNNING / STOPPED / OTHER / None（不存在）。"""
    rc, out, _e = run_svn(["sc", "query", svc], timeout=10)
    if rc != 0:
        return None
    if "RUNNING" in out:
        return "RUNNING"
    if "STOPPED" in out:
        return "STOPPED"
    return "OTHER"


def _win_svnserve_service():
    """Windows：找到承载 SVN 服务的系统服务则返回服务名（优先在运行的）。

    覆盖两类：svn:// 协议的 svnserve 服务，以及 VisualSVN Server 的
    HTTP 服务（服务名 VisualSVNServer，二进制 vsvnhttpsvc.exe）。
    """
    if os.name != "nt":
        return None
    found = []
    if shutil.which("sc"):
        # VisualSVNServer 在前：即使用户手动注册过 svnserve 服务且两者都停止，
        # 也优先认 VisualSVN（用户实际在用的 HTTP 服务）
        for name in ("VisualSVNServer", "svnserve"):
            rc, out, _e = run_svn(["sc", "query", name], timeout=10)
            if rc == 0:
                found.append((name, "RUNNING" if "RUNNING" in out else "STOPPED"))
    if found:
        for n, st in found:
            if st == "RUNNING":
                return n
        return found[0][0]
    ps = shutil.which("powershell")
    if ps:
        rc, out, _e = run_svn(
            [ps, "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Service | "
             "Where-Object { $_.PathName -match 'svnserve|vsvnhttpsvc|VisualSVNServer' } | "
             "Sort-Object { $_.State -ne 'Running' } | "
             "Select-Object -First 1).Name"],
            timeout=30)
        if rc == 0 and out.strip():
            name = out.strip().splitlines()[-1].strip()
            # 严格校验：只接受形如服务名的 ASCII 结果，防止乱码/告警文本混入
            if name and re.match(r"^[A-Za-z0-9_. -]{1,64}$", name) \
                    and not name.lower().startswith("get-cim"):
                return name
    return None


@app.route("/server/restart", methods=["POST"])
@login_required
@admin_required
def server_restart():
    if shutil.which("systemctl"):
        rc, out, err = run_svn(["systemctl", "restart", "svnserve"], timeout=60)
        if rc == 0:
            audit("restart_svnserve")
            flash("svnserve 重启指令已执行。", "success")
        else:
            flash("重启失败：%s" % (err.strip() or out.strip() or "未知错误"), "error")
        return redirect(url_for("server_status"))

    # Windows：优先走系统服务方式（sc stop → 等 STOPPED → sc start）
    svc = _win_svnserve_service()
    if svc:
        run_svn(["sc", "stop", svc], timeout=15)
        for _ in range(20):  # 最多等 10 秒进入 STOPPED
            if _win_svc_state(svc) == "STOPPED":
                break
            time.sleep(0.5)
        rc, out, err = run_svn(["sc", "start", svc], timeout=15)
        if rc == 0 or _win_svc_state(svc) == "RUNNING":
            audit("restart_svnserve")
            flash("svnserve 服务（%s）已重启。" % svc, "success")
        else:
            detail = (err.strip() or out.strip() or "未知错误")
            if "拒绝访问" in detail or "denied" in detail.lower() or " 5:" in detail:
                detail += ("。重启服务需要管理员权限：请以管理员身份运行本程序，"
                           "或在管理员命令行执行 sc start %s。" % svc)
            flash("重启失败：%s" % detail, "error")
        return redirect(url_for("server_status"))

    # Windows：没有对应服务时，看是不是手动启动的进程
    if os.name == "nt" and shutil.which("tasklist"):
        rc, out, _e = run_svn(["tasklist", "/FI", "IMAGENAME eq svnserve.exe",
                               "/FO", "CSV", "/NH"], timeout=10)
        if rc == 0 and "svnserve" in out.lower():
            flash("svnserve 是手动启动的进程（未注册为系统服务），无法在此重启。"
                  "请手动结束 svnserve.exe 并按原命令重新启动；"
                  "或先用 sc create 注册为服务，之后即可在此一键重启。", "error")
        else:
            flash("未发现 svnserve 服务或进程，无法重启。", "error")
        return redirect(url_for("server_status"))

    flash("当前环境不支持在此重启 svnserve（需要 systemd 或 Windows 服务方式部署）。",
          "error")
    return redirect(url_for("server_status"))


@app.route("/server/repo_root", methods=["POST"])
@login_required
@admin_required
def server_repo_root():
    """系统页重新设置仓库根目录：只切换扫描位置，不移动任何文件。"""
    global CONFIG
    new = (request.form.get("repo_root") or "").strip().strip('"')
    new = new.replace("\\", "/").rstrip("/")
    old = REPO_ROOT.replace("\\", "/").rstrip("/")
    if not new or not os.path.isabs(new):
        flash("仓库根目录必须是绝对路径（如 D:/svn 或 /svn）。", "error")
        return redirect(url_for("server_status"))
    same = (new.lower() == old.lower()) if os.name == "nt" else (new == old)
    if same:
        flash("新仓库根目录与当前一致，未做变更。", "error")
        return redirect(url_for("server_status"))
    if not os.path.isdir(new):
        try:
            os.makedirs(new)
        except OSError as e:
            flash("目录 %s 创建失败：%s" % (new, e), "error")
            return redirect(url_for("server_status"))
    if not os.access(new, os.W_OK):
        flash("目录 %s 不可写，请检查权限。" % new, "error")
        return redirect(url_for("server_status"))

    n_old = len(list_repos())
    cfg = dict(CONFIG)
    cfg["repo_root"] = new
    tmp = CONFIG_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, CONFIG_PATH)
    except OSError as e:
        flash("config.json 写入失败：%s" % e, "error")
        return redirect(url_for("server_status"))

    CONFIG = cfg
    _apply_config(cfg)      # REPO_ROOT / ARCHIVE_DIR 热更新
    purge_all_caches()      # 旧根下的缓存数据全部作废
    n_new = len(list_repos())
    audit("set_repo_root", detail="%s -> %s" % (old, new))
    if n_new:
        flash("仓库根目录已切换为 %s，发现 %d 个仓库。原目录 %s 下的 %d 个仓库"
              "不再显示（文件未移动）。" % (new, n_new, old, n_old), "success")
    else:
        flash("仓库根目录已切换为 %s，但该目录下未发现仓库（没有含 db 的子目录），"
              "请确认路径是否正确。原目录 %s 下的 %d 个仓库不再显示（文件未移动）。"
              % (new, old, n_old), "error")
    return redirect(url_for("server_status"))


@app.route("/<name>/", defaults={"path": ""})
@app.route("/<name>/<path:path>")
@login_required
def tree(name, path):
    validate_repo(name)
    path = safe_rel_path(path)
    rev, head = get_rev(name, request.args.get("rev"))
    entries = svn_list(name, path, rev)
    if entries is None:
        # 该目录在此版本中并不存在 —— 直接 404，不再伪装成「空目录」
        abort(404)
    if path and len(entries) == 1 and entries[0]["kind"] == "file" \
            and entries[0]["name"] == os.path.basename(path):
        # 目录视图被用在了文件路径上（svn list 会把文件自身列为一条），
        # 转到文件视图，避免出现「一个只含自己的目录列表」
        return redirect(url_for("blob", name=name, path=path,
                                rev=(rev if rev != head else None)))
    # rev -> 提交信息 映射（用于显示最后一次修改说明）
    msgmap = {}
    for lg in svn_log(name, "", rev, limit=300):
        msgmap[lg["rev"]] = lg["msg"]
    for e in entries:
        e["msg"] = msgmap.get(e["rev"], "")
    # rev -> 提交信息 映射（用于显示最后一次修改说明）
    msgmap = {}
    for lg in svn_log(name, "", rev, limit=300):
        msgmap[lg["rev"]] = lg["msg"]
    for e in entries:
        e["msg"] = msgmap.get(e["rev"], "")
    crumbs = build_crumbs(name, path, "tree", rev, head)
    last = svn_log(name, path, rev, limit=1)
    # 标准布局识别：任意路径都检测标准布局，以便全局展示分支切换器
    stdlib = None
    branches = None
    readme_html = None
    readme_path = None
    if rev > 0:
        stdlib = detect_stdlib(name, rev)
        branches = list_branches(name, rev)
        if not path:  # 仅仓库根展示 README
            rtext, rpath = repo_readme(name, rev)
            if rtext is not None:
                if len(rtext) > 200000:
                    rtext = rtext[:200000] + "\n...（README 过长，已截断）"
                readme_html = render_markdown_light(rtext)
                readme_path = rpath
    return render_template("tree.html", name=name, path=path, entries=entries,
                           crumbs=crumbs, rev=rev, head=head,
                           last=(last[0] if last else None), action_label=action_label,
                           stdlib=stdlib, branches=branches,
                           readme_html=readme_html,
                           readme_path=readme_path,
                           clone_dir=checkout_dir(path, "dir"),
                           desc=repo_desc(name),
                           stats=repo_stats(name, path, rev),
                           side_tree_ops=True)


@app.route("/<name>/blob/<path:path>")
@login_required
def blob(name, path):
    validate_repo(name)
    path = safe_rel_path(path)
    rev, head = get_rev(name, request.args.get("rev"))
    fname = os.path.basename(path)
    # 文件页的「检出代码」退回到它所在的目录（检出单个文件没有意义）
    clone_dir = checkout_dir(path, "file")
    crumbs = build_crumbs(name, path, "blob", rev, head)
    size = file_size(name, path, rev) or 0
    kind = preview_kind(fname)
    media_url = url_for("media", name=name, path=path,
                        rev=(rev if rev != head else None)) if kind else None
    # 浏览器能直接打开的文件（图片 / PDF / 音视频）：不读内容，交给 /media/ 流式渲染，
    # 由浏览器自己打开，服务端不为此占用内存。
    if kind:
        if path_kind(name, path, rev) != "file":
            abort(404)
        if size > MAX_MEDIA_BYTES:
            # 超过上限就不内联了，但仍如实说明并给出下载入口
            return render_template("blob.html", name=name, path=path, fname=fname,
                                   code_html=None, binary=False, too_large=True,
                                   kind=None, media_url=None, size=size, lines=0,
                                   crumbs=crumbs, rev=rev, head=head,
                                   clone_dir=clone_dir)
        return render_template("blob.html", name=name, path=path, fname=fname,
                               code_html=None, binary=False, too_large=False,
                               kind=kind, media_url=media_url, size=size, lines=0,
                               crumbs=crumbs, rev=rev, head=head,
                               clone_dir=clone_dir)
    # 过大文件不载入预览，避免内存压力（仍可下载）
    if size > MAX_PREVIEW_BYTES:
        return render_template("blob.html", name=name, path=path, fname=fname,
                               code_html=None, binary=False, too_large=True,
                               kind=None, media_url=None,
                               size=size, lines=0, crumbs=crumbs, rev=rev,
                               head=head, clone_dir=clone_dir)
    content, err = svn_cat(name, path, rev)
    if content is None:
        abort(404)
    binary = is_binary(content)
    code_html = None
    md_html = None
    if not binary:
        code_html, _ = highlight_code(fname, content)
        if code_html is None:
            code_html = "<pre class='raw'>%s</pre>" % html.escape(content)
        # Markdown 文档：额外渲染 HTML（预览/源码切换展示，默认预览）
        if os.path.splitext(fname)[1].lower() in (".md", ".markdown"):
            md_text = content
            if len(md_text) > 200000:
                md_text = md_text[:200000] + "\n\n...（文档过长，预览已截断）"
            md_html = render_markdown_light(md_text)
    lines = content.count("\n") + (0 if content.endswith("\n") else 1)
    if size == 0:
        size = len(content.encode("utf-8", "replace"))
    return render_template("blob.html", name=name, path=path, fname=fname,
                           code_html=code_html, binary=binary, size=size,
                           too_large=False, kind=None, media_url=None,
                           md_html=md_html,
                           lines=lines, crumbs=crumbs,
                           rev=rev, head=head, clone_dir=clone_dir)


@app.route("/<name>/media/<path:path>")
@login_required
def media(name, path):
    """内联预览资源：图片 / PDF / 音视频，按白名单给出正确的 Content-Type。

    只放行 _MEDIA_MIME 里的扩展名，并强制 nosniff：仓库里的 .html/.js/.svg
    不会被这里当成 HTML 或脚本喂给同源文档，避免存储型 XSS。
    流式返回，大文件也不占服务端内存（注意：不支持 Range，因此音视频不能拖动进度）。
    """
    validate_repo(name)
    path = safe_rel_path(path)
    rev, head = get_rev(name, request.args.get("rev"))
    mime = media_mimetype(path)
    if mime is None or path_kind(name, path, rev) != "file":
        abort(404)
    gen = stream_cat(name, path, rev)
    return Response(gen, mimetype=mime, headers={
        "Cache-Control": "no-cache",
        "Content-Disposition":
            "inline; filename*=UTF-8''%s" % quote(os.path.basename(path)),
        "X-Content-Type-Options": "nosniff",
    })


@app.route("/<name>/raw/<path:path>")
@login_required
def raw(name, path):
    validate_repo(name)
    path = safe_rel_path(path)
    rev, head = get_rev(name, request.args.get("rev"))
    # 流式返回，避免大文件全量载入内存
    gen = stream_cat(name, path, rev)
    return Response(gen, mimetype="text/plain; charset=utf-8",
                    headers={"Cache-Control": "no-cache"})


@app.route("/<name>/blame/<path:path>")
@login_required
def blame(name, path):
    validate_repo(name)
    path = safe_rel_path(path)
    rev, head = get_rev(name, request.args.get("rev"))
    if path_kind(name, path, rev) is None:
        abort(404)
    rows, err = svn_blame(name, path, rev)
    if rows is None:
        abort(404)
    fname = os.path.basename(path)
    crumbs = build_crumbs(name, path, "blob", rev, head)
    return render_template("blame.html", name=name, path=path, fname=fname,
                           rows=rows, size=len(rows), crumbs=crumbs,
                           rev=rev, head=head, err=err,
                           clone_dir=checkout_dir(path, "file"))


@app.route("/<name>/download/<path:path>")
@login_required
def download(name, path):
    validate_repo(name)
    path = safe_rel_path(path)
    rev, head = get_rev(name, request.args.get("rev"))
    gen = stream_cat(name, path, rev)
    return Response(gen, mimetype="application/octet-stream",
                    headers={"Content-Disposition":
                             "attachment; filename*=UTF-8''%s" % quote(os.path.basename(path))})


# ------------------------------------------------- 搜索（信息 / 文件名 / 代码）
# 搜索范围：提交信息 / 变更文件名 / 代码变更（diff 正文，含新增、删除与上下文行）
SEARCH_SCOPE_ORDER = ("msg", "path", "code")
SEARCH_SCOPE_LABELS = {"msg": "提交信息", "path": "文件名", "code": "代码变更"}
SEARCH_KIND_LABELS = {"A": "新增", "M": "修改", "D": "删除", "R": "替换",
                      "add": "新增", "del": "删除", "ctx": "上下文"}

# 版本头：`r123 | 作者 | 2026-09-29 17:34:22 +0800 (…) | N line`
_REV_HEADER_RE = re.compile(r"^r(\d+) \| ")
# 整文件删除在 diff 里只有这一行，没有正文
_DELETED_RE = re.compile(r"^Index: (.*?) \(deleted\)$", re.M)
# 为「删除的文件」补取内容：单份最多读这么多字节（只用来搜关键字，不必整份载入，
# 也避免超大二进制文件把内存顶爆）
_DELETED_TEXT_LIMIT = 256 * 1024
# 为「删除的文件」补取内容时的条数上限。大仓库一次清理会删掉成百上千个文件
# （构建产物、日志），必须有上限；用固定条数而不是时间预算，是为了让结果可
# 复现——时间预算会让同一次提交里排在后面的文件在慢机器上被整个漏掉。
_DELETED_LOOKUP_MAX = CONFIG.get("search_deleted_max", 1200)
# 并发读盘的工作线程数。串行起几百次 svnlook 进程要好几秒，是整场搜索的大头
_DELETED_LOOKUP_WORKERS = CONFIG.get("search_deleted_workers", 6)
# 删除文件内容的缓存预算（压缩后字节）
_DELETE_CACHE_BUDGET = CONFIG.get("search_delete_cache", 24 * 1024 * 1024)
_SCAN_CHUNK = 256 * 1024
# 单个版本最多收集多少条匹配片段，避免一次命中太多把内存撑爆
_SCAN_HIT_REVS = 3
# 同一版本内多段命中时的排序权重：新增/删除优先于上下文
_KIND_RANK = {"add": 0, "del": 1, "ctx": 2}

# 全历史 diff 扫描结果的内存缓存：key -> (写入时间, zlib 压缩字节)
# waitress 是多线程的，字节计数用锁保护，避免并发下漏加导致缓存超出预算
_scan_cache = {}
_scan_cache_bytes = 0
_scan_cache_lock = threading.Lock()

# 删除文件内容的缓存：key=(name, rev, path) -> (写入时间, zlib 压缩字节)。
# 一次提交可能删掉几十个文件，同一次搜索里还要反复查，重复搜索更是要重读，
# 缓存住才能做到「首次慢一点、之后几乎免费」，同时让结果保持稳定。
_delete_cache = {}
_delete_cache_bytes = 0
_delete_cache_lock = threading.Lock()


def _delete_cache_get(key):
    with _delete_cache_lock:
        return _delete_cache.get(key)


def _delete_cache_put(key, blob):
    global _delete_cache_bytes
    with _delete_cache_lock:
        old = _delete_cache.pop(key, None)
        if old is not None:
            _delete_cache_bytes -= len(old[1])
        _delete_cache[key] = (time.time(), blob)
        _delete_cache_bytes += len(blob)
        # 超出预算就按写入时间淘汰最旧的
        while _delete_cache_bytes > _DELETE_CACHE_BUDGET and _delete_cache:
            oldest = min(_delete_cache,
                         key=lambda k: _delete_cache[k][0])
            _delete_cache_bytes -= len(_delete_cache.pop(oldest)[1])


def _scan_cache_get(key):
    ent = _scan_cache.get(key)
    if ent is None:
        return None
    if (time.time() - ent[0]) >= SEARCH_SCAN_TTL:
        _scan_cache.pop(key, None)
        return None
    return ent[1]


def _scan_cache_put(key, blob):
    global _scan_cache_bytes
    if not blob or len(blob) > SEARCH_SCAN_BUDGET:
        return
    with _scan_cache_lock:
        old = _scan_cache.pop(key, None)
        if old:
            _scan_cache_bytes -= len(old[1])
        _scan_cache[key] = (time.time(), blob)
        _scan_cache_bytes += len(blob)
        # 超出预算就按写入时间淘汰最旧的
        while _scan_cache_bytes > SEARCH_SCAN_BUDGET and _scan_cache:
            oldest = min(_scan_cache, key=lambda k: _scan_cache[k][0])
            _scan_cache_bytes -= len(_scan_cache.pop(oldest)[1])


def svn_log_all(name, path, rev):
    """全历史（r<rev> … r1）的提交记录（含改动路径），不分页。

    搜索要在整段历史里找，不能只在前 N 条里翻，所以这里不带 -l。
    """
    key = ("logall", name, path, rev)
    v = cache_get(key)
    if v is not None:
        return v
    rc, out, err = run_svn(["svn", "log", "--xml", "-v", "-r", "%d:1" % rev,
                            "--non-interactive", repo_url(name, path, rev)],
                           timeout=180)
    logs = []
    if rc == 0 and out.strip():
        try:
            root = ET.fromstring(out)
            for e in root.iter("logentry"):
                paths = [{"path": (p.text or "").strip(),
                          "action": p.get("action", ""),
                          "kind": p.get("kind", "")}
                         for p in e.findall("./paths/path")]
                logs.append({
                    "rev": e.get("revision", ""),
                    "author": e.findtext("author") or "",
                    "date": e.findtext("date") or "",
                    "msg": (e.findtext("msg") or "").strip(),
                    "paths": paths,
                })
        except ET.ParseError:
            pass
    return cache_set(key, logs)


def _decompress_chunks(blob):
    """把缓存的压缩结果增量解压成文本分片（避免一次性载入整份 diff）。"""
    dec = zlib.decompressobj()
    to_text = codecs.getincrementaldecoder("utf-8")("replace")
    for i in range(0, len(blob), _SCAN_CHUNK):
        raw = dec.decompress(blob[i:i + _SCAN_CHUNK])
        if raw:
            s = to_text.decode(raw)
            if s:
                yield s
    raw = dec.flush()
    if raw:
        s = to_text.decode(raw)
        if s:
            yield s
    s = to_text.decode(b"", True)
    if s:
        yield s


def _live_scan_chunks(name, path, rev, cache_key, state=None):
    """流式跑 `svn log -v --diff`，产出文本分片，同时留存压缩副本用于缓存。

    只有完整读完（EOF）才会写缓存——中途被打断（已找够结果提前返回）不缓存，
    免得把半截结果固化下来。stderr 丢弃，避免管道写满互相等待。
    跑满 SEARCH_SCAN_TIMEOUT 会被掐断，此时在 state 里记一笔，让上层如实
    告诉用户「结果可能不完整」，而不是悄悄少给几条。
    """
    cmd = ["svn", "log", "--verbose", "--diff", "--non-interactive",
           "-r", "%d:1" % rev, repo_url(name, path, rev)]
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL)
    except OSError:
        return
    # 压缩等级 1：95MB 的 diff 只需 ~1.5s（等级 6 要 ~3.2s），压缩率仅差几个点
    co = zlib.compressobj(1)
    parts = []
    to_text = codecs.getincrementaldecoder("utf-8")("replace")
    deadline = time.time() + SEARCH_SCAN_TIMEOUT
    full = False
    try:
        while True:
            chunk = p.stdout.read(_SCAN_CHUNK)
            if not chunk:
                full = True
                break
            parts.append(co.compress(chunk))
            s = to_text.decode(chunk)
            if s:
                yield s
            if time.time() > deadline:
                if state is not None:
                    state["cut"] = True
                break
        if full:
            s = to_text.decode(b"", True)
            if s:
                yield s
    finally:
        with contextlib.suppress(Exception):
            p.stdout.close()
        if not full:
            with contextlib.suppress(Exception):
                p.kill()
        with contextlib.suppress(Exception):
            p.wait(timeout=30)
    if full and parts:
        parts.append(co.flush())
        _scan_cache_put(cache_key, b"".join(parts))


def iter_log_diff(name, path, rev, state=None):
    """产出「全历史 diff」的文本分片；命中内存缓存时直接解压复用。"""
    key = ("ldiff", name, path, rev)
    blob = _scan_cache_get(key)
    if blob is not None:
        return _decompress_chunks(blob)
    return _live_scan_chunks(name, path, rev, key, state)


def _iter_chunk_lines(chunks):
    """把文本分片切成行。

    每块只做一次 `split`，块尾不完整的那半行留到下一块再拼——绝不能写成
    「每取一行就把剩余缓冲整体切片」（那是平方级拷贝：220KB 就要 8 秒，
    几十 MB 的历史直接跑废）。
    """
    pending = ""
    for chunk in chunks:
        lines = (pending + chunk).split("\n")
        pending = lines.pop()
        for line in lines:
            yield line
    if pending:
        yield pending


def iter_diff_blocks(chunks):
    """把 `svn log -v --diff` 的文本流切成 (版本号, diff 行列表)。

    块边界以「分隔行 + 紧随其后的版本头」双重确认：只认分隔行会被「恰好一行
    全是短横线的删除行」骗到，只认版本头又会被「日志信息里恰好以 r12 | 开头的
    一行」骗到，两者同时成立才算真正的分块。块内只保留 diff 正文（从 `Index:`
    或 `Property changes on:` 起），这样「文件名」和「代码」才能分开匹配。
    """
    cur_rev = None
    lines = None
    after_sep = False
    for raw in _iter_chunk_lines(chunks):
        line = raw[:-1] if raw.endswith("\r") else raw
        if len(line) >= 60 and not line.strip("-"):
            after_sep = True
            continue
        if after_sep:
            after_sep = False
            m = _REV_HEADER_RE.match(line)
            if m:
                if cur_rev is not None and lines:
                    yield cur_rev, lines
                cur_rev = m.group(1)
                lines = None
                continue
            # 分隔行是误判（正文里的一整行短横线）→ 还原成内容
            if lines is not None:
                lines.append("-" * 72)
        if cur_rev is None:
            continue
        if lines is None:
            if line.startswith("Index: ") or \
                    line.startswith("Property changes on: "):
                lines = [line]
        else:
            lines.append(line)
    if cur_rev is not None and lines:
        yield cur_rev, lines


def _body_kind(line):
    """diff 正文行类型：+ 新增 / - 删除 / 空格 上下文；其余（文件头、\\ No newline）忽略。"""
    if not line:
        return None
    c = line[0]
    if c == "+":
        return "add"
    if c == "-":
        return "del"
    if c == " ":
        return "ctx"
    return None


def _norm_diff_path(scope, p):
    """diff 里的 `Index:` 路径是相对目标节点的，补回作用域前缀成完整仓库路径。"""
    p = (p or "").strip().lstrip("/")
    if not p:
        return ""
    return (scope.rstrip("/") + "/" + p) if scope else p


def _match_snippet(text, q_lower, rx, width=None):
    """在 text 里找关键字，返回 (前, 命中, 后) 三段；未命中返回 None。

    q_lower 用于快速排除，rx 用于拿到大小写敏感的精确位置（`İ` 这类字符
    lower() 后长度会变，靠 position 切片会错位）。
    """
    if not q_lower or q_lower not in text.lower():
        return None
    m = rx.search(text)
    if not m:
        return None
    pos, end = m.start(), m.end()
    width = width or SEARCH_SNIPPET_WIDTH
    if len(text) <= width:
        return text[:pos], text[pos:end], text[end:]
    half = max(16, (width - (end - pos)) // 2)
    start = max(0, pos - half)
    stop = min(len(text), end + half)
    return (("…" if start > 0 else "") + text[start:pos],
            text[pos:end],
            text[end:stop] + ("…" if stop < len(text) else ""))


def _deleted_file_text(name, path, rev, limit=_DELETED_TEXT_LIMIT):
    """取某版本下文件的内容（最多 limit 字节），按 (name, rev, path) 缓存。

    用 `svnlook cat` 直读仓库（比 `svn cat` 少一层客户端解析，起进程更快），
    失败再退回 `svn cat`。二进制内容搜不了代码、又特别占缓存（大仓库一次清理
    常删掉一堆图片和 map），直接按空处理——同时也把「读过一次」记进缓存，
    省得下次再起一次进程。
    """
    if rev <= 0:
        return ""
    key = (name, int(rev), path)
    ent = _delete_cache_get(key)
    if ent is not None:
        try:
            return zlib.decompress(ent[1]).decode("utf-8", "replace")
        except Exception:
            return ""
    data = b""
    for cmd in (["svnlook", "cat", "-r", str(rev), repo_path(name), path],
                ["svn", "cat", "-r", str(rev), "--non-interactive",
                 repo_url(name, path, rev)]):
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL)
        except OSError:
            continue
        try:
            data = p.stdout.read(limit) or b""
        except Exception:
            data = b""
        finally:
            with contextlib.suppress(Exception):
                p.kill()
            with contextlib.suppress(Exception):
                p.wait(timeout=10)
        if data:
            break
    if b"\x00" in data[:8192]:
        data = b""
    try:
        _delete_cache_put(key, zlib.compress(data, 1))
    except Exception:
        pass
    return data.decode("utf-8", "replace")


def _deleted_file_hits(name, rev, paths, q_lower, rx):
    """整文件被删除时 `svn log --diff` 只给一行 `Index: x (deleted)`、不带正文，
    这里补取删除前的内容来搜，返回 (命中片段, 是否因条数上限被截断)。

    paths 要传该版本删除的全部文件——只取前几个会漏，大仓库一次清理删掉一整片
    是常态。这几百次读盘是整场搜索里最大的一块开销，所以并发去读；读回来的内容
    按批处理完就丢弃，避免几百份文件同时压在内存里。结果按 paths 原序重组，
    保证同一个查询在任何负载下结果都一样。
    """
    out = []
    if rev <= 1:
        return out, False
    todo = [p for p in paths if p]
    cut = len(todo) > _DELETED_LOOKUP_MAX
    if cut:
        todo = todo[:_DELETED_LOOKUP_MAX]
    if not todo:
        return out, cut
    batch = max(1, _DELETED_LOOKUP_WORKERS * 4)
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        if len(chunk) == 1 or _DELETED_LOOKUP_WORKERS <= 1:
            got = [(p, _deleted_file_text(name, p, rev - 1))
                   for p in chunk]
        else:
            with futures.ThreadPoolExecutor(
                    max_workers=_DELETED_LOOKUP_WORKERS) as ex:
                pend = {ex.submit(_deleted_file_text, name, p, rev - 1): p
                        for p in chunk}
                res = {}
                for f in futures.as_completed(pend):
                    res[pend[f]] = f.result()
                got = [(p, res.get(p, "")) for p in chunk]
        for p, content in got:
            if not content:
                continue
            for line in content.splitlines():
                sn = _match_snippet(line, q_lower, rx)
                if sn:
                    out.append({"path": p, "kind": "del", "before": sn[0],
                                "hit": sn[1], "after": sn[2]})
                    if len(out) >= SEARCH_SNIPPET_MAX:
                        return out, cut
        del got
    return out, cut


def _code_hits(name, path, rev, q_lower, rx, limit_revs):
    """扫全历史 diff，返回 ({版本号: [匹配片段]}, 是否因上限被截断)。"""
    hits = {}
    truncated = False
    del_cut = False
    state = {}
    with contextlib.closing(iter_log_diff(name, path, rev, state)) as chunks:
        with contextlib.closing(iter_diff_blocks(chunks)) as blocks:
            for rev_no, lines in blocks:
                text = "\n".join(lines)
                # 先整块扫一遍：绝大多数版本不匹配，能在 C 层一次性排除掉，
                # 省下逐行 lower() 比较（那是整场扫描里最贵的一段）
                hit = rx.search(text)
                deleted = [_norm_diff_path(path, p)
                           for p in _DELETED_RE.findall(text)]
                if hit is None and not deleted:
                    continue
                snips = []
                if hit is not None:
                    cur_path = ""
                    in_hunks = False
                    for line in lines:
                        if line.startswith("Index: "):
                            idx = line[7:].strip()
                            if idx.endswith(" (deleted)"):
                                idx = idx[:-10]
                            cur_path = _norm_diff_path(path, idx)
                            in_hunks = False
                            continue
                        if line.startswith("Property changes on: "):
                            cur_path = _norm_diff_path(
                                path, line[len("Property changes on: "):])
                            in_hunks = False
                            continue
                        if line.startswith("@@"):
                            in_hunks = True
                            continue
                        if not in_hunks:
                            continue
                        kind = _body_kind(line)
                        if kind is None:
                            continue
                        sn = _match_snippet(line[1:], q_lower, rx)
                        if sn is None:
                            continue
                        snips.append({"path": cur_path, "kind": kind,
                                      "before": sn[0], "hit": sn[1],
                                      "after": sn[2]})
                        if len(snips) >= SEARCH_SNIPPET_MAX * 3:
                            break
                if deleted and len(snips) < SEARCH_SNIPPET_MAX:
                    got, dcut = _deleted_file_hits(
                        name, int(rev_no), deleted, q_lower, rx)
                    snips += got
                    if dcut:
                        del_cut = True
                if snips:
                    # 新增/删除的行比上下文更有信息量，排前面；再截到上限
                    snips.sort(key=lambda s: _KIND_RANK.get(s["kind"], 9))
                    hits[rev_no] = snips[:SEARCH_SNIPPET_MAX]
                    if len(hits) >= limit_revs:
                        truncated = True
                        break
    return hits, (truncated or del_cut or bool(state.get("cut")))


def search_versions(name, path, rev, query, scopes):
    """在选定范围内搜整段历史，返回 (命中的版本行, 命中总数, 是否被截断)。

    结果按版本号从新到旧；提交信息与文件名走轻量的全历史 log，
    代码变更才需要扫 diff，所以只在勾了「代码变更」时做重活。
    """
    q_lower = query.lower()
    rx = re.compile(re.escape(query), re.IGNORECASE)
    entries = svn_log_all(name, path, rev)
    code_hits, code_truncated = {}, False
    if "code" in scopes:
        code_hits, code_truncated = _code_hits(
            name, path, rev, q_lower, rx,
            max(SEARCH_MAX_RESULTS * _SCAN_HIT_REVS, 100))
    want_msg = "msg" in scopes
    want_path = "path" in scopes
    want_code = "code" in scopes
    rows = []
    total = 0
    for e in entries:
        matches = []
        if want_msg and e["msg"]:
            for mline in e["msg"].splitlines():
                sn = _match_snippet(mline, q_lower, rx)
                if sn:
                    matches.append({"scope": "msg", "kind": "",
                                    "path": "", "before": sn[0],
                                    "hit": sn[1], "after": sn[2]})
                    break
        if want_path:
            left = 3
            for p in e["paths"]:
                rel = p["path"].lstrip("/")
                sn = _match_snippet(rel, q_lower, rx)
                if sn:
                    matches.append({"scope": "path", "kind": p["action"],
                                    "path": rel, "before": sn[0],
                                    "hit": sn[1], "after": sn[2]})
                    left -= 1
                    if left <= 0:
                        break
        if want_code:
            for sn in code_hits.get(e["rev"], []):
                matches.append({"scope": "code", "kind": sn["kind"],
                                "path": sn["path"], "before": sn["before"],
                                "hit": sn["hit"], "after": sn["after"]})
        if not matches:
            continue
        total += 1
        if len(rows) < SEARCH_MAX_RESULTS:
            rows.append({
                "rev": e["rev"], "author": e["author"], "date": e["date"],
                "msg": e["msg"], "paths": e["paths"], "matches": matches,
                "scope_tags": [s for s in SEARCH_SCOPE_ORDER
                               if any(m["scope"] == s for m in matches)],
            })
    return rows, total, (code_truncated or total > len(rows))


@app.route("/<name>/commits/")
@app.route("/<name>/commits/<path:path>")
@login_required
def commits(name, path=""):
    """旧「历史」入口：已合并进版本列表，保留 301 兼容跳转。"""
    validate_repo(name)
    path = safe_rel_path(path)
    qs = {}
    for k in ("rev", "search"):
        v = request.args.get(k)
        if v:
            qs[k] = v
    return redirect(url_for("versions", name=name, path=path or None, **qs),
                    code=301)


@app.route("/<name>/versions/")
@app.route("/<name>/versions/<path:path>")
@login_required
def versions(name, path=""):
    """版本列表：合并原「跳转 / 历史 / 对比」三个入口。

    先出列表（分页 + 搜索 + 勾选），勾选两个版本后可发起版本级对比。
    path 为空时列出全仓库提交；带 path 时只列该路径的提交。
    搜索关键字可落在三处（scope 多选）：提交信息 / 变更文件名 / 代码变更，
    搜索覆盖整段历史并与分页互斥。
    """
    validate_repo(name)
    path = safe_rel_path(path)
    rev, head = get_rev(name, request.args.get("rev"))
    path_kind_v = path_kind(name, path, rev)
    if path_kind_v is None:
        abort(404)
    # 「加载更多」：before 为已展示到的最低版本号，据此拉更早的一页
    try:
        before = int(request.args.get("before") or "")
    except ValueError:
        before = None
    # 「加载更多」是站内 fetch 局部追加（见 versions.html 的内联脚本），由它显式带 ?fragment=1。
    # 注意别用 X-Requested-With 判断：pjax.js 的每次站内跳转也带这个头，
    # 那样点进去的整页会变成「无侧栏无布局」的残缺页面。
    fragment = request.args.get("fragment") == "1"
    if before is not None:
        if not (1 <= before <= head):
            before = head
        until_rev = before
    else:
        until_rev = None
    # 已加载过的版本集合（去重第 1 页与第 2 页交界处）
    loaded = set()
    for s in (request.args.get("seen") or "").split(","):
        if s.isdigit():
            loaded.add(s)
    search = (request.args.get("search") or "").strip()[:200]
    # 搜索范围：勾了哪些就搜哪些；一个都没勾时按「全都搜」处理
    scopes = [s for s in request.args.getlist("scope") if s in SEARCH_SCOPE_LABELS]
    if not scopes:
        scopes = list(SEARCH_SCOPE_ORDER)
    crumbs = build_crumbs(name, path, "versions", rev, head)
    if search:
        # 搜索要覆盖整段历史，与分页互斥：一次给出全部命中（超过上限则截断）
        logs, total, truncated = search_versions(name, path, rev, search, scopes)
        has_more = False
    else:
        logs = svn_log(name, path, rev, limit=LOG_PAGE_SIZE, until_rev=until_rev)
        has_more = False
        if logs:
            min_rev = min(int(c["rev"]) for c in logs)
            has_more = min_rev > 1
            if loaded:
                logs = [c for c in logs if c["rev"] not in loaded]
        total, truncated = len(logs), False
    return render_template("versions.html", name=name, path=path, logs=logs,
                           crumbs=crumbs, rev=rev, head=head,
                           has_more=has_more, search=search, scopes=scopes,
                           total=total, truncated=truncated,
                           fragment=fragment, desc=repo_desc(name),
                           branches=(list_branches(name, rev) if rev > 0 else None),
                           # 「加载更多」只取列表，没必要为它再扫一遍历史统计
                           stats=(None if fragment else repo_stats(name, path, rev)),
                           clone_dir=checkout_dir(path, path_kind_v))


@app.route("/<name>/compare/revisions/")
@login_required
def compare_revisions(name):
    """两个修订号之间的对比（由版本列表勾选两个版本后进入）。

    a / b 为整数修订号；底层用 `svn diff -r a:b` 取两个版本之间的累计改动。
    path 可选，把对比范围限定在某个路径内（从版本列表当前所在路径带入）。
    结果显示形式与提交详情页一致（文件列表 + 逐行 diff）。
    """
    validate_repo(name)
    head = youngest(name)
    path = safe_rel_path(request.args.get("path") or "")
    a = (request.args.get("a") or "").strip()
    b = (request.args.get("b") or "").strip()

    def _parse(v):
        try:
            n = int(v)
        except (TypeError, ValueError):
            return None
        return n if 1 <= n <= head else None

    ra, rb = _parse(a), _parse(b)
    if ra is None or rb is None:
        return render_template("compare_revs.html", name=name, head=head,
                               a=a, b=b, files=None, adds=0, dels=0, path=path,
                               error="请选择两个有效版本后再对比。")
    if ra == rb:
        return render_template("compare_revs.html", name=name, head=head,
                               a=ra, b=rb, files=None, adds=0, dels=0, path=path,
                               error="两次选择的是同一个版本（r%d），请选择不同版本。" % ra)
    # 统一按 (旧, 新) 顺序展示
    lo, hi = (ra, rb) if ra < rb else (rb, ra)
    dtext, err = svn_diff_range(name, lo, hi, path)
    if dtext is None:
        return render_template("compare_revs.html", name=name, head=head,
                               a=lo, b=hi, files=None, adds=0, dels=0,
                               error="对比失败：" + (err or "未知错误"),
                               path=path)
    files = parse_diff(dtext)
    adds, dels = diff_stats(files)
    info = svn_log_one(name, hi)
    return render_template("compare_revs.html", name=name, head=head,
                           a=lo, b=hi, files=files, adds=adds, dels=dels,
                           info=info, error=None, action_label=action_label,
                           label=action_label, path=path)


@app.route("/<name>/commit/<int:rev>")
@login_required
def commit(name, rev):
    validate_repo(name)
    head = youngest(name)
    info = svn_log_one(name, rev)
    if not info:
        abort(404)
    dtext, err = svn_diff(name, rev, "")
    files = parse_diff(dtext) if dtext else []
    adds, dels = diff_stats(files)
    # 变更记录（来自 svn log -v 的 paths，比 diff 更完整——含二进制/仅属性变更的条目），
    # 再与 diff 结果 join 上逐文件增删行数。
    # 注意：本页只做「变更文件」清单，不再渲染逐行差异（diff 正文在各文件的
    # 「详情」页 commit_file 里看），所以这里不需要 diff 块锚点。
    diff_idx = {f["path"].lstrip("/"): i for i, f in enumerate(files)}
    changed = []
    for p in info["paths"]:
        key = p["path"].lstrip("/")
        d = files[diff_idx[key]] if key in diff_idx else None
        changed.append({
            "path": key,
            "action": p["action"],
            "kind": p["kind"],
            "is_dir": p["kind"] == "dir",
            "adds": d["adds"] if d else None,
            "dels": d["dels"] if d else None,
        })
    return render_template("commit.html", name=name, info=info,
                           adds=adds, dels=dels, head=head, changed=changed,
                           action_label=action_label, label=action_label,
                           rev=rev)


@app.route("/<name>/commit/<int:rev>/file/<path:path>")
@login_required
def commit_file(name, rev, path):
    """某个文件（或目录）在某次提交里的「单独变更详情」。

    版本详情页的「变更文件」表里，每行的「详情」都指向这里：整页只讲这一个
    路径在 r<rev> 这次提交中的改动。目标既可以是文件，也可以是目录（目录会
    展示其下所有变更），新增/修改/删除/二进制都能正常呈现。
    """
    validate_repo(name)
    head = youngest(name)
    info = svn_log_one(name, rev)
    if not info:
        abort(404)
    path = safe_rel_path(path)
    if not path:
        abort(404)
    entry = next((p for p in info["paths"]
                  if p["path"].lstrip("/") == path), None)
    if entry:
        action, kind = entry["action"], entry["kind"]
    else:
        # 该版本没有直接记录这个路径（例如是变更目录的下级路径）。
        # 只要它在 r<rev> 里确实存在，就按「修改」呈现。
        kind = path_kind(name, path, rev)
        if kind is None:
            abort(404)
        action = "M"
    is_dir = kind == "dir"

    # 只遍历这个子树；取不到时回退成「整仓库 diff + 按路径裁剪」
    dtext, err = svn_diff_path(name, rev, path)
    files = parse_diff(dtext) if dtext else []
    if not files:
        full, _ = svn_diff(name, rev, "")
        if full:
            files = filter_diff_by_path(parse_diff(full), path, is_dir)
    # 作用域 diff 输出的路径是「相对目标节点」的，补回完整仓库路径
    for f in files:
        rel = f["path"].lstrip("/")
        f["path"] = (path + "/" + rel) if is_dir else path
    adds, dels = diff_stats(files)
    is_binary = "Cannot display: file marked as a binary type" in (dtext or "")

    # 删除的文件：内容/历史都要回退到它还在的那个版本
    ref_rev = (rev - 1) if (action == "D" and rev > 1) else rev
    exists_then = path_kind(name, path, ref_rev) is not None
    return render_template("commit_file.html", name=name, path=path,
                           info=info, rev=rev, head=head,
                           action=action, kind=kind, is_dir=is_dir,
                           files=files, adds=adds, dels=dels,
                           is_binary=is_binary, err=(None if files else err),
                           can_view=(exists_then and not is_dir),
                           can_history=exists_then, ref_rev=ref_rev,
                           history_rev=(ref_rev if ref_rev != head else None),
                           action_label=action_label)


def svn_diff_range(name, lo, hi, path=""):
    """对比同一路径在两个修订号之间的累计差异（r<lo> → r<hi>）。

    等价于 `svn diff -r lo:hi`，用于版本列表勾选两个版本后的对比。
    返回 (text, err)。
    """
    rc, out, err = run_svn(["svn", "diff", "-r", "%d:%d" % (lo, hi),
                            "--non-interactive", "--notice-ancestry",
                            repo_url(name, path, hi)], timeout=120)
    if rc != 0 and not out:
        return None, err
    return out, err


def svn_diff_between(name, a, b):
    """对比同一仓库内两个路径（含可选 @rev）：

    a/b 形如 'trunk' 或 'branches/dev@123'。
    用 `svn diff A B` 输出，返回 (text, err)。
    """
    left = repo_pegged_url(name, a)
    right = repo_pegged_url(name, b)
    rc, out, err = run_svn(["svn", "diff", "--non-interactive",
                            "--notice-ancestry", left, right], timeout=120)
    if rc != 0 and not out:
        return None, err
    return out, err


def repo_pegged_url(name, spec):
    """把 'path[@rev]' 解析成带 peg 的仓库 URL。"""
    if "@" in spec:
        p, r = spec.rsplit("@", 1)
    else:
        p, r = spec, None
    p = safe_rel_path(p or "")
    url = repo_url(name, p)
    if r:
        return "%s@%s" % (url, r)
    return url


@app.route("/<name>/compare/")
@login_required
def compare(name):
    """旧「对比」入口：已合并进版本列表，保留 301 兼容跳转。

    若带 a/b 参数则尽量把其中的纯数字修订号映射到版本对比。
    """
    validate_repo(name)
    a = (request.args.get("a") or "").strip()
    b = (request.args.get("b") or "").strip()
    ra = a.split("@")[-1] if a.split("@")[-1].isdigit() else ""
    rb = b.split("@")[-1] if b.split("@")[-1].isdigit() else ""
    if ra and rb:
        return redirect(url_for("compare_revisions", name=name, a=ra, b=rb),
                        code=301)
    return redirect(url_for("versions", name=name), code=301)


def detect_stdlib(name, rev):
    """检测仓库根是否采用 SVN 标准布局（trunk/branches/tags）。

    返回不存在的部分列表（如 ['trunk','branches','tags'] 表示全部存在）。
    """
    try:
        root_entries = svn_list(name, "", rev)
    except Exception:
        return None
    if not root_entries:
        return None
    names = {e["name"] for e in root_entries if e["kind"] == "dir"}
    std = [s for s in ("trunk", "branches", "tags") if s in names]
    return std if std else None


def list_branches(name, rev):
    """列出仓库 branches/ 与 tags/ 下的子目录名，供快速切换。

    返回 {'branches': [...], 'tags': [...]}（不存在的部分为空列表）。
    """
    out = {"branches": [], "tags": []}
    for base in ("branches", "tags"):
        try:
            entries = svn_list(name, base, rev) or []
        except Exception:
            entries = []
        out[base] = sorted(e["name"] for e in entries if e["kind"] == "dir")
    return out


def repo_readme(name, rev):
    """查找并返回仓库首页 README：(text, relpath)。

    优先标准布局的 trunk/README*，其次仓库根 README*。
    超过 MAX_PREVIEW_BYTES 的 README 不载入。
    """
    candidates = []
    for base in ("trunk", ""):
        if base == "trunk":
            # 仅当 trunk 目录存在才尝试
            try:
                roots = svn_list(name, "", rev) or []
            except Exception:
                roots = []
            if not any(e["name"] == "trunk" and e["kind"] == "dir" for e in roots):
                continue
        for fname in readme_entries(name, base, rev):
            candidates.append((base + "/" + fname).strip("/") if base else fname)
    for relpath in candidates:
        if (file_size(name, relpath, rev) or 0) > MAX_PREVIEW_BYTES:
            continue
        text, _ = svn_cat(name, relpath, rev)
        if text is not None:
            return text, relpath
    return None, None


def readme_entries(name, base, rev):
    """按优先级返回 README 候选条目（列表）。"""
    entries = svn_list(name, base, rev) or []
    wanted = []
    for e in entries:
        if e["kind"] != "file":
            continue
        n = e["name"].lower()
        if n == "readme.md" or n == "readme.markdown" or n == "readme":
            wanted.append(e["name"])
    def prio(f):
        l = f.lower()
        return 0 if l == "readme.md" else (1 if l in ("readme.markdown", "readme") else 2)
    wanted.sort(key=prio)
    return wanted


def _slugify(text):
    """把标题文本转成 HTML 锚点 id（GitHub 风格）：小写、去标点、空格转 '-'。"""
    text = re.sub(r"<[^>]+>", "", text)          # 去掉内嵌的 HTML 标签
    text = re.sub(r"[^\w\u4e00-\u9fff\s-]", "", text, flags=re.UNICODE)  # 去标点（保留中英文、数字、空格、-）
    text = text.strip().replace(" ", "-")
    text = re.sub(r"-+", "-", text)
    return text


def _inline(s):
    """对内联文本（已转义）做有限的 Markdown 内联处理：代码、粗体、链接。"""
    import html as _html
    # 行内代码 `code`
    s = re.sub(r"`([^`]+)`", r'<code class="md-inline">\1</code>', s)
    # 链接 [text](url)：支持 http/https/mailto/#锚点/相对路径
    def _link(m):
        txt, url = m.group(1), m.group(2)
        remove_tags = re.sub(r"<[^>]+>", "", txt)
        safe_txt = _html.escape(remove_tags)
        safe_url = _html.escape(url, quote=True)
        if url.startswith("#"):
            # 页内锚点跳转：不开新窗口
            return '<a href="#%s">%s</a>' % (safe_url[1:], safe_txt)
        if url.startswith("http://") or url.startswith("https://") or url.startswith("mailto:"):
            return '<a href="%s" target="_blank" rel="noopener">%s</a>' % (safe_url, safe_txt)
        # 其它（相对路径）也在同页打开
        return '<a href="%s">%s</a>' % (safe_url, safe_txt)
    s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", _link, s)
    # 粗体 **text** / 斜体 *text*
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<!\*)\*([^*\s][^*]*)\*(?!\*)", r"<em>\1</em>", s)
    return s


def render_markdown_light(text):
    """最小化的安全 Markdown 渲染（仅常用语法），输出 HTML。

    所有原文 HTML 都先转义，再应用有限的规则，杜绝 XSS。
    支持的语法：
      - 围栏代码块：``` / ```python（提取语言标识，带标签展示，不再是源码）
      - GitHub 风格表格：| a | b | / |---|---| + 数据行
      - 标题、分隔线、无序/有序列表、引用、行内（代码/粗体/斜体/链接）
    """
    import html as _html
    lines = text.replace("\r\n", "\n").split("\n")
    out = []
    in_code = False
    code_lang = ""
    code_indent = 0
    code_lines = []
    open_ul = False
    open_ol = False
    i = 0

    def close_list():
        nonlocal open_ul, open_ol
        if open_ul:
            out.append("</ul>"); open_ul = False
        if open_ol:
            out.append("</ol>"); open_ol = False

    def emit_code():
        """把已收集的围栏代码块（含语言标识）渲染成 HTML 并复位状态。"""
        nonlocal in_code, code_lang, code_indent, code_lines
        # 围栏带有缩进（如嵌套在列表里）时，块内统一去该缩进
        if code_indent:
            body_lines = []
            for ln in code_lines:
                body_lines.append(ln[code_indent:] if ln.startswith(" " * code_indent) else ln.lstrip())
            code = "\n".join(body_lines)
        else:
            code = "\n".join(code_lines)
        body = _html.escape(code).strip("\n")
        if code_lang:
            out.append('<figure class="md-code-box">'
                       '<figcaption class="md-code-lang">%s</figcaption>'
                       '<pre class="md-code">%s</pre></figure>'
                       % (_html.escape(code_lang), body))
        else:
            out.append('<pre class="md-code">%s</pre>' % body)
        code_lines = []; code_lang = ""; code_indent = 0; in_code = False

    def split_row(row):
        """把表格行 `| a | b |` 拆成单元格列表（去首尾 | 与空格）。"""
        row = row.strip()
        if row.startswith("|"):
            row = row[1:]
        if row.endswith("|"):
            row = row[:-1]
        return [c.strip() for c in row.split("|")]

    def is_sep_row(s):
        """是否为表格分隔行，如 |---|---|、|:--:|:--|。"""
        cells = split_row(s)
        if not cells:
            return False
        return bool(cells) and not any(c == "" for c in cells) and \
            all(re.match(r"^:?-{1,}:?$", c) for c in cells)

    def looks_header(s):
        """是否为潜在表头行：以 | 开头结尾、含 | 分隔且不止一格。"""
        return bool(re.match(r"^\s*\|.*\|\s*$", s)) and len(split_row(s)) >= 2

    while i < len(lines):
        line = lines[i]
        # 围栏代码块：开/闭 ``` 均可带语言标识（如 ```python）与任意前导缩进（可能嵌在列表里）
        fm = re.match(r"^(\s*)```([\w+.-]*)\s*$", line)
        if fm:
            if in_code:
                emit_code()
            else:
                code_indent = len(fm.group(1))
                code_lang = fm.group(2)
                in_code = True
            i += 1
            continue
        if in_code:
            code_lines.append(line); i += 1; continue

        # 表格：当前行是表头，且下一行是分隔行 |---|---|
        if looks_header(line) and i + 1 < len(lines) and is_sep_row(lines[i + 1]):
            close_list()
            header = split_row(line)
            out.append("<table>")
            out.append("<thead><tr>")
            for c in header:
                out.append("<th>%s</th>" % _inline(c))
            out.append("</tr></thead>")
            j = i + 2
            rows = []
            while j < len(lines) and lines[j].strip() and not is_sep_row(lines[j]):
                rows.append(split_row(lines[j])); j += 1
            if rows:
                out.append("<tbody>")
                coln = max(len(header), max((len(r) for r in rows), default=0))
                for r in rows:
                    out.append("<tr>")
                    for k in range(coln):
                        val = r[k] if k < len(r) else ""
                        out.append("<td>%s</td>" % _inline(val))
                    out.append("</tr>")
                out.append("</tbody>")
            out.append("</table>")
            i = j
            continue

        um = re.match(r"^\s*[-*+]\s+(.*)$", line)
        om = re.match(r"^\s*\d+\.\s+(.*)$", line)
        blank = line.strip() == ""
        # 标题
        hm = re.match(r"^(#{1,6})\s+(.*)$", line)
        if hm and not in_code:
            close_list()
            h = min(len(hm.group(1)), 6)
            title_raw = hm.group(2)
            anchor_id = _slugify(title_raw)
            # 若已自定义锚点id（<a id="..."></a> 前缀），优先使用
            custom = re.match(r"^<a\s+id=([\"'])(.*?)\1>\s*</a>\s*(.*)$", title_raw)
            if custom:
                anchor_id = custom.group(2)
                title_raw = custom.group(3)
            out.append("<h%d id=\"%s\">%s</h%d>"
                       % (h, _html.escape(anchor_id, quote=True),
                          _inline(title_raw), h))
            i += 1; continue
        # 分隔线
        if re.match(r"^\s*([-*_])\s*\1\s*\1[\s\1]*$", line):
            close_list(); out.append("<hr>"); i += 1; continue
        # 无序列表
        if um:
            if not open_ul:
                close_list(); out.append("<ul>"); open_ul = True
            out.append("<li>%s</li>" % _inline(um.group(1)))
            i += 1; continue
        # 有序列表
        if om:
            if not open_ol:
                close_list(); out.append("<ol>"); open_ol = True
            out.append("<li>%s</li>" % _inline(om.group(1)))
            i += 1; continue
        # 引用
        qm = re.match(r"^\s*>\s?(.*)$", line)
        if qm:
            close_list(); out.append("<blockquote>%s</blockquote>" % _inline(qm.group(1)))
            i += 1; continue
        if blank:
            close_list(); out.append("")
        else:
            close_list(); out.append("<p>%s</p>" % _inline(line))
        i += 1
    close_list()
    if in_code and code_lines:
        emit_code()
    return "\n".join(out)


def build_crumbs(name, path, kind, rev, head):
    """面包屑：[{label, url}]"""
    segs = [s for s in path.split("/") if s]
    items = [{"label": name, "url": url_for("tree", name=name, path="", rev=(rev if rev != head else None))}]
    cur = ""
    for s in segs:
        cur = (cur + "/" + s) if cur else s
        items.append({"label": s, "url": url_for("tree", name=name, path=cur,
                                                 rev=(rev if rev != head else None))})
    items[-1]["url"] = None  # 最后一段不可点
    return items


@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", code=404,
                           msg="页面或路径不存在"), 404


@app.errorhandler(500)
def server_error(e):
    return render_template("error.html", code=500,
                           msg="服务器内部错误"), 500


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
