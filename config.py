"""config.py — 读取凭据文件（.env 优先，旧名 env 兼容），集中管理配置并校验必填项。

键名沿用文件现有命名，不做重命名：
  PostgreSQL: PostgreSQL_IDRESS（地址）/ PostgreSQL_PORTS（端口）
              PostgreSQL_NAME（账号）/ PostgreSQL_KEY（密码）[/ DB_NAME 库名]
  SMTP  : SEND_MAIL / SEND_KEY / ACCEPT_MAIL / SEND_PORT
  其他  : GITHUB_TOKEN / LLM_BASE_URL / LLM_MODEL / LLM_API_KEY
"""

from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import dotenv_values

logger = logging.getLogger("config")

# 项目根目录 = 本文件所在目录
PROJECT_ROOT = Path(__file__).resolve().parent
LOG_DIR = PROJECT_ROOT / "logs"


def _default_env_file() -> Path:
    """凭据文件优先 .env，其次旧名 env（两者都在时 .env 生效）。"""
    for name in (".env", "env"):
        p = PROJECT_ROOT / name
        if p.is_file():
            return p
    return PROJECT_ROOT / ".env"


class ConfigError(Exception):
    """配置缺失或非法时抛出。"""


# ---------------------------------------------------------------------------
# 必填键定义：env 键名 -> 用途说明（缺失时报错列出）
# ---------------------------------------------------------------------------
REQUIRED_KEYS: dict[str, str] = {
    "PostgreSQL_IDRESS": "PostgreSQL 地址（主机名或 IP）",
    "PostgreSQL_NAME": "PostgreSQL 账号",
    "PostgreSQL_KEY": "PostgreSQL 密码",
    "SEND_MAIL": "QQ 发件邮箱",
    "SEND_KEY": "QQ 邮箱 SMTP 授权码",
    "ACCEPT_MAIL": "收件邮箱（逗号分隔可多个）",
    "GITHUB_TOKEN": "GitHub PAT（拉 README 用）",
    "LLM_BASE_URL": "OpenAI 兼容接口地址",
    "LLM_MODEL": "LLM 模型名",
    "LLM_API_KEY": "LLM API Key",
}

# 可选键及默认值
DEFAULTS: dict[str, str] = {
    "PostgreSQL_PORTS": "5432",     # PostgreSQL 默认端口
    "SEND_PORT": "465",
    "DB_NAME": "trending",          # 数据库名
}


def now_shanghai() -> datetime:
    """当前上海时间（独立函数，便于测试与统一时区）。"""
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def shanghai_today() -> str:
    """YYYY-MM-DD（上海日期，用于日志文件名）。"""
    return now_shanghai().strftime("%Y-%m-%d")


def _parse_accept_mail(raw: str) -> list[str]:
    """ACCEPT_MAIL 支持逗号分隔多个收件人。"""
    return [addr.strip() for addr in raw.split(",") if addr.strip()]


@dataclass(frozen=True)
class AppConfig:
    """全局配置（不可变），由 load() 构造。"""

    # PostgreSQL
    db_host: str = "127.0.0.1"
    db_port: int = 5432
    db_user: str = ""
    db_password: str = ""
    db_name: str = "trending"

    # SMTP
    smtp_host: str = "smtp.qq.com"
    smtp_port: int = 465
    send_mail: str = ""
    send_key: str = ""
    accept_mails: list[str] = field(default_factory=list)

    # GitHub
    github_token: str = ""

    # LLM
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""

    # 业务
    since: str = "weekly"

    def mail_subject_prefix(self) -> str:
        period = {"daily": "日", "weekly": "周", "monthly": "月"}.get(
            self.since, ""
        )
        return f"GitHub {period}趋势"


