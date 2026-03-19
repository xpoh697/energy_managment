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
        if soc < 80: return 1.0
        if soc >= 98: return 0.1
        return 1.0 - (soc - 80) * (0.9 / 18.0)

    @staticmethod
    def _format_h(h_abs):
        if h_abs is None: return "Нет данных"
        d = "Завтра " if h_abs >= 24 else ""
        return f"{d}{h_abs % 24:02d}:00"

    def get_battery_degradation_cost(self):
        """Cost of battery wear per kWh (Cycle Cost)."""
        batt_cost = self.manager.get_setting(CONF_BATTERY_COST, 0.0)
        cycles = self.manager.get_setting(CONF_BATTERY_RATED_CYCLES, 6000)
        
        _, cap, _ = self.manager.get_battery_state()
        if cap <= 0: cap = 10.0
        
        if cycles <= 0 or batt_cost <= 0: return 0.0
        return batt_cost / (cycles * cap)

    def get_efficiency_coefficient(self) -> float:
        """Calculates historical inverter/system efficiency."""
        man: Any = self.manager
        d_store = getattr(man, "data", {})
        if not isinstance(d_store, dict):
            return 1.0

        p_val = getattr(man, "custom_period", 14)
        n_days = 14
        if p_val is not None:
            try:
                n_days = int(float(str(p_val)))
            except (ValueError, TypeError):
                n_days = 14
        
        sum_g = 0.0
        sum_l = 0.0
        smp_count = 0
        
        l_map = d_store.get("losses", {})
        if not isinstance(l_map, dict):
            return 1.0
            
        for h_idx in range(24):
            key_h = str(h_idx)
            recs = l_map.get(key_h, [])
            if not isinstance(recs, list):
                continue
            
            n_tot = len(recs)
            start_i = 0
            if n_days > 0 and n_tot > n_days:
                start_i = int(n_tot - n_days)
                
            for i in range(n_tot):
                if i < start_i:
                    continue
                item = recs[i]
                if not isinstance(item, dict):
                    continue
                
                g_v = item.get("gen", 0.0)
                l_v = item.get("v", 0.0)
                g_val = float(normalize_float(g_v))
                l_val = float(normalize_float(l_v))
                
                if g_val > 0.01:
                    sum_g += g_val
                    sum_l += l_val
                    smp_count += 1
        
        if smp_count < 5 or sum_g < 0.1:
            return 1.0
            
        eff_ratio = float((sum_g - sum_l) / sum_g)
        return float(max(0.85, min(1.0, eff_ratio)))

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
            
            p_gen = dict(man.get_average_profile("generation", days_for_profile, "all"))
            hist_gen_so_far = float(sum(float(normalize_float(p_gen.get(str(h), 0.0))) for h in range(cur_hour + 1)))
            total_hist_gen = float(sum(float(normalize_float(p_gen.get(str(h), 0.0))) for h in range(24)))
            
            # --- Improved Performance Coefficients (v4.0) ---
            # A. Calculate Historical Average Performance from history list
            hist_recs = man.data.get("forecast_history", [])
            perf_list = []
            for rec in hist_recs:
                act = float(rec.get("actual", 0))
                fct = float(rec.get("forecast", 0))
                if fct > 0.1:
                    perf_list.append(max(0.3, min(act / fct, 1.5)))
            
            hist_coeff = float(sum(perf_list) / len(perf_list)) if perf_list else 1.0
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
            
            # Use a blend of history and today's yield performance
            blended_coeff = float((today_coeff * external_progress) + (hist_coeff * (1.0 - external_progress)))
            
            # Safety guards
            blended_coeff = float(max(0.3, min(blended_coeff, 1.5)))
            
            man.last_blended_coeff = blended_coeff
            forecast_val_adjusted = float(forecast_val * blended_coeff)
                
            # 2. Battery state
            batt_soc, batt_cap, batt_energy_val = man.get_battery_state()
            b_soc_f = float(batt_soc)
            b_cap_f = float(batt_cap)
            b_energy_f = float(batt_energy_val)
            
            min_soc_val = man.get_setting(CONF_MIN_SOC_BUY, 10.0)
            min_soc = float(min_soc_val) if min_soc_val is not None else 10.0
            eff_coeff = float(self.get_efficiency_coefficient() or 1.0)
                        
            # 3. Expected consumption
            occ_coeff = float(man.get_occupancy_coefficient())
            expected_today = float(man.get_expected_remaining("consumption_base", days_for_profile)) * occ_coeff
            expected_night = float(man.get_expected_night("consumption_base", days_for_profile)) * occ_coeff
            expected_consumption = float(expected_today + expected_night)
            
            # Current historical value (used for waste/potential detection)
            cur_hist_val = float(normalize_float(p_gen.get(str(cur_hour), 0.0)))

            # 1. Solar Remaining
            # Important: forecast_val (from man.forecast_today_sensor) typically already 
            # represents the REMAINING solar for the day. 
            # Multiplying by (hist_rem / total_hist_gen) would cause a double-reduction.
            solar_remaining = float(forecast_val_adjusted or 0.0)
            
            initial_budget = float(solar_remaining + (b_energy_f - (min_soc * b_cap_f / 100.0)) - expected_consumption)
            available_budget = initial_budget
            
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
                hist_rem = sum(float(p_gen.get(str(h), 0.0)) for h in range(cur_hour, 24))
                f_potential = float(f_today * (cur_hist_val / hist_rem)) if hist_rem > 0.1 else 0.0
                
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
            available_gen_kw = float(sum((get_kwh_val(man.hass.states.get(str(s)) or None) or 0.0) for s in p_gen_s)) + waste_kw
            
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
                        available_gen_kw -= e_kw
                        reserved_by.append(s_id_s)
                    
            return {
                "initial_budget": float(initial_budget or 0.0),
                "permissions": permissions or {},
                "permissions_reasons": permissions_reasons or {},
                "forecast_val": float(forecast_val_adjusted or 0.0),
                "forecast_raw": float(forecast_val or 0.0),
                "forecast_coefficient": float(blended_coeff or 1.0),
                "forecast_hist_coefficient": float(hist_coeff or 1.0),
                "forecast_today_coefficient": float(today_coeff or 1.0),
                "debug_actual_today": float(actual_today or 0.0),
                "debug_expected_today_total": float(expected_today_total or 0.0),
                "debug_expected_today_so_far": float(expected_today_total - forecast_val),
                "debug_fraction_so_far": float(external_progress if 'external_progress' in locals() else fraction_so_far),
                "batt_energy_val": float(b_energy_f or 0.0),
                "expected_consumption": float(expected_consumption or 0.0),
                "occupancy_coefficient": float(occ_coeff or 1.0),
                "efficiency_coefficient": float(eff_coeff or 1.0),
                "available_power_total_kw": float(initial_power_kw or 0.0),
                "available_gen_kw": float(available_gen_kw or 0.0),
                "waste_compensation_kw": float(waste_kw or 0.0),
                "battery_flexible_kw": float(batt_p_flexible or 0.0),
                "battery_discharge_budget_kw": float(batt_discharge_allowed or 0.0)
            }
        finally:
            self._calculating_strategy = old_calc

    def run_soc_simulation(self, start_soc, sim_range, now, commands=None):
        """Universal SOC simulation engine."""
        if not sim_range:
            return float(start_soc), {}

        man: Any = self.manager
        _, batt_cap, _ = man.get_battery_state()
        b_cap_f = float(batt_cap)
        if b_cap_f <= 0.1:
            return float(start_soc), {}

        f_today = float(man.get_forecast_value(man.forecast_today_sensor) or 0.0)
        f_tom = float(man.get_forecast_value(man.forecast_tomorrow_sensor) or 0.0)
        
        day_type_today = "weekend" if now.weekday() >= 5 else "weekday"
        tomorrow_dt = now + timedelta(days=1)
        day_type_tom = "weekend" if tomorrow_dt.weekday() >= 5 else "weekday"
        
        prof_gen = dict(man.get_average_profile("generation", man.custom_period, "all"))
        prof_cons_today = dict(man.get_average_profile("consumption_base", man.custom_period, day_type_today))
        prof_cons_tom = dict(man.get_average_profile("consumption_base", man.custom_period, day_type_tom))
        
        total_hist_gen = float(sum(float(normalize_float(prof_gen.get(str(h), 0.0))) for h in range(24)))
        if total_hist_gen < 0.1: total_hist_gen = 1.0
        
        blended_coeff = float(getattr(man, "last_blended_coeff", 1.0))
        eff_coeff = float(self.get_efficiency_coefficient() or 1.0)
        max_batt_p_v = man.get_setting(CONF_BATTERY_MAX_POWER, 5.0)
        max_batt_p = float(max_batt_p_v) if max_batt_p_v is not None else 5.0

        simulated_soc = float(start_soc)
        history_log = {}
        fraction_left_h1 = float(1.0 - (now.minute / 60.0))
        
        sim_consumed_today = {str(s_id): float(man.daily_deduct_consumption.get(str(s_id), 0.0)) 
                             for s_id in man.deduct_settings}
        sim_consumed_tom = {str(s_id): 0.0 for s_id in man.deduct_settings}

        for i, h_abs in enumerate(sim_range):
            real_h = int(h_abs % 24)
            is_tom = bool(h_abs >= 24)
            h_str = str(real_h)
            
            step_duration = float(fraction_left_h1 if i == 0 else 1.0)
            if step_duration <= 0.001: continue

            active_m_p = 0.0
            day_sim_consumed = sim_consumed_tom if is_tom else sim_consumed_today
            d_settings = dict(getattr(man, "deduct_settings", {}))
            for s_id, s_conf in d_settings.items():
                s_id_s = str(s_id)
                p_kw, _, _, sc_is_running = man.get_managed_load_stats(s_id_s)
                target_kwh = float(s_conf.get("required_kwh", 2.0))
                
                if day_sim_consumed.get(s_id_s, 0.0) < target_kwh:
                    only_solar = bool(s_conf.get(CONF_ONLY_SOLAR, False))
                    p_draw = 0.0
                    if sc_is_running and not is_tom:
                        p_draw = float(p_kw)
                    elif not bool(s_conf.get(CONF_IS_CYCLIC, False)):
                        p_draw = float(p_kw)
                    
                    if only_solar and expected_gen_kw < float(p_draw * 0.5):
                        p_draw = 0.0 # Solar only loads don't run at night/cloudy in sim
                    
                    if p_draw > 0.001:
                        active_m_p += p_draw
                        day_sim_consumed[s_id_s] += float(p_draw * step_duration)

            hist_hour_gen = float(normalize_float(prof_gen.get(h_str, 0.0)))
            if is_tom:
                # For tomorrow, always use full day history as baseline (starts at 00:00)
                expected_gen_kw = float(hist_hour_gen / total_hist_gen * f_tom * blended_coeff) if total_hist_gen > 0.1 else hist_hour_gen
            else:
                # DISTRIBUTION FIX: Use only the FUTURE part of historical profile for distribution
                # because f_today typically represents the REMAINING solar from NOW.
                if 'hist_rem_today' not in locals():
                    cur_h = int(now.hour)
                    hist_rem_today = float(sum(float(normalize_float(prof_gen.get(str(h), 0.0))) for h in range(cur_h, 24)))
                    if hist_rem_today < 0.1: hist_rem_today = total_hist_gen # Fallback

                expected_gen_kw = float(hist_hour_gen / hist_rem_today * f_today * blended_coeff) if hist_rem_today > 0.1 else hist_hour_gen
                
                if i == 0:
                    # Current actual power is often more accurate than profile for the first hour
                    cur_actual_gen = float(getattr(man, "avg_gen_kw", 0.0))
                    if cur_actual_gen > expected_gen_kw:
                        expected_gen_kw = cur_actual_gen
            
            p_cons = prof_cons_tom if is_tom else prof_cons_today
            occ_coeff = float(man.get_occupancy_coefficient())
            expected_cons_kw = float(normalize_float(p_cons.get(h_str, 0.0))) * occ_coeff
            
            cmd_p = 0.0
            if commands and h_abs in commands:
                cmd_p = float(commands[h_abs])
            
            net_house_kw = float(expected_gen_kw - expected_cons_kw)
            total_net_kw = float(net_house_kw + cmd_p - active_m_p)
            
            if total_net_kw > 0.001: 
                acc_ratio = float(self.get_cc_cv_ratio(simulated_soc))
                actual_charge_kw = float(min(total_net_kw * eff_coeff, max_batt_p * acc_ratio))
                if b_cap_f > 0.1:
                    simulated_soc = float(min(100.0, simulated_soc + (actual_charge_kw * step_duration / b_cap_f * 100.0)))
            elif total_net_kw < -0.001: 
                sim_eff = float(max(0.85, eff_coeff))
                # Cap discharge power by battery physical limits
                actual_discharge_kw = float(min(abs(total_net_kw) / sim_eff, max_batt_p))
                if b_cap_f > 0.1:
                    simulated_soc = float(max(0.0, simulated_soc - (actual_discharge_kw * step_duration / b_cap_f * 100.0)))
            
            history_log[f"{real_h:0>2}:59" + (" (Завтра)" if is_tom else "")] = float(round_f(simulated_soc, 1))

        return float(simulated_soc), history_log

    def get_market_strategy(self, mode="buy"):
        now = dt_util.now()
        man: Any = self.manager
        
        cache_key = f"market_strategy_{mode}"
        cached = self._strategy_cache.get(cache_key)
        if cached and (now - cached["time"]).total_seconds() < 30:
            return cached["res"]

        res = {
            "state": "idle",
            "active_hours": [],
            "active_periods": "",
            "recommended_power_kw": 0.0,
            "target_price": 0.0,
            "limit_used": 0.0,
            "today_prices": {},
            "tomorrow_prices": {},
            "multi_cycle": "Не предвидится",
            "buy_simulation": {"projected_soc_at_start_pct": 0.0, "projected_soc_at_end_pct": 0.0},
            "sell_simulation": {"projected_soc_at_start_pct": 0.0, "projected_soc_after_sale_pct": 0.0, "projected_soc_morning_pct": 0.0},
            "arbitrage_decision": "Нет данных",
            "arbitrage_buyback": {"opportunity": False, "power_kw": 0.0, "note": ""}
        }
        
        old_calc = bool(getattr(self, "_calculating_strategy", False))
        self._calculating_strategy = True
        try:
            cur_hour = int(now.hour)
            today_str = now.strftime("%Y-%m-%d")
            tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
            
            p_st = dict(man.data.get(f"prices_{mode}", {}))
            today_prices = dict(p_st.get(today_str, {}))
            tomorrow_prices = dict(p_st.get(tomorrow_str, {}))
        
            res["today_prices"] = today_prices
            res["tomorrow_prices"] = tomorrow_prices
            
            batt_soc, batt_cap, batt_energy_val = man.get_battery_state()
            b_soc = float(batt_soc)
            b_cap = float(batt_cap)
            
            today_type = "weekend" if now.weekday() >= 5 else "weekday"
            tom_type = "weekend" if (now + timedelta(days=1)).weekday() >= 5 else "weekday"
            
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

            def get_best_buyback(after_h):
                options = {int(h): float(p) for h, p in all_buy_prices.items() if int(h) > int(after_h)}
                if not options: return 999.0, None
                best_h = min(options, key=lambda k: options[k])
                return float(options[best_h]), int(best_h)

            # Find the absolute best buy hour for use in simulation windows
            _bb_options = [h for h in all_buy_prices if h >= cur_hour]
            _bb_h = min(_bb_options, key=lambda h: all_buy_prices[h]) if _bb_options else None
            best_buy_pair = (all_buy_prices[_bb_h], _bb_h) if _bb_h is not None else (999.0, None)

            best_arb_pair = (None, None)
            max_arb_gain = -999.0
            for h_s, p_s in all_sell_prices.items():
                if int(h_s) < cur_hour: continue
                p_b, h_b = get_best_buyback(h_s)
                if h_b is not None:
                    gain = float((float(p_s) - float(p_b)) * eff - deg_cost)
                    if gain > max_arb_gain:
                        max_arb_gain = gain
                        best_arb_pair = (int(h_s), int(h_b))

            global_arb_note = "Нет прибыльного арбитража"
            if max_arb_gain >= threshold:
                s_h, b_h = best_arb_pair
                if s_h is not None and b_h is not None:
                    global_arb_note = f"Арбитраж: Прд. {all_sell_prices[s_h]:.2f} ({self._format_h(s_h)}) -> Отк. {all_buy_prices[b_h]:.2f} ({self._format_h(b_h)}), выгода {max_arb_gain:.2f}"


            if mode == "buy":
                res["limit_used"] = buy_limit
                if negative_hours:
                    target_hours = list(negative_hours)
                    target_price = float(min([all_prices[h] for h in negative_hours]))
                    res["target_price"] = target_price
                else:
                    def is_buy_profitable_arb(buy_p, hour):
                        future_sell = [p_s for h_s, p_s in all_sell_prices.items() if h_s > hour]
                        if not future_sell: return False
                        return float((max(future_sell) - buy_p) * eff) >= threshold

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
                        target_price = float(min(p for h, p in combined))
                        res["target_price"] = target_price
                        
                        is_arb_window = any(is_buy_profitable_arb(p, h) for h, p in combined)
                        if dynamic_buy_ai and (not any(float(normalize_float(p)) <= buy_limit for h, p in combined) or is_arb_window):
                            res["state"] = "preparing_arbitrage"
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
                    gain = float((price - cheap_p_back) * eff - deg_cost)
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
                        cur_gain = float((cur_p_f - cheap_p_back) * eff - deg_cost)
                        
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
                
                safety_counter = 0
                while safety_counter < 48:
                    safety_counter += 1
                    added_bridge = False
                    # Plan simulation from actual start of charging until next significant sell peak
                    _win_end = int(best_arb_pair[0]) if best_arb_pair[0] is not None and int(best_arb_pair[0]) > cur_hour else int(max(all_buy_prices.keys()) if all_buy_prices else 23)
                    sim_range = list(range(cur_hour, _win_end + 1))
                    commands = {h_cmd: max_p for h_cmd in survival_hours}
                    _, log = self.run_soc_simulation(b_soc, sim_range, now, commands)
                    
                    violation_hour = None
                    for h_step in sim_range:
                        is_tom_sim = h_step >= 24
                        h_label = f"{h_step % 24:0>2}:59" + (" (Завтра)" if is_tom_sim else "")
                        soc_at_h = float(log.get(h_label, 100.0))
                        if soc_at_h < min_soc and violation_hour is None:
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
                self._calculating_strategy = old_calc
                return res
                
            target_hours_sorted = sorted(target_hours)
            found_periods = []
            def _format_period(s, e):
                s_d = "Завтра " if s >= 24 else ""
                e_d = "Завтра " if e >= 24 else ""
                return f"{s_d}{s % 24:02d}:00 - {e_d}{e % 24:02d}:59"
                
            if target_hours_sorted:
                start = prev = target_hours_sorted[0]
                for h in target_hours_sorted[1:]:
                    if h == prev + 1: prev = h
                    else:
                        found_periods.append(_format_period(start, prev))
                        start = prev = h
                found_periods.append(_format_period(start, prev))
                
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
                    cheapest_buy_in_window = min(float(all_buy_prices[h]) for h in target_hours_sorted if h >= cur_hour) if target_hours_sorted else 999.0
                    if peak_hour is not None and (best_peak_p - cheapest_buy_in_window) * eff >= strict_threshold:
                        is_strict_arb = True
                    
                    dynamic_buy_ai = bool(man.get_setting(CONF_DYNAMIC_SOC_BUY, True))
                    if negative_hours:
                        target_soc = 100.0
                    elif dynamic_buy_ai and is_strict_arb:
                        # ADAPTIVE TARGET: Only buy what the sun won't provide until the peak starts
                        # Run a 'dry' simulation to see what SOC we hit by peak_hour WITHOUT buying from grid
                        sim_range_dry = list(range(cur_hour, int(peak_hour)))
                        if sim_range_dry:
                            expected_soc_at_peak, _ = self.run_soc_simulation(b_soc, sim_range_dry, now, commands=None)
                            # We want to be at 100% at peak_hour. 
                            # If solar alone gives us 20% gain, we only need to buy up to 80% now.
                            sun_gain = max(0.0, expected_soc_at_peak - b_soc)
                            target_soc = float(min(100.0, 100.0 - sun_gain))
                        else:
                            target_soc = 100.0
                    elif dynamic_buy_ai:
                        budget_data = self.get_budget_and_permissions(man.custom_period, skip_strategy_check=True)
                        if budget_data:
                            expected_night_val = budget_data.get("expected_consumption", 0.0)
                            expected_night = float(normalize_float(expected_night_val))
                            forecast_val_raw = budget_data.get("forecast_val", 0.0)
                            forecast = float(normalize_float(forecast_val_raw))
                            total_avg = float(sum(man.get_average_profile("consumption_total", man.custom_period, tom_type).values()))
                            tomorrow_need = float(max(0.0, (total_avg - expected_night) - forecast))
                            target_soc = float(min(base_target, (expected_night + tomorrow_need) / b_cap * 100.0))
                        else: target_soc = base_target
                    else: target_soc = base_target
                    
                    target_soc = float(min(100.0, target_soc))
                    sim_soc_plan = b_soc
                    
                    charge_commands = {}
                    upcoming_p = 0.0
                    for h in target_hours_sorted:
                        if h < cur_hour: continue
                        rem_n = len([x for x in target_hours_sorted if x >= h]) or 1
                        if target_soc > sim_soc_plan:
                            p = float(min(max_p, (b_cap * (target_soc - sim_soc_plan) / 100.0) / rem_n))
                        else: p = 0.0
                        
                        if h == cur_hour: power_needed = p
                        if upcoming_p == 0: upcoming_p = p
                        
                        charge_commands[int(h)] = p
                        sim_soc_plan = float(min(100.0, sim_soc_plan + (p / b_cap * 100.0))) 
                    
                    if power_needed == 0:
                        power_needed = upcoming_p
                    
                    # --- BUY SIMULATION ---
                    if target_hours_sorted:
                        # Extend to tomorrow morning (8:00 AM) or end of peaks
                        sim_end_h = max(32, max(target_hours_sorted) + 1)
                        sim_range = list(range(cur_hour, sim_end_h))
                        _, sim_log = self.run_soc_simulation(b_soc, sim_range, now, charge_commands)
                        
                        # 1. Projected SOC at START of the first buy hour
                        first_h_buy = min(t for t in target_hours_sorted if t >= cur_hour)
                        if first_h_buy > cur_hour:
                            prev_h = first_h_buy - 1
                            key_start = f"{prev_h % 24:02d}:59" + (" (Завтра)" if prev_h >= 24 else "")
                            soc_at_start = float(sim_log.get(key_start, b_soc))
                        else:
                            soc_at_start = b_soc

                        # 2. Projected SOC AFTER the last buy hour
                        last_h_buy = max(target_hours_sorted)
                        key_end = f"{last_h_buy % 24:02d}:59" + (" (Завтра)" if last_h_buy >= 24 else "")
                        soc_at_end = float(sim_log.get(key_end, b_soc))
                        
                        # 3. Projected SOC TOMORROW MORNING (08:00 AM)
                        key_morning = "07:59 (Завтра)"
                        soc_morning = float(sim_log.get(key_morning, soc_at_end))
                        
                        res["buy_simulation"] = {
                            "projected_soc_at_start_pct": float(round_f(soc_at_start, 1)),
                            "projected_soc_at_end_pct": float(round_f(soc_at_end, 1)),
                            "projected_soc_morning_pct": float(round_f(soc_morning, 1))
                        }
                else: # sell
                    # Initial defaults for robustness
                    arb_gain = 0.0
                    cheap_h_back = None
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
                        
                    # Dynamic sunrise detection: Find first hour tomorrow with generation > 0.1 kWh
                    avg_prof_gen = man.get_average_profile("generation", man.custom_period, "all")
                    sunrise_h = 8 # default fallback
                    for h in range(4, 12):
                        if float(normalize_float(avg_prof_gen.get(str(h), 0.0))) > 0.1:
                            sunrise_h = h
                            break
                    
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
                    
                    # Adaptive buffer: 0% ONLY if ALL sale hours are in morning/day (sunrise_h-13) with solar.
                    is_evening_sale = any(h > 13 for h in target_hours_sorted) if target_hours_sorted else True
                    has_solar_coming = man.get_expected_remaining("generation") > 0.5
                    
                    active_buffer = soc_buffer_val
                    if not is_evening_sale and has_solar_coming:
                        active_buffer = 0.0

                    target_morning_soc = float(man.get_setting(CONF_MIN_SOC_BUY, 10.0)) + active_buffer
                    
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
                    tomorrow_cons_total = float(sum(man.get_average_profile("consumption_total", man.custom_period, tom_type).values())) * occ_coeff
                    solar_is_excess = bool(tomorrow_solar_total > tomorrow_cons_total + 2.0) # 2kWh buffer
                    
                    # PRECISE SIMULATION-BASED CALCULATION (v4.5)
                    if man.get_setting(CONF_DYNAMIC_SOC_SELL, True):
                        # 1. Run Baseline Simulation (zero sales) to see 'natural' morning SOC
                        sim_end_h = max(24 + sunrise_h, int(active_window[1]) + 1)
                        sim_range = range(cur_hour, sim_end_h)
                        _, baseline_log = self.run_soc_simulation(b_soc, sim_range, now, {})
                        
                        # Find natural SOC at sunrise (last point in sim_log or exact hour)
                        natural_morning_soc = b_soc
                        if baseline_log:
                            # Look for the last record or specific sunrise hour
                            target_time_str = f"{sunrise_h-1:02d}:59"
                            for entry in reversed(baseline_log):
                                if target_time_str in entry.get("time", ""):
                                    natural_morning_soc = float(entry.get("soc", b_soc))
                                    break
                            else:
                                # Fallback to the very last entry in the log
                                natural_morning_soc = float(baseline_log[-1].get("soc", b_soc))
                        
                        # 2. Available energy is the extra above target_morning_soc
                        extra_soc_pct = max(0.0, natural_morning_soc - target_morning_soc - 2.0) # 2% extra safety
                        available_sell_ac = float((extra_soc_pct * b_cap / 100.0) * eff)
                    else:
                        # Simple mode: energy above target SOC is sellable
                        available_sell_ac = float(max(0.0, (batt_energy_val - (base_target * b_cap / 100.0)) * eff))

                    res["sunrise_hour"] = sunrise_h  # Add to root for easier access
                    num_peaks_left = len([h for h in target_hours_sorted if h >= cur_hour]) or 1
                    
                    # Determine if we are in one of the selected Peak Hours
                    is_in_peak = bool(cur_hour in target_hours_sorted)
                    num_peaks_left = len([h for h in target_hours_sorted if h >= cur_hour]) or 1
                    
                    # Recommended power: Total available / hours left to sell
                    # Calculations are done even if NOT in peak to show potential in UI
                    power_peak = available_sell_ac / num_peaks_left
                    power_needed = float(max(0.0, power_peak))
                    
                    # Floor calculation: SOC needed NOW to have target_morning_soc at Sunrise
                    # This must account for ALL needs (House + Managed - Solar) until then.
                    res_cons_dc = max(0.0, (total_cons_to_sunrise + managed_needed_sunrise) / eff - (total_solar_to_sunrise / 0.98))
                    ai_soc_floor_reserve = target_morning_soc + (res_cons_dc / b_cap * 100.0) + 2.0 # +2% safety
                    
                    target_soc = float(max(base_target, ai_soc_floor_reserve))
                    
                    # Arbitrage decision for UI
                    # Rule: Arbitrage is ONLY beneficial if profit > threshold 
                    # AND it is better than just "saving for free solar tomorrow"
                    
                    p_bb, h_bb = get_best_buyback(cur_hour) 
                    gain_vs_buyback = 0.0
                    if h_bb is not None:
                         gain_vs_buyback = float((cur_p_f - p_bb) * eff - deg_cost)
                    
                    decision_tag = f"Лимит: {target_morning_soc:.0f}% на {sunrise_h:02d}:00"
                    arbitrage_is_best = False
                    
                    if is_in_peak:
                        # Is it better than solar?
                        # If solar is excess, replacement is free (0.0). If not, we'd rather keep it.
                        if gain_vs_buyback >= threshold:
                            decision_tag = "Арбитраж (Цена выгоднее выкупа)"
                            arbitrage_is_best = True
                        elif solar_is_excess:
                            decision_tag = "Продажа излишков (Солнца завтра много)"
                            arbitrage_is_best = True
                        else:
                            decision_tag = "Экономия (Солнца мало, откупа нет)"
                            arbitrage_is_best = False
                    
                    # If arbitrage is best, we can go down to base_target (e.g. 13%)
                    # If NOT, we MUST stay above ai_soc_floor_reserve (e.g. 23%)
                    if man.get_setting(CONF_DYNAMIC_SOC_SELL, True):
                        target_soc = float(base_target if arbitrage_is_best else max(base_target, ai_soc_floor_reserve))
                    else:
                        target_soc = base_target
                    
                    # Ensure global_arb_note is always consistent
                    best_buy_p, best_buy_h = get_best_buyback(cur_hour)
                    if best_buy_h is not None:
                        pot_gain = (cur_p_f - best_buy_p) * eff - deg_cost
                        global_arb_note = f"Откуп в {self._format_h(best_buy_h)} ({pot_gain:.2f})"
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
                    sim_end_h = max(24 + sunrise_h, int(active_window[1]) + 1)
                    sim_range = list(range(cur_hour, sim_end_h))
                    
                    # Use the actual calculated power_needed for the simulation, not just raw max_p
                    sim_commands = {int(h): -power_needed for h in target_hours_sorted if h >= cur_hour}
                    _, sim_log = self.run_soc_simulation(b_soc, sim_range, now, sim_commands)
                    
                    # 1. Projected SOC at START of the first peak
                    first_h_sell = min(t for t in target_hours_sorted if t >= cur_hour) if target_hours_sorted else cur_hour
                    if first_h_sell > cur_hour:
                        prev_h = first_h_sell - 1
                        key_start = f"{prev_h % 24:02d}:59" + (" (Завтра)" if prev_h >= 24 else "")
                        soc_at_start = float(sim_log.get(key_start, b_soc))
                    else:
                        soc_at_start = b_soc
                        
                    # 2. Projected SOC AFTER the last peak
                    last_h_sell = max(target_hours_sorted) if target_hours_sorted else cur_hour
                    key_after = f"{last_h_sell % 24:02d}:59" + (" (Завтра)" if last_h_sell >= 24 else "")
                    soc_after = float(sim_log.get(key_after, b_soc))
                    
                    # 3. Projected SOC TOMORROW MORNING (at Dynamic Sunrise)
                    key_morning = f"{sunrise_h-1:02d}:59 (Завтра)"
                    soc_morning = float(sim_log.get(key_morning, soc_after))

                    res["sell_simulation"] = {
                        "projected_soc_at_start_pct": float(round_f(soc_at_start, 1)),
                        "projected_soc_after_sale_pct": float(round_f(soc_after, 1)),
                        "projected_soc_morning_pct": float(round_f(soc_morning, 1))
                    }
                    
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
                        "ai_floor_soc_pct": float(round_f(ai_soc_floor_reserve, 1)),
                    }
                    if h_bb is not None and (gain_vs_buyback >= threshold):
                        res["arbitrage_buyback"]["power_kw"] = max_p
                        res["arbitrage_buyback"]["note"] = f"Откуп в {self._format_h(h_bb)} по {p_bb:.2f}"
                
            # Use current peak power only if we are actually in a peak hour
            # Otherwise show 0 as real command, but attributes will show the potential
            real_cmd_p = power_needed if (mode == "sell" and cur_hour in target_hours_sorted) else 0.0
            if mode == "buy" and cur_hour in target_hours_sorted:
                real_cmd_p = power_needed

            res["recommended_power_kw"] = float(round_f(min(float(power_needed), max_p), 3))
            res["active_hours"] = target_hours_sorted
            res["active_hours_formatted"] = ", ".join([self._format_h(h) for h in target_hours_sorted])
            res["active_periods"] = ", ".join(found_periods)
            res["target_soc"] = float(round_f(target_soc, 1))
            
            if cur_hour in target_hours_sorted and power_needed > 0.01:
                res["state"] = "active"
            elif not target_hours_sorted:
                res["state"] = "price_limit_not_met"
            else:
                if res.get("state") != "preparing_arbitrage":
                    res["state"] = "idle"
            
            self._strategy_cache[cache_key] = {"time": now, "res": res}
            return res
        finally:
            self._calculating_strategy = old_calc

