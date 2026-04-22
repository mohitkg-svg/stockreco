from pydantic import BaseModel
from typing import Optional, List, Literal
from datetime import datetime

# Type aliases — single source of truth for the string vocabulary that crosses
# the API boundary. Keeps backend + frontend honest about what's allowed.
SignalType = Literal["BUY", "SELL", "NEUTRAL"]
Timeframe = Literal["5m", "15m", "30m", "1h", "4h", "1d", "1mo"]
Direction = Literal["BUY", "SELL"]
SRType = Literal["support", "resistance"]


class AddTickerRequest(BaseModel):
    ticker: str


class WatchlistItem(BaseModel):
    ticker: str
    name: Optional[str] = None
    added_at: Optional[datetime] = None
    latest_signal: Optional[str] = None
    latest_confidence: Optional[float] = None
    price: Optional[float] = None
    change_pct: Optional[float] = None
    auto_trade_enabled: Optional[bool] = True

    class Config:
        from_attributes = True


class SignalResponse(BaseModel):
    id: Optional[int] = None
    ticker: str
    timeframe: Timeframe
    signal_type: SignalType
    confidence: float
    entry: Optional[float] = None
    stop_loss: Optional[float] = None
    target1: Optional[float] = None
    target2: Optional[float] = None
    target3: Optional[float] = None
    reasoning: Optional[str] = None
    patterns: Optional[str] = None
    strategy: Optional[str] = None
    generated_at: Optional[datetime] = None
    is_new: Optional[bool] = True
    backtest_score: Optional[float] = None
    backtest_best_strategy: Optional[str] = None
    backtest_return_pct: Optional[float] = None
    backtest_win_rate: Optional[float] = None
    backtest_trades: Optional[int] = None

    class Config:
        from_attributes = True


class ChartCandle(BaseModel):
    time: int  # Unix timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float


class IndicatorLine(BaseModel):
    name: str
    color: str
    values: List[dict]  # [{time, value}]


class SupportResistanceLevel(BaseModel):
    price: float
    type: SRType
    strength: int  # 1-3


class ChartDataResponse(BaseModel):
    ticker: str
    timeframe: str
    candles: List[ChartCandle]
    indicators: List[IndicatorLine]
    support_resistance: List[SupportResistanceLevel]
    supply_demand_zones: Optional[dict] = None  # {"demand": [...], "supply": [...]}
    fibonacci: Optional[dict] = None  # {swing_low, swing_high, direction, retracements:[...], extensions:[...]}
    gaps: Optional[dict] = None  # {"price_gaps": [...], "fvgs": [...]} — each item: {kind,direction,top,bottom,mid,size,size_pct,idx,age_bars,filled,fill_pct,name,description}


class BacktestStats(BaseModel):
    total_trades: int
    win_rate: float
    profit_factor: float
    total_return_pct: float
    max_drawdown_pct: float
    sharpe_ratio: float
    avg_win_pct: float
    avg_loss_pct: float


class BacktestResponse(BaseModel):
    ticker: str
    strategy: str
    stats: BacktestStats
    equity_curve: List[dict]  # [{time, value}]
    trades: List[dict]


class StrategyResult(BaseModel):
    strategy: str
    description: str
    direction: Direction
    confidence: float       # 0-100, backtest-derived
    stats: BacktestStats
    equity_curve: List[dict]
    trades: List[dict]


class MultiStrategyBacktestResponse(BaseModel):
    ticker: str
    best_strategy: Optional[str] = None
    best_direction: Optional[str] = None
    best_confidence: Optional[float] = None
    results: List[StrategyResult]


class AnalysisResponse(BaseModel):
    ticker: str
    name: Optional[str] = None
    current_price: Optional[float] = None
    change_pct: Optional[float] = None
    signals: List[SignalResponse]
    primary_signal: Optional[SignalResponse] = None
    timeframe_alignment: dict


class OverviewItem(BaseModel):
    ticker: str
    name: Optional[str] = None
    price: Optional[float] = None
    change_pct: Optional[float] = None
    signal_type: Optional[str] = None
    confidence: Optional[float] = None
    timeframe: Optional[str] = None
    is_new: bool = False
    auto_trade_enabled: Optional[bool] = True
