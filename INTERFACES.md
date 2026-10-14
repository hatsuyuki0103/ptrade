# INTERFACES.md — CANSLIM 模拟盘系统冻结接口契约

> **本文件是并行开发的唯一接口真源。任何模块不得偏离本文件的签名、字段名、错误码。**
> 需要变更 → 先改本文件并通知 Lead，不得私自改签名。
> 版本：**v3.5**（2026-10-10）

> ⚠️ **v1 → v2 → v3 演进（含一次 Lead 自纠错，务必读）**
>
> **v1 的错**：`margin_used` 被同时当作「现金桶」与「总资产分量」→ `total_assets` 虚高（700000 vs 正确 540000）。
> 但 v1 的**负债口径是对的**：融资买入时负债 = 全额买入金额 `N`。
>
> **v2 的错（Lead 自纠错，由 verifier 独立发现）**：v2 把负债改成 `N × (1 - m_long)`，
> 并把维持担保比例的分子写成 `available_cash + frozen_cash + long_mv`。
> 结果：50 万本金融资买入 20 万的场景 ratio 从 3.50 抬到 **13.50**；
> 更严重的是**多头融资账户的 130% 平仓线几乎不可达**（单笔 20 万时标的价格跌到 0，ratio 下界仍 = 8.50 > 1.30），
> `forced_liquidation` / 风险门禁 / task-2 必测项 11 全部退化成死代码。
> v2 还错在**把融券冻结券款算进维持担保比例分子**——冻结券款是要拿去还券的，不是担保物。
>
> **v3 结论**：回到 v1 的负债口径（交易所口径），并修正 v1 的 `total_assets` 与融券分子。
>
> **维持担保比例（最终，交易所口径）**
> ```
> 分子（担保物）= available_cash + frozen_collateral + long_market_value
> 分母（负债）  = financing_debt + short_market_value + interest_accrued
> 其中 financing_debt = Σ 融资买入金额 N（全额）              # 不是 N×(1-m)
>     short_market_value = Σ 融券欠券数量 × 最新价             # 市值口径
> ```
> **v3.1：融券卖出所得款计入分子。** 深交所投教明确「融券卖出所得资金」属于**可充抵保证金**的资金，
> 且交易所口径分子为「现金 + 信用证券账户内证券市值」——券款是信用资金账户里的现金。
> 两条腿的会计处理因此对称：融资 leg 把「借钱买来的市值」计入，融券 leg 把「借券卖出换来的现金」计入。
> （v3 曾把 `frozen_collateral` 排除在分子外，属**保守方向**的偏差 —— 低报 ratio、高报风险，
> 不会漏放风控，但会让 I3「与交易所口径一致」不成立。已修正。）
> `frozen_t1`（T+1 待交收卖出款）**仍排除**在分子外：那不是保证金资金，只是尚未交收。
>
> **融资买入时的自有资金不出账**：它在信用账户里，作为担保物计入分子，同时全额 N 计入融资负债。
>
> **v3 数值基准（测试直接断言，容差 1e-6；零费用口径）**
>
> | 场景 | available_cash | frozen_collateral | long_mv | financing_debt | 担保物 | 负债 | net_assets | ratio |
> |---|---|---|---|---|---|---|---|---|
> | 初始（50 万，无仓） | 500000 | 0 | 0 | 0 | 500000 | 0 | 500000 | `inf` |
> | 融资买入 20 万（m=0.80） | 500000 | 0 | 200000 | 200000 | 700000 | 200000 | 500000 | **3.5** |
> | 同上，票跌 20% | 500000 | 0 | 160000 | 200000 | 660000 | 200000 | 460000 | **3.3** |
> | **融资满额 N=625000** | 500000 | 0 | 625000 | 625000 | 1125000 | 625000 | 500000 | **1.8** |
> | 同上，票跌 50% | 500000 | 0 | 312500 | 625000 | 812500 | 625000 | 187500 | **1.30** |
> | 融券卖出 20 万（m=1.00） | 500000 | 200000 | 0 | 0 | 700000 | 200000 | 500000 | **3.5** |
> | 同上，券跌 20% | 500000 | 200000 | 0 | 0 | 700000 | 160000 | 540000 | **4.375** |
>
> **融资容量与「平仓线可达性」（v3.1 修正了 v3 的算术错误；v3.2 补充引擎可达性）**
>
> **谁真正 binding —— 三层约束，顺序如下（v3.2 补，务必按此理解）**：
>
> | 层 | 约束 | 默认参数上限 | 是否 binding |
> |---|---|---|---|
> | ① **仓位限制**（§5 强约束 6） | 单票 ≤ `net_assets × 20%` = 100000；5 票合计 ≤ 500000 | **N ≤ 500000（合计）** | ✅ **最终 binder** |
> | ② 保证金额度（§5 强约束 4） | `N × m_long ≤ net_assets` | N ≤ 625000 | 被 ① 先卡住 |
> | ③ 杠杆闸（§5 强约束 5b） | `N / net_assets ≤ max_leverage` | N ≤ 1000000 | 回退保护 |
>
> **对「纯函数基准」与「引擎可达」必须分开表述。** 上表的 `N=625000` 场景是**纯函数口径**
> （直接构造 `AccountState`，绕过 `submit`/风控），用来验证 maintain ratio 的数学。
> 它**不是**引擎能建出来的仓位 —— 引擎路径下真正的 binder 是**仓位限制**。
>
> **引擎可达的穿仓场景（v3.2 新增，必测）**：5 票各建 ~10 万融资仓（贴近单票 20% 上限），
> 合计 `long_mv ≈ 493100`、`financing_debt ≈ 493100` → 开仓 `ratio ≈ 2.0137`、`leverage ≈ 0.9865`；
> **标的下跌约 71.4%** 才触及 1.30。
> 即：**受单票 20% 与持仓数 5 的集中度约束限制，实盘穿仓需要极端跌幅**——这是真实且合理的行为
> （集中度约束本来就先于杠杆约束起作用），但**必须如实表述，不能宣称"跌一半就强平"**。
>
> **v3 曾犯的错**：声称「满仓 N=1250000 / ratio=1.40 / 跌 7.7% 触及 1.30」——
> 该仓位在 v3 自身规则下 `margin_required = 1000000 > net_assets = 500000` 且 `leverage = 2.5 > 2.0`，
> **根本建不了仓**；由 plugin-author 独立发现。随后 v3.1 又误称「N=625000 引擎可达」，
> 由 verifier 实测证伪（`POSITION_LIMIT_PCT` 直接拒单）——故本条以「纯函数 vs 引擎可达」分开表述。
>
> **`max_leverage` 的真实作用**是**回退保护**：默认参数下保证金规则更紧（1.25 倍）故不生效；
> 但若把 `margin_ratio_long` 调低（例如 0.30），保证金规则放宽到 N≤1666666，此时 `max_leverage=2.0` 会接管，
> 防止「开仓即低于平仓线」。**测试必须断言这层回退**（调低 `m_long` 后 `LEVERAGE_LIMIT` 生效）。
>
> **融券容量**：由 `max_leverage`（N ≤ 1000000）与 `max_short_exposure`（融券市值 ≤ net_assets × 0.50 = 250000）共同约束，
> **不由保证金额度规则约束**（券款本身即保证金，可充抵）。
>
> **1.30 对小仓位不可达（v3.3 注）**：容量表里 `N=200000` 一行，「220%」是 `v = 1.30N − M = -240000`
> （负价）的代数根，**没有物理意义**。该仓位在 `v = 0` 时 ratio 下界为 `M/N = 2.5`，
> 即 **1.30 永不可达**。同理任何 `N ≤ M/(1.30−1) = 1666666` 的仓位都有足够缓冲。
> 这是**正常且合理**的：小仓位本来就不该被强平。

### 3.2.1 「融上再融」（额度复用）机制 —— 用户 2026-10-10 显式要求

> 用户原话：「真实投资中，如果我融资赚了，我是有额度可以继续融资的，你可别忘了。」

**机制（本模型天然支持，因为额度全部由实时 `net_assets` 派生，不用静态值）**：

| 量 | 公式 | 浮盈时变化 |
|---|---|---|
| 单票上限 | `max_position_pct × net_assets` | **随浮盈上升** ↑ |
| 持仓数上限 | `max_positions` | 不变 |
| 保证金额度 | `margin_required ≤ net_assets` | **随浮盈上升** ↑ |
| 可再融资额度 | `margin_available = net_assets − margin_required` | **随浮盈上升** ↑ |
| 杠杆上限 | `total_debt ≤ max_leverage × net_assets` | **随浮盈上升** ↑ |

**四条硬规则**
1. **必须用实时 `net_assets` 计算额度，禁止缓存/静态化开仓额度**（这会让「融上再融」失效）。
2. **浮动盈利只放大额度，不产生可支用现金**：浮盈**不得**计入 `available_cash`。
   所以「浮盈 → 释放额度 → 再融资买入」是**可能的**；「浮盈 → 直接取现买别的」是**不可能的**。
3. 释放出的额度仍受 **①仓位限制 → ②保证金额度 → ③杠杆闸** 三层约束（默认参数下通常是 ① 先卡住）。
4. **利息按日计提**：融资负债越滚越大时 `interest_accrued` 同步增长，会**侵蚀**净资产，
   从而**反向压缩**额度。这是"融上再融"的风险面，必须可见。

**工作算例（零费用）**：50 万本金、`m_long=0.80`、融资买入 10 万（`N=100000`）

| 步骤 | available_cash | financing_debt | long_mv | net_assets | 单票上限<br>(20%×net) | margin_required<br>(=N×m) | margin_available<br>(=net−required) |
|---|---|---|---|---|---|---|---|
| 开仓后 | **500000** | 100000 | 100000 | **500000**（=本金，满足 I1） | **100000** | 80000 | **420000** |
| 标的涨 60% | **500000** | 100000 | 160000 | **560000** | **112000** | 80000 | **480000** |

→ 净资产从 500000 升到 **560000**（浮盈 60000），单票上限 100000 → **112000**、可再融资 420000 → **480000**；
**无需追加保证金即可提高融资规模** —— 这就是「融上再融」。

> **常见误算（务必避免）**：不要把 `net_assets` 当成 `net_assets − margin_required`。
> 上例中 `net_assets = 500000`（不是 420000）；`420000` 是 **`margin_available`**。
> 若先把它当净资产（得 420000），再减一次 `margin_required`（得 340000），就是**双重扣减**，
> 会同时违反 §3.2 的 I1、§3.3 的融资买入行（`available_cash -= F`，自有资金不出账）与 INV-1/INV-6。

**规则 2 的执行细节（防误读）**：`available_cash` 在全仓只有两个变更点 ——
① 成交现金流 `cash_delta`；② `settle_t1` 解冻 `frozen_t1`。
`realized_pnl` 是**纯审计字段**，任何路径都不得把它加进现金。
唯一「盈利变现金」的合法路径是**真实卖出**（所得进 `frozen_t1` → 次日转 `available_cash`）——
那是**已实现**收益，规则 2 限制的是**浮动**盈利。

**可提取额度（只读，v3.3）**：`maintenance_ratio >= max_maintenance_ratio`（3.00）时，
`risk_report()` 产出 `MAINTENANCE_EXTRACTABLE`（level=info）并给出可提取金额读数。
**本版本不实现「资金划出」交易**：需要模拟提取时，用 `init --reset` 或直接调整 `config.json` 的 `initial_cash`
（详情见 §11 L11）。
>
> 变更日志见文末 §10。**T1/T2/T3 必须按 v3.1 实现。**

---

## 0. 环境与运行约定

- Python **3.14.3**；包根目录 `D:\Ai投资`；包名 `ptrade`。
- 运行：`python -m ptrade <cmd>`（cwd = `D:\Ai投资`）。
- 依赖：`pandas`、`akshare`（数据层）；`pytest`（测试）。**不得引入其他第三方依赖**（不引入 pydantic/click/rich）。
- 所有文件 **UTF-8**。写文件与运行命令一律设 `PYTHONUTF8=1`（中文 Windows 必需）。
- 货币单位：**元**（float）。股数：**股**（int，100 股整数倍）。
- 日期：`datetime.date`；字符串形式一律 `YYYY-MM-DD`。持久化日期的键名 `trade_date`。

---

## 1. 目录与写入范围（并行分工边界）

