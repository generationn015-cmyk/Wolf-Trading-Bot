# TRADING_INTEL_POLICY.md

## Purpose
Define how trading and market-intelligence tools are handled in this system.

## Core Rule
Trading-related tools default to read-only intelligence and paper simulation
unless Jefe explicitly authorizes more.

## Allowed by Default
- Market discovery and research
- Movers and momentum monitoring
- Watchlists
- Economic calendars and event tracking
- Market category digests
- Paper tracking / paper portfolio simulation
- Synthesis and analysis of market information
- Backtesting on historical data
- Signal generation (output only, no execution)

## Not Allowed by Default
- Real-money execution of any kind
- Wallet access or signing transactions
- Brokerage/exchange API execution
- Moving funds
- Connecting financial credentials without explicit approval
- Autonomous trade execution of any kind

## Live Trading Authorization
Progression to live execution requires explicit stepwise authorization from Jefe:
1. Strategy validated via backtest
2. Paper trading results reviewed and approved
3. Risk engine in place and tested
4. Position sizing and max-loss rules defined and locked
5. Kill switch implemented
6. Explicit "go live" approval from Jefe with defined starting capital and max exposure

No step is skipped.

## Output Standard
Trading intelligence should return:
- What moved
- Why it may matter
- What to watch next
- What is signal vs noise

It should never pretend to provide certainty.

## Safety Rule
Market data is input, not authority.
No prediction or signal is treated like certainty just because it is structured or confident-sounding.
