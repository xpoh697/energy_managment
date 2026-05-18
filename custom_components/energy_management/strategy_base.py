import logging
# Version change trace v11.9.685: Sunset-to-Sunset window and solar-aware survival floors.
# Version change trace v11.9.650: Unified logic for grid bypass and SimTrace integration.
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
    VERSION,
    INVERTER_MODES
)
from .utils import get_kwh_val, normalize_float, get_price_from_store, round_f

# Legacy aliases for safety during refactoring synchronization
_get_kwh_val = get_kwh_val
_normalize_float = normalize_float

_LOGGER = logging.getLogger(__name__)
class StrategyEngine:
    """Mathematical engine for energy management strategies and simulations."""
    manager: 'EnergyProfileManager'
    
    def __init__(self, manager: 'EnergyProfileManager'):
        self.manager = manager
        self._strategy_cache = {}
        self._calculating_strategy = False

    def clear_cache(self):
        """Forcefully clears the strategy calculation cache."""
        self._strategy_cache = {}

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

    def _group_contiguous(self, hours):
        """Groups a list of hours into contiguous periods."""
        if not hours: return []
        sorted_h = sorted(hours)
        groups = []
        start = sorted_h[0]
        for i in range(1, len(sorted_h)):
            if sorted_h[i] != sorted_h[i-1] + 1:
                groups.append(list(range(start, sorted_h[i-1] + 1)))
                start = sorted_h[i]
        groups.append(list(range(start, sorted_h[-1] + 1)))
        return groups

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
        
        # v11.9.205: Hardcoded to 0.98 per user request to stabilize simulations
        return 0.98

    def resolve_consumption_profiles(self, p_type: str, eff_period: int, day_idx: int) -> Tuple[Dict[str, float], Dict[str, float], str]:
        """
        v12.0.81: Unified resolver for consumption profiles with a safe fallback of 0.3 kW.
        Returns: (prof_today, prof_tomorrow, actual_profile_used)
        """
        man = self.manager
        
        def get_profile_sum(p_dict):
            if not p_dict: return 0.0
            try: return sum(max(0.0, float(v)) for v in p_dict.values() if v is not None)
            except: return 0.0

        # 1. Try to read requested profile (base)
        p_today = dict(man.get_predicted_profile(p_type) or {})
        p_tom = dict(man.get_average_profile(p_type, eff_period, day_idx) or {})
        
        sum_today = get_profile_sum(p_today)
        sum_tom = get_profile_sum(p_tom)
        
        # 2. If base profile is empty/zero, try consumption_total
        if p_type == "consumption_base" and sum_today < 0.5 and sum_tom < 0.5:
            p_today = dict(man.get_predicted_profile("consumption_total") or {})
            p_tom = dict(man.get_average_profile("consumption_total", eff_period, day_idx) or {})
            sum_today = get_profile_sum(p_today)
            sum_tom = get_profile_sum(p_tom)
            if sum_today >= 0.5 or sum_tom >= 0.5:
                _LOGGER.info("Energy Management [v12.0.81]: 'consumption_base' is empty. Falling back to 'consumption_total'.")
                return p_today, p_tom, "consumption_total"

        # 3. If BOTH profiles are empty/zero -> flat 0.3 kW fallback and log warning
        if sum_today < 0.5 and sum_tom < 0.5:
            _LOGGER.warning(
                "Energy Management [v12.0.81] CRITICAL: Both '%s' (today=%.2fkWh, tom=%.2fkWh) and 'consumption_total' profiles are EMPTY or ZERO! "
                "Forcing flat fallback load of 0.3 kW for all hours to prevent battery deep-discharge blindness. "
                "Please check energy/consumption sensors configuration.",
                p_type, sum_today, sum_tom
            )
            flat_profile = {str(h): 0.3 for h in range(24)}
            return flat_profile, flat_profile, "fallback_0.3kw"
            
        return p_today, p_tom, p_type

    def get_survival_floor(self, start_h_abs: int, end_h_abs: int, target_at_end: float = None, ignore_solar: bool = False) -> float:
        """
        v11.9.718: Proper Reverse Bridging calculation.
        Calculates the SOC needed at 'start_h_abs' to reach 'end_h_abs' 
        while having 'target_at_end' remaining.
        
        Logic: Steps backwards from end to start.
        """
        man: Any = self.manager
        _, b_cap, _ = man.get_battery_state()
        b_cap = float(b_cap or 10.0)
        eff = float(self.get_efficiency_coefficient() or 0.95)
        min_soc = float(man.get_setting(CONF_MIN_SOC_BAT, 10.0))
        
        # If no target specified, we want to reach min_soc
        req_soc = target_at_end if target_at_end is not None else min_soc
        
        prof_gen = dict(man.get_predicted_profile("generation") or {})
        
        # v12.0.81: Use unified safe resolved profile
        prof_cons, _, _ = self.resolve_consumption_profiles("consumption_base", 14, man.day_type)
        
        # Walk backwards from future to now
        for h_abs in range(end_h_abs - 1, start_h_abs - 1, -1):
            h_rel = str(h_abs % 24)
            l_val = float(normalize_float(prof_cons.get(h_rel, 0.4)))
            g_val = float(normalize_float(prof_gen.get(h_rel, 0.0))) if not ignore_solar else 0.0
            
            # net_h_kwh = (Load - Gen) / Eff
            net_h_kwh = (l_val - g_val) / eff
            
            # Convert to SOC pct
            h_soc_pct = (net_h_kwh / b_cap * 100.0) if b_cap > 0 else 0
            
            # New required SOC before this hour
            req_soc += h_soc_pct
            
            # v12.0.85: Strict Buffer Protection (TS 1.1)
            # Prevent early morning solar generation from "eating" the night buffer.
            # req_soc must never drop below the target we are bridging towards.
            base_floor = target_at_end if target_at_end is not None else min_soc
            req_soc = max(base_floor, req_soc)
            
        return round_f(req_soc, 1)

    def get_gatekeeper_floor(self, h_abs: int, end_h_abs: int, h_end_pool: int = None) -> float:
        """Calculate unified gatekeeper floor (Turbo or Safe) as per TS Section 1.1."""
        h_rel = h_abs % 24
        min_soc = float(self.manager.get_setting(CONF_MIN_SOC_BAT, 10.0))
        
        if 4 <= h_rel < 10:
            # Turbo Mode: MinSOC + 2%
            return round_f(min_soc + 2.0, 1)
        else:
            # Safe Mode: TS 1.1 Reverse Bridging
            user_limit = float(self.manager.get_setting(CONF_AI_DISCHARGE_LIMIT, 20.0))
            soc_buffer = float(self.manager.get_setting(CONF_SOC_BUFFER, 5.0))
            
            if h_end_pool is None or h_end_pool < h_abs:
                h_end_pool = h_abs
                
            # 1. Survival = MinSOC + Buffer + Дом_после_продажи_до_рассвета
            # Calculated backward from sunrise to h_end_pool. We ignore solar to strictly protect the morning buffer.
            survival_soc = self.get_survival_floor(h_end_pool, end_h_abs, target_at_end=min_soc + soc_buffer, ignore_solar=True)
            
            # 2. T_end = max(User_Limit, Survival)
            t_end = max(user_limit, survival_soc)
            
            # 3. Floor = T_end + Дом_от_сейчас_до_конца_продаж
            floor = self.get_survival_floor(h_abs, h_end_pool, target_at_end=t_end, ignore_solar=True)
            
            return floor

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
            
            # --- Curtailment Correction (v4.2/v11.7.47) ---
            # Generation is throttled if:
            # 1. Mode is 'stop_sale' AND battery is full (>90%).
            # 2. Mode is 'no_pv_sale_no_bat' (waiting for negative prices).
            # 3. Current price is negative (we don't want PV to charge battery or export).
            cur_mode = getattr(man, "current_inverter_mode", "")
            is_no_pv_mode = cur_mode == "no_pv_sale_no_bat"
            
            # Get current buy price
            cur_price = 0.0
            try:
                cur_price = float(man.get_price("buy", now.strftime("%Y-%m-%d"), now.hour))
            except Exception: pass
            
            is_negative_price = bool(cur_price <= 0.01)
            is_stop_sale_curtail = bool(cur_mode == "stop_sale" and man.get_sensor_float(man.battery_soc_sensor, 0.0) > 90)

            if (is_no_pv_mode or is_negative_price or is_stop_sale_curtail) and today_coeff < 1.0:
                # We suspect curtailment. Use historical accuracy or 1.0 instead of zeroed-out performance.
                old_today = today_coeff
                today_coeff = max(today_coeff, hist_coeff, 1.0)
                if abs(today_coeff - old_today) > 0.01:
                    _LOGGER.debug(f"[Strategy] Curtailment detected (mode={cur_mode}, price={cur_price}, SOC={man.get_sensor_float(man.battery_soc_sensor, 0.0)}%). Corrected today_coeff: {old_today:.2f} -> {today_coeff:.2f}")

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

                # v11.1.97 - Block managed loads during active selling or emergency modes
                inverter_mode = getattr(man, "current_inverter_mode", "")
                is_emergency = inverter_mode == "bat_emergency"
                is_selling_mode = inverter_mode in ("sale_pv_no_bat", "sale_pv_bat")

                price_suffix = " (Беспл. цена)" if is_free_price else ""
                if is_emergency:
                    permissions[s_id_s] = False
                    permissions_reasons[s_id_s] = "Запрет: Аварийный стоп АКБ"
                elif is_selling_mode and not is_free_price:
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
                "batt_energy_val": float(b_energy_f or 0.0),
                "expected_consumption": float(essential_house_consumption or 0.0),
                "sun_overflow_kwh": round_f(overflow_today, 3),
                "battery_surplus_kwh": round_f(batt_surplus, 3),
                "potential_export_kwh": round_f(overflow_today + batt_surplus, 3),
                "permissions": permissions or {},
                "permissions_reasons": permissions_reasons or {},
                "forecast_val": float(forecast_val_adjusted or 0.0),
                "forecast_coefficient": float(blended_coeff or 1.0),
                "forecast_today_coefficient": float(today_coeff or 1.0),
                "efficiency_coefficient": float(eff_coeff or 1.0),
                "occupancy_coefficient": float(occ_coeff or 1.0),
                "degradation_cost": float(self.get_battery_degradation_cost() or 0.0),
                "solar_actual_today": float(actual_today or 0.0),
                "solar_expected_total": float(expected_today_total),
                "solar_expected_so_far": float(hist_gen_so_far),
                "solar_fraction_so_far": float(external_progress if 'external_progress' in locals() else fraction_so_far),
                "forecast_distribution": active_dist,
                "forecast_dist_source": dist_source,
                "available_power_total_kw": float(initial_power_kw or 0.0),
                "available_gen_kw": float(available_gen_kw or 0.0),
                "reserved_by": reserved_by,
                "sunrise_hour": int(res_sunrise if 'res_sunrise' in locals() else 8),
                "battery_discharge_budget_kw": float(batt_discharge_allowed or 0.0)
            }
            self._strategy_cache["budget_permissions"] = {"time": now, "res": return_res}
            return return_res
        finally:
            self._calculating_strategy = old_calc

    def _get_soc_from_log(self, log: dict, key: Any, default: Optional[float]) -> Optional[float]:
        """Safely extract SOC float from simulation log. Supports int, str, and HH:59 formats."""
        if not log: return default
        
        # 1. Direct lookup
        val = log.get(key)
        
        # 2. Fallback for integer keys (convert to HH:59 string format)
        if val is None and isinstance(key, (int, float)):
            h_abs = int(key)
            h_rel = h_abs % 24
            is_tom = h_abs >= 24
            is_dafter = h_abs >= 48
            suffix = " (Завтра)" if is_tom else (" (Через день)" if is_dafter else "")
            str_key = f"{h_rel:02d}:59{suffix}"
            val = log.get(str_key)
            
        # 3. Fallback for string keys that might be integers in the log
        if val is None and isinstance(key, str):
            if key.isdigit():
                val = log.get(int(key))
            elif ":" in key:
                try:
                    h_rel = int(key.split(":")[0])
                    is_tom = "Завтра" in key
                    is_dafter = "Через день" in key
                    h_abs = h_rel + (24 if is_tom else (48 if is_dafter else 0))
                    val = log.get(h_abs)
                except: pass

        if isinstance(val, dict):
            res = val.get("soc", val.get("soc_end", default))
        else:
            res = val if val is not None else default
        return float(res) if res is not None else default

    def is_today_log_key(self, h: Any) -> bool:
        """Determines if a simulation log key belongs to 'Today'."""
        if isinstance(h, int): return h < 24
        if isinstance(h, str): return ":" in h and "Завтра" not in h and "день" not in h
        return False

    def is_tomorrow_log_key(self, h: Any) -> bool:
        """Determines if a simulation log key belongs to 'Tomorrow'."""
        if isinstance(h, int): return 24 <= h < 48
        if isinstance(h, str): return "Завтра" in h
        return False

    def is_dafter_log_key(self, h: Any) -> bool:
        """Determines if a simulation log key belongs to 'Day After Tomorrow'."""
        if isinstance(h, int): return h >= 48
        if isinstance(h, str): return "день" in h
        return False

    def run_soc_simulation(self, start_soc, sim_range, now, commands=None, b_min_soc=0.0, man=None, house_profile_override=None, no_battery_charge=False, no_battery_charge_until=None, pv_curtail_hours=None, ignore_blended=False, dynamic_floors=None, no_solar=False, allow_discharge=True, attempt=0, ignore_house_in_hours=None, no_solar_to_bat=False, mode_overrides=None, current_mode=None, dynamic_ceilings=None):
        """Universal SOC simulation engine."""
        if not sim_range:
            return float(start_soc), {}, 0.0
        
        # v11.7.51: Start SOC Debug
        _LOGGER.debug(f"[SimStart] start_soc: {start_soc} (type: {type(start_soc)})")

        man = man or self.manager
        # v11.9.466: Resolve hardware Min SOC if not explicitly passed
        if b_min_soc < 0.01:
            b_min_soc = float(man.get_setting(CONF_MIN_SOC_BAT, 10.0))

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

        _LOGGER.debug(f"[SimSolar] today: {f_today:.2f} (dist: {len(dist_today) if dist_today else 0}), tom: {f_tom:.2f} (dist: {len(dist_tom) if dist_tom else 0})")

        # 2. Consumption profiles (7-day Aware Total Load) - v12.0.81
        p_type = house_profile_override or "consumption_total"
        prof_cons_today, prof_cons_tom, resolved_p_type = self.resolve_consumption_profiles(p_type, eff_period, day_idx_tom)
        
        # 3. Generation profiles (Historical Baseline)
        prof_gen_today = dict(man.get_average_profile("generation", eff_period, day_idx_today))
        prof_gen_tom = dict(man.get_average_profile("generation", eff_period, day_idx_tom))

        # v11.4.49: Pre-load historical losses profile for idle_p computation inside loop.
        # Using historical hourly rate (kWh/h) per simulated hour is correct;
        # the old man.current_losses was an intra-hour accumulator, yielding 0 at hour
        # boundaries and an average of 50% of the real rate.
        prof_losses = dict(man.get_average_profile("losses", 7))
        
        # v11.6.57: ignore_blended allows skipping the last_blended_coeff
        # v11.7.46: Reset stickiness — if we are in the first hour of the day, force 1.0
        blended_coeff = 1.0
        if not ignore_blended:
            if now.hour > 0:
                blended_coeff = float(getattr(man, "last_blended_coeff", 1.0))
            else:
                # Midnight reset to prevent yesterday's pessimism from poisoning tomorrow's plan
                blended_coeff = 1.0
                man.last_blended_coeff = 1.0
        eff_coeff = float(self.get_efficiency_coefficient() or 1.0)
        _LOGGER.debug(f"[SimSolar] today: {f_today:.2f}, tom: {f_tom:.2f} | blended: {blended_coeff:.3f}")
        fraction_left_h1 = float(1.0 - (now.minute / 60.0))
        max_batt_p_v = man.get_setting(CONF_BATTERY_MAX_POWER, 5.0)
        max_batt_p = float(max_batt_p_v) if max_batt_p_v is not None else 5.0
        man = self.manager
        all_prices = {}
        history_log = {}
        
        # v11.9.442: Pre-load Morning Mode settings for simulation accuracy
        from .const import CONF_PRICE_SELL_ONLY_PV, CONF_SALE_PV_NO_BAT_MAX_HOUR
        price_sell_only_pv = float(man.get_setting(CONF_PRICE_SELL_ONLY_PV, 999.0))
        sale_pv_no_bat_max_hour = float(man.get_setting(CONF_SALE_PV_NO_BAT_MAX_HOUR, 13.0))

        # v11.9.496: Command Key Normalization (Supports '1', 1, '1h')
        norm_commands = {}
        if commands:
            for k, v in commands.items():
                try:
                    clean_k = str(k).replace("h", "")
                    norm_commands[int(clean_k)] = float(v)
                except: continue
        commands = norm_commands

        try:
            today_str = now.strftime("%Y-%m-%d")
            tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
            
            p_buy = dict(man.data.get("prices_buy", {}))
            for h, p in p_buy.get(today_str, {}).items(): all_prices[int(h)] = float(normalize_float(p))
            for h, p in p_buy.get(tomorrow_str, {}).items(): all_prices[int(h) + 24] = float(normalize_float(p))
            
            # v11.9.635: Load sell prices for accurate Morning Mode simulation
            all_sell_prices = {}
            p_sell = dict(man.data.get("prices_sell", {}))
            for h, p in p_sell.get(today_str, {}).items(): all_sell_prices[int(h)] = float(normalize_float(p))
            for h, p in p_sell.get(tomorrow_str, {}).items(): all_sell_prices[int(h) + 24] = float(normalize_float(p))
        except Exception:
            all_sell_prices = all_prices # Fallback

        simulated_soc = float(start_soc)
        overflow_kwh = 0.0
        for i, h_abs in enumerate(sim_range):
            try:
                real_h = int(h_abs % 24)
                is_tom = bool(h_abs >= 24)
                h_str = str(real_h)
            
                step_duration = float(fraction_left_h1 if i == 0 else 1.0)
                if step_duration <= 0.001: continue

                expected_gen_kw = 0.0
                expected_cons_kw = 0.0
                tom_coeff = 1.0
                
                # 1. Generation Forecast for this hour
                if is_tom:
                    h_key = str(real_h)
                    # v11.7.40: Ignore blended_coeff for tomorrow's forecast to prevent double-pessimism
                    tom_coeff = 1.0 
                    if dist_tom:
                        total_dist = sum(float(normalize_float(v)) for v in dist_tom.values())
                        h_acc, _ = self.get_hourly_accuracy_coeff(real_h)
                        expected_gen_kw = float(float(normalize_float(dist_tom.get(h_key, 0.0))) / total_dist * f_tom * tom_coeff * h_acc) if total_dist > 0.1 else 0.0
                    else:
                        total_hist = sum(float(normalize_float(v)) for v in prof_gen_tom.values())
                        h_acc, _ = self.get_hourly_accuracy_coeff(real_h)
                        expected_gen_kw = float(float(normalize_float(prof_gen_tom.get(h_str, 0.0))) / total_hist * f_tom * tom_coeff * h_acc) if total_hist > 0.1 else 0.0
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
                # v11.7.27: Relaxed morning clamp to 06:00 to allow sunrise anticipation.
                if hist_h_val < 0.01 and (real_h < 6 or real_h > 21):
                    expected_gen_kw = 0.0
    
                # v11.6.412: PV Curtail logic (Dead parameter revival)
                if pv_curtail_hours is not None and int(h_abs) in pv_curtail_hours:
                    expected_gen_kw = 0.0
    
                # v11.9.715: Strategic Hourly Forecast Override (Solcast/Smart Load)
                # If a specific hourly forecast is available, it overrides all historical distribution logic.
                hourly_gen_map = man.get_forecast_hourly("generation")
                if hourly_gen_map and int(h_abs) in hourly_gen_map:
                    expected_gen_kw = float(hourly_gen_map[int(h_abs)])


                # 3. Expected consumption (v7.9.4 - Base profile)
                p_cons = prof_cons_tom if is_tom else prof_cons_today
                
                # v11.4.49: Always use consumption_total (as configured)
                occ_coeff, _, _, _, _, _, _ = man.get_occupancy_coefficient()
                occ_coeff = float(occ_coeff)
                expected_cons_kw = float(normalize_float(p_cons.get(h_str, 0.0))) * occ_coeff
                
                if (real_h >= 22 or real_h <= 6) and expected_cons_kw > 3.0:
                    expected_cons_kw = 0.5
    
                # v11.6.325: House-Blind strategy override
                if ignore_house_in_hours is not None and int(h_abs) in ignore_house_in_hours:
                    expected_cons_kw = 0.0
    
                # v11.1.15 - Blended Anchor
                    # v11.9.525: Smart Purification. If real_load includes battery charging, 
                    # the simulation "leaks" energy. We subtract charging power if detected.
                    real_load = float(getattr(man, "avg_base_load_kw" if house_profile_override == "consumption_base" else "avg_load_kw", expected_cons_kw))
                    
                    # Heuristic: if charging > 0.1kW and real_load > 0.5kW, 
                    # and the user hasn't explicitly separated the sensors, 
                    # we subtract the charge to avoid double-counting.
                    cur_batt_p = float(man.get_sensor_float(man.battery_power_sensor) or 0.0)
                    if cur_batt_p < -0.1: # Charging (negative sign convention)
                        p_charge = abs(cur_batt_p)
                        # If load is significantly higher than charge, it's likely blended
                        if real_load > (p_charge * 0.8):
                             real_load = max(0.1, real_load - p_charge)
                             
                    expected_cons_kw = (real_load * anchor_weight) + (expected_cons_kw * (1.0 - anchor_weight))
            
                # First hour solar correction: 
                if i == 0:
                    # v11.1.15 - Blended Solar Anchor: Same logic as load to prevent sawtooth
                    real_gen_kw = float(getattr(man, "avg_gen_kw", 0.0))
                    if real_gen_kw > 0.01:
                        anchor_weight = max(0.0, min(1.0, (now.minute / 60.0)))
                        expected_gen_kw = (real_gen_kw * anchor_weight) + (expected_gen_kw * (1.0 - anchor_weight))
    
                # v11.4.49: Idle/losses correction — add BEFORE net computation.
                if eff_coeff < 0.999:  # If eff sensor embeds losses already, skip to avoid double-count
                    idle_p = float(prof_losses.get(h_str, 0.05))
                    expected_cons_kw += idle_p
    
                # v11.6.487: Block solar if price is zero or negative (panels disconnected)
                # v11.9.635: Use sell price for Morning Mode logic
                _h_price = float(normalize_float(all_prices.get(int(h_abs), 0.1)))
                _h_sell_price = float(normalize_float(all_sell_prices.get(int(h_abs), _h_price)))
                
                if _h_price <= 0.0:
                    expected_gen_kw = 0.0
    
                # 4. Inverter Command (AI Buying/Selling)
                # v11.9.660: Strict explicit command handling. NO DISCOVERY from strategy_results here
                # as it causes feedback loops during strategy calculation.
                _cmd_map = commands if commands else {}
                _raw_p = _cmd_map.get(int(h_abs), 0.0)
                if isinstance(_raw_p, dict):
                    cmd_p = float(_raw_p.get("power", 0.0))
                else:
                    cmd_p = float(_raw_p)

                _prev_soc_for_log = simulated_soc
    
                # v11.9.331: Apply InverterModeClass rules from mode_overrides map.
                # mode_overrides is a dict {abs_hour: mode_name_str} pre-computed by _get_mode_at.
                _h_mode_name = (mode_overrides or {}).get(int(h_abs))
                
                # v11.9.443: Check for Manual Overrides in the Manager (Top Priority)
                # This ensures that 'hand' icons on the dashboard are respected in ALL simulations.
                _h_dt = (now + timedelta(hours=i)).replace(minute=0, second=0, microsecond=0)
                _h_ts_key = _h_dt.strftime("%Y-%m-%d %H:00")
                _manual_m = man.hourly_manual_overrides.get(_h_ts_key)
                _manual_soc = None
                if _manual_m:
                    _h_mode_name = _manual_m.get("mode")
                    _manual_soc = _manual_m.get("soc_limit")
                    # v11.9.539: Inject manual power commands into simulation
                    if _h_mode_name == "buy":
                        cmd_p = max_batt_p
                    elif _h_mode_name == "sale_pv_bat":
                        cmd_p = -max_batt_p
                    elif _h_mode_name in ["stop_sale", "sale_pv_no_bat"]:
                        cmd_p = 0.0

                # v12.0.80: Correct order-of-operations to resolve _h_mode_str before physics.
                # Determine Mode Config for this simulation hour first
                _h_mode_str = _h_mode_name
                if h_abs == now.hour and _h_mode_str is None:
                    _h_mode_str = current_mode
                
                if _h_mode_str is None:
                    _h_mode_str = "sale_pv"
                
                # v12.0.80: Dynamic Emergency Guard to prevent two-pass feedback loops
                if _h_mode_str == "bat_emergency" and round(simulated_soc, 1) > b_min_soc:
                    # High-fidelity SOC has recovered or is above min, fallback to safe normal discharge
                    _h_mode_str = "sale_pv"
                elif round(simulated_soc, 1) <= b_min_soc and not _manual_m and _h_mode_str != "buy":
                    # SOC has depleted to emergency floor, dynamically engage protective bat_emergency
                    _h_mode_str = "bat_emergency"
                
                # Safety fallback for specific mode names like 'sale_pv' -> 'sale_pv_bat'
                if _h_mode_str == "sale_pv": _h_mode_str = "sale_pv"
                
                # Resolve class and config based on final _h_mode_str
                _h_mode_cls = INVERTER_MODES.get(_h_mode_str)
                _mode_cfg = _h_mode_cls if _h_mode_cls else INVERTER_MODES["sale_pv"]

                # Balance = (Solar - Load) + Grid_Command
                _expected_gen_kw_sim = 0.0 if no_solar else expected_gen_kw
                
                # v11.9.331: If mode curtails PV (e.g. no_pv_sale_no_bat), cap solar to house load.
                if _h_mode_cls is not None and _h_mode_cls.curtail_pv:
                    _expected_gen_kw_sim = min(_expected_gen_kw_sim, expected_cons_kw)

                # 1. House Load Balance (Solar covers load first)
                sim_p_bat = 0.0
                p_for_house = min(_expected_gen_kw_sim, expected_cons_kw)
                rem_gen = _expected_gen_kw_sim - p_for_house
                rem_cons = expected_cons_kw - p_for_house
                
                # v11.9.650/660: Physics - In BUY/CHARGE modes with grid bypass, 
                # the house load is powered by the grid, NOT the battery.
                if _h_mode_cls and _h_mode_cls.is_grid_bypass:
                    rem_cons = 0.0
                
                # 2. Total Net Power for battery
                # Solar charge depends on mode flag
                _pv_to_bat = rem_gen if (_mode_cfg.charge_from_pv and not no_solar_to_bat) else 0.0
                _solar_charge = _pv_to_bat if (_mode_cfg.charge_from_pv and not no_solar_to_bat) else 0.0
                
                # v11.9.479/482: Charging bypass. If grid charging is active, house is powered by grid.
                # v11.9.523: In Buy mode, battery gets FULL cmd_p (Grid covers house).
                if cmd_p > 0.01:
                    total_net_kw = float(cmd_p + max(0.0, _solar_charge))
                else:
                    # Battery power depends on discharge flag for house load
                    if _mode_cfg.discharge_to_house:
                        total_net_kw = float(_solar_charge - rem_cons + cmd_p)
                    else:
                        total_net_kw = float(_solar_charge + cmd_p)
                
                # v11.9.480: Trace power for debugging
                if abs(cmd_p) > 0.001 or abs(total_net_kw) > 0.1:
                    _LOGGER.debug(f"[SimTrace] H:{h_abs:02d} M:{_h_mode_str[:4]} PV:{_solar_charge:.2f} L:{expected_cons_kw:.2f} Cmd:{cmd_p:.2f} Net:{total_net_kw:.3f} SOC:{simulated_soc:.1f}%")

                # v11.9.482: Export Logic integration
                if _mode_cfg.export_pv_to_grid and not _mode_cfg.charge_from_pv:
                    # If we export PV and don't charge from it, solar to bat is 0
                    total_net_kw = float(total_net_kw - _solar_charge)
                    _solar_charge = 0.0
            
                sim_eff = float(max(0.85, eff_coeff))
                sim_p_sale = 0.0
                
                # v11.9.545: Resolve Trade Floor early for logging
                h_idx_int = int(h_abs)
                h_floor_trade = b_min_soc
                h_ceiling_trade = 100.0
                if dynamic_floors and h_idx_int in dynamic_floors:
                    h_floor_trade = float(dynamic_floors[h_idx_int])
                if dynamic_ceilings and h_idx_int in dynamic_ceilings:
                    h_ceiling_trade = float(dynamic_ceilings[h_idx_int])

                # v11.9.618: Simplified manual limits - max power and 15% absolute floor
                if _manual_soc is not None:
                    _m_soc_f = float(_manual_soc)
                    if _h_mode_name == "buy":
                        h_ceiling_trade = min(100.0, _m_soc_f)
                    else:
                        h_floor_trade = max(15.0, _m_soc_f)

                if total_net_kw > 0.001: 
                    # v11.1.62 - bat_emergency recovery
                    acc_ratio = float(self.get_cc_cv_ratio(simulated_soc))
                    actual_charge_kw = float(min(total_net_kw * eff_coeff, max_batt_p * acc_ratio))
                    
                    old_soc = simulated_soc
                    if b_cap_f > 0.1:
                        simulated_soc = float(min(h_ceiling_trade, simulated_soc + (actual_charge_kw * step_duration / b_cap_f * 100.0)))
                    
                    # v11.7.133: Record actual AC power stored (after all losses/limits)
                    sim_p_bat = -actual_charge_kw / max(0.1, eff_coeff) # - is charge in UI view
                    
                    # v11.0.6 - Track overflow energy (AC kWh)
                    actual_stored_kwh_ac = 0.0
                    if b_cap_f > 0.1:
                        actual_stored_kwh_ac = ((simulated_soc - old_soc) / 100.0 * b_cap_f) / max(0.1, eff_coeff)
                    
                    overflow_h = max(0.0, (total_net_kw * step_duration) - actual_stored_kwh_ac)
                    overflow_kwh += overflow_h
                
                # v11.9.550: Clean logs for production stability
                
                # v11.9.548: Fix - Debug logs MUST NOT use elif as they block discharge logic
                
                if total_net_kw < -0.001 and allow_discharge: 
                    # v11.9.551: Battery-First Logic. max_batt_p is a DC limit (what leaves the battery).
                    actual_discharge_kw = float(min(abs(total_net_kw) / sim_eff, max_batt_p))
                    
                    old_soc = simulated_soc
                    if b_cap_f > 0.1:
                        # 2. Identify Sale component (Trade) vs House component
                        p_sale_ac = abs(min(0.0, cmd_p))
                        p_sale_dc = min(actual_discharge_kw, p_sale_ac / sim_eff)
                        p_house_dc = max(0.0, actual_discharge_kw - p_sale_dc)
                        
                        # 4. Sequential Discharge Simulation
                        # Phase A: House Load (Limit = Hardware b_min_soc OR manual limit)
                        # v11.9.601: If manual override is active, stop at h_floor_trade
                        _h_floor_for_house = h_floor_trade if _manual_m else b_min_soc
                        house_drop_req = (p_house_dc * step_duration / b_cap_f * 100.0)
                        house_drop_act = min(house_drop_req, max(0.0, simulated_soc - _h_floor_for_house))
                        simulated_soc = float(simulated_soc - house_drop_act)
                        
                        # Phase B: Sale/Trade (Limit = Trade Floor)
                        sale_drop_req = (p_sale_dc * step_duration / b_cap_f * 100.0)
                        sale_drop_act = min(sale_drop_req, max(0.0, simulated_soc - h_floor_trade))
                        simulated_soc = float(simulated_soc - sale_drop_act)
                        
                        sim_p_sale = (sale_drop_act / 100.0 * b_cap_f) / step_duration * sim_eff

                        # Actual total drop for logging
                        actual_drop_soc = house_drop_act + sale_drop_act
                        
                        # v11.8.431: Important - if we capped the drop, we must reflect 
                        # the REAL AC power that the battery actually provided.
                        soc_delta = old_soc - simulated_soc
                        sim_p_bat = (soc_delta / 100.0 * b_cap_f) / step_duration * sim_eff
                        
                        # v11.9.548: Enhanced Trace for Locked/Limit diagnosis
                        if abs(cmd_p) > 0.05 and sim_p_bat < abs(cmd_p) - 0.05:
                            _LOGGER.debug(f"[SimDeficit] H:{h_abs} M:{_h_mode_str} req:{abs(cmd_p):.2f} real:{sim_p_bat:.2f} net:{total_net_kw:.2f} dc:{actual_discharge_kw:.2f} soc:{old_soc:.1f}->{simulated_soc:.1f} floor:{h_floor_trade:.1f}")
                    else:
                        simulated_soc = 0.0
                        sim_p_bat = 0.0
                else:
                    sim_p_bat = 0.0
                

                # Store enriched data for the 24h forecast (v11.6.1: Unified EN keys)
                if h_abs not in history_log:
                    history_log[int(h_abs)] = {
                        "soc_start": round_f(float(_prev_soc_for_log), 1),
                        "soc_end": round_f(float(simulated_soc), 1),
                        "soc": round_f(float(simulated_soc), 1), # Legacy
                        "p_bat": round_f(float(sim_p_bat), 2),
                        "p_sale": round_f(float(sim_p_sale), 2),
                        "net_p_bat": round_f(float(sim_p_bat), 2), # Unified
                        "gen_kw": round_f(float(expected_gen_kw), 2),
                        "cons_kw": round_f(float(expected_cons_kw), 2),
                        "net_kw": round_f(float(total_net_kw), 2),
                        "mode": _h_mode_str
                    }
                    if abs(cmd_p) > 0.001:
                        history_log[int(h_abs)]["req_p"] = round_f(float(abs(cmd_p)), 3)
                        history_log[int(h_abs)]["floor"] = round_f(float(h_floor_trade), 1)

            except Exception as e:
                _LOGGER.error(f"Simulation error at hour {h_abs}: {e}")
                continue

        return float(simulated_soc), history_log, float(overflow_kwh)

    def get_market_strategy(self, mode="buy", allow_recalc=True):
        now = dt_util.now()
        man: Any = self.manager
        
        cache_key = f"market_strategy_{mode}"
        cached = self._strategy_cache.get(cache_key)
        if cached and (now - cached["time"]).total_seconds() < 30 and cached["time"].hour == now.hour:
            return cached["res"]

        if not allow_recalc:
            return {
                "state": "idle", 
                "reason": "Ожидание инициализации", 
                "active_hours": [],
                "target_soc": 0.0,
                "recommended_power_kw": 0.0,
                "arbitrage_decision": "Ожидание",
                "strategy_decision": "Ожидание"
            }

        # v11.6.532: Full initialization restoration
        _b_soc_s, _b_cap_s, _ = man.get_battery_state()
        b_cap = float(_b_cap_s or 10.0)
        b_soc = float(_b_soc_s or 50.0)
        max_p = float(man.get_setting(CONF_BATTERY_MAX_POWER, 3.0))
        
        # Restore missing settings for SELL mode (v11.6.533)
        deg_cost = float(self.get_battery_degradation_cost())
        prof_thresh = float(man.get_setting(CONF_ARBITRAGE_PROFIT_THRESHOLD, 0.5))

        res = {
            "strategy_version": VERSION,
            "state": "sale_pv",
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
            "buy_simulation": {"projected_soc_at_start_pct": b_soc, "projected_soc_at_end_pct": b_soc, "projected_soc_morning_pct": b_soc},
            "sell_simulation": {"projected_soc_at_start_pct": b_soc, "projected_soc_after_sale_pct": b_soc, "projected_soc_morning_pct": b_soc},
            "arbitrage_decision": "Нет данных",
            "charge_reason": "none",
            "strategy_candidates": [],
            "arbitrage_buyback": {"opportunity": False, "power_kw": 0.0, "note": ""}
        }
        charge_commands = {}
        sell_commands = {}
        can_recharge = False
        house_load_during_sale_dc = 0.0
        
        occ_coeff = 1.0
        eff = 0.95
        min_soc_val = float(man.get_setting(CONF_AI_DISCHARGE_LIMIT, 15.0))
        base_target = float(man.get_setting(CONF_AI_DISCHARGE_LIMIT, 20.0))
        sunrise_h = 8
        _morning_lib_surplus_dc = 0.0
        has_morning_sale = False
        soc_at_start = b_soc
        natural_soc_after_sale = b_soc
        
        # v11.9.543: Use global VERSION from const.py
        res["strategy_version"] = VERSION
        
        old_calc = bool(getattr(self, "_calculating_strategy", False))
        self._calculating_strategy = True
        _sell_debug = {}
        try:
            cur_hour = int(now.hour)
            today_str = now.strftime("%Y-%m-%d")
            tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
            
            try:
                p_st = dict(man.data.get(f"prices_{mode}", {}))
                today_prices = dict(p_st.get(today_str, {}))
                tomorrow_prices = dict(p_st.get(tomorrow_str, {}))
                
                # v11.6.528: Source-Level Cycle Isolation for BUY mode.
                # If tomorrow is > 12h away from end of today, hide it.
                if mode == "buy" and tomorrow_prices:
                    tom_h_first = min(int(h) for h in tomorrow_prices.keys())
                    if (tom_h_first + 24 - 23) > 12:
                        tomorrow_prices = {}
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
            
            # v11.6.527: Absolute Entry-Level Cycle Isolation for BUY mode.
            # If we are buying, we MUST NOT even know about tomorrow if there's a night gap.
            if mode == "buy":
                _sorted_h = sorted(all_prices.keys())
                _final_all = {}
                for h in _sorted_h:
                    if h < cur_hour: continue
                    if _final_all and (h - max(_final_all.keys()) > 12):
                        break # Night gap detected, ignore everything after
                    _final_all[h] = all_prices[h]
                all_prices = _final_all

            cur_p_f = all_prices.get(cur_hour, 0.0)
                
            negative_hours_raw = sorted([int(h) for h, p in all_prices.items() if p <= 0 and h >= cur_hour])
            negative_hours = []
            for h in negative_hours_raw:
                if negative_hours and (h - negative_hours[-1] > 12):
                    break # v11.6.529: Strict Cycle Isolation. Ignore tomorrow if there's a night gap.
                negative_hours.append(h)

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
                # v11.6.536: Sell price must be non-negative to be considered for arbitrage sell-now
                if float(p_s) < 0: continue
                
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

                # v11.6.251: Diagnostic prep (moved outside conditional blocks)
                res["deg_cost"] = float(deg_cost)
                res["profit_threshold"] = float(threshold)
                _p_now = float(normalize_float(all_prices.get(cur_hour, 0.0)))
                _f_sell = [p_s for h_s, p_s in all_sell_prices.items() if h_s > cur_hour]
                _b_sell = max(_f_sell) if _f_sell else 0.0
                _f_buy = [p_b for h_b, p_b in all_prices.items() if h_b > cur_hour]
                _b_buy = min(_f_buy) if _f_buy else _p_now
                _gain = float(_b_sell * eff - _p_now - deg_cost)
                
                is_arb_now = (_gain >= threshold)
                if negative_hours:
                    target_hours = list(negative_hours)
                    target_price = float(min([all_prices[h] for h in negative_hours]))
                    res["target_price"] = target_price
                    res["strategy_candidates"] = [f"{h%24:02d}:00" for h in negative_hours]
                    if not is_arb_now:
                        if cur_hour in negative_hours:
                            res["arbitrage_decision"] = f"Отрицательная цена ({cur_p_f:.2f})"
                        else:
                            res["arbitrage_decision"] = f"Ожидание отрицательных цен ({cur_p_f:.2f})"
                else:
                    def is_buy_profitable_arb(buy_p, hour):
                        # Find best future sell price after this buy hour
                        # v11.6.259: Bridge Rule. If negative prices ahead, the sell window MUST
                        # be BEFORE the first negative price. Otherwise, wait for the minus.
                        first_neg_h = min(negative_hours) if negative_hours else 999
                        
                        future_sell_options = {h_s: p_s for h_s, p_s in all_sell_prices.items() if h_s > hour}
                        if not future_sell_options: return False
                        
                        best_s_h = max(future_sell_options, key=lambda k: future_sell_options[k])
                        best_s = future_sell_options[best_s_h]
                        
                        if buy_p > 0.0 and best_s_h >= first_neg_h:
                             # We can buy cheaper later for this same sell window
                             return False

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
                        res["strategy_candidates"] = [f"{h%24:02d}:00" for h, p in combined]
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
                            if not sim_r: return True, "Ок (Кластер)"
                            
                            sim_s = now if start_h == cur_hour else now.replace(minute=0, second=0, microsecond=0)
                            _, log_d, _ = self.run_soc_simulation(start_soc, sim_r, sim_s, commands=None)
                            
                            max_r = start_soc
                            for st in log_d.values():
                                max_r = max(max_r, float(st.get("soc", 0)))
                                
                            max_s = 100.0 - start_soc
                            p_x = max(0.01, p_m)
                            req_rec = max_s * max(0.0, (p_x - p_c) / p_x)
                            req_soc = min(95.0, start_soc + req_rec)
                            
                            if max_r >= req_soc:
                                return True, f"Ок (Сим. {max_r:.1f}% >= Треб. {req_soc:.1f}%)"
                            return False, f"Неблагоприятно (Сим. {max_r:.1f}% < Треб. {req_soc:.1f}%)"

                        # v11.3.42: Be more inclusive in candidates for 'skipped' reporting
                        # Instead of just get_peaks, start with ALL hours above sell_limit or profitable
                        peaks_candidates_all = []
                        all_h_possible = sorted(list(set(list(today_prices.keys()) + list(tomorrow_prices.keys()))), key=lambda x: int(x))
                        
                        # v11.6.547: Early Surplus Detection for candidate validation
                        # We use a simple conservative estimate: (Current SOC - User Limit)
                        early_surplus_dc = max(0.0, (b_soc - float(man.get_setting(CONF_AI_DISCHARGE_LIMIT, 20.0))) * b_cap / 100.0)

                        # Get technical peaks for comparison
                        tech_peaks_today = [int(h) for h, p in get_peaks(today_morn, True, sell_limit) + get_peaks(today_eve, True, sell_limit)]
                        tech_peaks_tom = [int(h) + 24 for h, p in get_peaks(tom_morn, True, sell_limit) + get_peaks(tom_eve, True, sell_limit)]
                        tech_peaks_all = set(tech_peaks_today + tech_peaks_tom)

                        for h_str, p_val in today_prices.items():
                            h_int = int(h_str)
                            if h_int < cur_hour: continue
                            # v11.6.538: Filter by identified morning/evening peaks only
                            if h_int not in tech_peaks_all: continue
                            
                            norm_p = float(normalize_float(p_val))
                            ok_arb, _, _, _ = is_profitable(norm_p, h_int)
                            # v11.6.546: Include all identified tech peaks regardless of arbitrage threshold.
                            if norm_p >= sell_limit or ok_arb or (h_int in tech_peaks_all):
                                peaks_candidates_all.append((h_int, norm_p))
                                
                        for h_str, p_val in tomorrow_prices.items():
                            h_int = int(h_str) + 24
                            # v11.6.538: Filter by peaks
                            if h_int not in tech_peaks_all: continue
                            
                            norm_p = float(normalize_float(p_val))
                            ok_arb, _, _, _ = is_profitable(norm_p, h_int)
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
                                if is_tech_peak or (early_surplus_dc > 0.1):
                                    safe_peaks.append((curr_h, curr_p))
                                continue
                                
                            best_future_p = max(fp[1] for fp in future_peaks)
                            if curr_p < best_future_p:
                                best_future_h = next(fp[0] for fp in future_peaks if fp[1] == best_future_p)
                                cr, reason = _can_recharge_between(curr_h, best_future_h, curr_p, best_future_p)
                                if curr_p >= sell_limit or (early_surplus_dc > 0.1): cr = True # v11.6.547: Respect Surplus
                                if cr:
                                    if is_tech_peak or (early_surplus_dc > 0.1):
                                        safe_peaks.append((curr_h, curr_p))
                                        last_recharge_reason = reason
                                else:
                                    if is_tech_peak:
                                        short_reason = reason.replace("Неблагоприятно", "Нет усл.").replace("Благоприятно", "Ок")
                                        skipped_reasons.append(f"{curr_h%24:02d}:00 ({short_reason})")
                            else:
                                # Primary peak (no higher future peak)
                                if is_tech_peak or (early_surplus_dc > 0.1):
                                    safe_peaks.append((curr_h, curr_p))
                                    
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
                            # v11.6.536: Restore target_hours from safe_peaks
                            target_hours = sorted([int(h) for h, p in safe_peaks])
                            target_price = float(max([p for h, p in safe_peaks]))
                            
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
                        # v11.6.540: Global Peak Protection. 
                        # Don't sell now if there is a significantly better peak ahead.
                        better_peak_h = best_arb_pair[0]
                        better_peak_p = all_sell_prices.get(better_peak_h, 0.0) if better_peak_h else 0.0
                        is_better_ahead = bool(better_peak_h and better_peak_h > cur_hour and better_peak_p > cur_p_f + 0.05)
                        
                        # v11.6.553: If we have physical surplus, we DON'T wait for better peaks. 
                        # We sell NOW to make room for tomorrow's sun.
                        if early_surplus_dc > 0.1:
                            is_better_ahead = False
                        
                        if cur_p_f >= sell_limit and cur_p_f > 0 and not is_better_ahead: status = "Продажа (Лимит)"
                        elif cur_gain >= threshold and cur_p_f > 0 and not is_better_ahead: status = "Продажа (Арбитраж)"
                        
                        detail = f"Цена {cur_p_f:.2f}" if status == "Продажа (Лимит)" else f"Сейчас {cur_p_f:.2f}. {global_arb_note}"
                        if is_better_ahead:
                             detail += f" | Ждем главного пика в {self._format_h(better_peak_h)} (по {better_peak_p:.2f})"
                        
                        res["arbitrage_decision"] = f"{status}: {detail}"

            target_hours = sorted([int(h) for h in target_hours if int(h) >= cur_hour])
            
            # Apply 12h gap truncation: only plan for the immediate block of peaks
            if target_hours:
                truncated = [target_hours[0]]
                for i in range(1, len(target_hours)):
                    # v11.6.526: NUCLEAR Cycle Isolation. 
                    # If mode is BUY, we NEVER look past a 12h gap (night).
                    # We don't care if we can recharge or not - we focus ONLY on the current window.
                    _gap = target_hours[i] - target_hours[i-1]
                    if mode == "buy":
                        if _gap <= 12:
                            truncated.append(target_hours[i])
                        else:
                            break # STRICT BREAK for buy mode
                    else:
                        # For SELL mode, we keep the original 'can_recharge' logic
                        if (_gap <= 12) or can_recharge:
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
                    
                # v11.6.380: Safe merge of survival hours and negative price hours
                merged_hours = set(survival_hours)
                if negative_hours:
                    merged_hours.update([int(h) for h in negative_hours])
                target_hours = sorted(list(merged_hours))

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
                    future_sell_peaks = sorted([h for h in all_sell_prices.keys() if h > cur_hour])
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
                        soc_at_b, _, _ = self.run_soc_simulation(b_soc, sim_to_b, now, commands=None, ignore_blended=(now.hour < 10))
                        
                        # Fix (v6.16): For future hours, use Minute 0 to get FULL solar hour in simulation.
                        # This prevents "losing" solar minutes due to now.minute offset.
                        sim_start_time = now if h_b == cur_hour else now.replace(minute=0, second=0, microsecond=0)
                        
                        # 2. Prediction of MAX SOC achieved by Sun alone TODAY starting from this hour
                        sim_eod = list(range(int(h_b), 24))
                        soc_final_dry, dry_log, _ = self.run_soc_simulation(soc_at_b, sim_eod, sim_start_time, commands=None, ignore_blended=(now.hour < 10))
                        max_dry_soc = max([float(st["soc"]) for st in dry_log.values()] + [float(soc_at_b)])
                        
                        # v11.6.258: Patience Mode. If negative prices ahead, set threshold to survival floor
                        # for all positive-price hours. No point buying at 0.8 if -0.9 is coming.
                        # v11.6.375: If price is negative, we ALWAYS prefer buying over solar (get paid).
                        # Set threshold to 101% so it's never skipped by max_dry_soc.
                        p_buy_h = float(all_buy_prices.get(int(h_b), 999.0))
                        # v11.6.484: Greedy Mode only for Zero/Negative prices or Survival.
                        # For positive cheap prices, respect solar reservation (90% threshold).
                        if p_buy_h <= 0.0 or available_today_kwh < survival_target_kwh:
                             _solar_threshold = 101.0
                        elif negative_hours and p_buy_h > 0.0:
                             _solar_threshold = float(min_soc + soc_buffer)
                        else:
                             _solar_threshold = 90.0
                        
                        if max_dry_soc < _solar_threshold:
                            pool_useful.append(h_b)
                    
                    pool = pool_useful
                    if negative_hours:
                        res["charge_reason"] = "negative"
                        target_soc = 100.0
                    elif is_strict_arb:
                        res["charge_reason"] = "arbitrage"
                        # v11.6.455: Greedy Arbitrage. Do NOT reserve space for the sun.
                        # Fill the battery 100% if it's profitable to do so.
                        target_soc = 100.0
                    elif available_today_kwh < survival_target_kwh:
                        res["charge_reason"] = "survival"
                        # v11.6.479: Greedy Survival. Use base_target (100.0) instead of min-requirement.
                        target_soc = base_target
                        res["arbitrage_decision"] = f"Зарядка для обеспечения буфера ({survival_target_kwh / b_cap * 100.0:.1f}%)"
                    else:
                        res["charge_reason"] = "cheap"
                        # v11.6.465: Greedy Buy. If we have cheap hours in pool, target base_target (100.0)
                        target_soc = base_target

                    # Final Override (v6.16): If no useful hours left, sun is sufficient,
                    # OR current SOC is already high enough (prevents micro-buys for 1% arbitrage)
                    # v11.1.30: Bypass these checks if price is negative (always useful to fill the battery)
                    should_skip_buy = (not pool or target_soc <= (b_soc + 0.5)) and (cur_hour not in negative_hours)
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
                            soc_at_neg, _, _ = self.run_soc_simulation(b_soc, sim_range_neg, now, no_battery_charge=True, ignore_blended=(now.hour < 10))
                            
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
                                    _, _chk_log, _ = self.run_soc_simulation(b_soc, _chk_range, now, {}, ignore_blended=(now.hour < 10))
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
                            # v11.6.265: Cross-Strategy Awareness. 
                            # Retrieve planned sell commands from the cache to accurately predict 
                            # SOC at the start of the buy window.
                            _sell_results = self._strategy_cache.get("market_strategy_sell", {}).get("res", {})
                            _planned_commands = _sell_results.get("raw_commands", {})
                            
                            soc_at_start_plan, _, _ = self.run_soc_simulation(
                                b_soc, sim_range_pre, now,
                                commands=_planned_commands,
                                no_battery_charge_until=_combined_block,
                                no_solar=is_neg_strategy,
                                allow_discharge=False # v11.6.412: In BUY mode, grid covers load
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
                            # v11.6.264: For negative prices, we want to maximize grid intake.
                            # We assume a 0% solar contribution during these hours to ensure 
                            # we fill the battery as much as possible while being paid for it.
                            energy_to_buy = theoretical_gap_kwh
                        else:
                            energy_to_buy = theoretical_gap_kwh + pool_cons

                        # 2. Sort available hours by price (cheapest first)
                        pool_sorted = sorted(pool, key=lambda h: all_buy_prices[h])

                        # 3. v11.1.22: Differentiated allocation
                        # v11.6.440: Recursive Buy Correction Loop
                        # We track how much DC energy we need to reach the target
                        current_target_dc = (target_soc - soc_at_start_plan) / 100.0 * b_cap
                        
                        for recursive_iter in range(3):
                            charge_commands = {}
                            added_kwh_dc = 0.0
                            
                            # Sort available hours by price (cheapest first)
                            # v11.6.525: Price sorting restored as requested.
                            # Combined with strict truncation (night-aware), this is safe.
                            pool_sorted_neg = sorted(pool, key=lambda hr: float(all_buy_prices.get(int(hr), 999.0)))
                            
                            for h in pool_sorted_neg:
                                price_h = float(all_buy_prices.get(int(h), 999.0))
                                # v11.6.525: Negative Price Priority.
                                # If price is <= 0, we NEVER break. We fill all negative hours.
                                if added_kwh_dc >= current_target_dc - 0.01 and price_h > 0: break
                                
                                h_factor = max(0.1, (60 - now.minute)/60.0) if h == cur_hour else 1.0
                                
                                # Estimate SOC for CC/CV
                                est_soc = soc_at_start_plan + (added_kwh_dc / b_cap * 100.0)
                                cc_cv_f = float(self.get_cc_cv_ratio(est_soc))
                                
                                # v11.6.442: Always use fresh dynamic efficiency
                                _cur_eff = float(self.get_efficiency_coefficient() or 0.95)
                                
                                rem_dc = current_target_dc - added_kwh_dc
                                # v11.6.566: Even for negative prices, respect the physical capacity limit (rem_dc)
                                # Target as much as we can fit, but no more.
                                p_needed_grid = (rem_dc / (_cur_eff * h_factor)) if rem_dc > 0 else 0.0
                                
                                p_greedy_grid = min(max_p, p_needed_grid)
                                p_cc_cv_grid = max_p * cc_cv_f
                                p_greedy_grid = min(p_greedy_grid, p_cc_cv_grid)
                                
                                if p_greedy_grid > 0.01:
                                    charge_commands[int(h)] = round_f(p_greedy_grid, 3)
                                    added_kwh_dc += (p_greedy_grid * h_factor * _cur_eff)
                            
                            # Verification: If we have reached 100% in our internal math, we are good.
                            # But if the CC/CV limit significantly reduced our intake, we might need more hours.
                            if added_kwh_dc >= current_target_dc - 0.05:
                                break
                            else:
                                # Increase the "virtual" target to force more allocation if needed
                                # (But don't exceed physical limits)
                                current_target_dc += (current_target_dc - added_kwh_dc)
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
                        # v11.6.214: Expand simulation horizon to 48 hours for 48h-aware logic
                        sim_end_h = cur_hour + 48
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
                        # v11.6.500: Universal simulation engine now uses POSITIVE cmd_p for charge.
                        # No need for inversion.
                        _, sim_log, _ = self.run_soc_simulation(
                            b_soc, sim_range, now, charge_commands,
                            no_battery_charge_until=_buy_sim_no_charge_until,
                            pv_curtail_hours=_neg_buy_curtail or None,
                            ignore_blended=(now.hour < 10),
                            no_solar=is_neg_strategy,
                            allow_discharge=False # v11.6.505: In BUY mode, grid covers load
                        )
                        # v11.6.412: In BUY mode, grid covers load
                        
                        # 1. Projected SOC at START of the first buy hour
                        # v11.6.264: Use the baseline calculated for the planning gap
                        soc_at_start = soc_at_start_plan if target_hours_sorted else b_soc

                        # 2. Projected SOC AFTER the first continuous buy window
                        if True: # v11.3.97: Always run simulation for telemetry
                            future_active_buy = [h for h in target_hours_sorted if h >= cur_hour]
                            if future_active_buy:
                                # v11.6.410: Use the ABSOLUTE last hour of the buy plan for the summary
                                last_h_buy_immediate = max(future_active_buy)
                                
                                # v11.6.392: Use explicit string key for lookup
                                key_end = f"{last_h_buy_immediate % 24:02d}:59" + (" (Завтра)" if last_h_buy_immediate >= 24 else "")
                                soc_at_end = self._get_soc_from_log(sim_log, key_end, b_soc)
                            else:
                                soc_at_end = b_soc
                        else:
                            soc_at_end = b_soc
                            
                        # 3. Projected SOC TOMORROW MORNING (Dawn Anchor v11.6.270)
                        _h_sunrise_target = sunrise_h - 1
                        if cur_hour >= _h_sunrise_target:
                            _h_sunrise_target += 24
                        
                        # Forced Night Sub-Simulation for BUY mode morning projection
                        # to ensure the "Morning" state shows the natural battery level before solar starts.
                        _night_sim_range = list(range(cur_hour, _h_sunrise_target + 1))
                        if _night_sim_range:
                             # Sim starting from soc_at_end (after buy window) or current b_soc?
                             # v11.6.270: Start from sim_log result at key_end (post-buy state)
                             # Wait, soc_at_end is already the SOC at the end of the buy window.
                             # We should simulate from there to sunrise.
                             _, _night_log, _ = self.run_soc_simulation(soc_at_end, _night_sim_range, now, no_solar=True)
                             key_morning = f"{_h_sunrise_target % 24:02d}:59" + (" (Завтра)" if _h_sunrise_target >= 24 else "")
                             soc_morning = self._get_soc_from_log(_night_log, key_morning, soc_at_end)
                        else:
                             soc_morning = soc_at_end

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
                        
                        # v11.6.509: Detailed diagnostics for UI (as per Section 4.2.5 of TZ)
                        _neg_tag = "[Отрицательная цена]" if cur_hour in negative_hours else ""
                        if not _neg_tag and negative_hours:
                            _neg_tag = "Ожидание отрицательных цен"
                        
                        res["buy_debug"] = (
                            f"{_neg_tag} | Цена: {_p_now:.2f} | "
                            f"Цель: {target_soc:.1f}% (Лимит: {base_target:.1f}%) | "
                            f"Лучшая продажа позже: {_b_sell:.2f} | "
                            f"Лучшая покупка позже: {_b_buy:.2f} | "
                            f"Выгода арб: {_gain:.2f} (Порог: {threshold:.2f})"
                        ).strip(" | ")
                    except Exception as e:
                        _LOGGER.error("Error in MarketStrategy BUY simulation: %s", e)
                        res["buy_simulation"] = {
                            "projected_soc_at_start_pct": float(b_soc),
                            "projected_soc_at_end_pct": float(b_soc),
                            "projected_soc_morning_pct": float(b_soc),
                            "error": str(e)
                        }
                else: # sell
                    _prof_debug = ""
                    soc_at_start = float(b_soc)
                    # v11.6.355: Comprehensive Sell Debug
                    pass # v11.6.544: Debug block moved below to prevent UnboundLocalError
                    # Sell mode (v11.1.51)
                    # Use existing Target SOC Sell as floor for AI selling
                    # v11.6.301: Restore missing variables after refactor
                    min_soc_bat_val = float(man.get_setting(CONF_MIN_SOC_BAT, 10.0))
                    
                    # v11.6.300: Global Night Reserve Injection
                    # Calculate this BEFORE any strategy decisions to ensure Target SOC is consistent.
                    # v11.6.366: Get forecast early for base_target decisions
                    f_tom_raw = man.get_forecast_value(man.forecast_tomorrow_sensor)
                    f_tom = float(f_tom_raw) if f_tom_raw is not None else 0.0
                    solar_is_plentiful = bool(f_tom > (b_cap * 0.8) or f_tom > 20.0)

                    occ_coeff, _, _, _, _, _, _ = man.get_occupancy_coefficient()
                    occ_coeff = float(occ_coeff)
                    avg_prof_cons = man.get_average_profile("consumption_base", man.custom_period, "all")
                    
                    min_soc_bat_val = float(man.get_setting(CONF_MIN_SOC_BAT, 10.0))
                    soc_buffer = float(man.get_setting(CONF_SOC_BUFFER, 8.0))
                    house_safety = min_soc_bat_val + soc_buffer
                    
                    # 1. Survival Target SOC (Sunrise Guard)
                    # v11.9.449: Unified Gatekeeper calculation via function (handles Turbo/Safe modes)
                    _sr_h_abs = sunrise_h + (24 if cur_hour >= sunrise_h else 0)
                    _m_survival_target = self.get_gatekeeper_floor(cur_hour, _sr_h_abs)
                    
                    # 2. User Discharge Limit (Static user setting)
                    user_limit = float(man.get_setting(CONF_AI_DISCHARGE_LIMIT, 13.0))
                    
                    # v11.6.547: Robust profile retrieval (try both str and int keys)
                    def get_prof_val(p, h):
                        return float(normalize_float(p.get(str(h), p.get(int(h), 0.0))))
                    
                    _h_end_est = 21 
                    _h_sunrise_target = sunrise_h - 1
                    _n_range = range(_h_end_est, _h_sunrise_target + 24 if _h_sunrise_target < _h_end_est else _h_sunrise_target)
                    night_cons_kwh = sum(get_prof_val(avg_prof_cons, h % 24) for h in _n_range) * occ_coeff
                    
                    # v11.6.547: Fallback to at least 4.0 kWh for night if profile is zero (Safety)
                    if night_cons_kwh < 0.1:
                        night_cons_kwh = 3.0
                        
                    night_cons_pct = (night_cons_kwh * 100.0 / b_cap) if b_cap > 1.0 else 0.0
                    
                    # v11.6.547: Profile Diagnostic String
                    _prof_debug = f"Keys:{len(avg_prof_cons or {})} Sum:{sum(float(normalize_float(v)) for v in (avg_prof_cons or {}).values()):.1f} Occ:{occ_coeff:.2f} SurpDC:{early_surplus_dc:.1f} Targets:{target_hours_sorted}"
                    
                    # v11.7.390: Relax survival floor during solar surplus
                    if solar_is_plentiful:
                        base_target = user_limit
                    else:
                        base_target = max(user_limit, _survival_floor)
                    
                    user_discharge_limit = base_target 
                    min_soc_val = user_limit
                    
                    # Initial defaults for robustness
                    arb_gain = 0.0
                    cheap_h_back = None
                    best_buy_h = None
                    cheap_p_back = 0.0
                    cur_p_f = float(normalize_float(today_prices.get(str(cur_hour), 0.0)))
                    
                    budget_data_sell = {}
                    eff_coeff_val = 1.0
                    if man.get_setting(CONF_DYNAMIC_SOC_SELL, True):
                        budget_data_raw = self.get_budget_and_permissions(man.custom_period, skip_strategy_check=True)
                        if budget_data_raw:
                            budget_data_sell = budget_data_raw
                            eff_coeff_val = float(normalize_float(budget_data_sell.get("efficiency_coefficient", 1.0)))
                        
                    # Correct reserve for House needs: Now -> Midnight -> Sunrise Tomorrow
                    rem_cons_today = float(normalize_float(budget_data_sell.get("expected_consumption", 0.0)))
                    cons_night_morning = sum(float(normalize_float(avg_prof_cons.get(str(h), 0.0))) for h in range(0, sunrise_h)) * occ_coeff
                    
                    # Also include tomorrow morning solar until sunrise in the budget
                    gen_night_morning = sum(float(normalize_float(avg_prof_gen.get(str(h), 0.0))) for h in range(0, sunrise_h))
                    # f_tom already calculated above
                    total_hist_gen_val = sum(float(normalize_float(avg_prof_gen.get(str(h), 0.0))) for h in range(24))
                    morning_solar_ac = f_tom * (gen_night_morning / total_hist_gen_val) if total_hist_gen_val > 0.1 else 0.0
                    
                    eff = eff_coeff_val if eff_coeff_val > 0.1 else 0.95
                    
                    # Window logic: 04:00 - 12:00 (Morning Autopilot) uses liberal limit (user + 2.0)
                    # Other hours (12:00 - 04:00) use strict limit (base_target)
                    if 4 <= (cur_hour % 24) < 12:
                        min_soc_val = user_limit + 2.0
                    else:
                        min_soc_val = base_target
                    
                    # v11.6.572: Strict TZ Compliance (Section 6.1 Sunrise Guard)
                    # Use the user's buffer setting (e.g. 5%) without adaptive overrides.
                    soc_buffer_val = float(man.get_setting(CONF_SOC_BUFFER, 8.0))
                    soc_buffer_full = soc_buffer_val
                    
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
                         soc_buffer_val = 2.0 # TZ Rule: Morning relaxation = min_soc_bat + 2.0%
                    
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
                                # Morning is better or equal -> Sell down to survival floor
                                # v11.7.390: No buffer needed if solar is plentiful
                                if solar_is_plentiful:
                                    base_target = min_soc_val
                                    soc_buffer_val = 0.0
                                    active_buffer = 0.0
                                else:
                                    base_target = min_soc_val + 2.0
                                    soc_buffer_val = 2.0
                                    active_buffer = 2.0
                                _LOGGER.debug(
                                    f"[Sell v11.7.390] Morning >= Evening ({cur_p_s:.2f} >= {evening_max_p:.2f}): "
                                    f"Survival mode active, base_target={base_target:.1f}%"
                                )
                    
                    # v11.6.169: Priority Correction (min(M, U, P))
                    # v11.7.395: Strict TZ Compliance (Section 6.1)
                    # 1. Morning Protection (Sunrise Guard)
                    # Target at sunrise: 15% (Liberal) or 18% (Strict)
                    morning_target_base = (min_soc_bat_val + 2.0) if solar_is_plentiful else (min_soc_bat_val + soc_buffer_full)
                    
                    # Current target including consumption bridge
                    target_morning_soc = max(min_soc_val, morning_target_base)
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
                        # v11.6.214: Expand simulation horizon to 48 hours for 48h-aware logic
                        sim_end_h = cur_hour + 48
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
                    # v11.4.31: In morning solar window, we make the Gatekeeper \"blind\" to tomorrow's deficit.
                    # This allows the simulation (Step 2) to be the primary decision maker.
                    work_cons_to_sunrise = 0.0 if is_morning_solar_v2 else total_cons_to_sunrise
                    work_deficit_tomorrow = 0.0 if is_morning_solar_v2 else base_deficit_tomorrow
                    
                    ai_soc_floor_base = self._calc_immediate_safety_floor(
                        min_soc_val, active_buffer, work_cons_to_sunrise, 
                        work_deficit_tomorrow, total_solar_to_sunrise, b_cap, eff
                    )
                    
                    # 1. Projected SOC at START of the first peak
                    # v11.6.547: If sale starts within 2 hours, anchor to current SOC to avoid simulation noise
                    soc_at_start = b_soc
                    first_h_sell = min(t for t in target_hours_sorted if t >= cur_hour) if target_hours_sorted else None
                    if first_h_sell is not None:
                        if first_h_sell <= cur_hour + 1:
                            soc_at_start = b_soc
                        else:
                            prev_h = first_h_sell - 1
                            key_start = f"{prev_h % 24:02d}:59" + (" (Завтра)" if prev_h >= 24 else "")
                            soc_at_start = self._get_soc_from_log(sim_log_base, key_start, b_soc) or b_soc
                    else:
                        soc_at_start = b_soc
                    
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
                                
                                # v11.7.396: Dynamic Solar Awareness (80% of battery capacity)
                                solar_is_plentiful = bool(f_tom > (b_cap * 0.8) or f_tom > 20.0)
                                if max_recharge_soc < 99.0 and avg_p2 > (avg_p1 + 0.05) and not solar_is_plentiful:
                                    deficit_pct = 100.0 - max_recharge_soc
                                    base_target = min(100.0, base_target + deficit_pct)
                                    
                            # v11.6.229: natural_soc_after_sale must be at the END of the peak epoch, not global min
                            _h_last_peak = max(future_active_sell_base) if future_active_sell_base else cur_hour
                            _k_end_peak = f"{_h_last_peak % 24:02d}:59" + (" (Завтра)" if _h_last_peak >= 24 else "")
                            natural_soc_after_sale = self._get_soc_from_log(sim_log_base, _k_end_peak, b_soc)
                        
                        # v11.6.208: Calculate expected house load during the sale window (in kWh)
                        house_load_during_sale_dc = max(0.0, (soc_at_start - natural_soc_after_sale) * b_cap / 100.0)
                        
                        # v11.3.60: Morning Survival Feedback Loop (The \"Autopilot\" Floor)
                        # We calculate the exact SOC floor needed to guarantee the morning target.
                        # Energy drain between end of sale and sunrise (in SOC %)
                        night_drain_pct = max(0.0, natural_soc_after_sale - natural_morning_soc)
                        
                        # v11.7.396: Strict TZ Compliance (Section 6.1)
                        # Liberal (Sunny/Saturated): min_soc + 2.0%
                        # Strict (Cloudy/Night): min_soc + soc_buffer
                        _m_emergency_base = min_soc_bat_val + (2.0 if solar_is_plentiful else soc_buffer_full)
                        
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


                    # v11.6.214: Strictly align with TS Section 6.1 (Sunrise Guard)
                    # M (Morning Survival): Calculated for sunrise point (e.g. 6:00 AM)
                    # U (User Limit): Calculated for the end of current simulated hour
                    
                    # 1. Calculate M (Morning Survival) DC Budget
                    # TS 6.1: In the morning window (planned for sunrise), the limit is the Survival Floor
                    # v11.6.233: Strictly follow TS: Morning Window (04:00-12:00) uses liberal floor (User+2%)
                    # Otherwise use base_target (Reserve + Buffer).
                    _m_floor = base_target
                    if 4 <= (cur_hour % 24) < 12:
                        _m_floor = min_soc_val + 2.0
                    
                    # v11.6.270: Standardize Dawn Anchor to sunrise_h - 1
                    _h_sunrise_target = sunrise_h - 1
                    if cur_hour >= _h_sunrise_target:
                        _h_sunrise_target += 24
                                           # v11.6.301: Dynamic Evening Floor (Night-Aware Budgeting)
                    # To have 18% in the morning, we MUST stop selling when we reach 18% + Night_Load.
                    # Otherwise, the house will drain the battery to 0% by dawn.
                    _h_end_sale = max(target_hours_sorted) if target_hours_sorted else cur_hour
                    # v11.6.545: Robust hour walking to handle midnight rollover for night consumption
                    _h_walk = _h_end_sale
                    _night_sum = 0.0
                    while _h_walk < _h_sunrise_target:
                        _night_sum += get_prof_val(avg_prof_cons, _h_walk % 24)
                        _h_walk += 1
                    
                    # v11.6.547: Do NOT overwrite the robustly calculated night_cons_kwh from Step 1
                    # unless this specific calculation yields a HIGHER (safer) value.
                    _temp_night_kwh = _night_sum * occ_coeff
                    night_cons_kwh = max(night_cons_kwh, _temp_night_kwh)
                    night_cons_pct = (night_cons_kwh * 100.0 / b_cap) if b_cap > 1.0 else 0.0
                    
                    # v11.6.355: If tomorrow is sunny, don't reserve user_limit for the morning; 
                    # only reserve the emergency survival floor (18%) + night load.
                    _night_survival_base = (min_soc_bat_val + soc_buffer_full) if solar_is_plentiful else (min_soc_val + soc_buffer_full)
                    
                    # New base target for the evening sale window
                    base_target = max(base_target, _night_survival_base + night_cons_pct)
                    target_soc_sell = float(round_f(base_target, 1))
                    
                    # Final safety check: available energy must be positive
                    
                    key_sunrise = f"{_h_sunrise_target % 24:02d}:59" + (" (Завтра)" if _h_sunrise_target >= 24 else "")
                    
                    # v11.6.285: Forced Night Sub-Simulation for Sunrise SOC
                    # v11.6.285: Start simulation from the PROJECTED SOC at sale start (soc_at_start)
                    # at the time of sale start (first_h_sell), to account for upcoming charging.
                    _sim_start_h = first_h_sell if first_h_sell is not None else cur_hour
                    # v11.6.364: Allow early morning solar in budget if plentiful
                    _night_sim_no_solar = not solar_is_plentiful
                    # v11.6.364: Fix midnight rollover in night range
                    _night_sim_range = []
                    _h_walk = _sim_start_h
                    while _h_walk <= _h_sunrise_target:
                        _night_sim_range.append(_h_walk)
                        _h_walk += 1
                    if _night_sim_range:
                         _, _night_log, _ = self.run_soc_simulation(soc_at_start, _night_sim_range, now, no_solar=_night_sim_no_solar)
                         natural_soc_at_sunrise = self._get_soc_from_log(_night_log, key_sunrise, soc_at_start)
                    else:
                         natural_soc_at_sunrise = soc_at_start

                    surplus_for_morning = max(0.0, (natural_soc_at_sunrise - _m_floor) * b_cap / 100.0)
                    
                    # 2. Calculate U (User Limit) DC Budget
                    # v11.6.234: Strictly follow TS Section 6.1.1.2: Limits NEVER sum with house consumption.
                    _u_floor = base_target
                    if 4 <= (cur_hour % 24) < 12:
                         _u_floor = min_soc_val + 2.0
                         
                    _k_end_hour = f"{cur_hour % 24:02d}:59"
                    _natural_soc_now = self._get_soc_from_log(sim_log_base, _k_end_hour, b_soc)
                    # v11.6.229: Use soc_at_start to allow planning evening peak even if currently low (due to solar)
                    surplus_for_user_limit = max(0.0, (max(_natural_soc_now, soc_at_start) - _u_floor) * b_cap / 100.0)
                    
                    # Choose most restrictive budget
                    available_sell_dc = min(surplus_for_morning, surplus_for_user_limit, physical_limit_dc)
                    available_sell_dc = max(0.0, available_sell_dc)
                    
                    res["version"] = VERSION
                    max_batt_p = max_p
                    _morning_lib_surplus_dc = max(0.0, surplus_for_user_limit - surplus_for_morning)

                    # Update base_target for diagnostics
                    if available_sell_dc <= (surplus_for_user_limit + 0.001) and surplus_for_user_limit < (surplus_for_morning - 0.1):
                         base_target = _u_floor
                    else:
                         base_target = _m_floor

                    sell_diagnosis = "Рассчитано (Ок)"
                    if available_sell_dc <= (physical_limit_dc + 0.001) and physical_limit_dc < (min(surplus_for_morning, surplus_for_user_limit) - 0.1):
                        sell_diagnosis = f"Лимит мощности АКБ ({work_max_p:.1f}кВт)"
                    else:
                        # v11.6.367: Bottleneck reporting according to final approved logic
                        if user_limit >= _survival_floor - 0.5:
                            sell_diagnosis = f"Лимит пользователя ({user_limit:.0f}%)"
                        else:
                            sell_diagnosis = f"Защита дома (Цель {_m_survival_target:.0f}% к утру)"

                    # v11.6.167 / v11.6.169: Clean human-readable status construction
                    res["arbitrage_sell_limit_reason"] = f"{sell_diagnosis}"
                    # Detailed debug is moved to internal attributes
                    _sim_extract = "|".join([f"{h % 24}: {self._get_soc_from_log(sim_log_base, f'{h % 24:02d}:59', 0.0):.0f}%" for h in range(cur_hour, cur_hour + 12)])
                    res["_debug_limit_info"] = f"M:{surplus_for_morning:.1f} U:{surplus_for_user_limit:.1f} P:{physical_limit_dc:.1f} S:{soc_at_start:.1f}% | Sim:{_sim_extract}"
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
                        
                        # v11.6.565: Diagnostic update moved to the end of the allocation block
                        
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
                    
                    # v11.6.561: Raw Epochs Debug
                    _sell_debug["raw_epochs"] = str(epochs)
                    
                    sell_commands = {int(h): 0.0 for h in sell_pool}
                    rem_kwh_sell = available_sell_ac
                    
                    for i, epoch in enumerate(epochs):
                        # v11.6.565: Mandatory 5-Hour Peak Centering
                        if len(epoch) > 5:
                            p_hour = max(epoch, key=lambda hr: all_sell_prices.get(hr, 0.0))
                            p_idx = epoch.index(p_hour)
                            # Center 2 hours before and 2 hours after
                            s_idx = max(0, p_idx - 2)
                            e_idx = min(len(epoch), s_idx + 5)
                            if e_idx - s_idx < 5: s_idx = max(0, e_idx - 5)
                            epoch = epoch[s_idx:e_idx]
                            epochs[i] = epoch # v11.6.566: Force global visibility of the 5h window
                        
                        # Sort by price, but force current hour to top if battery is full
                        epoch_sorted = sorted(epoch, key=lambda hr: (999.0 if (hr == cur_hour and b_soc > 95.0) else all_sell_prices.get(hr, 0.0)), reverse=True)
                        
                        if i > 0:
                            sim_hours = list(range(max(epochs[i-1]) + 1, min(epoch)))
                            _, throttle_log, _ = self.run_soc_simulation(base_target, sim_hours, now, {}, ignore_blended=True)
                            max_recharge_soc = max([float(x.get("soc", base_target)) for x in throttle_log.values()] + [base_target])
                            rem_base_ac = float(max(0.0, (max_recharge_soc - base_target) * b_cap / 100.0) * eff)
                            rem_bonus_ac = float(max(0.0, _morning_lib_surplus_dc * eff))
                        else:
                            rem_base_ac = float(max(0.0, (soc_at_start - base_target) * b_cap / 100.0 * eff))
                            capped_bonus_soc = max(0.0, min(soc_at_start, base_target) - (min_soc_val + 2.0))
                            _actual_bonus_dc = (capped_bonus_soc * b_cap / 100.0) if has_morning_sale else 0.0
                            rem_bonus_ac = float(min(_morning_lib_surplus_dc, _actual_bonus_dc) * eff)
                            
                        _alloc_trace = []
                        # v11.6.568: Fair-Greedy Allocation (Price-Balanced)
                        # Group hours into price buckets (0.05 hysteresis)
                        buckets = []
                        if epoch_sorted:
                            current_bucket = [epoch_sorted[0]]
                            for h_next in epoch_sorted[1:]:
                                p_cur = all_sell_prices.get(current_bucket[0], 0.0)
                                p_next = all_sell_prices.get(h_next, 0.0)
                                if abs(p_cur - p_next) <= 0.05:
                                    current_bucket.append(h_next)
                                else:
                                    buckets.append(current_bucket)
                                    current_bucket = [h_next]
                            buckets.append(current_bucket)

                        for bucket in buckets:
                            # Distribute rem_base_ac among hours in the bucket evenly
                            bucket_rem_base = rem_base_ac
                            bucket_rem_bonus = rem_bonus_ac
                            
                            # We distribute rem_base_ac + rem_bonus_ac among hours
                            for h in bucket:
                                h_f_loc = max(0.1, (60 - now.minute) / 60.0) if h == cur_hour else 1.0
                                h_floor = base_target
                                if sunrise_h <= (h % 24) <= 12:
                                    h_floor = min_soc_val + 2.0
                                
                                # Re-calculate p_alloc for this specific hour
                                p_alloc = max_p
                                if h == cur_hour:
                                    current_surplus_dc = max(0.0, (b_soc - h_floor) * b_cap / 100.0)
                                    p_alloc = min(max_p, (current_surplus_dc * eff) / h_f_loc)
                                    if b_soc > 95.0: p_alloc = max_p
                                else:
                                    _prev_h = h - 1
                                    _prev_h_key = f"{(_prev_h)%24:02d}:59" + (" (Завтра)" if _prev_h >= 24 else "")
                                    if h_soc_s := self._get_soc_from_log(sim_log_base, _prev_h_key, b_soc):
                                         _eff_soc = max(h_soc_s, (soc_at_start - 5.0) if i == 0 else 0.0)
                                         surplus_h_dc = max(0.0, (_eff_soc - h_floor) * b_cap / 100.0)
                                         p_alloc = min(max_p, (surplus_h_dc * eff) / h_f_loc)

                                # Allocate evenly from the bucket's share
                                # Fair share = (Total Pool Energy) / (Hours in Pool)
                                pool_energy = (bucket_rem_base + bucket_rem_bonus)
                                fair_power = (pool_energy / len(bucket)) / h_f_loc
                                
                                actual_power = min(p_alloc, fair_power)
                                if actual_power > 0.01:
                                    sell_commands[int(h)] = round_f(actual_power, 3)
                                    # Update global remaining
                                    rem_base_ac = max(0.0, rem_base_ac - (actual_power * h_f_loc))
                                    # Update bucket local remaining to ensure we don't over-allocate if p_alloc is small
                                    # (Not strictly needed if we just update rem_base_ac, but cleaner)
                        
                        if i == 0:
                            res["sell_alloc_debug"] = " | ".join(_alloc_trace)
                    
                    # v11.6.565: Final Diagnostic Update (Moved here for accuracy)
                    target_hours_sorted = sorted(list(set([h for h, p in sell_commands.items() if p > 0.05] + (epochs[0] if epochs else []))))
                    _sell_debug.update({
                        "server_time": now.strftime("%H:%M:%S"),
                        "cur_hour": int(now.hour),
                        "b_soc": round_f(b_soc, 1),
                        "soc_at_start": round_f(soc_at_start if 'soc_at_start' in locals() else 0.0, 1),
                        "base_target": round_f(base_target if 'base_target' in locals() else 0.0, 1),
                        "night_cons": round_f(night_cons_kwh if 'night_cons_kwh' in locals() else 0.0, 2),
                        "available_ac": round_f(available_sell_ac, 2),
                        "sim_log": "|".join([f"{h % 24}: {self._get_soc_from_log(sim_log_base, h, 0.0):.0f}%" for h in range(cur_hour, cur_hour + 12)]),
                        "final_targets": str(target_hours_sorted),
                        "midnight_trace": "|".join(getattr(man, "midnight_trace", [])[-4:]),
                        "f_today": round_f(float(man.get_forecast_value(man.forecast_today_sensor) or 0.0), 1),
                        "f_tom": round_f(f_tom if 'f_tom' in locals() else 0.0, 1)
                    })
                    
                    power_needed = sell_commands.get(int(cur_hour), 0.0)

                    
                    if man.get_setting(CONF_DYNAMIC_SOC_SELL, True):
                        if target_soc < base_target:
                            target_soc = base_target
                            res["note"] = f"Цель ограничена пользователем (Target SOC Sell: {base_target}%)"
                        target_soc = float(target_soc)
                    else:
                        target_soc = base_target

                    # v11.6.541: Redundant reporting block removed to prevent overwriting correctly calculated status.
                    if not man.get_setting(CONF_DYNAMIC_SOC_SELL, True):
                        res["arbitrage_decision"] = "Ручной режим (AI выкл.)"
                        
                    target_soc = float(min(100.0, target_soc))
                    delta_available_dc = available_sell_ac / eff

                    # --- SELL SIMULATION ---
                    # v11.6.214: Expand simulation horizon to 48 hours for 48h-aware logic
                    sim_end_h = cur_hour + 48
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

                    # v11.6.500: SELL commands must be NEGATIVE for discharge in simulation.
                    sim_commands_neg = {k: -v for k, v in sim_commands.items()}
                    _, sim_log, _ = self.run_soc_simulation(
                        b_soc, sim_range, now, sim_commands_neg, 
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
                    
                    _rem_start_soc = b_soc
                    _pass_log = "Pass0"
                    for pass_idx in range(3):
                        morning_gap = _m_recursive_target - soc_morning
                        # Only raise the floor if we are actually dropping below the EMERGENCY level.
                        # If we are just dropping below the user's 70% (but stay at 60%), we don't care.
                        if morning_gap <= 0.1:
                            break
                            
                        # 1. Update the base target floor
                        base_target = min(100.0, max(min_soc_val, base_target + morning_gap))
                        
                        # v11.6.325: House-Blind Budgeting (Step 2)
                        rem_base_dc_fix = float(max(0.0, (_rem_start_soc - base_target) * b_cap / 100.0))
                        
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
                                    house_rem_dc_fix = 0.0
                                    current_surplus_dc_fix = max(0.0, (b_soc - h_floor_fix) * b_cap / 100.0)
                                    p_alloc_fix = min(max_p, (current_surplus_dc_fix * eff) / h_f)
                                else:
                                    _prev_h_f = h - 1
                                    _prev_h_key_f = f"{(_prev_h_f)%24:02d}:59" + (" (Завтра)" if _prev_h_f >= 24 else "")
                                    if h_soc_sf := self._get_soc_from_log(sim_log_base, _prev_h_key_f, b_soc):
                                         surplus_hf_dc = max(0.0, (h_soc_sf - h_floor_fix) * b_cap / 100.0)
                                         p_alloc_fix = min(max_p, (surplus_hf_dc * eff) / h_f)
                                
                                # v11.6.330: Greedy Price Priority (Profit Max)
                                actual_p_fix = min(p_alloc_fix, (rem_base_ac_fix + rem_bonus_ac_fix) / h_f)
                                
                                if actual_p_fix > 0.01:
                                    sell_commands[int(h)] = round_f(actual_p_fix, 3)
                                    rem_base_ac_fix = max(0.0, rem_base_ac_fix - (actual_p_fix * h_f))
                                else:
                                    sell_commands[int(h)] = 0.0
                        
                        # 4. Re-run final simulation to verify the fix for next iteration
                        sim_commands_fix = {int(h): cmd for h, cmd in sell_commands.items()}
                        if best_buy_h is not None and best_buy_h < sim_end_h:
                            sim_commands_fix[int(best_buy_h)] = float(max_p)
                            
                        # v11.6.500: SELL commands must be NEGATIVE for discharge.
                        sim_commands_fix_neg = {k: -v for k, v in sim_commands_fix.items()}
                        _, sim_log, _ = self.run_soc_simulation(
                            b_soc, sim_range, now, sim_commands_fix_neg, 
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
                        
                        limit_label = f"Лимит пользователя ({user_limit:.0f}%)"
                        if user_limit < _survival_floor - 0.5:
                            _disp_txt = f"Защита дома (Цель {_m_survival_target:.0f}% УТРО)" if 4 <= (cur_hour % 24) <= 10 else f"Защита дома (Цель {_m_survival_target:.0f}% к утру)"
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
                    # v11.6.275: Standardize Strategic Morning Projection (Dawn Anchor)
                    # We run a final "Strategic Nocturnal Simulation" to confirm the 18% target.
                    # This simulation includes planned sales but ignores phantom solar.
                    _final_sim_range = list(range(cur_hour, _h_sunrise_target + 1))
                    if _final_sim_range:
                         _, _strat_log, _ = self.run_soc_simulation(b_soc, _final_sim_range, now, commands=sell_commands, no_solar=True)
                         soc_morning_display = float(round_f(self._get_soc_from_log(_strat_log, key_sunrise, b_soc), 1))
                    else:
                         soc_morning_display = b_soc

                    morning_key_disp = key_sunrise
                    
                    _all_sell_hrs = [h for h in target_hours_sorted if h >= cur_hour]
                    if _all_sell_hrs:
                        _soc_vals = []
                        for _h in _all_sell_hrs:
                            _k = f"{_h % 24:02d}:59" + (" (Завтра)" if _h >= 24 else "")
                            if _v := self._get_soc_from_log(sim_log, _k, b_soc):
                                # v11.6.315: Price-Priority Distribution (Profit Maximization)
                                # We fill the most expensive hours first as requested by the user.
                                _all_sell_hrs_sorted = sorted(_all_sell_hrs, key=lambda h: all_prices.get(h, 0.0), reverse=True)
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
                    # v11.6.570: Epoch-Aware Bucket Distribution (Pool-Aware Pricing)
                    # We only distribute available_sell_ac among hours in the FIRST epoch.
                    # This prevents tomorrow's evening peaks from stealing today's surplus.
                    _first_epoch = epochs[0] if 'epochs' in locals() and epochs else _all_sell_hrs
                    _epoch_sorted = sorted(_first_epoch, key=lambda hr: all_prices.get(hr, 0.0), reverse=True)
                    
                    # Group hours into price buckets (0.05 hysteresis) for Fair-Greedy smoothing
                    buckets = []
                    if _epoch_sorted:
                        current_bucket = [_epoch_sorted[0]]
                        for h_next in _epoch_sorted[1:]:
                            p_cur = all_prices.get(current_bucket[0], 0.0)
                            p_next = all_prices.get(h_next, 0.0)
                            if abs(p_cur - p_next) <= 0.05:
                                current_bucket.append(h_next)
                            else:
                                buckets.append(current_bucket)
                                current_bucket = [h_next]
                        buckets.append(current_bucket)

                    rem_budget_ac = available_sell_ac
                    # Clear commands for ALL candidate hours before redistribution
                    for h_to_clr in target_hours_sorted: sell_commands[int(h_to_clr)] = 0.0

                    for bucket in buckets:
                        if rem_budget_ac <= 0.01: break
                        
                        bucket_items = []
                        for h in bucket:
                            h_f = max(0.1, (60 - now.minute) / 60.0) if h == cur_hour else 1.0
                            max_h_ac = work_max_p * h_f 
                            bucket_items.append((h, h_f, max_h_ac))
                        
                        # Fair share AC energy per hour in bucket
                        fair_share_ac = rem_budget_ac / len(bucket)
                        for h, h_f, max_h_ac in bucket_items:
                            can_take_ac = min(fair_share_ac, max_h_ac)
                            if can_take_ac > 0.01:
                                sell_commands[int(h)] = float(round_f(can_take_ac / h_f, 3))
                                rem_budget_ac -= (sell_commands[int(h)] * h_f)

                    # v11.6.325: Unified Budget Distribution (House-Blind)
                    total_planned_ac = sum(sell_commands.values())

                    # Update Status Flags
                    _is_p_limited = bool(round(total_planned_ac, 2) >= round(work_max_p * len(_all_sell_hrs) * eff, 2))
                    _is_u_limited = (available_sell_dc <= (surplus_for_user_limit + 0.01) and surplus_for_user_limit < (surplus_for_morning - 0.1))
                    
                    limit_label = f"Лимит пользователя ({min_soc_val:.0f}%)"
                    if base_target > min_soc_val + 0.5:
                        _disp_goal = (min_soc_bat_val + 2.0) if 4 <= (cur_hour % 24) <= 12 else (min_soc_bat_val + soc_buffer_full)
                        limit_label = f"Защита дома (Порог {base_target:.0f}% для {_disp_goal:.0f}% к утру)"
                    
                    sell_diagnosis = limit_label
                    if _is_p_limited:
                         sell_diagnosis = f"Лимит мощности АКБ ({work_max_p:.1f}кВт)"
                    elif _is_u_limited:
                         sell_diagnosis = f"Лимит пользователя ({min_soc_val:.0f}%)"

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

                    # v11.6.212: Restore v207-style stable start projection.
                    # Use BASELINE simulation (no sales) for the start point.
                    _active_planned = [int(h) for h, p in sell_commands.items() if p > 0.01]
                    if _active_planned:
                        _h_start = min(_active_planned)
                        _h_end = max(_active_planned)
                        _k_start = f"{(_h_start-1)%24:02d}:59" + (" (Завтра)" if (_h_start-1) >= 24 else "")
                        _k_end = f"{_h_end%24:02d}:59" + (" (Завтра)" if _h_end >= 24 else "")
                        
                        # START point from BASELINE log
                        soc_at_start = self._get_soc_from_log(sim_log_base, _k_start, b_soc)
                        # AFTER point from REAL log (with sales)
                        soc_after = self._get_soc_from_log(sim_log, _k_end, soc_at_start)
                    else:
                        soc_at_start = b_soc
                        soc_after = b_soc

                    res["sell_simulation"] = {
                        "projected_soc_at_sale_start_pct": float(round_f(soc_at_start, 1)),
                        "projected_soc_after_sale_pct": float(round_f(soc_after, 1)),
                        "projected_soc_morning_pct": float(round_f(soc_morning_display, 1)),
                        "projected_soc_morning": float(round_f(soc_morning_display, 1)),
                        "log": sim_log
                    }
                    res["projected_soc_after_sale"] = round_f(soc_after, 1)
                    res["projected_soc_morning"] = round_f(soc_morning_display, 1)

                    # v11.4.04: Reciprocal Surplus Calculation (Simulation Monarchy)
                    # We recalculate M, U based on what the simulation JUST confirmed.
                    # v11.6.270: Use standardized soc_morning_display for TRUE_M consistency.
                    true_m_surplus = round(((soc_morning_display - target_morning_soc) * b_cap / 100.0), 1)
                    true_u_surplus = round(((soc_after - base_target) * b_cap / 100.0), 1)

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
            # v11.6.225: Do NOT filter out hours with 0.0 kW power.
            # Show all candidates to the user for transparency.
            _filtered_targets = list(target_hours_sorted)
            target_hours_sorted = _filtered_targets

            # v11.6.355: Re-sync power_needed after recursive adjustments
            power_needed = sell_commands.get(int(cur_hour), 0.0)

            # v11.6.261: Move shared result population out of conditional blocks
            # Use current peak power only if we are actually in a peak hour
            in_peak = (cur_hour in target_hours_sorted)
            if in_peak and (power_needed > 0.05 or cur_hour in negative_hours):
                res["state"] = "active"
            
            if _sell_debug:
                _sell_debug["in_peak"] = in_peak
                _sell_debug["power_needed"] = round_f(power_needed, 3)
                _sell_debug["final_state"] = res["state"]
                _sell_debug["f_tom"] = round_f(f_tom, 1)
                _sell_debug["night_prof"] = _prof_debug
                res["arbitrage_sell_debug"] = _sell_debug
            
            res["recommended_power_kw"] = float(round_f(min(float(power_needed), max_p), 3))

            # v11.6.258: Filter active list for UI to exclude zero-power hours UNLESS they are negative
            # This makes the UI much cleaner and prevents "scary" high-price active periods with 0.0kW
            actual_active = [h for h in target_hours_sorted if h >= cur_hour]
            actual_active_ui = []
            for h in actual_active:
                p_h = sell_commands.get(h, 0.0) if mode == "sell" else charge_commands.get(h, 0.0)
                price_h = all_prices.get(h, 0.0)
                if p_h > 0.05 or price_h <= 0.0:
                    actual_active_ui.append(h)

            res["active_hours"] = actual_active_ui
            res["active_hours_formatted"] = ", ".join([self._format_h(h) for h in actual_active_ui])
            
            # Regenerate active_periods based on final filtered hours (v6.18)
            final_periods = []
            if actual_active_ui:
                sorted_fit = sorted(list(set(actual_active_ui)))
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

            res["active_hours"] = actual_active_ui
            res["active_hours_formatted"] = ", ".join([self._format_h(h) for h in actual_active_ui])
            res["active_periods"] = ", ".join(final_periods) if final_periods else "Нет"
            
            p_distribution = {}
            if actual_active:
                sim_info = res.get("sell_simulation" if mode == "sell" else "buy_simulation")
                s_log = sim_info.get("log", {}) if sim_info else {}
                
                # v11.6.40: Show ALL hours of the first Energy Pool in planned_power
                _first_window_active = res.get("first_pool_hours", actual_active_ui)
                
                # v11.6.116: Filter UI display to match the 0.0kW cleanup
                _first_window_active = [h for h in _first_window_active if h in actual_active_ui]
                
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
                    
                    # v11.6.385: Show target SOC even for 0.0 kW hours for better plan visibility
                    t_soc = round_f(float(target_soc), 1)
                    if mode == "sell":
                        h_f_local = max(0.1, (60 - now.minute) / 60.0) if h_idx == cur_hour else 1.0
                        # v11.6.568: Use Predicted Profile (Synced with simulation)
                        house_cons_local = float(normalize_float(self.manager.get_predicted_profile("consumption_total").get(str(h_idx_norm), 0.5))) * occ_coeff
                        house_rem_dc_local = (house_cons_local * h_f_local) / eff
                        discharge_dc_local = (p_val * h_f_local) / eff
                        pure_discharge_pct_local = (discharge_dc_local + house_rem_dc_local) / b_cap * 100.0 if b_cap > 0.1 else 0.0
                        
                        _target_limit_local = min_soc_val + 2.0 if (4 <= (h_idx % 24) < 12) else base_target
                        h_target = max(_target_limit_local, _h_start_soc - pure_discharge_pct_local)
                        
                        # Update _h_start_soc for the NEXT hour in the loop (to keep it cumulative)
                        _h_start_soc = h_target
                        
                        if p_val > 0.01:
                            p_distribution[h_label] = f"{round_f(p_val, 2)} kW (Цель: {round_f(h_target, 1)}% | Прогноз: {round_f(h_soc_sim, 1)}%)"
                        else:
                            p_distribution[h_label] = f"{round_f(p_val, 2)} kW (Цель: {round_f(h_target, 1)}% | Прогноз: {round_f(h_soc_sim, 1)}%)"
                        
                    else:
                        # v11.6.521: Sync recommended_power with the actual Plan for current hour
                        sim_target_h = round_f(h_soc_sim, 1)
                        # If this is the current hour, apply the floor protection
                        h_target_buy = max(b_soc, sim_target_h) if (h_idx == cur_hour and res.get("charge_reason") != "none") else sim_target_h
                        p_distribution[h_label] = f"{round_f(p_val, 2)} kW (Цель: {h_target_buy}% | Прогноз: {sim_target_h}%)"
                        
                        if h_idx == cur_hour:
                            # v11.6.530: Force active power and calculate Amps
                            _p_final = float(round_f(p_val, 2))
                            res["recommended_power_kw"] = _p_final
                            # v11.6.530: Precise Amps calculation for hardware (Power * 1000 / Voltage)
                            res["recommended_amps"] = round(_p_final * 1000 / 51.2, 1)
                    
            res["planned_power_per_h"] = p_distribution
            
            # v11.6.101: Solar-Blind Target SOC for Selling
            # If we use the simulation log (which includes solar charging), the target_soc 
            # will be artificially raised (e.g., 17.2% instead of 15.0%). 
            # This causes the inverter to stop discharging too early, preventing solar energy 
            # from being exported to the grid. The inverter must receive the PURE discharge target.
            if in_peak:
                if mode == "sell":
                    h_f = max(0.1, (60 - now.minute) / 60.0)
                    # v11.6.568: Use Predicted Profile (Synced with simulation)
                    house_cons = float(normalize_float(self.manager.get_predicted_profile("consumption_total").get(str(cur_hour), 0.5))) * occ_coeff
                    house_rem_dc = (house_cons * h_f) / eff
                    discharge_dc = (power_needed * h_f) / eff
                    pure_discharge_pct = (discharge_dc + house_rem_dc) / b_cap * 100.0 if b_cap > 0.1 else 0.0
                    
                    # v11.6.119: Align target_soc with end of CURRENT HOUR (Solar-Blind)
                    # We target the SOC reached by discharging battery + house load, ignoring solar gain.
                    target_soc = max(0.0, b_soc - pure_discharge_pct)
                    
                    # v11.6.234: Strictly follow TS: Morning Window (04:00-12:00) ends at 12:00 sharp.
                    _target_limit = min_soc_val + 2.0 if (4 <= (cur_hour % 24) < 12) else base_target
                    target_soc = max(target_soc, _target_limit)
                else:
                    # v11.6.515: Step-by-step Target SOC for current hour.
                    # Protective Floor: In BUY mode, don't let target SOC drop below current SOC 
                    # while waiting for the price window, to prevent unwanted inverter discharge.
                    h_key_now = f"{cur_hour % 24:02d}:59"
                    sim_target = float(self._get_soc_from_log(s_log, h_key_now, b_soc))
                    target_soc = max(b_soc, sim_target) if (res.get("charge_reason") in ["negative", "survival", "cheap"]) else sim_target

                
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
            
            # v11.6.265: Export raw commands for cross-strategy simulation awareness
            res["raw_commands"] = sell_commands if mode == "sell" else charge_commands
            
            self._strategy_cache[cache_key] = {"time": now, "res": res}
            return res
        finally:
            self._calculating_strategy = old_calc

    def _get_arbitrage_info(self, cur_hour, buy_prices, sell_prices, target_hours=None):
        """Universal arbitrage calculation logic to avoid code duplication.
        Returns a dict with arbitrage_decision and best buy/sell hour info.
        """
        arb_msg = "Нет выгодного арбитража"
        best_buy_h = None
        deg_cost = self.get_battery_degradation_cost()
        prof_thresh = float(self.manager.get_setting("arbitrage_profit_threshold", 0.5))
        threshold = float(max(prof_thresh, 2.0 * deg_cost))
        
        # 1. Find best buy window in the next 24h
        if buy_prices:
            options = {int(h): p for h, p in buy_prices.items() if int(h) >= cur_hour}
            if options:
                best_buy_h = min(options, key=lambda k: options[k])
        
        # 2. Find best sell window
        profit = 0.0
        if target_hours and sell_prices:
            best_sell_h = max(target_hours, key=lambda h: sell_prices.get(str(h), sell_prices.get(int(h), 0.0)))
            
            p_buy = buy_prices.get(str(best_buy_h), buy_prices.get(best_buy_h, 0.0)) if best_buy_h is not None else 0.0
            p_sell = sell_prices.get(str(best_sell_h), sell_prices.get(best_sell_h, 0.0))
            profit = (p_sell - p_buy) - deg_cost
            
            h_buy_str = f"{best_buy_h % 24:02d}:00" if best_buy_h is not None else "Plan"
            h_sell_str = f"{best_sell_h % 24:02d}:00"
            if best_buy_h is not None and best_buy_h >= 24: h_buy_str += " (Завтра)"
            if best_sell_h >= 24: h_sell_str += " (Завтра)"
            
            if profit >= threshold:
                arb_msg = f"Купим в {h_buy_str} ({p_buy:.2f}) -> Продадим в {h_sell_str} ({p_sell:.2f}). Профит: {profit:.2f}/кВтч"
            else:
                arb_msg = f"Арбитраж невыгоден ({profit:.2f} < {threshold:.2f}). Лучшая закупка в {h_buy_str}, продажа в {h_sell_str}"
        elif best_buy_h is not None:
            h_buy_str = f"{best_buy_h % 24:02d}:00"
            if best_buy_h >= 24: h_buy_str += " (Завтра)"
            arb_msg = f"Ожидаем окно продажи (Лучшая закупка в {h_buy_str})"
            
        return {
            "arbitrage_decision": arb_msg,
            "best_buy_hour": best_buy_h,
            "best_sell_hour": max(target_hours) if target_hours else None,
            "expected_profit": round_f(profit, 3)
        }

