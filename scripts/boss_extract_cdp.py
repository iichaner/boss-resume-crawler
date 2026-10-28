#!/usr/bin/env python3
"""
Boss直聘职位爬取 - 纯 CDP 模式（反爬优化版）

核心设计：
- 每次操作新建 WebSocket 连接，用完即关（防超时）
- 每提取 1 条立即追加写入 CSV（防数据丢失）
- 详情页提取后立即关闭 tab（防 tab 堆积）
- 随机化所有等待时间（反爬）
- 指数退避重试（容错）
"""

import json
import csv
import time
import subprocess
import os
import re
import glob
import random
import sys
import argparse
from datetime import datetime

# T6: 导入进度追踪器
def _pipeline_scripts_dir():
    """定位 boss-pipeline/scripts（BOSS_PIPELINE_SKILLS_ROOT > 同级兄弟目录）。

    不写死任何家目录路径：四个 skill 装在一起即可自动找到，换安装位置也不用改代码。
    """
    cands = []
    env = os.environ.get('BOSS_PIPELINE_SKILLS_ROOT')
    if env:
        cands.append(os.path.join(os.path.expanduser(env), 'boss-pipeline', 'scripts'))
    here = os.path.dirname(os.path.abspath(__file__))
    while True:
        cands.append(os.path.join(here, 'boss-pipeline', 'scripts'))
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    for c in cands:
        if os.path.isfile(os.path.join(c, 'config_paths.py')):
            return c
    return None


def _ensure_pipeline_path():
    """把 boss-pipeline/scripts 加入 sys.path（幂等）。返回目录或 None。"""
    d = _pipeline_scripts_dir()
    if d and d not in sys.path:
        sys.path.insert(0, d)
    return d


if _ensure_pipeline_path() is None:
    print("❌ 找不到 boss-pipeline/scripts：请把四个 skill 装在同一个目录下，"
          "或设置 BOSS_PIPELINE_SKILLS_ROOT 为该目录")
    exit(1)

from progress_tracker import ProgressTracker

try:
    import websocket
except ImportError:
    print("❌ 缺少 websocket-client → pip3 install websocket-client")
    exit(1)

# ============================================================
# CDP 连接加固（修复「滚动了但 0 条 / 不爬取」）
# 根因：① macOS 上 localhost 同时解析 ::1(IPv6) 与 127.0.0.1，Chromium 仅听
#       IPv4；ws://localhost 连 ::1 会被拒 → cdp_eval 静默失败 → 计数变 0。
#       ② 列表 tab 在运行中可能因 Boss 风控/重载而更换 page id，启动时缓存的
#       webSocketDebuggerUrl 会失效，此后所有 eval 永久失败（日志表现为 0/0）。
# 对策：强制 127.0.0.1；curl 直连 127.0.0.1；连接失败时自动重新解析 tab；
#       把本机地址加入 NO_PROXY，避免 ClashX 等代理拦截 localhost。
os.environ['NO_PROXY'] = '127.0.0.1,localhost,' + os.environ.get('NO_PROXY', '')
os.environ['no_proxy'] = os.environ['NO_PROXY']

CDP_HOST = '127.0.0.1'   # 替代 localhost，curl 直连 CDP

def _fix_ws(url):
    """把 ws URL 中的 localhost / [::1] 统一改成 127.0.0.1（强制 IPv4）。"""
    if not url:
        return url
    return url.replace('[::1]', '127.0.0.1').replace('localhost', '127.0.0.1')

# ============================================================
# PUA 薪资解码
# ============================================================
PUA_MAP = {
    0xe031: '0', 0xe032: '1', 0xe033: '2', 0xe034: '3', 0xe035: '4',
    0xe036: '5', 0xe037: '6', 0xe038: '7', 0xe039: '8', 0xe03a: '9',
}

def decode_pua(text):
    if not text:
        return text
    return ''.join(PUA_MAP.get(ord(c), c) for c in text)


# ============================================================
# CDP 工具函数（每次独立连接）
# ============================================================
# 目标列表页 URL（由 --url 传入；空则回退到启发式 tab 选择）
PREFERRED_LIST_URL = ''


def _url_key(url):
    """归一化 URL 用于 tab 匹配：按 (域名, 路径, 排序后的 query) 比较，忽略 hash。"""
    try:
        from urllib.parse import urlsplit, parse_qsl, urlencode
        s = urlsplit(url)
        q = sorted(parse_qsl(s.query))
        return (s.netloc, s.path, urlencode(q))
    except Exception:
        return url


