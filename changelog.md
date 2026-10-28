# 更新日志（维护记录）

> 本文件由 `README.md` 原「九、本次维护记录」抽离而来，按时间顺序记录每次功能变更与修复。
> 最新条目在最后。

---

- 2026-09-29 修复首仓库创建时的 `SameFileError`，`create_repo` 传入 `exclude`，
  备份文件留存于部署目录 `app.py.bak.<时间戳>`。
- 完善 某测试仓库：从 仓库A 复制 `passwd`/`authz`，使用既有账号即可正常
  访问（`svn ls` 实测 RC=0）。

- 2026-10-12 **仓库详情页重构：三入口合并为「版本列表」**
  - 移除仓库头部的【跳转下拉框 / 历史 / 对比】三个入口，合并为单个「版本」按钮；
    `blob.html`、`blame.html` 的「历史」按钮同步改为「版本」。
  - 新增 `versions()`（`/<name>/versions/[/<path>]`）：分页 + 搜索 + 勾选框；
    `versions.html` 用事件委托 + 选中队列实现「最多同时选两个，勾第三个淘汰最早」。
  - 新增 `compare_revisions()`（`/<name>/compare/revisions/?a=&b=[&path=]`）与
    `svn_diff_range()`（`svn diff -r a:b`，区间累计差异）；`compare_revs.html` 展示结果。
  - 旧路由 `/commits/`、`/compare/` 改为 301 兼容跳转；删除 `commits.html`、`compare.html`。
  - 清理死代码 `_rev_options()` / `REV_OPTIONS_LIMIT`（顺带省掉每页一次 `svn log` 子进程）。
  - 部署：备份 `部署备份目录/`，8 个文件 MD5 校验一致，
    远端 16/16 模板 parse 通过，重启后 healthz 200；真实仓库 仓库A(426 rev) 验证通过。
  - ⚠️ 注意：工作区里的**登录页改版尚未部署**，故本次 `style.css` 采用「只并入 `.ver-*` 样式块」的
    最小作用域方式上传（整份覆盖会让线上旧 `login.html` 失去样式而破版）。

- 2026-10-12 **登录页左右分栏改版上线**：`login.html`（24→96 行）、`static/style.css`、`app.py`
  三件套一起部署；顺带加固登录回跳的开放重定向（拒绝 `//`、`/\` 开头）。
  备份 `部署备份目录/`。

- 2026-10-12 **版本列表改表格 + 新增「版本详情」页**
  - `versions.html` 由 `ul` 列表改为 `table.ver-table`（7 列：勾选/版本/提交信息/作者/时间/文件数/操作），
    **移除每行的变更文件清单**，只保留「N 个文件」数量；搜索/加载更多/对比条/勾选逻辑不变。
  - `commit.html` 升级为**版本详情页**：元信息 + `table.changed-files`（状态/路径/±行数，
    路径链到 `#diff-<i>` 锚点）+ 逐行 diff。`commit()` 视图新增 `changed` 列表（以 `svn log -v`
    的 paths 为准，join `parse_diff` 的增删行数与锚点）。
  - `style.css` 新增 `table.ver-table` / `table.changed-files` / `.visually-hidden`；
    清理 `.commit-list`、`.commit-item*`、`.ci-*`、`.ver-head` 等死规则（+66/−22）。
  - 部署：备份 `部署备份目录/`，4 个文件 MD5 一致，14/14 模板 parse 通过，
    healthz 200；5 个真实仓库 × 首/中/末版本详情页全部 200。
  - 注：仓库A 是扁平仓库（无 `trunk`/`branches`/`tags`），`/versions/<不存在路径>` 返回 404 属既有正确行为。

- 2026-10-12 **「变更文件」表新增「详情」→ 单文件变更详情页**
  - 新增 `commit_file()`（`/<name>/commit/<rev>/file/<path>`）与 `commit_file.html`：整页只展示该文件
    （或目录）在这次提交中的改动；删除的文件把「内容 / 历史」链接回退到 `rev-1`（按钮变「查看删除前的内容」）；
    二进制文件给提示条；目录则列出其下所有变更。
  - 新 helper `svn_diff_path()` 用 `svn diff -c <rev> URL/<path>@<rev>` **路径作用域**取 diff（快），
    `filter_diff_by_path()` 作兜底。作用域 diff 的 `Index:` 路径是「相对目标节点」的，须回填完整仓库路径。
  - `commit.html` 变更文件表加第 4 列「操作」；`style.css` 新增 `.changed-files .cf-op` 与 `.cf-*`。
  - 边界：首次导入的提交里会有 `path = /`（仓库根）的行，空路径的「详情」改指版本详情页，路径列显示 `/`。
  - 部署：备份 `部署备份目录/`，4 个文件 MD5 一致，15/15 模板 parse 通过，
    healthz 200；**4513 个「版本 × 变更路径」详情页全部 200**（跨 5 个仓库）。

