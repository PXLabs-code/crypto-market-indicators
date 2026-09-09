"""加密货币多因子量化策略回测与可视化系统。

读取 ``data/`` 目录下由 ``update_data.py`` 维护的日度 CSV 数据（BTC 现货 OHLCV、
BTC MVRV、BTC 资金费率、市场恐惧与贪婪指数），对齐为统一的日频面板数据后，
对 9 套仓位策略进行历史回测、计算绩效指标，并生成自包含的交互式 Plotly HTML 看板
以及 Markdown 绩效对比表。

设计原则：
    - 严禁未来函数：所有需要 T+1 日才能知道的仓位一律 ``.shift(1)`` 后再参与收益计算。
    - 缺失值仅允许 ``.ffill()``，不允许 ``.bfill()``，避免用未来数据填补历史空值。
    - 所有策略均输出分级仓位 {0%, 25%, 50%, 75%, 100%}，非仓位打分类信号最终都会
      对齐（snap）到这一仓位网格上。
    - 每个策略都在 ``RULES`` 类属性中显式声明「触发条件 -> 目标仓位」的对照表，
      供 Markdown 报告与 HTML 看板自动生成规则说明。
"""

from __future__ import annotations

import argparse
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import List, Tuple

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# ---------------------------------------------------------------------------
# 全局常量
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
REPORTS_DIR = REPO_ROOT / "reports"

ANNUALIZATION_DAYS = 365  # 加密市场全年 365 天交易，不同于传统股市的 252 天
TRADING_COST_RATE = 0.001  # 单边手续费 + 滑点 0.1%
RISK_FREE_RATE = 0.0

MA_SHORT_WINDOW = 20
MA_LONG_WINDOW = 200
VOLATILITY_WINDOW = 20
DONCHIAN_SHORT_WINDOW = 20  # 唐奇安短通道（海龟交易法则的入场系统）
DONCHIAN_LONG_WINDOW = 55  # 唐奇安长通道（海龟交易法则的出场/趋势确认系统）
FUNDING_MA_WINDOW = 7  # 资金费率平滑窗口，过滤单日噪音
INITIAL_CAPITAL = 100_000.0  # 净值曲线展示用的假设初始本金（美元）
BULL_REGIME_FLOOR = 0.5  # 长期均线上方（确认牛市）时，逆势/防御类策略的最低仓位保护

# 分级仓位网格：所有策略最终仓位都会被吸附到这 5 档上
POSITION_GRID = [0.0, 0.25, 0.5, 0.75, 1.0]


def snap_to_grid(series: pd.Series, grid: List[float] = POSITION_GRID) -> pd.Series:
    """将连续仓位值就近吸附到分级仓位网格上（0% / 25% / 50% / 75% / 100%）。"""
    grid_arr = np.asarray(grid, dtype=float)
    values = series.to_numpy(dtype=float)
    nan_mask = np.isnan(values)
    values = np.nan_to_num(values, nan=0.0)
    idx = np.abs(values[:, None] - grid_arr[None, :]).argmin(axis=1)
    snapped = grid_arr[idx]
    snapped[nan_mask] = np.nan
    return pd.Series(snapped, index=series.index, name=series.name)


def apply_bull_floor(position: pd.Series, df: pd.DataFrame, floor: float = BULL_REGIME_FLOOR) -> pd.Series:
    """牛市仓位保护：当收盘价位于长期均线（MA200）上方（确认长期上升趋势）时，
    为逆势/防御类策略设置仓位下限，避免情绪或估值指标长期停留在极端区间导致
    策略在多年牛市中持续低仓/空仓，白白错过趋势收益。仅在确认牛市时生效，
    熊市/震荡市中策略仍可按原逻辑降至 0% 仓位以控制回撤。"""
    bull_regime = df["btc_close"] > df["ma_long"]
    floored = position.where(~(bull_regime & (position < floor)), floor)
    return snap_to_grid(floored)


# ---------------------------------------------------------------------------
# 1. 数据加载与预处理
# ---------------------------------------------------------------------------


def _read_csv(path: Path, columns: List[str]) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"缺少必需的数据文件: {path}")
    df = pd.read_csv(path, parse_dates=["timestamp"])
    df["timestamp"] = df["timestamp"].dt.tz_localize(None).dt.normalize()
    return df[columns]


