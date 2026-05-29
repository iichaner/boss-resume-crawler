# 数据字段规格

## 必要字段（缺失即失败）

| 字段名 | 类型 | 校验标准 | 提取来源 |
|--------|------|---------|---------|
| job_id | Text | 非空，>=20 字符 | 列表页 href 正则 `/job_detail/(.+?)\.html` |
| security_id | Text | 非空，>=30 字符 | 详情页 script 标签 |
| 薪资 | Text | 包含 "K" | 列表页 `.job-salary`，需 PUA 解码 |
| 职位描述 | Text | 非空，>=100 字符 | 详情页 body.innerText |
| 公司名称 | Text | 非空，>=2 字符 | 列表页 `.boss-name` link text |

## 普通字段

| 字段名 | 提取来源 |
|--------|---------|
| 职位名称 | 列表页 `.job-name` link text |
| 经验要求 | 列表页 `.tag-list li`，匹配 `\d+-\d+年` |
| 学历要求 | 列表页 `.tag-list li`，匹配 `本科\|大专\|硕士\|博士\|学历不限` |
| 城市 | 列表页 `.company-location`，按 `·` 拆分取第一段 |
| 区域 | 列表页 `.company-location`，按 `·` 拆分取剩余 |
| 招聘者 | 详情页（如有） |
| 招聘者职位 | 详情页（如有） |
| 创建日期 | 爬取时间，格式 `YYYY-MM-DD HH:MM` |

## CSV 字段顺序

```
职位名称,薪资,经验要求,学历要求,公司名称,城市,区域,job_id,security_id,职位描述,创建日期
```

## PUA 薪资解码

Boss 直聘使用 PUA Unicode 字符隐藏真实薪资数字：

```
0xe031 → 0    0xe036 → 5
0xe032 → 1    0xe037 → 6
0xe033 → 2    0xe038 → 7
0xe034 → 3    0xe039 → 8
0xe035 → 4    0xe03a → 9
```

Python 解码函数（已内嵌于脚本）：

```python
PUA_MAP = {
    0xe031: '0', 0xe032: '1', 0xe033: '2', 0xe034: '3', 0xe035: '4',
    0xe036: '5', 0xe037: '6', 0xe038: '7', 0xe039: '8', 0xe03a: '9'
}
def decode_pua(text):
    if not text: return text
    return ''.join(PUA_MAP.get(ord(c), c) for c in text)
```

## 页面选择器（当前有效）

| 元素 | 选择器 | 备注 |
|------|--------|------|
| 职位名称 | `.job-name` | ✅ |
| 薪资 | `.job-salary` | ✅ 需 PUA 解码 |
| 公司名称 | `.boss-name` | ✅ |
| 地区 | `.company-location` | ✅ |
| 经验/学历 | `.tag-list li` | ✅ |

**已失效选择器**：`.salary`, `.job-title`, `.company-name a`, `.area`
