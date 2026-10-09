# 仅供个人学习研究，不得用于商业用途
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import logging
import random
import struct
import time
import urllib.parse
import urllib.request
from typing import Any

LOG = logging.getLogger(__name__)

# ── 设备模拟参数 ──
APP_BASE_URL = "https://api5-normal-sinfonlineb.fqnovel.com"
APP_USER_AGENT = (
    "com.phoenix.read/73532 (Linux; U; Android 16; zh_CN; 25053RT47C; "
    "Build/BP2A.250605.031.A3; Cronet/TTNetVersion:04657795 2026-01-23 "
    "QuicVersion:c67e9834 2025-09-08)"
)
WEB_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
WEB_BASE_URL = "https://hongguoduanju.com"


def _new_device_id() -> str:
    return str(random.randint(1_000_000_000_000_000_000, 9_000_000_000_000_000_000))


# ── 位操作工具 ──

def _rotate_left_8(val: int, shift: int) -> int:
    val &= 0xFF
    shift %= 8
    return ((val << shift) | (val >> (8 - shift))) & 0xFF


def _reverse_8(val: int) -> int:
    val &= 0xFF
    result = 0
    for _ in range(8):
        result = (result << 1) | (val & 1)
        val >>= 1
    return result


# ── X-Gorgon / X-Khronos 签名 ──

_SIGN_KEY = bytes([
    0x44, 0xb9, 0xb9, 0xd9, 0xa4, 0xae, 0xf9, 0xfc,
    0xa4, 0x93, 0xaa, 0x75, 0x7c, 0xa3, 0xc2, 0xc4,
    0xa4, 0x96, 0x93, 0x8f,
])


def _sign_request(query_string: str, body: bytes | None, timestamp: int) -> dict[str, str]:
    query_hash = hashlib.md5(query_string.encode()).digest()
    payload = bytearray(20)
    payload[0:4] = query_hash[0:4]
    headers: dict[str, str] = {}
    if body is not None:
        body_hash = hashlib.md5(body).digest()
        payload[4:8] = body_hash[0:4]
        headers['X-SS-STUB'] = body_hash.hex().upper()
    payload[12:16] = bytes([0, 6, 11, 28])
    struct.pack_into('>I', payload, 16, timestamp & 0xFFFFFFFF)
    for i in range(20):
        payload[i] ^= _SIGN_KEY[i]
    for i in range(20):
        mixed = _rotate_left_8(payload[i], 4) ^ payload[(i + 1) % 20]
        payload[i] = _reverse_8(mixed) ^ 0xFF ^ 20
    signature = bytes([0x84, 0x04, 0x40, 0x1c, 0x00, 0x00]) + bytes(payload)
    headers['X-Khronos'] = str(timestamp)
    headers['X-Gorgon'] = signature.hex()
    headers['X-SS-Req-Ticket'] = str(int(time.time() * 1000))
    return headers


# ── 红果 App API 客户端 ──