```
D:\Ai投资\
  pyproject.toml
  README.md
  INTERFACES.md                     # 本文件（Lead 独占）
  ptrade\
    __init__.py                     # [T1]
    __main__.py                     # [T1]
    config.py                       # [T1]
    core.py                         # [T1]  领域模型 + 账户数学（纯函数，无 IO）
    datasource.py                   # [T1]  akshare 适配 + 缓存 + 夹具
    engine.py                       # [T2]  T+1 / 两融 / 风控 / 下单 / 建仓建议
    strategy.py                     # [T3]  CANSLIM 七因子打分 + 信号
    cli.py                          # [T1]  命令行入口
  tests\
    __init__.py
    conftest.py                     # [T1]  夹具构造
    test_core.py                    # [T1]
    test_datasource.py              # [T1]
    test_engine.py                  # [T2]
    test_strategy.py                # [T3]
    test_cli.py                     # [T1]
  data\
    cache\                          # 运行时缓存（不入库）
    fixture\                        # 离线夹具（入库）
```

**写入范围互斥**：`[T1]` `[T2]` `[T3]` 各自只能写自己标记的文件。`data/fixture/*` 由 T1 独占生成。

---

## 2. `ptrade/config.py` — `[T1]`

```python
@dataclass(frozen=True)
class FeeConfig:
    commission_rate: float = 0.00025   # 佣金费率（双边）
    commission_min: float = 5.0        # 单笔最低佣金
    stamp_tax_rate: float = 0.0005     # 印花税（仅卖出）
    transfer_fee_rate: float = 0.00001 # 过户费（沪深双边）
    slippage_bps: float = 5.0          # 市价单滑点（基点，1bp = 0.01%）

@dataclass(frozen=True)
class MarginConfig:
    min_maintenance_ratio: float = 1.30   # 平仓线（130%，交易所口径）
    warn_maintenance_ratio: float = 1.50  # 警戒线（150%）
    max_maintenance_ratio: float = 3.00   # 可提取线（300%）
    margin_ratio_long: float = 0.80       # 融资保证金比例：买入金额中自有资金占比 >= 此值
    margin_ratio_short: float = 1.00      # 融券保证金比例：卖出金额中自有资金占比 >= 此值
    financing_rate_annual: float = 0.065  # 融资年化利率
    lending_rate_annual: float = 0.080    # 融券年化费率
    short_source_limit_ratio: float = 0.02  # 单票可融券量 <= 该股流通股本的 2%（券源约束）

@dataclass(frozen=True)
class RiskConfig:
    max_position_pct: float = 0.20      # 单票市值（多头+融券）/ 净资产（**非总资产**，见 §5 强约束 6）
    max_positions: int = 5              # 同时持仓标的数
    stop_loss_pct: float = -0.07        # CANSLIM 止损线（-7%，相对成本价）
    take_profit_pct: float = 0.25       # 止盈提示线
    max_gross_exposure: float = 2.0     # 总敞口（多头市值+融券市值）/ 净资产
    max_short_exposure: float = 0.50    # 融券市值 / 净资产
    max_leverage: float = 2.0           # 【v3 新增】总负债 / 净资产 上限；防止「开仓即低于平仓线」

@dataclass(frozen=True)
class CANSLIMConfig:
    # 七因子权重，和 = 1.0
    weight_c: float = 0.18   # 当季 EPS 增长
    weight_a: float = 0.16   # 年度 EPS 增长
    weight_n: float = 0.14   # 新产品/新管理层/新高
    weight_s: float = 0.12   # 供给（流通盘 + 量能）
    weight_l: float = 0.16   # 相对强度（龙头/落后）
    weight_i: float = 0.12   # 机构认同度
    weight_m: float = 0.12   # 市场方向
    buy_threshold: float = 75.0     # 买入候选门槛（0-100）
    short_threshold: float = 40.0   # 做空候选门槛（<= 此值）
    c_accel_bonus: float = 1.25     # 增长加速加成倍数（叠加在基础得分上）

@dataclass(frozen=True)
class AccountConfig:
    initial_cash: float = 500000.0

@dataclass(frozen=True)
class AppConfig:
    account: AccountConfig
    fee: FeeConfig
    margin: MarginConfig
    risk: RiskConfig
    canslim: CANSLIMConfig
    root: str          # 数据根目录（绝对路径）

def load_config(root: str | None = None) -> AppConfig: ...
def save_default_config(path: str) -> AppConfig: ...
```

- `root` 默认 = 包的上两级目录（即 `D:\Ai投资`）。
- 配置文件：`<root>/config.json`，存在则覆盖默认值，缺失字段用默认值。

---

## 3. `ptrade/core.py` — `[T1]`（纯函数 + 领域模型，**不做 IO**）

### 3.1 枚举与错误码

```python
class Side(str, Enum):
    BUY = "buy"      # 普通买入（自有资金）
    SELL = "sell"    # 普通卖出（卖出持仓）
    MARGIN_BUY = "margin_buy"      # 融资买入（做多，产生负债）
    SHORT_SELL = "short_sell"      # 融券卖出（做空开仓）
    BUY_TO_COVER = "buy_to_cover"  # 买券还券（做空平仓）
    REPAY_MARGIN = "repay_margin"  # 卖券还款 / 直接还款
```

```python
class ErrorCode(str, Enum):
    OK = "OK"
    T1_NOT_SETTLED = "T1_NOT_SETTLED"                  # T+1 未解冻
    INSUFFICIENT_CASH = "INSUFFICIENT_CASH"
    INSUFFICIENT_POSITION = "INSUFFICIENT_POSITION"
    NOT_MARGIN_TARGET = "NOT_MARGIN_TARGET"            # 非两融标的
    NOT_SHORT_TARGET = "NOT_SHORT_TARGET"              # 非融券标的（融资标的但不可融券）
    INSUFFICIENT_MARGIN_SPACE = "INSUFFICIENT_MARGIN_SPACE"  # 保证金空间不足
    LEVERAGE_LIMIT = "LEVERAGE_LIMIT"                  # 【v3】杠杆超限 / 开仓后维持担保比例低于平仓线
    MARGIN_CALL_BLOCKED = "MARGIN_CALL_BLOCKED"        # 处于追保状态，禁止加仓
    BELOW_MAINTENANCE = "BELOW_MAINTENANCE"            # 触发平仓线，仅允许减仓
    POSITION_LIMIT_PCT = "POSITION_LIMIT_PCT"          # 单票仓位超限
    POSITION_LIMIT_COUNT = "POSITION_LIMIT_COUNT"      # 持仓数超限
    EXPOSURE_LIMIT = "EXPOSURE_LIMIT"                  # 总敞口/空头敞口超限
    SHORT_SOURCE_LIMIT = "SHORT_SOURCE_LIMIT"          # 券源不足（超流通股本比例上限）
    LIMIT_UP_NO_BUY = "LIMIT_UP_NO_BUY"                # 涨停不可买
    LIMIT_DOWN_NO_SELL = "LIMIT_DOWN_NO_SELL"          # 跌停不可卖
    NO_PRICE = "NO_PRICE"
    NO_MARKET_DATA = "NO_MARKET_DATA"
    INVALID_LOT = "INVALID_LOT"                        # 非 100 股整数倍
    SUSPENDED = "SUSPENDED"                            # 停牌/无行情
    ALREADY_EXISTS = "ALREADY_EXISTS"
    NOT_FOUND = "NOT_FOUND"
    UNSUPPORTED = "UNSUPPORTED"
```

### 3.2 数据模型

```python
@dataclass
class Lot:
    """持仓批次。买入/融券开仓各生成一条。"""
    lot_id: str                 # 例如 "L0001"
    code: str
    open_date: str              # YYYY-MM-DD
    unlock_date: str            # T+1 解冻交易日；open_date 当日 locked
    qty: int
    available_qty: int          # 可卖数量（T+1 后 = qty）
    cost_price: float           # 含费用的摊薄成本
    side: str                   # "long" | "short"
    kind: str                   # "cash"（自有）| "margin"（融资）| "short"（融券）
    last_price: float

@dataclass
class Position:
    code: str
    name: str
    long_qty: int
    long_available: int
    short_qty: int              # 融券未还数量
    short_available: int        # 可买券还券数量（T+1）
    long_cost: float            # 多头摊薄成本
    short_cost: float           # 空头开仓均价
    last_price: float
    lots: list[Lot]

    # 派生属性
    @property
    def long_value(self) -> float: ...      # long_qty * last_price
    @property
    def short_value(self) -> float: ...     # short_qty * last_price（市值口径）
    @property
    def long_pnl(self) -> float: ...
    @property
    def short_pnl(self) -> float: ...       # (short_cost - last_price) * short_qty

@dataclass
class Order:
    order_id: str
    ts: str                 # ISO8601
    trade_date: str
    code: str
    name: str
    side: str               # Side 的值
    qty: int
    price: float
    status: str             # "filled" | "rejected"
    error_code: str         # ErrorCode 值
    reason: str
    fee: float
    tax: float
    transfer_fee: float
    cash_delta: float       # 对 available_cash 的影响
    margin_used: float      # 本笔占用保证金（自有资金部分）
    debt_delta: float       # 对总负债的影响
    realized_pnl: float     # 平仓本笔实现盈亏

@dataclass
class AccountState:
    trade_date: str
    initial_cash: float
    available_cash: float       # 账户可用现金（信用账户内未买入的现金也在此）
    frozen_cash: float          # 冻结资金总额 = frozen_t1 + frozen_collateral（派生见下）
    frozen_t1: float            # 【T+1 待交收】普通卖出的待交收款；次日 settle_t1 转入 available_cash
    frozen_collateral: float    # 【融券担保】融券卖出券款；**不随交易日解冻**，仅买券还券时释放
    financing_debt: float       # 融资负债 = Σ 融资买入金额 N（**全额**，交易所口径）+ 已资本化利息
    short_cost_debt: dict[str, float]   # 融券负债（成本口径，按 code）：开仓时锁定，还券时清零
    short_qty_debt: dict[str, int]      # 融券欠券数量（按 code）
    interest_accrued: float     # 累计利息与融券费用（未资本化部分，计入负债）
    positions: dict[str, Position]
    last_prices: dict[str, float]
    margin_call: bool           # 是否触及警戒线
    forced_liquidation: bool    # 是否触及平仓线
    history: list[Order]

    # ---- 派生（账户数学，纯函数）----
    @property
    def long_market_value(self) -> float:
        """Σ position.long_value"""

    @property
    def short_market_value(self) -> float:
        """Σ position.short_value（市值口径，用于负债与维持担保比例分母）"""

    @property
    def total_assets(self) -> float:
        """total_assets = available_cash + frozen_t1 + frozen_collateral + long_market_value
        【会计恒等式口径】= 负债 + 净资产。注意它**不是**维持担保比例的分子。"""

    @property
    def collateral_value(self) -> float:
        """【v3.1】担保物价值 = available_cash + frozen_collateral + long_market_value
        含融券券款（深交所投教：融券卖出所得资金属可充抵保证金的资金），
        但**不含 `frozen_t1`**（T+1 待交收卖出款不是保证金资金）。"""

    @property
    def total_debt(self) -> float:
        """total_debt = financing_debt + short_market_value + interest_accrued
        【v3】financing_debt 用**全额**口径；融券负债用**市值**口径。"""

    @property
    def net_assets(self) -> float:      # total_assets - total_debt
    @property
    def maintenance_ratio(self) -> float:
        """【v3.1，交易所口径】维持担保比例 = collateral_value / total_debt
        = (available_cash + frozen_collateral + long_market_value)
          / (financing_debt + short_market_value + interest_accrued)
        分母 <= 0 时返回 math.inf。
        **不得**把 `frozen_t1` 计入分子。

        【v3.3 边界语义冻结】判据一律为**严格小于**（对齐交易所「低于 130%」措辞）：
          ratio < 1.30 → forced_liquidation
          ratio < 1.50 → margin_call（且未 forced）
          ratio >= 3.00 → 可提取额度（MAINTENANCE_EXTRACTABLE，只读提示）
        **恰好等于 1.30 时不算触发**（`812500/625000 == 1.3` → `margin_call`，不是 forced）。
        """
    @property
    def leverage(self) -> float:
        """【v3】总负债 / 净资产。开仓后不得超过 RiskConfig.max_leverage（默认 2.0）。"""
    @property
    def margin_required(self) -> float:
        """当前已占用自有保证金：
        融资部分 = Σ 各笔融资买入金额 * margin_ratio_long
        融券部分 = max(0, Σ short_market_value * margin_ratio_short - frozen_collateral)
        【仅作为开仓额度校验指标，不参与 total_assets / 维持担保比例计算】"""
    @property
    def margin_available(self) -> float:
        """margin_available = net_assets - margin_required"""
    @property
    def total_pnl(self) -> float:       # net_assets - initial_cash
```

**四条不变量（必须写成测试，任何记账变更都不得破坏）**

