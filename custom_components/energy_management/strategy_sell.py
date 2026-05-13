import logging
_LOGGER = logging.getLogger(__name__)
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Tuple, Optional
from homeassistant.util import dt as dt_util

if TYPE_CHECKING:
    from .sensor import EnergyProfileManager

from .const import (
    CONF_BATTERY_COST, 
    CONF_BATTERY_RATED_CYCLES,
    CONF_MIN_SOC_BAT,
    CONF_ACTIVE_SENSOR,
    CONF_IS_CYCLIC,
    CONF_ONLY_SOLAR,
    CONF_PRICE_BUY_LIMIT,
    CONF_PRICE_SELL_LIMIT,
    CONF_PRICE_SELL_ONLY_PV,
    CONF_BATTERY_MAX_POWER,
    CONF_AI_CHARGE_LIMIT,
    CONF_AI_DISCHARGE_LIMIT,
    CONF_EMERGENCY_SOC_LIMIT,
    CONF_ARBITRAGE_PROFIT_THRESHOLD,
    CONF_DYNAMIC_SOC_BUY,
    CONF_DYNAMIC_SOC_SELL,
    CONF_FORCE_MARKET_SELL,
    CONF_PRIORITY,
    CONF_SOC_BUFFER,
    CONF_BATTERY_DISCHARGE_ENABLED,
    CONF_SALE_PV_NO_BAT_MAX_HOUR,
    DOMAIN,
    VERSION
)
from .utils import get_kwh_val, normalize_float, get_price_from_store, round_f
from .strategy_base import StrategyEngine