def get_list_page_ws(preferred_url=None):
    """获取列表页 WebSocket URL。

    匹配优先级：
    1) 目标 URL 精确匹配（--url 传入，归一化比较）
    2) 带查询参数的搜索页（含 query= 或 position=）
    3) 任何 zhipin jobs 页（通用页，登录后可能停留在此）
    """
    preferred_url = preferred_url or PREFERRED_LIST_URL
    try:
        r = subprocess.run(
            ['curl', '-s', f'http://{CDP_HOST}:9222/json'],
            capture_output=True, text=True, timeout=10
        )
        pages = json.loads(r.stdout)

        # 优先级0：目标 URL 精确匹配（修复「指定列表页与实际爬取页不一致」）
        if preferred_url:
            want = _url_key(preferred_url)
            for p in pages:
                url = p.get('url', '')
                if p.get('type') == 'page' and url and _url_key(url) == want:
                    print(f"✅ 已匹配目标列表页: {url[:110]}")
                    return _fix_ws(p['webSocketDebuggerUrl'])
            # 未精确命中：对 jobs 页做 query 模糊匹配（query 值相同即视为同页）
            want_q = None
            try:
                from urllib.parse import urlsplit, parse_qsl
                want_q = dict(parse_qsl(urlsplit(preferred_url).query)).get('query')
            except Exception:
                pass
            if want_q:
                for p in pages:
                    url = p.get('url', '')
                    if p.get('type') == 'page' and 'zhipin' in url and 'jobs' in url:
                        try:
                            from urllib.parse import urlsplit, parse_qsl
                            pq = dict(parse_qsl(urlsplit(url).query)).get('query')
                            if pq == want_q:
                                print(f"✅ 已按搜索词匹配列表页: {url[:110]}")
                                return _fix_ws(p['webSocketDebuggerUrl'])
                        except Exception:
                            continue

        # 优先级1：带查询参数的搜索页（含 query= 或 position=）
        for p in pages:
            url = p.get('url', '')
            if p.get('type') == 'page' and 'zhipin' in url and 'jobs' in url:
                if 'query=' in url or 'position=' in url:
                    return _fix_ws(p['webSocketDebuggerUrl'])

        # 优先级2：任何 zhipin jobs 页（通用页，登录后可能停留在此）
        for p in pages:
            url = p.get('url', '')
            if p.get('type') == 'page' and 'zhipin' in url and 'jobs' in url:
                print(f"⚠️ 未找到搜索页，使用通用页: {url[:80]}")
                return _fix_ws(p['webSocketDebuggerUrl'])
    except Exception:
        pass
    return None


def check_login(ws_url):
    """检查 Boss 直聘是否已登录。返回 True/False"""
    try:
        # 检查页面标题是否含登录/扫码，或页面是否有登录按钮
        title = cdp_eval("document.title", ws_url, timeout=10)
        if title and ('登录' in title or '扫码' in title):
            return False
        # 检查页面是否有姓名（登录后才有）
        has_name = cdp_eval(
            "document.querySelector('.user-nav-name, .user-name, [class*=username]') !== null",
            ws_url, timeout=10
        )
        if has_name == 'true':
            return True
        # 检查是否有登录按钮
        login_btn = cdp_eval(
            "document.querySelector('.login-btn, [class*=login], [class*=scan]') !== null",
            ws_url, timeout=10
        )
        if login_btn == 'true':
            return False
        return True  # 默认假设已登录，避免误判
    except Exception:
        return True  # 异常时默认假设已登录


def cdp_eval(js_code, ws_url=None, timeout=15):
    """
    通过 CDP 执行 JavaScript，每次新建连接。
    返回执行结果，失败返回 None。

    加固：连接失败（如列表 tab 因风控/重载更换 page id 导致旧 ws URL 失效）
    时自动重新解析 tab 并重试一次；并在日志打印真实异常，便于开发者模式排查。
    """
    if not ws_url:
        ws_url = get_list_page_ws()
    if not ws_url:
        return None

    last_err = None
    for attempt in range(2):
        ws = None
        try:
            ws = websocket.create_connection(ws_url, timeout=timeout)
            ws.send(json.dumps({
                "id": 1,
                "method": "Runtime.evaluate",
                "params": {
                    "expression": js_code,
                    "returnByValue": True,
                    "timeout": timeout * 1000
                }
            }))
            response = ws.recv()
            result = json.loads(response)
            if 'result' in result and 'result' in result['result']:
                return result['result']['result'].get('value', '')
            return None
        except Exception as e:
            last_err = e
            # tab 可能已更换 id，重新解析一次再试
            fresh = get_list_page_ws()
            if fresh and fresh != ws_url:
                ws_url = fresh
                continue
            if attempt == 0:
                print(f"  ⚠️ cdp_eval 连接异常（将重试）: {e}")
        finally:
            if ws:
                try:
                    ws.close()
                except Exception:
                    pass
    if last_err:
        print(f"  ⚠️ cdp_eval 最终失败: {last_err}")
    return None


