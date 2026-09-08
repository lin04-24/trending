"""llm.py — 调用 OpenAI 兼容接口，一次生成四项中文内容。

产出（严格 JSON）：
  中文名 / 小介绍（≤30字）/ 中文分类 / 大介绍（≤300字，不含安装类内容）

兜底链（设计 §5.3）：
  主供应商（首次 + 重试2次）-> 副供应商（如配置，首次 + 重试1次）-> description 原文

生成温度由 env 的 LLM_TEMPERATURE 控制（默认 0.3，主副供应商共用）。
渠道故障判定：大介绍汉字数低于阈值（如整段返回英文）视为本次生成失败，
沿重试链降级。超时 60s，重试间递增间隔（2s → 4s）；解析失败正则提取 {...}。
"""

from __future__ import annotations

import json
import logging
import re
import sys
from dataclasses import dataclass

from openai import OpenAI

from config import AppConfig

logger = logging.getLogger("llm")

# LLM 调用超时（秒）与重试策略
LLM_TIMEOUT = 60
PRIMARY_MAX_ATTEMPTS = 3      # 主供应商：首次 + 重试 2 次
BACKUP_MAX_ATTEMPTS = 2       # 副供应商：首次 + 重试 1 次
RETRY_BACKOFF_BASE = 2        # 重试间隔基数（秒）：第 n 次失败后等 n*base

# 小介绍字数上限（提示词约束）；大介绍仅设宽松安全上限，防止 LLM 失控输出，
# 正常略超 300 字不截断（邮件端默认折叠展示，超长不影响浏览）
FULL_INTRO_LIMIT = 1000
BRIEF_INTRO_LIMIT = 30

# 四项字段长度硬限（入库保护，VARCHAR(300)/VARCHAR(50)）
_BRIEF_DB_LIMIT = 300
_CATEGORY_DB_LIMIT = 50

# 大介绍汉字数下限：低于该值判定为供应商渠道失败（如整段返回英文），
# 触发重试 / 切换副供应商。正常 300 字中文介绍远高于此值。
FULL_INTRO_MIN_HANZI = 50

_HANZI_RE = re.compile(r"[\u4e00-\u9fff]")


class LLMError(Exception):
    """LLM 调用或解析失败。"""


@dataclass(frozen=True)
class RepoSummary:
    """一次 LLM 调用的产出（四项中文内容 + 元信息）。"""

    zh_name: str            # 中文名
    brief_intro: str        # 小介绍：中文名+用途 ≤30字
    category: str           # 中文分类，如 "AI 工具"
    full_intro: str         # 大介绍：详细中文介绍 ≤300字
    llm_ok: bool            # False = 兜底数据（description 原文）
    elapsed: float = 0.0    # LLM 耗时（秒），写日志用


SYSTEM_PROMPT = """你是一名 GitHub 项目分析师。根据提供的项目资料，输出中文介绍。
你必须只输出一个 JSON 对象，不要输出任何其他文字、markdown 代码块标记或解释。
JSON 的四个键：
{
  "中文名": "项目的中文译名或意译名（保留专有名词，如 TensorFlow 可叫 TensorFlow）",
  "小介绍": "中文名+一句话用途，30字以内",
  "中文分类": "该项目的中文章节归类，从这些里选一个：AI 工具 / 前端框架 / 后端框架 / 命令行工具 / 学习资源 / 开发库 / 安全工具 / 数据工具 / 系统工具 / 效率工具 / 其他",
  "大介绍": "项目是什么、解决什么问题、核心功能。300字以内。禁止包含安装、构建、快速开始、命令示例等操作性内容"
}"""


def _build_user_prompt(
    repo_name: str,
    description: str | None,
    readme: str,
    is_chinese_readme: bool,
) -> str:
    lang_note = (
        "项目自带中文 README，请直接基于它精简提炼，不要翻译。"
        if is_chinese_readme
        else "项目 README 为英文（或仅有英文描述），请翻译并精简为中文。"
    )
    return (
        f"项目：{repo_name}\n"
        f"GitHub 简介：{description or '（无）'}\n"
        f"README 片段：\n{readme}\n\n"
        f"要求：{lang_note}"
    )


def parse_llm_json(text: str) -> dict[str, str]:
    """解析 LLM 输出为 dict。兼容 markdown 代码块包裹。纯函数，可测试。"""
    text = text.strip()
    # 剥离 ```json ... ``` 或 ``` ... ```
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # 正则提取第一个 {...}（平衡性不校验，LLM 输出多为单层）
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            raise LLMError(f"无法从 LLM 输出提取 JSON: {text[:200]!r}")
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError as e:
            raise LLMError(f"LLM 输出 JSON 解析失败: {e}") from e

    if not isinstance(data, dict):
        raise LLMError(f"LLM 输出不是 JSON 对象: {type(data).__name__}")
    return {str(k): str(v).strip() for k, v in data.items()}


