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
        
    def get_dp_advice(self, data_snapshot: Dict[str, Any] = None) -> Dict[str, Any]:
        t0 = time.time()
        # v11.9.61: Restore cache to prevent UI lag and sensor thrashing
        # v11.9.74: Bypass cache if explicit data_snapshot is provided
        if not data_snapshot and self._last_run and (t0 - self._last_run) < 60:
            return self._cache.get("advice", {})

        try:
            now = self.manager.now
            cur_hour = now.hour
            
            # v11.9.64: Use pre-fetched snapshot if available
            if data_snapshot:
                prices_buy = data_snapshot.get("prices_buy", {})
                prices_sell = data_snapshot.get("prices_sell", {})
                curr_s_raw = data_snapshot.get("soc", 0.0)
                b_cap = data_snapshot.get("capacity", 17.0)
            else:
                prices_buy = self._get_prices("prices_buy")
                prices_sell = self._get_prices("prices_sell")
                curr_s_raw, b_cap_raw, _ = self.manager.get_battery_state()
                b_cap = float(b_cap_raw or 17.0)

            if not prices_buy or not prices_sell:
                return {"error": "Missing price data"}

            b_cap = max(0.1, b_cap) # Prevent division by zero
            available_hours = sorted([int(h) for h in prices_buy.keys()])
            max_abs_h = max(available_hours) if available_hours else cur_hour + 23
            horizon = min(48, max_abs_h - cur_hour + 1)
            
            # --- Configuration ---
            max_p_dis = float(normalize_float(self.manager.get_setting(CONF_BATTERY_MAX_POWER, 5.0)))
            max_p_chg = max_p_dis 
            
            energy_step = 0.1
            energy_steps = int(round(b_cap / energy_step))
            
            cycle_cost = self._get_deg_cost(b_cap)
            min_soc = float(normalize_float(self.manager.get_setting(CONF_MIN_SOC_BAT, 10.0)))
            soc_buff = float(normalize_float(self.manager.get_setting(CONF_SOC_BUFFER, 13.0)))
            eff = getattr(self.manager, "last_eff_coeff", 0.98)  # align with strategy_base.py hardcoded 0.98
            
            min_end_usable = 2.3 # v11.9.40 (approx 13.5% SOC)
            
            # v11.9.48: Boiler logic completely removed from DP model.
            
            # v11.9.61: Use standard prediction profiles (consistent with working Sell strategy)
            forecast_gen = self.manager.get_predicted_profile("generation")
            avg_cons = self.manager.get_predicted_profile("consumption_base")
            # For tomorrow, use the same profile but shifted or average (simplified fallback)
            tomorrow_gen = self.manager.get_average_profile("generation", 7, (now.weekday() + 1) % 7)
            tomorrow_cons = self.manager.get_average_profile("consumption_base", 7, (now.weekday() + 1) % 7)
            
            # Populate extended horizon for DP
            f_gen_full = {str(h): float(normalize_float(forecast_gen.get(str(h), 0.0))) for h in range(24)}
            f_cons_full = {str(h): float(normalize_float(avg_cons.get(str(h), 0.0))) for h in range(24)}
            for h in range(24):
                f_gen_full[str(h+24)] = float(normalize_float(tomorrow_gen.get(str(h), 0.0)))
                f_cons_full[str(h+24)] = float(normalize_float(tomorrow_cons.get(str(h), 0.0)))
            
            neg_inf = -1e9
            # v11.9.42: Arbitrage TOP hours per day
            max_arb_h = int(normalize_float(self.manager.get_setting(CONF_MAX_ARBITRAGE_HOURS, 3)))
            min_dis_kwh = float(normalize_float(self.manager.get_setting(CONF_MIN_DISCHARGE_KWH, 0.5)))
            min_sell_p = float(normalize_float(self.manager.get_setting(CONF_MIN_SELL_PRICE, 0.01)))
            
            # DP Table: [hour][energy_idx] (2D Optimization)
            full_dp = [[(neg_inf, -1, ACT_IDLE, 0.0)] * (energy_steps + 1) for _ in range(horizon + 1)]

            # Initial state
            curr_si = min(energy_steps, max(0, int(round((curr_s_raw or 0.0) / 100.0 * b_cap / energy_step))))
            full_dp[0][curr_si] = (0.0, -1, ACT_IDLE, 0.0)
            
            sunrise_h = int(float(self.manager.get_setting("sunrise_h", 8.0)))
            
            def _update(nsi, act, amt, t_step, si_orig, total_rev):
                if nsi < 0 or nsi > energy_steps: return
                
                # v11.9.54: Progressive Global Floor Penalty
                floor_idx = int(round(min_soc / 100.0 * energy_steps))
                if nsi < floor_idx:
                    # Penalize distance to floor to force maximum recovery speed
                    dist_kwh = (floor_idx - nsi) * energy_step
                    total_rev -= (5000.0 + dist_kwh * 1000.0)
                
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
                p_buy = float(normalize_float(prices_buy.get(str(abs_h), 0.5)))
                p_sell = float(normalize_float(prices_sell.get(str(abs_h), 0.4)))
                gen = float(normalize_float(f_gen_full.get(str(abs_h), 0.0)))
                cons = float(normalize_float(f_cons_full.get(str(abs_h), 0.4)))
                
                # v11.9.47: Remove boiler from BASELINE.
                # It shouldn't 'eat' the sun in the model and force grid charging.
                pv_surplus = max(0.0, gen - cons)
                pv_deficit = max(0.0, cons - gen)

                for si in range(energy_steps + 1):
                    cur_rev, _, _, _ = full_dp[h][si]
                    if cur_rev <= neg_inf + 100: continue
                    
                    usable_energy = si * energy_step  # DC kWh stored in battery
                    # 1. ACT_IDLE: Baseline
                    _update(si, ACT_IDLE, 0.0, h, si, cur_rev + p_sell * pv_surplus - p_buy * pv_deficit + 1e-6)
                            
                    # 2. ACT_DIS: Forced discharge to grid (Arbitrage)
                    # exp_dc = DC energy drawn from battery; exp_ac = AC energy after inverter losses
                    if abs_h in top_sell_set:
                        exp_dc = min(usable_energy, max_p_dis)
                        exp_ac = exp_dc * eff
                        if exp_dc >= min_dis_kwh:
                            to_grid = max(0.0, exp_ac + gen - cons)
                            from_grid = max(0.0, cons - exp_ac - gen)
                            reward = p_sell * to_grid - p_buy * from_grid - (cycle_cost * exp_dc)
                            nsi = si - int(round(exp_dc / energy_step))
                            _update(nsi, ACT_DIS, exp_dc, h, si, cur_rev + reward)  # amt=DC (inverter command)
                            
                    # 3. ACT_PV_CHARGE: Surplus PV (AC) to battery (DC)
                    # chg_ac = AC power from PV; chg_dc = DC actually stored (after inverter losses)
                    if pv_surplus > 0.01 and si < energy_steps:
                        max_storable_dc = (energy_steps - si) * energy_step
                        chg_ac = min(pv_surplus, max_storable_dc / eff, max_p_chg)
                        chg_dc = chg_ac * eff
                        if chg_dc > 0.01:
                            ci = int(round(chg_dc / energy_step))
                            if ci > 0:
                                reward = p_sell * max(0.0, pv_surplus - chg_ac) - p_buy * pv_deficit
                                _update(si + ci, ACT_PV_CHARGE, chg_dc, h, si, cur_rev + reward)  # amt=DC (BMS command)

                    # 4. ACT_GRID_CHARGE: Buy from grid (AC) -> store in battery (DC)
                    # Iterate over DC steps (0.5 kWh granularity for performance), pay for AC drawn
                    # Note: grid_charge_step=0.5 reduces loop from ~66 to ~13 iterations per node
                    if si < energy_steps:
                        grid_charge_step = 0.1  # kWh DC granularity
                        max_storable_dc = min(max_p_chg * eff, (energy_steps - si) * energy_step)
                        ci_max = max(1, int(max_storable_dc / grid_charge_step))
                        for ci_coarse in range(1, ci_max + 1):
                            chg_dc = ci_coarse * grid_charge_step
                            chg_ac = chg_dc / eff  # AC drawn from grid to achieve chg_dc storage
                            ci = int(round(chg_dc / energy_step))
                            if si + ci > energy_steps: break
                            reward = p_sell * pv_surplus - p_buy * (chg_ac + pv_deficit) - (cycle_cost * chg_dc)
                            _update(si + ci, ACT_GRID_CHARGE, chg_dc, h, si, cur_rev + reward)  # amt=DC (BMS command)

                    # 5. ACT_SELF_CONSUME: Battery (DC) to home (AC)
                    # sc_dc = DC drawn from battery; actual AC coverage = sc_dc * eff
                    if pv_deficit > 0.01 and si > 0:
                        sc_dc = min(usable_energy, pv_deficit / eff, max_p_dis)
                        sc_ac = sc_dc * eff  # AC coverage for home
                        if sc_dc > 0.01:
                            sci = int(round(sc_dc / energy_step))
                            if sci > 0:
                                rem_def = max(0.0, pv_deficit - sc_ac)
                                _update(si - sci, ACT_SELF_CONSUME, sc_dc, h, si, cur_rev - p_buy * rem_def)  # amt=DC (BMS command)
                            
                    # 6. ACT_PAID_IMPORT: Negative price — let house consume from grid,
                    # keep battery intact to save capacity for an upcoming bigger sell peak.
                    if p_buy < 0 and cons > 0.01:
                        _update(si, ACT_PAID_IMPORT, 0.0, h, si, cur_rev - p_buy * cons)

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

            plan, plan_by_timestamp, formatted_plan, f_table = {}, {}, {}, {}
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
                gen = float(normalize_float(f_gen_full.get(str(abs_h), 0.0)))
                cons = float(normalize_float(f_cons_full.get(str(abs_h), 0.4)))
                
                mode_map = ["IDLE", "DIS", "PV_CHG", "GRID_CHG", "SELF_CON", "PAID_IMP"]
                mode = mode_map[act]
                if act == ACT_IDLE:
                    if gen > cons + 0.1: mode = "SOL"
                    elif abs(gen - cons) < 0.1: mode = "IDLE"
                    else: mode = "GRID"
                soc = int(round((si * energy_step) / b_cap * 100.0))
                plan[h_key] = {"mode": mode, "power_kw": round(amt, 2), "target_soc": soc}
                formatted_plan[h_key] = f"{mode} | {round(amt, 2)}kW | SOC: {soc}% | {round(p_buy, 2)}/{round(p_sell, 2)} | L:{round(cons,1)} G:{round(gen,1)}"
                
                # Absolute timestamp mapping for easy coordinate lookup
                ts_dt = now + timedelta(hours=h_idx)
                ts_key = ts_dt.strftime("%Y-%m-%d %H:00")
                plan_by_timestamp[ts_key] = {"mode": mode, "power_kw": round(amt, 2), "target_soc": soc}

                f_table[str(abs_h)] = {
                    "gen": round(gen, 2),
                    "cons": round(cons, 2),
                    "buy": round(p_buy, 2),
                    "sell": round(p_sell, 2)
                }

            # Debug Info

            # Debug Info
            coeff = getattr(self.manager, "last_blended_coeff", 1.0)
            total_gen_today_raw = sum(f_gen_full.get(str(h), 0.0) for h in range(0, 24)) / (coeff if coeff > 0 else 1.0)
            total_gen_today = sum(f_gen_full.get(str(h), 0.0) for h in range(0, 24))
            total_gen_today_rem = sum(f_gen_full.get(str(h), 0.0) for h in range(cur_hour, 24))
            total_gen_tomorrow = sum(f_gen_full.get(str(h), 0.0) for h in range(24, 48))
            
            # v11.9.68: Fix debug info - don't query HA directly from thread!
            if "calculation_debug" not in self.manager.data:
                self.manager.data["calculation_debug"] = {}
                
            dp_constants = {
                "terminal_val": round(terminal_val_kwh, 4),
                "min_sell_p": round(min_sell_p, 4),
                "cycle_cost": round(cycle_cost, 4),
                "horizon_h": horizon,
                "soc_start_pct": round(float(curr_s_raw or 0.0), 2),
                "soc_sensor_id": getattr(self.manager, 'battery_soc_sensor', 'None'),
                "b_cap_kwh": round(b_cap, 2),
                "gen_remaining_kwh": round(float(total_gen_today_rem), 2),
                "gen_total_today_kwh": round(float(total_gen_today), 2),
                "gen_coeff": round(float(coeff), 3),
                "gen_sensors": self.manager.forecast_today_sensor,
                "top_hours": sorted(list(top_sell_set))
            }

            res_final = {
                "plan": plan, 
                "plan_by_timestamp": plan_by_timestamp,
                "formatted_plan": formatted_plan,
                "best_value": round(best_val, 2),
                "debug": {
                    "calc_time": round(time.time()-t0, 2), 
                    "horizon": horizon,
                    "b_cap": b_cap,
                    "constants": dp_constants
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
        
        # v11.9.60: Start with average profile as BASELINE (Always reliable)
        profile_today = self._ensure_dict(self.manager.get_average_profile("generation", 14, datetime.now().weekday()))
        profile_tm = self._ensure_dict(self.manager.get_average_profile("generation", 14, (datetime.now().weekday() + 1) % 7))
        
        for h in range(24):
            res[str(h)] = float(normalize_float(profile_today.get(str(h), 0.0)))
            res[str(h + 24)] = float(normalize_float(profile_tm.get(str(h), 0.0)))
            
        # Overlay smart forecast if available (Solcast/Forecast.Solar)
        s_today = getattr(self.manager, "forecast_today_hourly_sensor", [])
        s_tomorrow = getattr(self.manager, "forecast_tomorrow_sensor", [])
        
        dist_today = self._ensure_dict(self.manager.get_forecast_hourly_distribution(s_today)) if s_today else {}
        if any(v > 0.01 for v in dist_today.values()):
             for h, v in dist_today.items(): res[str(h)] = float(normalize_float(v)) * coeff
             
        dist_tomorrow = self._ensure_dict(self.manager.get_forecast_hourly_distribution(s_tomorrow, (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d"))) if s_tomorrow else {}
        if any(v > 0.01 for v in dist_tomorrow.values()):
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
        for h, p in tm_data.items(): res[str(int(h) + 24)] = p
        return res

    def _get_deg_cost(self, cap: float) -> float:
        cost = float(self.manager.get_setting(CONF_BATTERY_COST, 0.0))
        cyc = float(self.manager.get_setting(CONF_BATTERY_RATED_CYCLES, 6000))
        if cap <= 0.1 or cyc <= 0 or cost <= 0: return 0.05
        return round(cost / (cyc * cap), 4)
