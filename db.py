"""db.py — MySQL 读写、自动建库建表、永久去重。

设计要点（§4）：
  - 首次运行自动建库 github_trending + 建表 repos（utf8mb4）
  - 去重键 repo_key = '作者/项目名' 唯一索引，一次入库终身去重
  - 入库统一 INSERT ... ON DUPLICATE KEY UPDATE last_seen/star_total
  - 连接失败提示 docker start <容器名>
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from contextlib import contextmanager

import pymysql

from config import AppConfig, now_shanghai
from llm import RepoSummary
from scraper import TrendingRepo

logger = logging.getLogger("db")

# 建库建表 SQL（IF NOT EXISTS，幂等）。
# 库名来自配置（默认 trending）；建库仅指定字符集 utf8mb4，
# 排序规则跟随服务器默认（MySQL 8 为 utf8mb4_0900_ai_ci），不强制覆盖。
DDL_DATABASE = """
CREATE DATABASE IF NOT EXISTS {db_name}
  DEFAULT CHARACTER SET utf8mb4
"""

DDL_TABLE = """
CREATE TABLE IF NOT EXISTS repos (
  id           INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  repo_key     VARCHAR(200) NOT NULL UNIQUE COMMENT '作者/项目名，去重键',
  author       VARCHAR(100) NOT NULL,
  repo_name    VARCHAR(100) NOT NULL,
  github_url   VARCHAR(160) NOT NULL,
  language     VARCHAR(50)  NULL,
  category     VARCHAR(50)  NULL COMMENT 'LLM 中文分类，如"AI 工具"',
  brief_intro  VARCHAR(300) NULL COMMENT '小介绍：中文名+用途，≤30字',
  full_intro   TEXT NULL COMMENT '大介绍：详细中文介绍，源自 README',
  first_seen   DATETIME NOT NULL COMMENT '首次爬到（上海时间）',
  last_seen    DATETIME NOT NULL COMMENT '最近一次出现在 trending',
  star_total   INT UNSIGNED DEFAULT 0,
  INDEX idx_last_seen (last_seen)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


class DatabaseError(Exception):
    """数据库致命错误（连不上 / 建库建表失败）。"""


class RepoRecord:
    """repos 表一行（查询结果）。"""

    __slots__ = (
        "repo_key", "author", "repo_name", "github_url", "language",
        "category", "brief_intro", "full_intro", "first_seen", "last_seen",
        "star_total",
    )

    def __init__(self, row: dict) -> None:  # noqa: ANN401 - pymysql dict row
        self.repo_key: str = row["repo_key"]
        self.author: str = row["author"]
        self.repo_name: str = row["repo_name"]
        self.github_url: str = row["github_url"]
        self.language: str | None = row["language"]
        self.category: str | None = row["category"]
        self.brief_intro: str | None = row["brief_intro"]
        self.full_intro: str | None = row["full_intro"]
        self.first_seen = row["first_seen"]
        self.last_seen = row["last_seen"]
        self.star_total: int = row["star_total"]


def connect(
    cfg: AppConfig,
    *,
    with_db: bool = True,
    init: bool = False,
) -> pymysql.connections.Connection:
    """建立 MySQL 连接。

    with_db=False 用于建库前（连接 server 级，不指定 database）。
    init=True 时先建库建表（幂等）。
    连不上抛 DatabaseError（含 docker start 提示）。
    """
    kwargs: dict = {
        "host": cfg.db_host,
        "port": cfg.db_port,
        "user": "root",
        "password": cfg.db_password,
        "charset": "utf8mb4",
        "cursorclass": pymysql.cursors.DictCursor,
        "connect_timeout": 10,
    }
    if with_db:
        kwargs["database"] = cfg.db_name

    try:
        conn = pymysql.connect(**kwargs)
    except pymysql.MySQLError as e:
        raise DatabaseError(
            f"MySQL 连接失败 127.0.0.1:{cfg.db_port}: {e}\n"
            f"提示: 请确认容器已运行 docker start {cfg.container_name}"
        ) from e

    if init:
        try:
            with conn.cursor() as cur:
                cur.execute(DDL_DATABASE.format(db_name=cfg.db_name))
                cur.execute(DDL_TABLE)
            conn.commit()
            logger.info(
                "数据库就绪: %s.repos（utf8mb4 + 服务器默认排序规则，幂等）",
                cfg.db_name,
            )
        except pymysql.MySQLError as e:
            conn.close()
            raise DatabaseError(f"建库建表失败: {e}") from e

    return conn


@contextmanager
def transaction(cfg: AppConfig):
    """提供已初始化的连接 + 自动 commit/rollback。

    供需要事务语义的调用方使用；主流程（main）直接用 connect() 并在
    邮件发送前显式提交（设计 §3.3：邮件失败不回滚数据库）。
    """
    conn = connect(cfg, init=True)
    try:
        yield conn
        conn.commit()
    except pymysql.MySQLError:
        conn.rollback()
        raise
    finally:
        conn.close()


def existing_keys(conn, repo_keys: Iterable[str]) -> set[str]:
    """查询哪些 repo_key 已在库中（本次去重判定）。"""
    keys = [k for k in repo_keys if k]
    if not keys:
        return set()
    placeholders = ", ".join(["%s"] * len(keys))
    sql = f"SELECT repo_key FROM repos WHERE repo_key IN ({placeholders})"
    with conn.cursor() as cur:
        cur.execute(sql, keys)
        rows = cur.fetchall()
    found = {r["repo_key"] for r in rows}
    logger.info("查重: %d 个键中 %d 个已存在", len(keys), len(found))
    return found


def upsert_repo(
    conn,
    repo: TrendingRepo,
    summary: RepoSummary,
) -> None:
    """新项目入库（INSERT ... ON DUPLICATE KEY UPDATE）。

    冲突时仅更新 last_seen 与 star_total —— 已有中文介绍永不覆盖。
    """
    now = now_shanghai().strftime("%Y-%m-%d %H:%M:%S")
    sql = """
        INSERT INTO repos
          (repo_key, author, repo_name, github_url, language, category,
           brief_intro, full_intro, first_seen, last_seen, star_total)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
          last_seen = VALUES(last_seen),
          star_total = VALUES(star_total)
    """
    params = (
        repo.repo_key, repo.author, repo.repo_name, repo.github_url,
        repo.language, summary.category, summary.brief_intro,
        summary.full_intro, now, now, repo.stars_total,
    )
    with conn.cursor() as cur:
        cur.execute(sql, params)
    logger.info("入库: %s（%s / %s）", repo.repo_key, summary.category, summary.zh_name)


def touch_last_seen(conn, repo: TrendingRepo) -> None:
    """老项目：仅更新 last_seen（与 star_total），不重推 LLM。"""
    now = now_shanghai().strftime("%Y-%m-%d %H:%M:%S")
    sql = (
        "UPDATE repos SET last_seen = %s, star_total = %s WHERE repo_key = %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (now, repo.stars_total, repo.repo_key))
    logger.info("已存在(仅 touch): %s", repo.repo_key)


def get_records(conn, repo_keys: list[str]) -> dict[str, RepoRecord]:
    """批量取回项目完整记录（邮件老项目区渲染用）。"""
    if not repo_keys:
        return {}
    placeholders = ", ".join(["%s"] * len(repo_keys))
    sql = f"SELECT * FROM repos WHERE repo_key IN ({placeholders})"
    with conn.cursor() as cur:
        cur.execute(sql, repo_keys)
        rows = cur.fetchall()
    return {r["repo_key"]: RepoRecord(r) for r in rows}


def count_all(conn) -> int:
    """库内总项目数（汇总日志用）。"""
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM repos")
        row = cur.fetchone()
    return int(row["n"])


# ---------------------------------------------------------------------------
# python3 -m db：自检（连接 + 建库建表 + 计数，不写入测试数据）
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    import config

    config.setup_logging(verbose=True)

    print("[自检] 连接 MySQL 并确保库表存在 …")
    try:
        conn = connect(config.load(), init=True)
    except DatabaseError as e:
        print(f"[FAIL] {e}")
        sys.exit(1)

    try:
        n = count_all(conn)
        print(f"[OK] 连接成功，github_trending.repos 当前 {n} 条记录")
    finally:
        conn.close()
