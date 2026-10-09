#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
红果短剧 / 苹果CMS(conch 模板) 短剧 m3u8 批量下载器（增强版 v2）
====================================================================
【合规声明】本工具仅供个人学习分镜、离线研究使用。
  - 下载内容版权归原作者/平台所有，请勿传播、二创搬运、商用。
  - 批量抓取可能违反平台服务条款，请仅用于个人学习，控制频率。
  - 因版权/平台规则导致的问题，使用者自行负责。

【原理】
  播放页 /vodplay/{id}-1-{n}.html 内嵌若干段 JSON：
      var player_aaaa={"flag":"play","encrypt":0,...,"url":"https://xxx/index.m3u8",...}
      var player_bbbb={...}            # 备用播放源（不同 CDN）
  encrypt=0 表示未加密，url 即为可直接下载的 m3u8 直链。
  逐集请求播放页 -> 提取首个可用 m3u8 -> 用 yt-dlp/ffmpeg 合并为 mp4。

【重要：网络环境限制】
  红果的视频 CDN（HyperCDN）会拦截【数据中心 IP】（云服务器/本沙箱），
  返回 502；只有【家庭宽带】能正常下载分片。
  因此：本机可完整验证「播放页解析 + m3u8 直链提取」，但真实整集视频
  必须在你自己的家庭宽带电脑上运行本脚本才能下完。
  （可用 `python hongguo_downloader.py --selftest 20` 验证解析逻辑。）

【依赖】本地需安装 yt-dlp 或 ffmpeg（yt-dlp 优先）。
        若 ffmpeg 不在 PATH，可用 --ffmpeg 显式指定其 exe 路径；
        脚本也会自动探测若干常见 Windows 安装目录作为兜底。
        若 yt-dlp CLI 不在 PATH，脚本会尝试 `python -m yt_dlp`。

【用法】
  # 0) 随机剧解析自测（不下载，验证本机解析逻辑 + CDN 域名分布）
  python hongguo_downloader.py --selftest 20

  # 1) 先测 1 集，验证你本地网络能下（家庭宽带最关键的一步！）
  python hongguo_downloader.py --id 15297 --test

  # 2) 按剧名搜索并下载整部（搜索可能被 Cloudflare 拦，家庭宽带通常可过）
  python hongguo_downloader.py --name "宴律" --out "D:/短剧"
  python hongguo_downloader.py --name "宴律" --select        # 多结果时手动选

  # 3) 直接给 id / 链接（最稳）
  python hongguo_downloader.py --id 15297 --out "D:/短剧"
  python hongguo_downloader.py --url "https://www.hongguoapp.cn/vodplay/15297-1-1.html"

  # 4) 只下第 5~10 集 / 加延时防封 / 传 Cookie 应对登录态 CDN
  python hongguo_downloader.py --id 15297 --start 5 --end 10
  python hongguo_downloader.py --id 15297 --delay 3
  python hongguo_downloader.py --id 15297 --cookie "session=xxxxx"

  拿到 id：浏览器打开红果短剧网页版 -> 点开想下的那部 -> 地址栏
  /voddetail/15297.html 或 /vodplay/15297-1-1.html 里的数字即 id。
