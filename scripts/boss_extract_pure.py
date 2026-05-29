#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Boss直聘职位爬取 - 纯 CDP 模式（不依赖 agent-browser）
依赖：websocket-client
"""

import argparse
import json
import csv
import re
import websocket
import time
import subprocess
import sys
import urllib.request
import glob
import os
from datetime import datetime

# 修复 Python 输出缓冲：强制行缓冲，print 实时输出
sys.stdout.reconfigure(line_buffering=True)

# ==================== PUA 字符映射 ====================
PUA_MAP = {
    0xe031: '0', 0xe032: '1', 0xe033: '2', 0xe034: '3', 0xe035: '4',
    0xe036: '5', 0xe037: '6', 0xe038: '7', 0xe039: '8', 0xe03a: '9'
}


def decode_pua(text):
    """解码 PUA 字符为真实数字"""
    if not text:
        return text
    return ''.join(PUA_MAP.get(ord(c), c) for c in text)


# ==================== CDP 工具 ====================

def cdp_execute(ws_url, js_code, timeout=15):
    """通过 CDP 执行 JavaScript"""
    try:
        ws = websocket.create_connection(ws_url, timeout=timeout)
        command = {
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": js_code, "returnByValue": True}
        }
        ws.send(json.dumps(command))
        response = ws.recv()
        result = json.loads(response)
        ws.close()
        if 'result' in result and 'result' in result['result']:
            return result['result']['result'].get('value', '')
        return None
    except Exception as e:
        print(f"  CDP 执行错误: {e}")
        return None


def cdp_scroll(ws_url, delta_y=800):
    """使用 CDP Input.dispatchMouseEvent 模拟鼠标滚轮"""
    try:
        ws = websocket.create_connection(ws_url, timeout=10)
        command = {
            "id": 1,
            "method": "Input.dispatchMouseEvent",
            "params": {
                "type": "mouseWheel",
                "x": 500, "y": 400,
                "deltaX": 0, "deltaY": delta_y
            }
        }
        ws.send(json.dumps(command))
        ws.recv()
        ws.close()
        return True
    except Exception as e:
        print(f"  滚动错误: {e}")
        return False


def count_jobs_on_page(ws_url):
    """统计当前页面职位数"""
    try:
        ws = websocket.create_connection(ws_url, timeout=10)
        command = {
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {
                "expression": "document.querySelectorAll('[class*=job-card]').length",
                "returnByValue": True
            }
        }
        ws.send(json.dumps(command))
        response = ws.recv()
        result = json.loads(response)
        ws.close()
        if 'result' in result and 'result' in result['result']:
            return result['result']['result'].get('value', 0)
        return 0
    except Exception as e:
        print(f"  统计职位数错误: {e}")
        return 0


def get_list_page_id():
    """获取列表页 ID 和 WebSocket URL"""
    result = subprocess.run(['curl', '-s', 'http://localhost:9222/json'],
                            capture_output=True, text=True, timeout=5)
    pages = json.loads(result.stdout)
    for p in pages:
        if 'jobs?' in p.get('url', ''):
            return p['id'], p['webSocketDebuggerUrl']
    return None, None


def open_new_tab(url):
    """打开新标签页"""
    try:
        req = urllib.request.Request(
            f'http://localhost:9222/json/new?{url}', method='PUT'
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            page_info = json.loads(response.read().decode('utf-8'))
            return page_info.get('id')
    except Exception as e:
        print(f"  打开新标签页错误: {e}")
        return None


def close_tab(page_id):
    """关闭标签页"""
    subprocess.run(['curl', '-s', f'http://localhost:9222/json/close/{page_id}'],
                   capture_output=True, timeout=5)


# ==================== 核心流程 ====================

def scroll_page(ws_url, max_scrolls=100):
    """滚动页面加载所有职位"""
    ws = websocket.create_connection(ws_url, timeout=10)
    ws.send(json.dumps({
        "id": 1, "method": "Runtime.evaluate",
        "params": {"expression": "window.scrollTo(0, 0)", "returnByValue": True}
    }))
    ws.recv()
    ws.close()
    time.sleep(1)

    prev_count = 0
    same_count = 0

    for i in range(max_scrolls):
        cdp_scroll(ws_url, 800)
        wait_time = 1.5 + (i % 3) * 0.5
        time.sleep(wait_time)

        if (i + 1) % 5 == 0:
            count = count_jobs_on_page(ws_url)
            print(f"  第 {i + 1} 次滚动: {count} 条职位")
            if count == prev_count:
                same_count += 1
                if same_count >= 3:
                    print(f"  连续 3 次数量不变，停止滚动")
                    break
            else:
                same_count = 0
            prev_count = count

    return prev_count


def extract_jobs_from_list(ws_url):
    """从列表页直接用 JavaScript 提取职位数据"""
    js_code = """
    (function() {
        var jobs = [];
        var items = document.querySelectorAll('li[class*="job-card"]');

        for (var i = 0; i < items.length; i++) {
            var item = items[i];

            var titleEl = item.querySelector('.job-name');
            var title = titleEl ? titleEl.innerText.trim() : '';

            var salaryEl = item.querySelector('.job-salary');
            var salary = salaryEl ? salaryEl.innerText.trim() : '';

            var companyEl = item.querySelector('.boss-name');
            var company = companyEl ? companyEl.innerText.trim() : '';

            var linkEl = item.querySelector('.job-name');
            var href = linkEl ? linkEl.href : '';
            var jobId = '';
            if (href) {
                var match = href.match(/job_detail\\/(.+?)\\.html/);
                if (match) jobId = match[1];
            }

            var areaEl = item.querySelector('.company-location');
            var areaText = areaEl ? areaEl.innerText.trim() : '';

            var tags = item.querySelectorAll('.job-info .tag-list li');
            var experience = '';
            var education = '';
            for (var j = 0; j < tags.length; j++) {
                var tagText = tags[j].innerText.trim();
                if (tagText.match(/^\\d+-\\d+年$/)) {
                    experience = tagText;
                } else if (tagText.match(/本科|大专|硕士|博士|学历不限/)) {
                    education = tagText;
                }
            }

            var city = '';
            var area = '';
            if (areaText) {
                var parts = areaText.split('·');
                city = parts[0] || '';
                area = parts.length > 1 ? parts.slice(1).join('·') : '';
            }

            jobs.push({
                title: title, salary: salary, company: company,
                city: city, area: area,
                experience: experience, education: education,
                jobId: jobId, href: href
            });
        }

        return JSON.stringify(jobs);
    })()
    """
    result = cdp_execute(ws_url, js_code, timeout=20)
    if result:
        try:
            return json.loads(result)
        except Exception:
            return []
    return []


def get_security_id(ws_url):
    """从详情页提取 security_id"""
    js_code = """
    (function() {
        var scripts = document.querySelectorAll('script');
        for (var i = 0; i < scripts.length; i++) {
            var text = scripts[i].innerText || scripts[i].textContent || '';
            var match = text.match(/securityId['":\\s]+['"]([^'"]+)['"]/);
            if (match) return match[1];
        }
        return '';
    })()
    """
    return cdp_execute(ws_url, js_code) or ''


def get_job_description(ws_url):
    """从详情页提取职位描述"""
    js_code = """
    (function() {
        var text = document.body.innerText;
        var start = text.indexOf('职位描述');
        if (start == -1) start = text.indexOf('岗位职责');
        if (start == -1) return '';

        var descStart = start + '职位描述'.length;
        var endMarkers = ['刚刚活跃', '工作地址', '查看更多信息', '在线状态', '投诉举报', '相似职位', '微信扫码分享', '举报'];
        var end = text.length;
        for (var i = 0; i < endMarkers.length; i++) {
            var pos = text.indexOf(endMarkers[i], start);
            if (pos != -1 && pos < end) end = pos;
        }
        return text.substring(descStart, end).trim();
    })()
    """
    return cdp_execute(ws_url, js_code) or ''


# ==================== 主函数 ====================

def main():
    parser = argparse.ArgumentParser(description='Boss直聘职位爬取（纯 CDP 模式，不依赖 agent-browser）')
    parser.add_argument('--output', '-o', default='.', help='输出目录（默认当前目录）')
    parser.add_argument('--max-scroll', type=int, default=100, help='最大滚动次数（默认 100）')
    args = parser.parse_args()

    output_dir = args.output
    max_scrolls = args.max_scroll

    print("=== Boss直聘职位爬取（纯 CDP 模式）===")

    # 获取列表页
    print("\n[Phase 1] 获取列表页...")
    page_id, ws_url = get_list_page_id()
    if not page_id:
        print("❌ 未找到 Boss 直聘列表页，请先打开 CloakBrowser")
        sys.exit(1)
    print(f"  列表页 ID: {page_id}")

    # 滚动
    print(f"\n[Phase 2] 滚动页面（最多 {max_scrolls} 次）...")
    final_count = scroll_page(ws_url, max_scrolls)
    print(f"  滚动完成，最终职位数: {final_count}")

    # 提取列表
    print("\n[Phase 3] 提取职位列表...")
    jobs = extract_jobs_from_list(ws_url)
    print(f"  找到 {len(jobs)} 条职位")

    if not jobs:
        print("❌ 未找到职位数据")
        return

    # 显示前 3 条
    print("\n  前 3 条预览:")
    for i, job in enumerate(jobs[:3]):
        print(f"    {i + 1}. {job.get('title', 'N/A')} | {decode_pua(job.get('salary', ''))} | {job.get('company', 'N/A')}")

    # Phase 4: 详情页提取
    print(f"\n[Phase 4] 详情页逐条提取（共 {len(jobs)} 条）...")
    all_jobs = []
    error_log = []

    for i, job in enumerate(jobs):
        print(f"\n  处理 {i + 1}/{len(jobs)}: {job.get('title', 'N/A')}")

        job_id = job.get('jobId', '')
        if not job_id:
            print(f"    跳过: job_id 为空")
            error_log.append({'title': job.get('title'), 'error': 'job_id 为空'})
            continue

        detail_url = f"https://www.zhipin.com/job_detail/{job_id}.html"
        detail_page_id = open_new_tab(detail_url)
        if not detail_page_id:
            print(f"    错误: 无法打开详情页")
            error_log.append({'title': job.get('title'), 'job_id': job_id, 'error': '无法打开详情页'})
            continue

        print(f"    等待页面加载（20 秒）...")
        time.sleep(20)

        detail_ws_url = f"ws://localhost:9222/devtools/page/{detail_page_id}"
        ready_state = cdp_execute(detail_ws_url, "document.readyState", timeout=5)
        if ready_state != 'complete':
            print(f"    错误: 页面未加载完成（{ready_state}）")
            close_tab(detail_page_id)
            error_log.append({'title': job.get('title'), 'job_id': job_id, 'error': f'页面未完成: {ready_state}'})
            continue

        security_id = get_security_id(detail_ws_url)
        if len(security_id) < 30:
            print(f"    警告: security_id 过短（{len(security_id)} 字符）")
            close_tab(detail_page_id)
            error_log.append({'title': job.get('title'), 'job_id': job_id, 'error': f'security_id 过短: {len(security_id)}'})
            continue

        description = get_job_description(detail_ws_url)
        print(f"    security_id: {security_id[:40]}... | 描述: {len(description)} 字符")

        close_tab(detail_page_id)
        time.sleep(1)

        all_jobs.append({
            '职位名称': job.get('title', ''),
            '薪资': decode_pua(job.get('salary', '')),
            '经验要求': job.get('experience', ''),
            '学历要求': job.get('education', ''),
            '公司名称': job.get('company', ''),
            '城市': job.get('city', ''),
            '区域': job.get('area', ''),
            'job_id': job_id,
            'security_id': security_id,
            '职位描述': description,
            '创建日期': datetime.now().strftime('%Y-%m-%d %H:%M')
        })

    print(f"\n成功提取 {len(all_jobs)} 条职位完整数据")

    # Phase 5: 保存 CSV
    print(f"\n[Phase 5] 保存 CSV...")
    date_str = datetime.now().strftime('%Y%m%d')
    time_str = datetime.now().strftime('%H%M')
    output_file = os.path.join(output_dir, f'jobs_data_{date_str}_{time_str}.csv')

    # 去重：扫描目录下所有 CSV
    existing_job_ids = set()
    for f in glob.glob(os.path.join(output_dir, 'jobs_data*.csv')):
        try:
            with open(f, 'r', encoding='utf-8-sig') as fh:
                for row in csv.DictReader(fh):
                    if row.get('job_id'):
                        existing_job_ids.add(row['job_id'])
        except Exception:
            pass

    new_jobs = [j for j in all_jobs if j.get('job_id') not in existing_job_ids]
    skipped = len(all_jobs) - len(new_jobs)

    fieldnames = ['职位名称', '薪资', '经验要求', '学历要求', '公司名称', '城市', '区域',
                  'job_id', 'security_id', '职位描述', '创建日期']

    with open(output_file, 'a', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not os.path.exists(output_file) or os.path.getsize(output_file) == 0:
            writer.writeheader()
        for job in new_jobs:
            writer.writerow(job)

    print(f"  新增: {len(new_jobs)} 条, 去重跳过: {len(all_jobs) - len(new_jobs)} 条")
    print(f"  文件: {output_file}")

    # 错误日志
    if error_log:
        error_dir = os.path.join(output_dir, 'temp')
        os.makedirs(error_dir, exist_ok=True)
        error_file = os.path.join(error_dir, 'error_log.csv')
        with open(error_file, 'a', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=['title', 'job_id', 'error'])
            if os.path.getsize(error_file) == 0:
                writer.writeheader()
            for err in error_log:
                writer.writerow(err)
        print(f"  错误日志: {error_file}")

    # Phase 6: 质量报告
    print(f"\n[Phase 6] 质量报告")
    total = len(existing_job_ids) + len(new_jobs)
    print(f"  本次新增: {len(new_jobs)} 条")
    print(f"  去重跳过: {len(all_jobs) - len(new_jobs)} 条")
    print(f"  累计总量: {total} 条")

    if new_jobs:
        job_id_ok = sum(1 for j in new_jobs if j.get('job_id') and len(j['job_id']) >= 20)
        sid_ok = sum(1 for j in new_jobs if j.get('security_id') and len(j['security_id']) >= 30)
        desc_ok = sum(1 for j in new_jobs if j.get('职位描述') and len(j['职位描述']) >= 100)
        n = len(new_jobs)
        print(f"  字段完整率: job_id {job_id_ok/n*100:.0f}% | security_id {sid_ok/n*100:.0f}% | 职位描述 {desc_ok/n*100:.0f}%")

    if error_log:
        print(f"  错误: {len(error_log)} 条")

    print("\n=== 完成 ===")


if __name__ == "__main__":
    main()
