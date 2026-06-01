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
import argparse
from datetime import datetime

try:
    import websocket
except ImportError:
    print("❌ 缺少 websocket-client → pip3 install websocket-client")
    exit(1)

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
def get_list_page_ws():
    """获取列表页 WebSocket URL"""
    try:
        r = subprocess.run(
            ['curl', '-s', 'http://localhost:9222/json'],
            capture_output=True, text=True, timeout=10
        )
        pages = json.loads(r.stdout)
        for p in pages:
            if p.get('type') == 'page' and 'jobs?' in p.get('url', ''):
                return p['webSocketDebuggerUrl']
    except Exception:
        pass
    return None


def cdp_eval(js_code, ws_url=None, timeout=15):
    """
    通过 CDP 执行 JavaScript，每次新建连接。
    返回执行结果，失败返回 None。
    """
    if not ws_url:
        ws_url = get_list_page_ws()
    if not ws_url:
        return None

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
    except Exception:
        pass
    finally:
        if ws:
            try:
                ws.close()
            except Exception:
                pass
    return None


def cdp_scroll(ws_url, delta_y=800):
    """模拟鼠标滚轮滚动（独立连接）"""
    ws = None
    try:
        ws = websocket.create_connection(ws_url, timeout=10)
        ws.send(json.dumps({
            "id": 1,
            "method": "Input.dispatchMouseEvent",
            "params": {
                "type": "mouseWheel",
                "x": random.randint(300, 700),
                "y": random.randint(300, 500),
                "deltaX": 0,
                "deltaY": delta_y
            }
        }))
        ws.recv()
    except Exception:
        pass
    finally:
        if ws:
            try:
                ws.close()
            except Exception:
                pass


def open_new_tab(url):
    """CDP 打开新 tab，返回 page_id"""
    try:
        req_url = f'http://localhost:9222/json/new?{url}'
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
            ['curl', '-s', f'http://localhost:9222/json/close/{page_id}'],
            capture_output=True, timeout=5
        )
    except Exception:
        pass


def get_tab_ws(page_id):
    """获取指定 tab 的 WebSocket URL"""
    try:
        r = subprocess.run(
            ['curl', '-s', 'http://localhost:9222/json'],
            capture_output=True, text=True, timeout=10
        )
        pages = json.loads(r.stdout)
        for p in pages:
            if p.get('id') == page_id:
                return p.get('webSocketDebuggerUrl')
    except Exception:
        pass
    return None


# ============================================================
# Phase 1: 列表页滚动
# ============================================================
def scroll_list_page(max_scrolls=100, ws_url=None):
    """
    滚动列表页加载更多职位。
    随机等待 1.5-3.5 秒，连续 3 次数量不变则停止。
    """
    print(f"\n[Phase 1] 滚动加载（最多 {max_scrolls} 次）...")

    prev_count = 0
    same_count = 0

    for i in range(max_scrolls):
        cdp_scroll(ws_url, random.randint(600, 1000))
        # 随机等待 1.5-3.5 秒
        wait = random.uniform(1.5, 3.5)
        time.sleep(wait)

        # 每 5 次统计一次
        if (i + 1) % 5 == 0:
            count_js = "document.querySelectorAll('.job-card-wrap').length"
            count = cdp_eval(count_js, ws_url)
            count = int(count) if count else 0
            print(f"  第 {i+1} 次滚动: {count} 条职位")

            if count == prev_count:
                same_count += 1
                if same_count >= 3:
                    print(f"  连续 3 次数量不变，停止滚动")
                    break
            else:
                same_count = 0
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


def extract_detail(job_id, base_wait=20, max_retries=3):
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
            wait = base_wait + random.uniform(0, 3) + attempt * 5
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
    parser = argparse.ArgumentParser(description='Boss直聘职位爬取（纯 CDP 模式）')
    parser.add_argument('--output', default='.', help='输出目录（默认当前目录）')
    parser.add_argument('--max-scroll', type=int, default=100, help='最大滚动次数（默认100）')
    parser.add_argument('--max-jobs', type=int, default=0, help='最大爬取条数（0=全部）')
    parser.add_argument('--base-wait', type=int, default=20, help='详情页基础等待秒数（默认20）')
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
    print()

    # 检查 CDP 连接
    ws_url = get_list_page_ws()
    if not ws_url:
        print("❌ 未找到 Boss 直聘列表页，请先启动 CloakBrowser")
        return

    # 加载已有数据
    existing_ids = load_existing_job_ids(output_dir)
    print(f"已有数据: {len(existing_ids)} 条（跨文件去重）")

    # Phase 1: 滚动
    scroll_list_page(args.max_scroll, ws_url)

    # Phase 2: 提取列表
    jobs = extract_job_list(ws_url)
    if not jobs:
        print("❌ 未找到职位数据")
        return

    # 过滤已有
    new_jobs = [j for j in jobs if j.get('job_id') and j['job_id'] not in existing_ids]
    print(f"\n待爬取: {len(new_jobs)} 条（去重跳过 {len(jobs) - len(new_jobs)} 条）")

    if args.max_jobs > 0:
        new_jobs = new_jobs[:args.max_jobs]
        print(f"限制爬取: {args.max_jobs} 条")

    if not new_jobs:
        print("✅ 无新增职位")
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
        sid, desc = extract_detail(job_id, args.base_wait)

        if not sid or len(sid) < 30:
            print(f"    ❌ security_id 提取失败")
            error_count += 1
            error_log.append({
                'title': job.get('title', ''),
                'job_id': job_id,
                'error': f'security_id 无效（长度 {len(sid or "")}）'
            })
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

        desc_len = len(desc or '')
        print(f"    ✅ sid={sid[:20]}... | 描述={desc_len} 字符")

        # 条间随机等待（反爬）
        if i < len(new_jobs) - 1:
            gap = random.uniform(1.0, 3.0)
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
    print(f"去重跳过:     {len(jobs) - len(new_jobs)} 条")
    print(f"累计总量:     {total} 条")
    print(f"输出文件:     {output_file}")
    if error_log:
        print(f"错误日志:     {os.path.join(output_dir, 'temp', 'error_log.csv')}")


if __name__ == '__main__':
    main()
