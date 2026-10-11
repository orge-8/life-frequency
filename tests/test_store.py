# -*- coding: utf-8 -*-
"""L3：SQLite 持久化层（life_store）的契约与降级。

钉住五件事：

1. **两张表与 schema 版本**都在，重开不重建（``IF NOT EXISTS`` + ``user_version``）；
2. **习惯日态按 ``(day_key, line_id)`` 存取**，跨日清理只看 day_key 的字面序；
3. **每次操作开新连接**——跨线程复用同一个 sqlite 连接会炸
   （``check_same_thread=True``），所以这里用真线程并发读写入库来证明接口是安全的；
4. **开不出库时降级成 MemoryStore**，接口一致（不是抛异常把插件拖垮）；
5. **关系档案的 CRUD**（步 5 的租户，先有契约再接线）。
"""

import sqlite3
import threading
from pathlib import Path

import pytest

import life_store as S

DAY = "2026-10-08"


@pytest.fixture()
def store(tmp_path):
    opened = S.open_store(str(tmp_path / "life_store.db"))
    yield opened
    opened.close()


# ---------------------------------------------------------------- 建库


def test_store_is_sqlite_and_creates_both_tables(tmp_path):
    path = tmp_path / "life_store.db"
    store = S.open_store(str(path))
    assert store.backend == "sqlite"
    assert path.is_file()

    conn = sqlite3.connect(str(path))
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    assert {"relationships", "routine_daily"} <= tables, tables
    assert version == S.SCHEMA_VERSION
    store.close()


def test_reopening_does_not_reset_schema_version(tmp_path):
    path = str(tmp_path / "life_store.db")
    first = S.open_store(path)
    first.save_day(DAY, {"abc": (3, S.ROUTINE_FIRED)})
    first.close()

    second = S.open_store(path)
    assert second.backend == "sqlite"
    assert second.load_day(DAY) == {"abc": (3, S.ROUTINE_FIRED)}, "重开后数据要还在"
    second.close()


# ---------------------------------------------------------------- 习惯日态


def test_load_day_starts_empty_and_roundtrips(store):
    assert store.load_day(DAY) == {}
    assert store.save_day(DAY, {"a": (5, S.ROUTINE_PENDING)}) is True
    assert store.load_day(DAY) == {"a": (5, S.ROUTINE_PENDING)}
    # 同一天同一行再写一次是 upsert，不是追加
    store.save_day(DAY, {"a": (-2, S.ROUTINE_FIRED)})
    assert store.load_day(DAY) == {"a": (-2, S.ROUTINE_FIRED)}


def test_days_are_isolated_and_pruned_by_literal_order(store):
    store.save_day("2026-10-07", {"old": (0, S.ROUTINE_FIRED)})
    store.save_day("2026-10-08", {"today": (0, S.ROUTINE_FIRED)})
    store.save_day("2026-10-09", {"future": (0, S.ROUTINE_PENDING)})

    assert store.load_day("2026-10-08") == {"today": (0, S.ROUTINE_FIRED)}
    # 严格小于：当天与未来都不能动
    assert store.prune_before("2026-10-08") == 1
    assert store.load_day("2026-10-07") == {}
    assert store.load_day("2026-10-09") != {}


def test_empty_and_blank_writes_are_noops(store):
    assert store.save_day(DAY, {}) is False
    assert store.save_day(DAY, {"": (1, 0), "   ": (1, 0)}) is False
    assert store.load_day(DAY) == {}


def test_concurrent_access_from_different_threads(store):
    """跨线程复用连接会炸，所以每个操作必须自带连接——用真线程证明它成立。"""

    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            store.save_day(DAY, {f"line{index}": (index, S.ROUTINE_PENDING)})
            store.load_day(DAY)
        except BaseException as exc:  # noqa: BLE001 —— 收集起来断言「没有异常」
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == [], errors
    assert len(store.load_day(DAY)) == 8


# ---------------------------------------------------------------- 关系档案


