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
---

# Boss直聘职位爬取

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

# 4. agent-browser（可选，脚本主模式需要）
which agent-browser 2>/dev/null || echo "⚠️ 未安装 agent-browser（纯 CDP 模式可用，但推荐安装）→ npm install -g agent-browser"
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
3. 等待用户明确说「已登录」或「继续」后，再执行后续 Phase

---

## 执行流程

### Phase 1：列表页滚动加载
使用 CDP `Input.dispatchMouseEvent mouseWheel` 模拟真实鼠标滚轮（agent-browser scroll 无效）。最多 100 次滚动，随机等待 1.5-3 秒，连续 3 次数量不变则停止。启动浏览器需添加 `"--remote-allow-origins=*"` 参数。
详见 [references/sop.md](references/sop.md) SOP-1。

### Phase 2：职位列表提取
从 snapshot 解析 `listitem` 节点，薪资需 PUA 解码（0xe031→0, ..., 0xe03a→9）。选择器和字段规格见 [references/data-spec.md](references/data-spec.md)。

### Phase 3：详情页逐条爬取
CDP 开新 tab → **强制等待 20 秒** → 检查 `readyState` → 提取 security_id + 职位描述 → 关闭 tab。等待不足会导致数据为空。
详见 [references/sop.md](references/sop.md) SOP-2、SOP-4。

### Phase 4：CSV 增量存储
追加模式（`'a'`）+ job_id 去重 + 每 10 条批次缓存。**禁止使用 `'w'` 覆盖模式。**
详见 [references/sop.md](references/sop.md) SOP-3。

### Phase 5：质量报告
输出本次新增/去重跳过/累计总量/字段完整率/错误详情。格式见下方「输出规范」。

## 数据校验标准

| 字段 | 校验 |
|------|------|
| job_id | 非空，>=20 字符 |
| security_id | 非空，>=30 字符 |
| 薪资 | 包含 "K" |
| 职位描述 | 非空，>=100 字符 |
| 公司名称 | 非空，>=2 字符 |

## 输出规范

- CSV 路径：`<工作目录>/jobs_data_{YYYYMMDD}_{HHMM}.csv`
- 错误日志：同目录下 `temp/error_log.csv`
- 质量报告格式：
  - 本次新增：N 条
  - 去重跳过：N 条
  - 累计总量：N 条
  - 字段完整率：job_id X% | security_id X% | 职位描述 X%
  - 错误：N 条（详情见 error_log.csv）

## 错误处理

两层 Fallback：
1. **第一层**：增加 50% 等待时间重试，同一位置最多 3 次
2. **第二层**：跳过错误职位，记录到 `temp/error_log.csv`，继续下一个

已知问题和修复方案见 [references/error-handling.md](references/error-handling.md)。

## 脚本

| 脚本 | 用途 | 运行说明 |
|------|------|---------|
| `scripts/boss_extract_final.py` | 主脚本：agent-browser snapshot + CDP 详情页提取 | 依赖：agent-browser, websocket-client |
| `scripts/boss_extract_pure.py` | 备用：纯 CDP JS 提取，不依赖 agent-browser | 依赖：websocket-client。当 agent-browser 不可用时使用 |

**通用参数：**
```bash
# 指定输出目录（默认当前目录）
python3 scripts/boss_extract_pure.py --output ~/Desktop/jobs

# 限制滚动次数（默认100）
python3 scripts/boss_extract_pure.py --max-scroll 30

# 组合使用
python3 scripts/boss_extract_pure.py --output ~/Desktop/jobs --max-scroll 50
```

优先使用 Agent 自主执行（实时处理异常更灵活），职位数量大时运行脚本批量处理。

## 反爬注意事项

- 必须使用有头模式（headless 会被拦截）
- 滚动必须使用 CDP `Input.dispatchMouseEvent mouseWheel`（agent-browser scroll 无效）
- 启动浏览器需添加 `"--remote-allow-origins=*"` 参数
- 滚动间隔 1.5-3 秒，模拟人类阅读节奏
- 详情页等待不低于 20 秒