- 2026-10-12 **搜索升级：从「只搜提交信息」到「信息 / 文件名 / 代码变更」三档全历史搜索**
  - `versions()` 新增 `?scope=`（可重复，取值 `msg`/`path`/`code`，默认三个全选；非法值忽略）；
    搜索走新的 `search_versions()`，**本地做不区分大小写的子串匹配**，覆盖整段历史、与分页互斥。
  - 新 helper：`svn_log_all()`（全历史 `log -v --xml`，供信息/文件名匹配）、
    `iter_log_diff()` + `iter_diff_blocks()`（流式切分 `svn log -v --diff`，以「分隔行 + 版本头」双重确认
    块边界，避免正文里的整行短横线或日志信息里的 `r1 | …` 造成误切）、
    `_code_hits()` / `_match_snippet()` / `_deleted_file_hits()` / `_norm_diff_path()`。
  - **关键坑**：`svn log --diff` 对**整文件删除**只给一行 `Index: x (deleted)`、不带正文，
    故删除的文件额外补一次 `svn cat -r rev-1` 把内容捞出来搜（否则「删掉的那段代码」永远搜不到）；
    属性变更段（`Property changes on:`）显式排除，不算代码。
  - 性能：扫描结果以 **zlib 压缩**存在内存（默认 TTL 1800s / 预算 64MB，LRU 淘汰，`threading.Lock` 记账），
    流式读写不整份载入；单次扫描墙钟上限 100s（小于 waitress 的 `--channel-timeout=120`），
    超时会如实标注「结果可能不完整」。仅搜信息/文件名时不触发扫描。
  - 模板/样式：`versions.html` 搜索框改为两行（关键字 + 范围勾选），结果行在提交信息下展示
    **命中片段**（`.ver-match` / `.vm-scope-*` / `.vm-kind-*` / `.vm-path` / `.vm-text` / `mark.vm-hit`）；
    `style.css` 新增 `.search-scopes` / `.ss-*` / `.vm-*` 一组规则（全部走 `:root` 变量）。
  - 本地验证：自造 16 版本仓库（增/改/删行、整文件删除、二进制、中文路径与内容、含 `@` 与空格文件名、
    72 个短横线的分隔行陷阱、日志信息里伪装成版本头的行、属性变更、>750KB 大文件分片解码），
    `test_client` **61/61 通过**。
  - 部署：备份 `部署备份目录/`，MD5 逐文件一致，远端模板 parse 通过，
    重启后 healthz 200。

