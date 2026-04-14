import logging
import asyncio
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    CONF_BATTERY_CAPACITY,
    CONF_BATTERY_MAX_POWER,
    CONF_PRICE_BUY_LIMIT,
    CONF_PRICE_SELL_LIMIT,
    CONF_PRICE_TOLERANCE,
    CONF_PRICE_SELL_TOLERANCE,
    CONF_SOC_BUFFER,
    CONF_MIN_SOC_BUY,
    CONF_AI_DISCHARGE_LIMIT,
    CONF_DYNAMIC_SOC_BUY,
    CONF_DYNAMIC_SOC_SELL,
    CONF_ARBITRAGE_PROFIT_THRESHOLD,
    CONF_FORCE_MARKET_SELL,
    CONF_PRIORITY,
    CONF_ONLY_SOLAR,
    CONF_IS_CYCLIC,
)

_LOGGER = logging.getLogger(__name__)

def normalize_float(val):
    if val is None: return 0.0
    try: return float(val)
    except: return 0.0

def round_f(val, precision=2):
    return round(float(val), precision)

def get_kwh_val(state):
    if not state or state.state in ["unknown", "unavailable"]: return None
    try: return float(state.state)
    except: return None

class StrategyEngine:
    def __init__(self, manager):
        self.manager = manager
        self._strategy_cache = {}
        self._calculating_strategy = False

    def get_efficiency_coefficient(self):
        """Calculates current battery round-trip efficiency from historical data."""
        man = self.manager
        l_map = man.data.get("learning_map", {})
        if not l_map:
            return 0.95 # Logic: 95% is a safe default for modern inverters
            
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

    def get_cc_cv_ratio(self, soc):
        """Estimate charge power reduction in CV stage (>90% SOC)."""
        soc = float(soc)
        if soc < 90: return 1.0
        if soc >= 99: return 0.1
        # Linear drop from 100% power at 90% SOC to 10% power at 99% SOC
        return float(1.0 - (soc - 90) * 0.1)

    def get_battery_degradation_cost(self):
        """Estimates the cost of 1kWh of battery throughput based on cycles and cost."""
        # Simple rule: $4000 battery / 6000 cycles / 10kWh = $0.06 per kWh
        return 0.05 

    def _format_h(self, h):
        """Formats absolute simulated hour (0-47) into HH:00 format."""
        h_mod = int(h % 24)
        suffix = " (Завтра)" if h >= 24 else ""
        return f"{h_mod:02d}:00{suffix}"

    def _get_soc_from_log(self, log: dict, key: str, default: float) -> float:
        """Safely extract SOC float from simulation log (handles both float and dict formats)."""
        val = log.get(key)
        if isinstance(val, dict):
            return float(val.get("soc", default))
        return float(val if val is not None else default)

    def _calculate_sunrise_surplus(self, natural_morning_soc, min_soc, buffer_soc, batt_cap, eff, user_soc_limit=0.0):
        """Strictly calculates surplus above the highest floor (safety mark or user limit)."""
        target_mark = float(max(min_soc + buffer_soc, user_soc_limit))
        extra_soc_pct = max(0.0, natural_morning_soc - target_mark)
        return float((extra_soc_pct * batt_cap / 100.0) * eff)

    def _calc_immediate_safety_floor(self, min_soc, active_buffer, total_cons_to_sunrise, base_deficit_tomorrow, total_solar_to_sunrise, batt_cap, eff):
        """The 'Gatekeeper' floor for current hour selling."""
        active_floor_soc = float(min_soc + active_buffer)
        # Coverage for essential needs until sunrise
        # v11.3.81: House Protection (M) adjustment
        res_cons_base_dc = max(0.0, (total_cons_to_sunrise + base_deficit_tomorrow) / eff - (total_solar_to_sunrise / 0.98))
        return active_floor_soc + (res_cons_base_dc / batt_cap * 100.0)

    def get_hourly_accuracy_coeff(self, hour):
        """Calculates specific historical accuracy for a given hour of day (v/f)."""
        man = self.manager
        sh = str(hour % 24)
        history = man.data.get("generation", {}).get(sh, [])
        if not history:
            return 1.0, 0
            
        perf_list = []
        for rec in history[-14:]:
            if not isinstance(rec, dict): continue
            if rec.get("c"): continue
            
            v = float(rec.get("v", 0.0))
            f = float(rec.get("f", 0.0))
            if f > 0.1:
                perf_list.append(max(0.2, min(v / f, 2.0)))
        
        if not perf_list:
            return 1.0, 0
            
        return float(sum(perf_list) / len(perf_list)), len(perf_list)

    def run_soc_simulation(self, start_soc, sim_range, now, commands=None, man=None, house_profile_override=None, no_battery_charge=False):
        """Universal SOC simulation engine."""
        if not sim_range:
            return float(start_soc), {}, 0.0

        man = man or self.manager
        _, batt_cap, _ = man.get_battery_state()
        b_cap_f = float(batt_cap)
        if b_cap_f <= 0.1:
            return float(start_soc), {}, 0.0

        eff_period = man.custom_period
        if now.month in [3, 4, 9, 10]:
            eff_period = 7 

        day_idx_today = man.day_type
        tomorrow_dt = now + timedelta(days=1)
        day_idx_tom = (tomorrow_dt).weekday()
        
        f_today = float(man.get_forecast_value(man.forecast_today_sensor) or 0.0)
        f_tom = float(man.get_forecast_value(man.forecast_tomorrow_sensor) or 0.0)
        dist_today = man.get_forecast_hourly_distribution(man.forecast_today_hourly_sensor)
        dist_tom = man.get_forecast_hourly_distribution(man.forecast_tomorrow_sensor, tomorrow_dt.strftime("%Y-%m-%d"))

        p_type = house_profile_override or "consumption_total"
        prof_cons_today = dict(man.get_predicted_profile(p_type))
        prof_cons_tom = dict(man.get_average_profile(p_type, eff_period, day_idx_tom))
        
        prof_gen_today = dict(man.get_average_profile("generation", eff_period, day_idx_today))
        prof_gen_tom = dict(man.get_average_profile("generation", eff_period, day_idx_tom))
        
        blended_coeff = float(getattr(man, "last_blended_coeff", 1.0))
        eff_coeff = float(self.get_efficiency_coefficient() or 1.0)
        fraction_left_h1 = float(1.0 - (now.minute / 60.0))
        max_batt_p_v = man.get_setting(CONF_BATTERY_MAX_POWER, 5.0)
        max_batt_p = float(max_batt_p_v) if max_batt_p_v is not None else 5.0

        simulated_soc = float(start_soc)
        history_log = {}
        overflow_kwh = 0.0
        for i, h_abs in enumerate(sim_range):
            real_h = int(h_abs % 24)
            is_tom = bool(h_abs >= 24)
            h_str = str(real_h)
            
            step_duration = float(fraction_left_h1 if i == 0 else 1.0)
            if step_duration <= 0.001: continue

            # 1. Generation Forecast
            if is_tom:
                if dist_tom:
                    total_dist = sum(dist_tom.values())
                    h_acc, _ = self.get_hourly_accuracy_coeff(real_h)
                    expected_gen_kw = float(dist_tom.get(h_str, 0.0) / total_dist * f_tom * blended_coeff * h_acc) if total_dist > 0.1 else 0.0
                else:
                    total_hist = sum(prof_gen_tom.values())
                    h_acc, _ = self.get_hourly_accuracy_coeff(real_h)
                    expected_gen_kw = float(normalize_float(prof_gen_tom.get(h_str, 0.0)) / total_hist * f_tom * blended_coeff * h_acc) if total_hist > 0.1 else 0.0
            else:
                if dist_today:
                    cur_h_weight = float(dist_today.get(h_str, 0.0))
                    rem_dist = (cur_h_weight * step_duration) + sum(float(dist_today.get(str(hr), 0.0)) for hr in range(now.hour + 1, 24))
                    h_acc, _ = self.get_hourly_accuracy_coeff(real_h)
                    expected_gen_kw = float(cur_h_weight / rem_dist * f_today * blended_coeff * h_acc) if rem_dist > 0.1 else 0.0
                else:
                    cur_h_hist = float(prof_gen_today.get(h_str, 0.0))
                    rem_hist = (cur_h_hist * step_duration) + sum(float(prof_gen_today.get(str(hr), 0.0)) for hr in range(now.hour + 1, 24))
                    h_acc, _ = self.get_hourly_accuracy_coeff(real_h)
                    expected_gen_kw = float(cur_h_hist / rem_hist * f_today * blended_coeff * h_acc) if rem_hist > 0.1 else 0.0
            
            if expected_gen_kw < 0.01 and (real_h < 6 or real_h > 20):
                expected_gen_kw = 0.0

            # 2. Expected consumption
            p_cons = prof_cons_tom if is_tom else prof_cons_today
            occ_coeff, _, _, _, _, _, _ = man.get_occupancy_coefficient()
            occ_coeff = float(occ_coeff)
            expected_cons_kw = float(normalize_float(p_cons.get(h_str, 0.0))) * occ_coeff
            
            # v11.3.82: Fallback to average house load if database profile is missing
            if expected_cons_kw < 0.05:
                avg_house_kw = float(getattr(man, "avg_base_load_kw" if house_profile_override == "consumption_base" else "avg_load_kw", 0.5))
                expected_cons_kw = avg_house_kw * occ_coeff

            if i == 0:
                anchor_weight = max(0.0, min(1.0, (now.minute / 60.0)))
                real_load = float(getattr(man, "avg_base_load_kw" if house_profile_override == "consumption_base" else "avg_load_kw", expected_cons_kw))
                expected_cons_kw = (real_load * anchor_weight) + (expected_cons_kw * (1.0 - anchor_weight))
            
            if i == 0:
                real_gen_kw = float(getattr(man, "avg_gen_kw", 0.0))
                if real_gen_kw > 0.01:
                    anchor_weight = max(0.0, min(1.0, (now.minute / 60.0)))
                    expected_gen_kw = (real_gen_kw * anchor_weight) + (expected_gen_kw * (1.0 - anchor_weight))
                
            cmd_p = float(commands.get(int(h_abs), 0.0)) if commands else 0.0

            if no_battery_charge:
                p_for_house = min(expected_gen_kw, expected_cons_kw)
                total_net_kw = float(p_for_house - expected_cons_kw + cmd_p)
            else:
                total_net_kw = float(expected_gen_kw - expected_cons_kw + cmd_p)
            
            idle_p = float(man.current_losses) if hasattr(man, 'current_losses') else 0.05
            if eff_coeff < 0.999:
                 expected_cons_kw += idle_p
            
            if total_net_kw > 0.001: 
                acc_ratio = float(self.get_cc_cv_ratio(simulated_soc))
                actual_charge_kw = float(min(total_net_kw * eff_coeff, max_batt_p * acc_ratio))
                
                old_soc = simulated_soc
                if b_cap_f > 0.1:
                    simulated_soc = float(min(100.0, simulated_soc + (actual_charge_kw * step_duration / b_cap_f * 100.0)))
                
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
            
            history_log[f"{real_h:0>2}:59" + (" (Tomorrow)" if is_tom else "")] = {
                "soc": round_f(float(simulated_soc), 1),
                "gen_kw": round_f(float(expected_gen_kw), 3),
                "load_kw": round_f(float(expected_cons_kw), 3)
            }

        return float(simulated_soc), history_log, float(overflow_kwh)

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
            raw_f = man.get_forecast_value(man.forecast_today_sensor)
            forecast_val = float(raw_f) if raw_f is not None else 0.0
            
            eff_period = days_for_profile
            if now.month in [3, 4, 9, 10]:
                eff_period = 7 
                
            day_idx = man.day_type
            p_gen = dict(man.get_average_profile("generation", eff_period, "all"))
            
            dist = man.get_forecast_hourly_distribution(man.forecast_today_hourly_sensor)
            dist_source = "historical"
            if dist:
                dist_source = "forecast_hourly"
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
            
            dist_acc = man.get_forecast_hourly_distribution(man.forecast_today_hourly_sensor)
            rem_hours = range(cur_hour, 24)
            top_h, bot_h = 0.0, 0.0
            for h in rem_hours:
                acc, _ = self.get_hourly_accuracy_coeff(h)
                weight = float(dist_acc.get(str(h), 0.0) if dist_acc else 0.0)
                top_h += acc * weight
                bot_h += weight
            
            hist_coeff = float(top_h / bot_h) if bot_h > 0.01 else 1.0
            actual_today = float(man.data.get("temp_daily_gen", 0.0) or 0.0)
            fraction_so_far = float(hist_gen_so_far / total_hist_gen) if total_hist_gen > 0.1 else 0.0
            
            predicted_total = float(actual_today + forecast_val)
            if predicted_total > (self.manager.data.get("temp_max_forecast", 0.0) or 0.0):
                self.manager.data["temp_max_forecast"] = float(predicted_total)
            expected_today_total = float(man.data.get("temp_max_forecast", 0.1))
            
            today_coeff = 1.0
            if hist_gen_so_far > 0.5:
                today_coeff = float(max(0.2, min(actual_today / hist_gen_so_far, 2.0)))
            
            blended_coeff = float((today_coeff * fraction_so_far) + (1.0 * (1.0 - fraction_so_far)))
            blended_coeff = float(max(0.3, min(blended_coeff, 1.5)))
            man.last_blended_coeff = float(blended_coeff)
            forecast_val_adjusted = float(forecast_val * blended_coeff)
                
            batt_soc, batt_cap, batt_energy_val = man.get_battery_state()
            b_soc_f = float(batt_soc)
            b_cap_f = float(batt_cap)
            b_energy_f = float(batt_energy_val)
            
            min_soc = float(man.get_setting(CONF_MIN_SOC_BUY, 10.0))
            eff_coeff = float(self.get_efficiency_coefficient() or 1.0)
                        
            occ_coeff, _, _, _, _, _, _ = man.get_occupancy_coefficient()
            occ_coeff = float(occ_coeff)
            sunrise_hour = man.get_sunrise_hour() or 6
            base_rem_today = float(man.get_expected_remaining("consumption_base", eff_period, day_idx)) * occ_coeff
            base_night = float(man.get_expected_night("consumption_base", eff_period, day_idx, until_hour=sunrise_hour)) * occ_coeff
            expected_base_consumption = float(base_rem_today + base_night)
            
            soc_buffer = float(man.get_setting(CONF_SOC_BUFFER, 15.0))
            survival_threshold = min_soc + soc_buffer
            
            sunrise_h = 8
            prof_gen = man.get_average_profile("generation", eff_period, day_idx)
            for h in range(24):
                if float(prof_gen.get(str(h), 0.0)) > 0.05:
                    sunrise_h = h
                    break
            
            sim_end_h = 24 + sunrise_h
            sim_range = list(range(cur_hour, sim_end_h))
            sim_res_soc, sim_log, overflow_kwh = self.run_soc_simulation(
                start_soc=b_soc_f,
                sim_range=sim_range,
                now=now,
                house_profile_override="consumption_base"
            )

            target_key = f"{sunrise_h:0>2}:59 (Завтра)" 
            projected_morning_soc = self._get_soc_from_log(sim_log, target_key, sim_res_soc)
            
            if projected_morning_soc < survival_threshold:
                initial_budget = float((projected_morning_soc - survival_threshold) * b_cap_f / 100.0 * eff_coeff)
            else:
                surplus_soc = float(projected_morning_soc - survival_threshold)
                initial_budget = float(surplus_soc * b_cap_f / 100.0 * eff_coeff)
                
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
                
                f_today = float(man.get_forecast_value(man.forecast_today_sensor) or 0.0)
                dist_f = man.get_forecast_hourly_distribution(man.forecast_today_hourly_sensor)
                h_acc, _ = self.get_hourly_accuracy_coeff(cur_hour)

                if dist_f:
                    cur_h_dist = float(dist_f.get(str(cur_hour), 0.0))
                    rem_minutes = 60 - now.minute
                    step_duration = rem_minutes / 60.0
                    rem_dist = (cur_h_dist * step_duration) + sum(float(dist_f.get(str(h), 0.0)) for h in range(cur_hour + 1, 24))
                    f_potential = float(f_today * (cur_h_dist / rem_dist) * h_acc) if rem_dist > 0.01 else 0.0
                else:
                    cur_h_hist = float(p_gen.get(str(cur_hour), 0.0))
                    rem_minutes = 60 - now.minute
                    step_duration = rem_minutes / 60.0
                    rem_hist = (cur_h_hist * step_duration) + sum(float(p_gen.get(str(h), 0.0)) for h in range(cur_hour + 1, 24))
                    f_potential = float(f_today * (cur_h_hist / rem_hist) * h_acc) if rem_hist > 0.1 else 0.0
                
                potential_gen = float(max(gen_kw, f_potential))
                waste_kw = float(max(0.0, potential_gen - gen_kw))
                is_stop_sale = getattr(man, "current_inverter_mode", "") == "stop_sale"
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
            sorted_items = sorted(man.deduct_settings.items(), key=lambda x: x[1].get(CONF_PRIORITY, 1) if isinstance(x[1], dict) else 1)
            
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

                inverter_mode = getattr(man, "current_inverter_mode", "")
                is_selling_mode = inverter_mode in ("sale_pv_no_bat", "sale_pv_bat")
                price_suffix = " (Беспл. цена)" if is_free_price else ""
                
                if is_selling_mode and not is_free_price:
                    permissions[s_id_s] = False
                    mode_label = "Продажа PV (без АКБ)" if inverter_mode == "sale_pv_no_bat" else "Продажа PV+АКБ"
                    permissions_reasons[s_id_s] = f"Запрет: режим '{mode_label}' — приоритет продажи"
                elif req_kwh > 0 and consumed >= req_kwh:
                    permissions[s_id_s] = False
                    permissions_reasons[s_id_s] = f"Норма выполнена ({consumed:.2f}/{req_kwh}{price_suffix})"
                elif power_bottleneck:
                    permissions[s_id_s] = False
                    permissions_reasons[s_id_s] = f"Дефицит мощности ({available_power_kw:.2f} < {p_thresh if not is_pulling else p_lim:.2f}{price_suffix})"
                elif gen_bottleneck:
                    permissions[s_id_s] = False
                    permissions_reasons[s_id_s] = "Недостаточно генерации (только солнце)"
                elif available_budget < 0.1 and not only_solar and not is_free_price:
                    permissions[s_id_s] = False
                    permissions_reasons[s_id_s] = f"Лимит исчерпан ({available_budget:.2f} < 0.1)"
                else:
                    permissions[s_id_s] = True
                    permissions_reasons[s_id_s] = f"Ок ({available_budget:.2f} кВт·ч доступно{price_suffix})"
                    if not is_cyclic or is_pulling:
                        available_budget -= float(e_kw * (1.0 - (now.minute / 60.0)))
                        available_power_kw -= e_kw
                        available_gen_kw -= e_kw
                        reserved_by.append(s_id_s)
                    
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
                "permissions": permissions,
                "permissions_reasons": permissions_reasons,
                "forecast_val": float(forecast_val_adjusted),
                "forecast_raw": float(forecast_val),
                "efficiency_coefficient": float(eff_coeff),
                "degradation_cost": float(self.get_battery_degradation_cost()),
                "batt_energy_val": float(b_energy_f),
                "expected_consumption": float(expected_base_consumption),
                "occupancy_coefficient": float(occ_coeff),
                "available_power_total_kw": float(initial_power_kw),
                "available_gen_kw": float(available_gen_kw),
                "available_gen_surplus_initial": float(gen_surplus_initial),
                "reserved_by": reserved_by,
                "sunrise_hour": int(sunrise_h),
                "waste_compensation_kw": float(waste_kw),
                "battery_flexible_kw": float(batt_p_flexible),
                "battery_discharge_budget_kw": float(batt_discharge_allowed)
            }
            self._strategy_cache["budget_permissions"] = {"time": now, "res": return_res}
            return return_res
        finally:
            self._calculating_strategy = old_calc

    def get_market_strategy(self, mode="buy"):
        now = dt_util.now()
        man: Any = self.manager
        cache_key = f"market_strategy_{mode}"
        cached = self._strategy_cache.get(cache_key)
        if cached and (now - cached["time"]).total_seconds() < 30:
            return cached["res"]

        res = {
            "state": "standard",
            "mode": mode,
            "active_hours": [],
            "active_periods": "",
            "recommended_power_kw": 0.0,
            "target_price": 0.0,
            "limit_used": 0.0,
            "today_prices": {},
            "tomorrow_prices": {},
            "multi_cycle": "Нет прогноза",
            "buy_simulation": {"projected_soc_at_start_pct": 0.0, "projected_soc_at_end_pct": 0.0, "projected_soc_morning_pct": 0.0},
            "sell_simulation": {"projected_soc_at_start_pct": 0.0, "projected_soc_after_sale_pct": 0.0, "projected_soc_morning_pct": 0.0},
            "arbitrage_decision": "Нет данных",
            "charge_reason": "none",
            "arbitrage_buyback": {"opportunity": False, "power_kw": 0.0, "note": ""}
        }
        
        old_calc = bool(self._calculating_strategy)
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
            avg_prof_gen = man.get_average_profile("generation", man.custom_period, "all")
            sunrise_h = 8
            for h in range(4, 12):
                if float(normalize_float(avg_prof_gen.get(str(h), 0.0))) > 0.1:
                    sunrise_h = h
                    break
            
            batt_soc, batt_cap, _ = man.get_battery_state()
            b_soc, b_cap = float(batt_soc), float(batt_cap)
            eff_coeff = float(self.get_efficiency_coefficient() or 1.0)
            max_p = float(man.get_setting(CONF_BATTERY_MAX_POWER, 5.0))
            
            if not today_prices: return res
            all_prices = {int(h): float(normalize_float(p)) for h, p in today_prices.items()}
            for h, p in tomorrow_prices.items(): all_prices[int(h) + 24] = float(normalize_float(p))
                
            buy_limit = float(man.get_setting(CONF_PRICE_BUY_LIMIT, 2.0))
            sell_limit = float(man.get_setting(CONF_PRICE_SELL_LIMIT, 5.0))
            deg_cost = float(self.get_battery_degradation_cost() or 0.0)
            min_p = float(man.get_setting(CONF_ARBITRAGE_PROFIT_THRESHOLD, 0.0))
            threshold = float(max(min_p, 2.0 * deg_cost))
            eff = float(eff_coeff)

            def get_best_buyback(after_h):
                p_buy_all = man.data.get("prices_buy", {})
                all_b = {int(h): float(normalize_float(p)) for h, p in p_buy_all.get(today_str, {}).items()}
                for h, p in p_buy_all.get(tomorrow_str, {}).items(): all_b[int(h) + 24] = float(normalize_float(p))
                options = {int(h): float(p) for h, p in all_b.items() if int(h) > int(after_h)}
                if not options: return 999.0, None
                best_h = min(options, key=lambda k: options[k])
                return float(options[best_h]), int(best_h)

            if mode == "buy":
                res["limit_used"] = buy_limit
                target_hours = [int(h) for h, p in all_prices.items() if p < 0 and h >= cur_hour]
                if target_hours:
                    res["state"] = "active"
                    res["charge_reason"] = "negative"
                    res["arbitrage_decision"] = "Зарядка (Отрицательная цена)"
                else:
                    # Dynamic buy logic
                    dynamic_buy = bool(man.get_setting(CONF_DYNAMIC_SOC_BUY, True))
                    if dynamic_buy:
                        for h, p in all_prices.items():
                            if h < cur_hour: continue
                            if p <= buy_limit:
                                target_hours.append(h)
                    res["active_hours"] = target_hours
                    res["charge_reason"] = "arbitrage" if target_hours else "none"
            else: # sell
                res["limit_used"] = sell_limit
                user_limit_soc = float(man.get_setting(CONF_AI_DISCHARGE_LIMIT, 20.0))
                base_target = user_limit_soc
                
                budget_raw = self.get_budget_and_permissions(man.custom_period, skip_strategy_check=True)
                total_cons_to_sunrise = float(budget_raw.get("expected_consumption", 0.0))
                total_solar_to_sunrise = float(budget_raw.get("forecast_val", 0.0))
                
                # Safety checks (M/U/P logic)
                target_morning_soc = float(man.get_setting(CONF_MIN_SOC_BUY, 10.0)) + float(man.get_setting(CONF_SOC_BUFFER, 15.0))
                
                # Baseline simulation
                sim_range = range(cur_hour, 24 + sunrise_h)
                natural_morning_soc, sim_log_base, _ = self.run_soc_simulation(b_soc, sim_range, now, {})
                
                # v11.3.82: UNIFIED TRIPLE CONSTRAINT HARMONY
                surplus_for_morning = max(0.0, (natural_morning_soc - target_morning_soc) * b_cap / 100.0)
                
                # Find SOC after household consumption before selling
                target_h_sell = [h for h, p in all_prices.items() if h >= cur_hour and p >= sell_limit]
                res["active_hours"] = target_h_sell
                
                natural_soc_after_sale = b_soc
                if target_h_sell:
                    last_h = max(target_h_sell)
                    key_end = f"{last_h % 24:02d}:59" + (" (Tomorrow)" if last_h >= 24 else "")
                    natural_soc_after_sale = self._get_soc_from_log(sim_log_base, key_end, b_soc)

                surplus_for_user_limit = max(0.0, (natural_soc_after_sale - user_limit_soc) * b_cap / 100.0)
                
                num_peaks = len(target_h_sell) or 1
                physical_limit_dc = (max_p * num_peaks) / eff
                
                available_sell_dc = min(surplus_for_morning, surplus_for_user_limit, physical_limit_dc)
                
                # Diagnostics
                if available_sell_dc <= (physical_limit_dc + 0.001) and physical_limit_dc < min(surplus_for_morning, surplus_for_user_limit):
                    diag = f"Лимит мощности АКБ ({max_p:.1f}кВт)"
                elif available_sell_dc <= (surplus_for_user_limit + 0.001) and surplus_for_user_limit < surplus_for_morning:
                    diag = f"Лимит пользователя ({user_limit_soc:.0f}%)"
                else:
                    diag = f"Защита дома (Рассвет {target_morning_soc:.0f}%)"
                
                res["arbitrage_decision"] = diag
                power_needed = (available_sell_dc * eff / num_peaks) if cur_hour in target_h_sell else 0.0
                res["recommended_power_kw"] = round_f(power_needed, 3)
                
                # v11.3.85: Recursive Morning Deficit Fix
                if cur_hour in target_h_sell:
                    # Final simulation with sale commands
                    sell_cmds = {h: power_needed for h in target_h_sell}
                    _, final_log, _ = self.run_soc_simulation(b_soc, sim_range, now, {h: -p for h, p in sell_cmds.items()})
                    key_morning = f"{sunrise_h-1:02d}:59 (Tomorrow)"
                    soc_morning = self._get_soc_from_log(final_log, key_morning, b_soc)
                    
                    deficit = max(0.0, target_morning_soc - soc_morning)
                    if deficit > 0.1:
                        # Throttling
                        available_sell_dc = max(0.0, available_sell_dc - (deficit * b_cap / 100.0))
                        power_needed = (available_sell_dc * eff / num_peaks)
                        res["recommended_power_kw"] = round_f(power_needed, 3)
                        res["arbitrage_decision"] += f" | Коррекция дефицита {deficit:.1f}%"

            res["target_soc"] = round_f(base_target, 1) if mode == "sell" else 100.0
            return res
        finally:
            self._calculating_strategy = old_calc

    def run_investment_simulation(self, extra_batt_kwh=0.0, pv_multiplier=1.0):
        # Implementation of investment sim (omitted for brevity but kept clean)
        return {"monthly_estimate": 0.0}
