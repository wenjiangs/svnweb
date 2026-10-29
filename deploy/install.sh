#!/usr/bin/env bash
# =====================================================================
# svnWeb 一键安装脚本（Linux）
#
# 用法（目标服务器上执行）：
#   wget -O install.sh https://raw.githubusercontent.com/wenjiangs/svnweb/main/deploy/install.sh
#   bash install.sh
#   （不传参数时自动下载 GitHub main 分支代码包；也可显式指定其他地址）
#   bash install.sh https://example.com/svnweb-python.tar.gz
#   bash install.sh /path/to/svnweb-python.tar.gz        # 本地代码包
#
#   代码包默认地址（可用参数覆盖，也可改成 Release 下载链接）：
#   https://github.com/wenjiangs/svnweb/archive/refs/heads/main.tar.gz
#
# 可选参数：
#   --dir <目录>        安装目录（默认 /opt/svnweb，可用环境变量 SVNWEB_INSTALL_DIR）
#   --port <端口>       Web 端口（默认 37245）
#   --svn-port <端口>   svn:// 端口（默认 3690，仅用于环境检查）
#   --no-service        只装文件，不创建/启动 systemd 服务
#   --non-interactive   非交互安装：需预先设置环境变量
#                       SVNWEB_REPO_ROOT（仓库根目录）与
#                       SVNWEB_ADMIN_PASSWORD（admin 登录密码，至少 6 位）
#
# 安装内容：
#   1. 环境检查（先检查后安装；任何一项不满足即退出，不产生任何文件）
#   2. 下载/解压代码包，安装到目标目录
#   3. 创建 venv 并安装 Python 依赖（Flask / Pygments / waitress）
#   4. 初始化 config.json（交互式向导，或 --non-interactive 环境变量方式）
#   5. 创建并启动 systemd 服务 svnweb.service（可 --no-service 跳过）
#
# 退出码：0 = 安装成功；1 = 环境不满足或安装失败
# =====================================================================
set -u

# ---------------------------------------------------------------- 默认配置
SVNWEB_PKG_URL="https://github.com/wenjiangs/svnweb/archive/refs/heads/main.tar.gz"  # 默认：GitHub main 分支
INSTALL_DIR="${SVNWEB_INSTALL_DIR:-/opt/svnweb}"
WEB_PORT=37245
SVN_PORT=3690
MAKE_SERVICE=1
NON_INTERACTIVE=0

APP_DIR="$INSTALL_DIR/python"
SERVICE_NAME=svnweb

# ---------------------------------------------------------------- 参数解析
PKG_URL_ARG=""
while [ $# -gt 0 ]; do
    case "$1" in
        --dir)          INSTALL_DIR="$2"; APP_DIR="$INSTALL_DIR/python"; shift 2 ;;
        --port)         WEB_PORT="$2"; shift 2 ;;
        --svn-port)     SVN_PORT="$2"; shift 2 ;;
        --no-service)   MAKE_SERVICE=0; shift ;;
        --non-interactive) NON_INTERACTIVE=1; shift ;;
        -h|--help)      grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*)             echo "未知选项：$1（--help 查看用法）"; exit 1 ;;
        *)              PKG_URL_ARG="$1"; shift ;;
    esac
done

# ---------------------------------------------------------------- 输出工具
if [ -t 1 ]; then
    C_G='\033[32m'; C_Y='\033[33m'; C_R='\033[31m'; C_B='\033[36m'; C_0='\033[0m'
else
    C_G=''; C_Y=''; C_R=''; C_B=''; C_0=''
fi
ok()   { printf "  ${C_G}[通过]${C_0} %s\n" "$1"; }
warn() { printf "  ${C_Y}[警告]${C_0} %s\n" "$1"; }
fail() { printf "  ${C_R}[失败]${C_0} %s\n" "$1"; }
info() { printf "  ${C_B}[提示]${C_0} %s\n" "$1"; }
title() { printf "\n${C_B}== %s ==${C_0}\n" "$1"; }