def load_dataset(data_dir: Path = DATA_DIR) -> pd.DataFrame:
    """加载并对齐全部数据源，返回以 BTC 现货价格日期为主轴的日频面板数据。

    对齐规则：
        - 资金费率（8 小时频率）先按 UTC 自然日聚合取均值，再并入主表。
        - 所有数据以 BTC 现货价格的 ``timestamp`` 为主轴做左外连接（left join）。
        - 缺失值只允许使用前向填充 ``ffill``，严禁使用 ``bfill``，防止未来数据泄露。
    """
    btc_price = _read_csv(data_dir / "btc" / "spot_ohlcv.csv", ["timestamp", "open", "high", "low", "close", "volume"])
    btc_mvrv = _read_csv(data_dir / "btc" / "mvrv.csv", ["timestamp", "mvrv"])
    btc_funding = _read_csv(data_dir / "btc" / "funding_rates.csv", ["timestamp", "funding_rate"])
    fgi = _read_csv(data_dir / "market" / "fear_greed.csv", ["timestamp", "fear_greed_value"])

    # 资金费率按 UTC 日度聚合取均值（8 小时结算 -> 每日 3 条取均值）
    funding_daily = (
        btc_funding.groupby(btc_funding["timestamp"].dt.floor("D"))["funding_rate"]
        .mean()
        .reset_index()
    )

    df = btc_price.sort_values("timestamp").rename(
        columns={"open": "btc_open", "high": "btc_high", "low": "btc_low", "close": "btc_close", "volume": "btc_volume"}
    )

    # 全部以 BTC 价格日期为主轴左连接，绝不使用其他 join 方式引入未来行
    df = df.merge(btc_mvrv, on="timestamp", how="left")
    df = df.merge(funding_daily, on="timestamp", how="left")
    df = df.merge(fgi, on="timestamp", how="left")

    df = df.sort_values("timestamp").reset_index(drop=True)

    # 严格只前向填充，不做后向填充，避免未来数据泄露
    fill_columns = ["mvrv", "funding_rate", "fear_greed_value"]
    df[fill_columns] = df[fill_columns].ffill()

    # 前向填充无法覆盖到某个因子首次出现之前的行，直接丢弃这些无法评估的历史行
    df = df.dropna(subset=fill_columns).reset_index(drop=True)

    df["btc_return"] = df["btc_close"].pct_change()
    df["ma_short"] = df["btc_close"].rolling(MA_SHORT_WINDOW, min_periods=MA_SHORT_WINDOW).mean()
    df["ma_long"] = df["btc_close"].rolling(MA_LONG_WINDOW, min_periods=MA_LONG_WINDOW).mean()
    # 历史已实现波动率（年化），仅使用过去 N 日收益，纯回溯不涉及未来数据
    df["realized_vol"] = df["btc_return"].rolling(VOLATILITY_WINDOW, min_periods=VOLATILITY_WINDOW).std() * np.sqrt(
        ANNUALIZATION_DAYS
    )
    # MVRV 历史分位数：expanding().rank() 只使用截至当日（含）的历史数据，不引入未来信息
    df["mvrv_percentile"] = df["mvrv"].expanding(min_periods=30).rank(pct=True)

    # 唐奇安通道：用 shift(1) 后的历史高低点滚动极值，代表「过去 N 日（不含当日）」的通道上下轨，
    # 这样「今日收盘价突破通道」才是真正的突破事件，而非当日高低点自身参与滚动导致的必然重合。
    df["donchian_high_short"] = (
        df["btc_high"].shift(1).rolling(DONCHIAN_SHORT_WINDOW, min_periods=DONCHIAN_SHORT_WINDOW).max()
    )
    df["donchian_low_short"] = (
        df["btc_low"].shift(1).rolling(DONCHIAN_SHORT_WINDOW, min_periods=DONCHIAN_SHORT_WINDOW).min()
    )
    df["donchian_high_long"] = (
        df["btc_high"].shift(1).rolling(DONCHIAN_LONG_WINDOW, min_periods=DONCHIAN_LONG_WINDOW).max()
    )
    df["donchian_low_long"] = (
        df["btc_low"].shift(1).rolling(DONCHIAN_LONG_WINDOW, min_periods=DONCHIAN_LONG_WINDOW).min()
    )
    # 资金费率 7 日均值，平滑单日噪音以识别持续性的多空拥挤/挤压趋势
    df["funding_rate_ma"] = df["funding_rate"].rolling(FUNDING_MA_WINDOW, min_periods=FUNDING_MA_WINDOW).mean()

    # 均线、波动率、分位数、唐奇安通道等滚动特征形成之前的行无法参与策略评估，一并丢弃
    df = df.dropna(
        subset=[
            "ma_short",
            "ma_long",
            "realized_vol",
            "mvrv_percentile",
            "donchian_high_short",
            "donchian_low_short",
            "donchian_high_long",
            "donchian_low_long",
            "funding_rate_ma",
        ]
    ).reset_index(drop=True)

    return df


# ---------------------------------------------------------------------------
# 2. 策略架构（策略模式）
# ---------------------------------------------------------------------------


class BaseStrategy(ABC):
    """策略基类：子类只需实现 ``generate_signals``，返回当日收盘后决定的目标仓位。

    返回的 ``position`` 序列语义为「T 日收盘后，基于 T 日及之前可得信息决定的目标仓位」，
    取值须来自分级仓位网格 ``POSITION_GRID``（0% / 25% / 50% / 75% / 100%）。
    回测引擎会对其整体 ``shift(1)`` 后再与 T+1 日收益率相乘，从而保证不使用未来函数。

    子类应在 ``RULES`` 中声明「触发条件 -> 目标仓位」的对照表（按触发优先级排列），
    用于在 Markdown 报告和 HTML 看板中自动生成策略规则说明。
    """

    name: str = "BaseStrategy"
    # 每一项为 (触发条件描述, 触发后目标仓位描述)，用于生成人类可读的规则说明表。
    RULES: List[Tuple[str, str]] = []

    @abstractmethod
    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        raise NotImplementedError


class BuyAndHoldStrategy(BaseStrategy):
    """a) Benchmark: BTC Buy & Hold —— 全程满仓 100%。"""

    name = "a) Buy & Hold 基准"
    RULES = [("无条件（任何行情下均持有）", "恒定 100%，不调仓")]

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        return pd.Series(1.0, index=df.index, name="position")


class TrendFollowingStrategy(BaseStrategy):
    """b) Trend Following：双均线（MA20/MA200）趋势策略。

    以短均线相对长均线的偏离幅度衡量趋势强度，偏离越大仓位越高（金叉顺势），
    偏离为负且幅度越大仓位越低（死叉减仓/清仓）。
    """

    name = "b) 双均线趋势策略"
    RULES = [
        ("(MA20-MA200)/MA200 >= 8%（强势多头排列/金叉走阔）", "买入至 100%"),
        ("3% <= 偏离 < 8%", "加仓至 75%"),
        ("-3% <= 偏离 < 3%（均线粘合，趋势不明）", "维持/回到 50%"),
        ("-8% <= 偏离 < -3%", "减仓至 25%"),
        ("偏离 < -8%（强势空头排列/死叉走阔）", "清仓至 0%"),
    ]

    # 偏离幅度分级阈值：(短均线-长均线)/长均线
    THRESHOLDS = [(0.08, 1.0), (0.03, 0.75), (-0.03, 0.5), (-0.08, 0.25)]
    FLOOR_WEIGHT = 0.0

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        spread = (df["ma_short"] - df["ma_long"]) / df["ma_long"]
        position = pd.Series(self.FLOOR_WEIGHT, index=df.index, name="position")
        for threshold, weight in sorted(self.THRESHOLDS, key=lambda item: item[0]):
            position[spread >= threshold] = weight
        return position


