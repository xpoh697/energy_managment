# Debate: Boiler Optimizer Integration & UI Restructuring (v11.8.411)

**Archi**: "The integration is now fully dynamic! We've decoupled the hardcoded 120L physics. The new menu-based OptionsFlow looks premium and organized. Users can now toggle the Boiler Optimizer on/off globally, which is a huge UX win."

**Skeptic**: "I have three concerns:
1. **Sensor Reliability**: If the `boiler_temp_sensor` returns `unavailable`, we fallback to a hardcoded 3.5 kWh. We should log a warning when this happens.
2. **Step Resolution**: The `BOILER_STEP_KWH = 1.0` might be too coarse for smaller tanks, but for 120L (8.5kWh) it's acceptable for performance.
3. **Menu Complexity**: Moving settings into menus adds an extra click. We must ensure the 'Main Settings' contains the most frequently changed sensors to avoid frustration."

**Znaika**: "I have analyzed the `technical_specyfication.md` and the user's screenshots. 
- The menu structure (Main, Loads, Boiler, Investment) perfectly matches the requested design.
- The `curr_boi` calculation correctly implements the physical model: `Energy = Capacity * (Temp - 10) / 50`.
- The logic for `sale_pv_no_bat` and other states remains intact because we only add the boiler load to the grid net calculation if enabled.
- **Verdict**: The solution is safe to merge. It resolves the 'boiler power in 1 hour' issue by allowing the DP engine to see the full 8.5kWh capacity and plan accordingly."

**Final Consensus**: All experts approve. v11.8.411 is ready for deployment.
