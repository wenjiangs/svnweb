# 别再忍受命令行了：我把自家 SVN 仓库搬进了浏览器

> 一个用 Flask 写的 GitHub 风格 SVN 浏览与管理工具，单文件后端、开箱即用、MIT 开源。

## 写在前面

团队还在用 SVN 的朋友，一定对这种日常不陌生：

- 想看看某个文件的历史改了什么？`svn log`、`svn diff` 敲一串命令，翻屏翻到眼花；
- 新同事入职要开账号，得 SSH 上服务器，挨个仓库改 `passwd` 和 `authz`，改完还要祈祷没写错空格；
- 想快速浏览一下仓库里有什么，`svn ls` 一层一层地列，像在黑屋子里摸东西。

Git 生态有 GitHub/GitLab，体验拉满；而 SVN 这边，市面上好用的 Web 界面少之又少。于是我自己动手写了一个——**SVN Web**，一个 GitHub 风格的 SVN 网页浏览与管理工具，今天开源出来分享给大家。

![登录页](docs/images/01-login.png)

## 它是什么？

**SVN Web** 基于 Python 3 + Flask + Pygments 构建，有一个很克制的设计理念：**它不做底层存储，文件即真相**。

它不碰你的仓库数据格式，而是直接调用系统自带的 `svn` / `svnlook` / `svnadmin` 命令行去读写仓库目录。这意味着：

- 任何装了 Subversion 的 Linux 机器，部署本程序即可直接管理现有仓库，**零迁移成本**；
- 不引入私有数据库，账号权限就是仓库里标准的 `conf/passwd` + `conf/authz` 文件，随时可以脱离本工具手工维护；
- 对仓库本身零风险——网页崩了、程序卸了，仓库毫发无损。

![仓库首页](docs/images/02-index.png)

## 功能速览

### 代码浏览：像用 GitHub 一样用 SVN

目录树 + 面包屑导航，自动识别 trunk / branches / tags 标准布局并支持分支快速切换。单文件查看自带 **Pygments 语法高亮**，Markdown 文件（比如 README）直接在线渲染成页面，图片、PDF、音视频也能内联预览。

![目录树浏览](docs/images/03-tree.png)

![代码高亮](docs/images/04-blob-code.png)

还有平时最依赖的 **Blame 逐行追溯**——每一行代码是谁在哪个版本改的，一目了然，Code Review 时特别好用。

![Blame 追溯](docs/images/06-blame.png)

### 历史与差异：全历史搜索是杀手级功能

版本列表做成了真表格，勾选两个版本就能直接发起区间 diff 对比。

![版本列表](docs/images/08-versions.png)

![版本对比](docs/images/11-compare.png)

最想推荐的是**全历史搜索**，支持三档范围：

- 按**提交信息**搜——「那次改支付逻辑的提交到底是哪一笔？」
- 按**文件名**搜——「这个文件曾经放在哪个目录？」
- 按**代码变更**搜——「哪次提交出现过这段代码？」（连**已删除文件的历史内容**都能搜到）

扫全历史很慢？内置了 zlib 缓存加速，第二次搜索基本秒出。

![全历史搜索](docs/images/09-search.png)

### 管理：把 SSH 和 vim 都省了

这部分是我作为管理员最有成就感的模块：

- **用户 × 仓库权限矩阵**：一个表格管所有 SVN 账号，谁对哪个仓库是读写、只读、无权限，勾选保存即同步到所有仓库的 passwd/authz。新同事入职开账号，30 秒搞定；
- 仓库的归档 / 恢复 / 重命名 / 彻底删除，图形化操作；
- `svnadmin dump` **在线备份与下载**、hooks 在线编辑、仓库锁查看与强制解除；
- 分支 / 标签创建（底层 `svn copy`）、仓库 zip 打包下载。

![用户权限矩阵](docs/images/18-users-matrix.png)

![仓库管理](docs/images/13-repoadmin-overview.png)

![在线备份](docs/images/16-repoadmin-backup.png)

### 运维视角：全局动态 + 审计 + 系统状态

- 「最近提交」页聚合所有仓库的提交流水，团队今天干了什么一页看完；
- 管理操作**全量审计日志**，可按仓库、动作过滤，谁改了权限都有据可查；
- 系统状态页可看 svnserve 进程、磁盘用量，还能一键重启服务。

![最近提交](docs/images/24-recent.png)

![系统状态](docs/images/22-server.png)

## 安全上不含糊

自托管工具最怕安全摆烂，这个项目做了不少实事：

- 登录防爆破（连续失败自动锁定）、路径穿越防护、开放重定向加固；
- Web 密码 Werkzeug scrypt 哈希存储，支持 admin / viewer 双角色，viewer 只读；
- 媒体预览白名单刻意排除 html/js，杜绝同源 XSS；
- 支持nginx HTTPS 反代部署，内置示例配置。

## 部署有多简单？

一条命令：

```bash
wget -O install.sh https://raw.githubusercontent.com/wenjiangs/svnweb/main/deploy/install.sh
bash install.sh
```

脚本会先做纯只读环境检查（不满足就中止，不留任何垃圾文件），然后自动完成安装、注册 systemd 服务、健康自检。首次访问打开网页还有**安装向导**，环境自检报告、仓库根目录、管理员账号，填完即用，无需重启。

## 项目地址 & 许可

- 技术栈：Python 3.8+ / Flask 3.0 / Pygments / waitress
- 许可证：MIT，随便用、随便改

如果你所在团队还在用 SVN，又受够了命令行和改配置文件，不妨试一试。有问题欢迎提 Issue，觉得有用的话给个 Star 就是最好的鼓励！

---

**本文标签**：`SVN` `Flask` `Python` `开源项目` `自托管` `版本控制` `开发工具` `Subversion`