class ValuationMeanReversionStrategy(BaseStrategy):
    """c) Valuation Mean-Reversion：MVRV 历史分位数高抛低吸策略。

    MVRV 分位数越低（历史级低估）越加仓博均值回归上行，分位数越高（历史级高估）
    越减仓规避回调风险。分位数使用 ``expanding().rank(pct=True)``，只回溯不前瞻。
    """

    name = "c) MVRV 估值分位数策略"
    RULES = [
        ("MVRV 历史分位数 <= 10%（深度低估）", "买入至 100%"),
        ("10% < 分位数 <= 30%", "加仓至 75%"),
        ("30% < 分位数 <= 70%（估值中性）", "维持/回到 50%"),
        ("70% < 分位数 <= 90%", "减仓至 25%"),
        ("分位数 > 90%（历史级高估）", "清仓至 0%"),
        ("牛市保护：收盘价 > MA200（确认长期上升趋势）", "仓位下限提升至 50%，避免高估值区间长期空仓错过牛市"),
    ]

    # (分位数上限, 仓位) —— 分位数从低到高分级
    THRESHOLDS = [(0.10, 1.0), (0.30, 0.75), (0.70, 0.5), (0.90, 0.25)]
    CEILING_WEIGHT = 0.0  # 分位数 > 0.90（历史级高估）时清仓

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        percentile = df["mvrv_percentile"]
        position = pd.Series(self.CEILING_WEIGHT, index=df.index, name="position")
        for threshold, weight in sorted(self.THRESHOLDS, key=lambda item: -item[0]):
            position[percentile <= threshold] = weight
        return apply_bull_floor(position, df)


class SentimentRegimeStrategy(BaseStrategy):
    """d) Sentiment Regime：Fear & Greed 极值反转策略。

    情绪指数越接近「极度恐惧」越逆势加仓，越接近「极度贪婪」越逆势减仓，
    在两端之间按情绪强度分级过渡。
    """

    name = "d) 情绪极值反转策略"
    RULES = [
        ("FGI <= 20（极度恐惧）", "逆势买入至 100%"),
        ("20 < FGI <= 40（恐惧）", "加仓至 75%"),
        ("40 < FGI <= 60（中性）", "维持/回到 50%"),
        ("60 < FGI <= 80（贪婪）", "减仓至 25%"),
        ("FGI > 80（极度贪婪）", "逆势卖出至 0%"),
        ("牛市保护：收盘价 > MA200（确认长期上升趋势）", "仓位下限提升至 50%，避免长期处于极度贪婪区间被反复清仓"),
    ]

    # (FGI 上限, 仓位) —— FGI 从低（恐惧）到高（贪婪）分级
    THRESHOLDS = [(20, 1.0), (40, 0.75), (60, 0.5), (80, 0.25)]
    CEILING_WEIGHT = 0.0  # FGI > 80（极度贪婪）时清仓

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        fgi = df["fear_greed_value"]
        position = pd.Series(self.CEILING_WEIGHT, index=df.index, name="position")
        for threshold, weight in sorted(self.THRESHOLDS, key=lambda item: -item[0]):
            position[fgi <= threshold] = weight
        return apply_bull_floor(position, df)


# --- 多因子综合打分（供 e 与 f 两个策略共用）---------------------------------

MVRV_SCORE_LOW, MVRV_SCORE_HIGH = 0.8, 3.5  # MVRV 归一化区间：<=0.8 深度低估，>=3.5 历史级高估
FUNDING_SCORE_EXTREME = 0.001  # 日均资金费率的极端参考值（0.1%）

WEIGHT_MVRV = 0.35
WEIGHT_FGI = 0.25
WEIGHT_FUNDING = 0.20
WEIGHT_MA = 0.20


def _clip01(series: pd.Series) -> pd.Series:
    return series.clip(lower=0.0, upper=1.0)


def _score_mvrv(mvrv: pd.Series) -> pd.Series:
    normalized = (MVRV_SCORE_HIGH - mvrv) / (MVRV_SCORE_HIGH - MVRV_SCORE_LOW)
    return _clip01(normalized) * 100


def _score_fgi(fgi: pd.Series) -> pd.Series:
    # FGI 本身即恐惧(低分=看多)到贪婪(高分=看空)的 0-100 指标，反转即为看多分数
    return 100 - fgi


def _score_funding(funding_rate: pd.Series) -> pd.Series:
    normalized = 0.5 - (funding_rate / FUNDING_SCORE_EXTREME) * 0.5
    return _clip01(normalized) * 100


def _score_ma_trend(df: pd.DataFrame) -> pd.Series:
    bullish = (df["btc_close"] > df["ma_long"]) & (df["ma_short"] > df["ma_long"])
    bearish = (df["btc_close"] < df["ma_long"]) & (df["ma_short"] < df["ma_long"])
    score = pd.Series(50.0, index=df.index)
    score[bullish] = 100.0
    score[bearish] = 0.0
    return score


def compute_composite_score(df: pd.DataFrame) -> pd.Series:
    """综合打分：MVRV(35%) + FGI(25%) + 资金费率(20%) + 均线趋势(20%) -> 0-100 分。"""
    return (
        WEIGHT_MVRV * _score_mvrv(df["mvrv"])
        + WEIGHT_FGI * _score_fgi(df["fear_greed_value"])
        + WEIGHT_FUNDING * _score_funding(df["funding_rate"])
        + WEIGHT_MA * _score_ma_trend(df)
    )


