"""
All tunable parameters live here. Nothing else in the codebase hardcodes a number.

IMPORTANT: fee_pct and slippage_pct are the two numbers that decide whether this
strategy is profitable or a slow fee-donation machine. Confirm Indodax's ACTUAL
maker/taker fee for your account tier before trusting any backtest result.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Config:
    # ---- market ----
    coin: str = "btc"                 # Indodax coin id, lowercase
    quote: str = "IDR"
    interval: str = "15m"             # MUST match your cron cadence

    # ---- capital ----
    starting_idr: float = 10_000_000.0
    grid_allocation: float = 0.70     # 70% works the grid
    hold_allocation: float = 0.30     # 30% bought once and left alone

    # ---- grid ----
    num_levels: int = 6               # buy levels below the anchor
    spacing_pct: float = 0.015        # 1.5% between levels
    take_profit_pct: float = 0.015    # sell a lot one spacing above its entry
    recentre_pct: float = 0.08        # re-anchor if price drifts 8% above anchor
                                      # with no open lots

    # ---- RSI filter ----
    rsi_period: int = 14
    rsi_buy_below: float = 45.0       # only buy a grid level when RSI is soft
    rsi_block_above: float = 75.0     # never buy into a blow-off top

    # ---- costs ----
    # Fees are ASYMMETRIC on every Indonesian exchange: selling costs much
    # more than buying, because national income tax (PPh 0.21%, PMK 50/2025)
    # applies to the sell side only and cannot be avoided by changing venue.
    #
    # Indodax charges makers 0% and takers 0.3% (before tax). A grid is
    # naturally a maker strategy -- resting orders above and below -- so the
    # maker preset is the one worth aiming at, at the cost of tracking which
    # orders actually fill rather than assuming an instant market fill.
    # Measured BTC/IDR spread on Indodax: 0.1176%.
    #
    # Default below is indodax_maker -- the realistic target for how this
    # strategy is meant to place orders.
    mode: str = "indodax_maker"
    fee_buy_pct: float = 0.000111
    fee_sell_pct: float = 0.002211
    slippage_pct: float = 0.0000

    # Venue presets.
    #                            buy        sell     round trip   slippage
    #   indodax_taker        0.3111%     0.5211%       0.8322%     half-spread
    #   indodax_maker        0.0111%     0.2211%       0.2322%     earns spread
    VENUES = {
        "indodax_taker":  (0.003111, 0.005211, 0.0006),
        "indodax_maker":  (0.000111, 0.002211, 0.0000),
    }

    def use_venue(self, name: str) -> "Config":
        if name not in self.VENUES:
            raise ValueError(f"unknown venue {name!r}; try {list(self.VENUES)}")
        self.fee_buy_pct, self.fee_sell_pct, self.slippage_pct = self.VENUES[name]
        self.mode = name
        # Maker orders rest at a price rather than crossing the spread, so the
        # intrabar model is the honest one there -- and Indodax's high/low are
        # clean enough to support it.
        self.fill_model = "intrabar" if name.endswith("maker") else "close"
        return self

    # ---- risk ----
    max_drawdown_pct: float = 0.20    # halt new trades at 20% below peak equity

    guardian_max_drawdown_pct: float = 0.10
    # Guardian's own circuit breaker threshold, tighter than the 20% shared
    # default above. Justified directly by measured behaviour: in its
    # actual purpose (Q3 crash) Guardian drew down only 3.01%; over 2 years
    # on the optimistic preset, 12.62%; on the realistic preset, 20.31% --
    # enough to trip the 20% breaker anyway, just much later than ideal.
    # A strategy whose explicit objective is capital preservation
    # shouldn't need to lose a fifth of the account before anything reacts.
    # Applied automatically when Guardian is the active strategy (see
    # backtest.py); pass --max-drawdown explicitly to override either way.
    min_edge_pct: float = 0.002       # required net edge per cycle after costs

    # ---- fill model ----
    # Indodax's high/low are verified clean (0 bad candles / 70,081 bars).
    #
    # "close"    : a level triggers only if the CLOSE crossed it. Conservative,
    #              and a faithful model of a bot that wakes every 15 minutes and
    #              sends a market order.
    # "intrabar" : assumes resting limit orders filled by the bar's high/low.
    #              Realistic here, since Indodax's high/low are trustworthy --
    #              this is what use_venue() switches to for maker presets.
    fill_model: str = "close"

    # ---- warmup ----
    warmup_bars: int = 50             # bars before the bot is allowed to act

    # ---- rolling history window ----
    # How many closes are kept in state["closes"]. Must be >= whatever the
    # longest indicator in your strategy needs (e.g. a trend SMA). Defaulted
    # high enough to cover Unyil 2.0's 2688-bar trend filter with margin --
    # the original grid strategy only needs ~15 bars for RSI(14), so this
    # extra headroom costs it nothing.
    history_window: int = 4900   # covers Unyil 2.0's 2688 AND Guardian's 4800

    # ---- Unyil 2.0: trend-following + adaptive sizing ----
    # A genuinely different strategy family from the grid above, not a
    # re-tuning of it. See strategy_unyil2.py for the full rationale.
    trend_ma_period: int = 2688       # ~28 days of 15m bars (28*96)
    trend_buffer_pct: float = 0.003   # 0.3% buffer around the MA
                                       # Was 1.0%. Lowered after real testing:
                                       # the wider buffer meant entries lagged
                                       # the start of a rally by weeks. 0.3%
                                       # enters faster at the cost of more
                                       # whipsaw trades -- a trade-off that
                                       # improved results in 3 of 4 real
                                       # quarters tested.
    stop_loss_pct: float = 0.15       # hard protective exit, independent of trend
    base_risk_frac: float = 1.00      # fraction of investable cash deployed on entry
                                       # Was 0.90. Full commitment: this is still
                                       # SPOT ONLY -- no leverage, no borrowing, so
                                       # 100% means 100% of cash you actually have,
                                       # and losses remain capped at what you put in.
    min_risk_frac: float = 1.00       # floor after a losing streak
                                       # Was 0.25. Set equal to base_risk_frac,
                                       # which effectively DISABLES loss-based
                                       # shrinking. Real-data diagnosis showed the
                                       # shrinking repeatedly undersized the entries
                                       # that turned into the biggest winners.
                                       # Raise min_risk_frac back below
                                       # base_risk_frac to re-enable it.
    loss_reduction_factor: float = 0.50   # risk_frac *= this after a qualifying loss
    win_recovery_factor: float = 1.50     # risk_frac *= this after a win (capped at base)
    losses_before_shrink: int = 2     # consecutive losses required before sizing
                                       # shrinks -- an isolated loss (often the
                                       # shakeout right before a real trend starts)
                                       # no longer punishes the position that
                                       # would have captured that trend

    # ---- periodic profit protection ----    # The bot's API key has no withdrawal permission (by design -- see
    # Unyil's Script safety requirements). So "taking profit every N days"
    # cannot mean an actual bank withdrawal; it means the strategy stops
    # risking money it has already made. Every withdrawal_period_days, any
    # open position is closed, and cash above the original principal is
    # swept into a reserve that future entries never touch. The user can
    # still manually withdraw that reserve any time they want it fully out
    # of the exchange -- this just guarantees it's protected in the
    # meantime, with the same effect a real periodic withdrawal would have.
    withdrawal_period_days: int = 183

    # ---- bounded adaptive sizing (OFF by default) ----
    # A stricter, auditable alternative to the simple loss-shrinking above.
    # Design constraints, each one deliberate:
    #   1. Adjusts POSITION SIZE ONLY -- never entry/exit rules. Changing
    #      when you trade creates entirely different (possibly worse)
    #      trades; changing how much you trade cannot.
    #   2. Hard floor and ceiling -- cannot spiral in either direction.
    #   3. Requires a minimum completed-trade sample before adjusting at
    #      all. Reacting to 2-3 outcomes is noise-chasing.
    #   4. Uses a rolling win rate, not the last single trade.
    #   5. Every adjustment is logged with its reason, for auditing.
    #   6. Ships OFF. Turn on only if testing shows it actually helps.
    #
    # HONEST CAVEAT: this strategy makes roughly 9-73 trades over two
    # years. A "rolling 10-trade win rate" is therefore only marginally
    # more than noise. The bounds prevent disaster; they do not guarantee
    # benefit. Measure it against the fixed-size version before trusting it.
    adaptive_sizing_enabled: bool = False
    adaptive_min_trades: int = 10        # no adjustment until this many completed trades
    adaptive_window: int = 10            # rolling window of trades for the win rate
    adaptive_floor: float = 0.50         # never size below this fraction
    adaptive_ceiling: float = 1.00       # never size above this fraction
    adaptive_low_winrate: float = 0.35   # below this -> step size down
    adaptive_high_winrate: float = 0.60  # above this -> step size up
    adaptive_step: float = 0.10          # size change per adjustment

    # ---- re-entry cooldown ----
    # After a LOSING exit, refuse new entries for this many hours.
    #
    # SHIPS DISABLED (0.0). Tested on real BTC/IDR data and it did not earn
    # its complexity:
    #     0h  -> -25.54 edge vs buy-and-hold
    #    12h  -> -24.96  (+0.58 pts: negligible)
    #    48h  -> -28.46  (worse: delayed entries shifted the whole trade
    #                     sequence onto different, worse price paths)
    #
    # The motivating evidence -- a cluster of four losing round trips inside
    # 48 hours (20-22 Dec 2024) -- turned out to come from the OPTIMISTIC
    # maker fill model, which books 73 trades over two years. Under the
    # pessimistic taker model there are only 8-9 trades total, so there is
    # almost no thrashing left for a cooldown to prevent.
    #
    # Kept in the code because the mechanism is sound and may matter if
    # real fill rates land near the maker end. Enable with --cooldown-hours.
    # Only ever applies after LOSSES, and never blocks an exit.
    reentry_cooldown_hours: float = 0.0

    # ---- regime-aware sizing experiment (pre-committed, one test) ----
    # NOT a mode of Unyil 2.0 itself -- Unyil 2.0's own entry/exit/stop
    # logic is completely untouched. This governs an EXTERNAL harness
    # (unyil2_regime_sizing.py) that drives strategy_unyil2.py's
    # regime_risk_multiplier hook, cutting new-entry size specifically on
    # a high-confidence (5/5) Bearish confirmation from regime_manager.py.
    #
    # Motivated directly by the regime-switching experiment: switching
    # into Guardian on EVERY Bearish confirmation underperformed running
    # Unyil 2.0 continuously; gating that switch to only high-confidence
    # (5/5) Bearish closed most (not all) of the gap. This asks a
    # narrower question: does simply shrinking Unyil 2.0's OWN position
    # size on that same narrow signal help, without swapping engines at
    # all. Pre-committed success bar: return stays within ~5 points of
    # the +91.76% baseline (real 2-year data) AND max drawdown improves
    # measurably from the 15.17% baseline. Short of both = a recorded
    # failure, not a starting point for further tuning.
    regime_bearish5_risk_multiplier: float = 0.5

    # Companion experiment targeting an EXISTING open position's exit
    # instead of new-entry sizing (see unyil2_regime_tight_stop.py for
    # the full rationale). While holding, tighten the stop to this value
    # during a high-confidence (5/5) Bearish confirmation; every other
    # bar, cfg.stop_loss_pct applies normally.
    regime_bearish5_tight_stop_pct: float = 0.08

    # ---- Unyil ORB: WRAPPER family, Opening Range Breakout ----
    # First purpose-built strategy for the WRAPPER category (tokenized
    # stocks: confirmed on NVDAX/TSLAX/AAPLX). All three NATIVE_CRYPTO
    # strategies showed structural weakness there because the underlying
    # market only has ~6.5h/day of real price discovery -- ORB is built
    # around that pattern directly instead of fighting it. See
    # strategy_orb.py's docstring for the full rationale.
    #
    # orb_session_start_hour is asset-specific (detect it with
    # asset_type_detector.detect_session_start_hour() rather than assume
    # NASDAQ hours -- a wrapper could track any market). None means
    # "not yet configured"; the strategy will not trade until it is set.
    orb_session_start_hour: Optional[int] = None
    orb_range_bars: int = 2            # bars defining the opening range (2 = 30 min)
    orb_breakout_buffer_pct: float = 0.002   # required move past the range high to enter
    orb_max_exposure: float = 1.00     # high-risk, high-conviction full sizing
    orb_stop_pct: float = 0.05         # tight stop -- wrong within the session, get out fast
    orb_trail_pct: float = 0.08        # trailing stop once the breakout is working

    # ---- WRAPPER-family switching: Usro (trending) + ORB (flat/declining) ----
    # A daily-close-based trend signal, NOT regime_manager.py -- that
    # classifier was built and validated specifically for native crypto's
    # continuous trading (SMA/ADX/ATR on 15-min bars), never validated for
    # wrapper assets' sparse, thin-liquidity pattern. This is deliberately
    # much simpler: compare today's close to the close `wrapper_trend_lookback_days`
    # ago. Motivated directly by the real finding that ORB beat the market
    # on TSLAX's decline but lost to holding on NVDAX/AAPLX's rallies.
    wrapper_trend_lookback_days: int = 5
    # Usro's own bar-count parameters are calibrated for 15-minute bars.
    # When running Usro within a 60-minute WRAPPER-switching harness, they
    # must be rescaled by the bar-spacing ratio (60min/15min = 4x) to
    # preserve the same REAL TIME lookback -- silently reusing 15-min bar
    # counts on 60-min bars would mean a 20-day trend instead of 5.
    usro_trend_period_60m: int = 120   # 480 // 4, preserves ~5 trading days
    # Fix for a real bug found during testing: comparing two single points
    # (today's close vs. N-days-ago close) whipsawed violently on thin
    # wrapper liquidity -- AAPLX flipped direction within one hour, TSLAX
    # switched 7 times in a single day, for a signal meant to represent a
    # 5-day trend. Both settings below directly target that noise, not
    # the underlying strategies' performance.
    wrapper_trend_smoothing_bars: int = 4     # average this many bars at each end, not one point
    wrapper_min_bars_between_switches: int = 24  # ~1 day at 60-min bars -- no intraday whipsaw

    # ---- Unyil GAP: WRAPPER family, Gap-and-Go / Gap-Fade ----
    # Reuses orb_session_start_hour (same underlying concept: the asset's
    # real session open) -- not a separate field, since it's the same
    # asset property ORB already needs.
    #
    # SPOT-ONLY REFRAMING, stated explicitly rather than left implicit:
    # the classic "gap-and-go vs. gap-fade" pair normally includes a short
    # side (fade an up-gap, or short a continuing down-gap). No shorting
    # exists in this system. So each mode only trades the ONE gap
    # direction it can express as a long position:
    #   "go":   buy an UP gap, betting it continues.
    #   "fade": buy a DOWN gap, betting it reverses (fills back up).
    # A down-gap continuation and an up-gap fade are simply not
    # tradeable here -- not a bug, a real boundary of a spot-only system.
    gap_mode: str = "go"               # "go" or "fade"
    gap_min_pct: float = 0.01          # minimum gap size to act on at all
    gap_max_exposure: float = 1.00
    gap_stop_pct: float = 0.05
    gap_trail_pct: float = 0.08

    # ---- One pre-committed experiment: EMA trend filter for gap_mode="fade" ----
    # Motivation: not every down-gap is an overreaction worth fading -- some
    # are the start of a genuine decline. This tests whether skipping fades
    # that occur deep below a clearly declining trend improves things,
    # without touching entries that still look like overreactions.
    # Ships disabled (False) -- existing behavior is completely unchanged
    # unless explicitly turned on for this one test.
    gap_fade_ema_enabled: bool = False
    gap_fade_ema_period: int = 120          # ~5 trading days at 60-min bars
    gap_fade_ema_max_below_pct: float = 0.05  # skip the fade if price is > 5% below its EMA

    # ---- WRAPPER-family circuit breaker, separate from native crypto's 20% ----
    # Calculated from real evidence across NVDAX, TSLAX, AAPLX, GOOGLX under
    # Gap-Fade: the shared 20% default never fired on a genuine win but came
    # within 2 points of halting AAPLX's real +13.42-edge run (peaked at
    # 18.00% live drawdown), while both real losing runs legitimately
    # tripped it (NVDAX 21.01%, GOOGLX 28.71% -- the latter jumping from
    # well under 20% to 28.71% within a single 60-min bar, a reminder that
    # some overshoot past any threshold is structural at this check
    # resolution, not something a number alone fixes). 22% adds real margin
    # on the winning side without giving up meaningful protection on either
    # real failure.
    wrapper_max_drawdown_pct: float = 0.22

    # ---- Unyil Guardian: conservative capital preservation ----
    # A separate strategy (strategy_guardian.py), not a mode of Unyil 2.0.
    # Objective is preserving capital in declines, explicitly at the cost
    # of upside in rallies. See that file's docstring for the full rationale.
    guardian_trend_fast: int = 1344      # ~14 days of 15m bars
    guardian_trend_slow: int = 4800      # ~50 days of 15m bars
    guardian_max_exposure: float = 0.50  # cap on investable cash per position.
                                          # The single biggest structural
                                          # difference from Unyil 2.0's 100%.
    guardian_stop_pct: float = 0.08      # hard stop, tighter than Unyil 2.0's 15%
    guardian_trail_pct: float = 0.06     # trailing stop below the high-water mark
    guardian_exit_buffer_pct: float = 0.02
    # Price must close this far BELOW the medium average to trigger a trend
    # exit. Without it, Guardian exits on any dip below the average and --
    # since both trends are usually still aligned -- immediately re-enters.
    # That produced 217 round trips over two years and burned 26.7% of
    # capital in fees on the optimistic preset, and made it outright
    # unusable on the realistic one (-4.76%, circuit breaker triggered).
    # Set to 0 for the original twitchy behaviour.

    # ---- Usro: bull-market specialist ----
    # The counterpart to Guardian. Fast, low-friction entry; a single wide
    # trailing stop instead of a trend-break exit, so ordinary rally
    # pullbacks don't shake the position out. See strategy_usro.py's
    # docstring for the full rationale. Not a defensive strategy -- run it
    # only when a sustained rally is expected or underway.
    usro_trend_period: int = 480      # ~5 days of 15m bars: fast, for quick entry
    usro_entry_buffer_pct: float = 0.001   # 0.1%: minimal delay past confirmation
    usro_max_exposure: float = 1.00   # full commitment, same lesson as Unyil 2.0
    usro_trail_pct: float = 0.15      # wide: survives normal rally pullbacks

    def validate(self) -> list[str]:
        """The 'Clears fee?' box, enforced at config load rather than per trade."""
        problems = []

        round_trip_cost = (self.fee_buy_pct + self.fee_sell_pct
                           + 2 * self.slippage_pct)
        net_edge = self.take_profit_pct - round_trip_cost
        if net_edge < self.min_edge_pct:
            problems.append(
                f"Grid cycle nets {net_edge:.4%} after costs "
                f"({round_trip_cost:.4%} round trip), below the {self.min_edge_pct:.4%} "
                f"minimum. Widen take_profit_pct or find a lower fee."
            )

        if abs(self.grid_allocation + self.hold_allocation - 1.0) > 1e-9:
            problems.append("grid_allocation + hold_allocation must equal 1.0")

        if self.history_window < self.trend_ma_period:
            problems.append(
                f"history_window ({self.history_window}) is shorter than "
                f"trend_ma_period ({self.trend_ma_period}) -- the trend filter "
                f"would never see enough bars to compute and would silently "
                f"never trade. Raise history_window to at least trend_ma_period."
            )

        if self.history_window < self.guardian_trend_slow:
            problems.append(
                f"history_window ({self.history_window}) is shorter than "
                f"guardian_trend_slow ({self.guardian_trend_slow}) -- Guardian's "
                f"long trend filter would never compute and it would silently "
                f"never trade. Raise history_window to at least guardian_trend_slow."
            )

        if not (0 < self.guardian_max_exposure <= 1.0):
            problems.append(
                f"guardian_max_exposure ({self.guardian_max_exposure}) must be "
                f"in (0, 1.0] -- it is a fraction of investable cash, and spot "
                f"trading cannot exceed 100%"
            )

        if self.guardian_trend_fast >= self.guardian_trend_slow:
            problems.append(
                f"guardian_trend_fast ({self.guardian_trend_fast}) must be shorter "
                f"than guardian_trend_slow ({self.guardian_trend_slow})"
            )

        if self.history_window < self.usro_trend_period:
            problems.append(
                f"history_window ({self.history_window}) is shorter than "
                f"usro_trend_period ({self.usro_trend_period}) -- Momentum's "
                f"trend filter would never compute and it would silently never trade."
            )

        if not (0 < self.usro_max_exposure <= 1.0):
            problems.append(
                f"usro_max_exposure ({self.usro_max_exposure}) must be "
                f"in (0, 1.0] -- spot trading cannot exceed 100%"
            )

        if not (0 < self.usro_trail_pct < 1.0):
            problems.append(
                f"usro_trail_pct ({self.usro_trail_pct}) must be in (0, 1.0)"
            )

        if self.orb_session_start_hour is not None:
            if not (0 <= self.orb_session_start_hour <= 23):
                problems.append(
                    f"orb_session_start_hour ({self.orb_session_start_hour}) "
                    f"must be an hour 0-23"
                )
            if self.orb_range_bars < 1:
                problems.append(f"orb_range_bars ({self.orb_range_bars}) must be >= 1")
            if not (0 < self.orb_max_exposure <= 1.0):
                problems.append(
                    f"orb_max_exposure ({self.orb_max_exposure}) must be in (0, 1.0]"
                )
            if not (0 < self.orb_stop_pct < 1.0):
                problems.append(f"orb_stop_pct ({self.orb_stop_pct}) must be in (0, 1.0)")
            if not (0 < self.orb_trail_pct < 1.0):
                problems.append(f"orb_trail_pct ({self.orb_trail_pct}) must be in (0, 1.0)")

        if self.gap_mode not in ("go", "fade"):
            problems.append(f"gap_mode ({self.gap_mode!r}) must be 'go' or 'fade'")
        if not (0 < self.gap_min_pct < 1.0):
            problems.append(f"gap_min_pct ({self.gap_min_pct}) must be in (0, 1.0)")
        if not (0 < self.gap_max_exposure <= 1.0):
            problems.append(f"gap_max_exposure ({self.gap_max_exposure}) must be in (0, 1.0]")
        if not (0 < self.gap_stop_pct < 1.0):
            problems.append(f"gap_stop_pct ({self.gap_stop_pct}) must be in (0, 1.0)")
        if not (0 < self.gap_trail_pct < 1.0):
            problems.append(f"gap_trail_pct ({self.gap_trail_pct}) must be in (0, 1.0)")
        if not (0 < self.wrapper_max_drawdown_pct < 1.0):
            problems.append(
                f"wrapper_max_drawdown_pct ({self.wrapper_max_drawdown_pct}) must be in (0, 1.0)"
            )
        if self.gap_fade_ema_enabled:
            if self.gap_fade_ema_period < 2:
                problems.append(f"gap_fade_ema_period ({self.gap_fade_ema_period}) must be >= 2")
            if not (0 < self.gap_fade_ema_max_below_pct < 1.0):
                problems.append(
                    f"gap_fade_ema_max_below_pct ({self.gap_fade_ema_max_below_pct}) "
                    f"must be in (0, 1.0)"
                )

        if self.adaptive_sizing_enabled:
            if not (0 < self.adaptive_floor <= self.adaptive_ceiling <= 1.0):
                problems.append(
                    f"adaptive bounds invalid: need 0 < floor ({self.adaptive_floor}) "
                    f"<= ceiling ({self.adaptive_ceiling}) <= 1.0"
                )
            if self.adaptive_low_winrate >= self.adaptive_high_winrate:
                problems.append(
                    f"adaptive_low_winrate ({self.adaptive_low_winrate}) must be below "
                    f"adaptive_high_winrate ({self.adaptive_high_winrate})"
                )
            if self.adaptive_min_trades < 5:
                problems.append(
                    f"adaptive_min_trades ({self.adaptive_min_trades}) is too small -- "
                    f"adjusting on fewer than 5 completed trades is noise-chasing"
                )
            if self.adaptive_step <= 0 or self.adaptive_step > 0.5:
                problems.append(
                    f"adaptive_step ({self.adaptive_step}) must be in (0, 0.5]"
                )

        if self.num_levels < 1:
            problems.append("num_levels must be >= 1")

        if self.take_profit_pct <= 0 or self.spacing_pct <= 0:
            problems.append("spacing_pct and take_profit_pct must be positive")

        return problems

    @property
    def grid_capital(self) -> float:
        return self.starting_idr * self.grid_allocation

    @property
    def hold_capital(self) -> float:
        return self.starting_idr * self.hold_allocation

    @property
    def slice_idr(self) -> float:
        """IDR committed per grid level."""
        return self.grid_capital / self.num_levels


DEFAULT = Config()