ver_ge() { [ "$(printf '%s\n' "$2" "$1" | sort -V | head -1)" = "$2" ]; }

port_listen() {
    if command -v ss >/dev/null 2>&1; then
        ss -lnt 2>/dev/null | grep -q ":$1[[:space:]]" && return 0
    elif command -v netstat >/dev/null 2>&1; then
        netstat -lnt 2>/dev/null | grep -q ":$1[[:space:]]" && return 0
    fi
    (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && { exec 3>&- 3<&-; return 0; }
    return 1
}

# 下载到指定文件（优先 curl，其次 wget）
fetch_to() {  # fetch_to <url> <output-file>
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL --connect-timeout 15 -o "$2" "$1"
    elif command -v wget >/dev/null 2>&1; then
        wget -q --timeout=15 -O "$2" "$1"
    else
        return 127
    fi
}

# ---------------------------------------------------------------- 全局状态
FAIL=0
SUCCESS=0
CREATED_INSTALL_DIR=0
TMP_DIR=""
PKG_SOURCE=""

cleanup() {
    # 临时目录无论如何都清掉
    if [ -n "$TMP_DIR" ] && [ -d "$TMP_DIR" ]; then
        rm -rf "$TMP_DIR"
    fi
    # 若目标目录是本脚本新建且安装未成功完成，回滚删除，不留半成品
    if [ "$SUCCESS" != "1" ] && [ "$CREATED_INSTALL_DIR" = "1" ] && [ -d "$INSTALL_DIR" ]; then
        rm -rf "$INSTALL_DIR"
        printf "${C_Y}安装失败，已回滚删除新建目录：%s${C_0}\n" "$INSTALL_DIR"
    fi
}
trap cleanup EXIT

die() {  # 安装中途失败（可能已产生文件，走回滚清理）
    printf "\n${C_R}安装失败：%s${C_0}\n" "$1"
    exit 1
}

# ================================================================
# 第一阶段：环境检查（纯只读，不产生任何文件）
# ================================================================
printf "${C_B}svnWeb 一键安装${C_0}  （安装目录=%s，Web 端口=%s）\n" "$INSTALL_DIR" "$WEB_PORT"

# ---------- 0. 代码包来源 ----------
title "0/6 代码包来源"
LOCAL_REPO_SOURCE=""
if [ -n "$PKG_URL_ARG" ]; then
    PKG_SOURCE="$PKG_URL_ARG"
elif [ -n "$SVNWEB_PKG_URL" ]; then
    PKG_SOURCE="$SVNWEB_PKG_URL"
elif [ -f "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)/../python/app.py" ]; then
    LOCAL_REPO_SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
    ok "脚本位于仓库内（deploy/），将直接安装本仓库代码：$LOCAL_REPO_SOURCE"
elif [ -f "./python/app.py" ]; then
    LOCAL_REPO_SOURCE="$(pwd)"
    ok "当前目录为仓库根，将直接安装本仓库代码：$LOCAL_REPO_SOURCE"
fi

if [ -z "$PKG_SOURCE" ] && [ -z "$LOCAL_REPO_SOURCE" ]; then
    fail "未指定代码包来源"
    info "用法：bash install.sh <代码包地址(.tar.gz/.tgz/.zip)>"
    info "或在脚本顶部写死 SVNWEB_PKG_URL 后免参数运行"
    FAIL=$((FAIL+1))
else
    ok "代码包来源：$PKG_SOURCE"
fi

# ---------- 1. Python ----------
title "1/6 Python（需 3.8+，含 venv / pip）"
PY=""
if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1 && python -c 'import sys; sys.exit(0 if sys.version_info[0]==3 else 1)' 2>/dev/null; then
    PY=python
fi
if [ -z "$PY" ]; then
    fail "未找到 Python 3。请先安装："
    info "  CentOS 7:        yum install -y python3"
    info "  CentOS 8+/Anolis: dnf install -y python3"
    info "  Debian/Ubuntu:   apt install -y python3 python3-venv"
    FAIL=$((FAIL+1))
else
    PYV="$($PY -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null)"
    if ver_ge "$PYV" "3.8"; then
        ok "Python $PYV（$PY）"
    else
        fail "Python $PYV 低于 3.8，请升级后重试"
        FAIL=$((FAIL+1))
    fi
    if "$PY" -m venv --help >/dev/null 2>&1; then
        ok "venv 模块可用"
    else
        fail "venv 模块不可用（Debian/Ubuntu: apt install python3-venv；CentOS: yum install python3）"
        FAIL=$((FAIL+1))
    fi
    if "$PY" -m ensurepip --version >/dev/null 2>&1; then
        ok "venv 内置 pip 可用（ensurepip）"
    else
        warn "ensurepip 不可用——venv 可能缺 pip（Debian/Ubuntu: apt install python3-venv）"
    fi
fi

# ---------- 2. SVN 工具链 ----------
title "2/6 SVN 工具链"
for tool in svn svnadmin svnlook; do
    if command -v "$tool" >/dev/null 2>&1; then
        ok "$tool：$($tool --version --quiet 2>/dev/null)"
    else
        fail "缺少 $tool（CentOS: yum install subversion / Debian: apt install subversion）"
        FAIL=$((FAIL+1))
    fi
done
if command -v svnserve >/dev/null 2>&1; then
    ok "svnserve 已安装（svn:// 协议可用）"
else
    warn "未找到 svnserve——Web 浏览仍可用（file:// 直读仓库），但 svn:// 检出/提交不可用"
fi

# ---------- 3. 端口 ----------
title "3/6 端口"
if port_listen "$WEB_PORT"; then
    fail "端口 $WEB_PORT 已被占用——请换端口安装：bash install.sh --port <其他端口>"
    FAIL=$((FAIL+1))
else
    ok "端口 $WEB_PORT（Web）空闲"
fi
if port_listen "$SVN_PORT"; then
    ok "端口 $SVN_PORT（svn://）已在监听"
else
    info "端口 $SVN_PORT（svn://）暂无监听——不影响安装，svnserve 可稍后自行配置"
fi

# ---------- 4. 下载与解压工具 ----------
title "4/6 下载与解压工具"
if [ -n "$PKG_SOURCE" ]; then
    case "$PKG_SOURCE" in
        http://*|https://*)
            if command -v curl >/dev/null 2>&1 || command -v wget >/dev/null 2>&1; then
                ok "curl/wget 可用"
            else
                fail "需要 curl 或 wget 下载代码包（CentOS: yum install curl / Debian: apt install curl）"
                FAIL=$((FAIL+1))
            fi
            ;;
        *)
            if [ -f "$PKG_SOURCE" ]; then
                ok "本地代码包存在：$PKG_SOURCE"
            else
                fail "本地代码包不存在：$PKG_SOURCE"
                FAIL=$((FAIL+1))
            fi
            ;;
    esac
    case "$PKG_SOURCE" in
        *.zip)
            if command -v unzip >/dev/null 2>&1; then
                ok "unzip 可用"
            else
                fail "需要 unzip 解压 .zip 包（CentOS: yum install unzip / Debian: apt install unzip）"
                FAIL=$((FAIL+1))
            fi
            EXT=zip
            ;;
        *)
            if command -v tar >/dev/null 2>&1; then
                ok "tar 可用"
            else
                fail "缺少 tar，无法解压代码包"
                FAIL=$((FAIL+1))
            fi
            EXT=tar
            ;;
    esac