class MultiFactorScoringStrategy(BaseStrategy):
    """e) Multi-Factor Scoring：MVRV(35%) + FGI(25%) + 资金费率(20%) + 均线(20%) 综合打分。

    综合分映射至 0% / 25% / 50% / 75% / 100% 五级仓位。
    """

    name = "e) 多因子加权打分策略"
    RULES = [
        ("综合得分 >= 65（看多，历史分位约前 10%）", "买入至 100%"),
        ("50 <= 得分 < 65", "加仓至 75%"),
        ("35 <= 得分 < 50（中性）", "维持/回到 50%"),
        ("20 <= 得分 < 35", "减仓至 25%"),
        ("得分 < 20（强烈看空）", "清仓至 0%"),
        ("牛市保护：收盘价 > MA200（确认长期上升趋势）", "仓位下限提升至 100%，与 Buy & Hold 完全对齐；仅在真正跌破 MA200（确认下降趋势）时才按打分逻辑降仓避险"),
    ]

    # MVRV/FGI/资金费率均为逆向（估值/情绪）因子，天然与均线趋势因子在牛市中方向相反，
    # 导致综合分历史上难以触及原先 75/60 分的高阈值（实测历史最高分不足 79）。
    # 下调各档阈值以匹配综合分的真实历史分布（均值约 55，最高约 79），并叠加更高的
    # 牛市仓位下限（75%，高于其余策略通用的 50% 下限），使本策略在牛市中的持仓强度
    # 更接近 Buy & Hold，同时仍保留熊市/顶部区域降仓避险的能力。
    SCORE_THRESHOLDS = [(65, 1.0), (50, 0.75), (35, 0.5), (20, 0.25)]
    FLOOR_WEIGHT = 0.25  # 综合分 < 20 时仅降至 25%（保留底仓），不再完全清仓
    BULL_FLOOR = 1.0  # 牛市（收盘价 > MA200）时仓位下限提升至 100%，与 Buy & Hold 完全对齐；
    # 仅在价格跌破 MA200（真正确认的下降趋势/熊市）时才允许按打分逻辑降仓避险。

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        score = compute_composite_score(df)
        position = pd.Series(self.FLOOR_WEIGHT, index=df.index, name="position")
        for threshold, weight in sorted(self.SCORE_THRESHOLDS, key=lambda item: item[0]):
            position[score >= threshold] = weight
        return apply_bull_floor(position, df, floor=self.BULL_FLOOR)


class DynamicVolTargetingStrategy(BaseStrategy):
    """f) Dynamic Vol-Targeting：多因子打分 * 波动率反比的动态风险控制策略。

    以多因子综合分（0-100 映射到 0-1）作为基础仓位方向，再乘以「目标波动率 / 已实现
    波动率」的风险缩放系数：已实现波动率越高（市场越剧烈），仓位相应收缩；波动率越低
    （市场越平稳），在基础方向允许范围内适度放大仓位。风险缩放系数设置上下限以避免
    极端放大或过度清零，最终结果吸附到分级仓位网格。
    """

    name = "f) 动态波动率目标策略"
    RULES = [
        ("基础方向仓位 = 综合得分 / 100（同 e 策略打分逻辑）", "0%~100% 连续值"),
        ("已实现波动率（20 日年化）高于目标波动率 70%", "风险系数 < 1，仓位相应收缩"),
        ("已实现波动率低于目标波动率 70%", "风险系数 > 1（上限 1.75 倍），仓位适度放大"),
        ("最终仓位 = clip(基础仓位 x 风险系数, 0, 1)", "就近吸附至 0/25/50/75/100%"),
    ]

    # 加密资产年化波动率长期高于 50%，原目标值会让风险系数长期 < 1，
    # 系统性压低仓位；上调目标波动率与放大上限，让策略在低波动的牛市阶段
    # 更充分地享受趋势收益，同时仍保留高波动期间的降仓保护。
    TARGET_ANNUAL_VOL = 0.7  # 目标年化波动率 70%，作为风险预算基准
    RISK_SCALE_MIN, RISK_SCALE_MAX = 0.3, 1.75

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        base_position = compute_composite_score(df) / 100.0
        risk_scale = (self.TARGET_ANNUAL_VOL / df["realized_vol"]).clip(
            lower=self.RISK_SCALE_MIN, upper=self.RISK_SCALE_MAX
        )
        raw_position = (base_position * risk_scale).clip(0.0, 1.0)
        return snap_to_grid(raw_position)


class DonchianBreakoutStrategy(BaseStrategy):
    """g) Donchian Breakout：唐奇安通道突破策略（海龟交易法则的简化分级版）。

    使用两条通道：
        - 短通道（20 日）：捕捉入场信号，价格突破短通道视为趋势启动。
        - 长通道（55 日）：确认强趋势，价格突破长通道视为趋势的强确认。

    通道均基于 ``shift(1)`` 后的历史最高/最低价滚动计算，即「过去 N 日（不含当日）」
    的通道上下轨，因此「今日收盘价突破通道」是真正的突破事件而非同日高低点的必然重合。
    """

    name = "g) 唐奇安通道突破策略"
    RULES = [
        ("收盘价突破过去 55 日最高价（长通道上轨，强势新高确认）", "买入至 100%"),
        ("收盘价突破过去 20 日最高价，但未突破 55 日最高价（短通道上轨）", "加仓至 75%"),
        ("价格位于 20 日与 55 日通道内部（无突破，趋势未明）", "维持/回到 75%（默认偏多，避免长期盘整错失趋势）"),
        ("收盘价跌破过去 20 日最低价，但未跌破 55 日最低价（短通道下轨）", "减仓至 25%"),
        ("收盘价跌破过去 55 日最低价（长通道下轨，强势新低确认）", "清仓至 0%"),
    ]

    # 通道内部（无突破）在长期牛市中经常出现（价格沿上轨附近盘整），
    # 将默认仓位由 50% 上调至 75%，减少策略在牛市盘整期被动降仓的损耗。
    NEUTRAL_WEIGHT = 0.75

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        close = df["btc_close"]
        # 条件按「更极端优先」排列：np.select 取首个为真的条件，
        # 由于长通道突破必然也满足短通道突破的价格关系，须把长通道条件放在前面。
        conditions = [
            close > df["donchian_high_long"],
            close > df["donchian_high_short"],
            close < df["donchian_low_long"],
            close < df["donchian_low_short"],
        ]
        choices = [1.0, 0.75, 0.0, 0.25]
        position = pd.Series(
            np.select(conditions, choices, default=self.NEUTRAL_WEIGHT), index=df.index, name="position"
        )
        return position


