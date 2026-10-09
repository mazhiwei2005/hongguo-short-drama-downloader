#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
红果短剧 官网直连下载器 (hongguoduanju.com 通道)
=================================================
原理: 完全绕开字节 fqnovel API(那个按IP风控,下3集就断)。
      改走官网 hongguoduanju.com:
        详情页 /detail?series_id=X  -> 剧名 + vid_list(全部分集)
        播放页 /player/{sid}/{vid}   -> 内嵌明文 main_url(MP4直链,无需解密)
        urllib 流式下载, 支持断点续传(Range)
用法:
  python hgweb_dl.py --sid 7685283486432775192            # 下载整部
  python hgweb_dl.py --sid X --start 5 --end 10           # 只下第5~10集
  python hgweb_dl.py --url "https://hongguoduanju.com/detail?series_id=X"
  python hgweb_dl.py --sid X --retry                      # 只补下失败清单里的集
  python hgweb_dl.py --sid X --test                       # 只测第1集
  python hgweb_dl.py --sid X --delay 3                    # 集间固定延时秒
合规: 仅供个人离线学习, 请勿传播/商用。
"""
import argparse
import json
import os
import random
import re
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

SITE = "https://hongguoduanju.com"
UA_WEB = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
UA_APP = "com.phoenix.read/71532 (Linux; U; Android 9; SM-N9860; Build/PQ3A.190705.10241111;tt-ok/3.12.13.20)"
FAIL_LIST = "_失败清单.txt"
CHUNK = 1 << 20  # 1MB 分块


def fetch(url, ua=UA_WEB, ref=None, timeout=30):
    h = {"User-Agent": ua, "Accept-Language": "zh-CN,zh;q=0.9"}
    if ref:
        h["Referer"] = ref
    req = Request(url, headers=h)
    with urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def fetch_bytes(url, ua=UA_APP, ref=None, timeout=60, rng=None):
    h = {"User-Agent": ua}
    if ref:
        h["Referer"] = ref
    if rng:
        h["Range"] = rng
    req = Request(url, headers=h)
    return urlopen(req, timeout=timeout)


def norm_url(raw):
    """去掉 HTML 转义并剥掉 Range 参数(CDN 回整段)"""
    u = raw.replace("\\u002F", "/").replace("\\u0026", "&").replace("&amp;", "&")
    u = re.sub(r"([?&])Range=[^&]*", r"\1", u)
    return re.sub(r"[?&]$", "", u)


# ===== 名称搜索(官网分类页建索引 + 本地模糊匹配) =====
INDEX_CACHE = "_剧库索引.json"
INDEX_PAGES = [
    "",                     # 首页
    "/category/real-drama",  # 真人剧
    "/category/comic-drama", # 漫剧
    "/category/ai-drama",    # AI剧
    "/rank/hot-drama",       # 热榜
    "/rank/hot-real-drama",
    "/rank/hot-comic-drama",
    "/rank/hot-ai-drama",
]


def fetch_index(refresh=False):
    """抓官网各列表页, 收集 (剧名, series_id)。带缓存。"""
    cache = Path(INDEX_CACHE)
    if cache.exists() and not refresh:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass
    seen = {}
    for page in INDEX_PAGES:
        try:
            html = fetch(f"{SITE}{page}", ref=f"{SITE}/")
        except Exception:
            continue
        # recommendList 条目: "series_id":"...","series_name":"..." 相邻出现
        for m in re.finditer(r'"series_id":"(\d{15,})","series_name":"((?:[^"\\]|\\.)*)"', html):
            try:
                title = json.loads('"' + m.group(2) + '"')
            except Exception:
                continue
            seen.setdefault(m.group(1), {"title": title, "sid": m.group(1), "page": page})
    idx = list(seen.values())
    try:
        cache.write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass
    return idx


def search_series(keyword, refresh=False):
    """按名称模糊匹配, 返回 [(title, sid, page)]"""
    kw = keyword.strip().lower()
    idx = fetch_index(refresh)
    hits = []
    for it in idx:
        if kw in it["title"].lower():
            hits.append(it)
    # 按相关性: 前缀匹配优先
    hits.sort(key=lambda x: (0 if x["title"].lower().startswith(kw) else 1))
    return hits


# ===== 分享链接解析(分享落地页取全集列表 + 当前集直链) =====
def parse_share_url(share_url):
    """输入红果分享短链/落地页URL, 返回 (title, chapter_ids, play_url, used_chapter_id)
    play_url 是当前分享集的明文 MP4 直链(v3-share.qznovel.com)"""
    code_m = re.search(r"/s/([A-Za-z0-9_-]+)/?$", share_url.strip())
    if code_m:
        target = f"https://novelquickapp.com/s/{code_m.group(1)}/"
    else:
        target = share_url.strip()
    html = fetch(target, ua=UA_WEB, ref=f"{SITE}/")
    # 全集 chapter ids
    m = re.search(r'chapter_ids&quot;:\[(.*?)\]', html)
    ids = re.findall(r'&quot;(\d+)&quot;', m.group(1)) if m else []
    # 当前集直链 + 当前集 id
    mp = re.search(r'&quot;play_url&quot;:&quot;(.*?)&quot;', html)
    play_url = norm_url(mp.group(1)) if mp else None
    mu = re.search(r'&quot;used_chapter_id&quot;:&quot;(\d+)&quot;', html)
    used = mu.group(1) if mu else (ids[0] if ids else None)
    # 剧名
    mt = re.search(r'&quot;series_title&quot;:&quot;((?:[^&"]|&amp;)*)&quot;', html) or \
         re.search(r'&quot;series_name&quot;:&quot;((?:[^&"]|&amp;)*)&quot;', html)
    title = re.sub(r'&amp;', '&', mt.group(1)) if mt else "红果剧集"
    return title, ids, play_url, used


def download_direct(url, out_path, sid, vid, timeout=300):
    """分享直链下载(明文MP4, 无加密)"""
    return download(url, out_path, sid, vid, timeout)


def get_series(sid):
    """详情页 -> (剧名, vid列表)"""
    html = fetch(f"{SITE}/detail?series_id={sid}", ref=f"{SITE}/")
    m = re.search(r'"vid_list":(\[.*?\])', html)
    if not m:
        raise RuntimeError("详情页未找到 vid_list (series_id 可能无效或页面改版)")
    vids = json.loads(m.group(1))
    mt = re.search(r'"series_name":"([^"]+)"', html)
    title = json.loads('"' + mt.group(1) + '"') if mt else f"剧集_{sid}"
    return title, [str(v) for v in vids]


def get_play_url(sid, vid):
    """player 页 -> 明文 MP4 直链"""
    html = fetch(f"{SITE}/player/{sid}/{vid}", ref=f"{SITE}/detail?series_id={sid}")
    m = re.search(r'"main_url":"([^"]+)"', html)
    if not m:
        raise RuntimeError(f"player 页未找到 main_url (vid={vid})")
    return norm_url(m.group(1))


def download(url, out_path, sid, vid, timeout=300):
    """流式下载, 支持断点续传。返回 (ok, size_mb)"""
    dest = Path(out_path)
    tmp = dest.with_suffix(dest.suffix + ".part")
    exist = tmp.stat().st_size if tmp.exists() else 0
    rng = f"bytes={exist}-" if exist else None
    resp = fetch_bytes(url, rng=rng, ref=f"{SITE}/player/{sid}/{vid}")
    status = getattr(resp, "status", 200)
    if status == 200:
        # 不支持续传, 从头写
        exist = 0
        tmp = dest.with_suffix(dest.suffix + ".part")
        if tmp.exists():
            tmp.unlink()
    total = int(resp.headers.get("Content-Length") or 0) + exist
    mode = "ab" if exist else "wb"
    got = exist
    with open(tmp, mode) as f:
        while True:
            b = resp.read(CHUNK)
            if not b:
                break
            f.write(b)
            got += len(b)
            if total:
                pct = got * 100 // total
                mb = got / 1048576
                print(f"\r    已下载 {mb:.1f} MB / {total/1048576:.1f} MB ({pct}%)  ", end="")
            else:
                print(f"\r    已下载 {got/1048576:.1f} MB  ", end="")
    print()
    resp.close()
    if total and got < total:
        return False, got / 1048576
    tmp.rename(dest)
    return True, got / 1048576


def run(sid, start, end, delay, out_root, retry_only, test_only):
    out_root = Path(out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    title, vids = get_series(sid)
    total = len(vids)
    print(f"[OK] 剧名: {title}")
    print(f"[OK] 总集数: {total}")

    # 集序 -> vid: 官网 vid_list 按下标即集数顺序
    fail_path = out_root / FAIL_LIST
    failed = set()
    if retry_only:
        if not fail_path.exists():
            print("[X] 没有失败清单, 无可补下")
            return
        for line in fail_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line and "|" in line:
                failed.add(line.split("|")[0])

    eps = list(range(start, min(end, total) + 1))
    if test_only:
        eps = [start]
    print(f"[OK] 本次下载集数: {eps[0]}~{eps[-1]} (共{len(eps)}集)")

    safe = re.sub(r'[\\/:*?"<>|]', "_", title)
    series_dir = out_root / safe
    series_dir.mkdir(parents=True, exist_ok=True)
    ok_count = skip_count = fail_count = 0
    fresh_fails = []

    for i, ep in enumerate(eps, 1):
        vid = vids[ep - 1]
        out_file = series_dir / f"第{ep:02d}集.mp4"
        if out_file.exists() and out_file.stat().st_size > 1024 * 1024:
            print(f"[SKIP] 第{ep:02d}集 已存在 ({out_file.stat().st_size/1048576:.1f} MB)")
            skip_count += 1
            continue
        if retry_only and str(ep) not in failed:
            continue
        print(f"---- 第{ep:02d}/{total}集 ({i}/{len(eps)}) ----")
        try:
            url = get_play_url(sid, vid)
            ok, mb = download(url, out_file, sid, vid)
            if ok:
                print(f"[OK] 第{ep:02d}集 完成 ({mb:.1f} MB)")
                ok_count += 1
            else:
                print(f"[X] 第{ep:02d}集 下载不完整, 已留 .part 供续传")
                fail_count += 1
                fresh_fails.append((ep, vid, "不完整"))
        except Exception as e:
            print(f"[X] 第{ep:02d}集 失败: {e}")
            fail_count += 1
            fresh_fails.append((ep, vid, str(e)[:80]))
        if i < len(eps):
            time.sleep(delay if delay else random.uniform(1.5, 3.5))

    if fresh_fails:
        with open(fail_path, "w", encoding="utf-8") as f:
            for ep, vid, why in fresh_fails:
                f.write(f"{ep}|{vid}|{why}\n")
        print(f"\n[!] 失败 {fail_count} 集, 已写入 {fail_path}")
        print("    稍后重跑加 --retry 可只补下失败集")
    print(f"\n===== 完成: 成功{ok_count} 跳过{skip_count} 失败{fail_count} =====")
    print(f"输出目录: {series_dir}")


def main():
    ap = argparse.ArgumentParser(description="红果短剧官网直连下载器")
    ap.add_argument("--sid", help="剧集 series_id (详情页链接里的19位数字)")
    ap.add_argument("--url", help="官网详情页链接, 自动提取 series_id")
    ap.add_argument("--share", help="红果分享链接(短链或落地页), 下载分享的那一集")
    ap.add_argument("--name", help="按剧名搜索 (从官网列表页索引匹配)")
    ap.add_argument("--refresh", action="store_true", help="搜索前刷新剧库索引")
    ap.add_argument("--start", type=int, default=1, help="起始集 (默认1)")
    ap.add_argument("--end", type=int, default=99999, help="结束集 (默认全集)")
    ap.add_argument("--out", default=str(Path.cwd() / "downloads"), help="输出根目录")
    ap.add_argument("--delay", type=float, default=0, help="集间固定延时秒 (默认1.5~3.5随机)")
    ap.add_argument("--retry", action="store_true", help="只补下失败清单里的集")
    ap.add_argument("--test", action="store_true", help="只下载 start 集验证")
    args = ap.parse_args()

    sid = args.sid
    if args.url:
        m = re.search(r"series_id=(\d+)", args.url)
        if not m:
            print("[X] 链接里没找到 series_id")
            sys.exit(1)
        sid = m.group(1)
    if args.share:
        print("[OK] 正在解析分享链接...")
        title, ids, play_url, used = parse_share_url(args.share)
        if not play_url:
            print("[X] 分享页没拿到播放地址, 链接可能失效")
            sys.exit(1)
        total = len(ids)
        print(f"[OK] 剧名: {title}")
        print(f"[OK] 总集数: {total} (分享页仅提供当前一集直链)")
        safe = re.sub(r'[\\/:*?"<>|]', "_", title)
        out_root = Path(args.out)
        out_root.mkdir(parents=True, exist_ok=True)
        series_dir = out_root / safe
        series_dir.mkdir(parents=True, exist_ok=True)
        ep_num = (ids.index(used) + 1) if used in ids else 1
        out_file = series_dir / f"第{ep_num:02d}集.mp4"
        if out_file.exists() and out_file.stat().st_size > 1024 * 1024:
            print(f"[SKIP] 第{ep_num:02d}集 已存在 ({out_file.stat().st_size/1048576:.1f} MB)")
        else:
            print(f"---- 第{ep_num:02d}/{total}集 ----")
            ok, mb = download_direct(play_url, out_file, "share", used or "")
            print(f"[OK] 第{ep_num:02d}集 完成 ({mb:.1f} MB)" if ok else f"[X] 第{ep_num:02d}集 下载不完整")
        print(f"输出目录: {series_dir}")
        sys.exit(0)
    if args.name and not sid:
        print(f"[OK] 正在索引官网剧库(首次较慢)...")
        hits = search_series(args.name, refresh=args.refresh)
        if not hits:
            print(f"[X] 没找到名称含「{args.name}」的剧, 可试 --refresh 刷新索引或换个关键词")
            sys.exit(1)
        print(f"[OK] 找到 {len(hits)} 部, 请选择:")
        for i, h in enumerate(hits[:20], 1):
            print(f"  [{i}] {h['title']}")
        try:
            pick = input("  输入编号(回车默认1): ").strip()
        except EOFError:
            pick = ""
        n = int(pick) if pick.isdigit() and 1 <= int(pick) <= len(hits) else 1
        sid = hits[n - 1]["sid"]
        print(f"[OK] 已选择: {hits[n - 1]['title']} (sid={sid})")
    if not sid:
        print("[X] 请提供 --sid / --url / --name 之一")
        sys.exit(1)
    if not sid.isdigit() or len(sid) < 15:
        print(f"[X] series_id 格式不对: {sid} (应是19位数字)")
        sys.exit(1)

    try:
        run(sid, args.start, args.end, args.delay, args.out, args.retry, args.test)
    except Exception as e:
        print(f"[X] 程序出错: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
