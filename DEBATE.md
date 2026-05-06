# DEBATE: DP Advice Formatting

## Archi (Lead Architect)
**Issue**: The DP Hourly Plan is currently a nested dictionary, which Home Assistant displays as a long, unreadable list of keys.
**Proposal**: Convert the plan values into a single formatted string per hour. This will match the "clean" look of our main strategy sensors.
**Format**: `Mode | Power | Boiler | SOC | Grid Net | Profit`
**Vibe**: Professional and readable.

## Skeptic (Senior SRE/Security)
**Concerns**: Will we lose the raw data?
**Response**: We can keep the raw data in a hidden debug attribute if needed, but for the UI sensor, the string is much better.

## Znaika (Senior Architect / TS Specialist)
**Analysis**: The user explicitly asked for "readable to the eye". String formatting is the standard way to achieve this in HA attributes for complex plans.
**Verdict**: Approved.

## Final Approval
**Archi**: Approved.
**Skeptic**: Approved.
**Znaika**: Approved.

## Resolution
Surgically modify `strategy_dp.py` to return a formatted string for each hour in the `plan` dictionary.