class FundingRateSqueezeStrategy(BaseStrategy):
    """h) Funding Rate Squeeze：资金费率挤压反转策略（加密货币衍生品特有信号）。

    永续合约资金费率反映多空杠杆拥挤程度：
        - 资金费率持续为负（空头向多头付费）代表空头过度拥挤，存在「逼空」反弹的
          潜在动能，逆势加仓做多。
        - 资金费率持续大幅为正（多头向空头付费）代表多头过度拥挤，存在「多杀多」
          回调的潜在风险，逆势减仓规避。

    使用 7 日均资金费率（``funding_rate_ma``）平滑单日噪音，只依赖历史滚动均值，
    不引入未来数据。
    """

    name = "h) 资金费率挤压反转策略"
    RULES = [
        ("7 日均资金费率 <= -0.05%（空头持续付费，逼空信号）", "逆势买入至 100%"),
        ("-0.05% < 7 日均资金费率 <= -0.01%", "加仓至 75%"),
        ("-0.01% < 7 日均资金费率 <= 0.03%（中性）", "维持/回到 50%"),
        ("0.03% < 7 日均资金费率 <= 0.07%（多头杠杆升温）", "减仓至 25%"),
        ("7 日均资金费率 > 0.07%（多头严重拥挤，潜在多杀多风险）", "逆势卖出至 0%"),
        ("牛市保护：收盘价 > MA200（确认长期上升趋势）", "仓位下限提升至 50%，避免长期牛市中因资金费率偏高被反复减仓"),
    ]

    # (7 日均资金费率上限, 仓位) —— 费率从低（空头拥挤）到高（多头拥挤）分级
    THRESHOLDS = [(-0.0005, 1.0), (-0.0001, 0.75), (0.0003, 0.5), (0.0007, 0.25)]
    CEILING_WEIGHT = 0.0  # 7 日均资金费率 > 0.07% 时清仓

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        funding_ma = df["funding_rate_ma"]
        position = pd.Series(self.CEILING_WEIGHT, index=df.index, name="position")
        for threshold, weight in sorted(self.THRESHOLDS, key=lambda item: -item[0]):
            position[funding_ma <= threshold] = weight
        return apply_bull_floor(position, df)


# ---------------------------------------------------------------------------
# 3. 回测引擎与绩效指标
# ---------------------------------------------------------------------------


@dataclass
class StrategyResult:
    name: str
    raw_position: pd.Series  # T 日收盘后决定的仓位（未 shift）
    position: pd.Series  # 实际生效仓位（已 shift(1)，用于计算 T 日收益）
    strategy_return: pd.Series
    equity_curve: pd.Series
    drawdown: pd.Series
    trade_count: int
    rules: List[Tuple[str, str]] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


def _annualized_return(equity_curve: pd.Series, periods: int) -> float:
    total_return = equity_curve.iloc[-1]
    if total_return <= 0 or periods <= 0:
        return -1.0
    return total_return ** (ANNUALIZATION_DAYS / periods) - 1


def compute_metrics(result: "StrategyResult") -> dict:
    equity = result.equity_curve
    returns = result.strategy_return
    drawdown = result.drawdown

    cumulative_return_pct = (equity.iloc[-1] - 1) * 100
    mdd_pct = drawdown.min() * 100

    ann_return = _annualized_return(equity, len(equity))
    ann_vol = returns.std(ddof=0) * np.sqrt(ANNUALIZATION_DAYS)
    sharpe = (ann_return - RISK_FREE_RATE) / ann_vol if ann_vol > 0 else np.nan
    calmar = ann_return / abs(mdd_pct / 100) if mdd_pct != 0 else np.nan

    wins = returns[returns > 0]
    losses = returns[returns < 0]
    avg_win = wins.mean() if len(wins) else 0.0
    avg_loss = abs(losses.mean()) if len(losses) else 0.0
    profit_loss_ratio = avg_win / avg_loss if avg_loss > 0 else np.nan
    total_decided = len(wins) + len(losses)
    win_rate_pct = (len(wins) / total_decided * 100) if total_decided > 0 else np.nan

    return {
        "cumulative_return_pct": cumulative_return_pct,
        "annualized_return_pct": ann_return * 100,
        "final_equity_usd": equity.iloc[-1] * INITIAL_CAPITAL,
        "mdd_pct": mdd_pct,
        "sharpe_ratio": sharpe,
        "calmar_ratio": calmar,
        "win_rate_pct": win_rate_pct,
        "profit_loss_ratio": profit_loss_ratio,
        "trade_count": result.trade_count,
    }


class BacktestEngine:
    """按策略模式驱动的多策略回测引擎，统一处理仓位滞后、调仓成本与绩效计算。"""

    def __init__(
        self,
        df: pd.DataFrame,
        strategies: List[BaseStrategy],
        cost_rate: float = TRADING_COST_RATE,
    ) -> None:
        self.df = df
        self.strategies = strategies
        self.cost_rate = cost_rate
        self.results: dict[str, StrategyResult] = {}

    def run(self) -> dict[str, StrategyResult]:
        btc_return = self.df["btc_return"].fillna(0.0)

        for strategy in self.strategies:
            raw_position = snap_to_grid(strategy.generate_signals(self.df).clip(0.0, 1.0))
            raw_position.name = "position"

            # 关键防未来函数处理：T 日生成的仓位需 shift(1)，
            # 使 T+1 日收益的计算依赖 T 日收盘时已经确定的仓位
            position = raw_position.shift(1)
            position.iloc[0] = 0.0  # 回测起点前视为空仓，不存在前一日持仓
            position = position.fillna(0.0)

            position_change = position.diff()
            position_change.iloc[0] = position.iloc[0]  # 建仓/首日的调仓成本
            cost = position_change.abs() * self.cost_rate

            strategy_return = position * btc_return - cost
            equity_curve = (1 + strategy_return).cumprod()

            running_max = equity_curve.cummax()
            drawdown = equity_curve / running_max - 1

            trade_count = int((position_change.abs() > 1e-9).sum())

            result = StrategyResult(
                name=strategy.name,
                raw_position=raw_position,
                position=position,
                strategy_return=strategy_return,
                equity_curve=equity_curve,
                drawdown=drawdown,
                trade_count=trade_count,
                rules=strategy.RULES,
            )
            result.metrics = compute_metrics(result)
            self.results[strategy.name] = result

        return self.results

    def best_strategy_name(self, rank_by: str = "sharpe_ratio") -> str:
        """选出用于图表标注的「最佳策略」，默认按夏普比率排序。"""
        return max(self.results, key=lambda name: _safe(self.results[name].metrics.get(rank_by)))


def _safe(value) -> float:
    return value if value is not None and not (isinstance(value, float) and np.isnan(value)) else -np.inf


# ---------------------------------------------------------------------------
# 4. 最新一日仓位预测信号
# ---------------------------------------------------------------------------


