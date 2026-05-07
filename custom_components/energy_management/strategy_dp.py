import logging
import time
import math
from datetime import datetime, timedelta
from typing import Dict, List, Any, Tuple

from .const import (
    CONF_BATTERY_MAX_POWER,
    CONF_BATTERY_COST,
    CONF_BATTERY_RATED_CYCLES,
    CONF_MIN_SOC_BAT,
    CONF_SOC_BUFFER,
    CONF_AI_DISCHARGE_LIMIT,
    CONF_BOILER_ENABLE,
    CONF_BOILER_POWER,
    CONF_BOILER_CAPACITY,
    CONF_BOILER_TEMP_SENSOR,
    CONF_BOILER_DEADLINE,
    CONF_MIN_SELL_POWER,
    CONF_DYNAMIC_SOC_SELL,
    CONF_FORCE_MARKET_SELL,
    CONF_MAX_ARBITRAGE_HOURS,
    CONF_MIN_SELL_PRICE,
    CONF_MIN_DISCHARGE_KWH,
    DOMAIN,
    VERSION
)
from .utils import normalize_float, round_f

_LOGGER = logging.getLogger(__name__)

# --- DP Parameters ---
ENERGY_STEP = 0.1          # 0.1 kWh precision (Restored as per user request)
BOILER_STEPS = 0           # Disabled for now
INF = 1e9                 

# Action types
ACT_IDLE = 0
ACT_DIS = 1
ACT_PV_CHARGE = 2
ACT_GRID_CHARGE = 3
ACT_SELF_CONSUME = 4
ACT_PAID_IMPORT = 5

