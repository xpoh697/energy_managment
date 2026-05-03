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

class StrategyBuy(StrategyEngine):
    """Specialized engine for BUY-mode energy management strategies."""
    
    def get_market_strategy(self, mode="buy"):
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
            "buy_simulation": {"projected_soc_at_start_pct": b_soc, "projected_soc_at_end_pct": b_soc, "projected_soc_morning_pct": b_soc},
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
            
            p_buy_st = dict(man.data.get("prices_buy", {}))
            today_prices = dict(p_buy_st.get(today_str, {}))
            tomorrow_prices = dict(p_buy_st.get(tomorrow_str, {}))
            
            # v11.6.528: Cycle Isolation
            if tomorrow_prices:
                tom_h_first = min(int(h) for h in tomorrow_prices.keys())
                if (tom_h_first + 24 - 23) > 12:
                    tomorrow_prices = {}
            
            res["today_prices"] = today_prices
            res["tomorrow_prices"] = tomorrow_prices

            avg_prof_gen = man.get_average_profile("generation", man.custom_period, man.day_type)
            avg_prof_cons = man.get_average_profile("consumption_base", man.custom_period, man.day_type)

            sunrise_h = 8
            for h in range(4, 12):
                if float(normalize_float(avg_prof_gen.get(str(h), 0.0))) > 0.1:
                    sunrise_h = h
                    break
            res["sunrise_hour"] = sunrise_h

            all_buy_prices = {}
            for h, p in today_prices.items(): all_buy_prices[int(h)] = float(normalize_float(p))
            for h, p in tomorrow_prices.items(): all_buy_prices[int(h) + 24] = float(normalize_float(p))

            # Filter for current cycle
            _sorted_h = sorted(all_buy_prices.keys())
            _final_buy = {}
            for h in _sorted_h:
                if h < cur_hour: continue
                if _final_buy and (h - max(_final_buy.keys()) > 12): break
                _final_buy[h] = all_buy_prices[h]
            all_buy_prices = _final_buy

            if not all_buy_prices: return res

            cur_p_f = all_buy_prices.get(cur_hour, 0.0)
            buy_limit = float(man.get_setting(CONF_PRICE_BUY_LIMIT, 2.0))
            eff = float(self.get_efficiency_coefficient() or 1.0)
            
            negative_hours = [h for h, p in all_buy_prices.items() if p < 0.0]

            # Arbitrage Logic for Buy Decisions
            s_p_today = dict(man.data.get("prices_sell", {}).get(today_str, {}))
            s_p_tom = dict(man.data.get("prices_sell", {}).get(tomorrow_str, {}))
            all_sell_prices = {}
            for h, p in s_p_today.items(): all_sell_prices[int(h)] = float(normalize_float(p))
            for h, p in s_p_tom.items(): all_sell_prices[int(h) + 24] = float(normalize_float(p))

            threshold = float(max(prof_thresh, 2.0 * deg_cost))
            
            def is_buy_profitable_arb(buy_p, hour):
                future_sell = {hs: ps for hs, ps in all_sell_prices.items() if hs > hour}
                if not future_sell: return False
                best_s = max(future_sell.values())
                gain = float(best_s * eff - buy_p - deg_cost)
                return gain >= threshold

            target_hours = []
            if negative_hours:
                target_hours = negative_hours
                res["charge_reason"] = "negative"
                res["arbitrage_decision"] = f"Отрицательная цена ({cur_p_f:.2f})"
            else:
                dynamic_buy = bool(man.get_setting(CONF_DYNAMIC_SOC_BUY, True))
                candidates = []
                for h, p in all_buy_prices.items():
                    if p <= buy_limit or (dynamic_buy and is_buy_profitable_arb(p, h)):
                        candidates.append(h)
                
                if candidates:
                    # Select cheapest windows (peaks)
                    min_p = min(all_buy_prices[h] for h in candidates)
                    target_hours = [h for h in candidates if all_buy_prices[h] <= min_p + 0.05]
                    res["charge_reason"] = "cheap" if not any(is_buy_profitable_arb(all_buy_prices[h], h) for h in target_hours) else "arbitrage"
                    res["arbitrage_decision"] = f"Ценовое окно ({cur_p_f:.2f})" if cur_hour in target_hours else "Ожидание окна"
                else:
                    res["charge_reason"] = "none"
                    res["state"] = "price_limit_not_met"

            # Survival Bridge Logic
            min_soc = float(man.get_setting(CONF_MIN_SOC_BAT, 10.0))
            if b_cap > 0 and dynamic_buy:
                survival_hours = set(target_hours)
                for _ in range(5): # Max 5 bridges
                    added = False
                    sim_range = list(range(cur_hour, max(all_buy_prices.keys()) + 1))
                    sim_cmds = {h: max_p for h in survival_hours}
                    _, log, _ = self.run_soc_simulation(b_soc, sim_range, now, sim_cmds)
                    
                    for h_step in sim_range:
                        h_key = f"{h_step % 24:02d}:59" + (" (Завтра)" if h_step >= 24 else "")
                        soc_h = self._get_soc_from_log(log, h_key, 100.0)
                        if soc_h < min_soc:
                            # Add cheapest hour before violation
                            search = [sh for sh in range(cur_hour, h_step + 1) if sh not in survival_hours and sh in all_buy_prices]
                            if search:
                                cheapest = min(search, key=lambda x: all_buy_prices[x])
                                survival_hours.add(cheapest)
                                added = True
                                break
                    if not added: break
                target_hours = sorted(list(survival_hours))
                if any(h not in (negative_hours or candidates if 'candidates' in locals() else []) for h in target_hours):
                    if cur_hour in target_hours and res["charge_reason"] == "none":
                        res["charge_reason"] = "survival"

            # Power Allocation
            charge_commands = {}
            target_soc = b_soc
            if target_hours:
                base_limit = float(man.get_setting(CONF_AI_CHARGE_LIMIT, 100.0))
                target_soc = 100.0 if res["charge_reason"] in ["negative", "arbitrage"] else base_limit
                
                # Pre-sim to find SOC at start of window
                first_h = min(target_hours)
                soc_at_start_plan, _, _ = self.run_soc_simulation(b_soc, list(range(cur_hour, first_h)), now, {}, allow_discharge=False)
                
                needed_kwh_dc = (target_soc - soc_at_start_plan) * b_cap / 100.0
                accum_kwh_dc = 0.0
                for h in sorted(target_hours):
                    if accum_kwh_dc >= needed_kwh_dc - 0.01 and all_buy_prices[h] > 0: break
                    h_factor = max(0.1, (60 - now.minute)/60.0) if h == cur_hour else 1.0
                    cc_cv = self.get_cc_cv_ratio(soc_at_start_plan + (accum_kwh_dc/b_cap*100.0))
                    p_charge = min(max_p, max_p * cc_cv)
                    charge_commands[h] = round_f(p_charge, 3)
                    accum_kwh_dc += (p_charge * h_factor * eff)
                
                # Final Simulation
                sim_range = list(range(cur_hour, cur_hour + 48))
                _, sim_log, _ = self.run_soc_simulation(b_soc, sim_range, now, charge_commands, allow_discharge=False)
                
                soc_end = self._get_soc_from_log(sim_log, f"{max(target_hours)%24:02d}:59" if target_hours else f"{cur_hour%24:02d}:59", b_soc)
                key_morning = f"{(sunrise_h-1)%24:02d}:59 (Завтра)"
                soc_morning = self._get_soc_from_log(sim_log, key_morning, soc_end)
                
                res["buy_simulation"] = {
                    "projected_soc_at_start_pct": round_f(soc_at_start_plan, 1),
                    "projected_soc_at_end_pct": round_f(soc_end, 1),
                    "projected_soc_morning_pct": round_f(soc_morning, 1),
                    "log": sim_log
                }
                res["charge_commands"] = charge_commands
                res["recommended_power_kw"] = charge_commands.get(cur_hour, 0.0)
                
                # recommended_amps
                v_val = 52.0
                if man.battery_voltage_sensor:
                    v_val = float(man.get_sensor_float(man.battery_voltage_sensor) or 52.0)
                res["recommended_amps"] = round_f((charge_commands.get(cur_hour, 0.0) * 1000.0) / v_val, 1) if v_val > 0 else 0.0
                
                res["planned_power_per_h"] = {f"{h%24:02d}:00": p for h, p in charge_commands.items()}
                res["analyzed_window"] = f"До {max(target_hours)%24:02d}:59"
                res["active_periods"] = f"{min(target_hours)%24:02d}:00 - {max(target_hours)%24:02d}:59"
                res["buy_debug"] = f"Бюджет DC: {needed_kwh_dc:.2f}кВтч | Причина: {res['charge_reason']}"
                
                if charge_commands.get(cur_hour, 0.0) > 0.05: res["state"] = "active"

            # Mode text
            txt = "Ожидание"
            if res["state"] == "active":
                reason = res.get("charge_reason", "")
                if reason == "negative": txt = "Зарядка (Отриц. цена)"
                elif reason == "arbitrage": txt = "Зарядка (Арбитраж)"
                elif reason == "survival": txt = "Зарядка (Выживание)"
                else: txt = "Зарядка (Дешево)"
            elif res["charge_reason"] == "none":
                txt = "В покупке нет необходимости"
            res["current_mode_text"] = txt
            res["raw_commands"] = charge_commands
            res["strategy_candidates"] = [f"{h%24:02d}:00" for h in target_hours]

            self._strategy_cache[cache_key] = {"time": now, "res": res}
            return res
        finally:
            self._calculating_strategy = old_calc
