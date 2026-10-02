# -*- coding: utf-8 -*-
"""L3：与 better-diary 的**真实**契约（跨插件，不是自说自话）。

本文件的独特价值：它不吃我自己编的假 payload，而是**加载日记插件的真实源码**，
调它真正的 ``api_get_day_digest``，再把返回值喂给 ``life_social.parse_digest`` /
``intake_digest``。任何一侧改了字段名或语义，这里立刻红——单侧的假数据测不出漂移。

目录查找顺序：``LF_BETTER_DIARY_DIR`` → 同级目录 → 作者本机路径。
找不到就 SKIP（不算通过、也不算失败），与 ``test_budget_pacer_contract`` 一致。

真机链路：

    生活频率 → ctx.api.call("org.orge-8.better-diary.get_day_digest", version="1", days=2)
             → 日记插件的 @API 处理器 → 只读读自己的 diaries.json
             → {"success": True, "result": <摘要>} → SDK 解包成 dict → 我们解析

两条硬纪律在这里被钉住：**不含日记正文**、**取不到也返回结构完整的 dict（不抛）**。
"""

import asyncio
import importlib.util
import json
import logging
import os
import pathlib
import sys
import types

import pytest

import life_social as SOS

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
API_NAME = "get_day_digest"
TZ = 480


def _entry_candidates() -> tuple[pathlib.Path, ...]:
    candidates: list[pathlib.Path] = []
    env = os.environ.get("LF_BETTER_DIARY_DIR", "").strip()
    if env:
        candidates.append(pathlib.Path(env))
    candidates.extend([
        PLUGIN_DIR.parent / "better-diary",                     # 同级目录（开发机）
        pathlib.Path("plugins") / "better-diary",               # 装进 MaiBot 的常见位置
        pathlib.Path(r"C:\path\to\better-diary"),   # 作者本机兜底
    ])
    return tuple(directory / "plugin.py" for directory in candidates)


def _load_better_diary():
    for entry in _entry_candidates():
        if not entry.is_file():
            continue
        directory = str(entry.parent)
        while directory in sys.path:      # 复现真机：插件目录不在 sys.path 上
            sys.path.remove(directory)
        spec = importlib.util.spec_from_file_location(
            "better_diary_contract", entry, submodule_search_locations=[directory]
        )
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    return None


@pytest.fixture(scope="module")
def better_diary():
    module = _load_better_diary()
    if module is None:
        pytest.skip("未找到 better-diary 源码（可用 LF_BETTER_DIARY_DIR 指定）")
    return module


ARCHIVE = {
    "2026-09-30": {
        "content": "私密日记正文：绝不该出现在摘要里",
        "word_count": 300,
        "generated_at": "2026-09-30 23:41:02",
        "material_mode": "events",
        "events": [
            {"who": "阿岚", "what": "说起她换了个新键盘", "quote": "这个轴体声音太大了",
             "event_id": "ev_a1"},
            {"who": "", "what": "群里在约周末爬山", "quote": "", "event_id": "ev_a2"},
        ],
        "meta": {"topics": ["键盘"], "people": ["阿岚"], "projects": ["还没写完的东西"],
                 "unresolved": ["周末到底去不去"]},
        "published_at": "2026-10-01 00:01:00",
        "model_date_line": "2026年9月29日，晴。",
    },
    "2026-10-01": {
        "content": "第二篇正文",
        "word_count": 200,
        "generated_at": "2026-10-01 23:50:00",
        "material_mode": "timeline_tail",
        "events": [],
        "meta": {"topics": ["下雨"], "people": [], "projects": [], "unresolved": []},
    },
}

CONTINUITY = {
    "previous_summary": "说起她换了个新键盘",
    "important_events": ["说起她换了个新键盘"],
    "ongoing_projects": ["还没写完的东西"],
    "ongoing_topics": ["键盘"],
    "unresolved_items": ["周末到底去不去"],
    "updated_at": "2026-10-01 23:50:00",
}


def _make_better_diary(module, data_dir: pathlib.Path):
    """用临时目录里的存档造一个真插件实例（只读路径不碰 send/db/chat）。"""

    plugin = module.create_plugin()
    ctx = types.SimpleNamespace(
        logger=logging.getLogger("plugin.org.orge-8.better-diary"),
        paths=types.SimpleNamespace(data_dir=str(data_dir)),
    )
    try:
        plugin.ctx = ctx
    except AttributeError:                     # 旧 SDK 用 _ctx
        plugin._ctx = ctx
    plugin.set_plugin_config(
        {"plugin": {"enabled": True, "config_version": "1.0.0"}, "diary": {"max_events": 3}}
    )
    plugin._data_dir = lambda: data_dir        # type: ignore[assignment]
    return plugin, ctx


