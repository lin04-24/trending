"""mailer.py — QQ 邮箱 SMTP 465 SSL 推送 + 手机优先 HTML 邮件模板。

设计要点（§6）：
  - multipart/alternative（text/html + 纯文本备用）
  - 全部内联 CSS（QQ 客户端剥离 <style> 标签）
  - max-width 640px / width 100% / 系统字体栈
  - 新项目大卡片 + 老项目灰底简列 + 页脚
  - 发送失败重试 2 次；失败不回滚数据库
"""

from __future__ import annotations

import html
import logging
import smtplib
import sys
import time
from dataclasses import dataclass
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

from config import AppConfig, now_shanghai
from db import RepoRecord
from llm import RepoSummary
from scraper import TrendingRepo

logger = logging.getLogger("mailer")

SMTP_TIMEOUT = 30
SEND_MAX_ATTEMPTS = 3          # 首次 + 重试 2 次

MAIL_BG = "#f4f5f7"
CARD_BG = "#ffffff"
PRIMARY = "#0969da"            # GitHub 蓝
GRAY_TEXT = "#57606a"
BORDER = "#d0d7de"


class MailError(Exception):
    """邮件发送失败（重试耗尽）。"""


@dataclass(frozen=True)
class NewItemCard:
    """邮件中一张新项目卡片所需数据。"""

    repo: TrendingRepo
    summary: RepoSummary
    first_seen: str             # 首次收录时间（YYYY-MM-DD HH:MM）


def _esc(text: str | None) -> str:
    """HTML 转义（None -> 空串）。"""
    return html.escape(text or "", quote=True)


# ---------------------------------------------------------------------------
# HTML 渲染（全内联 CSS）
# ---------------------------------------------------------------------------
def render_html(
    new_items: list[NewItemCard],
    old_records: list[RepoRecord],
    cfg: AppConfig,
    now_str: str | None = None,
) -> str:
    """渲染完整 HTML 邮件。

    new_items 按邮件顺序排列；old_records 灰底简列。
    """
    now = now_str or now_shanghai().strftime("%Y-%m-%d %H:%M")
    parts: list[str] = []

    # 周期标题随 --since 变化（weekly/daily/monthly）
    period_title = {"daily": "今日趋势", "weekly": "本周趋势", "monthly": "本月趋势"}.get(
        cfg.since, "趋势"
    )

    # ---------- 报告头 ----------
    parts.append(f"""
<div style="margin:0 auto;max-width:640px;width:100%;background:{MAIL_BG};font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif;padding:16px 12px;">
  <div style="background:{CARD_BG};border:1px solid {BORDER};border-radius:12px;margin-bottom:16px;padding:20px 20px 16px;">
    <div style="font-size:20px;font-weight:700;color:#1f2328;line-height:1.4;">📦 GitHub {period_title}</div>
    <div style="margin-top:6px;font-size:13px;color:{GRAY_TEXT};line-height:1.6;">
      {now} · 上海时间<br>
      本次爬取 <b style="color:#1f2328;">{len(new_items) + len(old_records)}</b> 个项目：
      新 <b style="color:#1a7f37;">{len(new_items)}</b> 个 ·
      已存在 <b style="color:{GRAY_TEXT};">{len(old_records)}</b> 个
    </div>
  </div>""")

    # ---------- 新项目卡片 ----------
    if new_items:
        parts.append(
            f'<div style="font-size:15px;font-weight:700;color:#1f2328;'
            f'padding:0 4px 8px;">🆕 新项目（{len(new_items)}）</div>'
        )
        for card in new_items:
            parts.append(_render_card(card))

    # ---------- 老项目区 ----------
    if old_records:
        rows: list[str] = []
        for rec in old_records:
            name = rec.brief_intro or rec.repo_key
            rows.append(
                f'<div style="padding:9px 0;border-bottom:1px solid #eaeef2;'
                f'font-size:13px;line-height:1.6;">'
                f'<a href="{_esc(rec.github_url)}" style="color:{PRIMARY};'
                f'text-decoration:none;font-weight:600;">{_esc(rec.repo_key)}</a>'
                f'<span style="color:{GRAY_TEXT};"> — {_esc(name)}</span>'
                f"</div>"
            )
        parts.append(f"""
  <div style="background:{CARD_BG};border:1px solid {BORDER};border-radius:12px;margin-top:16px;padding:16px 20px 8px;">
    <div style="font-size:14px;font-weight:700;color:#1f2328;padding-bottom:4px;">📥 之前已推送过（{len(old_records)}）</div>
    {''.join(rows)}
    <div style="padding:10px 0 8px;font-size:12px;color:{GRAY_TEXT};">这些项目此前已推送过详细介绍，本次仅在趋势榜再次出现。</div>
  </div>""")

    # ---------- 页脚 ----------
    parts.append(f"""
  <div style="text-align:center;font-size:12px;color:{GRAY_TEXT};padding:16px 0 8px;line-height:1.7;">
    本邮件由 trending 脚本自动发送 · 自动抓取 GitHub Trending<br>
    数据来源：github.com/trending · 若无需接收请在 QQ 邮箱设置过滤规则
  </div>
</div>""")

    return "".join(parts)


