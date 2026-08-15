"""scraper.py — 爬取 GitHub Trending 页并获取项目 README（中文优先）。

对外主要接口：
  fetch_trending(since)   -> list[TrendingRepo]     爬取趋势页（重试 3 次）
  parse_trending(html)    -> list[TrendingRepo]     纯解析（可测试）
  fetch_readme(repo, token) -> ReadmeResult         README 获取（中文优先链）

选择器集中定义在 SELECTORS，页面改版时只需调整此处。
"""

from __future__ import annotations

import base64
import logging
import random
import re
import sys
import time
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger("scraper")

# ---------------------------------------------------------------------------
# 页面结构选择器（集中定义，改版时只改这里）
# ---------------------------------------------------------------------------
SELECTORS = {
    "repo_row": "article.Box-row",
    "repo_link": "h2 a",
    "description": "p.col-9",
    "language": '[itemprop="programmingLanguage"]',
    "stars_link": 'a[href$="/stargazers"]',
}

TRENDING_URL = "https://github.com/trending"
TRENDING_TIMEOUT = 30

# 中文 README 文件名匹配（GitHub API 目录列表中发现用，大小写不敏感）
ZH_README_RE = re.compile(
    r"^readme[\._\-]?(zh|chinese|cn)([\._\-](cn|sc|hans|tw|hant))?"
    r"\.(md|markdown|txt)$",
    re.IGNORECASE,
)

# 中文 README 候选文件名（raw 直链兜底，按优先级）
ZH_README_CANDIDATES = (
    "README.zh-CN.md",
    "README-zh_CN.md",
    "README.zh_CN.md",
    "README_zh.md",
    "README-zh.md",
    "README_CN.md",
    "README-zh-cn.md",
)

# 中文字符检测（含中文标点扩展区）
ZH_CHAR_RE = re.compile(r"[一-龥]")

# 送 LLM 的 README 截断长度
README_LLM_LIMIT = 6000

# README 轻度清理正则（去 badge 噪音，让 LLM 看到真正内容）
_RE_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_RE_HTML_TAG = re.compile(r"<[^>]+>")
_RE_MD_BADGE = re.compile(r"!?\[[^\]]*\]\([^)]*\)", re.DOTALL)
_RE_MULTI_BLANK = re.compile(r"\n{3,}")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class ScraperError(Exception):
    """爬取致命错误（重试耗尽 / 页面改版解析 0 结果）。"""


@dataclass(frozen=True)
class TrendingRepo:
    """trending 页解析出的单个项目。"""

    author: str
    repo_name: str
    github_url: str
    language: str | None
    description: str | None
    stars_week: int          # 本周新增星
    stars_total: int         # 总星数

    @property
    def repo_key(self) -> str:
        """去重键：作者/项目名。"""
        return f"{self.author}/{self.repo_name}"


@dataclass(frozen=True)
class ReadmeResult:
    """fetch_readme 的返回。"""

    content: str             # 送 LLM 的文本（中文 README 或英文原文或 description 兜底）
    is_chinese: bool         # True = 项目自带中文 README（LLM 只做精简）
    source: str              # 来源描述：zh-readme / main-readme / api-readme / description


# ---------------------------------------------------------------------------
# Trending 页
# ---------------------------------------------------------------------------
def _get_with_retry(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    max_retries: int = 3,
    timeout: int = TRENDING_TIMEOUT,
    ok_codes: tuple[int, ...] = (200,),
    sleep_base: float = 2.0,
    what: str = "请求",
) -> requests.Response:
    """带重试的 GET：指数退避 + 抖动（2s → 4s → 8s ± 抖动）。

    4xx（非 429）不重试——请求本身有问题，重试无意义。
    """
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(
                url, headers=headers, params=params, timeout=timeout
            )
            if resp.status_code in ok_codes:
                return resp
            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                raise ScraperError(
                    f"{what}失败 HTTP {resp.status_code}: {url}"
                )
            last_exc = ScraperError(
                f"{what}失败 HTTP {resp.status_code}: {url}"
            )
        except requests.RequestException as e:
            last_exc = e

        if attempt < max_retries:
            delay = sleep_base * (2 ** (attempt - 1)) + random.uniform(0, 1)
            logger.warning(
                "%s 第 %d/%d 次失败（%s），%.1fs 后重试",
                what, attempt, max_retries, last_exc, delay,
            )
            time.sleep(delay)

    raise ScraperError(f"{what}重试 {max_retries} 次仍失败: {last_exc}")


def fetch_trending(since: str = "weekly") -> list[TrendingRepo]:
    """爬取 github.com/trending?since=…，返回项目列表。

    致命错误（网络重试耗尽 / 解析 0 结果）抛 ScraperError。
    """
    resp = _get_with_retry(
        TRENDING_URL,
        headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
        params={"since": since},
        what=f"trending 页({since})",
    )
    repos = parse_trending(resp.text)
    if not repos:
        raise ScraperError(
            "trending 页解析到 0 个项目，页面可能已改版，"
            f"请检查 SELECTORS 定义（{TRENDING_URL}?since={since}）"
        )
    logger.info("trending(%s) 解析到 %d 个项目", since, len(repos))
    return repos


