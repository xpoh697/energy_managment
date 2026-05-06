# DEBATE: Discharge Cycle Allocation (Through the Night)

## Archi (Lead Architect)
**Proposal**: Redefine "First Epoch" as the entire discharge cycle until the next solar refill. This includes evening peaks and morning peaks (even if separated by a gap) as long as they occur before the next sunrise.
**Vibe**: One battery charge = one sale strategy.

## Skeptic (Senior SRE/Security)
**Concerns**: This is actually safer because it allows the allocator to see the morning peak as part of the current budget. If the morning is more expensive than the evening, it will prioritize the morning correctly.

## Znaika (Senior Architect / TS Specialist)
**Analysis**: This perfectly matches the user's comment about discontinuous pools through the night.
**Verdict**: Approved.

## Final Approval
**Archi**: Approved.
**Skeptic**: Approved.
**Znaika**: Approved.

## Resolution
Filter `target_hours` to include all hours before the next `morning_h_abs`.
