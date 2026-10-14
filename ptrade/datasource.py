"""ptrade.datasource —— akshare 适配 + 缓存 + 离线夹具（INTERFACES.md v3 §4）。

设计要点
--------
* **离线绝不联网**：``offline=True`` 时只读 ``<root>/data/cache`` 与 ``<root>/data/fixture``，
  缺失即抛 :class:`DataUnavailable`。akshare 是**惰性导入**的，离线路径连 import 都不会发生
  （:meth:`DataSource._ak` 在离线模式下直接抛错，从机制上杜绝"静默联网"）。
* **缓存**：``<root>/data/cache/<key>.json``，格式 ``{"fetched_at": iso, "payload": ...}``；
  TTL 见 §4 表。夹具文件使用**同一套信封格式**，因此离线读取路径与缓存完全一致。
* **网络优先 + 缓存兜底**：在线时先查新鲜缓存，否则联网；联网失败回退到过期缓存；
  再失败抛 :class:`DataUnavailable`。
* **ETF/LOF 支持**：``5xxxxx`` / ``1xxxxx`` 等代码不做股票前缀白名单拦截（用户 K 线图需求）。
* ``daily(..., unadjust=True)``：返回**不复权**真实价格 + 真实成交额（K 线图用），
  ``adjust="qfq"``（默认）语义不变。
"""

from __future__ import annotations

import json
import math
import os
import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable

import pandas as pd

from . import store
from .config import AppConfig

_log = logging.getLogger(__name__)

__all__ = ["DataSource", "DataUnavailable", "NetworkForbiddenInLock"]

# ---- TTL（秒） ------------------------------------------------------------- #
TTL_TRADE_DATES = 24 * 3600
TTL_DAILY = 6 * 3600
TTL_SPOT = 60
TTL_MARGIN = 24 * 3600
TTL_FUNDAMENTAL = 24 * 3600
TTL_HOLDERS = 24 * 3600
TTL_INDEX = 6 * 3600

# ---- 统一列（契约） --------------------------------------------------------- #
DAILY_COLUMNS = ["date", "open", "high", "low", "close", "volume", "amount", "pct_chg", "turnover"]
SPOT_COLUMNS = [
    "code", "name", "last", "pct_chg", "open", "high", "low", "prev_close",
    "volume", "amount", "turnover", "pe_ttm", "pb", "total_mv", "float_mv",
    "limit_up", "limit_down",
]
MARGIN_COLUMNS = ["code", "name", "can_finance", "can_short", "limit_pct"]
QUARTER_COLUMNS = [
    "code", "name", "eps", "revenue", "revenue_yoy", "revenue_qoq",
    "net_profit", "net_profit_yoy", "net_profit_qoq", "bps", "roe",
    "ocf_ps", "gross_margin", "industry", "announce_date",
]
FIN_COLUMNS = [
    "date", "eps", "eps_deducted", "bps", "roe", "roe_weighted",
    "revenue_yoy", "net_profit_yoy", "gross_margin", "debt_ratio", "ocf_ps",
]
HOLDERS_COLUMNS = [
    "code", "name", "holders", "holders_prev", "holders_chg_pct",
    "avg_hold_value", "total_mv", "total_share",
]
TOPH_COLUMNS = [
    "code", "holder_name", "holder_type", "shares", "ratio", "change",
    "report_date", "announce_date",
]
INDEX_COLUMNS = ["date", "open", "high", "low", "close", "volume", "pct_chg"]

_STR_COLS = {
    "date", "code", "name", "industry", "announce_date", "report_date", "limit_pct",
    "holder_name", "holder_type",
}

# 剧情标的（夹具必须覆盖）与夹具比对基准池（供 L 因子行业内排名使用）
STORY_CODES = ["600519", "002594", "000001", "601398"]
UNIVERSE_CODES = [
    "600519", "000858", "000568", "002304",      # 酿酒
    "000001", "601398", "600036", "601288",      # 银行
    "002594", "600104", "601633", "000625",      # 汽车
    "600276", "300760",                          # 医药
    "002415", "603501", "300750", "002475",      # 电子/电力设备
    "601318", "601601",                          # 保险
    "600030", "601211",                          # 证券
    "000333", "600690",                          # 家电
    "600887", "000002", "601668", "601857",      # 食品/地产/建筑/石油
    "600110",                                    # 沪市两融**融资标的但融券余量=0** → can_short=False 负例
    "588000", "510050", "159915",                # ETF（K 线图需求，用户截图场景）
]

_INDEX_ALIASES = {
    "000001": "sh000001", "000300": "sh000300", "000905": "sh000905",
    "000016": "sh000016", "000688": "sh000688",
    "399001": "sz399001", "399006": "sz399006", "399005": "sz399005",
}


class DataUnavailable(RuntimeError):
    """数据不可用（离线缺夹具 / 联网失败且无缓存）。"""


class NetworkForbiddenInLock(DataUnavailable):
    """**写锁临界区内试图联网**（INV-42）。

    为什么需要**独立类型**（独立复核第二轮指出）：`DataUnavailable` 的语义是
    「这份数据暂时取不到」，各调用点的 `except Exception` 会**回退过期缓存**或**降级兜底**
    —— 那会把「锁内禁网」这个**编码错误**静默吞掉：

    * `_cached` / `daily` / `index_daily`：有过期缓存时静默返回**陈旧数据**（无异常），
      无缓存时才响亮报错 —— 等于「违规是否响亮」取决于缓存状态；
    * `engine._float_shares`：`except Exception: return 0.0` → `source_limit=0`
      → `0 < source_limit` 不成立 → **券源上限检查被静默跳过**（风控静默失效）。

    所以守卫错误必须**穿透过期缓存兜底与降级兜底**，一路上抛到写命令边界，
    让开发者立刻看见「这里有漏网取数」，而不是让用户拿到陈旧数据或被取消的风控。
    """


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _norm_code(code: str) -> str:
    """去掉 sh/sz/bj 前缀，统一小写 6 位代码（ETF 一视同仁）。"""
    c = str(code).strip().lower()
    for pre in ("sh", "sz", "bj", "csi"):
        if c.startswith(pre):
            c = c[len(pre):]
    return c


def _prefixed(code: str) -> str:
    """新浪风格前缀代码：sh600519 / sz000001 / sh588000 / sz159915。"""
    c = _norm_code(code)
    if c.startswith(("5", "6", "9")):
        return "sh" + c
    if c.startswith(("0", "1", "2", "3")):
        return "sz" + c
    if c.startswith(("4", "8")):
        return "bj" + c
    return "sh" + c


def _normalize_index_code(code: str) -> str:
    """指数代码归一：'000001'→'sh000001'，'csi931151'→'csi931151'。"""
    raw = str(code).strip().lower()
    if raw.startswith("csi"):
        return raw
    if raw.startswith(("sh", "sz", "bj")):
        return raw
    c = _norm_code(raw)
    if c in _INDEX_ALIASES:
        return _INDEX_ALIASES[c]
    if c.startswith(("000", "999", "88", "95")):
        return "sh" + c
    if c.startswith(("399", "159")):
        return "sz" + c
    return "sh" + c


def price_limit_pct(code: str, name: str = "") -> float:
    """涨跌幅限制（小数）。ST 5%；创业板/科创板 20%；北交所 30%；其余 10%。"""
    c = _norm_code(code)
    if "ST" in str(name).upper():
        return 0.05
    if c.startswith(("300", "301", "688", "689")):
        return 0.20
    if c.startswith(("4", "8")):
        return 0.30
    return 0.10


def _round2(value: float) -> float:
    """A 股价格四舍五入到 2 位（half-up，避免 Python banker's rounding）。"""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return float("nan")
    return math.floor(abs(value) * 100 + 0.5) / 100 * (1 if value >= 0 else -1)


def _py(value: Any) -> Any:
    """numpy/NaN → 原生 Python 值（JSON 可序列化）。"""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    item = getattr(value, "item", None)
    if callable(item) and not isinstance(value, (str, bytes)):
        try:
            value = item()
        except Exception:  # pragma: no cover - 防御
            pass
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return str(value)[:10]
    return value