def cdp_scroll(ws_url, delta_y=800, x=None, y=None):
    """
    模拟人类滚轮滚动（独立连接）：一次滚动拆成随机 3~6 小段，
    每段间隔随机 40~120ms，让「滚动时长 / 节奏」每次都不一样（ii 定，2026-10-27）。

    诊断结论（2026-08-05）：Boss 直聘列表页右侧 `.job-detail-container`
    是固定定位的职位详情预览面板，自带内部滚动（scrollTop 0~约281），
    x 坐标落在其范围内（约 428~1180）时，滚轮会先被它的内部滚动"吃掉"，
    只有填满后才冒泡到 window，导致主列表滚动被稀释、效率低。
    左侧 `.job-list-container`（约 x: 32~424，overflow:visible）区域滚轮
    会直接冒泡给 window/document，是真正应该命中的落点。

    因此坐标固定打在左侧安全区，不再随机落到右侧详情面板。
    """
    if x is None:
        x = random.randint(150, 380)
    if y is None:
        y = random.randint(300, 600)
    if not ws_url:
        ws_url = get_list_page_ws()
    if not ws_url:
        return
    ws = None
    last_err = None
    for attempt in range(2):
        try:
            ws = websocket.create_connection(ws_url, timeout=10)
            ws.settimeout(2)   # 防 recv 长时间阻塞（2026-10-27：30轮×多段×10s 会卡死整轮）
            steps = random.randint(3, 6)
            base = max(1, int(delta_y / steps))
            for s in range(steps):
                d = base if s < steps - 1 else delta_y - base * (steps - 1)
                ws.send(json.dumps({
                    "id": 1 + s,
                    "method": "Input.dispatchMouseEvent",
                    "params": {
                        "type": "mouseWheel",
                        "x": x,
                        "y": y,
                        "deltaX": 0,
                        "deltaY": d
                    }
                }))
                try:
                    ws.recv()
                except Exception:
                    pass
                if s < steps - 1:
                    time.sleep(random.uniform(0.04, 0.12))
            return
        except Exception as e:
            last_err = e
            # tab 可能已更换 id，重新解析一次再试
            fresh = get_list_page_ws()
            if fresh and fresh != ws_url:
                ws_url = fresh
                continue
            if attempt == 0:
                print(f"  ⚠️ cdp_scroll 连接异常（将重试）: {e}")
        finally:
            if ws:
                try:
                    ws.close()
                except Exception:
                    pass
    if last_err:
        print(f"  ⚠️ cdp_scroll 最终失败: {last_err}")


def open_new_tab(url):
    """CDP 打开新 tab，返回 page_id。

    修复「爬取详情页抢夺电脑焦点」：HTTP /json/new 新建 tab 会把浏览器窗口
    激活到前台，每爬一条就抢一次焦点。改用 CDP Target.createTarget +
    background:true 在后台创建 tab（不激活窗口），用户可继续做其他事。
    """
    # 方式一：CDP Target.createTarget 后台创建（首选，不抢焦点）
    try:
        import websocket as _ws
        base_ws = get_list_page_ws()
        if not base_ws:
            # 任取一个 page tab 作为控制连接
            r = subprocess.run(
                ['curl', '-s', f'http://{CDP_HOST}:9222/json'],
                capture_output=True, text=True, timeout=10
            )
            pages = json.loads(r.stdout)
            for p in pages:
                if p.get('type') == 'page' and p.get('webSocketDebuggerUrl'):
                    base_ws = _fix_ws(p['webSocketDebuggerUrl'])
                    break
        if base_ws:
            ws = _ws.create_connection(base_ws, timeout=10)
            ws.send(json.dumps({'id': 1, 'method': 'Target.createTarget',
                                'params': {'url': url, 'background': True}}))
            resp = json.loads(ws.recv())
            ws.close()
            tid = resp.get('result', {}).get('targetId')
            if tid:
                return tid
    except Exception:
        pass
    # 方式二：HTTP /json/new（回退，可能激活窗口但不阻塞功能）
    try:
        req_url = f'http://{CDP_HOST}:9222/json/new?{url}'
        r = subprocess.run(
            ['curl', '-s', '-X', 'PUT', req_url],
            capture_output=True, text=True, timeout=15
        )
        data = json.loads(r.stdout)
        return data.get('id')
    except Exception:
        return None