else
    ok "使用本仓库代码，无需下载/解压工具"
fi

# ---------- 5. 权限与安装目录 ----------
title "5/6 权限与安装目录"
if [ "$(id -u)" = "0" ]; then
    ok "root 权限"
else
    if [ ! -e "$INSTALL_DIR" ] && [ -w "$(dirname "$INSTALL_DIR")" ]; then
        warn "非 root 运行，但 $INSTALL_DIR 的父目录可写，可继续（systemd 服务将跳过）"
    else
        fail "非 root 权限无法写入 $INSTALL_DIR，请用 sudo 或 --dir 指定可写目录"
        FAIL=$((FAIL+1))
    fi
fi
if [ -e "$INSTALL_DIR" ]; then
    fail "安装目录已存在：$INSTALL_DIR"
    info "  若是旧安装，请先备份并移除，或换目录：bash install.sh --dir <其他目录>"
    FAIL=$((FAIL+1))
else
    ok "安装目录 $INSTALL_DIR 不存在，将新建"
fi

# ---------- 6. systemd ----------
title "6/6 systemd 服务"
SYSTEMD_OK=0
if [ "$(id -u)" = "0" ] && command -v systemctl >/dev/null 2>&1 \
   && systemctl is-system-running >/dev/null 2>&1; then
    if [ "$MAKE_SERVICE" = "1" ]; then
        ok "systemd 可用，将创建 $SERVICE_NAME.service"
        SYSTEMD_OK=1
    else
        info "已指定 --no-service，跳过服务创建"
    fi