class HongguoClient:
    def __init__(self, request_interval: float = 2.0):
        self.device_id = _new_device_id()
        self.install_id = _new_device_id()
        self.request_interval = request_interval
        self._last_request = 0.0
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _throttle(self) -> None:
        wait = self.request_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait + random.uniform(0, 0.25))

    def _base_query(self) -> dict[str, str]:
        return {
            'aid': '8662', 'app_name': 'novelread',
            'version_code': '73532', 'version_name': '7.3.5.32',
            'manifest_version_code': '73532', 'update_version_code': '73532',
            'channel': 'update_64', 'device_platform': 'android',
            'os': 'android', 'ssmix': 'a', 'device_type': '25053RT47C',
            'device_brand': 'Redmi', 'language': 'zh',
            'os_api': '36', 'os_version': '16',
            'resolution': '1280*2772', 'dpi': '520', 'ac': 'wifi',
            'device_id': self.device_id, 'iid': self.install_id,
        }

    def app_request(self, path: str, payload: dict | None = None,
                    timeout: int = 30) -> dict[str, Any]:
        """向红果 App 后端发起签名请求"""
        self._throttle()
        query = self._base_query()
        query['_rticket'] = str(int(time.time() * 1000))
        body = json.dumps(payload, ensure_ascii=False).encode() if payload else None
        query_string = urllib.parse.urlencode(query)
        now = int(time.time())
        sign_headers = _sign_request(query_string, body, now)
        url = APP_BASE_URL.rstrip('/') + path + '?' + query_string
        req = urllib.request.Request(url, method='POST', data=body)
        req.add_header('User-Agent', APP_USER_AGENT)
        req.add_header('Accept', 'application/json')
        req.add_header('Accept-Encoding', 'gzip')
        req.add_header('X-XS-From-Web', '0')
        req.add_header('Sdk-Version', '2')
        if body:
            req.add_header('Content-Type', 'application/json; charset=utf-8')
        for k, v in sign_headers.items():
            req.add_header(k, v)
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get('Content-Encoding') == 'gzip':
                    raw = gzip.decompress(raw)
                self._last_request = time.monotonic()
                return json.loads(raw) if raw else {}
        except Exception as e:
            self._last_request = time.monotonic()
            raise RuntimeError(f'红果 API 请求失败: {e}') from e

    # ── 业务方法 ──

    def get_video_model(self, video_id: str) -> dict:
        """获取单集的播放信息（CDN URL + 加密密钥）"""
        result = self.app_request('/novel/player/video_model/v1/', {
            'video_id': video_id,
            'content_type': 1,
            'biz_param': {'need_all_video_definition': True, 'video_platform': 3},
        })
        if result.get('code') != 0:
            raise RuntimeError(f"video_model 失败: {result.get('message', '未知错误')}")
        vm_raw = result.get('data', {}).get('video_model', '')
        if isinstance(vm_raw, str):
            return json.loads(vm_raw)
        return vm_raw

    def select_best_media(self, video_model: dict) -> dict:
        """从 video_model 中选择最佳画质"""
        video_list = video_model.get('video_list', {})
        if isinstance(video_list, dict):
            items = list(video_list.values())
        elif isinstance(video_list, list):
            items = video_list
        else:
            items = []
        best = None
        best_score = -1
        for item in items:
            meta = item.get('video_meta', {})
            codec = meta.get('codec_type', '').lower()
            if codec == 'bytevc2':
                continue
            # 解析 URL
            raw_url = item.get('main_url', '')
            try:
                media_url = base64.b64decode(raw_url).decode()
            except Exception:
                media_url = raw_url
            if not media_url.startswith('http'):
                continue
            # 画质评分
            height = 0
            try:
                height = int(meta.get('vheight', 0) or 0)
            except (ValueError, TypeError):
                pass
            score = height * 10
            if codec in ('h264', 'avc1'):
                score += 1
            # 密钥
            spade_a = item.get('encrypt_info', {}).get('spade_a', '')
            if score > best_score:
                best_score = score
                best = {
                    'url': media_url,
                    'quality': height,
                    'codec': codec,
                    'spade_a': spade_a,
                    'referer': 'https://novel.snssdk.com/',
                }
        if not best:
            raise RuntimeError('未找到可用的媒体变体')
        return best

    def download_bytes(self, url: str, referer: str = 'https://novel.snssdk.com/',
                       timeout: int = 120, max_retries: int = 3) -> bytes:
        """下载 URL 对应的完整文件，带重试机制"""
        last_err: Exception | None = None
        for attempt in range(max_retries):
            try:
                req = urllib.request.Request(url)
                req.add_header('User-Agent', WEB_USER_AGENT)
                req.add_header('Referer', referer)
                req.add_header('Connection', 'close')
                with self._opener.open(req, timeout=timeout) as resp:
                    return resp.read()
            except Exception as e:
                last_err = e
                LOG.warning('下载 CDN 分片异常 (第 %d/%d 次重试): %s', attempt + 1, max_retries, e)
                time.sleep(0.5 * (attempt + 1))
        raise last_err or RuntimeError('下载 CDN 文件失败')

    def download_range(self, url: str, start: int, end: int,
                       referer: str = 'https://novel.snssdk.com/',
                       timeout: int = 30) -> bytes:
        """Range 下载"""
        req = urllib.request.Request(url)
        req.add_header('User-Agent', WEB_USER_AGENT)
        req.add_header('Referer', referer)
        req.add_header('Range', f'bytes={start}-{end}')
        with self._opener.open(req, timeout=timeout) as resp:
            return resp.read()


# ── 网页端数据获取 ──

def _normalize_drama(raw_dict: dict) -> dict:
    """统一规范化短剧元数据字段"""
    vd = raw_dict.get('video_data', {})
    src = vd if isinstance(vd, dict) and vd else raw_dict
    sid = str(src.get('series_id') or raw_dict.get('keyword') or raw_dict.get('series_id') or '').strip()
    name = (raw_dict.get('name') or src.get('series_title') or src.get('series_name') or '未知').strip()
    cover = src.get('series_cover') or raw_dict.get('series_cover') or ''
    eps = src.get('episode_cnt') or 0
    eps_text = src.get('episode_right_text') or (f'全{eps}集' if eps else '')
    tags = src.get('tags') or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(',') if t.strip()]
    intro = src.get('series_intro') or ''
    return {
        'series_id': sid,
        'series_name': name,
        'series_cover': cover,
        'episode_cnt': eps,
        'episode_right_text': eps_text,
        'tags': tags,
        'series_intro': intro,
    }