def close_tab(page_id):
    """关闭指定 tab"""
    try:
        subprocess.run(
            ['curl', '-s', f'http://{CDP_HOST}:9222/json/close/{page_id}'],
            capture_output=True, timeout=5
        )
    except Exception:
        pass


def get_tab_ws(page_id):
    """获取指定 tab 的 WebSocket URL"""
    try:
        r = subprocess.run(
            ['curl', '-s', f'http://{CDP_HOST}:9222/json'],
            capture_output=True, text=True, timeout=10
        )
        pages = json.loads(r.stdout)
        for p in pages:
            if p.get('id') == page_id:
                return _fix_ws(p.get('webSocketDebuggerUrl'))
    except Exception:
        pass
    return None


# ============================================================
# Phase 1: 列表页滚动
# ============================================================
def scroll_list_page(max_scrolls=30, ws_url=None, scroll_wait=0.2, scroll_delta=(1500, 2500)):
    """
    滚动列表页加载更多职位（长滚动，ii 定 2026-10-27）。

    参数：最大 30 次；每次滚动后随机等 0.1~0.5 秒（基准 0.2s，模拟人类节奏）。
    滚动长度每次在 scroll_delta 范围内随机（长度不一）；
    滚动时长由 cdp_scroll 拆段实现（时长不一）。
    每次滚动都检查并打印 count/scrollHeight/scrollTop（便于诊断）。
    连续 5 次数量不变且确认到底才停止。
    到底后先回滚一段再滚到底（触底反弹），逼出懒加载 observer。

    诊断结论（2026-08-05）：右侧 `.job-detail-container` 是固定定位的职位详情
    预览面板，自带内部滚动（scrollTop 0~约281）。滚轮落点若在其范围内
    （约 x: 428~1180），会先被它的内部滚动“吃掉”，只有填满后才冒泡到
    window，导致主列表滚动被稀释、效率低。落点固定改到左侧列表列安全区
    （约 x: 150~380，见 cdp_scroll），滚轮直接冒泡给 window/document。
    """
    print(f"\n[Phase 1] 长滚动加载（最多 {max_scrolls} 次，间隔 0.1~0.5s 随机）...")

    check_js = """
    (function(){
        var cards = document.querySelectorAll('.job-card-wrap').length;
        var scrollH = document.documentElement.scrollHeight;
        var clientH = document.documentElement.clientHeight;
        var scrollT = window.pageYOffset || document.documentElement.scrollTop;
        var atBottom = (scrollT + clientH) >= (scrollH - 100);
        return JSON.stringify({count: cards, atBottom: atBottom, scrollHeight: scrollH, scrollTop: scrollT});
    })()
    """

    prev_count = 0
    same_count = 0
    bounced = False

    for i in range(max_scrolls):
        cdp_scroll(ws_url, random.randint(int(scroll_delta[0]), int(scroll_delta[1])))
        # 时间间隔：基准 0.2s，随机波动 0.1~0.5s（ii 定）
        time.sleep(random.uniform(0.1, 0.5))

        result = cdp_eval(check_js, ws_url)
        try:
            info = json.loads(result) if result else {}
        except Exception:
            info = {}
        count = info.get('count', 0)
        at_bottom = info.get('atBottom', False)
        scroll_h = info.get('scrollHeight', 0)
        scroll_t = info.get('scrollTop', 0)

        print(f"  第 {i+1} 次滚动: {count} 条职位 | 位置: {scroll_t}/{scroll_h} | 到底: {at_bottom}")

        if count == prev_count:
            same_count += 1
            if same_count >= 5 and at_bottom:
                if not bounced:
                    print("  连续多次数量不变且已到底，触底反弹重试...")
                    cdp_eval("window.scrollBy(0, -300)", ws_url)
                    time.sleep(1.5)
                    cdp_eval("window.scrollTo(0, document.documentElement.scrollHeight)", ws_url)
                    time.sleep(5)
                    bounced = True
                    same_count = 0
                    continue
                print(f"  反弹后仍连续 {same_count} 次数量不变且已到底，停止滚动")
                break
        else:
            same_count = 0
            bounced = False
        prev_count = count

    # 最终统计
    final_count = cdp_eval("document.querySelectorAll('.job-card-wrap').length", ws_url)
    final_count = int(final_count) if final_count else 0
    print(f"  滚动完成，最终职位数: {final_count}")
    return final_count


