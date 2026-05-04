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

BATTERY_POWER_STEP = 0.1  # kW (Requested by user)
BATTERY_SOC_STEP = 1.0    # % (Fine-grained for high precision)
BOILER_SOC_STEP = 20.0    # %

HORIZON_HOURS = 24
INVERTER_EFFICIENCY = 0.92

class DPPlanner:
    """
    Advanced DP Planner with 0.1kW resolution and Boiler integration.
    Standard Python implementation for maximum compatibility.
    """
    
    def __init__(self, manager):
        self.manager = manager
        
    def get_dp_advice(self) -> Dict[str, Any]:
        try:
            now = datetime.now()
            cur_hour = now.hour
            
            # 1. Gather environmental data
            prices_buy = self._get_prices("prices_buy")
            prices_sell = self._get_prices("prices_sell")
            forecast_gen = self.manager.get_average_profile("generation", 7, now.weekday())
            avg_cons = self.manager.get_average_profile("consumption_base", 7, now.weekday())
            
            if not prices_buy or not prices_sell:
                return {"error": "Missing price data"}

            # 2. Battery & Inverter Specs
            max_p = float(self.manager.get_setting(CONF_BATTERY_MAX_POWER, 5.0))
            _, b_cap, _ = self.manager.get_battery_state()
            deg_cost = self._get_deg_cost(b_cap)
            min_soc = float(self.manager.get_setting(CONF_MIN_SOC_BAT, 10.0))
            
            # 3. Discretization
            # steps_soc = [0.0, 1.0, 2.0 ... 100.0]
            steps_soc = [round(i * BATTERY_SOC_STEP, 1) for i in range(int(100/BATTERY_SOC_STEP) + 1)]
            steps_boiler = [round(i * BOILER_SOC_STEP, 1) for i in range(int(100/BOILER_SOC_STEP) + 1)]
            # actions_bat = [-5.0, -4.9 ... 5.0]
            actions_bat = [round(-max_p + i * BATTERY_POWER_STEP, 1) for i in range(int(2 * max_p / BATTERY_POWER_STEP) + 1)]
            
            # 4. Initialization
            dp_table = {} 

            # Boundary Condition (Final hour)
            dp_table[HORIZON_HOURS] = {}
            for s in steps_soc:
                for b in steps_boiler:
                    penalty = 0.0
                    if s < min_soc: penalty += 2000.0
                    if b < 50: penalty += 1000.0
                    dp_table[HORIZON_HOURS][(s, b)] = (penalty, None)

            # 5. Backward Pass
            for h in range(HORIZON_HOURS - 1, -1, -1):
                dp_table[h] = {}
                abs_h = (cur_hour + h) % 48
                h_rel = abs_h % 24
                
                p_buy = float(normalize_float(prices_buy.get(str(h_rel), 0.0)))
                p_sell = float(normalize_float(prices_sell.get(str(h_rel), 0.0)))
                gen = float(normalize_float(forecast_gen.get(str(h_rel), 0.0)))
                cons = float(normalize_float(avg_cons.get(str(h_rel), 0.4)))
                
                is_deadline = h_rel in BOILER_DEADLINES
                
                for s in steps_soc:
                    for b in steps_boiler:
                        best_val = float('inf')
                        best_act = None
                        
                        for b_p in actions_bat:
                            # 1. Battery Transition
                            s_next_raw = s - (b_p / b_cap * 100.0)
                            if s_next_raw < 0 or s_next_raw > 100: continue
                            
                            for b_on in [True, False]:
                                # 2. Boiler Transition
                                b_cons = BOILER_POWER if b_on else 0.0
                                b_next_raw = b - (BOILER_LOSS_RATE * 100.0) + (b_cons / BOILER_CAPACITY * 100.0)
                                b_next_raw = max(0, min(100, b_next_raw))
                                
                                # 3. Instant Cost
                                p_inv = b_p * (INVERTER_EFFICIENCY if b_p > 0 else 1.0/INVERTER_EFFICIENCY)
                                grid_net = cons + b_cons - gen - p_inv
                                
                                cost = 0.0
                                if grid_net > 0: cost += grid_net * p_buy
                                else: cost += grid_net * p_sell 
                                
                                cost += abs(b_p) * deg_cost
                                
                                if b_on and b >= 98: cost += 5.0 
                                if is_deadline and b_next_raw < 80: cost += 500.0 
                                
                                # 4. Recursive Step (Nearest Neighbor)
                                s_next_idx = round(min(steps_soc, key=lambda x: abs(x - s_next_raw)), 1)
                                b_next_idx = round(min(steps_boiler, key=lambda x: abs(x - b_next_raw)), 1)
                                
                                total_v = cost + dp_table[h+1][(s_next_idx, b_next_idx)][0]
                                
                                if total_v < best_val:
                                    best_val = total_v
                                    best_act = (b_p, b_on, round(grid_net, 2))
                        
                        dp_table[h][(s, b)] = (best_val, best_act)

            # 6. Path Reconstruction
            curr_s, _, _ = self.manager.get_battery_state()
            s_ptr = round(min(steps_soc, key=lambda x: abs(x - float(curr_s or 50))), 1)
            b_ptr = 60.0 
            
            plan = {}
            for h in range(HORIZON_HOURS):
                abs_h = (cur_hour + h) % 48
                _, act = dp_table[h][(s_ptr, b_ptr)]
                if not act: break
                
                b_p, b_on, g_net = act
                mode = self._map_mode(b_p, b_on, g_net, forecast_gen.get(str(abs_h%24), 0), prices_buy.get(str(abs_h%24), 10))
                
                h_key = f"{abs_h%24:02d}:00" + (" (Завтра)" if abs_h >= 24 else "")
                plan[h_key] = {
                    "mode": mode,
                    "power_kw": b_p,
                    "boiler": "ON" if b_on else "OFF",
                    "target_soc": s_ptr,
                    "grid_net": g_net
                }
                
                # Update pointers
                s_next_exact = s_ptr - (b_p / b_cap * 100.0)
                s_ptr = round(min(steps_soc, key=lambda x: abs(x - s_next_exact)), 1)
                
                b_cons = BOILER_POWER if b_on else 0.0
                b_next_exact = b_ptr - (BOILER_LOSS_RATE * 100.0) + (b_cons / BOILER_CAPACITY * 100.0)
                b_ptr = round(min(steps_boiler, key=lambda x: abs(x - b_next_exact)), 1)

            return plan

        except Exception as e:
            _LOGGER.error(f"DP Advice Error: {e}", exc_info=True)
            return {"error": str(e)}

    def _map_mode(self, b_p, b_on, g_net, gen, price) -> str:
        if b_p < -0.1 and g_net > 0.1: return "buy"
        if b_p > 0.1 and g_net < -0.1: return "sale_pv_bat"
        if abs(b_p) < 0.1 and g_net < -0.1 and float(normalize_float(gen)) > 0.1: return "sale_pv_no_bat"
        if g_net >= -0.05 and b_p < -0.1 and float(normalize_float(gen)) > 0.1: return "stop_sale"
        if abs(b_p) < 0.1 and float(normalize_float(price)) < 0: return "no_pv_sale_no_bat"
        return "sale_pv"

    def _get_prices(self, key: str) -> Dict[str, Any]:
        prices = self.manager.data.get(key, {})
        today_str = datetime.now().strftime("%Y-%m-%d")
        tomorrow_str = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        combined = dict(prices.get(today_str, {}))
        for h, p in prices.get(tomorrow_str, {}).items():
            combined[str(h)] = p 
        return combined

    def _get_deg_cost(self, cap: float) -> float:
        batt_cost = self.manager.get_setting(CONF_BATTERY_COST, 0.0)
        cycles = self.manager.get_setting(CONF_BATTERY_RATED_CYCLES, 6000)
        if cap <= 1.0 or cycles <= 0 or batt_cost <= 0: return 0.04
        return round_f(batt_cost / (cycles * cap), 4)
