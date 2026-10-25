# -*- coding: utf-8 -*-
"""配置层：24 个配置模型 + 默认值常量 + WebUI Schema 修正。

从 ``plugin.py`` 抽出来的（v1.21.0）。抽出的理由是**可维护性**，不是功能：
``plugin.py`` 里 6,130 行的插件类才是胶水层，而配置声明（2,100+ 行）只是
pydantic 模型 + 展示元数据，与宿主能力（``self.ctx.*``）毫无关系。

两条硬约定（与 ``plugin.py`` 相同，违反即加载失败）：

  1. **禁止** ``from __future__ import annotations``——pydantic 解析配置模型会失败。
  2. 本文件**不允许**出现 ``self.ctx``：``check_plugin.py`` 只扫 ``plugin.py``
     反推 capability，放进这里的能力调用不会被发现（会被当成多余声明剔除）。

``config_version`` 字段仍然声明在 ``LifeFrequencyConfig`` 里（宿主 1.2.3 的硬性要求），
所以 ``check_plugin.py`` 的「源码/config_version」检查会同时扫本文件。
"""

# --- 标准库 ---
import math
from dataclasses import replace as _replace
from typing import Any

# --- 宿主依赖：maibot_sdk（禁止 import src.*）---
from maibot_sdk import Field, PluginConfigBase

from pydantic import create_model, field_validator
"""pydantic 是 maibot-plugin-sdk 的硬依赖，这里只借它两个能力：

把 config.toml 里「写成裸字符串的列表」在字段校验**之前**转成 list；
以及为 WebUI Schema 动态拼一个「只含某个嵌套配置类」的临时模型。"""

from maibot_sdk.config import generate_plugin_config_schema
"""SDK 的配置 Schema 生成器：我们复用它生成嵌套节的字段元数据，只改 section 名与标题。"""

# ``DEFAULT_CARE_PATTERNS`` 的默认值来自 life_sim（纯模块，不反向依赖本文件）
try:
    from .life_sim import DEFAULT_CARE_PATTERNS
except ImportError:  # 平铺兜底（脚本直跑 / 旧测试）
    from life_sim import DEFAULT_CARE_PATTERNS


SUPPORTED_CONFIG_VERSION = "1.8.0"
"""``[plugin].config_version`` 的默认值（宿主硬性要求，缺失即加载失败）。

⚠ 这是**配置 schema 的版本**，不是插件版本。v1.14.0 递增到 1.1.0（病程字段）、
v1.16.0 到 1.2.0（`fatigue_ramp_curve`）、v1.16.1 到 1.3.0（回归/惯性/余波/节律）、
v1.16.2 到 1.4.0（体力耦合 + 内心维度通道）、v1.16.3 到 1.5.0（冲击边际效用 +
关系系数）、v1.17.0 到 1.6.0（`[schedule] honor_calendar` / `daily_jitter_minutes` /
`overtime_probability`）、v1.18.0 到 1.7.0（`[proactive]` 的
`failure_cooldown_minutes` / `confirm_send` / `confirm_delay_minutes` /
`interval_jitter_ratio` / `max_unanswered_times`）的原因有实测依据（见 runtime-gotchas §7.0.1）：
宿主只在**文件不存在**时
才初始化 ``config.toml``；版本号相同时配置**完全不写回**。⇒ 新增字段若不 bump，
已部署实例的 ``config.toml`` 里永远不会出现这些键（行为按 pydantic 默认值跑，但用户
在 WebUI / 文件里看不到、也无从调整）。bump 后宿主的重建语义是「新默认值为骨架 +
旧值覆盖」，只补新增字段、不动已存在字段的值——**所以新增字段必须带默认值，且不改
老字段的默认值**。别跟着 `_manifest.json` 的 version 走，两者含义不同。"""

DEFAULT_ACTIVITY_FACTOR_LINES = [
    "sleep=0.0",
    # v1.15.0（PR-R1）：小睡也是睡——同样「完全静默」。真正的静默由
    # ``compute_adjust`` 的硬闸保证（因子路径会被素材加成抬起来），这一行是
    # 因子表可读性与 replace 模式缺键告警的依据。
    "nap=0.0",
    "sick_rest=1.0",
    "before_sleep=1.15",
    # v1.3.0 加入的工作表活动：有固定作息的角色不再只能靠场景文字「假装」在上班
    "commute=0.85",
    "work=0.75",
    "meeting=0.5",
    "overtime=0.45",
    "lunch=1.1",
    "off_work=1.0",
    "night_study=0.5",
    "music=0.9",
    "game=0.75",
    "anime=0.75",
    "daze=0.6",
    "daily=1.0",
    # v1.9.1（physio）：吃饭时话不多、洗澡时几乎不说话
    "meal=0.8",
    "bath=0.5",
    # v1.11.1（interrupt）：正在回消息，话最不该少——这是她唯一理直气壮多说几句的时刻
    "chatting=1.1",
]
DEFAULT_HEALTH_FACTOR_LINES = ["healthy=1.0", "cold=0.3", "sleep_deprived=0.9"]
#: 病程阶段因子（v1.14.0）：初起 0.7（还能做事，话没那么少）、加重 0.15（几乎只剩养病）、
#: 好转 0.5。与 ``DEFAULT_HEALTH_FACTOR_LINES`` 的 ``cold`` 是**替代关系**：
#: 阶段表生效时 ``cold`` 不再参与（避免双重抑制），只有阶段表缺失时才回退它。
DEFAULT_COLD_STAGE_FACTOR_LINES = ["onset=0.7", "worsening=0.15", "recovering=0.5"]
DEFAULT_PHYSIO_MEAL_LINES = [
    # 三餐时间窗（半开区间）。weight < 1.0 = 偶尔不吃那一顿——天天分秒不差吃三顿也是机器
    "07:00-08:30|早餐|meal|weight=0.9",
    "12:00-13:30|午餐|meal|weight=1.0",
    "18:00-19:30|晚餐|meal|weight=1.0",
    "22:00-23:30|洗澡|bath|weight=0.8",
]
DEFAULT_MOOD_CURVE_LINES = ["0=0.55", "5=0.95", "10=1.35"]
DEFAULT_ENERGY_CURVE_LINES = ["0=0.50", "5=0.85", "10=1.25"]
#: 清醒疲劳曲线（v1.16.0 M1）：清醒小时 → **额外**每小时体力消耗。12 小时以内不额外
#: 掉体力（正常的一天不被惩罚），之后分段线性加重。标定依据见 README 与方案 §3 M1：
#: 清闲日（daze/daily ≈ -0.3/h）从 8.0 出发，16 小时后 ≈ 2.6，自然触到入睡阈值 3.0。
#: 空列表 = 关闭 = 与加这一层之前逐位一致。
DEFAULT_FATIGUE_RAMP_LINES = ["12=0", "16=-0.15", "20=-0.4", "24=-0.7"]
#: 日内节律曲线（v1.16.1 M4a）：**本地时刻分钟** → 基线偏移。05:00 最低落、13:00 略高、
#: 20:00 最高、23:00 归零。幅度刻意压在 ±0.3（对情绪因子影响 ≤ ±0.06 ≈ 4%——氛围层，
#: 不是数值层；方案决议 5 拍板「默认不放宽」，想要强体感就自己改这条曲线）。
#: 空列表 = 关闭 = 基线不随时刻浮动（与 v1.15.0 逐位一致）。
DEFAULT_DIURNAL_CURVE_LINES = ["300=-0.3", "780=0.15", "1200=0.3", "1380=0"]
#: 孤独 → 社交情绪系数曲线（v1.16.2 M3b）：孤独 ≤2 → ×0.8（被爱包围、对打扰钝感），
#: ≥7 → ×1.5（很想有人陪），其间线性。**不追求中性锚点**：孤独是状态不是身份，
#: 开箱时孤独基线 4.0 ⇒ 系数约 1.08，属于本版有意的行为变更（关掉即回旧行为）。
DEFAULT_LONELINESS_SOCIAL_LINES = ["2=0.8", "7=1.5"]
#: 情绪冲击的边际效用曲线（v1.16.3 M6）：`当前情绪=系数`。低谷雪中送炭、高涨快乐麻木；
#: 负向反过来（低谷麻木、高涨落差）。两端点外取端点值（interpolate 不平外推）。
DEFAULT_IMPACT_POSITIVE_LINES = ["2=1.3", "5=1.0", "8=0.7"]
DEFAULT_IMPACT_NEGATIVE_LINES = ["2=0.7", "5=1.0", "8=1.2"]
#: 上面两条曲线的**解析形态**（SimConfig 字段是点集，不是字符串行）。配置解析失败时
#: 回退它们，而不是回退成空曲线——空曲线会让「边际效用开着却恒等于 1.0」这种
#: 「配置写坏了、功能静默消失」的形态又出现一次。
DEFAULT_IMPACT_POSITIVE_CURVE = ((2.0, 1.3), (5.0, 1.0), (8.0, 0.7))
DEFAULT_IMPACT_NEGATIVE_CURVE = ((2.0, 0.7), (5.0, 1.0), (8.0, 1.2))
#: 社交情绪的**关系系数**曲线（v1.16.3 M7）：`熟悉度=系数`。中性锚点钉在陌生人身上
#: （决议 3）：没有档案 → 1.0 = 与升级前逐位一致，偏离只随关系加深单向发生。
DEFAULT_RELATION_EMOTION_LINES = ["20=1.0", "50=1.2", "80=1.5"]
#: 关系系数上限（决议 3：单向只放大、封顶 1.5）
RELATION_EMOTION_CAP = 1.5

DEFAULT_QUIET_HOURS_LINES = ["23:30-08:00"]
DEFAULT_PROACTIVE_QUIET_LINES = ["00:00-08:00"]
ALLOWED_FILTER_MODES = ("all", "whitelist", "blacklist")

def _dc_replace_rules(rules: Any, *, score_threshold: float) -> Any:
    """复制一个 ``ProactiveRules``，只改阈值（dataclass 的 ``replace``）。"""

    from dataclasses import replace as _replace

    return _replace(rules, score_threshold=score_threshold)



# ---------------------------------------------------------------- 归一化助手


def _as_str_list(value: Any) -> Any:
    """把 config.toml 里常见的「字符串形式的列表」友好地归一化成 list。

    真机踩坑（照 bd-repo 的结论）：用户很容易写成

        admin_ids = "123456789"          # 而不是 ["123456789"]
        target_chats = "group:123456"

    这里兼容中英文逗号、分号与换行当分隔符。
    """

    if value is None:
        return []
    if isinstance(value, str):
        normalized = value.replace("，", ",").replace("；", ";").replace("\n", ",")
        return [item.strip() for item in normalized.replace(";", ",").split(",") if item.strip()]
    return value


def _str_list_validator(*fields: str) -> Any:
    """给 ``list[str]`` 字段做一个「校验前归一化」的验证器。

    每个字段生成一个**独立**的归一化函数：pydantic 的 ``field_validator`` 会往目标
    函数上挂描述符，多个字段共用同一个函数对象容易互相干扰，所以这里不复用。
    """

    def _normalize(value: Any) -> Any:
        return _as_str_list(value)

    _normalize.__name__ = f"normalize_{'_'.join(fields) or 'str_list'}"
    return field_validator(*fields, mode="before")(_normalize)


def _as_sequence(value: Any) -> list[Any]:
    """能力返回值里取列表：成功是 list，失败是 dict —— 都要能接住。"""

    return value if isinstance(value, list) else []


def _as_number(value: Any, default: float | None = None) -> float | None:
    """能力返回值里取数字：``{"success": false}`` 这类失败形状退回默认值。"""

    if isinstance(value, bool) or isinstance(value, (dict, list)):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _as_text(value: Any, default: str = "") -> str:
    """能力返回值里取文本：失败形状（dict）退回默认值。"""

    if value is None or isinstance(value, (dict, list)):
        return default
    text = str(value).strip()
    return text or default


def _as_epoch(value: Any, default: float = 0.0) -> float:
    """从能力返回值里取**时间戳**：数字与数字字符串都要认。

    ⚠ 真机的 ``message.get_by_time_in_chat`` 把 ``timestamp`` 序列化成**字符串**
    （``plugin_runtime/host/message_utils.py``：``timestamp=str(session_message.timestamp.timestamp())``，
    且 ``MessageDict.timestamp`` 就声明为 ``str``）。而 ``_as_number`` 只认 int/float ⇒
    凡是用它读时间戳的地方在真机上都会拿到 0。这不是学术问题：v1.17.2 的
    「对方上次说话」基线补全（``_restore_last_user_from_history``）因此在真机上
    **从未真正生效**，只是本地测试全用 float 时间戳所以一直没暴露。

    这里兼容两种形态，并拒掉 bool / 容器 / 非有限数（NaN、Infinity）。
    """

    if isinstance(value, bool) or isinstance(value, (dict, list)):
        return default
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip()
        if not text:
            return default
        try:
            number = float(text)
        except ValueError:
            return default
    return number if math.isfinite(number) else default


# ===================================================================== 配置