@pytest.fixture()
def archive_dir(tmp_path):
    (tmp_path / "diaries.json").write_text(
        json.dumps(ARCHIVE, ensure_ascii=False), encoding="utf-8"
    )
    (tmp_path / "continuity.json").write_text(
        json.dumps(CONTINUITY, ensure_ascii=False), encoding="utf-8"
    )
    return tmp_path


def test_the_api_is_declared_public_version_1(better_diary, archive_dir):
    """结构性契约：``@API`` 必须 public + version 1，否则真机跨插件调用直接被拒。"""

    plugin, _ctx = _make_better_diary(better_diary, archive_dir)
    apis = [item for item in plugin.get_components() if item.get("type") == "API"]
    assert apis, "better-diary 没有暴露任何 API 组件"
    entry = next((item for item in apis if item["name"] == API_NAME), None)
    assert entry is not None, [item["name"] for item in apis]
    metadata = entry.get("metadata") or {}
    assert metadata.get("public") is True, "非 public 的 API 跨插件不可见"
    assert str(metadata.get("version")) == "1"
    assert metadata.get("handler_name") == "api_get_day_digest"


def test_real_digest_is_accepted_by_life_social(better_diary, archive_dir):
    """端到端：真 API 的返回 → 解析 → 入库 → 经历条目，全链路对得上。"""

    async def run():
        plugin, _ctx = _make_better_diary(better_diary, archive_dir)
        payload = await plugin.api_get_day_digest(days=2)
        assert isinstance(payload, dict) and payload.get("schema_version") == 1

        result = SOS.parse_digest(payload, tz_offset_minutes=TZ)
        assert result.usable is True, f"生活频率认不出日记插件的返回：{result.reason}"
        assert [item.what for item in result.items] == [
            "说起她换了个新键盘", "群里在约周末爬山",
        ]
        assert result.items[0].who == "阿岚"
        assert result.items[0].quote == "这个轴体声音太大了"
        assert result.items[0].date == "2026-09-30"
        # 时间戳要能对上「本地墙钟」这一口径
        assert result.items[0].at > 0

        seen: dict[str, float] = {}
        now = result.items[0].at + 60
        intake = SOS.intake_digest(
            result.items,
            SOS.IntakeContext(now=now, day_key="2026-10-01", activity="daily",
                              asleep=False, day_used=0.0, policy=SOS.SocialPolicy()),
            seen,
        )
        assert len(intake.events) == 2
        assert intake.events[0]["label"] == "和阿岚聊天"
        assert intake.events[0]["text"] == "说起她换了个新键盘"
        assert intake.emotion_used == pytest.approx(0.8)
        # 再取一次也不会重复入库（event_id 是内容哈希派生的稳定键）
        again = SOS.intake_digest(
            SOS.parse_digest(await plugin.api_get_day_digest(days=2),
                             tz_offset_minutes=TZ).items,
            SOS.IntakeContext(now=now + 60, day_key="2026-10-01", activity="daily",
                              asleep=False, day_used=0.8, policy=SOS.SocialPolicy()),
            seen,
        )
        assert again.events == []

    asyncio.run(run())


def test_the_diary_body_never_leaks_into_the_digest(better_diary, archive_dir):
    """日记正文是她的私密成品：摘要里一个字都不许有。"""

    async def run():
        plugin, _ctx = _make_better_diary(better_diary, archive_dir)
        payload = await plugin.api_get_day_digest(days=30)
        blob = json.dumps(payload, ensure_ascii=False)
        for leaked in ("私密日记正文", "第二篇正文", "published_at", "model_date_line",
                       "word_count"):
            assert leaked not in blob, f"摘要泄漏了 {leaked}"

    asyncio.run(run())


def test_empty_or_missing_archive_is_usable_not_an_error(better_diary, tmp_path):
    """「还没写过日记」和「对方答的不是这个契约」必须区分开：

    前者是正常状态（不告警、不退避），后者是故障（要退避 + 上报）。
    生活频率侧靠 ``DigestResult.usable`` 区分，这条用例把两边的语义钉在一起。
    """

    async def run():
        plugin, _ctx = _make_better_diary(better_diary, tmp_path)   # 空目录
        payload = await plugin.api_get_day_digest()
        assert isinstance(payload, dict) and payload["days"] == []
        assert payload["reason"], "空存档也要给出可读的原因"
        result = SOS.parse_digest(payload, tz_offset_minutes=TZ)
        assert result.usable is True, "空存档不是故障"
        assert result.items == [] and result.reason

        broken = SOS.parse_digest({"success": False, "error": "未找到 API 提供方插件"},
                                  tz_offset_minutes=TZ)
        assert broken.usable is False and "未找到 API 提供方插件" in broken.reason

    asyncio.run(run())
