# crypto-market-indicators

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[English](README.en.md) | 简体中文

自动采集并维护加密货币市场指标数据，并提供一套多因子量化策略回测与可视化系统。目前支持：

- Coin Metrics：BTC、ETH 的 MVRV（`CapMVRVCur`）
- Binance Spot：BTC/USDT、ETH/USDT 日线 OHLCV
- Binance Futures：BTC/USDT、ETH/USDT 永续合约资金费率
- Alternative.me：Crypto Fear & Greed Index（恐惧与贪婪指数）

采集结果以 CSV 文件保存在 `data/` 目录中，可用于市场分析、量化研究、策略回测和数据可视化。项目支持本地运行，也可通过 GitHub Actions 每日自动更新并提交数据。

## 项目结构

```text
.
├── .github/
│   └── workflows/
│       ├── update_data.yml      # GitHub Actions 自动更新任务
│       └── backtest.yml         # GitHub Actions 策略回测任务
├── scripts/
│   ├── update_data.py           # 数据采集、合并、校验与写入逻辑
│   └── backtest.py              # 多因子策略回测与交互式看板生成
├── tests/
│   └── test_update_data.py      # 数据连续性、去重和空值校验测试
├── data/                        # 运行脚本后生成或更新的数据目录
├── reports/                     # 回测生成的交互式看板（带 UTC 时间戳）
├── requirements.txt
├── LICENSE
└── README.md
```

## 功能说明

### 数据采集

| 数据集 | 数据源 | 资产/交易对 | 频率 |
| --- | --- | --- | --- |
| MVRV | Coin Metrics Community API | BTC、ETH | 1 天 |
| Spot OHLCV | Binance Spot API | BTCUSDT、ETHUSDT | 1 天 |
| Funding Rate | Binance Futures API | BTCUSDT、ETHUSDT | 8 小时 |
| Fear & Greed Index | Alternative.me API | 全市场 | 1 天 |

### 增量更新

脚本会读取已有 CSV 的最后一个时间戳，只请求后续数据。新旧数据合并后会：

1. 按时间戳排序；
2. 删除重复时间戳，并保留最新记录；
3. 检查时间戳是否连续；
4. 检查是否存在缺失或无效值；
5. 仅在数据发生变化时写入 CSV。

如果检测到数据断层、缺失时间戳或空值，脚本会抛出异常并拒绝写入，避免损坏已有数据。
Fear & Greed Index 仅对下文列出的已知上游历史缺日作例外；其他数据集仍执行严格连续性校验。

对于 Binance 数据，请求会优先使用公开的 market-data-only 现货端点，并为 Spot / Futures 配置有限候选端点、有限次数重试和指数退避。若遇到 HTTP 403/451 等访问限制，会尽快切换到下一个候选端点；若遇到 HTTP 429、5xx、超时或连接异常，则会在上限内重试并记录可诊断日志。

## 安装

### 环境要求

- Python 3.10 或更高版本
- 可访问 Coin Metrics、Binance 和 Alternative.me API 的网络环境

GitHub Actions 使用 Python 3.12。

### 克隆项目

```bash
git clone https://github.com/PXLabs-code/crypto-market-indicators.git
cd crypto-market-indicators
```

### 创建虚拟环境

Linux/macOS：

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Windows PowerShell：

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
```

### 安装依赖

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

目前使用的主要依赖为：

- `pandas`：时间序列处理和 CSV 读写
- `requests`：调用第三方 HTTP API

当前使用的都是公开接口，不需要配置 API Key 或其他环境变量。

## 使用方法

### 更新全部数据

```bash
python scripts/update_data.py
```

脚本会依次更新：

- BTC MVRV、现货日线和资金费率
- ETH MVRV、现货日线和资金费率
- 恐惧与贪婪指数

首次运行时会创建 `data/` 目录；后续运行会在已有 CSV 基础上增量更新。

### 运行测试

```bash
python -m unittest discover -s tests
```

测试覆盖：

- 日频时间序列连续性
- 时间序列断层检测
- 新旧数据合并与去重
- 缺失值检测
- Binance 端点切换、有限重试、日志截断和全部端点失败

## 数据目录与格式

运行成功后，数据按以下结构保存：

```text
data/
├── btc/
│   ├── mvrv.csv
│   ├── spot_ohlcv.csv
│   └── funding_rates.csv
├── eth/
│   ├── mvrv.csv
│   ├── spot_ohlcv.csv
│   └── funding_rates.csv
└── market/
    └── fear_greed.csv
