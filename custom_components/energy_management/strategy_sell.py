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
                target_hours = sorted(safe_peaks)
                if target_hours:
                    target_price = max([all_sell_prices[h] for h in target_hours], default=0.0)
                    epochs = self._group_contiguous(target_hours)

            if not target_hours:
                res["state"] = "price_limit_not_met"
                return res

            # --- TS 6.1 Sunrise Guard (Survival Logic) ---
            min_soc_val = float(man.get_setting(CONF_MIN_SOC_BAT, 10.0))
            user_limit = float(man.get_setting(CONF_AI_DISCHARGE_LIMIT, 20.0))
            soc_buffer = float(man.get_setting(CONF_SOC_BUFFER, 5.0))
            
            survival_floor = min_soc_val + soc_buffer
            base_target = max(user_limit, survival_floor)
            
            # v11.6.214: Multi-pass Allocation & Recursive Survival
            sell_commands = {}
            target_hours_sorted = sorted(target_hours)
            
            # 1. Initial Pass (Simulation without selling)
            sim_range = list(range(cur_hour, max(target_hours_sorted) + 24))
            _, base_log, _ = self.run_soc_simulation(b_soc, sim_range, now, {})
            
            # 2. Budgeting
            # Choose most restrictive floor: User Limit or Survival until Sunrise
            _h_sunrise_target = sunrise_h - 1
            if cur_hour >= _h_sunrise_target: _h_sunrise_target += 24
            key_sunrise = f"{_h_sunrise_target % 24:02d}:59" + (" (Завтра)" if _h_sunrise_target >= 24 else "")
            
            natural_soc_at_sunrise = self._get_soc_from_log(base_log, key_sunrise, b_soc)
            surplus_for_morning = max(0.0, (natural_soc_at_sunrise - (min_soc_val + 2.0)) * b_cap / 100.0)
            
            _k_end_hour = f"{cur_hour % 24:02d}:59"
            natural_soc_now = self._get_soc_from_log(base_log, _k_end_hour, b_soc)
            surplus_for_user_limit = max(0.0, (natural_soc_now - base_target) * b_cap / 100.0)
            
            available_sell_dc = min(surplus_for_morning, surplus_for_user_limit)
            available_sell_ac = max(0.0, available_sell_dc * eff)
            
            # 3. Fair-Greedy Allocation (Price Priority as per TS 4.1.3)
            # v11.6.330: Sort target hours by price DESCENDING
            target_hours_by_price = sorted(target_hours_sorted, key=lambda h: all_sell_prices.get(h, 0.0), reverse=True)
            
            rem_ac = available_sell_ac
            for h in target_hours_by_price:
                if rem_ac <= 0.01: break
                h_f = max(0.1, (60 - now.minute)/60.0) if h == cur_hour else 1.0
                p_alloc = min(max_p, rem_ac / h_f)
                sell_commands[h] = round_f(p_alloc, 3)
                rem_ac -= (p_alloc * h_f)
            
            # 4. Final Strategic Simulation
            sim_commands_neg = {h: -p for h, p in sell_commands.items()}
            _, sim_log, _ = self.run_soc_simulation(b_soc, sim_range, now, sim_commands_neg, b_min_soc=base_target)
            
            soc_after = self._get_soc_from_log(sim_log, f"{max(target_hours)%24:02d}:59" if target_hours else f"{cur_hour%24:02d}:59", b_soc)
            soc_morning = self._get_soc_from_log(sim_log, key_sunrise, b_soc)
            
            # 5. UI Diagnostics
            active_h = [h for h, p in sell_commands.items() if p > 0.05]
            res["sell_simulation"] = {
                "projected_soc_at_sale_start_pct": b_soc,
                "projected_soc_after_sale_pct": round_f(soc_after, 1),
                "projected_soc_morning_pct": round_f(soc_morning, 1),
                "log": sim_log
            }
            res["raw_commands"] = sell_commands
            res["recommended_power_kw"] = sell_commands.get(cur_hour, 0.0)
            res["limit_used"] = sell_limit
            res["target_price"] = target_price
            res["strategy_candidates"] = [f"{h%24:02d}:00" for h in target_hours]
            res["active_hours"] = active_h
            res["planned_power_per_h"] = {f"{h%24:02d}:00": p for h, p in sell_commands.items()}
            
            def group_h(hours):
                if not hours: return ""
                periods = self._group_contiguous(hours)
                groups = []
                for p in periods:
                    groups.append(f"{p[0]%24:02d}:00-{p[-1]%24:02d}:59")
                return ", ".join(groups)

            res["active_periods"] = group_h(active_h)
            if active_h:
                res["analyzed_window"] = f"До {max(active_h)%24:02d}:59"
            
            # recommended_amps (if voltage is available)
            v_val = 52.0 # Default fallback
            if man.battery_voltage_sensor:
                v_val = float(man.get_sensor_float(man.battery_voltage_sensor) or 52.0)
            res["recommended_amps"] = round_f((sell_commands.get(cur_hour, 0.0) * 1000.0) / v_val, 1) if v_val > 0 else 0.0
            
            res["arbitrage_decision"] = f"Продажа по {cur_p_f:.2f}" if cur_hour in active_h else "Ожидание пика"
            res["power_decision"] = "Активно" if cur_hour in active_h else "Ожидание"
            
            # Restore old sell_debug structure
            f_today = round_f(float(man.get_forecast_value(man.forecast_today_sensor) or 0.0), 1)
            f_tom_val = round_f(float(man.get_forecast_value(man.forecast_tomorrow_sensor) or 0.0), 1)
            
            res["arbitrage_sell_debug"] = {
                "base_target": round_f(base_target, 1),
                "available_ac": round_f(available_sell_ac, 2),
                "sim_log": "|".join([f"{h % 24}: {self._get_soc_from_log(sim_log, h, 0.0):.0f}%" for h in range(cur_hour, cur_hour + 12)]),
                "final_targets": str(target_hours),
                "midnight_trace": "|".join(getattr(man, "midnight_trace", [])[-4:]),
                "f_today": f_today,
                "f_tom": f_tom_val,
                "target_price": target_price,
                "cur_p": cur_p_f,
                "raw_epochs": str(epochs),
                "price_sorted": str(target_hours_by_price),
                "commands": {f"{h%24:02d}h": p for h, p in sell_commands.items()}
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
