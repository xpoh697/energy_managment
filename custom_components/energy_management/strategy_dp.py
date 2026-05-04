import logging
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

# --- DP CONFIGURATION CONSTANTS ---
BOILER_POWER = 2.5        # kW
BOILER_CAPACITY = 6.0     # kWh
BOILER_LOSS_RATE = 0.02   # 2% energy loss per hour
BOILER_DEADLINES = [7, 8, 19, 20, 21] # Target hours for 100% heat

BATTERY_POWER_STEP = 0.1  # kW resolution
BATTERY_SOC_STEP = 1.0    # % resolution
BOILER_SOC_STEP = 20.0    # % resolution

INVERTER_EFFICIENCY = 0.92

class DPPlanner:
    """
    Advanced DP Planner with armored data parsing and zero-division protection.
    Fixes: division by zero (Battery Capacity).
    """
    
    def __init__(self, manager):
        self.manager = manager
        
    def get_dp_advice(self) -> Dict[str, Any]:
        try:
            now = datetime.now()
            cur_hour = now.hour
            
            # 1. Fetch Price Data
            prices_buy = self._get_prices("prices_buy")
            prices_sell = self._get_prices("prices_sell")
            
            if not prices_buy or not prices_sell:
                return {"error": "Missing price data"}

            # Determine Horizon
            available_hours = sorted([int(h) for h in prices_buy.keys()])
            if not available_hours: return {"error": "No price indices found"}
            
            max_abs_h = max(available_hours)
            horizon = max_abs_h - cur_hour + 1
            if horizon <= 0: horizon = 24 
            
            # 2. Battery & Inverter Specs (Safety First)
            max_p = float(self.manager.get_setting(CONF_BATTERY_MAX_POWER, 5.0))
            _, b_cap_raw, _ = self.manager.get_battery_state()
            b_cap = float(b_cap_raw or 10.0)
            if b_cap <= 0.1: b_cap = 10.0 # Emergency fallback to avoid division by zero
            
            deg_cost = self._get_deg_cost(b_cap)
            min_soc = float(self.manager.get_setting(CONF_MIN_SOC_BAT, 10.0))
            
            # 3. Gather Smart Forecasts
            blended_coeff = getattr(self.manager, "last_blended_coeff", 1.0)
            forecast_gen = self._get_smart_gen_forecast(blended_coeff, horizon)
            
            avg_cons = self._ensure_dict(self.manager.get_average_profile("consumption_base", 7, now.weekday()))
            tomorrow_cons = self._ensure_dict(self.manager.get_average_profile("consumption_base", 7, (now.weekday() + 1) % 7))

            # 4. Discretization
            steps_soc = [round(i * BATTERY_SOC_STEP, 1) for i in range(int(100/BATTERY_SOC_STEP) + 1)]
            steps_boiler = [round(i * BOILER_SOC_STEP, 1) for i in range(int(100/BOILER_SOC_STEP) + 1)]
            actions_bat = [round(-max_p + i * BATTERY_POWER_STEP, 1) for i in range(int(2 * max_p / BATTERY_POWER_STEP) + 1)]
            
            dp_table = {} 

            # 5. Final State
            dp_table[horizon] = {}
            for s in steps_soc:
                for b in steps_boiler:
                    penalty = 0.0
                    if s < min_soc: penalty += 5000.0
                    if b < 50: penalty += 1000.0
                    dp_table[horizon][(s, b)] = (penalty, None)

            # 6. Backward Pass
            for h in range(horizon - 1, -1, -1):
                dp_table[h] = {}
                abs_h = cur_hour + h
                h_rel = abs_h % 24
                
                p_buy = float(normalize_float(prices_buy.get(str(abs_h), 0.0)))
                p_sell = float(normalize_float(prices_sell.get(str(abs_h), 0.0)))
                gen = float(normalize_float(forecast_gen.get(str(abs_h), 0.0)))
                
                active_cons = avg_cons if abs_h < 24 else tomorrow_cons
                cons = float(normalize_float(active_cons.get(str(h_rel), 0.4)))
                
                is_deadline = h_rel in BOILER_DEADLINES
                
                for s in steps_soc:
                    for b in steps_boiler:
                        best_val = float('inf')
                        best_act = None
                        
                        for b_p in actions_bat:
                            # Battery Transition (b_cap guaranteed > 0 here)
                            s_next_raw = s - (b_p / b_cap * 100.0)
                            if s_next_raw < -0.01 or s_next_raw > 100.01: continue
                            s_next_raw = max(0.0, min(100.0, s_next_raw))
                            
                            for b_on in [True, False]:
                                b_cons = BOILER_POWER if b_on else 0.0
                                b_next_raw = b - (BOILER_LOSS_RATE * 100.0) + (b_cons / BOILER_CAPACITY * 100.0)
                                b_next_raw = max(0, min(100, b_next_raw))
                                
                                p_inv = b_p * (INVERTER_EFFICIENCY if b_p > 0 else 1.0/INVERTER_EFFICIENCY)
                                grid_net = cons + b_cons - gen - p_inv
                                
                                cost = 0.0
                                if grid_net > 0: cost += grid_net * p_buy
                                else: cost += grid_net * p_sell 
                                
                                cost += abs(b_p) * deg_cost
                                if b_on and b >= 98: cost += 5.0 
                                if is_deadline and b_next_raw < 80: cost += 1000.0
                                
                                s_next_idx = round(min(steps_soc, key=lambda x: abs(x - s_next_raw)), 1)
                                b_next_idx = round(min(steps_boiler, key=lambda x: abs(x - b_next_raw)), 1)
                                
                                total_v = cost + dp_table[h+1][(s_next_idx, b_next_idx)][0]
                                
                                if total_v < best_val:
                                    best_val = total_v
                                    best_act = (b_p, b_on, round(grid_net, 2))
                        
                        dp_table[h][(s, b)] = (best_val, best_act)

            # 7. Reconstruct Path
            curr_s_raw, _, _ = self.manager.get_battery_state()
            s_ptr = round(min(steps_soc, key=lambda x: abs(x - float(curr_s_raw or 50))), 1)
            b_ptr = 60.0 
            
            plan = {}
            for h in range(horizon):
                abs_h = cur_hour + h
                _, act = dp_table[h][(s_ptr, b_ptr)]
                if not act: break
                
                b_p, b_on, g_net = act
                h_rel = abs_h % 24
                cur_p_buy = float(normalize_float(prices_buy.get(str(abs_h), prices_buy.get(str(h_rel), 10.0))))
                
                mode = self._map_mode(b_p, b_on, g_net, forecast_gen.get(str(abs_h), 0), cur_p_buy)
                
                h_key = f"{h_rel:02d}:00" + (" (Завтра)" if abs_h >= 24 else "")
                plan[h_key] = {
                    "mode": mode,
                    "power_kw": b_p,
                    "boiler": "ON" if b_on else "OFF",
                    "target_soc": s_ptr,
                    "grid_net": g_net
                }
                
                s_next_exact = s_ptr - (b_p / b_cap * 100.0)
                s_ptr = round(min(steps_soc, key=lambda x: abs(x - s_next_exact)), 1)
                
                b_cons = BOILER_POWER if b_on else 0.0
                b_next_exact = b_ptr - (BOILER_LOSS_RATE * 100.0) + (b_cons / BOILER_CAPACITY * 100.0)
                b_ptr = round(min(steps_boiler, key=lambda x: abs(x - b_next_exact)), 1)

            return plan

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

    def _map_mode(self, b_p, b_on, g_net, gen, price) -> str:
        if b_p < -0.1 and g_net > 0.1: return "buy"
        if b_p > 0.1 and g_net < -0.1: return "sale_pv_bat"
        if abs(b_p) < 0.1 and g_net < -0.1 and float(normalize_float(gen)) > 0.1: return "sale_pv_no_bat"
        if g_net >= -0.05 and b_p < -0.1 and float(normalize_float(gen)) > 0.1: return "stop_sale"
        if abs(b_p) < 0.1 and float(normalize_float(price)) < 0: return "no_pv_sale_no_bat"
        return "sale_pv"

    def _get_deg_cost(self, cap: float) -> float:
        batt_cost = float(self.manager.get_setting(CONF_BATTERY_COST, 0.0))
        cycles = float(self.manager.get_setting(CONF_BATTERY_RATED_CYCLES, 6000))
        if cap <= 0.1 or cycles <= 0 or batt_cost <= 0: return 0.04
        return round_f(batt_cost / (cycles * cap), 4)