```

所有 `timestamp` 字段均使用 UTC，并以 ISO 8601 格式导出：

```text
YYYY-MM-DDTHH:MM:SSZ
```

### `data/{asset}/mvrv.csv`

BTC 和 ETH 的 MVRV 数据。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `timestamp` | datetime | UTC 数据时间 |
| `mvrv` | float | Coin Metrics `CapMVRVCur` 指标值 |

示例：

```csv
timestamp,mvrv
2026-01-01T00:00:00Z,1.85
```

### `data/{asset}/spot_ohlcv.csv`

Binance 现货 1 日 K 线数据。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `timestamp` | datetime | K 线开盘时间（UTC） |
| `open` | float | 开盘价 |
| `high` | float | 最高价 |
| `low` | float | 最低价 |
| `close` | float | 收盘价 |
| `volume` | float | 基础资产成交量 |

示例：

```csv
timestamp,open,high,low,close,volume
2026-01-01T00:00:00Z,90000.0,92000.0,89000.0,91000.0,12345.67
```

### `data/{asset}/funding_rates.csv`

Binance USDⓈ-M 永续合约资金费率数据。
如果 Binance 资金费率接口暂时不可用或因地域限制无法访问，更新脚本会输出非致命问题摘要和 GitHub Actions warning 注解；当 `FUNDING_FAILURE_POLICY=fail` 时会直接失败退出。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `timestamp` | datetime | 资金费率结算时间（UTC） |
| `funding_rate` | float | 资金费率的小数值，例如 `0.0001` 表示 `0.01%` |

示例：

```csv
timestamp,funding_rate
2026-01-01T00:00:00Z,0.0001
```

### `data/market/fear_greed.csv`

Alternative.me Crypto Fear & Greed Index 数据。

上游完整历史存在已知缺日：`2018-04-14`、`2018-04-15`、`2018-04-16` 和 `2024-10-26`（参见[公开采集记录](https://github.com/jungmin127/study/blob/f685267e772b6d95453b4dbbbd9632acc3079924/260711-upbit-v1/external_data_service.py#L26-L44)及 [2024-10-26 缺日的独立记录](https://github.com/cancleeric/trustforge/blob/6db496cc8b4b7fe2a5e09d8a003c715365441693/docs/qa/HERMES-FIVE-YEAR-BACKFILL-STARTED-2026-07-17.md#L5-L18)）。
校验仅允许这些日期缺失并记录警告，不插值、不前向填充、不生成虚构数据；若上游补回这些日期，会正常合并。
其他缺日、异常时间戳和空值仍会导致更新失败，并保留已有 CSV。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `timestamp` | datetime | UTC 数据时间 |
| `fear_greed_value` | integer | 指数值，范围通常为 0–100 |
| `classification` | string | 市场情绪分类，如 `Extreme Fear`、`Fear`、`Neutral`、`Greed` 或 `Extreme Greed` |

示例：

```csv
timestamp,fear_greed_value,classification
2026-01-01T00:00:00Z,65,Greed
```

### 使用 Pandas 读取数据

```python
import pandas as pd

btc_ohlcv = pd.read_csv(
    "data/btc/spot_ohlcv.csv",
    parse_dates=["timestamp"],
)

btc_mvrv = pd.read_csv(
    "data/btc/mvrv.csv",
    parse_dates=["timestamp"],
)

print(btc_ohlcv.tail())
print(btc_mvrv.tail())
```

## GitHub Actions 自动更新

工作流文件位于 `.github/workflows/update_data.yml`，名称为 **Update Market Data**。

### 自动执行时间

工作流使用以下 cron 配置：

```yaml
schedule:
  - cron: '5 0 * * *'
```

它每天在 **00:05 UTC** 自动运行，即北京时间 **08:05**。GitHub Actions 的定时任务可能因平台负载而延迟几分钟执行。

### 自动更新流程

工作流会：

1. 检出仓库代码；
2. 安装 Python 3.12；
3. 安装 `requirements.txt` 中的依赖；
4. 执行 `python scripts/update_data.py`；
5. 检查 `data/` 目录是否有变化；
6. 如果有变化，自动提交并推送到当前分支；
7. 如果没有变化，则正常结束且不创建空提交。

当前工作流使用 `actions/checkout@v7` 和 `actions/setup-python@v7`，以兼容 GitHub-hosted runner 上的 Node.js 24。

自动提交信息格式为：

```text
Update market data: YYYY-MM-DD
```

### 手动触发

工作流配置了 `workflow_dispatch`，可从 GitHub 页面手动运行：

1. 打开仓库的 **Actions** 页面；
2. 选择 **Update Market Data**；
3. 点击 **Run workflow**；
4. 选择目标分支并确认运行。

### 权限要求

工作流需要向仓库提交更新，因此配置了：

```yaml
permissions:
  contents: write
