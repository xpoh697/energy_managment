import logging
import time
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
ENERGY_STEP = 0.1          # 0.1 kWh precision (same as dp_engine.py)
BOILER_STEPS = 5          
INF = 1e9                 

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
            
            # --- Future Peak Analysis ---
            max_future_buy = {}
            current_max = 0.0
            for h in range(horizon, -1, -1):
                abs_h = cur_hour + h
                p_b = float(normalize_float(prices_buy.get(str(abs_h), 0.5)))
                current_max = max(current_max, p_b)
                max_future_buy[h] = current_max

            # --- Params ---
            max_p = float(self.manager.get_setting(CONF_BATTERY_MAX_POWER, 5.0))
            curr_s_raw, b_cap_raw, _ = self.manager.get_battery_state()
            b_cap = float(b_cap_raw or 17.0)
            deg_cost = self._get_deg_cost(b_cap)
            min_soc = float(self.manager.get_setting(CONF_MIN_SOC_BAT, 10.0))
            min_sell_p = float(self.manager.get_setting(CONF_MIN_SELL_POWER, 0.5))
            
            energy_steps = int(round(b_cap / ENERGY_STEP))
            
            b_enabled = bool(self.manager.get_setting(CONF_BOILER_ENABLE, False))
            b_power = float(self.manager.get_setting(CONF_BOILER_POWER, 2.5)) if b_enabled else 0.0
            b_capacity = float(self.manager.get_setting(CONF_BOILER_CAPACITY, 8.5))
            
            forecast_gen = self._get_smart_gen_forecast(horizon)
            avg_cons = self._ensure_dict(self.manager.get_average_profile("consumption_base", 7, now.weekday()))
            tomorrow_cons = self._ensure_dict(self.manager.get_average_profile("consumption_base", 7, (now.weekday() + 1) % 7))
            
            dp_table = [{} for _ in range(horizon + 1)]

            # Terminal Value
            terminal_peak = max_future_buy[horizon]
            for si in range(energy_steps + 1):
                energy_kwh = si * ENERGY_STEP
                for bi in range((BOILER_STEPS + 1) if b_enabled else 1):
                    dp_table[horizon][(si, bi)] = (-energy_kwh * terminal_peak, 0, 0)

            # v11.8.504: Use dynamic efficiency from manager
            eff_coeff = getattr(self.manager, "last_eff_coeff", 0.96)
            
            # Forward Induction with proper efficiency-aware delta
            for h in range(horizon - 1, -1, -1):
                # ... (rest of h-loop setup)
                abs_h = cur_hour + h
                h_rel = abs_h % 24
                p_buy = float(normalize_float(prices_buy.get(str(abs_h), 0.5)))
                p_sell = float(normalize_float(prices_sell.get(str(abs_h), 0.4)))
                gen = float(normalize_float(forecast_gen.get(str(abs_h), 0.0)))
                cons = float(normalize_float((avg_cons if abs_h < 24 else tomorrow_cons).get(str(h_rel), 0.4)))
                
                h_min_soc = (min_soc + 10.0) if (21 <= h_rel or h_rel <= 6) else min_soc
                future_peak = max_future_buy[h+1]

                for si in range(energy_steps + 1):
                    cur_kwh = si * ENERGY_STEP
                    for bi in range((BOILER_STEPS + 1) if b_enabled else 1):
                        cur_boi_kwh = (bi / float(BOILER_STEPS)) * b_capacity if b_enabled else 0.0
                        best_val = INF
                        best_next = (si, bi)
                        
                        # v11.8.505: Battery Discharge Limit (DC) is max_p.
                        max_delta = max_p * 1.05 # 5% safety margin for stepping
                        si_min = max(0, int((cur_kwh - max_delta) / ENERGY_STEP))
                        si_max = min(energy_steps, int((cur_kwh + max_delta) / ENERGY_STEP))
                        
                        for next_si in range(si_min, si_max + 1):
                            next_kwh = next_si * ENERGY_STEP
                            delta_bat = cur_kwh - next_kwh
                            p_ac = delta_bat * eff_coeff if delta_bat >= 0 else delta_bat / eff_coeff
                            
                            for b_on in ([True, False] if b_enabled else [False]):
                                b_use = b_power if b_on else 0.0
                                next_boi_kwh = max(0.0, min(b_capacity, cur_boi_kwh - 0.1 + b_use))
                                next_bi = int(round(next_boi_kwh / b_capacity * BOILER_STEPS)) if b_enabled else 0
                                
                                grid = cons + b_use - gen - p_ac
                                if grid > 0:
                                    cost = grid * p_buy
                                else:
                                    if p_ac < -0.05:
                                        solar_avail = max(0, gen - cons - b_use)
                                        p_charge = abs(p_ac)
                                        if solar_avail >= p_charge:
                                            cost = (grid + p_charge) * p_sell - (p_charge * 2.0)
                                        else:
                                            cost = (p_charge - solar_avail) * p_buy
                                    else:
                                        cost = grid * p_sell
                                
                                # Sell Safety & Opportunity Cost
                                if p_ac > 0.05:
                                    current_val = p_buy if grid > -0.01 else p_sell
                                    if current_val < (future_peak * 0.95):
                                        cost += p_ac * (future_peak - current_val + 1.0)
                                    if grid < -0.01 and abs(grid) < min_sell_p:
                                        cost += 5.0

                                # Micro-movement penalty
                                if 0.01 < abs(p_ac) < 0.8:
                                    cost += 1.0
                                
                                cost += abs(delta_bat) * (deg_cost + 0.02)
                                if (next_kwh / b_cap * 100) < h_min_soc: cost += 2000.0
                                
                                total = cost + dp_table[h+1].get((next_si, next_bi), (INF, 0, 0))[0]
                                if total < best_val:
                                    best_val = total
                                    best_next = (next_si, next_bi)
                        
                        dp_table[h][(si, bi)] = (best_val, best_next[0], best_next[1])

            # Forward Path
            curr_si = int(round((curr_s_raw or 0.0) / 100.0 * b_cap / ENERGY_STEP))
            if b_enabled:
                temp_s = self.manager.get_setting(CONF_BOILER_TEMP_SENSOR)
                temp = float(self.manager.get_sensor_float(temp_s) or 20.0) if temp_s else 30.0
                curr_bi = int(round(max(0, min(50, temp-10))/50.0 * BOILER_STEPS))
            else: curr_bi = 0

            plan = {}
            formatted_plan = {}
            for h in range(horizon):
                abs_h = cur_hour + h
                h_rel = abs_h % 24
                entry = dp_table[h].get((curr_si, curr_bi))
                if not entry: break
                _, next_si, next_bi = entry
                
                cur_kwh = curr_si * ENERGY_STEP
                next_kwh = next_si * ENERGY_STEP
                delta_kwh = cur_kwh - next_kwh
                p_ac = delta_kwh * eff_coeff if delta_kwh >= 0 else delta_kwh / eff_coeff
                b_on = (next_bi > curr_bi) if b_enabled else False
                
                p_buy = float(normalize_float(prices_buy.get(str(abs_h), 0.5)))
                p_sell = float(normalize_float(prices_sell.get(str(abs_h), 0.4)))
                gen = float(normalize_float(forecast_gen.get(str(abs_h), 0.0)))
                cons = float(normalize_float((avg_cons if abs_h < 24 else tomorrow_cons).get(str(h_rel), 0.4)))
                g_net = cons + (b_power if b_on else 0.0) - gen - p_ac
                
                s_soc = (curr_si * ENERGY_STEP) / b_cap * 100.0
                mode = self._map_mode(delta_kwh, g_net, gen, p_sell, s_soc, min_soc)
                
                # v11.8.505: Show Battery Power (DC) in UI as requested
                p_bat_kw = round(abs(delta_kwh), 2)
                t_soc = int(round((next_si * ENERGY_STEP) / b_cap * 100.0))
                profit = round((-g_net * p_sell if g_net < 0 else -g_net * p_buy) - (abs(delta_kwh) * deg_cost), 2)
                b_str = " | B: ON" if b_on else ""
                
                h_key = f"{h_rel:02d}:00" + (" (Завтра)" if abs_h >= 24 else "")
                
                plan[h_key] = {
                    "mode": mode, "power_kw": p_bat_kw, "boiler": "ON" if b_on else "OFF",
                    "target_soc": t_soc, "grid_net": round(g_net, 2), "profit": profit
                }
                
                formatted_plan[h_key] = (
                    f"{mode} | {p_bat_kw}kW{b_str} | SOC: {t_soc}% | "
                    f"Pr: {round(p_buy, 2)}/{round(p_sell, 2)} | G: {round(g_net, 2)} | Prf: {profit}"
                )
                curr_si, curr_bi = next_si, next_bi

            start_entry = dp_table[0].get((int(round((curr_s_raw or 0.0) / 100.0 * b_cap / ENERGY_STEP)), curr_bi))
            total_val = -start_entry[0] if start_entry else 0.0

            return {
                "plan": plan, 
                "formatted_plan": formatted_plan,
                "debug": {
                    "calc_time": round(time.time()-t0, 2), 
                    "horizon": horizon, 
                    "future_peak": round(max_future_buy[0], 3),
                    "total_path_value": round(total_val, 2),
                    "avg_buy": round(sum(prices_buy.values())/len(prices_buy), 3) if prices_buy else 0,
                    "energy_steps": energy_steps
                }
            }
        except Exception as e:
            _LOGGER.error(f"DP Advice Error: {e}", exc_info=True)
            return {"error": str(e)}

    def _map_mode(self, d_kwh, g_net, gen, p_sell, soc, floor) -> str:
        if soc <= floor + 0.5 and d_kwh >= -0.05: return "bat_emergency"
        if d_kwh < -0.1 and g_net > 0.1: return "buy"
        if d_kwh > 0.1:
            if g_net < -0.1: return "sale_bat"
            return "bat_to_house"
        if gen > 0.1:
            if soc >= 99: return "sale_pv"
            if d_kwh < -0.1: return "sale_pv"
            return "sale_pv_no_bat"
        if g_net > 0.1: return "grid_only"
        return "sale_pv_no_bat"

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