class PluginSectionConfig(PluginConfigBase):
    """``[plugin]`` 配置节。``config_version`` 是宿主硬性要求，缺失即加载失败。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="是否启用插件；关闭后不改宿主频率（归还外部基数），也停止推进生活状态",
        json_schema_extra={"label": "启用插件", "order": 0},
    )
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description="配置 schema 版本（不是插件版本；由插件自动维护，勿手改）",
        json_schema_extra={"hidden": True, "disabled": True, "order": 99},
    )


class SimulationConfig(PluginConfigBase):
    """``[simulation]``：时间推进与冷启动。"""

    __ui_label__ = "生活时钟"
    __ui_icon__ = "clock"
    __ui_order__ = 1

    tick_seconds: int = Field(
        default=600,
        description="生活状态推进间隔（秒）。参考设定是每 10 分钟一次",
        json_schema_extra={"label": "推进间隔（秒）", "order": 0, "step": 60},
    )
    catch_up_max_hours: float = Field(
        default=72.0,
        description="离线补算的时间上限（小时）；限制的是分步次数，防止一次跑上千步",
        json_schema_extra={"label": "离线补算上限（小时）", "order": 1, "step": 1},
    )
    offline_gap_minutes: int = Field(
        default=30,
        description=(
            "超过它就判定为「停机间隙」：这段时间**不记账**"
            "（不计清醒/睡眠、不扣体力、不抽事件），只把生活日推进到当前。"
            "0 = 关闭判定（退回逐 tick 补算，会凭空造出「清醒 N 小时」）"
        ),
        json_schema_extra={"label": "停机间隙判定（分钟）", "order": 2, "step": 10},
    )
    seed: str = Field(
        default="",
        description="随机种子；留空则固定用插件默认种子（同种子可复现同一条时间线）",
        json_schema_extra={"label": "随机种子", "order": 3, "placeholder": "留空即可"},
    )
    dry_run: bool = Field(
        default=False,
        description="演算模式：只计算并记日志，不真的写宿主频率。首次安装建议先开",
        json_schema_extra={"label": "演算模式（不写宿主）", "order": 4},
    )
    tz_offset_minutes: int = Field(
        default=480,
        description="本地时区相对 UTC 的分钟偏移（默认 480 = UTC+8）",
        json_schema_extra={"label": "时区偏移（分钟）", "order": 5, "step": 30},
    )
    day_boundary_hour: int = Field(
        default=12,
        description="「生活日」的分界小时；凌晨的睡眠会算进前一天并在这里结算",
        json_schema_extra={"label": "生活日分界（小时）", "order": 6, "step": 1},
    )
    sleep_window: str = Field(
        default="03:00-11:00",
        description="睡眠时段；此时段内才允许自行入睡（体力很低或生病时不限）",
        json_schema_extra={"label": "睡眠时段", "order": 7, "placeholder": "03:00-11:00"},
    )
    sleep_energy_threshold: float = Field(
        default=3.0,
        description=(
            "体力低于它就可以随时入睡，不必等睡眠时段，"
            "也不受「每日清醒下限」限制（健康优先）"
        ),
        json_schema_extra={"label": "体力入睡阈值", "order": 8, "step": 0.1},
    )
    max_sleep_hours: float = Field(
        default=12.0,
        description="单日累计睡眠上限；超过就强制唤醒（防止模型让她睡一整天）",
        json_schema_extra={"label": "每日睡眠上限（小时）", "order": 9, "step": 0.5},
    )
    energy_full_wake: bool = Field(
        default=True,
        description=(
            "睡眠中体力恢复到上限（动态值：连熬 3 晚后是 8.5）就强制唤醒（感冒醒到养病）。"
            "睡觉的目的是恢复体力，满了继续躺只是空转；"
            "关掉则回到「模型提议醒 / 睡满每日上限」两条路"
        ),
        json_schema_extra={"label": "体力满唤醒", "order": 10},
    )
    energy_full_wake_min_hours: float = Field(
        default=6.5,
        description=(
            "体力满唤醒的**最短睡眠目标**（小时，v1.15.0）：睡不满它就算体力已满也继续睡。"
            "0 = 旧行为（体力一回满立刻醒）。默认 6.5 比熬夜阈值（默认 5 小时）高，"
            "这样才能断开「熬夜压低体力上限 → 睡得更短 → 债更还不清」的正反馈；"
            "睡觉不是充电，真人按钟点睡而不是按电量睡"
        ),
        json_schema_extra={"label": "最短睡眠目标（小时）", "order": 11, "step": 0.5},
    )
    rest_day_sleep_extension_minutes: int = Field(
        default=60,
        description=(
            "休息日（法定节假日 / 班表休息日）在最短睡眠目标上额外顺延的分钟数；"
            "0 = 不区分休息日。需要同时有「最短睡眠目标 > 0」才有效"
        ),
        json_schema_extra={"label": "休息日多睡（分钟）", "order": 12, "step": 15},
    )
    routine_can_wake: bool = Field(
        default=False,
        description=(
            "习惯表命中时能不能把她从睡眠里叫起来（v1.15.0）。默认**关** = 睡眠优先："
            "习惯是她清醒时的骨架，不是闹钟；打开 = 恢复 v1.14.x 的旧行为"
            "（睡满最短睡眠时长后，07:00「起床洗漱」这类习惯会把她叫醒）"
        ),
        json_schema_extra={"label": "习惯表可以叫醒她", "order": 13},
    )
    physio_can_wake: bool = Field(
        default=False,
        description=(
            "生理窗（三餐/洗澡）命中时能不能把她叫起来（v1.15.0）。"
            "默认**关** = 睡着的她不会被「该吃早饭了」叫醒（睡过头就当错过这一顿，"
            "醒来后窗口内仍能补上）；打开 = 恢复 v1.14.x 的旧行为"
        ),
        json_schema_extra={"label": "三餐窗可以叫醒她", "order": 14},
    )
    wake_daze_minutes: int = Field(
        default=15,
        description=(
            "长睡眠（≥3 小时）醒来先「赖床」的分钟数（v1.15.0）：这段时间里她不会被硬约束"
            "立刻送回床，也不会被最短停留期钉住。同一个窗口也被「夜间易醒」复用；"
            "0 = 关闭（醒来直接进日常）"
        ),
        json_schema_extra={"label": "醒后赖床（分钟）", "order": 15, "step": 5},
    )
    nap_enabled: bool = Field(
        default=True,
        description=(
            "启用「小睡」（v1.15.0）：白天（睡眠时段之外）精力低时眯一会儿，"
            "最多 `小睡上限` 分钟、体力恢复比睡觉弱、不做梦；睡着时同样完全静默。"
            "关掉后模型看不到这个选项，行为与升级前一致"
        ),
        json_schema_extra={"label": "启用小睡", "order": 16},
    )
    nap_max_minutes: int = Field(
        default=90,
        description="单次小睡的时长上限（分钟）；到了就叫醒她",
        json_schema_extra={"label": "小睡上限（分钟）", "order": 17, "step": 10},
    )
    nap_min_minutes: int = Field(
        default=20,
        description="单次小睡的最短时长（分钟）；刚眯下不会被马上叫起来",
        json_schema_extra={"label": "小睡最短（分钟）", "order": 18, "step": 5},
    )
    nap_energy_threshold: float = Field(
        default=4.0,
        description="体力低于它（或生病）才会小睡；精神好时提议小睡会被拒绝",
        json_schema_extra={"label": "小睡体力阈值", "order": 19, "step": 0.5},
    )
    wake_on_at: bool = Field(
        default=True,
        description=(
            "睡眠时被 @ 就立刻醒来（`唤醒保持` 分钟后自动回睡）。宿主的判定顺序是"
            "「先判频率是否静默、再判 @ 强制触发」，所以倍率为 0 时这条 @ 会被静默轮吃掉、"
            "**根本进不了 Planner**；打开后插件在消息钩子里就把该会话的倍率临时抬到清醒值，"
            "让这条 @ 真的被看见。只在 `activity = sleep` 时生效，`[frequency] quiet_hours`"
            "（你自己设的静默时段）不受影响；关掉即回到「睡就是睡」"
        ),
        json_schema_extra={"label": "睡眠时被 @ 唤醒", "order": 20},
    )
    wake_minutes: int = Field(
        default=10,
        description=(
            "被 @ 唤醒后保持清醒的分钟数；窗口内再来消息只顺延窗口、不重复写宿主。"
            "窗口结束且她仍在睡就自动回到静默（不需要再写一次 0）"
        ),
        json_schema_extra={"label": "唤醒保持（分钟）", "order": 21, "step": 5},
    )
    wake_on_private: bool = Field(
        default=False,
        description=(
            "睡眠中被**私聊**也叫醒她（v1.15.0）。默认关 = 「睡就是睡」的旧行为"
            "（私聊没有 @ 这个概念，所以原来私聊完全叫不醒）；"
            "打开后唤醒判据与打断机制对齐（私聊任意消息 / 群聊被 @）。"
            "`[frequency] quiet_hours` 与暂停仍然优先"
        ),
        json_schema_extra={"label": "私聊也叫醒她", "order": 23},
    )
    wake_private_scope: str = Field(default="", description="谁能靠私聊把她叫醒：`off` 谁都不行 / `admin` 只有管理员 / `anyone` 任何人。留空 = 跟随下方「私聊也叫醒她」（为真即 anyone，否则 admin）；`admin` 档在管理员 QQ 留空时等于 off（fail-closed）", json_schema_extra={"label": "私聊叫醒范围", "order": 22, "placeholder": "off / admin / anyone"})
    wake_owner_exempt_quiet: bool = Field(default=True, description="管理员私聊可以越过 `[frequency] quiet_hours`。这是**唯一**能越过你自己设的静默时段的路径；群聊 @ 与其他人一律照旧被拦", json_schema_extra={"label": "主人可越过静默时段", "order": 26})
    wake_extend_on_message: bool = Field(
        default=True,
        description=(
            "唤醒窗口内对方继续说话就顺延窗口（v1.15.0）：她回了一句、对方接着聊，"
            "不因为「没再 @ 她」而 10 分钟后突然断线。只顺延、不重复写宿主"
        ),
        json_schema_extra={"label": "对话顺延唤醒窗口", "order": 24},
    )
    wake_max_extensions_minutes: int = Field(
        default=30,
        description=(
            "一次被叫醒后最多保持清醒的分钟数（从第一次唤醒算起，v1.15.0）。"
            "防止活跃群里每 5 分钟一句话把窗口无限顺延、她整夜不睡；"
            "0 = 不设上限"
        ),
        json_schema_extra={"label": "唤醒总时长上限（分钟）", "order": 25, "step": 5},
    )
    wake_grumpy_note: bool = Field(
        default=True,
        description=(
            "被吵醒的质感（v1.15.0）：睡下没多久（<2 小时）就被叫醒时，"
            "给回复提示词补一句「刚睡下没多久，还有点迷糊」"
        ),
        json_schema_extra={"label": "被吵醒的语气", "order": 26},
    )


class ActivityLLMConfig(PluginConfigBase):
    """``[activity.llm]``：活动决策的模型调用参数。"""

    __ui_label__ = "活动模型"
    __ui_icon__ = "cpu"
    __ui_order__ = 10

    task_name: str = Field(
        default="",
        description="模型任务名；留空走宿主的默认插件任务（utils）",
        json_schema_extra={"label": "模型任务名", "order": 0, "placeholder": "留空 = utils"},
    )
    temperature: float = Field(
        default=0.8,
        description="采样温度；越高越发散、越低越稳定（0 ≈ 确定）。参考设定 0.8",
        json_schema_extra={"label": "温度", "order": 1, "step": 0.1},
    )
    max_tokens: int = Field(
        default=800,
        description=(
            "活动决策的输出上限（token）。**默认 800，不要退回 200**：v1.23.4 真机教训"
            "——200 在会先写一段推理/散文再给 JSON 的模型上会被截断，宿主只打一行"
            "「达到最大输出 token 限制」的 warning，插件侧看到的是「输出无法解析」，"
            "于是连续失败 18 次、生活状态冻结数小时（截断是确定性的，重试不会好）"
        ),
        json_schema_extra={"label": "最大 token", "order": 2, "step": 50},
    )
    timeout_ms: int = Field(
        default=120000,
        description=(
            "整条 cap.call 的等待预算（毫秒）。它由 SDK 转成 RPC 超时，覆盖宿主侧"
            "「模型回退链 + 重试」的**总耗时**，不是单个模型的超时；超了即按"
            "「失败保持上个活动」处理。SDK 内建默认只有 30000，而真机实测一次"
            "成功决策要 19 秒（还没算事件循环卡顿），所以别调回 20 秒级"
        ),
        json_schema_extra={"label": "等待预算（毫秒）", "order": 3, "step": 1000},
    )
    min_interval_seconds: int = Field(
        default=600,
        description="两次活动决策之间的最小间隔（秒）；默认与推进间隔一致",
        json_schema_extra={"label": "决策最小间隔（秒）", "order": 4, "step": 60},
    )
    sleep_interval_seconds: int = Field(
        default=1800,
        description=(
            "**睡眠中**的决策间隔（秒，v1.15.0）。睡满最短睡眠时长后到醒来之间，"
            "模型能做的有效决定只有「要不要提前醒」，而按 10 分钟一轮问一夜等于白烧"
            "token（每晚 18–30 次）。默认 30 分钟一轮；0 = 睡眠中完全不问模型"
            "（醒来交给体力满 / 睡眠上限 / 最短睡眠目标这些确定性条件）"
        ),
        json_schema_extra={"label": "睡眠中决策间隔（秒）", "order": 20, "step": 60},
    )
    fail_streak_limit: int = Field(
        default=3,
        description="连续失败多少次后进入冷却（期间保持上个活动，不再调模型）",
        json_schema_extra={"label": "连续失败上限", "order": 5, "step": 1},
    )
    cooldown_minutes: int = Field(
        default=30,
        description="冷却时长（分钟）",
        json_schema_extra={"label": "失败冷却（分钟）", "order": 6, "step": 5},
    )
    recent_events_in_prompt: int = Field(
        default=8,
        description=(
            "近期经历的**总条数上限**（近/中/远三层合计）。按「近约一半、中约三成、远拿剩下」"
            "分配到三层；<3 时全部给最近层。0 = 不带任何经历"
        ),
        json_schema_extra={"label": "提示词近期经历条数", "order": 7, "step": 1},
    )
    recent_near_hours: float = Field(
        default=12.0,
        description="「近」层的时间窗（小时）：她当前状态的直接成因",
        json_schema_extra={"label": "近层窗口（小时）", "order": 8, "step": 1},
    )
    recent_mid_hours: float = Field(
        default=72.0,
        description="「中」层的时间窗（小时，默认 3 天）：最近几天的基调",
        json_schema_extra={"label": "中层窗口（小时）", "order": 9, "step": 6},
    )
    recent_far_days: float = Field(
        default=14.0,
        description="「远」层的时间窗（天）：再早的事只剩「记得」的份量，不进提示词",
        json_schema_extra={"label": "远层窗口（天）", "order": 10, "step": 1},
    )
    persona_max_chars: int = Field(
        default=600,
        description=(
            "喂给活动决策的**人设字数上限**（字符）。超出上限的部分她看不到，"
            "并且**不会**有任何提示——人设长就把这里调大（调完立刻生效，不必重启）。"
            "活动决策默认每 10 分钟一轮，人设越长每轮输入 token 越多；"
            "0 = 干脆不带人设（省 token）"
        ),
        json_schema_extra={"label": "人设上限（字符）", "order": 11, "step": 100},
    )
    skip_when_forced: bool = Field(
        default=True,
        description=(
            "省调用的开关：当「无论模型答什么都只会保持当前活动」时"
            "（活动未满最短停留时间且此刻不会睡，或睡眠未满最短时长）就不再问模型。"
            "裁定结果与问了完全一致，只少刷新一句场景文字；"
            "状态卡会标注「本轮未问模型」，不会伪装成模型故障。关掉就退回每轮都问"
        ),
        json_schema_extra={"label": "跳过「问了也白问」的调用", "order": 12},
    )
    recent_pick_mode: str = Field(
        default="smart",
        description=(
            "近期经历怎么挑：smart = 层内按情绪/体力变化量排序（让「她此刻状态的成因」优先）"
            "+ 同标签去重 + 空层配额回收给有货的层；recent = 旧行为（固定配额、"
            "层内只取最新几条、不去重），出问题时可一键回退。填其它值一律按 smart 处理"
        ),
        json_schema_extra={
            "label": "经历挑法",
            "order": 13,
            "placeholder": "smart",
        },
    )
    recent_max_per_label: int = Field(
        default=1,
        description=(
            "同一个事件标签在**每一层内**最多出现几次。1 = 每层只出现一次"
            "（真机上 6 行里出现过 4 行都是「支线通关」、只有时间戳不同）。"
            "跨层允许重复是有意的：「近、远两层都有同一件事」=「她这几天一直在做同一件事」"
            "这个信号本身。0 = 不去重"
        ),
        json_schema_extra={"label": "每层同标签上限", "order": 14, "step": 1},
    )
    recent_events_keep: int = Field(
        default=300,
        description=(
            "状态文件里保留多少条「近期经历」（事件库与社交经历共用这个池子），"
            "提示词的近/中/远三层都从这里取。按事件约 18.9 条/日估算：300 条 ≈ 15.9 天，"
            "足够喂饱「远（14 天内）」层——v1.4.0 及以前固定 40 条 ≈ 2.1 天，"
            "远层永远拿不到内容。0 = 不保留任何经历（近层也会跟着空）。"
            "调大只多占一点状态文件空间（每条约 0.2KB），不影响倍率"
        ),
        json_schema_extra={"label": "经历留存条数", "order": 15, "step": 10},
    )


class ScheduleConfigModel(PluginConfigBase):
    """``[activity.schedule]``：作息班表（日程锚点）。

    默认**关闭**——打开后才会把「工作日 / 上下班时段 / 午休」写进活动提示词，
    并由强制层守住「上班时间不睡、在岗不摸鱼」。关着时行为与加这一层之前完全一致。
    """

    __ui_label__ = "作息班表"
    __ui_icon__ = "briefcase"
    __ui_order__ = 13

    enabled: bool = Field(
        default=False,
        description=(
            "按固定班表约束作息：提示词会写明「今天是不是工作日、现在是否在岗、岗位职责」，"
            "强制层则禁止上班时段睡觉、在岗时段打游戏/看番。适合有固定工作/上学的角色"
        ),
        json_schema_extra={"label": "启用班表", "order": 0},
    )
    workdays: str = Field(
        default="1-5",
        description='工作日，支持 "1-5"（周一至周五）、"六日"、"1,3,5"、"6-1"（跨周）',
        json_schema_extra={"label": "工作日", "order": 1, "placeholder": "1-5"},
    )
    work_window: str = Field(
        default="09:30-18:30",
        description="在岗时段（HH:MM-HH:MM）；通勤时间由下面那项从这个时刻往前推",
        json_schema_extra={"label": "在岗时段", "order": 2, "placeholder": "09:30-18:30"},
    )
    commute_minutes: int = Field(
        default=45,
        description="单程通勤时长（分钟）；上下班各占一段，0 = 没有通勤",
        json_schema_extra={"label": "单程通勤（分钟）", "order": 3, "step": 5},
    )
    lunch_window: str = Field(
        default="12:00-13:00",
        description="午休时段；这段时间不算「在岗」，摸鱼与放松在这里是允许的",
        json_schema_extra={"label": "午休时段", "order": 4, "placeholder": "12:00-13:00"},
    )
    duty: str = Field(
        default="",
        description="岗位/职责，写进活动提示词（例如「共鸣电台无线电技术员：观测共鸣、搜集与推送歌曲」）；留空则不提工作内容",
        json_schema_extra={"label": "岗位/职责", "order": 5, "rows": 2, "x-widget": "textarea"},
    )
    work_scene: str = Field(
        default="",
        description="冷启动 / 规则表在「在岗」相位使用的场景文本；留空用通用的「在工作」",
        json_schema_extra={"label": "在岗场景（可选）", "order": 6, "rows": 2, "x-widget": "textarea"},
    )
    honor_calendar: bool = Field(
        default=True,
        description=(
            "班表吃日历（v1.17.0）：法定节假日算休息日、调休的周末算上班日。"
            "关掉 = 退回「只按星期判定」（国庆长假会照常算工作日）——v1.16.3 的旧行为"
        ),
        json_schema_extra={"label": "按日历判定工作日", "order": 7},
    )
    daily_jitter_minutes: int = Field(
        default=0,
        description=(
            "按生活日的班表微扰（分钟，v1.17.0）：窗口整体前后平移这么多分钟，"
            "让她的上下班钟点不至于天天分秒不差。同一天恒定（确定性派生），"
            "0 = 关。上限 60"
        ),
        json_schema_extra={"label": "班表按日微扰（分钟）", "order": 8, "step": 5},
    )
    overtime_probability: float = Field(
        default=0.0,
        description=(
            "加班日概率（0–1，v1.17.0）：命中则当天下班端顺延下面那项分钟数，"
            "提示词写「今天要加班」。0 = 关"
        ),
        json_schema_extra={"label": "加班日概率", "order": 9, "step": 0.05},
    )
    overtime_extra_minutes: int = Field(
        default=60,
        description="加班日下班端顺延的分钟数（仅加班日生效；上限 480）",
        json_schema_extra={"label": "加班顺延（分钟）", "order": 10, "step": 15},
    )


class ActivityConfig(PluginConfigBase):
    """``[activity]``：作息活动——权重最大的一维。"""

    __ui_label__ = "作息活动"
    __ui_icon__ = "activity"
    __ui_order__ = 2

    mode: str = Field(
        default="llm",
        description="llm = 由模型依人设与近期事件决定；rules = 只用内置时段表（不调模型）",
        json_schema_extra={"label": "决策方式（llm/rules）", "order": 0, "placeholder": "llm"},
    )
    min_dwell_minutes: int = Field(
        default=60,
        description="清醒活动的最短停留时间（分钟）；防止模型每 10 分钟翻一次活动",
        json_schema_extra={"label": "最短停留（分钟）", "order": 1, "step": 10},
    )
    min_sleep_minutes: int = Field(
        default=180,
        description="单次睡眠的最短时长（分钟）；睡不够不会被叫醒",
        json_schema_extra={"label": "最短睡眠（分钟）", "order": 2, "step": 10},
    )
    min_awake_hours_per_day: float = Field(
        default=8.0,
        description="每日清醒下限（小时）；不够就拒绝入睡，防止模型一累就让她睡",
        json_schema_extra={"label": "每日清醒下限（小时）", "order": 3, "step": 0.5},
    )
    cold_onset_awake_floor: bool = Field(
        default=True,
        description=(
            "感冒**初起**也走每日清醒下限（v1.19.0 PR-AF-1），不再「病中随时可睡」。"
            "真机实测：初起也短路时，她睡到体力满醒来、下一轮又因病中立刻躺回去，"
            "清醒被压成 1 个 tick（10 分钟），睡觉占比 79.6%。"
            "关掉（False）= 回到「一生病就随时可睡」的旧行为。"
            "加重/好转期**不受**此开关影响，永远可以多躺"
        ),
        json_schema_extra={"label": "感冒初起也走清醒下限", "order": 9},
    )
    sleep_hard_floor: float = Field(
        default=0.5,
        description=(
            "体力低到这条线以下时**无视模型提议**强制入睡（v1.15.0）。"
            "0 = 关闭。原来只兜住了「模型没输出」那条路，模型一直提议别的事时"
            "她可以被按在 0 体力上熬夜"
        ),
        json_schema_extra={"label": "体力耗尽强制入睡", "order": 8, "step": 0.1},
    )
    reseed_after_hours: float = Field(
        default=8.0,
        description=(
            "插件**连续运行**这么久没有一次成功的模型决策，就按当前时刻用时段表"
            "重新取一次种子（模型挂了时她至少还能按作息走）。停机时间不计入。"
            "时段表槽位宽 1~3 小时，取 6~8 比较合适；24 等于让她在同一个活动里"
            "跨过一整个昼夜；0 = 关闭（会一直保持上个活动）"
        ),
        json_schema_extra={"label": "重新取种子（小时）", "order": 4, "step": 1},
    )
    activity_factors: list[str] = Field(
        default_factory=lambda: list(DEFAULT_ACTIVITY_FACTOR_LINES),
        description=(
            '活动 → 频率因子，每行一组 "活动=因子"。merge 模式（默认）缺键按内置'
            "默认补齐并告警；replace 模式以本列表为准——删掉某个内置因子后它保持"
            "删除，该活动按 1.0。注意 sleep=0 是「睡觉即静音」的硬闸，删掉前想清楚"
        ),
        json_schema_extra={
            "label": "活动因子（每行一组）",
            "order": 5,
            "rows": 15,
            "placeholder": "music=0.9",
        },
    )
    factors_mode: str = Field(
        default="merge",
        description=(
            "活动因子表的模式：merge = 配置与内置默认合并，缺的键按内置补齐并告警"
            "（防升级丢因子、防手滑删行）；replace = 以本列表为准、不再与内置合并——"
            "删掉的内置因子保持删除，该活动按 1.0，内置有而列表没写的键会告警提醒"
            "一次。填其它值按 merge 处理"
        ),
        json_schema_extra={"label": "因子表模式", "order": 6, "placeholder": "merge"},
    )
    llm: ActivityLLMConfig = Field(
        default_factory=ActivityLLMConfig,
        description="活动决策的模型调用参数；留空 task_name 走宿主的默认插件任务",
        json_schema_extra={"label": "活动模型调用参数", "order": 7},
    )

    _norm_activity_factors = _str_list_validator("activity_factors")


class CurveConfig(PluginConfigBase):
    """一套情绪/体力曲线。"""

    mood: list[str] = Field(
        default_factory=lambda: list(DEFAULT_MOOD_CURVE_LINES),
        description='情绪曲线，每行 "分数=因子"（分段线性）',
        json_schema_extra={"label": "情绪曲线", "order": 0, "rows": 3, "placeholder": "5=0.95"},
    )
    energy: list[str] = Field(
        default_factory=lambda: list(DEFAULT_ENERGY_CURVE_LINES),
        description='体力曲线，每行 "分数=因子"（分段线性）',
        json_schema_extra={"label": "体力曲线", "order": 1, "rows": 3, "placeholder": "5=0.85"},
    )

    _norm_curves = _str_list_validator("mood", "energy")


class CurvesConfig(PluginConfigBase):
    """按宿主触发模式分三套曲线，插件自动选。"""

    __ui_label__ = "曲线（按模式）"
    __ui_icon__ = "trending-up"
    __ui_order__ = 1

    frequency: CurveConfig = Field(
        default_factory=CurveConfig,
        description="宿主模式为 frequency（计数门，默认）时使用",
        json_schema_extra={"label": "计数门曲线（frequency）", "order": 0},
    )
    necessity: CurveConfig = Field(
        default_factory=CurveConfig,
        description="宿主模式为 reply_necessity（评分门，宿主 ≤1.3.1）时使用",
        json_schema_extra={"label": "评分门曲线（reply_necessity）", "order": 1},
    )
    dynamic: CurveConfig = Field(
        default_factory=CurveConfig,
        description=(
            "宿主模式为 dynamic（动态触发，MaiBot 1.3.2 起）时使用。"
            "该模式下倍率是「1 小时窗口内的目标回复比例」：≥1 即全放行、没有上行空间，"
            "所以这条曲线主要决定「状态差时压到多低」，整体调高不会有额外效果"
        ),
        json_schema_extra={"label": "动态触发曲线（dynamic）", "order": 2},
    )


class EmotionEnergyConfig(PluginConfigBase):
    """``[emotion_energy]``：情绪体力动力学。"""

    __ui_label__ = "情绪体力"
    __ui_icon__ = "heart"
    __ui_order__ = 3

    inertia_minutes: int = Field(
        default=40,
        description="事件冲击后的惯性期（分钟）：这段时间内情绪不回归，不会刚被气到就没事",
        json_schema_extra={"label": "情绪惯性期（分钟）", "order": 0, "step": 5},
    )
    recover_per_tick: float = Field(
        default=0.2,
        description="惯性期后每个推进间隔向基线回归多少分（0–10 制）",
        json_schema_extra={"label": "每次回归（分）", "order": 1, "step": 0.05},
    )
    recover_ratio_per_tick: float = Field(
        default=0.08,
        description=(
            "比例回归（v1.16.1）：每个推进间隔消除「与基线差距」的固定比例。"
            "真人的情绪是**爆发快消、余味长**——极端情绪初期回落快、快回到基线时变慢，"
            "固定步长做不到这一点。**设 0 = 回到「每次回归多少分」的旧线性行为**"
            "（此时上面的 `每次回归（分）` 生效）"
        ),
        json_schema_extra={"label": "比例回归（每 tick 消除比例）", "order": 2, "step": 0.01},
    )
    recover_min_step: float = Field(
        default=0.05,
        description=(
            "比例回归的每 tick 最小步长（0 = 不设下限）。没有它，「距基线 0.01」时"
            "比例步长会小到浮点精度以下、情绪尾巴永远擦不干净"
        ),
        json_schema_extra={"label": "比例回归最小步长", "order": 3, "step": 0.01},
    )
    sleep_recover_multiplier: float = Field(
        default=2.0,
        description="睡眠期间情绪恢复的倍数（参考设定：睡眠时恢复加倍）",
        json_schema_extra={"label": "睡眠恢复倍数", "order": 4, "step": 0.5},
    )
    inertia_scale_enabled: bool = Field(
        default=True,
        description=(
            "惯性期按冲击大小缩放（v1.16.1）：`惯性期 × |情绪增量|`，钳进下面的上下限。"
            "「笔没水了」（-0.3）冻结 12 分钟、「生日」（+1.5）冻结 60 分钟，"
            "而不是所有事都冻结 40 分钟。关掉 = 旧行为（一律固定惯性期）"
        ),
        json_schema_extra={"label": "惯性期按冲击缩放", "order": 5},
    )
    inertia_scale_min_minutes: int = Field(
        default=5,
        description="惯性期缩放的**下限**（分钟）：再小的事也让她缓一会儿",
        json_schema_extra={"label": "惯性缩放下限（分钟）", "order": 6, "step": 1},
    )
    inertia_scale_max_minutes: int = Field(
        default=90,
        description=(
            "惯性期缩放的**上限**（分钟）：防止一次大冲击把情绪冻住半天。"
            "取值为「本值与惯性期上限的较大者」——把惯性期配得比它还长时不会反被缩短"
        ),
        json_schema_extra={"label": "惯性缩放上限（分钟）", "order": 7, "step": 5},
    )
    afterglow_span_hours: float = Field(
        default=24.0,
        description="情绪余波统计窗口（小时）",
        json_schema_extra={"label": "余波窗口（小时）", "order": 8, "step": 1},
    )
    afterglow_cap: float = Field(
        default=0.6,
        description="余波对基线的最大偏移（±）；防止连续坏事把她永久压哑",
        json_schema_extra={"label": "余波上限（±分）", "order": 9, "step": 0.1},
    )
    afterglow_decay: bool = Field(
        default=True,
        description=(
            "余波按年龄线性衰减（v1.16.1）：`权重 = 1 − 事件年龄/窗口`，23 小时前的事"
            "几乎不留痕，也不再在出窗那一刻**跳变**消失（仓库里的「最佳保鲜相位」"
            "就是这个思路）。关掉 = 旧等权（窗口内一视同仁）"
        ),
        json_schema_extra={"label": "余波按年龄衰减", "order": 10},
    )
    afterglow_gain: float = Field(
        default=0.0,
        description=(
            "余波折算系数。**0 = 自动**：衰减开时 0.30、关时 0.15——等权改成衰减后"
            "有效总量约减半，系数要翻倍才能维持同等稳态余波。显式给值则以本值为准"
        ),
        json_schema_extra={"label": "余波折算系数（0 = 自动）", "order": 11, "step": 0.05},
    )
    fatigue_ramp_curve: list[str] = Field(
        default_factory=lambda: list(DEFAULT_FATIGUE_RAMP_LINES),
        description=(
            "清醒疲劳曲线（v1.16.0）：每行 `清醒小时=额外体力/小时`（负值），分段线性。"
            "她自己也会困——原来是「体力只由活动表决定」，清闲的一天永远掉不到入睡阈值，"
            "于是「困意」完全由睡眠窗口驱动而不是身体驱动。12 小时以内建议给 0（不惩罚"
            "正常的一天）。**清空 = 关闭 = 与升级前逐位一致**"
        ),
        json_schema_extra={"label": "清醒疲劳曲线（小时=体力/小时）", "order": 12, "rows": 4,
                           "placeholder": "12=0\n16=-0.15\n20=-0.4\n24=-0.7"},
    )
    emotion_fatigue_penalty: float = Field(
        default=0.3,
        description=(
            "疲劳压低情绪基线（v1.16.2）：`体力低于阈值` 时基线下移"
            " `本值 × (阈值 − 体力)`（体力 1 → −0.6、体力 0 → −0.9）。"
            "⚠ 这是「身体状态不直接进倍率」纪律的**唯一开口**（方案决议 1）：体力因子"
            "说的是「没力气说话」，基线下移说的是「累到不想说话」，两者是不同机制。"
            "**0 = 关闭（旧行为）**"
        ),
        json_schema_extra={"label": "疲劳压情绪的系数（0 = 关闭）", "order": 14, "step": 0.05},
    )
    emotion_fatigue_threshold: float = Field(
        default=3.0,
        description="体力低于它才开始压低情绪基线",
        json_schema_extra={"label": "疲劳压情绪的体力阈值", "order": 15, "step": 0.5},
    )
    low_energy_drain_multiplier: float = Field(
        default=1.25,
        description=(
            "低体力消耗放大（v1.16.2）：体力低于阈值时**负的**体力变化 ×本值"
            "（只放大消耗，不碰恢复项）——越累越容易更累的软恶性循环，把她推向休息。"
            "**1.0 = 关闭（旧行为）**"
        ),
        json_schema_extra={"label": "低体力消耗放大（1.0 = 关闭）", "order": 16, "step": 0.05},
    )
    low_energy_threshold: float = Field(
        default=3.0,
        description="体力低于它才开始放大消耗",
        json_schema_extra={"label": "低体力放大阈值", "order": 17, "step": 0.5},
    )
    emotion_impact_scaling: bool = Field(
        default=True,
        description=(
            "情绪冲击的边际效用（v1.16.3）：低谷时雪中送炭（正向 ×1.3）、高涨时快乐麻木"
            "（正向 ×0.7）、从高处跌落更疼（负向 ×1.2）、麻木时再挨一下没那么疼"
            "（负向 ×0.7）。作用在事件 / 日期 / 社交三条通道的情绪增量上。"
            "**关掉 = 增量原样进出（旧行为）**"
        ),
        json_schema_extra={"label": "情绪冲击边际效用", "order": 20},
    )
    impact_positive_curve: list[str] = Field(
        default_factory=lambda: list(DEFAULT_IMPACT_POSITIVE_LINES),
        description="正向增量曲线（每行 `当前情绪=系数`，分段线性、端点外取端点值）",
        json_schema_extra={"label": "正向冲击曲线（情绪=系数）", "order": 21, "rows": 3,
                           "placeholder": "2=1.3\n5=1.0\n8=0.7"},
    )
    impact_negative_curve: list[str] = Field(
        default_factory=lambda: list(DEFAULT_IMPACT_NEGATIVE_LINES),
        description="负向增量曲线（每行 `当前情绪=系数`）",
        json_schema_extra={"label": "负向冲击曲线（情绪=系数）", "order": 22, "rows": 3,
                           "placeholder": "2=0.7\n5=1.0\n8=1.2"},
    )

    _norm_impact_positive = _str_list_validator("impact_positive_curve")
    _norm_impact_negative = _str_list_validator("impact_negative_curve")
    baseline_diurnal_curve: list[str] = Field(
        default_factory=lambda: list(DEFAULT_DIURNAL_CURVE_LINES),
        description=(
            "日内节律曲线（v1.16.1）：每行 `本地时刻分钟=基线偏移`（分段线性）。"
            "清晨低落、傍晚松弛——她一天 24 小时的「底色」不再是一条直线。"
            "默认幅度 ±0.3（对倍率影响约 4%，是氛围不是数值）；"
            "**清空 = 关闭 = 与升级前逐位一致**"
        ),
        json_schema_extra={"label": "日内节律曲线（分钟=基线偏移）", "order": 13, "rows": 4,
                           "placeholder": "300=-0.3\n780=0.15\n1200=0.3\n1380=0"},
    )
    curves: CurvesConfig = Field(
        default_factory=CurvesConfig,
        description="两套曲线按宿主模式自动选：frequency = 计数门，reply_necessity = 评分门",
        json_schema_extra={"label": "情绪体力曲线（按宿主模式）", "order": 14},
    )

    _norm_fatigue_ramp = _str_list_validator("fatigue_ramp_curve")
    _norm_diurnal = _str_list_validator("baseline_diurnal_curve")


class FrequencyConfig(PluginConfigBase):
    """``[frequency]``：倍率管线的钳制与硬闸。"""

    __ui_label__ = "频率"
    __ui_icon__ = "sliders"
    __ui_order__ = 4

    mode_source: str = Field(
        default="auto",
        description=(
            "auto = 读宿主的 reply_trigger_mode 自动选曲线；也可强制 "
            "frequency / dynamic / reply_necessity（后者仅宿主 ≤1.3.1 有）"
        ),
        json_schema_extra={"label": "模式来源", "order": 0, "placeholder": "auto"},
    )
    max_adjust: float = Field(
        default=2.0,
        description="倍率上限。注意宿主生效频率 = talk_value × 倍率",
        json_schema_extra={"label": "倍率上限", "order": 1, "step": 0.1},
    )
    min_adjust: float = Field(
        default=0.0,
        description="倍率下限。0 = 允许完全静默",
        json_schema_extra={"label": "倍率下限", "order": 2, "step": 0.05},
    )
    quiet_hours: list[str] = Field(
        default_factory=lambda: list(DEFAULT_QUIET_HOURS_LINES),
        description='静默时段，每行 "HH:MM-HH:MM"；命中即写 0（不管在做什么）',
        json_schema_extra={"label": "静默时段（每行一组）", "order": 3, "rows": 2,
                           "placeholder": "23:30-08:00"},
    )
    paused: bool = Field(
        default=False,
        description="暂停：仍推进生活状态，但停止干预宿主频率（把倍率归还给外部基数）",
        json_schema_extra={"label": "暂停频率干预", "order": 4},
    )
    silence_floor: float = Field(
        default=0.0,
        description=(
            "睡眠/静默时段落地时的倍率下限。0 = 真静默（零模型开销，但会丢掉其它插件的主动开口，"
            "例如 group-welcome 的新人欢迎语）；>0 则不再静默：宿主**先判静默、再判 @ 强制触发**，"
            "所以任何触发模式下 @ 都会穿透进 Planner（不只是 reply_necessity），"
            "并且睡眠期间其它插件的主动开口也会一并生效"
        ),
        json_schema_extra={"label": "静默时段倍率下限", "order": 5, "step": 0.01},
    )
    material_bonus: float = Field(
        default=0.15,
        description=(
            "每条素材带来的加成（她攒了想说的话，就更想说）。按保鲜衰减后的"
            "**有效条数**计：全新鲜时 1 条 = 1，放旧的素材按比例折算"
        ),
        json_schema_extra={"label": "每条素材加成", "order": 6, "step": 0.05},
    )
    material_bonus_cap: float = Field(
        default=0.45,
        description="素材加成上限",
        json_schema_extra={"label": "素材加成上限", "order": 7, "step": 0.05},
    )

    _norm_quiet_hours = _str_list_validator("quiet_hours")


class HealthConfig(PluginConfigBase):
    """``[health]``：睡眠时长判熬夜、体力上限、感冒。"""

    __ui_label__ = "身体"
    __ui_icon__ = "activity"
    __ui_order__ = 5

    sleep_debt_threshold_minutes: int = Field(
        default=300,
        description=(
            "最近 24 小时累计睡眠低于它就算「没睡够的一夜」（默认 300 = 5 小时）。"
            "判据是滑动 24 小时窗口、在睡醒时结算，所以跨生活日边界的一段睡眠"
            "不会被切成两半分别判，拆成两段睡也会合起来算"
        ),
        json_schema_extra={"label": "熬夜阈值（分钟）", "order": 0, "step": 30},
    )
    sleep_debt_cap_nights: int = Field(
        default=3,
        description="连熬几夜后压低体力上限",
        json_schema_extra={"label": "连熬夜数", "order": 1, "step": 1},
    )
    sleep_debt_recovery_step: int = Field(
        default=1,
        description=(
            "一晚「睡够了」清掉几晚熬夜债（v1.15.0，默认 1 = 滞回：连熬三晚要三个好觉"
            "才还清）。0 = 旧行为（一晚直接归零）。旧行为与「体力满即醒」叠加时，"
            "她会在「熬夜 → 上限被压低 → 睡得更短」之间反复"
        ),
        json_schema_extra={"label": "每晚还几晚债", "order": 2, "step": 1},
    )
    sleep_deprived_energy_cap: float = Field(
        default=8.5,
        description="连熬之后的体力上限（参考设定：降到 8.5）",
        json_schema_extra={"label": "熬夜后体力上限", "order": 2, "step": 0.5},
    )
    health_factors: list[str] = Field(
        default_factory=lambda: list(DEFAULT_HEALTH_FACTOR_LINES),
        description='健康因子，每行一组 "状态=因子"（healthy/cold/sleep_deprived）',
        json_schema_extra={"label": "健康因子（每行一组）", "order": 3, "rows": 3},
    )
    cold_check_hour: int = Field(
        default=2,
        description="每天几点掷一次感冒骰子",
        json_schema_extra={"label": "感冒判定小时", "order": 4, "step": 1},
    )
    cold_min_days: int = Field(
        default=1,
        description="感冒最短持续天数",
        json_schema_extra={"label": "感冒最短天数", "order": 5, "step": 1},
    )
    cold_max_days: int = Field(
        default=3,
        description="感冒最长持续天数",
        json_schema_extra={"label": "感冒最长天数", "order": 6, "step": 1},
    )
    cold_base_risk: float = Field(
        default=0.05,
        description="感冒基础风险（0–1）",
        json_schema_extra={"label": "感冒基础风险", "order": 7, "step": 0.01},
    )
    cold_sleep_debt_risk: float = Field(
        default=0.08,
        description="每多一晚熬夜、以及体力很低时，各自增加的风险",
        json_schema_extra={"label": "熬夜/低体力风险增量", "order": 8, "step": 0.01},
    )
    cold_stage_factors: list[str] = Field(
        default_factory=lambda: list(DEFAULT_COLD_STAGE_FACTOR_LINES),
        description=(
            "病程阶段因子，每行一组 \"阶段=因子\"（onset 初起 / worsening 加重 / "
            "recovering 好转）。轻症与病重不再同一个压制值；留空则回退「健康因子」里的 cold"
        ),
        json_schema_extra={"label": "病程阶段因子（每行一组）", "order": 9, "rows": 3},
    )
    cold_immunity_days: int = Field(
        default=5,
        description="痊愈后几天内不再掷中招骰子（0 = 关掉免疫期）",
        json_schema_extra={"label": "病后免疫期（天）", "order": 10, "step": 1},
    )
    cold_convalescent_hours: int = Field(
        default=24,
        description=(
            "病后余韵时长（小时）：这段时间不算生病，只是提示词与状态卡里带一句"
            "「刚好利索，体力还没回来」（0 = 关掉）"
        ),
        json_schema_extra={"label": "病后余韵（小时）", "order": 11, "step": 1},
    )
    cold_care_daily_cap: int = Field(
        default=2,
        description=(
            "每日计入病程流转的关心次数上限（别人说「你吃药了吗」这类句式）。"
            "多出来的关心仍有情绪收益，但不再加速康复——0 = 关心不影响病程"
        ),
        json_schema_extra={"label": "每日关心计入上限", "order": 12, "step": 1},
    )
    cold_sick_leave: bool = Field(
        default=True,
        description=(
            "病假：加重/好转期她请病假，班表的在岗约束对她挂起（提示词与强制层都不再"
            "让她上班）。关掉 = 只有强制养病、没有病假说法"
        ),
        json_schema_extra={"label": "生病自动请病假", "order": 13},
    )
    cold_care_patterns: list[str] = Field(
        default_factory=lambda: list(DEFAULT_CARE_PATTERNS),
        description=(
            "关心句式正则表（每行一条）。默认要求「第二人称」（你吃药了吗）或"
            "「叮嘱/询问句式」（多喝热水、好点了吗），防「我昨天也感冒了」「他感冒一礼拜了」"
            "被当成关心；坏正则只告警并忽略。想更宽松可自己加行，例如 ^早点睡"
        ),
        json_schema_extra={"label": "关心句式（每行一条正则）", "order": 14, "rows": 3},
    )
    cold_season_factors: list[str] = Field(
        default_factory=list,
        description=(
            "季节风险系数，每行一组 \"月份=倍率\"（如 1=1.5、7=0.7），留空 = 不启用。"
            "启用后冬天更容易感冒，生病不再是均匀白噪声"
        ),
        json_schema_extra={"label": "季节风险系数（每行一组）", "order": 15, "rows": 3},
    )

    _norm_health_factors = _str_list_validator("health_factors")
    _norm_cold_stage_factors = _str_list_validator("cold_stage_factors")
    _norm_cold_care_patterns = _str_list_validator("cold_care_patterns")
    _norm_cold_season_factors = _str_list_validator("cold_season_factors")


class DateConfig(PluginConfigBase):
    """``[date]``：生日与自定义节日（默认没有内建日期）。"""

    __ui_label__ = "日期节日"
    __ui_icon__ = "calendar"
    __ui_order__ = 6

    birthday: str = Field(
        default="",
        description='生日（MM-DD），留空表示不登记。可填你农历生日对应的公历日期',
        json_schema_extra={"label": "生日（MM-DD）", "order": 0, "placeholder": "02-07"},
    )
    birthday_factor: float = Field(
        default=1.3,
        description="生日当天的频率倍率",
        json_schema_extra={"label": "生日倍率", "order": 1, "step": 0.1},
    )
    birthday_emotion: float = Field(
        default=1.5,
        description="生日当天的情绪增量",
        json_schema_extra={"label": "生日情绪增量", "order": 2, "step": 0.5},
    )
    birthday_material: str = Field(
        default="今天是我生日",
        description="生日当天产出的「想跟你说的素材」",
        json_schema_extra={"label": "生日素材", "order": 3, "rows": 2, "x-widget": "textarea"},
    )
    festivals: list[str] = Field(
        default_factory=list,
        description='节日，每行 "MM-DD|名称|emotion=..|energy=..|factor=..|weight=..|material=.."',
        json_schema_extra={"label": "节日（每行一组）", "order": 4, "rows": 3,
                           "placeholder": "06-01|儿童节|factor=1.1|material=今天路上好多小孩"},
    )
    date_factors: list[str] = Field(
        default_factory=list,
        description='按名称覆盖节日倍率，每行一组 "名称=因子"',
        json_schema_extra={"label": "节日倍率覆盖", "order": 5, "rows": 2, "placeholder": "生日=1.4"},
    )

    _norm_date_lists = _str_list_validator("festivals", "date_factors")


class EventsConfig(PluginConfigBase):
    """``[events]``：事件库的叠加与替换。"""

    __ui_label__ = "事件库"
    __ui_icon__ = "list"
    __ui_order__ = 7

    extra: list[str] = Field(
        default_factory=list,
        description='追加/覆盖事件，每行 "标签|activities=..|emotion=..|energy=..|weight=..|material=..|ttl=.."',
        json_schema_extra={"label": "追加事件（每行一组）", "order": 0, "rows": 3,
                           "placeholder": "期中考试|activities=sleep|emotion=-1|energy=-1"},
    )
    disabled: list[str] = Field(
        default_factory=list,
        description="要停用的事件标签",
        json_schema_extra={"label": "停用事件标签", "order": 1, "rows": 2, "placeholder": "加班"},
    )
    fire_probability: float = Field(
        default=0.4,
        description="每个推进间隔抽一次事件、命中的概率（参考设定：40%）",
        json_schema_extra={"label": "事件触发概率", "order": 2, "step": 0.05},
    )
    material_ttl_hours: float = Field(
        default=6.0,
        description="「想跟你说的素材」的有效期（小时）。参考设定是 6 小时过期",
        json_schema_extra={"label": "素材有效期（小时）", "order": 3, "step": 1},
    )
    material_best_ratio: float = Field(
        default=0.5,
        description=(
            "素材「最佳保鲜相位」：有效期的前这个比例是全额权重，之后线性衰减到"
            "「保鲜衰减下限」、到过期触底。衰减的素材在主动开口候选里自然排到队尾，"
            "素材加成也按有效条数折算。1 = 全额到过期（旧行为）"
        ),
        json_schema_extra={"label": "素材最佳保鲜比例", "order": 4, "step": 0.1},
    )
    material_decay_floor: float = Field(
        default=0.25,
        description=(
            "过了保鲜期后的权重下限（0–1）：0 = 衰减到零（等同提前过期），"
            "1 = 不衰减（旧行为）。衰减而非清零，让她「想说的念头」平滑降温"
        ),
        json_schema_extra={"label": "保鲜衰减下限", "order": 5, "step": 0.05},
    )

    _norm_event_lists = _str_list_validator("extra", "disabled")


class ApplyConfig(PluginConfigBase):
    """``[apply]``：把倍率写到哪些会话。"""

    __ui_label__ = "生效范围"
    __ui_icon__ = "target"
    __ui_order__ = 8

    interval_seconds: int = Field(
        default=60,
        description="把当前倍率同步到各会话的间隔（秒）。新会话最多等这么久才被覆盖",
        json_schema_extra={"label": "同步间隔（秒）", "order": 0, "step": 15},
    )
    platform: str = Field(
        default="all_platforms",
        description="取哪些平台的会话；all_platforms = 不限",
        json_schema_extra={"label": "平台", "order": 1, "placeholder": "all_platforms"},
    )
    filter_mode: str = Field(
        default="all",
        description="all = 所有会话；whitelist = 只在 target_chats 里；blacklist = 排除 target_chats",
        json_schema_extra={"label": "过滤模式", "order": 2, "placeholder": "all"},
    )
    target_chats: list[str] = Field(
        default_factory=list,
        description='目标列表，每行 "group:群号" 或 "private:QQ号"',
        json_schema_extra={"label": "目标会话（每行一组）", "order": 3, "rows": 3,
                           "placeholder": "group:123456"},
    )
    compose_external: bool = Field(
        default=True,
        description=(
            "开启后，下发 宿主上已有的倍率 × 生活倍率，并在卸载时只归还基数——"
            "这样 budget-pacer 之类也在写 frequency.set_adjust 的插件不会互相覆盖。"
            "关闭则直接覆盖宿主倍率（旧行为）"
        ),
        json_schema_extra={"label": "与其它插件乘性合成", "order": 4},
    )
    unbacked_retry_minutes: int = Field(
        default=15,
        description=(
            "对「写不进去」的会话（还没有 heartflow chat 对象，宿主会静默 no-op）"
            "多久重试一次；该会话一有消息进来就立刻重试。0 = 不退避"
        ),
        json_schema_extra={"label": "写不进去时的重试间隔（分钟）", "order": 5, "step": 5},
    )
    only_active_sessions: bool = Field(
        default=True,
        description=(
            "只干预「有活动迹象」的会话（在 active_window_hours 小时内收到过消息，"
            "或记忆里本来就在管）。宿主 chat.get_all_streams 会返回**全部历史会话**，"
            "真机上几十上百个历史会话都没有 heartflow chat：对它们写入必然被宿主静默 "
            "no-op（只记一条 warning），既刷日志又白跑 RPC。"
            "关掉则恢复「所有会话都先写一遍」的旧行为"
        ),
        json_schema_extra={"label": "只干预活跃会话", "order": 6},
    )
    active_window_hours: int = Field(
        default=72,
        description=(
            "「有活动迹象」的时间窗口：会话最后一次有消息（或最后一次主动开口）距现在"
            "超过这个小时数就不再干预。只对 [apply].only_active_sessions=true 生效。\n\n"
            "v1.23.2 为什么加这条：旧的判定是「state.sessions 里有没有这条记录」，而那个"
            "字典的 14 天清理只在超过 500 条时才跑——真机只有十几个会话，等于**永不清理**，"
            "于是「历史上收过一条消息的会话」会永久算活跃（真机实测 18 个会话一轮，"
            "其中多数早就没人说话了，名单外的僵尸会话也混在里面反复撞宿主策略）。\n\n"
            "0 = 不限时间（退回旧行为，只要见过就算活跃）"
        ),
        json_schema_extra={"label": "活跃判定窗口（小时）", "order": 7, "step": 24},
    )

    _norm_target_chats = _str_list_validator("target_chats")


class ProactiveConfigModel(PluginConfigBase):
    """``[proactive]``：主动开口（默认关闭）。"""

    __ui_label__ = "主动开口"
    __ui_icon__ = "message-circle"
    __ui_order__ = 9

    enabled: bool = Field(
        default=False,
        description="开启后，她攒到「想跟你说的素材」时会主动开话（走宿主的主动任务，Planner 仍可沉默）",
        json_schema_extra={"label": "启用主动开口", "order": 0},
    )
    score_threshold: float = Field(
        default=0.45,
        description="素材权重 + 体力偏置 + 情绪偏置 要达到它才开口",
        json_schema_extra={"label": "开口阈值", "order": 1, "step": 0.05},
    )
    min_interval_minutes: int = Field(
        default=180,
        description="同一会话两次主动之间的最小间隔（分钟）",
        json_schema_extra={"label": "最小间隔（分钟）", "order": 2, "step": 30},
    )
    recent_user_silence_minutes: int = Field(
        default=30,
        description="对方刚说过话的这段时间内不主动插嘴（分钟）",
        json_schema_extra={"label": "对方发言后冷却（分钟）", "order": 3, "step": 5},
    )
    daily_max: int = Field(
        default=1,
        description="每个会话每天最多主动几次",
        json_schema_extra={"label": "每日上限", "order": 4, "step": 1},
    )
    minimum_energy: float = Field(
        default=2.5,
        description="体力低于它就完全不主动开口（0–10 制）",
        json_schema_extra={"label": "体力下限", "order": 5, "step": 0.5},
    )
    quiet_hours: list[str] = Field(
        default_factory=lambda: list(DEFAULT_PROACTIVE_QUIET_LINES),
        description='主动开口的静默时段，每行 "HH:MM-HH:MM"',
        json_schema_extra={"label": "静默时段（每行一组）", "order": 6, "rows": 2,
                           "placeholder": "23:30-08:00"},
    )
    unanswered_backoff_factor: float = Field(
        default=2.0,
        description=(
            "未回应退避（仅私聊）：对方连续不回应时，最小间隔按 ×系数^连击 拉长"
            "（系数 2 → 3h/6h/12h…）；对方一回话立即归零。群聊与判不出类型的会话"
            "维持现有硬闸。1 = 关闭退避"
        ),
        json_schema_extra={"label": "未回应退避系数", "order": 7, "step": 0.5},
    )
    unanswered_backoff_max_streak: int = Field(
        default=4,
        description=(
            "退避连击上限（系数 2、上限 4 → 最长拉到 16 倍 = 48 小时）。"
            "封顶要**大于**「每天一次」的自然节奏才有意义：设 3（24 小时）时，"
            "在默认每日上限 1 次下退避永远不是约束条件；48 小时则确定性地变成隔天一次"
        ),
        json_schema_extra={"label": "退避连击上限", "order": 8, "step": 1},
    )
    filter_mode: str = Field(
        default="all",
        description=(
            "主动开口自己的生效范围：all = 跟随 [apply] 的范围；whitelist / blacklist = "
            "在 [apply] 范围内再按 target_chats 收窄（最终范围 = 两者交集，[apply] 始终是总闸）。"
            "写错值按最保守的 whitelist 处理并告警一次"
        ),
        json_schema_extra={"label": "主动开口范围（all/whitelist/blacklist）", "order": 9,
                           "placeholder": "all"},
    )
    target_chats: list[str] = Field(
        default_factory=list,
        description=(
            '主动开口的目标列表，语法与 [apply].target_chats 一致，每行 "group:群号" 或 '
            '"private:QQ号"；仅在 filter_mode 非 all 时使用。whitelist 为空 = 不在任何'
            "会话主动开口（保守行为，会告警一次）"
        ),
        json_schema_extra={"label": "主动开口目标（每行一组）", "order": 10, "rows": 3,
                           "placeholder": "group:123456"},
    )
    inject_context_fact: bool = Field(
        default=True,
        description=(
            "开口前先往该会话写一条「她为什么突然开口」的上下文事实（活动 / 情绪体力 / "
            "想聊什么）。模型因此知道来由，而不是突兀地自说自话。关掉则只靠意图文本"
        ),
        json_schema_extra={"label": "写入开口来由", "order": 11},
    )
    failure_cooldown_minutes: int = Field(
        default=15,
        description=(
            "一次**没发出去**的尝试（触发失败、或确认 Planner 沉默）之后的冷却分钟数。"
            "没有它时宿主持续故障会让每个推进周期都重评一次并刷一条告警。0 = 关闭"
        ),
        json_schema_extra={"label": "失败后冷却（分钟）", "order": 12, "step": 5},
    )
    confirm_send: bool = Field(
        default=True,
        description=(
            "开口后延迟核实她是否真的说出了口：宿主对主动任务只回「已受理」，"
            "Planner 仍可沉默；受理就计数会让每日上限被一次沉默白吃掉。"
            "关掉则退回旧语义（受理即计一次）"
        ),
        json_schema_extra={"label": "核实真的说出了口", "order": 13},
    )
    confirm_delay_minutes: int = Field(
        default=10,
        description="开口后等多久去核实（分钟）：给宿主的主动轮留足跑完的时间",
        json_schema_extra={"label": "核实延迟（分钟）", "order": 14, "step": 5},
    )
    interval_jitter_ratio: float = Field(
        default=0.0,
        description=(
            "最小间隔的抖动比例：0 = 关（精确间隔）；0.15 = 间隔随机上浮 0~15%。"
            "倍率按「生活日 + 会话 + 当天第几次」确定性派生，同一天恒同值，重启一致"
        ),
        json_schema_extra={"label": "间隔抖动比例", "order": 15, "step": 0.05},
    )
    max_unanswered_times: int = Field(
        default=0,
        description=(
            "连续未回应硬上限：对方连续这么多次没回话就彻底停口（适用面同「群聊也启用退避」），"
            "对方一回复立即恢复。与未回应退避互补（退避管节奏、上限管止损）。"
            "0 = 关闭，只靠退避"
        ),
        json_schema_extra={"label": "连续未回应上限", "order": 16, "step": 1},
    )
    unanswered_backoff_group: bool = Field(
        default=True,
        description=(
            "未回应退避是否对**群聊**也生效（v1.22.2 起默认开）。原来是「群聊总会有人接」"
            "所以只对私聊退避；允许她去活跃冷群之后这个假设反过来——没人的群最不可能有人接。"
            "关掉则回到 v1.22.1 的仅私聊退避"
        ),
        json_schema_extra={"label": "群聊也启用退避", "order": 17},
    )
    cold_group_enabled: bool = Field(
        default=False,
        description=(
            "冷群搭话（v1.22.2，默认关）：群聊超过「冷群判据」没人说话时，她抛一条"
            "**能被回答**的公共话题（吃/睡/追剧/作息这类），而不是汇报自己的生活。"
            "素材按会话即时生成、不落盘、不进全局素材池，不会漂到别的群。"
            "建议配合「主动开口范围 = whitelist」只对你希望她活跃的几个群开启"
        ),
        json_schema_extra={"label": "冷群搭话", "order": 18},
    )
    cold_group_hours: float = Field(
        default=2.0,
        description=(
            "「冷」的判据（小时）：最后一条人类消息距今超过它才算冷。0 = 关闭。"
            "刻意不是「主动节奏」——节奏仍由最小间隔与退避决定"
        ),
        json_schema_extra={"label": "冷群判据（小时）", "order": 19, "step": 0.5},
    )

    _norm_proactive_quiet = _str_list_validator("quiet_hours")
    _norm_proactive_targets = _str_list_validator("target_chats")


class PromptConfig(PluginConfigBase):
    """``[prompt]``：把生活摘要注入回复提示词。"""

    __ui_label__ = "提示词注入"
    __ui_icon__ = "edit"
    __ui_order__ = 10

    inject_enabled: bool = Field(
        default=True,
        description="把当前生活状态（活动/情绪/体力/最近经历）注入回复请求，让语气贴合状态",
        json_schema_extra={"label": "注入生活摘要", "order": 0},
    )
    max_chars: int = Field(
        default=600,
        description="注入文本的最大长度",
        json_schema_extra={"label": "注入上限（字符）", "order": 1, "step": 50},
    )


class SecurityConfig(PluginConfigBase):
    """``[security]``：谁能改状态（fail-closed）。"""

    __ui_label__ = "权限"
    __ui_icon__ = "shield"
    __ui_order__ = 11

    admin_ids: list[str] = Field(
        default_factory=list,
        description='管理员 QQ，每行一个。留空 = 只有本机控制台操作者能改状态（fail-closed）',
        json_schema_extra={"label": "管理员 QQ（每行一个）", "order": 0, "rows": 3,
                           "placeholder": "123456"},
    )

    _norm_admin_ids = _str_list_validator("admin_ids")


class EconomyConfig(PluginConfigBase):
    """``[economy]``：把真实月度预算接成她的「经济」维度（默认开启，缺失即降级）。

    数据来自 budget-pacer 的公开只读 API（``get_budget_status``）。生活费 = 月预算、
    每天开销 = 当日真实花费、余额 = 生活费 − 本月已花。**本维度不参与倍率**：
    宿主每个会话只有一个频率标量，budget-pacer 已经在按预算压它，再乘一次就是
    同一原因双重压制；经济只影响她的**行为与展示**。
    """

    __ui_label__ = "经济（月度预算）"
    __ui_icon__ = "wallet"
    __ui_order__ = 12

    enabled: bool = Field(
        default=True,
        description=(
            "把 budget-pacer 的真实月度预算接成她的生活费账本，并在手头紧时让她省着花。"
            "关掉则这一维完全不取数、不调用任何跨插件 API"
        ),
        json_schema_extra={"label": "接入预算插件", "order": 0},
    )
    api_plugin_id: str = Field(
        default="org.orge-8.budget-pacer",
        description="预算插件 ID（提供 get_budget_status 的插件）",
        json_schema_extra={"label": "预算插件 ID", "order": 1},
    )
    api_name: str = Field(
        default="get_budget_status",
        description="要调用的 API 名（version 1 的只读预算快照）",
        json_schema_extra={"label": "API 名", "order": 2},
    )
    api_version: str = Field(
        default="1",
        description="API 版本；预算插件做不兼容更新时会递增",
        json_schema_extra={"label": "API 版本", "order": 3},
    )
    refresh_interval_minutes: int = Field(
        default=30,
        description="多久重新取一次预算数据（分钟）。取数是只读 RPC，不唤醒模型",
        json_schema_extra={"label": "取数间隔（分钟）", "order": 4, "step": 5},
    )
    stale_after_minutes: int = Field(
        default=180,
        description=(
            "数据超过这个时长算过期：仍然显示在状态卡上并带 ⚠，但不再拿它约束她的行为。"
            "0 = 关闭过期检查"
        ),
        json_schema_extra={"label": "过期时长（分钟）", "order": 5, "step": 30},
    )
    broke_ratio: float = Field(
        default=0.10,
        description="余额占生活费比例低于它 = 见底（或已超支）",
        json_schema_extra={"label": "见底阈值（余额占比）", "order": 6, "step": 0.05},
    )
    tight_ratio: float = Field(
        default=0.30,
        description="余额占生活费比例低于它 = 手头紧",
        json_schema_extra={"label": "手头紧阈值（余额占比）", "order": 7, "step": 0.05},
    )
    roomy_ratio: float = Field(
        default=0.60,
        description="余额占生活费比例高于它 = 宽裕",
        json_schema_extra={"label": "宽裕阈值（余额占比）", "order": 8, "step": 0.05},
    )
    tight_pacing: float = Field(
        default=1.20,
        description=(
            "节奏比（花费进度 ÷ 时间进度）高于它也算手头紧——但只在这条成立时才看："
            "花费进度已经达到下面那个阈值，避免月初被时间进度下限放大成误报"
        ),
        json_schema_extra={"label": "花费超前的节奏比阈值", "order": 9, "step": 0.1},
    )
    pacing_from_spend_ratio: float = Field(
        default=0.50,
        description=(
            "花费进度至少到这个比例，才允许节奏比把档位升级为「手头紧」。"
            "预算插件的月初时间进度被 p_floor_days 兜在 1 天上，"
            "不设这道门槛的话「花了 2% 的钱」就能算出节奏比 2.0"
        ),
        json_schema_extra={"label": "节奏比生效的最低花费进度", "order": 10, "step": 0.05},
    )
    hint_in_prompt: bool = Field(
        default=True,
        description=(
            "手头紧时是否把「省着花」写进活动决策提示词。"
            "关掉则经济只展示、不影响她的行为"
        ),
        json_schema_extra={"label": "影响她的行为", "order": 11},
    )
    api_timeout_seconds: int = Field(
        default=10,
        description="跨插件调用的超时（秒）。宿主默认 30 秒太长，会把生活循环一起卡住",
        json_schema_extra={"label": "调用超时（秒）", "order": 12, "step": 5},
    )


class SocialConfig(PluginConfigBase):
    """``[social]``：把「这几天聊了什么」接成她的经历（默认关闭）。

    数据来自 better-diary 的**只读** API ``get_day_digest``（选材事件：谁、做了什么、
    原话），再叠上 ``chat.receive.after_process`` 钩子收到的**无原文**信号
    （「有人找过她」）。两条都只往她的「近期经历」里加条目，不参与倍率、不写宿主。
    """

    __ui_label__ = "社交经历（与日记插件联动）"
    __ui_icon__ = "chat"
    __ui_order__ = 14

    enabled: bool = Field(
        default=False,
        description=(
            "把日记插件的选材摘要接成她的「近期经历」，让活动决策知道她这几天和谁聊了什么。"
            "关掉则完全不调跨插件 API、也不记录入站信号"
        ),
        json_schema_extra={"label": "接入社交经历", "order": 0},
    )
    api_plugin_id: str = Field(
        default="org.orge-8.better-diary",
        description="日记插件 ID（提供 get_day_digest 的插件）",
        json_schema_extra={"label": "日记插件 ID", "order": 1},
    )
    api_name: str = Field(
        default="get_day_digest",
        description="要调用的 API 名（version 1 的只读选材摘要）",
        json_schema_extra={"label": "API 名", "order": 2},
    )
    api_version: str = Field(
        default="1",
        description="API 版本；日记插件做不兼容更新时会递增",
        json_schema_extra={"label": "API 版本", "order": 3},
    )
    fetch_days: int = Field(
        default=2,
        description=(
            "一次往回取几天。2 = 昨天 + 今天（日记是睡前生成的，取 2 天才能盖住"
            "「昨天睡前」到「今天现在」这段）。上限 30"
        ),
        json_schema_extra={"label": "往回取几天", "order": 4, "step": 1},
    )
    refresh_interval_minutes: int = Field(
        default=30,
        description="多久重新取一次摘要（分钟）。只读 RPC，不唤醒模型",
        json_schema_extra={"label": "取数间隔（分钟）", "order": 5, "step": 5},
    )
    digest_emotion: float = Field(
        default=0.4,
        description=(
            "一条「她今天和人聊到的事」值多少情绪（24 小时内的事才算，更早的早通过"
            "情绪余波结算过了）"
        ),
        json_schema_extra={"label": "聊天经历的情绪影响", "order": 6, "step": 0.05},
    )
    mention_emotion: float = Field(
        default=0.3,
        description="有人点名找她（群里 @ / 私聊）值多少情绪",
        json_schema_extra={"label": "被叫到的情绪影响", "order": 7, "step": 0.05},
    )
    group_emotion: float = Field(
        default=0.1,
        description="只是群里有人在聊（没点她）值多少情绪",
        json_schema_extra={"label": "群里有动静的情绪影响", "order": 8, "step": 0.05},
    )
    daily_emotion_cap: float = Field(
        default=1.5,
        description=(
            "每个生活日的社交情绪**总额度**，用完就只记事、不再加情绪。"
            "没有这道闸的话「群里今天多热闹」会直接变成她的情绪基线"
        ),
        json_schema_extra={"label": "每日情绪额度", "order": 9, "step": 0.5},
    )
    record_while_asleep: bool = Field(
        default=True,
        description="睡觉时也把「有人找过她」记进经历（醒来时能在近期经历里看到）",
        json_schema_extra={"label": "睡眠中也记录", "order": 10},
    )
    emotion_while_asleep: bool = Field(
        default=False,
        description=(
            "睡眠中的社交是否也产生情绪增量。默认关：被找这件事记下来，"
            "但情绪不吃这笔账"
        ),
        json_schema_extra={"label": "睡眠中也影响情绪", "order": 11},
    )
    include_quote: bool = Field(
        default=False,
        description=(
            "是否把对方的「原话」写进经历正文。默认关：更省 token，"
            "也让她的经历里少一点外部原文"
        ),
        json_schema_extra={"label": "带上原话", "order": 12},
    )
    max_digest_events_per_day: int = Field(
        default=3,
        description="一天最多接几条聊天摘要进经历（与日记插件的 max_events 同量级）",
        json_schema_extra={"label": "每日聊天经历上限", "order": 13, "step": 1},
    )
    max_live_events_per_day: int = Field(
        default=2,
        description=(
            "一天最多产出几条「有人找我」。**必须有上界**：经历只有 40 个位置，"
            "不设上限会让攒下的信号挤掉她自己的生活事件"
        ),
        json_schema_extra={"label": "每日「有人找我」上限", "order": 14, "step": 1},
    )
    api_timeout_seconds: int = Field(
        default=10,
        description="跨插件调用的超时（秒）。只读接口，卡住就该立刻降级",
        json_schema_extra={"label": "调用超时（秒）", "order": 15, "step": 5},
    )

    # ---- 社交情绪按关系加权（v1.16.3 M7）----
    relation_emotion_scaling: bool = Field(
        default=True,
        description=(
            "熟人找她更开心（v1.16.3）：社交情绪按 `[relations]` 的熟悉度加权"
            "（陌生 1.0 / 熟 1.2 / 亲密 1.5，单向只放大、封顶 1.5）。"
            "**中性锚点钉在陌生人身上**（决议 3）：新装插件时 relations 没有任何数据、"
            "所有人都是陌生 ⇒ 与升级前逐位一致，偏离只随关系加深发生。"
            "仍受每日情绪额度约束（先缩放、再封顶）。关掉 = 所有人生效相同"
        ),
        json_schema_extra={"label": "社交情绪按关系加权", "order": 16},
    )
    relation_emotion_curve: list[str] = Field(
        default_factory=lambda: list(DEFAULT_RELATION_EMOTION_LINES),
        description=(
            "熟悉度 → 社交情绪系数（v1.16.3）：每行 `熟悉度=系数`，分段线性、"
            "端点外取端点值；系数会被钳到 [1.0, 1.5]（单向只放大）"
        ),
        json_schema_extra={"label": "关系系数曲线（熟悉度=系数）", "order": 17, "rows": 3,
                           "placeholder": "20=1.0\n50=1.2\n80=1.5"},
    )

    _norm_relation_emotion_curve = _str_list_validator("relation_emotion_curve")


class WorldConfig(PluginConfigBase):
    """``[world]``：把「外面的世界」接成她的经历。

    四个**只读**数据源（全部 ``@API(version="1", public=True)``，零网络零写盘）：

    - ``bilibili-live-gateway.get_live_status``       → UP 主开播了
    - ``bilibili-dynamic-push.get_recent_pushes``     → UP 主发了新视频 / 新动态
    - ``group-welcome.get_recent_newcomers``          → 群里来了个新人
    - ``cv_lyric_context.get_recent_songs``           → 有人聊到了《X》
    - ``bilibili-dynamic-push.get_subscriptions``     → 只用于状态卡（订阅了谁）

    **总开关默认开、每个源独立可关**：每个源都自带降级（未装 / 未升级 / 超时 / 坏结构
    都只少一类事件），所以「没装那些插件」不会报错、也不会拖住生活循环。
    """

    __ui_label__ = "外面的世界（跨插件联动）"
    __ui_icon__ = "globe"
    __ui_order__ = 15

    enabled: bool = Field(
        default=True,
        description=(
            "把「UP 主开播 / 发了新动态 / 群里来新人 / 有人聊到某首歌」接成她的近期经历。"
            "关掉则完全不调这些跨插件 API"
        ),
        json_schema_extra={"label": "接入外面的世界", "order": 0},
    )
    live_enabled: bool = Field(
        default=True,
        description="接入「UP 主开播」（bilibili-live-gateway 的 get_live_status）",
        json_schema_extra={"label": "开播", "order": 1},
    )
    video_enabled: bool = Field(
        default=True,
        description="接入「UP 主发了新动态」（bilibili-dynamic-push 的 get_recent_pushes）",
        json_schema_extra={"label": "新动态", "order": 2},
    )
    newcomer_enabled: bool = Field(
        default=True,
        description="接入「群里来了个新人」（group-welcome 的 get_recent_newcomers）",
        json_schema_extra={"label": "新人", "order": 3},
    )
    song_enabled: bool = Field(
        default=True,
        description="接入「有人聊到了某首歌」（cv_lyric_context 的 get_recent_songs）",
        json_schema_extra={"label": "歌曲", "order": 4},
    )

    live_plugin_id: str = Field(
        default="org.mai-mai.bilibili-live-gateway",
        description="直播间插件 ID（提供 get_live_status）",
        json_schema_extra={"label": "直播插件 ID", "order": 5},
    )
    push_plugin_id: str = Field(
        default="org.mai-mai.bilibili-dynamic-push",
        description="动态推送插件 ID（提供 get_recent_pushes / get_subscriptions）",
        json_schema_extra={"label": "动态插件 ID", "order": 6},
    )
    newcomer_plugin_id: str = Field(
        default="org.orge-8.group-welcome",
        description="群欢迎插件 ID（提供 get_recent_newcomers）",
        json_schema_extra={"label": "群欢迎插件 ID", "order": 7},
    )
    song_plugin_id: str = Field(
        default="org.mai-mai.cv-lyric-context",
        description="歌词插件 ID（提供 get_recent_songs）",
        json_schema_extra={"label": "歌词插件 ID", "order": 8},
    )
    api_version: str = Field(
        default="1",
        description="这些 API 的版本；上游做不兼容更新时会递增",
        json_schema_extra={"label": "API 版本", "order": 9},
    )

    refresh_interval_minutes: int = Field(
        default=10,
        description=(
            "多久取一次（分钟）。只读 RPC、不唤醒模型；每个源单独 5 秒超时，"
            "任一源超时只少那一类事件"
        ),
        json_schema_extra={"label": "取数间隔（分钟）", "order": 10, "step": 5},
    )
    api_timeout_seconds: int = Field(
        default=5,
        description="单个源每次调用的超时（秒）。四个源串行最坏会累加，所以别设太大",
        json_schema_extra={"label": "单源超时（秒）", "order": 11, "step": 1},
    )
    newcomer_window_seconds: int = Field(
        default=86400,
        description="向群欢迎插件要多久以内入群的新人（秒），默认 24 小时",
        json_schema_extra={"label": "新人时间窗（秒）", "order": 12, "step": 3600},
    )
    max_items_per_source: int = Field(
        default=10,
        description="每个源单轮最多取几条（上游自己也有上限，这里再兜一层，1~50）",
        json_schema_extra={"label": "单源条数上限", "order": 13, "step": 1},
    )

    live_emotion: float = Field(
        default=0.20,
        description="「关注的 UP 主开播了」值多少情绪（24 小时内的事才算）",
        json_schema_extra={"label": "开播的情绪影响", "order": 14, "step": 0.05},
    )
    video_emotion: float = Field(
        default=0.15,
        description="「UP 主发了新视频/新动态」值多少情绪",
        json_schema_extra={"label": "新动态的情绪影响", "order": 15, "step": 0.05},
    )
    newcomer_emotion: float = Field(
        default=0.10,
        description="「群里来了个新人」值多少情绪",
        json_schema_extra={"label": "新人的情绪影响", "order": 16, "step": 0.05},
    )
    song_emotion: float = Field(
        default=0.12,
        description="「有人聊到了某首歌」值多少情绪",
        json_schema_extra={"label": "歌曲的情绪影响", "order": 17, "step": 0.05},
    )
    daily_emotion_cap: float = Field(
        default=1.0,
        description="每个生活日「外面的世界」能给她多少情绪；用完只记事、不加情绪",
        json_schema_extra={"label": "每日情绪额度", "order": 18, "step": 0.1},
    )
    max_events_per_day: int = Field(
        default=6,
        description=(
            "每个生活日最多接进几条世界事件。**必须有上界**：否则热闹的群会把"
            "她自己的生活事件挤掉"
        ),
        json_schema_extra={"label": "每日条数上限", "order": 19, "step": 1},
    )


class CalendarConfig(PluginConfigBase):
    """``[calendar]``：中国日历（v1.10.0）——法定节假日 / 调休 / 主要农历节日。

    数据是**随插件分发的离线表**（``data/calendar.toml``），不是用户配置：
    农历日期与调休安排任何算法都算不出来，只有国务院公布的表才可信。
    ``extra`` 允许用户追加自己的重要日子（纪念日、生日级事件）。
    """

    __ui_label__ = "中国日历"
    __ui_icon__ = "calendar"
    __ui_order__ = 18

    enabled: bool = Field(
        default=True,
        description="启用日历：节假日影响班表与提示词、节日进日期行与素材",
        json_schema_extra={"label": "启用中国日历", "order": 0},
    )
    extra: list[str] = Field(
        default_factory=list,
        description=(
            '追加自己的日子，每行 "YYYY-MM-DD|名称|kind"（kind: holiday=放假 / '
            "festival=节日不放假 / workday_swap=调休上班，默认 festival）"
        ),
        json_schema_extra={
            "label": "追加日期（每行一组）",
            "order": 1,
            "rows": 2,
            "placeholder": "2026-05-20|纪念日|festival",
        },
    )

    _norm_calendar_lists = _str_list_validator("extra")


class MoodConfig(PluginConfigBase):
    """``[mood]``：内心维度（stress / loneliness / social battery，v1.10.1）。

    ⚠ 三者**只进 prompt / 素材 / 主动开口硬闸，不进倍率**——仓库纪律
    （经济维度先例 +「不要双重抑制」）：倍率已有活动×情绪×体力三层调制。
    """

    __ui_label__ = "内心状态"
    __ui_icon__ = "heart"
    __ui_order__ = 19

    enabled: bool = Field(
        default=True,
        description="启用内心维度：压力/孤独/社交电量（只影响她怎么说、何时开口，不影响频率倍率）",
        json_schema_extra={"label": "启用内心维度", "order": 0},
    )
    inject_notice: bool = Field(
        default=True,
        description=(
            "压力/孤独/电量过载时，向会话注入一条世界内的语气提示"
            "（每天每会话至多一条；失败只降级）"
        ),
        json_schema_extra={"label": "消息风格注入", "order": 1},
    )

    stress_regress_per_tick: float = Field(
        default=0.02, description="无事件时压力每 tick（10 分钟）向基线回归的步长",
        json_schema_extra={"label": "压力回归步长", "order": 2, "step": 0.01},
    )
    stress_per_work_tick: float = Field(
        default=0.05, description="工作/会议/加班/熬夜类活动每 tick 的压力上调",
        json_schema_extra={"label": "工作压力增速", "order": 3, "step": 0.01},
    )
    stress_relief_per_sleep_tick: float = Field(
        default=0.08, description="睡眠每 tick 的压力下调",
        json_schema_extra={"label": "睡眠解压", "order": 4, "step": 0.01},
    )
    battery_per_message: float = Field(
        default=0.05, description="每条入站消息消耗的社交电量",
        json_schema_extra={"label": "消息耗电", "order": 5, "step": 0.05},
    )
    battery_per_mention: float = Field(
        default=0.15, description="被 @ 一次消耗的社交电量（比普通消息累）",
        json_schema_extra={"label": "被 @ 耗电", "order": 6, "step": 0.05},
    )
    battery_per_proactive: float = Field(
        default=0.3, description="她主动开口一次消耗的社交电量",
        json_schema_extra={"label": "主动开口耗电", "order": 7, "step": 0.05},
    )

    # ---- 睡眠的情绪侧（v1.15.0，PR-R2）----
    insomnia_enabled: bool = Field(
        default=True,
        description=(
            "入睡困难（v1.15.0）：压力大的时候躺下也睡不着——把「入睡」收口成"
            "「准备睡觉·翻来覆去睡不着」，并记一条经历。体力已经耗尽时仍然沾床就着"
        ),
        json_schema_extra={"label": "启用入睡困难", "order": 10},
    )
    insomnia_stress_threshold: float = Field(
        default=7.0,
        description="压力达到它才可能睡不着（0–10）",
        json_schema_extra={"label": "失眠压力阈值", "order": 11, "step": 0.5},
    )
    insomnia_probability: float = Field(
        default=0.5,
        description="满足条件时「这一觉睡不着」的概率；每生活日至多一次",
        json_schema_extra={"label": "失眠概率", "order": 12, "step": 0.05},
    )
    night_waking_enabled: bool = Field(
        default=False,
        description=(
            "夜间易醒（v1.15.0，默认关）：长睡眠中段偶尔醒一下（切「发呆」一小段再睡回去），"
            "并记一条「半夜醒了一下」的经历。默认关是为了先观察失眠那一半的效果"
        ),
        json_schema_extra={"label": "启用夜间易醒", "order": 13},
    )
    night_waking_probability: float = Field(
        default=0.02,
        description="每个推进间隔（默认 10 分钟）夜里醒过来的概率；每夜至多一次",
        json_schema_extra={"label": "夜醒概率", "order": 14, "step": 0.01},
    )

    # ---- 内心维度 → 外显情绪（v1.16.2 M3）----
    stress_breakdown_enabled: bool = Field(
        default=True,
        description=(
            "高压崩溃（v1.16.2）：压力达到阈值并**持续**若干小时后，她真的会崩一次"
            "（情绪 −0.8 + 一条「绷不住」的素材与经历，每生活日至多一次）。"
            "修的是「提示词说她没耐心、数值却比平时还高」的言行不一。"
            "事件措辞是硬编码的确定性产出（与病程事件同一套管线），只暴露开关与阈值"
        ),
        json_schema_extra={"label": "启用高压崩溃", "order": 20},
    )
    stress_breakdown_threshold: float = Field(
        default=7.0,
        description="压力达到它并持续够久才可能崩（0–10）",
        json_schema_extra={"label": "崩溃压力阈值", "order": 21, "step": 0.5},
    )
    stress_breakdown_hours: float = Field(
        default=2.0,
        description="压力要**持续**这么久才算绷不住（瞬时高压不算）",
        json_schema_extra={"label": "高压持续时长（小时）", "order": 22, "step": 0.5},
    )
    stress_breakdown_reset: float = Field(
        default=5.0,
        description="压力回落到它以下就清掉高压起点（下一次高压重新计时）",
        json_schema_extra={"label": "高压重置阈值", "order": 23, "step": 0.5},
    )
    loneliness_social_scaling: bool = Field(
        default=True,
        description=(
            "孤独放大社交收益（v1.16.2）：孤独的人被找更开心（孤独高 → 情绪收益最高 ×1.5）、"
            "被爱包围的人对打扰更钝感（孤独低 → ×0.8）。**仍受每日情绪额度约束**"
            "（先缩放、再封顶）；关掉 = 所有人生效相同（旧行为）"
        ),
        json_schema_extra={"label": "孤独放大社交收益", "order": 24},
    )
    loneliness_social_curve: list[str] = Field(
        default_factory=lambda: list(DEFAULT_LONELINESS_SOCIAL_LINES),
        description=(
            "孤独 → 社交情绪系数（v1.16.2）：每行 `孤独值=系数`，分段线性、端点外取端点值"
        ),
        json_schema_extra={"label": "孤独系数曲线（孤独=系数）", "order": 25, "rows": 2,
                           "placeholder": "2=0.8\n7=1.5"},
    )

    _norm_loneliness_curve = _str_list_validator("loneliness_social_curve")


class RelationsConfig(PluginConfigBase):
    """``[relations]``：关系模型（v1.11.0，决策 5：**默认开启**）。

    只对「明确跟她说过话」的人建档：私聊任意消息 / 群聊被 @ 或被回复。
    档案存 SQLite（life_store.db 的 relationships 表，第二个租户）。
    消费点全部在阈值与提示词层，**不进倍率**。
    """

    __ui_label__ = "人际关系"
    __ui_icon__ = "users"
    __ui_order__ = 20

    enabled: bool = Field(
        default=True,
        description="启用关系模型：对跟她说过话的人建档、按熟悉度调整主动开口阈值与语气",
        json_schema_extra={"label": "启用关系模型", "order": 0},
    )
    keep: int = Field(
        default=200,
        description="关系档案上限（超过按「最近互动最久远」淘汰）",
        json_schema_extra={"label": "档案上限", "order": 1},
    )
    decay_days: float = Field(
        default=7.0,
        description="超过这么久没互动，熟悉度开始按每周 -0.5 衰减；0 = 关闭衰减",
        json_schema_extra={"label": "衰减起点（天）", "order": 2},
    )


class InterruptConfig(PluginConfigBase):
    """``[interrupt]``：打断机制（v1.11.1）。

    收到**对她说的话**（私聊任意 / 群聊被 @）时，把主活动临时切到 ``chatting``
    若干分钟，回完再回到原活动——「吃饭吃到一半放下筷子回消息」这件事。
    只切活动与窗口，**零 RPC、零落盘**（落盘随下一次 tick），因此挂在消息主链上
    是安全的（另有 ``error_policy=SKIP`` 兜底）。

    ⚠ 全部消费点在**活动层**（``enforce`` 窗口内保住 ``chatting``、``request_is_pointless``
    窗口内跳过）。**不进倍率**：活动因子表里的 ``chatting=1.1`` 是既有机制
    （她正在回消息理应多说几句），不是这一层新加的乘法项。
    """

    __ui_label__ = "消息打断"
    __ui_icon__ = "zap"
    __ui_order__ = 21

    enabled: bool = Field(
        default=True,
        description="启用打断机制：收到私聊或被 @ 时临时切到「聊天中」，回完接着干原活动",
        json_schema_extra={"label": "启用打断机制", "order": 0},
    )
    window_minutes: int = Field(
        default=5,
        description=(
            "每次打断的窗口长度（分钟）：从收到消息算起，这段时间内她保持「聊天中」；"
            "连续发消息只顺延、不重新计时也不重复记录"
        ),
        json_schema_extra={"label": "打断窗口（分钟）", "order": 1},
    )
    inject_notice: bool = Field(
        default=True,
        description=(
            "打断时向该会话注入一条世界内事实（「她刚才在吃饭，看到你说话就放下手里的事」）"
            "；按会话去重，失败只降级"
        ),
        json_schema_extra={"label": "注入「放下手里的事」", "order": 2},
    )
    hold_over_sleep: bool = Field(
        default=True,
        description=(
            "窗口内**保住**「聊天中」不被硬约束切走（含「该睡了」）；"
            "关掉 = 回消息途中照样会被送去睡觉"
        ),
        json_schema_extra={"label": "窗口内保住聊天态", "order": 3},
    )


class DreamConfig(PluginConfigBase):
    """``[dream]``：梦境与睡眠余波（v1.12.0，方案八）。

    睡醒不一定清爽——有时候带着一个梦。梦是**廉价的真实感**：一行文本、
    一上午的余韵。触发点是「睡够 ≥ 3 小时后醒来」的那一刻，按概率生成；
    生成两级：短 LLM（昨日经历 + 内心状态做种子）→ 模板库兜底，**永不阻塞
    醒来流程**。

    ⚠ 梦只进**叙事层**：``recent_events``（下一轮活动决策的近层经历可见）与
    主动开口素材（「我昨晚梦到…」，上午全额、午后触底——下午还讲梦就奇怪了）。
    不进倍率、不进关系、不动情绪体力。
    """

    __ui_label__ = "梦境"
    __ui_icon__ = "moon"
    __ui_order__ = 22

    enabled: bool = Field(
        default=True,
        description="启用梦境：睡够 3 小时以上醒来时，按概率带着一个梦醒来",
        json_schema_extra={"label": "启用梦境", "order": 0},
    )
    probability: float = Field(
        default=0.35,
        description="每次（≥3 小时的）长睡眠醒来的做梦概率 0–1",
        json_schema_extra={"label": "做梦概率", "order": 1, "step": 0.05},
    )
    use_llm: bool = Field(
        default=True,
        description=(
            "用一次短模型调用生成梦的内容（预算约 50 token，失败自动落到内置模板库）；"
            "关掉则只用模板库，省一次调用"
        ),
        json_schema_extra={"label": "用模型生成梦", "order": 2},
    )


class MotivesConfig(PluginConfigBase):
    """``[motives]``：主动开口动机扩展（v1.13.0，方案步 9，默认开）。

    人不是「有素材才说话」——早上想道早安、做了好玩的事想分享、想起好久没聊的
    朋友想去问一句。这三类**动机素材**与节日/梦境素材走同一条管道
    （``state.materials`` → 主动开口挑选），发不发仍由既有管线裁决
    （阈值/间隔/静默时段/社交电量硬闸照旧）——动机不绕过任何闸，也不进倍率。
    """

    __ui_label__ = "开口动机"
    __ui_icon__ = "sparkles"
    __ui_order__ = 23

    enabled: bool = Field(
        default=True,
        description="启用动机素材：问候 / 生活分享 / 关系维护三类「想说话的念头」",
        json_schema_extra={"label": "启用动机素材", "order": 0},
    )
    greeting: bool = Field(
        default=True,
        description="问候类：早安（06:00–11:00）/ 晚安（22:00–01:00）时段各至多一条",
        json_schema_extra={"label": "问候类动机", "order": 1},
    )
    share: bool = Field(
        default=True,
        description="生活分享类：按当前活动低概率冒出「想说说」的念头（每活动每天至多一条）",
        json_schema_extra={"label": "分享类动机", "order": 2},
    )
    relation: bool = Field(
        default=True,
        description=(
            "关系维护类：对熟悉（≥认识档）且超过设定天数没说话的人，"
            "冒出「好久没聊了」的念头（需要关系模型在建档才有数据）"
        ),
        json_schema_extra={"label": "关系维护动机", "order": 3},
    )
    relation_idle_days: float = Field(
        default=3.0,
        description="超过这么多天没和某位熟人说话，就会想 TA（0 = 关闭关系维护）",
        json_schema_extra={"label": "想起熟人的天数", "order": 4, "step": 0.5},
    )


class PhysioConfig(PluginConfigBase):
    """``[physio]``：三餐与生理锚点（v1.9.1）。

    与 ``[routines]`` 的分工：习惯表是**用户手写的作息**（想让她 07:15 洗漱就写一行）；
    physio 是**生理本能**（到点会饿、晚上会想洗澡），只配时间窗与幅度，不配场景。
    两层都出 proposal、都走 enforce 唯一收口；同时命中时习惯先到先得（本 tick 只出
    一个 proposal），下一个 tick 轮到生理窗。
    """

    __ui_label__ = "三餐生理"
    __ui_icon__ = "utensils"
    __ui_order__ = 17

    enabled: bool = Field(
        default=True,
        description="启用生理锚点：饱腹结算 + 三餐/洗澡时间窗",
        json_schema_extra={"label": "启用生理锚点", "order": 0},
    )
    meals: list[str] = Field(
        default_factory=lambda: list(DEFAULT_PHYSIO_MEAL_LINES),
        description=(
            '生理窗，每行 "HH:MM-HH:MM|名称|类型|weight=.."；类型 meal=吃饭 / bath=洗澡 / '
            "snack=加餐（仅饿过头时出现）。默认三餐齐备"
        ),
        json_schema_extra={
            "label": "生理时间窗（每行一组）",
            "order": 1,
            "rows": 4,
            "placeholder": "07:00-08:30|早餐|meal|weight=0.9",
        },
    )
    satiety_decay_per_hour: float = Field(
        default=0.8,
        description="清醒时每小时饱腹下降（0–10）；睡着不消耗",
        json_schema_extra={"label": "饱腹下降速率/小时", "order": 2, "step": 0.1},
    )
    meal_duration_minutes: int = Field(
        default=40,
        description="一餐的最短停留（分钟）：吃饭窗命中后，她至少吃这么久",
        json_schema_extra={"label": "一餐最短停留（分钟）", "order": 3},
    )

    _norm_physio_lists = _str_list_validator("meals")


class RoutinesConfig(PluginConfigBase):
    """``[routines]``：习惯层——每天固定时间做固定的事（v1.9.0）。

    ⚠ ``lines`` **默认为空**：没有配任何习惯时，这一层完全不存在（行为与加它之前
    一模一样）。这是刻意的——习惯表会**压住模型**的决定权，默认塞几行示例等于
    悄悄改写所有存量实例的作息。想用就在配置页填行。
    """

    __ui_label__ = "生活习惯"
    __ui_icon__ = "clock"
    __ui_order__ = 16

    enabled: bool = Field(
        default=True,
        description="启用习惯层（配了 lines 才有实际作用）",
        json_schema_extra={"label": "启用习惯层", "order": 0},
    )
    lines: list[str] = Field(
        default_factory=list,
        description=(
            '习惯行，每行 "HH:MM-HH:MM|场景|活动|修饰符"。'
            '修饰符：weight=0-1（当日命中概率）、jitter=分钟（窗口平移幅度）、'
            'days=1-5（哪些星期生效，空=每天）、workday_only=true / holiday_only=true、'
            'physio=true（标记为三餐/洗澡，v1.9.x 的 physio 消费）'
        ),
        json_schema_extra={
            "label": "习惯（每行一组）",
            "order": 1,
            "rows": 4,
            "placeholder": "07:00-07:30|起床洗漱|daily|weight=1.0",
        },
    )
    jitter_minutes: int = Field(
        default=15,
        description=(
            "默认抖动幅度（分钟）：窗口整体前后平移这么多分钟，让她的作息不至于"
            "分秒不差。按**生活日**固定一次，不是每轮重掷"
        ),
        json_schema_extra={"label": "默认抖动（分钟）", "order": 2},
    )
    llm_gap_minutes: float = Field(
        default=120.0,
        description=(
            "习惯命中后至少隔这么久才再问一次模型（省调用、也让作息更稳）。"
            "模型的舞台是习惯之间的空隙，不是习惯窗口内"
        ),
        json_schema_extra={"label": "命中后的提问间隔（分钟）", "order": 3, "step": 10},
    )

    _norm_routine_lists = _str_list_validator("lines")


class MemoryConfig(PluginConfigBase):
    """``[memory]``：把 A_Memorix 的长期记忆只读接进来（v1.22.0，**默认关闭**）。

    接入点只有两个，都在「阈值与提示词层」，**不进倍率**：

    - **主动开口素材**（``in_materials``）：她「还记得这个人提过的事」，于是有个由头
      开口。素材带 ``target_user_id``，只在那个人的会话里可见。
    - **关系档案**（``in_relations``）：``/生活 关系`` 与状态卡里，熟人后面跟一行旧事。

    **刻意不接的两处**（都不是遗漏，写下来免得日后被当成 bug 改回去）：

    1. **活动提示词**：``LifeState.activity`` 是**全局单份**（一个人格、一份人生），
       而记忆是按人的。把「张三在备考」喂进全局活动生成，就会在她所有会话里生效——
       那是串人。要么活动按会话分开（架构级改动），要么不接，这里选后者。
    2. **回复摘要**（``_life_digest``）：它直接进回复 prompt，掺进别人的记忆既是隐私
       事故、也是出戏（"你上次不是说……"）。守卫测试见
       ``tests/test_memory_contract.py::test_memory_never_reaches_the_reply_digest``。

    默认关闭的理由与 ``[economy]`` / ``[world]`` 一致：老实例升级后行为必须逐字不变，
    要用才开。开之前请确认宿主已配置 A_Memorix（否则每次都是空结果，白花 RPC）。
    """

    __ui_label__ = "长期记忆（A_Memorix）"
    __ui_icon__ = "database"
    __ui_order__ = 26

    enabled: bool = Field(
        default=False,
        description=(
            "只读接入宿主长期记忆（knowledge.search → A_Memorix）。读不到/超时/结构损坏"
            "都只少这一次的记忆，不影响生活循环与频率"
        ),
        json_schema_extra={"label": "启用长期记忆", "order": 0},
    )
    in_materials: bool = Field(
        default=True,
        description="记忆参与主动开口：想到「TA 提过的事」时有一条开口由头（只在该人的会话里）",
        json_schema_extra={"label": "进主动开口素材", "order": 1},
    )
    in_relations: bool = Field(
        default=True,
        description="记忆进关系档案：/生活 关系 与状态卡里，熟人后面跟一行旧事",
        json_schema_extra={"label": "进关系档案", "order": 2},
    )
    ttl_hours: float = Field(
        default=6.0,
        description=(
            "同一个人的记忆最多隔这么久重查一次（懒刷新：到期后下一次真要用时才查，"
            "没有后台定时任务）"
        ),
        json_schema_extra={"label": "缓存时长（小时）", "order": 3, "step": 0.5},
    )
    search_limit: int = Field(
        default=5,
        description="每次检索取回多少条记忆（越大越容易捞到无关条目，也越慢）",
        json_schema_extra={"label": "单次检索条数", "order": 4},
    )
    per_person_limit: int = Field(
        default=2,
        description="同一个人最多生成几条开口素材（多了会像在翻旧账）",
        json_schema_extra={"label": "每人素材条数", "order": 5},
    )
    fact_max_chars: int = Field(
        default=60,
        description="单条事实的字符上限：只保留一句概括，不复述原话",
        json_schema_extra={"label": "单条字数上限", "order": 6},
    )
    material_weight: float = Field(
        default=0.6,
        description=(
            "记忆素材在主动开口评分里的权重。参考：问候/分享 0.5、关系维护 0.7——"
            "记忆是附带话题，不该盖过「真的有事想说」"
        ),
        json_schema_extra={"label": "素材权重", "order": 7, "step": 0.05},
    )
    material_ttl_hours: float = Field(
        default=3.0,
        description="记忆素材的有效期（小时）。素材不落盘，只影响一次巡检内的保鲜衰减",
        json_schema_extra={"label": "素材有效期（小时）", "order": 8, "step": 0.5},
    )
    min_familiarity: float = Field(
        default=20.0,
        description=(
            "只对熟悉度达到这个档位的人用记忆（20 = 「认识」档，与关系维护素材同一门槛）。"
            "对陌生人提旧事会很怪"
        ),
        json_schema_extra={"label": "最低熟悉度", "order": 9},
    )
    require_person_mention: bool = Field(
        default=False,
        description=(
            "严格模式：要求每条记忆里出现目标本人的昵称/QQ（默认关）。服务端已按 person_id "
            "圈定，再要求点名会丢掉「提过要考公」这类正确条目；只在怀疑串人时临时打开"
        ),
        json_schema_extra={"label": "严格模式（要求点名）", "order": 10},
    )


class CatchupConfig(PluginConfigBase):
    """``[catchup]``：睡醒补答（v1.23.0）。主动补答在自然醒时触发 Planner，被动回填注入她下一次回复；配额与 ``[proactive]`` 分开计——补答回答的是「有人断在半截等你」。"""

    __ui_label__ = "睡醒补答"; __ui_icon__ = "moon"; __ui_order__ = 27
    enabled: bool = Field(default=True, description="记录她睡着时（私聊 / 群聊被 @）进来的消息并在醒来补答；关 = 回到「睡就是睡」", json_schema_extra={"label": "启用补答", "order": 0})
    active_enabled: bool = Field(default=False, description="自然醒时**主动**触发 Planner（原文进任务描述，措辞与是否开口由宿主决定）。默认关 = 只做被动回填，先确认真机效果再打开", json_schema_extra={"label": "醒来主动补答", "order": 1})
    per_wake_max: int = Field(default=5, description="一次睡醒最多主动补几个会话", json_schema_extra={"label": "单次睡醒上限", "order": 2, "step": 1})
    per_person_daily: int = Field(default=1, description="同一个人每天最多被主动补一次；0 与 1 同义", json_schema_extra={"label": "每人每日上限", "order": 3, "step": 1})
    global_daily_max: int = Field(default=6, description="补答自己的每日总上限（与主动开口分开计）；0 = 不设上限。默认 6 ≈ 两次睡醒 × 3 个会话", json_schema_extra={"label": "补答每日上限", "order": 4, "step": 1})
    late_degrade_hours: float = Field(default=6.0, description="迟到超过它（小时）不再主动补，只留被动回填；0 = 不设上限。隔一整天再提就很奇怪了", json_schema_extra={"label": "迟到降级（小时）", "order": 5, "step": 1})
    keep_per_person: int = Field(default=5, description="每人最多保留的原文条数；0 = 全留", json_schema_extra={"label": "每人保留条数", "order": 6, "step": 1})
    max_chars: int = Field(default=200, description="单条原文的截断长度；0 = 不截断", json_schema_extra={"label": "单条截断（字符）", "order": 7, "step": 50})
    retention_hours: float = Field(default=24.0, description="条目保留时长（小时），超过即删；它是「重启后补上个星期留言」的唯一兜底", json_schema_extra={"label": "条目保留（小时）", "order": 8, "step": 1})
    requote_limit: int = Field(default=0, description="塞给 Planner / 回填时最多引几条原文。**默认 0 = 不复述**：那些消息本来就在宿主的对话历史里，再说一遍会让同一句话出现两次；调大只用于「历史被裁剪」的兜底", json_schema_extra={"label": "引用原文条数", "order": 9, "step": 1})
    stagger_max_seconds: int = Field(default=120, description="多个会话在同一次睡醒命中时按它随机散开（秒），避免同刻给宿主丢多个主动任务；0 = 不散开", json_schema_extra={"label": "触发错开（秒）", "order": 10, "step": 20})
    persona_chars: int = Field(default=200, description="把多少人设摘要塞进补答的任务描述（0 = 不塞）；只影响口吻，不替代宿主的完整人设", json_schema_extra={"label": "人设摘要（字符）", "order": 11, "step": 50})

class LifeFrequencyConfig(PluginConfigBase):
    """插件配置根模型。

    ⚠ 所有子类必须定义在根模型**之前**：``default_factory=X`` 是在类体执行时求值的。
    """

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    simulation: SimulationConfig = Field(default_factory=SimulationConfig)
    activity: ActivityConfig = Field(default_factory=ActivityConfig)
    emotion_energy: EmotionEnergyConfig = Field(default_factory=EmotionEnergyConfig)
    frequency: FrequencyConfig = Field(default_factory=FrequencyConfig)
    health: HealthConfig = Field(default_factory=HealthConfig)
    date: DateConfig = Field(default_factory=DateConfig)
    events: EventsConfig = Field(default_factory=EventsConfig)
    apply: ApplyConfig = Field(default_factory=ApplyConfig)
    proactive: ProactiveConfigModel = Field(default_factory=ProactiveConfigModel)
    prompt: PromptConfig = Field(default_factory=PromptConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    economy: EconomyConfig = Field(default_factory=EconomyConfig)
    social: SocialConfig = Field(default_factory=SocialConfig)
    world: WorldConfig = Field(default_factory=WorldConfig)
    routines: RoutinesConfig = Field(default_factory=RoutinesConfig)
    physio: PhysioConfig = Field(default_factory=PhysioConfig)
    calendar: CalendarConfig = Field(default_factory=CalendarConfig)
    mood: MoodConfig = Field(default_factory=MoodConfig)
    relations: RelationsConfig = Field(default_factory=RelationsConfig)
    interrupt: InterruptConfig = Field(default_factory=InterruptConfig)
    dream: DreamConfig = Field(default_factory=DreamConfig)
    motives: MotivesConfig = Field(default_factory=MotivesConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    catchup: CatchupConfig = Field(default_factory=CatchupConfig)
    # ⚠ 必须是**顶层**节：SDK 只在顶层把 "是配置模型类" 的字段展开成 section
    # （`maibot_sdk/config.py:209-227`），嵌在别的节里的配置对象会退化成
    # `type=object` 的普通字段、不带 `properties`，WebUI 只能把它渲染成
    # `[object Object]` 的文本框（`[activity.llm]`、`[emotion_energy.curves]` 就是这样）。
    schedule: ScheduleConfigModel = Field(default_factory=ScheduleConfigModel)

# ---------------------------------------------------------------- WebUI Schema 修正

#: 会把嵌套配置提升成「点号路径 section」的原因见 ``_promote_nested_config_sections``
_WEBUI_SHIM_NAME = "_LifeFrequencyWebuiShim"


def _field_annotation_model(config_class: Any, field_name: str) -> Any:
    """取字段注解里嵌套的配置模型类；不是配置模型就返回 ``None``。"""

    info = getattr(config_class, "model_fields", {}).get(field_name)
    annotation = getattr(info, "annotation", None)
    if isinstance(annotation, type) and issubclass(annotation, PluginConfigBase):
        return annotation
    return None


def _dotted_section_for(
    nested_class: Any,
    *,
    path: str,
    title: str,
    description: str,
    order: float,
) -> list[dict[str, Any]]:
    """为一个嵌套配置类生成「点号路径 section」，并递归处理它内部的嵌套。

    借 SDK 自己的 ``generate_plugin_config_schema`` 生成字段元数据（标签/顺序/选择项/
    列表项），所以不会与主配置页的展示风格分叉；我们只改 section 名与标题。

    返回列表：自己的 section 在前，递归出来的更深层 section 在后。
    """

    leaf = path.rsplit(".", 1)[-1]
    shim = create_model(  # type: ignore[call-overload] —— pydantic 的动态模型
        _WEBUI_SHIM_NAME,
        __base__=PluginConfigBase,
        **{leaf: (nested_class, Field(default_factory=nested_class))},
    )
    sections = generate_plugin_config_schema(shim).get("sections") or {}
    section = sections.get(leaf)
    if not isinstance(section, dict):
        return []

    section["name"] = path              # ← WebUI 按 section 名当点号路径读写
    section["title"] = title
    section["description"] = description
    section["order"] = order
    section["collapsed"] = False
    section.pop("icon", None)

    extra: list[dict[str, Any]] = []
    removed = 0
    fields = section.get("fields")
    if isinstance(fields, dict):
        for field_name in list(fields):
            field = fields.get(field_name) or {}
            if field.get("type") != "object":
                continue
            deeper_class = _field_annotation_model(nested_class, field_name)
            if deeper_class is None:
                continue
            deeper_path = f"{path}.{field_name}"
            label = str(field.get("label") or field_name)
            del fields[field_name]
            removed += 1
            extra.extend(
                _dotted_section_for(
                    deeper_class,
                    path=deeper_path,
                    title=label if label != field_name else f"[{deeper_path}]",
                    description=str(field.get("description") or f"[{deeper_path}]"),
                    order=order + 0.25,
                )
            )
    if removed and not fields:
        # 中间层被掏空（例如 [emotion_energy.curves] 只剩两个嵌套对象）：
        # 留着它只会在页面上多一张空卡片
        return extra
    return [section, *extra]


def _promote_nested_config_sections(schema: dict[str, Any], root_class: Any) -> dict[str, Any]:
    """把 SDK schema 里 ``type=object`` 的字段提升成独立的「点号路径 section」。

    为什么必须做：SDK 只在**顶层**把配置模型展开成 section
    （``maibot_sdk/config.py:209-227``）；``_build_section_schema``（同文件 :274-280）
    对节内字段不做这个判断，一律当普通字段 → ``type=object``、``ui_type=json``、
    **不带 properties**。而 WebUI 的插件配置页按 ``ui_type`` 选控件
    （``dashboard/src/routes/plugin-config.tsx:163-346``），分支里**没有 json**，
    于是落到 ``default`` 当文本框渲染 → 值被字符串化成 ``[object Object]``，
    用户根本改不了（``[activity.llm]``、``[emotion_energy.curves]`` 一直如此）。

    修法：把 ``activity.llm`` 变成 section 名即可——WebUI 读写都是按
    **section 名当点号路径**（``dashboard/src/routes/plugin-config/utils.ts:25-76``：
    ``getNestedRecord(config, "activity.llm")`` / ``setNestedField(..., "temperature", v)``），
    于是 ``config.toml`` 的**键路径一个都不用改**，旧配置原样继续生效。
    """

    sections = schema.get("sections")
    if not isinstance(sections, dict) or root_class is None:
        return schema

    promoted: dict[str, dict[str, Any]] = {}
    emptied: list[str] = []
    for section_name, section in list(sections.items()):
        if not isinstance(section, dict):
            continue
        fields = section.get("fields")
        if not isinstance(fields, dict):
            continue
        base_order = float(section.get("order") or 0)
        removed = 0
        for field_name in list(fields):
            field = fields.get(field_name) or {}
            if field.get("type") != "object":
                continue
            nested_class = _resolve_nested_class(root_class, section_name, field_name)
            if nested_class is None:
                continue
            path = f"{section_name}.{field_name}"
            label = str(field.get("label") or field_name)
            del fields[field_name]
            removed += 1
            for extra in _dotted_section_for(
                nested_class,
                path=path,
                title=label if label != field_name else f"[{path}]",
                description=str(field.get("description") or f"[{path}]"),
                order=base_order + 0.5,
            ):
                promoted[extra["name"]] = extra
        if removed and not fields:
            # 被掏空的中间层（例如 [emotion_energy.curves] 只剩两个嵌套对象）
            # 不该在页面上留一张空卡片
            emptied.append(section_name)

    for name in emptied:
        sections.pop(name, None)
    if promoted:
        sections.update(promoted)
    return schema


def _resolve_nested_class(root_class: Any, section_path: str, field_name: str) -> Any:
    """沿 ``section_path``（可能是点号路径）定位配置模型类，再取字段的嵌套模型。"""

    current = root_class
    for part in str(section_path or "").split("."):
        if not part:
            continue
        nested = _field_annotation_model(current, part)
        if nested is None:
            return None
        current = nested
    return _field_annotation_model(current, field_name)


#: 默认展开的「常用节」（v1.21.0）——装完必须过一遍的，或者最常改的。
#: 顺序即页面顺序；不在这个元组里的节一律折叠。
#:
#: 为什么改成白名单：v1.20.1 的配置页有 **28 张卡片 / 251 个字段**默认全开，
#: 用户的第一反应是「不知道从哪下手」。白名单把默认视图压到 7 节 / 35 字段，
#: 其余各节收起但**标题与「什么时候需要动」说明仍然可见**——需要调的人点得开，
#: 不需要调的人不会被淹没。这里只改展示元数据，配置键一个都不动。
_WEBUI_PRIMARY_SECTIONS: tuple[str, ...] = (
    "plugin",     # 插件总开关
    "apply",      # 倍率写到哪些会话
    "security",   # 谁能改她的状态
    "activity",   # 作息活动（权重最大的一维）
    "frequency",  # 倍率钳制与硬闸
    "prompt",     # 生活摘要是否注入提示词
    "date",       # 生日与自定义节日
)

#: 折叠节的「什么时候需要动」说明（v1.21.0）。折叠后标题与这段文字仍然可见，
#: 所以它要回答的是「我要不要点开」，而不是「这节是什么」。
_WEBUI_SECTION_WHEN = {
    "simulation": "第一次装时装一次就够：时区、tick 间隔、冷启动预算。之后基本不用回来。",
    "activity.llm": "她觉得活动生成太慢、太贵、或者老失败时再调：模型名、温度、超时、冷却。",
    "emotion_energy": "想改情绪体力的手感（涨落快慢、惯性、下限）时才动。默认值已按真机标定。",
    "emotion_energy.curves.frequency": "只在你想让「按次数触发」模式的宿主也带点随机性时动。",
    "emotion_energy.curves.necessity": "只在你想调「按评分触发」模式的评分门位置时动。",
    "emotion_energy.curves.dynamic": "只在你想调「动态触发」模式的曲线时动。",
    "health": "感冒与熬夜的判定尺度（多晚算熬夜、病多久好、体力上限）不合口味时调。",
    "events": "要加自己的事件（比如「今天被老板骂了」）或屏蔽内置事件时用。",
    "proactive": "想让她**主动找话说**时才打开。默认关闭——开了会真的发消息。",
    "economy": "装了 budget-pacer 并想让她的「经济」维度接真实预算时才配。",
    "social": "装了日记类插件、想让她的「最近经历」来自日记时才配。",
    "world": "装了 bilibili / 入群欢迎 / 点歌类插件、想让她知道「外面的世界」时才配。",
    "routines": "想加「每天 8:30 通勤」「每天 22:00 洗漱」这类固定习惯时用。",
    "physio": "想让她按三餐时间饿、按作息犯困时才调窗口。",
    "calendar": "想让她过法定节假日与调休（需要中国日历数据源）时才开。",
    "mood": "想改压力 / 孤独 / 社交电量这三条内心曲线的涨落速度时调。默认值已标定。",
    "relations": "想调「越熟的人越容易开口」的力度时才动。",
    "interrupt": "她被 @ 或私聊时要不要临时切成「聊天中」——不合口味时调。",
    "dream": "想让她睡醒后带一点梦的余波时才开。",
    "motives": "主动开口的动机配比（问候 / 生活分享 / 关系维护）要改时调。",
    "memory": (
        "宿主已配 A_Memorix 长期记忆、想让她「记得这个人提过的事」时才打开。"
        "默认关闭——开着但宿主没记忆库，等于每次巡检白查一遍。"
    ),
    "schedule": "有固定班表（上下班时间、通勤、午休、周末双休）时才打开。",
    "catchup": "想让她「睡醒记得别人找过她」时才动：默认只做被动回填，要她主动开口再打开主动补答。",
}


def _apply_webui_display_polish(schema: dict[str, Any]) -> dict[str, Any]:
    """WebUI 可视化模式的显示层补丁（只改展示元数据，不碰任何配置键）。

    为什么要把 description 抄进 hint：可视化模式的 ``FieldRenderer``
    （``dashboard/src/routes/plugin-config.tsx:163-361``）按 ``ui_type``
    渲染控件时**只输出 label / hint / placeholder，从不渲染 description**——
    而本插件全部字段的填法说明都写在 description 里，不搬进 hint，
    用户在配置页上一个字都看不到（源代码模式才见得到）。

    v1.21.0 起这里多做两件事（都只作用于展示层）：

    1. **常用/高级分组**：``_WEBUI_PRIMARY_SECTIONS`` 里的 7 节展开并排在最前
       （order 0–6），其余各节折叠并整体后移（order +100）。默认视图因此从
       28 张卡片 / 251 字段降到 7 张 / 35 字段。
    2. **「什么时候需要动」写进折叠节的说明**：折叠后标题与说明仍然可见，
       用户不必点开就知道这节跟自己有没有关系。
    """

    primary_index = {name: i for i, name in enumerate(_WEBUI_PRIMARY_SECTIONS)}
    for name, section in (schema.get("sections") or {}).items():
        if not isinstance(section, dict):
            continue

        index = primary_index.get(name)
        if index is None:
            section["collapsed"] = True
            # 后移到常用节之后；保留节间相对顺序（含点号路径节 2.5 / 3.75 这类小数）
            section["order"] = 100.0 + float(section.get("order") or 0)
            when = _WEBUI_SECTION_WHEN.get(name)
            if when:
                existing = str(section.get("description") or "").strip()
                section["description"] = f"{existing}\n\n【什么时候需要动】{when}".strip()
        else:
            section["collapsed"] = False
            section["order"] = float(index)

        for field in (section.get("fields") or {}).values():
            if isinstance(field, dict) and not field.get("hint") and field.get("description"):
                field["hint"] = field["description"]
    return schema
