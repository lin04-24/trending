# trending — GitHub 趋势爬取 + LLM 中文介绍 + QQ 邮箱推送

一个运行在 Linux 服务器上的 Python 定时任务：周期性爬取 [GitHub Trending](https://github.com/trending)，用 LLM 为每个**新出现**的项目生成中文介绍，永久去重后存入 PostgreSQL，并通过 QQ 邮箱推送一份手机端友好的 HTML 周报/日报邮件。

## 功能简介

- **趋势爬取**：解析 `github.com/trending`，支持 `daily / weekly / monthly` 三种周期（`--since` 参数），提取项目名、语言、简介、总星数与本周期新增星数；失败自动重试（指数退避 + 抖动）。
- **永久去重**：以 `作者/项目名` 为唯一键存入 PostgreSQL，一次入库终身去重；老项目仅更新 `last_seen` 与星数，不重复调用 LLM。首次运行自动建库、建表，无需手动初始化 SQL。
- **LLM 中文介绍**：调用任意 OpenAI 兼容接口，一次生成四项内容——中文名、小介绍（≤30 字）、中文分类（如"AI 工具 / 前端框架 / 命令行工具…"）、大介绍（≤300 字，自动排除安装、构建等操作性内容）；生成温度可通过 `LLM_TEMPERATURE` 配置（0~2，默认 0.3，主副供应商共用）。
- **中文 README 优先**：优先发现并使用项目自带的中文 README（`README.zh-CN.md` 等），经 GitHub API 下载、不依赖国内常不可达的 raw 域名；无中文版时将英文 README 送 LLM 翻译精简。
- **QQ 邮箱推送**：SMTP_SSL 465 端口发送 `multipart/alternative` 邮件；HTML 模板全内联 CSS（兼容 QQ 邮箱客户端剥离 `<style>` 的行为），新项目大卡片 + `<details>` 折叠大介绍，老项目灰底简列，另附纯文本降级版本；支持多收件人。
- **失败兜底设计**：单个项目失败不拖垮整体；入库先于发邮件且独立提交，邮件失败不回滚数据库（下次运行该项目按已存在处理，不重推）；LLM 调用沿主→副供应商重试链降级（含整段返回英文的渠道故障判定），全部失败自动降级为项目 description 原文。
- **演练与冒烟**：`--dry-run` 不发邮件不写库（HTML 落地为 `mail_preview.html` 预览）；`--no-llm` 配合 dry-run 用假数据完全离线跑通流水线；`--limit N` 只处理前 N 个项目。
- **日志留痕**：按上海日期滚动写 `logs/trending-YYYY-MM-DD.log`，全程记录每一步决策。
- **自检入口**：`config.py`、`scraper.py`、`db.py`、`llm.py`、`mailer.py` 均支持以 `python3 xxx.py` / `python3 -m xxx` 方式单独自检，便于部署时分段排障。

## 项目结构

```
trending/
├── main.py        # 入口：编排六步流水线（配置→爬取→查重分组→入库→发邮件→汇总）
├── scraper.py     # 爬 trending 页 + GitHub API 拉 README（中文优先）
├── llm.py         # OpenAI 兼容接口调用：中文名/小介绍/分类/大介绍
├── db.py          # PostgreSQL 读写、自动建库建表、永久去重
├── mailer.py      # QQ SMTP 推送 + 手机优先 HTML 模板渲染
├── config.py      # 读 .env，集中管理配置并校验必填项
├── requirements.txt
├── .env.example   # 配置模板（复制为 .env 填入真实值）
└── logs/          # 运行日志（按日期滚动，已 gitignore）
```

## Linux 部署方法（详细）

以下步骤在全新服务器上从零部署，以 Ubuntu / Debian 为例（CentOS 等发行版仅包管理命令不同）。建议以普通用户 + `sudo` 执行。

### 1. 环境准备

需要 Python 3.10+ 与 git：

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
python3 --version   # 确认 >= 3.10
```

### 2. 获取代码

```bash
sudo mkdir -p /opt/trending && sudo chown $USER /opt/trending
git clone https://github.com/lin04-24/trending.git /opt/trending
cd /opt/trending
```

### 3. 创建虚拟环境并安装依赖

```bash
python3 -m venv venv
./venv/bin/pip install -U pip
./venv/bin/pip install -r requirements.txt
```

依赖共 5 项：`requests`、`beautifulsoup4`、`psycopg2-binary`、`openai`、`python-dotenv`（均为官方源即可，国内服务器慢可追加 `-i https://pypi.tuna.tsinghua.edu.cn/simple`）。

### 4. 准备 PostgreSQL

数据库可与其他服务共用现有实例，也可在本机新起一个。二选一：

**方式 A：Docker 运行一个专用实例（推荐，隔离干净）**

```bash
docker run -d --name trending-pg \
  -e POSTGRES_USER=trending \
  -e POSTGRES_PASSWORD=换成强密码 \
  -e POSTGRES_DB=trending \
  -p 127.0.0.1:5432:5432 \
  -v trending-pgdata:/var/lib/postgresql/data \
  --restart unless-stopped \
  postgres:16
```

只绑定 `127.0.0.1` 表示仅本机可连；若脚本与数据库不在同一台机器，改为 `-p 0.0.0.0:5432:5432` 并自行配置防火墙白名单。

**方式 B：apt 安装系统级 PostgreSQL**

```bash
sudo apt install -y postgresql
sudo -u postgres psql -c "CREATE USER trending WITH PASSWORD '换成强密码' CREATEDB;"
sudo -u postgres psql -c "CREATE DATABASE trending OWNER trending;"
```

> `CREATEDB` 权限允许脚本首次运行时自动建库；若不授予，请按上面命令预先创建 `trending` 库。数据表（`repos` 表、索引、注释）由脚本首次运行时自动创建，幂等可重复执行。

### 5. 配置凭据文件 .env

```bash
cp .env.example .env
vim .env
```

逐项填写（均为必填，缺失时启动会明确报错列出）：

| 键 | 说明 |
|---|---|
| `PostgreSQL_IDRESS` | 数据库地址，本机填 `127.0.0.1` |
| `PostgreSQL_PORTS` | 数据库端口，默认 `5432` |
| `PostgreSQL_NAME` | 数据库用户名（如上方创建的 `trending`） |
| `PostgreSQL_KEY` | 数据库密码 |
| `DB_NAME` | 库名，默认 `trending`（可选） |
| `SEND_MAIL` | QQ 发件邮箱，如 `you@qq.com` |
| `SEND_KEY` | QQ 邮箱 **SMTP 授权码**（不是 QQ 密码）：QQ 邮箱网页版 → 设置 → 账号 → 开启 SMTP 服务后生成 |
| `ACCEPT_MAIL` | 收件邮箱，多个用英文逗号分隔 |
| `SEND_PORT` | SMTP 端口，固定 `465`（可选） |
| `GITHUB_TOKEN` | GitHub [Personal Access Token](https://github.com/settings/tokens)（classic），用于提升 API 速率限制并拉取 README；读公开仓库可不勾选任何权限 |
| `LLM_BASE_URL` | OpenAI 兼容接口地址（以 `/v1` 结尾，如 `https://api.openai.com/v1` 或任意中转网关） |
| `LLM_MODEL` | 模型名 |
| `LLM_API_KEY` | 对应的 API Key |
| `LLM_TEMPERATURE` | 生成温度（可选，0~2，默认 `0.3`）：主副供应商共用，越低输出越稳定，适合 JSON 结构化中文生成 |
| `LLM_BACKUP_BASE_URL` | 副供应商接口地址（可选，与下面两项**同时填写才启用**） |
| `LLM_BACKUP_MODEL` | 副供应商模型名 |
| `LLM_BACKUP_API_KEY` | 副供应商 API Key |

LLM 重试链：主供应商「首次 + 重试 2 次」均失败（含大介绍汉字数低于阈值 50 的渠道故障，如整段返回英文）后，自动切换副供应商「首次 + 重试 1 次」；全部失败用 GitHub 简介兜底，单项目不拖垮整体。副供应商留空时，主供应商失败后直接兜底。

`.env` 已被 `.gitignore` 忽略，请勿提交或转发。

### 6. 分段自检（定位问题最快的方式）

```bash
./venv/bin/python3 config.py           # ① 配置加载与连通信息
./venv/bin/python3 -m scraper weekly   # ② 爬取 trending 页（解析出约 25 个项目）
./venv/bin/python3 -m db               # ③ 数据库连接、自动建库建表
./venv/bin/python3 -m llm --live-test  # ④ LLM 实调一次（用内置 ripgrep 样例）
./venv/bin/python3 -m mailer           # ⑤ 离线渲染邮件模板 -> mail_preview.html
```

每步输出 `[OK]` 即为通过；某步失败只需排查对应配置，不影响后续思路。

### 7. 演练运行（不写库、不发邮件）

```bash
./venv/bin/python3 main.py --dry-run --limit 3
```

跑完检查项目根目录生成的 `mail_preview.html`（浏览器打开即是最终邮件效果）。首次练习可加 `--no-llm` 完全离线跑通结构：

```bash
./venv/bin/python3 main.py --dry-run --no-llm
```

### 8. 正式运行

```bash
./venv/bin/python3 main.py
```

终端会输出一行汇总，如：`本次爬取 25 个项目：新 25 个，已存在 0 个，邮件已推送 ✓`。完整过程见 `logs/trending-YYYY-MM-DD.log`。

常用参数：

```bash
main.py --since daily      # 改推每日趋势（默认 weekly）
main.py --limit 5          # 冒烟：只处理前 5 个项目
main.py --verbose          # 终端显示 DEBUG 日志
```

### 9. 定时任务（cron）

编辑 crontab：

```bash
crontab -e
```

添加一行（每周一 08:00 上海时间运行；虚拟环境内的 python 直接以绝对路径调用）：

```cron
0 8 * * 1 cd /opt/trending && ./venv/bin/python3 main.py >> logs/cron.log 2>&1
```

> 若服务器系统时区为 UTC（`timedatectl` 查看），cron 按系统时区触发：上海周一 08:00 对应 UTC 周一 00:00，应写 `0 0 * * 1`；或先把系统时区改为上海：`sudo timedatectl set-timezone Asia/Shanghai`。

每日推送则用 `0 8 * * *`（daily 场景记得给 `main.py` 加 `--since daily`）。

### 10. 升级与维护

```bash
cd /opt/trending
git pull
./venv/bin/pip install -r requirements.txt   # 依赖有更新时
```

- 日志：`logs/trending-YYYY-MM-DD.log` 按日期滚动，可直接检索 `ERROR`。
- 数据备份：只需备份 PostgreSQL 中的 `repos` 表（去重与历史介绍全在里面）。
- 停止推送：注释 crontab 对应行即可；`.env` 中增删 `ACCEPT_MAIL` 收件人可随时调整推送范围。

## 常见问题

- **提示 env 缺失配置项**：`.env` 必须放在项目根目录（与 `main.py` 同级），键名与 `.env.example` 完全一致（区分大小写，如 `PostgreSQL_IDRESS`）。
- **数据库连接失败**：依次确认 PostgreSQL 进程在跑、`.env` 四项数据库配置正确、`PostgreSQL_IDRESS` 不是 `0.0.0.0`（应为数据库服务器 IP 或 `127.0.0.1`）。
- **邮件发送失败**：确认使用的是 QQ 邮箱 SMTP **授权码**而非 QQ 密码；SMTP 服务已在 QQ 邮箱设置中开启；服务器出网 465 端口未被云厂商安全组拦截。
- **README 拉取失败率高**：检查 `GITHUB_TOKEN` 是否有效；脚本已全程走 GitHub API 并自动兜底 description，个别失败不影响整体推送。

## 版本

- **v1.2**：LLM 主副供应商重试链（主供应商首次+重试 2 次均失败后自动切换副供应商首次+重试 1 次，副供应商三项 env 可选配置）；新增 `LLM_TEMPERATURE` 生成温度配置（0~2，默认 0.3，主副供应商共用，非法或越界自动回落并告警）；大介绍汉字数低于 50 判定为渠道故障并沿重试链降级。见 [Releases](https://github.com/lin04-24/trending/releases)。
- **v1.0**：首个正式版本。六模块流水线（scraper / llm / db / mailer / config / main），PostgreSQL 存储，QQ 邮箱推送，dry-run 演练与分段自检，见 [Releases](https://github.com/lin04-24/trending/releases)。