def latest_signal(result: StrategyResult) -> dict:
    """基于 T-0 日收盘数据生成的、对 T+1 日生效的仓位预测信号。"""
    latest_target = float(result.raw_position.iloc[-1])
    previous_target = float(result.raw_position.iloc[-2]) if len(result.raw_position) > 1 else 0.0

    if latest_target > previous_target:
        action = "加仓 / 买入"
    elif latest_target < previous_target:
        action = "减仓 / 卖出"
    else:
        action = "维持仓位"

    return {
        "strategy": result.name,
        "previous_position": previous_target,
        "next_position": latest_target,
        "action": action,
    }


# ---------------------------------------------------------------------------
# 5. 交互式 Plotly 可视化看板
# ---------------------------------------------------------------------------

PALETTE = [
    "#1f77b4",  # 蓝
    "#ff7f0e",  # 橙
    "#2ca02c",  # 绿
    "#d62728",  # 红
    "#9467bd",  # 紫
    "#8c564b",  # 棕
    "#e377c2",  # 粉
    "#17becf",  # 青
    "#bcbd22",  # 橄榄黄
]


def _hex_to_rgba(hex_color: str, alpha: float) -> str:
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r}, {g}, {b}, {alpha})"


def build_price_and_equity_figure(
    df: pd.DataFrame, results: dict[str, StrategyResult], best_name: str, default_visible: Tuple[str, ...] = ()
) -> go.Figure:
    strategy_names = list(results.keys())
    dates = df["timestamp"]
    # 净值/回撤曲线默认只显示 Buy & Hold 基准与最佳策略，其余策略默认收起为
    # "legendonly"（图例中可见但曲线隐藏），避免 9 条曲线一次性全部展示导致
    # 无法看清；用户可点击图例手动勾选/取消需要对比的策略。
    default_visible_set = set(default_visible) or {name for name in (strategy_names[0], best_name) if name}

    fig = make_subplots(
        rows=3,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.06,
        row_heights=[0.42, 0.36, 0.22],
        subplot_titles=(
            "BTC 价格走势与策略买卖信号（使用上方下拉菜单切换策略）",
            "各策略累计净值曲线对比（初始资金 $100,000，对数坐标；默认仅显示 Buy & Hold 与最佳策略，"
            "点击右侧图例可手动勾选/取消其他策略）",
            "各策略动态回撤 (%) 对比",
        ),
    )

    # --- 栏 1：BTC 价格 + 各策略买卖点（默认仅显示第一个策略，可通过下拉菜单切换）---
    fig.add_trace(
        go.Scatter(x=dates, y=df["btc_close"], mode="lines", name="BTC 收盘价", line=dict(color="black", width=1)),
        row=1,
        col=1,
    )

    signal_trace_indices: list[int] = []
    for i, name in enumerate(strategy_names):
        result = results[name]
        position_change = result.position.diff()
        buy_mask = position_change > 1e-9
        sell_mask = position_change < -1e-9
        visible = i == 0

        fig.add_trace(
            go.Scatter(
                x=dates[buy_mask],
                y=df["btc_close"][buy_mask],
                mode="markers",
                name=f"{name} · 买入/加仓",
                marker=dict(symbol="triangle-up", color="green", size=9, line=dict(width=1, color="darkgreen")),
                visible=visible,
                showlegend=False,
            ),
            row=1,
            col=1,
        )
        signal_trace_indices.append(len(fig.data) - 1)

        fig.add_trace(
            go.Scatter(
                x=dates[sell_mask],
                y=df["btc_close"][sell_mask],
                mode="markers",
                name=f"{name} · 卖出/减仓",
                marker=dict(symbol="triangle-down", color="red", size=9, line=dict(width=1, color="darkred")),
                visible=visible,
                showlegend=False,
            ),
            row=1,
            col=1,
        )
        signal_trace_indices.append(len(fig.data) - 1)

    # --- 栏 2 / 栏 3：各策略净值曲线（以 $100,000 初始资金换算）与回撤曲线，
    #     共用图例组（legendgroup）联动显隐。默认仅 Buy & Hold + 最佳策略可见，
    #     其余策略以 legendonly 形式收起，由用户点击图例手动选择展示对象。 ---
    for i, name in enumerate(strategy_names):
        result = results[name]
        color = PALETTE[i % len(PALETTE)]
        equity_usd = result.equity_curve * INITIAL_CAPITAL
        initial_visibility = True if name in default_visible_set else "legendonly"

        fig.add_trace(
            go.Scatter(
                x=dates,
                y=equity_usd,
                mode="lines",
                name=name,
                legendgroup=name,
                visible=initial_visibility,
                line=dict(color=color, width=1.8),
                hovertemplate="%{x|%Y-%m-%d}<br>" + name + ": $%{y:,.0f}<extra></extra>",
            ),
            row=2,
            col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=dates,
                y=result.drawdown * 100,
                mode="lines",
                name=name,
                legendgroup=name,
                showlegend=False,
                visible=initial_visibility,
                line=dict(color=color, width=1.2),
                fill="tozeroy",
                fillcolor=_hex_to_rgba(color, 0.12),
            ),
            row=3,
            col=1,
        )

    fig.update_yaxes(type="log", title_text="BTC 价格 (USDT, 对数坐标)", row=1, col=1)
    fig.update_yaxes(type="log", title_text="策略净值 (USD, 初始资金 $100,000, 对数坐标)", row=2, col=1)
    fig.update_yaxes(title_text="回撤 (%)", row=3, col=1)

    # 下拉菜单：一次只让一个策略的买卖信号在栏 1 中可见
    buttons = []
    for i, name in enumerate(strategy_names):
        visible_list = [False] * len(signal_trace_indices)
        visible_list[2 * i] = True
        visible_list[2 * i + 1] = True
        buttons.append(
            dict(label=name, method="restyle", args=[{"visible": visible_list}, signal_trace_indices])
        )

    # 图例固定在栏 2/3（净值 + 回撤）右侧、纵向排列，与栏 1 顶部的下拉菜单在空间上
    # 完全分离，避免两者重叠导致互相遮挡、点击无响应。
    row2_domain_top = fig.layout.yaxis2.domain[1]

    fig.update_layout(
        updatemenus=[
            dict(
                type="dropdown",
                buttons=buttons,
                x=0.0,
                xanchor="left",
                y=1.10,
                yanchor="top",
                active=0,
                showactive=True,
            )
        ],
        legend=dict(
            groupclick="togglegroup",
            orientation="v",
            yanchor="top",
            y=row2_domain_top,
            xanchor="left",
            x=1.02,
            title=dict(text="策略图例（点击显示/隐藏）"),
        ),
        margin=dict(t=110, b=40, l=60, r=260),
        height=1200,
        hovermode="x unified",
        template="plotly_white",
    )

    return fig