elif [ "$MAKE_SERVICE" = "1" ]; then
    warn "systemd 不可用（无 root 或非 systemd 系统）——将跳过服务创建，装完附手动启动说明"
fi

# ---------- 汇总：任何一项失败 => 立即退出，不产生任何文件 ----------
if [ "$FAIL" -gt 0 ]; then
    printf "\n"
    fail "共 $FAIL 项环境检查未通过"
    printf "  ${C_R}请先解决以上「失败」项，再重新运行本脚本。本次安装已停止，未产生任何文件。${C_0}\n"
    exit 1
fi
printf "\n${C_G}环境检查全部通过，开始安装……${C_0}\n"

# ================================================================
# 第二阶段：临时区下载 / 解压 / 校验 / venv 冒烟测试（失败自动清理）
# ================================================================
TMP_DIR="$(mktemp -d /tmp/svnweb-install.XXXXXX)"

SRC_DIR=""
if [ -n "$LOCAL_REPO_SOURCE" ]; then
    SRC_DIR="$LOCAL_REPO_SOURCE/python"
else
    title "下载代码包"
    PKG_FILE="$TMP_DIR/svnweb.$EXT"
    case "$PKG_SOURCE" in
        http://*|https://*) fetch_to "$PKG_SOURCE" "$PKG_FILE" || die "代码包下载失败：$PKG_SOURCE" ;;
        *)                  cp "$PKG_SOURCE" "$PKG_FILE" || die "代码包复制失败：$PKG_SOURCE" ;;
    esac
    ok "就绪：$(du -h "$PKG_FILE" 2>/dev/null | cut -f1)"

    title "解压代码包"
    EXTRACT_DIR="$TMP_DIR/extract"
    mkdir -p "$EXTRACT_DIR"
    if [ "$EXT" = "zip" ]; then
        unzip -q "$PKG_FILE" -d "$EXTRACT_DIR" || die "zip 解压失败"
    else
        tar -xzf "$PKG_FILE" -C "$EXTRACT_DIR" 2>/dev/null \
            || tar -xf "$PKG_FILE" -C "$EXTRACT_DIR" 2>/dev/null \
            || die "解压失败（请确认是 .tar.gz/.tgz 格式）"
    fi
    # 定位 python/ 目录（兼容压缩包带一层外层目录的情况）
    if [ -f "$EXTRACT_DIR/python/app.py" ]; then
        SRC_DIR="$EXTRACT_DIR/python"
    else
        SRC_DIR="$(dirname "$(find "$EXTRACT_DIR" -maxdepth 4 -type f -name app.py -path '*/python/*' 2>/dev/null | head -1)")"
    fi
    [ -f "$SRC_DIR/app.py" ] || die "代码包中未找到 python/app.py——请确认打包内容包含 python/ 目录"
    ok "代码定位：$SRC_DIR"
