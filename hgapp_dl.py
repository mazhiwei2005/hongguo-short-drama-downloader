#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
红果短剧全集下载器 (App 通道版) — 突破网页端 3 集限制
=====================================================
原理: 逆向红果官方 App 接口 (X-Gorgon/X-Khronos 签名 + 随机设备身份),
      直连 App 视频后端拿全集播放地址, spade_a 密钥解密 CENC 加密流,
      输出标准可播放 MP4。网页端 3 集限制对它无效。

用法:
  python hgapp_dl.py --sid 7684151693751110681             # 下载整部
  python hgapp_dl.py --sid X --start 5 --end 10            # 只下第5~10集
  python hgapp_dl.py --name "曝光神话"                      # 名称搜索选剧
  python hgapp_dl.py --url "https://hongguoduanju.com/detail?series_id=X"
  python hgapp_dl.py --sid X --retry                       # 补下失败集
  python hgapp_dl.py --sid X --test                        # 只测第1集
依赖: pycryptodome (AES), 首次运行自动安装提示
"""
import argparse
import json
import os
import queue
import random
import re
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

# ---- 依赖自检: 缺 pycryptodome 就自动装到当前解释器 ----
try:
    import Crypto  # noqa: F401
except ImportError:
    import subprocess
    print("[!] 缺少 pycryptodome, 正在自动安装(清华源)...")
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "pycryptodome",
             "-i", "https://pypi.tuna.tsinghua.edu.cn/simple"],
            timeout=180)
    except Exception as e:
        print(f"[X] 自动安装失败: {e}")
        print("    请手动运行: 双击菜单选 [9] 安装依赖, 或命令行执行:")
        print(f"    {sys.executable} -m pip install pycryptodome")
        sys.exit(1)
    try:
        import Crypto  # noqa: F401
    except ImportError:
        print("[X] 安装后仍无法导入 Crypto, 请检查 Python 环境")
        sys.exit(1)

from HGDJ.client import HongguoClient, web_get_series_detail  # noqa: E402
from HGDJ.crypto import decrypt_episode  # noqa: E402

FAIL_LIST = "_失败清单_全集.txt"
CHUNK = 1 << 20
MAX_RETRY_NEW_CLIENT = 3  # 风控时换新设备身份重试次数
MAX_RETRY_DOWNLOAD = 3  # CDN 下载失败自动重试次数(网络抖动自愈, 2s/4s/8s 退避)


def get_series_vids(sid):
    """网页详情页拿全集 vid 列表 (不受 3 集限制)"""
    sd = web_get_series_detail(sid)
    vd = sd.get('video_data') or sd
    title = (vd.get('series_title') or vd.get('series_name') or f'剧集_{sid}')
    # 兼容两种结构: 内嵌 video_list 或 vid_list
    vids = []
    for key in ('vid_list', 'video_list'):
        vl = vd.get(key)
        if isinstance(vl, list):
            vids = [str(v.get('vid') if isinstance(v, dict) else v) for v in vl]
            break
        if isinstance(vl, dict):
            vids = [str(v.get('vid', k)) for k, v in vl.items()]
            break
    if not vids:
        # 兜底: 详情页 HTML 里的 vid_list JSON
        html = _web_html(f'/detail?series_id={sid}')
        m = re.search(r'"vid_list":(\[.*?\])', html)
        if m:
            vids = [str(v) for v in json.loads(m.group(1))]
    if not vids:
        raise RuntimeError('未能获取分集列表')
    return title, vids


def _web_html(path):
    from HGDJ.client import web_fetch
    return web_fetch(path)


def download_episode2(client, vid, out_file, timeout=180):
    """单集完整流程(带 spade_a): 取播放地址+密钥 -> 下载 -> 解密 -> 存盘"""
    tmp = out_file.with_suffix(out_file.suffix + '.part')
    if tmp.exists() and tmp.stat().st_size > 1024 * 1024:
        data = tmp.read_bytes()
        # 需要重新取密钥
        vm = client.get_video_model(vid)
        best = client.select_best_media(vm)
        spade_a = best['spade_a']
    else:
        vm = client.get_video_model(vid)
        best = client.select_best_media(vm)
        spade_a = best['spade_a']
        data = client.download_bytes(best['url'], referer=best['referer'], timeout=timeout)
        tmp.write_bytes(data)
    plain = decrypt_episode(data, spade_a)
    out_file.write_bytes(plain)
    tmp.unlink(missing_ok=True)
    return True, len(plain) / 1048576, best.get('quality', 0)


def share_to_series_id(share_url):
    """红果分享链接 -> 剧集 series_id (落地页 URL 里的 video_id 参数)
    支持: novelquickapp.com/s/短码 或 完整分享文案里的链接"""
    import urllib.request
    code_m = re.search(r"/s/([A-Za-z0-9_-]+)/?$", share_url.strip())
    if code_m:
        url = f"https://novelquickapp.com/s/{code_m.group(1)}/"
    else:
        url = share_url.strip()
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 Mobile Safari/604.1"})
    with urllib.request.urlopen(req, timeout=25) as r:
        final = r.geturl()
    m = re.search(r"video_id=(\d{15,})", final)
    if not m:
        raise RuntimeError("分享链接里没找到 series_id")
    return m.group(1)


# ---- 带进度条下载: pip 风格 [####----] 45% ----
def _progress(info, lock, buf_len, total_len):
    """更新进度条(单行刷新)。info: {'ep': 集数, 'pref': 前缀文本}"""
    try:
        if total_len:
            pct = buf_len * 100 // total_len
            bar_len = 30
            filled = pct * bar_len // 100
            bar = '#' * filled + '-' * (bar_len - filled)
            suffix = f"{buf_len/1048576:.1f}/{total_len/1048576:.1f}MB"
        else:
            pct = 0
            bar = '-' * 30
            suffix = f"{buf_len/1048576:.1f}MB"
        with lock:
            sys.stdout.write(f"\r{info['pref']} 第{info['ep']:02d}集 [{bar}] {pct:3d}% {suffix}   ")
            sys.stdout.flush()
    except Exception:
        pass


def download_with_progress(client, url, referer, ep, pref, lock, timeout=180):
    """流式下载 + pip风格进度条, 返回 bytes"""
    from HGDJ.client import WEB_USER_AGENT
    req = urllib.request.Request(url)
    req.add_header('User-Agent', WEB_USER_AGENT)
    req.add_header('Referer', referer)
    req.add_header('Connection', 'close')
    resp = urllib.request.urlopen(req, timeout=timeout)
    total = int(resp.headers.get('Content-Length') or 0)
    buf = bytearray()
    info = {'ep': ep, 'pref': pref}
    while True:
        b = resp.read(1 << 20)
        if not b:
            break
        buf.extend(b)
        _progress(info, lock, len(buf), total)
    resp.close()
    with lock:
        sys.stdout.write('\r' + ' ' * 70 + '\r')   # 清进度条行
        sys.stdout.flush()
    return bytes(buf)


def run(sid, start, end, delay, out_root, retry_only, test_only, parallel=3):
    out_root = Path(out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    title, vids = get_series_vids(sid)
    total = len(vids)
    print(f"[OK] 剧名: {title}")
    print(f"[OK] 总集数: {total} (App 通道, 不受 3 集限制)")

    fail_path = out_root / f"_失败清单_{sid}.txt"
    failed = set()
    if retry_only:
        if fail_path.exists():
            for line in fail_path.read_text(encoding='utf-8', errors='ignore').splitlines():
                line = line.strip()
                if line and '|' in line:
                    failed.add(line.split('|')[0])
        else:
            print("[X] 没有失败清单, 无可补下")
            return

    eps = list(range(start, min(end, total) + 1))
    if test_only:
        eps = [start]
    print(f"[OK] 本次下载: 第{eps[0]}~{eps[-1]}集 (共{len(eps)}集)")
    if parallel > 1:
        print(f"[OK] 模式: API串行预取({delay or 2}s间隔) + CDN并行下载 x{parallel}")

    safe = re.sub(r'[\\/:*?"<>|]', '_', title)
    series_dir = out_root / safe
    series_dir.mkdir(parents=True, exist_ok=True)

    client = HongguoClient(request_interval=delay if delay else 2.0)
    interval = delay if delay else 2.0
    ok_count = skip_count = fail_count = 0
    fresh_fails = []
    lock = threading.Lock()

    def handle_result(ep, vid, ok, mb, q, err):
        nonlocal ok_count, fail_count
        if ok:
            with lock:
                ok_count += 1
                cur = ok_count
            if cur % 20 == 0:
                print(f"  ... 已完成 {cur} 集")
        else:
            with lock:
                fail_count += 1
            print(f"[X] 第{ep:02d}集 失败: {err}")
            fresh_fails.append((ep, vid, err or '失败'))

    # ---- 预取线程: 严格串行调 API(间隔 interval), 不增加请求频率 ----
    jobs = queue.Queue(maxsize=parallel * 4)
    current_parallel = [parallel]   # 自适应并发: 失败自动降级

    def prefetch_worker():
        nonlocal skip_count
        for ep in eps:
            vid = vids[ep - 1]
            out_file = series_dir / f"第{ep:02d}集.mp4"
            if out_file.exists() and out_file.stat().st_size > 1024 * 1024:
                with lock:
                    skip_count += 1
                continue
            if retry_only and str(ep) not in failed:
                continue
            done = False
            for attempt in range(MAX_RETRY_NEW_CLIENT):
                try:
                    vm = client.get_video_model(vid)
                    best = client.select_best_media(vm)
                    jobs.put((ep, vid, best['url'], best['spade_a'], best.get('quality', 0)))
                    done = True
                    break
                except Exception as e:
                    if attempt < MAX_RETRY_NEW_CLIENT - 1:
                        time.sleep(2)
                        client = HongguoClient(request_interval=interval)
                    else:
                        jobs.put((ep, vid, None, None, 0, str(e)[:80]))
            if not done:
                jobs.put((ep, vid, None, None, 0))
            time.sleep(random.uniform(0.1, 0.3))
        for _ in range(parallel):
            jobs.put(None)

    # ---- 下载线程: 并行下 CDN 文件 + 解密; 失败自动降并发(3->2->1) ----
    lock_par = threading.Lock()
    lock_prog = threading.Lock()   # 进度条专用锁

    def maybe_degade(ok_flag):
        """下载失败 -> 降低并发(最低1), 保证稳定; 不自动恢复"""
        with lock_par:
            if not ok_flag and current_parallel[0] > 1:
                current_parallel[0] = max(1, current_parallel[0] - 1)
                print(f"  [!] 检测到失败, 并发降为 {current_parallel[0]} (保稳定)")

    def dl_worker(idx):
        while True:
            # 降级后, 编号超出的 worker 自动退出
            if idx >= current_parallel[0]:
                return
            try:
                item = jobs.get(timeout=1)
            except queue.Empty:
                continue
            if item is None:
                return
            ep, vid, url, spade_a, q = item[:5]
            err = item[5] if len(item) > 5 else None
            out_file = series_dir / f"第{ep:02d}集.mp4"
            if url is None:
                maybe_degade(False)
                handle_result(ep, vid, False, 0, 0, err or '预取失败')
                continue
            ok = False
            last_err = err or '下载失败'
            for attempt in range(1, MAX_RETRY_DOWNLOAD + 1):
                try:
                    data = download_with_progress(client, url, 'https://novel.snssdk.com/',
                                                  ep, f"[{idx}]", lock_prog, timeout=180)
                    plain = decrypt_episode(data, spade_a) if spade_a else data
                    out_file.write_bytes(plain)
                    handle_result(ep, vid, True, len(plain) / 1048576, q, None)
                    ok = True
                    break
                except Exception as e:
                    last_err = str(e)[:80]
                    if attempt < MAX_RETRY_DOWNLOAD:
                        wait = 2 ** attempt  # 2s / 4s / 8s 指数退避
                        print(f"  [!] 第{ep:02d}集 第{attempt}次失败({last_err}), {wait}s后重试...")
                        time.sleep(wait)
                        # URL 可能已失效(CDN 链接有时效), 重试前重新取一次播放地址
                        try:
                            vm = client.get_video_model(vid)
                            best = client.select_best_media(vm)
                            url = best['url']
                            spade_a = best['spade_a']
                        except Exception:
                            pass
            if not ok:
                maybe_degade(False)
                handle_result(ep, vid, False, 0, q, last_err)

    prefetch = threading.Thread(target=prefetch_worker, daemon=True)
    prefetch.start()
    pool = ThreadPoolExecutor(max_workers=parallel, thread_name_prefix='dl')
    futures = [pool.submit(dl_worker, i) for i in range(parallel)]
    prefetch.join()
    for f in futures:
        f.result()
    pool.shutdown()

    if fresh_fails:
        fail_path.write_text('\n'.join(f"{e}|{v}|{w}" for e, v, w in fresh_fails) + '\n', encoding='utf-8')
        print(f"\n[!] 失败 {fail_count} 集, 已写入 {fail_path}")
        print("    稍后重跑加 --retry 可只补下失败集")
    print(f"\n===== 完成: 成功{ok_count} 跳过{skip_count} 失败{fail_count} =====")
    print(f"输出目录: {series_dir}")


def main():
    ap = argparse.ArgumentParser(description="红果短剧全集下载器(App通道)")
    ap.add_argument("--sid", help="series_id (多个用逗号分隔, 依次下载)")
    ap.add_argument("--url", help="官网详情页链接 (多个用逗号分隔, 依次下载)")
    ap.add_argument("--name", help="按剧名搜索选剧")
    ap.add_argument("--share", help="红果分享链接(短链或落地页), 下载分享的那一集")
    ap.add_argument("--start", type=int, default=1)
    ap.add_argument("--end", type=int, default=99999)
    ap.add_argument("--out", default=str(Path.cwd() / "downloads"))
    ap.add_argument("--delay", type=float, default=0, help="集间延时秒(默认2)")
    ap.add_argument("--retry", action="store_true", help="只补下失败清单里的集")
    ap.add_argument("--test", action="store_true", help="只下载 start 集验证")
    ap.add_argument("links", nargs="*", help="直接拖入/传入的链接或sid(自动识别, 无需参数名)")
    args = ap.parse_args()

    # ---- 收集待下载剧集列表(支持多链接/多sid, 按输入顺序依次下载) ----
    sids = []

    def add_sid(s):
        s = str(s).strip()
        if s.isdigit() and len(s) >= 15:
            sids.append(s)

    # 1) 拖拽/裸参数里的链接或 sid (自动识别, 不用参数名)
    for raw in args.links:
        # 一个参数里可能含多个链接/换行/空格
        for part in re.split(r"[,，\s]+", raw):
            if not part:
                continue
            m = re.search(r"series_id=(\d{15,})", part)
            if m:
                add_sid(m.group(1))
            elif re.fullmatch(r"\d{15,}", part):
                add_sid(part)
            else:
                mm = re.search(r"/s/([A-Za-z0-9_-]+)/?", part)
                if mm:
                    try:
                        add_sid(share_to_series_id(part))
                    except Exception:
                        print(f"[X] 分享链接解析失败: {part[:60]}")
                else:
                    print(f"[X] 无法识别的输入: {part[:60]}")
    if args.sid:
        for part in re.split(r"[,，\s]+", args.sid):
            add_sid(part)
    if args.url:
        for part in re.split(r"[,，\s]+", args.url):
            if not part:
                continue
            m = re.search(r"series_id=(\d+)", part)
            if m:
                add_sid(m.group(1))
            else:
                print(f"[X] 链接里没找到 series_id: {part[:60]}")
    if args.share:
        try:
            sid = share_to_series_id(args.share)
            add_sid(sid)
            print(f"[OK] 分享链接解析成功: series_id={sid}")
        except Exception as e:
            print(f"[X] 分享链接解析失败: {e}")
            sys.exit(1)
    if args.name and not sids:
        # 复用 hgweb_dl 的索引搜索
        import importlib.util
        hgweb = importlib.util.spec_from_file_location("hgweb_dl", BASE_DIR / "hgweb_dl.py")
        hgweb_mod = importlib.util.module_from_spec(hgweb)
        try:
            hgweb.spec.loader.exec_module(hgweb_mod)
            print("[OK] 正在索引官网剧库...")
            hits = hgweb_mod.search_series(args.name)
            if not hits:
                print(f"[X] 没找到名称含「{args.name}」的剧")
                sys.exit(1)
            print(f"[OK] 找到 {len(hits)} 部:")
            for i, h in enumerate(hits[:20], 1):
                print(f"  [{i}] {h['title']}")
            try:
                pick = input("  输入编号(回车默认1): ").strip()
            except EOFError:
                pick = ""
            n = int(pick) if pick.isdigit() and 1 <= int(pick) <= len(hits) else 1
            add_sid(hits[n - 1]["sid"])
            print(f"[OK] 已选择: {hits[n - 1]['title']}")
        except Exception as e:
            print(f"[X] 搜索功能不可用: {e}")
            sys.exit(1)
    if not sids:
        print("[X] 请提供 --sid / --url / --name / --share 之一")
        sys.exit(1)

    total_shows = len(sids)
    print(f"[OK] 共 {total_shows} 部剧排队, 按输入顺序依次下载")
    for idx, sid in enumerate(sids, 1):
        print(f"\n{'='*50}\n[第{idx}/{total_shows}部] series_id={sid}\n{'='*50}")
        try:
            run(sid, args.start, args.end, args.delay, args.out, args.retry, args.test)
        except Exception as e:
            print(f"[X] 这部下载出错: {e}")
            continue

    # ---- 交互续下: 下载完可继续输入新链接/sid, 回车结束 ----
    try:
        is_tty = sys.stdin and sys.stdin.isatty()
    except Exception:
        is_tty = False
    while is_tty:
        try:
            more = input("\n[+] 粘贴新链接或sid继续下载(逗号分隔多个), 直接回车结束: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not more:
            break
        newsids = []
        for part in re.split(r"[,，\s]+", more):
            if not part:
                continue
            m = re.search(r"series_id=(\d{15,})|(\d{15,})", part)
            if m:
                s = m.group(1) or m.group(2)
                if s and len(s) >= 15:
                    newsids.append(s)
        if not newsids:
            print("[X] 没识别到有效的链接/sid")
            continue
        for sid in newsids:
            print(f"\n{'='*50}\n[续下] series_id={sid}\n{'='*50}")
            try:
                run(sid, args.start, args.end, args.delay, args.out, args.retry, args.test)
            except Exception as e:
                print(f"[X] 这部下载出错: {e}")
    print("\n===== 全部剧处理完毕 =====")


if __name__ == "__main__":
    main()
