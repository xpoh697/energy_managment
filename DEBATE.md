# Debate: Cleaning Diagnostics and Adding charge_reason (v12.2.7)

## Archi (Lead Architect)
The user wants to add `survival_target` as a top-level attribute next to `gatekeeper_floor`. This is a great addition for transparency, as it shows the "pessimistic" target the system is aiming for before arbitrage or negative prices.

**Proposed Changes:**
- In `sensor.py`: Add `survival_target` to the main attributes dictionary of the `Market Strategy` sensor.

## Skeptic (Security/SRE)
1. **Redundancy**: It's already in `buy_debug`, but since we are moving towards top-level attributes, it's fine.
2. **Naming**: We should ensure it doesn't conflict with `target_soc`. `survival_target` is specifically the floor for bridge charging.

## Znaika (Technical Specialist)
- This follows the logic of providing detailed diagnostics to the user. `survival_target` is the calculated energy needed to survive until dawn (plus buffer).
- No architectural issues.

**Consensus:**
Add `survival_target` attribute to `sensor.py`.

## Final Approval
- Skeptic: Approved.
- Znaika: Approved.