| 编号 | 不变量 | 含义 |
|---|---|---|
| I1 | 零费用口径下，开仓瞬间 `net_assets == initial_cash`（容差 1e-6） | 开仓不产生盈亏（有费时 `net_assets == initial_cash - F`） |
| I2 | 价格变动后 `net_assets - initial_cash == 多头浮盈 + 空头浮盈 - 累计费用 - 累计利息` | 盈亏只来自价格、费用与利息 |
| I3 | `maintenance_ratio == collateral_value / total_debt`，且与**交易所公开口径**一致 | 分子 = 可用现金 + 融券冻结券款 + 多头市值（**不含 `frozen_t1`**），分母 = 融资全额负债 + 融券市值 + 利息 |
| **I4** | **开仓后 `maintenance_ratio >= min_maintenance_ratio`（默认 1.30）**，否则拒单 | 禁止「开仓即低于平仓线」；由 §5 强约束 5b 的 `max_leverage` 回退保证（正常参数下保证金额度规则更紧） |

**v3.1 数值基准（测试直接断言，容差 1e-6；统一【零费用口径】，`F = 0`）**

> **口径声明（v3.2 修正）**：本表**全部**为零费用口径。费用的正确性由**独立的费用用例**验证
> （`calc_fees` 的佣金/印花税/过户费分项 + 三类开仓各自扣 `F`），**不与本表混用**。
> v3.1 曾在前两行用零费用、后两行用含费用（`499848 = 500000 − 152`），导致 INV-4「逐格 1e-6」必然失败
> —— 由 verifier 在对抗 QA 中实测发现。

| 场景 | available_cash | frozen_collateral | long_mv | financing_debt | 担保物 | 负债 | net_assets | maintenance_ratio |
|---|---|---|---|---|---|---|---|---|
| 初始（50 万，无仓） | 500000 | 0 | 0 | 0 | 500000 | 0 | 500000 | `inf` |
| 融资买入 20 万（m=0.80） | 500000 | 0 | 200000 | 200000 | 700000 | 200000 | 500000 | **3.5** |
| 同上，票跌 20% | 500000 | 0 | 160000 | 200000 | 660000 | 200000 | 460000 | **3.3** |
| 融资满额 N=625000（纯函数口径） | 500000 | 0 | 625000 | 625000 | 1125000 | 625000 | 500000 | **1.8** |
| 同上，票跌 50% | 500000 | 0 | 312500 | 625000 | 812500 | 625000 | 187500 | **1.300（临界，未触发）** |
| 同上，票跌 **50.1%** | 500000 | 0 | 311875 | 625000 | 811875 | 625000 | 186875 | **1.299（触发 forced）** |
| 同上，票跌 60% | 500000 | 0 | 250000 | 625000 | 750000 | 625000 | 125000 | **1.20** |
| 融券卖出 20 万（m=1.00） | 500000 | 200000 | 0 | 0 | 700000 | 200000 | 500000 | **3.5** |
| 同上，券跌 20% | 500000 | 200000 | 0 | 0 | 700000 | 160000 | 540000 | **4.375** |

> **含费用对照（单独用例，输入 `F` 已在表内）**：
> - 融资买入 20 万：`F = 52`（佣金 50 + 过户费 2，无印花税）→ `available_cash = 499948`、担保物 `699948`、`ratio = 3.49974`
> - 融券卖出 20 万：`F = 152`（佣金 50 + 印花税 100 + 过户费 2）→ `available_cash = 499848`、担保物 `699848`、`ratio = 3.49924`
> - 券跌 20%：负债 `160000`，`ratio = 4.37405`
>
> `frozen_cash` 是派生量：`frozen_cash = frozen_t1 + frozen_collateral`。

### 3.3 纯函数（**无 IO，必须可单测**）

```python
def round_lot(qty: int) -> int:
    """向下取整到 100 股。"""

def calc_fees(side: str, price: float, qty: int, fee: FeeConfig,
              transfer_fee: bool = True) -> tuple[float, float, float]:
    """返回 (commission, stamp_tax, transfer_fee)。
    - commission = max(price*qty*commission_rate, commission_min)；仅在 amount > 0 时收
    - stamp_tax  = price*qty*stamp_tax_rate，仅 SELL / BUY_TO_COVER 之外的卖出方向收；
                   具体：卖出方向集合 = {SELL, SHORT_SELL, MARGIN_BUY(否)}
                   → SELL 收；SHORT_SELL 收；BUY_TO_COVER 不收；买入类不收
    - transfer_fee = price*qty*transfer_fee_rate（transfer_fee=True 时）
    """

def apply_fill(state: AccountState, order: Order) -> AccountState:
    """把已成交订单应用到账户（返回新状态，不修改入参）。"""

def accrue_interest(state: AccountState, margin: MarginConfig, days: int = 1) -> AccountState:
    """按日计提融资利息 + 融券费用，累加到 interest_accrued。
    融资利息 = financing_debt × financing_rate_annual / 365 × days
    融券费用 = **short_market_value**（市值口径，不是 short_cost_debt）× lending_rate_annual / 365 × days
    【v3.1 口径冻结】融券费用基数用市值口径，与负债口径一致。
    """

def mark_to_market(state: AccountState, prices: dict[str, float]) -> AccountState:
    """按最新价刷新持仓 last_price / last_prices。
    【v3.4 修正】`short_market_value` 是派生属性（`short_qty × last_price`），无需重算；
    v2 已删字段 `lending_debt_value` 不再存在。"""

def settle_t1(state: AccountState, new_trade_date: str) -> AccountState:
    """把 unlock_date <= new_trade_date 的批次置为可卖；
    **只把 frozen_t1 转回 available_cash**（绝不触碰 frozen_collateral，融券券款必须锁到买券还券）；
    更新 trade_date。"""

def check_risk(state: AccountState, cfg: RiskConfig, margin: MarginConfig) -> list[dict]:
    """返回告警列表 [{"level": "warn"|"critical", "code": "...", "message": "..."}]。
    必含（v3.1 冻结 code 集合）：
      - "MAINTENANCE_FORCED"     level=critical  maintenance_ratio < margin.min_maintenance_ratio（1.30）
      - "MAINTENANCE_WARN"       level=warn      maintenance_ratio < margin.warn_maintenance_ratio（1.50）
      - "MAINTENANCE_EXTRACTABLE" level=info     maintenance_ratio >= margin.max_maintenance_ratio（3.00）→ 可提取
        【v3.1 新增】让 max_maintenance_ratio 从死配置变为真实告警，否则该字段永不生效
      - "POSITION_LIMIT_PCT"     level=warn      单票市值 > max_position_pct * net_assets
      - "POSITION_LIMIT_COUNT"   level=warn      持仓数 > max_positions
      - "EXPOSURE_LIMIT"         level=warn      总敞口 > max_gross_exposure * net_assets
      - "SHORT_EXPOSURE_LIMIT"   level=warn      融券市值 > max_short_exposure * net_assets
      - "STOP_LOSS"              level=critical  任一多头浮亏 <= stop_loss_pct
      - "TAKE_PROFIT"            level=info      任一多头浮盈 >= take_profit_pct
    `risk_report().status` 的取值由 critical 告警决定：有 MAINTENANCE_FORCED → "forced"；
    有 MAINTENANCE_WARN（且无 FORCED）→ "margin_call"；否则 "safe"。"""

def new_state(cfg: AppConfig) -> AccountState: ...
def next_order_id(state: AccountState) -> str: ...
def next_lot_id(state: AccountState) -> str: ...
def snapshot(state: AccountState) -> dict: ...      # CLI/持久化用
def restore(d: dict, cfg: AppConfig) -> AccountState:
    """反序列化。**v3.1：必须接收 cfg 并按 cfg 校验/绑定**，不得静默使用默认参数
    （否则自定义 margin_ratio_long 下的 margin_required 会算错 —— plugin-author A8）。"""
```

**账户数学约定（v2，必须严格遵守，测试会逐条验证）**

> 设 `N` = 成交金额（`price × qty`），`F` = 该笔费用合计，`m_long` = `margin_ratio_long`，`m_short` = `margin_ratio_short`。

| 操作 | available_cash | frozen_t1 | frozen_collateral | financing_debt | short_qty_debt | 备注 |
|---|---|---|---|---|---|---|
| 普通买入 | `-(N + F)` | 0 | 0 | 0 | 0 | 生成 `kind="cash"` 的 long lot，当日 locked |
| 普通卖出 | 0（T+1 到账） | `+(N - F)` | 0 | 0 | 0 | 次日 `settle_t1` 转入 available_cash |
| 融资买入 | `-F` | 0 | 0 | `+N`（**全额**） | 0 | 自有资金不出账，留作担保物；生成 `kind="margin"` 的 long lot |
| 融券卖出 | `-F`（`m_short>1` 时再 `-(m_short-1)×N`） | 0 | `+N` | 0 | `+qty`（同时 `short_cost_debt[code] += N`） | 券款即保证金，全部冻结；**费用必须从 available_cash 扣**（v3 修洞） |
| 买券还券 | `-(N + F) + 该笔券款` | 0 | `-该笔券款` | 0 | `-qty`（清零时删除键） | 券款优先用于还券，不足部分从 available_cash 补 |
| 卖券还款 | `+(N - F) - repay` | 0 | 0 | `-repay` | 0 | 简化：全额还款；`repay = min(净额, financing_debt)` |

> `frozen_cash` 是**派生量**：`frozen_cash = frozen_t1 + frozen_collateral`（不单独存储，避免三者不一致）。
> **`settle_t1` 只解冻 `frozen_t1`，绝不触碰 `frozen_collateral`**——融券券款必须一直锁到买券还券为止。
> 这是 v1 未区分的第二个坑：v1 只有一个 `frozen_cash`，`settle_t1` 会连融券券款一起解冻，凭空多出可用资金。
>
> **v3 修洞（由 core-builder 独立发现）**：v2 表格把「融券卖出」的 `available_cash` 写成 `0`，
> 于是该笔费用 `F` **没有从任何桶扣减** → 融券卖出免手续费，`net_assets` 凭空多出 `F`（实测 N=200000 时 F=152）。
> 已修正为 `available_cash = -F`。融资买入同理改为只扣 `F`（自有资金不出账）。
> 对账：BUY / MARGIN_BUY / SHORT_SELL 三者都扣 `F`，口径一致。
> 融券卖出确实要收印花税（卖出方向），已在 §3.3 `calc_fees` 的卖出方向集合中。

> **v2 要点**：融券卖出款 `+N` 冻结后**同时**计入 `total_assets`（作为 `frozen_cash`）与 `total_debt`（作为 `short_market_value`），
> 开仓时 `short_market_value == short_cost_debt == N`，故 `net_assets` 不变（满足不变量 I1）。
> 价格变动后 `short_market_value` 随 `last_price` 变化，空头盈亏自然体现在 `net_assets`（满足 I2）。
>
> **`margin_required` 不参与 `total_assets`**：它只是「已占用开仓额度」指标，用于校验新开仓是否还有保证金空间。


### 3.4 持久化格式

`<root>/data/state.json`（UTF-8, `ensure_ascii=False`, `indent=2`）：

```json
{
  "schema_version": 3,
  "app": {"initial_cash": 500000.0},
  "state": { /* AccountState 的可 JSON 化形式（v3 字段） */ }
}
```

`<root>/data/ledger.jsonl`：每行一个 `Order` 的 JSON（append-only）。

> **v2 字段变更**：删除 `margin_used`、`lending_debt_value`、`lending_debt_qty`；
> 新增 `short_cost_debt: dict[str,float]`、`short_qty_debt: dict[str,int]`。
> **v3 变更**：`available_cash` 语义不变；`financing_debt` 口径改回全额；新增派生属性
> `collateral_value`、`leverage`；`RiskConfig` 新增 `max_leverage`。
> `schema_version` 从 1 升到 **3**；读到 v1/v2 文件必须抛 `ValueError`（不做静默迁移）。

---

## 4. `ptrade/datasource.py` — `[T1]`