- 2026-10-12 **搜索修正：删除文件漏报 + 结果不可复现（同一查询两次结果不同）**
  - 现象：线上 仓库A 搜 `post`，`total` 在 234/235 之间漂移；仓库A r415 一次删掉 6 个文件，
    含关键字的那个**搜不到**。用独立实现（逐条 `svn diff -c <rev>` + `svnlook cat -r rev-1`）
    交叉验证后定位到两处：
    1. `_deleted_file_hits()` 只补取**前 `SEARCH_SNIPPET_MAX`(4) 个**被删文件 —— 一次提交删掉
       6 个以上就会漏（仓库B 的 r25 一次删了 166 个，仓库A 累计 562 个被删文件）；
    2. 补取用的是**共享的 0.8s 时间预算** —— 大仓库光扫描就 8s，预算还没开始就过期；
       即便没过期，快慢也随机器负载浮动，同一个查询两次结果不同。
  - 修法：**去掉时间预算，改为确定性的条数上限** `_DELETED_LOOKUP_MAX`（默认 1200），
    一次提交里所有被删文件都参与补取，超上限才截断并如实标注；
    补取结果按 `(name, rev, path)` 进 `_delete_cache`（zlib，默认 24MB，LRU），
    重复搜索不再重读盘；二进制内容（含 `\x00`）直接按空处理，不占缓存。
  - 另一个坑：`svn log --xml` 里 `text-mods="false"` 的删除，连 `svn diff -c` 都不给正文，
    但内容确实存在于 `rev-1` —— 所以补取只能走 `svnlook cat`，不能指望 diff 能拿到。
    另外 `svn cat` 的 URL 必须带 `@<peg-rev>`，否则 `-r` 会被 peg 默认值（HEAD）盖掉。
  - `_deleted_file_hits()` 顺带改成并发读盘（默认 6 worker、分批处理控内存）。
    实测与串行差异不大（`svnlook` 起进程是 CPU-bound，受 GIL 限制，并发只有几个百分点），
    保留是因为多核下略优；读回的内容按批用完即弃，避免几百份文件同时压在内存里。
  - 本地验证：测试仓库加了 r17/r18（一次新增 6 个文件、再一次性全删，关键字故意放在第 5、6 个），
    `test_client` **70/70 通过**（新增 9 项：多文件删除不漏、结果可复现、删除内容缓存命中）。
  - 线上复验：5 个仓库逐个跑 `xcheck.py`（独立实现交叉比对 code / msg / path 三档命中集合）
    全部 `ALL MATCH`；仓库A `total` 稳定在 240、仓库C 128（补回了原先漏掉的删除命中）。
    性能（缓存命中）：仓库B 7.7s → 1.3s，仓库C 1.29s → 0.72s，仓库D 0.8s → 0.3s。
  - 剩余成本说明：首次搜索要为每个被删文件起一次 `svnlook`（约 7-10ms/个），
    仓库A 562 个文件约 8s，这是 SVN CLI 的固有开销；缓存命中后这部分降为 0。
  - 部署：备份 `部署备份目录/`（时间预算版）与
    `部署备份目录/`（条数上限版，上线用），MD5 逐文件一致，
    py_compile 与远端模板 parse 通过，重启后 healthz 200。

- 2026-10-12 **版本详情页去掉底部的「逐行差异」（列表页只做索引）**
  - `commit.html` 删掉底部整段「逐行差异」（标题 + `#diff-filter` 过滤框 + 内联脚本 + 全部
    `div.diff-file` 块 + 空态），页面只保留 hero 元信息 + `table.changed-files` + 返回按钮。
  - 因为不再有 diff 块，原先「文件路径 → `#diff-<i>` 锚点」的跳转失效：路径改为直接链到
    单文件变更详情页 `/<name>/commit/<rev>/file/<path>`（与第 4 列「详情」同目标），
    点路径看改动的习惯得以保留，且比内联长页更好定位。
  - `commit()` 视图同步清掉只服务于内联 diff 的死数据：`changed[].anchor`、`status_map`，
    以及传给模板的 `files`（`files` 仍作为局部变量用于 `diff_stats` 与逐文件 ± 行数 join）。
  - 兜底：`path` 为空（首次导入只报仓库根 `/`）的行没有单路径详情，改为给「浏览」→
    `/<name>?rev=<rev>`（该版本的文件树），避免出现指向本页的死链。
  - 未改动：`commit_file.html`（单文件详情）与 `compare_revs.html`（版本对比）仍渲染逐行差异；
    `style.css` 无需改动——`.diff-file` / `.diff-table` / `.d-*` / `.diff-filter` 都还被这两个页面使用。
  - 本地验证：新增 `test_commit_page.py` **48/48 通过**（7 个版本 × 无残留 diff 元素 / 清单仍在 /
    详情链可打开，含根目录兜底分支的定向渲染），既有 `test_search.py` **70/70** 无回归。
  - 线上复验：5 个真实仓库 × 首/中/末共 15 个版本详情页，全部 200、无残留 diff 元素、无死链。
  - 部署：备份 `部署备份目录/`，`app.py` 与 `templates/commit.html`
    MD5 逐文件一致，py_compile 与远端模板 parse 通过，重启后 healthz 200、服务 active。

- 2026-10-28 **新增首次访问安装向导（/install）**
  - `app.py` 配置加载重构为 `_apply_config(cfg)`：config.json 不存在时进入
    `INSTALL_MODE`，`before_request` 门禁把全站访问（含 /login、/healthz）引导到
    `/install`；向导页自带服务端环境自检（Python 版本、svn/svnadmin/svnlook/svnserve
    与版本号、3690 端口监听状态）。
  - 表单：仓库根目录（不存在自动创建并校验可写）、检出地址（主机名 + 可选端口，
    **支持自定义 SVN 端口**，两栏全空则按访问域名推断）、第一个管理员账号密码
    （pbkdf2 哈希 + 随机 secret_key）、可选 behind_proxy（ProxyFix）。
  - 提交后原子写入 config.json（tmp + os.replace）并**热更新全部配置立即生效**，
    无需重启；安装完成后 `/install` 永久跳转登录页（防他人重装覆盖）。
  - 新增 `templates/install.html`（复用登录页两栏视觉，左栏环境自检报告）与
    `.inst-*` 样式块；`RESERVED_REPO_NAMES` 补 `install`/`account`/`media`。
  - 沙盒端到端测试 25/25 通过（门禁跳转、非法提交不落盘、端口校验、自定义端口
    生成 svn_base、配置热更新、新管理员登录）；既有配置环境回归正常。