fi

title "校验程序文件"
for f in app.py requirements.txt init_config.py config.example.json; do
    [ -f "$SRC_DIR/$f" ] || die "代码包缺少 $SRC_DIR/$f"
    ok "$f"
done
[ -d "$SRC_DIR/templates" ] || die "代码包缺少 templates/ 目录"
ok "templates/"
[ -d "$SRC_DIR/static" ] || die "代码包缺少 static/ 目录"
ok "static/"

title "venv 冒烟测试"
VENVTEST_OK=0
if "$PY" -m venv "$TMP_DIR/venvtest" >/dev/null 2>&1; then
    for vbin in "$TMP_DIR/venvtest/bin" "$TMP_DIR/venvtest/Scripts"; do
        if [ -x "$vbin/python" ] || [ -x "$vbin/python.exe" ]; then
            "$vbin/python" -m pip --version >/dev/null 2>&1 && VENVTEST_OK=1 && break
        fi
    done
fi
if [ "$VENVTEST_OK" = "1" ]; then
    ok "venv + pip 可正常创建"
else
    die "venv/pip 冒烟测试失败（Debian/Ubuntu: apt install python3-venv python3-pip）"
fi

# ================================================================
# 第三阶段：正式安装（此后任何失败都会回滚删除安装目录）
# ================================================================
title "安装程序文件"
mkdir -p "$INSTALL_DIR" || die "无法创建安装目录 $INSTALL_DIR"
CREATED_INSTALL_DIR=1
cp -r "$SRC_DIR" "$APP_DIR" || die "复制程序文件失败"
# 清掉不该带上的文件
rm -rf "$APP_DIR/__pycache__" "$APP_DIR/config.json" "$APP_DIR/config.json.bak" \
       "$APP_DIR/audit.log" 2>/dev/null
ok "程序文件已安装到 $APP_DIR"

title "创建 venv 并安装依赖"
"$PY" -m venv "$APP_DIR/venv" || die "venv 创建失败"
VENV_BIN="$APP_DIR/venv/bin"      # Windows venv 为 Scripts/，Linux 为 bin/
if [ ! -x "$VENV_BIN/python" ] && [ ! -x "$VENV_BIN/python.exe" ]; then
    VENV_BIN="$APP_DIR/venv/Scripts"
fi
if ! "$VENV_BIN/python" -m pip install -q -r "$APP_DIR/requirements.txt"; then
    die "pip 依赖安装失败（网络问题可先执行 export PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple 再重试）"
fi
ok "依赖安装完成（Flask / Pygments / waitress）"

title "初始化配置 config.json"
if [ "$NON_INTERACTIVE" = "1" ]; then
    if [ -z "${SVNWEB_REPO_ROOT:-}" ] || [ -z "${SVNWEB_ADMIN_PASSWORD:-}" ]; then
        die "--non-interactive 需要环境变量 SVNWEB_REPO_ROOT 与 SVNWEB_ADMIN_PASSWORD"
    fi
    if [ "${#SVNWEB_ADMIN_PASSWORD}" -lt 6 ]; then
        die "SVNWEB_ADMIN_PASSWORD 至少 6 位"
    fi
    SVNWEB_ADMIN_USER="${SVNWEB_ADMIN_USER:-admin}"
    SVNWEB_REPO_ROOT="${SVNWEB_REPO_ROOT%/}"
    if [ ! -d "$SVNWEB_REPO_ROOT" ]; then
        mkdir -p "$SVNWEB_REPO_ROOT" || die "无法创建仓库根目录 $SVNWEB_REPO_ROOT"
        ok "已创建仓库根目录：$SVNWEB_REPO_ROOT"
    fi
    SVNWEB_ADMIN_USER="$SVNWEB_ADMIN_USER" \
    SVNWEB_ADMIN_PASSWORD="$SVNWEB_ADMIN_PASSWORD" \
    SVNWEB_REPO_ROOT="$SVNWEB_REPO_ROOT" \
    "$VENV_BIN/python" - "$APP_DIR" <<'PYEOF'
