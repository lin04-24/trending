"""db.py — PostgreSQL 读写、自动建库建表、永久去重。

设计要点（§4）：
  - 首次运行自动建库 trending + 建表 repos（PostgreSQL 默认 UTF8，无需指定字符集）
  - 去重键 repo_key = '作者/项目名' 唯一约束，一次入库终身去重
  - 入库统一 INSERT ... ON CONFLICT (repo_key) DO UPDATE last_seen/star_total
  - 连接参数来自 env：PostgreSQL_IDRESS/PORTS/NAME/KEY（见 config.py）
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from contextlib import contextmanager

import psycopg2
from psycopg2.extras import RealDictCursor

from config import AppConfig, now_shanghai
from llm import RepoSummary
from scraper import TrendingRepo

logger = logging.getLogger("db")

# 建表 SQL（IF NOT EXISTS，幂等）。
# PostgreSQL 没有 CREATE DATABASE IF NOT EXISTS，建库逻辑见 _ensure_database。
# PostgreSQL 无 UNSIGNED，星数上限 INTEGER（21 亿）足够。
DDL_TABLE = """
CREATE TABLE IF NOT EXISTS repos (
  id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  repo_key     VARCHAR(200) NOT NULL UNIQUE,
  author       VARCHAR(100) NOT NULL,
  repo_name    VARCHAR(100) NOT NULL,
  github_url   VARCHAR(160) NOT NULL,
  language     VARCHAR(50)  NULL,
  category     VARCHAR(50)  NULL,
  brief_intro  VARCHAR(300) NULL,
  full_intro   TEXT NULL,
  first_seen   TIMESTAMP NOT NULL,
  last_seen    TIMESTAMP NOT NULL,
  star_total   INTEGER DEFAULT 0
)
"""

DDL_INDEX = "CREATE INDEX IF NOT EXISTS idx_last_seen ON repos (last_seen)"

DDL_COMMENTS = """
COMMENT ON COLUMN repos.repo_key    IS '作者/项目名，去重键';
COMMENT ON COLUMN repos.category    IS 'LLM 中文分类，如"AI 工具"';
COMMENT ON COLUMN repos.brief_intro IS '小介绍：中文名+用途，≤30字';
COMMENT ON COLUMN repos.full_intro  IS '大介绍：详细中文介绍，源自 README';
COMMENT ON COLUMN repos.first_seen  IS '首次爬到（上海时间）';
COMMENT ON COLUMN repos.last_seen   IS '最近一次出现在 trending'
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

    def __init__(self, row: dict) -> None:  # noqa: ANN401 - RealDictCursor 行
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


def _connect_kwargs(cfg: AppConfig, dbname: str) -> dict:
    return {
        "host": cfg.db_host,
        "port": cfg.db_port,
        "user": cfg.db_user,
        "password": cfg.db_password,
        "dbname": dbname,
        "connect_timeout": 10,
        "cursor_factory": RealDictCursor,
    }


def _ensure_database(cfg: AppConfig) -> None:
    """目标库不存在时创建。

    通过维护库 postgres 查 pg_database 判存后按需创建；若维护库不可达
    （部分托管服务限制访问），跳过建库，由后续直连目标库兜底。
    """
    if not re.fullmatch(r"[A-Za-z0-9_]+", cfg.db_name):
        raise DatabaseError(
            f"非法数据库名: {cfg.db_name!r}（仅允许字母数字下划线）"
        )

    try:
        admin = psycopg2.connect(**_connect_kwargs(cfg, "postgres"))
    except psycopg2.Error as e:
        logger.warning(
            "维护库 postgres 不可达，跳过建库（假定 %s 已由服务端创建）: %s",
            cfg.db_name, e,
        )
        return

    try:
        admin.autocommit = True  # CREATE DATABASE 不能在事务块中执行
        with admin.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (cfg.db_name,)
            )
            if cur.fetchone() is None:
                cur.execute(f'CREATE DATABASE "{cfg.db_name}"')
                logger.info("已创建数据库 %s", cfg.db_name)
    finally:
        admin.close()


def connect(cfg: AppConfig, *, init: bool = False):
    """建立 PostgreSQL 连接（RealDictCursor，行结果为 dict）。

    init=True 时先确保库存在并建表建索引（幂等）。
    连不上抛 DatabaseError（提示检查 env 中 PostgreSQL_* 四项配置）。
    """
    if init:
        _ensure_database(cfg)

    try:
        conn = psycopg2.connect(**_connect_kwargs(cfg, cfg.db_name))
    except psycopg2.Error as e:
        raise DatabaseError(
            f"PostgreSQL 连接失败 {cfg.db_host}:{cfg.db_port}"
            f"（用户 {cfg.db_user}）: {e}\n"
            "提示: 请检查 env 中 PostgreSQL_IDRESS / PostgreSQL_PORTS / "
            "PostgreSQL_NAME / PostgreSQL_KEY 是否正确，服务器是否可达"
        ) from e

    if init:
        try:
            with conn.cursor() as cur:
                cur.execute(DDL_TABLE)
                cur.execute(DDL_INDEX)
                cur.execute(DDL_COMMENTS)
            conn.commit()
            logger.info("数据库就绪: %s.repos（幂等）", cfg.db_name)
        except psycopg2.Error as e:
            conn.close()
            raise DatabaseError(f"建表失败: {e}") from e

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
    except psycopg2.Error:
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
    """新项目入库（INSERT ... ON CONFLICT DO UPDATE）。

    冲突时仅更新 last_seen 与 star_total —— 已有中文介绍永不覆盖。
    """
    now = now_shanghai().strftime("%Y-%m-%d %H:%M:%S")
    sql = """
        INSERT INTO repos
          (repo_key, author, repo_name, github_url, language, category,
           brief_intro, full_intro, first_seen, last_seen, star_total)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (repo_key) DO UPDATE
          SET last_seen = EXCLUDED.last_seen,
              star_total = EXCLUDED.star_total
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

    print("[自检] 连接 PostgreSQL 并确保库表存在 …")
    try:
        cfg = config.load()
    except config.ConfigError as e:
        print(f"[FAIL] {e}")
        sys.exit(1)

    try:
        conn = connect(cfg, init=True)
    except DatabaseError as e:
        print(f"[FAIL] {e}")
        sys.exit(1)

    try:
        n = count_all(conn)
        print(f"[OK] 连接成功，{cfg.db_name}.repos 当前 {n} 条记录")
    finally:
        conn.close()
