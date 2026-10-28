# -*- coding: utf-8 -*-
"""一键演示数据：在工作区根的 localrepos/ 下建两个演示仓库并填充提交历史。

用途：快速生成一套带真实历史的演示环境（标准布局仓库 + 扁平仓库、分支、标签），
用于本地预览或截图。不会触碰 config.json 指向的生产仓库根。

用法：
    python seed_demo.py [仓库根目录]      # 默认 <工作区根>/localrepos

依赖：PATH 中可找到 svn 客户端（提交通过 file:// 直接写仓库，无需 svnserve）。
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent          # python/
ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else BASE_DIR.parent / "localrepos"

SVN = shutil.which("svn")
if not SVN:
    sys.exit("错误：PATH 中找不到 svn 客户端。请先安装 Subversion "
             "（Windows 可安装 TortoiseSVN 并勾选 command line tools）。")

sys.path.insert(0, str(BASE_DIR))
import app as A  # noqa: E402

if ROOT.exists():
    shutil.rmtree(ROOT)
ROOT.mkdir(parents=True)
A.REPO_ROOT = str(ROOT)
A.ARCHIVE_DIR = str(ROOT / ".archived")
A.AUDIT_PATH = str(ROOT / "audit.log")


def svn(*args, cwd=None):
    p = subprocess.run([SVN, "--non-interactive"] + list(args),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=120, cwd=cwd)
    out = p.stdout.decode("utf-8", "replace")
    err = p.stderr.decode("utf-8", "replace")
    if p.returncode != 0:
        raise RuntimeError("svn %s failed: %s" % (" ".join(args), err))
    return out


def url(name, path=""):
    base = Path(ROOT, name).as_uri()
    return (base + "/" + path) if path else base + "/"


def write(files, root):
    for rel, text in files.items():
        p = Path(root, rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


# ---------- 仓库 1：demo-web（标准布局，多次提交 + 分支标签）----------
ok, msg = A.create_repo("demo-web")
print(msg)
u = url("demo-web")
svn("mkdir", "-m", "初始化标准布局", u + "trunk", u + "branches", u + "tags")

wc = tempfile.mkdtemp(prefix="wc_demo_")
svn("checkout", u + "trunk", wc)

write({
    "README.md": "# demo-web\n\n内部demo项目，用于本地预览 SVN Web 界面。\n\n## 功能\n- 仓库浏览\n- 提交历史\n- 代码高亮\n",
    "app.py": "from flask import Flask\n\napp = Flask(__name__)\n\n@app.route('/')\ndef index():\n    return 'hello demo-web'\n",
    "static/style.css": "body { font-family: sans-serif; margin: 0; }\n.header { background: #24292f; color: #fff; }\n",
    "templates/index.html": "<h1>demo-web</h1>\n<p>模板文件示例</p>\n",
}, wc)
svn("add", "--force", ".", cwd=wc)
svn("commit", "-m", "初始代码：框架与页面模板", cwd=wc)

write({
    "README.md": "# demo-web\n\n内部demo项目，用于本地预览 SVN Web 界面。\n\n## 功能\n- 仓库浏览\n- 提交历史\n- 代码高亮\n- Markdown 渲染\n\n## 更新\n- v1.0 支持 Markdown 渲染\n",
    "utils.py": "def fmt_size(n):\n    for unit in ('B', 'KB', 'MB', 'GB'):\n        if n < 1024:\n            return '%.1f %s' % (n, unit)\n        n /= 1024.0\n",
}, wc)
svn("add", "utils.py", cwd=wc)
svn("commit", "-m", "新增工具函数，README 补充更新记录", cwd=wc)

svn("copy", "-m", "发布 v1.0.0", u + "trunk", u + "tags/v1.0.0")
svn("copy", "-m", "开分支 feature-login", u + "trunk", u + "branches/feature-login")

wc2 = tempfile.mkdtemp(prefix="wc_feat_")
svn("checkout", u + "branches/feature-login", wc2)
write({"login.py": "def login(user, pwd):\n    return user == 'admin' and pwd == 'admin123'\n"}, wc2)
svn("add", "login.py", cwd=wc2)
svn("commit", "-m", "feature-login: 登录模块雏形", cwd=wc2)

# ---------- 仓库 2：tools（扁平布局）----------
ok, msg = A.create_repo("tools")
print(msg)
u = url("tools")
wc3 = tempfile.mkdtemp(prefix="wc_tools_")
svn("checkout", u, wc3)
write({
    "deploy.sh": "#!/bin/sh\nrsync -avz ./dist/ server:/opt/app/\n",
    "backup.py": "import shutil\n\ndef backup(src, dst):\n    shutil.make_archive(dst, 'zip', src)\n",
}, wc3)
svn("add", "--force", ".", cwd=wc3)
svn("commit", "-m", "运维小工具集：部署与备份脚本", cwd=wc3)
write({"monitor.py": "import psutil\n\ndef cpu():\n    return psutil.cpu_percent(interval=1)\n"}, wc3)
svn("add", "monitor.py", cwd=wc3)
svn("commit", "-m", "补充 CPU 监控脚本", cwd=wc3)

# 仓库描述
A.save_repo_desc("demo-web", "内部 demo 项目，用于预览界面效果")
A.save_repo_desc("tools", "运维脚本与监控小工具")

for d in (wc, wc2, wc3):
    shutil.rmtree(d, ignore_errors=True)

print("repos:", A.list_repos())
print("SEED OK ->", ROOT)
