# 详细 SOP

## SOP-0：登录状态检查

> 用于所有 Phase 之前。BOSS 直聘未登录时只能看到有限职位，且无法获取完整数据。

### 执行步骤

```bash
# 1. 获取页面快照
snapshot=$(agent-browser --cdp 9222 snapshot -i --timeout 8000 2>/dev/null)

# 2. 检查登录状态
if echo "$snapshot" | grep -qE "登录/注册|立即登录|我要找工作"; then
  echo "⚠️ 未登录状态"
  echo "请在 CloakBrowser 浏览器中手动扫码登录 BOSS 直聘"
  echo "登录完成后请告知我，我再继续执行"
  # 暂停，等待用户确认
  exit 0
fi

# 3. 确认已登录
username=$(echo "$snapshot" | grep -oE 'link "[^"]+"' | head -20 | grep -vE "首页|职位|公司|校园|海归|APP|消息|简历|推荐|搜索|地图" | head -1)\necho "✅ 已登录: $username"
```

### 判断标准

| 状态 | 特征 | 处理 |
|------|------|------|
| ❌ 未登录 | snapshot 中出现「登录/注册」「立即登录」「我要找工作」 | 暂停，等待用户登录 |
| ✅ 已登录 | snapshot 中出现用户名（如「陈新彦」）或「简历」「消息」链接 | 继续执行 |

### 注意事项

- 登录态保存在浏览器 profile 中，通常无需每次登录
- 如果页面跳转到登录页，说明 session 过期，需重新登录
- **绝对不要尝试自动登录**（扫码需人工操作）

---

## SOP-1：列表页滚动策略

> 用于 Phase 1。BOSS 直聘反爬机制检测快速滚动，必须模拟人类鼠标滚轮。

### 关键发现

- ❌ `agent-browser scroll bottom` 无效（仅触发 scroll 事件，不加载新内容）
- ✅ `CDP Input.dispatchMouseEvent mouseWheel` 有效（模拟真实鼠标滚轮）

### 执行步骤（Python 脚本）

```python
import json
import websocket
import time
import subprocess

def get_ws_url():
    result = subprocess.run(['curl', '-s', 'http://localhost:9222/json'], capture_output=True, text=True, timeout=5)
    pages = json.loads(result.stdout)
    for p in pages:
        if p.get('type') == 'page' and 'zhipin' in p.get('url', ''):
            return p['webSocketDebuggerUrl']
    return None

def cdp_scroll(ws_url, delta_y=800):
    """使用 CDP Input.dispatchMouseEvent 模拟鼠标滚轮"""
    ws = websocket.create_connection(ws_url, timeout=10)
    command = {
        "id": 1,
        "method": "Input.dispatchMouseEvent",
        "params": {
            "type": "mouseWheel",
            "x": 500,
            "y": 400,
            "deltaX": 0,
            "deltaY": delta_y
        }
    }
    ws.send(json.dumps(command))
    ws.recv()
    ws.close()

def count_jobs(ws_url):
    """统计当前页面职位数"""
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

# 主流程
ws_url = get_ws_url()

# 回到顶部
ws = websocket.create_connection(ws_url, timeout=10)
ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": "window.scrollTo(0, 0)", "returnByValue": True}}))
ws.recv()
ws.close()
time.sleep(1)

# 滚动加载
prev_count = 0
same_count = 0

for i in range(100):  # 最多滚动100次
    cdp_scroll(ws_url, 800)
    time.sleep(1.5 + (i % 3) * 0.5)  # 随机等待1.5-3秒
    
    if (i + 1) % 5 == 0:
        count = count_jobs(ws_url)
        print(f"第 {i+1} 次滚动: {count} 条职位")
        
        if count == prev_count:
            same_count += 1
            if same_count >= 3:
                print("连续3次数量不变，停止")
                break
        else:
            same_count = 0
        prev_count = count
```

### 启动浏览器要求

必须添加 `--remote-allow-origins=*` 参数，否则 CDP WebSocket 连接会被拒绝：

```bash
open ~/.cache/cloakbrowser/Chromium.app --args \
  --remote-debugging-port=9222 \
  "--remote-allow-origins=*" \
  --user-data-dir=<你的浏览器数据目录> \
  "<列表页URL>"
```

### 验证加载

```bash
# 使用 agent-browser snapshot 统计
agent-browser --cdp 9222 snapshot -i --timeout 8000 2>/dev/null | grep -cE "listitem.*K.*(年|经验)"
```

也可以用纯 JS 统计：
```bash
curl -s http://localhost:9222/json | python3 -c "import json,sys; pages=json.load(sys.stdin); [print(p['webSocketDebuggerUrl']) for p in pages if 'jobs' in p.get('url','')]"
```