def _records(df: pd.DataFrame | None, columns: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """DataFrame → JSON 可序列化 records（NaN → None）。"""
    if df is None or len(df) == 0:
        return []
    if columns is not None:
        for col in columns:
            if col not in df.columns:
                df[col] = None
        df = df[list(columns)]
    return [{str(k): _py(v) for k, v in rec.items()} for rec in df.to_dict("records")]


def _pick(df: pd.DataFrame, *names: str) -> pd.Series | None:
    """按候选列名取列（兼容不同 akshare 版本的列名漂移）。"""
    for name in names:
        if name in df.columns:
            return df[name]
    return None


def _bool_cn(series: pd.Series | None) -> pd.Series:
    """'是'/'否'/True/1 → bool。"""
    if series is None:
        return pd.Series(dtype=bool)
    text = series.astype(str).str.strip()
    return text.isin(["是", "True", "true", "1", "1.0", "Y", "y"])


# --------------------------------------------------------------------------- #
# DataSource
# --------------------------------------------------------------------------- #
class DataSource:
    """akshare 数据源 + 本地缓存 + 离线夹具。"""

    def __init__(self, cfg: AppConfig, offline: bool = False, scenario: bool = False):
        """``scenario=True`` 时在夹具之前叠加 ``<root>/data/scenario``（**人为构造的剧本数据**）。

        剧本目录只用于单元测试标定：``--offline`` 的 CLI 路径**不会**读它（默认为 ``False``）。
        scenario 缺失的文件自动回落 ``data/fixture``。
        """
        self.cfg = cfg
        self.offline = bool(offline)
        self.scenario = bool(scenario)
        self.root = cfg.root
        self.cache_dir = os.path.join(self.root, "data", "cache")
        self.fixture_dir = os.path.join(self.root, "data", "fixture")
        self.scenario_dir = os.path.join(self.root, "data", "scenario")
        self.fixture_dirs = (
            [self.scenario_dir, self.fixture_dir] if self.scenario else [self.fixture_dir]
        )
        os.makedirs(self.cache_dir, exist_ok=True)
        if not self.offline:
            os.makedirs(self.fixture_dir, exist_ok=True)
        self._ak_module: Any = None

    # ---- 夹具读取（支持 scenario 叠加） ---- #
    def _read_fixture(self, key: str) -> dict[str, Any] | None:
        for directory in self.fixture_dirs:
            entry = self._read_entry(directory, key)
            if entry is not None:
                return entry
        return None

    # ---- akshare 惰性加载（离线路径绝不 import） ---- #
    def _ak(self) -> Any:
        if self.offline:
            raise DataUnavailable(
                "offline=True：禁止访问 akshare（离线模式只读 data/cache 与 data/fixture）"
            )
        # **锁内禁网**（架构评审 S-1 第 2 条；2026-10-13 落地）。
        #
        # BLOCK-1 的「锁外预热」只把**已知**的取数挪出了临界区；万一还有漏网的取数，
        # 它仍会在持有写锁的情况下联网 —— 那是 **fail-slow**：写者互相饿死、
        # 网页端返回 503，而账户本身毫无问题，排查起来极其困难。
        # 这里把它变成 **fail-fast**：在锁内一旦真的要联网，就**响亮报错**并指明修法。
        #
        # 为什么判据是「进 _ak()」而不是「进 _cached()」：
        # `_cached` 的新鲜缓存命中 / 离线夹具读都**不需要** akshare，
        # 只有真正要联网时才走到这里 —— 所以这个位置既严格又不误伤缓存命中。
        if store.in_critical_section():
            raise NetworkForbiddenInLock(
                "写锁临界区内禁止联网取数：请把该数据的获取移到锁外"
                "（用 service.prewarm_before_write 预热），或改用 --offline。"
                "详见 INTERFACES.md INV-42。"
            )
        if self._ak_module is None:
            import akshare as ak  # 惰性导入

            self._ak_module = ak
        return self._ak_module

    def _call(self, fn: Callable[..., Any], *args: Any, retries: int = 2, **kwargs: Any) -> Any:
        """节流 0.3s + 失败重试（退避 1s / 3s）。``retries=0`` 用于"快速失败再走兜底源"。"""
        last: Exception | None = None
        for attempt in range(retries + 1):
            if attempt == 1:
                time.sleep(1.0)
            elif attempt >= 2:
                time.sleep(3.0)
            time.sleep(0.3)
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - 网络/解析异常统一重试
                last = exc
        raise DataUnavailable(f"akshare 接口 {getattr(fn, '__name__', fn)} 失败: {last}") from last

    # ---- 缓存 / 夹具读写（同一信封格式） ---- #
    @staticmethod
    def _read_entry(directory: str, key: str) -> dict[str, Any] | None:
        path = os.path.join(directory, f"{key}.json")
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError):
            return None
        if isinstance(payload, dict) and "payload" in payload:
            return payload
        return {"fetched_at": None, "payload": payload}

    @staticmethod
    def _write_entry(directory: str, key: str, payload: Any) -> str:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"{key}.json")
        entry = {"fetched_at": datetime.now(timezone.utc).isoformat(), "payload": payload}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(entry, fh, ensure_ascii=False)
        return path

    @staticmethod
    def _expired(entry: dict[str, Any], ttl: float) -> bool:
        stamp = entry.get("fetched_at")
        if not stamp:
            return True
        try:
            fetched = datetime.fromisoformat(str(stamp))
        except ValueError:
            return True
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - fetched).total_seconds()
        if ttl == TTL_DAILY and age < TTL_DAILY:
            # 收盘后（15:30）当日数据失效
            now = datetime.now()
            if now.hour * 60 + now.minute >= 15 * 60 + 30:
                try:
                    fetched_local = fetched.astimezone()
                    if fetched_local.date() < now.date():
                        return True
                except Exception:  # pragma: no cover - 防御
                    pass
        return age > ttl

    def _cached(
        self,
        key: str,
        ttl: float,
        fetch: Callable[[], Any],
        fixture_key: str | None = None,
    ) -> Any:
        """新鲜缓存 → 联网 → 过期缓存兜底；离线：新鲜缓存 → 夹具 → 过期缓存 → 抛错。"""
        cached = self._read_entry(self.cache_dir, key)
        if cached is not None and not self._expired(cached, ttl):
            return cached["payload"]
        fixture_name = fixture_key or key
        if self.offline:
            fixture = self._read_fixture( fixture_name)
            if fixture is not None:
                return fixture["payload"]
            if cached is not None:
                return cached["payload"]  # 离线只能用过期缓存
            raise DataUnavailable(
                f"离线模式缺少数据 {key}（cache/fixture 均无；fixture 期望 {fixture_name}.json）"
            )
        try:
            payload = fetch()
        except NetworkForbiddenInLock:
            # **守卫错误必须穿透**（INV-42）：若在这里回退过期缓存，「锁内禁网」就会被静默吞掉 ——
            # 违规是否响亮会取决于缓存状态（有过期缓存就静默返回陈旧数据）。独立复核实测：同一锁内调用
            # 有过期缓存 → 静默返回 5572 行陈旧数据、无异常；无缓存 → 才响亮报错。
            raise
        except Exception as exc:  # noqa: BLE001
            if cached is not None:
                return cached["payload"]
            raise DataUnavailable(str(exc)) from exc
        self._write_entry(self.cache_dir, key, payload)
        return payload

    @staticmethod
    def _frame(payload: Any, columns: list[str]) -> pd.DataFrame:
        """payload（records）→ 统一列的 DataFrame（数值列强制 numeric）。"""
        rows = payload.get("rows") if isinstance(payload, dict) and "rows" in payload else payload
        if not isinstance(rows, list):
            rows = []
        frame = pd.DataFrame(rows, columns=columns)
        for col in columns:
            if col not in _STR_COLS:
                frame[col] = pd.to_numeric(frame[col], errors="coerce")
        return frame

    # ---- 日期工具 ---- #
    @staticmethod
    def _today() -> str:
        return date.today().isoformat()

    @staticmethod
    def _offset(d: str, days: int) -> str:
        try:
            base = datetime.strptime(d, "%Y-%m-%d").date()
        except ValueError:
            base = date.today()
        return (base + timedelta(days=days)).isoformat()

    # ------------------------------------------------------------------ #
    # 交易日历
    # ------------------------------------------------------------------ #
    def trade_dates(self) -> list[str]:
        """升序交易日列表 ``YYYY-MM-DD``。缓存 24h。"""
        payload = self._cached("trade_dates", TTL_TRADE_DATES, self._fetch_trade_dates)
        if isinstance(payload, dict):
            payload = payload.get("rows") or payload.get("trade_dates") or []
        return sorted({str(x)[:10] for x in payload})

    def _fetch_trade_dates(self) -> list[str]:
        ak = self._ak()
        frame = self._call(ak.tool_trade_date_hist_sina)
        series = _pick(frame, "trade_date", "日期")
        if series is None:
            return []
        return sorted({str(x)[:10] for x in series.tolist()})

    def is_trade_date(self, d: str) -> bool:
        return str(d)[:10] in set(self.trade_dates())

    def next_trade_date(self, d: str) -> str:
        """严格大于 ``d`` 的最近交易日。"""
        day = str(d)[:10]
        for item in self.trade_dates():
            if item > day:
                return item
        raise DataUnavailable(f"交易日历中 {day} 之后没有交易日")

    def prev_trade_date(self, d: str) -> str:
        """严格小于 ``d`` 的最近交易日。"""
        day = str(d)[:10]
        found = ""
        for item in self.trade_dates():
            if item < day:
                found = item
            else:
                break
        if not found:
            raise DataUnavailable(f"交易日历中 {day} 之前没有交易日")
        return found

    def latest_trade_date(self) -> str:
        """最近一个**已收盘**的交易日。

        * sina 日历含未来日期 → 先按今天裁剪；
        * 若今天本身是交易日但当前时间 < 15:30（尚未收盘）→ 回退到上一交易日，
          避免用未收盘当日数据出信号（v3.1 未来函数防护）。
        """
        dates = self.trade_dates()
        if not dates:
            return self._today()
        today = self._today()
        past = [d for d in dates if d <= today]
        if not past:
            return dates[-1]
        latest = past[-1]
        now = datetime.now()
        if latest == today and (now.hour * 60 + now.minute) < 15 * 60 + 30:
            earlier = [d for d in past if d < today]
            if earlier:
                return earlier[-1]
        return latest

    def latest_trade_asof(
        self, probe_codes: "list[str] | None" = None, *, max_back: int = 30
    ) -> str:
        """最近一个**既有交易日历、又有实际行情**的交易日（asof 的安全默认值）。

        **为什么需要它**（独立复核第二轮 N4，且这是**生产**缺陷不只是测试问题）：
        ``latest_trade_date()`` 只读交易日历 —— 日历可以覆盖到未来（sina 日历到 2026-12-31），
        而**行情数据只到已收盘的最近一天**。把日历日期当 asof 会构造出一个
        ``asof-60d ~ asof`` 里**一根日线都没有**的窗口：

        * 自选池打分：``_has_market_data`` 判「无数据」→ **整个池子被报成无数据**
          （**已更正**：第三轮独立复核用真实 `WebApp.dispatch` 跑 7 个时钟证明
          `/api/watchlist/score` **不走** `score_watchlist`，该端点全部
          `200 / no_data=0 / 真实分数` —— 所以「生产端点全池 no_data」这句**不成立**，
          受影响的是调用 `score_watchlist` 的路径（CLI/测试）与**缺省 asof 的日期选择**本身）；
        * 测试夹具：一批无关用例因 ``SUSPENDED`` 集体变红（实测 8 个，前一天全绿）。

        :param probe_codes: **优先用这些代码当探针**（调用方知道自己在乎哪些票，
            例如自选池的成员）。这一点很关键 —— 第一版我从全市场快照里随便挑了 5 只
            （挑到 ``920xxx`` 北交所票，夹具里没有它们的日线），**每个探针都失败** →
            直接回退日历日期，修复等于没做。用调用方给的代码才有意义。
        :param max_back: 最多往回找几个交易日（默认 30）。

        判定方式**两段式**（见下方实现）：
        先找「**那一天本身有行情**」（``quote()['suspended'] is False``）——
        这同时满足策略层与引擎下单；找不到再退到「**回看窗口非空**」。
        """
        try:
            base = self.latest_trade_date()
        except NetworkForbiddenInLock:
            # **守卫错误必须上抛**（第四轮复核 M2，MEDIUM）：本函数另外三处
            # （`_has_quote` / `_has_window` / 快照探针）都加了同款前置穿透，
            # **唯独这里漏了** → 会静默回退「今天」。
            # 可达路径：`service.resolve_asof(ds, None)` 在 `state.trade_date` 为空时，
            # 会**在锁内**走到这里（写命令的缺省交易日解析）。
            raise
        except Exception:  # noqa: BLE001 - 无日历时交给调用方
            return self._today()

        # 探针优先级：调用方给的代码（调用方知道自己在乎哪些票）> 快照里的活跃票。
        # 快照里随便挑的票可能**根本没有日线数据**（第一版挑到 920xxx 北交所票，
        # 夹具里没有它们的日线 → 每个探针都失败 → 直接回退日历日期，修复等于没做）。
        probes: list[str] = [str(c).zfill(6) for c in (probe_codes or [])]
        if not probes:
            try:
                frame = self.spot()
                if frame is not None and len(frame) > 0:
                    for _, row in frame.iterrows():
                        last = row.get("last")
                        vol = row.get("volume")
                        if last is None or pd.isna(last) or float(last) <= 0:
                            continue
                        if vol is None or pd.isna(vol) or float(vol) <= 0:
                            continue
                        code = str(row.get("code") or "").zfill(6)
                        if code and code not in probes:
                            probes.append(code)
                        if len(probes) >= 5:
                            break
            except NetworkForbiddenInLock:
                raise  # 守卫错误：上抛，**不得**当成「拿不到快照」
            except Exception:  # noqa: BLE001 - 拿不到快照就不探，退回日历日期
                probes = []
        if not probes:
            return base

        # **两段式判据**（2026-10-13 修正，P3-2）：
        # 两个消费者需要的「有数据的日期」**不是同一件事**：
        #   * `score_watchlist` / 策略层：只要求 **asof 回看窗口非空**（它们自己去取窗口内的日线）；
        #   * `resolve_asof` / 引擎下单：要求 **那一天本身有行情**，否则 `quote()` 标
        #     `suspended=True`，订单被 `SUSPENDED` 误拒 —— 实测（真实时钟、离线、缺省下单）
        #     `quote(000001, 2026-10-12).suspended=True` 而窗口里其实有数据。
        # 第一版只用「窗口非空」判据，于是返回了 2026-10-12（本身无数据）→ 缺省下单全被判停牌。
        # 所以先按「那一天本身有行情」找，找不到再退到「窗口非空」。
        def _has_quote(code: str, d: str) -> bool:
            try:
                return not self.quote(code, d)["suspended"]
            except NetworkForbiddenInLock:
                raise
            except Exception:  # noqa: BLE001 - 该票该日取不到
                return False

        def _has_window(code: str, d: str) -> bool:
            try:
                frame = self.daily(code, self._offset(d, -60), d)
                return frame is not None and len(frame) > 0
            except NetworkForbiddenInLock:
                raise  # 守卫错误：上抛，**不得**当「该探针不行」吞掉（复核者 F-1）
            except Exception:  # noqa: BLE001 - 该票该窗口取不到
                return False

        # 第一段：那一天本身有行情（优先，因为它同时满足两个消费者）
        day = base
        for _ in range(max(1, int(max_back))):
            if any(_has_quote(code, day) for code in probes):
                return day
            day = self.prev_trade_date(day)

        # 第二段：退而求其次，只要回看窗口非空。
        #
        # **它会返回一个「本身没有行情」的日期**（第四轮复核 M6）——
        # 触发条件是「**所有探针在该日期都没有行情**」，复核用合成 `daily_999001.json`
        # 复现过（`asof=2026-08-28, suspended=true`）。
        # 对「窗口消费者」（`score_watchlist`）够用；对「下单消费者」不够 ——
        # 边缘标的（长期停牌 / B 股 / 新上市）仍可能复发 P3-2 的同类症状。
        #
        # 本函数**无法**在这一段里再找出「本身有行情」的日期（那正是第一段找不到的），
        # 所以这里**不假装成功**：发一条 `RuntimeWarning` 让「退到了次优判据」**可见**，
        # 而不是静默返回一个下游可能被判停牌的日期。
        day = base
        for _ in range(max(1, int(max_back))):
            if any(_has_window(code, day) for code in probes):
                # 用 **logger** 而不是 `warnings.warn`：这条消息会**频繁出现**
                # （实测离线夹具下自选池打分就走到第二段 → 全量测试报出 9 条 warnings），
                # `warnings.warn` 会污染 CLI/网页端输出、并在测试里刷屏。
                # 「留痕」的诉求仍在（`caplog` 可断言、`--verbose` 可见），但不打扰正常使用。
                _log.info(
                    "latest_trade_asof 退到「回看窗口非空」判据：返回 %s，"
                    "但探针 %s 在该日都没有行情（第一段判据未命中）。"
                    "对下单用途这可能不足（下游 quote() 会标 suspended）—— "
                    "请显式指定 asof / --trade-date，或先补齐该标的的数据。",
                    day, probes[:3],
                )
                return day
            day = self.prev_trade_date(day)
        return base

    # ------------------------------------------------------------------ #
    # 日线
    # ------------------------------------------------------------------ #
    @staticmethod
    def _adjust_key(adjust: str, unadjust: bool) -> str:
        if unadjust or not adjust:
            return ""
        return str(adjust)

    def _fixture_daily_key(self, code: str, adj: str) -> str:
        return f"daily_{_norm_code(code)}" if adj == "qfq" else f"daily_{_norm_code(code)}_{adj or 'unadjust'}"

    def daily(
        self,
        code: str,
        start: str,
        end: str,
        adjust: str = "qfq",
        unadjust: bool = False,
    ) -> pd.DataFrame:
        """日线。列：date, open, high, low, close, volume, amount, pct_chg, turnover。

        * ``adjust="qfq"``（默认）= 前复权；``adjust=""`` / ``unadjust=True`` = **不复权**真实价。
        * ``date`` 为 ``YYYY-MM-DD`` 字符串，升序。
        * ``amount`` = 成交额（**元**，akshare 原样）。
        * ETF/LOF（588000 / 510050 / 159915 等）同样支持。
        """
        c = _norm_code(code)
        adj = self._adjust_key(adjust, unadjust)
        start, end = str(start)[:10], str(end)[:10]
        cache_key = f"daily_{c}_{adj or 'none'}_{start.replace('-', '')}_{end.replace('-', '')}"
        fixture_key = self._fixture_daily_key(c, adj)
        cached = self._read_entry(self.cache_dir, cache_key)
        if cached is not None and not self._expired(cached, TTL_DAILY):
            return self._frame(cached["payload"], DAILY_COLUMNS)
        if self.offline:
            fixture = self._read_fixture( fixture_key)
            if fixture is not None:
                return self._slice_daily(fixture["payload"], start, end)
            if cached is not None:
                return self._frame(cached["payload"], DAILY_COLUMNS)
            raise DataUnavailable(
                f"离线模式缺少日线 {c}（期望夹具 {fixture_key}.json，区间 {start}~{end}）"
            )
        try:
            payload = self._fetch_daily(c, adj, start, end)
        except NetworkForbiddenInLock:
            raise      # 同上：守卫错误不得被过期缓存兜底吞掉
        except Exception as exc:  # noqa: BLE001
            if cached is not None:
                return self._frame(cached["payload"], DAILY_COLUMNS)
            raise DataUnavailable(str(exc)) from exc
        self._write_entry(self.cache_dir, cache_key, payload)
        return self._frame(payload, DAILY_COLUMNS)

    @staticmethod
    def _slice_daily(payload: Any, start: str, end: str) -> pd.DataFrame:
        rows = payload.get("rows") if isinstance(payload, dict) and "rows" in payload else payload
        rows = [r for r in (rows or []) if start <= str(r.get("date", ""))[:10] <= end]
        frame = DataSource._frame(rows, DAILY_COLUMNS)
        return frame.sort_values("date").reset_index(drop=True) if len(frame) else frame

    @staticmethod
    def _is_fund_code(code: str) -> bool:
        """ETF/LOF：沪市 5xxxxx、深市 1xxxxx（159915 等）。"""
        c = _norm_code(code)
        return c.startswith("5") or c.startswith("1")

    def _fetch_daily(self, code: str, adj: str, start: str, end: str) -> list[dict[str, Any]]:
        """东财优先（含涨跌幅/换手率）→ 新浪股票源 → 新浪基金源（ETF/LOF）。"""
        ak = self._ak()
        if self._is_fund_code(code):
            # ETF/LOF：新浪基金接口（实测 stock_zh_a_daily 对 ETF 返回空/解码失败）
            try:
                frame = self._call(ak.fund_etf_hist_sina, retries=1, symbol=_prefixed(code))
                rows = self._map_etf_sina(frame, start, end)
                if rows:
                    return rows
            except Exception:  # noqa: BLE001 - 继续走通用路径
                pass
        s, e = start.replace("-", ""), end.replace("-", "")
        try:
            # 东财优先；实测不稳定 → 只试 1 次（快速失败），随后走新浪兜底
            frame = self._call(
                ak.stock_zh_a_hist, retries=0,
                symbol=code, period="daily", start_date=s, end_date=e, adjust=adj,
            )
            rows = self._map_hist_daily(frame)
            if rows:
                return rows
        except Exception:  # noqa: BLE001 - 回退新浪源
            pass
        frame = self._call(
            ak.stock_zh_a_daily, symbol=_prefixed(code), start_date=start, end_date=end, adjust=adj
        )
        return self._map_sina_daily(frame)

    @staticmethod
    def _map_hist_daily(frame: pd.DataFrame | None) -> list[dict[str, Any]]:
        """东财 ``stock_zh_a_hist`` → 契约列（**成交量 手 → 股 ×100**，单位表 §4）。"""
        if frame is None or len(frame) == 0:
            return []
        out = pd.DataFrame()
        mapping = {
            "date": ("日期",), "open": ("开盘",), "high": ("最高",), "low": ("最低",),
            "close": ("收盘",), "volume": ("成交量",), "amount": ("成交额",),
            "pct_chg": ("涨跌幅",), "turnover": ("换手率",),
        }
        for target, candidates in mapping.items():
            series = _pick(frame, *candidates)
            out[target] = series if series is not None else None
        out = out.dropna(subset=["date", "close"])
        out["date"] = out["date"].astype(str).str[:10]
        out["volume"] = pd.to_numeric(out["volume"], errors="coerce") * 100.0  # 手 → 股
        rows = _records(out, DAILY_COLUMNS)
        return sorted(rows, key=lambda r: r["date"])

    @staticmethod
    def _map_etf_sina(frame: pd.DataFrame | None, start: str, end: str) -> list[dict[str, Any]]:
        """新浪 ``fund_etf_hist_sina`` → 契约列（成交量单位=份/股，已是原样；无复权选项）。"""
        if frame is None or len(frame) == 0:
            return []
        out = pd.DataFrame()
        for target, candidates in {
            "date": ("date",), "open": ("open",), "high": ("high",), "low": ("low",),
            "close": ("close",), "volume": ("volume",), "amount": ("amount",),
        }.items():
            series = _pick(frame, *candidates)
            out[target] = series if series is not None else None
        out = out.dropna(subset=["date", "close"]).sort_values("date")
        out["date"] = out["date"].astype(str).str[:10]
        out = out[(out["date"] >= start) & (out["date"] <= end)]
        out["pct_chg"] = (pd.to_numeric(out["close"], errors="coerce").pct_change() * 100).fillna(0.0)
        out["turnover"] = None
        return _records(out, DAILY_COLUMNS)

    @staticmethod
    def _map_sina_daily(frame: pd.DataFrame | None) -> list[dict[str, Any]]:
        """新浪 ``stock_zh_a_daily`` → 契约列（成交量已是**股**；换手率为小数 → 百分数）。"""
        if frame is None or len(frame) == 0:
            return []
        out = pd.DataFrame()
        for target, candidates in {
            "date": ("date",), "open": ("open",), "high": ("high",), "low": ("low",),
            "close": ("close",), "volume": ("volume",), "amount": ("amount",),
            "turnover": ("turnover",),
        }.items():
            series = _pick(frame, *candidates)
            out[target] = series if series is not None else None
        out = out.dropna(subset=["date", "close"]).sort_values("date")
        out["date"] = out["date"].astype(str).str[:10]
        close = pd.to_numeric(out["close"], errors="coerce")
        out["pct_chg"] = (close.pct_change() * 100.0).fillna(0.0)
        turnover = pd.to_numeric(out["turnover"], errors="coerce")
        if len(turnover.dropna()) and float(turnover.max()) <= 1.5:
            out["turnover"] = turnover * 100.0  # 新浪源为小数比例 → 统一为百分数
        return _records(out, DAILY_COLUMNS)

    def last_close(self, code: str, asof: str) -> float:
        """``asof``（含）之前最后一根日线的收盘价。"""
        frame = self.daily(code, self._offset(asof, -420), asof)
        if len(frame) == 0:
            raise DataUnavailable(f"{_norm_code(code)} 在 {asof} 之前没有日线数据")
        value = frame.iloc[-1]["close"]
        if value is None or (isinstance(value, float) and math.isnan(value)):
            raise DataUnavailable(f"{_norm_code(code)} 在 {asof} 的收盘价为空")
        return float(value)

    # ------------------------------------------------------------------ #
    # 实时快照
    # ------------------------------------------------------------------ #
    def spot(self, codes: list[str] | None = None) -> pd.DataFrame:
        """全市场/指定代码快照。``codes=None`` 时返回全市场（走缓存，TTL 60s）。

        单位（§4 强制表）：价格=元；``volume``=股；``amount``=元；
        ``total_mv``/``float_mv``=元；``turnover``/``pct_chg``=百分数。
        """
        payload = self._cached("spot", TTL_SPOT, self._fetch_spot)
        frame = self._frame(payload, SPOT_COLUMNS)
        if len(frame) == 0:
            return frame
        frame["code"] = frame["code"].astype(str).map(_norm_code)
        frame = frame.drop_duplicates(subset=["code"], keep="first").reset_index(drop=True)
        if codes:
            wanted = {_norm_code(c) for c in codes}
            frame = frame[frame["code"].isin(wanted)].reset_index(drop=True)
            frame = self._enrich_spot(frame)
        return frame

    def _fetch_spot(self) -> Any:
        """东财全市场快照；实测不可用时回退新浪快照（无估值列，按需补齐）。"""
        ak = self._ak()
        try:
            frame = self._call(ak.stock_zh_a_spot_em, retries=0)
            mapped = self._map_spot(frame, volume_in_lots=True)
            if mapped["rows"]:
                return mapped
        except Exception:  # noqa: BLE001 - 回退新浪源
            pass
        frame = self._call(ak.stock_zh_a_spot)
        return self._map_spot(frame, volume_in_lots=False)

    def _enrich_spot(self, frame: pd.DataFrame, ) -> pd.DataFrame:
        """用新浪日线补齐缺失的 ``float_mv`` / ``turnover``（仅对被请求的少量代码）。"""
        if len(frame) == 0 or self.offline:
            return frame
        need = frame["float_mv"].isna() | frame["turnover"].isna()
        if not bool(need.any()):
            return frame
        ak = self._ak()
        frame = frame.copy()
        for idx in frame.index[need]:
            code = _norm_code(frame.at[idx, "code"])
            try:
                day = self._today()
                daily = self._call(
                    ak.stock_zh_a_daily, retries=1,
                    symbol=_prefixed(code), start_date=self._offset(day, -20), end_date=day, adjust="",
                )
                if daily is None or len(daily) == 0:
                    continue
                last = daily.iloc[-1]
                close = float(last["close"])
                shares = last.get("outstanding_share")
                if shares is not None and not pd.isna(shares) and pd.isna(frame.at[idx, "float_mv"]):
                    frame.at[idx, "float_mv"] = close * float(shares)
                turnover = last.get("turnover")
                if turnover is not None and not pd.isna(turnover) and pd.isna(frame.at[idx, "turnover"]):
                    value = float(turnover)
                    frame.at[idx, "turnover"] = value * 100.0 if value <= 1.5 else value
            except Exception:  # noqa: BLE001 - 补齐失败不阻塞
                continue
        return frame

    @staticmethod
    def _map_spot(frame: pd.DataFrame | None, volume_in_lots: bool = True) -> dict[str, Any]:
        """东财/新浪快照 → 契约列。``volume_in_lots``：东财成交量为「手」，需 ×100。"""
        if frame is None or len(frame) == 0:
            return {"asof": date.today().isoformat(), "rows": []}
        out = pd.DataFrame()
        mapping = {
            "code": ("代码",), "name": ("名称",), "last": ("最新价",), "pct_chg": ("涨跌幅",),
            "open": ("今开",), "high": ("最高",), "low": ("最低",), "prev_close": ("昨收",),
            "volume": ("成交量",), "amount": ("成交额",), "turnover": ("换手率",),
            "pe_ttm": ("市盈率-动态",), "pb": ("市净率",),
            "total_mv": ("总市值",), "float_mv": ("流通市值",),
        }
        for target, candidates in mapping.items():
            series = _pick(frame, *candidates)
            out[target] = series if series is not None else None
        out["code"] = out["code"].astype(str).map(_norm_code)
        if volume_in_lots:
            out["volume"] = pd.to_numeric(out["volume"], errors="coerce") * 100.0  # 手 → 股
        prev = pd.to_numeric(out["prev_close"], errors="coerce")
        last = pd.to_numeric(out["last"], errors="coerce")
        ups, downs = [], []
        for code, name, p in zip(out["code"], out["name"], prev):
            pct = price_limit_pct(code, "" if name is None else str(name))
            if p is None or (isinstance(p, float) and math.isnan(p)) or float(p) <= 0:
                ups.append(None)
                downs.append(None)
            else:
                ups.append(_round2(float(p) * (1 + pct)))
                downs.append(_round2(float(p) * (1 - pct)))
        out["limit_up"] = ups
        out["limit_down"] = downs
        out["last"] = last
        return {"asof": date.today().isoformat(), "rows": _records(out, SPOT_COLUMNS)}

    def _spot_frame_raw(self) -> tuple[pd.DataFrame, str]:
        """夹具/缓存里的 spot（不触发联网失败）：返回 (frame, asof)。"""
        entry = self._read_entry(self.cache_dir, "spot")
        if entry is None or self._expired(entry, TTL_SPOT):
            if self.offline:
                entry = self._read_fixture( "spot") or entry
        if entry is None:
            return pd.DataFrame(columns=SPOT_COLUMNS), ""
        payload = entry.get("payload") or {}
        asof = str(payload.get("asof", ""))[:10] if isinstance(payload, dict) else ""
        frame = self._frame(payload, SPOT_COLUMNS)
        return frame, asof

    def live_quote(self, code: str, *, require_today: bool = True) -> dict:
        """**按当前快照价**报价（B，2026-10-14：用户选定的「实时价」入口）。

        返回::

            {"quote": <同 quote() 的 dict>, "source": "live",
             "asof": <快照 payload.asof>, "fetched_at": <缓存 fetched_at>,
             "stale": <bool>}

        **为什么需要它**：``resolve_asof`` 在 15:30 前会把 asof 回退到上一交易日
        （未来函数防护），于是**盘中缺省下单会走到「日线推导」分支并判停牌**。
        重写 ``resolve_asof`` 会动到回测语义，所以给一个**显式**入口：用户明说「按现价成交」。

        **绝不静默退回陈旧价**：``require_today=True`` 时，若快照的 ``asof`` 不是今天
        （或快照缺失/过期），抛 :class:`DataUnavailable` 并说明怎么办 ——
        因为「以为是实时价、其实是几天前的价」比「明确失败」危险得多。

        ``fetched_at`` 是**真正回答「这笔用的哪一刻的价」**的字段（D：落进订单审计）。
        """
        entry = self._read_entry(self.cache_dir, "spot")
        expired = entry is None or self._expired(entry, TTL_SPOT)
        if expired and self.offline:
            entry = self._read_fixture("spot") or entry
        if entry is None:
            raise DataUnavailable(
                f"没有可用的实时快照（{self.cache_dir}/spot.json 缺失）："
                f"在线时先跑一次取数（如 `python -m ptrade sync --codes {code}`），"
                f"或改用 --price <价格> 显式指定成交价。"
            )
        payload = entry.get("payload") or {}
        asof = str(payload.get("asof", ""))[:10] if isinstance(payload, dict) else ""
        fetched_at = str(entry.get("fetched_at", ""))
        today = self._today()
        # **`asof` 缺失不得被当作新鲜**（第四轮复核 M4，MEDIUM）：
        # 原写法 `bool(asof) and asof != today` 在 `asof` 为空串/缺键时为假 →
        # `require_today=True` **拦不住**，实测会成交并记下
        # `price_source='live'` + `price_asof=''` = 「贴着实时的标签、却不知道是哪天的价」。
        # 「不知道是哪天」必须按**不可用**处理，而不是按新鲜处理。
        stale = bool(expired) or (not asof) or (asof != today)
        if require_today and stale:
            when = f"asof={asof or '(未记录)'}、fetched_at={fetched_at or '(未记录)'}"
            raise DataUnavailable(
                f"实时快照不可用（{when}，今天 {today}）：不能按当前价成交。"
                f"请在线重取快照，或改用 --price <价格> 显式指定。"
                f"（**不会**退回陈旧价 —— 那会让你以为按实时价成交了。）"
            )
        frame = self._frame(payload, SPOT_COLUMNS)
        c = _norm_code(code)
        row = frame[frame["code"].astype(str).str.zfill(6) == c] if len(frame) else frame
        if len(row) == 0:
            raise DataUnavailable(f"实时快照里没有 {c}（快照 asof={asof or '未知'}）")
        item = row.iloc[0]
        quote = self.quote(c, asof or today)
        return {"quote": quote, "source": "live", "asof": asof,
                "fetched_at": fetched_at, "stale": stale}

    def quote(self, code: str, asof: str) -> dict:
        """单票快照：``{"code","name","last","prev_close","limit_up","limit_down","suspended"}``。

        优先用快照（asof 与快照日期一致）；否则由日线推导（历史 asof 也能工作）。
        """
        c = _norm_code(code)
        asof = str(asof)[:10]
        frame, spot_asof = self._spot_frame_raw()
        if len(frame) and (spot_asof == asof or not spot_asof):
            row = frame[frame["code"].astype(str).str.zfill(6) == c]
            if len(row):
                item = row.iloc[0]
                last = item.get("last")
                prev = item.get("prev_close")
                volume = item.get("volume")
                last_f = float(last) if last is not None and not pd.isna(last) else float("nan")
                suspended = bool(
                    math.isnan(last_f) or last_f <= 0
                    or (volume is not None and not pd.isna(volume) and float(volume) <= 0)
                )
                pct = price_limit_pct(c, "" if item.get("name") is None else str(item.get("name")))
                base = float(prev) if prev is not None and not pd.isna(prev) and float(prev) > 0 else last_f
                return {
                    "code": c,
                    "name": "" if item.get("name") is None else str(item.get("name")),
                    "last": last_f,
                    "prev_close": base,
                    "limit_up": _round2(base * (1 + pct)),
                    "limit_down": _round2(base * (1 - pct)),
                    "suspended": suspended,
                }
        # 日线推导（历史 asof / 快照不可用）
        window = self.daily(c, self._offset(asof, -60), asof)
        name = c
        try:
            name = str(self.spot([c]).iloc[0]["name"])
        except NetworkForbiddenInLock:
            raise  # 守卫错误：上抛，**不得**把名称静默退回代码
        except Exception:  # noqa: BLE001 - 名称非必需
            pass
        if len(window) == 0:
            pct = price_limit_pct(c, name)
            return {
                "code": c, "name": name, "last": float("nan"), "prev_close": float("nan"),
                "limit_up": float("nan"), "limit_down": float("nan"), "suspended": True,
            }
        last_row = window.iloc[-1]
        last = float(last_row["close"])
        same_day = str(last_row["date"])[:10] == asof
        prev = float(window.iloc[-2]["close"]) if len(window) >= 2 else last
        volume = last_row.get("volume")
        suspended = (not same_day) or bool(
            volume is not None and not pd.isna(volume) and float(volume) <= 0
        )
        pct = price_limit_pct(c, name)
        return {
            "code": c, "name": name, "last": last, "prev_close": prev,
            "limit_up": _round2(prev * (1 + pct)), "limit_down": _round2(prev * (1 - pct)),
            "suspended": suspended,
        }

    # ------------------------------------------------------------------ #
    # 两融
    # ------------------------------------------------------------------ #
    def margin_targets(self, asof: str | None = None) -> pd.DataFrame:
        """两融标的名单。列：code, name, can_finance, can_short, limit_pct。

        深市用 ``stock_margin_underlying_info_szse``；沪市用 ``stock_margin_detail_sse``
        的「标的证券代码」+ 融券余量（用于识别不可融券标的）。沪深合并去重。
        """
        payload = self._cached(
            "margin_targets", TTL_MARGIN, lambda: self._fetch_margin_targets(asof)
        )
        return self._frame(payload, MARGIN_COLUMNS)

    def _fetch_margin_targets(self, asof: str | None) -> list[dict[str, Any]]:
        ak = self._ak()
        day = (asof or self.latest_trade_date()).replace("-", "")
        rows: dict[str, dict[str, Any]] = {}

        # 深市：显式 融资标的 / 融券标的 标记
        frame = self._call(ak.stock_margin_underlying_info_szse, date=day)
        if frame is not None and len(frame):
            codes = _pick(frame, "证券代码", "标的证券代码")
            names = _pick(frame, "证券简称", "标的证券简称")
            fin = _bool_cn(_pick(frame, "融资标的"))
            short = _bool_cn(_pick(frame, "融券标的"))
            limit = _pick(frame, "涨跌幅限制")
            for i, code in enumerate(codes.tolist() if codes is not None else []):
                c = _norm_code(code)
                if not c:
                    continue
                rows[c] = {
                    "code": c,
                    "name": str(names.iloc[i]) if names is not None else "",
                    "can_finance": bool(fin.iloc[i]) if len(fin) else True,
                    "can_short": bool(short.iloc[i]) if len(short) else False,
                    "limit_pct": str(limit.iloc[i]) if limit is not None else "",
                }

        # 沪市：「标的证券代码」即两融标的集合（含 ETF，按 60/68/51 过滤）
        frame = self._call(ak.stock_margin_detail_sse, date=day)
        if frame is not None and len(frame):
            codes = _pick(frame, "标的证券代码", "证券代码")
            names = _pick(frame, "标的证券简称", "证券简称")
            short_bal = _pick(frame, "融券余量", "融券余额")
            for i, code in enumerate(codes.tolist() if codes is not None else []):
                c = _norm_code(code)
                if not c or c in rows:
                    continue
                # 沪市：融券余量 > 0 才认定为当期可融券标的（保守，避免假阳性券源）
                can_short = False
                if short_bal is not None:
                    try:
                        can_short = float(short_bal.iloc[i]) > 0
                    except (TypeError, ValueError):
                        can_short = False
                rows[c] = {
                    "code": c,
                    "name": str(names.iloc[i]) if names is not None else "",
                    "can_finance": True,
                    "can_short": can_short,
                    "limit_pct": "",
                }
        return list(rows.values())

    def is_margin_target(self, code: str, asof: str | None = None) -> bool:
        c = _norm_code(code)
        frame = self.margin_targets(asof)
        if len(frame) == 0:
            return False
        hit = frame[frame["code"].astype(str).str.zfill(6) == c]
        return bool(len(hit)) and bool(hit.iloc[0]["can_finance"])

    def is_short_target(self, code: str, asof: str | None = None) -> bool:
        c = _norm_code(code)
        frame = self.margin_targets(asof)
        if len(frame) == 0:
            return False
        hit = frame[frame["code"].astype(str).str.zfill(6) == c]
        return bool(len(hit)) and bool(hit.iloc[0]["can_short"])

    # ------------------------------------------------------------------ #
    # 基本面
    # ------------------------------------------------------------------ #
    def quarterly_report(self, period: str) -> pd.DataFrame:
        """业绩报表（累积口径），列见契约。``period`` 形如 ``'20250630'``。"""
        key = f"quarterly_{str(period)[:8]}"
        payload = self._cached(key, TTL_FUNDAMENTAL, lambda: self._fetch_quarterly(str(period)[:8]))
        return self._frame(payload, QUARTER_COLUMNS)

    def _fetch_quarterly(self, period: str) -> list[dict[str, Any]]:
        ak = self._ak()
        frame = self._call(ak.stock_yjbb_em, date=period)
        return self._map_quarterly(frame)

    @staticmethod
    def _map_quarterly(frame: pd.DataFrame | None) -> list[dict[str, Any]]:
        if frame is None or len(frame) == 0:
            return []
        out = pd.DataFrame()
        mapping = {
            "code": ("股票代码", "代码"), "name": ("股票简称", "名称"), "eps": ("每股收益",),
            "revenue": ("营业总收入-营业总收入", "营业总收入"),
            "revenue_yoy": ("营业总收入-同比增长",), "revenue_qoq": ("营业总收入-季度环比增长",),
            "net_profit": ("净利润-净利润", "净利润"),
            "net_profit_yoy": ("净利润-同比增长",), "net_profit_qoq": ("净利润-季度环比增长",),
            "bps": ("每股净资产",), "roe": ("净资产收益率",), "ocf_ps": ("每股经营现金流量",),
            "gross_margin": ("销售毛利率",), "industry": ("所处行业",),
            "announce_date": ("最新公告日期",),
        }
        for target, candidates in mapping.items():
            series = _pick(frame, *candidates)
            out[target] = series if series is not None else None
        out["code"] = out["code"].astype(str).str.zfill(6)
        out["announce_date"] = out["announce_date"].astype(str).str[:10]
        return _records(out, QUARTER_COLUMNS)

    def fin_indicator(self, code: str, start_year: str) -> pd.DataFrame:
        """财务指标（新浪源，中文长列名 → 契约英文列）。"""
        c = _norm_code(code)
        key = f"fin_indicator_{c}_{str(start_year)[:4]}"
        payload = self._cached(
            key, TTL_FUNDAMENTAL,
            lambda: self._map_fin_indicator(self._call(self._ak().stock_financial_analysis_indicator,
                                                       symbol=c, start_year=str(start_year)[:4])),
            fixture_key=f"fin_indicator_{c}",
        )
        return self._frame(payload, FIN_COLUMNS)

    @staticmethod
    def _map_fin_indicator(frame: pd.DataFrame | None) -> list[dict[str, Any]]:
        if frame is None or len(frame) == 0:
            return []
        out = pd.DataFrame()
        mapping = {
            "date": ("日期",),
            "eps": ("摊薄每股收益(元)", "加权每股收益(元)"),
            "eps_deducted": ("扣除非经常性损益后的每股收益(元)",),
            "bps": ("每股净资产_调整前(元)", "每股净资产_调整后(元)"),
            "roe": ("净资产收益率(%)",),
            "roe_weighted": ("加权净资产收益率(%)",),
            "revenue_yoy": ("主营业务收入增长率(%)",),
            "net_profit_yoy": ("净利润增长率(%)",),
            "gross_margin": ("销售毛利率(%)",),
            "debt_ratio": ("资产负债率(%)",),
            "ocf_ps": ("每股经营性现金流(元)",),
        }
        for target, candidates in mapping.items():
            series = _pick(frame, *candidates)
            out[target] = series if series is not None else None
        out = out.dropna(subset=["date"])
        out["date"] = out["date"].astype(str).str[:10]
        return _records(out.sort_values("date"), FIN_COLUMNS)

    def holders(self, period: str) -> pd.DataFrame:
        """股东户数。列见契约（``holders_chg_pct`` 为百分数）。"""
        key = f"holders_{str(period)[:8]}"
        payload = self._cached(key, TTL_HOLDERS, lambda: self._fetch_holders(str(period)[:8]))
        return self._frame(payload, HOLDERS_COLUMNS)

    def _fetch_holders(self, period: str) -> list[dict[str, Any]]:
        ak = self._ak()
        frame = self._call(ak.stock_zh_a_gdhs, symbol=period)
        if frame is None or len(frame) == 0:
            return []
        out = pd.DataFrame()
        mapping = {
            "code": ("代码", "证券代码"), "name": ("名称", "证券简称"),
            "holders": ("股东户数-本次",), "holders_prev": ("股东户数-上次",),
            "holders_chg_pct": ("股东户数-增减比例",), "avg_hold_value": ("户均持股市值",),
            "total_mv": ("总市值",), "total_share": ("总股本",),
        }
        for target, candidates in mapping.items():
            series = _pick(frame, *candidates)
            out[target] = series if series is not None else None
        out["code"] = out["code"].astype(str).str.zfill(6)
        return _records(out, HOLDERS_COLUMNS)

    def top_holders(self, code: str, asof: str | None = None) -> pd.DataFrame:
        """十大股东/机构持股。**按 asof 取该时点已披露的真实期次**（防前视）。

        列：``code, holder_name, holder_type, shares, ratio, change, report_date, announce_date``

        * ``asof=None``：返回**最新一期**（与旧行为一致，单期行数不变）。
        * ``asof='YYYY-MM-DD'``：只保留 ``announce_date <= asof``（缺失公告日时退回 ``report_date``）
          的期次中**最新的一期**；``asof`` 之前无任何已披露期次 → 返回**空表**（消费方按"数据缺失"降级，
          绝不回落到未来期次）。
        * ``frame.attrs['top_holders_meta']`` 提供
          ``{"report_date","announce_date","requested_asof","disclosed_on","is_historical","periods"}``：
          ``is_historical=True`` 表示该期在 asof 时点**确已披露**（无前视）。
        * ``change`` = 相对上一报告期的持股数量变动（无上期则 None，不臆测）。
        * ``ratio`` 为百分数；源接口对部分股东缺失时由夹具生成器用「持股数量 ÷ 总股本」补齐。
        """
        c = _norm_code(code)
        key = f"top_holders_{c}"
        payload = self._cached(
            key, TTL_HOLDERS,
            lambda: self._map_top_holders(
                self._call(self._ak().stock_main_stock_holder, stock=c), c),
        )
        frame = self._frame(payload, TOPH_COLUMNS)
        meta: dict[str, Any] = {
            "requested_asof": str(asof)[:10] if asof else None,
            "report_date": None, "announce_date": None, "disclosed_on": None,
            "is_historical": False, "periods": 0,
        }
        if len(frame) == 0:
            meta["note"] = "数据源无任何期次"
            frame.attrs["top_holders_meta"] = meta
            return frame
        # 显式空值处理：pandas 新版字符串 dtype 下 `astype(str)` 仍可能保留 NA（不是字面量 "nan"），
        # 用字符串黑名单会失效并**静默清空整表**。故统一 fillna("") 后再比较，并只丢弃缺期次的行。
        frame["report_date"] = frame["report_date"].fillna("").astype(str).str.strip().str[:10]
        frame["announce_date"] = frame["announce_date"].fillna("").astype(str).str.strip().str[:10]
        missing_period = int((frame["report_date"] == "").sum())
        if missing_period:
            frame = frame[frame["report_date"] != ""].reset_index(drop=True)
            meta["rows_without_period_dropped"] = missing_period
        if len(frame) == 0:
            meta["note"] = "所有行都缺少 report_date（数据源未给期次）"
            frame.attrs["top_holders_meta"] = meta
            return frame
        disclosed = [
            a if a else r for r, a in zip(frame["report_date"], frame["announce_date"])
        ]
        frame["_disclosed_on"] = disclosed
        meta["periods"] = int(frame["report_date"].nunique())
        if asof:
            asof_day = str(asof)[:10]
            eligible = frame[frame["_disclosed_on"] <= asof_day]
            periods = sorted({str(p) for p in eligible["report_date"].unique()})
            if not periods:
                meta["note"] = f"{asof_day} 之前没有任何已披露期次（不回落到未来期次）"
                empty = frame.iloc[0:0].drop(columns=["_disclosed_on"])
                empty.attrs["top_holders_meta"] = meta
                return empty
            latest = periods[-1]
            selected = eligible[eligible["report_date"] == latest]
        else:
            periods = sorted({str(p) for p in frame["report_date"].unique()})
            latest = periods[-1]
            selected = frame[frame["report_date"] == latest]
        selected = selected.drop(columns=["_disclosed_on"]).reset_index(drop=True)
        announced = [a for a in selected["announce_date"] if a]
        meta.update(
            report_date=latest,
            announce_date=str(announced[0])[:10] if announced else None,
            disclosed_on=str(announced[0])[:10] if announced else latest,
            is_historical=True,
        )
        selected.attrs["top_holders_meta"] = dict(meta)
        return selected

    @staticmethod
    def _map_top_holders(frame: pd.DataFrame | None, code: str) -> list[dict[str, Any]]:
        """十大股东：**保留全部报告期**，并计算同名股东相对上一期的持股变动。

        列：code, holder_name, holder_type, shares, ratio, change, report_date, announce_date

        * ``report_date``   = 「截至日期」= 股东名册报告期（真实期次）
        * ``announce_date`` = 「公告日期」= 该期披露日（防前视：只有 announce_date <= asof 才可用）
        * ``change``        = 本期持股数量 − 上一报告期同名股东持股数量（无上期则 None，**不臆测**）
        """
        if frame is None or len(frame) == 0:
            return []
        out = pd.DataFrame()
        for target, candidates in {
            "holder_name": ("股东名称",), "shares": ("持股数量",), "ratio": ("持股比例",),
        }.items():
            series = _pick(frame, *candidates)
            out[target] = series if series is not None else None
        out["code"] = code
        nature = _pick(frame, "股本性质", "股东性质")
        out["holder_type"] = nature if nature is not None else "未知"
        report = _pick(frame, "截至日期", "报告期")
        out["report_date"] = report.astype(str).str[:10] if report is not None else None
        announce = _pick(frame, "公告日期", "最新公告日期")
        out["announce_date"] = announce.astype(str).str[:10] if announce is not None else None
        out["shares"] = pd.to_numeric(out["shares"], errors="coerce")
        ratio = pd.to_numeric(out["ratio"], errors="coerce")
        if len(ratio.dropna()) and float(ratio.max()) <= 1.5:
            ratio = ratio * 100.0  # 统一为百分数
        out["ratio"] = ratio
        out = out.dropna(subset=["report_date"]).sort_values(["report_date", "holder_name"])

        periods = sorted({str(p) for p in out["report_date"].unique()})
        change_map: dict[tuple[str, str], float] = {}
        for index in range(1, len(periods)):
            prev_rows = out[out["report_date"] == periods[index - 1]]
            prev_shares = {
                str(n): float(v)
                for n, v in zip(prev_rows["holder_name"], prev_rows["shares"])
                if v is not None and not pd.isna(v)
            }
            cur_rows = out[out["report_date"] == periods[index]]
            for name, shares in zip(cur_rows["holder_name"], cur_rows["shares"]):
                key = (periods[index], str(name))
                if key[1] in prev_shares and shares is not None and not pd.isna(shares):
                    change_map[key] = float(shares) - prev_shares[key[1]]
        out["change"] = [
            change_map.get((str(p), str(n)))
            for p, n in zip(out["report_date"], out["holder_name"])
        ]
        return _records(out, TOPH_COLUMNS)

    # ---- 行业 ---- #
    def _industry_map(self) -> dict[str, str]:
        payload = self._cached("industry_map", TTL_FUNDAMENTAL, self._fetch_industry_map)
        if isinstance(payload, dict) and "rows" in payload:
            payload = payload["rows"]
        if isinstance(payload, list):
            return {_norm_code(r.get("code", "")): str(r.get("industry", "")) for r in payload}
        return dict(payload or {})

    def _fetch_industry_map(self) -> list[dict[str, Any]]:
        """行业归属取自业绩报表的「所处行业」（单次全市场调用，口径统一）。"""
        period = self._latest_report_period()
        frame = self.quarterly_report(period)
        if len(frame) == 0:
            return []
        rows = [
            {"code": str(r["code"]).zfill(6), "industry": str(r.get("industry") or "")}
            for r in frame.to_dict("records")
        ]
        return rows

    def _latest_report_period(self) -> str:
        """最近一个已披露的报告期（按今天回推，避开未披露期）。"""
        today = date.today()
        candidates = []
        for year in (today.year, today.year - 1):
            for month_day, _ in (("1231", 4), ("0930", 10), ("0630", 8), ("0331", 4)):
                candidates.append(f"{year}{month_day}")
        for period in sorted(candidates, reverse=True):
            if period <= today.strftime("%Y%m%d"):
                return period
        return f"{today.year - 1}1231"

    def industry_of(self, code: str) -> str:
        """行业名；取不到返回空串（策略层据此退化到绝对 RS 分档）。"""
        try:
            return self._industry_map().get(_norm_code(code), "")
        except NetworkForbiddenInLock:
            raise      # 守卫错误：上抛，**不得**静默退化成「无行业」
        except DataUnavailable:
            return ""

    def industry_constituents(self, industry: str) -> list[str]:
        """同行业代码列表；取不到返回空列表。"""
        try:
            mapping = self._industry_map()
        except NetworkForbiddenInLock:
            raise      # 守卫错误：上抛（它是 DataUnavailable 子类，必须排在前面）
        except DataUnavailable:
            return []
        return sorted([c for c, ind in mapping.items() if ind and ind == str(industry)])

    def universe(self) -> list[str]:
        """比对基准池（夹具/缓存内标的），用于市场宽度与行业内排名。"""
        try:
            payload = self._cached("universe", TTL_FUNDAMENTAL, lambda: list(UNIVERSE_CODES))
        except NetworkForbiddenInLock:
            raise      # 守卫错误：上抛，**不得**静默换掉比对基准池
        except DataUnavailable:
            return list(UNIVERSE_CODES)
        if isinstance(payload, dict):
            payload = payload.get("rows") or []
        codes = [_norm_code(c) for c in (payload or [])]
        return codes or list(UNIVERSE_CODES)

    # ------------------------------------------------------------------ #
    # 指数
    # ------------------------------------------------------------------ #
    def index_daily(self, index_code: str, start: str, end: str) -> pd.DataFrame:
        """指数日线（东财优先，新浪兜底）。列：date, open, high, low, close, volume, pct_chg。"""
        code = _normalize_index_code(index_code)
        start, end = str(start)[:10], str(end)[:10]
        cache_key = f"index_{code}_{start.replace('-', '')}_{end.replace('-', '')}"
        fixture_key = f"index_{code}"
        cached = self._read_entry(self.cache_dir, cache_key)
        if cached is not None and not self._expired(cached, TTL_INDEX):
            return self._frame(cached["payload"], INDEX_COLUMNS)
        if self.offline:
            fixture = self._read_fixture( fixture_key)
            if fixture is not None:
                return self._slice_index(fixture["payload"], start, end)
            if cached is not None:
                return self._frame(cached["payload"], INDEX_COLUMNS)
            raise DataUnavailable(f"离线模式缺少指数日线 {code}（期望夹具 {fixture_key}.json）")
        try:
            payload = self._fetch_index(code, start, end)
        except NetworkForbiddenInLock:
            raise      # 同上
        except Exception as exc:  # noqa: BLE001
            if cached is not None:
                return self._frame(cached["payload"], INDEX_COLUMNS)
            raise DataUnavailable(str(exc)) from exc
        self._write_entry(self.cache_dir, cache_key, payload)
        return self._frame(payload, INDEX_COLUMNS)

    @staticmethod
    def _slice_index(payload: Any, start: str, end: str) -> pd.DataFrame:
        rows = payload.get("rows") if isinstance(payload, dict) and "rows" in payload else payload
        rows = [r for r in (rows or []) if start <= str(r.get("date", ""))[:10] <= end]
        frame = DataSource._frame(rows, INDEX_COLUMNS)
        return frame.sort_values("date").reset_index(drop=True) if len(frame) else frame

    def _fetch_index(self, code: str, start: str, end: str) -> list[dict[str, Any]]:
        ak = self._ak()
        try:
            frame = self._call(
                ak.stock_zh_index_daily_em, symbol=code,
                start_date=start.replace("-", ""), end_date=end.replace("-", ""),
            )
            rows = self._map_index_em(frame)
            if rows:
                return rows
        except Exception:  # noqa: BLE001 - 回退新浪源（东财指数接口实测不稳定）
            pass
        frame = self._call(ak.stock_zh_index_daily, symbol=code)
        return self._map_index_sina(frame, start, end)

    @staticmethod
    def _map_index_em(frame: pd.DataFrame | None) -> list[dict[str, Any]]:
        if frame is None or len(frame) == 0:
            return []
        out = pd.DataFrame()
        for target, candidates in {
            "date": ("date", "日期"), "open": ("open", "开盘"), "high": ("high", "最高"),
            "low": ("low", "最低"), "close": ("close", "收盘"), "volume": ("volume", "成交量"),
            "amount": ("amount", "成交额"),
        }.items():
            series = _pick(frame, *candidates)
            out[target] = series if series is not None else None
        out = out.dropna(subset=["date", "close"]).sort_values("date")
        out["date"] = out["date"].astype(str).str[:10]
        out["pct_chg"] = (pd.to_numeric(out["close"], errors="coerce").pct_change() * 100).fillna(0.0)
        return _records(out, INDEX_COLUMNS)

    @staticmethod
    def _map_index_sina(frame: pd.DataFrame | None, start: str, end: str) -> list[dict[str, Any]]:
        if frame is None or len(frame) == 0:
            return []
        out = pd.DataFrame()
        for target, candidates in {
            "date": ("date",), "open": ("open",), "high": ("high",), "low": ("low",),
            "close": ("close",), "volume": ("volume",),
        }.items():
            series = _pick(frame, *candidates)
            out[target] = series if series is not None else None
        out = out.dropna(subset=["date", "close"]).sort_values("date")
        out["date"] = out["date"].astype(str).str[:10]
        out = out[(out["date"] >= start) & (out["date"] <= end)]
        close = pd.to_numeric(out["close"], errors="coerce")
        out["pct_chg"] = (close.pct_change() * 100).fillna(0.0)
        return _records(out, INDEX_COLUMNS)

    # ---- 市场方向 ---- #
    def market_breadth(self, asof: str) -> dict:
        """市场方向代理指标（指数部分来自指数日线；涨跌家数来自比对基准池）。"""
        asof = str(asof)[:10]
        frame = self.index_daily("sh000001", self._offset(asof, -500), asof)
        index_close = index_ma50 = index_ma200 = None
        pct_chg_1d = pct_chg_20d = 0.0
        above50 = above200 = False
        if len(frame):
            closes = pd.to_numeric(frame["close"], errors="coerce").dropna().tolist()
            index_close = float(closes[-1])
            index_ma50 = float(sum(closes[-50:]) / min(len(closes), 50))
            index_ma200 = float(sum(closes[-200:]) / min(len(closes), 200))
            above50 = index_close >= index_ma50
            above200 = index_close >= index_ma200
            if len(closes) >= 2 and closes[-2]:
                pct_chg_1d = (closes[-1] / closes[-2] - 1.0) * 100.0
            if len(closes) >= 21 and closes[-21]:
                pct_chg_20d = (closes[-1] / closes[-21] - 1.0) * 100.0
        adv, dec, up_limit, down_limit = self._breadth_counts(asof)
        return {
            "index_close": index_close,
            "index_ma50": index_ma50,
            "index_ma200": index_ma200,
            "above_ma50": bool(above50),
            "above_ma200": bool(above200),
            "pct_chg_1d": float(pct_chg_1d),
            "pct_chg_20d": float(pct_chg_20d),
            "advancers": int(adv),
            "decliners": int(dec),
            "limit_up_count": int(up_limit),
            "limit_down_count": int(down_limit),
        }

    def _breadth_counts(self, asof: str) -> tuple[int, int, int, int]:
        """涨跌家数 / 涨跌停家数：优先用当日全市场快照，否则用比对基准池日线。"""
        frame, spot_asof = self._spot_frame_raw()
        if len(frame) and spot_asof == asof:
            last = pd.to_numeric(frame["last"], errors="coerce")
            prev = pd.to_numeric(frame["prev_close"], errors="coerce")
            up_lim = pd.to_numeric(frame["limit_up"], errors="coerce")
            down_lim = pd.to_numeric(frame["limit_down"], errors="coerce")
            valid = last.notna() & prev.notna() & (last > 0) & (prev > 0)
            adv = int((valid & (last > prev)).sum())
            dec = int((valid & (last < prev)).sum())
            up = int((valid & up_lim.notna() & (last >= up_lim - 0.005)).sum())
            down = int((valid & down_lim.notna() & (last <= down_lim + 0.005)).sum())
            return adv, dec, up, down

        codes = self.universe()
        adv = dec = up = down = 0
        for code in codes:
            try:
                window = self.daily(code, self._offset(asof, -40), asof)
            except NetworkForbiddenInLock:
                raise  # 守卫错误：上抛，**不得**少算涨跌家数
            except DataUnavailable:
                continue
            if len(window) < 2:
                continue
            last_row = window.iloc[-1]
            prev_close = float(window.iloc[-2]["close"])
            close = float(last_row["close"])
            if prev_close <= 0:
                continue
            if close > prev_close:
                adv += 1
            elif close < prev_close:
                dec += 1
            pct = price_limit_pct(code)
            if close >= _round2(prev_close * (1 + pct)) - 0.005:
                up += 1
            elif close <= _round2(prev_close * (1 - pct)) + 0.005:
                down += 1
        return adv, dec, up, down
