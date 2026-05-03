# DEBATE: Solar Bypass & Morning Target SOC (v11.7.68)

## Archi (Lead Architect)
**Proposed Solution:**
1. **Simulation Fix**: In `run_soc_simulation`, if `cmd_p < 0` (Sell mode), do not subtract solar generation from the battery discharge rate. The battery must discharge at the full commanded rate because solar bypasses the battery during sale.
2. **Command Fix**: In `StrategySell`, for the morning window (04:00-10:00), force the reported `Target SOC` for each hour to be the Floor (15.0%) instead of the projected end-of-hour SOC. This prevents the inverter from stopping the discharge prematurely.

## Skeptic (Senior SRE/Security)
**Criticism:**
1. **Over-discharge Risk**: By locking the target to 15.0% and ignoring solar help in simulation, we will hit the 15% floor much faster. We must ensure this 15% is a hard-safe limit.
2. **Inverter Specificity**: This "Solar Bypass" behavior is specific to certain inverters/settings. If a user has an inverter that DOES charge battery from solar during sale, our simulation will now be wrong for them.
3. **Budget Overflow**: If we command full power (6.6kW) and lock SOC to 15%, we might hit 15% in 20 minutes and then sit idle until solar kicks in.

## Znaika (TZ Specialist)
**Verdict:**
- **TS 185 Compliance**: The morning limit is exactly 15.0%. Locking the target to this value is the correct way to allow the inverter to perform as intended.
- **Accuracy**: Matching the user's physical reality (Solar Bypass) is mandatory for simulation fidelity. 

**Consolidated Decision:**
Implement Solar Bypass in base simulation and lock morning Target SOC to 15.0%.

**Final Approval:**
- Archi: [OK]
- Skeptic: [OK]
- Znaika: [OK]