### 上限检测

- 连续 3 次滚动后数量不变 → 判定到达上限
- 记录最后一条职位（公司名 + 岗位名），供用户人工校验

### 测试结果

| 滚动次数 | 职位数 |
|----------|--------|
| 0（初始） | 15 |
| 10 | 90 |
| 20 | 405 |
| 30 | 540 |
| 50 | 810 |

---

## SOP-2：详情页数据提取

> 用于 Phase 3。逐条打开详情页，提取 security_id 和职位描述。

### 打开详情页

```python
import urllib.request

def open_new_tab(url):
    req = urllib.request.Request(f'http://localhost:9222/json/new?{url}', method='PUT')
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.loads(response.read().decode('utf-8')).get('id')
```

### 等待与校验

```python
time.sleep(20)  # 强制等待 20 秒（关键！）

# 检测页面加载完成
ready_state = cdp_execute(ws_url, "document.readyState")
if ready_state != 'complete':
    close_tab(page_id)
    continue  # 跳过当前职位
```

### 提取 security_id

```javascript
(function() {
    var scripts = document.querySelectorAll('script');
    for (var i = 0; i < scripts.length; i++) {
        var text = scripts[i].innerText || scripts[i].textContent || '';
        // 匹配 securityId:'xxx' 或 securityId:"xxx" 或 securityId: 'xxx'
        var match = text.match(/securityId['":\s]+['"]([^'"]+)['"]/);
        if (match) return match[1];
    }
    return '';
})()
```

### 提取职位描述

```javascript
(function() {
    var text = document.body.innerText;
    var start = text.indexOf('职位描述');
    if (start == -1) start = text.indexOf('岗位职责');
    if (start == -1) return '';

    var descStart = start + '职位描述'.length;
    var endMarkers = ['刚刚活跃', '工作地址', '查看更多信息', '在线状态', '投诉举报', '相似职位'];
    var end = text.length;
    for (var i = 0; i < endMarkers.length; i++) {
        var pos = text.indexOf(endMarkers[i], start);
        if (pos != -1 && pos < end) end = pos;
    }
    return text.substring(descStart, end).trim();
})()
```

### 关闭详情页（必须）

**提取完成后立即关闭 tab，不关闭会导致 tab 堆积，占用 CDP 连接资源。**

```python
subprocess.run(['curl', '-s', f'http://localhost:9222/json/close/{page_id}'],
              capture_output=True, timeout=5)
time.sleep(1)  # 等待 tab 完全关闭
```

> 新脚本 `boss_extract_cdp.py` 在 `finally` 块中确保关闭，即使提取失败也会关闭。

### 数据校验

提取后立即校验，不合格则跳过：
- `security_id` 长度 < 30 → 跳过
- 职位描述长度 < 100 → 警告但保留

---

## SOP-3：CSV 增量存储

> 用于 Phase 4。每次爬取生成新文件，增量追加，绝不覆盖。

### 文件命名

```
jobs_data_{YYYYMMDD}_{HHMM}.csv
```

路径：`<输出目录>/`（由 `--output` 参数指定，默认当前目录）

### 去重逻辑

```python
import glob

existing_job_ids = set()
for f in glob.glob('<输出目录>/jobs_data*.csv'):  # 替换为实际输出目录
    with open(f, 'r', encoding='utf-8-sig') as fh:
        for row in csv.DictReader(fh):
            if row.get('job_id'):
                existing_job_ids.add(row['job_id'])

new_jobs = [j for j in all_jobs if j['job_id'] not in existing_job_ids]
```

### 追加写入

```python
with open(output_file, 'a', newline='', encoding='utf-8-sig') as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    if not file_exists:
        writer.writeheader()
    for job in new_jobs:
        writer.writerow(job)
```

**⚠️ 禁止使用 `'w'` 模式！必须用 `'a'` 追加模式。**

### 即时写入（新策略）

**每提取 1 条立即追加写入主 CSV 文件**，不再使用批次缓存。

优势：
- 进程中断最多丢失 1 条数据（当前正在提取的那条）
- 无需手动合并批次文件
- 实时可见爬取进度（CSV 行数 = 已完成条数）

---

## SOP-4：CDP 工具函数

> 供详情页提取使用的核心工具函数。

### cdp_execute

```python
import websocket, json

def cdp_execute(ws_url, js_code, timeout=15):
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
```

### 获取页面 ID

```python
def get_list_page_id():
    result = subprocess.run(['curl', '-s', 'http://localhost:9222/json'],
                          capture_output=True, text=True, timeout=5)
    pages = json.loads(result.stdout)
    for p in pages:
        if 'jobs?' in p.get('url', ''):
            return p['id'], p['webSocketDebuggerUrl']
    return None, None
```