def test_relationship_crud(store):
    assert store.get_relationship("10001") is None

    record = S.new_relationship("10001", now=1000.0)
    assert record["user_id"] == "10001"
    assert record["first_seen_at"] == 1000.0
    assert record["familiarity"] == 0.0

    assert store.put_relationship(record) is True
    fetched = store.get_relationship("10001")
    assert fetched is not None and fetched["user_id"] == "10001"

    fetched["familiarity"] = 42.0
    fetched["last_interaction_at"] = 2000.0
    store.put_relationship(fetched)  # upsert，不是重复插入
    assert store.get_relationship("10001")["familiarity"] == 42.0

    assert store.delete_relationship("10001") is True
    assert store.get_relationship("10001") is None
    assert store.delete_relationship("10001") is False


def test_top_relationships_orders_by_familiarity(store):
    for index, familiarity in enumerate((10.0, 80.0, 50.0)):
        record = S.new_relationship(f"u{index}", now=1000.0 + index)
        record["familiarity"] = familiarity
        store.put_relationship(record)

    top = store.top_relationships(limit=2)
    assert [item["user_id"] for item in top] == ["u1", "u2"], top
    assert store.top_relationships(limit=0) == []


def test_prune_relationships_drops_the_stalest(store):
    for index in range(5):
        record = S.new_relationship(f"u{index}", now=1000.0 + index)
        store.put_relationship(record)

    assert store.prune_relationships(keep=2) == 3
    remaining = {item["user_id"] for item in store.top_relationships(limit=10)}
    assert remaining == {"u3", "u4"}, remaining
    assert store.prune_relationships(keep=10) == 0


def test_relationship_rows_normalizes_foreign_records():
    cleaned = S.relationship_rows(
        [
            {"user_id": " 42 ", "familiarity": "30", "unknown": "x"},
            {"user_id": "", "familiarity": 5},          # 无主键：丢弃
            "not a mapping",                             # 脏类型：丢弃
        ]
    )
    assert [item["user_id"] for item in cleaned] == ["42"]
    assert cleaned[0]["familiarity"] == 30.0
    assert "unknown" not in cleaned[0]


# ---------------------------------------------------------------- 降级


def test_memory_backend_has_the_same_interface():
    store = S.MemoryStore()
    assert store.backend == "memory"
    store.save_day(DAY, {"a": (1, S.ROUTINE_FIRED)})
    assert store.load_day(DAY) == {"a": (1, S.ROUTINE_FIRED)}
    assert store.prune_before("2026-10-09") == 1
    store.put_relationship(S.new_relationship("7", now=1.0))
    assert store.get_relationship("7")["user_id"] == "7"
    assert store.prune_relationships(keep=0) == 1
    store.close()
    assert store.load_day(DAY) == {}


def test_unopenable_path_falls_back_to_memory_and_reports(tmp_path):
    """路径是个目录 ⇒ SQLite 开不出来。这时候必须降级，绝不能让插件起不来。"""

    directory = tmp_path / "not-a-file"
    directory.mkdir()
    messages: list[str] = []
    store = S.open_store(str(directory), on_error=messages.append)
    assert isinstance(store, S.MemoryStore), type(store)
    assert store.backend == "memory"
    assert messages, "降级必须留痕，否则现场无从得知习惯层在裸奔"
    # 降级后接口照常可用
    store.save_day(DAY, {"a": (0, S.ROUTINE_PENDING)})
    assert store.load_day(DAY) == {"a": (0, S.ROUTINE_PENDING)}


def test_empty_path_falls_back_to_memory():
    messages: list[str] = []
    store = S.open_store("", on_error=messages.append)
    assert store.backend == "memory"
    assert messages


def test_broken_store_keeps_answering_with_defaults(tmp_path):
    """库坏了之后：读回空、写回 False，而不是抛异常打断 tick。"""

    store = S.LifeStore(str(tmp_path / "broken.db"))
    store.close()  # 标记 broken
    assert store.load_day(DAY) == {}
    assert store.save_day(DAY, {"a": (0, 0)}) is False
    assert store.prune_before(DAY) == 0
    assert store.get_relationship("1") is None
    assert store.top_relationships() == []


def test_store_never_touches_the_plugin_directory(tmp_path):
    """数据库必须落在 Runner 给的 data_dir，绝不能污染插件目录。"""

    plugin_dir = Path(__file__).resolve().parent.parent
    store = S.open_store(str(tmp_path / "x.db"))
    store.save_day(DAY, {"a": (0, 0)})
    store.close()
    assert not (plugin_dir / "life_store.db").exists()
