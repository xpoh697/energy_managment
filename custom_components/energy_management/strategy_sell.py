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
        
        last_h_floor = min_soc_val + soc_buffer
        
        try:
            cur_hour = int(now.hour)
            today_str = now.strftime("%Y-%m-%d")
            tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
            
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
                if target < limit: return []
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
                
                # v11.7.270: Solar Saturation Awareness (TS 198)
                f_today_val = float(man.get_sensor_float(man.forecast_today_sensor) or 0.0)
                energy_to_full = (100.0 - b_soc) * b_cap / 100.0
                is_solar_surplus = (f_today_val > energy_to_full + 2.0) # 2kWh buffer
                
                safe_peaks = []
                # v11.7.296: Floodgate Mode - if surplus is huge, include all hours above limit
                if is_solar_surplus:
                    for h in range(cur_hour, cur_hour + 12):
                        p = all_sell_prices.get(h, 0.0)
                        if p >= sell_limit:
                            safe_peaks.append((h, p))
                
                for h, p in tech_peaks:
                    if h < cur_hour: continue
                    if any(x[0] == h for x in safe_peaks): continue
                    
                    is_ok, _ = is_profitable(p, h)
                    if p >= sell_limit or is_ok or surplus_dc > 0.1:
                        safe_peaks.append((h, p))
                
                # Convert back to just hours for target_hours
                safe_peaks = [x[0] for x in safe_peaks]
            target_hours = sorted([h for h in safe_peaks if h >= cur_hour])
            if not target_hours:
                res["state"] = "price_limit_not_met"
                res["current_mode_text"] = "Нет будущих окон"
                return res

            target_price = all_sell_prices.get(target_hours[0], 0.0) if target_hours else 0.0

            # --- TS 6.1 Sunrise Guard & Budget Grouping ---
            # Initial contiguity grouping
            epochs = self._group_contiguous(target_hours)
            
            min_soc_val = float(man.get_setting(CONF_MIN_SOC_BAT, 10.0))
            user_limit = float(man.get_setting(CONF_AI_DISCHARGE_LIMIT, 20.0))
            soc_buffer = float(man.get_setting(CONF_SOC_BUFFER, 5.0))
            # Window check
            current_hour_in_24 = cur_hour % 24
            is_morning_window = (4 <= current_hour_in_24 < 10)
            active_floor = (min_soc_val + 2.0) if is_morning_window else max(user_limit, min_soc_val + soc_buffer)
            
            # Initial potential budget (max discharge possible until window-specific floor)
            # v11.7.69: Budget for the NEXT target hour depends on ITS time window
            first_target_h = target_hours[0] if target_hours else cur_hour
            first_target_h_rel = first_target_h % 24
            is_target_morning = (4 <= first_target_h_rel < 10)
            
            floor_for_budget = (min_soc_val + 2.0) if is_target_morning else active_floor
            available_sell_dc = max(0.0, (b_soc - floor_for_budget) * b_cap / 100.0)
            available_sell_ac = max(0.0, available_sell_dc * eff)
            limit_reason = ""
            
            # --- Stage 1: Dynamic Gatekeeper (TS 6.1.1 / 183) ---
            # Global floor for budget calculation (from end of sale to sunrise)
            last_sell_planned_h = max(target_hours) if target_hours else cur_hour
            morning_h = man.get_sunrise_hour() or 8
            if cur_hour < morning_h:
                morning_h_abs = morning_h
            else:
                morning_h_abs = morning_h + 24
            
            # v11.7.111: Re-init profiles with safety
            prof_cons_cur = dict(man.get_average_profile("consumption_base", 7, now.weekday()))
            prof_cons_tom = dict(man.get_average_profile("consumption_base", 7, (now.weekday() + 1) % 7))
            occ_coeff, _, _, _, _, _, _ = man.get_occupancy_coefficient()
            occ_coeff = float(occ_coeff)

            # v11.7.129: Stage 1 - Base Safety Floors (TS 6.1)
            # 1. Gatekeeper (Survival): min_soc + house load until sunrise
            house_kwh_until_sunrise = 0.0
            gatekeeper_floor = min_soc_val
            morning_reserve_floor = min_soc_val + soc_buffer
            
            if is_morning_window:
                # v11.7.390: Relax buffer during solar surplus mornings
                if is_solar_surplus:
                    active_safety_floor = min_soc_val
                else:
                    active_safety_floor = min_soc_val + 2.0
                house_kwh_until_sunrise = 0.0
            else:
                # v11.8.430: Find the END of the current or near-term sell block
                # instead of the absolute last hour of the 48h plan.
                next_sunrise_abs = morning_h_abs
                
                # We care about the house load until the FIRST sunrise after our current selling activity
                current_epoch_end = cur_hour
                if target_hours:
                    for h in target_hours:
                        if h < next_sunrise_abs:
                            current_epoch_end = h
                        else:
                            break
                
                range_start = current_epoch_end + 1
                for h_abs in range(range_start, next_sunrise_abs):
                    h_rel = h_abs % 24
                    p_prof = prof_cons_tom if h_abs >= 24 else prof_cons_cur
                    h_load = p_prof.get(f"{h_rel:02d}") or p_prof.get(str(h_rel))
                    house_kwh_until_sunrise += float(normalize_float(h_load if h_load is not None else 0.4))
                
                gatekeeper_floor = min_soc_val + (house_kwh_until_sunrise / b_cap * 100.0)
                morning_reserve_floor = (min_soc_val + soc_buffer) + (house_kwh_until_sunrise / b_cap * 100.0)
                active_safety_floor = max(user_limit, gatekeeper_floor, morning_reserve_floor)

            # --- Stage 2: Budget Calculation (Projected SOC at Start of Sale) ---
            soc_at_start = b_soc
            if first_target_h > cur_hour:
                # Find projected SOC at the exact start of the sale window (End of the hour BEFORE)
                prev_h = first_target_h - 1
                sale_start_key = f"{prev_h%24:02d}:59" + (" (Завтра)" if prev_h >= 24 else "")
                
                # We use the baseline simulation (no commands) to see where we'll be
                _, sim_log_base, _ = self.run_soc_simulation(
                    start_soc=b_soc, sim_range=list(range(cur_hour, first_target_h)),
                    now=now, commands={}, house_profile_override="consumption_base", ignore_blended=True
                )
                soc_at_start = self._get_soc_from_log(sim_log_base, sale_start_key, b_soc)

            available_sell_dc = max(0.0, (soc_at_start - active_safety_floor) * b_cap / 100.0)
            available_sell_ac = available_sell_dc * eff
            
            _LOGGER.warning(f"[BudgetDebug] Start:{soc_at_start:.1f}% SafetyFloor:{active_safety_floor:.1f}% AvailableAC:{available_sell_ac:.2f}kWh")

            f_tom = float(man.get_forecast_value(man.forecast_tomorrow_sensor) or 0.0)
            tom_h_need = sum(float(normalize_float(prof_cons_tom.get(str(h % 24), 0.0))) for h in range(morning_h + 24, morning_h + 48))
            solar_deficit = max(0.0, tom_h_need - f_tom)
            if solar_deficit > 0.1:
                available_sell_ac = max(0.0, available_sell_ac - (solar_deficit / eff))
                if not limit_reason or limit_reason == "None": limit_reason = "Дефицит солнца завтра"

            # --- Stage 3: Greedy Priority Allocator (v11.7.136) ---
            sell_commands = {}
            
            # v11.7.275: Pre-populate sim_log with Baseline (Solar-Aware)
            sim_range = list(range(cur_hour, cur_hour + 48))
            _, sim_log, _ = self.run_soc_simulation(
                b_soc, sim_range, now, 
                commands={}, 
                b_min_soc=min_soc_val, 
                ignore_blended=True, house_profile_override="consumption_base"
            )
            
            # v11.7.397: Saturation Awareness (hit_full_before)
            # Check if any hour today reaches 99% SOC in the baseline simulation
            hit_full_before = any(v.get("soc", 0.0) >= 99.0 for h, v in sim_log.items() if ":" in h and "Завтра" not in h)
            is_solar_surplus = is_solar_surplus or hit_full_before

            # v11.7.280: Restore epochs for the allocator
            p_today = man.data.get("prices_sell", {})
            p_tomorrow = man.data.get("prices_sell_tomorrow", {})
            epochs = self.get_strategy_epochs(target_hours, p_today, p_tomorrow)
            first_epoch = epochs[0] if epochs else []

            # 1. Pre-calculate safety floors
            floors_sliding = {}
            floors_anchored = {}
            _sim_cons_profile = dict(man.get_predicted_profile("consumption_total"))
            
            morning_strict = min_soc_val + soc_buffer
            last_h_floor = morning_strict
            
            for hx in range(24):
                h_prof_debug[hx] = round_f(float(normalize_float(_sim_cons_profile.get(str(hx), 0.0))), 2)

            for h_sim in sim_range:
                h_sim_norm = h_sim % 24
                h_floor = min_soc_val
                
                is_morning_presale = bool(4 <= h_sim_norm < 11)
                can_bypass_floor = bool(is_morning_presale and (hit_full_before or is_solar_surplus))
                
                if not can_bypass_floor:
                    target_sunrise = sunrise_h if h_sim < sunrise_h else (sunrise_h + 24 if h_sim < sunrise_h + 24 else sunrise_h + 48)
                    h_rem_kwh = sum(float(normalize_float(_sim_cons_profile.get(str(hx % 24), 0.5))) for hx in range(h_sim, target_sunrise))
                    
                    bridge_val = (h_rem_kwh / b_cap * 100.0)
                    h_floor = max(min_soc_val + bridge_val, morning_strict)
                else:
                    h_floor = min_soc_val + 2.0
                
                h_floor = max(h_floor, user_limit, min_soc_val)
                
                # Sliding floor for simulation
                floors_sliding[h_sim] = float(h_floor)
                
                # Anchored floor for strategy (prevents selling survival buffers)
                floors_anchored[h_sim] = float(h_floor)

            # 2. Greedy Fill in Price-Descending order
            # v11.7.297: If solar surplus is huge, we don't care about the DC budget limit
            effective_budget_ac = 99.0 if is_solar_surplus else available_sell_ac
            h_by_priority = sorted(target_hours, key=lambda h: all_sell_prices.get(h, 0.0), reverse=True)
            
            for h_target in h_by_priority:
                if effective_budget_ac <= 0.05: break
                
                test_p = max_p
                if not is_solar_surplus:
                    test_p = min(max_p, effective_budget_ac)
                
                while test_p > 0.05:
                    test_commands = dict(sell_commands)
                    test_commands[h_target] = test_p
                    
                    _, trial_log, _ = self.run_soc_simulation(
                        b_soc, sim_range, now, 
                        commands={h: -p for h, p in test_commands.items()}, 
                        b_min_soc=min_soc_val, dynamic_floors=floors_anchored,
                        ignore_blended=True, house_profile_override="consumption_base"
                    )
                    
                    h_key = f"{h_target%24:02d}:59" + (" (Завтра)" if h_target >= 24 else "")
                    real_p = trial_log.get(h_key, {}).get("p_bat", 0.0)
                    
                    # Check if simulation accepted the power
                    is_ok = (real_p >= test_p - 0.1)
                    
                    if not is_ok and real_p > 0.1:
                        if is_solar_surplus:
                            # In solar surplus, always accept whatever we can discharge
                            is_ok = True
                            test_p = real_p 
                        else:
                            # Try one more time with exactly what the simulation allowed
                            test_p = real_p
                            continue
                    
                    if is_ok:
                        # v11.7.138: Saturation Check
                        for h_prev in sell_commands:
                            if sell_commands[h_prev] > 0.05:
                                h_prev_key = f"{h_prev%24:02d}:59" + (" (Завтра)" if h_prev >= 24 else "")
                                prev_real_p = trial_log.get(h_prev_key, {}).get("p_bat", 0.0)
                                if prev_real_p < sell_commands[h_prev] - 0.1:
                                    is_ok = False
                                    break
                    
                    if is_ok:
                        # Double Cycle Optimizer
                        if len(epochs) > 1 and not is_solar_surplus:
                            p1 = max([all_sell_prices.get(h, 0.0) for h in epochs[0]])
                            p2 = max([all_sell_prices.get(h, 0.0) for h in epochs[1]])
                            if p2 > p1 + 0.05:
                                # If this hour is in the first epoch, and second epoch is better, block it
                                if h_target in epochs[0]:
                                    is_ok = False
                                    if not limit_reason: limit_reason = "Ожидание пика"

                    if is_ok:
                        sell_commands[h_target] = test_p
                        sim_log = trial_log
                        if not is_solar_surplus:
                            effective_budget_ac -= test_p
                        break
                    else:
                        test_p = max(0.0, test_p - (max_p / 6.0))
                
                if test_p <= 0.05:
                    sell_commands[h_target] = 0.0

            house_rem_total = house_kwh_until_sunrise



            # --- Stage 4: Build Plan ---
            planned_results = {}
            sorted_h = sorted(sell_commands.keys())
            active_h = [h for h, p in sell_commands.items() if p > 0.05]
            limit_reason = "Активная продажа (Приоритет: Цена)" if active_h else "Ожидание пика"
            
            for h in sorted_h:
                p = sell_commands.get(h, 0.0)
                if p <= 0.05: continue
                
                h_sim_key = f"{h%24:02d}:59" + (" (Завтра)" if h >= 24 else "")
                sim_entry = sim_log.get(h_sim_key, {})
                real_p = float(sim_entry.get("p_bat", 0.0))
                sim_soc = float(sim_entry.get("soc", b_soc))
                
                # Diagnostics: Determine why we aren't selling at max_p
                if real_p < p - 0.1:
                    h_floor = floors.get(h, min_soc_val + soc_buffer)
                    if abs(sim_soc - user_limit) < 0.2:
                        limit_reason = "Лимит пользователя"
                    elif h_floor > min_soc_val + soc_buffer + 0.5:
                        limit_reason = "Gatekeeper"
                    else:
                        limit_reason = "Утренний лимит"
                
                planned_results[f"{h%24:02d}:00" + (" (Завтра)" if h >= 24 else "")] = {
                    "power": round_f(real_p, 3),
                    "soc": round_f(sim_soc, 1)
                }

            # 5. UI Diagnostics (v11.7.137: Restored missing variables)
            morning_h_abs = morning_h + (24 if cur_hour < morning_h else 0)
            morning_key = f"{morning_h%24:02d}:59" + (" (Завтра)" if morning_h_abs >= 24 else "")
            soc_morning = self._get_soc_from_log(sim_log, morning_key, b_soc)
            
            last_sell_h = max(active_h) if active_h else cur_hour
            last_h_key = f"{last_sell_h%24:02d}:59" + (" (Завтра)" if last_sell_h >= 24 else "")
            soc_end = self._get_soc_from_log(sim_log, last_h_key, b_soc)
            
            target_morning = (min_soc_val + 2.0) if (4 <= (morning_h % 24) < 10) else (min_soc_val + soc_buffer)
            # Gatekeeper: min_soc + house load until sunrise
            gatekeeper_val = floors_anchored.get(cur_hour, min_soc_val + soc_buffer)

            res.update({
                "planned_power_per_h": planned_results,
                "target_soc": round_f(active_safety_floor, 1),
                "recommended_power_kw": sell_commands.get(cur_hour, 0.0),
                "limit_used": user_limit,
                "target_price": target_price,
                "limit_reason": limit_reason,
                "target_morning": round_f(target_morning, 1),
                "gatekeeper_after_sale": round_f(gatekeeper_val, 1),
                "projected_soc_morning": round_f(soc_morning, 1),
                "projected_soc_after_sale": round_f(soc_end, 1)
            })
            
            # --- Stage 4: Final Simulation for UI (Projection) ---
            # v11.8.438: Run simulation with ALL commands and SLIDING floors for realistic UI charts
            _, sim_log_final, _ = self.run_soc_simulation(
                b_soc, sim_range, now, 
                commands={h: -p for h, p in sell_commands.items()}, 
                b_min_soc=min_soc_val, dynamic_floors=floors_sliding,
                ignore_blended=True, house_profile_override="consumption_base"
            )
            sim_log = sim_log_final # Use this for all UI displays below

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
            
            # v11.7.131: Strictest limit identification for UI
            overall_limit = ""
            if not sell_commands:
                overall_limit = "Цена"
            else:
                # Which floor actually won the max() in Stage 1?
                if user_limit >= max(gatekeeper_floor, morning_reserve_floor):
                    overall_limit = "Лимит пользователя"
                elif gatekeeper_floor >= morning_reserve_floor:
                    overall_limit = "Gatekeeper"
                else:
                    overall_limit = "Утренний лимит"
            
            # v11.7.131: Final status building
            if cur_hour in active_h:
                p_now = sell_commands.get(cur_hour, 0.0)
                # 1. Inverter priority
                if p_now >= max_p - 0.1:
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
            prof_cons_debug = "|".join([f"{h%24}:{(float(prof_cons_tom.get(str(h%24), 0.0)) if h>=24 else float(prof_cons_cur.get(str(h%24), 0.0))):.1f}" for h in range(cur_hour, cur_hour + 12)])

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
            morning_h_abs = sunrise_h if cur_hour < sunrise_h else (sunrise_h + 24)
            first_sell_h = min(active_h) if active_h else cur_hour
            last_sell_h = max(active_h) if active_h else cur_hour
            
            res["sell_simulation"] = {
                "hit_full_before": hit_full_before,
                "projected_soc_at_sale_start_pct": round_f(self._get_soc_from_log(sim_log, f"{(first_sell_h-1)%24:02d}:59" + (" (Завтра)" if (first_sell_h-1) >= 24 else ""), b_soc), 1),
                "projected_soc_after_sale_pct": round_f(self._get_soc_from_log(sim_log, f"{last_sell_h%24:02d}:59" + (" (Завтра)" if last_sell_h >= 24 else ""), b_soc), 1),
                "projected_soc_morning_pct": round_f(self._get_soc_from_log(sim_log, f"{morning_h_abs%24:02d}:59" + (" (Завтра)" if morning_h_abs >= 24 else ""), b_soc), 1),
                "log": sim_log
            }

            res.update({
                "gatekeeper_floor": round_f(gatekeeper_floor, 1),
                "survival_target": round_f(morning_reserve_floor, 1),
                "target_price": round_f(target_price, 3),
                "limit_used": user_limit,
                "active_hours": active_h,
                "raw_commands": sell_commands
            })

            res["arbitrage_sell_debug"] = {
                "start_soc": b_soc,
                "gatekeeper_floor": round_f(gatekeeper_floor, 1),
                "active_safety_floor": round_f(active_safety_floor, 1),
                "survival_target": round_f(morning_reserve_floor, 1),
                "available_ac": round_f(available_sell_ac, 2),
                "limit_reason": limit_reason or "None",
                "next_peak": f"{target_hours[0] % 24:02d}:00" if target_hours else "None",
                "soc_at_peak": f"{b_soc:.1f}%", 
                "house_until_sunrise": round_f(house_kwh_until_sunrise, 2),
                "house_h": prof_cons_debug,
                "sim_gen": round_f(sum(float(v.get('gen', 0)) for v in sim_log.values()), 1),
                "sim_log": " | ".join(debug_log_parts),
                "final_targets": str(target_hours),
                "f_today": f_today,
                "f_tom": f_tom_val,
                "target_price": round_f(target_price, 3),
                "cur_p": cur_p_f,
                "house_profile_debug": h_prof_debug,
                "commands": {f"{h}h": p for h, p in sell_commands.items()}
            }

            if sell_commands.get(cur_hour, 0.0) > 0.05:
                res["state"] = "active"
                res["current_mode_text"] = "Активная продажа"
            else:
                res["current_mode_text"] = "Ожидание пика" if target_hours else "Нет ценового окна"

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
