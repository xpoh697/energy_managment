# Project Task Log & Universal Rules

## Current Status (v11.9.4)
- [x] Target-hour survival look-ahead (v11.9.4).
- [x] Lossy arbitrage protection (selling low to buy high) (v11.9.4).
- [x] Emergency SOC recovery bonus.
- [x] High-precision 0.1kWh steps.

## Universal Rules (DO NOT BREAK)

### 1. Discharge Limits (The Constitution)
- **Gatekeeper**: `MinSOC + House_Load_Until_Sunrise`. Must be calculated every hour.
- **Morning Reserve**: 
    - 10:00 - 04:00: `MinSOC + soc_buffer` (Safety window).
    - 04:00 - 10:00: `MinSOC + 2%` (Liberal window for morning presale).
- **User Limit**: Always respect `ai_discharge_limit_soc`.
- **Arbitration**: Final limit = `max(Gatekeeper, Morning_Reserve, User_Limit)`.
- **Stop Sell**: Discharge for export is FORBIDDEN if Price < `price_stop_sell`.

### 2. Context & Timing
- **Saturation Bypass**: `hit_full_before` override is ONLY active 04:00-11:00.
- **Floor Integrity**: Use `floors_anchored` for strategy decisions, `floors_sliding` for UI projection.

### 3. Simulation Integrity
- **No Zero Load**: If manager base load is 0, ALWAYS fallback to hourly consumption profile.
- **Efficiency**: Use dynamic `eff_coeff` (default 0.95) for all SOC projections.

### 4. Workflow (MANDATORY)
1. **Bump Version**: Update `VERSION` in `const.py` AND `version` in `manifest.json` BEFORE any deploy.
2. **Sync**: Use `robocopy` to `\\192.168.100.5\config\custom_components\energy_management`.
3. **Pycache**: Delete `__pycache__` folder on the server immediately after sync.
4. **Git Push**: Commit and push to GitHub after every successful sync.
5. **Restart**: Trigger Home Assistant restart to apply changes.