```python
class DataSource:
    def __init__(self, cfg: AppConfig, offline: bool = False): ...

    # ---- 交易日历 ----
    def trade_dates(self) -> list[str]:
        """升序交易日列表 YYYY-MM-DD。缓存 24h。"""
    def is_trade_date(self, d: str) -> bool: ...
    def next_trade_date(self, d: str) -> str: ...
    def prev_trade_date(self, d: str) -> str: ...
    def latest_trade_date(self) -> str: ...

    # ---- 日线 ----
    def daily(self, code: str, start: str, end: str, adjust: str = "qfq") -> pd.DataFrame:
        """列：date, open, high, low, close, volume, amount, pct_chg, turnover
        统一为英文列名，date 为 YYYY-MM-DD 字符串，升序。"""
    def last_close(self, code: str, asof: str) -> float: ...

    # ---- 实时快照（用于最新价 / 涨跌停判定）----
    def spot(self, codes: list[str] | None = None) -> pd.DataFrame:
        """列：code, name, last, pct_chg, open, high, low, prev_close,
               volume, amount, turnover, pe_ttm, pb, total_mv, float_mv,
               limit_up, limit_down
        codes=None 时返回全市场（走缓存）。"""
    def quote(self, code: str, asof: str) -> dict:
        """单票快照：{"code","name","last","prev_close","limit_up","limit_down","suspended"}"""

    # ---- 两融 ----
    def margin_targets(self, asof: str | None = None) -> pd.DataFrame:
        """两融标的名单。列：code, name, can_finance(bool), can_short(bool), limit_pct(str)
        沪深合并去重。缓存 24h。接口失败时回退到最近缓存；再失败抛 DataUnavailable。"""
    def is_margin_target(self, code: str, asof=None) -> bool: ...
    def is_short_target(self, code: str, asof=None) -> bool: ...

    # ---- 基本面 ----
    def quarterly_report(self, period: str) -> pd.DataFrame:
        """业绩报表（累积口径）。period 形如 '20250630'。
        列：code, name, eps, revenue, revenue_yoy, revenue_qoq,
             net_profit, net_profit_yoy, net_profit_qoq, bps, roe,
             ocf_ps, gross_margin, industry, announce_date"""
    def fin_indicator(self, code: str, start_year: str) -> pd.DataFrame:
        """财务指标。列：date, eps, eps_deducted, bps, roe, roe_weighted,
             revenue_yoy, net_profit_yoy, gross_margin, debt_ratio, ocf_ps"""
    def holders(self, period: str) -> pd.DataFrame:
        """股东户数。列：code, name, holders, holders_prev, holders_chg_pct,
             avg_hold_value, total_mv, total_share"""
    def top_holders(self, code: str) -> pd.DataFrame:
        """十大股东/机构持股。列：code, holder_name, holder_type, shares, ratio, change"""
    def industry_of(self, code: str) -> str: ...
    def industry_constituents(self, industry: str) -> list[str]: ...

    # ---- 指数 ----
    def index_daily(self, index_code: str, start: str, end: str) -> pd.DataFrame:
        """指数日线。列：date, open, high, low, close, volume, pct_chg
        index_code 接受 'sh000001' / '000001' / 'csi931151'。"""
    def market_breadth(self, asof: str) -> dict:
        """市场方向代理指标：{"index_close","index_ma50","index_ma200",
        "above_ma50":bool,"above_ma200":bool,"pct_chg_1d","pct_chg_20d",
        "advancers":int,"decliners":int,"limit_up_count":int,"limit_down_count":int}"""

class DataUnavailable(RuntimeError): ...
```

**单位约定（v3 补充，强制；防「量纲不一致导致打分静默失效」）**

| 字段 | 单位 | 说明 |
|---|---|---|
| 所有价格类（`last`/`open`/`high`/`low`/`close`/`prev_close`/`limit_up`/`limit_down`） | **元**（float） | 不复权口径为真实价 |
| `volume` | **股** | akshare 沪深日线成交量单位就是「手」，**必须 ×100 转成股** |
| `amount` | **元** | 成交额 |
| `total_mv` / `float_mv` | **元** | 总市值 / 流通市值。**不转亿元**，由消费方自行换算 |
| `turnover` | **百分数**（如 `3.5` 表示 3.5%） | 与 akshare「换手率」列一致，**不转小数** |
| `pct_chg` | **百分数**（如 `-2.1` 表示 -2.1%） | 同上 |
| `revenue` / `net_profit` / `amount`（财务） | **元** | |
| `revenue_yoy` / `net_profit_yoy` / `net_profit_qoq` / `revenue_qoq` | **百分数**（如 `82.5` 表示 +82.5%） | |
| `roe` / `gross_margin` / `debt_ratio` | **百分数**（如 `17.3` 表示 17.3%） | 需参与打分阈值比较 |
| `eps` / `bps` / `ocf_ps` | **元/股** | |
| `holders_chg_pct` | **百分数** | 股东户数增减比例 |
| `ratio`（`top_holders`） | **百分数**（如 `12.5` 表示 12.5%） | |
| `shares`（`top_holders`） | **股** | |

> **`strategy.py` 必须按上表直接取值**，不得做「单位自适应探测」——量纲由数据层保证。
> 数据层的夹具（`data/fixture/*.json`）必须与上表一致，否则打分阈值会静默失效。

**缓存策略（强制）**
- 缓存路径：`<root>/data/cache/<key>.json`，key 形如 `daily_000001_qfq_20250801_20251009`。
- 缓存元数据：`{"fetched_at": iso, "payload": ...}`；TTL 见下表；TTL 内直接读缓存。
- **`offline=True` 时绝不联网**：只读 `data/cache` 与 `data/fixture`，缺失即抛 `DataUnavailable`。
- **测试必须 `offline=True`**。
- akshare 调用需 `time.sleep(0.3)` 节流；单接口失败重试 2 次（指数退避 1s/3s）。

| 数据 | TTL |
|---|---|
| 交易日历 | 24h |
| 日线 | 6h（收盘后当日 15:30 后失效） |
| 实时快照 | 60s |
| 两融标的 | 24h |
| 业绩报表 / 财务指标 | 24h |
| 股东户数 | 24h |
| 指数日线 | 6h |

| akshare 接口 | 用途 |
|---|---|
| `tool_trade_date_hist_sina()` | 交易日历 |
| `stock_zh_a_hist(symbol, period='daily', start_date, end_date, adjust)` | 日线 |
| `stock_zh_a_spot_em()` | 全市场快照 |
| `stock_bid_ask_em(symbol)` | 单票涨跌停/昨收 |
| `stock_margin_underlying_info_szse(date)` | 深市两融标的 |
| `stock_margin_detail_sse(date)` | 沪市两融明细（含标的代码，用于沪市白名单兜底） |
| `stock_yjbb_em(date)` | 业绩报表 |
| `stock_financial_analysis_indicator(symbol, start_year)` | 财务指标 |
| `stock_zh_a_gdhs(symbol=period)` | 股东户数 |
| `stock_main_stock_holder(stock)` | 主要股东 |
| `stock_zh_index_daily_em(symbol, start_date, end_date)` | 指数日线（**首选**） |
| `stock_zh_index_daily(symbol)` | 指数日线（**兜底**，新浪源） |
| `stock_board_industry_name_em()` / `stock_board_industry_cons_em(symbol)` | 行业归属 |

> **沪市两融白名单**：`stock_margin_detail_sse` 的「标的证券代码」即为沪市两融标的集合（含 ETF，需用股票代码前缀过滤 60/68）。此接口可用性已实测通过。

**夹具（T1 生成，供全体离线测试）**
- `data/fixture/daily_<code>.json`、`data/fixture/spot.json`、`data/fixture/trade_dates.json`、
  `data/fixture/margin_targets.json`、`data/fixture/quarterly_<period>.json`、
  `data/fixture/fin_indicator_<code>.json`、`data/fixture/holders_<period>.json`、
  `data/fixture/index_<code>.json`、`data/fixture/top_holders_<code>.json`。
- 夹具需覆盖 3 个剧情标的：
  - `600519`（样例强票：增长加速 + 相对强度高 + 机构认同）
  - `002594`（样例中性票）
  - `000001`（样例弱票 / 做空候选）
- 夹具中的两融名单必须**同时包含** `600519`、`002594`、`000001`，且另含一个 `can_short=False` 的融资标的用于负例
  （**实测修正**：`601398` 是活跃融券标的，负例改用 **`600110 诺德股份`**，理由见 §11 L13）。
- **夹具必须保持纯真实**：不得为让测试变绿而改写财务/行情数值。需要"构造剧本"时一律放 `data/scenario/`
  并带顶层 `"_synthetic": true`（**已实测隔离**：`DataSource` 默认不读，仅 `scenario=True` 时叠加）。

**错误与降级语义（v3.5 冻结，core-builder 要求落契约）**
| 情形 | 行为 |
|---|---|
| **per-code 数据集**（`daily` / `last_close` / `top_holders` / `fin_indicator` 等）离线缺夹具，**或**代码未知 | **抛 `DataUnavailable`** |
| 消费方遇到单票 `DataUnavailable` | **必须按单票失败降级**（`strategy` 已 catch → `total=0` + `risks` 标注 + `signal="avoid"`），**不得整批失败** |
| 整表型数据集（`trade_dates` / `spot` / `margin_targets`）缺失 | 抛 `DataUnavailable`（无法降级，必须报错） |

> verifier 已实测用户路径优雅降级：`scan --codes 999999 --offline` → `rc=0 / ok=true / total=0 / avoid`。

**`top_holders` 的 asof 语义（v3.5，CR-7 修复后）**
- 数据源**支持真历史期次**（实测 `stock_main_stock_holder("600519")` = **896 行 / 93 个「截至日期」**，2001-07-31 → 2026-06-30，每行带公告日期）。
- `top_holders(code, asof=None)`：取 `announce_date <= asof` 的**最新一期**；`asof=None` 取最新一期。
- 输出列必须含 **`report_date`**（该数据所属报告期）与 **`announce_date`**（真实公告日）。
- **逐行**丢弃缺期次的行并在 `meta["rows_without_period_dropped"]` 记账 —— **不得因部分行缺期次而清空整表**
  （这是 CR-17 的根因：`astype(str)` + 字符串黑名单在新 pandas 字符串 dtype 下对 NA 列失效，静默清空）。
- `change` = 本期 shares − **真实上一期同名股东** shares；源缺 shares 的行保持 `NaN`，**不得填 0**。

---

## 5. `ptrade/engine.py` — `[T2]`（只依赖 `core` + `datasource`）

```python
@dataclass
class OrderRequest:
    code: str
    side: str            # Side 值
    qty: int
    price: float | None = None   # None = 市价（用当日收盘价 ± 滑点）
    trade_date: str = ""         # 空 = 用 state.trade_date

class Engine:
    def __init__(self, cfg: AppConfig, ds: DataSource, state: AccountState): ...

    # ---- 生命周期 ----
    def roll_to(self, trade_date: str) -> AccountState:
        """推进到新交易日：结算 T+1 → 按日计息 → mark-to-market → 风险评估。
        若 trade_date 早于当前 trade_date 或不是交易日 → raise ValueError。"""

    # ---- 校验（不落单，纯校验，供 CLI dry-run 与策略复用）----
    def validate(self, req: OrderRequest) -> Order:
        """返回 status='filled' 的**预案**副本或 status='rejected'。绝不改动 state。"""

    # ---- 下单 ----
    def submit(self, req: OrderRequest) -> Order:
        """校验 → 成交（或拒单）→ apply_fill → 追加 history → 落 ledger.jsonl → 返回 Order。
        拒单也返回 Order（status='rejected'），不抛异常（除 IO 错误）。"""

    # ---- 建仓建议 ----
    def position_sizing(self, code: str, price: float, score: float) -> dict:
        """返回 {"qty", "amount", "margin_required", "binding_constraint", "reason"}
        binding_constraint ∈ {"cash","position_pct","position_count","exposure","score","ok"}"""

    def build_plan(self, recommendations: list[dict]) -> dict:
        """把 strategy 的推荐转成可执行计划：
        {"generated_for": trade_date, "actions": [{"code","name","action","side","qty",
          "price","amount","margin_required","reason","score"}], "skipped":[...], "notes":[...]}
        action ∈ {"open_long","add_long","open_short","close_long","close_short","hold","watch"}"""

    def close_position(self, code: str, trade_date: str, ratio: float = 1.0) -> list[Order]:
        """按 ratio 平掉多头与空头（先买券还券，再卖券还款/卖出）。"""

    def risk_report(self) -> dict:
        """{"maintenance_ratio", "status": "safe"|"warn"|"margin_call"|"forced",
            "alerts": [...], "exposure": {...}, "positions": [...]}"""
```

**强约束（测试逐条验证）**
1. **T+1**：`submit` 中卖出方向（`SELL` 用 `long_available`、`BUY_TO_COVER` 用 `short_available`）必须检查可用数量；当日买入的多头 `available_qty == 0` → `T1_NOT_SETTLED`。
   > **注意**：`SHORT_SELL`（融券开仓）不是"卖出持仓"，它不受 T+1 可用量约束 —— 它受**融券标的白名单**与**券源上限**约束。
   > 只有 `SELL`（卖多头）与 `BUY_TO_COVER`（买券还券）查可用量。
