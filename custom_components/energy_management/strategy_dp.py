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
)
from .utils import normalize_float, round_f

_LOGGER = logging.getLogger(__name__)

# --- DP Parameters ---
ENERGY_STEP = 0.2          # 0.2 kWh precision (5 steps per kWh)
BOILER_STEPS = 10         # 1 step = 5 degrees (10 to 60)
INF = 1e9                 

# Action types
ACT_SOL = 0
ACT_DIS = 1
ACT_PV_CHARGE = 2
ACT_GRID_CHARGE = 3
ACT_SELF_CONSUME = 4

class DPPlanner:
    def __init__(self, manager):
        self.manager = manager
        
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
            
            # v11.9.0: Step resolution improvement
            energy_step = 0.2 if b_cap > 15 else 0.1
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
            
            neg_inf = -1e9
            
            # DP Tables: [hour][energy_idx][boiler_idx]
            # State: (revenue, prev_si, prev_bi, action_type, amount, boiler_on)
            full_dp = [[None] * (energy_steps + 1) for _ in range(horizon + 1)]
            for h in range(horizon + 1):
                for si in range(energy_steps + 1):
                    full_dp[h][si] = [(neg_inf, -1, -1, 0, 0.0, False)] * (BOILER_STEPS + 1)

            # Initial state
            curr_si = min(energy_steps, max(0, int(round((curr_s_raw or 0.0) / 100.0 * b_cap / energy_step))))
            if b_enabled:
                temp = float(self.manager.get_sensor_float(temp_s) or 20.0) if temp_s else 30.0
                curr_bi = int(round(max(0, min(50, temp-10))/50.0 * BOILER_STEPS))
            else: curr_bi = 0
            
            full_dp[0][curr_si][curr_bi] = (0.0, -1, -1, 0, 0.0, False)

            # --- Forward Induction (Unified DP with Boiler) ---
            for h in range(horizon):
                abs_h = cur_hour + h
                h_rel = abs_h % 24
                p_buy = float(normalize_float(prices_buy.get(str(abs_h), 0.5)))
                p_sell = float(normalize_float(prices_sell.get(str(abs_h), 0.4)))
                gen = float(normalize_float(forecast_gen.get(str(abs_h), 0.0)))
                cons = float(normalize_float((avg_cons if abs_h < 24 else tomorrow_cons).get(str(h_rel), 0.4)))
                
                # Dynamic night buffer
                h_min_soc = (min_soc + soc_buff) if (21 <= h_rel or h_rel <= 6) else min_soc

                for si in range(energy_steps + 1):
                    for bi in range(BOILER_STEPS + 1):
                        cur_rev, _, _, _, _, _ = full_dp[h][si][bi]
                        if cur_rev <= neg_inf + 100: continue
                        
                        usable_energy = si * energy_step
                        cur_boi_kwh = (bi / float(BOILER_STEPS)) * b_capacity if b_enabled else 0.0
                        
                        for b_on in ([True, False] if b_enabled else [False]):
                            b_use = b_power if b_on else 0.0
                            # Transition boiler
                            next_boi_kwh = max(0.0, min(b_capacity, cur_boi_kwh - 0.4 + b_use)) # 0.4kWh loss/usage
                            nbi = int(round(next_boi_kwh / b_capacity * BOILER_STEPS)) if b_enabled else 0
                            
                            pv_surplus = max(0.0, gen - cons - b_use)
                            pv_deficit = max(0.0, cons + b_use - gen)
                            
                            def update_state(nsi: int, rwd: float, act: int, amt: float, penalty: float = 0.0):
                                total_rev = cur_rev + rwd - penalty
                                if total_rev > full_dp[h+1][nsi][nbi][0]:
                                    full_dp[h+1][nsi][nbi] = (total_rev, si, bi, act, amt, b_on)

                            # 1. ACT_SOL (Grid Only / Fallback): Battery idle
                            # v11.9.2: Apply penalty only if we are below floor and not charging
                            penalty = 5.0 if (si * energy_step / b_cap * 100) < h_min_soc else 0.0
                            update_state(si, p_sell * pv_surplus - p_buy * pv_deficit + 1e-6, ACT_SOL, 0.0, penalty)
                            
                            # 2. ACT_DIS: Discharge to grid (Commercial Sale)
                            if p_sell > 0.01:
                                max_exp = min(max_p_dis, usable_energy)
                                for ei in range(1, int(round(max_exp / energy_step)) + 1):
                                    exp = ei * energy_step
                                    nsi = si - ei
                                    to_grid = max(0.0, exp*eff + gen - cons - b_use)
                                    from_grid = max(0.0, cons + b_use - exp*eff - gen)
                                    reward = p_sell * to_grid - p_buy * from_grid - (cycle_cost * exp)
                                    # v11.9.2: STRICT penalty for grid sales below survival floor
                                    dis_penalty = 10.0 if (nsi * energy_step / b_cap * 100) < h_min_soc else 0.0
                                    update_state(nsi, reward, ACT_DIS, exp, dis_penalty)
                                    
                            # 3. ACT_PV_CHARGE: Surplus to battery (Always good)
                            if pv_surplus > 0.01 and si < energy_steps:
                                max_pvc = min(pv_surplus, (energy_steps - si) * energy_step, max_p_chg / eff)
                                for ci in range(1, int(max_pvc / energy_step) + 1):
                                    chg = ci * energy_step
                                    nsi = si + ci
                                    reward = p_sell * (pv_surplus - chg/eff) - p_buy * pv_deficit
                                    reward += 1e-4 * chg
                                    update_state(nsi, reward, ACT_PV_CHARGE, chg)

                            # 4. ACT_GRID_CHARGE: Buy from grid (Helps survival)
                            if si < energy_steps:
                                max_gc = min(max_p_chg, (energy_steps - si) * energy_step)
                                for ci in range(1, int(max_gc / energy_step) + 1):
                                    chg = ci * energy_step
                                    nsi = si + ci
                                    # v11.9.3: Incentive to reach survival floor if price is not extreme
                                    em_bonus = 0.4 if (si * energy_step / b_cap * 100) < h_min_soc else 0.0
                                    reward = p_sell * pv_surplus - p_buy * (chg/eff + pv_deficit) - (cycle_cost * chg) + em_bonus
                                    update_state(nsi, reward, ACT_GRID_CHARGE, chg)

                            # 5. ACT_SELF_CONSUME: Battery to home ONLY
                            if pv_deficit > 0.01 and si > 0:
                                max_sc = min(usable_energy, pv_deficit / eff)
                                for sci in range(1, int(round(max_sc / energy_step)) + 1):
                                    sc = sci * energy_step
                                    nsi = si - sci
                                    rem_def = max(0.0, pv_deficit - sc * eff)
                                    # v11.9.3: Physical limit is strict, survival floor is soft for house
                                    sc_penalty = 20.0 if (nsi * energy_step / b_cap * 100) < min_soc else 0.0
                                    update_state(nsi, -p_buy * rem_def, ACT_SELF_CONSUME, sc, sc_penalty)
                                    
                            # 6. ACT_PAID_IMPORT: Negative price handling
                            if p_buy < 0 and (cons + b_use) > 0.01:
                                update_state(si, -p_buy * (cons + b_use), ACT_PAID_IMPORT, 0.0)

            # --- Backtrack ---
            best_val = neg_inf
            best_state = (curr_si, curr_bi)
            avg_p_sell = sum(prices_sell.values()) / len(prices_sell) if prices_sell else 0.4
            
            # v11.9.1: Enforce min_end_usable in backtrack selection
            min_end_idx = int(round(min_end_usable / energy_step))
            
            for si in range(energy_steps + 1):
                # Penalty for ending below smart reserve
                reserve_penalty = -20.0 if si < min_end_idx else 0.0
                
                for bi in range(BOILER_STEPS + 1):
                    val, _, _, _, _, _ = full_dp[horizon][si][bi]
                    # Terminal value: remaining energy value
                    val += (si * energy_step) * avg_p_sell * 0.9 + reserve_penalty
                    if val > best_val:
                        best_val = val
                        best_state = (si, bi)

            plan, formatted_plan = {}, {}
            curr_h_state = best_state
            results = []
            for h in range(horizon, 0, -1):
                si, bi = curr_h_state
                entry = full_dp[h][si][bi]
                _, psi, pbi, act, amt, b_on = entry
                results.append((h-1, act, amt, si, bi, b_on))
                curr_h_state = (psi, pbi)
            
            results.reverse()
            for h_idx, act, amt, si, bi, b_on in results:
                abs_h = cur_hour + h_idx
                h_rel = abs_h % 24
                h_key = f"{h_rel:02d}:00" + (" (Завтра)" if abs_h >= 24 else "")
                
                p_buy = float(normalize_float(prices_buy.get(str(abs_h), 0.5)))
                p_sell = float(normalize_float(prices_sell.get(str(abs_h), 0.4)))
                gen = float(normalize_float(forecast_gen.get(str(abs_h), 0.0)))
                cons = float(normalize_float((avg_cons if abs_h < 24 else tomorrow_cons).get(str(h_rel), 0.4)))
                
                b_use = b_power if b_on else 0.0
                mode = ["SOL", "DIS", "PV_CHG", "GRID_CHG", "SELF_CON", "PAID_IMP"][act]
                
                soc = int(round((si * energy_step) / b_cap * 100.0))
                b_indicator = f" | B: {'ON' if b_on else 'OFF'}"
                
                plan[h_key] = {"mode": mode, "power_kw": round(amt, 2), "target_soc": soc}
                formatted_plan[h_key] = f"{mode} | {round(amt, 2)}kW{b_indicator} | SOC: {soc}% | {round(p_buy, 2)}/{round(p_sell, 2)}"

            return {
                "plan": plan, 
                "formatted_plan": formatted_plan,
                "debug": {
                    "calc_time": round(time.time()-t0, 2), 
                    "horizon": horizon,
                    "best_val": round(best_val, 2),
                    "energy_step": energy_step
                }
            }
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