- 2026-10-28 **安装向导页改版（用户反馈两处）**
  - ① 不再复用登录页两栏视觉，改为独立的居中单列「分步卡片」设计：Logo 头部 +
    步骤胶囊导航（1 环境检查 › 2 基础配置 › 3 管理员账号）+ 三张编号卡片 +
    底部通栏提交按钮；环境自检改为双列网格（状态图标 + 名称 + 版本号）。
  - ② 修复「表单图标未显示」：旧版输入框没放 `.field-icon`；新版每个输入框
    （仓库根/主机名/端口/用户名/密码×2）都带图标，端口行用自定义 stroke 图标
    （globe/hash），密码框保留显隐切换。
  - style.css 的 `.inst-*` 旧块整体替换为 `.setup-*`；≤560px 时 env 网格折单列。
  - 沙盒回归：页面渲染（setup 结构/步骤胶囊/6 个自检条目/6 个输入框图标/无 login-panel）
    + 安装提交（自定义端口 8443 生成 svn_base）+ 新管理员登录，全部通过。

- 2026-10-28 **安装向导改成分步式（用户反馈）**
  - 三个步骤改为一次只显示一步：卡片挂 `data-panel`，步骤条从锚点改为可点击按钮
    （`data-goto`），点击可随意前后切换；底部新增导航区（上一步 / 下一步 /
    立即安装），提交按钮只在第 3 步显示。
  - 环境检查失败（svn/svnadmin/svnlook 任一缺失）时锁定在第 1 步：步骤条禁用、
    「下一步」禁用，第 1 步顶部显示红色横幅列出缺失项；svnserve 缺失仅按警告
    处理（黄色 + 专用图标），不阻断流程。
  - POST 校验失败后服务端传 `open_step`（仓库根/检出地址错误→打开步骤 2，
    账号密码错误→打开步骤 3），出错直接落在对应步骤。
  - 表单加 `novalidate`（去掉原生校验对隐藏字段的干扰），校验完全交给服务端；
    步骤条状态：is-active（当前）/ is-done（已完成）/ disabled（锁定）。
  - 沙盒端到端 17/17 通过（面板显隐、环境失败锁定、错误回跳、完整安装+登录）；
    Node 校验模板内联 JS 语法通过；真实环境（INSTALL_MODE）回归正常。

- 2026-10-28 **系统页 svnserve 状态支持 Windows（用户反馈）**
  - 问题：Windows 上 /server/ 页 svnserve 状态恒为「未知」——检测只认
    systemctl / pgrep，两者在 Windows 都不存在，直接落到默认值。
  - 修复：`_svnserve_status()` 增加 Windows 分支（tasklist /FI
    "IMAGENAME eq svnserve.exe" /FO CSV /NH，进程名 ASCII 不受 GBK 输出
    编码影响；无匹配时 rc 各版本不一致，按输出内容判定）；另加兜底分支——
    无任何进程检测手段时退回探测默认端口 3690。版本号检测本就跨平台。
  - 本机实测：svnserve 未启动→「未发现进程」；启动后→「运行中」+
    版本 1.14.2。磁盘/目录大小检测本就走 shutil.disk_usage/os.walk，无需改。
  - 已知限制不变：/server/ 的重启按钮仅 systemd 环境可用，Windows 下
    按钮降级并提示。