# ============================================================
# Phase 2: 列表页提取
# ============================================================
EXTRACT_LIST_JS = """
(function(){
    var items = document.querySelectorAll('.job-card-wrap');
    var jobs = [];
    items.forEach(function(item){
        var titleEl = item.querySelector('.job-name');
        var salaryEl = item.querySelector('.job-salary');
        var companyEl = item.querySelector('.boss-name');
        var locationEl = item.querySelector('.company-location');
        var tagEls = item.querySelectorAll('.tag-list li');
        var href = titleEl ? titleEl.getAttribute('href') : '';
        var jobIdMatch = href ? href.match(/job_detail\\/(.+?)\\.html/) : null;
        var tags = [];
        tagEls.forEach(function(t){ tags.push(t.textContent.trim()); });
        if(titleEl){
            jobs.push({
                title: titleEl.textContent.trim(),
                salary: salaryEl ? salaryEl.textContent.trim() : '',
                company: companyEl ? companyEl.textContent.trim() : '',
                location: locationEl ? locationEl.textContent.trim() : '',
                tags: tags.join('|'),
                job_id: jobIdMatch ? jobIdMatch[1] : ''
            });
        }
    });
    return JSON.stringify(jobs);
})()
"""

def extract_job_list(ws_url=None):
    """从列表页提取所有职位基础信息"""
    print("\n[Phase 2] 提取职位列表...")
    result = cdp_eval(EXTRACT_LIST_JS, ws_url)
    if not result:
        print("  ❌ 提取失败")
        return []
    try:
        jobs = json.loads(result)
        print(f"  找到 {len(jobs)} 条职位")
        return jobs
    except json.JSONDecodeError:
        print(f"  ❌ JSON 解析失败")
        return []


# ============================================================
# Phase 3: 详情页提取
# ============================================================
EXTRACT_SECURITY_ID_JS = """
(function(){
    var scripts = document.querySelectorAll('script');
    for(var i=0;i<scripts.length;i++){
        var text = scripts[i].innerText || scripts[i].textContent || '';
        var m = text.match(/securityId['":\\s]+['"]([^'"]+)['"]/);
        if(m) return m[1];
    }
    return '';
})()
"""

EXTRACT_DESC_JS = """
(function(){
    var text = document.body.innerText;
    var start = text.indexOf('职位描述');
    if(start==-1) start = text.indexOf('岗位职责');
    if(start==-1) return '';
    var descStart = start + '职位描述'.length;
    var endMarkers = ['刚刚活跃','工作地址','查看更多信息','在线状态','投诉举报','相似职位'];
    var end = text.length;
    for(var i=0;i<endMarkers.length;i++){
        var pos = text.indexOf(endMarkers[i], start);
        if(pos!=-1 && pos<end) end = pos;
    }
    return text.substring(descStart, end).trim();
})()
"""


def extract_detail(job_id, base_wait=20, max_retries=3, detail_jitter=3.0):
    """
    提取单条职位详情（security_id + 职位描述）。
    
    流程：
    1. CDP 打开新 tab
    2. 等待页面加载（base_wait + 随机波动）
    3. 检查 readyState
    4. 提取 security_id
    5. 提取职位描述
    6. 关闭 tab
    
    返回 (security_id, description) 或 (None, None)
    """
    detail_url = f"https://www.zhipin.com/job_detail/{job_id}.html"

    for attempt in range(max_retries):
        page_id = None
        try:
            # 打开新 tab
            page_id = open_new_tab(detail_url)
            if not page_id:
                continue

            # 等待页面加载（基础 + 随机波动 + 重试退避）
            wait = base_wait + random.uniform(0, detail_jitter) + attempt * 5
            time.sleep(wait)

            # 获取该 tab 的 WebSocket URL
            tab_ws = get_tab_ws(page_id)
            if not tab_ws:
                continue

            # 检查 readyState
            ready = cdp_eval("document.readyState", tab_ws, timeout=10)
            if ready != 'complete':
                continue

            # 提取 security_id
            sid = cdp_eval(EXTRACT_SECURITY_ID_JS, tab_ws, timeout=15)

            # 提取职位描述
            desc = cdp_eval(EXTRACT_DESC_JS, tab_ws, timeout=15)

            # 校验
            if sid and len(str(sid)) >= 30:
                return str(sid), str(desc or '')

        except Exception:
            pass
        finally:
            # 无论成功失败，关闭 tab
            if page_id:
                close_tab(page_id)
                time.sleep(1)

    return None, None


