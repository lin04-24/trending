"""main.py — 入口：编排六步流水线（设计 §3.2），支持 --dry-run。

流程：
  1. config.load() 读 env，缺失必填直接报错退出
  2. scraper.fetch_trending() 爬趋势页
  3. db 查重分组：新项目 -> README -> LLM -> 入库；老项目 -> touch last_seen
  4. mailer 渲染 + QQ SMTP 465 发送
  5. 终端一行汇总
  6. logs/trending-YYYY-MM-DD.log 全程留痕

失败原则（§3.3）：单项目失败不拖垮整体；入库先于邮件且独立提交，
邮件失败不回滚数据库（下次运行该项目按已存在处理，不重推大介绍）。
"""

from __future__ import annotations

import argparse
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import config as config_mod
import db
import llm
import mailer
import scraper
from config import AppConfig, load, now_shanghai

logger = logging.getLogger("main")


# ---------------------------------------------------------------------------
# 流水线数据结构
# ---------------------------------------------------------------------------
@dataclass
class PipelineResult:
    """一次运行的汇总（终端输出与日志收尾用）。"""

    total: int = 0
    new_count: int = 0
    old_count: int = 0
    mail_sent: bool = False
    dry_run: bool = False


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="GitHub Trending 爬取 + LLM 中文介绍 + QQ 邮箱推送"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="演练模式：不发邮件、不写库（LLM 正常调用）",
    )
    parser.add_argument(
        "--no-llm", action="store_true",
        help="配合 --dry-run：用假数据代替 LLM 调用（完全离线可跑）",
    )
    parser.add_argument(
        "--since", default="weekly", choices=["daily", "weekly", "monthly"],
        help="trending 周期（默认 weekly）",
    )
    parser.add_argument(
        "--limit", type=int, default=0, metavar="N",
        help="只处理前 N 个项目（冒烟测试用，0 = 全部）",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="终端显示 DEBUG 日志"
    )
    args = parser.parse_args(argv)
    if args.no_llm and not args.dry_run:
        parser.error("--no-llm 仅可与 --dry-run 组合使用（避免假数据入库）")
    if args.limit < 0:
        parser.error("--limit 不能为负")
    return args


# ---------------------------------------------------------------------------
# 第 3 步：查重分组 + 新项目处理
# ---------------------------------------------------------------------------
def _fake_summary(repo: scraper.TrendingRepo) -> llm.RepoSummary:
    """--no-llm 模式下的假数据（保持流水线结构一致）。"""
    desc = (repo.description or "GitHub 项目")[:20]
    return llm.RepoSummary(
        zh_name=repo.repo_name,
        brief_intro=f"{repo.repo_name}：{desc}",
        category="未分类",
        full_intro=(
            f"（dry-run 假数据）{repo.repo_name}："
            f"{repo.description or '该项目暂无描述'}"
        ),
        llm_ok=False,
    )


def _fetch_readmes(
    repos: list[scraper.TrendingRepo], github_token: str
) -> dict[str, scraper.ReadmeResult]:
    """并发预取新项目 README（I/O 密集，线程池加速）。

    单个失败不拖垮整体：返回 description 兜底结果。
    """
    if not repos:
        return {}

    logger.info("并发拉取 %d 个新项目 README …", len(repos))
    readme_map: dict[str, scraper.ReadmeResult] = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(scraper.fetch_readme, repo, github_token): repo.repo_key
            for repo in repos
        }
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                readme_map[key] = fut.result()
            except Exception as e:  # noqa: BLE001
                logger.error("README 拉取失败 %s: %s", key, e)
                readme_map[key] = scraper.ReadmeResult(
                    content="", is_chinese=False, source="error-fallback"
                )
    return readme_map


def _process_new_repos(
    cfg: AppConfig,
    new_repos: list[scraper.TrendingRepo],
    conn,
    use_llm: bool,
) -> list[mailer.NewItemCard]:
    """逐个处理新项目：README（已预取）-> LLM -> 入库。

    单项目任何失败均兜底降级，不抛出（设计 §3.3）。
    """
    readme_map = _fetch_readmes(new_repos, cfg.github_token)
    cards: list[mailer.NewItemCard] = []

    for repo in new_repos:
        try:
            readme = readme_map.get(repo.repo_key)
            if readme is None or not readme.content:
                # 预取失败的项目：用 description 兜底
                readme = scraper.ReadmeResult(
                    content=repo.description or repo.repo_name,
                    is_chinese=False,
                    source="description",
                )

            if use_llm:
                summary = llm.summarize(
                    cfg, repo.repo_name, repo.description,
                    readme.content, readme.is_chinese,
                )
            else:
                summary = _fake_summary(repo)

            cards.append(
                mailer.NewItemCard(
                    repo=repo,
                    summary=summary,
                    first_seen=now_shanghai().strftime("%Y-%m-%d %H:%M"),
                )
            )
            if conn is not None:
                db.upsert_repo(conn, repo, summary)
        except Exception as e:  # noqa: BLE001 - 单项目失败不拖垮整体
            logger.error("新项目处理失败 %s: %s", repo.repo_key, e)

    return cards


