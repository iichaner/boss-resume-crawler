---
name: boss-resume-crawler
description: "从 Boss 直聘批量爬取职位详情（含 security_id、职位描述），支持 PUA 薪资解码和增量去重。"
metadata:
  {
    "openclaw":
      {
        "emoji": "🕷️",
        "requires": { "bins": ["curl", "python3"] },
      },
  }
read_when:
  - 用户要求爬取 Boss 直聘职位
  - 用户提到"爬取"、"抓取"、"JD 数据"、"Boss 直聘"
  - 用户要求批量获取职位详情或 security_id
allowed-tools: Bash,Read,Write,exec
---

# Boss直聘职位爬取

## 快速验证（环境 OK 后立即跑通）

```bash
# 一条命令验证：爬取 5 条职位，输出到 /tmp
python3 scripts/boss_extract_cdp.py --max-scroll 3 --max-jobs 5 --output /tmp/boss_test
```

验证通过 → 正式爬取。失败 → 检下方依赖和 CDP 连接。

---

## 性能基准（实测）

| 指标 | 数值 |
|------|------|
| 单页滚动加载 | ~2 秒/次 |
| 详情页提取 | ~22 秒/条（含 20 秒等待 + 随机波动） |
| 100 条职位总耗时 | ~40 分钟 |
| 500 条职位总耗时 | ~3 小时 |

> 详情页耗时主要由等待时间决定（20 秒/条），这是 Boss 直聘客户端渲染的硬限制。

---

## 首次使用：依赖检查（必须）

**在执行任何爬取操作之前，必须先检查以下依赖是否就绪。缺失时提示用户安装。**

### 检查脚本

```bash
# 1. Python3
python3 --version 2>/dev/null || echo "❌ 未安装 Python3 → https://www.python.org/downloads/"

# 2. websocket-client（Python 库）
python3 -c "import websocket" 2>/dev/null || echo "❌ 缺少 websocket-client → pip3 install websocket-client"

# 3. CDP 连接（CloakBrowser 是否启动）
curl -s http://localhost:9222/json >/dev/null 2>&1 || echo "❌ CDP 未连接，请先启动 CloakBrowser（见下方说明）"
```

### CloakBrowser 启动方法

CloakBrowser 是一个反检测 Chromium 浏览器，用于绕过 Boss 直聘的自动化检测。

**安装：**
```bash
# 安装依赖
npm install cloakbrowser playwright-core

# 下载 Chromium（需要代理）
export https_proxy=http://127.0.0.1:7890  # 根据你的代理配置
curl -L --max-time 600 -o /tmp/cloakbrowser-darwin-x64.tar.gz <下载链接>
tar -xzf /tmp/cloakbrowser-darwin-x64.tar.gz -C ~/.cache/cloakbrowser/

# 设置环境变量
export CLOAKBROWSER_BINARY_PATH=~/.cache/cloakbrowser/Chromium.app/Contents/MacOS/Chromium
```

> CloakBrowser 项目地址：https://github.com/nickspaargaren/cloakbrowser

**启动（有头模式，必须）：**
```bash
open ~/.cache/cloakbrowser/Chromium.app --args \
  --remote-debugging-port=9222 \
  "--remote-allow-origins=*" \
  --user-data-dir=<你的浏览器数据目录> \
  "<Boss直聘列表页URL>"
```

> ⚠️ Boss 直聘会检测 headless 模式，**必须使用有头模式**（能看到浏览器窗口）。

### 依赖就绪标志

所有 ✅ 后方可执行爬取：
- [ ] Python3 可用
- [ ] websocket-client 已安装
- [ ] CDP 连接正常（`curl -s http://localhost:9222/json` 返回页面列表）
- [ ] 用户已登录 Boss 直聘（见下方登录检查）

---

## 输入要求

- **必须提供** Boss 直聘列表页 URL（含 `zhipin.com/web/geek/jobs`）
- 未提供 URL 时必须主动询问，不要自行构造

## 登录状态检查（必须在 Phase 1 之前执行）

打开列表页后，**首先检查登录状态**，未登录则暂停等待人类操作：

```bash
snapshot=$(agent-browser --cdp 9222 snapshot -i --timeout 8000 2>/dev/null)
if echo "$snapshot" | grep -qE "登录/注册|立即登录|登录"; then
  echo "⚠️ 未登录状态，请手动扫码登录"
  echo "登录完成后告知我，我再继续"
fi
```

**判断逻辑：**
- ❌ 出现「登录/注册」「立即登录」「我要找工作」等链接 → 未登录
- ✅ 出现用户名或用户头像链接 → 已登录

