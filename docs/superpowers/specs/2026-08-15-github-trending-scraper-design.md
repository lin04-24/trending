# GitHub 本周热门项目爬取与 QQ 邮箱推送系统 — 设计计划书

- **日期**：2026-08-15
- **状态**：待审阅
- **项目目录**：`F:\1\trending`

---

## 1. 项目目标

编写一个 Python 脚本，运行于云服务器（1Panel 面板），周期性完成：

1. 爬取 [GitHub Trending (weekly)](https://github.com/trending?since=weekly) 全部项目（约 25 个）
2. 将项目信息存入服务器本地 MySQL Docker 容器（`1Panel-mysql-wMbD`，MySQL 8.4.11）
3. 对比数据库做**永久去重**（键 = 作者/项目名）：新项目调 LLM 生成中文简介后入库；已存在项目跳过生成、更新 `last_seen`，在终端与日志中提示"已存在于数据库"
4. 每次运行后通过 QQ 邮箱 SMTP（465 SSL）推送渲染好的 **HTML 邮件**（手机端优先），内容含：当前上海时间、GitHub 链接、项目类型（语言 + 中文分类）、新项目**大介绍**（详细中文介绍，源自 README，不含安装教程）；老项目在邮件末尾简列（链接 + 小介绍）

## 2. 需求确认记录

| 事项 | 决定 |
|---|---|
| 中文简介生成 | OpenAI 兼容 LLM API（base_url、模型名从 .env 读取） |
| 运行环境 | 云服务器 Python 3 + 1Panel 脚本库 + 计划任务定时执行 |
| 推送范围 | 新项目详推大介绍；老项目邮件末尾简列（链接+小介绍） |
| GitHub 访问 | 服务器直连 + GitHub PAT（.env 配置） |
| 重复提示 | 终端打印汇总（N 个已存在），详情写日志文件 |
| 项目类型 | 编程语言 + LLM 归类中文分类（如"AI 工具"） |
| 去重判定 | 永久去重：只要库中存在该 作者/项目名 即为重复，一次入库终身去重 |
| 中文 README | 项目自带中文 README（如 README.zh-CN.md）时优先直接使用，LLM 只做精简 |
| 实现方案 | 方案 B：小型模块化包（5 模块 + 入口） |

## 3. 架构与数据流

### 3.1 目录结构

```
F:\1\trending\
├── main.py            # 入口：编排六步流水线，支持 --dry-run
├── scraper.py         # 爬 trending 页 + 拉 README（GitHub API + Token）
├── llm.py             # 调 OpenAI 兼容接口：大/小介绍、中文名、中文分类
├── db.py              # MySQL 读写、自动建库建表、查重
├── mailer.py          # QQ SMTP 推送 + HTML 模板渲染（内联 CSS）
├── config.py          # 读 .env，集中管理配置 + 校验必填项
├── requirements.txt
├── env                # 现有环境变量文件（沿用键名，见 §8）
├── .gitignore         # 忽略 env、logs/、data/
└── logs/              # 滚动日志（trending-YYYY-MM-DD.log）
```

### 3.2 六步流水线

```
1. config.load() 读 .env（MySQL/SMTP/LLM/GitHub Token），缺失必填项直接报错退出
2. scraper.fetch_trending() 解析 github.com/trending?since=weekly
   → [{author, repo_name, language, description, stars_week, stars_total}]
3. db.partition(new, existing) 按唯一键 repo_key = 'author/repo_name' 分组：
   ├─ 新项目：scraper.fetch_readme() 拉 README（中文优先）
   │   → llm.summarize() 一次调用产出 {中文名, 小介绍, 中文分类, 大介绍}
   │   → db.upsert() 入库（含 first_seen/last_seen = 当前上海时间）
   └─ 老项目：db.touch_last_seen() 更新 last_seen，跳过 LLM
4. mailer.render_html() 渲染邮件（§6），QQ SMTP 465 SSL 发送
5. 终端一行汇总：本次爬取 N 个：新 X 个，已存在 Y 个，邮件已推送 ✓
6. logging 按日期写 logs/trending-YYYY-MM-DD.log（含每项目处理结果、LLM 耗时、邮件结果）
```

### 3.3 失败处理原则

**单项目失败不拖垮整体**：

| 故障点 | 处理 |
|---|---|
| trending 页拉取失败（重试 3 次后仍失败） | 致命：中止本次运行，写 ERROR 日志，不发邮件 |
| 单项目 README 拉取失败 / API 404 | 用英文 description 兜底送 LLM；仍失败则用 description 原文入库 |
| LLM 超时（60s）/解析失败 | 重试 1 次；仍失败则大介绍 = description 原文，中文分类 = "未分类"，正常入库推送 |
| 单项目入库失败 | 记 ERROR，其余项目照常；邮件照发 |
| 邮件发送失败 | 重试 2 次；仍失败记 ERROR 日志，终端明确提示。**数据库不回滚**（下次运行该项目按已存在处理，不重推大介绍） |

---

## 4. 数据库设计

数据库 `github_trending`，单表 `repos`（utf8mb4，Asia/Shanghai 时区）：

```sql
CREATE DATABASE IF NOT EXISTS github_trending
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS repos (
  id           INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  repo_key     VARCHAR(200) NOT NULL UNIQUE COMMENT '作者/项目名，去重键',
  author       VARCHAR(100) NOT NULL,
  repo_name    VARCHAR(100) NOT NULL,
  github_url   VARCHAR(160) NOT NULL,           -- https://github.com/{owner≤39}/{repo≤100} = 最长 158
  language     VARCHAR(50)  NULL,
  category     VARCHAR(50)  NULL COMMENT 'LLM 中文分类，如"AI 工具"',
  brief_intro  VARCHAR(300) NULL COMMENT '小介绍：中文名+用途，≤30字',
  full_intro   TEXT NULL COMMENT '大介绍：详细中文介绍，源自 README',
  first_seen   DATETIME NOT NULL COMMENT '首次爬到（上海时间）',
  last_seen    DATETIME NOT NULL COMMENT '最近一次出现在 trending',
  star_total   INT UNSIGNED DEFAULT 0,
  INDEX idx_last_seen (last_seen)
);
```

- 去重：`repo_key` 唯一索引；入库统一 `INSERT ... ON DUPLICATE KEY UPDATE last_seen = VALUES(last_seen), star_total = VALUES(star_total)`
- 自动初始化：首次运行自动建库建表，无需手动 init
- 连接：PyMySQL → `127.0.0.1:3306`，root + env 中的 `PANEL_DB_ROOT_PASSWORD`

---

## 5. 爬取与 LLM 细节

### 5.1 Trending 页爬取（scraper.py）

- `GET https://github.com/trending?since=weekly`，浏览器 UA，`timeout=30`
- BeautifulSoup 解析 `article.Box-row`：
  - `author/repo`：`h2 a` 的 href（`/owner/repo`）
  - `description`：`p.col-9`
  - `language`：`[itemprop="programmingLanguage"]`
  - 周星 / 总星：`href="/…/stargazers"` 链接计数与浮动条文本
- 重试 3 次，指数退避 + 抖动（2s → 4s → 8s ± 抖动）
- 解析结果为 0 个项目视为解析失败（页面改版告警），按致命错误处理

### 5.2 README 获取（scraper.py）

对每个**新项目**：

1. `GET /repos/{owner}/{repo}/readme`（GitHub API + Bearer Token）拿主 README（base64 → 原文）
2. 中文优先链：依次尝试 raw 地址 `README.zh-CN.md`、`README_zh.md`、`README-zh.md`、`README_CN.md`，任一 200 且含中文（正则 `[\u4e00-\u9fa5]`）则直接采用；全 404 则用主 README
3. 中文 README 存在时 LLM 只做精简（提示词去翻译指令）；英文 README 则翻译+精简
4. 截断到前 6000 字符送 LLM（保留开头动机/简介章节，天然避开多数安装章节位于尾部的情况；提示词同时明确"不含安装、构建、快速开始"）
5. README 完全拉不到：用 description 兜底

### 5.3 LLM 生成（llm.py）

一次调用同时产出四项（省 token、省时延）：

```json
{
  "中文名": "项目名",
  "小介绍": "名称+用途，≤30字",
  "中文分类": "如：AI 工具 / 前端框架 / 命令行工具 / 学习资源…",
  "大介绍": "项目是什么、解决什么问题、核心功能。300字内。不含安装/构建/快速开始等操作性内容"
}
```

- 客户端：`openai` 官方 SDK，`base_url = LLM_BASE_URL`，`api_key = LLM_API_KEY`，`model = LLM_MODEL`（均来自 .env）
- 提示词要求返回**严格 JSON**（response_format 若支持则用 `json_object`；不支持则提示词约束 + 正则提取 `{...}`）
- 超时 60s，失败重试 1 次
- 兜底链：中文 README 精简失败 → 英文翻译失败 → description 原文

---

## 6. 邮件设计（mailer.py）

### 6.1 发送

- `smtplib.SMTP_SSL` + `email.mime.multipart`：`multipart/alternative`（text/html + 纯文本备用）
- QQ 邮箱：`smtp.qq.com:465`，登录 env 的 `SEND_MAIL` / `SEND_KEY`（授权码）
- 收件人：`ACCEPT_MAIL`（支持逗号分隔多个）
- 邮件标题：`GitHub 周趋势 {新 X} 新项目 | {YYYY-MM-DD HH:mm 上海时间}`

### 6.2 HTML 模板（手机优先，QQ 客户端兼容）

- **全部内联 CSS**（QQ 邮箱客户端会剥离 `<style>` 标签），`max-width: 640px`，`width: 100%`，系统字体栈
- 结构：

```
┌────────────────────────────┐
│ 报告头：GitHub 本周趋势      │
│ 上海时间 + 新 X / 已存在 Y   │
├────────────────────────────┤
│ [新项目卡片] × N             │
│  中文名（大标题）             │
│  author/repo（英文小字）     │
│  [Python] [AI 工具] 徽章     │
│  GitHub 链接按钮             │
│  大介绍段落（详细中文）        │
│  首次收录时间（小字）          │
├────────────────────────────┪
│ 老项目区（灰底折叠列表）       │
│  • 链接 + 中文名/小介绍 × M  │
├────────────────────────────┤
│ 页脚：本邮件由脚本自动发送     │
┪════════════════════════════╝
```

- 老项目区：灰底，每行 `中文名 — 小介绍 [→GitHub]`，不推大介绍
- 兜底数据（LLM 失败项）照常展示，分类徽章显示"未分类"

## 7. 日志与终端输出

- `logs/trending-YYYY-MM-DD.log`，UTF-8，格式：`[时间] [级别] 模块 消息`
- 终端仅一行汇总：`本次爬取 25 个项目：新 3 个，已存在 22 个，邮件已推送 ✓`
- 1Panel 计划任务可捕获 stdout 与日志文件双通道

## 8. 配置文件

沿用现有 `env` 文件键名（不重命名），新增 LLM 与 GitHub Token：

```bash
# ===== MySQL（现有，沿用） =====
PANEL_DB_ROOT_PASSWORD='mysql_wCWYSF'
PANEL_APP_PORT_HTTP=3306
CONTAINER_NAME='1Panel-mysql-wMbD'

# ===== SMTP（现有，沿用） =====
SEND_MAIL=2813025815@qq.com
SEND_KEY=hbfkosyibpvjdeif
ACCEPT_MAIL=2780867912@qq.com
SEND_PORT=465

# ===== 新增 =====
GITHUB_TOKEN=ghp_xxxxxxxxxxxx
LLM_BASE_URL=https://api.xxx.com/v1
LLM_MODEL=deepseek-chat        # 例
LLM_API_KEY=sk-xxxxxxxxxxxx
```

- `config.py` 启动时校验必填键，缺失即报错退出并列出缺失项
- `.gitignore` 加入：`env`、`logs/`、`data/`、`conf/`、`__pycache__/`

## 9. 依赖

```
requests
beautifulsoup4
pymysql
openai
python-dotenv
```

均纯 Python 或轮子可用，服务器 `pip3 install -r requirements.txt` 一步装完，无系统依赖。

## 10. 部署步骤（服务器，1Panel）

1. 上传项目目录（rsync / 1Panel 文件管理）至如 `/opt/trending/`
2. 编辑 `env` 填入 GITHUB_TOKEN、LLM_BASE_URL、LLM_MODEL、LLM_API_KEY；确认 MySQL 容器已运行（已有）
3. `pip3 install -r requirements.txt`
4. 验证：`python3 -m scraper`（打印解析结果不落库）、`python3 -m db`（建库建表）、`python3 main.py --dry-run`
5. 1Panel → 计划任务 → 添加：脚本路径 `python3 /opt/trending/main.py`，建议每周一 09:00 执行
6. 验证首封邮件与数据库入库

## 11. 测试策略

- 模块独立验证：scraper / llm / db / mailer 各自支持 `python -m` 独立运行自检
- `main.py --dry-run`：完整流水线但不发邮件不写库（LLM 可选 --no-llm 用假数据代替）
- 部署前在本机（可直连 GitHub）验证爬取与邮件；数据库测试在服务器做
- 上线后首周人工核对邮件与库内数据一致性（新项目数、老项目数、中文简介质量）

## 12. 风险与边界

| 风险 | 影响 | 缓解 |
|---|---|---|
| GitHub 页面改版致解析失败 | 致命错误、不发邮件 | 解析 0 结果即告警日志；HTML 结构选择器集中定义便于快速修复 |
| LLM 生成质量不稳/超时 | 邮件中个别项目简介为英文原文 | 重试 1 次 + description 兜底；不影响入库与推送 |
| GitHub API 限速（有 Token 5000/h，充裕） | README 拉取失败 | description 兜底 |
| MySQL 容器未启动 | 致命错误退出 | 启动时 ping 不通即报错退出并提示 `docker start 1Panel-mysql-wMbD` |
| 服务器无法直连 GitHub | 致命错误退出 | 预先在服务器 curl 验证；如后续不稳定可加 PROXY_URL 可选配置 |
| 邮件进垃圾箱 | 邮件看不到 | 固定发件人+固定标题格式；如进垃圾箱在 QQ 邮箱设置白名单 |

## 13. 未来扩展（不在本期范围）

- 按语言/分类筛选推送（如只推 AI 类）
- Telegram / 企业微信等多渠道
- 前端展示页（历史项目检索）
- 项目热度变化追踪（周星数变化曲线）
