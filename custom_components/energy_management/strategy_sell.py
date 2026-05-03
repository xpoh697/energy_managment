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
            "charge_reason": "none",
            "strategy_candidates": [],
            "raw_commands": {}
        }
        
        old_calc = bool(getattr(self, "_calculating_strategy", False))
        self._calculating_strategy = True
        
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
                safe_peaks = []
                for h, p in tech_peaks:
                    if h < cur_hour: continue
                    is_ok, _ = is_profitable(p, h)
                    if p >= sell_limit or is_ok or surplus_dc > 0.1:
                        safe_peaks.append(h)
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
            
            # Initial potential budget (max discharge possible until morning floor)
            available_sell_dc = max(0.0, (b_soc - 15.0) * b_cap / 100.0)
            available_sell_ac = max(0.0, available_sell_dc * eff)
            limit_reason = ""

                    

            # --- Stage 1: Strategic Thresholds (TS 6.1.1) ---
            # Gatekeeper = Emergency Reserve (18%) + House load kWh until sunrise
            survival_reserve = min_soc_val + soc_buffer # 18.0%
            morning_h = man.get_sunrise_hour() or 8
            morning_h_abs = morning_h + 24 if now.hour >= 18 else morning_h
            morning_key = f"{morning_h%24:02d}:59" + (" (Завтра)" if morning_h_abs >= 24 else "")
            
            # Sum up kWh from consumption_base profile
            prof_cons_cur = dict(man.get_average_profile("consumption_base", 7, now.weekday()))
            prof_cons_tom = dict(man.get_average_profile("consumption_base", 7, (now.weekday() + 1) % 7))
            
            # --- TS 6.1.1 Gatekeeper Window Logic ---
            is_morning_window = (4 <= (cur_hour % 24) < 10)
            
            if is_morning_window:
                # Morning: Hard Floor 15% (min_soc + 2%), NO Gatekeeper
                base_target = min_soc_val + 2.0
                limit_reason = "Morning Floor"
            else:
                # v11.7.35: Extended range to 10:00 AM to cover the whole morning window
                occ_coeff, _, _, _, _, _, _ = man.get_occupancy_coefficient()
                occ_coeff = float(occ_coeff)
                morning_window_end_abs = (10 + 24) if cur_hour >= 18 else 10
                
                # Calculate house load until sunrise (for survival) but plan until end of window
                house_kwh_until_sunrise = 0.0
                for h_abs in range(cur_hour, morning_window_end_abs):
                    h_rel = str(h_abs % 24)
                    p_cons = prof_cons_tom if h_abs >= 24 else prof_cons_cur
                    h_load = float(normalize_float(p_cons.get(h_rel, 0.4))) * occ_coeff
                    
                    step = 1.0
                    if h_abs == cur_hour:
                        step = 1.0 - (now.minute / 60.0)
                    
                    # Only count towards survival reserve until actual sunrise
                    if h_abs < morning_h_abs:
                        house_kwh_until_sunrise += h_load * step
                  
                gatekeeper_floor = survival_reserve + (house_kwh_until_sunrise / b_cap * 100.0)
                base_target = max(active_floor, gatekeeper_floor)

            # --- Stage 2: Budget Reductions (Solar Deficit & Next Peak) ---
            # Rule A: Solar Deficit (Protection for weak Day 2 generation)
            f_tom = float(man.get_forecast_value(man.forecast_tomorrow_sensor) or 0.0)
            tomorrow_morning_h = morning_h + 24
            next_morning_h = morning_h + 48
            # Total house need tomorrow until Day 3 morning
            tomorrow_house_need_kwh = sum(float(normalize_float(prof_cons_tom.get(str(h % 24), 0.0))) for h in range(tomorrow_morning_h, next_morning_h))
            
            solar_deficit_kwh_ac = max(0.0, tomorrow_house_need_kwh - f_tom)
            if solar_deficit_kwh_ac > 0.1:
                available_sell_ac = max(0.0, available_sell_ac - solar_deficit_kwh_ac)
                if not limit_reason: limit_reason = "Solar Deficit"

            # Rule B: Next Peak Protection (TS 4.1.4 / 6.1.1)
            # Find the best peak in the next 48h that has a BETTER price than now
            future_peaks = [h for h, p in all_sell_prices.items() if h > cur_hour and p > cur_p_f + 0.01]
            if future_peaks:
                best_peak_h = max(future_peaks, key=lambda h: all_sell_prices.get(h, 0.0))
                best_peak_p = all_sell_prices.get(best_peak_h, 0.0)
                
                # Simulation until that peak to see if we reach 100%
                _, sim_log_peak, _ = self.run_soc_simulation(
                    start_soc=b_soc,
                    sim_range=list(range(cur_hour, best_peak_h + 1)),
                    now=now,
                    commands={},
                    house_profile_override="consumption_base",
                    ignore_blended=True
                )
                if sim_log_peak:
                    peak_key = f"{best_peak_h%24:02d}:59"
                    if best_peak_h >= 48: peak_key += " (Через день)"
                    elif best_peak_h >= 24: peak_key += " (Завтра)"
                    
                    soc_at_peak = self._get_soc_from_log(sim_log_peak, peak_key, b_soc)
                    
                    # NEW v11.7.67: Check if we hit 100% ANYTIME before the peak
                    hit_full_before = False
                    for k, v in sim_log_peak.items():
                        if isinstance(v, dict) and v.get("soc", 0.0) >= 99.9:
                            hit_full_before = True
                            break
                    
                    target_soc_at_peak = 100.0
                    if soc_at_peak < target_soc_at_peak - 0.5 and not hit_full_before:
                        deficit_kwh = (target_soc_at_peak - soc_at_peak) * b_cap / 100.0
                        available_sell_ac = max(0.0, available_sell_ac - (deficit_kwh * eff))
                        limit_reason = f"Peak Prep {best_peak_h%24:02d}:00 ({round_f(best_peak_p,2)})"
                        next_peak_h = best_peak_h 
                    else:
                        next_peak_h = best_peak_h 
                        if hit_full_before:
                            limit_reason = "None (Will hit 100% anyway)"

            # --- Stage 3: Recursive Jeweler Loop ---
            first_epoch_hours = epochs[0] if epochs else []
            target_hours_by_price = sorted(first_epoch_hours, key=lambda h: all_sell_prices.get(h, 0.0), reverse=True)
            last_sell_h = max(first_epoch_hours) if first_epoch_hours else cur_hour
            
            current_budget_ac = available_sell_ac
            sell_commands = {}
            sim_log = {} # Fresh log for this calculation
            
            for attempt in range(5):
                temp_commands = {}
                rem_ac = current_budget_ac
                for h in target_hours_by_price:
                    if rem_ac <= 0.01: break
                    h_f = max(0.1, (60 - now.minute)/60.0) if h == cur_hour else 1.0
                    p_alloc = min(max_p, rem_ac / h_f)
                    temp_commands[h] = round_f(p_alloc, 3)
                    rem_ac = max(0.0, rem_ac - (p_alloc * h_f))
                
                sell_commands = temp_commands
                sim_commands_neg = {h: -p for h, p in sell_commands.items()}
                sim_range = list(range(cur_hour, morning_window_end_abs + 1))
                
                res_soc, sim_log, _ = self.run_soc_simulation(
                    start_soc=b_soc,
                    sim_range=list(range(cur_hour, cur_hour + 48)),
                    now=now,
                    commands=sim_commands_neg,
                    house_profile_override="consumption_base",
                    ignore_blended=True,
                    attempt=attempt # v11.7.56
                )
                
                # v11.7.54: Raw Log Dump
                _LOGGER.warning(f"[SimLogDump] Hour {cur_hour}: {sim_log.get('00:59', 'N/A')}")
                
                # Check end-of-sale SOC
                last_h_key = f"{last_sell_h%24:02d}:59" + (" (Завтра)" if last_sell_h >= 24 else "")
                end_of_sale_soc = self._get_soc_from_log(sim_log, last_h_key, b_soc)
                
                # Check Sunrise SOC
                sunrise_soc = self._get_soc_from_log(sim_log, morning_key, b_soc)
                
                # Dynamic targets
                is_last_h_morning = (4 <= (last_sell_h % 24) < 10)
                target_at_end = (min_soc_val + 2.0) if is_last_h_morning else base_target
                
                # Deficit: Must satisfy both end-of-sale target AND sunrise survival
                d_end = target_at_end - end_of_sale_soc
                d_morning = survival_reserve - sunrise_soc
                
                # v11.7.27: Anti-clamping penalty. If we hit 0%, assume we need more reduction.
                if sunrise_soc < 0.1: d_morning += 5.0 
                if end_of_sale_soc < 0.1: d_end += 5.0
                
                total_deficit_soc = max(d_end, d_morning)
                
                if total_deficit_soc > 0.1:
                    reduction_ac = (total_deficit_soc * b_cap / 100.0) * eff
                    current_budget_ac = max(0.0, current_budget_ac - reduction_ac)
                    if not limit_reason: 
                        limit_reason = "Gatekeeper" if d_morning > d_end else "User"
                else:
                    break

            # 4. Build the hourly plan (Honest Simulation)
            planned_results = {}
            sorted_h_keys = sorted(sell_commands.keys())
            for h in sorted_h_keys:
                p = sell_commands[h]
                if p <= 0: continue
                h_fmt = f"{h%24:02d}:00"
                if h >= 24: h_fmt += " (Завтра)"
                
                h_sim_key = f"{h%24:02d}:59" + (" (Завтра)" if h >= 24 else "")
                h_soc = self._get_soc_from_log(sim_log, h_sim_key, b_soc)
                
                planned_results[h_fmt] = {
                    "power": round_f(p, 3),
                    "soc": round_f(h_soc, 1)
                }

            # 5. UI Diagnostics
            res["planned_power_per_h"] = planned_results
            res["target_soc"] = round_f(active_floor, 1)
            res["recommended_power_kw"] = sell_commands.get(cur_hour, 0.0)
            res["limit_used"] = sell_limit
            res["target_price"] = target_price
            res["gatekeeper_floor"] = round_f(gatekeeper_floor, 1) if not is_morning_window else 15.0
            res["strategy_candidates"] = [f"{h%24:02d}:00" + (" (Завтра)" if h >= 24 else "") for h in target_hours]
            active_h = [h for h, p in sell_commands.items() if p > 0.05]
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
                "log": sim_log
            }
            res["raw_commands"] = sell_commands
            
            v_val = 52.0
            if man.battery_voltage_sensor:
                v_val = float(man.get_sensor_float(man.battery_voltage_sensor) or 52.0)
            res["recommended_amps"] = round_f((sell_commands.get(cur_hour, 0.0) * 1000.0) / v_val, 1) if v_val > 0 else 0.0
            
            res["arbitrage_decision"] = f"Продажа по {cur_p_f:.2f}" if cur_hour in active_h else "Ожидание пика"
            
            # v11.7.20 Limit Reason Reporting
            # Priority: Solar Deficit > Next Peak > Gatekeeper > User > Inverter
            overall_limit = limit_reason if limit_reason else ("User" if current_budget_ac < available_sell_ac - 0.1 else "")
            
            if cur_hour in active_h:
                p_now = sell_commands.get(cur_hour, 0.0)
                if overall_limit:
                    res["power_decision"] = f"Лимит: {overall_limit}"
                    if p_now >= max_p - 0.05:
                        res["power_decision"] += " (Инвертор)"
                elif p_now >= max_p - 0.05:
                    res["power_decision"] = "Лимит: Инвертор"
                else:
                    res["power_decision"] = "Активно"
            else:
                res["power_decision"] = "Ожидание пика"
            
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
                    gen = val.get('gen', val.get('gen_kw', 0.0))
                    load = val.get('load', val.get('load_kw', 0.0))
                    cmd = sell_commands.get(h, 0.0)
                    net = gen - load - cmd
                    debug_log_parts.append(f"{h_rel}: {val.get('soc', 0):.0f}% ({net:.1f}k)")
                else:
                    debug_log_parts.append(f"{h_rel}: ---")

            res["arbitrage_sell_debug"] = {
                "start_soc": b_soc,
                "base_target": round_f(base_target, 1),
                "available_ac": round_f(current_budget_ac, 2),
                "limit_reason": limit_reason or "None",
                "next_peak": f"{next_peak_h % 24:02d}:00" if next_peak_h >= 0 else "None",
                "soc_at_peak": f"{soc_at_peak:.1f}%" if 'soc_at_peak' in locals() else "N/A",
                "house_until_sunrise": round_f(house_kwh_until_sunrise, 2),
                "house_h": prof_cons_debug,
                "sim_gen": round_f(sum(float(v.get('gen', 0)) for v in sim_log.values()), 1),
                "sim_log": " | ".join(debug_log_parts),
                "final_targets": str(target_hours),
                "f_today": f_today,
                "f_tom": f_tom_val,
                "target_price": round_f(target_price, 3) if current_budget_ac > 0.01 else 0.0,
                "cur_p": cur_p_f,
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