def load(
    env_file: Path | None = None,
    since: str = "weekly",
    extra: dict[str, str] | None = None,
) -> AppConfig:
    """读取 env 文件并构造 AppConfig。

    - 缺失必填键 -> 抛 ConfigError（main 捕获后列出缺失项退出）
    - since 仅接受 daily/weekly/monthly
    - extra 允许调用方覆盖个别键（测试用）
    """
    path = env_file or _default_env_file()
    values: dict[str, str] = {k: "" for k in REQUIRED_KEYS}
    values.update(DEFAULTS)

    if path.is_file():
        file_vals = dotenv_values(path)
        values.update({k: v for k, v in file_vals.items() if v is not None})
    else:
        logger.warning("凭据文件不存在: %s", path)

    if extra:
        values.update(extra)

    # ---- 校验必填 ----
    missing = [k for k in REQUIRED_KEYS if not values.get(k, "").strip()]
    if missing:
        lines = "\n".join(
            f"  - {k}: {REQUIRED_KEYS[k]}" for k in missing
        )
        raise ConfigError(f"env 缺失必填配置项:\n{lines}\nenv 文件位置: {path}")

    if since not in {"daily", "weekly", "monthly"}:
        raise ConfigError(f"since 非法: {since}（仅支持 daily/weekly/monthly）")

    def _int(key: str, fallback: int) -> int:
        raw = (values.get(key) or "").strip()
        if not raw:
            return fallback
        try:
            return int(raw)
        except ValueError:
            logger.warning("%s=%r 非法，使用默认 %d", key, raw, fallback)
            return fallback

    return AppConfig(
        db_host=(values.get("PostgreSQL_IDRESS") or "127.0.0.1").strip(),
        db_port=_int("PostgreSQL_PORTS", 5432),
        db_user=values["PostgreSQL_NAME"].strip(),
        db_password=values["PostgreSQL_KEY"].strip(),
        db_name=(values.get("DB_NAME") or "trending").strip(),
        smtp_port=_int("SEND_PORT", 465),
        send_mail=values["SEND_MAIL"].strip(),
        send_key=values["SEND_KEY"].strip(),
        accept_mails=_parse_accept_mail(values["ACCEPT_MAIL"]),
        github_token=values["GITHUB_TOKEN"].strip(),
        llm_base_url=values["LLM_BASE_URL"].strip().rstrip("/"),
        llm_model=values["LLM_MODEL"].strip(),
        llm_api_key=values["LLM_API_KEY"].strip(),
        since=since,
    )


# ---------------------------------------------------------------------------
# 日志：终端一行汇总 + 按日期文件 logs/trending-YYYY-MM-DD.log
# ---------------------------------------------------------------------------
def setup_logging(verbose: bool = False) -> None:
    """配置根 logger：终端 INFO（verbose 时 DEBUG）+ 滚动日期文件。

    - 文件 logs/trending-YYYY-MM-DD.log，UTF-8，跨日自动切换
    - 格式 [时间] [级别] 模块 消息（时间用上海时区）
    """
    LOG_DIR.mkdir(exist_ok=True)

    fmt = "%(asctime)s [%(levelname)s] %(name)s %(message)s"
    datefmt = "%H:%M:%S"
    formatter = logging.Formatter(fmt, datefmt=datefmt)
    # Formatter 的 asctime 用本地时区；沙箱 UTC，改为上海时间显示
    formatter.converter = (  # type: ignore[method-assign]
        lambda *a: time.gmtime(time.time() + 8 * 3600)
    )

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for h in list(root.handlers):
        root.removeHandler(h)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = _DailyFileHandler(LOG_DIR / "trending.log", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # 降低第三方库噪音
    for noisy in ("urllib3", "openai", "httpx", "httpcore", "httpcore2", "httpx2"):
        logging.getLogger(noisy).setLevel(logging.INFO)


class _DailyFileHandler(logging.FileHandler):
    """按上海日期写 logs/trending-YYYY-MM-DD.log，emit 时跨日切换。"""

    def __init__(self, base_path: Path, encoding: str = "utf-8") -> None:
        self._base = base_path
        super().__init__(self._current_path(), encoding=encoding)
        self._opened_files: set[str] = set()

    @staticmethod
    def _today() -> str:
        return shanghai_today()

    def _current_path(self) -> Path:
        return self._base.with_name(f"trending-{self._today()}.log")

    def _switch_to(self, target: Path) -> None:
        """关闭旧文件，打开新日期文件。"""
        self.close()
        self.baseFilename = str(target)
        if self.encoding:
            self.stream = open(  # noqa: SIM115 - FileHandler 自管生命周期
                self.baseFilename, "a", encoding=self.encoding
            )
        else:
            self.stream = open(self.baseFilename, "a")  # noqa: SIM115
        self._opened_files.add(self.baseFilename)

    def emit(self, record: logging.LogRecord) -> None:
        target = self._current_path()
        if self.baseFilename != str(target):
            self._switch_to(target)
        if self.stream is None:  # FileHandler.close 后置 None
            self._switch_to(target)
        super().emit(record)


if __name__ == "__main__":
    # python3 config.py：自检（不打印任何密钥）
    setup_logging(verbose=True)
    try:
        cfg = load()
    except ConfigError as e:
        print(f"[FAIL] {e}")
        sys.exit(1)
    print("[OK] 配置加载成功")
    print(f"  PostgreSQL : {cfg.db_host}:{cfg.db_port} / 库 {cfg.db_name} / 用户 {cfg.db_user}")
    print(f"  SMTP       : {cfg.smtp_host}:{cfg.smtp_port} 发件 {cfg.send_mail}")
    print(f"  收件人     : {', '.join(cfg.accept_mails)}")
    print(f"  GitHub PAT : {cfg.github_token[:7]}…（已配置）")
    print(f"  LLM        : {cfg.llm_base_url} / 模型 {cfg.llm_model}")
    print(f"  当前上海时间: {now_shanghai().strftime('%Y-%m-%d %H:%M:%S')}")