def web_fetch(path: str, timeout: int = 15) -> str:
    """从红果网页端获取 HTML，自动处理 URL 编码"""
    parsed = urllib.parse.urlsplit(path)
    safe_path = urllib.parse.quote(parsed.path)
    if parsed.query:
        safe_path += '?' + urllib.parse.quote(parsed.query, safe='=&')
    url = WEB_BASE_URL.rstrip('/') + safe_path
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers={'User-Agent': WEB_USER_AGENT})
    with opener.open(req, timeout=timeout) as resp:
        raw = resp.read()
        encoding = resp.headers.get_content_charset() or 'utf-8'
        return raw.decode(encoding, errors='replace')


def web_get_series_detail(series_id: str) -> dict:
    """从网页端获取剧集详情（含 vid_list）"""
    import re
    html = web_fetch(f'/detail?series_id={series_id}')
    m = re.search(r'_ROUTER_DATA\s*=\s*(\{.*?\});', html, re.DOTALL)
    if not m:
        raise RuntimeError('无法解析网页数据')
    data = json.loads(m.group(1))
    detail_page = data.get('loaderData', {}).get('detail_page', {})
    sd = detail_page.get('seriesDetail', {})
    if not sd:
        raise RuntimeError('未获取到剧集详情')
    return sd


def web_get_homepage() -> list[dict]:
    """从网页端获取首页推荐短剧"""
    import re
    raw_html = web_fetch('/')
    m = re.search(r'_ROUTER_DATA\s*=\s*(\{.*?\});', raw_html, re.DOTALL)
    results = []
    seen = set()
    if m:
        try:
            data = json.loads(m.group(1))
            page = data.get('loaderData', {}).get('page', {})
            # 1. 顶部 Banner 推荐
            for b in page.get('bannerList', []):
                item = _normalize_drama(b)
                if item['series_id'] and item['series_id'] not in seen:
                    seen.add(item['series_id'])
                    results.append(item)
            # 2. 首页各版块（热播短剧、真人剧、漫剧、AI剧）
            for sec in page.get('homeSections', []):
                for v in sec.get('video_list', []):
                    item = _normalize_drama(v)
                    if item['series_id'] and item['series_id'] not in seen:
                        seen.add(item['series_id'])
                        results.append(item)
        except Exception as e:
            LOG.warning('解析首页推荐异常: %s', e)

    return results


def web_get_category(category: str, page: int = 1) -> dict:
    """从网页端获取分类列表与分页信息"""
    import re
    path_map = {
        'real-drama': '/category/real-drama',
        'comic-drama': '/category/comic-drama',
        'ai-drama': '/category/ai-drama',
        'comic': '/category/comic',
    }
    base_path = path_map.get(category, f'/category/{category}')
    path = f'{base_path}?page={page}' if page > 1 else base_path
    raw_html = web_fetch(path)
    m = re.search(r'_ROUTER_DATA\s*=\s*(\{.*?\});', raw_html, re.DOTALL)
    results = []
    pagination = {'total': 0, 'pageNum': page, 'pageSize': 24, 'totalPages': 1}
    seen = set()
    if m:
        try:
            data = json.loads(m.group(1))
            loader = data.get('loaderData', {})
            cat_page = loader.get('category_$', {})
            if 'pagination' in cat_page and isinstance(cat_page['pagination'], dict):
                pagination = cat_page['pagination']
            items = cat_page.get('recommendList', [])
            for raw_item in items:
                item = _normalize_drama(raw_item)
                if item['series_id'] and item['series_id'] not in seen:
                    seen.add(item['series_id'])
                    results.append(item)
        except Exception as e:
            LOG.warning('解析分类列表异常: %s', e)

    return {'items': results, 'pagination': pagination}


def web_search(keyword: str) -> list[dict]:
    """通过网页端 /search/{keyword} 搜索短剧"""
    import re
    raw_html = web_fetch(f'/search/{keyword}')
    m = re.search(r'_ROUTER_DATA\s*=\s*(\{.*?\});', raw_html, re.DOTALL)
    results = []
    seen = set()
    if m:
        try:
            data = json.loads(m.group(1))
            loader = data.get('loaderData', {})
            for k, page in loader.items():
                if 'search' in k and isinstance(page, dict):
                    sl = page.get('searchList', [])
                    for raw_item in sl:
                        item = _normalize_drama(raw_item)
                        if item['series_id'] and item['series_id'] not in seen:
                            seen.add(item['series_id'])
                            results.append(item)
        except Exception as e:
            LOG.warning('解析搜索结果异常: %s', e)

    return results