def _truncate(text: str, limit: int) -> str:
    """按字数截断（中文友好，超限加省略号）。"""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _fallback(repo_desc: str | None, repo_name: str, elapsed: float) -> RepoSummary:
    """兜底：LLM 失败时用 description 原文。"""
    desc = (repo_desc or "").strip()
    full = desc if desc else f"{repo_name}（暂无简介，LLM 生成失败）"
    return RepoSummary(
        zh_name=repo_name,
        brief_intro=_truncate(desc or repo_name, BRIEF_INTRO_LIMIT),
        category="未分类",
        full_intro=full,
        llm_ok=False,
        elapsed=elapsed,
    )


def _hanzi_count(text: str) -> int:
    """汉字数量（CJK 基本区），用于判定产出是否为中文。"""
    return len(_HANZI_RE.findall(text))


@dataclass(frozen=True)
class _Provider:
    """一个供应商渠道及其重试预算。"""

    label: str
    base_url: str
    model: str
    api_key: str
    max_attempts: int


def _providers(cfg: AppConfig) -> list[_Provider]:
    """主 -> 副供应商链条；副供应商三项任缺其一即视为未配置。"""
    chain = [
        _Provider(
            "主供应商", cfg.llm_base_url, cfg.llm_model,
            cfg.llm_api_key, PRIMARY_MAX_ATTEMPTS,
        )
    ]
    if all(
        (cfg.llm_backup_base_url, cfg.llm_backup_model, cfg.llm_backup_api_key)
    ):
        chain.append(
            _Provider(
                "副供应商", cfg.llm_backup_base_url, cfg.llm_backup_model,
                cfg.llm_backup_api_key, BACKUP_MAX_ATTEMPTS,
            )
        )
    else:
        logger.debug(
            "副供应商未配置（LLM_BACKUP_BASE_URL/MODEL/API_KEY 需同时填写），"
            "主供应商重试耗尽后直接兜底"
        )
    return chain


def _make_client(base_url: str, api_key: str) -> OpenAI:
    return OpenAI(
        base_url=base_url,
        api_key=api_key,
        timeout=LLM_TIMEOUT,
        max_retries=0,          # 重试由本模块控制
    )


def _call_once(
    client: OpenAI, model: str, user_prompt: str, temperature: float
) -> dict[str, str]:
    """单次调用 + 产出校验；任何不合格均抛 LLMError（重试与否由调用方决定）。"""
    kwargs: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
    }
    try:
        # response_format 若网关支持则启用严格 JSON
        resp = client.chat.completions.create(
            **kwargs, response_format={"type": "json_object"}
        )
    except Exception as e_fmt:  # noqa: BLE001 - 网关兼容性探测
        warn = str(e_fmt)
        if any(
            kw in warn.lower()
            for kw in ("response_format", "json_object", "json mode")
        ):
            logger.info("网关不支持 response_format，改用提示词约束")
            resp = client.chat.completions.create(**kwargs)
        else:
            raise

    raw = resp.choices[0].message.content
    if not raw:
        raise LLMError("LLM 返回空内容")
    data = parse_llm_json(raw)

    full = data.get("大介绍") or ""
    if not full:
        raise LLMError("大介绍为空")
    hanzi = _hanzi_count(full)
    if hanzi < FULL_INTRO_MIN_HANZI:
        raise LLMError(
            f"大介绍仅 {hanzi} 个汉字（阈值 {FULL_INTRO_MIN_HANZI}），"
            "疑似渠道故障返回英文"
        )
    return data