class StrategySell(StrategyEngine):
    """Specialized engine for SELL-mode energy management strategies."""

    def get_strategy_epochs(self, target_hours, prices_today, prices_tomorrow):
        """Groups target hours into contiguous windows (epochs)."""
        if not target_hours: return []
        return self._group_contiguous(target_hours)
    
    def get_market_strategy(self, mode="sell"):
        now = dt_util.now()
        man: Any = self.manager
        
        cache_key = f"market_strategy_{mode}"
        cached = self._strategy_cache.get(cache_key)
        if cached and (now - cached["time"]).total_seconds() < 30 and cached["time"].hour == now.hour:
            return cached["res"]

        _b_soc_s, _b_cap_s, _ = man.get_battery_state()
        b_cap = float(_b_cap_s or 10.0)
        b_soc = float(_b_soc_s or 50.0)
        max_p = float(man.get_setting(CONF_BATTERY_MAX_POWER, 3.0))
        deg_cost = float(self.get_battery_degradation_cost())
        prof_thresh = float(man.get_setting(CONF_ARBITRAGE_PROFIT_THRESHOLD, 0.5))
        target_price = 0.0
        limit_reason = "None"
        next_peak_h = -1
        soc_at_peak = 0.0
        sim_log = {}
        target_hours = []
        sell_commands = {}
        current_budget_ac = 0.0
        prof_cons_debug = ""
        house_kwh_until_sunrise = 0.0
        f_today = 0.0
        f_tom = 0.0
        cur_hour = now.hour

        res = {
            "strategy_version": VERSION,
            "state": "standard",
            "mode": mode,
            "active_hours": [],
            "active_periods": "",
            "recommended_power_kw": 0.0,
            "target_price": 0.0,
            "limit_used": 0.0,
            "today_prices": {},
            "tomorrow_prices": {},
            "multi_cycle": "Не предвидится",
            "deg_cost": deg_cost,
            "profit_threshold": prof_thresh,
            "sell_simulation": {"projected_soc_at_start_pct": b_soc, "projected_soc_after_sale_pct": b_soc, "projected_soc_morning_pct": b_soc},
            "arbitrage_decision": "Нет данных",
            "charge_reason": "Нет",
            "strategy_candidates": [],
            "raw_commands": {}
        }
        
        old_calc = bool(getattr(self, "_calculating_strategy", False))
        self._calculating_strategy = True
        
        # v11.8.437: Initialize all attributes to 0 to avoid stale data or missing keys
        gatekeeper_floor = 0.0
        active_safety_floor = 0.0
        morning_reserve_floor = 0.0
        h_prof_debug = {}
        
        # v11.8.435: Retrieve settings early to avoid UnboundLocalError
        min_soc_val = float(man.get_setting(CONF_MIN_SOC_BAT, 10.0))
        soc_buffer = float(man.get_setting(CONF_SOC_BUFFER, 5.0))
        user_limit = float(man.get_setting(CONF_AI_DISCHARGE_LIMIT, 20.0))
        price_sell_limit = float(man.get_setting(CONF_PRICE_SELL_LIMIT, 5.0))
        res["limit_used"] = price_sell_limit
        
        try:
            cur_hour = int(now.hour)
            today_str = now.strftime("%Y-%m-%d")
            tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
            _sell_debug = {} # v11.9.250: Diagnostics container
            
            p_sell_st = dict(man.data.get("prices_sell", {}))
            today_prices = dict(p_sell_st.get(today_str, {}))
            tomorrow_prices = dict(p_sell_st.get(tomorrow_str, {}))
            
            res["today_prices"] = today_prices
            res["tomorrow_prices"] = tomorrow_prices

            force_sell = bool(man.get_setting(CONF_FORCE_MARKET_SELL, False))
            if force_sell:
                res["target_price"] = 0.0
                res["limit_used"] = 0.0
                res["active_hours"] = [cur_hour]
                res["state"] = "active"
                res["current_mode_text"] = "Принудительная продажа"
                return res

            avg_prof_gen = man.get_average_profile("generation", man.custom_period, man.day_type)
            avg_prof_cons = man.get_average_profile("consumption_base", man.custom_period, man.day_type)

            sunrise_h = 8
            for h in range(4, 12):
                if float(normalize_float(avg_prof_gen.get(str(h), 0.0))) > 0.1:
                    sunrise_h = h
                    break
            res["sunrise_hour"] = sunrise_h
            
            # v11.8.523: Define profiles at top level to avoid UnboundLocalError in downstream stages
            _sim_gen_profile = dict(man.get_predicted_profile("generation"))
            _sim_cons_profile = dict(man.get_predicted_profile("consumption_base"))

            target_hours = []
            epochs = []
            all_sell_prices = {}
            for h, p in today_prices.items(): all_sell_prices[int(h)] = float(normalize_float(p))
            for h, p in tomorrow_prices.items(): all_sell_prices[int(h) + 24] = float(normalize_float(p))
            
            if not all_sell_prices: return res

            cur_p_f = all_sell_prices.get(cur_hour, 0.0)
            sell_limit = float(man.get_setting(CONF_PRICE_SELL_LIMIT, 5.0))
            eff = float(self.get_efficiency_coefficient() or 1.0)
            
            # Arbitrage Logic
            p_buy_st = dict(man.data.get("prices_buy", {}))
            b_p_today = dict(p_buy_st.get(today_str, {}))
            b_p_tom = dict(p_buy_st.get(tomorrow_str, {}))
            all_buy_prices = {}
            for h, p in b_p_today.items(): all_buy_prices[int(h)] = float(normalize_float(p))
            for h, p in b_p_tom.items(): all_buy_prices[int(h) + 24] = float(normalize_float(p))

            threshold = float(max(prof_thresh, 2.0 * deg_cost))
            
            def get_best_buyback(after_h):
                options = {h: p for h, p in all_buy_prices.items() if h > after_h}
                if not options: return 999.0, None
                best_h = min(options, key=lambda k: options[k])
                return options[best_h], best_h

            def is_profitable(price, hour):
                p_bb, h_bb = get_best_buyback(hour)
                if h_bb is None: return False, 0.0
                gain = float(price * eff - p_bb - deg_cost)
                return gain >= threshold, gain

            def get_peaks(window, limit):
                if not window: return []
                w_vals = [float(v) for v in window.values()]
                if not w_vals: return []
                target = max(w_vals)
                if target < limit - 0.001: return []
                peak_hours = [int(h) for h, p in window.items() if float(p) == target]
                peaks = set()
                for peak_h in peak_hours:
                    h = peak_h
                    while str(h) in window and float(window[str(h)]) >= limit:
                        peaks.add((h, float(window[str(h)])))
                        h -= 1
                    h = peak_h + 1
                    while str(h) in window and float(window[str(h)]) >= limit:
                        peaks.add((h, float(window[str(h)])))
                        h += 1
                return sorted(list(peaks), key=lambda x: x[0])

            # Identification of Target Peaks
            today_morn = {h: p for h, p in today_prices.items() if int(h) < 13}
            today_eve = {h: p for h, p in today_prices.items() if int(h) >= 13}
            tom_morn = {h: p for h, p in tomorrow_prices.items() if int(h) < 13}
            tom_eve = {h: p for h, p in tomorrow_prices.items() if int(h) >= 13}

            dynamic_sell = bool(man.get_setting(CONF_DYNAMIC_SOC_SELL, True))
            target_hours = []
            
            if not dynamic_sell:
                target_hours = [h for h, p in all_sell_prices.items() if p >= sell_limit and h >= cur_hour]
                target_price = max([all_sell_prices[h] for h in target_hours], default=0.0)
                epochs = self._group_contiguous(target_hours)
            else:
                # Find tech peaks
                tech_peaks = []
                for win in [today_morn, today_eve, tom_morn, tom_eve]:
                    win_peaks = get_peaks(win, sell_limit)
                    if win == tom_morn or win == tom_eve:
                        tech_peaks.extend([(h + 24, p) for h, p in win_peaks])
                    else:
                        tech_peaks.extend(win_peaks)
                
                # Filter by profitability or surplus
                surplus_dc = max(0.0, (b_soc - float(man.get_setting(CONF_AI_DISCHARGE_LIMIT, 20.0))) * b_cap / 100.0)
                
                # v11.9.270: Solar Saturation Awareness (TS 198) - Unified forecast fetch
                f_today_val = float(man.get_forecast_value(man.forecast_today_sensor) or 0.0)
                f_tom_v = float(man.get_forecast_value(man.forecast_tomorrow_sensor) or 0.0)
                energy_to_full = (100.0 - b_soc) * b_cap / 100.0
                
                # v11.9.423: Removed tomorrow's forecast from surplus trigger to protect night survival.
                # Surplus is only if TODAY we have more energy than we can store.
                is_solar_surplus = (f_today_val > energy_to_full + 2.0)
                
                # v11.9.240: Explicit debug components
                _debug_surplus = {
                    "f_today": round_f(f_today_val, 1),
                    "f_tom": round_f(f_tom_v, 1),
                    "e_to_full": round_f(energy_to_full, 1),
                    "is_surplus": is_solar_surplus
                }
                _sell_debug["debug_surplus"] = _debug_surplus
                
                safe_peaks = []
                # v11.7.296: Floodgate Mode - if surplus is huge, include all hours above limit
                if is_solar_surplus:
                    for h in range(cur_hour, cur_hour + 12):
                        p = all_sell_prices.get(h, 0.0)
                        if p >= sell_limit - 0.001:
                            safe_peaks.append((h, p))
                
                for h, p in tech_peaks:
                    if h < cur_hour: continue
                    if any(x[0] == h for x in safe_peaks): continue
                    
                    is_ok, _ = is_profitable(p, h)
                    if p >= sell_limit - 0.001 or is_ok or surplus_dc > 0.1:
                        safe_peaks.append((h, p))
                
                # Convert back to just hours for target_hours
                safe_peaks = [x[0] for x in safe_peaks]
            target_hours = sorted([h for h in safe_peaks if h >= cur_hour])
            if not target_hours:
                res["state"] = "price_limit_not_met"
                res["current_mode_text"] = "Нет будущих окон"
                # v11.9.332: DO NOT return early. We must proceed to simulation to get morning SOC projection.
                # return res 

            # v11.8.521: UI Bugfix - Target price should be the maximum of all candidates, not just the first hour.
            target_price = max([all_sell_prices.get(th, 0.0) for th in target_hours], default=0.0) if target_hours else 0.0

            # --- TS 6.1 Sunrise Guard & Budget Grouping ---
            # Initial contiguity grouping
            epochs = self._group_contiguous(target_hours)
            
            # v11.8.561: Unified Gatekeeper - Refined Split Limits
            emergency_soc = float(man.get_setting(CONF_MIN_SOC_BAT, 10.0))
            user_limit = float(man.get_setting(CONF_AI_DISCHARGE_LIMIT, 20.0))
            soc_buffer = float(man.get_setting(CONF_SOC_BUFFER, 5.0))
            
            # Policy & Survival Components
            morning_h = man.get_sunrise_hour() or 8
            def get_next_sunrise(h_abs):
                if h_abs < morning_h: return morning_h
                if h_abs < morning_h + 24: return morning_h + 24
                return morning_h + 48

            cur_h_rel = cur_hour % 24
            is_turbo_win = (4 <= cur_h_rel < 10)
            
            # 1. Find the end of the current NIGHT POOL (before turbo or sunrise)
            # v11.8.563: If pool is separated, we must meet the limit at the VERY END of the pool.
            h_end_pool = cur_hour
            next_sunrise_abs = get_next_sunrise(cur_hour)
            
            # Pool boundary: If we are before 04:00, the "night pool" ends at 04:00. 
            # If we are after 04:00, it ends at Sunrise.
            pool_boundary = 4 if cur_hour < 4 else next_sunrise_abs
            
            if target_hours:
                for h in target_hours:
                    if h < pool_boundary:
                        h_end_pool = h
                    else:
                        break
            
            # v11.9.447: Use unified Gatekeeper Floor from base class to ensure consistency
            # This replaces the manual split-pool calculation which was prone to profile mismatches.
            gatekeeper = self.get_gatekeeper_floor(cur_hour, next_sunrise_abs)
            
            # Recalculate house percentages for diagnostic compatibility using the same engine
            house_after_pct = round_f(self.get_survival_floor(h_end_pool, next_sunrise_abs) - emergency_soc, 1)
            house_during_pct = round_f(self.get_survival_floor(cur_hour, h_end_pool) - emergency_soc, 1)

            # 3. Determine Active Safety Floor for Current Hour (Limit for SALE)
            active_safety_floor = max(user_limit, gatekeeper)
            if is_turbo_win:
                limit_reason = "Turbo (Morning)"
            else:
                limit_reason = "Safe Mode"
                limit_reason = "Safe Mode"

            available_sell_dc_pre = max(0.0, (b_soc - active_safety_floor) * b_cap / 100.0)
            available_sell_ac = max(0.0, available_sell_dc_pre * eff)
            
            # --- Stage 1: Projection & Saturation Awareness ---
            sim_range = list(range(cur_hour, cur_hour + 48))
            _, sim_log_base, _ = self.run_soc_simulation(
                b_soc, sim_range, now, 
                commands={}, 
                b_min_soc=emergency_soc, 
                ignore_blended=True, house_profile_override="consumption_base"
            )
            
            # Detect Solar Surplus (Saturation Awareness)
            hit_full_before = any(v.get("soc", 0.0) >= 99.0 for h, v in sim_log_base.items() if ":" in h and "Завтра" not in h)
            is_solar_surplus = is_solar_surplus or hit_full_before

            # Find projected SOC at the exact start of the sale window
            soc_at_start = b_soc
            if target_hours and target_hours[0] > cur_hour:
                prev_h = target_hours[0] - 1
                sale_start_key = f"{prev_h%24:02d}:59" + (" (Завтра)" if prev_h >= 24 else "")
                soc_at_start = self._get_soc_from_log(sim_log_base, sale_start_key, b_soc)
            
            # v11.7.280: Restore epochs for the allocator
            p_today = man.data.get("prices_sell", {})
            p_tomorrow = man.data.get("prices_sell_tomorrow", {})
            epochs = self.get_strategy_epochs(target_hours, p_today, p_tomorrow)
            first_epoch = epochs[0] if epochs else []

            # 1. Pre-calculate safety floors for simulation (Sliding Guard)
            # v11.9.8: Unified Floor Logic (House Survival + User Limit + Efficiency)
            floors_sliding = {}
            floors_anchored = {}
            _sim_cons_profile = dict(man.get_predicted_profile("consumption_total"))
            _sim_gen_profile = dict(man.get_predicted_profile("generation_total"))
            
            for h_abs in sim_range:
                # v11.9.449: Unified Gatekeeper Floor handles Turbo Mode internally
                next_sr = get_next_sunrise(h_abs)
                survival_floor = self.get_gatekeeper_floor(h_abs, next_sr)
                h_floor = max(user_limit, survival_floor)
                
                floors_sliding[h_abs] = h_floor 
                floors_anchored[h_abs] = h_floor

            # 2. Budget Calculation
            available_sell_dc = max(0.0, (b_soc - active_safety_floor) * b_cap / 100.0)
            
            # v11.8.490: First Non-Empty Discharge Cycle
            # Group target hours into 24h clusters ending at 10:00 AM.
            # We only process the FIRST cluster that has any target hours.
            if target_hours:
                cycle_map = {}
                for h in target_hours:
                    # Shift by 10 hours so each cycle ends at 10:00 AM
                    c_id = (h - 10) // 24
                    if c_id not in cycle_map: cycle_map[c_id] = []
                    cycle_map[c_id].append(h)
                
                first_c_id = min(cycle_map.keys())
                target_hours = cycle_map[first_c_id]
            
            # Sort by Price (Primary) and Hour (Secondary). 
            # If prices are equal, prioritize the LATER hour to protect SOC for earlier peaks
            # and follow the natural flow of evening discharge.
            # v11.8.558: Saturation-Aware Sorting
            # If price is the same, prioritize EARLIER hours during solar surplus
            # to make room for incoming solar and maximize immediate revenue.
            if is_solar_surplus:
                h_by_priority = sorted(target_hours, key=lambda h: (all_sell_prices.get(h, 0.0), -h), reverse=True)
            else:
                h_by_priority = sorted(target_hours, key=lambda h: (all_sell_prices.get(h, 0.0), h), reverse=True)
            max_batt_p = float(man.get_setting(CONF_BATTERY_MAX_POWER, 5.0))
            # 1. Initial Budget (Initial Guess as per TS 103)
            # v11.9.225: Synchronized start floor (use floor of the first target hour)
            first_sell_h_abs = target_hours[0] if target_hours else cur_hour
            start_floor = floors_sliding.get(first_sell_h_abs, active_safety_floor)
            
            # v11.9.285: In surplus mode, allow initial budget to ignore heavy nocturnal floors
            # but stay strictly above Turbo Mode limit (MinSOC + 2%)
            if is_solar_surplus:
                start_floor = emergency_soc + 2.0
                
            # v11.9.320: Use DC budget (no eff here) as per user request to avoid under-selling.
            available_sell_dc = max(0.0, (soc_at_start - start_floor) * b_cap / 100.0)
            
            # v11.9.430: If no energy above floor, clear targets to avoid "phantom" plans in UI
            if available_sell_dc <= 0.05:
                target_hours = []
                _sell_debug["limit_reason"] = "Недостаточно заряда"

            target_budget_ac = available_sell_dc # DC kwh equivalent
            
            _sell_debug["initial_budget"] = round_f(target_budget_ac, 2)
            _sell_debug["target_floors"] = {f"{h%24:02d}h": round_f(floors_sliding.get(h, 0.0), 1) for h in target_hours}
            
            sell_commands = {}
            sim_log = {}
            total_deficit_kwh = 0.0
            deficit_detail = []
            max_soc_deficit_kwh = 0.0
            
            if target_hours:
                for attempt in range(20): # v11.9.315: Increased iterations for complex cases
                    sell_commands = {}
                    rem_budget = target_budget_ac
                
                    # 2. Distribution: Strict price-hour priority (TS 104)
                    for h in h_by_priority:
                        duration = 1.0
                        if h == cur_hour:
                            duration = max(0.01, 1.0 - (now.minute / 60.0))
                        
                        # v11.9.551: DC-Oriented Allocation. 1kW in plan = 1kW from Battery (DC).
                        # p_export is DC power here.
                        p_export = min(max_batt_p, rem_budget / duration)
                        sell_commands[h] = round_f(p_export, 3) if p_export > 0.05 else 0.0
                        rem_budget -= p_export * duration
                
                    # 3. Simulation Check (TS 105)
                    # Important: We must pass AC commands to simulation and inverter!
                    # AC_cmd = DC_plan * eff
                    sim_commands = {h: -(p * eff) for h, p in sell_commands.items()}
                    
                    _, trial_log, _ = self.run_soc_simulation(
                        b_soc, sim_range, now, commands=sim_commands, 
                        b_min_soc=emergency_soc, ignore_blended=True, 
                        house_profile_override="consumption_base", dynamic_floors=floors_sliding
                    )
                    sim_log = trial_log
                    
                    # 4. Bidirectional Refinement (TS 106)
                    total_deficit_kwh = 0.0
                    for h_cmd, p_req in sell_commands.items():
                        if p_req <= 0: continue
                        duration = 1.0
                        if h_cmd == cur_hour: duration = max(0.01, 1.0 - (now.minute / 60.0))
                        
                        h_sim_key = f"{h_cmd%24:02d}:59" + (" (Завтра)" if h_cmd >= 24 else "")
                        p_real_bat = trial_log.get(h_sim_key, {}).get("p_bat", 0.0)
                        # v11.9.556: Convert AC power from simulation to DC before comparing with DC request
                        # This prevents the allocator from seeing efficiency loss as a "deficit".
                        p_real_dc = p_real_bat / max(0.1, eff)
                        
                        if p_real_bat >= 0 and p_real_dc < p_req - 0.05:
                            diff = (p_req - p_real_dc) * duration
                            total_deficit_kwh += diff
                            
                            sim_data = trial_log.get(h_sim_key, {})
                            h_mode = sim_data.get('mode', 'unk')
                            h_net = sim_data.get('net_kw', 0.0)
                            h_floor = sim_data.get('floor', 0.0)
                            h_soc = sim_data.get('soc', 0.0)
                            
                            reason = " (Locked/Limit)"
                            if p_real_bat > 0.05 or h_soc < h_floor + 0.1:
                                reason = f" (Floor: {h_floor:.1f}%)"
                            
                            deficit_detail.append(f"{h_cmd}h: req {p_req:.2f}, real {p_real_bat:.2f} [M:{h_mode} Net:{h_net:.2f}]{reason}")
                    
                    # v11.9.566: Limit deficit check to sell window + 3h buffer ONLY.
                    # Natural overnight house consumption causing SOC drops FAR from sell window
                    # is Buy strategy's responsibility, not Sell's. Checking the full 48h range
                    # causes the sell budget to be cut incorrectly.
                    max_soc_deficit_kwh = 0.0
                    sell_window_end = (max(target_hours) + 3) if target_hours else cur_hour
                    for h_abs in sim_range:
                        if h_abs > sell_window_end:
                            break
                        h_key = f"{h_abs%24:02d}:59" + (" (Завтра)" if h_abs >= 24 else (" (Через день)" if h_abs >= 48 else ""))
                        soc_h = trial_log.get(h_key, {}).get("soc", 100.0)
                        floor_h = floors_sliding.get(h_abs, emergency_soc + 2.0)
                        if soc_h < floor_h - 0.1:
                            max_soc_deficit_kwh = max(max_soc_deficit_kwh, (floor_h - soc_h) * b_cap / 100.0)
                    
                    # v11.9.541: Convergence check based on morning SOC at sunrise
                    sunrise_key = f"{next_sunrise_abs%24:02d}:59" + (" (Завтра)" if next_sunrise_abs >= 24 else "")
                    soc_morning_sim = trial_log.get(sunrise_key, {}).get("soc", 0.0)
                    target_morning = floors_sliding.get(next_sunrise_abs, emergency_soc + 2.0)
                    soc_err = soc_morning_sim - target_morning

                    # v11.9.542: Priority-Based Refinement
                    if total_deficit_kwh > 0.15:
                        # Priority 1: Physical impossibility
                        target_budget_ac = max(0.0, target_budget_ac - total_deficit_kwh * 0.6)
                    elif max_soc_deficit_kwh > 0.05:
                        # Priority 2: Correct SOC deficit seen at any hour
                        # v11.9.565: Use 1.0x multiplier (was 1.1) to prevent over-correction
                        target_budget_ac = max(0.0, target_budget_ac - max_soc_deficit_kwh * 1.0)
                    elif soc_err > 0.1:
                        # Priority 3: Increase budget if we have surplus at sunrise
                        target_budget_ac += (soc_err * b_cap / 100.0) * 0.4
                    else:
                        # Convergence!
                        _sell_debug["final_budget"] = round_f(target_budget_ac, 2)
                        _sell_debug["total_deficit"] = round_f(total_deficit_kwh, 3)
                        _sell_debug["deficit_detail"] = deficit_detail
                        _sell_debug["max_soc_deficit"] = round_f(max_soc_deficit_kwh, 3)
                        break
            else:
                # v11.9.332: No sell windows found. Run natural flow simulation.
                _, sim_log, _ = self.run_soc_simulation(
                    b_soc, sim_range, now, commands={}, 
                    b_min_soc=emergency_soc, ignore_blended=True, 
                    house_profile_override="consumption_base", dynamic_floors=floors_sliding
                )
            
            # v11.9.567: Final redistribution REMOVED. It bypassed floor checks, causing
            # sell commands to push SOC below the gatekeeper floor.
            # sell_commands from the convergence loop are already floor-verified.

            # v11.9.255: Update diagnostics
            _sell_debug["final_budget"] = round_f(target_budget_ac, 2)
            _sell_debug["total_deficit"] = round_f(total_deficit_kwh, 3)
            _sell_debug["deficit_detail"] = deficit_detail
            _sell_debug["commands"] = {f"{h}h": round_f(p, 3) for h, p in sell_commands.items() if p > 0}
            _sell_debug["max_batt_p"] = round_f(max_batt_p, 2)
            
            # Final Pass: Use the best sim_log we found



            # --- Stage 4: Build Plan ---
            planned_results = {}
            sorted_h = sorted(sell_commands.keys())
            active_h = [h for h, p in sell_commands.items() if p > 0.05]
            
            # Initial reason based on current hour command
            cur_cmd = sell_commands.get(cur_hour, 0.0)
            if cur_cmd > 0.05:
                limit_reason = "Активная продажа (Приоритет: Цена)"
            elif cur_hour in target_hours:
                limit_reason = "Ограничение (Цена > Порог, но бюджет на другие часы)"
            elif target_hours:
                limit_reason = "Ожидание пика"
            elif available_sell_dc <= 0.05:
                limit_reason = "Недостаточно заряда"
            else:
                limit_reason = "Нет ценового окна"
            
            for h in sorted_h:
                h_sim_key = f"{h%24:02d}:59" + (" (Завтра)" if h >= 24 else "")
                sim_entry = sim_log.get(h_sim_key, {})
                # v11.9.270: Show ONLY hours where we explicitly planned a battery sale
                if sell_commands.get(h, 0.0) <= 0.05: continue
                
                # v11.9.265: Diagnostics show TOTAL export (Solar + Battery)
                real_p_export = float(sim_entry.get("p_inv_ac", sim_entry.get("p_bat", 0.0)))
                real_p_bat = float(sim_entry.get("p_bat", 0.0))
                
                if real_p_export <= 0.05: continue
                
                sim_soc = float(sim_entry.get("soc", b_soc))
                
                # Diagnostics: Determine why we aren't selling at max_p
                if real_p_export < sell_commands.get(h, 0.0) - 0.1:
                    h_floor = floors_anchored.get(h, emergency_soc + 2.0)
                    if abs(sim_soc - user_limit) < 0.2:
                        limit_reason_h = "Лимит пользователя"
                    elif h_floor > emergency_soc + 2.0 + 0.5:
                        limit_reason_h = "Gatekeeper"
                    else:
                        limit_reason_h = "Утренний лимит"
                    if h == cur_hour:
                        limit_reason = limit_reason_h
                
                # v11.9.275: Technical Diagnostics & Inverter Integration Info
                p_bat_req = sell_commands.get(h, 0.0)
                limit_reason_h = "Max" if p_bat_req >= max_batt_p - 0.15 else "AI"
                
                if real_p_bat < p_bat_req - 0.15:
                    h_floor = floors_anchored.get(h, emergency_soc + 2.0)
                    if abs(sim_soc - user_limit) < 0.2:
                        limit_reason_h = "Лимит пользователя"
                    elif h_floor > emergency_soc + 2.0 + 0.5:
                        limit_reason_h = "Gatekeeper"
                    else:
                        limit_reason_h = "Утренний лимит"

                if h == cur_hour:
                    limit_reason = limit_reason_h

                h_key = f"{h%24:02d}:00" + (" (Завтра)" if h >= 24 else "")
                # v11.9.320: Show DC battery power (divide AC back by eff) as requested by user.
                # Inverter script needs to know how much to pull from the battery.
                p_bat_dc = real_p_bat / max(0.1, eff)
                planned_results[h_key] = {
                    "power": round_f(p_bat_dc, 3),
                    "soc": round_f(sim_soc, 1),
                    "display": f"{p_bat_dc:.3f} кВт (SOC: {sim_soc:.1f}%) [{real_p_export:.1f} Exp] ({limit_reason_h})"
                }

            # 5. UI Diagnostics (v11.7.137: Restored missing variables)
            morning_h_abs = morning_h if cur_hour < morning_h else (morning_h + 24)
            morning_key = f"{morning_h%24:02d}:59" + (" (Завтра)" if morning_h_abs >= 24 else "")
            soc_morning = self._get_soc_from_log(sim_log, morning_key, b_soc)
            
            last_sell_h = max(active_h) if active_h else cur_hour
            last_h_key = f"{last_sell_h%24:02d}:59" + (" (Завтра)" if last_sell_h >= 24 else "")
            soc_end = self._get_soc_from_log(sim_log, last_h_key, b_soc)
            
            target_morning = emergency_soc + 2.0
            # Gatekeeper: Current anchored floor
            gatekeeper_val = floors_anchored.get(cur_hour, emergency_soc + 2.0)

            res.update({
                "planned_power_per_h": planned_results,
                "target_soc": round_f(active_safety_floor, 1),
                "recommended_power_kw": sell_commands.get(cur_hour, 0.0),
                "limit_used": user_limit if not is_turbo_win else "None",
                "target_price": target_price,
                "limit_reason": limit_reason,
                "target_morning": round_f(target_morning, 1),
                "gatekeeper_after_sale": round_f(gatekeeper, 1) if not is_turbo_win else "Turbo",
                "gatekeeper_floor": round_f(gatekeeper_val, 1),
                "projected_soc_morning": round_f(soc_morning, 1),
                "projected_soc_after_sale": round_f(soc_end, 1),
                "arbitrage_buyback": {
                    "target_morning_soc_pct": round_f(floors_sliding.get(morning_h_abs, emergency_soc + 2.0), 1)
                }
            })
            
            # --- Stage 4: Final Simulation for UI (Projection) ---
            # v11.9.440: Inject mode_overrides to ensure UI simulation respects charge_from_pv rules
            mode_overrides_sim = {}
            for h in sim_range:
                if h in sell_commands and sell_commands[h] > 0.01:
                    if man.get_setting(CONF_BATTERY_DISCHARGE_ENABLED, True):
                        mode_overrides_sim[h] = "sale_pv_bat"
                    else:
                        mode_overrides_sim[h] = "sale_pv_no_bat"
                elif h in active_h: # Selling but maybe zero power
                     mode_overrides_sim[h] = "sale_pv_no_bat"

            _, sim_log_final, _ = self.run_soc_simulation(
                b_soc, sim_range, now, 
                commands={h: -p for h, p in sell_commands.items()}, 
                b_min_soc=emergency_soc, dynamic_floors=floors_sliding,
                ignore_blended=True, house_profile_override="consumption_base",
                mode_overrides=mode_overrides_sim
            )
            sim_log = sim_log_final # Use this for all UI displays below

            # v11.8.523: Show all price candidates, even if battery power is 0kW.
            res["strategy_candidates"] = [f"{h%24:02d}:00" + (" (Завтра)" if h >= 24 else "") for h in target_hours]
            res["active_hours"] = active_h
            
            def group_h(hours):
                if not hours: return ""
                periods = self._group_contiguous(hours)
                groups = []
                for p in periods:
                    groups.append(f"{p[0]%24:02d}:00-{p[-1]%24:02d}:59" + (" (Завтра)" if p[0] >= 24 else ""))
                return ", ".join(groups)

            res["active_periods"] = group_h(active_h)
            res["analyzed_window"] = f"До {max(active_h)%24:02d}:59" + (" (Завтра)" if max(active_h) >= 24 else "") if active_h else "Нет продажи"
            
            # v11.7.58: Correctly find SOC from log using new keys
            def _get_soc_val(log, h_abs):
                h_rel = h_abs % 24
                day_suffix = ""
                if h_abs >= 48: day_suffix = " (Через день)"
                elif h_abs >= 24: day_suffix = " (Завтра)"
                return self._get_soc_from_log(log, f"{h_rel:02d}:59{day_suffix}", b_soc)

            first_sell_h = min(active_h) if active_h else cur_hour
            last_sell_h = max(active_h) if active_h else cur_hour
            
            res["sell_simulation"] = {
                "projected_soc_at_sale_start_pct": round_f(_get_soc_val(sim_log, first_sell_h - 1), 1),
                "projected_soc_after_sale_pct": round_f(_get_soc_val(sim_log, last_sell_h), 1),
                "projected_soc_morning_pct": round_f(_get_soc_val(sim_log, morning_h_abs), 1),
                "hit_full_before": hit_full_before if 'hit_full_before' in locals() else False,
                "log": sim_log
            }
            res["raw_commands"] = sell_commands
            
            v_val = 52.0
            if man.battery_voltage_sensor:
                v_val = float(man.get_sensor_float(man.battery_voltage_sensor) or 52.0)
            res["recommended_amps"] = round_f((sell_commands.get(cur_hour, 0.0) * 1000.0) / v_val, 1) if v_val > 0 else 0.0
            
            res["arbitrage_decision"] = f"Продажа по {cur_p_f:.2f}" if cur_hour in active_h else "Ожидание пика"
            
            # v11.8.559: Simplified Overall Limit ID
            overall_limit = limit_reason
            if not any(p > 0.05 for p in sell_commands.values()):
                overall_limit = "Цена"
            
            # v11.7.131: Final status building
            if cur_hour in active_h:
                p_now = sell_commands.get(cur_hour, 0.0)
                # 1. Inverter priority
                if p_now >= max_batt_p - 0.1:
                    res["power_decision"] = "Лимит: Инвертор"
                else:
                    res["power_decision"] = f"Лимит: {overall_limit}"
            else:
                res["power_decision"] = "Ожидание пика"
                if overall_limit != "Цена":
                    res["power_decision"] += f" ({overall_limit})"
            
            # Restore old sell_debug structure
            f_today = round_f(float(man.get_forecast_value(man.forecast_today_sensor) or 0.0), 1)
            f_tom_val = round_f(float(man.get_forecast_value(man.forecast_tomorrow_sensor) or 0.0), 1)
            
            # House profile for debug
            prof_cons_debug = "|".join([f"{h%24}:{(float(avg_prof_cons.get(str(h%24), 0.0))):.1f}" for h in range(cur_hour, cur_hour + 12)])

            # v11.7.55: Rock-solid sim_log display
            debug_log_parts = []
            for h in range(cur_hour, cur_hour + 24): # Show full 24h
                h_rel = h % 24
                day_suffix = ""
                if h >= 48:
                    day_suffix = " (Через день)"
                elif h >= 24:
                    day_suffix = " (Завтра)"
                
                key = f"{h_rel:02d}:59{day_suffix}"
                val = sim_log.get(key)
                
                if isinstance(val, dict):
                    # v11.7.140: Show ACTUAL simulated power. 
                    # Positive values in p_bat mean Discharge (Sell) in history log.
                    p_sim = val.get('p_bat', 0.0)
                    debug_log_parts.append(f"{h_rel}: {val.get('soc', 0):.0f}% ({p_sim:.1f}k)")
                else:
                    debug_log_parts.append(f"{h_rel}: ---")

            # v11.7.397: Saturation Awareness for UI
            # morning_h_abs already defined above correctly
            first_sell_h = min(active_h) if active_h else cur_hour
            last_sell_h = max(active_h) if active_h else cur_hour
            
            res["sell_simulation"] = {
                "hit_full_before": hit_full_before,
                "projected_soc_at_sale_start_pct": round_f(self._get_soc_from_log(sim_log, f"{(first_sell_h-1)%24:02d}:59" + (" (Завтра)" if (first_sell_h-1) >= 24 else ""), b_soc), 1),
                "projected_soc_after_sale_pct": round_f(self._get_soc_from_log(sim_log, f"{last_sell_h%24:02d}:59" + (" (Завтра)" if last_sell_h >= 24 else ""), b_soc), 1),
                # v11.9.145: Use morning_h_abs - 1 to see SOC at the START of sunrise hour (before generation)
                "projected_soc_morning_pct": round_f(self._get_soc_from_log(sim_log, f"{(morning_h_abs-1)%24:02d}:59" + (" (Завтра)" if (morning_h_abs-1) >= 24 else ""), b_soc), 1),
                "log": sim_log
            }

            # v11.9.145: Correct sim_gen to only sum the next 24 hours
            sim_gen_24h = 0.0
            for h_offset in range(24):
                h_sim = cur_hour + h_offset
                h_sim_rel = h_sim % 24
                h_sim_suffix = ""
                if h_sim >= 48: h_sim_suffix = " (Через день)"
                elif h_sim >= 24: h_sim_suffix = " (Завтра)"
                h_sim_key = f"{h_sim_rel:02d}:59{h_sim_suffix}"
                h_sim_data = sim_log.get(h_sim_key)
                if isinstance(h_sim_data, dict):
                    sim_gen_24h += float(h_sim_data.get('gen_kw', 0.0))

            res["arbitrage_sell_debug"] = {
                "start_soc": f"{b_soc:.1f}%",
                "gatekeeper_cur_h": round_f(gatekeeper, 1) if not is_turbo_win else "Turbo",
                "gatekeeper_last_sell_h": round_f(floors_sliding.get(max(target_hours), gatekeeper), 1) if target_hours else round_f(gatekeeper, 1),
                "active_safety_floor": round_f(active_safety_floor, 1),
                "available_ac": round_f(available_sell_ac, 2),
                "limit_reason": limit_reason or "None",
                "next_peak": f"{target_hours[0] % 24:02d}:00" if target_hours else "None",
                "soc_at_peak": round_f(soc_at_start, 1),
                "house_until_sunrise_pct": round_f(house_after_pct + house_during_pct, 2),
                "house_h": "Profile",
                "sim_gen": round_f(sim_gen_24h, 1),
                "sim_log": " | ".join(debug_log_parts),
                "final_budget": round_f(target_budget_ac, 2),
                "total_deficit": round_f(total_deficit_kwh, 3),
                "max_soc_deficit": round_f(max_soc_deficit_kwh, 3),
                "final_targets": str(target_hours),
                "f_today": f_today,
                "f_tom": f_tom_val,
                "target_price": round_f(target_price, 3),
                "cur_p": cur_p_f,
                "house_profile_debug": h_prof_debug,
                "commands": {f"{h}h": p for h, p in sell_commands.items()}
            }
            if '_sell_debug' in locals(): res["arbitrage_sell_debug"].update(_sell_debug)

            if sell_commands.get(cur_hour, 0.0) > 0.05:
                res["state"] = "active"
                res["current_mode_text"] = "Активная продажа"
            else:
                res["current_mode_text"] = "Ожидание пика" if target_hours else "Нет ценового окна"

            # v11.9.578: Proper Arbitrage Decision (TS 199/User Req)
            arb_msg = "Нет выгодного арбитража"
            buy_res = self.manager.get_market_strategy("buy")
            buy_active_h = buy_res.get("active_hours", [])
            
            # Find best buy hour (lowest price)
            best_buy_h = None
            if buy_active_h:
                best_buy_h = min(buy_active_h, key=lambda h: self.manager.get_price("buy", today_str, h % 24) or 999.0)
            
            if target_hours:
                # Find best sell hour (highest price)
                best_sell_h = max(target_hours, key=lambda h: self.manager.get_price("sell", today_str, h % 24) or 0.0)
                
                p_buy = (self.manager.get_price("buy", today_str, best_buy_h % 24) if best_buy_h is not None else target_price) or 0.0
                p_sell = (self.manager.get_price("sell", today_str, best_sell_h % 24)) or 0.0
                profit = (p_sell - p_buy) - deg_cost
                
                h_buy_str = f"{best_buy_h % 24:02d}:00" if best_buy_h is not None else "Plan"
                h_sell_str = f"{best_sell_h % 24:02d}:00"
                if best_buy_h is not None and best_buy_h >= 24: h_buy_str += " (Завтра)"
                if best_sell_h >= 24: h_sell_str += " (Завтра)"
                
                arb_msg = f"Купим в {h_buy_str} ({p_buy:.2f}) -> Продадим в {h_sell_str} ({p_sell:.2f}). Профит: {profit:.2f}/кВтч"
            elif buy_active_h:
                arb_msg = "Зарядка активна, но выгодных окон продажи пока нет"
            
            res["arbitrage_decision"] = arb_msg
            res["strategy_decision"] = res.get("current_mode_text", "Ожидание")

            self._strategy_cache[cache_key] = {"time": now, "res": res}
            return res
        finally:
            self._calculating_strategy = old_calc

    # --- Support Methods ---
    def _group_contiguous(self, hours):
        if not hours: return []
        hours = sorted(list(set(hours)))
        periods = []
        if not hours: return periods
        curr = [hours[0]]
        for i in range(1, len(hours)):
            if hours[i] == hours[i-1] + 1:
                curr.append(hours[i])
            else:
                periods.append(curr)
                curr = [hours[i]]
        periods.append(curr)
        return periods

    def _get_soc_from_log(self, log, key, default):
        entry = log.get(key)
        if isinstance(entry, dict):
            return float(entry.get("soc", default))
        return float(default)