def run_pipeline(
    cfg: AppConfig, dry_run: bool, no_llm: bool = False, limit: int = 0
) -> PipelineResult:
    """执行第 2~5 步。致命错误抛出由 main 捕获。"""
    result = PipelineResult(dry_run=dry_run)
    use_llm = not no_llm

    # ---- 第 2 步：爬取 trending ----
    repos = scraper.fetch_trending(cfg.since)
    if limit > 0:
        repos = repos[:limit]
        logger.info("冒烟模式：仅处理前 %d 个项目", limit)
    result.total = len(repos)
    logger.info("第 2 步完成：爬取 %d 个项目", len(repos))

    # ---- 第 3 步：查重分组 + 新项目处理 + 入库（先于邮件，独立提交）----
    conn = None
    cards: list[mailer.NewItemCard] = []
    old_records: list[db.RepoRecord] = []

    try:
        if not dry_run:
            conn = db.connect(cfg, init=True)
            existing = db.existing_keys(conn, [r.repo_key for r in repos])
        else:
            existing = set()          # dry-run 视为全新

        new_repos = [r for r in repos if r.repo_key not in existing]
        old_repos = [r for r in repos if r.repo_key in existing]
        result.new_count = len(new_repos)
        result.old_count = len(old_repos)
        logger.info(
            "第 3 步分组：新 %d / 已存在 %d", len(new_repos), len(old_repos)
        )

        cards = _process_new_repos(cfg, new_repos, conn if not dry_run else None, use_llm)

        if conn is not None:
            for repo in old_repos:
                try:
                    db.touch_last_seen(conn, repo)
                except Exception as e:  # noqa: BLE001
                    logger.error("touch 失败 %s: %s", repo.repo_key, e)
            old_records = list(
                db.get_records(conn, [r.repo_key for r in old_repos]).values()
            )
            if old_records:
                # 保持邮件顺序 = trending 页顺序
                order = {r.repo_key: i for i, r in enumerate(old_repos)}
                old_records.sort(key=lambda rec: order[rec.repo_key])

        logger.info(
            "第 3 步完成：新项目卡片 %d 张，老项目记录 %d 条",
            len(cards), len(old_records),
        )
    finally:
        if conn is not None:
            try:
                conn.commit()
                logger.info("数据库已提交（先于邮件发送，邮件失败不回滚）")
            except Exception as e:  # noqa: BLE001
                logger.error("数据库提交失败: %s", e)
            finally:
                conn.close()

    # ---- 第 4 步：渲染 + 发送 ----
    if not cards and not old_records:
        logger.warning("无任何可推送内容，跳过邮件")
        return result

    html_body = mailer.render_html(cards, old_records, cfg)
    text_body = mailer.render_text(cards, old_records)
    if dry_run:
        preview = config_mod.PROJECT_ROOT / "mail_preview.html"
        preview.write_text(html_body, encoding="utf-8")
        logger.info("dry-run：邮件未发送，HTML 已存 %s", preview.name)
    else:
        result.mail_sent = mailer.send(
            cfg, html_body, text_body, new_count=len(cards)
        )

    return result


# ---------------------------------------------------------------------------
# 终端一行汇总（设计 §7）
# ---------------------------------------------------------------------------
def _summary_line(result: PipelineResult) -> str:
    if result.dry_run:
        mail_part = "邮件未发送（dry-run）"
    elif result.mail_sent:
        mail_part = "邮件已推送 ✓"
    else:
        mail_part = "邮件发送失败 ✗（详见日志）"
    return (
        f"本次爬取 {result.total} 个项目：新 {result.new_count} 个，"
        f"已存在 {result.old_count} 个，{mail_part}"
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_mod.setup_logging(verbose=args.verbose)

    # ---- 第 1 步：加载配置 ----
    try:
        cfg = load(since=args.since)
    except config_mod.ConfigError as e:
        print(f"[配置错误] {e}", file=sys.stderr)
        return 2

    # ---- 第 2~5 步 ----
    try:
        result = run_pipeline(
            cfg,
            dry_run=args.dry_run,
            no_llm=args.no_llm,
            limit=args.limit,
        )
    except (scraper.ScraperError, db.DatabaseError) as e:
        logger.error("致命错误，本次运行中止（不发邮件）: %s", e)
        print(f"[致命错误] {e}", file=sys.stderr)
        return 1

    # ---- 终端一行汇总 ----
    print(_summary_line(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
