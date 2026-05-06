# DEBATE: Removing DP Opportunity Cost Heuristics

## Archi (Lead Architect)
**Issue**: The DP algorithm is refusing to sell at 100% SOC during a price peak (0.89) because of a hardcoded "Opportunity Cost" penalty.
**Proposal**: Remove the heuristic penalty for selling (lines 134-139). A DP with a 48-hour horizon doesn't need artificial fear; it already calculates if it can refill from the sun.
**Vibe**: Trust the math, remove the "crutches".

## Skeptic (Senior SRE/Security)
**Concerns**: Won't it become too aggressive and leave the house empty for a morning spike?
**Response**: No, we still have `h_min_soc` (survival floor) and the DP sees the house load. It will only sell if it's truly optimal over the 48h window.

## Znaika (Senior Architect / TS Specialist)
**Analysis**: The user's screenshot clearly shows sub-optimal behavior (100% SOC, good price, zero sale). The heuristic is contradicting the core purpose of DP.
**Verdict**: Approved for removal.

## Final Approval
**Archi**: Approved.
**Skeptic**: Approved.
**Znaika**: Approved.

## Resolution
Surgically remove the "Opportunity Cost" and "Micro-movement penalty" blocks from `strategy_dp.py`.
