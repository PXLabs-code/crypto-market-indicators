"""加密货币多因子量化策略回测与可视化系统。

读取 ``data/`` 目录下由 ``update_data.py`` 维护的日度 CSV 数据（BTC 现货 OHLCV、
BTC MVRV、BTC 资金费率、市场恐惧与贪婪指数），对齐为统一的日频面板数据后，
对三套仓位策略（Buy & Hold 基准、情绪与估值阈值、多因子加权打分）进行历史回测，
计算绩效指标，并生成三栏可视化看板 PNG 及 Markdown 绩效对比表。

设计原则：
    - 严禁未来函数：所有需要 T+1 日才能知道的仓位一律 ``.shift(1)`` 后再参与收益计算。
    - 缺失值仅允许 ``.ffill()``，不允许 ``.bfill()``，避免用未来数据填补历史空值。
"""

from __future__ import annotations

import argparse
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

import matplotlib

matplotlib.use("Agg")  # 无显示环境下生成图片，必须在 pyplot 导入前设置

import matplotlib.dates as mdates
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


def _configure_cjk_font() -> None:
    """尽量选用系统内可用的中文字体，避免图表中的中文显示为方块（tofu）。

    在 GitHub Actions（ubuntu-latest）上建议预先安装 ``fonts-noto-cjk``；
    本地 Windows/macOS 常见中文字体也在候选列表中，找不到时静默回退到默认字体。
    """
    candidates = [
        "Noto Sans CJK SC",
        "Noto Sans CJK",
        "Microsoft YaHei",
        "SimHei",
        "PingFang SC",
        "WenQuanYi Zen Hei",
        "Source Han Sans SC",
    ]
    available = {f.name for f in fm.fontManager.ttflist}
    for name in candidates:
        if name in available:
            matplotlib.rcParams["font.sans-serif"] = [name]
            break
    matplotlib.rcParams["axes.unicode_minus"] = False


_configure_cjk_font()

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
MA_LONG_WINDOW = 60

# 仓位分级仓位标签，供图表 / 报告展示使用
POSITION_LABELS = {0.0: "空仓 0%", 0.3: "轻仓 30%", 0.5: "半仓 50%", 0.6: "中仓 60%", 1.0: "满仓 100%"}


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

    # 均线未形成前的行同样无法参与均线趋势打分，一并丢弃保持面板数据完整可用
    df = df.dropna(subset=["ma_short", "ma_long"]).reset_index(drop=True)

    return df


# ---------------------------------------------------------------------------
# 2. 策略架构（策略模式）
# ---------------------------------------------------------------------------


class BaseStrategy(ABC):
    """策略基类：子类只需实现 ``generate_signals``，返回当日收盘后决定的目标仓位。

    返回的 ``position`` 序列语义为「T 日收盘后，基于 T 日及之前可得信息决定的目标仓位」，
    取值范围 [0.0, 1.0]。回测引擎会对其整体 ``shift(1)`` 后再与 T+1 日收益率相乘，
    从而保证不使用未来函数。
    """

    name: str = "BaseStrategy"

    @abstractmethod
    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        raise NotImplementedError


class BuyAndHoldStrategy(BaseStrategy):
    """基准策略：全程满仓 100%。"""

    name = "Buy & Hold 基准"

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        return pd.Series(1.0, index=df.index, name="position")


class SentimentValuationStrategy(BaseStrategy):
    """情绪与估值阈值策略：结合 FGI（情绪）与 MVRV（估值）划分 0% / 50% / 100% 仓位。

    阈值假设（可按需调整）：
        - 极度贪婪（FGI >= 75）且明显高估（MVRV >= 3.0） -> 清仓 0%，警惕顶部风险。
        - 极度恐惧（FGI <= 25）且明显低估（MVRV <= 1.0） -> 满仓 100%，博反转。
        - 其余情况 -> 半仓 50%，中性持有。
    """

    name = "情绪估值阈值策略"

    FGI_EXTREME_GREED = 75
    FGI_EXTREME_FEAR = 25
    MVRV_OVERVALUED = 3.0
    MVRV_UNDERVALUED = 1.0

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        fgi = df["fear_greed_value"]
        mvrv = df["mvrv"]

        position = pd.Series(0.5, index=df.index, name="position")

        overheated = (fgi >= self.FGI_EXTREME_GREED) & (mvrv >= self.MVRV_OVERVALUED)
        oversold = (fgi <= self.FGI_EXTREME_FEAR) & (mvrv <= self.MVRV_UNDERVALUED)

        position[overheated] = 0.0
        position[oversold] = 1.0
        return position