2. **融券白名单**：`SHORT_SELL` 必须 `ds.is_short_target(code)`，否则 `NOT_SHORT_TARGET`；`MARGIN_BUY` 必须 `ds.is_margin_target`，否则 `NOT_MARGIN_TARGET`。
3. **涨跌停**：`quote()` 给出 `limit_up`/`limit_down`；`price >= limit_up` 时买入方向拒（`LIMIT_UP_NO_BUY`），`price <= limit_down` 时卖出方向拒（`LIMIT_DOWN_NO_SELL`）。`suspended` → `SUSPENDED`。
4. **保证金额度**（v3）：开仓前计算 `margin_required_new`，若 `margin_required_new > net_assets` → `INSUFFICIENT_MARGIN_SPACE`。
   - 融资买入：`margin_required_new = 现有 margin_required + N × m_long`
   - 融券卖出：`margin_required_new = 现有 margin_required + max(0, N × m_short - N)`（券款可抵扣）
5. **现金**：`available_cash >= 现金支出` → 否则 `INSUFFICIENT_CASH`。三种开仓的现金支出：
   - 普通买入：`N + F`
   - 融资买入：`F`（自有资金不出账）
   - 融券卖出：`F`（`m_short > 1.0` 时再加 `(m_short-1)×N`）
   - **`REPAY_MARGIN`（卖券还款）只支持全额还款**（v3.1 裁剪，见 §11）：`repay = financing_debt`。
     净额不足全额时 → `INSUFFICIENT_CASH`。**本版本不实现部分还款**，避免除零与比例分摊的复杂度。
5b. **杠杆闸（v3 新增，回退保护）**：开仓后必须同时满足
   `leverage_after <= max_leverage`（默认 2.0）**且** `maintenance_ratio_after >= min_maintenance_ratio`（默认 1.30），
   否则 `LEVERAGE_LIMIT`。
   > **谁真正生效（重要，别误解）**：默认参数（50 万本金、`m_long=0.80`、`max_leverage=2.0`）下，
   > **强约束 4 的保证金额度规则先卡死**：`margin_required = N × 0.80 ≤ 500000` → **N ≤ 625000**；
   > 而杠杆闸允许 N ≤ 1000000。所以 **`N=1000000` 会被 `INSUFFICIENT_MARGIN_SPACE` 拒绝，根本轮不到 `LEVERAGE_LIMIT`**。
   > **`max_leverage` 的作用是回退保护**：若把 `margin_ratio_long` 调低（如 0.30），保证金规则放宽到 N ≤ 1666666，
   > 此时杠杆闸接管，防止「开仓即低于平仓线」（`N=1666666` 的开仓 ratio 仅 1.30 边缘，杠杆 3.33 > 2.0 → 拒）。
   > **测试必须分别断言这两层**（TC-E-33 改成：调低 `m_long` 后 `LEVERAGE_LIMIT` 生效）。
   >
   > 为什么需要杠杆闸：只校验「自付现金 ≤ 可用现金」时，理论上可建出开仓 ratio < 1.30 的仓位
   > （`N=2500000` 时开仓 ratio = 1.20）。默认参数下保证金规则已挡住这种情况，但约束不该依赖参数巧合。
6. **仓位**：单票 `long_value + short_value` 不得超过 `max_position_pct * net_assets` → `POSITION_LIMIT_PCT`。
   > **v2 裁决（Lead，2026-10-10）**：分母统一为 **`net_assets`（净资产）**，不是 `total_assets`。
   > 理由：`total_assets` 含融券券款与融资买入形成的市值，在杠杆下随开仓自动放大，
   > 用它当分母会让「单票 20%」的约束在越杠杆时越松（可自证：同一笔买入在 2 倍杠杆下占用比例减半），
   > 风控形同虚设。净资产才是真实风险资本。`config.py` 的注释必须与此一致。
7. **券源**：`SHORT_SELL` 累计欠券量不得超过流通股本 × `short_source_limit_ratio` → `SHORT_SOURCE_LIMIT`。
8. **风险状态门禁**：`forced_liquidation=True` 时只允许减仓（拒 `BUY`/`MARGIN_BUY`/`SHORT_SELL` → `BELOW_MAINTENANCE`）；`margin_call=True` 时拒加仓 → `MARGIN_CALL_BLOCKED`。
9. **手数**：`qty` 必须 > 0 且 `qty % 100 == 0`，否则 `INVALID_LOT`。
10. **排序**：同一 `submit` 内校验顺序固定 =
    `参数合法性 → 停牌 → 涨跌停 → 风险门禁 → 白名单 → 手数 → T+1 → 现金 → 保证金额度 → 杠杆/维护保证金 → 券源 → 仓位/敞口`。
    **先返回第一个命中的错误码**（测试依赖此确定性）。

> **v2 顺序说明**：`风险门禁` 前移到白名单之前（处于平仓线的账户应先报 `BELOW_MAINTENANCE` 而非 `NOT_MARGIN_TARGET`，
> 因为前者是账户状态、后者是标的状态，账户状态优先）。`T+1` 放在手数之后（手数不合法时先报 `INVALID_LOT`）。

---

## 6. `ptrade/strategy.py` — `[T3]`（只依赖 `core` + `datasource`）

```python
FACTORS = ("C", "A", "N", "S", "L", "I", "M")

@dataclass
class FactorScore:
    key: str            # "C".."M"
    score: float        # 0-100
    weight: float
    value: dict         # 原始输入值（可 JSON 化，用于解释）
    note: str           # 中文解释

@dataclass
class CanslimResult:
    code: str
    name: str
    asof: str
    total: float                 # 0-100 加权总分
    factors: list[FactorScore]
    signal: str                  # "buy" | "watch" | "avoid" | "short"
    reasons: list[str]           # 中文
    risks: list[str]

class CanslimScorer:
    def __init__(self, cfg: AppConfig, ds: DataSource, offline: bool = False): ...

    def score(self, code: str, asof: str) -> CanslimResult: ...
    def score_many(self, codes: list[str], asof: str) -> list[CanslimResult]:
        """按 total 降序。单票失败不抛异常，记入 risks 并给总分 0。"""
    def market_direction(self, asof: str) -> FactorScore:
        """M 因子共用（不随个股变化），内部对同日结果做缓存。"""

def rank(results: list[CanslimResult], cfg: AppConfig) -> dict:
    """{"buys": [...], "shorts": [...], "watch": [...], "avoid": [...],
        "top": [...], "asof": str}
    - buys:   signal=="buy" 且 total >= buy_threshold，按 total 降序
    - shorts: signal=="short" 且 total <= short_threshold，按 total 升序
    """

def recommend(results: list[CanslimResult], cfg: AppConfig) -> list[dict]:
    """产出可执行建议。每条：
    {"code","name","action","side","score","confidence","entry","stop_loss",
     "take_profit","position_pct","reasons","risks","factors":{"C":..,"A":..}}
    - action ∈ {"open_long","open_short","close","watch"}
    - side   ∈ {"long","short","none"}
    - stop_loss / take_profit 价格由 entry 与 risk 配置算出
    """
```

### 6.1 CANSLIM 七因子打分口径（**必须逐条实现，测试按此断言**）

> **⚠️ asof 纪律（v3.3 强制，防前瞻偏差）**
> 所有因子只能使用**截至 `asof` 已公开**的数据：
> 1. **财报**：只能用 `report_periods(asof)` 白名单内的报告期（按法定披露截止日过滤）。
>    例：`asof=2025-10-09` 时 2025 年报（`2025-12-31`，披露截止 2026-04-30）**不可用**。
> 2. **快照**：`spot()` 无 `asof` 语义（它返回"当下"），因此
>    **`asof < latest_trade_date` 时禁止使用 `spot()`**，S 因子改用 `daily()` 对应交易日推导；
>    缺 `turnover`/流通股本 → S=50（中性）+ `risks` 标注 `"S：历史回溯缺当日快照"`。**不得回退到 `spot()`**。
> 3. **行情**：`daily()` 的日期必须 `<= asof`。
> 4. **股东/机构**：按报告期的历史数据，不得用最新一期替代。
>
> 违反 asof 纪律属 **CRITICAL**（未来函数会让回测与历史复盘结论虚假乐观）。

> **全局不变量（v3.5 新增，由 verifier 的架构 WATCH 结论推动）**
>
> **任何返回历史序列的 `DataSource` 方法必须接受 `asof`，且不得返回晚于 `asof` 的行。**
> `spot()` 是**显式豁免项**（语义就是"当下"），但按上面第 2 条，`asof < latest_trade_date` 时**消费方禁用**它。
>
> **违反此不变量属 CRITICAL 级前视缺陷。**
>
> **背景**：本项目实测到 **4 处前视**，分属 4 个不同层面 ——
> ① 数据源方法缺 `asof`（`top_holders`）；② 数据源缺期间列（无 `report_date`）；
> ③ 夹具期次语义错位（`announce_date` 记成抓取日）；④ 实现过滤静默失效（NA 列被 `astype(str)` + 字符串黑名单误判 → 清空整表）。
> **共同点是没有任何"一次性锁全库"的守卫** —— `pytest` 在存在前视的时期**仍然全绿过**。
> 因此守卫测试（`tests/test_asof_guard.py`，插桩记录所有返回序列的 `max(date|report_date|announce_date)`）是**必需项而非可选加固**。

所有子分先归一到 **0-100**，再按权重加权；缺失数据 → 该因子 50 分（中性）并在 `risks` 标注 `"<因子>数据缺失"`。

| 因子 | 含义 | 主数据源 | 打分规则 |
|---|---|---|---|
| **C** | 当季 EPS 增长 | `quarterly_report(最近报告期)` 的 `net_profit_yoy` | `>=100%` → 100；`>=50%` → 90；`>=30%` → 80；`>=20%` → 70；`>=10%` → 55；`>=0` → 40；`<0` → 15。**加速加成**：若本季增速 > 上季增速，得分 `× c_accel_bonus`（上限 100） |
| **A** | 年度 EPS 增长 | `fin_indicator(code)` 的年度 `net_profit_yoy` / `roe` | 取最近 3 个年报：每年 `net_profit_yoy` 映射同上但阈值更宽（`>=50`→100, `>=25`→85, `>=15`→70, `>=5`→55, `>=0`→40, `<0`→20）；`roe >= 17%` 额外 +10（上限 100）；取三年均值 |
| **N** | 新产品/新管理层/新高 | `daily()` 的 250 日区间位置 + 量能 | 创新高（收盘价 ≥ 250 日最高 × 0.98）→ 100；距高点 `<=5%` → 85；`<=15%` → 65；`<=30%` → 45；否则 25。**若同时突破且成交量 > 50 日均量 ×1.5 → 取 `max(score, 90)`** |
| **S** | 供给（流通盘 + 量能） | `spot()` 的 `float_mv`、`turnover`；`holders()` 的 `holders_chg_pct` | 流通市值区间打分：`< 50 亿` → 80，`50-200 亿` → 90，`200-500 亿` → 75，`500-1000 亿` → 60，`>=1000 亿` → 45；换手率 `3%-15%` → +10；股东户数**减少** → +10（筹码集中），增加 → −10。上限 100、下限 0 |
| **L** | 龙头 / 落后（相对强度） | `daily()` 计算 RS：个股近 6 月涨幅 − 指数近 6 月涨幅 | RS 百分位在此票所属行业（`industry_constituents`）内排名：前 10% → 100，前 25% → 85，前 50% → 65，前 75% → 45，后 25% → 25。**行业可比较对手（可算 RS 的成分股数 − 自身）< 10 时，强制降级为绝对 RS 分档**（`>30%`→90, `>15%`→75, `>0`→60, `>-15%`→40, 否则 20）；行业取不到时同样走绝对分档。<br>**本分值是「板块内相对强度」，不等于绝对强度** —— `buy` 的绝对强度由 §6.2 的 `abs_rs > 0` 独立前置把守。 |
| **I** | 机构认同度 | `top_holders()` 的机构持股比例合计与变化 | 机构合计持股 `10%-60%` → 85；`60%-80%` → 70；`>80%` → 55（过挤）；`3%-10%` → 60；`<3%` → 35；机构数/持股**环比增加** → +10，减少 → −10。上限 100、下限 0 |
| **M** | 市场方向 | `market_breadth()` | 指数在 50 日线上 +20、200 日线上 +20；指数 20 日涨幅 `>5%` → +25，`>0` → +15，`>-5%` → +5，否则 0；涨跌家数比 `>2` → +20，`>1` → +12，`>=0.5` → +5，否则 0；涨停家数 `>50` → +15，`>=20` → +8。基础 20 分，上限 100 |

### 6.2 信号判定（**顺序敏感，先命中先返回**）

