# DEBATE: Targeted "Budget Ignorance" during Sales

## Archi (Lead Architect)
**Issue**: The budget calculation was too paranoid (Survival Bridge was blocking sales when SOC was 18% and floor 13%).
**Proposal**: 
1. Redefine `available_ac` to include `f_today` solar forecast. Even if `f_today` is 0 (sensor lag), we allow a "Daylight Grace" where we don't save energy for the next night until sunset.
2. During the Greedy loop: if an hour is a high-price peak, we ignore the daily budget and only respect the 13% SOC floor.

## Skeptic (Senior SRE/Security)
**Concerns**: If we ignore house load until sunrise during the night, we might go dark.
**Response**: We only ignore it for "Sale" windows. The `gatekeeper_floor` (absolute minimum) still exists in the SOC simulation to prevent total blackout.

## Znaika (Senior Architect / TS Specialist)
**Analysis**: This matches the user's "Don't f*** my brain for the whole day" feedback. We keep the protection but make it "Sale-Aware".
**Verdict**: Approved.

## Final Approval
**Archi**: Approved.
**Skeptic**: Approved.
**Znaika**: Approved.

## Resolution
Implement "Daylight Grace" for budget calculation and skip budget-throttling for top-tier price windows.
