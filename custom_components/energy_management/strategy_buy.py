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
        """Standardized Buying Strategy v11.9.89+"""
        def group_h(hours):
            if not hours: return ""
            sorted_h = sorted(list(hours))
            groups = []
            if not sorted_h: return ""
            start = sorted_h[0]
            prev = sorted_h[0]
            for h in sorted_h[1:]:
                if h == prev + 1:
                    prev = h
                else:
                    groups.append(f"{start%24:02d}-{prev%24:02d}" if start != prev else f"{start%24:02d}")
                    start = h
                    prev = h
            groups.append(f"{start%24:02d}-{prev%24:02d}" if start != prev else f"{start%24:02d}")
            return ", ".join(groups)

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
            "charge_reason": "Нет",
            "is_charging_now": False,
            "strategy_candidates": [],
            "raw_commands": {}
        }
        
        old_calc = bool(getattr(self, "_calculating_strategy", False))
        self._calculating_strategy = True
        
        # v11.9.106: Set the active buy limit for debug transparency
        price_buy_limit = float(man.get_setting(CONF_PRICE_BUY_LIMIT, 0.05))
        target_soc = b_soc # v11.9.135: Global init to prevent UnboundLocalError
        charge_commands = {} # v11.9.140: Global init to prevent UnboundLocalError
        target_hours = []
        negative_hours = []
        # v11.9.130: Unified Key Helper (Moved to top in v11.9.147)
        def get_h_log_key(h_abs):
            h_rel = h_abs % 24
            suffix = ""
            if h_abs >= 48: suffix = " (Через день)"
            elif h_abs >= 24: suffix = " (Завтра)"
            return f"{h_rel:02d}:59{suffix}"

        res["limit_used"] = price_buy_limit
        
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
            buy_limit = price_buy_limit
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
                res["charge_reason"] = "Отрицательная цена"
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
                    res["charge_reason"] = "Дешево" if not any(is_buy_profitable_arb(all_buy_prices[h], h) for h in target_hours) else "Арбитраж"
                    res["arbitrage_decision"] = f"Ценовое окно ({cur_p_f:.2f})" if cur_hour in target_hours else "Ожидание окна"
                else:
                    res["charge_reason"] = "Нет"
                    res["state"] = "price_limit_not_met"

                # v11.9.98: Optimized Survival Bridge Logic
                min_soc = float(man.get_setting(CONF_MIN_SOC_BAT, 10.0))
                # Instead of searching ONLY before violation, we search globally for the cheapest hours
                # but only if SOC is actually critical or it's a cheap window.
                survival_hours = set(target_hours)
                avg_price = sum(all_buy_prices.values()) / len(all_buy_prices) if all_buy_prices else 0.0
                
                for _ in range(5): # Max 5 bridges
                    added = False
                    sim_range = list(range(cur_hour, max(all_buy_prices.keys()) + 1))
                    sim_cmds = {h: max_p for h in survival_hours}
                    # v11.9.99: Use consumption_base for survival checks (don't panic due to car chargers)
                    _, log, _ = self.run_soc_simulation(b_soc, sim_range, now, sim_cmds, house_profile_override="consumption_base")
                    
                    first_violation_h = None
                    for h_step in sim_range:
                        h_key = f"{h_step % 24:02d}:59" + (" (Завтра)" if h_step >= 24 else "")
                        soc_h = self._get_soc_from_log(log, h_key, 100.0)
                        if soc_h < min_soc:
                            first_violation_h = h_step
                            break
                    
                    if first_violation_h is not None:
                        res["survival_violation_hour"] = first_violation_h
                        candidates_global = [sh for sh in all_buy_prices.keys() if sh not in survival_hours]
                        if candidates_global:
                            cheapest = min(candidates_global, key=lambda x: all_buy_prices[x])
                            c_price = all_buy_prices[cheapest]
                            
                            # Panic Brake v2: Strict Price Ceiling
                            # If SOC > 25%, don't buy if price > 3x buy_limit
                            is_too_expensive = bool(c_price > buy_limit * 3.0 and b_soc > 25.0)
                            is_peak = bool(c_price > avg_price)
                            is_urgent = bool(first_violation_h <= cur_hour + 2)
                            
                            if (not is_peak and not is_too_expensive) or is_urgent or b_soc < 20.0:
                                survival_hours.add(cheapest)
                                added = True
                    
                    if not added: break
                
                target_hours = sorted(list(survival_hours))
                if any(h not in (negative_hours or (candidates if 'candidates' in locals() else [])) for h in target_hours):
                    res["charge_reason"] = "Выживание"

                # v12.3.0: Standardized Survival Floor (Gatekeeper)
                morning_h = man.get_sunrise_hour() or 8
                morning_h_abs = morning_h + (24 if cur_hour >= 4 else 0)
                
                last_h = max(target_hours) if target_hours else cur_hour
                # Gatekeeper includes SOC Buffer
                survival_target = self.get_gatekeeper_floor(last_h + 1, morning_h_abs)
                
                base_limit = float(man.get_setting(CONF_AI_CHARGE_LIMIT, 100.0))
                
                # v11.9.120: Adhere to user limit (base_limit) for Arbitrage and Cheap modes
                if res["charge_reason"] == "Отрицательная цена":
                    target_soc = 100.0
                elif res["charge_reason"] == "Выживание":
                    target_soc = min(base_limit, survival_target)
                else:
                    # Cheap and Arbitrage both respect the user limit
                    target_soc = base_limit
                
                res["survival_target"] = survival_target
                res["target_soc"] = round_f(target_soc, 1)
                
                # Power Allocation
                charge_commands = {}
                soc_at_start_plan = b_soc
                soc_end = b_soc
                soc_morning = b_soc

                if target_hours:
                    first_h = min(target_hours)
                    # v11.9.133: Reverted to False to match user expectation of SOC at start
                    soc_at_start_plan, _, _ = self.run_soc_simulation(b_soc, list(range(cur_hour, first_h)), now, {}, allow_discharge=False)
                    
                    # v11.9.120: Deduct solar forecast during the charging window to avoid overshooting target_soc
                    _sim_h_window = list(range(cur_hour, max(target_hours) + 1))
                    _, _log_sun, _ = self.run_soc_simulation(b_soc, _sim_h_window, now, {}, allow_discharge=False)
                    # v11.9.147: Use unified helper to avoid Tomorrow suffix mismatches
                    soc_with_sun_only = self._get_soc_from_log(_log_sun, get_h_log_key(max(target_hours)), b_soc)
                    
                    # Energy needed from grid = (Target - SOC_with_sun_only)
                    needed_kwh_dc = max(0.0, (target_soc - soc_with_sun_only) * b_cap / 100.0)
                    accum_kwh_dc = 0.0
                    for h in sorted(target_hours):
                        if accum_kwh_dc >= needed_kwh_dc - 0.01 and all_buy_prices[h] > 0: break
                        h_factor = max(0.1, (60 - now.minute)/60.0) if h == cur_hour else 1.0
                        
                        # v12.2.1: Throttle power to reach exactly target_soc
                        remaining_kwh_needed = max(0.0, (needed_kwh_dc - accum_kwh_dc))
                        p_needed = remaining_kwh_needed / (h_factor * eff) if h_factor > 0 else 0
                        
                        cc_cv = self.get_cc_cv_ratio(soc_at_start_plan + (accum_kwh_dc/b_cap*100.0))
                        p_charge = min(max_p, max_p * cc_cv, p_needed)
                        
                        charge_commands[h] = round_f(p_charge, 3)
                        accum_kwh_dc += (p_charge * h_factor * eff)
                    
                    # UI Reporting for active window
                    res["analyzed_window"] = f"До {max(target_hours)%24:02d}:59"
                    res["active_periods"] = group_h(target_hours)
                    res["limit_used"] = buy_limit
                else:
                    res["analyzed_window"] = "Нет окон"
                    res["active_periods"] = ""
                
                # Final Simulation
                sim_range = list(range(cur_hour, cur_hour + 48))
                # v11.9.133: Reverted to allow_discharge=False to show clean Target SOC matching the limit
                _, sim_log, _ = self.run_soc_simulation(b_soc, sim_range, now, charge_commands, allow_discharge=False)
                
                last_h = max(target_hours) if target_hours else cur_hour
                soc_end = self._get_soc_from_log(sim_log, get_h_log_key(last_h), b_soc)
                
                res["gatekeeper_floor"] = self.get_gatekeeper_floor(last_h + 1, morning_h_abs)
                res["survival_floor"] = self.get_survival_floor(last_h + 1, morning_h_abs)
                
                soc_morning = self._get_soc_from_log(sim_log, get_h_log_key(morning_h_abs - 1), soc_end)
                res["buy_simulation"] = {
                    "projected_soc_at_start_pct": round_f(soc_at_start_plan, 1),
                    "projected_soc_at_end_pct": round_f(soc_end, 1),
                    "projected_soc_morning_pct": round_f(soc_morning, 1)
                }
                res["charge_commands"] = charge_commands
                res["recommended_power_kw"] = charge_commands.get(cur_hour, 0.0)
                
                # v11.9.97: Set definitive flag for current hour activity
                is_neg = bool(all_buy_prices.get(cur_hour, 1.0) <= 0.0)
                res["is_charging_now"] = bool(res["recommended_power_kw"] > 0.05 or is_neg)
                
                # recommended_amps
                v_val = 52.0
                if man.battery_voltage_sensor:
                    v_val = float(man.get_sensor_float(man.battery_voltage_sensor) or 52.0)
                res["recommended_amps"] = round_f((charge_commands.get(cur_hour, 0.0) * 1000.0) / v_val, 1) if v_val > 0 else 0.0
                
                # v11.9.101: Log significant strategy updates (added SOC and Power)
                strat_log = f"[Strategy Buy] SOC: {b_soc:.1f}% | Power: {res['recommended_power_kw']:.1f} kW | Reason: {res['charge_reason']} | Target: {target_soc:.1f}% | Now Charging: {res['is_charging_now']} | Windows: {res.get('active_periods','')}"
                if str(strat_log) != str(getattr(self, "_last_strat_log", "")):
                    man.log_to_file(strat_log)
                    self._last_strat_log = strat_log
                

                # v11.9.130: Build the hourly plan using the unified key helper
                planned_results = {}
                for h, p in charge_commands.items():
                    if p <= 0.05: continue
                    h_fmt = f"{h%24:02d}:00" + (" (Завтра)" if h >= 24 else "")
                    
                    h_soc = self._get_soc_from_log(sim_log, get_h_log_key(h), b_soc)
                    
                    planned_results[h_fmt] = {
                        "power": round_f(p, 3),
                        "soc": round_f(h_soc, 1)
                    }
                res["planned_power_per_h"] = planned_results
                res["target_soc"] = round_f(target_soc, 1)

                # v12.0.1: Synchronize with Inverter Mode Command sensor
                res["active_hours"] = [int(h) for h, v in charge_commands.items() if v > 0.05]
                if negative_hours:
                    res["first_negative_hour"] = min(negative_hours)
                    # v12.0.2: Always allow waiting if negative prices are coming
                    res["can_wait_for_negative"] = True

                if charge_commands.get(cur_hour, 0.0) > 0.05 or cur_p_f <= 0: 
                    res["state"] = "active"
                    res["power"] = charge_commands.get(cur_hour, 0.0)

            # v12.0.3: Detailed diagnostics for UI (as per Section 4.2.5 of TZ)
            # Moved outside if-block to ensure visibility even when not buying
            _neg_tag = "[Отрицательная цена]" if cur_hour in negative_hours else ""
            if not _neg_tag and negative_hours:
                _neg_tag = "Ожидание отрицательных цен"
            
            # Calculate arbitrage diagnostics for debug
            future_sell = {hs: ps for hs, ps in all_sell_prices.items() if hs > cur_hour}
            _best_s = max(future_sell.values()) if future_sell else 0.0
            _gain = float(_best_s * eff - cur_p_f - deg_cost) if _best_s > 0 else 0.0
            _is_arb = bool(_gain >= threshold)
            
            future_buy = {hb: pb for hb, pb in all_buy_prices.items() if hb > cur_hour}
            _best_b = min(future_buy.values()) if future_buy else 0.0

            res["buy_debug"] = {
                "summary": f"{_neg_tag} | Цена: {cur_p_f:.2f} | Цель: {target_soc:.1f}%".strip(" | "),
                "current_price": cur_p_f,
                "target_soc": round_f(target_soc, 1),
                "is_arbitrage_profitable": _is_arb,
                "best_sell_later": round_f(_best_s, 2),
                "best_buy_later": round_f(_best_b, 2),
                "arbitrage_gain": round_f(_gain, 3),
                "negative_prices_upcoming": bool(negative_hours),
                "charge_reason": res.get("charge_reason", "Нет"),
                "gatekeeper_floor": res.get("gatekeeper_floor", 0.0),
                "survival_floor": res.get("survival_floor", 0.0),
                "target_hours": target_hours,
                "candidates": candidates if 'candidates' in locals() else [],
                "commands": {f"{h}h": p for h, p in charge_commands.items() if p > 0}
            }

            # Mode text
            txt = "Ожидание"
            if res["state"] == "active":
                reason = res.get("charge_reason", "")
                if reason == "Отрицательная цена": txt = "Зарядка (Отриц. цена)"
                elif reason == "Арбитраж": txt = "Зарядка (Арбитраж)"
                elif reason == "Выживание": txt = "Зарядка (Выживание)"
                else: txt = "Зарядка (Дешево)"
            elif res["charge_reason"] == "Нет":
                txt = "В покупке нет необходимости"
            res["current_mode_text"] = txt
            res["power_decision"] = txt if res["state"] == "active" else "Ожидание окна"
            res["raw_commands"] = charge_commands
            res["strategy_candidates"] = [f"{h%24:02d}:00" for h in target_hours]

            self._strategy_cache[cache_key] = {"time": now, "res": res}
            return res
        finally:
            self._calculating_strategy = old_calc
