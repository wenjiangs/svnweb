#!/usr/bin/env bash
# =====================================================================
# svnWeb 环境检查脚本（Linux / macOS）
#
# 在首次部署（运行 init_config.py）之前执行，逐项检查运行环境：
#   1. Python >= 3.8 与 venv 模块
#   2. SVN 工具链：svn / svnadmin / svnlook（必需）、svnserve（svn:// 协议需要）
#   3. svnserve 端口是否在监听（支持自定义端口，默认 3690）
#   4. Web 端口 37245 是否空闲
#   5. config.json 是否已生成；repo_root 是否存在、可写、已含多少仓库
#   6. Python 依赖（Flask / Pygments / waitress）是否已安装
#
# 用法：
#   bash check_env.sh [svn端口]        # 端口默认 3690
#
# 退出码：0 = 可以继续部署；1 = 有必须解决的项（FAIL）
# =====================================================================
set -u

SVN_PORT="${1:-3690}"
WEB_PORT=37245
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(dirname "$SCRIPT_DIR")/python"

FAIL=0; WARN=0; OKN=0

if [ -t 1 ]; then
    C_G='\033[32m'; C_Y='\033[33m'; C_R='\033[31m'; C_B='\033[36m'; C_0='\033[0m'
else
    C_G=''; C_Y=''; C_R=''; C_B=''; C_0=''
fi

ok()   { printf "  ${C_G}[通过]${C_0} %s\n" "$1"; OKN=$((OKN+1)); }
warn() { printf "  ${C_Y}[警告]${C_0} %s\n" "$1"; WARN=$((WARN+1)); }
fail() { printf "  ${C_R}[失败]${C_0} %s\n" "$1"; FAIL=$((FAIL+1)); }
info() { printf "  ${C_B}[提示]${C_0} %s\n" "$1"; }

title() { printf "\n${C_B}== %s ==${C_0}\n" "$1"; }

ver_ge() {  # ver_ge A B —— 版本 A >= B ?
    [ "$(printf '%s\n' "$2" "$1" | sort -V | head -1)" = "$2" ]
}

port_listen() {  # port_listen 端口 —— 是否有进程在监听
    if command -v ss >/dev/null 2>&1; then
        ss -lnt 2>/dev/null | grep -q ":$1[[:space:]]" && return 0
    elif command -v netstat >/dev/null 2>&1; then
        netstat -lnt 2>/dev/null | grep -q ":$1[[:space:]]" && return 0
    fi
    (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && { exec 3>&- 3<&-; return 0; }
    return 1
}

printf "${C_B}svnWeb 环境检查${C_0}  （svn 端口=%s，web 端口=%s）\n" "$SVN_PORT" "$WEB_PORT"

# ---------------------------------------------------------- 1. Python
title "1/5 Python"
if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1 && python -c 'import sys; sys.exit(0 if sys.version_info[0]==3 else 1)' 2>/dev/null; then
    PY=python
else
    fail "未找到 Python 3，请先安装（python3 --version）"
    PY=""
fi
if [ -n "$PY" ]; then
    PYV="$($PY -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null)"
    if ver_ge "$PYV" "3.8"; then
        ok "Python $PYV（$PY）"
    else
        fail "Python $PYV 低于 3.8，请升级"
    fi
    if "$PY" -m venv --help >/dev/null 2>&1; then
        ok "venv 模块可用"
    else
        warn "venv 模块不可用（Debian/Ubuntu 可安装 python3-venv 包）"
    fi
fi

# ---------------------------------------------------------- 2. SVN 工具链
title "2/5 SVN 工具链"
for tool in svn svnadmin svnlook; do
    if command -v "$tool" >/dev/null 2>&1; then
        ok "$tool：$(command -v $tool)（$($tool --version --quiet 2>/dev/null)）"
    else
        fail "缺少 $tool（CentOS: yum install subversion / Debian: apt install subversion）"
    fi
done
if command -v svnserve >/dev/null 2>&1; then
    ok "svnserve：$(command -v svnserve)（$(svnserve --version 2>/dev/null | head -1)）"
else
    warn "未找到 svnserve——浏览器仍可用（file:// 直读仓库），但 svn:// 检出/提交不可用"
fi

# ---------------------------------------------------------- 3. 端口
title "3/5 端口"
if port_listen "$SVN_PORT"; then
    ok "端口 $SVN_PORT（svn://）已在监听"
else
    info "端口 $SVN_PORT（svn://）暂无监听——svnserve 未启动或端口不同"
    info "  用自定义端口时：svnserve -r <repo_root> --listen-port <端口>，"
    info "  并把 config.json 的 svn_base 写成 svn://<ip>:<端口>（或留空由页面推断）"
fi
if port_listen "$WEB_PORT"; then
    warn "端口 $WEB_PORT 已被占用——若不是本程序，请换端口启动 waitress"
else
    ok "端口 $WEB_PORT（Web）空闲可用"
fi

# ---------------------------------------------------------- 4. 配置与仓库根
title "4/5 配置与仓库根目录"
if [ -f "$APP_DIR/config.json" ]; then
    ok "config.json 已生成"
    REPO_ROOT="$("$PY" - "$APP_DIR/config.json" <<'PYEOF'
import json, sys
try:
    print(json.load(open(sys.argv[1], encoding="utf-8")).get("repo_root", ""))
except Exception:
    print("")
PYEOF
)"
else
    info "config.json 尚未生成——检查通过后运行：python3 init_config.py"
    REPO_ROOT="${SVNWEB_REPO_ROOT:-}"
