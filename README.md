# 定投工具

## 安装

需要 Python 3.11+ 和 Git。以下命令在 PowerShell 中执行。

```powershell
git clone https://github.com/yinsuecci/invest_tool.git
cd invest_tool
python -m pip install -e .
```

后续命令均在项目根目录执行。仓库不包含行情和回测报告，首次使用需先生成。

## 下载行情

```powershell
python -m dca_lab fetch --data data/seven-assets --assets GOOG XAU SOL BTC ETH SPY QQQ --start 2026-07-01T00:00:00+00:00 --end 2026-10-29T16:00:00+00:00
```

无需 API Key，但网络须能访问币安公开接口。更换区间时修改 `--start`、`--end`，并使用新的数据目录；预留至少 800 小时指标预热数据。所选标的须有共同有效行情。

支持的代码为 `GOOG`、`XAU`、`SOL`、`BTC`、`ETH`、`SPY`、`QQQ`。其中 GOOG、SPY、QQQ 分别使用 GOOGLB、SPYB、QQQB 的 USDT 现货交易对，XAU 使用 XAUTUSDT；不是原生股票或指数合约。

## 生成回测

一条命令生成七标的策略方案、独立拟合报告和对比页面：

```powershell
python -m dca_lab generate --data data/seven-assets --output reports/generated --daily-total 30
```

七标的合计每日入金默认为 30U，可通过 `--daily-total` 调整。输出目录需为新目录；重复运行时更换 `--output`，避免覆盖已有结果。下载行情仍使用上一节的独立命令。

## 启动页面

```powershell
python -m dca_lab.compact_app --source reports/generated/independent/report.json --output reports/generated/app --port 59755
```

打开 <http://127.0.0.1:59755/>，使用 `Ctrl+C` 停止服务。

- 切换区间、策略和普通定投频次，查看同最终投入金额的成本与资产对比。
- 点击标的查看 K 线买点，调整日期和 K 线周期。
- 展开“买入记录”查看成交明细。

更换端口时修改 `--port`；使用已有报告时，将 `--source` 指向对应的组合回测 `report.json`。

## 接入币安

1. 点击右上角“币安 API”。
2. 输入 API Key 和 Secret，点击“测试连接”。
3. 保持“模拟”，或选择“仅查询”查看账户。
4. 查询并选择可用的 USDT 活期产品，设置交易备用金。
5. 点击“预览”核对金额；需要自动执行时，勾选自动申购并保存。

模拟模式不实际申购。自动申购每 5 分钟检查一次，仅使用扣除备用金后的现货可用 USDT，并受产品最低金额及额度限制。停止服务后不再执行，不会自动赎回理财补足下单资金。

如需开放真实执行选项，停止服务后重新启动：

```powershell
python -m dca_lab.compact_app --source reports/generated/independent/report.json --output reports/generated/app --port 59755 --enable-live
```

启动和连接后仍默认为模拟，需在页面明确选择“实盘”并保存。只读密钥用于查询，真实操作需相应交易权限；不要授予提现权限。密钥仅保存在进程内存中，断开或重启后需重新连接。

当前提供下单接口，**不会自动执行回测策略**。接口用法见 [docs/binance-api.md](docs/binance-api.md)；服务运行后可查看 <http://127.0.0.1:59755/openapi.json>。服务仅限本机使用，不要通过公网代理或隧道暴露。

## 测试与源码导出

运行测试：

```powershell
python -m unittest discover -s tests
```

检查隐私并导出源码：

```powershell
python tools/release_source.py --check
python tools/release_source.py
```

源码包位于 `release/dca-lab-source.zip`，不包含本地行情、报告、密钥或订单数据库。发布注意事项见 [SECURITY.md](SECURITY.md)。