def parse_trending(html: str) -> list[TrendingRepo]:
    """解析 trending HTML。纯函数，便于离线测试。"""
    soup = BeautifulSoup(html, "html.parser")
    repos: list[TrendingRepo] = []

    for row in soup.select(SELECTORS["repo_row"]):
        link = row.select_one(SELECTORS["repo_link"])
        if not link or not link.get("href"):
            continue
        href = str(link["href"]).strip()
        m = re.match(r"^/([^/\s]+)/([^/\s]+)/?$", href)
        if not m:
            continue
        author, repo_name = m.group(1), m.group(2)

        desc_el = row.select_one(SELECTORS["description"])
        description = desc_el.get_text(strip=True) if desc_el else None

        lang_el = row.select_one(SELECTORS["language"])
        language = lang_el.get_text(strip=True) if lang_el else None

        stars_total = 0
        for a in row.select(SELECTORS["stars_link"]):
            raw = a.get_text(strip=True).replace(",", "")
            if raw.isdigit():
                stars_total = int(raw)
                break

        # 周星：浮动条文本 "N stars this week" / "N stars this month"…
        stars_week = _parse_stars_period(row)

        repos.append(
            TrendingRepo(
                author=author,
                repo_name=repo_name,
                github_url=f"https://github.com/{author}/{repo_name}",
                language=language,
                description=description,
                stars_week=stars_week,
                stars_total=stars_total,
            )
        )

    return repos


def _parse_stars_period(row) -> int:  # noqa: ANN001 - bs4 Tag
    """从浮动条文本提取本周期新增星数。

    兼容 "1,234 stars this week" 与 daily 页的 "1,234 stars today"。
    """
    text = row.get_text(" ", strip=True)
    m = re.search(
        r"([\d,]+)\s+stars?\s+this\s+(week|month|day|quarter|year)"
        r"|([\d,]+)\s+stars?\s+today",
        text,
        re.IGNORECASE,
    )
    if not m:
        return 0
    raw = m.group(1) or m.group(3) or "0"
    try:
        return int(raw.replace(",", ""))
    except ValueError:
        return 0


# ---------------------------------------------------------------------------
# README 获取（仅新项目）
# ---------------------------------------------------------------------------
def _clean_readme(text: str) -> str:
    """轻度清理 README：去 HTML 注释/标签、markdown 徽章链接，压缩空行。

    只为让 LLM 看到真正内容（badge 行常占据开头数百字符），不做深度解析。
    """
    text = _RE_HTML_COMMENT.sub("", text)
    text = _RE_HTML_TAG.sub(" ", text)
    # 反复去除徽章（嵌套形式 [![alt](img)](link) 需两轮）
    for _ in range(2):
        text = _RE_MD_BADGE.sub("", text)
    # 引用式徽章行：[![alt][ref]][ref2] / [ref]: url（行首即链接语法，非正文）
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("[![", "![", "[!")):
            continue
        if re.match(r"^\[[^\]]+\]:\s*\S", stripped):
            continue
        lines.append(line)
    text = "\n".join(lines)
    text = _RE_MULTI_BLANK.sub("\n\n", text)
    return text.strip()


def _api_get(
    url: str,
    github_token: str,
    session: requests.Session,
    *,
    ok_codes: tuple[int, ...] = (200,),
    what: str = "请求",
    retries: int = 1,
) -> requests.Response | None:
    """GitHub API GET（带 Token）。失败/非预期状态码返回 None。

    网络抖动（超时/SSL 中断）快速重试 1 次；404 等常规未命中不重试。
    """
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": UA,
    }
    resp: requests.Response | None = None
    for attempt in range(retries + 1):
        try:
            resp = session.get(url, headers=headers, timeout=15)
            if resp.status_code in ok_codes:
                return resp
            if resp.status_code not in (403, 429, 404):  # 常规未命中不告警
                logger.warning("%s HTTP %d", what, resp.status_code)
            return None
        except requests.RequestException as e:
            if attempt < retries:
                time.sleep(1)
                continue
            logger.warning("%s 失败: %s", what, e)
            return None
    return None


def _find_zh_readme_via_api(
    repo_key: str, github_token: str, session: requests.Session
) -> str | None:
    """用 GitHub API 目录列表发现中文 README 文件名。

    1 次 API 请求即可确定是否存在中文变体，替代对 raw 域名盲试 7 次
    （raw.githubusercontent.com 在国内网络常不可达，盲试最坏 ~105s/项目）。
    """
    resp = _api_get(
        f"https://api.github.com/repos/{repo_key}/contents",
        github_token,
        session,
        ok_codes=(200,),
        what=f"目录列表({repo_key})",
    )
    if resp is None:
        return None
    try:
        names = [i["name"] for i in resp.json() if i.get("type") == "file"]
    except (ValueError, KeyError, TypeError):
        return None
    # 精确候选名优先，其余正则匹配长尾（README.zh-Hans.md 等）
    name_map = {n.lower(): n for n in names}
    for cand in ZH_README_CANDIDATES:
        if cand.lower() in name_map:
            return name_map[cand.lower()]
    for n in names:
        if ZH_README_RE.match(n):
            return n
    return None


