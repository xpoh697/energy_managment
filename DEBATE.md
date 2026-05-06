# DEBATE: Realistic Power Planning vs Aggressive Commands

## Archi (Lead Architect)
**Issue**: UI shows 6.6kW even for hours where the battery is projected to be empty (simulation SOC hits floor).
**Proposal**: Use `real_p` (from simulation log) for the `Planned power` display and for the final command calculation.
This ensures that "Price Priority" naturally drains the battery on the BEST hours, and the LATER (less profitable) hours show 0kW or low power if the battery is depleted.

## Skeptic (Senior SRE/Security)
**Concerns**: If we set command to 3.2kW (real_p) but solar suddenly spikes to 5kW, we limit the export.
**Response**: We can set the inverter command to `max_p` but the UI display should show `real_p`. 
Actually, the user wants "Distribution". If we have 5kWh, we shouldn't "promise" 6.6kW for 3 hours.

## Znaika (Senior Architect / TS Specialist)
**Analysis**: The user's "Top-Down" mandate means the best hour gets first dibs. If it takes everything, others get 0.
**Verdict**: Use `real_p` for BOTH UI and commands to ensure the plan is physically possible.

## Final Approval
**Archi**: Approved.
**Skeptic**: Approved.
**Znaika**: Approved.

## Resolution
Update `sell_commands[h_target]` to `real_p` after each successful simulation step.
