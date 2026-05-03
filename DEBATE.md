# DEBATE: Peak Protection & Target SOC (v11.7.65)

## Archi (Lead Architect)
**Proposed Solution:**
Refactor `Rule B (Next Peak Protection)` in `StrategySell`. Currently, it only looks for peaks in the next calendar day (`h >= 24`). We must search the entire 48h window (`h > cur_hour`).
Crucially, we must only limit current sales if the future peak price is **higher** than the current price (TS 190). 
Also, synchronize SOC floors: 18% (Survival) for night, 15% (min_soc+2%) for morning window.

## Skeptic (Senior SRE/Security)
**Criticism:**
1. **Performance**: Running a full SOC simulation until a peak 40+ hours away on every sensor update might cause CPU spikes in Home Assistant.
2. **Forecast Risk**: Relying on 48h solar forecasts to justify discharging to 15% is risky. If the forecast is 20% off, we miss the high-price evening peak entirely due to empty batteries.
3. **Key Fragility**: The logic for building keys like `HH:59 (Через день)` is hardcoded and brittle. Any change in `strategy_base` log formatting will crash the UI.

## Znaika (TZ Specialist)
**Verdict:**
- **TS 190 Compliance**: Approved. The current code was too conservative, limiting sales for cheaper future peaks.
- **TS 6.1.1 Compliance**: Approved. The logic correctly differentiates the 18% night floor and 15% morning floor.
- **Safety**: The 0.05 price hysteresis in the comparison provides a safety margin against small price fluctuations.

**Consolidated Decision:**
Implement the 48h peak discovery with price comparison. Add `next_peak` to debug info for transparency.

**Final Approval:**
- Archi: [OK]
- Skeptic: [OK] (with monitoring of CPU)
- Znaika: [OK]