fi
if [ -n "$REPO_ROOT" ]; then
    if [ -d "$REPO_ROOT" ]; then
        ok "repo_root 存在：$REPO_ROOT"
        if [ -w "$REPO_ROOT" ]; then
            ok "repo_root 可写"
        else
            fail "repo_root 无写权限（新建仓库需要）：$REPO_ROOT"
        fi
        N=0
        for d in "$REPO_ROOT"/*/; do
            [ -d "$d/db" ] && [ -f "$d/format" ] && N=$((N+1))
        done
        if [ "$N" -gt 0 ]; then
            ok "检测到 $N 个既有 SVN 仓库，登录后即可浏览"
        else
            info "repo_root 下暂无仓库；可在网页「新建仓库」，或手动 svnadmin create"
        fi
    else
        warn "repo_root 不存在：$REPO_ROOT（init_config.py 可自动创建，或手动 mkdir -p）"
    fi
else
    info "跳过 repo_root 检查（未配置；可 SVNWEB_REPO_ROOT=/path 环境变量指定后重跑）"
fi

# ---------------------------------------------------------- 5. Python 依赖
title "5/5 Python 依赖"
if [ -n "$PY" ]; then
    MISS=""
    for mod in flask pygments waitress; do
        "$PY" -c "import $mod" 2>/dev/null || MISS="$MISS $mod"
    done
    if [ -z "$MISS" ]; then
        ok "Flask / Pygments / waitress 均已安装"
    else
        info "尚未安装依赖:$MISS（属正常，装在 venv 里即可）"
        info "  python3 -m venv venv && venv/bin/pip install -r $APP_DIR/requirements.txt"
    fi
fi

# ---------------------------------------------------------- 汇总
printf "\n${C_B}== 汇总 ==${C_0}\n"
printf "  通过 %d 项 / 警告 %d 项 / 失败 %d 项\n" "$OKN" "$WARN" "$FAIL"
if [ "$FAIL" -gt 0 ]; then
    printf "  ${C_R}请先解决「失败」项再运行 init_config.py。${C_0}\n"
    exit 1
fi
printf "  ${C_G}环境就绪，可以运行 init_config.py 完成初始化。${C_0}\n"
exit 0
