# SVN Web —— GitHub 风格的 Subversion 网页浏览器

一个基于 **Python 3 + Flask + Pygments** 的轻量 SVN 网页浏览与管理工具。  
它**不做底层存储**，而是通过调用系统自带的 `svn` / `svnlook` / `svnadmin` 命令行直接读写本地  
SVN 仓库目录——**任何装了 Subversion 的机器，部署本程序即可通过 Web 管理与浏览 SVN 仓库**。

![Python](https://img.shields.io/badge/Python-3.8%2B-blue)

![Flask](https://img.shields.io/badge/Flask-3.0-green)

![License](https://img.shields.io/badge/License-MIT-yellow)

## 功能一览

**浏览与查看**

- 仓库列表、目录树、面包屑导航，SVN 标准布局识别（trunk/branches/tags 快捷导航 + 分支快速切换）
- 单文件查看（Pygments 语法高亮）、原始内容与下载（流式）、媒体内联预览（图片/PDF/音视频，MIME 白名单）
- README/.md 在线渲染（轻量 Markdown）、大文件保护（超限只下载不预览）
- 文件 Blame 逐行追溯

**历史与差异**

- 版本列表（真表格，勾选两个版本即可发起区间 diff 对比，最多同时选中两个）
- 版本详情页（元信息 + 变更文件清单）；单文件变更详情页（该文件在该次提交中的改动）
- 版本区间对比（`svn diff -r a:b`）、提交 diff 文件级筛选
- **全历史搜索**三档范围：提交信息 / 文件名 / 代码变更（含被删文件的内容，不区分大小写子串匹配，缓存加速）
- 「最近提交」全局动态页（聚合所有仓库的提交流）

**管理（admin 角色）**

- 新建仓库（自动写入 svnserve 配置并沿用既有账号权限）、**归档 / 恢复 / 彻底删除 / 重命名**
- **用户管理页**：SVN 账号集中管理，用户 × 仓库**权限矩阵**（读写/只读/无），保存即同步到所有仓库的 passwd/authz
- 仓库级：账号一览、用户组、authz 图形化编辑 + 高级原文编辑、`svnadmin dump` 在线备份与下载、  
  hooks 在线编辑、仓库锁查看与强制解除
- 分支/标签创建（`svn copy`）、仓库打包下载（zip）
- 审计日志（管理操作全埋点，可按仓库/动作过滤）、系统状态页（svnserve 状态、磁盘用量、服务重启）

**安全**

- 登录防爆破（连续失败锁定）、开放重定向加固、路径穿越防护（`..` 直接 404）、仓库名白名单校验
- 密码 Werkzeug 哈希存储（scrypt / pbkdf2）、Web 多账号角色（admin / viewer，viewer 只读）
- 媒体白名单刻意排除 html/htm/js（防同源 XSS）、在线改密（Web 侧，会话不掉线）
- 可选 HTTPS 反代（内置 nginx 示例 + `behind_proxy` 信任头）

---

## 一、目录结构

```
svnWeb/
├── README.md               # 本文档
├── LICENSE                 # MIT 许可证
├── changelog.md            # 更新日志（按时间顺序记录功能变更与修复）
├── deploy/
│   ├── check_env.sh        # Linux 环境检查（Python/SVN 工具链/端口/仓库根逐项体检）
│   ├── check_env.bat       # Windows 环境检查
│   └── nginx-svnweb.conf.example  # nginx HTTPS 反代示例
├── python/                 # 程序本体
│   ├── app.py              # 全部后端逻辑（单文件应用）
│   ├── init_config.py      # 可选：命令行交互式初始化/重置（生成 secret_key 与密码哈希；日常首次初始化用 Web 安装向导）
│   ├── config.example.json # 配置模板（secret_key / password_hash 均为占位符）
│   ├── requirements.txt    # Python 依赖（Flask / Pygments / waitress）
│   ├── static/
│   │   ├── style.css       # GitHub 风格样式表
│   │   ├── pjax.js         # 局部刷新（含弹窗 / 复制等全局脚本）
│   │   └── nprogress.js    # 顶部进度条（附 .css）
│   └── templates/          # Jinja2 模板
│       ├── base.html       # 基础布局 + 全局导航 + 弹窗脚本
│       ├── install.html    # 首次访问安装向导（环境自检 + 分步配置）
│       ├── login.html      # 登录页
│       ├── index.html      # 仓库列表首页
│       ├── recent.html     # 最近提交（全局动态）
│       ├── server.html     # 系统状态（磁盘 / SVN 服务 / 仓库根目录 / 重启）
│       ├── users_*.html    # 用户管理（SVN 账号 × 仓库权限矩阵 / Web 账号）
│       ├── admin_*.html    # 仓库管理（概览/账号/组/权限/备份/hooks/锁/危险操作）
│       ├── newrepo.html    # 新建仓库表单
│       ├── created.html    # 创建成功页（展示检出地址 + 初始化命令）
│       ├── tree.html       # 目录树视图
│       ├── blob.html       # 单文件查看（高亮 / Markdown 渲染 / 媒体预览）
│       ├── blame.html      # 逐行追溯
│       ├── versions.html   # 版本列表表格（勾选两个版本 → 发起对比）
│       ├── commit.html     # 版本详情（元信息 + 变更文件清单）
│       ├── commit_file.html # 单文件变更详情
│       ├── compare_revs.html # 版本对比结果
│       ├── audit.html      # 审计日志查询
│       ├── archived.html   # 已归档仓库管理
│       └── …（admin_layout / users_layout / _repohead / _reposide /
│            _breadcrumb / _clonemodal / error 等布局与通用片段）
```

> 运行期会在 `config.json` 的 `repo_root` 下产生仓库数据；备份默认写到程序目录 `backups/`，  
> 审计日志为 `audit.log`——均已 gitignore，不属于源码。

---

## 二、技术栈

| 组件       | 选型                                          | 说明                              |
| -------- | ------------------------------------------- | ------------------------------- |
| 语言       | Python ≥ 3.8                                | 无编译依赖                           |
| Web 框架   | Flask 3.0                                   | 轻量、单文件即可组织                      |
| WSGI 服务器 | waitress                                    | 跨平台纯 Python，生产级，多线程             |
| 语法高亮     | Pygments                                    | `get_lexer_for_filename` 自动识别语言 |
| SVN 客户端  | 系统 `svn` / `svnlook` / `svnadmin`           | 通过 `subprocess` 调用，对仓库零风险       |
| 密码存储     | `werkzeug.security`（scrypt / pbkdf2:sha256） | 登录校验                            |

依赖清单（`python/requirements.txt`）：`Flask`、`Pygments`、`waitress`（版本锁定见文件）。

---

## 三、快速开始

> **⚠️ 平台建议：推荐部署在 Linux 上，不推荐在 Windows 中使用本程序。**
>
> Windows 上最常见的 SVN 服务端是 **VisualSVN Server**（图形化管理器），它的用户、  
> 组、权限存在自己的中央数据库里，与本程序管理的 `conf/passwd` + `conf/authz`  
> 文件体系**互不相通**——你会遇到「Manager 里建的仓库网页看不到」「网页里设的  
> 权限对 https 检出不生效」等一系列错位。完整差异与原因见  
> **[七・与 VisualSVN Server 的差异](#七补与-visualsvn-server图形化管理器的差异windows-用户必读)**。  
> 本程序的设计目标是「文件即真相」的 Linux 服务器场景；Windows 仅保证程序能运行  
> （代码浏览不受影响），管理类功能（用户 / 权限 / 组 / 服务重启）不保证可用。

前提：机器上已安装 **Subversion**（提供 `svn` / `svnlook` / `svnadmin`）与 **Python ≥ 3.8**。

### 一键安装（Linux，推荐）

```bash
wget -O install.sh https://raw.githubusercontent.com/wenjiangs/svnweb/main/deploy/install.sh
bash install.sh
```

> GitHub 访问受限的服务器可用加速镜像下载脚本与代码包（脚本内置同样的镜像回退）：
>
> ```bash
> wget -O install.sh https://ghfast.top/https://raw.githubusercontent.com/wenjiangs/svnweb/main/deploy/install.sh
> ```

脚本会先做**纯只读环境检查**（Python ≥ 3.8 与 venv/pip、svn/svnadmin/svnlook、端口、
权限、systemd），**任何一项不满足即停止安装，不在系统上产生任何文件**；检查通过后自动
下载代码 → 安装到 `/opt/svnweb` → 创建 venv 装依赖 → 初始化 `config.json` →
注册并启动 `systemd` 服务 `svnweb`，最后做 `/healthz` 健康自检。

常用变体：

```bash
bash install.sh --dir /opt/svnweb --port 37245          # 自定义目录与端口
bash install.sh /path/to/svnweb-python.tar.gz           # 使用本地代码包（离线安装）
bash install.sh --no-service                            # 只装文件，不创建 systemd 服务

# 无人值守（免交互向导）：
SVNWEB_REPO_ROOT=/svn SVNWEB_ADMIN_PASSWORD='你的密码(≥6位)' \
  bash install.sh --non-interactive
```

### 手动安装

```bash
# 0) 环境检查（可选但推荐：Python/SVN 工具链、端口、仓库根、依赖逐项体检）
#    Linux:   bash deploy/check_env.sh [svn端口]      # 端口默认 3690
#    Windows: deploy\check_env.bat [svn端口]
#    自定义了 svn 端口就把端口传进去；脚本会提示检出地址前缀该怎么配

# 1) 获取源码
git clone https://github.com/wenjiangs/svnweb.git && cd svnweb/python

# 2) 虚拟环境 + 依赖
python3 -m venv venv
venv/bin/pip install -r requirements.txt      # Windows: venv\Scripts\pip install -r requirements.txt

# 3) 启动，浏览器打开 http://<ip>:37245
venv/bin/waitress-serve --listen=0.0.0.0:37245 app:app
# 或开发预览：python3 app.py
```

**首次访问自动进入安装向导**（`/install`）：页面自带环境自检报告（Python / SVN 工具链 /  
svnserve 端口监听状态），填入仓库根目录、检出地址（支持自定义 SVN 端口）、  
第一个管理员账号密码，提交即生成 `config.json` 并**立即生效、无需重启**；  
此后向导永久关闭（不可被他人重装覆盖）。

也可以用命令行初始化代替网页向导：

```bash
python3 init_config.py
```

---

## 四、配置说明 `config.json`

由 `init_config.py` 生成，也可参照 `config.example.json` 手写。常用项：

| 键                                       | 默认         | 说明                                                                                      |
| --------------------------------------- | ---------- | --------------------------------------------------------------------------------------- |
| `repo_root`                             | `/svn`     | SVN 仓库父目录（与 svnserve 的 `-r` 一致），所有仓库都在其下                                                |
| `username` / `password_hash`            | —          | Web 登录单账号；密码哈希由 `init_config.py` 生成，也可在线改密                                              |
| `secret_key`                            | —          | Flask session 签名密钥，**必须随机生成**（init_config.py 自动处理）                                      |
| `svn_base`                              | 空          | 对外展示的检出地址前缀（如 `svn://192.168.1.10`）；留空按访问域名推断                                           |
| `behind_proxy`                          | `false`    | 部署在 nginx 反代后设为 `true`（启用 ProxyFix）                                                     |
| `web_users`                             | `{}`       | 可选多账号：`{"用户名": {"password_hash": "...", "role": "admin"\|"viewer"}}`；不配置时沿用顶层单账号（admin） |
| `backup_dir`                            | `backups/` | `svnadmin dump` 备份文件的存放目录                                                               |
| `cache_ttl`                             | `20`       | svn 命令结果内存缓存秒数                                                                          |
| `max_highlight_bytes`                   | 512KB      | 超过不做语法高亮                                                                                |
| `max_preview_bytes`                     | 5MB        | blob 在线预览上限，超过只可下载                                                                      |
| `max_media_bytes`                       | 32MB       | 媒体内联预览上限（图片/PDF/音视频流式）                                                                  |
| `log_page_size`                         | `60`       | 版本列表每页条数                                                                                |
| `login_max_fail` / `login_lock_seconds` | 5 / 300    | 登录防爆破：连续失败次数 / 锁定秒数                                                                     |
| `recent_limit` / `recent_per_repo`      | 60 / 40    | 「最近提交」页聚合条数 / 每仓上限                                                                      |

> **`config.json` 含 `password_hash` 与 `secret_key`，属敏感文件，已列入 `.gitignore`，勿提交公开仓库。**

---

## 五、关键路由表

| 方法/路径                                       | 说明                                                         |
| ------------------------------------------- | ---------------------------------------------------------- |
| GET/POST `/login`、GET `/logout`             | 登录 / 退出                                                    |
| POST `/account/password`                    | 在线修改当前 Web 账号密码（会话不掉线）                                     |
| GET `/`                                     | 仓库列表（搜索过滤）                                                 |
| GET `/<name>`、`/<name>/<path>`              | 目录树浏览（文件自动跳转文件视图）                                          |
| GET `/<name>/blob/<path>`                   | 文件查看（高亮 / Markdown 渲染 / 媒体预览）                              |
| GET `/<name>/media/<path>`                  | 媒体内联预览（MIME 白名单 + 流式）                                      |
| GET `/<name>/raw/<path>`、`/download/<path>` | 原始内容 / 下载（流式）                                              |
| GET `/<name>/blame/<path>`                  | 文件逐行追溯                                                     |
| GET `/<name>/versions/[<path>]`             | 版本列表（`?search=&scope=msg,path,code` 全历史搜索，`?before=` 加载更多） |
| GET `/<name>/commit/<rev>`                  | 版本详情（变更文件清单）                                               |
| GET `/<name>/commit/<rev>/file/<path>`      | 单文件变更详情                                                    |
| GET `/<name>/compare/revisions/?a=&b=`      | 版本区间对比                                                     |
| GET/POST `/<name>/admin/<section>`          | 仓库管理（overview/accounts/groups/authz/backup/hooks/locks）    |
| GET/POST `/users/`                          | 用户管理（SVN 账号 × 仓库权限矩阵 + Web 账号改密）                           |
| POST `/<name>/branch`                       | 创建分支/标签（svn copy）                                          |
| GET `/<name>/archive/<path>`                | 目录打包下载（zip，超限 413）                                         |
| GET `/<name>/backup/<fname>`                | 下载 `svnadmin dump` 备份文件                                    |
| GET `/admin/new`                            | 新建仓库；GET/POST `/archived/` 归档仓库恢复/彻底删除                     |
| GET `/recent/`                              | 最近提交（全局动态）                                                 |
| GET `/audit/`                               | 审计日志（按仓库/动作过滤）                                             |
| GET `/server/`、POST `/server/restart`       | 系统状态页 / 重启 svnweb 服务                                       |
| GET `/healthz`                              | 免登录健康检查                                                    |
| GET `/<name>/commits/`、`/compare/`          | 旧入口，301 兼容跳转                                               |


除 `/login`、`/logout`、`/healthz` 外，所有路由均有登录保护；管理类路由仅 admin 角色可用，  
viewer 角色只读。

> **仓库名限制**：仓库直接作为顶层路径（`/<name>`），故仓库名不得与系统路由保留名  
> （`login`、`admin`、`users`、`recent`、`audit`、`server`、`static`、`healthz` 等）冲突，  
> 创建时自动校验并拒绝。

---

## 六、部署说明（生产环境）

### 6.1 环境依赖

- **推荐 Linux**（生产示例为 Linux + systemd）；Windows 可运行但不推荐，见  
  [七](#七补与-visualsvn-server图形化管理器的差异windows-用户必读)
- 系统包：`subversion`（svn / svnlook / svnadmin）
- Python ≥ 3.8（建议虚拟环境）

### 6.2 svnserve（svn:// 协议，3690 端口）

`svnserve` 负责 `svn://` 协议的账号认证与读写；Web 层在线维护的 passwd/authz 与 svnserve 共用。

```ini
# /etc/systemd/system/svnserve.service
[Unit]
Description=Subversion protocol daemon
After=network.target

[Service]
Type=forking
ExecStart=/usr/bin/svnserve --daemon --pid-file=/run/svnserve/svnserve.pid -r /svn
PIDFile=/run/svnserve/svnserve.pid
RuntimeDirectory=svnserve
RuntimeDirectoryMode=0755
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable --now svnserve
```

> `-r /svn` 与 `config.json` 的 `repo_root` 保持一致：客户端检出 `svn://<ip>/<repo>` 即映射到 `/svn/<repo>`。

### 6.3 Web 应用（waitress + systemd）

```ini
# /etc/systemd/system/svnweb.service
[Unit]
Description=SVN Web - GitHub-style Subversion browser
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/svnweb/python
ExecStart=/opt/svnweb/python/venv/bin/waitress-serve --listen=0.0.0.0:37245 --threads=8 --channel-timeout=120 app:app
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable --now svnweb
curl -sI http://127.0.0.1:37245/healthz   # 期望 200
```

> `app:app` 表示从 `app.py` 导入名为 `app` 的 Flask 实例（WSGI 入口）。

### 6.4 HTTPS 反向代理（推荐）

参考 `deploy/nginx-svnweb.conf.example`：nginx 终止 443/TLS 并转发到 37245，  
同时在 `config.json` 设 `"behind_proxy": true`。

### 6.5 验证

```bash
curl -sI http://127.0.0.1:37245/ | head -1          # Web（302 → /login）
svn ls svn://<ip>/<repo> --username <账号>           # svn 协议
```

---

## 七、常见问题排查

| 现象                                                  | 原因                                  | 处理                                                        |
| --------------------------------------------------- | ----------------------------------- | --------------------------------------------------------- |
| 启动报找不到 `config.json`                                | 首次部署未初始化                            | 运行 `python3 init_config.py`                               |
| 登录提示账号或密码错误                                         | 密码哈希不是本机生成 / 忘记密码                   | 重新运行 `init_config.py` 覆盖生成（会自动备份旧配置）                      |
| 新建仓库报 “'passwd' and 'passwd' are the same file”     | 旧版 `pick_template_repo` 选到自己的历史 bug | 已修复；如仍在旧代码，按 changelog 2026-09-29 条目打补丁                   |
| svn 检出提示 `Authentication error: Password incorrect` | svnserve 的 `conf/passwd` 含多余空格等     | 检查 passwd 文件；或在用户管理页重新保存该账号                               |
| Web 能访问但 `svn://` 检不出来                              | svnserve 未启动 / `-r` 不对 / 3690 未放行   | `systemctl status svnserve`；`ss -lntp \| grep 3690`；防火墙放行 |
| 新改的密码不生效                                            | svn 客户端凭据缓存                         | `rm -rf ~/.subversion/auth`                               |
| 密码含 `*`、`(` 等特殊字符时检出失败                              | svn 客户端 / shell 解析                  | 用单引号包裹、或从文件读取，避免手打漏字符                                     |
| 搜索提示「结果可能不完整」                                       | 全历史代码扫描触发超时/超预算保护                   | 属如实提示；缓存命中后自动加速，也可调大 `search_scan_timeout`                |

---

## 七・补、与 VisualSVN Server（图形化管理器）的差异（Windows 用户必读）

Windows 上大家习惯用 **VisualSVN Server** 的图形管理器（Manager）来管仓库、  
用户、组和后台任务（jobs）。**本程序与它是两套完全独立、互不相通的管理体系**，  
如果你装了 VisualSVN Server，**不建议再使用本程序做管理**（代码浏览不受影响）。

### 两套体系的本质差异

|            | 图形化管理器（VisualSVN Server）                   | 本程序（网页管理）                              |
| ---------- | ------------------------------------------ | -------------------------------------- |
| 服务协议       | `https://`（内置 Apache，进程 vsvnhttpsvc.exe）   | `svn://`（svnserve）                     |
| 账号（users）  | **中央数据库**（Windows 注册表），Manager 里维护，全仓库共用一份 | 每仓库一份 `conf/passwd` 文本文件，**没有全局账号库**   |
| 组（groups）  | 中央数据库，可跨仓库引用                               | 每仓库 `conf/authz` 的 `[groups]` 节，仓库各自独立 |
| 路径授权       | 每仓库 `conf/VisualSVN.SvnAuthz.ini`（引用中央用户库） | 每仓库 `conf/authz` 的路径节                  |
| 后台任务（jobs） | Manager 内置（备份计划、钩子调度等）                     | 不涉及（本程序的备份 / 审计是独立实现）                  |
| 仓库发现       | Manager 自动登记                               | 只扫描 `repo_root` 目录下「含 `db` 子目录」的文件夹    |

**决定性规则：客户端用哪种 URL，就走哪套账号。**  
`svn://主机/仓库` → svnserve 读 `conf/passwd` + `conf/authz`（本程序管理的这套）；  
`https://主机/svn/仓库` → VisualSVN 读自己的注册表用户库，**本程序写的任何账号 /  
权限对它完全无效**。网页里浏览代码走本地文件直读（`file://`），不经过任何一种服务。

### Windows 上使用本程序的典型错位（实测会遇到）

1. **Manager 里建的仓库，网页上看不到** —— VisualSVN 的仓库根（如  
   `D:\ProgramData\Repositories`）和本程序 `config.json` 的 `repo_root` 不是  
   同一个目录。把 `repo_root` 指到 VisualSVN 的仓库根可以「看」，但管理仍然错位（见下）。
2. **网页里设的用户 / 权限矩阵 / 组，https 检出完全不生效** —— 它们写的是  
   `conf/passwd` / `conf/authz`，VisualSVN 根本不读这两个文件；反之 Manager 里建  
   的 users / groups 本程序也读写不到。
3. **「重启 SVN 服务」需要管理员权限** —— Windows 服务管理（`sc start/stop`）  
   要求本程序以管理员身份运行，否则报「拒绝访问」；且 VisualSVN 的服务名是自定义的，  
   需要靠二进制路径反查。
4. **在线编辑的 hook 不能直接用** —— 本程序生成的是 shell 脚本形式的 hook  
   （`SVNLOOK=/usr/bin/svnlook`），Windows 的 svnserve 只认 `.bat` / `.exe`，需手工改写。
5. **归档仓库的阻断保护弱化** —— Linux 上用 `chmod 000` 让 svnserve 无法读取  
   已归档仓库；Windows 的 chmod 只读写只读位，这层保护基本失效。

### 结论与建议

- **推荐**：在 **Linux** 服务器上部署本程序，仓库用网页新建 / 管理（或 `svnadmin
  create`），检出统一走 `svn://`——用户、组、权限矩阵全部在网页上管，文件即数据库，  
  所见即所得，没有任何第三套体系搅局。
- **不推荐**：在 Windows 上把本程序当管理工具用。如果机器上已有 VisualSVN Server，  
  请继续用它的 Manager 管账号和权限；本程序至多当作「网页代码浏览」使用。
- 本程序**刻意不读取、不修改** VisualSVN 的注册表数据库——那是 Windows 专属的  
  非标准存储，与本程序「跨平台、文件即真相」的设计原则相悖，未来也不会支持。

---

## 八、安全建议

- **永远不要复用示例配置**：`secret_key` 必须随机（`init_config.py` 已自动处理），密码必须自己设置。
- `config.json` 已在 `.gitignore`，请勿提交到公开仓库或复制给他人。
- 生产环境建议：nginx 443 + `behind_proxy: true`；服务器防火墙限制 37245 端口来源。
- `web_users` 的 viewer 角色只读，适合给只看不改的同事开账号。
- Web 站本身不含 SVN 密码明文；SVN 侧 passwd 为明文存储（svnserve 协议限制），请确保  
  `repo_root/*/conf/` 目录权限仅限运行用户可读（`chmod 600`）。

---

## 九、维护记录

历史维护记录见 **[changelog.md](changelog.md)**，按时间顺序记录每次功能变更与修复；  
后续新增条目请直接追加到该文件。

## 许可证

[MIT](LICENSE)