====================================================================
"""
import argparse
import json
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import quote

BASE = "https://www.hongguoapp.cn"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
REF = "https://www.hongguoapp.cn/"
SLEEP_BETWEEN = 1.5          # 默认集间延时（秒），防封
ID_RANGE = (14000, 15650)    # --selftest 随机 id 区间

# ffmpeg 解析：默认用 --ffmpeg 显式路径 -> 否则 PATH 探测 -> 否则常见 Windows 目录兜底
FFMPEG_BIN = None
COMMON_FFMPEG_PATHS = [
    r"D:\EVCapture\ffmpeg.exe",
    r"C:\ffmpeg\bin\ffmpeg.exe",
    r"C:\Users\Administrator\Desktop\UHUB-V20.0\ffmpeg.exe",
    r"E:\BaiduNetdiskDownload\UHUB-V20.0\ffmpeg.exe",
    r"D:\ffmpeg\bin\ffmpeg.exe",
]


def fetch(url, ref=REF, cookie=None, timeout=30):
    """请求页面，返回解码后的文本。4xx/5xx 抛 HTTPError，网络异常抛 URLError。"""
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": ref,
    }
    if cookie:
        headers["Cookie"] = cookie
    req = Request(url, headers=headers)
    with urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def extract_m3u8(html):
    """从播放页 HTML 提取 (m3u8_url, 剧名)。遍历所有 player_<hash> 播放源，
    返回第一个合法的 http(s) m3u8。拿不到返回 (None, '')。"""
    best_url, name = None, ""
    # 非贪婪逐块匹配：每个 player_<hash>={...} 各自成对象，取首个合法 m3u8。
    # （贪婪 .* 会把多个 player_ 块拼成一段非法 JSON，导致 json.loads 报 "Extra data"）
    for m in re.finditer(r'player_[a-z0-9]+=(\{.*?\})\s*</script>', html, re.S):
        try:
            obj = json.loads(m.group(1))
        except Exception:
            continue
        url = obj.get("url") or ""
        # 个别站点 url 为分号分隔的多个清晰度，取第一个 m3u8
        if ";" in url:
            url = url.split(";")[0]
        if isinstance(url, str) and re.search(r'https?://\S+\.m3u8', url):
            best_url = url
            name = (obj.get("vod_data", {}) or {}).get("vod_name") \
                or obj.get("title") or obj.get("vod_name") or ""
            break
    return best_url, name


def search(name, cookie=None):
    """按剧名搜索，返回 [(id, title), ...]。被拦截时返回空列表。"""
    url = f"{BASE}/vodsearch/-------------.html?wd={quote(name)}"
    try:
        html = fetch(url, cookie=cookie)
    except HTTPError as e:
        print(f"  [搜索] HTTP {e.code}，可能被 Cloudflare 拦截。建议改用 --id。")
        return []
    except URLError as e:
        print(f"  [搜索] 网络错误：{e}。建议改用 --id。")
        return []
    items = []
    for m in re.finditer(r'href="/voddetail/(\d+)\.html"[^>]*title="([^"]*)"', html):
        items.append((m.group(1), m.group(2).strip()))
    if not items:  # 退路：取标签内文本
        for m in re.finditer(r'href="/voddetail/(\d+)\.html"[^>]*>([^<]+)</a>', html):
            items.append((m.group(1), m.group(2).strip()))
    seen, uniq = set(), []
    for i, t in items:
        if i not in seen:
            seen.add(i)
            uniq.append((i, t or "未知剧名"))
    return uniq


def _yt_dlp_base(ref, cookie):
    """返回 yt-dlp 基础命令（自动选 CLI 或 `python -m yt_dlp`）。拿不到返回 None。"""
    if shutil.which("yt-dlp"):
        base = ["yt-dlp"]
    elif shutil.which("python") or shutil.which("python3"):
        py = shutil.which("python") or shutil.which("python3")
        try:                       # 确认 yt_dlp 模块可用
            subprocess.run([py, "-c", "import yt_dlp"], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            base = [py, "-m", "yt_dlp"]
        except Exception:
            return None
    else:
        return None
    cmd = base + ["--retries", "3", "--add-header", f"Referer:{ref}"]
    if cookie:
        cmd += ["--add-header", f"Cookie:{cookie}"]
    return cmd


def _resolve_ffmpeg(explicit=None):
    """返回可用的 ffmpeg 路径；优先 --ffmpeg 显式路径，其次 PATH，再次常见 Windows 目录。"""
    candidates = []
    if explicit:
        candidates.append(explicit)
    w = shutil.which("ffmpeg")
    if w:
        candidates.append(w)
    candidates += [p for p in COMMON_FFMPEG_PATHS if Path(p).exists()]
    for c in candidates:
        if Path(c).exists():
            return c
    return None


def download_m3u8(url, out_path, ref=REF, cookie=None, retries=2):
    """用 yt-dlp 或 ffmpeg 下载 m3u8 并合并为 mp4，失败重试。成功返回 True。"""
    global FFMPEG_BIN
    out_path = str(out_path)
    for attempt in range(1, retries + 1):
        ytl = _yt_dlp_base(ref, cookie)
        if ytl:
            cmd = ytl + ["-o", out_path, url]
            print(f"    [yt-dlp] 下载中（第{attempt}次）...")
            if subprocess.run(cmd).returncode == 0 and Path(out_path).exists():
                return True
        elif FFMPEG_BIN:
            hh = f"Referer: {ref}\r\n"
            if cookie:
                hh += f"Cookie: {cookie}\r\n"
            cmd = [FFMPEG_BIN, "-y", "-headers", hh, "-i", url,
                   "-c", "copy", "-bsf:a", "aac_adtstoasc", out_path]
            print(f"    [ffmpeg] 下载中（第{attempt}次）...")
            if subprocess.run(cmd).returncode == 0 and Path(out_path).exists():
                return True
        else:
            print("  [错误] 本地未找到 yt-dlp 或 ffmpeg，请先安装其一并加入 PATH，"
                  "或用 --ffmpeg 指定 ffmpeg.exe 路径。")
            return False
        print(f"    [重试] 第{attempt}次失败，2 秒后重试...")
        time.sleep(2)
    return False


def _collect_real_ids(limit):
    """从首页抓取真实在列的剧集 id（避免随机猜 id 大量 404）。失败返回空列表。"""
    try:
        html = fetch(BASE + "/")
    except Exception:
        return []
    uniq, seen = [], set()
    for i in re.findall(r'/voddetail/(\d+)\.html', html):
        if i not in seen:
            seen.add(i)
            uniq.append(i)
    return uniq[:limit]


def selftest(count=20, id_min=ID_RANGE[0], id_max=ID_RANGE[1], cookie=None):
    """抽取 count 个真实剧集 id，仅验证播放页解析 + m3u8 提取（不下载）。
    统计成功率、失败原因分布、出现的 CDN 域名。优先用首页真实 id，缺则随机补。"""
    real = _collect_real_ids(count)
    if len(real) < count:
        extra = random.sample(range(id_min, id_max + 1),
                              min(count - len(real), id_max - id_min + 1))
        real = real + extra
    ids = real[:count]
    print(f"=== 自测：{len(ids)} 个剧集（{len(real)} 个来自首页真实 id），仅验证解析 ===")
    ok = fail_blocked = fail_nourl = fail_err = 0
    hosts = {}
    for did in ids:
        url = f"{BASE}/vodplay/{did}-1-1.html"
        try:
            html = fetch(url, cookie=cookie)
        except HTTPError as e:
            if e.code == 404:
                fail_nourl += 1
            else:
                fail_blocked += 1
            continue
        except URLError:
            fail_err += 1
            continue
        m3u8, _ = extract_m3u8(html)
        if not m3u8:
            fail_nourl += 1
            continue
        ok += 1
        h = re.search(r'https?://([^/]+)/', m3u8)
        if h:
            hosts[h.group(1)] = hosts.get(h.group(1), 0) + 1
    total = len(ids)
    print(f"\n--- 解析自测结果 ---")
    print(f"样本数      : {total}")
    print(f"提取成功    : {ok}  ({ok*100//total}%)")
    print(f"无播放地址  : {fail_nourl}  (多为 404/剧集不存在)")
    print(f"被拦截/其它 : {fail_blocked + fail_err}")
    if hosts:
        print(f"CDN 域名分布 :")
        for h, c in sorted(hosts.items(), key=lambda x: -x[1]):
            print(f"    {h}: {c}")
    print("注：本机为数据中心 IP，真实视频分片会被 CDN 502 拦截；"
          "上述仅为解析层验证。整集下载请在家用宽带电脑运行。")
    return ok


def parse_id_from_url(url):
    m = re.search(r'/vod(?:play|detail)/(\d+)', url)
    return m.group(1) if m else None


def sanitize(name):
    return re.sub(r'[\\/:*?"<>|]', '_', name).strip() or "短剧"


def main():
    ap = argparse.ArgumentParser(description="红果短剧 m3u8 批量下载器（个人学习用途）")
    ap.add_argument("--id", help="剧集 id（详情页/播放页 URL 里的数字）")
    ap.add_argument("--url", help="播放页或详情页完整链接，自动提取 id")
    ap.add_argument("--name", help="按剧名搜索（可能被 Cloudflare 拦，家庭宽带通常可过）")
    ap.add_argument("--select", action="store_true", help="搜索出多结果时手动选择")
    ap.add_argument("--out", default="./downloads", help="输出根目录（默认 ./downloads）")
    ap.add_argument("--start", type=int, default=1, help="起始集（默认 1）")
    ap.add_argument("--end", type=int, default=9999, help="结束集（默认到最后一集）")
    ap.add_argument("--test", action="store_true", help="只下载第 start 集做验证")
    ap.add_argument("--selftest", type=int, metavar="N", nargs="?", const=20,
                    help="随机抽 N 个剧仅验证 m3u8 解析（不下载），默认 20")
    ap.add_argument("--delay", type=float, default=SLEEP_BETWEEN,
                    help=f"集间延时秒数，防封（默认 {SLEEP_BETWEEN}）")
    ap.add_argument("--cookie", help="可选 Cookie 字符串（个别 CDN 需要登录态）")
    ap.add_argument("--ffmpeg", help="显式指定 ffmpeg.exe 路径（当 ffmpeg 不在 PATH 时）")
    args = ap.parse_args()

    # 解析 ffmpeg 路径（--ffmpeg > PATH > 常见 Windows 目录），供下载兜底使用
    global FFMPEG_BIN
    FFMPEG_BIN = _resolve_ffmpeg(args.ffmpeg)
    if FFMPEG_BIN:
        print(f"[ffmpeg] 使用：{FFMPEG_BIN}")
    else:
        print("[ffmpeg] 未检测到（yt-dlp 可用时不影响下载；否则需 --ffmpeg 指定）")

    # --selftest 独立模式
    if args.selftest:
        selftest(args.selftest, cookie=args.cookie)
        return

    # 解析目标 id
    did = args.id or (parse_id_from_url(args.url) if args.url else None)
    if not did and args.name:
        results = search(args.name, cookie=args.cookie)
        if not results:
            print("搜索无结果或被拦截，请改用 --id / --url。")
            sys.exit(1)
        if args.select and len(results) > 1:
            print("搜索结果：")
            for i, (rid, title) in enumerate(results[:20], 1):
                print(f"  [{i}] {title}  (id={rid})")
            try:
                choice = int(input("请选择序号：")) - 1
                did = results[choice][0]
            except Exception:
                did = results[0][0]
        else:
            did = results[0][0]
            print(f"  >> 自动选择：{results[0][1]} (id={did})")

    if not did:
        print("请提供 --id / --url / --name 之一。例如：python hongguo_downloader.py --id 15297 --test")
        sys.exit(1)

    print(f"==> 目标剧 id = {did}，集范围 {args.start}~{args.end}"
          f"{'  [TEST：仅第'+str(args.start)+'集]' if args.test else ''}")

    drama_name = None
    n = args.start
    ok = fail = 0
    while n <= args.end:
        play_url = f"{BASE}/vodplay/{did}-1-{n}.html"
        try:
            html = fetch(play_url, cookie=args.cookie)
        except HTTPError as e:
            if e.code == 404:
                print(f"  [第{n}集] 404 -> 已到剧终。")
            elif e.code in (403, 502):
                print(f"  [第{n}集] HTTP {e.code} -> 疑似数据中心 IP 被 CDN 拦截，"
                      f"请改用家庭宽带电脑运行；或尝试 --cookie。停止。")
            else:
                print(f"  [第{n}集] HTTP {e.code}，停止。")
            break
        except URLError as e:
            print(f"  [第{n}集] 网络错误 {e}，停止。")
            break

        m3u8, name = extract_m3u8(html)
        if not m3u8:
            print(f"  [第{n}集] 播放页正常但无播放地址 -> 已到剧终。")
            break

        if drama_name is None:
            drama_name = sanitize(name) or f"短剧_{did}"
            print(f"  >> 剧名：{name or drama_name}  ->  目录：{Path(args.out)/drama_name}")

        out_dir = Path(args.out) / drama_name
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"第{str(n).zfill(2)}集.mp4"
        if out_file.exists() and out_file.stat().st_size > 0:
            print(f"  [第{n}集] 已存在，跳过。")
            ok += 1
            if args.test:
                break
            n += 1
            continue

        print(f"  [第{n}集] m3u8: {m3u8[:80]}...")
        if download_m3u8(m3u8, out_file, cookie=args.cookie):
            ok += 1
            print(f"  [第{n}集] 完成 -> {out_file}")
        else:
            fail += 1
            print(f"  [第{n}集] 下载失败（数据中心 IP 被 CDN 拦截？请换家庭宽带）。")

        if args.test:
            break
        n += 1
        if n <= args.end:
            time.sleep(args.delay)

    print(f"\n完成：成功 {ok} 集，失败 {fail} 集。")
    if fail:
        print("提示：若大量失败，多为 CDN 屏蔽 IP 或需登录态。用浏览器登录后导出 Cookie "
              "传入 --cookie；或调大 --delay；或换家庭宽带网络运行。")


if __name__ == "__main__":
    main()