**未登录时的处理：**
1. 暂停所有爬取工作
2. 提示用户：「页面显示未登录，请在浏览器中扫码登录，完成后告知我」
3. 等待用户明确说「已登录」或「继续」
4. **⚠️ 登录后必须重新导航到目标列表页 URL**（登录完成后 Boss 直聘会自动跳转到 `https://www.zhipin.com/web/geek/jobs` 通用页，不是你的搜索页）
5. 使用 CDP 导航到目标 URL 后，再执行后续 Phase

**登录后重新导航（必须）：**

```python
import websocket, json, time, urllib.request

# 获取当前页面
pages = json.loads(urllib.request.urlopen('http://localhost:9222/json').read())
target_page = None
for p in pages:
    if p.get('type') == 'page' and 'zhipin' in p.get('url', ''):
        target_page = p
        break

if target_page:
    ws = websocket.create_connection(target_page['webSocketDebuggerUrl'], timeout=10)
    # 导航到目标搜索 URL
    ws.send(json.dumps({
        'id': 1,
        'method': 'Page.navigate',
        'params': {'url': '<你的目标列表页URL>'}
    }))
    ws.recv()
    ws.close()
    time.sleep(3)  # 等待页面加载
    print(f'✅ 已导航到目标页面')
```

或使用命令行：
```bash
# 通过 CDP 导航
curl -s http://localhost:9222/json | python3 -c "
import json,sys,urllib.request
pages = json.load(sys.stdin)
for p in pages:
    if 'zhipin' in p.get('url',''):
        page_id = p['id']
        break
urllib.request.urlopen(f'http://localhost:9222/json/close/{page_id}')
" 
# 关闭后用脚本重新打开目标 URL
```

---

## 执行流程

### Phase 0：创建工作目录（必须首先执行）

**在执行任何爬取操作之前，第一件事：在项目路径下创建以日期命名的工作文件夹。**

```bash
# 获取今天日期
TODAY=$(date +%Y-%m-%d)

# 创建工作目录（Obsidian Vault 项目路径）
PROJECT_DIR="/Users/ii/Obsidian Vault/7_Project/${TODAY}-boss直聘JD抓取"
mkdir -p "${PROJECT_DIR}"
```

**规则：**
- 工作目录命名格式：`YYYY-MM-DD-boss直聘JD抓取`
- 路径固定在 `/Users/ii/Obsidian Vault/7_Project/` 下
- 所有后续产出（CSV、错误日志、临时文件）全部放入此目录
- **禁止使用 `/tmp/` 或其他临时路径**

### Phase 1：列表页滚动加载
使用 CDP `Input.dispatchMouseEvent mouseWheel` 模拟真实鼠标滚轮（agent-browser scroll 无效）。最多 **200** 次滚动，随机等待 1.5-3.5 秒，**连续 5 次数量不变 + 至少 3 次确认已到页面底部**才停止。启动浏览器需添加 `"--remote-allow-origins=*"` 参数。
详见 [references/sop.md](references/sop.md) SOP-1。

### Phase 2：职位列表提取
通过 CDP 执行 JavaScript 从 `.job-card-wrap` 提取职位基础信息，薪资需 PUA 解码（0xe031→0, ..., 0xe03a→9）。选择器和字段规格见 [references/data-spec.md](references/data-spec.md)。

### Phase 3：详情页逐条爬取
CDP 开新 tab → **等待 20 秒 + 随机波动（0-3 秒）** → 检查 `readyState` → 提取 security_id + 职位描述 → **立即关闭 tab**。

> **为什么必须等 20 秒？** Boss 直聘详情页使用客户端渲染（React/Vue），CDP 打开新 tab 后需要等待
> JS 框架完成 hydration + API 请求返回数据。实测 <15 秒约 40% 概率拿到空数据，<10 秒几乎必定为空。

**为什么每次都要关闭 tab？** 不关闭会导致 tab 堆积，占用 CDP 连接资源，后续操作超时。

详见 [references/sop.md](references/sop.md) SOP-2、SOP-4。

### Phase 4：CSV 增量存储
**每提取 1 条立即追加写入 CSV**（不缓存到内存）。追加模式（`'a'`）+ 跨文件 job_id 去重。**禁止使用 `'w'` 覆盖模式。**
详见 [references/sop.md](references/sop.md) SOP-3。

### Phase 5：质量报告
输出本次新增/去重跳过/累计总量/字段完整率/错误详情。格式见下方「输出规范」。

## 数据校验标准

| 字段 | 校验 |
|------|------|
| job_id | 非空，>=20 字符 |
| security_id | 非空，>=30 字符 |
| 薪资 | 包含 "K"（实习岗日薪除外） |
| 职位描述 | 非空，>=100 字符 |
| 公司名称 | 非空，>=2 字符 |

## 输出规范