def summarize(
    cfg: AppConfig,
    repo_name: str,
    description: str | None,
    readme: str,
    is_chinese_readme: bool,
) -> RepoSummary:
    """一次 LLM 调用生成四项中文内容。任何失败 -> 兜底（不抛异常）。

    重试链：主供应商 首次+重试2次 -> 副供应商（如配置）首次+重试1次。
    单项目 LLM 失败不影响整体流水线（设计 §3.3）。
    """
    import time as _time

    started = _time.perf_counter()
    user_prompt = _build_user_prompt(repo_name, description, readme, is_chinese_readme)

    last_err: Exception | None = None
    for prov in _providers(cfg):
        client = _make_client(prov.base_url, prov.api_key)
        for attempt in range(1, prov.max_attempts + 1):
            try:
                data = _call_once(client, prov.model, user_prompt, cfg.llm_temperature)

                zh_name = data.get("中文名") or repo_name
                brief = data.get("小介绍") or ""
                category = data.get("中文分类") or "未分类"
                if not brief:
                    brief = f"{zh_name}：详见大介绍"

                elapsed = _time.perf_counter() - started
                logger.info(
                    "LLM 完成 %s：%s %.1fs（%s/%s）",
                    repo_name, prov.label, elapsed, category, zh_name,
                )
                return RepoSummary(
                    zh_name=_truncate(zh_name, 100),
                    brief_intro=_truncate(brief, _BRIEF_DB_LIMIT),
                    category=_truncate(category, _CATEGORY_DB_LIMIT),
                    full_intro=_truncate(data["大介绍"], FULL_INTRO_LIMIT),
                    llm_ok=True,
                    elapsed=elapsed,
                )

            except Exception as e:  # noqa: BLE001 - 失败沿重试链降级
                last_err = e
                if attempt < prov.max_attempts:
                    delay = RETRY_BACKOFF_BASE * attempt
                    logger.warning(
                        "%s 第 %d/%d 次失败 %s: %s，%ds 后重试",
                        prov.label, attempt, prov.max_attempts, repo_name, e, delay,
                    )
                    _time.sleep(delay)
                else:
                    logger.error(
                        "%s 重试耗尽 %s: %s（%.1fs）",
                        prov.label, repo_name, e, _time.perf_counter() - started,
                    )

    logger.error(
        "全部供应商失败 %s: %s（%.1fs），使用 description 兜底",
        repo_name, last_err, _time.perf_counter() - started,
    )
    return _fallback(description, repo_name, _time.perf_counter() - started)


# ---------------------------------------------------------------------------
# python3 -m llm：自检
# ---------------------------------------------------------------------------
def _self_test() -> int:
    """离线自检：JSON 解析器各分支。"""
    ok = 0

    def check(name: str, cond: bool) -> None:
        nonlocal ok
        mark = "OK " if cond else "FAIL"
        print(f"  [{mark}] {name}")
        if cond:
            ok += 1

    print("[LLM 解析自检]")

    # 纯 JSON
    d = parse_llm_json('{"中文名": "向量库", "小介绍": "x", "中文分类": "AI 工具", "大介绍": "y"}')
    check("纯 JSON", d["中文名"] == "向量库")

    # 代码块包裹
    d = parse_llm_json('```json\n{"中文名": "a", "大介绍": "b"}\n```')
    check("markdown 包裹", d["中文名"] == "a")

    # 前后噪音
    d = parse_llm_json('好的，以下是结果：\n{"中文名": "b", "大介绍": "c"}\n希望有帮助')
    check("前后噪音提取", d["中文名"] == "b")

    # 数字值转字符串
    d = parse_llm_json('{"中文名": 123}')
    check("非字符串值", d["中文名"] == "123")

    # 非法输入
    try:
        parse_llm_json("完全不是 JSON")
        check("非法输入报错", False)
    except LLMError:
        check("非法输入报错", True)

    # 汉字计数（渠道故障判定依据）
    check("汉字计数（中文）", _hanzi_count("这是一个用于测试的中文介绍") == 13)
    check("汉字计数（英文/空）", _hanzi_count("Open source server, built with Rust!") == 0)

    print(f"\n解析自检 {ok}/7 通过")
    return 0 if ok == 7 else 1


def _live_test() -> int:
    """实调测试：真实 API 生成一个项目的四项内容。"""
    import config as config_mod

    config_mod.setup_logging(verbose=True)
    from config import load

    cfg = load()
    print(f"[实调] 主供应商 {cfg.llm_base_url} 模型 {cfg.llm_model}")
    if cfg.llm_backup_base_url:
        print(
            f"[实调] 副供应商 {cfg.llm_backup_base_url} 模型 {cfg.llm_backup_model}"
        )
    else:
        print("[实调] 副供应商未配置")
    s = summarize(
        cfg,
        repo_name="ripgrep",
        description="ripgrep recursively searches directories for a regex pattern",
        readme=(
            "# ripgrep\n\nripgrep is a line-oriented search tool that recursively "
            "searches the current directory for a regex pattern. By default, "
            "ripgrep will respect gitignore rules and skip hidden files.\n\n"
            "## Speed\nIt is generally faster than any other search tool."
        ),
        is_chinese_readme=False,
    )
    print(f"\n  llm_ok   : {s.llm_ok}")
    print(f"  中文名  : {s.zh_name}")
    print(f"  小介绍  : {s.brief_intro}")
    print(f"  中文分类: {s.category}")
    print(f"  大介绍  : {s.full_intro}")
    print(f"  耗时    : {s.elapsed:.1f}s")
    return 0 if s.llm_ok else 1


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    if "--live-test" in sys.argv:
        sys.exit(_live_test())
    print("用法: python3 -m llm --self-test | --live-test")
    sys.exit(0)