```
1. total <= short_threshold                                     → "short"
2. total >= buy_threshold 且 M >= 50 且 L >= 60 且 abs_rs > 0    → "buy"
3. total >= buy_threshold 但 (M < 50 或 L < 60 或 abs_rs <= 0)   → "watch"（原因写入 reasons）
4. total >= 50                                                  → "watch"
5. 其他                                                          → "avoid"
```

> **v3.5 修订（L 因子 CR-16 裁决联动，由 verifier 提出、Lead 采纳）**
>
> L 因子新增**绝对强度前置** `abs_rs > 0`（`abs_rs` = 个股近 6 月涨幅 − 指数近 6 月涨幅，**严格 `> 0`，不加 epsilon**）。
> 含义：`L >= 60` 原本只表示"**板块内**相对强"（弱行业里更弱的对手也能把你抬上去），
> 加上 `abs_rs > 0` 后，`buy` 的前置条件才真正等于"**绝对跑赢指数**"。
>
> **分支 3 必须同步扩展为三条件或**，否则该输入会掉进分支 4：
> 信号**碰巧仍是 `watch`**，但 `reasons` 会缺失、且「先命中先返回」不再穷尽 —— **这属于契约不一致，verifier 会 BLOCK。**
>
> **判定口径（三处同源，不得各自解释）**
> 1. **`abs_rs` 取不到** → **fail-closed**：不得判 `buy`，须落分支 3 → `watch`，且 `reasons` 标注原因。
> 2. **`abs_rs` 必须 ≤ `asof`**（遵守 §6.1 asof 纪律，不得用未来价）。
> 3. **L 的分值仍严格等于 §6.1 表格值**（`abs_rs` 是**独立前置**，**不钳制** L 分数）——
>    否则 `L` 就不再等于契约冻结的分档，测试无法对照。
>
> **相关子裁决：行业样本门槛**
> - 行业可算 RS 的成分股 **< 10** → L 因子降级为**绝对 RS 分档**。
> - `abs_rs` 恰好 `== 0` → 不通过前置（`abs_rs <= 0`）。
> - 小行业降级路径下，`abs_rs ∈ (0, 15%]` 时 L 恒为 **60**，**恰好压在硬门槛上** ——
>   必须写**显式边界测试**锁住（`abs_rs = 0.0001` 通过前置、`abs_rs = 0` 不通过）。

`reasons` / `risks` 必须是**中文**且引用具体数值（例如 `"当季净利润同比 +82.5%，且较上季 +31.0% 加速"`）。

---

## 7. `ptrade/cli.py` — `[T1]`

```
python -m ptrade init [--cash 500000] [--reset]
python -m ptrade status [--json]
python -m ptrade sync --codes 600519,002594 [--start 2025-04-01] [--end 2025-10-09] [--offline]
python -m ptrade scan --codes 600519,002594,000001 [--asof 2025-10-09] [--offline] [--json]
python -m ptrade recommend --codes ... [--asof ...] [--offline] [--json]
python -m ptrade plan --codes ... [--asof ...] [--offline] [--json]
python -m ptrade order --code 600519 --side buy --qty 100 [--price 1500] [--trade-date 2025-10-09] [--offline] [--json]
python -m ptrade settle --trade-date 2025-10-10 [--json]
python -m ptrade risk [--json]
python -m ptrade close --code 600519 [--ratio 1.0] [--trade-date ...] [--json]
python -m ptrade ledger [--limit 20] [--json]
python -m ptrade chart --code 600519 [--period daily|weekly|monthly] [--asof 2025-10-09] [--out <path>] [--offline] [--json]
```

- **`chart`（v3.1 新增，G7）**：渲染**离线自包含单文件 HTML**（K线 + MA12/24/48/96 + BOLL + MACD + SAR + 成交量/成交额，日/周/月线页内切换，含 CANSLIM 分析区）。
  - 调 `ptrade.chart.build_chart(cfg, ds, code, asof, period="daily", out=None, use_qfq=True, analysis=None) -> str`（返回 HTML 绝对路径）
  - 默认输出 `<root>/reports/chart_<code>_<asof>.html`
  - `--json` → `{"path": "<abs>", "code": ..., "asof": ..., "period": ...}`
  - `chart.py` 未就绪时返回 `ok=false` + 明确 error，**不得崩栈**
  - 依赖限制不变：只允许 `pandas / akshare / pytest`；ECharts 作为 vendored 静态资源内联，不计入 Python 依赖

- `--offline` 必须透传到 `DataSource(offline=True)`；**任何命令在 `--offline` 下不得联网**。
- `--json` 输出 `{"ok": bool, "command": str, "data": {...}, "error": str|null}` 到 stdout；**退出码 0/1**（业务拒单不算错误：`ok=true` 且 `data.orders[].status == "rejected"`）。
- 无 `--json` 时输出中文可读表格。
- 全局 `--root <path>` 可覆盖数据根目录（测试用）。
- `--asof` 缺省 = 最近交易日。
- **`--asof` 只允许已收盘的交易日（v3.1，防未来函数）**：若 `--asof` 等于当前日历交易日且当前时间早于 **15:30**，
  则该日的日线数据不完整 → 命令必须报错（`NO_MARKET_DATA`，附说明「当日尚未收盘」），
  或自动回退到**前一交易日**并在输出中明确标注 `asof` 实际取值。**禁止**用未收盘的当日数据产出信号。
- **复权口径在一个命令内不得混用**：同一输出里的价格序列必须来自同一 `adjust` 口径，并在输出中标注是 `qfq` 还是不复权。

---

## 8. 测试规格 — 全覆盖

- `tests/conftest.py`：`offline_cfg`（tmp_path 为 root，预置夹具）、`seeded_account`、`engine_offline`、`scorer_offline`。**禁止联网**。
- 每个模块的正例 + 负例，逐条对应本文件的 ErrorCode 与账户数学表。
- T+1 双向验证：当日买入当日卖 → `T1_NOT_SETTLED`；`settle` 到次一交易日 → 卖出成功。
- 两融负例：非融券标的 `SHORT_SELL` → `NOT_SHORT_TARGET`；非两融标的 `MARGIN_BUY` → `NOT_MARGIN_TARGET`。
- 维持担保比例：构造穿仓场景断言 `maintenance_ratio` 数值与 `risk_report().status`。
- CANSLIM：用夹具构造强票/弱票，断言 `signal`、`total` 落在预期区间、`factors` 七项齐全且各 key 唯一。
- CLI：`subprocess` 调 `python -m ptrade ... --offline --json` 断言退出码与 JSON 结构。

### 8.1 必测项（不变量）—— v3.4 权威版

> **修订说明**：本表原为 v2 残留（INV-3/INV-6 与 §3.2 直接矛盾），已由 core-builder 在 task-1 收尾时上报、Lead 于 v3.4 修订。
> 完整版（含 INV-7..INV-23 与对应测试用例）见 `.omx/plans/test-spec-canslim-mock-trading.md` §3.3b。

