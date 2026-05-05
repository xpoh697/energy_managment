import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List, Any, Tuple

from .const import (
    CONF_BATTERY_MAX_POWER,
    CONF_BATTERY_COST,
    CONF_BATTERY_RATED_CYCLES,
    CONF_MIN_SOC_BAT,
    CONF_SOC_BUFFER
)
from .utils import normalize_float, round_f

_LOGGER = logging.getLogger(__name__)

# --- DP CONFIGURATION (SAFE GRID) ---
BOILER_POWER = 2.5        # kW
BOILER_CAPACITY = 6.0     # kWh
BOILER_LOSS_RATE = 0.02   # 2% energy loss per hour
BOILER_DEADLINES = [7, 8, 19, 20, 21] 

# Increased steps for performance (from 1% to 5%/10%)
BATTERY_POWER_STEP = 0.5  # kW (Faster than 0.1)
BATTERY_SOC_STEP = 5.0    # % (Faster than 1.0)
BOILER_SOC_STEP = 10.0    # % (Faster than 1.0)

INVERTER_EFFICIENCY = 0.92

class DPPlanner:
    """
    Performance-optimized DP Planner.
    Reduces state space to prevent CPU exhaustion.
    """
    
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
            
            max_p = float(self.manager.get_setting(CONF_BATTERY_MAX_POWER, 5.0))
            curr_s_raw, b_cap_raw, _ = self.manager.get_battery_state()
            b_cap = float(b_cap_raw or 10.0)
            deg_cost = self._get_deg_cost(b_cap)
            min_soc = float(self.manager.get_setting(CONF_MIN_SOC_BAT, 10.0))
            
            blended_coeff = getattr(self.manager, "last_blended_coeff", 1.0)
            forecast_gen = self._get_smart_gen_forecast(blended_coeff, horizon)
            avg_cons = self._ensure_dict(self.manager.get_average_profile("consumption_base", 7, now.weekday()))
            tomorrow_cons = self._ensure_dict(self.manager.get_average_profile("consumption_base", 7, (now.weekday() + 1) % 7))

            # Grids
            steps_soc = [float(x) for x in range(0, 101, int(BATTERY_SOC_STEP))]
            steps_boiler = [float(x) for x in range(0, 101, int(BOILER_SOC_STEP))]
            actions_bat = [round(-max_p + i * BATTERY_POWER_STEP, 1) for i in range(int(2 * max_p / BATTERY_POWER_STEP) + 1)]
            
            avg_p_buy = sum(prices_buy.values()) / len(prices_buy) if prices_buy else 0.5
            dp_table = {} 

            # Final state
            dp_table[horizon] = {}
            for s in steps_soc:
                for b in steps_boiler:
                    val = 0.0
                    if s < min_soc: val += 10000.0
                    val -= s * (b_cap / 100.0) * avg_p_buy * 0.4 # Reward for SOC
                    if b < 40: val += 1000.0
                    dp_table[horizon][(s, b)] = (val, None)

            # v11.7.300: Solar Surplus Awareness for DP
            f_today = float(self.manager.get_sensor_float(self.manager.forecast_today_sensor) or 0.0)
            energy_to_full = (100.0 - (curr_s_raw or 0.0)) * b_cap / 100.0
            is_solar_surplus = (f_today > energy_to_full + 5.0)
            
            # Backward induction
            for h in range(horizon - 1, -1, -1):
                dp_table[h] = {}
                abs_h = cur_hour + h
                h_rel = abs_h % 24
                
                p_buy = float(normalize_float(prices_buy.get(str(abs_h), 0.5)))
                p_sell = float(normalize_float(prices_sell.get(str(abs_h), 0.4)))
                gen = float(normalize_float(forecast_gen.get(str(abs_h), 0.0)))
                cons = float(normalize_float((avg_cons if abs_h < 24 else tomorrow_cons).get(str(h_rel), 0.4)))
                eff_gen_cons = cons - gen
                is_deadline = h_rel in BOILER_DEADLINES
                
                # v11.7.300: Relax floors during solar surplus morning
                is_morning_surplus = (4 <= h_rel < 13) and is_solar_surplus
                h_min_soc = (min_soc + 2.0) if is_morning_surplus else min_soc
                
                for s in steps_soc:
                    for b in steps_boiler:
                        best_val = float('inf')
                        best_act = None
                        
                        for b_p in actions_bat:
                            s_next_raw = s - (b_p / b_cap * 100.0)
                            if s_next_raw < 0 or s_next_raw > 100: continue
                            s_next_idx = float(round(s_next_raw / BATTERY_SOC_STEP) * BATTERY_SOC_STEP)
                            
                            for b_on in [True, False]:
                                b_cons = BOILER_POWER if b_on else 0.0
                                b_next_raw = b - (BOILER_LOSS_RATE * 100.0) + (b_cons / BOILER_CAPACITY * 100.0)
                                b_next_raw = max(0.0, min(100.0, b_next_raw))
                                b_next_idx = float(round(b_next_raw / BOILER_SOC_STEP) * BOILER_SOC_STEP)
                                
                                p_inv = b_p * (INVERTER_EFFICIENCY if b_p > 0 else 1.0/INVERTER_EFFICIENCY)
                                grid_net = eff_gen_cons + b_cons - p_inv
                                
                                cost = (grid_net * p_buy) if grid_net > 0 else (grid_net * p_sell)
                                cost += abs(b_p) * deg_cost
                                
                                # Constraints
                                if s_next_raw < (h_min_soc - 0.5): cost += 2000.0
                                # v11.7.300: Reduce "Low SOC" panic if we have surplus coming
                                if is_morning_surplus and s_next_raw < 30: cost += (30 - s_next_raw) * 0.1
                                
                                if b_p > 0.1 and grid_net < -0.1: cost += abs(grid_net) * 0.1
                                if is_deadline and b_next_raw < 80: cost += 3000.0
                                
                                total_v = cost + dp_table[h+1][(s_next_idx, b_next_idx)][0]
                                if total_v < best_val:
                                    best_val = total_v
                                    best_act = (b_p, b_on, round(grid_net, 2))
                        
                        dp_table[h][(s, b)] = (best_val, best_act)

            # Forward path
            s_ptr = float(round((float(curr_s_raw or 50)) / BATTERY_SOC_STEP) * BATTERY_SOC_STEP)
            b_ptr = 40.0
            plan = {}
            for h in range(horizon):
                abs_h = cur_hour + h
                _, act = dp_table[h].get((s_ptr, b_ptr), (0, None))
                if not act: break
                b_p, b_on, g_net = act
                h_rel = abs_h % 24
                p_buy = float(normalize_float(prices_buy.get(str(abs_h), 0.5)))
                p_sell = float(normalize_float(prices_sell.get(str(abs_h), 0.4)))
                mode = self._map_mode(b_p, b_on, g_net, forecast_gen.get(str(abs_h), 0), p_buy, p_sell, s_ptr, min_soc)
                
                plan[f"{h_rel:02d}:00" + (" (Завтра)" if abs_h >= 24 else "")] = {
                    "mode": mode, "power_kw": b_p, "boiler": "ON" if b_on else "OFF",
                    "target_soc": int(s_ptr), "grid_net": g_net,
                    "price_buy": round(p_buy, 3), "price_sell": round(p_sell, 3)
                }
                s_next = s_ptr - (b_p / b_cap * 100.0)
                s_ptr = float(max(0, min(100, round(s_next / BATTERY_SOC_STEP) * BATTERY_SOC_STEP)))
                b_next = b_ptr - (BOILER_LOSS_RATE * 100.0) + ((BOILER_POWER if b_on else 0.0) / BOILER_CAPACITY * 100.0)
                b_ptr = float(max(0, min(100, round(b_next / BOILER_SOC_STEP) * BOILER_SOC_STEP)))

            return {
                "plan": plan, 
                "debug": {
                    "calc_time_sec": round(time.time()-t0, 2), 
                    "horizon": horizon,
                    "f_today": round(f_today, 1),
                    "energy_to_full": round(energy_to_full, 1),
                    "is_solar_surplus": is_solar_surplus,
                    "min_soc": min_soc,
                    "deg_cost": deg_cost,
                    "avg_p_buy": round(avg_p_buy, 3),
                    "blended_coeff": round(blended_coeff, 2)
                }
            }
        except Exception as e:
            _LOGGER.error(f"DP Advice Error: {e}", exc_info=True)
            return {"error": str(e)}

    def _get_smart_gen_forecast(self, blended_coeff: float, horizon: int) -> Dict[str, float]:
        now = datetime.now()
        today_s = getattr(self.manager, "forecast_today_hourly_sensor", None)
        tomorrow_s = getattr(self.manager, "forecast_tomorrow_sensor", None)
        today_dist = self._ensure_dict(self.manager.get_forecast_hourly_distribution(today_s)) if today_s else {}
        tomorrow_date = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        tomorrow_dist = self._ensure_dict(self.manager.get_forecast_hourly_distribution(tomorrow_s, tomorrow_date)) if tomorrow_s else {}
        combined_raw = {}
        for h, v in today_dist.items(): combined_raw[str(h)] = v
        for h, v in tomorrow_dist.items(): combined_raw[str(int(h) + 24)] = v
        if not combined_raw:
            prof_today = self._ensure_dict(self.manager.get_average_profile("generation", 7, now.weekday()))
            prof_tomorrow = self._ensure_dict(self.manager.get_average_profile("generation", 7, (now.weekday() + 1) % 7))
            for h, v in prof_today.items(): combined_raw[str(h)] = v
            for h, v in prof_tomorrow.items(): combined_raw[str(int(h) + 24)] = v
        corrected = {}
        for abs_h_str, val in combined_raw.items():
            try:
                if isinstance(val, str) and (val.startswith("sensor.") or "_" in val): continue
                abs_h = int(abs_h_str)
                h_acc, _ = self.manager.strategy_engine.get_hourly_accuracy_coeff(abs_h % 24)
                corrected[str(abs_h)] = float(normalize_float(val)) * h_acc * blended_coeff
            except (ValueError, TypeError): continue
        return corrected

    def _ensure_dict(self, data: Any) -> Dict[str, Any]:
        if isinstance(data, str) and (data.startswith("sensor.") or "forecast" in data): return {}
        if isinstance(data, dict): return data
        if isinstance(data, list):
            new_dict = {}
            for i, item in enumerate(data):
                if isinstance(item, dict):
                    hour = item.get("hour", item.get("h", i))
                    val = item.get("value", item.get("v", item.get("p", 0.0)))
                    new_dict[str(hour)] = val
                else: new_dict[str(i)] = item
            return new_dict
        return {}

    def _get_prices(self, key: str) -> Dict[str, Any]:
        prices_store = self.manager.data.get(key, {})
        if not isinstance(prices_store, dict): return {}
        today_str = datetime.now().strftime("%Y-%m-%d")
        tomorrow_str = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        combined = {}
        t_data = self._ensure_dict(prices_store.get(today_str, {}))
        for h, p in t_data.items(): combined[str(h)] = p
        tm_data = self._ensure_dict(prices_store.get(tomorrow_str, {}))
        for h, p in tm_data.items(): combined[str(int(h) + 24)] = p
        return combined

    def _map_mode(self, b_p, b_on, g_net, gen, p_buy, p_sell, soc, floor) -> str:
        if soc <= floor + 0.2 and b_p >= -0.1: return "bat_emergency"
        if b_p < -0.1: return "buy" if g_net > 0.1 else "charge_pv"
        if b_p > 0.1: return "sale_pv_bat"
        if abs(b_p) <= 0.1:
            if float(normalize_float(p_sell)) < 0: return "no_pv_sale_no_bat"
            if g_net < -0.1 and float(normalize_float(gen)) > 0.1: return "sale_pv_no_bat"
        return "sale_pv"

    def _get_deg_cost(self, cap: float) -> float:
        batt_cost = float(self.manager.get_setting(CONF_BATTERY_COST, 0.0))
        cycles = float(self.manager.get_setting(CONF_BATTERY_RATED_CYCLES, 6000))
        if cap <= 0.1 or cycles <= 0 or batt_cost <= 0: return 0.04
        return round_f(batt_cost / (cycles * cap), 4)