class DPPlanner:
    def __init__(self, manager):
        self.manager = manager
        self._cache = {}
        self._last_run = 0
        
    def get_dp_advice(self) -> Dict[str, Any]:
        t0 = time.time()
        try:
            now = datetime.now()
            cur_hour = now.hour
            
            prices_buy = self._get_prices("prices_buy")
            prices_sell = self._get_prices("prices_sell")
            
            if not prices_buy or not prices_sell:
                return {"error": "Missing price data"}

            available_hours = sorted([int(h) for h in prices_buy.keys()])
            max_abs_h = max(available_hours) if available_hours else cur_hour + 23
            horizon = min(48, max_abs_h - cur_hour + 1)
            
            # --- Configuration (Sync with dp_engine.py logic) ---
            max_p_dis = float(self.manager.get_setting(CONF_BATTERY_MAX_POWER, 5.0))
            max_p_chg = max_p_dis # Simplified or can be separate
            curr_s_raw, b_cap_raw, _ = self.manager.get_battery_state()
            b_cap = float(b_cap_raw or 17.0)
            
            # v11.9.0: Step resolution improvement (v11.9.19: Fixed at 0.1)
            energy_step = 0.1
            energy_steps = int(round(b_cap / energy_step))
            
            cycle_cost = self._get_deg_cost(b_cap)
            min_soc = float(self.manager.get_setting(CONF_MIN_SOC_BAT, 10.0))
            soc_buff = float(self.manager.get_setting(CONF_SOC_BUFFER, 13.0))
            eff = getattr(self.manager, "last_eff_coeff", 0.96)
            
            # v11.9.1: Smart Terminal Reserve (Looking beyond 48h horizon)
            # We estimate how much we need to survive from the END of the horizon until the next generation window
            horizon_end_dt = now + timedelta(hours=horizon)
            min_end_usable = self._calc_survival_beyond_horizon(horizon_end_dt, b_cap)
            
            # Boiler Params
            b_power = float(self.manager.get_setting(CONF_BOILER_POWER, 2.5))
            b_capacity = float(self.manager.get_setting(CONF_BOILER_CAPACITY, 8.5))
            temp_s = self.manager.get_setting(CONF_BOILER_TEMP_SENSOR)
            b_enabled = bool(self.manager.get_setting(CONF_BOILER_ENABLE, False)) or bool(temp_s)
            
            forecast_gen = self._get_smart_gen_forecast(horizon)
            avg_cons = self._ensure_dict(self.manager.get_average_profile("consumption_base", 7, now.weekday()))
            tomorrow_cons = self._ensure_dict(self.manager.get_average_profile("consumption_base", 7, (now.weekday() + 1) % 7))
            tomorrow_gen = self._ensure_dict(self.manager.get_average_profile("generation_total", 7, (now.weekday() + 1) % 7))
            
            neg_inf = -1e9
            
            # v11.9.6: Advanced Battery Settings
            max_arb_h = int(self.manager.get_setting(CONF_MAX_ARBITRAGE_HOURS, 24))
            min_dis_kwh = float(self.manager.get_setting(CONF_MIN_DISCHARGE_KWH, 0.1))
            min_sell_p = float(self.manager.get_setting(CONF_MIN_SELL_PRICE, 0.01))
            max_p_chg = float(self.manager.get_setting(CONF_BATTERY_MAX_POWER, 6.6))
            max_p_dis = float(self.manager.get_setting(CONF_BATTERY_MAX_POWER, 6.6))
            user_limit = float(self.manager.get_setting(CONF_AI_DISCHARGE_LIMIT, 13.0))

            # DP Tables: [hour][energy_idx][arb_idx] (v11.9.19: Removed boiler dim)
            # State: (revenue, prev_si, prev_ai, action_type, amount)
            full_dp = [[None] * (energy_steps + 1) for _ in range(horizon + 1)]
            for h in range(horizon + 1):
                for si in range(energy_steps + 1):
                    full_dp[h][si] = [(neg_inf, -1, -1, 0, 0.0)] * (max_arb_h + 1)

            # Initial state
            curr_si = min(energy_steps, max(0, int(round((curr_s_raw or 0.0) / 100.0 * b_cap / energy_step))))
            full_dp[0][curr_si][0] = (0.0, -1, -1, ACT_IDLE, 0.0)
            
            # v11.9.14: Define sunrise hour for floor calculation
            sunrise_h = int(float(self.manager.get_setting("sunrise_h", 8.0)))
            
            # v11.9.9: Pre-calculate Survival Floors for the DP horizon (sync with strategy_sell)
            floors_sliding = {}
            for t_idx in range(horizon + 1):
                h_abs = cur_hour + t_idx
                h_rel = h_abs % 24
                if 4 <= h_rel < 10:
                    floors_sliding[t_idx] = min_soc + 1.0 # Turbo morning
                else:
                    # Bridge to next sunrise
                    next_sr_abs = h_abs + 1
                    while next_sr_abs % 24 != sunrise_h: next_sr_abs += 1
                    
                    h_bridge_kwh = 0.0
                    for h_f in range(h_abs + 1, next_sr_abs):
                        l_v = float(normalize_float((avg_cons if h_f < 24 else tomorrow_cons).get(str(h_f % 24), 0.4)))
                        g_v = float(normalize_float((forecast_gen if h_f < 24 else tomorrow_gen).get(str(h_f % 24), 0.0)))
                        h_bridge_kwh += max(0.0, l_v - g_v)
                    
                    # Convert house need to SOC % via efficiency
                    survival_floor = (min_soc + soc_buff) + (h_bridge_kwh / b_cap * 100.0 / eff)
                    floors_sliding[t_idx] = max(user_limit, survival_floor)

            def _update(nsi, nai, reward, act, amt, t_step, c_rev, si_orig, ai_orig):
                """Helper to update state transitions with Survival Floor Penalty (v11.9.9)"""
                if nsi < 0 or nsi > energy_steps: return
                
                total_rev = c_rev + reward
                
                # Apply Penalty if NEXT state violates the survival floor for that hour
                floor_soc = floors_sliding.get(t_step + 1, min_soc)
                if (nsi * energy_step) < (floor_soc * b_cap / 100.0):
                    total_rev -= 1000.0 # Heavy penalty
                
                if total_rev > full_dp[t_step + 1][nsi][nai][0]:
                    full_dp[t_step + 1][nsi][nai] = (total_rev, si_orig, ai_orig, act, amt)

            # --- Forward Induction (Unified 4D DP) ---
            for h in range(horizon):
                abs_h = cur_hour + h
                h_rel = abs_h % 24
                p_buy = float(normalize_float(prices_buy.get(str(abs_h), 0.5)))
                p_sell = float(normalize_float(prices_sell.get(str(abs_h), 0.4)))
                gen = float(normalize_float(forecast_gen.get(str(abs_h), 0.0)))
                cons = float(normalize_float((avg_cons if abs_h < 24 else tomorrow_cons).get(str(h_rel), 0.4)))

                for si in range(energy_steps + 1):
                    for ai in range(max_arb_h + 1):
                        cur_rev, _, _, _, _ = full_dp[h][si][ai]
                        if cur_rev <= neg_inf + 100: continue
                        
                        usable_energy = si * energy_step
                        
                        # Boiler is handled statically now (if enabled)
                        b_use = b_power if b_enabled else 0.0
                        
                        pv_surplus = max(0.0, gen - cons - b_use)
                        pv_deficit = max(0.0, cons + b_use - gen)
                        
                        # 1. ACT_IDLE (Baseline Grid)
                        _update(si, ai, p_sell * pv_surplus - p_buy * pv_deficit + 1e-6, ACT_IDLE, 0.0, h, cur_rev, si, ai)
                                
                        # 2. ACT_DIS
                        if p_sell > min_sell_p and ai < max_arb_h:
                            max_exp = min(max_p_dis, usable_energy)
                            for ei in range(1, int(round(max_exp / energy_step)) + 1):
                                exp = ei * energy_step
                                if exp < min_dis_kwh: continue
                                
                                nsi = si - ei
                                to_grid = max(0.0, exp*eff + gen - cons - b_use)
                                from_grid = max(0.0, cons + b_use - exp*eff - gen)
                                reward = p_sell * to_grid - p_buy * from_grid - (cycle_cost * exp)
                                _update(nsi, ai + 1, reward, ACT_DIS, exp, h, cur_rev, si, ai)
                                
                        # 3. ACT_PV_CHARGE: Surplus to battery (v11.9.24: Single step optimization)
                        if pv_surplus > 0.01 and si < energy_steps:
                            # Charge as much as possible from surplus
                            chg = min(pv_surplus * eff, (energy_steps - si) * energy_step, max_p_chg)
                            if chg > 0.01:
                                ci = int(round(chg / energy_step))
                                if ci > 0:
                                    nsi = si + ci
                                    reward = p_sell * (pv_surplus - chg/eff) - p_buy * pv_deficit
                                    reward += 1e-4 * chg
                                    _update(nsi, ai, reward, ACT_PV_CHARGE, chg, h, cur_rev, si, ai)
 
                        # 4. ACT_GRID_CHARGE: Buy from grid (Keep loop for precision)
                        if si < energy_steps:
                            max_gc = min(max_p_chg, (energy_steps - si) * energy_step)
                            for ci in range(1, int(max_gc / energy_step) + 1):
                                chg = ci * energy_step
                                nsi = si + ci
                                reward = p_sell * pv_surplus - p_buy * (chg/eff + pv_deficit) - (cycle_cost * chg)
                                _update(nsi, ai, reward, ACT_GRID_CHARGE, chg, h, cur_rev, si, ai)
 
                        # 5. ACT_SELF_CONSUME: Battery to home ONLY (v11.9.24: Single step optimization)
                        if pv_deficit > 0.01 and si > 0:
                            # Discharge as much as possible to cover deficit
                            sc = min(usable_energy, pv_deficit / eff, max_p_dis)
                            if sc > 0.01:
                                sci = int(round(sc / energy_step))
                                if sci > 0:
                                    nsi = si - sci
                                    rem_def = max(0.0, pv_deficit - sc * eff)
                                    _update(nsi, ai, -p_buy * rem_def, ACT_SELF_CONSUME, sc, h, cur_rev, si, ai)
                                
                        # 6. ACT_PAID_IMPORT: Negative price handling
                        if p_buy < 0 and (cons + b_use) > 0.01:
                            _update(si, ai, -p_buy * (cons + b_use), ACT_PAID_IMPORT, 0.0, h, cur_rev, si, ai)

            # --- Backtrack ---
            # v11.9.7: Use "Replacement Cost" logic for terminal value (inspired by author's engine)
            # Find minimum future buy price to estimate what it costs to "refill" the battery later
            min_future_buy = min(prices_buy.values()) if prices_buy else 0.5
            # Terminal value = what a kWh in battery is worth at the end of the horizon.
            # It's either the cost to buy it back later + wear, OR the min price we'd sell it for.
            terminal_val_kwh = max(min_sell_p, min_future_buy + cycle_cost)
            
            best_val = neg_inf
            best_state = (curr_si, 0)
            min_end_idx = int(round(min_end_usable / energy_step))
            
            for si in range(energy_steps + 1):
                # Penalty for ending below smart reserve (Survival Floor)
                reserve_penalty = -20.0 if si < min_end_idx else 0.0
                for ai in range(max_arb_h + 1):
                    val, _, _, _, _ = full_dp[horizon][si][ai]
                    # Final score = profit during 48h + value of remaining energy
                    val += (si * energy_step) * terminal_val_kwh + reserve_penalty
                    if val > best_val:
                        best_val = val
                        best_state = (si, ai)

            plan, formatted_plan = {}, {}
            curr_h_state = best_state
            results = []
            for h in range(horizon, 0, -1):
                si, ai = curr_h_state
                entry = full_dp[h][si][ai]
                _, psi, pai, act, amt = entry
                results.append((h-1, act, amt, si))
                curr_h_state = (psi, pai)
            
            results.reverse()
            for h_idx, act, amt, si in results:
                abs_h = cur_hour + h_idx
                h_rel = abs_h % 24
                h_key = f"{h_rel:02d}:00" + (" (Завтра)" if abs_h >= 24 else "")
                
                p_buy = float(normalize_float(prices_buy.get(str(abs_h), 0.5)))
                p_sell = float(normalize_float(prices_sell.get(str(abs_h), 0.4)))
                gen = float(normalize_float(forecast_gen.get(str(abs_h), 0.0)))
                cons = float(normalize_float((avg_cons if abs_h < 24 else tomorrow_cons).get(str(h_rel), 0.4)))
                
                mode = ["IDLE", "DIS", "PV_CHG", "GRID_CHG", "SELF_CON", "PAID_IMP"][act]
                
                soc = int(round((si * energy_step) / b_cap * 100.0))
                
                plan[h_key] = {"mode": mode, "power_kw": round(amt, 2), "target_soc": soc}
                formatted_plan[h_key] = f"{mode} | {round(amt, 2)}kW | SOC: {soc}% | {round(p_buy, 2)}/{round(p_sell, 2)}"

            # Prepare hourly forecast table for debug
            f_table = {}
            for h_idx in range(horizon):
                abs_h = cur_hour + h_idx
                h_key = f"{abs_h % 24:02d}:00" + (" (Next)" if abs_h >= 24 else "")
                f_table[h_key] = {
                    "gen": round(float(normalize_float(forecast_gen.get(str(abs_h), 0.0))), 2),
                    "cons": round(float(normalize_float((avg_cons if abs_h < 24 else tomorrow_cons).get(str(abs_h % 24), 0.4))), 2),
                    "buy": round(prices_buy.get(str(abs_h), 0.0), 2)
                }

            res_final = {
                "plan": plan, 
                "formatted_plan": formatted_plan,
                "debug": {
                    "calc_time": round(time.time()-t0, 2), 
                    "horizon": horizon,
                    "best_val": round(best_val, 2),
                    "energy_step": energy_step,
                    "raw_soc": curr_s_raw,
                    "soc_sensor": self.manager.battery_soc_sensor,
                    "b_cap": b_cap,
                    "forecast_table": f_table
                }
            }
            self._cache = res_final
            self._last_run = t0
            return res_final
        except Exception as e:
            _LOGGER.error(f"DP Advice Error: {e}", exc_info=True)
            return {"error": str(e)}

    def _calc_survival_beyond_horizon(self, end_dt: datetime, b_cap: float) -> float:
        """Estimates the required energy to survive from horizon end until the next charge window."""
        try:
            # Look 18 hours beyond horizon
            survival_hours = 18
            reserve_kwh = 0.0
            
            # 1. Find next sunrise or cheap window
            # For simplicity, we use the average consumption profile until 09:00 AM of the day after
            # If horizon ends at 18:00 Tomorrow, we need to survive until 09:00 Day After Tomorrow.
            
            curr_dt = end_dt
            for _ in range(survival_hours):
                h_rel = curr_dt.hour
                weekday = curr_dt.weekday()
                
                # Get consumption for this hour
                profile = self._ensure_dict(self.manager.get_average_profile("consumption_base", 7, weekday))
                cons = float(normalize_float(profile.get(str(h_rel), 0.4)))
                reserve_kwh += cons
                
                # If it's morning (generation starts), we can stop
                if 7 <= h_rel <= 9:
                    break
                
                curr_dt += timedelta(hours=1)
            
            # Efficiency overhead
            eff = getattr(self.manager, "last_eff_coeff", 0.96)
            return round(reserve_kwh / eff, 2)
            
        except Exception as e:
            _LOGGER.error(f"Error calculating terminal reserve: {e}")
            return 2.0 # Fallback 2kWh

    def _get_smart_gen_forecast(self, horizon) -> Dict[str, float]:
        res = {}
        coeff = getattr(self.manager, "last_blended_coeff", 1.0)
        s_today = getattr(self.manager, "forecast_today_hourly_sensor", None)
        s_tomorrow = getattr(self.manager, "forecast_tomorrow_sensor", None)
        dist_today = self._ensure_dict(self.manager.get_forecast_hourly_distribution(s_today)) if s_today else {}
        dist_tomorrow = self._ensure_dict(self.manager.get_forecast_hourly_distribution(s_tomorrow, (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d"))) if s_tomorrow else {}
        for h, v in dist_today.items(): res[str(h)] = float(normalize_float(v)) * coeff
        for h, v in dist_tomorrow.items(): res[str(int(h) + 24)] = float(normalize_float(v)) * coeff
        return res

    def _ensure_dict(self, data: Any) -> Dict[str, Any]:
        if isinstance(data, dict): return data
        if isinstance(data, list): return {str(i): v for i, v in enumerate(data)}
        return {}

    def _get_prices(self, key: str) -> Dict[str, Any]:
        ps = self.manager.data.get(key, {})
        res = {}
        t_str = datetime.now().strftime("%Y-%m-%d")
        tm_str = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        for h, p in self._ensure_dict(ps.get(t_str, {})).items(): res[str(h)] = p
        tm_data = self._ensure_dict(ps.get(tm_str, {}))
        if not tm_data: tm_data = self._ensure_dict(ps.get(t_str, {}))
        for h, p in tm_data.items(): res[str(int(h) + 24)] = p
        return res

    def _get_deg_cost(self, cap: float) -> float:
        cost = float(self.manager.get_setting(CONF_BATTERY_COST, 0.0))
        cyc = float(self.manager.get_setting(CONF_BATTERY_RATED_CYCLES, 6000))
        if cap <= 0.1 or cyc <= 0 or cost <= 0: return 0.05
        return round(cost / (cyc * cap), 4)