def _render_card(card: NewItemCard) -> str:
    """单张新项目卡片：中文名大标题 / 英文名 / 徽章 / 链接 / 大介绍 / 收录时间。"""
    r, s = card.repo, card.summary
    badges: list[str] = []
    if r.language:
        badges.append(
            f'<span style="display:inline-block;background:#ddf4ff;color:#0969da;'
            f'border-radius:10px;padding:2px 10px;font-size:12px;'
            f'font-weight:600;margin-right:6px;">{_esc(r.language)}</span>'
        )
    badges.append(
        f'<span style="display:inline-block;background:#f0fff4;color:#1a7f37;'
        f'border-radius:10px;padding:2px 10px;font-size:12px;'
        f'font-weight:600;">{_esc(s.category)}</span>'
    )
    return f"""
<div style="background:{CARD_BG};border:1px solid {BORDER};border-radius:12px;margin-bottom:14px;padding:20px;">
  <div style="font-size:18px;font-weight:700;color:#1f2328;line-height:1.4;">{_esc(s.zh_name)}</div>
  <div style="margin-top:2px;font-size:12px;color:{GRAY_TEXT};">{_esc(r.repo_key)} · ⭐ {_esc(_fmt_stars(r.stars_total))}（本周 +{_fmt_stars(r.stars_week)}）</div>
  <div style="margin:10px 0 12px;">{''.join(badges)}</div>
  <a href="{_esc(r.github_url)}"
     style="display:inline-block;background:{PRIMARY};color:#ffffff;text-decoration:none;
            border-radius:8px;padding:8px 18px;font-size:13px;font-weight:600;">
     在 GitHub 查看
  </a>
  <div style="margin-top:14px;font-size:14px;color:#1f2328;line-height:1.8;">{_esc(s.full_intro)}</div>
  <div style="margin-top:12px;font-size:12px;color:{GRAY_TEXT};">首次收录：{_esc(card.first_seen)}</div>
</div>"""


def _fmt_stars(n: int) -> str:
    """星数千分位。"""
    return f"{n:,}" if isinstance(n, int) else str(n)


def render_text(
    new_items: list[NewItemCard],
    old_records: list[RepoRecord],
) -> str:
    """纯文本备用部分（multipart/alternative 降级显示）。"""
    lines: list[str] = []
    for card in new_items:
        r, s = card.repo, card.summary
        lines.append(f"【新】{s.zh_name}（{r.repo_key}）")
        lines.append(f"  分类：{s.category} | 语言：{r.language or '无'}")
        lines.append(f"  链接：{r.github_url}")
        lines.append(f"  介绍：{s.full_intro}")
        lines.append("")
    if old_records:
        lines.append(f"—— 已推送过（{len(old_records)}）——")
        for rec in old_records:
            lines.append(f"  {rec.repo_key} — {rec.brief_intro or ''}")
        lines.append("")
    return "\n".join(lines)


def build_message(
    cfg: AppConfig,
    html_body: str,
    text_body: str,
    new_count: int,
    now_str: str | None = None,
) -> MIMEMultipart:
    """构造邮件：标题（设计 §6.1）+ multipart/alternative。"""
    now = now_str or now_shanghai().strftime("%Y-%m-%d %H:%M")
    subject = f"{cfg.mail_subject_prefix()} {new_count} 新项目 | {now} 上海时间"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr(
        (str(Header(cfg.mail_subject_prefix() + "推送", "utf-8")), cfg.send_mail)
    )
    msg["To"] = ", ".join(cfg.accept_mails)
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    return msg