class MultiFactorScoringStrategy(BaseStrategy):
    """多因子加权打分策略：MVRV(35%) + FGI(25%) + 资金费率(20%) + 均线趋势(20%) -> 0-100 综合分。

    各因子先分别归一化为 0-100 的「看多分数」（分数越高越看多），加权求和后
    映射到 0% / 30% / 60% / 100% 四级仓位。
    """

    name = "多因子加权打分策略"

    WEIGHT_MVRV = 0.35
    WEIGHT_FGI = 0.25
    WEIGHT_FUNDING = 0.20
    WEIGHT_MA = 0.20

    # MVRV 归一化区间：<=0.8 视为深度低估(100分)，>=3.5 视为历史级高估(0分)
    MVRV_LOW, MVRV_HIGH = 0.8, 3.5
    # 资金费率归一化区间：正费率越高代表多头拥挤程度越高（看空），负费率相反（看多）
    FUNDING_EXTREME = 0.001  # 0.1%，日均资金费率的极端参考值

    SCORE_THRESHOLDS = [(75, 1.0), (55, 0.6), (35, 0.3), (0, 0.0)]

    @staticmethod
    def _clip01(series: pd.Series) -> pd.Series:
        return series.clip(lower=0.0, upper=1.0)

    def _score_mvrv(self, mvrv: pd.Series) -> pd.Series:
        normalized = (self.MVRV_HIGH - mvrv) / (self.MVRV_HIGH - self.MVRV_LOW)
        return self._clip01(normalized) * 100

    def _score_fgi(self, fgi: pd.Series) -> pd.Series:
        # FGI 本身即恐惧(低分=看多)到贪婪(高分=看空)的 0-100 指标，反转即为看多分数
        return 100 - fgi

    def _score_funding(self, funding_rate: pd.Series) -> pd.Series:
        normalized = 0.5 - (funding_rate / self.FUNDING_EXTREME) * 0.5
        return self._clip01(normalized) * 100

    def _score_ma_trend(self, df: pd.DataFrame) -> pd.Series:
        bullish = (df["btc_close"] > df["ma_long"]) & (df["ma_short"] > df["ma_long"])
        bearish = (df["btc_close"] < df["ma_long"]) & (df["ma_short"] < df["ma_long"])
        score = pd.Series(50.0, index=df.index)
        score[bullish] = 100.0
        score[bearish] = 0.0
        return score

    def composite_score(self, df: pd.DataFrame) -> pd.Series:
        score = (
            self.WEIGHT_MVRV * self._score_mvrv(df["mvrv"])
            + self.WEIGHT_FGI * self._score_fgi(df["fear_greed_value"])
            + self.WEIGHT_FUNDING * self._score_funding(df["funding_rate"])
            + self.WEIGHT_MA * self._score_ma_trend(df)
        )
        return score

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        score = self.composite_score(df)
        position = pd.Series(0.0, index=df.index, name="position")
        # 必须按阈值从低到高依次赋值，让更高档位的赋值覆盖更低档位，
        # 否则最后一次循环（>=0 恒真）会把所有仓位错误地重置为最低档。
        for threshold, weight in sorted(self.SCORE_THRESHOLDS, key=lambda item: item[0]):
            position[score >= threshold] = weight
        return position


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

    return {
        "cumulative_return_pct": cumulative_return_pct,
        "annualized_return_pct": ann_return * 100,
        "mdd_pct": mdd_pct,
        "sharpe_ratio": sharpe,
        "calmar_ratio": calmar,
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
            raw_position = strategy.generate_signals(self.df).clip(0.0, 1.0)
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
# 5. 可视化看板
# ---------------------------------------------------------------------------


def plot_dashboard(df: pd.DataFrame, results: dict[str, StrategyResult], best_name: str, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fig, (ax_price, ax_equity, ax_dd) = plt.subplots(
        3, 1, figsize=(16, 14), dpi=150, sharex=True, gridspec_kw={"height_ratios": [2, 1.4, 1]}
    )

    dates = df["timestamp"]
    best_result = results[best_name]

    # --- 栏 1：BTC 对数价格 + 最佳策略买卖点 ---
    ax_price.plot(dates, df["btc_close"], color="black", linewidth=1.0, label="BTC 收盘价")
    ax_price.set_yscale("log")
    ax_price.set_ylabel("BTC 价格 (USDT, 对数坐标)")
    ax_price.set_title(f"BTC 价格走势与最佳策略交易信号（{best_name}）")

    position_change = best_result.position.diff()
    buy_points = position_change > 1e-9
    sell_points = position_change < -1e-9

    ax_price.scatter(
        dates[buy_points], df["btc_close"][buy_points], marker="^", color="green", s=60, zorder=5, label="买入/加仓"
    )
    ax_price.scatter(
        dates[sell_points], df["btc_close"][sell_points], marker="v", color="red", s=60, zorder=5, label="卖出/减仓"
    )
    ax_price.legend(loc="upper left")
    ax_price.grid(True, which="both", linestyle="--", alpha=0.3)

    # --- 栏 2：各策略累计净值对比（对数坐标）---
    for name, result in results.items():
        style = "-" if name == best_name else "--"
        linewidth = 2.0 if name == best_name else 1.2
        ax_equity.plot(dates, result.equity_curve, style, linewidth=linewidth, label=name)
    ax_equity.set_yscale("log")
    ax_equity.set_ylabel("策略净值 (对数坐标)")
    ax_equity.set_title("各策略累计净值曲线对比")
    ax_equity.legend(loc="upper left")
    ax_equity.grid(True, which="both", linestyle="--", alpha=0.3)

    # --- 栏 3：最佳策略动态回撤（线性坐标，红色填充）---
    drawdown_pct = best_result.drawdown * 100
    ax_dd.fill_between(dates, drawdown_pct, 0, color="red", alpha=0.3)
    ax_dd.plot(dates, drawdown_pct, color="darkred", linewidth=0.8)
    ax_dd.set_ylabel("回撤 (%)")
    ax_dd.set_title(f"最佳策略动态回撤（{best_name}）")
    ax_dd.grid(True, linestyle="--", alpha=0.3)

    ax_dd.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax_dd.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    fig.autofmt_xdate()

    fig.tight_layout()
    fig.savefig(output_path)
    plt.close(fig)


# ---------------------------------------------------------------------------
# 6. Markdown 报告
# ---------------------------------------------------------------------------


def build_markdown_report(results: dict[str, StrategyResult], best_name: str) -> str:
    lines = ["## 加密货币多因子量化策略回测报告", ""]

    lines.append(
        "| 策略 | 累计收益率 (%) | 年化收益率 (%) | 最大回撤 MDD (%) | 夏普比率 | 卡玛比率 | 盈亏比 | 调仓次数 |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")

    def fmt(value: float, digits: int = 2) -> str:
        if value is None or (isinstance(value, float) and np.isnan(value)):
            return "N/A"
        return f"{value:.{digits}f}"

    for name, result in results.items():
        m = result.metrics
        marker = " ⭐" if name == best_name else ""
        lines.append(
            f"| {name}{marker} | {fmt(m['cumulative_return_pct'])} | {fmt(m['annualized_return_pct'])} | "
            f"{fmt(m['mdd_pct'])} | {fmt(m['sharpe_ratio'])} | {fmt(m['calmar_ratio'])} | "
            f"{fmt(m['profit_loss_ratio'])} | {m['trade_count']} |"
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

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 7. CLI 入口
# ---------------------------------------------------------------------------


def build_strategies() -> List[BaseStrategy]:
    return [
        BuyAndHoldStrategy(),
        SentimentValuationStrategy(),
        MultiFactorScoringStrategy(),
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

    dashboard_path = args.reports_dir / "backtest_dashboard.png"
    plot_dashboard(df, results, best_name, dashboard_path)

    report_markdown = build_markdown_report(results, best_name)
    summary_path = args.reports_dir / "performance_summary.md"
    summary_path.write_text(report_markdown, encoding="utf-8")

    print(report_markdown)
    print(f"看板图已保存至: {dashboard_path}")
    print(f"Markdown 报告已保存至: {summary_path}")


if __name__ == "__main__":
    main()
