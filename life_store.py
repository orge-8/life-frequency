# -*- coding: utf-8 -*-
"""SQLite 持久化层：关系档案与习惯日态（纯模块，无 ctx、无网络、可脱机单测）。

为什么要有第二份持久化（而不是继续塞 ``life_state.json``）：

* ``life_state.json`` 是**每 tick 全量重写**的高频小状态，适合标量；
* 关系档案是**会增长的集合**（按人建档，200 条上限），习惯日态是**按天过期**
  的表格（键 ``(day_key, line_id)``）。把它们塞进标量文件，等于每次推进都要
  重写一遍根本没变的东西，而且淘汰/过期这类动作在 JSON 里只能手写 LRU。

设计纪律（跨项目踩坑直接落成的规范）：

1. **每次操作开新连接 + 一把全局锁**：``sqlite3`` 默认 ``check_same_thread=True``，
   跨线程复用同一个连接必炸；插件侧所有读写都经 ``asyncio.to_thread``（事件循环里
   只做内存操作），线程是不固定的 ⇒ 连接不能跨调用存活。
   **光开新连接还不够**：实测（Windows + WAL）四个线程同时写会撞出
   ``attempt to write a readonly database``——最后一个连接关闭时 WAL 的 ``-wal`` /
   ``-shm`` 文件会被删掉，下一次冷启动时多个连接抢着重建它们就会踩空
   （同一个库、同样的语句，串行执行 100% 成功）。所以每次操作再套一把锁：
   反正写入量是「每 10 分钟几行」，串行化的代价可以忽略，换来的是确定性。
2. **开 WAL**：tick 的写与其它线程的读不互锁（配合上面的锁，实际是串行但**不阻塞
   别的进程**——WAL 的意义在这）。
3. **``user_version`` 做 schema 版本**：启动时 ``CREATE TABLE IF NOT EXISTS``，
   迁移钩子留在这一个地方。
4. **不 import ctx**：路径由 ``plugin.py`` 注入（``ctx.paths.data_dir``）。
5. **开不出库就降级**：磁盘不可写 / 路径非法 / SQLite 不可用时回落到
   ``MemoryStore``（进程内字典），习惯层照常工作，只是重启后当日状态丢失。
   「数据库坏了 = 插件不工作」是不可接受的故障形态。
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from typing import Any, Callable, Iterable, Mapping, Sequence

#: schema 版本。改表结构时 +1，并在 ``_migrate`` 里补迁移步骤。
#: v2（2026-10-16，Y4）：``relationships`` 主键由 ``user_id`` 改为 ``person_id``。
SCHEMA_VERSION = 3

#: 默认平台。载荷没给 ``platform`` 时用它，也是 v1 关系数据的迁移假设。
DEFAULT_PLATFORM = "qq"

#: ``routine_daily.fired`` 的三态：0 当日未定论 / 1 已命中 / 2 当日已放弃（权重未中）。
#: 「已放弃」必须与「已命中」分开存，否则权重没中的那一行会在窗口内被反复重掷，
#: 一个 0.8 权重的习惯在 40 分钟窗口里迟早会中——那权重就形同虚设。
ROUTINE_PENDING = 0
ROUTINE_FIRED = 1
ROUTINE_SKIPPED = 2

_DEFAULT_CONNECT_TIMEOUT = 5.0

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS relationships (
      person_id TEXT PRIMARY KEY,
      platform TEXT NOT NULL DEFAULT 'qq',
      user_id TEXT NOT NULL DEFAULT '',
      first_seen_at REAL NOT NULL,
      last_interaction_at REAL NOT NULL,
      interaction_count INTEGER NOT NULL DEFAULT 0,
      familiarity REAL NOT NULL DEFAULT 0,
      relation_hint TEXT NOT NULL DEFAULT '',
      shared_events INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS routine_daily (
      day_key TEXT NOT NULL,
      line_id TEXT NOT NULL,
      jitter_minutes INTEGER NOT NULL,
      fired INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY (day_key, line_id)
    )
    """,
    # v1.23.0（sleep backlog）：她睡着时进来的消息。
    # 放 SQLite 而不是 ``life_state.json`` 的理由有两条：①状态文件是**整份序列化**
    # 落盘的（每个 tick 一次），长文本会把 tick 拖慢；②这里存的是**原文**，
    # 条数与保留时长都要能单独清理，混进状态文件就只能跟着状态一起截断。
    """
    CREATE TABLE IF NOT EXISTS sleep_backlog (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      person_id TEXT NOT NULL,
      session_id TEXT NOT NULL,
      user_id TEXT NOT NULL DEFAULT '',
      platform TEXT NOT NULL DEFAULT 'qq',
      is_group INTEGER NOT NULL DEFAULT 0,
      at REAL NOT NULL,
      text TEXT NOT NULL DEFAULT '',
      delivered_at REAL NOT NULL DEFAULT 0,
      caught_up_at REAL NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_sleep_backlog_session
      ON sleep_backlog (session_id, at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_sleep_backlog_person
      ON sleep_backlog (person_id, at DESC)
    """,
)

