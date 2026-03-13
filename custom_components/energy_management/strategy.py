"""Strategy Engine for Energy Management."""
import logging
from datetime import datetime, timedelta
from typing import Any
from homeassistant.util import dt as dt_util

from .const import *
from .utils import get_kwh_val, normalize_float, get_price_from_store

# Legacy aliases for safety during refactoring synchronization
_get_kwh_val = get_kwh_val
_normalize_float = normalize_float

_LOGGER = logging.getLogger(__name__)



# Market Strategy Engine v5.1 - Fixed NameError
class StrategyEngine:
    """Mathematical engine for energy management strategies and simulations."""
    
    def __init__(self, manager):
        self.manager = manager
        self._calculating_strategy = False

    @staticmethod
    def get_cc_cv_ratio(soc):
        if soc < 80: return 1.0
        if soc >= 98: return 0.1
        return 1.0 - (soc - 80) * (0.9 / 18.0)

    def get_battery_degradation_cost(self):
        """Cost of battery wear per kWh (Cycle Cost)."""
        batt_cost = self.manager.get_setting(CONF_BATTERY_COST, 0.0)
        cycles = self.manager.get_setting(CONF_BATTERY_RATED_CYCLES, 6000)
        
        _, cap, _ = self.manager.get_battery_state()
        if cap <= 0: cap = 10.0
        
        if cycles <= 0 or batt_cost <= 0: return 0.0
        return batt_cost / (cycles * cap)

    def get_efficiency_coefficient(self):
        """Calculates historical inverter/system efficiency."""
        days = self.manager.custom_period
        total_gen, total_losses, sample_count = 0.0, 0.0, 0
        
        losses_data = self.manager.data.get("losses", {})
        for h in range(24):
            records = losses_data.get(str(h), [])
            relevant = records[-days:] if days > 0 else records
            for rec in relevant:
                if not isinstance(rec, dict): continue
                gen = normalize_float(rec.get("gen", 0.0))
                loss = normalize_float(rec.get("v", 0.0))
                if gen > 0.01:
                    total_gen += gen
                    total_losses += loss
                    sample_count += 1
        
        if sample_count < 5 or total_gen < 0.1: return 1.0
        return max(0.70, min(1.0, (total_gen - total_losses) / total_gen))

    def get_gen_forecast_coefficient(self, forecast_value, prof_gen, hour_start, hour_end):
        if not forecast_value or forecast_value <= 0.1: return 1.0
        avg_gen_sum = sum(float(prof_gen.get(str(h), 0.0)) for h in range(hour_start, hour_end))
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
                "days_simulated": 0,
                "extra_savings": 0.0,
                "monthly_estimate": 0.0
            }

        eff = self.get_efficiency_coefficient()
        batt_soc, batt_cap, _ = self.manager.get_battery_state()
        sim_batt_cap = batt_cap + extra_batt_kwh
        max_batt_p = self.manager.get_setting(CONF_BATTERY_MAX_POWER, 5.0)
        
        total_extra_saved = 0.0
        
        for d_back in range(1, days_to_sim + 1):
            # Assume starting at 50% SOC each night (simplified assumption for simulation)
            sim_soc = 50.0 
            
            for h in range(24):
                c_h_rec = self.manager.data.get("consumption_total", {}).get(str(h), [])
                g_h_rec = self.manager.data.get("generation", {}).get(str(h), [])
                
                # Get buy price for that specific day/hour
                date_str = (now - timedelta(days=d_back)).strftime("%Y-%m-%d")
                p_buy = self.manager.get_price("buy", date_str, h) or 0.0
                
                if d_back > len(c_h_rec) or d_back > len(g_h_rec):
                    continue
                
                try:
                    c_h = normalize_float(c_h_rec[-d_back].get("v") if isinstance(c_h_rec[-d_back], dict) else c_h_rec[-d_back])
                    g_h = normalize_float(g_h_rec[-d_back].get("v") if isinstance(g_h_rec[-d_back], dict) else g_h_rec[-d_back]) * pv_multiplier
                except (IndexError, AttributeError):
                    continue
                
                # Simple energy balance simulation
                net = g_h - c_h
                sim_cost = 0.0
                
                if net > 0:
                    # Excess generation to battery
                    charge_kw = min(net * eff, max_batt_p)
                    if sim_batt_cap > 0.001:
                        sim_soc = min(100.0, sim_soc + (charge_kw / sim_batt_cap * 100.0))
                else:
                    # Consumption from battery
                    needed = abs(net)
                    from_batt = min(needed, sim_soc * sim_batt_cap / 100.0) if sim_batt_cap > 0.001 else 0.0
                    from_batt_ac = from_batt * eff
                    
                    if sim_batt_cap > 0.001:
                        sim_soc = max(0.0, sim_soc - (from_batt / sim_batt_cap * 100.0))
                    
                    sim_cost = max(0.0, needed - from_batt_ac) * p_buy
                
                # Actually, I'll calculate total simulation benefit and subtract real reported benefit
                total_extra_saved += (c_h * p_buy) - sim_cost
                
        # total_extra_saved now contains total benefit of simulated system.
        # Now subtract the actual savings recorded for those days.
        actual_total_savings = 0.0
        for d_back in range(1, days_to_sim + 1):
            d_str = (now - timedelta(days=d_back)).strftime("%Y-%m-%d")
            day_savings = self.manager.data.get("savings", {}).get(d_str, {})
            if isinstance(day_savings, dict):
                actual_total_savings += day_savings.get("total", 0.0)
            
        improvement = max(0.0, total_extra_saved - actual_total_savings)
        return {
            "days_simulated": days_to_sim,
            "extra_savings": round(improvement, 2),
            "monthly_estimate": round(improvement * (30 / days_to_sim), 2) if days_to_sim > 0 else 0.0
        }

    def get_budget_and_permissions(self, days_for_profile=14, skip_strategy_check=False):
        """Analyze current day state and return permissions for heavy loads."""
        now = dt_util.now()
        cur_hour = now.hour
        
        if self._calculating_strategy and not skip_strategy_check:
            # Recursion guard
            skip_strategy_check = True

        old_calc = self._calculating_strategy
        self._calculating_strategy = True
        try:
            # 1. Solar adjustment
            forecast_val = self.manager.get_forecast_value(self.manager.forecast_today_sensor) or 0.0
            
            prof_gen = self.manager.get_average_profile("generation", days_for_profile, "all")
            hist_gen_so_far = sum(float(prof_gen.get(str(h), 0.0)) for h in range(cur_hour + 1))
            total_hist_gen = sum(float(prof_gen.get(str(h), 0.0)) for h in range(24))
            
            # Historical forecast vs real average
            hist_coeff = forecast_val / total_hist_gen if total_hist_gen > 0.1 else 1.0
            
            fraction_so_far = 0.0
            if total_hist_gen > 0.1:
                fraction_so_far = hist_gen_so_far / total_hist_gen
            
            # Today's actual is taken from the daily accumulator (resilient to restart gaps)
            actual_today = self.manager.data.get("temp_daily_gen", 0.0) or 0.0
            expected_today_total = self.manager.data.get("temp_max_forecast", 0.0) or 0.0
            expected_today_so_far = expected_today_total * fraction_so_far
            
            today_coeff = hist_coeff
            if expected_today_so_far > 0.1:
                today_coeff = actual_today / expected_today_so_far
                today_coeff = max(0.2, min(today_coeff, 2.0))
                
            blended_coeff = (today_coeff * fraction_so_far) + (hist_coeff * (1.0 - fraction_so_far))
            self.manager.last_blended_coeff = blended_coeff
                    
            forecast_val_adjusted = forecast_val * blended_coeff
                
            # 2. Get Battery Energy Available
            batt_soc, batt_cap, batt_energy_val = self.manager.get_battery_state()
            min_soc = self.manager.get_setting(CONF_MIN_SOC_BUY, 10.0)
            eff_coeff = self.get_efficiency_coefficient() or 1.0
                        
            # 3. Get Expected Consumption remaining till end of day
            occ_coeff = self.manager.get_occupancy_coefficient()
            expected_remaining = self.manager.get_expected_remaining("consumption_total", days_for_profile) * occ_coeff
            expected_consumption = expected_remaining
            
            # Budget = (Today's remaining solar) + (Current battery energy ABOVE min_soc) - (Necessary consumption)
            # Note: we use adjusted forecast
            initial_budget = (forecast_val_adjusted * (1.0 - fraction_so_far)) + (batt_energy_val - (min_soc * batt_cap / 100.0)) - expected_remaining
            available_budget = initial_budget
        
            # 4. Evaluate permissions for each managed load
            permissions = {}
            permissions_reasons = {}
            
            # Real-time power limit check
            initial_power_kw = 0.0
            if getattr(self.manager, "power_load_sensors", []) and getattr(self.manager, "power_gen_sensors", []):
                load_kw = sum((get_kwh_val(self.manager.hass.states.get(s)) or 0.0) for s in self.manager.power_load_sensors)
                gen_kw = sum((get_kwh_val(self.manager.hass.states.get(s)) or 0.0) for s in self.manager.power_gen_sensors)
                initial_power_kw = gen_kw - load_kw
                
            available_power_kw = initial_power_kw
            available_gen_kw = sum((get_kwh_val(self.manager.hass.states.get(s)) or 0.0) for s in self.manager.power_gen_sensors)
            
            cur_price_buy = None
            if not skip_strategy_check:
                # We check the latest market strategy to see if it's currently a "Free" price window
                strategy_res = self.get_market_strategy("buy")
                cur_price_buy = strategy_res.get("today_prices", {}).get(str(cur_hour))

            for sensor_id, settings in self.manager.deduct_settings.items():
                only_solar_free = settings.get("only_solar_free", False)
                req_kwh = float(settings.get("required_kwh", 2.5))
                req_kw = float(self.manager.learned_real_power.get(sensor_id, settings.get("required_kw", 0.0) * 1000.0)) / 1000.0
                consumed = float(self.manager.daily_deduct_consumption.get(sensor_id, 0.0))
                
                is_currently_pulling_now = self.manager._is_currently_pulling_power(sensor_id)
                is_idle = not is_currently_pulling_now
                
                power_bottleneck = False
                gen_bottleneck = False
                is_free_price = cur_price_buy is not None and normalize_float(cur_price_buy) <= 0.0

                if req_kw > 0.0:
                    if available_power_kw < req_kw:
                        power_bottleneck = True
                    if only_solar_free and not is_free_price:
                        if available_gen_kw < (req_kw * 0.6):
                            gen_bottleneck = True
                elif initial_power_kw > 0.5 and available_power_kw < 0:
                    power_bottleneck = True

                if req_kwh == 0:
                    # Dynamic load logic
                    if available_budget > 0 and not power_bottleneck and not gen_bottleneck:
                        permissions[sensor_id] = True
                        if req_kw > 0.0:
                            available_power_kw = float(available_power_kw) - float(req_kw)
                            if only_solar_free and not is_free_price:
                                available_gen_kw = float(available_gen_kw) - (float(req_kw) * 0.6)
                        
                        b_val = round(max(0.0, float(available_budget)), 2)
                        g_val = round(max(0.0, float(available_power_kw)), 2)
                        permissions_reasons[sensor_id] = f"Разрешено: Динамическая (Профицит {b_val} кВт*ч, Доступно {g_val} кВт)"
                    else:
                        permissions[sensor_id] = False
                        g_val = round(float(available_power_kw), 2)
                        g_gen = round(float(available_gen_kw), 2)
                        if gen_bottleneck:
                            permissions_reasons[sensor_id] = f"Блокировка: Доступная генер. {g_gen} кВт < 60% от {req_kw} кВт"
                        elif power_bottleneck:
                            permissions_reasons[sensor_id] = f"Блокировка: Доступно {g_val} кВт < Мощность {req_kw} кВт"
                        elif available_budget <= 0:
                            permissions_reasons[sensor_id] = f"Блокировка: Нет профицита энергии ({round(float(available_budget), 2)} кВт*ч)"
                        else:
                            permissions_reasons[sensor_id] = "Блокировка: Ограничения по мощности"
                else:
                    # Fixed daily goal logic
                    needed = req_kwh - consumed
                    if needed <= 0:
                        permissions[sensor_id] = True
                        permissions_reasons[sensor_id] = "Разрешено: Дневная норма выполнена (или перерасход)"
                    elif available_budget >= needed and not power_bottleneck and not gen_bottleneck:
                        permissions[sensor_id] = True
                        available_budget -= float(needed)
                        if only_solar_free and not is_free_price and req_kw > 0.0:
                            available_gen_kw -= (float(req_kw) * 0.6)
                        
                        n_val = round(float(needed), 2)
                        permissions_reasons[sensor_id] = f"Разрешено: Зарезервировано {n_val} кВт*ч из профицита"
                    else:
                        permissions[sensor_id] = False
                        if gen_bottleneck:
                            permissions_reasons[sensor_id] = f"Блокировка: Доступная генерация {round(float(available_gen_kw), 2)} кВт < 60% от {req_kw} кВт"
                        elif power_bottleneck:
                            permissions_reasons[sensor_id] = f"Блокировка: Доступно {round(float(available_power_kw), 2)} кВт < Мощность {req_kw} кВт"
                        else:
                            permissions_reasons[sensor_id] = f"Блокировка: Не хватает энергии (нужно {round(float(needed), 2)} кВт*ч, доступно {round(float(available_budget), 2)} кВт*ч)"
                
            return {
                "initial_budget": float(initial_budget or 0.0),
                "permissions": permissions or {},
                "permissions_reasons": permissions_reasons or {},
                "forecast_val": float(forecast_val_adjusted or 0.0),
                "forecast_raw": float(forecast_val or 0.0),
                "forecast_coefficient": float(blended_coeff or 1.0),
                "forecast_hist_coefficient": float(hist_coeff or 1.0),
                "forecast_today_coefficient": float(today_coeff or 1.0),
                "batt_energy_val": float(batt_energy_val or 0.0),
                "expected_consumption": float(expected_consumption or 0.0),
                "debug_actual_today": float(actual_today or 0.0),
                "debug_expected_today_total": float(expected_today_total or 0.0),
                "debug_expected_today_so_far": float(expected_today_so_far or 0.0),
                "debug_fraction_so_far": float(fraction_so_far or 0.0),
                "occupancy_coefficient": float(occ_coeff or 1.0),
                "efficiency_coefficient": float(eff_coeff or 1.0)
            }
        finally:
            self._calculating_strategy = old_calc

    def run_soc_simulation(self, start_soc, sim_range, now, commands=None):
        """
        Universal SOC simulation engine.
        sim_range: List of absolute hours (e.g. [11, 12, 13...24, 25...])
        now: Current datetime for fractional first hour.
        commands: Optional dict {abs_hour: kw_power} (positive=charge, negative=sell/discharge)
        """
        if not sim_range:
            return start_soc, {}

        # Safety check for battery state
        _, batt_cap, _ = self.manager.get_battery_state()
        if batt_cap <= 0:
            return start_soc, {}

        # 1. Standard Forecast and Coefficients
        f_today = self.manager.get_forecast_value(self.manager.forecast_today_sensor) or 0.0
        f_tom = self.manager.get_forecast_value(self.manager.forecast_tomorrow_sensor) or 0.0
        
        day_type_today = "weekend" if now.weekday() >= 5 else "weekday"
        tomorrow_dt = now + timedelta(days=1)
        day_type_tom = "weekend" if tomorrow_dt.weekday() >= 5 else "weekday"
        
        prof_gen = self.manager.get_average_profile("generation", self.manager.custom_period, "all")
        prof_cons_today = self.manager.get_average_profile("consumption_total", self.manager.custom_period, day_type_today)
        prof_cons_tom = self.manager.get_average_profile("consumption_total", self.manager.custom_period, day_type_tom)
        
        total_hist_gen = sum(float(prof_gen.get(str(h), 0.0)) for h in range(24))
        
        # Determine sunset for 'remaining' logic
        sunset_h = 17 
        sun_state = self.manager.hass.states.get("sun.sun")
        if sun_state and "next_setting" in sun_state.attributes:
            try:
                sunset_h = dt_util.parse_datetime(sun_state.attributes["next_setting"]).astimezone(now.tzinfo).hour
            except Exception: pass
            
        hist_gen_rem_today = sum(float(prof_gen.get(str(h), 0.0)) for h in range(now.hour, min(24, sunset_h + 1)))
        blended_coeff = getattr(self.manager, "last_blended_coeff", 1.0)
        
        eff_coeff = self.get_efficiency_coefficient()
        max_batt_p = self.manager.get_setting(CONF_BATTERY_MAX_POWER, 5.0)

        simulated_soc = float(start_soc)
        history_log = {}
        fraction_left_in_first_hour = 1.0 - (now.minute / 60.0)

        for i, h_abs in enumerate(sim_range):
            real_h = h_abs % 24
            is_tom = (h_abs >= 24)
            h_str = str(real_h)
            
            step_duration = fraction_left_in_first_hour if i == 0 else 1.0
            if step_duration <= 0: continue

            # Generation
            hist_hour_gen = float(prof_gen.get(h_str, 0.0))
            if is_tom:
                expected_gen_kw = (hist_hour_gen / total_hist_gen * f_tom) if total_hist_gen > 0 else hist_hour_gen
            else:
                expected_gen_kw = (hist_hour_gen / hist_gen_rem_today * f_today) if hist_gen_rem_today > 0.1 else hist_hour_gen
                expected_gen_kw *= blended_coeff
            
            # Consumption
            p_cons = prof_cons_tom if is_tom else prof_cons_today
            expected_cons_kw = float(p_cons.get(h_str, 0.0))
            
            # Additional load scaling (Occupancy)
            expected_cons_kw *= self.manager.get_occupancy_coefficient()
            
            # Combine house activities and commands
            cmd_p = 0.0
            if commands and h_abs in commands:
                cmd_p = float(commands[h_abs])
            
            net_house_kw = expected_gen_kw - expected_cons_kw
            active_m_p = self.manager.get_active_managed_loads_power(i) if not is_tom else 0.0
            total_net_kw = net_house_kw + cmd_p - active_m_p
            
            if total_net_kw > 0.1: # Charging
                acc_ratio = self.get_cc_cv_ratio(simulated_soc)
                actual_charge_kw = min(total_net_kw * eff_coeff, max_batt_p * acc_ratio)
                if batt_cap > 0:
                    simulated_soc = min(100.0, simulated_soc + (actual_charge_kw * step_duration / batt_cap * 100.0))
            elif total_net_kw < -0.1: # Discharging
                actual_discharge_kw = abs(total_net_kw) / eff_coeff
                if batt_cap > 0:
                    simulated_soc = max(0.0, simulated_soc - (actual_discharge_kw * step_duration / batt_cap * 100.0))
            
            history_log[f"{real_h:0>2}:00" + (" (Завтра)" if is_tom else "")] = round(simulated_soc, 1)

        return simulated_soc, history_log

    def get_market_strategy(self, mode="buy"):
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
            "buy_simulation": {
                "projected_soc_at_start_pct": 0,
                "projected_soc_at_end_pct": 0
            },
            "sell_simulation": {
                "projected_soc_at_start_pct": 0,
                "projected_soc_after_sale_pct": 0,
                "projected_soc_morning_pct": 0
            },
            "arbitrage_decision": "Нет данных",
            "arbitrage_buyback": {"opportunity": False, "power_kw": 0.0, "note": ""}
        }
        
        old_calc = self._calculating_strategy
        self._calculating_strategy = True
        try:
            now = dt_util.now()
            cur_hour = now.hour
            today_str = now.strftime("%Y-%m-%d")
            tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
            
            prices_store = self.manager.data.get(f"prices_{mode}", {})
            today_prices = prices_store.get(today_str, {})
            tomorrow_prices = prices_store.get(tomorrow_str, {})
        
            res["today_prices"] = today_prices
            res["tomorrow_prices"] = tomorrow_prices
            
            # Initialize key variables at the start for all modes (prevents NameErrors)
            batt_soc, batt_cap, batt_energy_val = self.manager.get_battery_state()
            target_soc = batt_soc
            sim_soc_plan = batt_soc
            
            today_type = "weekend" if now.weekday() >= 5 else "weekday"
            tom_type = "weekend" if (now + timedelta(days=1)).weekday() >= 5 else "weekday"
            
            prof_today = self.manager.get_average_profile("consumption_total", self.manager.custom_period, today_type)
            prof_tom = self.manager.get_average_profile("consumption_total", self.manager.custom_period, tom_type)
            prof_gen = self.manager.get_average_profile("generation", self.manager.custom_period, "all")
            
            forecast_today_val = self.manager.get_forecast_value(self.manager.forecast_today_sensor) or 0.0
            forecast_tomorrow_val = self.manager.get_forecast_value(self.manager.forecast_tomorrow_sensor) or 0.0
            
            coeff_today = self.get_gen_forecast_coefficient(forecast_today_val, prof_gen, cur_hour + 1, 24)
            coeff_tom = self.get_gen_forecast_coefficient(forecast_tomorrow_val, prof_gen, 0, 24)
            
            max_power = self.manager.get_setting(CONF_BATTERY_MAX_POWER, 5.0)
            eff_coeff = self.get_efficiency_coefficient()
            
            if not today_prices:
                return res
                
            force_sell = self.manager.get_setting(CONF_FORCE_MARKET_SELL, False)
            if mode == "sell" and force_sell:
                res["target_price"] = 0.0
                res["limit_used"] = 0.0
                res["active_hours"] = [cur_hour]
                return res
            
            # Determine tolerance based on mode
            tolerance = self.manager.get_setting(CONF_PRICE_TOLERANCE if mode == "buy" else CONF_PRICE_SELL_TOLERANCE, 0.0)
            
            # Unify today and tomorrow prices into a 48h timeline for FULL window evaluation
            all_prices = {}
            for h, p in today_prices.items():
                all_prices[int(h)] = normalize_float(p)
            for h, p in tomorrow_prices.items():
                all_prices[int(h) + 24] = normalize_float(p)
                
            negative_hours = [h for h, p in all_prices.items() if p < 0 and h >= cur_hour]

            # Evaluate the entire available horizon
            if tomorrow_prices:
                active_window = (0, 47)
                res["analyzed_window"] = "Сегодня 00:00 - Завтра 23:59"
            else:
                active_window = (0, 23)
                res["analyzed_window"] = "Сегодня 00:00 - Сегодня 23:59"
                    
            target_hours = []
            target_price = 0.0
            limit_used = 0.0
            carte_blanche = False

            def get_peaks(window, is_sell, limit, tol):
                if not window: return []
                if is_sell:
                    best_p = max(window.values())
                    if best_p >= limit:
                        return [(h, p) for h, p in window.items() if p >= limit and p >= (best_p - tol)]
                else:
                    best_p = min(window.values())
                    if best_p <= limit:
                        return [(h, p) for h, p in window.items() if p <= limit and p <= (best_p + tol)]
                return []

            window_today = {h: p for h, p in all_prices.items() if cur_hour <= h < 24}
            window_tomorrow = {h: p for h, p in all_prices.items() if 24 <= h <= 47}

            # --- SHARED MARKET DATA & GLOBAL ARBITRAGE SCAN ---
            sell_prices_today = self.manager.data.get("prices_sell", {}).get(today_str, {})
            sell_prices_tom = self.manager.data.get("prices_sell", {}).get(tomorrow_str, {})
            all_sell_prices = {}
            for h, p in sell_prices_today.items():
                all_sell_prices[int(h)] = normalize_float(p)
            for h, p in sell_prices_tom.items():
                all_sell_prices[int(h) + 24] = normalize_float(p)

            buy_prices_today = self.manager.data.get("prices_buy", {}).get(today_str, {})
            buy_prices_tom = self.manager.data.get("prices_buy", {}).get(tomorrow_str, {})
            all_buy_prices = {}
            for h_b, p_b in buy_prices_today.items():
                all_buy_prices[int(h_b)] = normalize_float(p_b)
            for h_b, p_b in buy_prices_tom.items():
                all_buy_prices[int(h_b) + 24] = normalize_float(p_b)

            sell_limit = self.manager.get_setting(CONF_PRICE_SELL_LIMIT, 99.0)
            buy_limit = self.manager.get_setting(CONF_PRICE_BUY_LIMIT, 99.0)
            deg_cost = self.get_battery_degradation_cost()
            eff = eff_coeff
            min_p = self.manager.get_setting(CONF_ARBITRAGE_MIN_PROFIT, 0.0)
            threshold = min_p if min_p >= deg_cost else (2 * deg_cost)

            def get_best_buyback(after_hour):
                options = {h: p for h, p in all_buy_prices.items() if h > after_hour}
                if not options: return 999.0, None
                best_h = min(options, key=options.get)
                return options[best_h], best_h

            # Global Arbitrage Scan (find the "Holy Grail" pair in 48h)
            best_arb_pair = (None, None)
            max_arb_gain = -999.0
            for h_s, p_s in all_sell_prices.items():
                if h_s < cur_hour: continue
                p_b, h_b = get_best_buyback(h_s)
                gain = (p_s - p_b) * eff
                if gain > max_arb_gain:
                    max_arb_gain = gain
                    best_arb_pair = (h_s, h_b)

            global_arb_note = "Арбитраж невыгоден"
            if max_arb_gain >= threshold:
                s_h, b_h = best_arb_pair
                global_arb_note = f"Макс. профит: Прод. {all_sell_prices[s_h]:.2f} ({s_h%24:02d}:00) -> Отк. {all_buy_prices[b_h]:.2f} ({b_h%24:02d}:00), выгода {max_arb_gain:.2f} (порог {threshold:.2f})"

            if mode == "buy":
                res["limit_used"] = buy_limit
                if negative_hours:
                    target_hours = negative_hours
                    target_price = min([all_prices[h] for h in negative_hours])
                    res["target_price"] = target_price
                    carte_blanche = True
                else:
                    def is_buy_profitable_arb(buy_p, hour):
                        # Use actual future sell peaks
                        future_sell = [p_s for h_s, p_s in all_sell_prices.items() if h_s > hour]
                        if not future_sell: return False
                        return (max(future_sell) - buy_p) * eff >= threshold

                    wt_filtered = {h: p for h, p in window_today.items() if p <= buy_limit or is_buy_profitable_arb(p, h)}
                    wom_filtered = {h: p for h, p in window_tomorrow.items() if p <= buy_limit or is_buy_profitable_arb(p, h)}
                    
                    peaks_today = get_peaks(wt_filtered, False, 999.0, tolerance)
                    peaks_tom = get_peaks(wom_filtered, False, 999.0, tolerance)
                    combined = peaks_today + peaks_tom
                    if combined:
                        target_hours = [h for h, p in combined]
                        target_price = min(p for h, p in combined)
                        res["target_price"] = target_price

                res["arbitrage_decision"] = global_arb_note
            else: # sell
                res["limit_used"] = sell_limit
                if negative_hours and cur_hour in negative_hours:
                    res["state"] = "price_limit_not_met"
                    return res
                
                def is_profitable(price, hour):
                    cheap_p_back, _ = get_best_buyback(hour)
                    return (price - cheap_p_back) * eff >= threshold

                raw_peaks_today = get_peaks(window_today, True, 0.0, tolerance)
                raw_peaks_tom = get_peaks(window_tomorrow, True, 0.0, tolerance)
                
                if not raw_peaks_today and not raw_peaks_tom:
                    res["state"] = "price_limit_not_met"
                    res["arbitrage_decision"] = f"Ожидание: Пиков не найдено | {global_arb_note}"
                else:
                    peaks_today = []
                    for h, p in raw_peaks_today:
                        if p >= sell_limit or is_profitable(p, h):
                            peaks_today.append((h, p))
                            
                    peaks_tom = []
                    for h, p in raw_peaks_tom:
                        if p >= sell_limit or is_profitable(p, h):
                            peaks_tom.append((h, p))
                    
                    if not peaks_today and not peaks_tom:
                        res["state"] = "unprofitable_arbitrage"
                        res["multi_cycle"] = "Деградация АКБ > Выгоды (и цена ниже лимита)"
                    else:
                        # Success: found target hours either by limit or by profit
                        if peaks_today and peaks_tom:
                            max_h_today = max(h for h, p in peaks_today)
                            min_h_tom = min(h for h, p in peaks_tom)
                            
                            buy_limit = self.manager.get_setting(CONF_PRICE_BUY_LIMIT, 99.0)
                            can_recharge = False
                            for h in range(max_h_today + 1, min_h_tom):
                                if all_prices.get(h, 99.0) <= buy_limit:
                                    can_recharge = True
                                    res["multi_cycle"] = "Благоприятно (Дешевая сеть ночью)"
                                    break
                                if 8 <= (h % 24) <= 16:
                                    fsensors = self.manager.forecast_tomorrow_sensor
                                    if fsensors:
                                        if isinstance(fsensors, str): fsensors = [fsensors]
                                        val_sum = 0.0
                                        for fsensor in fsensors:
                                            st = self.manager.hass.states.get(fsensor)
                                            v = get_kwh_val(st)
                                            if v is not None: val_sum += v
                                    if val_sum > 3.0:
                                        can_recharge = True
                                        res["multi_cycle"] = "Благоприятно (Ожидается солнце)"
                                        break
                                else:
                                    can_recharge = True
                                    res["multi_cycle"] = "Благоприятно (Световой день)"
                                    break
                                
                            if can_recharge:
                                combined = peaks_today + peaks_tom
                                target_hours = [h for h, p in combined]
                                target_price = max(p for h, p in combined)
                            else:
                                res["multi_cycle"] = "Неблагоприятно (Нет условий для дозарядки)"
                                max_today_p = max(p for h, p in peaks_today)
                                max_tom_p = max(p for h, p in peaks_tom)
                                if max_today_p >= max_tom_p:
                                    target_hours = [h for h, p in peaks_today]
                                    target_price = max_today_p
                                else:
                                    target_hours = [h for h, p in peaks_tom]
                                    target_price = max_tom_p
                        elif peaks_today:
                            target_hours = [h for h, p in peaks_today]
                            target_price = max(p for h, p in peaks_today)
                        elif peaks_tom:
                            target_hours = [h for h, p in peaks_tom]
                            target_price = max(p for h, p in peaks_tom)
                        
                        res["target_price"] = target_price
                        
                        cur_p = normalize_float(today_prices.get(str(cur_hour), 0.0))

                        
                        cheap_p_back, cheap_h_back = get_best_buyback(cur_hour)
                        cur_gain = (cur_p - cheap_p_back) * eff
                        
                        status = "Ожидание"
                        if cur_p >= sell_limit: status = "Продажа (Лимит)"
                        elif cur_gain >= threshold: status = "Продажа (Арбитраж)"
                        
                        detail = f"Сейчас {cur_p:.2f}. {global_arb_note}"
                        if best_arb_pair[0] is not None and best_arb_pair[0] > cur_hour and all_sell_prices[best_arb_pair[0]] > cur_p + 0.01:
                             detail += f" | Ждем главного пика в {best_arb_pair[0]%24:02d}:00"

                        res["arbitrage_decision"] = f"{status}: {detail}"

            target_hours = [h for h in target_hours if h >= cur_hour]

            # Survival Logic
            if mode == "buy" and batt_cap > 0 and self.manager.get_setting(CONF_DYNAMIC_SOC_BUY, True) and active_window:
                min_soc = self.manager.get_setting(CONF_MIN_SOC_BUY, 10.0)
                natural_hours_names = set(target_hours)
                survival_hours = set(target_hours)
            
                while True:
                    added_bridge = False
                    commands = {h_cmd: max_power for h_cmd in survival_hours}
                    sim_range = list(range(cur_hour, active_window[1] + 1))
                    final_soc, log = self.run_soc_simulation(batt_soc, sim_range, now, commands)
                    
                    violation_hour = None
                    for h_step in sim_range:
                        h_label = f"{h_step%24:0>2}:00" + (" (Завтра)" if h_step >= 24 else "")
                        soc_at_h = log.get(h_label, 100.0)
                        if soc_at_h < min_soc and violation_hour is None:
                            violation_hour = h_step
                    
                    if violation_hour is not None:
                        search_space = [sh for sh in range(cur_hour, violation_hour + 1) if sh not in survival_hours and sh in all_prices]
                        if search_space:
                            cheapest_bridge = min(search_space, key=lambda sh: all_prices[sh])
                            survival_hours.add(cheapest_bridge)
                            added_bridge = True
                    
                    if not added_bridge:
                        cur_hour_label = f"{cur_hour:0>2}:00"
                        if cur_hour in survival_hours and cur_hour not in natural_hours_names:
                            res["charge_reason"] = "survival"
                            res["charge_target_soc"] = 100.0
                        else:
                            res["charge_reason"] = "price"
                        break
                target_hours = list(survival_hours)

            res["limit_used"] = buy_limit if mode == "buy" else sell_limit
            future_active = [h for h in target_hours if h >= cur_hour]
            if future_active:
                upcoming_h = future_active[0]
                rel_hours = [h for h in future_active if (h < 24 if upcoming_h < 24 else h >= 24)]
                p_list = [all_prices.get(h, 0.0) for h in rel_hours]
                if p_list:
                    res["target_price"] = min(p_list) if mode == "buy" else max(p_list)

            if not target_hours and mode == "buy":
                res["state"] = "price_limit_not_met"
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
                
            def _format_hour_simple(h):
                d = "Завтра " if h >= 24 else "Сегодня "
                return f"{d}{h % 24:02d}:00"
                
            # Target & Power Calculation
            power_needed = 0.0
            target_soc = batt_soc
            sim_soc_plan = batt_soc
            if batt_cap > 0:
                if mode == "buy":
                    base_target = self.manager.get_setting(CONF_TARGET_SOC_BUY, 100.0)
                    if negative_hours or (locals().get('profitable_sell_peaks') or False): target_soc = 100.0
                    elif self.manager.get_setting(CONF_DYNAMIC_SOC_BUY, True):
                        budget_data = self.get_budget_and_permissions(self.manager.custom_period, skip_strategy_check=True)
                        expected_night = budget_data.get("expected_consumption", 0.0)
                        forecast = budget_data.get("forecast_val", 0.0)
                        total_avg = sum(self.manager.get_average_profile("consumption_total", self.manager.custom_period, tom_type).values())
                        tomorrow_need = max(0.0, (total_avg - expected_night) - forecast)
                        target_soc = min(base_target, (expected_night + tomorrow_need) / batt_cap * 100.0)
                    else: target_soc = base_target
                    
                    target_soc = min(100.0, target_soc)
                    sim_soc_plan = batt_soc
                    
                    charge_commands = {}
                    for h in target_hours_sorted:
                        if h < cur_hour: continue
                        rem_n = len([x for x in (natural_hours_names if 'natural_hours_names' in locals() else target_hours_sorted) if x >= h]) or 1
                        if target_soc > sim_soc_plan:
                            p = min(max_power, (batt_cap * (target_soc - sim_soc_plan) / 100.0) / rem_n)
                        else: p = 0.0
                        if h == cur_hour: power_needed = p
                        charge_commands[h] = p
                        sim_soc_plan = min(100.0, sim_soc_plan + (p / batt_cap * 100.0))
                    
                    # --- BUY SIMULATION ---
                    sim_range = list(range(cur_hour, max(target_hours_sorted) + 1))
                    _, sim_log = self.run_soc_simulation(batt_soc, sim_range, now, charge_commands)
                    
                    last_h = max(target_hours_sorted)
                    key_end = f"{last_h%24:02d}:00" + (" (Завтра)" if last_h >= 24 else "")
                    
                    res["buy_simulation"] = {
                        "projected_soc_at_start_pct": round(batt_soc, 1),
                        "projected_soc_at_end_pct": round(sim_log.get(key_end, sim_soc_plan), 1)
                    }
                else: # sell
                    base_target = self.manager.get_setting(CONF_TARGET_SOC_SELL, 20.0)
                    target_soc = base_target
                    
                    if self.manager.get_setting(CONF_DYNAMIC_SOC_SELL, True):
                        budget_data = self.get_budget_and_permissions(self.manager.custom_period, skip_strategy_check=True)
                        expected_night = budget_data.get("expected_consumption", 0.0)
                        eff_coeff = budget_data.get("efficiency_coefficient", 1.0)
                        min_soc_reserve = self.manager.get_setting(CONF_MIN_SOC_BUY, 10.0)
                        
                        expected_night_from_batt = expected_night / eff_coeff if eff_coeff > 0 else expected_night
                        ai_soc_reserve = (expected_night_from_batt / batt_cap * 100.0) + min_soc_reserve
                        
                        # Decision logic: 
                        # Is selling now (and buying back at cheap hour) better than saving for tomorrow's morning consumption?
                        cur_p = normalize_float(today_prices.get(str(cur_hour), 0.0))

                        
                        cheap_p_back, _ = get_best_buyback(cur_hour)
                        # Profit if we sell now and buy back
                        arb_gain = (cur_p - cheap_p_back) * eff_coeff - deg_cost
                        
                        # "Holding" gain = saving money on future purchase at standard buy limit
                        hold_gain = (buy_limit - cur_p) # simplified, if holding saves us from buying at buy_limit later
                        
                        if arb_gain > 0.05: # Arbitrage is clearly better (adjustable margin)
                            target_soc = base_target
                            res["arbitrage_decision"] += " | Цель: Продажа (Арбитраж выгоднее хранения)"
                        else:
                            target_soc = max(base_target, ai_soc_reserve)
                            res["arbitrage_decision"] += " | Цель: Хранение (До солнца)"
                    
                    target_soc = min(100.0, target_soc)
                    if len(target_hours) > 0:
                        # --- DUAL STRATEGY ARBITRAGE ANALYSIS ---
                        # Strategy 1: Last until Solar
                        # We only need to keep enough for tonight's consumption
                        # Strategy 2: Last until next Cheap Hour (Buyback)
                        # Identify the cheapest buyback hour and price
                        
                        full_buy_prices = {}
                        buy_prices_today = self.manager.data.get("prices_buy", {}).get(today_str, {})
                        buy_prices_tom = self.manager.data.get("prices_buy", {}).get(tomorrow_str, {})
                        for h, p in buy_prices_today.items():
                            try: full_buy_prices[int(h)] = float(str(p).replace(',', '.'))
                            except ValueError: continue
                        for h, p in buy_prices_tom.items():
                            try: full_buy_prices[int(h) + 24] = float(str(p).replace(',', '.'))
                            except ValueError: continue
                            
                        # Find cheapest buyback after current sell hours
                        future_buy = {h: p for h, p in full_buy_prices.items() if h > max(target_hours)}
                        cheapest_h = min(future_buy, key=future_buy.get) if future_buy else None
                        cheap_p = future_buy[cheapest_h] if cheapest_h else 999.0
                        
                        cur_sell_p = today_prices.get(str(cur_hour), 0.0)
                        try: cur_sell_p = float(str(cur_sell_p).replace(',', '.'))
                        except ValueError: cur_sell_p = 0.0
                        
                        # Gain = (Sell Price - Buy Price) * Efficiency
                        threshold = min_p if min_p >= deg_cost else (2 * deg_cost)
                        raw_diff = (cur_sell_p - cheap_p)
                        actual_gain = raw_diff * eff_coeff
                        
                        sell_strategy_note = "Продажа до солнца (стандарт)"
                        if cheapest_h:
                            if actual_gain >= threshold:
                                sell_strategy_note = f"Арбитраж ВЫГОДЕН: Продажа {cur_sell_p:.2f} -> Откуп {cheap_p:.2f} в {cheapest_h%24:02d}:00 (Профит {actual_gain:.2f} > Порога {threshold:.2f})"
                            else:
                                sell_strategy_note = f"Арбитраж НЕВЫГОДЕН: Продажа {cur_sell_p:.2f} -> Откуп {cheap_p:.2f}. Выгода {actual_gain:.2f} < Порога {threshold:.2f}"
                        
                        res["arbitrage_decision"] = f"{res.get('arbitrage_decision', '')} | {sell_strategy_note}"

                        delta_available = batt_energy_val - (target_soc * batt_cap / 100.0)
                        power_needed = max(0.0, (delta_available * eff_coeff) / len(target_hours))

                    # --- BUYBACK OPPORTUNITY (ARBITRAGE) ---
                    # Find cheapest buy price in the window
                    buy_prices = all_prices # all_prices contains buy prices for 'buy' mode, but here we are in 'sell' mode.
                    # Wait, all_prices here depends on 'mode'. If mode=='sell', all_prices are sell prices.
                    # We need BUY prices to check for buyback.
                    buy_prices_today = self.manager.data.get("prices_buy", {}).get(today_str, {})
                    buy_prices_tom = self.manager.data.get("prices_buy", {}).get(tomorrow_str, {})
                    full_buy_prices = {}
                    for h, p in buy_prices_today.items():
                        try: full_buy_prices[int(h)] = float(str(p).replace(',', '.'))
                        except ValueError: full_buy_prices[int(h)] = 0.0
                    for h, p in buy_prices_tom.items():
                        try: full_buy_prices[int(h) + 24] = float(str(p).replace(',', '.'))
                        except ValueError: full_buy_prices[int(h) + 24] = 0.0
                    
                    if full_buy_prices:
                        cheapest_h = min([h for h in full_buy_prices if h > max(target_hours or [cur_hour])], key=lambda h: full_buy_prices[h], default=None)
                        if cheapest_h is not None:
                            cheap_p = full_buy_prices[cheapest_h]
                            cur_sell_p = today_prices.get(str(cur_hour), 0.0)
                            try: cur_sell_p = float(str(cur_sell_p).replace(',', '.'))
                            except ValueError: cur_sell_p = 0.0
                            
                            buy_limit = self.manager.get_setting(CONF_PRICE_BUY_LIMIT, 0.0)
                            if (cur_sell_p - cheap_p) * eff_coeff >= deg_cost:
                                res["arbitrage_buyback"] = {
                                    "opportunity": True,
                                    "power_kw": power_needed if cur_hour in target_hours else 0.0,
                                    "available_kwh": round(batt_energy_val, 2),
                                    "reserve_kwh": round(target_soc * batt_cap / 100.0, 2),
                                    "energy_to_wait_kwh": round(max(0.0, (target_soc * batt_cap / 100.0) - batt_energy_val), 2),
                                    "note": f"Выгодно: продажа по {cur_sell_p}, откуп по {cheap_p} в {cheapest_h%24:02d}:00"
                                }

                    # --- SELL SIMULATION ---
                    sim_range = list(range(cur_hour, active_window[1] + 1))
                    sim_commands = {h: -max_power for h in target_hours if h >= cur_hour}
                    _, sim_log = self.run_soc_simulation(batt_soc, sim_range, now, sim_commands)
                    
                    last_h = max(target_hours or [cur_hour])
                    key_after = f"{last_h%24:02d}:00" + (" (Завтра)" if last_h >= 24 else "")
                    
                    # Logically determine what "morning" means relative to the sale
                    # If sale is today (<24h), morning is tomorrow 08:00 (h=32)
                    # If sale is tomorrow (>=24h), morning is the end of the simulation we have (h=47)
                    morning_h = 32 if last_h < 24 else 47
                    suffix = " (Завтра)" if morning_h >= 24 else ""
                    key_morning = f"{morning_h%24:02d}:00" + suffix
                    
                    res["sell_simulation"] = {
                        "projected_soc_at_start_pct": round(batt_soc, 1),
                        "projected_soc_after_sale_pct": round(sim_log.get(key_after, batt_soc), 1),
                        "projected_soc_morning_pct": round(sim_log.get(key_morning, sim_log.get("08:00 (Завтра)", 0.0)), 1)
                    }
                
            res["recommended_power_kw"] = round(min(float(power_needed), max_power), 3)
            res["active_hours"] = target_hours_sorted
            res["active_hours_formatted"] = ", ".join([_format_hour_simple(h) for h in target_hours_sorted])
            res["active_periods"] = ", ".join(found_periods)
            res["state"] = "active" if cur_hour in target_hours_sorted and res["recommended_power_kw"] > 0 else "idle"
            return res
        finally:
            self._calculating_strategy = old_calc
