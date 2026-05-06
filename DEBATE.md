# DEBATE: Pure Price Priority vs Temporal Safety

## Archi (Lead Architect)
**Proposal**: We must sort hours strictly by price (descending). The highest price hour gets the full 6.6kW command immediately. We don't care if it "throttles" a later, cheaper hour. Total profit is maximized by selling as much as possible at the highest prices first.
**Vibe**: High-speed profit. No more "saving energy" for later if now is better.

## Skeptic (Senior SRE/Security)
**Concerns**:
1. **Battery Depletion**: If we dump 6.6kW at 19:00 just because it's slightly more expensive than 21:00, we might hit the 48% floor mid-hour at 20:00 (the peak). We MUST ensure the peak (20:00) is never sacrificed.
2. **House Survival**: Selling everything early might leave us with 13% SOC at 22:00, forcing a grid buy if the house load spikes.
3. **Inverter Stress**: Constant 6.6kW commands regardless of SOC might trigger hardware protections if we don't track the 'Gatekeeper' correctly.

## Znaika (Senior Architect / TS Specialist)
**Analysis**:
- **TS 181-194**: We MUST maintain the 48% reserve (morning) or 15% (morning window).
- **The Issue**: The user explicitly said "ignore surplus/mixing, just send 6.6kW".
- **Verdict**: We will implement the Price-Priority sort. To address Skeptic's concern, we will keep the `curr_floors` (Gatekeeper) active. If an hour can't take 6.6kW without hitting the floor, it will take what it can. BUT, we will NOT throttle an expensive hour just to "save" energy for a cheaper one later.

## Final Approval
**Archi**: Approved.
**Skeptic**: Approved, provided the `curr_floors` (Emergency + House Load) is strictly enforced in the simulation.
**Znaika**: Approved. This matches the user's direct instruction to prioritize price.

## Resolution
Modify `strategy_sell.py` to:
1. Sort by price.
2. Remove the "Saturation Check" (don't protect cheaper hours).
3. Always try 6.6kW command.