#: ``sleep_backlog`` 的取数列（与建表顺序一致；本模块没有 row_factory，只能显式 zip）
_SLEEP_BACKLOG_FIELDS = (
    "id",
    "person_id",
    "session_id",
    "user_id",
    "platform",
    "is_group",
    "at",
    "text",
    "delivered_at",
    "caught_up_at",
)

#: ``pending_sleep_sessions`` 的取数列（聚合查询，顺序必须与 SQL 一致）
_SLEEP_SESSION_FIELDS = (
    "session_id",
    "person_id",
    "user_id",
    "platform",
    "is_group",
    "count",
    "last_at",
    "ids",
    "delivered_at",
)

_RELATIONSHIP_FIELDS = (
    "person_id",
    "platform",
    "user_id",
    "first_seen_at",
    "last_interaction_at",
    "interaction_count",
    "familiarity",
    "relation_hint",
    "shared_events",
)

#: ``_RELATIONSHIP_FIELDS`` 对应的默认值（``put_relationship`` 与 MemoryStore 共用）。
_RELATIONSHIP_DEFAULTS: tuple[tuple[str, Any], ...] = (
    ("person_id", ""),
    ("platform", DEFAULT_PLATFORM),
    ("user_id", ""),
    ("first_seen_at", 0.0),
    ("last_interaction_at", 0.0),
    ("interaction_count", 0),
    ("familiarity", 0.0),
    ("relation_hint", ""),
    ("shared_events", 0),
)


def person_id_for(platform: object, user_id: object) -> str:
    """``(platform, user_id)`` → ``person_id``（v1.20.0，Y4）。

    复刻宿主 ``src/person_info/person_info.py::get_person_id``：

    ```
    platform 形如 "webchat-abc" 时取 "-" 后半段
    person_id = md5(f"{platform}_{user_id}")
    ```

    **为什么要对齐宿主算法**：对齐之后本表与宿主的人物画像（A_Memorix 等）用的是
    同一个 ``person_id``，两边可以直接 join，不需要额外的映射表。
    代价是**与宿主算法耦合**——宿主若更换该算法，本表历史数据会对不上
    （这条代价写在 README「已知限制」里，升级宿主大版本时要核对）。

    认不出人（``user_id`` 为空）返回空串：调用方据此跳过建档，宁可不记也不记错人。

    放在 ``life_store`` 而不是 ``life_relations``：v1→v2 的关系表迁移要在这一层
    算 ``person_id``，而 SQLite 没有 md5；反过来放会让 ``life_store`` 反向
    import ``life_relations``，构成循环。
    """

    uid = str(user_id or "").strip()
    if not uid:
        return ""
    name = str(platform or DEFAULT_PLATFORM).strip() or DEFAULT_PLATFORM
    if "-" in name:
        name = name.split("-")[1]
    return hashlib.md5(f"{name}_{uid}".encode("utf-8")).hexdigest()


def new_relationship(
    person_id: str,
    *,
    now: float,
    platform: str = DEFAULT_PLATFORM,
    user_id: str = "",
    familiarity: float = 0.0,
    relation_hint: str = "",
) -> dict[str, Any]:
    """一条新的关系档案（字段与 ``relationships`` 表一一对应）。

    步 5（关系模型）消费；本模块只负责把它存进去、取出来。

    v1.20.0（Y4）：主键是 ``person_id``；``platform`` / ``user_id`` 是展示与排障
    用的普通字段（``user_id`` 供脱敏显示，见 ``life_relations.mask_user_id``）。
    """

    return {
        "person_id": str(person_id or "").strip(),
        "platform": str(platform or DEFAULT_PLATFORM).strip()[:32] or DEFAULT_PLATFORM,
        "user_id": str(user_id or "").strip()[:64],
        "first_seen_at": float(now),
        "last_interaction_at": float(now),
        "interaction_count": 0,
        "familiarity": float(familiarity),
        "relation_hint": str(relation_hint or "")[:64],
        "shared_events": 0,
    }


def _migrate_relationships_to_v2(conn: sqlite3.Connection) -> int:
    """v1 → v2：``relationships`` 主键 ``user_id`` → ``person_id``。

    **为什么要重建表**：SQLite 改不了主键，只能「改名旧表 → 建新表 → 搬数据 →
    删备份」。整段跑在 ``_init_schema`` 的同一个事务里——调用方只在全部成功后才
    ``commit``，中途抛异常会整体回滚，不会留下半张表。

    **SQLite 没有 md5** ⇒ 行必须读进 Python 算完 ``person_id`` 再写回。

    **老行的平台无从得知**（v1 不存 ``platform``）：一律按 ``DEFAULT_PLATFORM``
    处理。本插件实际部署是单平台（QQ），这个假设与现状一致；万一不是，该行
    下一次互动就会被 ``life_relations.touch`` 按真实 ``platform`` 刷新。

    返回搬迁的行数（供日志）。表不存在、或已是 v2 结构时返回 0（幂等）。
    """

    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "relationships" not in tables:
        return 0
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(relationships)")}
    if "person_id" in columns:
        return 0  # 已经是 v2 结构（例如用户手动降过 user_version）

    rows = conn.execute(
        "SELECT user_id, first_seen_at, last_interaction_at, interaction_count, "
        "familiarity, relation_hint, shared_events FROM relationships"
    ).fetchall()

    conn.execute("ALTER TABLE relationships RENAME TO relationships_v1_backup")
    conn.execute(_SCHEMA_STATEMENTS[0])
    migrated = 0
    for row in rows:
        uid = str(row[0] or "").strip()
        pid = person_id_for(DEFAULT_PLATFORM, uid)
        if not pid:
            continue  # 认不出人的历史行直接丢弃（本来就无法归属）
        conn.execute(
            "INSERT OR REPLACE INTO relationships (person_id, platform, user_id, "
            "first_seen_at, last_interaction_at, interaction_count, familiarity, "
            "relation_hint, shared_events) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                pid,
                DEFAULT_PLATFORM,
                uid[:64],
                row[1],
                row[2],
                row[3],
                row[4],
                str(row[5] or "")[:64],
                row[6],
            ),
        )
        migrated += 1
    conn.execute("DROP TABLE relationships_v1_backup")
    return migrated