# ============================================================
# Phase 4: CSV 增量存储
# ============================================================
CSV_FIELDS = [
    '职位名称', '薪资', '经验要求', '学历要求',
    '公司名称', '城市', '区域', 'job_id', 'security_id',
    '职位描述', '创建日期'
]


def load_existing_job_ids(output_dir):
    """加载已有 job_id（跨所有 CSV 文件去重）"""
    existing = set()
    for f in glob.glob(os.path.join(output_dir, 'jobs_data*.csv')):
        try:
            with open(f, 'r', encoding='utf-8-sig') as fh:
                for row in csv.DictReader(fh):
                    if row.get('job_id'):
                        existing.add(row['job_id'])
        except Exception:
            pass
    # 分层抓取跳过集来自 _preseed.json（不再依赖 jobs_data_preseed.csv：
    # 预置文件改名 _preseed_data.csv 后不再匹配 jobs_data*.csv 通配，
    # 避免判定/全局统计/判定映射误把「内部跳过清单」当成本轮爬取结果）
    try:
        with open(os.path.join(output_dir, '_preseed.json'), 'r', encoding='utf-8') as f:
            rows = json.load(f)
        if isinstance(rows, list):
            for r in rows:
                jid = (r.get('job_id') or '').strip()
                if jid:
                    existing.add(jid)
    except Exception:
        pass
    return existing