| 编号 | 测试 | 断言 |
|---|---|---|
| INV-1 | 六种交易逐一开仓后净资产守恒；**零费用口径** `net_assets == initial_cash`；**有费用口径** `net_assets == initial_cash - F` | 见 §3.3 I1；容差 1e-6 |
| INV-2 | 开仓后价格 ±20%：`net_assets - initial_cash == 多头浮盈 + 空头浮盈 - 累计费用 - 累计利息` | 见 §3.3 I2 |
| INV-3 | **`maintenance_ratio == collateral_value / total_debt`**，其中 `collateral_value == available_cash + frozen_collateral + long_market_value`（**仅排除 `frozen_t1`**） | 见 §3.3 I3；容差 1e-9 |
| INV-4 | §3.2 **v3.4 数值基准表 8 行逐格** + 含费用对照 3 行（**两表不得混用口径**） | 容差 1e-6 |
| INV-5 | `margin_required` 不参与 `total_assets` / 维持担保比例（清零后两者不变） | 防 v1 回归 |
| INV-6 | **融资负债全额口径**：融资买入 20 万（m=0.80）→ `financing_debt == 200000`，**不是** 40000 | 防 v2 回归 |
| INV-7 | **v2 虚高不可回归**：融资 20 万场景 `maintenance_ratio == 3.5`，**不是** 13.5 | 防 v2 回归 |
| INV-8 | **融券券款不可解冻**：`settle_t1` 跨日后 `available_cash` 只受计息影响、`frozen_collateral` 与 `short_qty_debt` 不变 | 防 v1 `frozen_cash` 整体解冻回归 |
| INV-9 | **普通卖出款可解冻**：跨日后 `frozen_t1` 归零转入 `available_cash`，`frozen_collateral` 不受影响 | T+1 双向 |
| INV-10 | 同日两类卖出 → `frozen_cash == frozen_t1 + frozen_collateral`，次日仅 `frozen_t1` 解冻 | 派生量一致性 |
| INV-11 | **平仓线可达性**：融资满额 `N=625000` 开仓 `1.80`；跌 **50%** → `1.30`；跌 60% → `1.20` | 防 v2「不可达」回归 |
| INV-12 | **两层约束各自生效**：`N=2500000` → `INSUFFICIENT_MARGIN_SPACE`；调低 `m_long=0.30` → `LEVERAGE_LIMIT` | `max_leverage` 是真实回退保护 |
| INV-13 | `leverage == total_debt / net_assets` | 派生属性一致性 |
| INV-14 | **M 因子缺失不得放行买入**：M 缺失（=50）时即使 `total >= 75` 也须 `watch` + risks 说明 | 防「数据故障反而放行」 |
| INV-15 | **融券费用基数** = `short_market_value`（市值口径），不是 `short_cost_debt` | 口径唯一 |
| INV-16 | **未来函数防护**：`--asof` 当日未收盘须报错或回退并标注；同命令内复权口径不得混用 | 防未来函数 |
| INV-17 | **非有限价拒单**：`price ∈ {inf, -inf, nan, 1e400}` → `NO_PRICE`；`json.dumps(..., allow_nan=False)` 必须通过 | 防风控被 NaN 静默绕过 + 非法 JSON |
| INV-18 | **`SHORT_SELL` 不受 T+1 可用量约束**：同日两笔 `SHORT_SELL` 均成交；`SELL`/`BUY_TO_COVER` 必须被拦 | 「融券开仓 ≠ 卖出持仓」 |
| INV-19 | **三层 binding 顺序**：单票 11 万（超 20% 上限）→ `POSITION_LIMIT_PCT` | 见 §3.2 三层表 |
| INV-20 | **引擎可达穿仓**：5 票各 ~10 万融资 → 开仓 `ratio ≈ 2.0137`；跌 71.4% → `forced` | 引擎路径真实可达性 |
| INV-21 | **边界语义严格 `<`**：`ratio==1.300` → `margin_call`（非 forced）；`1.299` → `forced`；三例同测 50.0%/50.1%/49.9% | 五处一致 |
| INV-22 | **「融上再融」**：浮盈 → `net_assets`↑ → 单票上限与 `margin_available`↑；**浮盈不得计入 `available_cash`** | §3.2.1 四条硬规则 |
| INV-23 | **可提取额度只读**：`ratio >= 3.00` 时 `risk_report()` 产出 `MAINTENANCE_EXTRACTABLE`（info）；本版本不实现资金划出 | 让 `max_maintenance_ratio` 生效 |
| INV-35 | **写者互斥（执行体级）**：任一数据根下，同一时刻最多一个**执行体**（进程或线程）处于 `store.locked_update` 临界区；`store.state_lock` 同进程同线程**可重入**，跨线程/跨进程互斥。超时抛 `StateLockTimeout`（`error_code="STATE_LOCK_TIMEOUT"`），**不得继续写** | 新：`tests/test_store.py` S1–S3、S7；跨前端 C1–C4 |
| INV-36 | **原子写必须有写方重试**：`os.replace` 在 Windows 上遇读者持有句柄会 `PermissionError(errno=13)`；`store.atomic_write_text` 在**有界预算**（`WRITE_RETRY_BUDGET_SECONDS`，默认 0.40s）内重试**可重试 errno**（11/13/16/26），预算耗尽抛 `StateWriteTimeout`（`error_code="STATE_WRITE_TIMEOUT"`）。**绝不退回非原子写、绝不假装成功** | core-builder 实测：去掉重试 45/200 失败（≈22.5%），有重试 0/200；`test_S9_*`、S10 |
| INV-37 | **重试逻辑单点落位**：除 `store.py` 外，任何模块**不得**直接调用 `os.replace`（否则绕过写方重试；`watchlist.py` 曾因此在 4 读者线程下 60 次写失败 50 次） | `test_meta_only_store_may_call_os_replace`（AST 判定）+ `test_meta_guard_would_catch_a_violation` |
| INV-38 | **审计与成交分流**：`state_audit.jsonl` 记**状态写入审计**（每次落盘一行，含 `source`/`pid`/`net_assets`/`ledger_lines`）；`ledger.jsonl` **只记成交**，每行一个 `Order`。**禁止**把审计行混进 `ledger.jsonl` —— `_read_ledger` 逐行全收、不做 kind 过滤，混入会让 `ledger --json` 返回幽灵订单并让计数判据失效 | Critic P1 裁决；`test_ledger_*` |
| INV-39 | **ledger 水位与分叉可检出**：`state.json` 的 `app.ledger_lines` 记录落盘时刻的 ledger 非空行数；`store.check_ledger_state_consistency(cfg)` 比对水位与实测行数，返回 `diverged`。任一侧不可读时 `diverged=None` 且 `consistent=False`（**不得**把「无法判定」当成「没问题」） | 新：`test_store.py` S12–S14；实测：正常路径 0/0、1/1 一致；注入一行后 `diverged=True` 检出 |
| INV-40 | **append_jsonl 行原子**：追加前取该数据根常驻锁，保证一次写入一行（Windows CRT 的 `O_APPEND` 是「seek + write」两步，**非行原子**；3 进程 × 60 行实测丢 3 行） | core-builder 实测；`tests/test_store.py` S8（断言精确行数） |
| INV-42 | **写锁临界区内禁止联网取数**（架构评审 S-1 第 2 条）：`store.state_lock` 进入临界区时把**本线程**标记为持锁中（`store.in_critical_section()`，按线程、可重入、退出恢复）；`DataSource._ak()` 在该标记为真且非离线时抛 **`NetworkForbiddenInLock`**（`DataUnavailable` 的**子类** —— 为兼容既有错误映射）并指明修法（取数移到锁外，用 `service.prewarm_before_write` 预热，或改用 `--offline`）。**该错误必须穿透「过期缓存兜底」与「降级兜底」**：`_cached` / `daily` / `index_daily` 在回退过期缓存前要先 `raise`；`engine._quote` / `_is_margin_target` / `_is_short_target` / `_float_shares` / `_mark_prices` / `_backfill_unlock_dates`、`watchlist._has_market_data` / `score_watchlist`、`datasource.industry_of` / `industry_constituents` / `universe` / `market_breadth` / `quote`(名称回退)、**`engine._evaluate` 里包住 `live_quote` 的那处**、**`datasource.latest_trade_asof` 的 `latest_trade_date()` 兜底**（后两处是第四轮复核找到的）等**降级站点**都要上抛 —— 否则用户拿到的是**错误的正常结果**（订单以错误码被拒、券源上限静默跳过、有数据的票判「无数据」、沿用旧价重估），而不是明确的失败。网页端映射：守卫错误 → **HTTP 409 + `NETWORK_IN_LOCK`**（见 **INV-45** 的完整映射表；按**类型** `isinstance` 匹配，不用类名字符串）。**理由**：BLOCK-1 的锁外预热只挪走**已知**取数；漏网取数会 fail-slow —— 悄悄持锁联网、写者互相饿死、网页端 503，而账户本身毫无问题，排查极难。此开关把「悄悄慢」变成「明确错」 | `tests/test_inlock_network_guard.py`（**22 条**：核心断言 + 「锁外允许」「离线错误不被顶替」「按线程」对照 + 9 个降级站点的参数化用例 + 过期缓存 A/B）；非空守卫实验：移除拦截 → `test_ak_is_refused_inside_lock` 变红（`DID NOT RAISE`）；去掉穿透 → `test_guard_error_not_swallowed_by_stale_cache` 变红；漏网实测 `.omx/probe/detect_inlock_network2.py`：数据源可用时锁内联网 **0** 次（锁外预热 4/1/3 次） |
| INV-43 | **写命令必须先锁外预热**：`order` / `settle` / `close`（CLI 与网页端**都要**，第一版只做了 CLI，独立复核发现网页侧零预热）在进入临界区前调用 `service.prewarm_before_write`；交易日必须用**与锁内相同**的逻辑在锁外解析（`init` 后 `state.trade_date` 为空、交易日由锁内 `resolve_asof` 才定，用请求里的日期会让预热落空）。预热键**来自实测**（锁内真实请求：`trade_dates` 恒需，`spot` 与 `spot([codes])` 与 `margin_targets` 按需），**不得凭想象加键**（曾预热 `daily(c, None, asof)` —— 该键引擎从不请求，是空预热还写出垃圾缓存文件） | `tests/test_cli.py::test_block1_*`；`tests/test_webapp.py::test_inv43_*`；**实测（模拟在线：假 akshare = 真实取数点 0.6s/次，锁超时 1.0s，3 并发 `POST /api/order`）**：预热 OFF → 锁内真实取数 **4** 次 / 最长持锁 **3.93s** / **2×503**；预热 ON → **0** 次 / **0.12s** / **0×503**。CLI 同向 4/4.00s → 0/0.11s。**注意**：`verify_block1_web2.py` 走的是**离线**路径，离线 `_cached` 不写缓存，物理上不可能把锁内取数降到 0 —— 该脚本的数字不可作为证据（独立复核第二轮指出） |
| INV-44 | **实时价成交与成交价审计**（B+D，2026-10-14 用户选定）：`order --price live`（网页端 `{"price":"live"}`）表示**按当前快照价成交** —— 取 `<root>/data/cache/spot.json` 的 `last`（在线时是 akshare 实时全市场快照，`TTL_SPOT=60s`）；**快照陈旧/缺失时抛 `DataUnavailable` 并明确拒绝，绝不静默退回陈旧价**。live 模式下**交易日取自快照自身的 `asof`**（`service.live_trade_date`），**不沿用 `resolve_asof`** —— 后者在 15:30 前回退到上一交易日，而引擎停牌检查看的是 `td` 那天有没有行情，两者不一致会让 live 下单被判 `SUSPENDED`（实测：不传 `--trade-date` 时 trade_date 落到昨天 → 拒绝）。**不放宽未来函数防护**：`live_quote(require_today=True)` 已要求快照必须是**今天**的。每笔订单（含**拒绝单**）带 `price_source`（`live`/`request`/`""`）、`price_asof`、`price_fetched_at`，并落进 `ledger.jsonl` —— 事后能回答「这笔用的是哪一刻的价」 | `tests/test_live_price.py`（**17 条**：CLI 入口解析 · 常量与 `Engine.LIVE_PRICE_SENTINEL` 一致 · 新鲜快照成交且审计完整 · **陈旧快照必须拒绝** · `live_quote` 陈旧时抛错 · 数字价 `price_source="request"` **对照** · 审计落进台账 · `live_trade_date` 取自快照 · 端到端 CLI 不传 `--trade-date` 时交易日 == 快照 asof）；接口：`DataSource.live_quote()` · `service.live_trade_date()` · `Engine.LIVE_PRICE_SENTINEL` | **豁免说明**：live 模式**有意豁免**「asof 必须是已收盘交易日」这条规则 —— 15:30 前 `resolve_asof` 会回退到上一交易日，而 live 用**当天**快照成交，于是订单记在「今天」。这是**用户显式选择**（`--price live`）且三审计字段可追溯的**有意例外**，不是放宽未来函数防护（`live_quote(require_today=True)` 仍要求快照必须是今天）。 **`asof` 缺失/为空必须按「不可用」处理**（第四轮复核 M4：旧写法 `bool(asof) and ...` 在 `asof` 为空时为假 → 当作新鲜 → 会成交并记下 `price_source="live"` + `price_asof=""`，即「贴着实时标签、却不知道是哪天的价」）。
| INV-45 | **网页端 HTTP 状态码映射表是契约**（`webapp.map_exception`，纯函数）：`_ParamError` → **400** · `_LockTimeout` → **503 + `STATE_LOCK_TIMEOUT`** · `NetworkForbiddenInLock` → **409 + `NETWORK_IN_LOCK`** · `DataUnavailable` → **200 业务态** · `ValueError` → **400** · 其它 → **`None`**（由 `dispatch` 兜底 **500**）。**必须按类型 `isinstance` 匹配，子类排在父类之前** —— `NetworkForbiddenInLock` ⊂ `DataUnavailable`，顺序错了守卫错误会变成 200 业务态；**禁止**用 `type(exc).__name__ == "..."` 做类名字符串匹配（会漏掉所有子类，实测曾让守卫错误变成 500）。映射抽成**纯函数**是为了可「喂异常对象 → 断言状态码」地单测：此前它藏在 `dispatch` 的 `except` 链里，变异实验要跑整条路由、会**卡死**（实测 >300s），导致 409 这条长期拿不到「违反时会红」的证据 | `tests/test_webapp.py::test_map_exception_status_codes`（5 类参数化）· `test_map_exception_returns_none_for_unknown` · `test_map_exception_guard_must_come_before_dataunavailable`（**断言子类关系与顺序**）；变异实验：注掉 409 分支 → 两条用例在 **1.8s** 内变红（此前会卡死） |
| INV-46 | **缺省交易日必须是「有行情的最近交易日」**（P3-2，2026-10-14 修）：`service.resolve_asof(ds, None)` 的缺省值取自 `DataSource.latest_trade_asof()`，**不是** `latest_trade_date()`。后者读的是**交易日历**（可覆盖到未来），而行情只到已收盘的最近一天 —— 用日历日期当 asof 会构造出「`asof-60d` 窗口里一根日线都没有」的区间，实测导致**真实时钟下离线缺省下单全部 `rejected / SUSPENDED`**（今天就在发作）。`latest_trade_asof` 用**两段式**判据：① 先找「**那一天本身有行情**」（`quote()['suspended'] is False`，引擎下单要的就是这个）；② 找不到再退到「**回看窗口非空**」（`score_watchlist` 这类窗口消费者）。**两段都必要**：删掉①会让缺省下单重新被判 `SUSPENDED`（变异实测）；只留①会让窗口消费者拿到「当天没行情」的日期（第四轮复核 M6 的边缘情形） | `tests/test_clock_robustness.py`（时钟推到 2026-10-13/12-10/12-31/2027-06-30/2030-01-01，断言返回**同一个有数据的日期**）· `tests/test_live_price.py` 的 live 交易日用例；变异实验：删掉第一段 → 缺省下单精确复现 `rejected / SUSPENDED`（第 4 轮复核实测） |

---

## 9. 参考项目说明（诚实标注）

`https://aistudio.baidu.com/projectdetail/9728332` 为**单页应用外壳**，静态抓取只能得到标题
「【ERNIE-4.5-VL-28B-A3B-Thinking】量化投资推荐系统」，正文需登录且由 JS 渲染，**不可复现读取**。

因此本项目**只取该参考路线的产品定位**（「LLM/规则驱动的量化选股推荐系统」），
技术规格全部落在 AKShare 公开接口契约与威廉·欧奈尔 CANSLIM 的公开原则之上。
**未复制、未推测、未虚构该项目的任何实现细节。**

---

## 10. 接口变更日志