def send(
    cfg: AppConfig,
    html_body: str,
    text_body: str,
    new_count: int,
) -> bool:
    """SMTP_SSL 465 登录并发送（重试 2 次）。失败返回 False（不抛异常）。

    设计 §3.3：邮件失败不回滚数据库，下次运行按已存在处理。
    """
    msg = build_message(cfg, html_body, text_body, new_count)
    last_err: Exception | None = None

    for attempt in range(1, SEND_MAX_ATTEMPTS + 1):
        try:
            with smtplib.SMTP_SSL(
                cfg.smtp_host, cfg.smtp_port, timeout=SMTP_TIMEOUT
            ) as smtp:
                smtp.login(cfg.send_mail, cfg.send_key)
                smtp.sendmail(
                    cfg.send_mail, cfg.accept_mails, msg.as_string()
                )
            logger.info(
                "邮件已发送 -> %s（新 %d）", ", ".join(cfg.accept_mails), new_count
            )
            return True
        except (smtplib.SMTPException, OSError) as e:
            last_err = e
            if attempt < SEND_MAX_ATTEMPTS:
                delay = attempt * 5
                logger.warning(
                    "邮件第 %d 次发送失败: %s，%ds 后重试", attempt, e, delay
                )
                time.sleep(delay)
            else:
                logger.error("邮件重试 %d 次仍失败: %s", attempt, e)

    logger.error("邮件推送失败（数据库不回滚）: %s", last_err)
    return False


# ---------------------------------------------------------------------------
# python3 -m mailer：自检
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import config as config_mod

    config_mod.setup_logging(verbose=True)
    from config import load

    cfg = load()

    # 演示数据（离线渲染，不发送）
    from db import RepoRecord
    from llm import RepoSummary
    from scraper import TrendingRepo

    demo_new = [
        NewItemCard(
            repo=TrendingRepo(
                author="BurntSushi", repo_name="ripgrep",
                github_url="https://github.com/BurntSushi/ripgrep",
                language="Rust",
                description="ripgrep recursively searches directories",
                stars_week=1200, stars_total=50000,
            ),
            summary=RepoSummary(
                zh_name="ripgrep", brief_intro="ripgrep：极速正则搜索工具",
                category="命令行工具",
                full_intro="ripgrep（rg）是用 Rust 编写的命令行文本搜索工具，"
                           "在大规模代码库中递归搜索正则模式。默认遵循 .gitignore "
                           "规则并跳过隐藏文件，速度通常显著快于其他搜索工具。",
                llm_ok=True,
            ),
            first_seen="2026-08-15 20:30",
        ),
        NewItemCard(
            repo=TrendingRepo(
                author="ant-design", repo_name="ant-design",
                github_url="https://github.com/ant-design/ant-design",
                language="TypeScript", description="enterprise UI language",
                stars_week=800, stars_total=96000,
            ),
            summary=RepoSummary(
                zh_name="Ant Design", brief_intro="Ant Design：企业级 React 组件库",
                category="前端框架",
                full_intro="Ant Design 是一套企业级 UI 设计语言和 React 组件库，"
                           "提供丰富的高质量组件，服务于中后台产品设计与开发。",
                llm_ok=True,
            ),
            first_seen="2026-08-15 20:30",
        ),
    ]
    demo_old = [
        RepoRecord({
            "repo_key": "vuejs/core", "author": "vuejs", "repo_name": "core",
            "github_url": "https://github.com/vuejs/core", "language": "TypeScript",
            "category": "前端框架", "brief_intro": "Vue：渐进式 JS 框架",
            "full_intro": "", "first_seen": None, "last_seen": None,
            "star_total": 49000,
        }),
        RepoRecord({
            "repo_key": "pydantic/pydantic", "author": "pydantic", "repo_name": "pydantic",
            "github_url": "https://github.com/pydantic/pydantic", "language": "Python",
            "category": "开发库", "brief_intro": "Pydantic：数据校验库",
            "full_intro": "", "first_seen": None, "last_seen": None,
            "star_total": 13000,
        }),
    ]

    html_body = render_html(demo_new, demo_old, cfg)
    text_body = render_text(demo_new, demo_old)
    msg = build_message(cfg, html_body, text_body, new_count=len(demo_new))

    out = "mail_preview.html"
    with open(out, "w", encoding="utf-8") as f:
        f.write(html_body)
    print(f"[OK] 渲染成功 -> {out}")
    print(f"[OK] 邮件标题: {msg['Subject']}")
    print(f"[OK] 纯文本部分 {len(text_body)} 字符")
    if "--send-demo" in sys.argv:
        ok = send(cfg, html_body, text_body, new_count=len(demo_new))
        print(f"[{'OK' if ok else 'FAIL'}] 发送{'成功' if ok else '失败'}")
        sys.exit(0 if ok else 1)
    print("[提示] 加 --send-demo 可真实发送演示邮件")