def append_to_csv(job, output_file):
    """追加单条记录到 CSV（立即写入，不缓存）"""
    file_exists = os.path.exists(output_file) and os.path.getsize(output_file) > 0
    with open(output_file, 'a', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerow(job)


def parse_tags(tags_str):
    """从 tag 字符串提取经验和学历"""
    tags = tags_str.split('|') if tags_str else []
    exp = ''
    edu = ''
    for t in tags:
        if re.match(r'\d+-\d+年|经验不限', t):
            exp = t
        elif t in ('本科', '大专', '硕士', '博士', '学历不限'):
            edu = t
    return exp, edu


def parse_location(loc_str):
    """解析城市和区域"""
    if not loc_str:
        return '', ''
    parts = loc_str.strip().split('·')
    city = parts[0] if parts else ''
    district = '·'.join(parts[1:]) if len(parts) > 1 else ''
    return city, district


# ============================================================
# 主流程
# ============================================================
def main():
    global PREFERRED_LIST_URL
    parser = argparse.ArgumentParser(description='Boss直聘职位爬取（纯 CDP 模式）')
    parser.add_argument('--url', default='', help='目标列表页 URL（指定后优先匹配该页面，避免连错 tab）')
    parser.add_argument('--output', default='.', help='输出目录（默认当前目录）')
    parser.add_argument('--max-scroll', type=int, default=30, help='最大滚动次数（默认30）')
    parser.add_argument('--max-jobs', type=int, default=0, help='最大爬取条数（0=全部）')
    parser.add_argument('--base-wait', type=int, default=20, help='详情页基础等待秒数（默认20）')
    parser.add_argument('--scroll-wait', type=float, default=0.2, help='滚动间隔基准秒数（实际随机波动 0.1~0.5s）')
    parser.add_argument('--scroll-delta', type=str, default='1.5-2.5', help='滚动距离范围(K px)，格式 min-max')
    parser.add_argument('--job-gap', type=str, default='1.0-3.0', help='条间随机间隔范围(秒)，格式 min-max')
    parser.add_argument('--detail-jitter', type=float, default=3.0, help='详情页随机波动上限(秒，默认3)')
    args = parser.parse_args()

    output_dir = args.output
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(os.path.join(output_dir, 'temp'), exist_ok=True)

    # 生成输出文件名
    now = datetime.now()
    output_file = os.path.join(
        output_dir,
        f'jobs_data_{now.strftime("%Y%m%d")}_{now.strftime("%H%M")}.csv'
    )

    print("=" * 50)
    print("Boss直聘职位爬取（纯 CDP 反爬模式）")
    print("=" * 50)
    print(f"输出目录: {output_dir}")
    print(f"输出文件: {output_file}")
    print(f"详情页等待: {args.base_wait} 秒 + 随机波动")
    if args.url:
        print(f"目标列表页: {args.url}")
        PREFERRED_LIST_URL = args.url
    print()

    # T6: 初始化进度追踪器（先不启动，等 CDP 连接后读取 URL）
    tracker = ProgressTracker(output_dir)

    # T6: 自动启动 Dashboard 监控页面
    dashboard_port = 8765
    try:
        _ensure_pipeline_path()
        from serve_dashboard import start_dashboard_server
        dashboard_url = start_dashboard_server(output_dir, port=dashboard_port, background=True)
        print(f"🌐 Dashboard: {dashboard_url}")
    except Exception as e:
        print(f"  Dashboard 启动失败（端口可能被占用）: {e}")
        for p in range(8766, 8780):
            try:
                dashboard_url = start_dashboard_server(output_dir, port=p, background=True)
                print(f"🌐 Dashboard: {dashboard_url}")
                break
            except Exception:
                continue

    # 检查 CDP 连接（含重试：浏览器/CDP 可能尚未就绪）
    ws_url = None
    for _ in range(5):
        ws_url = get_list_page_ws(args.url)
        if ws_url:
            break
        print("  ⏳ 尚未找到 Boss 列表页，2 秒后重试...")
        time.sleep(2)
    if not ws_url:
        print("❌ 未找到 Boss 直聘列表页，请先启动 CloakBrowser 并打开职位列表")
        tracker.finish("error")
        return

    try:
        _cur = cdp_eval('window.location.href', ws_url, timeout=5)
        print(f"📍 实际连接页面: {_cur}")
    except Exception:
        pass

    # 预检：确认列表页确实已加载出职位卡片（避免对空页/验证页空滚 250 秒）
    preflight = 0
    for _ in range(10):
        preflight_raw = cdp_eval("document.querySelectorAll('.job-card-wrap').length", ws_url, timeout=10)
        preflight = int(preflight_raw) if preflight_raw else 0
        if preflight > 0:
            break
        print(f"  ⏳ 列表页卡片数为 0（可能仍在加载或弹出验证/风控页），3 秒后复查...")
        time.sleep(3)
    if preflight == 0:
        print("❌ 列表页未检测到职位卡片：可能被风控/验证页拦截，或连错 tab。请检查浏览器页面后重试。")
        tracker.finish("error")
        return

    # 读取当前页面 URL 作为搜索链接
    search_url = ''
    try:
        search_url = cdp_eval('window.location.href', ws_url, timeout=5)
    except Exception:
        pass
    # 断点续爬检测：目录已有数据 → resume 模式（不清空 jobs_cache，累计统计）
    has_existing_data = bool(glob.glob(os.path.join(output_dir, 'jobs_data*.csv')))
    if not has_existing_data:
        jc = os.path.join(output_dir, 'jobs_cache.json')
        has_existing_data = os.path.isfile(jc) and os.path.getsize(jc) > 2
    if not has_existing_data:
        pe = os.path.join(output_dir, '_preseed.json')
        has_existing_data = os.path.isfile(pe) and os.path.getsize(pe) > 2
    if has_existing_data:
        print(f"📌 检测到已有数据，进入续爬模式（去重跳过 + 累计统计）")
    tracker.start(list_total=0, search_url=search_url, resume=has_existing_data)

    # 检查登录状态
    if not check_login(ws_url):
        print("\n⚠️  Boss 直聘未登录。请在浏览器中完成扫码登录，登录后按回车继续...")
        input()
        # 重试一次
        ws_url = get_list_page_ws(args.url)
        if not ws_url:
            print("❌ 登录后未找到 Boss 列表页，请确认 URL 正确")
            tracker.finish("error")
            return
        if not check_login(ws_url):
            print("❌ 仍未检测到登录状态，请确认扫码成功")
            tracker.finish("error")
            return

    # 加载已有数据
    existing_ids = load_existing_job_ids(output_dir)
    print(f"已有数据: {len(existing_ids)} 条（跨文件去重）")

    # Phase 1: 滚动
    delta_parts = args.scroll_delta.split('-')
    scroll_delta = (float(delta_parts[0]) * 1000, float(delta_parts[1]) * 1000)
    gap_parts = args.job_gap.split('-')
    job_gap_min = float(gap_parts[0])
    job_gap_max = float(gap_parts[1])
    scroll_list_page(args.max_scroll, ws_url, args.scroll_wait, scroll_delta)

    # T6: 滚动后更新列表总数
    list_count = cdp_eval("document.querySelectorAll('.job-card-wrap').length", ws_url)
    tracker.update_list_total(int(list_count) if list_count else 0)

    # Phase 2: 提取列表
    jobs = extract_job_list(ws_url)
    if not jobs:
        print("❌ 未找到职位数据")
        tracker.finish("error")
        return

    # === 存在性信号落盘（长期监控对账 / 抓取量闸门的唯一依据）===
    # 全部可见卡片（不过滤），与详情抓取解耦：监控端用它在跨轮主表里判定
    # 「新增/未出现/重发」，预置跳过只影响详情抓取，绝不能再回显为在场。
    try:
        with open(os.path.join(output_dir, 'list_jobs.json'), 'w', encoding='utf-8') as f:
            json.dump(jobs, f, ensure_ascii=False)
        print(f"  列表卡片落盘: {len(jobs)} 条 → list_jobs.json")
    except Exception as e:
        print(f"  ⚠️ list_jobs.json 落盘失败: {e}")

    # 过滤已有
    new_jobs = [j for j in jobs if j.get('job_id') and j['job_id'] not in existing_ids]
    print(f"\n待爬取: {len(new_jobs)} 条（去重跳过 {len(jobs) - len(new_jobs)} 条）")

    if args.max_jobs > 0:
        new_jobs = new_jobs[:args.max_jobs]
        print(f"限制爬取: {args.max_jobs} 条")

    if not new_jobs:
        print("✅ 无新增职位")
        tracker.finish()
        return

    # Phase 3 & 4: 逐条提取 + 立即写入
    print(f"\n[Phase 3] 详情页提取（共 {len(new_jobs)} 条）...")

    success_count = 0
    error_count = 0
    error_log = []

    for i, job in enumerate(new_jobs):
        title = job.get('title', '')[:30]
        job_id = job.get('job_id', '')
        print(f"\n  [{i+1}/{len(new_jobs)}] {title}")

        if not job_id:
            print(f"    ⚠️ 跳过：无 job_id")
            error_count += 1
            error_log.append({
                'title': job.get('title', ''),
                'job_id': '',
                'error': '无 job_id'
            })
            continue

        # 提取详情
        sid, desc = extract_detail(job_id, args.base_wait, 3, args.detail_jitter)

        if not sid or len(sid) < 30:
            print(f"    ❌ security_id 提取失败")
            error_count += 1
            error_log.append({
                'title': job.get('title', ''),
                'job_id': job_id,
                'error': f'security_id 无效（长度 {len(sid or "")}）'
            })
            # T6: 记录失败
            tracker.on_job_scraped({'职位名称': job.get('title',''), 'job_id': job_id, '公司名称': job.get('company','')}, success=False, error='security_id 无效')
            continue

        # 解析字段
        salary = decode_pua(job.get('salary', ''))
        exp, edu = parse_tags(job.get('tags', ''))
        city, district = parse_location(job.get('location', ''))

        # 立即写入 CSV
        csv_row = {
            '职位名称': job.get('title', ''),
            '薪资': salary,
            '经验要求': exp,
            '学历要求': edu,
            '公司名称': job.get('company', ''),
            '城市': city,
            '区域': district,
            'job_id': job_id,
            'security_id': sid,
            '职位描述': desc or '',
            '创建日期': now.strftime('%Y-%m-%d %H:%M'),
        }
        append_to_csv(csv_row, output_file)
        success_count += 1

        # T6: 更新进度
        tracker.on_job_scraped(csv_row, success=True)

        desc_len = len(desc or '')
        print(f"    ✅ sid={sid[:20]}... | 描述={desc_len} 字符")

        # 条间随机等待（反爬）
        if i < len(new_jobs) - 1:
            gap = random.uniform(job_gap_min, job_gap_max)
            time.sleep(gap)

    # 写入错误日志
    if error_log:
        error_file = os.path.join(output_dir, 'temp', 'error_log.csv')
        with open(error_file, 'a', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=['title', 'job_id', 'error'])
            if not os.path.exists(error_file) or os.path.getsize(error_file) == 0:
                writer.writeheader()
            for err in error_log:
                writer.writerow(err)

    # T6: 标记爬取结束
    tracker.finish()

    # Phase 5: 质量报告
    total = 0
    for f_path in glob.glob(os.path.join(output_dir, 'jobs_data*.csv')):
        try:
            with open(f_path, 'r', encoding='utf-8-sig') as fh:
                total += sum(1 for _ in csv.DictReader(fh))
        except Exception:
            pass

    print("\n" + "=" * 50)
    print("爬取完成")
    print("=" * 50)
    print(f"本次新增:     {success_count} 条")
    print(f"本次失败:     {error_count} 条")
    print(f"去重/限量跳过: {len(jobs) - len(new_jobs)} 条")
    print(f"累计总量:     {total} 条")
    print(f"输出文件:     {output_file}")
    if error_log:
        print(f"错误日志:     {os.path.join(output_dir, 'temp', 'error_log.csv')}")


if __name__ == '__main__':
    main()