def _fmt(value, digits: int = 2, suffix: str = "") -> str:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return "N/A"
    return f"{value:.{digits}f}{suffix}"


def _fmt_usd(value) -> str:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return "N/A"
    return f"${value:,.0f}"


def build_metrics_table_html(results: dict[str, StrategyResult], best_name: str) -> str:
    """生成可点击表头排序的绩效对比表（原生 HTML + 内联 JS，自包含无外部依赖）。"""
    headers = [
        ("策略", "text"),
        ("期末净值 (10万起投)", "num"),
        ("累计收益率 (%)", "num"),
        ("年化收益率 (%)", "num"),
        ("最大回撤 MDD (%)", "num"),
        ("夏普比率", "num"),
        ("卡玛比率", "num"),
        ("胜率 (%)", "num"),
        ("盈亏比", "num"),
        ("调仓次数", "num"),
        ("最新预测信号（T+1）", "text"),
    ]

    header_html = "".join(
        f'<th data-type="{dtype}" onclick="sortTable({idx})">{escape(label)}<span class="sort-arrow"></span></th>'
        for idx, (label, dtype) in enumerate(headers)
    )

    rows_html = []
    for name, result in results.items():
        m = result.metrics
        signal = latest_signal(result)
        marker = " ⭐" if name == best_name else ""
        row_class = ' class="best-row"' if name == best_name else ""
        signal_text = f"{signal['next_position']:.0%}（{signal['action']}）"
        cells = [
            escape(name) + marker,
            _fmt_usd(m["final_equity_usd"]),
            _fmt(m["cumulative_return_pct"]),
            _fmt(m["annualized_return_pct"]),
            _fmt(m["mdd_pct"]),
            _fmt(m["sharpe_ratio"]),
            _fmt(m["calmar_ratio"]),
            _fmt(m["win_rate_pct"]),
            _fmt(m["profit_loss_ratio"]),
            str(m["trade_count"]),
            escape(signal_text),
        ]
        rows_html.append(f"<tr{row_class}>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")

    table_html = f"""
    <table id="metrics-table">
      <thead><tr>{header_html}</tr></thead>
      <tbody>{''.join(rows_html)}</tbody>
    </table>
    """
    return table_html


def build_rules_html(results: dict[str, StrategyResult]) -> str:
    """为每个策略生成可折叠的「触发条件 -> 目标仓位」规则说明（<details> 原生折叠，无需 JS）。"""
    sections = []
    for name, result in results.items():
        if not result.rules:
            continue
        rows = "".join(
            f"<tr><td>{escape(condition)}</td><td>{escape(action)}</td></tr>" for condition, action in result.rules
        )
        sections.append(
            f"""
        <details class="rule-block">
          <summary>{escape(name)}</summary>
          <table class="rule-table">
            <thead><tr><th>触发条件</th><th>买入/卖出后仓位状况</th></tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </details>
        """
        )
    return f"""
    <h2>策略规则说明（触发条件 → 目标仓位）</h2>
    <p class="hint">点击策略名称展开/折叠该策略的具体触发条件与调仓后仓位状态。</p>
    {''.join(sections)}
    """


DASHBOARD_STYLE = """
<style>
  body { font-family: "Segoe UI", "Microsoft YaHei", "PingFang SC", Arial, sans-serif; margin: 24px; color: #1a1a1a; background: #fafafa; }
  h1 { font-size: 22px; margin-bottom: 4px; }
  h2 { font-size: 17px; margin: 24px 0 8px; }
  p.subtitle { color: #666; margin-top: 0; }
  table { border-collapse: collapse; width: 100%; margin: 16px 0 28px; background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
  th, td { border: 1px solid #e0e0e0; padding: 8px 10px; text-align: center; font-size: 13px; white-space: nowrap; }
  th { background: #2c3e50; color: #fff; cursor: pointer; user-select: none; }
  th:hover { background: #3d5570; }
  tbody tr:nth-child(even) { background: #f5f7fa; }
  tr.best-row { background: #fff6da !important; font-weight: 600; }
  .sort-arrow::after { content: ""; margin-left: 4px; }
  .sort-arrow.asc::after { content: "▲"; }
  .sort-arrow.desc::after { content: "▼"; }
  .hint { color: #888; font-size: 12px; margin-bottom: 8px; }
  details.rule-block { background: #fff; border: 1px solid #e0e0e0; border-radius: 6px; margin-bottom: 10px; padding: 10px 14px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); }
  details.rule-block summary { cursor: pointer; font-weight: 600; font-size: 14px; padding: 4px 0; }
  table.rule-table { margin: 10px 0 4px; box-shadow: none; }
  table.rule-table th, table.rule-table td { white-space: normal; text-align: left; font-size: 12.5px; }
  table.rule-table th:first-child, table.rule-table td:first-child { width: 62%; }
</style>
"""

SORT_SCRIPT = """
<script>
function sortTable(colIndex) {
  const table = document.getElementById('metrics-table');
  const tbody = table.tBodies[0];
  const rows = Array.from(tbody.rows);
  const headerCell = table.tHead.rows[0].cells[colIndex];
  const dtype = headerCell.getAttribute('data-type');
  const currentDir = headerCell.getAttribute('data-dir') === 'asc' ? 'desc' : 'asc';

  Array.from(table.tHead.rows[0].cells).forEach(cell => {
    cell.removeAttribute('data-dir');
    const arrow = cell.querySelector('.sort-arrow');
    if (arrow) arrow.className = 'sort-arrow';
  });
  headerCell.setAttribute('data-dir', currentDir);
  const arrow = headerCell.querySelector('.sort-arrow');
  if (arrow) arrow.className = 'sort-arrow ' + currentDir;

  rows.sort((rowA, rowB) => {
    let a = rowA.cells[colIndex].innerText.trim();
    let b = rowB.cells[colIndex].innerText.trim();
    if (dtype === 'num') {
      a = parseFloat(a.replace(/[%$,]/g, '').replace('N/A', '-Infinity'));
      b = parseFloat(b.replace(/[%$,]/g, '').replace('N/A', '-Infinity'));
      if (isNaN(a)) a = -Infinity;
      if (isNaN(b)) b = -Infinity;
      return currentDir === 'asc' ? a - b : b - a;
    }
    return currentDir === 'asc' ? a.localeCompare(b, 'zh') : b.localeCompare(a, 'zh');
  });

  rows.forEach(row => tbody.appendChild(row));
}
</script>
"""


