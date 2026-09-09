"""加密货币多因子量化策略回测与可视化系统。

读取 ``data/`` 目录下由 ``update_data.py`` 维护的日度 CSV 数据（BTC 现货 OHLCV、
BTC MVRV、BTC 资金费率、市场恐惧与贪婪指数），对齐为统一的日频面板数据后，
对 6 套仓位策略进行历史回测、计算绩效指标，并生成自包含的交互式 Plotly HTML 看板
以及 Markdown 绩效对比表。

设计原则：
    - 严禁未来函数：所有需要 T+1 日才能知道的仓位一律 ``.shift(1)`` 后再参与收益计算。
    - 缺失值仅允许 ``.ffill()``，不允许 ``.bfill()``，避免用未来数据填补历史空值。
    - 所有策略均输出分级仓位 {0%, 25%, 50%, 75%, 100%}，非仓位打分类信号最终都会
      对齐（snap）到这一仓位网格上。
"""

from __future__ import annotations

import argparse
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import List

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

    # 均线、波动率、分位数等滚动特征形成之前的行无法参与策略评估，一并丢弃
    df = df.dropna(subset=["ma_short", "ma_long", "realized_vol", "mvrv_percentile"]).reset_index(drop=True)

    return df


# ---------------------------------------------------------------------------
# 2. 策略架构（策略模式）
# ---------------------------------------------------------------------------


class BaseStrategy(ABC):
    """策略基类：子类只需实现 ``generate_signals``，返回当日收盘后决定的目标仓位。

    返回的 ``position`` 序列语义为「T 日收盘后，基于 T 日及之前可得信息决定的目标仓位」，
    取值须来自分级仓位网格 ``POSITION_GRID``（0% / 25% / 50% / 75% / 100%）。
    回测引擎会对其整体 ``shift(1)`` 后再与 T+1 日收益率相乘，从而保证不使用未来函数。
    """

    name: str = "BaseStrategy"

    @abstractmethod
    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        raise NotImplementedError


class BuyAndHoldStrategy(BaseStrategy):
    """a) Benchmark: BTC Buy & Hold —— 全程满仓 100%。"""

    name = "a) Buy & Hold 基准"

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        return pd.Series(1.0, index=df.index, name="position")


class TrendFollowingStrategy(BaseStrategy):
    """b) Trend Following：双均线（MA20/MA200）趋势策略。

    以短均线相对长均线的偏离幅度衡量趋势强度，偏离越大仓位越高（金叉顺势），
    偏离为负且幅度越大仓位越低（死叉减仓/清仓）。
    """

    name = "b) 双均线趋势策略"

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

    # (分位数上限, 仓位) —— 分位数从低到高分级
    THRESHOLDS = [(0.10, 1.0), (0.30, 0.75), (0.70, 0.5), (0.90, 0.25)]
    CEILING_WEIGHT = 0.0  # 分位数 > 0.90（历史级高估）时清仓

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        percentile = df["mvrv_percentile"]
        position = pd.Series(self.CEILING_WEIGHT, index=df.index, name="position")
        for threshold, weight in sorted(self.THRESHOLDS, key=lambda item: -item[0]):
            position[percentile <= threshold] = weight
        return position


class SentimentRegimeStrategy(BaseStrategy):
    """d) Sentiment Regime：Fear & Greed 极值反转策略。

    情绪指数越接近「极度恐惧」越逆势加仓，越接近「极度贪婪」越逆势减仓，
    在两端之间按情绪强度分级过渡。
    """

    name = "d) 情绪极值反转策略"

    # (FGI 上限, 仓位) —— FGI 从低（恐惧）到高（贪婪）分级
    THRESHOLDS = [(20, 1.0), (40, 0.75), (60, 0.5), (80, 0.25)]
    CEILING_WEIGHT = 0.0  # FGI > 80（极度贪婪）时清仓

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        fgi = df["fear_greed_value"]
        position = pd.Series(self.CEILING_WEIGHT, index=df.index, name="position")
        for threshold, weight in sorted(self.THRESHOLDS, key=lambda item: -item[0]):
            position[fgi <= threshold] = weight
        return position


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

    SCORE_THRESHOLDS = [(80, 1.0), (65, 0.75), (45, 0.5), (30, 0.25)]
    FLOOR_WEIGHT = 0.0  # 综合分 < 30 时清仓

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        score = compute_composite_score(df)
        position = pd.Series(self.FLOOR_WEIGHT, index=df.index, name="position")
        for threshold, weight in sorted(self.SCORE_THRESHOLDS, key=lambda item: item[0]):
            position[score >= threshold] = weight
        return position


