"""Deterministic fixtures shared by engine, report, and backtest tests."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from etf_rotation.engine import DecisionContext, DailyDecision
from etf_rotation.models import QdiiCheck
from etf_rotation.signals import Signal
from etf_rotation.state import EXECUTION_MAP, SleeveState, initial_ledger


SHANGHAI = ZoneInfo("Asia/Shanghai")
AS_OF = datetime(2026, 7, 20, 9, 35, tzinfo=SHANGHAI)


def _signal(asset: str, trend_ok: bool = True) -> Signal:
    return Signal(asset, .1, .2, .2, .2, .5, 1., 10., 11., trend_ok)


def _qdii(ticker: str) -> QdiiCheck:
    stamp = AS_OF - timedelta(minutes=1)
    return QdiiCheck(
        ticker=ticker, subscriptions_open=True, redemptions_open=True,
        minimum_creation_units=1_000_000, net_creation_limit_units=2_000_000,
        recent_premium_warning=False, estimated_fair_value=10.0,
        bid=10.00, ask=10.01, quote_timestamp=stamp,
        fair_value_timestamp=stamp, average_turnover_20d=1_000_000_000.0,
        ask_depth_cny=50_000.0, pcf_source="official", warning_source=None,
        quote_source="quotes",
    )


def make_context(**kwargs) -> DecisionContext:
    """Return a valid, observation-only context; aliases keep plan tests terse."""
    aliases = {
        "blockers": "snapshot_blockers",
        "eligible_wrappers": "eligible_wrappers",
    }
    for old, new in aliases.items():
        if old in kwargs:
            value = kwargs.pop(old)
            if old == "eligible_wrappers":
                # Remove unavailable Nasdaq checks rather than weakening their checks.
                checks = {ticker: _qdii(ticker) for ticker in value}
                kwargs["qdii_checks"] = {"513050": _qdii("513050"), **checks}
            else:
                kwargs[new] = value
    ledger = kwargs.pop("ledger", initial_ledger())
    sleeve_name = kwargs.get("sleeve_name", "risk")
    ranking = kwargs.pop("ranking", (
        ("CSI300", "CHINEXT", "CHINA_INTERNET50", "NASDAQ100")
        if sleeve_name == "risk" else ("GOLD_CNY", "CN_GOVT_10Y")
    ))
    # Convenient state overrides keep callers from hand-building a ledger.
    held_asset = kwargs.pop("held_asset", None)
    held_ticker = kwargs.pop("held_ticker", None)
    held_shares = kwargs.pop("held_shares", 300)
    candidate = kwargs.pop("candidate", kwargs.pop("candidate_asset", None))
    challenger = kwargs.pop("challenger", kwargs.pop("challenger_asset", None))
    confirmations = kwargs.pop("confirmation_count", None)
    if any(value is not None for value in (held_asset, candidate, challenger, confirmations)):
        original = ledger.sleeves[sleeve_name]
        asset = held_asset
        if asset is not None:
            ticker = held_ticker or ("159659" if asset == "NASDAQ100" else EXECUTION_MAP[asset])
            state = SleeveState(original.cash_cny, asset, ticker, held_shares, float(held_shares) * 10.0,
                                None, challenger, confirmations if confirmations is not None else (1 if challenger else 0),
                                original.bad_trend_days, original.exit_ready, None, ())
        else:
            state = SleeveState(original.cash_cny, None, None, 0, 0.0, candidate, None,
                                confirmations if confirmations is not None else (1 if candidate else 0),
                                original.bad_trend_days, original.exit_ready, None, ())
        ledger = replace(ledger, sleeves={**ledger.sleeves, sleeve_name: state})
    base = dict(
        decision_as_of=AS_OF, sleeve_name=sleeve_name,
        is_cn_trading_day=True, is_weekly_decision_day=True,
        observation_only=True, unresolved_order=False, snapshot_blockers=(),
        ledger=ledger, ranking=ranking, signals=_base_signals(),
        qdii_checks=_base_qdii(), wrapper_fee_rates={
            "159659": .0065, "159660": .0065, "159941": .01, "513390": .0065,
        }, held_qdii_priority_exit=False,
        execution_prices={"510300": 10.0, "159915": 10.0, "518850": 10.0, "511260": 10.0},
        execution_blockers={}, evidence_timestamps={"snapshot": AS_OF - timedelta(minutes=1)},
        weekly_execution_valid=True,
    )
    base.update(kwargs)
    return DecisionContext(**base)


def _base_signals():
    return {asset: _signal(asset) for asset in (
        "CSI300", "CHINEXT", "CHINA_INTERNET50", "NASDAQ100", "GOLD_CNY", "CN_GOVT_10Y"
    )}


def _base_qdii():
    return {ticker: _qdii(ticker) for ticker in ("513050", "159659", "159660", "159941", "513390")}


def make_decision(**kwargs) -> DailyDecision:
    base = dict(
        decision_as_of=AS_OF, sleeve_name="risk", current_logical_asset=None,
        current_execution_ticker=None, model_candidate=None, effective_candidate=None,
        execution_ticker=None, action="KEEP_CASH", share_quantity=0,
        reference_amount_cny=0.0, remaining_cash_cny=15000.0, blockers=(),
        evidence_timestamps={"snapshot": AS_OF - timedelta(minutes=1)},
    )
    base.update(kwargs)
    if "action" not in kwargs and any(base[field] is not None for field in ("model_candidate", "effective_candidate", "execution_ticker")):
        base["action"] = "OBSERVE"
    if ("effective_candidate" not in kwargs and base["model_candidate"] is not None
            and base["execution_ticker"] is not None):
        base["effective_candidate"] = base["model_candidate"]
    return DailyDecision(**base)


@dataclass(frozen=True)
class BacktestCase:
    signal_day_price: float = 10.0
    next_open_price: float = 10.2
    wrapper_available: bool = True
    commission_rate: float = .00012
    slippage_rate: float = .001


def make_backtest_case(**kwargs) -> BacktestCase:
    return replace(BacktestCase(), **kwargs)
