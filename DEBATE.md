# DEBATE: Energy Management Strategy Evolution

## v11.3.40 - Transparency Mission
**Archi**: We need to show why peaks are skipped. The "Black Box" is annoying the user.
**Skeptic**: 
1. Excess noise in the UI (non-peak hours).
2. Risk of leaking internal strategy state.
3. Performance overhead of collecting reasons for every hour.
**Resolution**: Implement skip reasons only for technical peaks. Add `strategy_version`.

## v11.3.49 - Threshold Clarification
**Archi**: Rename `profit_threshold` to `arbitrage_profit_threshold` for clarity.
**Skeptic**: 
1. Breaking change for existing dashboards? (Checked: dashboard uses dynamic attributes).
2. Need to ensure the value is actually used for arbitrage.
3. Version bump mandatory.
**Resolution**: Renamed and exposed in sensor attributes.

## v11.3.60 - Auto-Correcting Morning Survival (Autopilot)
**Archi**: Don't just show a deficit. Automatically raise `target_soc` to guarantee the morning buffer.
**Skeptic**: 
1. Iterative simulation might be slow.
2. Need to account for discharge efficiency (eff ~0.95).
3. Status messages must clearly explain the new "Partial Sale" behavior.
**Resolution**: Implemented the feedback loop. `target_soc` now dynamically tracks the safety floor.

## v11.3.60 Final Approval
**Skeptic**: Reviewed the implementation of `survival_floor`. It correctly factors in the night drain. Code is safe for deployment. **APPROVED**.