class DynamicVolTargetingStrategy(BaseStrategy):
    """f) Dynamic Vol-Targeting：多因子打分 * 波动率反比的动态风险控制策略。

    以多因子综合分（0-100 映射到 0-1）作为基础仓位方向，再乘以「目标波动率 / 已实现
    波动率」的风险缩放系数：已实现波动率越高（市场越剧烈），仓位相应收缩；波动率越低
    （市场越平稳），在基础方向允许范围内适度放大仓位。风险缩放系数设置上下限以避免
    极端放大或过度清零，最终结果吸附到分级仓位网格。
    """

    name = "f) 动态波动率目标策略"

    TARGET_ANNUAL_VOL = 0.5  # 目标年化波动率 50%，作为风险预算基准
    RISK_SCALE_MIN, RISK_SCALE_MAX = 0.2, 1.5

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        base_position = compute_composite_score(df) / 100.0
        risk_scale = (self.TARGET_ANNUAL_VOL / df["realized_vol"]).clip(
            lower=self.RISK_SCALE_MIN, upper=self.RISK_SCALE_MAX
        )
        raw_position = (base_position * risk_scale).clip(0.0, 1.0)
        return snap_to_grid(raw_position)


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
]


def _hex_to_rgba(hex_color: str, alpha: float) -> str:
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r}, {g}, {b}, {alpha})"


def build_price_and_equity_figure(df: pd.DataFrame, results: dict[str, StrategyResult]) -> go.Figure:
    strategy_names = list(results.keys())
    dates = df["timestamp"]

    fig = make_subplots(
        rows=3,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.06,
        row_heights=[0.42, 0.36, 0.22],
        subplot_titles=(
            "BTC 价格走势与策略买卖信号（使用上方下拉菜单切换策略）",
            "各策略累计净值曲线对比（对数坐标，点击图例可显示/隐藏）",
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

    # --- 栏 2 / 栏 3：各策略净值曲线与回撤曲线，共用图例组（legendgroup）联动显隐 ---
    for i, name in enumerate(strategy_names):
        result = results[name]
        color = PALETTE[i % len(PALETTE)]

        fig.add_trace(
            go.Scatter(
                x=dates,
                y=result.equity_curve,
                mode="lines",
                name=name,
                legendgroup=name,
                line=dict(color=color, width=1.8),
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
                line=dict(color=color, width=1.2),
                fill="tozeroy",
                fillcolor=_hex_to_rgba(color, 0.12),
            ),
            row=3,
            col=1,
        )

    fig.update_yaxes(type="log", title_text="BTC 价格 (USDT, 对数坐标)", row=1, col=1)
    fig.update_yaxes(type="log", title_text="策略净值 (对数坐标)", row=2, col=1)
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
        legend=dict(groupclick="togglegroup", orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0.0),
        margin=dict(t=110, b=40, l=60, r=30),
        height=1200,
        hovermode="x unified",
        template="plotly_white",
    )

    return fig


def _fmt(value, digits: int = 2, suffix: str = "") -> str:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return "N/A"
    return f"{value:.{digits}f}{suffix}"


def build_metrics_table_html(results: dict[str, StrategyResult], best_name: str) -> str:
    """生成可点击表头排序的绩效对比表（原生 HTML + 内联 JS，自包含无外部依赖）。"""
    headers = [
        ("策略", "text"),
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


DASHBOARD_STYLE = """
<style>
  body { font-family: "Segoe UI", "Microsoft YaHei", "PingFang SC", Arial, sans-serif; margin: 24px; color: #1a1a1a; background: #fafafa; }
  h1 { font-size: 22px; margin-bottom: 4px; }
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
      a = parseFloat(a.replace('%', '').replace('N/A', '-Infinity'));
      b = parseFloat(b.replace('%', '').replace('N/A', '-Infinity'));
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
    fig = build_price_and_equity_figure(df, results)
    chart_html = fig.to_html(full_html=False, include_plotlyjs=True, div_id="backtest-chart")
    table_html = build_metrics_table_html(results, best_name)

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

    lines.append(
        "| 策略 | 累计收益率 (%) | 年化收益率 (%) | 最大回撤 MDD (%) | 夏普比率 | 卡玛比率 | 胜率 (%) | 盈亏比 | 调仓次数 |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")

    for name, result in results.items():
        m = result.metrics
        marker = " ⭐" if name == best_name else ""
        lines.append(
            f"| {name}{marker} | {_fmt(m['cumulative_return_pct'])} | {_fmt(m['annualized_return_pct'])} | "
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
                 "`backtest_dashboard.html` 查看。")
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

    dashboard_path = args.reports_dir / "backtest_dashboard.html"
    build_dashboard(df, results, best_name, dashboard_path)

    report_markdown = build_markdown_report(results, best_name)
    summary_path = args.reports_dir / "performance_summary.md"
    summary_path.write_text(report_markdown, encoding="utf-8")

    print(report_markdown)
    print(f"交互式看板已保存至: {dashboard_path}")
    print(f"Markdown 报告已保存至: {summary_path}")


if __name__ == "__main__":
    main()
