# Debate: Fixing Zero Power and SOC Limit Issues

## Context
The Energy Management System was reporting 0 recommended power during profitable arbitrage windows and ignoring user-defined SOC discharge limits (e.g., locking at 80% or 100% instead of 13%).

## Participants
- **Archi (Lead Architect)**: Focuses on feature delivery and responsiveness.
- **Skeptic (Senior SRE/Security)**: Focuses on safety, error handling, and conservative limits.

---

### Round 1: The "Zero Power" Bottleneck (v11.3.68)
**Archi**: The Gatekeeper is too aggressive! It zeroes out the entire arbitrage budget `available_sell_ac` if current SOC is 1% below the morning floor, even if the user has 99% charge. We should only throttle the *current* hour power, not wipe the whole plan.
**Skeptic**: If we don't zero it, we might plan to sell energy we don't have, leading to deep discharge if the sun doesn't come up.
**Resolution**: Decoupled planning from throttling. `available_sell_ac` now represents the global surplus. Only `recommended_power_kw` is zeroed for the current hour if `SOC < gatekeeper_floor`.

### Round 2: Configuring the AI Discharge Limit (v11.3.69)
**Skeptic**: We're using `CONF_DYNAMIC_SOC_SELL` but the UI uses `CONF_AI_DISCHARGE_LIMIT`. This is a classic config mismatch. The engine is reading 20% while the user sees 13% in the UI.
**Archi**: Fine, let's unify them. Base target should strictly follow `CONF_AI_DISCHARGE_LIMIT`.
**Resolution**: Set `base_target` to `CONF_AI_DISCHARGE_LIMIT`. Removed morning survival floor override from `base_target`, keeping it as a separate gatekeeper check.

### Round 3: The Double Cycle Optimizer & Night Lock (v11.3.70 - v11.3.71)
**Archi**: At 3 AM, the optimizer sees zero solar and panics, setting the floor to 100% because it thinks we have a recurring deficit. We need a "daylight awareness" check.
**Skeptic**: Agree. Also, the order of calculations in v11.3.70 was wrong. `survival_floor` must be calculated *before* the optimizer runs so the optimizer knows its absolute ceiling.
**Resolution (v11.3.71)**: Moved `survival_floor` up. Added `has_daylight` check to the optimizer. Ensure `base_target` is capped by `survival_floor` to avoid over-discharge.

### Round 4: Final User Limit Isolation (v11.3.72 - v11.3.73)
**Archi**: The user set 80% but the system uses 28% and outputs 6.6kW. It's ignoring the user!
**Skeptic**: It's because `base_target` is being used as a scratch variable by the optimizer and the morning safety logic.
**Resolution**: Created `user_limit_soc` as an immutable variable. All surplus calculations (`U`) now use this fixed value. Added `UI:` to diagnostics for transparency.

### Round 5: House Load Compensation (v11.3.74)
**Archi**: User is right to be angry. 90% limit resulted in 87% final SOC. We forgot that the house drains the battery *while* we sell.
**Skeptic**: The formula was `Start - Limit`. It should be `End_Natural - Limit`.
**Resolution**: Restored `natural_soc_after_sale` simulation. The sale budget now accounts for background house load during the sale window.

### Round 6: Simulation Sync (v11.3.75 - v11.3.76)
**Archi**: Why is `M` still 12.0 and SOC morning 25%? They are lying to each other!
**Skeptic**: `M` was using current SOC instead of morning SOC. We must use the simulation's end point. 
**Resolution (v11.3.76)**: Unified `M` with the exact 7:00 AM simulation result. Moved bottleneck logic to the very end of the loop to ensure all constraints are fully calculated.

**Skeptic Approval (v11.3.76)**: ✅
**Archi Approval (v11.3.76)**: ✅
