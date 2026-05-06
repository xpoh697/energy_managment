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
            
            # --- Params ---
            max_p = float(self.manager.get_setting(CONF_BATTERY_MAX_POWER, 5.0))
            curr_s_raw, b_cap_raw, _ = self.manager.get_battery_state()
            b_cap = float(b_cap_raw or 17.0)
            deg_cost = self._get_deg_cost(b_cap)
            min_soc = float(self.manager.get_setting(CONF_MIN_SOC_BAT, 10.0))
            soc_buff = float(self.manager.get_setting(CONF_SOC_BUFFER, 13.0))
            eff = getattr(self.manager, "last_eff_coeff", 0.96)
            
            energy_steps = int(round(b_cap / ENERGY_STEP))
            
            b_power = float(self.manager.get_setting(CONF_BOILER_POWER, 2.5))
            b_capacity = float(self.manager.get_setting(CONF_BOILER_CAPACITY, 8.5))
            temp_s = self.manager.get_setting(CONF_BOILER_TEMP_SENSOR)
            b_enabled = bool(self.manager.get_setting(CONF_BOILER_ENABLE, False)) or bool(temp_s)
            
            forecast_gen = self._get_smart_gen_forecast(horizon)
            avg_cons = self._ensure_dict(self.manager.get_average_profile("consumption_base", 7, now.weekday()))
            tomorrow_cons = self._ensure_dict(self.manager.get_average_profile("consumption_base", 7, (now.weekday() + 1) % 7))
            
            # DP Tables: [hour][energy_idx][boiler_idx]
            # We store (max_revenue, prev_si, prev_bi, action_type, amount, boiler_on)
            dp = [[None] * (energy_steps + 1) for _ in range(horizon + 1)]
            for h in range(horizon + 1):
                for si in range(energy_steps + 1):
                    dp[h][si] = [(-INF, -1, -1, 0, 0.0, False)] * (BOILER_STEPS + 1)

            # Initial state
            curr_si = min(energy_steps, max(0, int(round((curr_s_raw or 0.0) / 100.0 * b_cap / ENERGY_STEP))))
            temp = 20.0
            if b_enabled:
                temp = float(self.manager.get_sensor_float(temp_s) or 20.0) if temp_s else 30.0
                curr_bi = int(round(max(0, min(50, temp-10))/50.0 * BOILER_STEPS))
            else: curr_bi = 0
            
            dp[0][curr_si][curr_bi] = (0.0, -1, -1, 0, 0.0, False)

            # --- Forward Induction ---
            for h in range(horizon):
                abs_h = cur_hour + h
                h_rel = abs_h % 24
                p_buy = float(normalize_float(prices_buy.get(str(abs_h), 0.5)))
                p_sell = float(normalize_float(prices_sell.get(str(abs_h), 0.4)))
                gen = float(normalize_float(forecast_gen.get(str(abs_h), 0.0)))
                cons = float(normalize_float((avg_cons if abs_h < 24 else tomorrow_cons).get(str(h_rel), 0.4)))
                
                h_min_soc = (min_soc + soc_buff) if (21 <= h_rel or h_rel <= 6) else min_soc

                for si in range(energy_steps + 1):
                    for bi in range(BOILER_STEPS + 1):
                        cur_rev, _, _, _, _, _ = dp[h][si][bi]
                        if cur_rev <= -INF + 100: continue
                        
                        cur_kwh = si * ENERGY_STEP
                        cur_boi_kwh = (bi / float(BOILER_STEPS)) * b_capacity if b_enabled else 0.0
                        
                        # Possible actions
                        for b_on in ([True, False] if b_enabled else [False]):
                            b_use = b_power if b_on else 0.0
                            # Boiler state transition (heat + cooling/usage 0.5kWh/h)
                            next_boi_kwh = max(0.0, min(b_capacity, cur_boi_kwh - 0.5 + b_use))
                            nbi = int(round(next_boi_kwh / b_capacity * BOILER_STEPS)) if b_enabled else 0
                            
                            # Action: SOL (Battery Idle)
                            pv_surplus = max(0.0, gen - cons - b_use)
                            pv_deficit = max(0.0, cons + b_use - gen)
                            rev = pv_surplus * p_sell - pv_deficit * p_buy
                            
                            # Penalize SOC below limit
                            if (cur_kwh / b_cap * 100) < h_min_soc: rev -= 10.0 
                            
                            new_rev = cur_rev + rev
                            if new_rev > dp[h+1][si][nbi][0]:
                                dp[h+1][si][nbi] = (new_rev, si, bi, ACT_SOL, 0.0, b_on)
                            
                            # Action: DISCHARGE (to home/grid)
                            if si > 0:
                                max_dis = min(max_p, cur_kwh)
                                min_dis_limit = float(self.manager.get_setting(CONF_MIN_SELL_POWER, 0.5))
                                # Optimized: skip steps, only check min and max and a few points in between
                                for steps in [int(min_dis_limit / ENERGY_STEP), int(max_dis / ENERGY_STEP)]:
                                    if steps < 1: continue
                                    dis_kwh = steps * ENERGY_STEP
                                    nsi = si - steps
                                    if nsi < 0: continue
                                    p_ac = dis_kwh * eff
                                    to_grid = max(0.0, p_ac + gen - cons - b_use)
                                    from_grid = max(0.0, cons + b_use - p_ac - gen)
                                    rev = to_grid * p_sell - from_grid * p_buy - (dis_kwh * deg_cost)
                                    if (nsi * ENERGY_STEP / b_cap * 100) < h_min_soc: rev -= 10.0
                                    new_rev = cur_rev + rev
                                    if new_rev > dp[h+1][nsi][nbi][0]:
                                        dp[h+1][nsi][nbi] = (new_rev, si, bi, ACT_DIS, dis_kwh, b_on)

                            # Action: SELF_CONSUME (battery to home only, no grid export)
                            if si > 0 and (cons + b_use - gen) > 0.01:
                                max_sc = min(max_p, cur_kwh, cons + b_use - gen)
                                # Optimized: just use max possible self-consume
                                steps = int(max_sc / ENERGY_STEP)
                                if steps >= 1:
                                    sc_kwh = steps * ENERGY_STEP
                                    nsi = si - steps
                                    p_ac = sc_kwh * eff
                                    remaining_deficit = max(0.0, cons + b_use - gen - p_ac)
                                    rev = -remaining_deficit * p_buy
                                    if (nsi * ENERGY_STEP / b_cap * 100) < h_min_soc: rev -= 10.0
                                    new_rev = cur_rev + rev
                                    if new_rev > dp[h+1][nsi][nbi][0]:
                                        dp[h+1][nsi][nbi] = (new_rev, si, bi, ACT_SELF_CONSUME, sc_kwh, b_on)

                            # Action: PV_CHARGE (from surplus)
                            if pv_surplus > 0.01 and si < energy_steps:
                                max_chg = min(max_p, energy_steps * ENERGY_STEP - cur_kwh, pv_surplus / eff)
                                # Optimized: just use max possible PV charge
                                steps = int(max_chg / ENERGY_STEP)
                                if steps >= 1:
                                    chg_kwh = steps * ENERGY_STEP
                                    nsi = si + steps
                                    used_pv = chg_kwh / eff
                                    rev = max(0.0, pv_surplus - used_pv) * p_sell - pv_deficit * p_buy
                                    rev += 0.005 * chg_kwh
                                    new_rev = cur_rev + rev
                                    if new_rev > dp[h+1][nsi][nbi][0]:
                                        dp[h+1][nsi][nbi] = (new_rev, si, bi, ACT_PV_CHARGE, chg_kwh, b_on)
                                        
                            # Additional Boiler logic bonus
                            if b_on and nbi > bi:
                                dp[h+1][nsi][nbi] = (dp[h+1][nsi][nbi][0] + 0.01, *dp[h+1][nsi][nbi][1:])

                            # Action: GRID_CHARGE (buy from grid)
                            if si < energy_steps:
                                max_gc = min(max_p, energy_steps * ENERGY_STEP - cur_kwh)
                                # Optimized: check max grid charge
                                steps = int(max_gc / ENERGY_STEP)
                                if steps >= 1:
                                    chg_kwh = steps * ENERGY_STEP
                                    nsi = si + steps
                                    grid_buy = chg_kwh / eff
                                    rev = pv_surplus * p_sell - (grid_buy + pv_deficit) * p_buy - (chg_kwh * deg_cost)
                                    new_rev = cur_rev + rev
                                    if new_rev > dp[h+1][nsi][nbi][0]:
                                        dp[h+1][nsi][nbi] = (new_rev, si, bi, ACT_GRID_CHARGE, chg_kwh, b_on)

            # --- Find Best End State ---
            best_val = -INF
            best_state = (curr_si, curr_bi)
            avg_p_buy = sum(prices_buy.values()) / len(prices_buy) if prices_buy else 0.5
            
            for si in range(energy_steps + 1):
                for bi in range(BOILER_STEPS + 1):
                    val, _, _, _, _, _ = dp[horizon][si][bi]
                    # Terminal value: remaining energy value
                    # Battery energy is valued at ~sell price
                    val += (si * ENERGY_STEP) * 0.4 
                    # v11.8.511: Boiler energy is valued at ~buy price (saved cost)
                    val += (bi / float(BOILER_STEPS) * b_capacity) * avg_p_buy * 0.8
                    
                    if val > best_val:
                        best_val = val
                        best_state = (si, bi)

            # --- Backtrack ---
            plan = {}
            formatted_plan = {}
            curr_h_state = best_state
            
            results = []
            for h in range(horizon, 0, -1):
                si, bi = curr_h_state
                entry = dp[h][si][bi]
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
                
                mode = "Idle"
                b_use = b_power if b_on else 0.0
                p_ac = 0.0
                
                if act == ACT_SOL: mode = "sale_pv_no_bat" if gen > 0.1 else "grid_only"
                elif act == ACT_DIS: 
                    mode = "sale_bat"
                    p_ac = amt * eff
                elif act == ACT_SELF_CONSUME:
                    mode = "bat_to_house"
                    p_ac = amt * eff
                elif act == ACT_PV_CHARGE: 
                    mode = "sale_pv"
                    p_ac = -amt # DC charge
                elif act == ACT_GRID_CHARGE: 
                    mode = "buy"
                    p_ac = -amt # DC charge
                
                g_net = cons + b_use - gen - p_ac
                profit = round((-g_net * p_sell if g_net < 0 else -g_net * p_buy) - (abs(amt) * deg_cost if act in [ACT_DIS, ACT_GRID_CHARGE] else 0), 2)
                
                soc = int(round((si * ENERGY_STEP) / b_cap * 100.0))
                b_temp = 10 + (bi * 5)
                b_indicator = f" | B: {'ON' if b_on else 'OFF'} ({b_temp}°C)"
                
                plan[h_key] = {"mode": mode, "power_kw": round(amt, 2), "target_soc": soc}
                formatted_plan[h_key] = (
                    f"{mode} | {round(amt, 2)}kW{b_indicator} | SOC: {soc}% | Pr: {round(p_buy, 2)}/{round(p_sell, 2)} | G: {round(g_net, 2)} | Prf: {profit}"
                )

            return {
                "plan": plan, 
                "formatted_plan": formatted_plan,
                "debug": {
                    "calc_time": round(time.time()-t0, 2), 
                    "horizon": horizon,
                    "best_val": round(best_val, 2),
                    "seen_temp": round(temp, 1) if b_enabled else "N/A"
                }
            }
            
        except Exception as e:
            _LOGGER.error(f"DP Advice Error: {e}", exc_info=True)
            return {"error": str(e)}

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
