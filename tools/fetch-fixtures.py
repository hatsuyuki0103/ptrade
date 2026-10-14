#!/usr/bin/env python3
"""从 GitHub Release 下载离线夹具到 ``data/fixture/``。

夹具不入 git（约 6.6 MB / 90 个 JSON，机器生成），这个脚本让你一条命令拿到它。

用法::

    python tools/fetch-fixtures.py                    # 下载最新 release
    python tools/fetch-fixtures.py --tag v1.1.0       # 指定版本
    python tools/fetch-fixtures.py --root <dir>       # 指定数据根
    python tools/fetch-fixtures.py --list             # 只看有哪些 asset

也支持自动回退到 ``tools/build_fixtures.py`` 现拉重建（需联网 + akshare）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile

REPO = "hatsuyuki0103/ptrade"
API = f"https://api.github.com/repos/{REPO}/releases"
ASSET_HINTS = ("fixtures", "fixture")


def _get(url: str, *, raw: bool = False, timeout: int = 60):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "ptrade-fetch-fixtures",
            "Accept": "application/octet-stream" if raw else "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read()


def list_releases() -> list[dict]:
    data = json.loads(_get(API))
    out = []
    for rel in data:
        assets = [
            {
                "name": a["name"],
                # `browser_download_url` 在 **objects.githubusercontent.com**（本机实测不可达）；
                # `api_url` 在 **api.github.com**（本机可达）。优先用后者。
                "url": a["browser_download_url"],
                "api_url": a.get("url", ""),
                "size": a["size"],
            }
            for a in rel.get("assets", [])
        ]
        out.append({"tag": rel["tag_name"], "name": rel.get("name"), "assets": assets})
    return out


def pick_asset(rel: dict) -> dict | None:
    for want in ASSET_HINTS:
        for a in rel["assets"]:
            if want in a["name"].lower():
                return a
    return None


def extract(archive: str, dest: str, scenario_dest: str | None = None) -> int:
    """把压缩包里的 JSON 解出来。返回写入数量。

    兼容两种布局：
    - ``fixtures/<name>.json`` + ``scenario/<name>.json``（新，推荐）
    - 全部平铺在一层（旧）

    未知路径按文件名落到 ``dest``（夹具目录）。
    """
    os.makedirs(dest, exist_ok=True)
    if scenario_dest:
        os.makedirs(scenario_dest, exist_ok=True)
    count = 0

    def target_for(name: str) -> str:
        parts = [p for p in name.replace("\\", "/").split("/") if p and p != "."]
        base = parts[-1]
        # 含 scenario/ 段的走剧本目录，其余走夹具目录
        if scenario_dest and any(p.lower() == "scenario" for p in parts[:-1]):
            return os.path.join(scenario_dest, base)
        return os.path.join(dest, base)

    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                if info.is_dir() or not info.filename.endswith(".json"):
                    continue
                with z.open(info) as src, open(target_for(info.filename), "wb") as dst:
                    shutil.copyfileobj(src, dst)
                count += 1
    elif tarfile.is_tarfile(archive):
        with tarfile.open(archive) as t:
            for member in t.getmembers():
                if not member.isfile() or not member.name.endswith(".json"):
                    continue
                src = t.extractfile(member)
                if src is None:
                    continue
                with src, open(target_for(member.name), "wb") as dst:
                    shutil.copyfileobj(src, dst)
                count += 1
    else:
        raise SystemExit(f"无法识别的压缩格式：{archive}")
    return count


def default_root() -> str:
    env = os.environ.get("PTRADE_HOME")
    if env and env.strip():
        return os.path.abspath(os.path.expanduser(env.strip()))
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return repo


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="下载 ptrade 离线夹具")
    ap.add_argument("--tag", default=None, help="release tag（默认取最新）")
    ap.add_argument("--root", default=None, help="数据根目录")
    ap.add_argument("--list", action="store_true", help="只列出 release / asset")
    ap.add_argument("--no-fallback", action="store_true", help="下载失败时不回退到 build_fixtures.py")
    args = ap.parse_args(argv)

    root = os.path.abspath(args.root) if args.root else default_root()
    dest = os.path.join(root, "data", "fixture")
    scen = os.path.join(root, "data", "scenario")
    print(f"数据根   : {root}")
    print(f"夹具目录 : {dest}")
    print(f"剧本目录 : {scen}")
    print()

    try:
        releases = list_releases()
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        print(f"⚠️ 取 release 列表失败：{exc}")
        releases = []

    if args.list:
        if not releases:
            print("（无 release 或网络不可用）")
            return 1
        for rel in releases:
            print(f"{rel['tag']}  {rel['name'] or ''}")
            for a in rel["assets"]:
                print(f"    {a['name']:<46} {a['size']:>12,} B")
        return 0

    target = None
    if releases:
        if args.tag:
            target = next((r for r in releases if r["tag"] == args.tag), None)
            if target is None:
                print(f"⚠️ 未找到 tag {args.tag}")
        else:
            target = releases[0]

    asset = pick_asset(target) if target else None
    if asset:
        print(f"下载 {target['tag']} / {asset['name']} ({asset['size']:,} B) …")
        blob = None
        # **优先走 API 资产端点**（`api.github.com`，本机可达）：
        # `browser_download_url` 在 `objects.githubusercontent.com`，本机实测**不可达**
        # （`WinError 10060`）→ 用户会看到「下载失败」然后被回退到「现拉重建」，
        # 那条路要求联网 + akshare，于是**最终什么也没拿到**。这是真实的可用性缺陷。
        for label, url, raw in (
            ("API", asset.get("api_url"), True),
            ("浏览器直链", asset["url"], True),
        ):
            if not url:
                continue
            try:
                blob = _get(url, raw=raw, timeout=300)
                if blob:
                    print(f"  （经 {label} 端点获取成功）")
                    break
            except (urllib.error.URLError, OSError) as exc:
                print(f"  ⚠️ {label} 端点失败：{str(exc)[:110]}")
        if not blob:
            print("⚠️ 所有下载端点都失败")
        if blob:
            with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(asset["name"])[1]) as fh:
                fh.write(blob)
                archive = fh.name
            try:
                n = extract(archive, dest, scen)
            finally:
                os.unlink(archive)
            n_scen = len([f for f in os.listdir(scen)]) if os.path.isdir(scen) else 0
            print(f"✅ 解出 {n} 个 JSON（夹具 {n - n_scen if n_scen else n} / 剧本 {n_scen}）")
            if n:
                return 0
            print("⚠️ 压缩包里没有 JSON")
    else:
        print("⚠️ 最新 release 里没有夹具 asset")

    if args.no_fallback:
        return 1
    builder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "build_fixtures.py")
    print()
    print(f"回退到现拉重建：{builder}")
    print("（需要联网 + akshare，约 15 分钟；会打印进度）")
    if not os.path.isfile(builder):
        print("⚠️ build_fixtures.py 不存在，无法回退")
        return 1
    env = {**os.environ, "PYTHONUTF8": "1", "PTRADE_HOME": root}
    return subprocess.call([sys.executable, builder, "--root", root], env=env)


if __name__ == "__main__":
    raise SystemExit(main())