def _download_via_api(
    repo_key: str, filename: str, github_token: str, session: requests.Session
) -> str | None:
    """经 API contents 接口下载指定文件（base64 解码），不依赖 raw 域名。"""
    resp = _api_get(
        f"https://api.github.com/repos/{repo_key}/contents/{filename}",
        github_token,
        session,
        ok_codes=(200,),
        what=f"下载({repo_key}/{filename})",
    )
    if resp is None:
        return None
    try:
        b64 = resp.json().get("content", "")
        if not b64:
            return None
        return base64.b64decode(b64).decode("utf-8", errors="replace")
    except (ValueError, KeyError) as e:
        logger.warning("解码失败 %s/%s: %s", repo_key, filename, e)
        return None


def fetch_readme(
    repo: TrendingRepo,
    github_token: str,
    session: requests.Session | None = None,
) -> ReadmeResult:
    """获取项目 README，中文优先。

    链路（设计 §5.2，网络受限优化版）：
      1. GitHub API /readme 拿主 README（base64）
      2. API 目录列表发现中文 README 变体 -> 经 API 下载（不碰 raw 域名）
      3. 主 README 兜底（英文）；raw 直链仅在 API 全失败时尝试
    全部失败 -> description 兜底。单项目失败不抛异常。
    """
    sess = session or requests.Session()
    api_headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": UA,
    }
    key = repo.repo_key
    main_readme: str | None = None

    # 1) 主 README via API
    try:
        resp = _get_with_retry(
            f"https://api.github.com/repos/{key}/readme",
            headers=api_headers,
            ok_codes=(200, 404),
            what=f"README API({key})",
        )
        if resp.status_code == 200:
            data = resp.json()
            content_b64 = data.get("content", "")
            main_readme = base64.b64decode(content_b64).decode(
                "utf-8", errors="replace"
            )
    except (ScraperError, requests.RequestException, ValueError, KeyError) as e:
        logger.warning("README API 失败 %s: %s", key, e)

    # 2) 中文变体：API 目录发现 + API 下载（绕开 raw 域名）
    zh_name = _find_zh_readme_via_api(key, github_token, sess)
    if zh_name:
        content = _download_via_api(key, zh_name, github_token, sess)
        if content and ZH_CHAR_RE.search(content):
            logger.info("命中中文 README: %s/%s", key, zh_name)
            return ReadmeResult(
                content=_clean_readme(content)[:README_LLM_LIMIT],
                is_chinese=True,
                source="zh-readme",
            )
        logger.warning("中文 README 下载失败，回退主 README: %s", key)

    # 3) 主 README（英文或无中文版）
    if main_readme:
        return ReadmeResult(
            content=_clean_readme(main_readme)[:README_LLM_LIMIT],
            is_chinese=False,
            source="api-readme",
        )

    # 4) raw 直链兜底（API 全失败且网络可达时；不可达快速跳过）
    for name in ZH_README_CANDIDATES:
        try:
            resp = sess.get(
                f"https://raw.githubusercontent.com/{key}/HEAD/{name}",
                headers={"User-Agent": UA},
                timeout=8,
            )
        except requests.RequestException:
            break   # raw 域名不可达，剩余候选无需再试
        if resp.status_code == 200 and ZH_CHAR_RE.search(resp.text):
            logger.info("命中中文 README(raw 兜底): %s/%s", key, name)
            return ReadmeResult(
                content=_clean_readme(resp.text)[:README_LLM_LIMIT],
                is_chinese=True,
                source="zh-readme",
            )

    # 5) description 兜底
    logger.warning("README 全链路失败，用 description 兜底: %s", key)
    return ReadmeResult(
        content=repo.description or repo.repo_name,
        is_chinese=False,
        source="description",
    )


# ---------------------------------------------------------------------------
# python3 -m scraper：自检
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import config

    config.setup_logging(verbose=True)
    since = sys.argv[1] if len(sys.argv) > 1 else "weekly"

    print(f"[自检] 拉取 https://github.com/trending?since={since} …")
    try:
        items = fetch_trending(since)
    except ScraperError as e:
        print(f"[FAIL] {e}")
        sys.exit(1)

    print(f"[OK] 解析 {len(items)} 个项目，前 5 个：\n")
    for r in items[:5]:
        print(f"  {r.repo_key:<40} {r.language or '-':<12} "
              f"周+{r.stars_week:<6} 总{r.stars_total:<7} {r.description or ''}")

    if "--readme" in sys.argv:
        from config import load

        cfg = load(since=since)
        print(f"\n[自检] 拉取第一个项目 README …")
        rd = fetch_readme(items[0], cfg.github_token)
        preview = rd.content[:300].replace("\n", " ")
        print(f"[OK] 来源={rd.source} 中文={rd.is_chinese} 长度={len(rd.content)}")
        print(f"     预览: {preview}")
