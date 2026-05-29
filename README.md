# Boss直聘职位爬取 Skill

> 🕷️ 从 Boss 直聘批量提取职位详情（含 security_id、职位描述），支持 PUA 薪资解码和增量去重。

一个 [OpenClaw](https://github.com/openclaw/openclaw) Skill，帮助 AI Agent 或用户自动化爬取 Boss 直聘的职位数据。

---

## 功能特性

- 🔄 **智能滚动加载** — 使用 CDP 模拟真实鼠标滚轮，绕过 Boss 直聘反爬检测
- 🔐 **PUA 薪资解码** — 自动将 Boss 直聘的特殊 Unicode 字符还原为真实薪资数字
- 📋 **详情页深度提取** — 逐条打开详情页，提取 security_id 和完整职位描述
- 💾 **CSV 增量存储** — 自动去重，追加写入，绝不覆盖历史数据
- ✅ **质量报告** — 每次爬取后输出字段完整率和错误统计
- 🛡️ **两层容错** — 失败自动重试（+50% 等待时间），仍失败则跳过并记录错误日志

## 前置依赖

| 依赖 | 必需 | 说明 |
|------|------|------|
| Python 3.6+ | ✅ | 运行脚本 |
| websocket-client | ✅ | Python CDP 通信库 |
| CloakBrowser | ✅ | 反检测 Chromium 浏览器 |
| agent-browser | ⚠️ 推荐 | OpenClaw 浏览器自动化工具（纯 CDP 模式可不装） |

## 安装

### 1. 克隆仓库

```bash
git clone https://github.com/<你的用户名>/boss-resume-crawler.git
cd boss-resume-crawler
```

### 2. 安装 Python 依赖

```bash
pip3 install websocket-client
```

### 3. 安装 CloakBrowser

CloakBrowser 是一个反检测 Chromium 浏览器，用于绕过 Boss 直聘的自动化检测。

```bash
# 安装 npm 包
npm install cloakbrowser playwright-core

# 下载 Chromium（需要代理访问 GitHub）
export https_proxy=http://127.0.0.1:7890  # 根据你的代理配置调整
curl -L --max-time 600 -o /tmp/cloakbrowser-darwin-x64.tar.gz <下载链接>

# 解压到缓存目录
tar -xzf /tmp/cloakbrowser-darwin-x64.tar.gz -C ~/.cache/cloakbrowser/
```

> 📖 详细安装说明请参考 [CloakBrowser 官方文档](https://github.com/nickspaargaren/cloakbrowser)

### 4. 安装 agent-browser（可选）

```bash
npm install -g agent-browser
```

> agent-browser 提供 snapshot 等高级功能。如果不想安装，可使用纯 CDP 模式脚本。

## 使用方法

### 方式一：Agent 自主模式（推荐少量职位）

将此 skill 安装到 OpenClaw 的 skills 目录后，直接对 Agent 说：

```
帮我爬取这个 Boss 直聘页面的职位数据：<URL>
```

Agent 会自动：
1. 检查依赖和登录状态
2. 滚动加载列表页
3. 逐条提取详情页数据
4. 保存 CSV 并输出质量报告

**适合场景：** 职位数量 < 50 条，需要灵活判断页面状态。

### 方式二：脚本批量模式（推荐大量职位）

```bash
# 使用 agent-browser 模式（推荐）
python3 scripts/boss_extract_final.py --output ~/Desktop/jobs --max-scroll 50

# 使用纯 CDP 模式（不依赖 agent-browser）
python3 scripts/boss_extract_pure.py --output ~/Desktop/jobs --max-scroll 50
```

**参数说明：**

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--output`, `-o` | 当前目录 | CSV 输出目录 |
| `--max-scroll` | 100 | 列表页最大滚动次数 |

**适合场景：** 职位数量大（几百条），跑完收结果。

### 两种模式对比

| | Agent 自主模式 | 脚本批量模式 |
|---|---|---|
| **执行者** | OpenClaw Agent 实时操作 | Python 脚本一次性跑完 |
| **速度** | 慢（逐步执行） | 快（批量处理） |
| **灵活性** | 高（异常实时处理） | 低（按预设逻辑） |
| **依赖** | agent-browser | agent-browser 或纯 CDP |

## 启动 CloakBrowser

**每次爬取前都需要启动 CloakBrowser 并登录 Boss 直聘：**

```bash
# 启动浏览器（有头模式，必须）
open ~/.cache/cloakbrowser/Chromium.app --args \
  --remote-debugging-port=9222 \
  "--remote-allow-origins=*" \
  --user-data-dir=<你的浏览器数据目录> \
  "https://www.zhipin.com/web/geek/jobs?query=总经理助理&city=101010100"
```

> ⚠️ **必须使用有头模式**（能看到浏览器窗口），headless 模式会被 Boss 直聘拦截。

启动后在浏览器中手动扫码登录，登录态会保存在浏览器 profile 中。

## 输出示例

### CSV 字段

| 字段名 | 说明 | 示例 |
|--------|------|------|
| 职位名称 | 职位标题 | 总经理助理 |
| 薪资 | PUA 解码后的真实薪资 | 15-20K·13薪 |
| 经验要求 | 工作经验 | 5-10年 |
| 学历要求 | 最低学历 | 本科 |
| 公司名称 | 招聘公司 | 某某科技有限公司 |
| 城市 | 工作城市 | 上海 |
| 区域 | 具体区域 | 浦东新区 |
| job_id | Boss 直聘职位 ID | abc123def456 |
| security_id | 安全标识（用于打招呼等操作） | a1b2c3d4e5f6... |
| 职位描述 | 完整的岗位职责和任职要求 | 负责协助总经理... |
| 创建日期 | 爬取时间 | 2026-05-28 15:30 |

### 质量报告

```
[Phase 5] 质量报告
  本次新增: 45 条
  去重跳过: 3 条
  累计总量: 128 条
  字段完整率: job_id 100% | security_id 98% | 职位描述 96%
  错误: 2 条
```

## 数据校验标准

| 字段 | 校验规则 |
|------|---------|
| job_id | 非空，≥ 20 字符 |
| security_id | 非空，≥ 30 字符 |
| 薪资 | 包含 "K" |
| 职位描述 | 非空，≥ 100 字符 |
| 公司名称 | 非空，≥ 2 字符 |

## 反爬注意事项

| 项目 | 要求 | 原因 |
|------|------|------|
| 浏览器模式 | 有头模式 | headless 会被拦截 |
| 滚动方式 | CDP `Input.dispatchMouseEvent` | 普通 scroll 事件不触发加载 |
| 滚动间隔 | 1.5-3 秒随机 | 模拟人类阅读节奏 |
| 详情页等待 | ≥ 20 秒 | 不足会导致数据为空 |
| 浏览器参数 | `--remote-allow-origins=*` | 否则 CDP 连接被拒绝 |

## 常见问题

### Q: CDP 连接失败？

确认 CloakBrowser 已启动且端口 9222 可用：
```bash
curl -s http://localhost:9222/json
```
返回页面列表即正常。如果为空，检查浏览器是否启动、端口是否正确。

### Q: 职位描述全部为空？

详情页等待时间不足。脚本默认等待 20 秒，如果网络较慢，可修改脚本中的 `time.sleep(20)` 增加等待时间。

### Q: 滚动后职位数量不变？

- 检查是否使用了 CDP `Input.dispatchMouseEvent`（而非普通 scroll）
- Boss 直聘可能有加载上限，连续 3 次数量不变即停止
- 网络问题也可能导致加载失败

### Q: 被 Boss 直聘风控拦截？

- 确认使用有头模式（非 headless）
- 减少滚动频率，增加等待时间
- 不要同时开多个爬取进程
- 使用不同的浏览器 profile

### Q: CSV 打开乱码？

CSV 使用 `utf-8-sig` 编码。用 Excel 打开时选择 UTF-8 编码，或直接用文本编辑器/WPS 打开。

## 项目结构

```
boss-resume-crawler/
├── SKILL.md                    # OpenClaw Skill 入口文件
├── README.md                   # 本文档
├── LICENSE                     # MIT 开源协议
├── references/
│   ├── sop.md                  # 详细操作流程（SOP）
│   ├── data-spec.md            # 数据字段规格和选择器
│   └── error-handling.md       # 错误处理方案
└── scripts/
    ├── boss_extract_final.py   # 主脚本（agent-browser 模式）
    └── boss_extract_pure.py    # 备用脚本（纯 CDP 模式）
```

## License

[MIT](LICENSE)

## 致谢

- [OpenClaw](https://github.com/openclaw/openclaw) — AI Agent 运行时框架
- [CloakBrowser](https://github.com/nickspaargaren/cloakbrowser) — 反检测浏览器
- [agent-browser](https://github.com/nickspaargaren/agent-browser) — 浏览器自动化工具
