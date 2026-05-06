# DEBATE: Refactoring DP Strategy based on dp_engine.py

## Archi (Lead Architect)
**Issue**: The current `strategy_dp.py` has become cluttered with heuristics and doesn't match the clean, reliable logic of the reference `dp_engine.py`.
**Proposal**: Perform a complete refactor of `strategy_dp.py`. Use the architecture of `dp_engine.py` (Action-based transitions, explicit backtracking tables) but integrate our smart forecast data and boiler control.
**Vibe**: Clean start, high performance, reference-grade logic.

## Skeptic (Senior SRE/Security)
**Concerns**: Will the boiler logic slow down the DP?
**Response**: With 5 boiler steps and 170 energy steps, the state space is manageable. We will ensure the loops are optimized.

## Znaika (Senior Architect / TS Specialist)
**Analysis**: The user wants the DP to be "blindly" optimal based on our profiles. By using the `dp_engine.py` core, we ensure that the optimization is mathematically sound without artificial "fear" penalties.
**Verdict**: Approved. This is the right move for stabilization.

## Final Approval
**Archi**: Approved.
**Skeptic**: Approved.
**Znaika**: Approved.

## Resolution
Rewrite `strategy_dp.py` using `dp_engine.py` as the architectural baseline. Integrate HA-specific sensors and boiler state into the new engine.
