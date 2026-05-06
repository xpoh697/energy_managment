# DEBATE: Correct Cycle Boundary Logic

## Archi (Lead Architect)
**Issue**: The previous 10:00 AM cutoff was too static. At 09:11 AM, it looked at 10:00 AM Today, found nothing, and deleted the evening peak.
**Proposal**: The boundary must be the 10:00 AM that follows the **next** sunset. 
- If Day/Evening (now > sunrise): Cutoff = Tomorrow 10:00 AM.
- If Night/Early Morning (now < sunrise): Cutoff = Today 10:00 AM.
This keeps the evening peak visible and planned during the day.

## Skeptic (Senior SRE/Security)
**Concerns**: None. This ensures the allocator always has a full discharge window to work with.

## Znaika (Senior Architect / TS Specialist)
**Analysis**: This fixes the "disappearing windows" bug while respecting the "one discharge cycle" rule.
**Verdict**: Approved.

## Final Approval
**Archi**: Approved.
**Skeptic**: Approved.
**Znaika**: Approved.

## Resolution
Update `strategy_sell.py` with dynamic `cutoff_abs` based on sunrise.