import json, os, secrets, sys
from werkzeug.security import generate_password_hash
base = sys.argv[1]
with open(os.path.join(base, "config.example.json"), encoding="utf-8") as f:
    cfg = json.load(f)
cfg["repo_root"] = os.environ["SVNWEB_REPO_ROOT"]
cfg["username"] = os.environ["SVNWEB_ADMIN_USER"]
cfg["password_hash"] = generate_password_hash(os.environ["SVNWEB_ADMIN_PASSWORD"])
cfg["secret_key"] = secrets.token_hex(16)
with open(os.path.join(base, "config.json"), "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
print("config.json 已生成")
PYEOF
    ok "config.json 已生成（非交互模式）"
else
    # 交互式向导（init_config.py 自带确认/备份逻辑）
    "$VENV_BIN/python" "$APP_DIR/init_config.py" || die "配置初始化未完成"
    [ -f "$APP_DIR/config.json" ] || die "config.json 未生成"
    ok "config.json 已生成"
fi

# ---------- systemd 服务 ----------
if [ "$SYSTEMD_OK" = "1" ]; then
    title "创建并启动 systemd 服务"
    cat > "/etc/systemd/system/$SERVICE_NAME.service" <<EOF
[Unit]
Description=svnWeb - GitHub style SVN web browser
After=network.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/waitress-serve --listen=0.0.0.0:$WEB_PORT --threads=8 --channel-timeout=120 app:app
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --quiet "$SERVICE_NAME"
    systemctl restart "$SERVICE_NAME"
    ok "服务已创建并启动：systemctl status $SERVICE_NAME"

    # 启动自检
    title "启动自检"
    HEALTH_OK=0
    for i in 1 2 3 4 5 6 7 8 9 10; do
        if command -v curl >/dev/null 2>&1; then
            CODE="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$WEB_PORT/healthz" 2>/dev/null || echo 000)"
        else
            CODE="$(wget -q -O /dev/null --server-response "http://127.0.0.1:$WEB_PORT/healthz" 2>&1 | awk '/HTTP\\//{print $2; exit}')"
        fi
        if [ "$CODE" = "200" ]; then HEALTH_OK=1; break; fi
        sleep 1
    done
    if [ "$HEALTH_OK" = "1" ]; then
        ok "服务健康检查通过（/healthz = 200）"
    else
        warn "健康检查未通过（可能仍在启动）——请执行：systemctl status $SERVICE_NAME 与 journalctl -u $SERVICE_NAME -e"
    fi
fi

# ---------- 完成 ----------
SUCCESS=1
printf "\n${C_G}==============================================${C_0}\n"
printf "${C_G}  svnWeb 安装完成！${C_0}\n"
printf "${C_G}==============================================${C_0}\n"
printf "  安装目录：%s\n" "$INSTALL_DIR"
printf "  配置文件：%s\n" "$APP_DIR/config.json"
if [ "$SYSTEMD_OK" = "1" ]; then
    printf "  访问地址：http://<服务器IP>:%s/\n" "$WEB_PORT"
    printf "  常用命令：systemctl restart %s | journalctl -u %s -e\n" "$SERVICE_NAME" "$SERVICE_NAME"
else
    printf "  手动启动：cd %s && venv/bin/waitress-serve --listen=0.0.0.0:%s --threads=8 app:app\n" "$APP_DIR" "$WEB_PORT"
fi
printf "  首次访问请使用安装时设置的账号密码登录。\n"
exit 0