def build_dashboard_html(df: pd.DataFrame, results: dict[str, StrategyResult], best_name: str) -> str:
    fig = build_price_and_equity_figure(df, results, best_name)
    chart_html = fig.to_html(full_html=False, include_plotlyjs=True, div_id="backtest-chart")
    table_html = build_metrics_table_html(results, best_name)
    rules_html = build_rules_html(results)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<title>加密货币多因子量化策略回测看板</title>
{DASHBOARD_STYLE}
</head>
<body>
<h1>加密货币多因子量化策略回测与可视化看板</h1>
<p class="subtitle">数据截至 {df['timestamp'].iloc[-1].date()} · 最佳策略（按夏普比率排序）：<b>{escape(best_name)}</b></p>
<p class="hint">点击表头可按该列排序（再次点击切换升序/降序）。</p>
{table_html}
{chart_html}
{rules_html}
{SORT_SCRIPT}
</body>
</html>
"""


def build_dashboard(df: pd.DataFrame, results: dict[str, StrategyResult], best_name: str, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    html = build_dashboard_html(df, results, best_name)
    output_path.write_text(html, encoding="utf-8")


# ---------------------------------------------------------------------------
# 6. Markdown 报告
# ---------------------------------------------------------------------------


def build_markdown_report(results: dict[str, StrategyResult], best_name: str) -> str:
    lines = ["## 加密货币多因子量化策略回测报告", ""]
    lines.append(f"（假设初始资金 ${INITIAL_CAPITAL:,.0f}，按各策略净值曲线折算「期末净值」一列）")
    lines.append("")

    lines.append(
        "| 策略 | 期末净值 ($) | 累计收益率 (%) | 年化收益率 (%) | 最大回撤 MDD (%) | 夏普比率 | 卡玛比率 | 胜率 (%) | 盈亏比 | 调仓次数 |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")

    for name, result in results.items():
        m = result.metrics
        marker = " ⭐" if name == best_name else ""
        lines.append(
            f"| {name}{marker} | {_fmt_usd(m['final_equity_usd'])} | {_fmt(m['cumulative_return_pct'])} | "
            f"{_fmt(m['annualized_return_pct'])} | "
            f"{_fmt(m['mdd_pct'])} | {_fmt(m['sharpe_ratio'])} | {_fmt(m['calmar_ratio'])} | "
            f"{_fmt(m['win_rate_pct'])} | {_fmt(m['profit_loss_ratio'])} | {m['trade_count']} |"
        )

    lines.append("")
    lines.append(f"最佳策略（按夏普比率排序）：**{best_name}**")
    lines.append("")
    lines.append("### 最新一日（T-0）仓位预测信号")
    lines.append("")
    lines.append("| 策略 | 前一日仓位 | 下一交易日目标仓位 | 建议动作 |")
    lines.append("| --- | --- | --- | --- |")
    for name, result in results.items():
        signal = latest_signal(result)
        lines.append(
            f"| {name} | {signal['previous_position']:.0%} | {signal['next_position']:.0%} | {signal['action']} |"
        )
    lines.append("")
    lines.append("完整交互式看板（可切换策略、排序表格、缩放图表）请在 Workflow Artifacts 中下载 "
                 "`backtest_dashboard_<UTC时间戳>.html` 查看。")
    lines.append("")
    lines.append("### 策略规则说明（触发条件 → 目标仓位）")
    lines.append("")
    for name, result in results.items():
        if not result.rules:
            continue
        lines.append(f"**{name}**")
        lines.append("")
        lines.append("| 触发条件 | 买入/卖出后仓位状况 |")
        lines.append("| --- | --- |")
        for condition, action in result.rules:
            lines.append(f"| {condition} | {action} |")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 7. CLI 入口
# ---------------------------------------------------------------------------


def build_strategies() -> List[BaseStrategy]:
    return [
        BuyAndHoldStrategy(),
        TrendFollowingStrategy(),
        ValuationMeanReversionStrategy(),
        SentimentRegimeStrategy(),
        MultiFactorScoringStrategy(),
        DynamicVolTargetingStrategy(),
        DonchianBreakoutStrategy(),
        FundingRateSqueezeStrategy(),
    ]


def main() -> None:
    # 保证控制台输出中文/emoji 时不因本地编码（如 Windows 下的 GBK）而报错
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="加密货币多因子量化策略回测与可视化系统")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR, help="数据目录路径")
    parser.add_argument("--reports-dir", type=Path, default=REPORTS_DIR, help="报告输出目录路径")
    args = parser.parse_args()

    df = load_dataset(args.data_dir)

    engine = BacktestEngine(df, build_strategies())
    results = engine.run()
    best_name = engine.best_strategy_name()

    args.reports_dir.mkdir(parents=True, exist_ok=True)

    # 文件名带 UTC 时间戳，避免每次运行相互覆盖，便于在 reports/ 目录中追溯历史看板。
    run_timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dashboard_path = args.reports_dir / f"backtest_dashboard_{run_timestamp}.html"
    build_dashboard(df, results, best_name, dashboard_path)

    report_markdown = build_markdown_report(results, best_name)

    # 仅打印到 stdout（供 CI 捕获后写入 $GITHUB_STEP_SUMMARY），不再落盘为
    # reports/performance_summary.md —— reports/ 目录只产出带 UTC 时间戳的
    # backtest_dashboard_<timestamp>.html（每次运行生成独立文件，便于追溯历史看板并提交入库）。
    print(report_markdown)
    print(f"交互式看板已保存至: {dashboard_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