- **工作目录**：`/Users/ii/Obsidian Vault/7_Project/YYYY-MM-DD-boss直聘JD抓取/`（独立使用时默认）
  - **boss-pipeline 场景**：输出目录改为统一根目录下的 `01_raw/`（即 `workspace/boss直聘一体化/YYYY-MM-DD-搜索词/01_raw/`），通过 `--output` 参数传入，脚本本身无需改动
- CSV 路径：`<输出目录>/jobs_data_{YYYYMMDD}_{HHMM}.csv`
- 错误日志：`<输出目录>/temp/error_log.csv`
- 质量报告格式：
  - 本次新增：N 条
  - 去重跳过：N 条
  - 累计总量：N 条
  - 字段完整率：job_id X% | security_id X% | 职位描述 X%
  - 错误：N 条（详情见 error_log.csv）

## 错误处理

两层 Fallback：
1. **第一层**：增加等待时间重试（+5 秒/次），同一位置最多 3 次
2. **第二层**：跳过错误职位，记录到 `temp/error_log.csv`，继续下一个

### 常见错误速查

| 现象 | 原因 | 解决方案 |
|------|------|---------|
| WebSocket 连接超时 | CDP 长连接被断开 | 新脚本已修复（每次独立连接） |
| security_id 为空 | 详情页等待时间不足 | 增加 `--base-wait 25` |
| 职位描述全部为空 | 等待 <15 秒 | 确保等待 ≥20 秒 |
| ACCOUNT_RISK | 被风控拦截 | 切换 CloakBrowser profile（用新 --user-data-dir） |
| CDP 连接被拒 | 缺少 `--remote-allow-origins=*` | 启动时添加该参数 |
| 滚动后只有 15 条 | agent-browser scroll 无效 | 脚本已使用 CDP `Input.dispatchMouseEvent mouseWheel` |
| CSV 数据被覆盖 | 用了 `'w'` 模式 | 脚本已使用 `'a'` 追加模式 |
| PUA 薪资乱码 | 未解码 | 脚本已内置 decode_pua 函数 |
| 进程中断后数据丢失 | 旧脚本无批次缓存 | 新脚本每条立即写入 CSV |

已知问题和修复方案详见 [references/error-handling.md](references/error-handling.md)。

## 脚本

| 脚本 | 用途 | 运行说明 |
|------|------|---------|
| `scripts/boss_extract_cdp.py` | **默认脚本**：纯 CDP 模式，反爬优化 | 依赖：websocket-client。推荐使用 |
| `scripts/boss_extract_pure.py` | 旧版备用：纯 CDP 模式（无反爬优化） | 依赖：websocket-client。不推荐 |
| `scripts/boss_extract_final.py` | agent-browser 模式 | 依赖：agent-browser, websocket-client |

**默认脚本参数：**
```bash
# 指定输出目录（必须是 Phase 0 创建的工作目录）
python3 scripts/boss_extract_cdp.py --output "/Users/ii/Obsidian Vault/7_Project/2026-07-06-boss直聘JD抓取"

# 限制滚动次数（默认200，建议保持 200 以确保充分滚动）
python3 scripts/boss_extract_cdp.py --max-scroll 200

# 限制爬取条数（默认全部）
python3 scripts/boss_extract_cdp.py --max-jobs 10

# 调整详情页等待秒数（默认20）
python3 scripts/boss_extract_cdp.py --base-wait 25

# 组合使用
python3 scripts/boss_extract_cdp.py --output "/Users/ii/Obsidian Vault/7_Project/2026-07-06-boss直聘JD抓取" --max-scroll 200
```

**脚本设计原则（反爬）：**
- 每次 CDP 操作新建 WebSocket 连接，用完即关（防超时）
- 每提取 1 条立即追加写入 CSV（防数据丢失）
- 详情页提取后立即关闭 tab（防 tab 堆积）
- 所有等待时间加随机波动（反爬）
- 失败自动重试，指数退避（容错）

优先使用 Agent 自主执行（实时处理异常更灵活），职位数量大时运行脚本批量处理。

## 反爬注意事项

- **必须使用有头模式**（headless 会被拦截）
- **必须使用 CloakBrowser**（反检测 Chromium）
- **滚动必须使用 CDP `Input.dispatchMouseEvent mouseWheel`**（agent-browser scroll 无效）
- 启动浏览器需添加 `"--remote-allow-origins=*"` 参数
- 滚动间隔 1.5-3.5 秒（随机化），模拟人类阅读节奏
- 详情页等待 20 秒 + 随机波动（0-3 秒）+ 重试退避（+5 秒/次）
- 每次 CDP 操作独立连接，避免长连接被检测
- 详情页提取后立即关闭 tab，避免大量 tab 堆积
