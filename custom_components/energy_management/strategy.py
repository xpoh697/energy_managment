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
    CONF_MIN_SOC_BUY,
    CONF_ACTIVE_SENSOR,
    CONF_IS_CYCLIC,
    CONF_ONLY_SOLAR,
    CONF_PRICE_BUY_LIMIT,
    CONF_PRICE_SELL_LIMIT,
    CONF_PRICE_TOLERANCE,
    CONF_PRICE_SELL_TOLERANCE,
    CONF_BATTERY_MAX_POWER,
    CONF_FORCE_MARKET_SELL,
    CONF_ARBITRAGE_MIN_PROFIT,
    CONF_TARGET_SOC_SELL,
    CONF_TARGET_SOC_BUY,
    CONF_DYNAMIC_SOC_SELL,
    CONF_DYNAMIC_SOC_BUY,
    CONF_PRIORITY,
    CONF_SOC_BUFFER,
    DOMAIN
)
from .utils import get_kwh_val, normalize_float, get_price_from_store, round_f

# Legacy aliases for safety during refactoring synchronization
_get_kwh_val = get_kwh_val
_normalize_float = normalize_float

_LOGGER = logging.getLogger(__name__)



# Market Strategy Engine v5.1 - Fixed NameError
class StrategyEngine:
    """Mathematical engine for energy management strategies and simulations."""
    manager: 'EnergyProfileManager'
    
    def __init__(self, manager: 'EnergyProfileManager'):
        self.manager = manager
        self._strategy_cache = {}
        self._calculating_strategy = False

    @staticmethod
    def get_cc_cv_ratio(soc):
        """Strict CC/CV ratio based on user-provided table (v6.11).
        - 20-95%: 100% power
        - 95-97%: 30-50% (avg 40%)
        - 98-99%: 10-15% (avg 12.5%)
        - 100%: 0%
        """
        if soc >= 100: return 0.0
        if soc >= 98: return 0.125
        if soc >= 95: return 0.40
        return 1.0 # 20-95% range
    @staticmethod
    def _format_h(h_abs):
        if h_abs is None: return "Нет данных"
        d = "Завтра " if h_abs >= 24 else ""
        return f"{d}{h_abs % 24:02d}:00"

    def get_battery_degradation_cost(self):
        """Cost of battery wear per kWh (Cycle Cost). Syncs with UI sensor."""
        batt_cost = self.manager.get_setting(CONF_BATTERY_COST, 0.0)
        cycles = self.manager.get_setting(CONF_BATTERY_RATED_CYCLES, 6000)
        
        # Pull battery capacity once
        _, cap, _ = self.manager.get_battery_state()
        if cap <= 1.0: cap = 10.0 # Safety default
        
        if cycles <= 0 or batt_cost <= 0: return 0.0
        # Formula: total_cost / (total_cycles * total_capacity)
        return round_f(batt_cost / (cycles * cap), 4)

    def get_efficiency_coefficient(self) -> float:
        """Calculates historical inverter efficiency (Smart filtering for High Power)."""
        man: Any = self.manager
        d_store = getattr(man, "data", {})
        if not isinstance(d_store, dict): return 0.95

        l_map = d_store.get("losses", {})
        if not isinstance(l_map, dict): return 0.95
            
        sum_g = 0.0
        sum_l = 0.0
        smp_count = 0
        
        # Rule: We only count samples where Generation was > 1kW 
        # to avoid standby-power bias (where 0.3kW loss on 0.5kW gen makes eff look like 40%)
        # Arbitrage happens at high power (5kW), so we need High-Power Efficiency.
        for h_idx in range(24):
            recs = l_map.get(str(h_idx), [])
            if not isinstance(recs, list): continue
            
            for item in recs[-14:]: # Last 14 days
                if not isinstance(item, dict): continue
                g_val = float(normalize_float(item.get("gen", 0.0)))
                l_val = float(normalize_float(item.get("v", 0.0)))
                
                # Rule: Only samples with > 1.0 kW generation (representing significant activity)
                if g_val > 1.0:
                    sum_g += g_val
                    sum_l += l_val
                    smp_count += 1
        
        if smp_count < 3 or sum_g < 1.0:
            return 0.95 # Reasonable modern inverter default
            
        eff_ratio = float((sum_g - sum_l) / sum_g)
        # Never allow less than 85% or more than 99%
        return float(max(0.85, min(0.99, eff_ratio)))

    # --- REFACTOR v6.2 MODULAR HELPERS ---

    def _get_sunrise_baseline_soc(self, current_soc, now, sunrise_h, best_buy_pair, all_buy_prices, threshold, eff, deg_cost, max_p):
        """Runs a baseline simulation to end-of-night without any selling."""
        cur_hour = now.hour
        # 1. Run Baseline Simulation (including profitable buy-backs before sunrise)
        sim_end_h = 24 + sunrise_h
        sim_range = range(cur_hour, sim_end_h)
        
        # Add predicted buy-backs to the baseline so we 'see' them in the morning projection
        baseline_commands = {}
        if best_buy_pair[1] is not None and best_buy_pair[1] < sunrise_h:
            for h_b, p_b in all_buy_prices.items():
                if h_b < sunrise_h and h_b > cur_hour:
                    # If this hour is profitable (Gain >= threshold)
                    # Note: We use a simplified check for baseline inclusion
                    baseline_commands[int(h_b)] = float(max_p)
        
        _, baseline_log, _ = self.run_soc_simulation(current_soc, sim_range, now, baseline_commands)
        
        # Find natural SOC at sunrise
        natural_morning_soc = current_soc
        if baseline_log:
            key_morning_sim = f"{sunrise_h-1:02d}:59 (Завтра)"
            natural_morning_soc = self._get_soc_from_log(baseline_log, key_morning_sim, current_soc)
        
        return natural_morning_soc

    def _calculate_sunrise_surplus(self, natural_morning_soc, min_soc, buffer_soc, batt_cap, eff):
        """Strictly calculates surplus above the safety mark (e.g. 28%)."""
        target_morning_soc = float(min_soc + buffer_soc)
        extra_soc_pct = max(0.0, natural_morning_soc - target_morning_soc)
        return float((extra_soc_pct * batt_cap / 100.0) * eff)

    def _calc_immediate_safety_floor(self, min_soc, active_buffer, total_cons_to_sunrise, base_deficit_tomorrow, total_solar_to_sunrise, batt_cap, eff):
        """The 'Gatekeeper' floor for current hour selling."""
        active_floor_soc = float(min_soc + active_buffer)
        # Coverage for essential needs until sunrise
        res_cons_base_dc = max(0.0, (total_cons_to_sunrise + base_deficit_tomorrow) / eff - (total_solar_to_sunrise / 0.98))
        return active_floor_soc + (res_cons_base_dc / batt_cap * 100.0)

    def get_hourly_accuracy_coeff(self, hour):
        """Calculates specific historical accuracy for a given hour of day (v/f)."""
        man = self.manager
        sh = str(hour)
        history = man.data.get("generation", {}).get(sh, [])
        if not history:
            return 1.0, 0
            
        # Use last 14 days for a stable profile
        perf_list = []
        for rec in history[-14:]:
            if not isinstance(rec, dict): continue
            # v7.7 - Skip records where generation was curtailed (c=True)
            if rec.get("c"): continue
            
            v = float(rec.get("v", 0.0))
            f = float(rec.get("f", 0.0))
            if f > 0.1:
                # Clamp per-hour ratio to avoid outliers (0.2x to 2.0x)
                perf_list.append(max(0.2, min(v / f, 2.0)))
        
        if not perf_list:
            return 1.0, 0
            
        # Standard average
        return float(sum(perf_list) / len(perf_list)), len(perf_list)

    def get_gen_forecast_coefficient(self, forecast_value: float, prof_gen: dict, hour_start: int, hour_end: int) -> float:
        if not forecast_value or forecast_value <= 0.1:
            return 1.0
        
        p = prof_gen or {}
        avg_gen_sum = sum(float(normalize_float(p.get(str(h), 0.0))) for h in range(hour_start, hour_end))
        if avg_gen_sum <= 0.1:
            return 1.0
        return float(forecast_value / avg_gen_sum)
        if avg_gen_sum <= 0.1: return 1.0
        return max(0.2, min(forecast_value / avg_gen_sum, 2.0))

    def run_investment_simulation(self, extra_batt_kwh=0.0, pv_multiplier=1.0):
        """Simulate last 30 days with modified system specs to predict extra savings."""
        now = dt_util.now()
        
        # We look back at available history (up to 30 days)
        max_idx = 0
        for h in range(24):
            max_idx = max(max_idx, len(self.manager.data.get("consumption_total", {}).get(str(h), [])))
        
        days_to_sim = min(30, max_idx - 1)
        if days_to_sim <= 0:
            return {
                "sell_simulation": {},
                "arbitrage_buyback": {},
                "analyzed_window": "Неизвестно",
                "monthly_estimate": 0.0
            }

        man: Any = self.manager
        eff = float(self.get_efficiency_coefficient())
        
        b_soc, b_cap, _ = man.get_battery_state()
        sim_batt_cap = float(b_cap + extra_batt_kwh)
        max_batt_p = float(man.get_setting(CONF_BATTERY_MAX_POWER, 5.0))
        
        total_extra_saved = 0.0
        actual_baseline_savings = 0.0
        hours_simulated = 0
        days_with_data = 0
        
        for d_back in range(1, days_to_sim + 1):
            sim_soc = 50.0 
            day_has_data = False
            day_sim_saved = 0.0
            
            for h_idx in range(24):
                c_h_rec = man.data.get("consumption_total", {}).get(str(h_idx), [])
                g_h_rec = man.data.get("generation", {}).get(str(h_idx), [])
                
                date_str = (now - timedelta(days=d_back)).strftime("%Y-%m-%d")
                p_buy = float(man.get_price("buy", date_str, h_idx) or 0.0)
                p_sell = float(man.get_price("sell", date_str, h_idx) or 0.0)
                
                if p_buy <= 0: # Skip hours without prices
                    continue
                
                if d_back > len(c_h_rec) or d_back > len(g_h_rec):
                    continue
                
                try:
                    c_h = float(normalize_float(c_h_rec[-d_back].get("v") if isinstance(c_h_rec[-d_back], dict) else c_h_rec[-d_back]))
                    g_h = float(normalize_float(g_h_rec[-d_back].get("v") if isinstance(g_h_rec[-d_back], dict) else g_h_rec[-d_back])) * pv_multiplier
                except (IndexError, AttributeError):
                    continue
                
                net = float(g_h - c_h)
                sim_cost = 0.0
                
                if net > 0:
                    charge_kw = float(min(net * eff, max_batt_p))
                    if sim_batt_cap > 0.001:
                        sim_soc = float(min(100.0, sim_soc + (charge_kw / sim_batt_cap * 100.0)))
                else:
                    needed = float(abs(net))
                    from_batt = float(min(needed, sim_soc * sim_batt_cap / 100.0) if sim_batt_cap > 0.001 else 0.0)
                    from_batt_ac = float(from_batt * eff)
                    
                    if sim_batt_cap > 0.001:
                        sim_soc = float(max(0.0, sim_soc - (from_batt / sim_batt_cap * 100.0)))
                    
                    sim_cost = float(max(0.0, needed - from_batt_ac) * p_buy)
                
                # Add solar selling profit if any
                excess = float(max(0.0, net - (charge_kw / eff if net > 0 else 0.0)))
                sell_profit = float(excess * p_sell)
                
                day_sim_saved += float((c_h * p_buy) - sim_cost + sell_profit)
                day_has_data = True
                hours_simulated += 1

            if day_has_data:
                total_extra_saved += day_sim_saved
                days_with_data += 1
                
                # Simulated baseline with EXISTING battery
                day_baseline_saved = 0.0
                sim_soc_base = 50.0
                for h_idx_b in range(24):
                    try:
                        c_h_b = float(normalize_float(c_h_rec[-d_back].get("v") if isinstance(c_h_rec[-d_back], dict) else c_h_rec[-d_back]))
                        g_h_b = float(normalize_float(g_h_rec[-d_back].get("v") if isinstance(g_h_rec[-d_back], dict) else g_h_rec[-d_back])) * pv_multiplier
                        
                        net_b = g_h_b - c_h_b
                        cost_b = 0.0
                        if net_b > 0:
                            ch_b = min(net_b * eff, max_batt_p)
                            if b_cap > 0.1: sim_soc_base = min(100.0, sim_soc_base + (ch_b / b_cap * 100.0))
                            cost_b = -max(0.0, net_b - (ch_b / eff)) * p_sell # Income
                        else:
                            nd_b = abs(net_b)
                            fb_b = min(nd_b, sim_soc_base * b_cap / 100.0) if b_cap > 0.1 else 0.0
                            if b_cap > 0.1: sim_soc_base = max(0.0, sim_soc_base - (fb_b / b_cap * 100.0))
                            cost_b = max(0.0, nd_b - (fb_b * eff)) * p_buy
                        day_baseline_saved += (c_h_b * p_buy) - cost_b
                    except: continue
                actual_baseline_savings += day_baseline_saved

        improvement = max(0.0, total_extra_saved - actual_baseline_savings)
        return {
            "days_simulated": days_with_data,
            "extra_savings": round_f(improvement, 2),
            "monthly_estimate": round_f(improvement * (30 / days_with_data), 2) if days_with_data > 0 else 0.0
        }

    def get_budget_and_permissions(self, days_for_profile=14, skip_strategy_check=False):
        """Analyze current day state and return permissions for heavy loads."""
        man: Any = self.manager
        now = dt_util.now()
        cur_hour = int(now.hour)
        
        if self._calculating_strategy and not skip_strategy_check:
            skip_strategy_check = True

        old_calc = bool(self._calculating_strategy)
        self._calculating_strategy = True
        try:
            # 1. Solar adjustment
            raw_f = man.get_forecast_value(man.forecast_today_sensor)
            forecast_val = float(raw_f) if raw_f is not None else 0.0
            
            # v5.2 - Dynamic Period Adaptability (Fast Learning in Transition Seasons)
            # March, April, Sept, Oct are transition seasons for solar
            curr_month = now.month
            eff_period = days_for_profile
            if curr_month in [3, 4, 9, 10]:
                eff_period = 7 # Accelerated learning
                
            day_idx = man.day_type
            p_gen = dict(man.get_average_profile("generation", eff_period, "all"))
            
            dist = man.get_forecast_hourly_distribution(man.forecast_today_hourly_sensor)
            dist_source = "historical"
            if dist:
                dist_source = "forecast_hourly"
                # Use Solcast curve if available
                hist_gen_so_far = float(sum(float(dist.get(str(h), 0.0)) for h in range(cur_hour + 1)))
                total_hist_gen = float(sum(float(dist.get(str(h), 0.0)) for h in range(24)))
                active_dist = dist
            else:
                hist_gen_so_far = float(sum(float(normalize_float(p_gen.get(str(h), 0.0))) for h in range(cur_hour + 1)))
                total_hist_gen = float(sum(float(normalize_float(p_gen.get(str(h), 0.0))) for h in range(24)))
                active_dist = p_gen
            
            # --- Improved Performance Coefficients (v4.0 + Hourly Awareness v7.4) ---
            # A. Calculate Historical Average Performance for the REMAINING part of the day
            # This captures if, say, Solcast always underestimates mornings but overestimates evenings.
            
            # v7.6 - Weighted historical coefficient (avoids jumps at hour boundaries)
            dist = man.get_forecast_hourly_distribution(man.forecast_today_hourly_sensor)
            rem_hours = range(cur_hour, 24)
            
            top_h = 0.0
            bot_h = 0.0
            for h in rem_hours:
                acc, _ = self.get_hourly_accuracy_coeff(h)
                weight = float(dist.get(str(h), 0.0) if dist else 0.0)
                top_h += acc * weight
                bot_h += weight
            
            if bot_h > 0.01:
                hist_coeff = float(top_h / bot_h)
            else:
                # Fallback to simple average (at night or if dist empty)
                rem_accs_data = [self.get_hourly_accuracy_coeff(h) for h in rem_hours]
                rem_accs = [d[0] for d in rem_accs_data if d[0] is not None]
                hist_coeff = float(sum(rem_accs) / len(rem_accs)) if rem_accs else 1.0
            
            # Debug info for the current hour specifically
            h_acc_cur, h_count_cur = self.get_hourly_accuracy_coeff(cur_hour)

            actual_today = float(man.data.get("temp_daily_gen", 0.0) or 0.0)
            
            fraction_so_far = float(hist_gen_so_far / total_hist_gen) if total_hist_gen > 0.1 else 0.0
            predicted_total = float(actual_today + forecast_val)
            
            # temp_max_forecast: High-water mark for the day's forecast
            if predicted_total > (self.manager.data.get("temp_max_forecast", 0.0) or 0.0):
                self.manager.data["temp_max_forecast"] = float(predicted_total)
            
            expected_today_total = float(man.data.get("temp_max_forecast", 0.0) or 0.1)
            
            # B. Today's Performance (Current Estimate vs Morning Promise)
            # This avoids 'timing shift' errors because it doesn't care about the hourly distribution curves.
            today_coeff = 1.0
            if expected_today_total > 0.5:
                today_coeff = float(max(0.2, min(predicted_total / expected_today_total, 2.0)))
            
            # --- Curtailment Correction (v4.2) ---
            # The inverter only chokes PV panels in 'stop_sale' mode if there is "no room for energy"
            # (i.e., battery is full or nearly full).
            is_stop_sale = getattr(man, "current_inverter_mode", "") == "stop_sale"
            if is_stop_sale and today_coeff < 1.0:
                # Check if battery is full enough to cause curtailment
                b_soc_cur, _, _ = man.get_battery_state()
                if b_soc_cur > 95:
                    # We suspect curtailment because export is forbidden AND battery is full.
                    # In this case, frozen high-water performance (or at least 1.0/history) is used.
                    old_today = today_coeff
                    today_coeff = max(today_coeff, hist_coeff, 1.0)
                    if abs(today_coeff - old_today) > 0.01:
                        _LOGGER.debug(f"[Strategy] Curtailment detected (mode=stop_sale, SOC={b_soc_cur}%). Corrected today_coeff: {old_today:.2f} -> {today_coeff:.2f}")

            # C. Blended Coeff: Weighted average of Today vs History
            # We trust today's data more as the day progresses (using the external forecast's own timing).
            external_progress = 1.0 - (forecast_val / expected_today_total) if expected_today_total > 0.1 else fraction_so_far
            external_progress = max(0.0, min(external_progress, 1.0))
            
            # v7.6.1 - Correct blended multiplier: We blend today's consistency with 1.0 baseline,
            # because historical bias (h_acc) is handled per-hour in simulation steps.
            blended_coeff = float((today_coeff * external_progress) + (1.0 * (1.0 - external_progress)))
            
            # Safety guards
            blended_coeff = float(max(0.3, min(blended_coeff, 1.5)))
            
            man.last_blended_coeff = float(blended_coeff)
            forecast_val_adjusted = float(forecast_val * blended_coeff)
                
            # 2. Battery state
            batt_soc, batt_cap, batt_energy_val = man.get_battery_state()
            b_soc_f = float(batt_soc)
            b_cap_f = float(batt_cap)
            b_energy_f = float(batt_energy_val)
            
            min_soc_val = man.get_setting(CONF_MIN_SOC_BUY, 10.0)
            min_soc = float(min_soc_val) if min_soc_val is not None else 10.0
            eff_coeff = float(self.get_efficiency_coefficient() or 1.0)
                        
            # 3. Expected consumption (v7.9.4 - Base profile + Simulation Guard)
            # Use 'base' profile as the absolute essential house survival floor.
            occ_coeff = float(man.get_occupancy_coefficient())
            sunrise_hour = man.get_sunrise_hour() or 6
            base_rem_today = float(man.get_expected_remaining("consumption_base", eff_period, day_idx)) * occ_coeff
            base_night = float(man.get_expected_night("consumption_base", eff_period, day_idx, until_hour=sunrise_hour)) * occ_coeff
            expected_base_consumption = float(base_rem_today + base_night)
            
            # v7.9.4 - Survival Projection Gate
            # We check if even WITH just the base load, we can reach morning safely.
            soc_buffer = float(man.get_setting(CONF_SOC_BUFFER, 15.0))
            survival_threshold = min_soc + soc_buffer
            
            # Quick 24h simulation (baseline only) to find projected morning SOC
            # We need to reach the next sunrise (approx 6-8 AM)
            sim_range = list(range(cur_hour, cur_hour + 24))
            sim_res_soc, sim_log, overflow_kwh = self.run_soc_simulation(
                start_soc=b_soc_f,
                sim_range=sim_range,
                now=now,
                house_profile_override="consumption_base"
            )
            # Find the SOC at the start of tomorrow's generation (sunrise)
            projected_morning_soc = 0.0
            sunrise_h = 8 # Default
            prof_gen = man.get_average_profile("generation", eff_period, day_idx)
            for h in range(24):
                if float(prof_gen.get(str(h), 0.0)) > 0.05:
                    sunrise_h = h
                    break
            
            # Sunrise tomorrow is at 24 + sunrise_h
            morning_h_abs = 24 + sunrise_h
            # v7.9.6 - Correct key format for simulation log lookup
            target_key = f"{sunrise_h:0>2}:59 (Завтра)" 
            projected_morning_soc = self._get_soc_from_log(sim_log, target_key, sim_res_soc)
            
            # If we don't reach morning safely even with BASE load -> No budget for anything.
            if projected_morning_soc < survival_threshold:
                initial_budget = float((projected_morning_soc - survival_threshold) * b_cap_f / 100.0 * eff_coeff)
                _LOGGER.debug(f"[Budget] Survival gate locked: Projected morning SOC {projected_morning_soc:.1f}% < {survival_threshold}%")
            else:
                # v7.9.5 - Balanced view (Matching Simulation): (Morning_SOC - Target_SOC) converted to AC kWh.
                # This ensures the UI surplus matches the 24h Prediction screen.
                surplus_soc = float(projected_morning_soc - survival_threshold)
                initial_budget = float(surplus_soc * b_cap_f / 100.0 * eff_coeff)
                
            available_budget = initial_budget
            
            # For diagnostic attributes
            essential_house_consumption = expected_base_consumption 
            
            permissions = {}
            permissions_reasons = {}
            initial_power_kw = 0.0
            batt_p_flexible = 0.0
            waste_kw = 0.0
            
            p_load_s = list(getattr(man, "power_load_sensors", []))
            p_gen_s = list(getattr(man, "power_gen_sensors", []))
            
            if p_load_s and p_gen_s:
                load_kw = float(sum((get_kwh_val(man.hass.states.get(str(s)) or None) or 0.0) for s in p_load_s))
                gen_kw = float(sum((get_kwh_val(man.hass.states.get(str(s)) or None) or 0.0) for s in p_gen_s))
                
                initial_power_kw = float(gen_kw - load_kw)

                # Potential generation from Forecast (Today remaining distributed by profile)
                # This ensures we don't start boilers on cloudy days just because "history says it's sunny".
                f_today = float(man.get_forecast_value(man.forecast_today_sensor) or 0.0)
                
                # Check for Solcast hourly curve
                dist = man.get_forecast_hourly_distribution(man.forecast_today_hourly_sensor)
                dist_source = "historical"
                
                # v7.2 - Hourly Accuracy adjustment
                h_acc, _ = self.get_hourly_accuracy_coeff(cur_hour)

                if dist:
                    dist_source = "forecast_hourly"
                    cur_h_dist = float(dist.get(str(cur_hour), 0.0))
                    # v7.5.1 - Simplified Power calculation: (Weight / Sum of Weights) * Total Energy
                    rem_minutes = 60 - now.minute
                    step_duration = rem_minutes / 60.0
                    rem_dist = (cur_h_dist * step_duration) + sum(float(dist.get(str(h), 0.0)) for h in range(cur_hour + 1, 24))
                    f_potential = float(f_today * (cur_h_dist / rem_dist) * h_acc) if rem_dist > 0.01 else 0.0
                else:
                    rem_minutes = 60 - now.minute
                    step_duration = rem_minutes / 60.0
                    cur_h_hist = float(p_gen.get(str(cur_hour), 0.0))
                    rem_hist = (cur_h_hist * step_duration) + sum(float(p_gen.get(str(h), 0.0)) for h in range(cur_hour + 1, 24))
                    f_potential = float(f_today * (cur_h_hist / rem_hist) * h_acc) if rem_hist > 0.1 else 0.0
                
                potential_gen = float(max(gen_kw, f_potential))
                waste_kw = float(max(0.0, potential_gen - gen_kw))

                # Special fix: Solar waste is only possible in 'stop_sale' mode or if we are not importing.
                # If we are in 'sale_pv' mode, any surplus is exported, so no "waste" occurs.
                is_stop_sale = getattr(man, "current_inverter_mode", "") == "stop_sale"
                
                # If we are importing or NOT in stop_sale, we aren't wasting solar surplus
                if initial_power_kw < -0.1 or not is_stop_sale:
                    waste_kw = 0.0
                
                if man.battery_power_sensor:
                    st_batt = man.hass.states.get(str(man.battery_power_sensor))
                    batt_v = get_kwh_val(st_batt) or 0.0
                    batt_p_flexible = float(max(0.0, -float(batt_v)))
                
                batt_discharge_allowed = 0.0
                if initial_budget > 0.5:
                    max_batt_p_v = man.get_setting(CONF_BATTERY_MAX_POWER, 5.0)
                    max_batt_p = float(max_batt_p_v) if max_batt_p_v is not None else 5.0
                    batt_discharge_allowed = float(max_batt_p) * float(min(1.0, initial_budget / 3.0))
                
                initial_power_kw = float(initial_power_kw + waste_kw + batt_p_flexible + batt_discharge_allowed)
                
            available_power_kw = initial_power_kw
            
            # --- Only Solar Logic Enhancement (v4.5.4) ---
            # available_gen_kw should be the CURRENT solar surplus (Solar - Base House Load)
            # Base House Load = Total Load - Managed Loads currently running
            current_managed_load_kw = 0.0
            for s_id in man.deduct_settings:
                if man._is_currently_pulling_power(str(s_id)):
                    current_managed_load_kw += float(man.learned_real_power.get(str(s_id), 0.0)) / 1000.0
            
            base_house_load = max(0.0, float(load_kw - current_managed_load_kw))
            available_gen_kw = float(gen_kw - base_house_load) + waste_kw
            gen_surplus_initial = available_gen_kw
            
            cur_price_buy = None
            if not skip_strategy_check:
                strategy_res = self.get_market_strategy("buy")
                cur_price_buy = strategy_res.get("today_prices", {}).get(str(cur_hour))

            reserved_by = []
            # Sort loads by Priority (lower value = higher priority)
            # This ensures budget reservation happens in the correct order.
            sorted_items = sorted(
                man.deduct_settings.items(),
                key=lambda x: x[1].get(CONF_PRIORITY, 1) if isinstance(x[1], dict) else 1
            )
            
            for s_id, s_conf in sorted_items:
                s_id_s = str(s_id)
                s_conf = dict(s_conf if isinstance(s_conf, dict) else {})
                expected_kw, rem_kwh, is_cyclic, _ = man.get_managed_load_stats(s_id_s)
                e_kw = float(expected_kw)
                
                only_solar = bool(s_conf.get(CONF_ONLY_SOLAR, False))
                req_kwh = float(s_conf.get("required_kwh", 2.5))
                consumed = float(man.daily_deduct_consumption.get(s_id_s, 0.0))
                
                is_pulling = bool(man._is_currently_pulling_power(s_id_s))
                is_free_price = cur_price_buy is not None and float(normalize_float(cur_price_buy)) <= 0.0

                power_bottleneck = False
                gen_bottleneck = False
                p_thresh = 0.0
                if e_kw > 0.0:
                    is_strict = bool(only_solar and not is_free_price)
                    p_thresh = float(e_kw * 0.6) if is_strict else 0.0
                    p_lim = float(-(e_kw * 0.4)) if is_strict else float(-(e_kw * 0.95))
                    
                    if is_pulling:
                        if available_power_kw < p_lim: power_bottleneck = True
                    else:
                        if available_power_kw < p_thresh: power_bottleneck = True
                            
                    if only_solar and not is_free_price:
                        if available_gen_kw < float(e_kw * 0.6): gen_bottleneck = True
                elif initial_power_kw > 0.5 and available_power_kw < 0:
                    power_bottleneck = True

                price_suffix = " (Беспл. цена)" if is_free_price else ""
                if req_kwh > 0 and consumed >= req_kwh:
                    permissions[s_id_s] = False
                    permissions_reasons[s_id_s] = f"Норма выполнена ({consumed:.2f}/{req_kwh}{price_suffix})"
                elif power_bottleneck:
                    permissions[s_id_s] = False
                    permissions_reasons[s_id_s] = f"Дефицит мощности ({available_power_kw:.2f} < {p_thresh if not is_pulling else p_lim:.2f}{price_suffix})"
                elif gen_bottleneck:
                    permissions[s_id_s] = False
                    permissions_reasons[s_id_s] = "Недостаточно генерации (Только солнце)"
                elif available_budget < 0.1 and not only_solar and not is_free_price:
                    permissions[s_id_s] = False
                    permissions_reasons[s_id_s] = f"Лимит исчерпан ({available_budget:.2f} < 0.1)"
                else:
                    permissions[s_id_s] = True
                    permissions_reasons[s_id_s] = f"Ок ({available_budget:.2f} кВт·ч доступно{price_suffix})"
                    
                    # Reservation logic:
                    # - Non-cyclic (boilers/heaters): Reservation is always active.
                    # - Cyclic (washers/dishwashers): Reserve ONLY if already started (is_pulling).
                    # This allows several cyclic loads to have 'OK' status without blocking each other
                    # before a human actually presses the START button.
                    if not is_cyclic or is_pulling:
                        available_budget -= float(e_kw * (1.0 - (now.minute / 60.0)))
                        available_power_kw -= e_kw
                        # Subtraction ensures next devices in loop see less solar
                        available_gen_kw -= e_kw
                        reserved_by.append(s_id_s)
                    
            return {
                "initial_budget": float(initial_budget or 0.0),
                "battery_capacity_kwh": float(b_cap_f or 0.0),
                "projected_morning_soc": float(round_f(projected_morning_soc, 1)),
                "survival_threshold": float(round_f(survival_threshold, 1)),
                "battery_energy_kwh": round_f(b_energy_f, 3),
                "expected_consumption_kwh": round_f(expected_base_consumption, 3),
                "permissions": permissions or {},
                "permissions_reasons": permissions_reasons or {},
                "forecast_val": float(forecast_val_adjusted or 0.0),
                "forecast_raw": float(forecast_val or 0.0),
                "forecast_coefficient": float(blended_coeff or 1.0),
                "forecast_hist_coefficient": float(hist_coeff or 1.0),
                "forecast_today_coefficient": float(today_coeff or 1.0),
                "efficiency_coefficient": float(eff_coeff or 1.0),
                "degradation_cost": float(self.get_battery_degradation_cost() or 0.0),
                "debug_actual_today": float(actual_today or 0.0),
                "debug_expected_today_total": float(expected_today_total),
                "debug_expected_today_so_far": float(hist_gen_so_far),
                "debug_h_acc_cur": float(h_acc_cur),
                "debug_h_count_cur": int(h_count_cur),
                "debug_hist_coeff_rem": float(hist_coeff),
                "forecast_distribution": active_dist,
                "forecast_dist_source": dist_source,
                "debug_sample_keys": [],
                "debug_interval_sample": "",
                "debug_raw_attributes_sample": "",
                "debug_forecast_sensors": [],
                "debug_fraction_so_far": float(external_progress if 'external_progress' in locals() else fraction_so_far),
                "batt_energy_val": float(b_energy_f or 0.0),
                "expected_consumption": float(essential_house_consumption or 0.0),
                "occupancy_coefficient": float(occ_coeff or 1.0),
                "efficiency_coefficient": float(eff_coeff or 1.0),
                "available_power_total_kw": float(initial_power_kw or 0.0),
                "available_gen_kw": float(available_gen_kw or 0.0), # Remaining surplus after loop
                "available_gen_surplus_initial": float(gen_surplus_initial or 0.0),
                "reserved_by": reserved_by,
                "sunrise_hour": int(res_sunrise if 'res_sunrise' in locals() else 8),
                "waste_compensation_kw": float(waste_kw or 0.0),
                "battery_flexible_kw": float(batt_p_flexible or 0.0),
                "battery_discharge_budget_kw": float(batt_discharge_allowed or 0.0)
            }
        finally:
            self._calculating_strategy = old_calc

    def _get_soc_from_log(self, log: dict, key: str, default: float) -> float:
        """Safely extract SOC float from simulation log (handles both float and dict formats)."""
        val = log.get(key)
        if isinstance(val, dict):
            return float(val.get("soc", default))
        return float(val if val is not None else default)

    def run_soc_simulation(self, start_soc, sim_range, now, commands=None, man=None, house_profile_override=None):
        """Universal SOC simulation engine."""
        if not sim_range:
            return float(start_soc), {}, 0.0

        man = man or self.manager
        _, batt_cap, _ = man.get_battery_state()
        b_cap_f = float(batt_cap)
        if b_cap_f <= 0.1:
            return float(start_soc), {}, 0.0

        # v5.2 - Dynamic Period Adaptability (Fast Learning in Transition Seasons) 
        eff_period = man.custom_period
        if now.month in [3, 4, 9, 10]:
            eff_period = 7 

        day_idx_today = man.day_type
        tomorrow_dt = now + timedelta(days=1)
        day_idx_tom = (tomorrow_dt).weekday() # Simplified, manager.day_type handles today holiday
        
        # 1. Solar distribution (Solcast Curve)
        f_today = float(man.get_forecast_value(man.forecast_today_sensor) or 0.0)
        f_tom = float(man.get_forecast_value(man.forecast_tomorrow_sensor) or 0.0)
        dist_today = man.get_forecast_hourly_distribution(man.forecast_today_hourly_sensor)
        dist_tom = man.get_forecast_hourly_distribution(man.forecast_tomorrow_sensor, tomorrow_dt.strftime("%Y-%m-%d"))

        # 2. Consumption profiles (7-day Aware Total Load)
        p_type = house_profile_override or "consumption_total"
        prof_cons_today = dict(man.get_predicted_profile(p_type))
        prof_cons_tom = dict(man.get_average_profile(p_type, eff_period, day_idx_tom))
        
        # 3. Generation profiles (Historical Baseline)
        prof_gen_today = dict(man.get_average_profile("generation", eff_period, day_idx_today))
        prof_gen_tom = dict(man.get_average_profile("generation", eff_period, day_idx_tom))
        
        blended_coeff = float(getattr(man, "last_blended_coeff", 1.0))
        eff_coeff = float(self.get_efficiency_coefficient() or 1.0)
        fraction_left_h1 = float(1.0 - (now.minute / 60.0))
        max_batt_p_v = man.get_setting(CONF_BATTERY_MAX_POWER, 5.0)
        max_batt_p = float(max_batt_p_v) if max_batt_p_v is not None else 5.0

        sim_consumed_today = {str(s_id): float(man.daily_deduct_consumption.get(str(s_id), 0.0)) 
                             for s_id in man.deduct_settings}
        sim_consumed_tom = {str(s_id): 0.0 for s_id in man.deduct_settings}

        dist_today = man.get_forecast_hourly_distribution(man.forecast_today_hourly_sensor)
        dist_tom = man.get_forecast_hourly_distribution(man.forecast_tomorrow_sensor, (now + timedelta(days=1)).strftime("%Y-%m-%d"))


        simulated_soc = float(start_soc)
        history_log = {}
        overflow_kwh = 0.0
        for i, h_abs in enumerate(sim_range):
            real_h = int(h_abs % 24)
            is_tom = bool(h_abs >= 24)
            h_str = str(real_h)
            
            step_duration = float(fraction_left_h1 if i == 0 else 1.0)
            if step_duration <= 0.001: continue

            # 1. Generation Forecast for this hour
            if is_tom:
                if dist_tom:
                    total_dist = sum(dist_tom.values())
                    h_acc, _ = self.get_hourly_accuracy_coeff(int(h_abs) % 24)
                    expected_gen_kw = float(dist_tom.get(h_str, 0.0) / total_dist * f_tom * blended_coeff * h_acc) if total_dist > 0.1 else 0.0
                else:
                    total_hist = sum(prof_gen_tom.values())
                    h_acc, _ = self.get_hourly_accuracy_coeff(int(h_abs) % 24)
                    expected_gen_kw = float(normalize_float(prof_gen_tom.get(h_str, 0.0)) / total_hist * f_tom * blended_coeff * h_acc) if total_hist > 0.1 else 0.0
            else:
                if dist_today:
                    # v7.5 - Pro-rate the current hour weight to match f_today (remaining energy)
                    cur_h_weight = float(dist_today.get(h_str, 0.0))
                    rem_dist = (cur_h_weight * step_duration) + sum(float(dist_today.get(str(hr), 0.0)) for hr in range(now.hour + 1, 24))
                    
                    h_acc, _ = self.get_hourly_accuracy_coeff(int(h_abs) % 24)
                    # v7.6.1 - Correct units: Power (kW) = Weight / Sum_Weights * Total_Energy * Calibration * Hourly_Bias
                    expected_gen_kw = float(cur_h_weight / rem_dist * f_today * blended_coeff * h_acc) if rem_dist > 0.1 else 0.0
                else:
                    cur_h_hist = float(prof_gen_today.get(h_str, 0.0))
                    rem_hist = (cur_h_hist * step_duration) + sum(float(prof_gen_today.get(str(hr), 0.0)) for hr in range(now.hour + 1, 24))
                    
                    h_acc, _ = self.get_hourly_accuracy_coeff(int(h_abs) % 24)
                    expected_gen_kw = float(cur_h_hist / rem_hist * f_today * blended_coeff * h_acc) if rem_hist > 0.1 else 0.0
            
            # v7.8.6 - Dynamic Solar Night Clamp
            # We determine if it's "night" by checking the historical generation profile.
            p_gen_check = prof_gen_tom if is_tom else prof_gen_today
            hist_h_val = float(normalize_float(p_gen_check.get(h_str, 0.0)))
            
            # If history says 0 and it's typical night hours, force 0.
            # This prevents the "Weighted inflation" from turnining technical noise into 2kW at 2 AM.
            if hist_h_val < 0.01 and (real_h < 8 or real_h > 20):
                expected_gen_kw = 0.0

            # First hour correction: 
            # Use real-time power (kW) if available, but ensure it's treated as Power (kW).
            # Solar (expected_gen_kw) from 'energy_h' is already kWh for the remaining period.
            if i == 0:
                # v7.5.2 - If we have real-time power, we can anchor the first step to it.
                real_gen_kw = float(getattr(man, "avg_gen_kw", 0.0))
                if real_gen_kw > 0.01:
                    expected_gen_kw = real_gen_kw

            # 4. Inverter Command (AI Buying/Selling)
            cmd_p = float(commands.get(int(h_abs), 0.0)) if commands else 0.0

            # 3. Consumption Profile (includes historical managed loads)
            p_cons = prof_cons_tom if is_tom else prof_cons_today
            occ_coeff = float(man.get_occupancy_coefficient())
            expected_cons_kw = float(normalize_float(p_cons.get(h_str, 0.0))) * occ_coeff
            
            # Anchor the first step of simulation to REAL active load, not profile.
            if i == 0:
                # v7.9.7 - If it's the first step, use real-time power (kW)
                # Ensure we use BASE power for survival simulations to avoid double-counting active loads.
                if house_profile_override == "consumption_base":
                    expected_cons_kw = float(getattr(man, "avg_base_load_kw", expected_cons_kw))
                else:
                    expected_cons_kw = float(getattr(man, "avg_load_kw", expected_cons_kw))
                
                # Special case: if we are using predicted_profile's h==cur_hour, it might be Energy (kWh)
                # But avg_load_kw is always better for the first step.
            
            # v7.2 - Unified unit handling: Power (kW) * Time (h) = Energy (kWh)
            total_net_kw = float(expected_gen_kw - expected_cons_kw + cmd_p)
            
            # v7.9.9: If we have high-quality efficiency data (>0.6) from the user's sensor, 
            # we assume it ALREADY includes the idle losses to avoid double-counting.
            # Otherwise, we add the constant idle_p (usually 0.05kW) to the house load.
            idle_p = float(man.current_losses) if hasattr(man, 'current_losses') else 0.05
            if eff_coeff < 0.999: # Only add if not already in efficiency
                 expected_cons_kw += idle_p
            
            if total_net_kw > 0.001: 
                acc_ratio = float(self.get_cc_cv_ratio(simulated_soc))
                actual_charge_kw = float(min(total_net_kw * eff_coeff, max_batt_p * acc_ratio))
                
                old_soc = simulated_soc
                if b_cap_f > 0.1:
                    simulated_soc = float(min(100.0, simulated_soc + (actual_charge_kw * step_duration / b_cap_f * 100.0)))
                
                # v11.0.6 - Track overflow energy (AC kWh)
                actual_stored_kwh_ac = 0.0
                if b_cap_f > 0.1:
                    actual_stored_kwh_ac = ((simulated_soc - old_soc) / 100.0 * b_cap_f) / max(0.1, eff_coeff)
                
                overflow_h = max(0.0, (total_net_kw * step_duration) - actual_stored_kwh_ac)
                overflow_kwh += overflow_h
            elif total_net_kw < -0.001: 
                sim_eff = float(max(0.85, eff_coeff))
                actual_discharge_kw = float(min(abs(total_net_kw) / sim_eff, max_batt_p))
                if b_cap_f > 0.1:
                    simulated_soc = float(max(0.0, simulated_soc - (actual_discharge_kw * step_duration / b_cap_f * 100.0)))
            
            # Store enriched data for the 24h forecast sensors
            history_log[f"{real_h:0>2}:59" + (" (Завтра)" if is_tom else "")] = {
                "soc": round_f(float(simulated_soc), 1),
                "gen_kw": round_f(float(expected_gen_kw), 3),
                "load_kw": round_f(float(expected_cons_kw), 3)
            }

        return float(simulated_soc), history_log, float(overflow_kwh)

    def get_market_strategy(self, mode="buy"):
        now = dt_util.now()
        man: Any = self.manager
        
        cache_key = f"market_strategy_{mode}"
        cached = self._strategy_cache.get(cache_key)
        if cached and (now - cached["time"]).total_seconds() < 30:
            return cached["res"]

        res = {
            "state": "idle",
            "mode": mode,
            "active_hours": [],
            "active_periods": "",
            "recommended_power_kw": 0.0,
            "target_price": 0.0,
            "limit_used": 0.0,
            "today_prices": {},
            "tomorrow_prices": {},
            "multi_cycle": "Не предвидится",
            "buy_simulation": {"projected_soc_at_start_pct": 0.0, "projected_soc_at_end_pct": 0.0, "projected_soc_morning_pct": 0.0},
            "sell_simulation": {"projected_soc_at_start_pct": 0.0, "projected_soc_after_sale_pct": 0.0, "projected_soc_morning_pct": 0.0},
            "arbitrage_decision": "Нет данных",
            "charge_reason": "none",
            "arbitrage_buyback": {"opportunity": False, "power_kw": 0.0, "note": ""}
        }
        charge_commands = {}
        
        old_calc = bool(getattr(self, "_calculating_strategy", False))
        self._calculating_strategy = True
        try:
            cur_hour = int(now.hour)
            today_str = now.strftime("%Y-%m-%d")
            tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
            
            try:
                p_st = dict(man.data.get(f"prices_{mode}", {}))
                today_prices = dict(p_st.get(today_str, {}))
                tomorrow_prices = dict(p_st.get(tomorrow_str, {}))
            except Exception as e:
                _LOGGER.error(f"Error fetching prices in MarketStrategy: {e}")
                return res
        
            res["today_prices"] = today_prices
            res["tomorrow_prices"] = tomorrow_prices

            # Common data for all modes
            avg_prof_gen = man.get_average_profile("generation", man.custom_period, "all")
            sunrise_h = 8 # default fallback
            for h in range(4, 12):
                if float(normalize_float(avg_prof_gen.get(str(h), 0.0))) > 0.1:
                    sunrise_h = h
                    break
            res["sunrise_hour"] = sunrise_h
            
            batt_soc, batt_cap, batt_energy_val = man.get_battery_state()
            b_soc = float(batt_soc)
            b_cap = float(batt_cap)
            
            today_idx = man.day_type
            tom_idx = (now + timedelta(days=1)).weekday()
            
            prof_gen = dict(man.get_average_profile("generation", man.custom_period, "all"))
            
            f_today_v = float(man.get_forecast_value(man.forecast_today_sensor) or 0.0)
            f_tom_v = float(man.get_forecast_value(man.forecast_tomorrow_sensor) or 0.0)
            
            eff_coeff = float(self.get_efficiency_coefficient() or 1.0)
            max_p_v = man.get_setting(CONF_BATTERY_MAX_POWER, 5.0)
            max_p = float(max_p_v) if max_p_v is not None else 5.0
            
            if not today_prices: return res
                
            force_sell = bool(man.get_setting(CONF_FORCE_MARKET_SELL, False))
            if mode == "sell" and force_sell:
                res["target_price"] = 0.0
                res["limit_used"] = 0.0
                res["active_hours"] = [cur_hour]
                return res
            
            all_prices = {}
            for h, p in today_prices.items(): all_prices[int(h)] = float(normalize_float(p))
            for h, p in tomorrow_prices.items(): all_prices[int(h) + 24] = float(normalize_float(p))
                
            negative_hours = [int(h) for h, p in all_prices.items() if p < 0 and h >= cur_hour]

            buy_limit = float(man.get_setting(CONF_PRICE_BUY_LIMIT, 2.0))
            sell_limit = float(man.get_setting(CONF_PRICE_SELL_LIMIT, 5.0))
            tolerance = float(man.get_setting(CONF_PRICE_TOLERANCE, 0.1))
            eff = float(eff_coeff)
            active_window = (cur_hour, 47) if tomorrow_prices else (cur_hour, 23)
            # End the window at :59 for clarity
            res["analyzed_window"] = f"До {self._format_h(active_window[1]).replace(':00', ':59')}"
            
            target_hours = []
            target_price = 0.0

            def get_peaks(window, is_sell, limit, tol):
                if not window: return []
                w_vals = [float(v) for v in window.values()]
                if is_sell:
                    best_p = float(max(w_vals))
                    if best_p >= float(limit):
                        return [(int(h), float(p)) for h, p in window.items() if float(p) >= float(limit) and float(p) >= (best_p - float(tol))]
                else:
                    best_p = float(min(w_vals))
                    if best_p <= float(limit):
                        return [(int(h), float(p)) for h, p in window.items() if float(p) <= float(limit) and float(p) <= (best_p + float(tol))]
                return []

            # Shared arbitrage data
            s_p_today = dict(man.data.get("prices_sell", {}).get(today_str, {}))
            s_p_tom = dict(man.data.get("prices_sell", {}).get(tomorrow_str, {}))
            all_sell_prices = {}
            for h, p in s_p_today.items(): all_sell_prices[int(h)] = float(normalize_float(p))
            for h, p in s_p_tom.items(): all_sell_prices[int(h) + 24] = float(normalize_float(p))

            b_p_today = dict(man.data.get("prices_buy", {}).get(today_str, {}))
            b_p_tom = dict(man.data.get("prices_buy", {}).get(tomorrow_str, {}))
            all_buy_prices = {}
            for h, p in b_p_today.items(): all_buy_prices[int(h)] = float(normalize_float(p))
            for h, p in b_p_tom.items(): all_buy_prices[int(h) + 24] = float(normalize_float(p))

            deg_cost = float(self.get_battery_degradation_cost() or 0.0)
            min_p_v = man.get_setting(CONF_ARBITRAGE_MIN_PROFIT, 0.0)
            min_p = float(min_p_v) if min_p_v is not None else 0.0
            threshold = float(max(min_p, 2.0 * deg_cost))
            
            currency = getattr(self.manager.hass.config, "currency", "EUR") or "EUR"

            def get_best_buyback(after_h):
                options = {int(h): float(p) for h, p in all_buy_prices.items() if int(h) > int(after_h)}
                if not options: return 999.0, None
                best_h = min(options, key=lambda k: options[k])
                return float(options[best_h]), int(best_h)

            # Find the absolute best buy hour for use in simulation windows
            _bb_options = [h for h in all_buy_prices if h >= cur_hour]
            _bb_h = min(_bb_options, key=lambda h: all_buy_prices[h]) if _bb_options else None
            best_buy_pair = (all_buy_prices[_bb_h], _bb_h) if _bb_h is not None else (999.0, None)

            # --- Shared Arbitrage Analysis (v6.6) ---
            # 1. Gain from SELLING NOW (or soon) and BUYING BACK LATER (Primary for SELL mode)
            best_sell_now_pair = (None, None)
            max_gain_sell_now = -999.0
            for h_s, p_s in all_sell_prices.items():
                if int(h_s) < cur_hour: continue
                p_b, h_b = get_best_buyback(h_s)
                if h_b is not None:
                    gain = float(float(p_s) * eff - float(p_b) - deg_cost)
                    if gain > max_gain_sell_now:
                        max_gain_sell_now = gain
                        best_sell_now_pair = (int(h_s), int(h_b))

            # 2. Gain from BUYING NOW (or soon) and SELLING LATER (Primary for BUY mode)
            best_buy_now_pair = (None, None)
            max_gain_buy_now = -999.0
            for h_b, p_b in all_buy_prices.items():
                if int(h_b) < cur_hour: continue
                # Find best future sell after this buy hour
                future_sell = [p_s for h_s, p_s in all_sell_prices.items() if h_s > h_b]
                if future_sell:
                    best_s_p = max(future_sell)
                    best_s_h = [h_s for h_s, p_s in all_sell_prices.items() if h_s > h_b and p_s == best_s_p][0]
                    gain = float(best_s_p * eff - p_b - deg_cost)
                    if gain > max_gain_buy_now:
                        max_gain_buy_now = gain
                        best_buy_now_pair = (int(best_s_h), int(h_b))

            # Use mode-specific gain for decision logic and UI strings
            max_arb_gain = max_gain_buy_now if mode == "buy" else max_gain_sell_now
            best_arb_pair = best_buy_now_pair if mode == "buy" else best_sell_now_pair

            global_arb_note = "Нет прибыльного арбитража"
            if max_arb_gain >= threshold:
                s_h, b_h = best_arb_pair
                if s_h is not None and b_h is not None:
                    global_arb_note = f"Арбитраж: Продажа в {self._format_h(s_h)} (по {all_sell_prices[s_h]:.2f}), выгода {max_arb_gain:.2f} {currency}/кВт·ч"


            if mode == "buy":
                res["limit_used"] = buy_limit
                if negative_hours:
                    target_hours = list(negative_hours)
                    target_price = float(min([all_prices[h] for h in negative_hours]))
                    res["target_price"] = target_price
                else:
                    def is_buy_profitable_arb(buy_p, hour):
                        # Find best future sell price after this buy hour
                        future_sell = [p_s for h_s, p_s in all_sell_prices.items() if h_s > hour]
                        if not future_sell: return False
                        best_s = max(future_sell)
                        
                        # Use the strict formula: (Sell * Eff) - Buy - Deg >= Threshold
                        gain = float(best_s * eff - buy_p - deg_cost)
                        return gain >= threshold

                    dynamic_buy_ai = bool(man.get_setting(CONF_DYNAMIC_SOC_BUY, True))
                    wt_filtered = {h: p for h, p in today_prices.items() if float(normalize_float(p)) <= buy_limit or (dynamic_buy_ai and is_buy_profitable_arb(float(normalize_float(p)), int(h)))}
                    wom_filtered = {h: p for h, p in tomorrow_prices.items() if float(normalize_float(p)) <= buy_limit or (dynamic_buy_ai and is_buy_profitable_arb(float(normalize_float(p)), int(h) + 24))}
                    
                    if not dynamic_buy_ai:
                        # Use all hours meeting the limit
                        combined = [(int(h), float(p)) for h, p in today_prices.items() if float(normalize_float(p)) <= buy_limit]
                        combined += [(int(h) + 24, float(p)) for h, p in tomorrow_prices.items() if float(normalize_float(p)) <= buy_limit]
                    else:
                        peaks_today = get_peaks(wt_filtered, False, 999.0, tolerance)
                        peaks_tom = get_peaks(wom_filtered, False, 999.0, tolerance)
                        combined = peaks_today + peaks_tom
                    
                    if combined:
                        target_hours = [int(h) for h, p in combined]
                        
                        # --- v6.3: LIFT TOLERANCE FOR PROFITABLE ARBITRAGE ---
                        # If an hour is profitable for arbitrage, we MUST include it in target_hours
                        # even if it wasn't selected by get_peaks (e.g. it's 0.18 and best is 0.12).
                        if dynamic_buy_ai:
                            for h, p in (today_prices | tomorrow_prices if tomorrow_prices else today_prices).items():
                                h_abs = int(h)
                                if h_abs <= cur_hour: continue
                                if h_abs not in target_hours and is_buy_profitable_arb(float(normalize_float(p)), h_abs):
                                    target_hours.append(h_abs)
                                    _LOGGER.debug(f"[Strategy] v6.3: Profitable hour {h_abs} (p:{p}) added to plan via arbitrage bypass")

                        target_price = float(min(p for h, p in combined))
                        res["target_price"] = target_price
                        
                        is_arb_window = any(is_buy_profitable_arb(p, h) for h, p in combined)
                        if dynamic_buy_ai and (not any(float(normalize_float(p)) <= buy_limit for h, p in combined) or is_arb_window):
                            res["state"] = "preparing_arbitrage"
                    
                    if res.get("state") == "preparing_arbitrage":
                        if is_arb_window:
                            s_h, b_h = best_arb_pair
                            res["arbitrage_decision"] = f"Заряд для продажи в {self._format_h(s_h)}, выгода {max_arb_gain:.2f} {currency}/кВт·ч"
                        else:
                            res["arbitrage_decision"] = "Заряд для обеспечения дома (Survival)"
                    else:
                        res["arbitrage_decision"] = global_arb_note
            else: # sell
                res["limit_used"] = sell_limit
                if negative_hours and cur_hour in negative_hours:
                    res["state"] = "price_limit_not_met"
                    res["arbitrage_decision"] = "Отрицательная цена"
                    self._calculating_strategy = old_calc
                    return res
                
                def is_profitable(price, hour):
                    cheap_p_back, cheap_h = get_best_buyback(hour)
                    if cheap_h is None: return False, 0.0, 999.0, None
                    # gain = (Sale Price - Buyback Price) * Efficiency - Degradation Cost
                    gain = float(price * eff - cheap_p_back - deg_cost)
                    return gain >= threshold, gain, cheap_p_back, cheap_h

                raw_peaks_today = get_peaks(today_prices, True, 0.0, tolerance)
                raw_peaks_tom = get_peaks(tomorrow_prices, True, 0.0, tolerance)
                
                if not raw_peaks_today and not raw_peaks_tom:
                    res["state"] = "price_limit_not_met"
                    res["arbitrage_decision"] = "Нет ценового окна"
                else:
                    dynamic_sell_ai = bool(man.get_setting(CONF_DYNAMIC_SOC_SELL, True))
                    if not dynamic_sell_ai:
                        # Use all hours meeting the limit
                        peaks_today = [(int(h), float(p)) for h, p in today_prices.items() if float(normalize_float(p)) >= sell_limit]
                        peaks_tom = [(int(h) + 24, float(p)) for h, p in tomorrow_prices.items() if float(normalize_float(p)) >= sell_limit]
                    else:
                        peaks_today = []
                        for h, p in raw_peaks_today:
                            ok_arb, _, _, _ = is_profitable(float(normalize_float(p)), int(h))
                            if float(normalize_float(p)) >= sell_limit or ok_arb: # when AI is on, any arb or limit peak is ok
                                peaks_today.append((int(h), float(normalize_float(p))))
                                
                        peaks_tom = []
                        for h, p in raw_peaks_tom:
                            ok_arb, _, _, _ = is_profitable(float(normalize_float(p)), int(h) + 24)
                            if float(normalize_float(p)) >= sell_limit or ok_arb:
                                peaks_tom.append((int(h) + 24, float(normalize_float(p))))
                    
                    if not peaks_today and not peaks_tom:
                        res["state"] = "price_limit_not_met"
                        res["multi_cycle"] = "Не предвидится"
                        res["arbitrage_decision"] = "Нет ценового окна"
                    else:
                        if dynamic_sell_ai and not any(p >= sell_limit for h, p in peaks_today + peaks_tom):
                            res["state"] = "preparing_arbitrage"
                        
                        if peaks_today and peaks_tom:
                            max_h_today = max(h for h, p in peaks_today)
                            min_h_tom = min(h for h, p in peaks_tom)
                            
                            can_recharge = False
                            for h in range(max_h_today + 1, min_h_tom):
                                if all_buy_prices.get(h, 99.0) <= buy_limit:
                                    can_recharge = True
                                    res["multi_cycle"] = "Благоприятно (Дешевая сеть ночью)"
                                    break
                                if 8 <= (h % 24) <= 16:
                                    val_sum = 0.0
                                    fsensors = man.forecast_tomorrow_sensor
                                    if fsensors:
                                        if isinstance(fsensors, str): fsensors = [fsensors]
                                        for fsensor in fsensors:
                                            st = man.hass.states.get(fsensor)
                                            v = get_kwh_val(st)
                                            if v is not None: val_sum += float(v)
                                    
                                    if val_sum > 3.0:
                                        can_recharge = True
                                        res["multi_cycle"] = "Благоприятно (Ожидается солнце)"
                                        break
                                
                            if can_recharge:
                                combined = peaks_today + peaks_tom
                                target_hours = [int(h) for h, p in combined]
                                target_price = float(max(p for h, p in combined))
                            else:
                                res["multi_cycle"] = "Неблагоприятно (Нет условий для дозарядки)"
                                max_today_p = float(max(p for h, p in peaks_today))
                                max_tom_p = float(max(p for h, p in peaks_tom))
                                if max_today_p >= max_tom_p:
                                    target_hours = [int(h) for h, p in peaks_today]
                                    target_price = max_today_p
                                else:
                                    target_hours = [int(h) for h, p in peaks_tom]
                                    target_price = max_tom_p
                        elif peaks_today:
                            target_hours = [int(h) for h, p in peaks_today]
                            target_price = float(max(p for h, p in peaks_today))
                        elif peaks_tom:
                            target_hours = [int(h) for h, p in peaks_tom]
                            target_price = float(max(p for h, p in peaks_tom))

                        # Arbitrage note for the sensor
                        cheap_p_back, cheap_h_back = get_best_buyback(cur_hour)
                        cur_p_f = float(normalize_float(today_prices.get(str(cur_hour), 0.0)))
                        cur_gain = float(cur_p_f * eff - cheap_p_back - deg_cost)
                        
                        status = "Ожидание"
                        if cur_p_f >= sell_limit: status = "Продажа (Лимит)"
                        elif cur_gain >= threshold: status = "Продажа (Арбитраж)"
                        
                        detail = f"Сейчас {cur_p_f:.2f}. {global_arb_note}"
                        if best_arb_pair[0] is not None and best_arb_pair[0] > cur_hour and all_sell_prices.get(best_arb_pair[0], 0) > cur_p_f + 0.01:
                             detail += f" | Ждем главного пика в {self._format_h(best_arb_pair[0])}"
                        
                        res["arbitrage_decision"] = f"{status}: {detail}"

            target_hours = sorted([int(h) for h in target_hours if int(h) >= cur_hour])
            
            # Apply 12h gap truncation: only plan for the immediate block of peaks
            if target_hours:
                truncated = [target_hours[0]]
                for i in range(1, len(target_hours)):
                    if target_hours[i] - target_hours[i-1] <= 12:
                        truncated.append(target_hours[i])
                    else:
                        break
                target_hours = truncated

            # Survival Logic
            if mode == "buy" and b_cap > 0 and man.get_setting(CONF_DYNAMIC_SOC_BUY, True):
                # Adaptive active_window for buy mode: current hour until next sell peak for the arbitrage window
                active_window = (best_buy_pair[1], best_arb_pair[0]) if best_arb_pair[0] is not None else (best_buy_pair[1], int(best_buy_pair[1] or 0) + 1)
                
                min_soc = float(man.get_setting(CONF_MIN_SOC_BUY, 10.0))
                natural_hours_names = set(target_hours)
                survival_hours = set(target_hours)
                
                active_dist = man.data.get("avg_profiles", {}).get("generation", {})
                safety_counter = 0
                while safety_counter < 48:
                    safety_counter += 1
                    added_bridge = False
                    # Plan simulation from actual start of charging until next significant sell peak
                    _win_end = int(best_arb_pair[0]) if best_arb_pair[0] is not None and int(best_arb_pair[0]) > cur_hour else int(max(all_buy_prices.keys()) if all_buy_prices else 23)
                    sim_range = list(range(cur_hour, _win_end + 1))
                    commands = {h_cmd: max_p for h_cmd in survival_hours}
                    _, log, _ = self.run_soc_simulation(b_soc, sim_range, now, commands)
                    
                    violation_hour = None
                    for h_step in sim_range:
                        is_tom_sim = h_step >= 24
                        h_label = f"{h_step % 24:0>2}:59" + (" (Завтра)" if is_tom_sim else "")
                        soc_at_h = self._get_soc_from_log(log, h_label, 100.0)
                        
                        # IMMINENT SOLAR AWARENESS (v5.3)
                        # If we have a minor violation (< 15% depth) but solar is expected to kick in 
                        # within 3 hours, we don't start grid charging yet.
                        is_minor = (min_soc - soc_at_h) < 15.0
                        solar_income_soon = sum(float(normalize_float(active_dist.get(str(hs % 24), 0.0))) for hs in range(h_step, h_step + 3)) > 0.5
                        if soc_at_h < min_soc and violation_hour is None:
                            if is_minor and solar_income_soon and h_step < 12:
                                # Skip this violation, wait for sun
                                continue
                            violation_hour = h_step
                    
                    if violation_hour is not None:
                        search_space = [sh for sh in range(cur_hour, violation_hour + 1) if sh not in survival_hours and sh in all_buy_prices]
                        if search_space:
                            cheapest_bridge = min(search_space, key=lambda sh: all_buy_prices[sh])
                            survival_hours.add(int(cheapest_bridge))
                            added_bridge = True
                    
                    if not added_bridge:
                        break
                    
                target_hours = list(survival_hours)

            res["limit_used"] = buy_limit if mode == "buy" else sell_limit
            future_active = sorted([h for h in target_hours if h >= cur_hour])
            if future_active:
                upcoming_h = future_active[0]
                rel_hours = [h for h in future_active if (h < 24 if upcoming_h < 24 else h >= 24)]
                p_list = [float(all_prices.get(h, 0.0)) for h in rel_hours]
                if p_list:
                    res["target_price"] = float(min(p_list) if mode == "buy" else max(p_list))

            if not target_hours and mode == "buy":
                res["state"] = "price_limit_not_met"
                # Continue to simulation to show natural discharge
                
            target_hours_sorted = sorted(target_hours)
            found_periods = [] # Legacy reference, actual logic moved to end of function (v6.18)
                
            # Target & Power Calculation
            power_needed = 0.0
            target_soc = b_soc
            sim_soc_plan = b_soc
            if b_cap > 0.1:
                if mode == "buy":
                    base_target = float(man.get_setting(CONF_TARGET_SOC_BUY, 100.0))
                    
                    is_strict_arb = False
                    # Only buy for arbitrage if profit covers DOUBLE battery wear (charging + discharging)
                    strict_threshold = max(threshold, 2 * deg_cost)
                    
                    # 1. Look for future sell peaks (arbitrage opportunities)
                    future_sell_peaks = sorted([h for h, p in all_sell_prices.items() if h > cur_hour])
                    best_peak_p = 0.0
                    peak_hour = None
                    if future_sell_peaks:
                        best_peak_p = max(all_sell_prices[h] for h in future_sell_peaks)
                        peak_hour = [h for h in future_sell_peaks if all_sell_prices[h] == best_peak_p][0]
                    
                    # 2. Check if pre-charging from current grid price is profitable against this peak
                    cheapest_buy_in_window = min(float(all_buy_prices.get(h, 999.0)) for h in target_hours_sorted if h >= cur_hour) if target_hours_sorted else 999.0
                    if peak_hour is not None and (best_peak_p * eff - cheapest_buy_in_window - deg_cost) >= threshold:
                        is_strict_arb = True
                    
                    # --- Adaptive Target Engine (v6.9) ---
                    # We only buy from grid what the Sun WON'T provide before the peak starts.
                    peak_h_for_adaptive = peak_hour if peak_hour else 18
                    sim_range_dry = list(range(cur_hour, int(peak_h_for_adaptive)))
                    
                    # 1. Survival Check (Mandatory)
                    budget_data = self.get_budget_and_permissions(man.custom_period, skip_strategy_check=True)
                    solar_income = float(normalize_float(budget_data.get("forecast_val", 0.0) if budget_data else 0.0))
                    cons_until_morning = float(normalize_float(budget_data.get("expected_consumption", 2.0) if budget_data else 2.0))
                    
                    survival_target_kwh = cons_until_morning + (min_soc * b_cap / 100.0)
                    available_today_kwh = (b_soc * b_cap / 100.0) + solar_income
                    
                    # --- Granular Solar Priority (v6.14) ---
                    # We only buy from grid what the Sun WON'T provide before the peak starts.
                    peak_h = peak_hour if peak_hour else 18
                    pool = [h for h in target_hours_sorted if h >= cur_hour]
                    pool_useful = []
                    
                    for h_b in pool:
                        # 1. Prediction of SOC at the START of this hour (solar only)
                        sim_to_b = list(range(cur_hour, int(h_b)))
                        soc_at_b, _, _ = self.run_soc_simulation(b_soc, sim_to_b, now, commands=None)
                        
                        # Fix (v6.16): For future hours, use Minute 0 to get FULL solar hour in simulation.
                        # This prevents "losing" solar minutes due to now.minute offset.
                        sim_start_time = now if h_b == cur_hour else now.replace(minute=0, second=0, microsecond=0)
                        
                        # 2. Prediction of MAX SOC achieved by Sun alone TODAY starting from this hour
                        sim_eod = list(range(int(h_b), 24))
                        soc_final_dry, dry_log, _ = self.run_soc_simulation(soc_at_b, sim_eod, sim_start_time, commands=None)
                        max_dry_soc = max([float(st["soc"]) for st in dry_log.values()] + [float(soc_at_b)])
                        
                        if max_dry_soc < 99.0: # If sun alone won't reach 100% at any point today
                            pool_useful.append(h_b)
                    
                    pool = pool_useful
                    if is_strict_arb:
                        res["charge_reason"] = "arbitrage"
                        # Adaptive Target: 100% minus what the sun gives eventually
                        expected_soc_at_peak, _, _ = self.run_soc_simulation(b_soc, list(range(cur_hour, int(peak_h))), now, commands=None)
                        sun_gain_pct = max(0.0, expected_soc_at_peak - b_soc)
                        target_soc = float(min(100.0, 100.0 - sun_gain_pct))
                    elif negative_hours:
                        res["charge_reason"] = "negative"
                        target_soc = 100.0
                    elif available_today_kwh < survival_target_kwh:
                        res["charge_reason"] = "survival"
                        target_soc = float(min(base_target, survival_target_kwh / b_cap * 100.0))
                    else:
                        res["charge_reason"] = "none"
                        target_soc = b_soc

                    # Final Override (v6.16): If no useful hours left, sun is sufficient,
                    # OR current SOC is already high enough (prevents micro-buys for 1% arbitrage)
                    if not pool or target_soc <= (b_soc + 0.5):
                        target_soc = b_soc
                        res["charge_reason"] = "none"
                        pool = [] # Empty pool to clear attributes
                    
                    target_soc = float(min(100.0, target_soc))
                    sim_soc_plan = b_soc
                    
                    charge_commands = {}
                    charge_commands = {int(h): 0.0 for h in target_hours_sorted if h >= cur_hour}
                    if target_hours_sorted:
                        # 1. Calculate how much kWh we roughly need to add
                        # We use 110% of the theoretical gap to be safe (cover base consumption during charge)
                        theoretical_gap_kwh = max(0.0, (target_soc - b_soc) / 100.0 * b_cap)
                        energy_to_buy = theoretical_gap_kwh * 1.1

                        # 2. Sort available hours by price (cheapest first)
                        pool_sorted = sorted(pool, key=lambda h: all_buy_prices[h])

                        # 3. v6.4: Smooth window distribution (Smooth as requested in pt 4)
                        # Instead of filling cheapest first at max power, we spread energy across the whole window.
                        # Formula: Power = (Total Energy Needed) / (Sum of time factors)
                        total_h_factors = sum(max(0.1, (60 - now.minute)/60.0) if h == cur_hour else 1.0 for h in pool)
                        if total_h_factors > 0.01:
                            p_req = float(energy_to_buy / total_h_factors)
                            # Clamp to BMS limit
                            p_final = min(max_p, p_req)
                            for h in pool:
                                charge_commands[int(h)] = p_final
                        else:
                            for h in pool:
                                charge_commands[int(h)] = min(max_p, energy_to_buy)

                    power_needed = charge_commands.get(cur_hour, 0.0)
                    upcoming_p = next((p for h, p in charge_commands.items() if p > 0), 0.0)
                    
                    
                    # --- BUY SIMULATION ---
                    try:
                        # We simulate natural behavior even if no grid purchase is planned
                        # v7.8 - Ensure simulation covers at least 24h OR until sunrise tomorrow
                        sim_end_h = max(cur_hour + 24, 24 + sunrise_h + 1)
                        sim_range = list(range(cur_hour, sim_end_h))
                        _, sim_log, _ = self.run_soc_simulation(b_soc, sim_range, now, charge_commands)
                        
                        # 1. Projected SOC at START of the first buy hour
                        if target_hours_sorted:
                            first_h_buy = min(t for t in target_hours_sorted if t >= cur_hour)
                            if first_h_buy > cur_hour:
                                prev_h = first_h_buy - 1
                                key_start = f"{prev_h % 24:02d}:59" + (" (Завтра)" if prev_h >= 24 else "")
                                soc_at_start = self._get_soc_from_log(sim_log, key_start, b_soc)
                            else:
                                soc_at_start = b_soc
                        else:
                            soc_at_start = b_soc

                        # 2. Projected SOC AFTER the last buy hour (or noon today if no buys)
                        last_h_buy = max(target_hours_sorted) if target_hours_sorted else min(13, cur_hour + 6)
                        key_end = f"{last_h_buy % 24:02d}:59" + (" (Завтра)" if last_h_buy >= 24 else "")
                        soc_at_end = self._get_soc_from_log(sim_log, key_end, b_soc)
                            
                        # 3. Projected SOC TOMORROW MORNING (At actual sunrise)
                        # v7.9.8 - Ensure consistency with Energy Balance sensor
                        sunrise_h_sim = 8
                        prof_gen_tom = man.get_average_profile("generation", self.manager.custom_period, (now + timedelta(days=1)).weekday())
                        for h in range(24):
                            if float(prof_gen_tom.get(str(h), 0.0)) > 0.05:
                                sunrise_h_sim = h
                                break
                                
                        key_morning = f"{sunrise_h_sim:02d}:59 (Завтра)"
                        soc_morning = self._get_soc_from_log(sim_log, key_morning, soc_at_end)
                        
                        # v7.2 - CLEANUP: If no buy is currently planned for today, return current SOC
                        if not target_hours_sorted:
                            power_needed = 0.0
                            soc_at_start = b_soc
                            soc_at_end = b_soc

                        res["buy_simulation"] = {
                            "projected_soc_at_start_pct": float(round_f(soc_at_start, 1)),
                            "projected_soc_at_end_pct": float(round_f(soc_at_end, 1)),
                            "projected_soc_morning_pct": float(round_f(soc_morning, 1)),
                            "log": sim_log
                        }

                        # v7.1: Update target_soc to reflect the end of the current buy period
                        # instead of the theoretical daily target (as requested by USER).
                        if cur_hour in target_hours_sorted and man.get_setting(CONF_DYNAMIC_SOC_BUY, True):
                            target_soc = float(round_f(soc_at_end, 1))
                    except Exception as e:
                        _LOGGER.error("Error in MarketStrategy BUY simulation: %s", e)
                        res["buy_simulation"] = {
                            "projected_soc_at_start_pct": float(b_soc),
                            "projected_soc_at_end_pct": float(b_soc),
                            "projected_soc_morning_pct": float(b_soc),
                            "error": str(e)
                        }
                else: # sell
                    # Initial defaults for robustness
                    arb_gain = 0.0
                    cheap_h_back = None
                    best_buy_h = None
                    cheap_p_back = 0.0
                    cur_p_f = float(normalize_float(today_prices.get(str(cur_hour), 0.0)))
                    
                    base_target = float(man.get_setting(CONF_TARGET_SOC_SELL, 20.0))
                    occ_coeff = float(man.get_occupancy_coefficient())
                    
                    budget_data_sell = {}
                    eff_coeff_val = 1.0
                    if man.get_setting(CONF_DYNAMIC_SOC_SELL, True):
                        budget_data_raw = self.get_budget_and_permissions(man.custom_period, skip_strategy_check=True)
                        if budget_data_raw:
                            budget_data_sell = budget_data_raw
                            eff_coeff_val = float(normalize_float(budget_data_sell.get("efficiency_coefficient", 1.0)))
                        
                    
                    # Correct reserve for House needs: Now -> Midnight -> Sunrise Tomorrow
                    rem_cons_today = float(normalize_float(budget_data_sell.get("expected_consumption", 0.0)))
                    avg_prof_cons = man.get_average_profile("consumption_base", man.custom_period, "all")
                    cons_night_morning = sum(float(normalize_float(avg_prof_cons.get(str(h), 0.0))) for h in range(0, sunrise_h)) * occ_coeff
                    
                    # Also include tomorrow morning solar until sunrise in the budget
                    gen_night_morning = sum(float(normalize_float(avg_prof_gen.get(str(h), 0.0))) for h in range(0, sunrise_h))
                    f_tom_raw = man.get_forecast_value(man.forecast_tomorrow_sensor)
                    f_tom = float(f_tom_raw) if f_tom_raw is not None else 0.0
                    total_hist_gen_val = sum(float(normalize_float(avg_prof_gen.get(str(h), 0.0))) for h in range(24))
                    morning_solar_ac = f_tom * (gen_night_morning / total_hist_gen_val) if total_hist_gen_val > 0.1 else 0.0
                    
                    eff = eff_coeff_val if eff_coeff_val > 0.1 else 0.95
                    
                    # House survivability: Target SOC at Sunrise (e.g. 13% un-reducible + 15% buffer = 28%)
                    soc_buffer_val = float(man.get_setting(CONF_SOC_BUFFER, 15.0))
                    
                    # Adaptive buffer (v5.4): 0% if solar covers house needs today.
                    active_buffer = soc_buffer_val
                    
                    has_solar_coming = man.get_expected_remaining("generation") > 0.5
                    is_morning = (cur_hour < 13)
                    
                    # Logic: If (Solar Today) > (Cons Today) + 2kWh margin, we don't need buffer now.
                    solar_left = float(normalize_float(budget_data_sell.get("forecast_val", 0.0)))
                    cons_left = float(normalize_float(budget_data_sell.get("expected_consumption", 0.0)))
                    if solar_left > (cons_left + 2.0) and is_morning:
                        active_buffer = 0.0
                    else:
                        is_evening_sale = any(h > 13 for h in target_hours_sorted) if target_hours_sorted else True
                        if not is_evening_sale and has_solar_coming:
                            active_buffer = 0.0

                    min_soc_val = float(man.get_setting(CONF_MIN_SOC_BUY, 10.0))
                    # Hard Target for tomorrow morning (always includes full buffer)
                    target_morning_soc = min_soc_val + soc_buffer_val
                    # Dynamic floor for NOW (can be adaptive 0% buffer)
                    active_floor_soc = min_soc_val + active_buffer
                    
                    # AC Balance until Sunrise tomorrow
                    # budget_data_sell.get("expected_consumption") ALREADY includes both today's remaining AND night until 8 AM
                    # We adjust it precisely to our sunrise_h
                    comp_cons_to_8am = float(normalize_float(budget_data_sell.get("expected_consumption", 0.0)))
                    # If sunrise is e.g. 6AM instead of 8AM, we subtract 6-8AM from budget
                    diff_range = range(min(sunrise_h, 8), max(sunrise_h, 8))
                    diff_kwh = sum(float(normalize_float(avg_prof_cons.get(str(h), 0.0))) for h in diff_range) * occ_coeff
                    total_cons_to_sunrise = comp_cons_to_8am - diff_kwh if sunrise_h < 8 else comp_cons_to_8am + diff_kwh
                    
                    rem_solar_today = float(normalize_float(budget_data_sell.get("forecast_val", 0.0)))
                    total_solar_to_sunrise = rem_solar_today + morning_solar_ac
                    
                    # Also count energy for non-solar-only managed loads until sunrise
                    managed_needed_sunrise = 0.0
                    sorted_loads = man.deduct_settings.items()
                    for s_id, s_conf in sorted_loads:
                        if not isinstance(s_conf, dict): continue
                        if bool(s_conf.get(CONF_ONLY_SOLAR, False)):
                            continue # Solar-only loads don't drain batt at night
                        
                        _, rem_kwh, is_cyclic, _ = man.get_managed_load_stats(str(s_id))
                        # Today's remaining
                        managed_needed_sunrise += float(rem_kwh)
                        # Tomorrow 0-8 AM (entire required amount for non-cyclic)
                        if not bool(s_conf.get(CONF_IS_CYCLIC, False)):
                            managed_needed_sunrise += float(s_conf.get("required_kwh", 2.0))
                    
                    # Replacement Cost Logic: 
                    # If we sell now, and tomorrow morning we have EXCESS solar (more than house needs), 
                    # then the "cost" of that energy is 0 (it would have been sold anyway).
                    # But if tomorrow we will be short on solar, then selling now means we lose "free" energy.
                    tomorrow_solar_total = f_tom
                    
                    # 1. First safety check: Base consumption tomorrow (essential needs only)
                    tomorrow_cons_base = float(sum(man.get_average_profile("consumption_base", man.custom_period, tom_idx).values())) * occ_coeff
                    base_deficit_tomorrow = max(0.0, tomorrow_cons_base - tomorrow_solar_total)
                    
                    # 2. Planning: Total consumption (full profile with all historical loads)
                    tomorrow_cons_total = float(sum(man.get_average_profile("consumption_total", man.custom_period, tom_idx).values())) * occ_coeff
                    
                    # Deficit for the full profile (used for conservative solar_is_excess check)
                    tomorrow_deficit_full = max(0.0, tomorrow_cons_total - tomorrow_solar_total)
                    solar_is_excess = bool(tomorrow_solar_total > tomorrow_cons_total + 1.5) # 1.5kWh buffer
                    
                    # PRECISE SIMULATION-BASED CALCULATION (v6.2 Modular)
                    if man.get_setting(CONF_DYNAMIC_SOC_SELL, True):
                        # 1. Run Baseline Simulation
                        natural_morning_soc = self._get_sunrise_baseline_soc(
                            b_soc, now, sunrise_h, best_buy_pair, 
                            all_buy_prices, threshold, eff, deg_cost, max_p
                        )
                        
                        # 2. Available energy is the extra above target_morning_soc (Safety margin)
                        available_sell_ac = self._calculate_sunrise_surplus(
                            natural_morning_soc, min_soc_val, soc_buffer_val, b_cap, eff
                        )
                    else:
                        # Simple mode: energy above target SOC is sellable
                        available_sell_ac = float(max(0.0, (batt_energy_val - (base_target * b_cap / 100.0)) * eff))

                    upcoming = [h for h in target_hours_sorted if h >= cur_hour]
                    block_len = 0
                    if upcoming:
                        block_len = 1
                        for i in range(1, len(upcoming)):
                            if upcoming[i] == upcoming[i-1] + 1:
                                block_len += 1
                            else:
                                break
                    
                    num_peaks_left_raw = float(block_len)
                    is_in_peak = bool(cur_hour in target_hours_sorted)
                    if is_in_peak:
                        # Use remaining minutes for more stable power calculation
                        num_peaks_left = max(0.1, (num_peaks_left_raw - 1) + (60 - now.minute) / 60.0)
                    else:
                        num_peaks_left = float(num_peaks_left_raw) or 1.0
                    
                    # --- TWO-STEP SAFETY CHECK (Refined v6.2) ---
                    # 1. Base-only Gatekeeper: Can we cover Essential House Needs for the next 24+ hours?
                    ai_soc_floor_base = self._calc_immediate_safety_floor(
                        min_soc_val, active_buffer, total_cons_to_sunrise, 
                        base_deficit_tomorrow, total_solar_to_sunrise, b_cap, eff
                    )
                    
                    # 2. Daily Surplus Calculation (Sunrise-Aware v6.2)
                    available_sell_dc = self._calculate_sunrise_surplus(
                        natural_morning_soc, min_soc_val, soc_buffer_val, b_cap, 1.0
                    )
                    surplus_soc_at_sunrise = (available_sell_dc / b_cap * 100.0) if b_cap > 0.1 else 0.0
                    
                    # Final safety floor
                    ai_soc_floor_final = max(target_morning_soc, ai_soc_floor_base)
                    
                    # Arbitrage math for the Gatekeeper logic
                    p_bb, h_bb = get_best_buyback(cur_hour) 
                    gain_vs_buyback = 0.0
                    if h_bb is not None:
                         gain_vs_buyback = float(cur_p_f * eff - p_bb - deg_cost)
                    
                    decision_tag = f"Лимит: {target_morning_soc:.0f}% на {sunrise_h:02d}:00"
                    arbitrage_is_best = False
                    result_is_profitable = bool(gain_vs_buyback >= threshold)
                    
                    if is_in_peak:
                        if result_is_profitable:
                            decision_tag = "Арбитраж (Цена выгоднее выкупа)"
                            arbitrage_is_best = True
                        elif solar_is_excess:
                            decision_tag = "Продажа излишков (Солнца завтра много)"
                            arbitrage_is_best = True
                        else:
                            decision_tag = "Экономия (Солнца мало, откупа нет)"
                            arbitrage_is_best = False

                    # Final Permission Check
                    if b_soc < ai_soc_floor_base and not (arbitrage_is_best and result_is_profitable):
                        # Throttled/Idle because base needs for tomorrow are not guaranteed
                        target_soc = ai_soc_floor_base
                        available_sell_ac = 0.0
                        if is_in_peak and not arbitrage_is_best:
                            decision_tag = "Защита базы (Завтра мало солнца)"
                    else:
                        # We are allowed to sell the surplus!
                        target_soc = target_morning_soc
                        available_sell_ac = float(max(0.0, available_sell_dc * eff))
                    
                    # Recommended power: Balanced nearest-window allocation (v5.5)
                    # We spread the sunrise surplus evenly across the current contiguous peak block.
                    power_peak = available_sell_ac / num_peaks_left
                    power_needed = float(max(0.0, power_peak))
                    
                    if man.get_setting(CONF_DYNAMIC_SOC_SELL, True):
                        target_soc = float(target_soc)
                    else:
                        target_soc = base_target

                    # Ensure global_arb_note is always consistent
                    best_buy_p, best_buy_h = get_best_buyback(cur_hour)
                    if best_buy_h is not None:
                        pot_gain_val = cur_p_f * eff - best_buy_p - deg_cost
                        global_arb_note = f"Откуп в {self._format_h(best_buy_h)} (выгода {pot_gain_val:.2f})"
                    else:
                        global_arb_note = "Нет окна откупа"

                    if man.get_setting(CONF_DYNAMIC_SOC_SELL, True):
                        res["arbitrage_decision"] = f"{decision_tag} | {global_arb_note}"
                    else:
                        res["arbitrage_decision"] = "Ручной режим (AI выкл.)"
                    target_soc = float(min(100.0, target_soc))
                    delta_available_dc = available_sell_ac / eff

                    # --- SELL SIMULATION ---
                    # Extend simulation to tomorrow morning (Sunrise) or end of peaks, whichever is later
                    # v7.8 - Ensure simulation covers at least 24h OR until sunrise tomorrow
                    sim_end_h = max(cur_hour + 24, 24 + sunrise_h + 1)
                    sim_range = list(range(cur_hour, sim_end_h))
                    
                    # Use the actual calculated power_needed for the simulation
                    sim_commands = {int(h): -power_needed for h in target_hours_sorted if h >= cur_hour}
                    
                    # v7.8.7 - Only include buy-back in the simulation if it's actually profitable 
                    # and planned. Otherwise we "hallucinate" energy in the morning SOC.
                    if best_buy_h is not None and best_buy_h < sim_end_h:
                        pot_gain_val = cur_p_f * eff - best_buy_p - deg_cost
                        min_profit = man.get_setting(CONF_ARBITRAGE_MIN_PROFIT, 0.05)
                        if pot_gain_val >= min_profit:
                            sim_commands[int(best_buy_h)] = float(max_p)

                    _, sim_log, _ = self.run_soc_simulation(b_soc, sim_range, now, sim_commands)
                    
                    # 1. Projected SOC at START of the first peak
                    first_h_sell = min(t for t in target_hours_sorted if t >= cur_hour) if target_hours_sorted else None
                    if first_h_sell is not None and first_h_sell > cur_hour:
                        prev_h = first_h_sell - 1
                        key_start = f"{prev_h % 24:02d}:59" + (" (Завтра)" if prev_h >= 24 else "")
                        soc_at_start = self._get_soc_from_log(sim_log, key_start, b_soc)
                    else:
                        soc_at_start = b_soc
                        
                    # 2. Projected SOC AFTER the last peak
                    last_h_sell = max(target_hours_sorted) if target_hours_sorted else None
                    if last_h_sell is not None:
                        key_after = f"{last_h_sell % 24:02d}:59" + (" (Завтра)" if last_h_sell >= 24 else "")
                        soc_after = self._get_soc_from_log(sim_log, key_after, b_soc)
                    else:
                        soc_after = b_soc
                    
                    # 3. Projected SOC TOMORROW MORNING (at Dynamic Sunrise)
                    key_morning = f"{sunrise_h-1:02d}:59 (Завтра)"
                    soc_morning = self._get_soc_from_log(sim_log, key_morning, soc_after)

                    # v7.2 - CLEANUP: If no sale is currently planned for today, return current SOC
                    # to avoid "nonsense" projections in the UI.
                    if not target_hours_sorted:
                        power_needed = 0.0
                        soc_at_start = b_soc
                        soc_after = b_soc
                        # soc_morning remains as natural discharge result

                    res["sell_simulation"] = {
                        "projected_soc_at_sale_start_pct": float(round_f(soc_at_start, 1)),
                        "projected_soc_after_sale_pct": float(round_f(soc_after, 1)),
                        "projected_soc_morning_pct": float(round_f(soc_morning, 1)),
                        "log": sim_log
                    }

                    # v7.1: Update target_soc to reflect the end of the current sale period
                    # instead of the fixed morning value (as requested by USER).
                    if is_in_peak and man.get_setting(CONF_DYNAMIC_SOC_SELL, True):
                        target_soc = float(round_f(soc_after, 1))
                    
                    # Arbitrage details for UI attributes
                    res["arbitrage_buyback"] = {
                        "power_kw": 0.0,
                        "note": "Нет выгодного окна для откупа" if not arbitrage_is_best else "",
                        "available_kwh": float(round_f(available_sell_ac, 2)),
                        "sunrise_hour": sunrise_h,
                        "soc_buffer_pct": float(soc_buffer_val),
                        "target_morning_soc_pct": float(target_morning_soc),
                        "reserve_kwh": float(round_f(target_morning_soc * b_cap / 100.0, 2)),
                        "energy_to_wait_kwh": float(round_f(total_cons_to_sunrise, 2)),
                        "ai_floor_soc_pct": float(round_f(ai_soc_floor_final, 1)),
                    }
                    if h_bb is not None and (gain_vs_buyback >= threshold):
                        res["arbitrage_buyback"]["power_kw"] = max_p
                        res["arbitrage_buyback"]["note"] = f"Откуп в {self._format_h(h_bb)} по {p_bb:.2f}"
                
            # Use current peak power only if we are actually in a peak hour
            # Otherwise show 0 as real command, but attributes will show the potential
            in_peak = (cur_hour in target_hours_sorted) and (power_needed > 0.01)
            real_cmd_p = power_needed if (mode == "sell" and in_peak) else 0.0
            if mode == "buy" and in_peak:
                real_cmd_p = power_needed
            
            # STATE TRANSITION FIX: If we have power and we are in peak, we are ACTIVE
            if in_peak and power_needed > 0.01:
                res["state"] = "active"

            res["recommended_power_kw"] = float(round_f(min(float(power_needed), max_p), 3))
            # Only show hours that actually have planned power
            if mode == "buy":
                actual_active = [h for h in target_hours_sorted if charge_commands.get(h, 0.0) > 0.01]
            else:
                # v7.2.1: Always show future peak windows if they are identified, 
                # even if current power_needed is 0 (e.g. waiting for peak or saving battery).
                actual_active = [h for h in target_hours_sorted if h >= cur_hour]

            # Regenerate active_periods based on final filtered hours (v6.18)
            final_periods = []
            if actual_active:
                sorted_fit = sorted(list(set(actual_active)))
                if sorted_fit:
                    groups = []
                    cur_group = [sorted_fit[0]]
                    for i in range(1, len(sorted_fit)):
                        if sorted_fit[i] == sorted_fit[i-1] + 1:
                            cur_group.append(sorted_fit[i])
                        else:
                            groups.append(cur_group)
                            cur_group = [sorted_fit[i]]
                    groups.append(cur_group)
                    for g in groups:
                        h_min = min(g) % 24
                        h_max = max(g) % 24
                        suffix_min = " (Завтра)" if min(g) >= 24 else ""
                        suffix_max = " (Завтра)" if max(g) >= 24 else ""
                        if len(g) == 1:
                            final_periods.append(f"{h_min:02d}:00 - {h_min:02d}:59{suffix_min}")
                        else:
                            final_periods.append(f"{h_min:02d}:00{suffix_min} - {h_max:02d}:59{suffix_max}")

            res["active_hours"] = actual_active
            res["active_hours_formatted"] = ", ".join([self._format_h(h) for h in actual_active])
            res["active_periods"] = ", ".join(final_periods) if final_periods else "Нет"
            res["target_soc"] = float(round_f(target_soc, 1))
            
            # Mode Detection Logic (Moved from sensor.py for better centralization)
            cur_mode_text = "Ожидание"
            state = res.get("state")
            if state == "active":
                if mode == "buy":
                    reason_tag = "Зарядка"
                    c_reason = res.get("charge_reason", "manual")
                    if c_reason == "survival": reason_tag = "Зарядка (Выживание)"
                    elif c_reason == "arbitrage": reason_tag = "Зарядка (Арбитраж)"
                    elif c_reason == "negative": reason_tag = "Зарядка (Отриц. цена)"
                    
                    cur_mode_text = f"Экстренная {reason_tag}" if res.get("charge_reason") == "survival" and b_soc < 15 else f"Активная {reason_tag}"
                else:
                    rec_p = float(res.get("recommended_power_kw", 0.0) or 0.0)
                    if rec_p <= 0:
                        if "Экономия" in decision_tag:
                            cur_mode_text = "Ожидание (Экономия)"
                        else:
                            cur_mode_text = "Ожидание (Пусто)"
                    else:
                        tag = "Консервативно"
                        if arbitrage_is_best:
                            tag = "Арбитраж" if "Арбитраж" in decision_tag else "Излишки солнца"
                        cur_mode_text = f"Активная продажа ({tag})"
            elif res.get("charge_reason") == "none" and mode == "buy":
                cur_mode_text = "В покупке нет необходимости"
            elif state == "preparing_arbitrage":
                if mode == "buy":
                    c_reason = res.get("charge_reason", "manual")
                    if c_reason == "survival":
                        cur_mode_text = "Ожидание (Заряд для дома)"
                    elif c_reason == "arbitrage":
                        cur_mode_text = "Ожидание (Заряд арбитража)"
                    else:
                        cur_mode_text = "Ожидание дешевой цены"
                else:
                    cur_mode_text = "Ожидание арбитража"
            elif state in ["price_limit_not_met", "unprofitable_arbitrage"] or not target_hours_sorted or state == "idle":
                if mode == "buy":
                    if res.get("charge_reason") == "none":
                        cur_mode_text = "В покупке нет необходимости"
                    else:
                        cur_mode_text = "Нет ценового окна"
                else: # sell
                    if state == "idle":
                         cur_mode_text = "Ожидание"
                    else:
                         cur_mode_text = "Нет ценового окна"
            elif state == "idle":
                if mode == "buy" and res.get("charge_reason") == "survival":
                    cur_mode_text = "Ожидание (Экстренно)"
                elif mode == "sell":
                    arb_dec = str(res.get("arbitrage_decision", ""))
                    if "Экономия" in arb_dec:
                        cur_mode_text = "Ожидание (Экономия заряда)"
                    elif "Арбитраж" in arb_dec:
                        cur_mode_text = "Ожидание (Арбитраж)"
                    else:
                        cur_mode_text = "Ожидание (Пик цены)"
            
            res["current_mode_text"] = cur_mode_text
            
            self._strategy_cache[cache_key] = {"time": now, "res": res}
            return res
        finally:
            self._calculating_strategy = old_calc

