#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
svnWeb 初始化配置脚本（交互式）

首次部署时运行本脚本，自动生成 python/config.json：
  - 随机 secret_key（secrets.token_hex，128bit）
  - 登录密码的 Werkzeug 哈希（pbkdf2:sha256，明文不落盘）
  - 仓库根目录、检出地址前缀等基础项

用法：
    python3 init_config.py

仅依赖 werkzeug（Flask 的自带依赖）；无需安装其他包。
"""
import json
import os
import secrets
import sys

try:
    from werkzeug.security import generate_password_hash
except ImportError:
    print("缺少 werkzeug，请先安装依赖：")
    print("    pip install -r requirements.txt")
    sys.exit(1)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")


def ask(prompt, default=""):
    """带默认值的输入。"""
    tip = " [%s]" % default if default else ""
    val = input("%s%s: " % (prompt, tip)).strip()
    return val or default


def ask_password():
    """两次输入密码（回显关闭），校验一致性。"""
    import getpass
    while True:
        pw1 = getpass.getpass("设置 Web 登录密码（输入不回显）: ")
        if len(pw1) < 6:
            print("  密码至少 6 位，请重新输入。")
            continue
        pw2 = getpass.getpass("再输入一遍确认: ")
        if pw1 != pw2:
            print("  两次输入不一致，请重新输入。")
            continue
        return pw1


def main():
    print("=" * 56)
    print("svnWeb 初始化配置")
    print("=" * 56)

    if os.path.exists(CONFIG_PATH):
        ans = input("config.json 已存在，覆盖它吗？[y/N]: ").strip().lower()
        if ans != "y":
            print("已取消，未做任何改动。")
            return
        bak = CONFIG_PATH + ".bak"
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            with open(bak, "w", encoding="utf-8") as g:
                g.write(f.read())
        print("旧配置已备份为 config.json.bak")

    is_windows = (os.name == "nt")
    default_root = "D:/svn" if is_windows else "/svn"

    # 1) 仓库根目录
    repo_root = ask("SVN 仓库根目录（svnserve -r 指向的父目录，所有仓库都在其下）",
                    default_root)
    repo_root = repo_root.replace("\\", "/").rstrip("/")
    if not os.path.isdir(repo_root):
        ans = input("目录 %s 不存在，现在创建吗？[Y/n]: " % repo_root).strip().lower()
        if ans in ("", "y"):
            try:
                os.makedirs(repo_root)
                print("  已创建。")
            except OSError as e:
                print("  创建失败：%s；请手动创建后重跑本脚本。" % e)
                return
        else:
            print("  注意：目录不存在时本站将看不到任何仓库，可稍后修改 config.json 的 repo_root。")

    # 2) 登录账号与密码
    username = ask("Web 登录用户名", "admin")
    password = ask_password()

    # 3) 检出地址前缀
    print("对外展示的检出地址前缀（如 svn://192.168.1.10 或 https://svn.example.com/svn）。")
    print("留空则按浏览器访问的域名自动推断。")
    svn_base = ask("svn_base").rstrip("/")

    config = {
        "repo_root": repo_root,
        "username": username,
        "password_hash": generate_password_hash(password, method="pbkdf2:sha256"),
        "secret_key": secrets.token_hex(32),
        "cache_ttl": 20,
        "max_highlight_bytes": 524288,
        "max_preview_bytes": 5242880,
        "max_media_bytes": 33554432,
        "log_page_size": 60,
        "login_max_fail": 5,
        "login_lock_seconds": 300,
        "svn_base": svn_base,
        "behind_proxy": False,
        "web_users": {},
    }

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print()
    print("✔ 已生成 %s" % CONFIG_PATH)
    print("  - secret_key 已随机生成；密码已哈希存储，明文未写盘。")
    print("  - 后续如需多账号/角色（admin|viewer），可在 config.json 的 web_users 中添加，")
    print("    或登录后在「用户管理」页在线维护。")
    print("  - 配置项完整说明见 README.md「三、配置说明」。")
    print()
    print("下一步：启动服务（开发预览）")
    print("    python3 app.py            # 或 waitress-serve --listen=0.0.0.0:37245 app:app")
    print("默认端口与部署方式见 README.md「六、部署说明」。")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已取消。")
