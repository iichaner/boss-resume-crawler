#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Boss直聘职位数据提取 - agent-browser snapshot + CDP 详情页提取
依赖：agent-browser, websocket-client
"""

import argparse
import json
import csv
import re
import websocket
import time
import shutil
import subprocess
import sys
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


# ==================== 工具函数 ====================

def find_agent_browser():
    """动态查找 agent-browser 可执行文件"""
    # 1. 从 PATH 查找
    path = shutil.which('agent-browser')
    if path:
        return path
    # 2. 常见 npm 全局安装路径
    candidates = [
        '~/.npm-global/lib/node_modules/agent-browser/bin/agent-browser-darwin-x64',
        '~/.npm-global/lib/node_modules/agent-browser/bin/agent-browser-linux-x64',
        '/usr/local/lib/node_modules/agent-browser/bin/agent-browser-darwin-x64',
    ]
    import os
    for c in candidates:
        expanded = os.path.expanduser(c)
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            return expanded
    return None


def get_ws_url():
    """获取列表页 WebSocket URL"""
    result = subprocess.run(['curl', '-s', 'http://localhost:9222/json'],
                            capture_output=True, text=True, timeout=5)
    pages = json.loads(result.stdout)
    for p in pages:
        if p.get('type') == 'page' and 'jobs?' in p.get('url', ''):
            return p['webSocketDebuggerUrl']
    return None


def get_page_id():
    """获取列表页 ID"""
    result = subprocess.run(['curl', '-s', 'http://localhost:9222/json'],
                            capture_output=True, text=True, timeout=5)
    pages = json.loads(result.stdout)
    for page in pages:
        if page.get('type') == 'page' and 'jobs?' in page.get('url', ''):
            return page['id']
    return None


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


def open_new_tab(url):
    """打开新标签页"""
    try:
        import urllib.request
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
    # 回到顶部
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


def extract_jobs_from_snapshot(agent_browser_path):
    """从 snapshot 提取职位数据"""
    result = subprocess.run(
        [agent_browser_path, '--cdp', '9222', 'snapshot', '-i', '--timeout', '8000'],
        capture_output=True, text=True, timeout=15
    )
    snapshot = result.stdout
    jobs = []
    lines = snapshot.split('\n')

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith('- listitem "') and 'K' in line and '年' in line:
            match = re.match(r'- listitem "([^"]+)".*\[.*ref=(\w+)\]', line)
            if match:
                job_text = match.group(1)
                list_ref = match.group(2)
                title_ref = ''
                company_ref = ''
                company = ''
                link_refs = []

                for j in range(i + 1, min(i + 5, len(lines))):
                    link_line = lines[j].strip()
                    if link_line.startswith('- link "'):
                        link_match = re.match(r'- link "([^"]*)".*\[ref=(\w+)\]', link_line)
                        if link_match:
                            link_refs.append((link_match.group(1), link_match.group(2)))

                if len(link_refs) >= 1:
                    title_ref = link_refs[0][1]
                if len(link_refs) >= 2:
                    company_ref = link_refs[1][1]
                    company = link_refs[1][0]

                # 解析薪资
                salary_match = re.search(
                    r'([\ue031-\ue03a]+-[\ue031-\ue03a]+K(?:·[\ue031-\ue03a]+薪)?)', job_text
                )
                salary_raw = salary_match.group(1) if salary_match else ''
                salary = decode_pua(salary_raw)

                if salary_raw:
                    title_end = job_text.find(salary_raw)
                    title = job_text[:title_end].strip()
                    after_salary = job_text[title_end + len(salary_raw):]
                else:
                    title = job_text
                    after_salary = ''

                experience = ''
                exp_match = re.search(r'(\d+-\d+年)', after_salary)
                if exp_match:
                    experience = exp_match.group(1)

                education = ''
                edu_match = re.search(r'(本科|大专|硕士|博士|学历不限)', after_salary)
                if edu_match:
                    education = edu_match.group(1)

                location = ''
                loc_match = re.search(r'([\u4e00-\u9fa5]+·[\u4e00-\u9fa5]+(?:·[\u4e00-\u9fa5]+)?)', after_salary)
                if loc_match:
                    location = loc_match.group(1)

                city, area = '', ''
                if location:
                    parts = location.split('·')
                    city = parts[0]
                    area = '·'.join(parts[1:]) if len(parts) > 1 else ''

                jobs.append({
                    'title': title, 'salary': salary, 'experience': experience,
                    'education': education, 'company': company,
                    'city': city, 'area': area,
                    'list_ref': list_ref, 'title_ref': title_ref, 'company_ref': company_ref
                })
        i += 1

    return jobs


def get_link_href(agent_browser_path, ref):
    """获取指定 ref 的链接"""
    result = subprocess.run(
        [agent_browser_path, '--cdp', '9222', 'get', 'attr', f'@{ref}', 'href', '--timeout', '5000'],
        capture_output=True, text=True, timeout=10
    )
    return result.stdout.strip()


def get_security_id_from_detail(ws_url):
    """从详情页 script 标签提取 security_id"""
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


def get_job_description_from_detail(ws_url):
    """从详情页提取职位描述"""
    js_code = '''
    (function() {
        var text = document.body.innerText;
        var start = text.indexOf("职位描述");
        if (start == -1) start = text.indexOf("岗位职责");
        if (start == -1) start = text.indexOf("任职要求");
        if (start == -1) return "";
        var endMarkers = ["刚刚活跃", "工作地址", "查看更多信息", "求职工具", "活跃度"];
        var end = text.length;
        for (var i = 0; i < endMarkers.length; i++) {
            var pos = text.indexOf(endMarkers[i], start);
            if (pos != -1 && pos < end) end = pos;
        }
        return text.substring(start, end).replace(/\\n/g, " ").replace(/\\r/g, " ").trim();
    })()
    '''
    return cdp_execute(ws_url, js_code) or ''


# ==================== 主函数 ====================

def main():
    parser = argparse.ArgumentParser(description='Boss直聘职位数据提取（agent-browser 模式）')
    parser.add_argument('--output', '-o', default='.', help='输出目录（默认当前目录）')
    parser.add_argument('--max-scroll', type=int, default=100, help='最大滚动次数（默认 100）')
    args = parser.parse_args()

    output_dir = args.output
    max_scrolls = args.max_scroll

    print("=== Boss直聘职位数据提取（agent-browser 模式）===")

    # 检查 agent-browser
    agent_browser = find_agent_browser()
    if not agent_browser:
        print("❌ 未找到 agent-browser，请先安装：npm install -g agent-browser")
        sys.exit(1)
    print(f"agent-browser: {agent_browser}")

    # 获取页面
    page_id = get_page_id()
    if not page_id:
        print("❌ 未找到 Boss 直聘列表页，请先打开 CloakBrowser")
        sys.exit(1)

    ws_url = f"ws://localhost:9222/devtools/page/{page_id}"
    ws_url_scroll = get_ws_url()
    if not ws_url_scroll:
        print("❌ 无法获取 WebSocket URL")
        sys.exit(1)

    # Phase 1: 滚动
    print(f"\n[Phase 1] 滚动页面（最多 {max_scrolls} 次）...")
    final_count = scroll_page(ws_url_scroll, max_scrolls)
    print(f"  滚动完成，最终职位数: {final_count}")

    # Phase 2: 提取列表
    print("\n[Phase 2] 提取职位列表...")
    jobs = extract_jobs_from_snapshot(agent_browser)
    print(f"  找到 {len(jobs)} 条职位")

    if not jobs:
        print("❌ 未找到职位数据")
        return

    # 提取 job_id
    print("\n[Phase 3] 提取详情页数据...")
    for job in jobs:
        title_ref = job.get('title_ref', '')
        if title_ref:
            href = get_link_href(agent_browser, title_ref)
            if href:
                job_id_match = re.search(r'/job_detail/(.+?)\.html', href)
                job['job_id'] = job_id_match.group(1) if job_id_match else ''
            else:
                job['job_id'] = ''
        else:
            job['job_id'] = ''

    # Phase 3: 详情页逐条提取
    all_jobs = []
    error_log = []

    for i, job in enumerate(jobs):
        print(f"\n  处理 {i + 1}/{len(jobs)}: {job.get('title', 'N/A')}")

        job_id = job.get('job_id', '')
        if not job_id:
            print(f"    跳过: job_id 为空")
            error_log.append({'title': job.get('title'), 'error': 'job_id 为空'})
            continue

        detail_url = f"https://www.zhipin.com/job_detail/{job_id}.html"
        subprocess.run(
            [agent_browser, '--cdp', '9222', 'navigate', detail_url],
            capture_output=True, timeout=10
        )
        time.sleep(10)

        # 获取详情页 ID
        result = subprocess.run(['curl', '-s', 'http://localhost:9222/json'],
                                capture_output=True, text=True, timeout=5)
        pages = json.loads(result.stdout)
        detail_page_id = None
        for p in pages:
            if p.get('type') == 'page' and job_id in p.get('url', ''):
                detail_page_id = p['id']
                break

        if not detail_page_id:
            print(f"    错误: 无法找到详情页")
            error_log.append({'title': job.get('title'), 'job_id': job_id, 'error': '无法找到详情页'})
            continue

        detail_ws_url = f"ws://localhost:9222/devtools/page/{detail_page_id}"

        security_id = get_security_id_from_detail(detail_ws_url)
        print(f"    security_id: {security_id[:50] if security_id else '空'}...")

        description = get_job_description_from_detail(detail_ws_url)
        print(f"    职位描述长度: {len(description)}")

        close_tab(detail_page_id)
        time.sleep(0.5)

        all_jobs.append({
            '职位名称': job.get('title', ''),
            '薪资': job.get('salary', ''),
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

    # Phase 4: 保存 CSV
    print(f"\n[Phase 4] 保存 CSV...")
    date_str = datetime.now().strftime('%Y%m%d')
    time_str = datetime.now().strftime('%H%M')
    output_file = f'{output_dir}/jobs_data_{date_str}_{time_str}.csv'

    # 去重
    existing_job_ids = set()
    import glob
    for f in glob.glob(f'{output_dir}/jobs_data*.csv'):
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
        if not existing_job_ids:
            writer.writeheader()
        for job in new_jobs:
            writer.writerow(job)

    print(f"  新增: {len(new_jobs)} 条, 去重跳过: {skipped} 条")
    print(f"  文件: {output_file}")

    # 错误日志
    if error_log:
        import os
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

    # Phase 5: 质量报告
    print(f"\n[Phase 5] 质量报告")
    total_existing = len(existing_job_ids) + len(new_jobs)
    print(f"  本次新增: {len(new_jobs)} 条")
    print(f"  去重跳过: {skipped} 条")
    print(f"  累计总量: {total_existing} 条")

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