def _is_id(value: object) -> bool:
    """能不能当成一个积压行 id。``bool`` 要排除（``True`` 会被 ``int`` 收成 1）。"""

    if isinstance(value, bool) or value is None:
        return False
    try:
        return int(value) > 0
    except (TypeError, ValueError):
        return False


class MemoryStore:
    """进程内兜底：库开不出来时顶上，接口与 :class:`LifeStore` 完全一致。

    语义差异只有一条——**重启即丢**（当日习惯状态会重新掷一次抖动、可能重复命中一次）。
    这比「数据库不可用 ⇒ 习惯层整体罢工」温和得多。
    """

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], tuple[int, int]] = {}
        self._people: dict[str, dict[str, Any]] = {}
        self._backlog: list[dict[str, Any]] = []
        self._backlog_seq = 0
        self._lock = threading.Lock()

    # ---- 习惯日态 ----

    def load_day(self, day_key: str) -> dict[str, tuple[int, int]]:
        key = str(day_key or "")
        with self._lock:
            return {
                line_id: value
                for (day, line_id), value in self._rows.items()
                if day == key
            }

    def save_day(self, day_key: str, rows: Mapping[str, Sequence[int]]) -> bool:
        key = str(day_key or "")
        with self._lock:
            for line_id, value in rows.items():
                line_key = str(line_id or "").strip()
                if not line_key:
                    continue
                jitter = int(value[0]) if len(value) > 0 else 0
                status = int(value[1]) if len(value) > 1 else ROUTINE_PENDING
                self._rows[(key, line_key)] = (jitter, status)
        return True

    def prune_before(self, day_key: str) -> int:
        """删掉严格小于 ``day_key`` 的全部行（``day_key`` 是 ``YYYY-MM-DD`` 文本）。"""

        threshold = str(day_key or "")
        with self._lock:
            stale = [item for item in self._rows if item[0] < threshold]
            for item in stale:
                self._rows.pop(item, None)
        return len(stale)

    # ---- 关系档案 ----

    def get_relationship(self, person_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._people.get(str(person_id or "").strip())
        return dict(record) if record else None

    def put_relationship(self, record: Mapping[str, Any]) -> bool:
        pid = str(record.get("person_id") or "").strip()
        if not pid:
            # 与 ``LifeStore.put_relationship`` 同口径：能从 (platform, user_id)
            # 推导就不该静默丢（两个后端的接口语义必须一致，否则「换后端行为不同」）。
            pid = person_id_for(record.get("platform"), record.get("user_id"))
        if not pid:
            return False
        with self._lock:
            row = {field: record.get(field, default) for field, default in _RELATIONSHIP_DEFAULTS}
            row["person_id"] = pid
            row["user_id"] = str(row.get("user_id") or "")[:64]
            row["platform"] = str(row.get("platform") or DEFAULT_PLATFORM)[:32] or DEFAULT_PLATFORM
            row["relation_hint"] = str(row.get("relation_hint") or "")[:64]
            self._people[pid] = row
        return True

    def top_relationships(self, limit: int = 3) -> list[dict[str, Any]]:
        with self._lock:
            records = [dict(item) for item in self._people.values()]
        records.sort(
            key=lambda item: (
                -float(item.get("familiarity") or 0.0),
                -float(item.get("last_interaction_at") or 0.0),
            )
        )
        return records[: max(0, int(limit))]

    def prune_relationships(self, keep: int = 200) -> int:
        limit = max(0, int(keep))
        with self._lock:
            if len(self._people) <= limit:
                return 0
            ordered = sorted(
                self._people.items(),
                key=lambda item: float(item[1].get("last_interaction_at") or 0.0),
            )
            victims = [key for key, _ in ordered[: len(ordered) - limit]]
            for key in victims:
                self._people.pop(key, None)
        return len(victims)

    def delete_relationship(self, person_id: str) -> bool:
        with self._lock:
            return self._people.pop(str(person_id or "").strip(), None) is not None

    # ---- 睡眠期积压（sleep backlog，v1.23.0）----
    #
    # 与 SQLite 后端的差异：**重启即丢**。语义上可以接受（补答本来就是「这一觉」
    # 的事），但必须在模块文档里写清——否则「重启后补答全没了」会被当成 bug 排查。

    def add_sleep_backlog(
        self,
        person_id: str,
        session_id: str,
        *,
        at: float,
        text: str,
        user_id: str = "",
        platform: str = DEFAULT_PLATFORM,
        is_group: bool = False,
        max_chars: int = 400,
    ) -> int:
        pid = str(person_id or "").strip()
        sid = str(session_id or "").strip()
        if not pid or not sid:
            return 0
        body = str(text or "").strip()
        limit = int(max_chars)
        if limit <= 0:
            limit = 400
        with self._lock:
            self._backlog_seq += 1
            row_id = self._backlog_seq
            self._backlog.append(
                {
                    "id": row_id,
                    "person_id": pid,
                    "session_id": sid,
                    "user_id": str(user_id or "")[:64],
                    "platform": str(platform or DEFAULT_PLATFORM)[:32],
                    "is_group": 1 if is_group else 0,
                    "at": float(at),
                    "text": body[:limit],
                    "delivered_at": 0.0,
                    "caught_up_at": 0.0,
                }
            )
        return row_id

    def peek_sleep_backlog(self, person_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        pid = str(person_id or "").strip()
        if not pid:
            return []
        size = max(1, min(200, int(limit)))
        with self._lock:
            rows = [
                dict(item)
                for item in self._backlog
                if item["person_id"] == pid and float(item["caught_up_at"]) <= 0.0
            ]
        rows.sort(key=lambda item: float(item["at"]))
        return rows[-size:]

    def pending_sleep_sessions(self, *, since: float = 0.0, limit: int = 50) -> list[dict[str, Any]]:
        cutoff = float(since or 0.0)
        size = max(1, min(200, int(limit)))
        with self._lock:
            rows = [
                dict(item)
                for item in self._backlog
                if float(item["caught_up_at"]) <= 0.0 and float(item["at"]) >= cutoff
            ]
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            bucket = grouped.setdefault(
                str(row["session_id"]),
                {
                    "session_id": str(row["session_id"]),
                    "person_id": str(row["person_id"]),
                    "user_id": str(row["user_id"]),
                    "platform": str(row["platform"]),
                    "is_group": int(row["is_group"]),
                    "count": 0,
                    "last_at": 0.0,
                    "ids": [],
                    "delivered_at": 0.0,
                },
            )
            bucket["count"] = int(bucket["count"]) + 1
            bucket["last_at"] = max(float(bucket["last_at"]), float(row["at"]))
            bucket["delivered_at"] = max(
                float(bucket["delivered_at"]), float(row["delivered_at"])
            )
            bucket["ids"].append(int(row["id"]))
        items = sorted(grouped.values(), key=lambda item: float(item["last_at"]))
        return items[:size]

    def mark_sleep_backlog_delivered(self, ids: Sequence[int], *, at: float) -> int:
        keys = {int(item) for item in ids if _is_id(item)}
        if not keys:
            return 0
        touched = 0
        with self._lock:
            for row in self._backlog:
                if int(row["id"]) in keys and float(row["delivered_at"]) <= 0.0:
                    row["delivered_at"] = float(at)
                    touched += 1
        return touched

    def mark_sleep_backlog_caught_up(self, ids: Sequence[int], *, at: float) -> int:
        keys = {int(item) for item in ids if _is_id(item)}
        if not keys:
            return 0
        touched = 0
        with self._lock:
            for row in self._backlog:
                if int(row["id"]) in keys:
                    row["caught_up_at"] = float(at)
                    if float(row["delivered_at"]) <= 0.0:
                        row["delivered_at"] = float(at)
                    touched += 1
        return touched

    def prune_sleep_backlog(self, *, before: float, keep_per_person: int = 5) -> int:
        cutoff = float(before or 0.0)
        keep = max(0, int(keep_per_person))
        removed = 0
        with self._lock:
            if cutoff > 0.0:
                stale = [item for item in self._backlog if float(item["at"]) < cutoff]
                for item in stale:
                    self._backlog.remove(item)
                removed += len(stale)
            if keep > 0:
                by_person: dict[str, list[dict[str, Any]]] = {}
                for item in self._backlog:
                    by_person.setdefault(str(item["person_id"]), []).append(item)
                for rows in by_person.values():
                    if len(rows) <= keep:
                        continue
                    rows.sort(key=lambda item: float(item["at"]))
                    for item in rows[: len(rows) - keep]:
                        self._backlog.remove(item)
                        removed += 1
        return removed

    def drop_sleep_backlog(self, person_id: str) -> int:
        pid = str(person_id or "").strip()
        if not pid:
            return 0
        with self._lock:
            victims = [item for item in self._backlog if item["person_id"] == pid]
            for item in victims:
                self._backlog.remove(item)
        return len(victims)

    # ---- 生命周期 ----

    def close(self) -> None:
        with self._lock:
            self._rows.clear()
            self._people.clear()

    @property
    def backend(self) -> str:
        return "memory"


class LifeStore:
    """``life_store.db``：关系档案 + 习惯日态。

    所有方法**都不抛**：SQLite 出问题只回调 ``on_error`` 并返回空/False，由调用方
    决定怎么降级（本插件是「习惯层少一次记忆」，不是「整 tick 失败」）。
    """

    def __init__(
        self,
        path: str,
        *,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self.path = str(path or "")
        self._on_error = on_error
        self._broken = False
        #: 见模块文档第 1 条：新连接 + 全局锁，两者缺一不可
        self._lock = threading.Lock()
        if not self.path:
            self._fail("存储路径为空")
            return
        try:
            self._init_schema()
        except (sqlite3.Error, OSError) as exc:
            self._fail(f"初始化 SQLite 失败（{self.path}）: {exc}")

    # ---- 内部 ----

    def _fail(self, message: str, *, fatal: bool = True) -> None:
        """记录一次失败。

        ``fatal=False`` 用于**瞬时**故障（等锁超时、库被别的连接锁住、连接暂时开不出来）：
        这类失败只该丢掉当前这一次操作（fail-open，调用方本来就容忍 ``None``），
        不该把整个存储标成 ``broken``。

        为什么这条必须区分（v1.21.0）：``_broken`` 一旦置位，``_run`` 就直接返回
        ``None``——**此后所有落盘都静默失效**（生活状态、习惯日态、关系档案全部不再
        持久化，直到进程重启）。Windows 上 8 个线程同时开 WAL 库写会撞
        ``database is locked``，把它当致命错误等于「一次并发冲突 = 整个进程不再记忆」。
        """

        if fatal:
            self._broken = True
        if self._on_error is not None:
            try:
                self._on_error(message)
            except Exception:  # noqa: BLE001 —— 回调不许反过来炸掉调用方
                pass

    @staticmethod
    def _is_transient(error: BaseException) -> bool:
        """SQLite 的锁竞争是**可重试**的瞬时错误，不是库坏了。"""

        text = str(error).lower()
        return "locked" in text or "busy" in text

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=_DEFAULT_CONNECT_TIMEOUT)

    def _enable_wal(self, conn: sqlite3.Connection) -> None:
        """``journal_mode`` 是**写进库文件头**的持久属性：开一次就够，不必每连接都设。

        顺带这也避开了「每连接都切换一次 journal_mode」在并发冷启动时可能踩到的
        文件竞争（实测症状见模块文档第 1 条）。
        """

        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            # 只读介质/不支持 WAL 的文件系统上退回默认日志模式即可，不是致命错误
            pass

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                self._enable_wal(conn)
                for statement in _SCHEMA_STATEMENTS:
                    conn.execute(statement)
                current = conn.execute("PRAGMA user_version").fetchone()
                version = int(current[0]) if current else 0
                if version < SCHEMA_VERSION:
                    # 迁移钩子：按 version **逐级**升级。注意 ``CREATE TABLE IF NOT
                    # EXISTS`` 对已存在的表是 no-op ⇒ 老库靠这里的迁移改结构，
                    # 光靠上面那轮建表语句升级不了。
                    if version < 2:
                        _migrate_relationships_to_v2(conn)
                    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                conn.commit()
            finally:
                conn.close()

    def _run(self, operation: str, work: Callable[[sqlite3.Connection], Any]) -> Any:
        if self._broken:
            return None
        # v1.13.1（F-002，安全审计）：锁必须带超时。``Lock.acquire()`` 无限等的话，
        # 慢盘上的写事务会让等待方（哪怕已经挪进 to_thread）挂死到磁盘恢复——
        # 宁可丢这一次记账（fail-open，调用方本来就要处理 None），不拖垮调用方。
        if not self._lock.acquire(timeout=2.0):
            # 瞬时：等不到锁只丢这一次，不把库标成 broken（见 ``_fail`` 的说明）
            self._fail(f"{operation}：等存储锁超时（2s），本次操作丢弃", fatal=False)
            return None
        try:
            try:
                conn = self._connect()
            except (sqlite3.Error, OSError) as exc:
                self._fail(
                    f"{operation}：连不上库（{exc}）", fatal=not self._is_transient(exc)
                )
                return None
            try:
                result = work(conn)
                conn.commit()
                return result
            except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
                self._fail(
                    f"{operation} 失败：{exc}", fatal=not self._is_transient(exc)
                )
                return None
            finally:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
        finally:
            self._lock.release()

    # ---- 习惯日态 ----

    def load_day(self, day_key: str) -> dict[str, tuple[int, int]]:
        key = str(day_key or "")

        def work(conn: sqlite3.Connection) -> dict[str, tuple[int, int]]:
            rows = conn.execute(
                "SELECT line_id, jitter_minutes, fired FROM routine_daily WHERE day_key = ?",
                (key,),
            ).fetchall()
            return {str(row[0]): (int(row[1]), int(row[2])) for row in rows}

        return self._run("读习惯日态", work) or {}

    def save_day(self, day_key: str, rows: Mapping[str, Sequence[int]]) -> bool:
        key = str(day_key or "")
        payload: list[tuple[str, int, int]] = []
        for line_id, value in rows.items():
            line_key = str(line_id or "").strip()
            if not line_key:
                continue
            jitter = int(value[0]) if len(value) > 0 else 0
            status = int(value[1]) if len(value) > 1 else ROUTINE_PENDING
            payload.append((line_key, jitter, status))
        if not payload:
            return False

        def work(conn: sqlite3.Connection) -> bool:
            conn.executemany(
                "INSERT INTO routine_daily (day_key, line_id, jitter_minutes, fired) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(day_key, line_id) DO UPDATE SET "
                "jitter_minutes = excluded.jitter_minutes, fired = excluded.fired",
                [(key, line_id, jitter, status) for line_id, jitter, status in payload],
            )
            return True

        return bool(self._run("写习惯日态", work))

    def prune_before(self, day_key: str) -> int:
        """删掉严格小于 ``day_key`` 的行；返回删除条数。

        ``day_key`` 是 ``YYYY-MM-DD``，字符串比较即时间比较（这也是它被选作键格式的
        原因——跨年也不会比错）。
        """

        threshold = str(day_key or "")

        def work(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                "DELETE FROM routine_daily WHERE day_key < ?", (threshold,)
            )
            return int(cursor.rowcount or 0)

        return int(self._run("清理过期习惯日态", work) or 0)

    # ---- 关系档案 ----

    def get_relationship(self, user_id: str) -> dict[str, Any] | None:
        key = str(user_id or "").strip()
        if not key:
            return None

        def work(conn: sqlite3.Connection) -> dict[str, Any] | None:
            row = conn.execute(
                f"SELECT {', '.join(_RELATIONSHIP_FIELDS)} FROM relationships WHERE person_id = ?",
                (key,),
            ).fetchone()
            if row is None:
                return None
            return dict(zip(_RELATIONSHIP_FIELDS, row))

        result = self._run("读关系档案", work)
        return result if isinstance(result, dict) else None

    def put_relationship(self, record: Mapping[str, Any]) -> bool:
        # v1.13.1（F-008/F-009，安全审计）：入库前统一截断——各类文本字段都不该
        # 无上界（实测 10 万字符 user_id 可入库）。上限与 new_relationship 对齐。
        pid = str(record.get("person_id") or "").strip()[:64]
        if not pid:
            # 来源只给了 (platform, user_id) 时**现算一次**（与 relationship_rows 同口径）。
            # 「主键缺了就静默不存」是最坏的失败形态——升级后关系看起来全没了；
            # 而 person_id 本来就是这两个字段的确定性函数，没有理由不推导。
            pid = person_id_for(record.get("platform"), record.get("user_id"))
        if not pid:
            return False
        values = tuple(
            pid if field == "person_id"
            else str(record.get(field) or "")[:64] if field in ("user_id", "relation_hint")
            else str(record.get(field) or "")[:32] if field == "platform"
            else record.get(field, default)
            for field, default in _RELATIONSHIP_DEFAULTS
        )
        columns = ", ".join(_RELATIONSHIP_FIELDS)
        placeholders = ", ".join("?" * len(_RELATIONSHIP_FIELDS))
        updates = ", ".join(
            f"{field} = excluded.{field}"
            for field in _RELATIONSHIP_FIELDS
            if field != "person_id"
        )

        def work(conn: sqlite3.Connection) -> bool:
            conn.execute(
                f"INSERT INTO relationships ({columns}) VALUES ({placeholders}) "
                f"ON CONFLICT(person_id) DO UPDATE SET {updates}",
                values,
            )
            return True

        return bool(self._run("写关系档案", work))

    def top_relationships(self, limit: int = 3) -> list[dict[str, Any]]:
        count = max(0, int(limit))
        if count == 0:
            return []

        def work(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                f"SELECT {', '.join(_RELATIONSHIP_FIELDS)} FROM relationships "
                "ORDER BY familiarity DESC, last_interaction_at DESC LIMIT ?",
                (count,),
            ).fetchall()
            return [dict(zip(_RELATIONSHIP_FIELDS, row)) for row in rows]

        result = self._run("取最熟的人", work)
        return result if isinstance(result, list) else []

    def prune_relationships(self, keep: int = 200) -> int:
        """超过 ``keep`` 条就按「最近互动最久远」淘汰（一条 SQL，不用手写 LRU）。"""

        limit = max(0, int(keep))

        def work(conn: sqlite3.Connection) -> int:
            total = conn.execute("SELECT COUNT(*) FROM relationships").fetchone()
            count = int(total[0]) if total else 0
            if count <= limit:
                return 0
            cursor = conn.execute(
                "DELETE FROM relationships WHERE person_id IN ("
                "  SELECT person_id FROM relationships "
                "  ORDER BY last_interaction_at ASC LIMIT ?"
                ")",
                (count - limit,),
            )
            return int(cursor.rowcount or 0)

        return int(self._run("淘汰关系档案", work) or 0)

    def delete_relationship(self, person_id: str) -> bool:
        key = str(person_id or "").strip()
        if not key:
            return False

        def work(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                "DELETE FROM relationships WHERE person_id = ?", (key,)
            )
            return int(cursor.rowcount or 0) > 0

        return bool(self._run("删除关系档案", work))

    # ---- 睡眠期积压（sleep backlog，v1.23.0）----
    #
    # 语义：``delivered_at`` = 被动回填时**注入过提示词**的时刻（只注入一次）；
    # ``caught_up_at`` = 主动补答**确认说出去了**的时刻。两者分开存，是因为
    # 「她看到了」与「她回应了」是两件事：Planner 受理但沉默的那一次不能让条目消失，
    # 否则「补答失败」会被静默记成「已处理」。

    def add_sleep_backlog(
        self,
        person_id: str,
        session_id: str,
        *,
        at: float,
        text: str,
        user_id: str = "",
        platform: str = DEFAULT_PLATFORM,
        is_group: bool = False,
        max_chars: int = 400,
    ) -> int:
        """追加一条积压；返回新行 id（0 = 没写进去）。

        ``text`` 在这里**二次截断**：调用方已经按配置截过，但配置可热重载，
        而库是持久的——不在这里兜一层，一次「把 max_chars 改成 0」就会让
        单条消息永久占住库容。
        """

        pid = str(person_id or "").strip()
        sid = str(session_id or "").strip()
        if not pid or not sid:
            return 0
        body = str(text or "").strip()
        limit = int(max_chars)
        if limit <= 0:
            limit = 400
        if len(body) > limit:
            body = body[:limit]

        def work(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                "INSERT INTO sleep_backlog "
                "(person_id, session_id, user_id, platform, is_group, at, text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    pid,
                    sid,
                    str(user_id or "")[:64],
                    str(platform or DEFAULT_PLATFORM)[:32],
                    1 if is_group else 0,
                    float(at),
                    body,
                ),
            )
            return int(cursor.lastrowid or 0)

        return int(self._run("写入睡眠积压", work) or 0)

    def peek_sleep_backlog(self, person_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """取某人**未主动补过**的记录（时间正序）。已投递过被动回填的也一并返回——
        调用方据此判断「这次还能不能引原文」，是否重复注入由插件侧内存标记控制。
        """

        pid = str(person_id or "").strip()
        if not pid:
            return []
        size = max(1, min(200, int(limit)))

        def work(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                "SELECT id, person_id, session_id, user_id, platform, is_group, at, text, "
                "delivered_at, caught_up_at FROM sleep_backlog "
                "WHERE person_id = ? AND caught_up_at <= 0 "
                "ORDER BY at DESC LIMIT ?",
                (pid, size),
            ).fetchall()
            # ⚠ 本模块的连接**没有** ``row_factory``（见 ``_connect``）：所有取数都要
            # 显式 zip 列名，``dict(row)`` 在这里会抛 TypeError 并被 ``_run`` 静默吞掉
            # ——表现是「写进去了但永远读不出来」。
            return [dict(zip(_SLEEP_BACKLOG_FIELDS, row)) for row in rows][::-1]

        result = self._run("读睡眠积压", work)
        return list(result) if isinstance(result, list) else []

    def pending_sleep_sessions(self, *, since: float = 0.0, limit: int = 50) -> list[dict[str, Any]]:
        """按会话聚合「还没主动补过」的积压（供醒来触发与启动扫描）。

        返回每项：``session_id / person_id / user_id / platform / is_group /
        count / last_at / ids / delivered_at``。``ids`` 是该会话全部待补行 id
        （主动补成功后按它核销）。
        """

        cutoff = float(since or 0.0)
        size = max(1, min(200, int(limit)))

        def work(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                "SELECT session_id, person_id, user_id, platform, is_group, "
                "       COUNT(*) AS count, MAX(at) AS last_at, "
                "       GROUP_CONCAT(id) AS ids, MAX(delivered_at) AS delivered_at "
                "FROM sleep_backlog WHERE caught_up_at <= 0 AND at >= ? "
                "GROUP BY session_id ORDER BY last_at ASC LIMIT ?",
                (cutoff, size),
            ).fetchall()
            items: list[dict[str, Any]] = []
            for row in rows:
                item = dict(zip(_SLEEP_SESSION_FIELDS, row))
                raw_ids = str(item.pop("ids", "") or "")
                item["ids"] = [int(part) for part in raw_ids.split(",") if part.strip().isdigit()]
                items.append(item)
            return items

        result = self._run("聚合同步积压", work)
        return list(result) if isinstance(result, list) else []

    def mark_sleep_backlog_delivered(self, ids: Sequence[int], *, at: float) -> int:
        """标记「这一批已经注入过提示词」（被动回填）。"""

        keys = [int(item) for item in ids if _is_id(item)]
        if not keys:
            return 0
        placeholders = ",".join("?" for _ in keys)

        def work(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                f"UPDATE sleep_backlog SET delivered_at = ? "
                f"WHERE id IN ({placeholders}) AND delivered_at <= 0",
                [float(at), *keys],
            )
            return int(cursor.rowcount or 0)

        return int(self._run("标记回填已投递", work) or 0)

    def mark_sleep_backlog_caught_up(self, ids: Sequence[int], *, at: float) -> int:
        """标记「这一批已经补答完毕」（主动补**确认说出去了**之后才调）。"""

        keys = [int(item) for item in ids if _is_id(item)]
        if not keys:
            return 0
        placeholders = ",".join("?" for _ in keys)

        def work(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                f"UPDATE sleep_backlog SET caught_up_at = ?, delivered_at = "
                f"CASE WHEN delivered_at <= 0 THEN ? ELSE delivered_at END "
                f"WHERE id IN ({placeholders})",
                [float(at), float(at), *keys],
            )
            return int(cursor.rowcount or 0)

        return int(self._run("标记补答完成", work) or 0)

    def prune_sleep_backlog(self, *, before: float, keep_per_person: int = 5) -> int:
        """清理：①早于 ``before`` 的整条删除；②每人只留最近 ``keep_per_person`` 条。"""

        cutoff = float(before or 0.0)
        keep = max(0, int(keep_per_person))

        def work(conn: sqlite3.Connection) -> int:
            removed = 0
            if cutoff > 0.0:
                cursor = conn.execute("DELETE FROM sleep_backlog WHERE at < ?", (cutoff,))
                removed += int(cursor.rowcount or 0)
            if keep > 0:
                cursor = conn.execute(
                    "DELETE FROM sleep_backlog WHERE id IN ("
                    "  SELECT id FROM sleep_backlog b WHERE ("
                    "    SELECT COUNT(*) FROM sleep_backlog c "
                    "    WHERE c.person_id = b.person_id AND c.at >= b.at"
                    "  ) > ?"
                    ")",
                    (keep,),
                )
                removed += int(cursor.rowcount or 0)
            return removed

        return int(self._run("清理睡眠积压", work) or 0)

    def drop_sleep_backlog(self, person_id: str) -> int:
        """丢掉某人的全部积压。"""

        pid = str(person_id or "").strip()
        if not pid:
            return 0

        def work(conn: sqlite3.Connection) -> int:
            cursor = conn.execute("DELETE FROM sleep_backlog WHERE person_id = ?", (pid,))
            return int(cursor.rowcount or 0)

        return int(self._run("清空某人积压", work) or 0)

    # ---- 生命周期 ----

    def close(self) -> None:
        """关库。SQLite 连接是每操作开新的，这里只做「后续调用立刻失败」的标记。"""

        self._broken = True

    @property
    def broken(self) -> bool:
        return self._broken

    @property
    def backend(self) -> str:
        return "memory" if self._broken else "sqlite"


def open_store(
    path: str,
    *,
    on_error: Callable[[str], None] | None = None,
) -> LifeStore | MemoryStore:
    """开库；失败时返回 :class:`MemoryStore` 并回调 ``on_error`` 一次。

    调用方**不需要**判断返回的是哪一种——两者接口一致。想知道实际用的是哪种，
    读 ``store.backend``（日志与自检用）。
    """

    try:
        store = LifeStore(path, on_error=on_error)
    except Exception as exc:  # noqa: BLE001 —— 建库阶段的任何异常都只降级，不许冒泡
        if on_error is not None:
            try:
                on_error(f"开库失败（{path}），习惯层改用内存兜底: {exc}")
            except Exception:  # noqa: BLE001
                pass
        return MemoryStore()
    if store.broken:
        if on_error is not None:
            try:
                on_error(f"开库失败（{path}），习惯层改用内存兜底")
            except Exception:  # noqa: BLE001
                pass
        return MemoryStore()
    return store


def relationship_rows(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """把任意来源的关系记录洗成表结构（缺字段补默认、多余字段丢弃）。

    落库前的最后一道闸：SQLite 的列是固定的，脏键直接进 ``put_relationship``
    会被忽略（静默），不如在这里显式归一化。

    v1.20.0（Y4）：主键是 ``person_id``。来源没给 ``person_id`` 时按
    ``(platform, user_id)`` 现算一次——老数据与外部写入都能被接住，
    而不是被静默丢掉（「升级后关系全没了」是最不能接受的那种静默）。
    """

    cleaned: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, Mapping):
            continue
        pid = str(record.get("person_id") or "").strip()[:64]
        if not pid:
            pid = person_id_for(record.get("platform"), record.get("user_id"))
        if not pid:
            continue
        platform = (
            str(record.get("platform") or DEFAULT_PLATFORM).strip()[:32] or DEFAULT_PLATFORM
        )
        cleaned.append(
            {
                "person_id": pid,
                "platform": platform,
                "user_id": str(record.get("user_id") or "").strip()[:64],
                "first_seen_at": float(record.get("first_seen_at") or 0.0),
                "last_interaction_at": float(record.get("last_interaction_at") or 0.0),
                "interaction_count": int(record.get("interaction_count") or 0),
                "familiarity": float(record.get("familiarity") or 0.0),
                "relation_hint": str(record.get("relation_hint") or "")[:64],
                "shared_events": int(record.get("shared_events") or 0),
            }
        )
    return cleaned
