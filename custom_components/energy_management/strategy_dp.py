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
ENERGY_STEP = 0.1          # 0.1 kWh precision
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
            
            # --- Configuration ---
            max_p_dis = float(self.manager.get_setting(CONF_BATTERY_MAX_POWER, 5.0))
            max_p_chg = max_p_dis 
            curr_s_raw, b_cap_raw, _ = self.manager.get_battery_state()
            b_cap = float(b_cap_raw or 17.0)
            
            energy_step = 0.1
            energy_steps = int(round(b_cap / energy_step))
            
            cycle_cost = self._get_deg_cost(b_cap)
            min_soc = float(self.manager.get_setting(CONF_MIN_SOC_BAT, 10.0))
            soc_buff = float(self.manager.get_setting(CONF_SOC_BUFFER, 13.0))
            eff = getattr(self.manager, "last_eff_coeff", 0.96)
            
            min_end_usable = 2.3 # v11.9.40 (approx 13.5% SOC)
            
            # v11.9.48: Boiler logic completely removed from DP model.
            
            forecast_gen = self._get_smart_gen_forecast(horizon)
            avg_cons = self._ensure_dict(self.manager.get_average_profile("consumption_base", 7, now.weekday()))
            tomorrow_cons = self._ensure_dict(self.manager.get_average_profile("consumption_base", 7, (now.weekday() + 1) % 7))
            
            neg_inf = -1e9
            # v11.9.42: Arbitrage TOP hours per day
            max_arb_h = int(self.manager.get_setting(CONF_MAX_ARBITRAGE_HOURS, 3))
            min_dis_kwh = float(self.manager.get_setting(CONF_MIN_DISCHARGE_KWH, 0.5))
            min_sell_p = float(self.manager.get_setting(CONF_MIN_SELL_PRICE, 0.01))
            
            # DP Table: [hour][energy_idx] (2D Optimization)
            full_dp = [[(neg_inf, -1, ACT_IDLE, 0.0)] * (energy_steps + 1) for _ in range(horizon + 1)]

            # Initial state
            curr_si = min(energy_steps, max(0, int(round((curr_s_raw or 0.0) / 100.0 * b_cap / energy_step))))
            full_dp[0][curr_si] = (0.0, -1, ACT_IDLE, 0.0)
            
            sunrise_h = int(float(self.manager.get_setting("sunrise_h", 8.0)))
            
            def _update(nsi, act, amt, t_step, si_orig, total_rev):
                if nsi < 0 or nsi > energy_steps: return
                
                # v11.9.45: Strict Global Floor (using min_soc instead of soc_buff)
                floor_soc = min_soc
                
                if (nsi * energy_step) < (floor_soc * b_cap / 100.0):
                    # Penalize heavily if we DISCHARGE below the floor
                    if nsi < si_orig:
                        total_rev -= 10000.0
                    # If we are already below floor (at start), standing IDLE is okay but not great
                    elif nsi == si_orig:
                        total_rev -= 10.0 
                
                if total_rev > full_dp[t_step + 1][nsi][0]:
                    full_dp[t_step + 1][nsi] = (total_rev, si_orig, act, amt)

            # v11.9.41: Top-N Arbitrage Sorting PER DAY
            top_sell_set = set()
            for day in range(horizon // 24 + 1):
                d_start = cur_hour + day * 24
                d_end = d_start + 24
                d_prices = [(int(h_key), p) for h_key, p in prices_sell.items() if d_start <= int(h_key) < d_end and p > min_sell_p]
                d_top = sorted(d_prices, key=lambda x: x[1], reverse=True)[:max_arb_h]
                for h_abs, p in d_top: top_sell_set.add(h_abs)
            
            # --- Forward Induction (2D DP) ---
            for h in range(horizon):
                abs_h = cur_hour + h
                h_rel = abs_h % 24
                p_buy = float(normalize_float(prices_buy.get(str(abs_h), 0.5)))
                p_sell = float(normalize_float(prices_sell.get(str(abs_h), 0.4)))
                gen = float(normalize_float(forecast_gen.get(str(abs_h), 0.0)))
                cons = float(normalize_float((avg_cons if abs_h < 24 else tomorrow_cons).get(str(h_rel), 0.4)))
                
                # v11.9.47: Remove boiler from BASELINE.
                # It shouldn't 'eat' the sun in the model and force grid charging.
                pv_surplus = max(0.0, gen - cons)
                pv_deficit = max(0.0, cons - gen)

                for si in range(energy_steps + 1):
                    cur_rev, _, _, _ = full_dp[h][si]
                    if cur_rev <= neg_inf + 100: continue
                    
                    usable_energy = si * energy_step
                    # 1. ACT_IDLE: Baseline
                    _update(si, ACT_IDLE, 0.0, h, si, cur_rev + p_sell * pv_surplus - p_buy * pv_deficit + 1e-6)
                            
                    # 2. ACT_DIS: Forced discharge to grid (Arbitrage)
                    if abs_h in top_sell_set:
                        exp = min(usable_energy, max_p_dis)
                        if exp >= min_dis_kwh:
                            to_grid = max(0.0, exp + gen - cons - b_use)
                            from_grid = max(0.0, cons + b_use - exp - gen)
                            reward = p_sell * to_grid - p_buy * from_grid - (cycle_cost * exp)
                            nsi = si - int(round(exp / energy_step))
                            _update(nsi, ACT_DIS, exp, h, si, cur_rev + reward)
                            
                    # 3. ACT_PV_CHARGE: Surplus to battery
                    if pv_surplus > 0.01 and si < energy_steps:
                        chg = min(pv_surplus, (energy_steps - si) * energy_step, max_p_chg)
                        if chg > 0.01:
                            ci = int(round(chg / energy_step))
                            if ci > 0:
                                reward = p_sell * max(0.0, pv_surplus - chg) - p_buy * pv_deficit
                                _update(si + ci, ACT_PV_CHARGE, chg, h, si, cur_rev + reward)

                    # 4. ACT_GRID_CHARGE: Buy from grid
                    if si < energy_steps:
                        max_gc = min(max_p_chg, (energy_steps - si) * energy_step)
                        for ci in range(1, int(max_gc / energy_step) + 1):
                            chg = ci * energy_step
                            reward = p_sell * pv_surplus - p_buy * (chg + pv_deficit) - (cycle_cost * chg)
                            _update(si + ci, ACT_GRID_CHARGE, chg, h, si, cur_rev + reward)

                    # 5. ACT_SELF_CONSUME: Battery to home
                    if pv_deficit > 0.01 and si > 0:
                        sc = min(usable_energy, pv_deficit, max_p_dis)
                        if sc > 0.01:
                            sci = int(round(sc / energy_step))
                            if sci > 0:
                                rem_def = max(0.0, pv_deficit - sc)
                                _update(si - sci, ACT_SELF_CONSUME, sc, h, si, cur_rev - p_buy * rem_def)
                            
                    # 6. ACT_PAID_IMPORT: Negative price handling
                    if p_buy < 0 and (cons + b_use) > 0.01:
                        _update(si, ACT_PAID_IMPORT, 0.0, h, si, cur_rev - p_buy * (cons + b_use))

            # --- Backtrack ---
            min_future_buy = min(prices_buy.values()) if prices_buy else 0.5
            terminal_val_kwh = max(min_sell_p, min_future_buy)
            
            best_val, best_si = neg_inf, curr_si
            min_end_idx = int(round(min_end_usable / energy_step))
            
            for si in range(energy_steps + 1):
                reserve_penalty = -20.0 if si < min_end_idx else 0.0
                val, _, _, _ = full_dp[horizon][si]
                val += (si * energy_step) * terminal_val_kwh + reserve_penalty
                if val > best_val:
                    best_val, best_si = val, si

            plan, formatted_plan, f_table = {}, {}, {}
            curr_si_back = best_si
            results = []
            for h in range(horizon - 1, -1, -1):
                res = full_dp[h+1][curr_si_back]
                if res[1] == -1: break
                _, prev_si, act, amt = res
                results.append((h, act, amt, curr_si_back))
                curr_si_back = prev_si
            
            results.reverse()
            for h_idx, act, amt, si in results:
                abs_h = cur_hour + h_idx
                h_rel = abs_h % 24
                h_key = f"{h_rel:02d}:00" + (" (Завтра)" if abs_h >= 24 else "")
                p_buy = float(normalize_float(prices_buy.get(str(abs_h), 0.5)))
                p_sell = float(normalize_float(prices_sell.get(str(abs_h), 0.4)))
                gen = float(normalize_float(forecast_gen.get(str(abs_h), 0.0)))
                cons = float(normalize_float((avg_cons if abs_h < 24 else tomorrow_cons).get(str(h_rel), 0.4)))
                
                mode_map = ["IDLE", "DIS", "PV_CHG", "GRID_CHG", "SELF_CON", "PAID_IMP"]
                mode = mode_map[act]
                if act == ACT_IDLE:
                    if gen > cons + 0.1: mode = "SOL"
                    else: mode = "GRID"
                soc = int(round((si * energy_step) / b_cap * 100.0))
                plan[h_key] = {"mode": mode, "power_kw": round(amt, 2), "target_soc": soc}
                formatted_plan[h_key] = f"{mode} | {round(amt, 2)}kW | SOC: {soc}% | {round(p_buy, 2)}/{round(p_sell, 2)}"
                f_table[str(abs_h)] = {
                    "gen": round(gen, 2),
                    "cons": round(cons, 2),
                    "buy": round(p_buy, 2),
                    "sell": round(p_sell, 2)
                }

            # Debug Info
            total_gen_tomorrow = sum(forecast_gen.get(str(h), 0.0) for h in range(cur_hour + 24, cur_hour + 48))
            
            if "calculation_debug" not in self.manager.data:
                self.manager.data["calculation_debug"] = {}
                
            self.manager.data["calculation_debug"]["dp_constants"] = {
                "terminal_val": round(terminal_val_kwh, 4),
                "min_sell_p": round(min_sell_p, 4),
                "cycle_cost": round(cycle_cost, 4),
                "horizon_h": horizon,
                "gen_tomorrow": round(total_gen_tomorrow, 2),
                "top_hours": sorted(list(top_sell_set))
            }

            res_final = {
                "plan": plan, 
                "formatted_plan": formatted_plan,
                "best_value": round(best_val, 2),
                "debug": {
                    "calc_time": round(time.time()-t0, 2), 
                    "horizon": horizon,
                    "b_cap": b_cap,
                    "forecast_table": f_table
                }
            }
            self._cache["advice"] = res_final
            self._last_run = t0
            return res_final
        except Exception as e:
            _LOGGER.error(f"DP Advice Error: {e}", exc_info=True)
            return {"error": str(e)}

    def _calc_survival_beyond_horizon(self, end_dt: datetime, b_cap: float) -> float:
        """Estimates the required energy to survive from horizon end until the next charge window."""
        try:
            survival_hours = 18
            reserve_kwh = 0.0
            curr_dt = end_dt
            for _ in range(survival_hours):
                h_rel = curr_dt.hour
                weekday = curr_dt.weekday()
                profile = self._ensure_dict(self.manager.get_average_profile("consumption_base", 7, weekday))
                cons = float(normalize_float(profile.get(str(h_rel), 0.4)))
                reserve_kwh += cons
                if 7 <= h_rel <= 9: break
                curr_dt += timedelta(hours=1)
            eff = getattr(self.manager, "last_eff_coeff", 0.96)
            return round(reserve_kwh / eff, 2)
        except Exception as e:
            _LOGGER.error(f"Error calculating terminal reserve: {e}")
            return 2.0 

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
