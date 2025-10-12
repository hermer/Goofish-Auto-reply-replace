#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自动从 GitHub Raw 拉取 replace/ 目录中的改动文件，并在本地进行备份后替换。

默认远程仓库（可通过环境变量/参数覆盖）:
  https://raw.githubusercontent.com/OnlineMo/Goofish-Auto-reply-replace/refs/heads/main

拉取顺序:
  1) 远程读取 replace/filelist.txt
  2) 逐个下载对应的 replace/{path} 文件
  3) 在项目根目录按 {path} 写入（必要时创建目录）
  4) 若目标文件已存在，先备份到 backup_replace_YYYYmmdd_HHMMSS/{path}

使用:
  python3 apply_replace_from_github.py
  python3 apply_replace_from_github.py --base https://raw.githubusercontent.com/.../refs/heads/main
  REPLACE_BASE_URL=... python3 apply_replace_from_github.py

文件列表示例（replace/filelist.txt）:
  Start.py
  utils/cookiecloud.py
"""

import argparse
import datetime
import os
import sys
import shutil
import urllib.request
import urllib.error

DEFAULT_BASE = "https://raw.githubusercontent.com/OnlineMo/Goofish-Auto-reply-replace/refs/heads/main"
REMOTE_FILELIST_PATH = "replace/filelist.txt"


def log(msg: str):
    print(msg, flush=True)


def fetch_url(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (replace-fetch-script)"
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status} for {url}")
        return resp.read()


def ensure_parent_dir(path: str):
    parent = os.path.dirname(path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)


def timestamp() -> str:
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def parse_filelist(text: str):
    lines = text.splitlines()
    result = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # 统一使用正斜杠
        result.append(line.replace("\\", "/"))
    return result


def backup_if_exists(dest_path: str, backup_root: str):
    if os.path.exists(dest_path):
        backup_target = os.path.join(backup_root, dest_path)
        ensure_parent_dir(backup_target)
        shutil.copy2(dest_path, backup_target)
        log(f"[备份] {dest_path} -> {backup_target}")


def write_bytes(dest_path: str, content: bytes):
    ensure_parent_dir(dest_path)
    with open(dest_path, "wb") as f:
        f.write(content)


def main():
    parser = argparse.ArgumentParser(description="从 GitHub Raw 拉取 replace/ 文件并替换到本地项目（带自动备份）")
    parser.add_argument("-b", "--base", help="自定义 GitHub Raw 基地址（默认使用脚本内置）")
    parser.add_argument("-n", "--dry-run", action="store_true", help="仅预览将要替换的文件，不实际写入")
    parser.add_argument("-t", "--timeout", type=int, default=30, help="网络请求超时秒数，默认 30")
    args = parser.parse_args()

    base_url = (args.base or os.environ.get("REPLACE_BASE_URL") or DEFAULT_BASE).rstrip("/")
    filelist_url = f"{base_url}/{REMOTE_FILELIST_PATH}"

    log(f"==> 基地址: {base_url}")
    log(f"==> 读取文件列表: {filelist_url}")

    try:
        filelist_bytes = fetch_url(filelist_url, timeout=args.timeout)
    except Exception as e:
        log(f"[错误] 获取远程文件列表失败: {e}")
        # 回退：尝试读取本地 replace/filelist.txt
        local_filelist = os.path.join("replace", "filelist.txt")
        if os.path.exists(local_filelist):
            log(f"尝试使用本地 {local_filelist}")
            with open(local_filelist, "r", encoding="utf-8") as f:
                filelist_text = f.read()
        else:
            sys.exit(1)
    else:
        filelist_text = filelist_bytes.decode("utf-8", errors="ignore")

    paths = parse_filelist(filelist_text)
    if not paths:
        log("[警告] 文件列表为空")
        sys.exit(0)

    backup_root = f"backup_replace_{timestamp()}"
    if not args.dry_run:
        os.makedirs(backup_root, exist_ok=True)
        log(f"备份目录: {backup_root}")

    replaced = 0
    failed = 0

    for rel_path in paths:
        # 远程: replace/{rel_path}
        remote_url = f"{base_url}/replace/{rel_path}"
        # 本地: 项目根目录/{rel_path}
        dest_path = os.path.normpath(rel_path.replace("/", os.sep))

        log(f"\n[下载] {remote_url}")
        log(f"[目标] {dest_path}")

        if args.dry_run:
            continue

        try:
            content = fetch_url(remote_url, timeout=args.timeout)
        except Exception as e:
            log(f"[错误] 下载失败: {e}")
            failed += 1
            continue

        try:
            backup_if_exists(dest_path, backup_root)
            write_bytes(dest_path, content)
            log(f"[完成] 写入 {dest_path} ({len(content)} bytes)")
            replaced += 1
        except Exception as e:
            log(f"[错误] 写入失败: {e}")
            failed += 1

    log("\n========== 汇总 ==========")
    log(f"成功替换: {replaced}")
    log(f"失败数量: {failed}")
    if not args.dry_run:
        log(f"备份目录: {backup_root}")
    log("完成。")

    if failed > 0:
        sys.exit(2)


if __name__ == "__main__":
    main()