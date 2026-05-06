# DEBATE: Correcting Power Sign in Simulation

## Archi (Lead Architect)
**Issue**: The simulation was treating positive power as charging. This caused "Strategy candidates" to show 0kW discharge because the battery appeared full.
**Proposal**: Invert the sign in `run_soc_simulation` call: `commands={h: -p for h, p in trial_cmds.items()}`.
**Vibe**: Negative = Discharge = Sale.

## Skeptic (Senior SRE/Security)
**Concerns**: None. This is a classic sign-inversion bug.

## Znaika (Senior Architect / TS Specialist)
**Analysis**: Matches the mathematical model of the `StrategyEngine`.
**Verdict**: Approved.

## Final Approval
**Archi**: Approved.
**Skeptic**: Approved.
**Znaika**: Approved.

## Resolution
Invert the sign of power commands passed to `run_soc_simulation`.
