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

    def _calculate_sunrise_surplus(self, natural_morning_soc, min_soc, buffer_soc, batt_cap, eff, user_soc_limit=0.0):
        """Strictly calculates surplus above the highest floor (safety mark or user limit)."""
        # v11.1.77: Respect the highest floor (Morning safety vs User defined min SOC)
        target_mark = float(max(min_soc + buffer_soc, user_soc_limit))
        extra_soc_pct = max(0.0, natural_morning_soc - target_mark)
        return float(extra_soc_pct * batt_cap / 100.0)


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
        
        cache_key = "budget_permissions"
        cached = self._strategy_cache.get(cache_key)
        if cached and (now - cached["time"]).total_seconds() < 30:
            return cached["res"]
        
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
                # v11.3.62: Proportional current hour to prevent "sawtooth" effects at hour boundaries
                past_h_gen = float(sum(float(dist.get(str(h), 0.0)) for h in range(cur_hour)))
                current_h_gen = float(dist.get(str(cur_hour), 0.0)) * (now.minute / 60.0)
                hist_gen_so_far = past_h_gen + current_h_gen
                total_hist_gen = float(sum(float(dist.get(str(h), 0.0)) for h in range(24)))
                active_dist = dist
            else:
                p_gen_norm = {h: float(normalize_float(v)) for h, v in p_gen.items()}
                past_h_gen = float(sum(p_gen_norm.get(str(h), 0.0) for h in range(cur_hour)))
                current_h_gen = float(p_gen_norm.get(str(cur_hour), 0.0)) * (now.minute / 60.0)
                hist_gen_so_far = past_h_gen + current_h_gen
                total_hist_gen = float(sum(p_gen_norm.values()))
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
            
            # B. Today's Performance (Current Efficiency vs Time-Proportional Plan)
            # v11.3.64: Reactive "Local" coefficient (Actual / Expected So Far).
            # This allows faster recovery after clouds pass, as it doesn't penalize future
            # forecast by comparing to a "perfect morning promise" total.
            today_coeff = 1.0
            if hist_gen_so_far > 0.5:
                today_coeff = float(max(0.2, min(actual_today / hist_gen_so_far, 2.0)))
            
            # --- Curtailment Correction (v4.2) ---
            # The inverter only chokes PV panels in 'stop_sale' mode if there is "no room for energy"
            # (i.e., battery is full or nearly full).
            is_stop_sale = getattr(man, "current_inverter_mode", "") == "stop_sale"
            if is_stop_sale and today_coeff < 1.0:
                # Check if battery is full enough to cause curtailment
                b_soc_cur, _, _ = man.get_battery_state()
                if b_soc_cur > 90:
                    # We suspect curtailment because export is forbidden AND battery is full.
                    # In this case, frozen high-water performance (or at least 1.0/history) is used.
                    old_today = today_coeff
                    today_coeff = max(today_coeff, hist_coeff, 1.0)
                    if abs(today_coeff - old_today) > 0.01:
                        _LOGGER.debug(f"[Strategy] Curtailment detected (mode=stop_sale, SOC={b_soc_cur}%). Corrected today_coeff: {old_today:.2f} -> {today_coeff:.2f}")

            # C. Blended Coeff: Weighted average of Today vs 1.0 (Baseline)
            # v11.3.64: Using fraction_so_far as the stable progress measure.
            external_progress = max(0.0, min(fraction_so_far, 1.0))
            
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
            
            min_soc_val = man.get_setting(CONF_MIN_SOC_BAT, 10.0)
            min_soc = float(min_soc_val) if min_soc_val is not None else 10.0
            eff_coeff = float(self.get_efficiency_coefficient() or 1.0)
                        
            # 3. Expected consumption (v7.9.4 - Base profile + Simulation Guard)
            # Use 'base' profile as the absolute essential house survival floor.
            # Use 'base' profile as the absolute essential house survival floor.
            occ_coeff, occ_home, occ_away, occ_cur, occ_sensors, occ_avg_home, occ_avg_away = man.get_occupancy_coefficient()
            occ_coeff = float(occ_coeff)
            occ_home = float(occ_home)
            occ_away = float(occ_away)
            occ_cur = int(occ_cur)
            occ_sensors = list(occ_sensors)
            occ_avg_home = float(occ_avg_home)
            occ_avg_away = float(occ_avg_away)
            sunrise_hour = man.get_sunrise_hour() or 6
            base_rem_today = float(man.get_expected_remaining("consumption_base", eff_period, day_idx)) * occ_coeff
            base_night = float(man.get_expected_night("consumption_base", eff_period, day_idx, until_hour=sunrise_hour)) * occ_coeff
            expected_base_consumption = float(base_rem_today + base_night)
            
            # v7.9.4 - Survival Projection Gate
            # We check if even WITH just the base load, we can reach morning safely.
            soc_buffer = float(man.get_setting(CONF_SOC_BUFFER, 15.0))
            survival_threshold = min_soc + soc_buffer
            
            # Find the SOC at the start of tomorrow's generation (sunrise)
            # v11.1.19 - Move sunrise calculation UP to limit simulation range
            sunrise_h = 8 # Default
            prof_gen = man.get_average_profile("generation", eff_period, day_idx)
            for h in range(24):
                if float(prof_gen.get(str(h), 0.0)) > 0.05:
                    sunrise_h = h
                    break
            
            # Quick accurate simulation (baseline only) until tomorrow's sunrise
            # This allows overflow_kwh to accurately represent "Today's" exportable surplus.
            sim_end_h = 24 + sunrise_h
            sim_range = list(range(cur_hour, sim_end_h))
            
            sim_res_soc, sim_log, overflow_kwh = self.run_soc_simulation(
                start_soc=b_soc_f,
                sim_range=sim_range,
                now=now,
                b_min_soc=0.0, # Budget calc needs natural discharge
                house_profile_override="consumption_base"
            )

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
                # v11.5.5: Switch from instantaneous to 10-minute averaged sensors to prevent switching chatter
                avg_l = float(getattr(man, "avg_load_kw", 0.0))
                avg_g = float(getattr(man, "avg_gen_kw", 0.0))
                
                # Check if history is populated (prevent zeroing on fresh reboot)
                if avg_l > 0.01 or avg_g > 0.01 or getattr(man, "power_history", []):
                    load_kw = avg_l
                    gen_kw = avg_g
                else:
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
                    # v11.5.4: Use actual measured power instead of learned power to avoid math collapse if learned power is corrupted
                    p_val = float(man.last_known_power.get(str(s_id), 0.0)) / 1000.0
                    if p_val <= 0.1:
                        # Fallback
                        p_val = float(man.learned_real_power.get(str(s_id), 0.0)) / 1000.0
                    current_managed_load_kw += min(20.0, p_val) # Clamp to sane values
            
            raw_house_deficit = float(load_kw - gen_kw)
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
                
                # v11.5.4: Safeguard against e_kw=0 bypassing all bottleneck checks!
                if e_kw < 0.1 and is_pulling:
                    cur_w = float(man.last_known_power.get(s_id_s, 0.0))
                    if cur_w > 100.0:
                        e_kw = cur_w / 1000.0
                    else:
                        e_kw = 2.0  # Safe fallback to trigger threshold limits!
                        
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
                        # v11.5.4: Strongest assertion: If RAW deficit of the whole house is severe (>500W), AND base_house_load already ate PV, kill only_solar!
                        if available_gen_kw < float(e_kw * 0.6): 
                            gen_bottleneck = True
                        elif is_pulling and (raw_house_deficit > 0.5) and (available_gen_kw < e_kw):
                            gen_bottleneck = True
                elif initial_power_kw > 0.5 and available_power_kw < 0:
                    power_bottleneck = True

                # v11.1.97 - Block managed loads during active selling modes
                inverter_mode = getattr(man, "current_inverter_mode", "")
                is_selling_mode = inverter_mode in ("sale_pv_no_bat", "sale_pv_bat")

                price_suffix = " (Беспл. цена)" if is_free_price else ""
                if is_selling_mode and not is_free_price:
                    permissions[s_id_s] = False
                    # v11.4.46: dict lookup avoids else-trap that labelled ANY unknown mode as "PV+АКБ"
                    _mode_labels = {
                        "sale_pv_no_bat": "Продажа PV (без АКБ)",
                        "sale_pv_bat": "Продажа PV+АКБ",
                    }
                    mode_label = _mode_labels.get(inverter_mode, inverter_mode)
                    # v11.4.46: expose the REAL underlying reason so user sees WHY, not just WHAT
                    if power_bottleneck:
                        _extra = f" | Дефицит мощности ({available_power_kw:.2f}кВт)"
                    elif gen_bottleneck:
                        _extra = f" | Нет генерации ({available_gen_kw:.2f}кВт)"
                    elif initial_budget < -0.1:
                        _extra = f" | Бюджет {initial_budget:.2f}кВт·ч"
                    else:
                        _extra = ""
                    permissions_reasons[s_id_s] = f"Запрет: Режим '{mode_label}'{_extra}"
                elif req_kwh > 0 and consumed >= req_kwh:
                    permissions[s_id_s] = False
                    permissions_reasons[s_id_s] = f"Норма выполнена ({consumed:.2f}/{req_kwh}{price_suffix})"
                elif power_bottleneck:
                    permissions[s_id_s] = False
                    permissions_reasons[s_id_s] = f"Дефицит мощности ({available_power_kw:.2f} < {p_thresh if not is_pulling else p_lim:.2f}{price_suffix})"
                elif gen_bottleneck:
                    permissions[s_id_s] = False
                    permissions_reasons[s_id_s] = "Недостаточно генерации (Только солнце)"
                elif only_solar and initial_budget < -0.3 and not is_free_price:
                    # v11.4.42: Solar surplus must go to BATTERY, not to load, when morning SOC is critical.
                    # only_solar loads bypass the normal budget check, but if the battery itself
                    # can't make it to sunrise, diverting solar to loads makes things worse.
                    permissions[s_id_s] = False
                    permissions_reasons[s_id_s] = f"Заряд АКБ (бюджет {initial_budget:.2f} кВт·ч)"
                elif available_budget < 0.1 and not only_solar and not is_free_price:
                    permissions[s_id_s] = False
                    permissions_reasons[s_id_s] = f"Лимит исчерпан ({available_budget:.2f} < 0.1)"
                else:
                    permissions[s_id_s] = True
                    # v11.5.6: Display accurate reason for only_solar devices
                    if only_solar and not is_free_price:
                        permissions_reasons[s_id_s] = f"Ок (Профицит солнца: {available_gen_kw:.2f} кВт)"
                    else:
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
                    
            # v11.1.19 - Use the returned overflow_kwh directly.
            # Since simulation range is limited to sunrise, this IS today's overflow.
            overflow_today = float(overflow_kwh or 0.0)
            
            batt_surplus = self._calculate_sunrise_surplus(projected_morning_soc, min_soc, soc_buffer, b_cap_f, eff_coeff)
            
            return_res = {
                "initial_budget": float(initial_budget or 0.0),
                "battery_capacity_kwh": float(b_cap_f or 0.0),
                "projected_morning_soc": float(round_f(projected_morning_soc, 1)),
                "survival_threshold": float(round_f(survival_threshold, 1)),
                "battery_energy_kwh": round_f(b_energy_f, 3),
                "expected_consumption_kwh": round_f(expected_base_consumption, 3),
                "sun_overflow_kwh": round_f(overflow_today, 3),
                "battery_surplus_kwh": round_f(batt_surplus, 3),
                "potential_export_kwh": round_f(overflow_today + batt_surplus, 3),
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
                "debug_occ_home_hours": int(occ_home),
                "debug_occ_away_hours": int(occ_away),
                "debug_occ_current": int(occ_cur),
                "debug_occ_sensors": occ_sensors,
                "debug_occ_avg_home": float(occ_avg_home),
                "debug_occ_avg_away": float(occ_avg_away),
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
            self._strategy_cache["budget_permissions"] = {"time": now, "res": return_res}
            return return_res
        finally:
            self._calculating_strategy = old_calc

    def _get_soc_from_log(self, log: dict, key: str, default: Optional[float]) -> Optional[float]:
        """Safely extract SOC float from simulation log (handles both float and dict formats)."""
        val = log.get(key)
        if isinstance(val, dict):
            res = val.get("soc", default)
        else:
            res = val if val is not None else default
        return float(res) if res is not None else None

    def run_soc_simulation(self, start_soc, sim_range, now, commands=None, b_min_soc=0.0, man=None, house_profile_override=None, no_battery_charge=False, no_battery_charge_until=None, pv_curtail_hours=None, ignore_blended=False, dynamic_floors=None):
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

        # v11.4.49: Pre-load historical losses profile for idle_p computation inside loop.
        # Using historical hourly rate (kWh/h) per simulated hour is correct;
        # the old man.current_losses was an intra-hour accumulator, yielding 0 at hour
        # boundaries and an average of 50% of the real rate.
        prof_losses = dict(man.get_average_profile("losses", 7))
        
        # v11.6.57: ignore_blended allows skipping the last_blended_coeff (which can be <0.2 in the morning)
        blended_coeff = 1.0 if ignore_blended else float(getattr(man, "last_blended_coeff", 1.0))
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

            # 3. Expected consumption (v7.9.4 - Base profile)
            p_cons = prof_cons_tom if is_tom else prof_cons_today
            
            # v11.4.49: Always use consumption_total (as configured) — both total and base
            # are ≈identical at night anyway (backup confirmed: night profiles differ <0.01 kWh/h).
            # Over-conservatism is preferred per user: 'better too much than too little at sunrise'.
            
            occ_coeff, _, _, _, _, _, _ = man.get_occupancy_coefficient()
            occ_coeff = float(occ_coeff)
            expected_cons_kw = float(normalize_float(p_cons.get(h_str, 0.0))) * occ_coeff

            
            # v11.1.15 - Blended Anchor: Smoothly transition from profile to real-time load
            # to avoid discontinuities at the top of the hour (v7.9.7 fix)
            if i == 0:
                anchor_weight = max(0.0, min(1.0, (now.minute / 60.0)))
                # Only anchor if we have a reasonable load reading
                real_load = float(getattr(man, "avg_base_load_kw" if house_profile_override == "consumption_base" else "avg_load_kw", expected_cons_kw))
                expected_cons_kw = (real_load * anchor_weight) + (expected_cons_kw * (1.0 - anchor_weight))
            
            # First hour solar correction: 
            if i == 0:
                # v11.1.15 - Blended Solar Anchor: Same logic as load to prevent sawtooth
                real_gen_kw = float(getattr(man, "avg_gen_kw", 0.0))
                if real_gen_kw > 0.01:
                    anchor_weight = max(0.0, min(1.0, (now.minute / 60.0)))
                    expected_gen_kw = (real_gen_kw * anchor_weight) + (expected_gen_kw * (1.0 - anchor_weight))

            # v11.4.49: Idle/losses correction — add BEFORE net computation.
            # BUG fixed: idle_p was previously added to expected_cons_kw AFTER total_net_kw
            # was already calculated → had ZERO effect on SOC simulation (only polluted log).
            # Additionally, man.current_losses is an intra-hour kWh accumulator:
            #   • resets to 0 at every hour boundary → idle_p = 0 exactly at hour start
            #   • averages ~0.05 kWh mid-hour but historical rate is 0.10 kWh/h at night
            # Fix: use pre-loaded historical losses profile (kWh/h) for THIS simulated hour.
            if eff_coeff < 0.999:  # If eff sensor embeds losses already, skip to avoid double-count
                idle_p = float(prof_losses.get(h_str, 0.05))
                expected_cons_kw += idle_p

            # 4. Inverter Command (AI Buying/Selling)
            cmd_p = float(commands.get(int(h_abs), 0.0)) if commands else 0.0

            # v11.6.154: Pure Discharge Logic.
            # If we are selling (cmd_p > 0), PV does NOT charge the battery.
            # It only covers the house load, and the rest goes to the grid (ignored here).
            is_selling = bool(cmd_p > 0.01)
            
            if no_battery_charge or is_selling or (no_battery_charge_until is not None and h_abs < no_battery_charge_until):
                # PV only covers load, no battery charge from surplus
                p_for_house = min(expected_gen_kw, expected_cons_kw)
                rem_cons = expected_cons_kw - p_for_house
                # Battery net: discharge command PLUS remaining house needs
                total_net_kw = -cmd_p - rem_cons
            else:
                # Normal mode: PV covers load and then charges battery
                total_net_kw = expected_gen_kw - expected_cons_kw - cmd_p

            
            if total_net_kw > 0.001: 
                # v11.1.62 - bat_emergency recovery: Allow charging from solar 'crumbs' even in emergency
                # to match inverter's physical behavior (steering to limit+1%).
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
            
            # v11.6.94: Removed hard floor clamp. 
            # The simulation should show NATURAL discharge below the safety floor 
            # due to house load, not artificially 'stick' to it.
            
            # Store enriched data for the 24h forecast (v11.6.1: Unified EN keys)
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
        if cached and (now - cached["time"]).total_seconds() < 30 and cached["time"].hour == now.hour:
            return cached["res"]

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
            "buy_simulation": {"projected_soc_at_start_pct": 0.0, "projected_soc_at_end_pct": 0.0, "projected_soc_morning_pct": 0.0},
            "sell_simulation": {"projected_soc_at_start_pct": 0.0, "projected_soc_after_sale_pct": 0.0, "projected_soc_morning_pct": 0.0},
            "arbitrage_decision": "Нет данных",
            "charge_reason": "none",
            "arbitrage_buyback": {"opportunity": False, "power_kw": 0.0, "note": ""}
        }
        charge_commands = {}
        can_recharge = False
        
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
            cur_p_f = all_prices.get(cur_hour, 0.0)
                
            negative_hours = [int(h) for h, p in all_prices.items() if p < 0 and h >= cur_hour]

            buy_limit = float(man.get_setting(CONF_PRICE_BUY_LIMIT, 2.0))
            sell_limit = float(man.get_setting(CONF_PRICE_SELL_LIMIT, 5.0))
            eff = float(eff_coeff)
            active_window = (cur_hour, 47) if tomorrow_prices else (cur_hour, 23)
            # End the window at :59 for clarity
            res["analyzed_window"] = f"До {self._format_h(active_window[1]).replace(':00', ':59')}"
            
            target_hours = []
            target_price = 0.0

            def get_peaks(window, is_sell, limit):
                if not window: return []
                w_vals = [float(v) for v in window.values()]
                if not w_vals: return []
                
                limit = float(limit)
                target = max(w_vals) if is_sell else min(w_vals)
                
                if (is_sell and target < limit) or (not is_sell and target > limit):
                    return []
                    
                peak_hours = [int(h) for h, p in window.items() if float(p) == target]
                peaks = set()
                
                for peak_h in peak_hours:
                    # expand left
                    h = peak_h
                    while str(h) in window:
                        p = float(window[str(h)])
                        if (is_sell and p >= limit) or (not is_sell and p <= limit):
                            peaks.add((h, p))
                            h -= 1
                        else:
                            break
                    # expand right
                    h = peak_h + 1
                    while str(h) in window:
                        p = float(window[str(h)])
                        if (is_sell and p >= limit) or (not is_sell and p <= limit):
                            peaks.add((h, p))
                            h += 1
                        else:
                            break
                return sorted(list(peaks), key=lambda x: x[0])

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
            min_p_v = man.get_setting(CONF_ARBITRAGE_PROFIT_THRESHOLD, 0.0)
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
                        peaks_today = get_peaks(wt_filtered, False, buy_limit)
                        peaks_tom = get_peaks(wom_filtered, False, buy_limit)
                        combined = peaks_today + peaks_tom
                    
                    is_arb_window = False
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
                    
                    # v11.4.06: Clean Arbitrage reporting (Buy mode)
                    if is_arb_window:
                        s_h, b_h = best_arb_pair
                        res["arbitrage_decision"] = f"Покупаем сейчас по {cur_p_f:.2f} | Продадим в {self._format_h(s_h)} по {all_sell_prices.get(s_h, 0.0):.2f} | Выгода {max_arb_gain:.2f}"
                    else:
                        c_reason = res.get("charge_reason", "manual")
                        if c_reason == "survival":
                            res["arbitrage_decision"] = f"Зарядка для дома по {cur_p_f:.2f} (Выживание)"
                        else:
                            res["arbitrage_decision"] = f"Покупаем сейчас по {cur_p_f:.2f} | Нет выгодной цели продажи"
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

                # v11.3.32: Bi-Modal Daily Peak Strategy (Morning vs Evening)
                # Split day at 13:00 to naturally find optimal peaks for both halves.
                today_morn = {h: p for h, p in today_prices.items() if cur_hour <= int(h) < 13}
                today_eve = {h: p for h, p in today_prices.items() if cur_hour <= int(h) and int(h) >= 13}
                
                tom_morn = {h: p for h, p in tomorrow_prices.items() if int(h) < 13}
                tom_eve = {h: p for h, p in tomorrow_prices.items() if int(h) >= 13}

                raw_peaks_today = get_peaks(today_morn, True, sell_limit) + get_peaks(today_eve, True, sell_limit)
                raw_peaks_tom = get_peaks(tom_morn, True, sell_limit) + get_peaks(tom_eve, True, sell_limit)
                
                if not raw_peaks_today and not raw_peaks_tom:
                    res["state"] = "price_limit_not_met"
                    res["arbitrage_decision"] = "Нет ценового окна"
                else:
                    res["strategy_version"] = VERSION
                    dynamic_sell_ai = bool(man.get_setting(CONF_DYNAMIC_SOC_SELL, True))
                    if not dynamic_sell_ai:
                        # Use all hours meeting the limit
                        peaks_today = [(int(h), float(p)) for h, p in today_prices.items() if float(normalize_float(p)) >= sell_limit]
                        peaks_tom = [(int(h) + 24, float(p)) for h, p in tomorrow_prices.items() if float(normalize_float(p)) >= sell_limit]
                        
                        combined = peaks_today + peaks_tom
                        target_hours = sorted(list(set([int(h) for h, p in combined])))
                        target_price = float(max((p for h, p in combined), default=0.0))
                    else:
                        def _can_recharge_between(start_h, end_h, p_c, p_m):
                            if end_h <= start_h: return False, "Слишком короткий период"
                            # v11.3.42: Return True if cheap grid window exists
                            for h_ch in range(int(start_h) + 1, int(end_h)):
                                if all_buy_prices.get(h_ch, 99.0) <= buy_limit:
                                    return True, "Ок (Дешевая сеть)"
                                    
                            start_soc = float(man.get_setting(CONF_AI_DISCHARGE_LIMIT, 20.0))
                            sim_r = list(range(int(start_h) + 1, int(end_h)))
                            if not sim_r: return False, "Слишком короткий период"
                            
                            sim_s = now if start_h == cur_hour else now.replace(minute=0, second=0, microsecond=0)
                            _, log_d, _ = self.run_soc_simulation(start_soc, sim_r, sim_s, commands=None)
                            
                            max_r = start_soc
                            for st in log_d.values():
                                max_r = max(max_r, float(st.get("soc", 0)))
                                
                            max_s = 100.0 - start_soc
                            p_x = max(0.01, p_m)
                            req_rec = max_s * max(0.0, (p_x - p_c) / p_x)
                            req_soc = min(95.0, start_soc + req_rec + 1.0)
                            
                            if max_r >= req_soc:
                                return True, f"Ок (Сим. {max_r:.1f}% >= Треб. {req_soc:.1f}%)"
                            return False, f"Неблагоприятно (Сим. {max_r:.1f}% < Треб. {req_soc:.1f}%)"

                        # v11.3.42: Be more inclusive in candidates for 'skipped' reporting
                        # Instead of just get_peaks, start with ALL hours above sell_limit or profitable
                        peaks_candidates_all = []
                        all_h_possible = sorted(list(set(list(today_prices.keys()) + list(tomorrow_prices.keys()))), key=lambda x: int(x))
                        
                        # Get technical peaks for comparison
                        tech_peaks_today = [int(h) for h, p in get_peaks(today_morn, True, sell_limit) + get_peaks(today_eve, True, sell_limit)]
                        tech_peaks_tom = [int(h) + 24 for h, p in get_peaks(tom_morn, True, sell_limit) + get_peaks(tom_eve, True, sell_limit)]
                        tech_peaks_all = set(tech_peaks_today + tech_peaks_tom)

                        for h_str, p_val in today_prices.items():
                            h_int = int(h_str)
                            if h_int < cur_hour: continue
                            norm_p = float(normalize_float(p_val))
                            ok_arb, _, _, _ = is_profitable(norm_p, h_int)
                            # v11.3.54: Only consider if price meets limit OR it's a profitable arbitrage cycle. 
                            if norm_p >= sell_limit or ok_arb:
                                peaks_candidates_all.append((h_int, norm_p))
                                
                        for h_str, p_val in tomorrow_prices.items():
                            h_int = int(h_str) + 24
                            norm_p = float(normalize_float(p_val))
                            ok_arb, _, _, _ = is_profitable(norm_p, h_int)
                            # v11.3.54
                            if norm_p >= sell_limit or ok_arb:
                                peaks_candidates_all.append((h_int, norm_p))
                                
                        peaks_candidates_all.sort(key=lambda x: x[0])
                        
                        safe_peaks = []
                        last_recharge_reason = "Единичный пик"
                        skipped_reasons = []
                        
                        for i, (curr_h, curr_p) in enumerate(peaks_candidates_all):
                            is_tech_peak = bool(curr_h in tech_peaks_all)
                            future_peaks = peaks_candidates_all[i+1:]
                            
                            if not future_peaks:
                                if is_tech_peak:
                                    safe_peaks.append((curr_h, curr_p))
                                continue
                                
                            best_future_p = max(fp[1] for fp in future_peaks)
                            if curr_p < best_future_p:
                                best_future_h = next(fp[0] for fp in future_peaks if fp[1] == best_future_p)
                                cr, reason = _can_recharge_between(curr_h, best_future_h, curr_p, best_future_p)
                                if curr_p >= sell_limit: cr = True # v11.3.97: Respect User Limit
                                if cr:
                                    if is_tech_peak:
                                        safe_peaks.append((curr_h, curr_p))
                                        last_recharge_reason = reason
                                    # v11.3.47: Remove 'skipped' noise for non-peaks
                                else:
                                    # v11.3.42+44: Report skipped due to recharge deficiency
                                    if is_tech_peak:
                                        short_reason = reason.replace("Неблагоприятно", "Нет усл.").replace("Благоприятно", "Ок")
                                        skipped_reasons.append(f"{curr_h%24:02d}:00 ({short_reason})")
                            else:
                                # Primary peak (no higher future peak)
                                if is_tech_peak:
                                    safe_peaks.append((curr_h, curr_p))
                                # v11.3.47: No more 'Below peak' reporting to keep status clean
                                    
                        # v11.3.46: Final reporting and diagnostic data
                        def format_skipped(reasons):
                            if not reasons: return ""
                            seen = set()
                            res_clean = []
                            for r in reasons:
                                if r not in seen:
                                    res_clean.append(r)
                                    seen.add(r)
                            return ", ".join(res_clean)
                        
                        res["strategy_candidates"] = [f"{h%24:02d}:00" for h, p in peaks_candidates_all]
                        res["deg_cost"] = float(deg_cost)
                        res["profit_threshold"] = float(threshold)
                        
                        # Determine multi_cycle status
                        txt = format_skipped(skipped_reasons)
                        if not safe_peaks:
                            res["state"] = "price_limit_not_met"
                            res["multi_cycle"] = f"Лимит цены не достигнут (Пропуск: {txt})" if txt else "Нет выгодных окон"
                            target_hours = []
                            target_price = 0.0
                        else:
                            # v11.3.46 Logic: 
                            # If we have distinct peak windows (e.g. 09:00 and 19:00), show last_recharge_reason.
                            # If we have a cluster (19,20), but something was skipped, show skipped reasons.
                            unique_periods = len(set(h // 4 for h, p in safe_peaks)) # simplified grouping
                            if len(safe_peaks) > 1 and unique_periods > 1 and last_recharge_reason != "Единичный пик":
                                res["multi_cycle"] = last_recharge_reason
                            else:
                                if txt:
                                    res["multi_cycle"] = f"Единичный пик (Пропуск: {txt})"
                                else:
                                    res["multi_cycle"] = "Единичный пик"
                                
                            target_hours = sorted(list(set([h for h, p in safe_peaks])))
                            target_price = float(max((p for h, p in safe_peaks), default=0.0))
                        
                        _LOGGER.debug(f"[Strategy] v11.3.46: Active: {target_hours}, candidates: {res['strategy_candidates']}")

                        # Arbitrage note for the sensor
                        cheap_p_back, cheap_h_back = get_best_buyback(cur_hour)
                        cur_p_f = float(normalize_float(today_prices.get(str(cur_hour), 0.0)))
                        cur_gain = float(cur_p_f * eff - cheap_p_back - deg_cost)
                        
                        status = "Ожидание"
                        if cur_p_f >= sell_limit: status = "Продажа (Лимит)"
                        elif cur_gain >= threshold: status = "Продажа (Арбитраж)"
                        
                        detail = f"Цена {cur_p_f:.2f}" if status == "Продажа (Лимит)" else f"Сейчас {cur_p_f:.2f}. {global_arb_note}"
                        if best_arb_pair[0] is not None and best_arb_pair[0] > cur_hour and all_sell_prices.get(best_arb_pair[0], 0) > cur_p_f + 0.01:
                             detail += f" | Ждем главного пика в {self._format_h(best_arb_pair[0])}"
                        
                        res["arbitrage_decision"] = f"{status}: {detail}"

            target_hours = sorted([int(h) for h in target_hours if int(h) >= cur_hour])
            
            # Apply 12h gap truncation: only plan for the immediate block of peaks
            if target_hours:
                truncated = [target_hours[0]]
                for i in range(1, len(target_hours)):
                    # v11.3.4: If we have a double-cycle opportunity, allow large gaps between cycles.
                    # Otherwise, only plan for the immediate block of peaks (standard behavior).
                    if (target_hours[i] - target_hours[i-1] <= 12) or can_recharge:
                        truncated.append(target_hours[i])
                    else:
                        break
                target_hours = truncated

            # Survival Logic
            if mode == "buy" and b_cap > 0 and man.get_setting(CONF_DYNAMIC_SOC_BUY, True):
                # Adaptive active_window for buy mode: current hour until next sell peak for the arbitrage window
                active_window = (best_buy_pair[1], best_arb_pair[0]) if best_arb_pair[0] is not None else (best_buy_pair[1], int(best_buy_pair[1] or 0) + 1)
                
                min_soc = float(man.get_setting(CONF_MIN_SOC_BAT, 10.0))
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
                        # v11.4.52: Do NOT allow simulation to drop below 5% absolute SOC to prevent total shutdown
                        is_minor = soc_at_h >= 5.0 and (min_soc - soc_at_h) < 15.0
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
            charge_commands = {}
            sell_commands = {}
            target_soc = b_soc
            sim_soc_plan = b_soc
            if b_cap > 0.1:
                if mode == "buy":
                    # Buy mode (v11.1.51)
                    # Use existing Target SOC Buy as ceiling for AI charging (except negative price)
                    base_target = float(man.get_setting(CONF_AI_CHARGE_LIMIT, 100.0))
                    
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
                    
                    # v11.4.52: Precise sunrise-based consumption calc identical to sell logic
                    comp_cons_to_8am = float(normalize_float(budget_data.get("expected_consumption", 2.0) if budget_data else 2.0))
                    occ_coeff, _, _, _, _, _, _ = man.get_occupancy_coefficient() if man else (1.0, 0,0,0,0,0,0)
                    tom_idx = (now + timedelta(days=1)).weekday()
                    prof_cons_for_buy = dict(man.get_average_profile("consumption_base", man.custom_period, "all"))
                    diff_range = range(min(sunrise_h, 8), max(sunrise_h, 8))
                    diff_kwh = sum(float(normalize_float(prof_cons_for_buy.get(str(h), 0.0))) for h in diff_range) * occ_coeff
                    cons_until_morning = comp_cons_to_8am - diff_kwh if sunrise_h < 8 else comp_cons_to_8am + diff_kwh
                    
                    # v11.1.102: Include buffer in survival target to eliminate the "dead zone" (10% buy vs 25% sell limits)
                    soc_buffer = float(man.get_setting(CONF_SOC_BUFFER, 15.0))
                    survival_target_kwh = cons_until_morning + ((min_soc + soc_buffer) * b_cap / 100.0)
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
                        
                        # v11.1.30: Always include negative price hours regardless of future solar potential
                        p_buy_h = all_buy_prices.get(h_b, 999.0)
                        if max_dry_soc < 99.0 or p_buy_h <= 0.0:
                            pool_useful.append(h_b)
                    
                    pool = pool_useful
                    if negative_hours:
                        res["charge_reason"] = "negative"
                        target_soc = 100.0
                    elif is_strict_arb:
                        res["charge_reason"] = "arbitrage"
                        # Adaptive Target: 100% minus what the sun gives eventually
                        expected_soc_at_peak, _, _ = self.run_soc_simulation(b_soc, list(range(cur_hour, int(peak_h))), now, commands=None)
                        sun_gain_pct = max(0.0, expected_soc_at_peak - b_soc)
                        target_soc = float(min(100.0, 100.0 - sun_gain_pct))
                    elif available_today_kwh < survival_target_kwh:
                        res["charge_reason"] = "survival"
                        target_soc = float(min(base_target, survival_target_kwh / b_cap * 100.0))
                        res["arbitrage_decision"] = f"Заряд для обеспечения буфера ({target_soc:.1f}%)"
                    else:
                        res["charge_reason"] = "none"
                        target_soc = b_soc

                    # Final Override (v6.16): If no useful hours left, sun is sufficient,
                    # OR current SOC is already high enough (prevents micro-buys for 1% arbitrage)
                    # v11.1.30: Bypass these checks if price is negative (always useful to fill the battery)
                    should_skip_buy = (not pool or target_soc <= (b_soc + 0.5)) and not negative_hours
                    if should_skip_buy:
                        target_soc = b_soc
                        res["charge_reason"] = "none"
                        pool = [] # Empty pool to clear attributes
                    
                    # User-defined Ceiling (v11.1.62) - Using existing CONF_AI_CHARGE_LIMIT
                    # Skip check if price is negative as requested by USER
                    if target_soc > base_target and not negative_hours:
                        target_soc = base_target
                        res["note"] = f"Цель ограничена пользователем (Target SOC Buy: {base_target}%)"
                    
                    target_soc = float(min(100.0, target_soc))
                    sim_soc_plan = b_soc
                    
                    charge_commands = {int(h): 0.0 for h in target_hours_sorted if h >= cur_hour}
                    if True: # v11.3.97: Always run simulation for telemetry
                        
                        # v11.1.39: Survival simulation for NO_PV_SALE_NO_BAT mode
                        # We evaluate this first so we know if PV charging will be blocked
                        res["can_wait_for_negative"] = False
                        first_neg_h = None
                        for h_idx in range(cur_hour, min(48, cur_hour + 24)):
                            if all_buy_prices.get(h_idx, 999.0) < 0.0:
                                first_neg_h = h_idx
                                break
                        
                        if first_neg_h is not None and first_neg_h > cur_hour:
                            # Simulation: Can we survive until first_neg_h without extra charging from PV?
                            sim_range_neg = list(range(cur_hour, first_neg_h))
                            soc_at_neg, _, _ = self.run_soc_simulation(b_soc, sim_range_neg, now, no_battery_charge=True)
                            
                            # v11.6.28: threshold uses CONF_MIN_SOC_BAT (emergency_soc_limit, default 10%).
                            # At the negative price hour, the system immediately starts buying from grid,
                            # so we only need to survive above the physical battery floor.
                            threshold_neg = max(float(man.get_setting(CONF_MIN_SOC_BAT, 10.0)), 5.0)
                            res["can_wait_for_negative"] = bool(soc_at_neg > threshold_neg)
                            res["first_negative_hour"] = first_neg_h
                            
                            res["debug_soc_at_neg"] = soc_at_neg
                            res["debug_threshold"] = threshold_neg

                        # v11.6.12: Accurate SOC planning for Buy Window
                        # Pre-simulate the expected SOC at the START of the window
                        soc_at_start_plan = b_soc
                        is_neg_strategy = bool(res.get("charge_reason") == "negative")
                        will_block_pv = res["can_wait_for_negative"] and is_neg_strategy
                        
                        # v11.6.13 (revised): sale_pv_no_bat shrinks from the RIGHT:
                        # It stays active until "latest_charge_start = first_sell_peak - hours_solar_needs_to_full"
                        # This mirrors the BMS logic in _get_mode_at (latest_start = peak_abs - total_needed)
                        _current_inverter_mode = getattr(man, "current_inverter_mode", "sale_pv")
                        _sale_pv_no_bat_max_h_v = man.get_setting(CONF_SALE_PV_NO_BAT_MAX_HOUR, 13.0)
                        _sale_pv_no_bat_max_h = int(float(_sale_pv_no_bat_max_h_v) if _sale_pv_no_bat_max_h_v is not None else 13)
                        pv_no_bat_block_until = None
                        if _current_inverter_mode == "sale_pv_no_bat" and cur_hour < _sale_pv_no_bat_max_h:
                            _sell_peaks_ahead = [h for h in (self.manager.get_market_strategy("sell") or {}).get("active_hours", []) if h > cur_hour]
                            _first_sell_peak = min(_sell_peaks_ahead) if _sell_peaks_ahead else _sale_pv_no_bat_max_h
                            _ai_target_soc = float(man.get_setting(CONF_AI_DISCHARGE_LIMIT, 100.0))
                            # Mini-sim: how long does solar take to charge from b_soc to _ai_target_soc?
                            _chk_range = list(range(cur_hour, min(_first_sell_peak, _sale_pv_no_bat_max_h) + 1))
                            _hours_to_full = len(_chk_range)  # pessimistic default
                            if _chk_range:
                                try:
                                    _, _chk_log, _ = self.run_soc_simulation(b_soc, _chk_range, now, {})
                                    for _ci, _cv in enumerate(_chk_log.values()):
                                        _cv_soc = _cv.get("soc", 0.0) if isinstance(_cv, dict) else float(_cv)
                                        if _cv_soc >= (_ai_target_soc - 0.5):
                                            _hours_to_full = _ci + 1
                                            break
                                except Exception:
                                    pass
                            # latest_charge_start = peak - hours_needed; sale_pv_no_bat blocks until then
                            _latest_cs = max(cur_hour, _first_sell_peak - _hours_to_full)
                            _raw_block = min(_sale_pv_no_bat_max_h, _latest_cs)
                            pv_no_bat_block_until = _raw_block if _raw_block > cur_hour else None
                        
                        first_h_buy = next((h for h in target_hours_sorted if h >= cur_hour), cur_hour)
                        if first_h_buy > cur_hour:
                            sim_range_pre = list(range(cur_hour, first_h_buy))
                            # Combine neg-price block and sale_pv_no_bat block into a single no_charge_until
                            _combined_block = None
                            if will_block_pv:
                                _combined_block = first_h_buy
                            # v11.6.14: Extend block to first_h_buy — hours between sale_pv_no_bat end
                            # and buy window may be no_pv_sale_no_bat (also blocks charging)
                            if pv_no_bat_block_until is not None:
                                _effective_block = max(pv_no_bat_block_until, first_h_buy)
                                _combined_block = max(_combined_block or 0, _effective_block)
                            soc_at_start_plan, _, _ = self.run_soc_simulation(
                                b_soc, sim_range_pre, now,
                                no_battery_charge_until=_combined_block
                            )

                        # 1. Calculate how much kWh we roughly need to add based on EXPECTED SOC
                        eff_coeff = float(self.get_efficiency_coefficient() or 0.95)
                        theoretical_gap_kwh = max(0.0, (target_soc - soc_at_start_plan) / 100.0 * b_cap) / max(0.1, eff_coeff)
                        
                        avg_prof_cons = man.get_average_profile("consumption_base", man.custom_period, "all")
                        pool_cons = 0.0
                        for h in pool:
                            h_f = max(0.1, (60 - now.minute)/60.0) if h == cur_hour else 1.0
                            h_cons = float(normalize_float(avg_prof_cons.get(str(h % 24), 0.5)))
                            pool_cons += h_cons * h_f
                            
                        if res.get("charge_reason") == "survival":
                            energy_to_buy = theoretical_gap_kwh
                        elif is_neg_strategy:
                            # v11.6.16: In negative-price buy window, house is powered from grid.
                            # Battery does NOT drain from consumption during buy hours.
                            # We only need to charge the theoretical gap (no pool_cons needed).
                            energy_to_buy = theoretical_gap_kwh
                        else:
                            energy_to_buy = theoretical_gap_kwh + pool_cons

                        # 2. Sort available hours by price (cheapest first)
                        pool_sorted = sorted(pool, key=lambda h: all_buy_prices[h])

                        # 3. v11.1.22: Differentiated allocation
                        if is_neg_strategy:
                            pool_sorted_neg = sorted(pool, key=lambda hr: all_buy_prices.get(hr, 999.0))
                            rem_kwh = energy_to_buy
                            for h in pool_sorted_neg:
                                h_factor = max(0.1, (60 - now.minute)/60.0) if h == cur_hour else 1.0
                                p_greedy = min(max_p * 0.9, rem_kwh / h_factor) if rem_kwh > 0.05 else 0.0
                                charge_commands[int(h)] = round_f(p_greedy, 3)
                                rem_kwh -= (p_greedy * h_factor)
                        else:
                            total_h_factors = sum(max(0.1, (60 - now.minute)/60.0) if h == cur_hour else 1.0 for h in pool)
                            if total_h_factors > 0.01:
                                p_req = float(energy_to_buy / total_h_factors)
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
                        sim_end_h = max(cur_hour + 24, 24 + sunrise_h + 1)
                        sim_range = list(range(cur_hour, sim_end_h))
                        # Apply blocked PV only UP TO the negative prices (so tomorrow afternoon works properly)
                        # v11.6.13: Also account for sale_pv_no_bat blocking solar charge
                        _buy_sim_no_charge_until = None
                        if will_block_pv:
                            _buy_sim_no_charge_until = first_h_buy
                        if pv_no_bat_block_until is not None:
                            # Extend to first_h_buy: post-sale_pv_no_bat hours may be no_pv_sale_no_bat
                            _effective_pv_block = max(pv_no_bat_block_until, first_h_buy)
                            _buy_sim_no_charge_until = max(_buy_sim_no_charge_until or 0, _effective_pv_block)
                        # v11.6.16: During negative-price buy window, inverter curtails PV regardless of
                        # actual grid power — inverter is in buy-mode config which suppresses PV.
                        _neg_buy_curtail = set()
                        if is_neg_strategy:
                            _neg_buy_curtail = {h for h in target_hours_sorted if all_buy_prices.get(h, 0.0) < 0.0}
                        _, sim_log, _ = self.run_soc_simulation(
                            b_soc, sim_range, now, charge_commands,
                            no_battery_charge_until=_buy_sim_no_charge_until,
                            pv_curtail_hours=_neg_buy_curtail or None
                        )
                        
                        # 1. Projected SOC at START of the first buy hour
                        if True: # v11.3.97: Always run simulation for telemetry
                            valid_buy_hours = [t for t in target_hours_sorted if t >= cur_hour]
                            if valid_buy_hours:
                                first_h_buy_sim = min(valid_buy_hours)
                                if first_h_buy_sim > cur_hour:
                                    prev_h = first_h_buy_sim - 1
                                    key_start = f"{prev_h % 24:02d}:59" + (" (Завтра)" if prev_h >= 24 else "")
                                    soc_at_start = self._get_soc_from_log(sim_log, key_start, b_soc)
                                else:
                                    soc_at_start = b_soc
                            else:
                                soc_at_start = b_soc
                        else:
                            soc_at_start = b_soc

                        # 2. Projected SOC AFTER the first continuous buy window
                        if True: # v11.3.97: Always run simulation for telemetry
                            future_active_buy = [h for h in target_hours_sorted if h >= cur_hour]
                            if future_active_buy:
                                # Find the last hour of the first continuous block
                                last_h_buy_immediate = future_active_buy[0]
                                for i in range(1, len(future_active_buy)):
                                    if future_active_buy[i] == future_active_buy[i-1] + 1:
                                        last_h_buy_immediate = future_active_buy[i]
                                    else:
                                        break
                                
                                key_end = f"{last_h_buy_immediate % 24:02d}:59" + (" (Завтра)" if last_h_buy_immediate >= 24 else "")
                                soc_at_end = self._get_soc_from_log(sim_log, key_end, b_soc)
                            else:
                                soc_at_end = b_soc
                        else:
                            soc_at_end = b_soc
                            
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
                            "projected_soc_at_buy_start": float(round_f(soc_at_start, 1)),
                            "projected_soc_at_end_pct": float(round_f(min(soc_at_end, target_soc), 1)),
                            "projected_soc_at_end": float(round_f(min(soc_at_end, target_soc), 1)),
                            "projected_soc_morning_pct": float(round_f(soc_morning, 1)),
                            "projected_soc_morning": float(round_f(soc_morning, 1)),
                            "no_battery_charge_until": _buy_sim_no_charge_until,
                            "pv_curtail_hours": _neg_buy_curtail,
                            "log": sim_log
                        }

                        # v7.1: Note: Simulation results are no longer used to override target_soc 
                        # to ensure the UI shows the intended target (v11.1.61).
                        
                        # v11.6.30: Expose charge_commands in res so BatterySocPredictionSensor
                        # can pass real buy power commands to its own simulation.
                        res["charge_commands"] = {int(k): float(v) for k, v in charge_commands.items()}
                    except Exception as e:
                        _LOGGER.error("Error in MarketStrategy BUY simulation: %s", e)
                        res["buy_simulation"] = {
                            "projected_soc_at_start_pct": float(b_soc),
                            "projected_soc_at_end_pct": float(b_soc),
                            "projected_soc_morning_pct": float(b_soc),
                            "error": str(e)
                        }
                else: # sell
                    # Sell mode (v11.1.51)
                    # Use existing Target SOC Sell as floor for AI selling
                    base_target = float(man.get_setting(CONF_AI_DISCHARGE_LIMIT, 20.0))
                    user_discharge_limit = base_target  # v11.4.39: Store original user value before any system corrections
                    # Initial defaults for robustness
                    arb_gain = 0.0
                    cheap_h_back = None
                    best_buy_h = None
                    cheap_p_back = 0.0
                    cur_p_f = float(normalize_float(today_prices.get(str(cur_hour), 0.0)))
                    
                    occ_coeff, _, _, _, _, _, _ = man.get_occupancy_coefficient()
                    occ_coeff = float(occ_coeff)
                    
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
                    
                    # v11.6.196: Emergency Reserve (min_soc_bat)
                    min_soc_bat_val = float(man.get_setting(CONF_MIN_SOC_BAT, 10.0))
                    
                    # v11.6.162: Min SOC is a HARD floor, not a sunrise target. 
                    min_soc_val = float(man.get_setting(CONF_AI_DISCHARGE_LIMIT, 15.0))
                    
                    # Adaptive buffer: If Min SOC is high (e.g. 70%), we don't need a huge additional buffer.
                    soc_buffer_val = float(man.get_setting(CONF_SOC_BUFFER, 15.0))
                    soc_buffer_full = 3.0 if min_soc_val > 25.0 else soc_buffer_val
                    soc_buffer_val = 3.0 if min_soc_val > 25.0 else soc_buffer_val
                    
                    # v11.4.30: Early Detection for Morning Liberalization
                    # We need solar context to decide if we relax the buffer
                    rem_solar_today = float(normalize_float(budget_data_sell.get("forecast_val", 0.0)))
                    gen_night_morning = sum(float(normalize_float(avg_prof_gen.get(str(h), 0.0))) for h in range(0, sunrise_h))
                    total_hist_gen_val = sum(float(normalize_float(avg_prof_gen.get(str(h), 0.0))) for h in range(24))
                    morning_solar_ac = f_tom * (gen_night_morning / total_hist_gen_val) if total_hist_gen_val > 0.1 else 0.0
                    total_solar_to_sunrise = rem_solar_today + morning_solar_ac
                    cur_h_gen_prof = float(normalize_float(avg_prof_gen.get(str(cur_hour % 24), 0.0)))
                    
                    cur_pv = float(man.avg_gen_kw or 0.0)
                    is_morning_solar_v2 = (4 <= cur_hour <= 12) and (total_solar_to_sunrise > 0.05 or cur_h_gen_prof > 0.05 or rem_solar_today > 0.05 or cur_pv > 0.5)
                    if is_morning_solar_v2:
                         soc_buffer_val = 3.0 # Standard morning relaxation
                    
                    _is_morning_liberal = False
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
                    
                    # v11.5.0: Morning Solar Liberalization
                    has_morning_sale = any(h < 13 for h in target_hours_sorted) if target_hours_sorted else False
                    
                    if is_morning_solar_v2 and cur_hour < 12 and has_morning_sale:
                        # v11.6.55: User Request: Compare current price with evening peak (13:00-23:00)
                        evening_hours = [h for h in all_sell_prices.keys() if 13 <= h <= 23]
                        evening_max_p = max([all_sell_prices[h] for h in evening_hours] + [0.0])
                        cur_p_s = all_sell_prices.get(cur_hour, 0.0)
                        
                        _lib_sim_range = list(range(cur_hour, 21))
                        _lib_start_soc = min_soc_val + 2.0  # Anchor: after selling down to floor
                        try:
                            # v11.6.57: Use ignore_blended=True to avoid pessimistic morning scaling (46kWh means 46kWh)
                            _, _lib_log, _ = self.run_soc_simulation(_lib_start_soc, _lib_sim_range, now, ignore_blended=True)
                            _lib_max_soc = max(
                                [float(st.get("soc", _lib_start_soc)) for st in _lib_log.values()]
                                + [_lib_start_soc]
                            )
                        except Exception:
                            _lib_max_soc = 0.0
                            
                        # v11.6.54: Unconditional flag in morning solar window
                        _is_morning_liberal = True
                        
                        if _is_morning_liberal:
                            # v11.6.55: Refined Price-Aware Strategy
                            # 1. If morning price < evening price -> Ensure 100% recharge by evening peak.
                            # 2. If morning price >= evening price -> Only ensure 15% survival for next morning.
                            
                            if cur_p_s < evening_max_p - 0.05:
                                # Deficit to reach full charge (100%) by evening
                                recharge_deficit_soc = max(0.0, 100.0 - _lib_max_soc)
                                base_target = min_soc_val + 2.0 + recharge_deficit_soc
                                soc_buffer_val = 2.0 + recharge_deficit_soc
                                active_buffer = 2.0 + recharge_deficit_soc
                                _LOGGER.debug(
                                    f"[Sell v11.6.55] Morning < Evening ({cur_p_s:.2f} < {evening_max_p:.2f}): "
                                    f"Raising base_target to {base_target:.1f}% to ensure evening charge (sim_max={_lib_max_soc:.1f}%)"
                                )
                            else:
                                # Morning is better or equal -> Sell down to 15% survival floor
                                base_target = min_soc_val + 2.0
                                soc_buffer_val = 2.0
                                active_buffer = 2.0
                                _LOGGER.debug(
                                    f"[Sell v11.6.55] Morning >= Evening ({cur_p_s:.2f} >= {evening_max_p:.2f}): "
                                    f"Survival mode active, base_target={base_target:.1f}%"
                                )
                    
                    # v11.6.169: Priority Correction (min(M, U, P))
                    has_morning_sale = any((sunrise_h <= (h % 24) <= 12) for h in target_hours_sorted) if target_hours_sorted else False
                    # 1. Home Protection Floor (M_floor): Reserve + Buffer
                    _m_floor = min_soc_bat_val + (2.0 if has_morning_sale else soc_buffer_full)
                    
                    # 2. Final Sunrise Target: max(User Limit, Home Protection)
                    # If User=70 and Home=25, Target=70.
                    target_morning_soc = max(min_soc_val, _m_floor)
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
                    
                    # (rem_solar_today and total_solar_to_sunrise are now calculated early above for v11.4.30)
                    
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
                    
                    # Deficit for the full profile
                    tomorrow_deficit_full = max(0.0, tomorrow_cons_total - tomorrow_solar_total)
                    solar_is_excess = bool(tomorrow_solar_total > tomorrow_cons_total + 1.5) # 1.5kWh buffer
                    
                    # PRECISE SIMULATION-BASED CALCULATION (v6.2 Modular)
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
                    
                    if man.get_setting(CONF_DYNAMIC_SOC_SELL, True):
                        # 1. Run Baseline Simulation (v11.3.21: Get full log for start_soc detection)
                        sim_end_h = max(cur_hour + 24, 24 + sunrise_h + 1)
                        sim_range = list(range(cur_hour, sim_end_h))
                        # v11.6.13: If currently in sale_pv_no_bat mode, block PV charging in simulation
                        # until the mode's dynamic boundary (min of max_hour and first sell peak).
                        _sell_sim_current_mode = getattr(man, "current_inverter_mode", "sale_pv")
                        _sell_pv_no_bat_max_h_v = man.get_setting(CONF_SALE_PV_NO_BAT_MAX_HOUR, 13.0)
                        _sell_pv_no_bat_max_h = int(float(_sell_pv_no_bat_max_h_v) if _sell_pv_no_bat_max_h_v is not None else 13)
                        _sell_sim_no_charge_until = None
                        if _sell_sim_current_mode == "sale_pv_no_bat" and cur_hour < _sell_pv_no_bat_max_h:
                            _sell_peaks_for_sim = [h for h in target_hours_sorted if h > cur_hour]
                            _first_sell_for_sim = min(_sell_peaks_for_sim) if _sell_peaks_for_sim else _sell_pv_no_bat_max_h
                            _ai_target_sell = float(man.get_setting(CONF_AI_DISCHARGE_LIMIT, 100.0))
                            # Mini-sim: how many hours does solar need to charge from b_soc to target?
                            _sell_chk_range = list(range(cur_hour, min(_first_sell_for_sim, _sell_pv_no_bat_max_h) + 1))
                            _sell_hours_to_full = len(_sell_chk_range)  # pessimistic default
                            if _sell_chk_range:
                                try:
                                    _, _sell_chk_log, _ = self.run_soc_simulation(b_soc, _sell_chk_range, now, {}, b_min_soc=0.0)
                                    for _si, _sv in enumerate(_sell_chk_log.values()):
                                        _sv_soc = _sv.get("soc", 0.0) if isinstance(_sv, dict) else float(_sv)
                                        if _sv_soc >= (_ai_target_sell - 0.5):
                                            _sell_hours_to_full = _si + 1
                                            break
                                except Exception:
                                    pass
                            _sell_latest_cs = max(cur_hour, _first_sell_for_sim - _sell_hours_to_full)
                            _sell_raw_block = min(_sell_pv_no_bat_max_h, _sell_latest_cs)
                            _sell_sim_no_charge_until = _sell_raw_block if _sell_raw_block > cur_hour else None
                        elif _sell_sim_current_mode == "no_pv_sale_no_bat":
                            # v11.6.14: no_pv_sale_no_bat also blocks PV charging (c_amps_fixed=0.0).
                            # Block simulation charging until the first negative-price buy hour.
                            _sell_neg_h = None
                            for _nh in range(cur_hour, min(48, cur_hour + 24)):
                                if all_buy_prices.get(_nh, 999.0) < 0.0:
                                    _sell_neg_h = _nh
                                    break
                            if _sell_neg_h is not None and _sell_neg_h > cur_hour:
                                _sell_sim_no_charge_until = _sell_neg_h
                        # v11.6.75: Remove NoChgUntil from baseline. User wants Budget to match Gatekeeper floor
                        # without double-counting the safety margin of tomorrow's solar block.
                        # v11.6.152: Trust forecast 100% in the morning (until 10:00) to allow sales,
                        # but use realistic confidence thereafter for accurate planning.
                        _ignore_blended = bool(cur_hour < 10)
                        _, sim_log_base, _ = self.run_soc_simulation(
                            b_soc, sim_range, now, {},
                            b_min_soc=0.0,
                            ignore_blended=_ignore_blended
                        )

                        
                        # v11.6.41: Fix massive bug where natural_morning_soc was taking the end-of-sim SOC (100% due to tomorrow's sun)
                        key_sunrise = f"{sunrise_h-1:02d}:59" + (" (Завтра)" if sunrise_h-1 < cur_hour else "")
                        natural_morning_soc = self._get_soc_from_log(sim_log_base, key_sunrise, b_soc)
                    
                    # --- TWO-STEP SAFETY CHECK (Refined v6.2) ---
                    # 1. Base-only Gatekeeper: Can we cover Essential House Needs for the next 24+ hours?
                    # v11.4.31: In morning solar window, we make the Gatekeeper "blind" to tomorrow's deficit.
                    # This allows the simulation (Step 2) to be the primary decision maker.
                    work_cons_to_sunrise = 0.0 if is_morning_solar_v2 else total_cons_to_sunrise
                    work_deficit_tomorrow = 0.0 if is_morning_solar_v2 else base_deficit_tomorrow
                    
                    ai_soc_floor_base = self._calc_immediate_safety_floor(
                        min_soc_val, active_buffer, work_cons_to_sunrise, 
                        work_deficit_tomorrow, total_solar_to_sunrise, b_cap, eff
                    )
                    
                    # 1. Projected SOC at START of the first peak (v11.3.20: Early detection)
                    soc_at_start = b_soc
                    first_h_sell = min(t for t in target_hours_sorted if t >= cur_hour) if target_hours_sorted else None
                    if first_h_sell is not None and first_h_sell > cur_hour:
                        prev_h = first_h_sell - 1
                        key_start = f"{prev_h % 24:02d}:59" + (" (Завтра)" if prev_h >= 24 else "")
                        soc_at_start = self._get_soc_from_log(sim_log_base, key_start, b_soc) or b_soc
                    
                    # 2. Daily Surplus (Sunrise-Aware v6.2)
                    # v11.3.9: TRIPLE CONSTRAINT - Sale is limited by: 
                    # 1. User SOC Limit 2. Morning Survival 3. Physical Battery Power (C-rate/Time)
                    surplus_for_morning = self._calculate_sunrise_surplus(
                        natural_morning_soc, min_soc_val, soc_buffer_val, b_cap, 1.0, 0.0 
                    )
                    
                    # v11.6.200: Initialize simulation state with baseline data
                    sim_log = sim_log_base
                    soc_morning = natural_morning_soc
                    soc_after = soc_at_start
                    
                    # v11.3.26: Calculate User Limit using natural SOC at the END of the sale window.
                    # This guarantees we account for the house background load during the sale.
                    natural_soc_after_sale = soc_at_start
                    if True: # v11.3.97: Always run simulation for telemetry
                        future_active_sell_base = [h for h in target_hours_sorted if h >= cur_hour]
                        
                        # --- v11.3.36: Smart Deficit Throttling (Double Cycle Optimizer) ---
                        # If the sun cannot recharge the battery to 100% between Morning and Evening peaks,
                        # it is mathematically optimal to HOLD the deficit energy in the Morning 
                        # and sell it in the Evening at the higher price.
                        epochs_eval = []
                        current_ep = []
                        for h in sorted(future_active_sell_base):
                            if not current_ep or h - current_ep[-1] <= 3:
                                current_ep.append(h)
                            else:
                                epochs_eval.append(current_ep)
                                current_ep = [h]
                        if current_ep:
                            epochs_eval.append(current_ep)
                            
                        # Apply ONLY if we are in the first epoch of a multi-epoch cycle
                        if len(epochs_eval) > 1 and cur_hour <= max(epochs_eval[0]):
                            end_first = max(epochs_eval[0])
                            start_second = min(epochs_eval[1])
                            
                            # We MUST run a micro-simulation starting exactly at the discharge floor (base_target)
                            # to accurately measure the true charging capacity of the daytime sun.
                            # Baseline sim_log_base starts at current SOC, which masks the true solar potential.
                            throttle_sim_hours = list(range(int(end_first) + 1, int(start_second)))
                            if throttle_sim_hours:
                                # v11.6.35: Night-Aware Deficit Throttling.
                                # Always run simulation regardless of solar generation.
                                # Without solar (night), sim returns max_recharge_soc = base_target,
                                # making deficit = 100% - base_target, raising base_target to 100%
                                # -> nothing sold in Window1, all energy reserved for higher-priced Window2.
                                # v11.6.58: Use ignore_blended=True to avoid pessimistic morning scaling (which causes mythical deficits)
                                _, throttle_log, _ = self.run_soc_simulation(base_target, throttle_sim_hours, now, ignore_blended=True)
                                max_recharge_soc = max([float(x.get("soc", base_target)) for x in throttle_log.values()] + [base_target])
                                
                                # v11.4.25: Price-Aware Deficit Throttling
                                # Only hold the energy if the second epoch price is higher than the first.
                                prices_all = all_sell_prices
                                avg_p1 = sum(float(prices_all.get(h, 0.0)) for h in epochs_eval[0]) / len(epochs_eval[0])
                                avg_p2 = sum(float(prices_all.get(h, 0.0)) for h in epochs_eval[1]) / len(epochs_eval[1])
                                
                                if max_recharge_soc < 99.0 and avg_p2 > (avg_p1 + 0.05):
                                    deficit_pct = 100.0 - max_recharge_soc
                                    base_target = min(100.0, base_target + deficit_pct)
                                    
                        if future_active_sell_base:
                            last_h_base = future_active_sell_base[-1]
                            for i in range(1, len(future_active_sell_base)):
                                if future_active_sell_base[i] != future_active_sell_base[i-1] + 1:
                                    last_h_base = future_active_sell_base[i-1]
                                    break
                            key_nat_end = f"{last_h_base % 24:02d}:59" + (" (Завтра)" if last_h_base >= 24 else "")
                            natural_soc_after_sale = self._get_soc_from_log(sim_log_base, key_nat_end, soc_at_start)
                        
                        # v11.3.60: Morning Survival Feedback Loop (The "Autopilot" Floor)
                        # We calculate the exact SOC floor needed to guarantee the morning target.
                        # Energy drain between end of sale and sunrise (in SOC %)
                        night_drain_pct = max(0.0, natural_soc_after_sale - natural_morning_soc)
                        
                        # v11.6.192: Emergency Base for Survival Floor (M)
                        # We only need to ensure the house stays above (Reserve + Buffer) BY MORNING.
                        # As per TS Round 231: Limits NEVER SUM with house consumption in labels/floors.
                        _m_emergency_base = min_soc_bat_val + soc_buffer_full
                        
                        # Final base target is the HIGHEST of User Limit (Min SOC) or Survival Floor (Reserve+Buffer)
                        base_target = max(min_soc_val, _m_emergency_base)
                        survival_floor = _m_emergency_base # For label logic
                        
                        if survival_floor > min_soc_val + 0.5:
                             res["morning_autopilot_active"] = True
                             res["morning_autopilot_floor"] = round_f(survival_floor, 1)
                         
                        # target_morning_soc remains as calculated at line 1978 (buffer-aware)
                        pass
                    
                    # v11.3.11: Factor in physical energy capacity of the identified peaks
                    # Using global max_p which already accounts for CONF_BATTERY_MAX_POWER (e.g. 6.2kW)
                    # Auto-convert Watts to kW if user entered 6200 instead of 6.2
                    work_max_p = max_p if max_p < 100 else max_p / 1000.0
                    
                    # Account for remaining minutes in the current hour if it's a peak
                    total_h_allowed = num_peaks_left
                    physical_limit_dc = (work_max_p * total_h_allowed) / eff
                    
                    # v11.6.84: Expand budget to accommodate deeper morning discharge (15% vs 18%)
                    # v11.6.104: Use soc_buffer_full instead of soc_buffer_val to guarantee the 3% bonus 
                    # even if the autopilot raised base_target to 18%.


                    # v11.6.173: Formalized min(M, U, P) budget allocation
                    _morning_lib_surplus_dc = (soc_buffer_full - 2.0) * b_cap / 100.0 if has_morning_sale else 0.0
                    
                    # M: Morning Survival (Includes night drain protection)
                    surplus_for_morning = max(0.0, (natural_soc_after_sale - survival_floor) * b_cap / 100.0) + _morning_lib_surplus_dc
                    
                    # U: User Limit (Raw floor at end of sale window, NO night drain)
                    _user_budget_floor = min_soc_val
                    surplus_for_user_limit = max(0.0, (natural_soc_after_sale - _user_budget_floor) * b_cap / 100.0)
                    
                    # Choose most restrictive budget
                    available_sell_dc = min(surplus_for_morning, surplus_for_user_limit, physical_limit_dc)

                    # v11.6.161: Ensure base_target reflects the chosen constraint
                    if available_sell_dc <= (surplus_for_user_limit + 0.001) and surplus_for_user_limit < (surplus_for_morning - 0.1):
                         base_target = _user_budget_floor
                    else:
                         base_target = survival_floor

                    sell_diagnosis = "Рассчитано (Ок)"
                    if available_sell_dc <= (physical_limit_dc + 0.001) and physical_limit_dc < (min(surplus_for_morning, surplus_for_user_limit) - 0.1):
                        sell_diagnosis = f"Лимит мощности АКБ ({work_max_p:.1f}кВт)"
                    else:
                        sell_diagnosis = f"Лимит пользователя ({min_soc_val:.0f}%)" if base_target <= min_soc_val + 0.5 else f"Защита дома (Цель {min_soc_bat_val + soc_buffer_full:.0f}% к утру)"

                    # v11.6.167 / v11.6.169: Clean human-readable status construction
                    res["arbitrage_sell_limit_reason"] = f"{sell_diagnosis}"
                    # Detailed debug is moved to internal attributes
                    res["_debug_limit_info"] = f"M:{surplus_for_morning:.1f} U:{surplus_for_user_limit:.1f} P:{physical_limit_dc:.1f} S:{soc_at_start:.1f}% (Nat:{natural_soc_after_sale:.1f}%)"
                    res["_debug_passes"] = _pass_log if '_pass_log' in locals() else ""
                    # res["power_decision"] = (f"Распределение на {num_peaks_left:.1f}ч{_lib_tag}" if num_peaks_left > 1.1 else f"{sell_diagnosis}{_lib_tag}") # v11.6.161: Moved to end
                    
                    # v11.3.37: UI Feedback for Smart Deficit Throttling
                    if available_sell_dc < 0.05 and num_peaks_left > 0.1 and cur_hour < 13:
                        mc_status = res.get("multi_cycle", "")
                        if "Благоприятно" in mc_status:
                            res["multi_cycle"] = mc_status.replace("Благоприятно", "Ограничено (мало солнца)")
                    
                    surplus_soc_at_sunrise = (surplus_for_morning / b_cap * 100.0) if b_cap > 0.1 else 0.0
                    ai_soc_floor_final = target_morning_soc
                    
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
                    if b_soc < ai_soc_floor_base and not (is_in_peak and cur_p_f >= sell_limit):
                        # Throttled/Idle because base needs for tomorrow are not guaranteed
                        target_soc = ai_soc_floor_base
                        available_sell_ac = 0.0
                        if is_in_peak and not arbitrage_is_best:
                            decision_tag = "Защита базы (Завтра мало солнца)"
                        else:
                            decision_tag = f"Ожидание ({sell_diagnosis})"
                    else:
                        target_soc = base_target
                        decision_tag = f"{decision_tag} | {sell_diagnosis}"
                        available_sell_ac = float(max(0.0, available_sell_dc * eff))
                        
                    # --- v11.6.38: Energy Pooling (Round 108) ---
                    # Group hours into pools separated by SOLAR GENERATION.
                    # If two hours are separated only by night, they share the same energy pool
                    # and must be sorted globally by price!
                    sell_pool = [h for h in target_hours_sorted if h >= cur_hour]
                    
                    # v11.6.190: Initialize bottleneck flags to prevent UnboundLocalError
                    _is_p_limited = False
                    _is_u_limited = False
                    
                    epochs = []
                    current_epoch = []
                    for h in sorted(sell_pool):
                        if not current_epoch:
                            current_epoch.append(h)
                        else:
                            has_solar = False
                            for h_mid in range(current_epoch[-1] + 1, h):
                                if 8 <= (h_mid % 24) <= 18:
                                    has_solar = True
                                    break
                            
                            if not has_solar:
                                current_epoch.append(h)
                            else:
                                epochs.append(current_epoch)
                                current_epoch = [h]
                    if current_epoch:
                        epochs.append(current_epoch)

                    # v11.6.180: Define the first pool for UI display filtering
                    if epochs:
                        res["first_pool_hours"] = epochs[0]
                        
                    sell_commands = {int(h): 0.0 for h in sell_pool}
                    rem_kwh_sell = available_sell_ac
                    
                    for i, epoch in enumerate(epochs):
                        epoch_sorted = sorted(epoch, key=lambda hr: all_sell_prices.get(hr, 0.0), reverse=True)
                        
                        if i > 0:
                            sim_hours = list(range(max(epochs[i-1]) + 1, min(epoch)))
                            _, throttle_log, _ = self.run_soc_simulation(base_target, sim_hours, now, {}, ignore_blended=True)
                            max_recharge_soc = max([float(x.get("soc", base_target)) for x in throttle_log.values()] + [base_target])
                            rem_base_ac = float(max(0.0, (max_recharge_soc - base_target) * b_cap / 100.0) * eff)
                            rem_bonus_ac = float(max(0.0, _morning_lib_surplus_dc * eff))
                        else:
                            # v11.6.119: Use natural_soc_after_sale instead of soc_at_start.
                            # This ensures the actual power commands (Planned power) respect 
                            # the same house-aware budget as the diagnostics.
                            rem_base_ac = float(max(0.0, (natural_soc_after_sale - base_target) * b_cap / 100.0 * eff))
                            capped_bonus_soc = max(0.0, min(soc_at_start, base_target) - (min_soc_val + 2.0))
                            _actual_bonus_dc = (capped_bonus_soc * b_cap / 100.0) if has_morning_sale else 0.0
                            rem_bonus_ac = float(min(_morning_lib_surplus_dc, _actual_bonus_dc) * eff)
                            
                        for h in epoch_sorted:
                            h_f = max(0.1, (60 - now.minute) / 60.0) if h == cur_hour else 1.0
                            p_alloc = max_p
                            
                            # v11.6.90: Morning Floor starts EXACTLY at sunrise
                            h_floor = base_target
                            if sunrise_h <= (h % 24) <= 12:
                                h_floor = min_soc_val + 2.0
                                
                            # Selective Throttling strictly for the current hour
                            if h == cur_hour:
                                house_cons_hourly = float(normalize_float(avg_prof_cons.get(str(cur_hour % 24), 0.5))) * occ_coeff
                                house_rem_dc = (house_cons_hourly * h_f) / eff
                                current_surplus_dc = max(0.0, (b_soc - h_floor) * b_cap / 100.0 - house_rem_dc)
                                max_allowed_sell_ac = float(max(0.0, current_surplus_dc * eff))
                                p_alloc = min(max_p, max_allowed_sell_ac / h_f)
                            else:
                                # For future hours, also respect the local hour-specific floor.
                                # v11.6.84: Use a more generous simulation-aware cap for morning hours.
                                p_alloc = max_p
                                # v11.6.110: Fix key format: log stores "HH:59", not "HH:00".
                                # Use end-of-previous-hour SOC to represent the SOC entering this hour.
                                # v11.6.111: Key in history_log uses ' (Завтра)' for h >= 24.
                                _prev_h = h - 1
                                _prev_h_key = f"{(_prev_h)%24:02d}:59" + (" (Завтра)" if _prev_h >= 24 else "")
                                if h_soc_s := self._get_soc_from_log(sim_log_base, _prev_h_key, b_soc):
                                     surplus_h_dc = max(0.0, (h_soc_s - h_floor) * b_cap / 100.0)
                                     p_alloc = min(max_p, (surplus_h_dc * eff) / h_f)
                                
                            if (rem_base_ac + rem_bonus_ac) > 0.05:
                                # v11.6.90: Segregate budgets starting from sunrise_h
                                is_morning = sunrise_h <= (h % 24) <= 12
                                
                                # 1. Try to take from base budget first (Limit 18%)
                                power_from_base = min(p_alloc, rem_base_ac / h_f)
                                rem_base_ac -= (power_from_base * h_f)
                                
                                # 2. If it's morning and base is empty, take from bonus (Limit 15%)
                                power_from_bonus = 0.0
                                if is_morning and (p_alloc - power_from_base) > 0.01:
                                    power_from_bonus = min(p_alloc - power_from_base, rem_bonus_ac / h_f)
                                    rem_bonus_ac -= (power_from_bonus * h_f)
                                
                                actual_power = power_from_base + power_from_bonus
                                if actual_power > 0.01:
                                    sell_commands[int(h)] = round_f(actual_power, 3)
                    
                    # v11.6.83: Morning Floor Liberation is now integrated into the core distribution loop
                    # via dynamic h_floor and expanded budget.

                    power_needed = sell_commands.get(int(cur_hour), 0.0)

                    
                    if man.get_setting(CONF_DYNAMIC_SOC_SELL, True):
                        if target_soc < base_target:
                            target_soc = base_target
                            res["note"] = f"Цель ограничена пользователем (Target SOC Sell: {base_target}%)"
                        target_soc = float(target_soc)
                    else:
                        target_soc = base_target

                    # v11.4.06: Clean Arbitrage reporting (Sell mode)
                    best_buy_p, best_buy_h = get_best_buyback(cur_hour)
                    if best_buy_h is not None:
                        pot_gain_val = cur_p_f * eff - best_buy_p - deg_cost
                        global_arb_note = f"Купим в {self._format_h(best_buy_h)} по {best_buy_p:.2f} | Выгода {pot_gain_val:.2f}"
                    else:
                        global_arb_note = "Нет окна откупа"

                    if man.get_setting(CONF_DYNAMIC_SOC_SELL, True):
                        res["arbitrage_decision"] = f"Продаем сейчас по {cur_p_f:.2f} | {global_arb_note}"
                    else:
                        res["arbitrage_decision"] = "Ручной режим (AI выкл.)"
                        
                    target_soc = float(min(100.0, target_soc))
                    delta_available_dc = available_sell_ac / eff

                    # --- SELL SIMULATION ---
                    sim_end_h = max(cur_hour + 24, 24 + sunrise_h + 1)
                    sim_range = list(range(cur_hour, sim_end_h))
                    
                    last_h_sell = max(target_hours_sorted) if target_hours_sorted else None

                    # --- FINAL SIMULATION ---
                    sim_commands = {int(h): cmd for h, cmd in sell_commands.items()}
                    if best_buy_h is not None and best_buy_h < sim_end_h:
                        pot_gain_val = cur_p_f * eff - best_buy_p - deg_cost
                        diff_threshold = float(man.get_setting(CONF_ARBITRAGE_PROFIT_THRESHOLD, 0.1))
                        if pot_gain_val >= diff_threshold:
                            sim_commands[int(best_buy_h)] = float(max_p)

                    # v11.6.91: Ensure ALL simulations use the same dynamic floor constraints
                    _strat_floors = {}
                    _strat_sunrise = sunrise_h if 'sunrise_h' in locals() else 6
                    for h_sim in sim_range:
                        h_sim_norm = h_sim % 24
                        if _strat_sunrise <= h_sim_norm <= 12:
                            _strat_floors[h_sim] = min_soc_val + 2.0
                        else:
                            _strat_floors[h_sim] = base_target

                    _, sim_log, _ = self.run_soc_simulation(
                        b_soc, sim_range, now, sim_commands, 
                        b_min_soc=base_target,
                        no_battery_charge_until=_sell_sim_no_charge_until,
                        ignore_blended=True,
                        dynamic_floors=_strat_floors
                    )

                    
                    # 1. Projected SOC at START (Already calculated early)
                    # 2. Daily Surplus (Already calculated early)
                    # v11.6.162: Projected SOC after sale should be the MINIMUM reached during sales
                    future_active_sell = [h for h in target_hours_sorted if h >= cur_hour]
                    if future_active_sell:
                        # Find minimum SOC in the simulation log across all sell hours
                        soc_values_during_sales = []
                        for h_sell in future_active_sell:
                            h_key = f"{h_sell % 24:02d}:59" + (" (Завтра)" if h_sell >= 24 else "")
                            if val := self._get_soc_from_log(sim_log, h_key, b_soc):
                                soc_values_during_sales.append(float(val))
                        soc_after = min(soc_values_during_sales) if soc_values_during_sales else b_soc
                    else:
                        soc_after = b_soc
                    
                    res["projected_soc_after_sale"] = round_f(soc_after, 1)

                    # v11.3.9: Projected SOC TOMORROW MORNING (at Dynamic Sunrise)
                    key_morning = f"{sunrise_h-1:02d}:59 (Завтра)"
                    soc_morning = self._get_soc_from_log(sim_log, key_morning, soc_after)
                    res["projected_soc_morning"] = round_f(soc_morning, 1)


                    # v11.6.165: Define key_after for the recursive loop
                    last_h_sell_pool1 = max(epochs[0]) if 'epochs' in locals() and epochs else (future_active_sell[-1] if future_active_sell else cur_hour)
                    key_after = f"{last_h_sell_pool1 % 24:02d}:59" + (" (Завтра)" if last_h_sell_pool1 >= 24 else "")

                    # v11.6.175: Recursive Survival targeting
                    # The recursion should only "save" the house from dropping below the absolute minimum (25%),
                    # it should NOT try to maintain the user's high arbitrage limit (70%) until sunrise.
                    # v11.6.203: Synchronize recursive target with dynamic morning limits
                    _m_recursive_target = (min_soc_bat_val + 2.0) if 4 <= (cur_hour % 24) <= 12 else (min_soc_bat_val + soc_buffer_full)
                    
                    _pass_log = "Pass0"
                    for pass_idx in range(3):
                        morning_gap = _m_recursive_target - soc_morning
                        # Only raise the floor if we are actually dropping below the EMERGENCY level.
                        # If we are just dropping below the user's 70% (but stay at 60%), we don't care.
                        if morning_gap <= 0.1:
                            break
                            
                        # 1. Update the base target floor
                        base_target = min(100.0, max(min_soc_val, base_target + morning_gap))
                        
                        # 2. Re-calculate available volume WITH house load awareness
                        _rem_start_soc = soc_at_start if 'soc_at_start' in locals() else b_soc
                        rem_base_dc_fix = float(max(0.0, (_rem_start_soc - base_target) * b_cap / 100.0))
                        
                        # Subtract house load for the first pool (evening sale)
                        _house_during_fix = house_load_during_sale_dc if 'house_load_during_sale_dc' in locals() else 0.0
                        rem_base_dc_fix = max(0.0, rem_base_dc_fix - _house_during_fix)
                        
                        rem_base_ac_fix = float(rem_base_dc_fix * eff)
                        
                        # Bonus in Step 2 for first epoch
                        _actual_bonus_dc_fix = (max(0.0, min(_rem_start_soc, base_target) - (min_soc_val + 2.0)) * b_cap / 100.0) if has_morning_sale else 0.0
                        rem_bonus_ac_fix = float(min(_morning_lib_surplus_dc, _actual_bonus_dc_fix) * eff)
                            
                        # 3. Re-distribute sell_commands
                        for i, epoch in enumerate(epochs):
                            epoch_sorted = sorted(epoch, key=lambda hr: all_sell_prices.get(hr, 0.0), reverse=True)
                            if i > 0:
                                sim_hours = list(range(max(epochs[i-1]) + 1, min(epoch)))
                                _, throttle_log, _ = self.run_soc_simulation(base_target, sim_hours, now, {})
                                max_recharge_soc = max([float(x.get("soc", base_target)) for x in throttle_log.values()] + [base_target])
                                rem_base_ac_fix = float(max(0.0, (max_recharge_soc - base_target) * b_cap / 100.0) * eff)
                                rem_bonus_ac_fix = float(max(0.0, _morning_lib_surplus_dc * eff))
                            
                            for h in epoch_sorted:
                                h_f = max(0.1, (60 - now.minute) / 60.0) if h == cur_hour else 1.0
                                h_floor_fix = base_target
                                if sunrise_h <= (h % 24) <= 12:
                                    h_floor_fix = min_soc_val + 2.0
                                    
                                p_alloc_fix = max_p
                                if h == cur_hour:
                                    house_cons_fix = float(normalize_float(avg_prof_cons.get(str(cur_hour % 24), 0.5))) * occ_coeff
                                    house_rem_dc_fix = (house_cons_fix * h_f) / eff
                                    current_surplus_dc_fix = max(0.0, (b_soc - h_floor_fix) * b_cap / 100.0 - house_rem_dc_fix)
                                    p_alloc_fix = min(max_p, (current_surplus_dc_fix * eff) / h_f)
                                else:
                                    # v11.6.110: Same fix in Step 2 (Recursive Fix)
                                    # v11.6.111: Same fix for suffix in Step 2
                                    _prev_h_f = h - 1
                                    _prev_h_key_f = f"{(_prev_h_f)%24:02d}:59" + (" (Завтра)" if _prev_h_f >= 24 else "")
                                    if h_soc_sf := self._get_soc_from_log(sim_log_base, _prev_h_key_f, b_soc):
                                         surplus_hf_dc = max(0.0, (h_soc_sf - h_floor_fix) * b_cap / 100.0)
                                         p_alloc_fix = min(max_p, (surplus_hf_dc * eff) / h_f)
                                
                                if (rem_base_ac_fix + rem_bonus_ac_fix) > 0.05:
                                    is_morning_fix = sunrise_h <= (h % 24) <= 12
                                    
                                    # 1. Base
                                    p_base_fix = min(p_alloc_fix, rem_base_ac_fix / h_f)
                                    rem_base_ac_fix -= (p_base_fix * h_f)
                                    
                                    # 2. Bonus
                                    p_bonus_fix = 0.0
                                    if is_morning_fix and (p_alloc_fix - p_base_fix) > 0.01:
                                        p_bonus_fix = min(p_alloc_fix - p_base_fix, rem_bonus_ac_fix / h_f)
                                        rem_bonus_ac_fix -= (p_bonus_fix * h_f)
                                        
                                    actual_p_fix = p_base_fix + p_bonus_fix
                                    if actual_p_fix > 0.01:
                                        sell_commands[int(h)] = round_f(actual_p_fix, 3)
                                    else:
                                        sell_commands[int(h)] = 0.0
                                else:
                                    sell_commands[int(h)] = 0.0
                        
                        # 4. Re-run final simulation to verify the fix for next iteration
                        sim_commands_fix = {int(h): cmd for h, cmd in sell_commands.items()}
                        if best_buy_h is not None and best_buy_h < sim_end_h:
                            sim_commands_fix[int(best_buy_h)] = float(max_p)
                            
                        _, sim_log, _ = self.run_soc_simulation(
                            b_soc, sim_range, now, sim_commands_fix, 
                            b_min_soc=base_target,
                            no_battery_charge_until=_sell_sim_no_charge_until,
                            ignore_blended=True,
                            dynamic_floors=_strat_floors
                        )
                        soc_morning = self._get_soc_from_log(sim_log, key_morning, soc_after)
                        _pass_log += f" | P{pass_idx+1}:{soc_morning:.1f}%"
                        
                        # v11.6.179: Final Status Update (Post-Simulation)
                        # We determine the bottleneck by comparing original budgets
                        _is_p_limited = (available_sell_dc <= (physical_limit_dc + 0.01) and physical_limit_dc < (min(surplus_for_morning, surplus_for_user_limit) - 0.1))
                        _is_u_limited = (available_sell_dc <= (surplus_for_user_limit + 0.01) and surplus_for_user_limit < (surplus_for_morning - 0.1))
                        
                        limit_label = f"Лимит пользователя ({min_soc_val:.0f}%)"
                        _disp_goal = (min_soc_bat_val + 2.0) if 4 <= (cur_hour % 24) <= 12 else (min_soc_bat_val + soc_buffer_full)
                        _disp_txt = f"Защита дома (Лимит {_disp_goal:.0f}% УТРО)" if 4 <= (cur_hour % 24) <= 12 else f"Защита дома (Цель {_disp_goal:.0f}% к утру)"
                        res["arbitrage_sell_limit_reason"] = _disp_txt
                        limit_label = _disp_txt
                        
                        if _is_p_limited:
                             limit_label = f"Лимит мощности АКБ ({work_max_p:.1f}кВт)"
                        
                        total_planned_ac = sum(cmd * (max(0.1, (60 - now.minute) / 60.0) if h == cur_hour else 1.0) for h, cmd in sell_commands.items())
                        res["power_decision"] = f"{limit_label} | {total_planned_ac:.1f}кВтч в {self._format_h(min(epochs[0]))}-{self._format_h(max(epochs[0]))}"

                        # Update user status
                        if _is_p_limited:
                             res["arbitrage_sell_limit_reason"] = f"Лимит мощности АКБ ({work_max_p:.1f}кВт)"
                        elif _is_u_limited:
                             res["arbitrage_sell_limit_reason"] = f"Лимит пользователя ({min_soc_val:.0f}%)"
                        else:
                             res["arbitrage_sell_limit_reason"] = _disp_txt

                        res["_debug_passes"] = _pass_log

                        
                        # 5. Re-extract markers
                        if future_active_sell:
                            soc_after = self._get_soc_from_log(sim_log, key_after, b_soc)
                        soc_morning = self._get_soc_from_log(sim_log, key_morning, soc_after)
                        
                        res["morning_autopilot_active"] = True
                        res["morning_autopilot_floor"] = round_f(base_target, 1)

                    if not target_hours_sorted:
                        power_needed = 0.0
                        soc_at_start = b_soc
                        soc_after = b_soc  # v11.4.44-fix: only reset when no peaks, else keep sim value
                    # soc_morning remains as natural discharge result

                    # Removed temporary debug diagnostics

                    # v11.4.42: Unified night sub-simulation for morning SOC display.
                    # natural_morning_soc from sim_log_base is unreliable: the full sim distributes
                    # f_tom (tomorrow's solar) across ALL tomorrow hours. dist_tom['6'] can be >= 0.01
                    # so night clamp (real_h < 8) doesn't fire, giving +47% SOC overnight. Wrong.
                    # Fix: ALWAYS compute morning via a short night sub-sim starting from
                    # natural_soc_after_sale (Branch A) or post-sale SOC (Branch B).
                    # v11.6.162: Final Status and Projection Construction
                    morning_key_disp = f"{sunrise_h - 1:02d}:59 (Завтра)"
                    soc_morning_display = float(round_f(self._get_soc_from_log(sim_log, morning_key_disp, b_soc), 1))
                    
                    _all_sell_hrs = [h for h in target_hours_sorted if h >= cur_hour]
                    if _all_sell_hrs:
                        _soc_vals = []
                        for _h in _all_sell_hrs:
                            _k = f"{_h % 24:02d}:59" + (" (Завтра)" if _h >= 24 else "")
                            if _v := self._get_soc_from_log(sim_log, _k, b_soc):
                                _soc_vals.append(float(_v))
                        display_soc_after = min(_soc_vals) if _soc_vals else b_soc
                    else:
                        display_soc_after = b_soc
                        
                    # v11.6.172: Snap Projections to active limits for UI consistency
                    _limit_is_user = (base_target <= min_soc_val + 0.5)
                    if _limit_is_user:
                         if abs(display_soc_after - min_soc_val) < 1.0:
                              display_soc_after = min_soc_val
                    else:
                         # Protection Mode: Snap evening to Protection Floor, morning to Survival Target
                         if abs(display_soc_after - base_target) < 1.0:
                              display_soc_after = base_target
                         if abs(soc_morning_display - target_morning_soc) < 1.0:
                              soc_morning_display = target_morning_soc
                    
                    res["projected_soc_after_sale"] = round_f(display_soc_after, 1)
                    res["projected_soc_morning"] = round_f(soc_morning_display, 1)

                    # v11.6.162: Status Label Construction
                    limit_label = f"Лимит пользователя ({min_soc_val:.0f}%)"
                    if base_target > min_soc_val + 0.5:
                        _disp_goal = (min_soc_bat_val + 2.0) if 4 <= (cur_hour % 24) <= 12 else (min_soc_bat_val + soc_buffer_full)
                        limit_label = f"Защита дома (Лимит {_disp_goal:.0f}% УТРО)" if 4 <= (cur_hour % 24) <= 12 else f"Защита дома (Цель {_disp_goal:.0f}% к утру)"
                    
                    # v11.6.185: Restore missing variable for energy calculation
                    _all_sell_hrs = [h for h in target_hours_sorted if h >= cur_hour]
                    
                    # Core Diagnostic (v11.6.189: Use correct bottleneck flag)
                    total_planned_ac = sum(sell_commands.get(int(h), 0.0) * (max(0.1, (60 - now.minute) / 60.0) if h == cur_hour else 1.0) for h in _all_sell_hrs)
                    if _is_p_limited:
                         sell_diagnosis = f"Лимит мощности АКБ ({work_max_p:.1f}кВт)"
                    elif _is_u_limited:
                         sell_diagnosis = f"Лимит пользователя ({min_soc_val:.0f}%)"
                    else:
                         sell_diagnosis = limit_label

                    # v11.6.53: Smart Pool Splitting Status
                    future_sells = {h: p for h, p in sell_commands.items() if h >= cur_hour and p > 0.01}
                    if future_sells:
                        _epochs_ref = epochs if 'epochs' in locals() and epochs else [list(future_sells.keys())]
                        pool_strs = []
                        for ei, ep in enumerate(_epochs_ref):
                            ep_sells = {h: p for h, p in future_sells.items() if h in ep}
                            if not ep_sells: continue
                            
                            h_list = sorted(ep_sells.keys())
                            groups = []
                            current_group = [h_list[0]]
                            for i in range(1, len(h_list)):
                                if h_list[i] == h_list[i-1] + 1: current_group.append(h_list[i])
                                else:
                                    groups.append(current_group)
                                    current_group = [h_list[i]]
                            groups.append(current_group)
                            
                            if ei == 0:
                                group_strs = []
                                for g in groups:
                                    g_sum = sum(ep_sells[h] for h in g)
                                    first_g = g[0]
                                    last_g = g[-1]
                                    is_morn = (first_g % 24) >= 4 and (first_g % 24) <= 12
                                    prefix = "допродажа " if is_morn and g != groups[0] else ""
                                    if len(g) > 1:
                                        group_strs.append(f"{prefix}{g_sum:.1f}кВтч в {self._format_h(first_g)}-{self._format_h(last_g)}")
                                    else:
                                        group_strs.append(f"{prefix}{g_sum:.1f}кВтч в {self._format_h(first_g)}")
                                pool_strs.append(", ".join(group_strs))
                            else:
                                pool_strs.append(f"+ Пул {ei+1} (↑ солнце): {self._format_h(h_list[0])}")
                        
                        res["power_decision"] = f"{sell_diagnosis} | " + ", ".join(pool_strs)
                    else:
                        res["power_decision"] = sell_diagnosis
                        
                    res_soc_after = float(res["projected_soc_after_sale"])
                    res_soc_morning = float(res["projected_soc_morning"])

                    res["sell_simulation"] = {
                        "projected_soc_at_sale_start_pct": float(round_f(soc_at_start, 1)),
                        "projected_soc_after_sale_pct": res_soc_after,
                        "projected_soc_morning_pct": res_soc_morning,
                        "projected_soc_morning": res_soc_morning,
                        "log": sim_log
                    }

                    # v11.4.04: Reciprocal Surplus Calculation (Simulation Monarchy)
                    # We recalculate M, U based on what the simulation JUST confirmed.
                    true_m_surplus = round(((soc_morning - target_morning_soc) * b_cap / 100.0), 1)
                    true_u_surplus = round(((soc_after - base_target) * b_cap / 100.0), 1)
                    
                    # v11.6.200: Update the Diagnostic Reason string only if power_decision exists
                    true_sell_diag = res.get("power_decision")
                    if true_sell_diag:
                        if "Защита дома" in true_sell_diag:
                            # v11.6.198: Keep the target SOC in the diagnostic string
                            if "(" in true_sell_diag and ")" in true_sell_diag:
                                 _diag_parts = true_sell_diag.split(")")
                                 true_sell_diag = _diag_parts[0] + ")"
                            else:
                                 true_sell_diag = "Защита дома"
                        
                        # v11.6.72: Hyper-Detailed Diagnostics for Budget Debugging
                        diag_fixed = f"{true_sell_diag} | TRUE_M:{true_m_surplus:.1f} TRUE_U:{true_u_surplus:.1f} P:{physical_limit_dc:.1f}"
                        res["arbitrage_sell_limit_reason"] = (
                            f"{diag_fixed} | S:{soc_at_start:.1f}% Cur:{b_soc:.1f}% | "
                            f"Cap:{b_cap:.1f} T:{base_target:.0f}% Eff:{eff:.3f} "
                            f"M_dc:{surplus_for_morning:.2f} U_dc:{surplus_for_user_limit:.2f} AC:{available_sell_ac:.2f} "
                            f"NoChg:{_sell_sim_no_charge_until}"
                        )




                    # v7.1: Note: Simulation results are no longer used to override target_soc (v11.1.61).
                    
                    # v11.1.20 - Calculate potential gain using target_price if we are preparing for a future peak
                    best_sell_price_for_arb = max(cur_p_f, float(target_price or 0.0))
                    gain_for_attr = float(best_sell_price_for_arb * eff - p_bb - deg_cost) if h_bb is not None else 0.0

                    # Arbitrage details for UI attributes
                    # v11.6.71: Synchronize attributes with the FINAL results (including Step 2)
                    final_total_sell_ac = sum(sell_commands.values()) if sell_commands else 0.0
                    
                    res["arbitrage_buyback"] = {
                        "power_kw": 0.0,
                        "note": "Нет выгодного окна для откупа",
                        "available_kwh": float(round_f(final_total_sell_ac, 2)),
                        "sunrise_hour": sunrise_h,
                        "soc_buffer_pct": float(soc_buffer_val),
                        "target_morning_soc_pct": float(target_morning_soc),
                        "reserve_kwh": float(round_f(target_morning_soc * b_cap / 100.0, 2)),
                        "energy_to_wait_kwh": float(round_f(total_cons_to_sunrise, 2)),
                        "ai_floor_soc_pct": float(round_f(ai_soc_floor_final, 1)),
                        "gatekeeper_floor": float(round_f(res.get("morning_autopilot_floor", ai_soc_floor_final), 1)),
                    }

                    if h_bb is not None and (gain_for_attr >= threshold):
                        res["arbitrage_buyback"]["power_kw"] = max_p
                        res["arbitrage_buyback"]["note"] = f"Откуп в {self._format_h(h_bb)} по {p_bb:.2f}"
                
            # v11.6.116: Filter out hours with 0.0 kW power from planning completely.
            # Strategy is only active if planned power > 0.01 or (BUY mode and negative price).
            _filtered_targets = []
            for h in target_hours_sorted:
                p_val = sell_commands.get(h, 0.0) if mode == "sell" else charge_commands.get(h, 0.0)
                is_neg_buy = (mode == "buy" and negative_hours and h in negative_hours)
                if p_val > 0.01 or is_neg_buy:
                    _filtered_targets.append(h)
            target_hours_sorted = _filtered_targets

            # Use current peak power only if we are actually in a peak hour
            in_peak = (cur_hour in target_hours_sorted)
            if in_peak:
                res["state"] = "active"
            
            real_cmd_p = power_needed if in_peak else 0.0

            res["recommended_power_kw"] = float(round_f(min(float(power_needed), max_p), 3))
            
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
            
            p_distribution = {}
            if actual_active:
                sim_info = res.get("sell_simulation" if mode == "sell" else "buy_simulation")
                s_log = sim_info.get("log", {}) if sim_info else {}
                
                # v11.6.40: Show ALL hours of the first Energy Pool in planned_power
                _first_window_active = res.get("first_pool_hours", actual_active)
                
                # v11.6.116: Filter UI display to match the 0.0kW cleanup
                _first_window_active = [h for h in _first_window_active if h in target_hours_sorted]
                
                for h_idx in sorted(_first_window_active):
                    h_label = self._format_h(h_idx)
                    p_val = sell_commands.get(h_idx, 0.0) if mode == "sell" else charge_commands.get(h_idx, 0.0)
                    
                    is_tom = (h_idx >= 24)
                    h_idx_norm = h_idx % 24
                    key_h = f"{h_idx_norm:02d}:59" + (" (Завтра)" if is_tom else "")
                    
                    # v11.6.113: Re-anchor starting SOC for EVERY hour from the log.
                    # This correctly handles gaps (house load between sell windows).
                    _prev_h = h_idx - 1
                    if h_idx == cur_hour:
                        _h_start_soc = float(b_soc)
                    else:
                        _is_tom_prev = (_prev_h >= 24)
                        _prev_key = f"{_prev_h % 24:02d}:59" + (" (Завтра)" if _is_tom_prev else "")
                        _h_start_soc = float(self._get_soc_from_log(s_log, _prev_key, b_soc))
                    
                    # Fallback for forecast display
                    h_soc_sim = float(self._get_soc_from_log(s_log, key_h, target_soc))
                    
                    if mode == "sell":
                        h_f_local = max(0.1, (60 - now.minute) / 60.0) if h_idx == cur_hour else 1.0
                        house_cons_local = float(normalize_float(self.manager.get_average_profile("consumption_total", self.manager.custom_period, now.weekday()).get(str(h_idx_norm), 0.5))) * occ_coeff
                        house_rem_dc_local = (house_cons_local * h_f_local) / eff
                        discharge_dc_local = (p_val * h_f_local) / eff
                        pure_discharge_pct_local = (discharge_dc_local + house_rem_dc_local) / b_cap * 100.0 if b_cap > 0.1 else 0.0
                        
                        _target_limit_local = min_soc_val + 2.0 if (sunrise_h <= h_idx_norm <= 12) else base_target
                        # Target is start_soc minus pure discharge (ignoring solar)
                        h_target = max(_target_limit_local, _h_start_soc - pure_discharge_pct_local)
                        
                        if p_val > 0.01:
                            p_distribution[h_label] = f"{round_f(p_val, 2)} kW (Цель: {round_f(h_target, 1)}% | Прогноз: {round_f(h_soc_sim, 1)}%)"
                        else:
                            p_distribution[h_label] = f"{round_f(p_val, 2)} kW (Прогноз: {round_f(h_soc_sim, 1)}%)"
                        
                    else:
                        p_distribution[h_label] = f"{round_f(p_val, 2)} kW (Прогноз: {round_f(h_soc_sim, 1)}%)"
                    
            res["planned_power_per_h"] = p_distribution
            
            # v11.6.101: Solar-Blind Target SOC for Selling
            # If we use the simulation log (which includes solar charging), the target_soc 
            # will be artificially raised (e.g., 17.2% instead of 15.0%). 
            # This causes the inverter to stop discharging too early, preventing solar energy 
            # from being exported to the grid. The inverter must receive the PURE discharge target.
            if in_peak:
                if mode == "sell":
                    h_f = max(0.1, (60 - now.minute) / 60.0)
                    house_cons = float(normalize_float(self.manager.get_average_profile("consumption_total", self.manager.custom_period, now.weekday()).get(str(cur_hour), 0.5))) * occ_coeff
                    house_rem_dc = (house_cons * h_f) / eff
                    discharge_dc = (power_needed * h_f) / eff
                    pure_discharge_pct = (discharge_dc + house_rem_dc) / b_cap * 100.0 if b_cap > 0.1 else 0.0
                    
                    # v11.6.119: Align target_soc with end of CURRENT HOUR (Solar-Blind)
                    # We target the SOC reached by discharging battery + house load, ignoring solar gain.
                    target_soc = max(0.0, b_soc - pure_discharge_pct)
                    
                    # Ensure we never target below the identified safe floor for this hour
                    _target_limit = min_soc_val + 2.0 if (sunrise_h <= (cur_hour % 24) <= 12) else base_target
                    target_soc = max(target_soc, _target_limit)
                else:
                    # For buying, it's fine to use the simulation log (HH:59)
                    sim_info = res.get("buy_simulation")
                    if sim_info:
                        s_log = sim_info.get("log", {})
                        key_cur = f"{now.hour:02d}:59"
                        if key_cur in s_log:
                            target_soc = float(self._get_soc_from_log(s_log, key_cur, target_soc))

                
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
            elif state in ["price_limit_not_met", "unprofitable_arbitrage"] or not target_hours_sorted or state == "standard":
                if mode == "buy":
                    if res.get("charge_reason") == "none":
                        cur_mode_text = "В покупке нет необходимости"
                    else:
                        cur_mode_text = "Нет ценового окна"
                else: # sell
                    if state == "standard":
                         cur_mode_text = "Ожидание"
                    else:
                         cur_mode_text = "Нет ценового окна"
            elif state == "standard":
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

