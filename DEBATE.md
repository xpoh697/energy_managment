# DEBATE: Single Epoch Allocation

## Archi (Lead Architect)
**Proposal**: Limit the Greedy Allocator to `epochs[0]`. If we have multiple price peaks separated by low-price hours, we only plan for the first one. This ensures maximum power for the immediate profit window.
**Vibe**: Focus on the now. Tomorrow can wait.

## Skeptic (Senior SRE/Security)
**Concerns**: None. This actually improves safety by not committing battery energy to distant future windows.

## Znaika (Senior Architect / TS Specialist)
**Analysis**: This simplifies the simulation and aligns with the user's desire for high-power discharge in the current peak.
**Verdict**: Approved.

## Final Approval
**Archi**: Approved.
**Skeptic**: Approved.
**Znaika**: Approved.

## Resolution
Filter `target_hours` to only include hours from the first epoch before entering the greedy loop.