```

如果工作流可以采集数据但无法推送，请检查：

1. 仓库 **Settings → Actions → General → Workflow permissions**；
2. 工作流是否具有 `contents: write` 权限；
3. 默认分支保护规则是否禁止 GitHub Actions 直接推送；
4. 组织级 Actions 策略是否覆盖了仓库配置。

如果默认分支要求所有修改必须通过 Pull Request，请调整工作流为创建分支和 Pull Request，而不是直接执行 `git push`。

## 添加新的资产

`update_asset()` 接收 Coin Metrics 资产代码和 Binance 交易对。要增加其他资产，可在 `main()` 中加入调用：

```python
update_asset("sol", "SOLUSDT")
```

添加前请确认：

- Coin Metrics Community API 提供该资产的 `CapMVRVCur` 指标；
- Binance Spot 和 USDⓈ-M Futures 均支持对应交易对；
- 数据频率满足当前严格连续性校验规则。

## 多因子量化策略回测与可视化

除数据采集外，仓库还提供一套完整的加密货币多因子量化策略回测系统（`scripts/backtest.py`），基于 `data/` 目录中的日度数据自动生成交互式 HTML 看板。

### 数据预处理

- 资金费率按 UTC 日度聚合取均值（`groupby(date).mean()`）。
- 所有数据按 `timestamp` 以 BTC 价格日期为主轴左外连接。
- 缺失值仅使用 `.ffill()` 前向填充，不使用 `.bfill()`，避免未来数据泄露。

### 策略列表

采用策略模式（`BaseStrategy` 基类 + 子类 `generate_signals(df)`），所有策略仓位均为 `[0%, 25%, 50%, 75%, 100%]` 分级仓位，且 T 日生成的仓位统一 `shift(1)` 后于 T+1 日生效，避免未来函数：

| 策略 | 说明 |
| --- | --- |
| a) Buy & Hold 基准 | 全程 100% 仓位 |
| b) 双均线趋势策略 | MA20/MA200 交叉与偏离幅度分级 |
| c) MVRV 估值分位数策略 | MVRV 历史分位数高抛低吸 |
| d) 情绪极值反转策略 | Fear & Greed Index 极值反转 |
| e) 多因子加权打分策略 | MVRV(35%) + FGI(25%) + 资金费率(20%) + 均线(20%) 综合打分 |
| f) 动态波动率目标策略 | 综合打分方向 x 波动率反比风险控制 |
| g) 唐奇安通道突破策略 | 20/55 日通道突破（海龟法则简化版） |
| h) 资金费率挤压反转策略 | 永续合约资金费率逼空/多杀多反转信号 |

每个策略均在 `RULES` 类属性中显式声明「触发条件 → 目标仓位」对照表，并在看板与 Markdown 报告中展示。

### 绩效指标

针对加密货币 365 天年化计算：累计收益率、年化收益率、最大回撤（MDD）、夏普比率（无风险利率 0%）、卡玛比率、胜率、盈亏比、调仓次数，以及基于 $100,000 初始本金的期末净值。每次仓位变动按变动绝对值扣除单边 0.1% 的手续费与滑点。

### 交互式看板

运行后在 `reports/` 目录生成自包含的 Plotly HTML 看板 `backtest_dashboard_<UTC时间戳>.html`，包含：

1. BTC 对数坐标价格走势，可通过下拉菜单切换查看指定策略的买卖/调仓信号标记点；
2. 各策略累计净值曲线对比（对数坐标），可点击图例手动勾选/取消显示；
3. 各策略动态回撤（%）填充图对比；
4. 可排序的策略绩效指标对比表格，以及最新一日（T-0）仓位预测信号。

### 本地运行

```bash
python -m pip install -r requirements.txt
python scripts/backtest.py
```

生成的看板会输出到 `reports/backtest_dashboard_<UTC时间戳>.html`。

### GitHub Actions 自动回测

工作流文件位于 `.github/workflows/backtest.yml`，名称为 **Strategy Backtest**：

- 触发方式：`workflow_dispatch`（手动触发）与 `workflow_run`（在 **Update Market Data** 成功运行后自动触发）；
- 运行回测脚本，并将策略绩效 Markdown 对比表追加至 Job Summary（`$GITHUB_STEP_SUMMARY`）；
- 将生成的看板 HTML 提交回仓库 `reports/` 目录，并同时打包为 Workflow Artifacts（`backtest-reports`）供下载查看。

## 注意事项

- 所有时间戳均按 UTC 处理。
- Binance 单次请求上限为 1000 条记录；脚本会自动循环分页回填：Spot 每批 1000 条并以最后一根 K 线时间 +1 天继续，Funding 每批 1000 条并以最后一条 fundingTime +1ms 继续。
- MVRV 会基于 Coin Metrics `next_page_token` 连续分页拉取，直到历史数据全部回填完成。
- 首次回填会使用明确的历史起始时间（而不是依赖 API 默认“最近数据”行为）。
- 第三方 API 暂时不可用、限流或返回不连续数据时，任务会失败且不会写入异常结果。
- Binance 请求会记录失败端点、状态码和截断后的响应正文；如果所有候选端点都失败，脚本会以非零退出码结束，避免写入不完整数据。
- 如果 GitHub-hosted runner 的所有 Binance 候选出口仍然受地区或合规限制影响，可考虑改用 self-hosted runner，或接入其他兼容的数据源；备用端点不能保证一定绕过这些限制。

## License

本项目采用 [MIT License](LICENSE) 开源，允许自由复制、修改、分发（包括商业用途），但不提供任何担保。

## 免责声明

本项目提供的市场数据采集与策略回测功能仅用于研究与技术展示，回测结果基于历史数据模拟，不代表未来收益，不构成任何投资建议。
