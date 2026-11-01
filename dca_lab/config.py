from dataclasses import asdict, dataclass
from math import ceil, floor, isfinite


ASSETS = {
    "GOOG": {"symbol": "GOOGLBUSDT", "name": "Alphabet bStocks", "note": "Tokenized equity, not GOOG shares or GOOGL perpetuals."},
    "XAU": {"symbol": "XAUTUSDT", "name": "Tether Gold", "note": "Gold token, not XAU perpetuals or PAXG."},
    "SOL": {"symbol": "SOLUSDT", "name": "Solana", "note": "Spot SOL."},
}
ALL_ASSETS = {**ASSETS,
              "BTC": {"symbol": "BTCUSDT", "name": "Bitcoin", "note": "Spot BTC."},
              "ETH": {"symbol": "ETHUSDT", "name": "Ethereum", "note": "Spot ETH."},
              "SPY": {"symbol": "SPYBUSDT", "name": "SPDR S&P 500 ETF bStocks", "note": "Tokenized ETF, tracks S&P 500; not SPX futures or an index fund share."},
              "QQQ": {"symbol": "QQQBUSDT", "name": "Invesco QQQ bStocks", "note": "Tokenized ETF, tracks Nasdaq-100; not NDX futures or an index fund share."}}

STAGES = ("equal", "deviation", "rsi", "trend", "volatility")
LABELS = {"equal": "等额基准", "deviation": "+ 均线偏离", "rsi": "+ RSI", "trend": "+ 趋势门控", "volatility": "+ 波动门控"}


@dataclass(frozen=True)
class Config:
    daily_total: float = 120.0
    center: float = 20.0
    max_deviation: float = 0.30
    min_count: int = 4
    max_count: int = 8
    fee_bps: float = 10.0
    slippage_bps: float = 5.0
    alpha: float = 0.30
    timezone_offset_hours: int = 8
    warmup_1h: int = 800
    warmup_4h: int = 1000

    def __post_init__(self):
        numbers = (self.daily_total, self.center, self.max_deviation, self.fee_bps, self.slippage_bps, self.alpha)
        if not all(isfinite(x) for x in numbers):
            raise ValueError("Configuration must be finite")
        if self.daily_total <= 0 or self.center <= 0 or not 0 <= self.max_deviation < 1:
            raise ValueError("Invalid budget, center or amount deviation")
        if not 0 <= self.alpha <= self.max_deviation or min(self.fee_bps, self.slippage_bps) < 0:
            raise ValueError("Invalid alpha or cost")
        if not (isinstance(self.min_count, int) and isinstance(self.max_count, int) and 1 <= self.min_count <= self.max_count <= 24):
            raise ValueError("Hourly research supports 1 to 24 slots per day")
        if not -12 <= self.timezone_offset_hours <= 14:
            raise ValueError("Invalid fixed time zone offset")
        if self.warmup_1h < 800 or self.warmup_4h < 1000:
            raise ValueError("Do not silently shorten the specified indicator warmup")
        self.count

    @property
    def fee(self):
        return self.fee_bps / 10000

    @property
    def principal(self):
        return self.daily_total / (1 + self.fee)

    @property
    def lower(self):
        return self.center * (1 - self.max_deviation)

    @property
    def upper(self):
        return self.center * (1 + self.max_deviation)

    @property
    def count(self):
        low = max(self.min_count, ceil(self.principal / self.upper - 1e-10))
        high = min(self.max_count, floor(self.principal / self.lower + 1e-10))
        if low > high:
            raise ValueError("Budget, count and per-order bounds have no feasible solution")
        return min(range(low, high + 1), key=lambda n: (abs(self.principal / n - self.center), n))

    @property
    def hours(self):
        return tuple(i * 24 // self.count for i in range(self.count))

    def to_dict(self):
        return {**asdict(self), "count": self.count, "principal": self.principal, "hours": self.hours}