| 版本 | 日期 | 变更 | 原因 | 影响写线 |
|---|---|---|---|---|
| v1 | 2026-10-10 | 首次冻结 | 并行开发需签名真源 | T1/T2/T3 |
| **v2** | 2026-10-10 | ① 取消 `margin_used` 现金桶；② `financing_debt` 改为 `N×(1-m_long)`；③ 新增 `short_cost_debt`/`short_qty_debt`；④ 负债用市值口径；⑤ 新增 `margin_required`/`margin_available`；⑥ 错误码 `INSUFFICIENT_MARGIN`→`INSUFFICIENT_MARGIN_SPACE`，新增 `SHORT_SOURCE_LIMIT`；⑦ 校验顺序调整（风险门禁前移、T+1 后移）；⑧ `schema_version` 1→2；⑨ 新增三条不变量与 v2 数值基准表 | 实测发现 v1 维持担保比例**系统性虚高**（50 万+融资 20 万场景：v1 `total_assets=700000` / ratio=3.50；正确 `total_assets=540000` / ratio=13.50）→ 会**漏报平仓线**，CRITICAL | T1/T2/T3 |
| **v2-gate** | 2026-10-10 | ⑩ 拆分 `frozen_t1`（T+1 待交收）与 `frozen_collateral`（融券券款）；`frozen_cash` 改为派生量 `frozen_t1 + frozen_collateral`；`settle_t1` 只解冻 `frozen_t1` | v1 只有一个 `frozen_cash`，`settle_t1` 会把融券券款一起解冻 → 凭空多出可用资金（实测由 `test-spec` TC-C-10 编写时暴露） | T1/T2 |
| **v3** | 2026-10-10 | ⑪ `financing_debt` **改回全额 N**（推翻 v2 的 `N×(1-m)`）；⑫ 维持担保比例分子改为 `available_cash + long_market_value`（**不含冻结资金**），新增 `collateral_value` 属性；⑬ 融资买入 `available_cash` 只扣 `F`（自有资金不出账）；⑭ **融券卖出 `available_cash = -F`**（修 v2 费用消失洞）；⑮ 新增 `leverage` 属性、`RiskConfig.max_leverage=2.0`、错误码 `LEVERAGE_LIMIT`、§5 强约束 5b；⑯ 新增不变量 I4；⑰ 基准表按 v3 重算 | **verifier 独立发现 v2 为 CRITICAL**：v2 把 ratio 从 3.50 抬到 13.50，且单笔融资 20 万时标的跌到 0 的 ratio 下界 = 8.50 > 1.30 → **130% 平仓线对多头融资账户不可达**，`forced_liquidation`/风险门禁/必测项 11 退化为死代码；v2 还把融券冻结券款错算进分子。**core-builder 独立发现** v2 融券卖出费用凭空消失（N=200000 时 F=152 未从任何桶扣减）。两项均经 Lead 独立复现确认 | T1/T2/T3 |
| **v3.1** | 2026-10-10 | ⑰ `collateral_value` **改为含 `frozen_collateral`**（融券券款计入分子），仅排除 `frozen_t1`；⑱ 基准表「满仓融资」从 N=1250000 改为 **N=625000**（开仓 ratio 1.80），触发 1.30 的跌幅从错误的 7.7% 改为 **50%**；⑲ 明确融资容量由**保证金额度规则（N ≤ 625000）**绑定而非杠杆闸，`max_leverage` 定位为**回退保护** | **plugin-author 在 task-6 独立发现 v3 的 BLOCK 级算术矛盾**：v3 称「满仓 N=1250000 / ratio=1.40 / 跌 7.7% 触及 1.30」，但该仓位在 v3 自身规则下 `margin_required = 1000000 > net_assets = 500000` 且 `leverage = 2.5 > 2.0`，**根本建不了仓**；且实际 binder 是保证金规则（N≤625000）而非杠杆闸（N≤1000000），触及 1.30 需跌 **50%** 而非 7.7%。另指出 v3 把融券券款排除出分子与深交所投教「融券卖出所得资金可充抵保证金」不符。三项均经 Lead 独立复现确认 | T1/T2/T3 |

| **v3.2** | 2026-10-10 | ⑳ 基准表统一为**零费用口径**（含费用数值改为独立对照块）；㉑ 明确**三层约束的 binding 顺序**：仓位限制(①) → 保证金额度(②) → 杠杆闸(③)，`N=625000` 场景标注为**纯函数口径**；㉒ 新增**引擎可达**的穿仓场景（5 票各 ~10 万融资 → 开仓 ratio 2.0137 → 需跌 ~71.4% 触 1.30）；㉓ `_as_float` 必须拒 `inf`（纳 `NO_PRICE` 语义） | **verifier 对抗 QA 发现**：H2 基准表零费用/含费用**混用**导致 INV-4「逐格 1e-6」必然失败（融资行实际 `F=52`）；H3 v3.1 的 `N=625000` 在 Engine 路径下被 `POSITION_LIMIT_PCT` 直接拒（实测：单票上限 `0.20×500000=100000`，5 票合计 500000，真正的 binder 是仓位限制）；H1 `price=inf` 被接受 → `frozen_t1=NaN` 污染账户并落盘为**非法 JSON**（裸 `NaN` 字面量），风控门禁全部失效 | T1/T2 |

| **v3.3** | 2026-10-10 | ㉔ **冻结边界语义为严格 `<`**：`ratio == 1.300` 恰好 → `margin_call`（非 forced）；跌 50.1% → `1.299` → `forced`；基准表补 50.1% 行（`long_mv=311875` / 担保物 `811875` / net `186875`）。㉕ 容量表 `N=200000` 的「220%」改为「**不可达**」（该根对应负价）。㉖ **新增 §3.2.1「融上再融」机制**（用户显式要求）：额度全部由实时 `net_assets` 派生；浮盈放大额度但**不得**计入 `available_cash`；浮动盈利只释放额度、不产生可支用现金。㉗ `MAINTENANCE_EXTRACTABLE` 明确为**只读**提示，本版本不实现资金划出 | **plugin-author 第三轮复核发现 N1（BLOCK）**：AC-5/TC-E-34 主张「`ratio == 1.30` → forced」，而 TC-E-23 主张「`1.30 <= ratio < 1.50` → margin_call」，实现 6 处均为严格 `<` → 同一边界三处互斥，AC-5/TC-E-34 必然红。浮点事实 `812500/625000 == 1.3`。另 N3（负价代数根）与用户追加的「融上再融」需求 | T1/T2/T3 |
| **v3.5** | 2026-10-10 | ㉚ **§6.2 分支 3 扩展为三条件或**：`total >= buy_threshold 但 (M < 50 或 L < 60 或 abs_rs <= 0)` → `watch`；㉛ **新增 `abs_rs > 0` 为 `buy` 独立前置**（严格 `> 0`，不加 epsilon；**不钳制 L 分值**）；㉜ **§6.1 的 L 门槛改为「可比较对手（成分股数 − 自身）< 10」**（采纳 verifier 口径：百分位分辨率 = 比较次数 = n−1，计自身等于让候选股给自己凑样本）；㉝ 含费对照 `3.49971` → **`3.49974`**；㉞ §8.1 补齐为 **INV-1..INV-23 权威版**；㉟ §3.2 基准表融券两行改回零费用口径、`mark_to_market` docstring 清理 v2 残留字段 | **verifier 复核 CR-16 时提出**：分支 2 加前置后，分支 3 若不扩展会「掉进分支 4 兜底」——信号碰巧仍是 watch，但 `reasons` 缺失且「先命中先返回」**不再穷尽**，实现与冻结文本不一致。**strategy-builder 全池实测**：所有标的行业样本 < 10 → ① 使全池改走绝对分档；真实 600519 绝对 RS=−6.8% 却因「白酒Ⅱ」3 家对手更差拿到 L=100（`percentile=1.0`）→ 修复后 L=40 + `abs_rs<=0` **双重拦截**。**plugin-author N8** 指出含费对照数字未落盘。**core-builder 上报** §8.1 的 INV-3/INV-6 为 v2 残留 | T1/T3 |

**v1..v3.1 演进取证脚本**（均在 `.omx/probe/`，可独立复跑）
| 脚本 | 用途 |
|---|---|
| `verify_margin_formula.py` | v1 `margin_used` 重复计入的反例 |
| `derive_margin_model.py` | v2 第一版推导（内建 assert 当场暴露 I1 失败，保留作演进证据） |
| `derive_margin_model2.py` | v2 第二版推导（后被 v3 推翻，保留作演进证据） |
| `verify_v31_capacity.py` | v3.1 融资容量/平仓线可达性核算（复现 plugin-author 的 BLOCK） |

**v1 错误的取证**：`D:\Ai投资\.omx\probe\verify_margin_formula.py`（反例复现）与
`D:\Ai投资\.omx\probe\derive_margin_model2.py`（v2 推导 + 不变量验证，全部 assert 通过）。

> **§3.2 分子排除 `frozen_t1` 的说明（保守方向）**：交易所口径的分子是「现金」，
> 而实盘当日卖出所得在成交后即属可用资金。本项目把 `frozen_t1` 排除在分子外，
> 是**保守方向的模型简化**（低报 ratio、高报风险，不会漏放风控）。
> 若需与实盘完全一致，可在后续版本把 `frozen_t1` 计入分子。

---

## 11. 已知限制与非目标（不假装已解决）

| # | 限制 | 影响 | 处置 |
|---|---|---|---|
| L1 | **夹具与断言同源**：`data/fixture/`（T1 生成）与 `tests/test_strategy.py`（T3 断言）由同一套假设驱动 | 若夹具量纲/取值写错，测试可能"自证正确" | strategy-builder 已加 `require_scored()` 守卫，禁止"夹具没生成 → 空洞通过"；§4 单位表把量纲写成强制契约以缩小空间 |
| L2 | **仓位规则双份实现**：`strategy.recommend()` 的 `position_pct` 与 `engine.position_sizing()` 各有一套 | 两处可能漂移 | **`engine.position_sizing()` 是唯一权威**；`recommend().position_pct` 仅作展示，`engine.build_plan()` 必须重算 |
| L3 | **`REPAY_MARGIN` 无部分还款**（本版本裁剪） | 无法只还一部分融资负债 | 已冻结为全额还款；部分还款列为 v2 候选 |
| L4 | **按日线收盘撮合**，非盘中撮合 | 无法模拟日内滑点/排队 | 明确非目标；`slippage_bps` 只对市价单做近似 |
| L5 | **两融名单接口故障**时仅能回退缓存/夹具，再失败抛 `DataUnavailable` | 极端情况下无法判定标的资格 | 明确失败而非静默放行（这是有意选择） |
| L6 | **akshare 接口抖动**（`stock_financial_analysis_indicator` 慢、`stock_zh_index_daily_em` 曾 ConnectionError） | 实时拉数可能失败 | 缓存 + 夹具 + 双接口兜底；`docs` 记录实测 |
| L7 | **`--asof` 当日收盘前**的数据不完整 | 可能引入未来函数 | §7 已冻结：15:30 前报错或回退并标注 |
| L8 | **`institution` 识别依赖 `top_holders.holder_type` 取值域** | 该来源标签变化会影响 I 因子 | 已按「基金/保险/社保/证券/信托/银行/QFII/法人」白名单 + 排除「个人/自然人」实现 |
| L9 | **本系统不构成投资建议**，不接实盘 | — | 非目标；技能与 README 均含免责声明 |
| L10 | **L 因子的行业排名路径**依赖 `industry_constituents`，该接口抖动会导致退化到绝对 RS 分档 | 打分口径会在两种路径间切换 | 退化时必须在 `risks` 中标注，不得静默 |
| L11 | **不实现「资金划出/提取」交易**（`maintenance_ratio >= 3.00` 时只产出 `MAINTENANCE_EXTRACTABLE` 只读提示） | 无法模拟把浮盈提现出账户 | §3.2.1 已说明；需要模拟提取用 `init --reset` 或改 `config.json` 的 `initial_cash` |
| L12 | **「融上再融」用实时 `net_assets` 派生额度**，未建模券商的「授信额度」概念与单券集中度折算率差异 | 真实券商会给每条标的不同的折算率与授信上限，本模型用统一比例近似 | §3.2.1 已冻结机制与四条硬规则；折算率差异列为 v2 候选 |
| L13 | **沪市 `can_short` 用「融券余量 > 0」保守近似**（深市用官方「融券标的」标记） | 余量为 0 只说明"当前无人融券"，≠ 交易所摘牌出名单；判定会随当日券源漂移 | 实测 472 条 `can_short=False` 全是无行情 ETF；负例改用 `600110 诺德股份`。稳定化需交易所 SSE 官方名单接口（akshare 暂无） |
| L14 | **C 因子的报告期选择用「法定披露截止日」白名单，不读 `announce_date`** | `asof` 落在「真实披露日 ~ 法定截止日」之间时（如 2025-08-13 ~ 08-31），仍用上一期 → **最多保守滞后 2~4 周**。方向**安全（绝不前视）** | strategy-builder 留痕；数据层现已提供 `announce_date` 列，将来可加 `announce_date <= asof` 过滤提高精度 |
| L15 | **行业排名路径在离线夹具下事实上不可达**（`industry_map.json` 仅 31 行、最大行业 4 只 → 全池 `competitors < 10` → 一律降级绝对分档） | 属**夹具覆盖度**限制而非实现缺陷；live 路径取 `quarterly_report(最新期)`（全市场 `stock_yjbb_em`），真实行业成员数足够触发百分位路径 | 百分位路径的覆盖由 11 只合成行业的用例保住（`test_l_gate_requires_ten_comparators` 等） |
| L16 | **`chart.py` 的浏览器内实际渲染未验证** | 只验到 payload、指标数值与 HTML 自包含性；周/月线切换在真实浏览器中的渲染无浏览器面可验 | verifier 将以「三周期数据集齐全 + 切换绑定 + bars 数一致」的**静态等价断言**替代，并在 evidence 中显式标注**非浏览器验证** |
| L17 | **未覆盖**：`offline=False` 真实联网路径、缓存 TTL 15:30 失效分支、多批次+部分卖出+跨日解冻组合、`close --ratio<1` 分批、`build_plan` 每种 binding 精确算例 | 安全边界（禁外网副作用）与时间旅行成本 | 原样记入最终摘要的「未覆盖」一节，不粉饰 |
