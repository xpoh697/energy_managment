# DEBATE: Fixing Double-Counting in SOC Floors

## Archi (Lead Architect)
**Issue**: The system is double-counting house consumption. It adds `bridge_soc` (predicted house load) to `morning_strict` (which already includes the user's `soc_buffer`).
**Proposal**: Change the summation to `max()` as per TS Section 6.1. This ensures we stay at the highest of the limits, not their sum.
**Vibe**: Precise and TS-compliant.

## Skeptic (Senior SRE/Security)
**Concerns**: Will this deplete the battery too much?
**Response**: No, the `soc_buffer` (13%) is specifically chosen by the user to cover house load. Adding more load on top is redundant and violates the "greedy" arbitrage principle.

## Znaika (Senior Architect / TS Specialist)
**Analysis**: TS Line 184 explicitly states: "Limits NEVER sum up. The system chooses the strictest (highest) constraint." The current implementation violates this.
**Verdict**: Mandatory fix. Also, correct the morning window end from 11:00 to 10:00 as per TS Line 183.

## Final Approval
**Archi**: Approved.
**Skeptic**: Approved.
**Znaika**: Approved.

## Resolution
Surgically edit `strategy_sell.py` to replace `+ bridge_soc` with `max()` logic and adjust the morning window timeframe.