- 2026-10-28 **系统页「重启 svnserve」支持 Windows（用户反馈）**
  - 原来只有 systemctl 一条路，Windows 上点按钮报「当前环境没有 systemd」。
  - 新增 Windows 分支：先发现服务——`sc query svnserve`，查不到再用
    `Get-CimInstance Win32_Service` 按 PathName 含 svnserve 反查自定义服务名；
    找到服务则 `sc stop` → 轮询等 STOPPED（最多 10 秒）→ `sc start`，成功写审计。
  - 无服务但有 svnserve 进程（手动启动）：明确提示手动重启或用 sc create 注册
    服务；无服务无进程则提示「无需重启」。均不再笼统报 systemd 错误。
  - 顺手加固 `run_svn()`：except 补 PermissionError（命令被策略拦截/无权限时
    返回 127 而不是抛异常 500）。
  - 实测：服务反查查询语法已验证；沙盒 test_client 走完整 POST /server/restart
    链路正常、无 500。sc stop/start 正向流程需在注册了 svnserve 服务的
    Windows 机器上验证（本机未注册服务）。

- 2026-10-28 **修复重启 svnserve 的乱码与权限提示（用户反馈）**
  - 乱码根因：中文 Windows 的系统命令（sc/tasklist）控制台输出是 GBK，
    `run_svn()` 固定用 utf-8 解码 →「失败 5：拒绝访问」变乱码。
    修复：新增 `_decode_output()`——先 utf-8（svn 系列工具输出 utf-8），
    出现替换符 U+FFFD 时回退 GBK；实测 tasklist GBK 提示与模拟 sc 错误文本
    均正确解码，svn utf-8 输出不受影响。
  - 权限问题：sc start/stop 需要管理员权限，非管理员进程报 OpenService 失败 5。
    识别「拒绝访问/denied/ 5:」后在错误信息后追加明确指引（以管理员身份运行
    本程序，或在管理员命令行执行 sc start）。

- 2026-10-28 **识别 VisualSVN Server（用户反馈：服务里没有 svnserve）**
  - 澄清：VisualSVN Server 的 HTTP 服务（服务名 VisualSVNServer，二进制
    vsvnhttpsvc.exe）与 svnserve 是两种服务——前者走 http(s)://，不含
    svnserve 进程；用户本机 VisualSVN 在跑（昨天能访问的原因），svnserve
    服务其实存在但处于 Stopped。
  - `_svnserve_status()` Windows 分支扩展检测三种进程：svnserve /
    visualsvnserver / vsvnhttpsvc（tasklist 全量输出按内容匹配），
    命中 vsvnhttpsvc 时显示「运行中（VisualSVN HTTP）」。
  - `_win_svnserve_service()` 服务发现扩展：sc query 依次试 svnserve、
    VisualSVNServer（有 RUNNING 的优先）；WMI 反查条件改为 PathName 匹配
    'svnserve|vsvnhttpsvc|VisualSVNServer' 且按 Running 优先排序——重启按钮
    会重启「正在运行的 SVN 服务」。
  - 系统页文案改为「SVN 服务」/「重启 SVN 服务」。
  - 本机实测：状态=运行中（VisualSVN HTTP）；WMI 反查返回 VisualSVNServer。
    注意重启 VisualSVN / svnserve 服务同样需要管理员权限（error 5 提示已带指引）。

- 2026-10-28 **服务发现优先 VisualSVN（用户确认 svnserve 是手动装的，不作目标）**
  - sc query 顺序调整为 VisualSVNServer 在前：两者都停止时也优先认 VisualSVN，
    避免重启落在手动注册的 svnserve 服务上；有服务在运行时仍优先运行中的那个。
  - 用户如想清掉手动服务：管理员命令行执行 sc delete svnserve。

- 2026-10-28 **系统页支持重新设置仓库根目录（用户需求，带提示）**
  - 新增 POST /server/repo_root（admin）：校验绝对路径/非空/与当前不同
    （Windows 下忽略大小写比较），目录不存在自动创建并校验可写；
    tmp+os.replace 原子写 config.json → _apply_config 热更新（REPO_ROOT、
    ARCHIVE_DIR 跟随）→ purge_all_caches() 清空全部仓库缓存 → 写审计
    set_repo_root（old -> new）。
  - flash 提示区分两种结果：新根发现 N 个仓库（含原目录 M 个不再显示、
    文件未移动）；新根无仓库时用 error 级提示「未发现仓库，请确认路径」。
  - server.html 新增「仓库根目录」卡片：显示当前根 + 修改表单 + 三条提示
    （不移动文件 / 原目录仓库不再显示 / svnserve -r 需同步修改），
    提交前 confirm 二次确认。
  - 沙盒端到端：页面渲染、相对路径拒绝、同路径不变更、切换含 2 仓库目录
    （提示+热更新+首页可见）、切空目录警告+自动创建、审计记录，全部通过。
