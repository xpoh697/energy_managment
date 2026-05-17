# Energy management strategy buy - v11.9.743
# Version change trace v11.9.743: Fix 'log' key mismatch (Dictionary vs String) for sensor.py.
import logging
_LOGGER = logging.getLogger(__name__)
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Tuple, Optional
from homeassistant.util import dt as dt_util

if TYPE_CHECKING:
    from .sensor import EnergyProfileManager

from .const import (
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
    
    def get_market_strategy(self, mode="buy", sell_commands=None, allow_recalc=True):
        """Standardized Buying Strategy v11.9.180+"""
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
        
        _b_soc_s, _b_cap_s, _ = man.get_battery_state()
        b_soc_current = float(_b_soc_s or 50.0)

        # v11.9.180: Extended cache time for stability
        # v12.0.82: Cache invalidation if physical SOC deviates significantly
        cache_key = f"market_strategy_{mode}"
        cached = self._strategy_cache.get(cache_key)
        if cached and (now - cached["time"]).total_seconds() < 120 and cached["time"].hour == now.hour:
            cached_soc = cached.get("start_soc", b_soc_current)
            if abs(b_soc_current - cached_soc) <= 3.0:
                return cached["res"]

        if not allow_recalc:
            return {
                "state": "idle", 
                "reason": "Ожидание инициализации", 
                "active_hours": [],
                "target_soc": 0.0,
                "recommended_power_kw": 0.0,
                "arbitrage_decision": "Ожидание",
                "strategy_decision": "Ожидание",
                "charge_reason": "Ожидание инициализации",
                "is_charging_now": False,
                "today_prices": {},
                "tomorrow_prices": {},
                "raw_commands": {}
            }

        _b_soc_s, _b_cap_s, _ = man.get_battery_state()
        b_cap = float(_b_cap_s or 10.0)
        b_soc = float(_b_soc_s or 50.0)
        max_p = float(normalize_float(man.get_setting(CONF_BATTERY_MAX_POWER, 3.0)))
        deg_cost = float(self.get_battery_degradation_cost())
        prof_thresh = float(man.get_setting(CONF_ARBITRAGE_PROFIT_THRESHOLD, 0.5))

        res = {
            "strategy_version": VERSION,
            "state": "standard",
            "mode": mode,
            "active_hours": [],
            "active_periods": "",
            "recommended_power_kw": 0.0,
            "recommended_amps": 0.0,
            "analyzed_window": "Нет данных",
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
        
        _buy_debug = res.setdefault("buy_debug", {})
        
        old_calc = bool(getattr(self, "_calculating_strategy", False))
        self._calculating_strategy = True
        
        price_buy_limit = float(man.get_setting(CONF_PRICE_BUY_LIMIT, 0.05))
        target_soc = b_soc
        charge_commands = {}
        target_hours = []
        negative_hours = []
        

        user_limit = float(man.get_setting(CONF_AI_DISCHARGE_LIMIT, 20.0))
        charge_limit = float(man.get_setting(CONF_AI_CHARGE_LIMIT, 100.0))
        res["limit_used"] = price_buy_limit
        res["discharge_limit"] = user_limit
        res["charge_limit"] = charge_limit
        
        try:
            cur_hour = int(now.hour)
            today_str = now.strftime("%Y-%m-%d")
            tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
            sim_range = list(range(cur_hour, cur_hour + 48))

            # Construct mode overrides from active manual overrides
            m_manual_overrides = {}
            for i, h_abs in enumerate(sim_range):
                h_dt = (now + timedelta(hours=i)).replace(minute=0, second=0, microsecond=0)
                h_ts_key = h_dt.strftime("%Y-%m-%d %H:00")
                manual_m = man.hourly_manual_overrides.get(h_ts_key)
                if manual_m:
                    m_manual_overrides[h_abs] = manual_m.get("mode")
                elif h_dt.strftime("%Y-%m-%d") == now.strftime("%Y-%m-%d") and h_dt.hour in man.manual_mode_overrides:
                    m_manual_overrides[h_abs] = man.manual_mode_overrides[h_dt.hour]
            
            p_buy_st = dict(man.data.get("prices_buy", {}))
            today_prices = dict(p_buy_st.get(today_str, {}))
            tomorrow_prices = dict(p_buy_st.get(tomorrow_str, {}))
            
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

            _sorted_h = sorted(all_buy_prices.keys())
            _final_buy = {}
            for h in _sorted_h:
                if h < cur_hour: continue
                if _final_buy and (h - max(_final_buy.keys()) > 12): break
                _final_buy[h] = all_buy_prices[h]
            all_buy_prices = _final_buy

            if not all_buy_prices: return res

            cur_p_f = all_buy_prices.get(cur_hour, 99.0)
            buy_limit = price_buy_limit
            eff = float(self.get_efficiency_coefficient() or 1.0)
            
            negative_hours = [h for h, p in all_buy_prices.items() if p < 0.0]

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
            candidates = []
            if negative_hours:
                target_hours = negative_hours
                res["charge_reason"] = "Отрицательная цена"
                res["arbitrage_decision"] = f"Отрицательная цена ({cur_p_f:.2f})"
            else:
                dynamic_buy = bool(man.get_setting(CONF_DYNAMIC_SOC_BUY, True))
                for h, p in all_buy_prices.items():
                    if p <= buy_limit or (dynamic_buy and is_buy_profitable_arb(p, h)):
                        candidates.append(h)
                
                if candidates:
                    min_p = min(all_buy_prices[h] for h in candidates)
                    target_hours = [h for h in candidates if all_buy_prices[h] <= min_p + 0.05]
                    res["charge_reason"] = "Дешево" if not any(is_buy_profitable_arb(all_buy_prices[h], h) for h in target_hours) else "Арбитраж"
                    res["arbitrage_decision"] = f"Ценовое окно ({cur_p_f:.2f})" if cur_hour in target_hours else "Ожидание окна"
                else:
                    res["charge_reason"] = "Нет"
                    res["state"] = "price_limit_not_met"

            min_soc = float(man.get_setting(CONF_MIN_SOC_BAT, 10.0))
            survival_hours = set(target_hours)
            avg_price = sum(all_buy_prices.values()) / len(all_buy_prices) if all_buy_prices else 0.0
            
            morning_h = man.get_sunrise_hour() or 8
            morning_h_abs = morning_h + (24 if cur_hour >= 4 else 0)
            
            # v11.9.434: Find the absolute cheapest hour before sunrise for optimal charging
            all_pre_sunrise = [h for h in all_buy_prices.keys() if h < morning_h_abs]
            cheapest_global = min(all_pre_sunrise, key=lambda h: all_buy_prices[h]) if all_pre_sunrise else cur_hour
            
            # v12.0.81: Get resolved consumption profile details for UI display
            _, _, p_used_label = self.resolve_consumption_profiles("consumption_base", 14, man.day_type)
            _buy_debug["profile_used"] = p_used_label

            survival_targets = {} # {hour: target_soc}

            for _loop_i in range(12):
                added = False
                # v11.9.473: Use 48h horizon for planning to match UI simulation
                sim_range = list(range(cur_hour, cur_hour + 48))
                sim_cmds = {h: max_p for h in survival_hours}
                
                # v11.9.714: Inject planned sell commands into survival simulation
                if sell_commands:
                    for h_str, p in sell_commands.items():
                        try:
                            h_int = int(h_str)
                            if h_int not in sim_cmds:
                                sim_cmds[h_int] = -abs(float(p)) # Discharge power as negative
                        except (ValueError, TypeError):
                            continue
                
                # v11.9.527: Merge current real-time activity into survival sim commands
                _p_raw_s = float(man.get_sensor_float(man.battery_power_sensor) or 0.0)
                if _p_raw_s < -0.05:
                    sim_cmds[cur_hour] = abs(_p_raw_s)
                
                _, log, _ = self.run_soc_simulation(
                    b_soc, sim_range, now, sim_cmds, 
                    allow_discharge=True, house_profile_override="consumption_base",
                    mode_overrides=m_manual_overrides
                )
                
                deadline_h = None
                
                for h_step in sim_range:
                    soc_h = self._get_soc_from_log(log, h_step, 100.0)
                    
                    # v11.9.724: Unified Trigger Logic (Deadline ONLY)
                    # We only care if simulation predicts hitting MinSOC + 5.0% (18%)
                    limit = min_soc + 5.0
                    
                    if deadline_h is None and soc_h < limit:
                        deadline_h = h_step 
                        break
                
                # v11.9.711: Capture debug samples from the first simulation pass
                if _loop_i == 0 and deadline_h is not None:
                    res["deadline_hour"] = deadline_h
                    res["survival_status"] = f"Дедлайн в {deadline_h%24:02d}:00"
       
                # v11.9.711: Capture debug samples from the first simulation pass
                if _loop_i == 0:
                    res["buy_debug"]["sim_keys_sample"] = [f"'{k}'" for k in list(log.keys())[:3]]
                    res["buy_debug"]["morning_lookup_key"] = morning_h_abs
                    # v11.9.712: Expanded samples
                    tom_h = cur_hour + 24
                    res["buy_debug"]["tomorrow_lookup_key"] = tom_h
                    res["buy_debug"]["tomorrow_sim_key"] = next((f"'{k}'" for k in log.keys() if self.is_tomorrow_log_key(k)), "Not found")
                    
                    # v11.9.713: Manual Override Keys Diagnostic
                    res["buy_debug"]["manual_override_keys"] = list(man.hourly_manual_overrides.keys())
                    res["buy_debug"]["timestamp_sample"] = (now + timedelta(hours=9)).strftime("%Y-%m-%d %H:00")
                
                if deadline_h is not None:
                    # Hour X is the critical deadline.
                    hour_X = deadline_h
                    morning_h_abs = (man.get_sunrise_hour() or 8) + (24 if cur_hour >= 4 else 0)
                    
                    # v11.9.622: Search for USER anchors (Manual Sell)
                    anchor_h = cur_hour - 1
                    # v11.9.722: Include violation hour itself in anchor scan (+1)
                    for offset in range(max(0, deadline_h - cur_hour + 1)):
                        h_abs = cur_hour + offset
                        h_dt = (now + timedelta(hours=offset)).replace(minute=0, second=0, microsecond=0)
                        h_ts_key = h_dt.strftime("%Y-%m-%d %H:00")
                        manual_m = man.hourly_manual_overrides.get(h_ts_key)
                        if manual_m and manual_m.get("mode") == "sale_pv_bat":
                            anchor_h = max(anchor_h, h_abs)

                    # Get candidates strictly AFTER the anchor
                    candidates_after = [h for h in all_buy_prices.keys() if h > anchor_h and h < morning_h_abs and h not in survival_hours]
                    
                    if not candidates_after:
                        # Emergency Fallback: If no slots after sale, pick absolute cheapest global
                        target_h = cheapest_global
                        target_type = "Gatekeeper"
                    else:
                        best_after = min(candidates_after, key=lambda h: all_buy_prices[h])
                        if best_after <= hour_X:
                            # Case A: Best slot after sale is before deadline
                            target_h = best_after
                            target_type = "Gatekeeper"
                        else:
                            # Case B: Best slot is past deadline. Bridge needed after sale but before deadline.
                            bridge_candidates = [h for h in candidates_after if h <= hour_X]
                            if bridge_candidates:
                                target_h = min(bridge_candidates, key=lambda h: all_buy_prices[h])
                                target_type = "Bridge"
                            else:
                                # No bridge possible after sale. Take first available hour after sale.
                                target_h = min(candidates_after)
                                target_type = "Bridge"

                    # v11.9.719: Save target and update iteration flag (OUTSIDE B/A blocks)
                    # v11.9.733: Use Survival_Floor + 5% (TS 4.2.1.3)
                    survival_targets[target_h] = self.get_survival_floor(target_h, morning_h_abs) + 5.0
                    survival_hours.add(target_h)
                    added = True
                if not added: break
            
            target_hours = sorted(list(survival_hours))
            if any(h not in (negative_hours or (candidates if 'candidates' in locals() else [])) for h in target_hours):
                res["charge_reason"] = "Выживание"

            morning_h = man.get_sunrise_hour() or 8
            morning_h_abs = morning_h + (24 if cur_hour >= 4 else 0)
            
            # v11.9.434: Dynamic Target SOC based on Bridge/Gatekeeper status
            current_survival_target = survival_targets.get(cur_hour)
            if current_survival_target:
                survival_target = current_survival_target
            else:
                last_h = max(target_hours) if target_hours else cur_hour
                # v11.9.723: Target is always Survival_Floor + 5.0% (TS 4.2.1.3)
                survival_target = self.get_survival_floor(last_h, morning_h_abs) + 5.0
            
            _buy_debug["survival_floor"] = round_f(self.get_survival_floor(cur_hour, morning_h_abs), 1)
            _buy_debug["gatekeeper_floor"] = round_f(self.get_gatekeeper_floor(cur_hour, morning_h_abs), 1)
            _buy_debug["deadline_level"] = round_f(min_soc + 5.0, 1)
            _buy_debug["cheapest_global"] = f"{cheapest_global%24:02d}:00"
            _buy_debug["survival_targets"] = {f"{h%24:02d}h": round_f(t, 1) for h, t in survival_targets.items()}

            base_limit = float(man.get_setting(CONF_AI_CHARGE_LIMIT, 100.0))
            
            if res.get("charge_reason") == "Отрицательная цена":
                target_soc = 100.0
            elif res.get("charge_reason") == "Выживание":
                target_soc = min(base_limit, survival_target)
            else:
                target_soc = base_limit
            
            res["survival_target"] = survival_target
            res["target_soc"] = round_f(target_soc, 1)
            
            # v11.9.475: Ensure survival hours are included in the allocation window
            # even if their price is above the market limit.
            final_target_set = set(target_hours) | survival_hours
            target_hours = sorted(list(final_target_set))
            
            charge_commands = {}
            soc_at_start_plan = b_soc
            soc_end = b_soc
            soc_morning = b_soc

            # v11.9.573: Unconditional Solar-Aware Status
            # 1. Prepare Solar Horizon
            sunset_h = man.get_sunset_hour() or 18
            # Use tomorrow's sunset if we are late in the day (after 15:00) or no windows found today
            planning_h = min(target_hours) if target_hours else (cur_hour + 24 if cur_hour > 15 else cur_hour)
            sunset_abs = sunset_h + (24 if planning_h >= 24 else 0)
            
            # 2. Run baseline simulation to see if solar is enough
            _sim_h_disp = list(range(cur_hour, max(sunset_abs, (max(target_hours) if target_hours else cur_hour)) + 1))
            _p_raw = float(man.get_sensor_float(man.battery_power_sensor) or 0.0)
            current_cmd = {cur_hour: abs(_p_raw)} if _p_raw < -0.05 else {}
            _, _log_sun, _ = self.run_soc_simulation(
                b_soc, _sim_h_disp, now, current_cmd, 
                allow_discharge=True, no_solar_to_bat=False, house_profile_override="consumption_base",
                mode_overrides=m_manual_overrides
            )
            
            # v11.9.602: Peak Solar Awareness
            # If at ANY hour from now until sunset the battery reaches 95% from solar, 
            # then grid charging at positive prices is absolutely NOT needed.
            hours_until_sunset = [h for h in _sim_h_disp if h <= sunset_abs]
            if not hours_until_sunset: hours_until_sunset = [cur_hour]
            
            peak_soc_before_sunset = max([self._get_soc_from_log(_log_sun, h, 0.0) for h in hours_until_sunset])
            is_solar_enough = bool(peak_soc_before_sunset >= 95.0)
            
            soc_at_sunset = self._get_soc_from_log(_log_sun, sunset_abs, b_soc)
            
            charge_commands = {}
            if not target_hours:
                if is_solar_enough:
                    decision_str = f"Скип (Солнце {soc_at_sunset:.0f}%)"
                else:
                    decision_str = f"Дорого (>{buy_limit:.2f})"
                res["charge_reason"] = "Нет"
                res["analyzed_window"] = "Нет окон"
                res["active_periods"] = ""
                needed_kwh_dc = max(0.0, (target_soc - b_soc) * b_cap / 100.0)
            else:
                # We have candidate hours. Decide if we actually need to buy.
                planning_h = min(target_hours)
                soc_at_start_plan = self._get_soc_from_log(log, planning_h, b_soc)
                cur_p = all_buy_prices.get(cur_hour, 99.0)

                # v12.0.81: Scan all hours before morning sunrise to correctly see the nighttime dip (breach)
                pre_sunrise_hours = [h for h in sim_range if h < morning_h_abs]
                if not pre_sunrise_hours:
                    pre_sunrise_hours = [cur_hour]
                min_predicted_soc = min([self._get_soc_from_log(log, h, 100.0) for h in pre_sunrise_hours])
                
                # v11.9.623: Survival Priority. Never skip survival due to future solar.
                if cur_p > 0 and is_solar_enough and res.get("charge_reason") != "Выживание":
                    needed_kwh_dc = 0.0
                    res["charge_reason"] = "Скип (Будет солнце)"
                    decision_str = f"Скип (Пик Солнца {peak_soc_before_sunset:.0f}%)"
                    _buy_debug["solar_skip"] = f"Peak SOC before sunset {peak_soc_before_sunset:.1f}% >= 95%"
                elif res.get("charge_reason") == "Отрицательная цена":
                    needed_kwh_dc = max(0.0, (target_soc - soc_at_start_plan) * b_cap / 100.0)
                    decision_str = f"Зарядка (Минус {cur_p:.2f})"
                elif res.get("charge_reason") == "Выживание":
                    # v11.9.732: Use minimum predicted dip to calculate required survival energy
                    needed_kwh_dc = max(0.0, (target_soc - min_predicted_soc) * b_cap / 100.0)
                    decision_str = f"Зарядка (Выживание)"
                    if cur_hour not in target_hours:
                        decision_str = "Ожидание окна"
                else:
                    needed_kwh_dc = max(0.0, (target_soc - soc_at_start_plan) * b_cap / 100.0)
                    if needed_kwh_dc > 0:
                        decision_str = f"Зарядка ({res.get('charge_reason')})"
                        if cur_hour not in target_hours:
                            decision_str = "Ожидание окна"
                    else:
                        decision_str = "Ожидание окна"
                
                _buy_debug["min_predicted_soc"] = round_f(min_predicted_soc, 1)
                _buy_debug["needed_kwh_dc_survival"] = round_f(needed_kwh_dc, 3)

                # v11.9.200: Advanced Allocator (Price-Priority with progressive SOC)
                # 1. Sort by price to fill cheapest hours first
                sorted_by_price = sorted(target_hours, key=lambda x: all_buy_prices.get(int(x), 100.0))
                
                # 2. Fill energy budget based on price attractiveness
                accum_kwh_dc = 0.0
                for h in sorted_by_price:
                    # v11.9.625: Strict Solar Guard.
                    # For this specific hour 'h', check if solar will fill the battery 
                    # at ANY point between 'h' and sunset.
                    # v11.9.625: Disabled for Survival mode.
                    h_future_until_sunset = [sh for sh in _sim_h_disp if sh >= h and sh <= sunset_abs]
                    if h_future_until_sunset and res.get("charge_reason") != "Выживание":
                        peak_after_h = max([self._get_soc_from_log(_log_sun, sh, 0.0) for sh in h_future_until_sunset])
                        if peak_after_h >= 95.0 and all_buy_prices.get(h, 0) > 0:
                            charge_commands[h] = 0.0
                            continue

                    if accum_kwh_dc >= (needed_kwh_dc - 0.01) and all_buy_prices.get(h, 0) > 0:
                        charge_commands[h] = 0.0
                        continue
                        
                    h_factor = max(0.1, (60 - now.minute)/60.0) if h == cur_hour else 1.0
                    rem_needed = max(0.0, (needed_kwh_dc - accum_kwh_dc))
                    p_needed = rem_needed / (h_factor * eff) if h_factor > 0 else 0
                    
                    # Estimate CC/CV based on projected SOC at this point
                    est_soc = soc_at_start_plan + (accum_kwh_dc / b_cap * 100.0)
                    cc_cv = self.get_cc_cv_ratio(est_soc)
                    
                    p_charge = min(max_p, max_p * cc_cv, p_needed)
                    charge_commands[h] = round_f(p_charge, 3)
                    accum_kwh_dc += (p_charge * h_factor * eff)
                
                # v11.9.645: Emergency Plan Trace
                _plan_details = [f"{h%24:02d}h:{p:.2f}kW" for h, p in charge_commands.items() if p > 0]
                man.log_to_file(f"[Strategy Buy Plan] Reason: {res.get('charge_reason')} | Target: {target_soc}% | Plan: {', '.join(_plan_details)}")
                
                # v11.9.574: TS compliance (Negative prices block PV charging - section 152)
                if res.get("charge_reason") == "Отрицательная цена":
                    res["no_battery_charge_until"] = max(target_hours) + 1
                
                # Check if allocator actually planned anything
                actual_planned = sum(charge_commands.values())
                if actual_planned <= 0.05 and needed_kwh_dc > 0.1 and res.get("charge_reason") != "Выживание":
                    # v11.9.604: We needed energy, but skipped all windows because solar will fill later
                    decision_str = "Скип (Солнце наполнит)"
                    res["charge_reason"] = "Скип (Солнце)"
                
                res["analyzed_window"] = f"До {max(target_hours)%24:02d}:59"
                res["active_periods"] = group_h(target_hours)

            res["strategy_decision"] = decision_str
            # v11.9.582: Removed sell_strategy call to avoid recursion deadlock
            
            # v11.9.200: Debug Logging for Allocator
            _dbg_log = f"[Strategy Buy Debug] Status: {decision_str} | Target: {target_soc}% | StartSOC: {soc_at_start_plan:.1f}% | Need: {needed_kwh_dc:.3f} kWh | Cap: {b_cap:.1f}"
            man.log_to_file(_dbg_log)
            
            # v11.9.473: Pass dynamic floors to final simulation to distinguish House vs Trade limits
            sim_range = list(range(cur_hour, cur_hour + 48))
            d_floors = {h: self.get_gatekeeper_floor(h, morning_h_abs) for h in sim_range}
            
            # v11.9.481: Debug charge_commands integrity
            if charge_commands:
                _LOGGER.debug(f"[Strategy Buy] Final Charge Commands: {charge_commands}")
            
            # v11.9.491: Final debug of command mapping
            if charge_commands:
                _LOGGER.debug(f"[Strategy Buy] FINAL Simulation Keys: {list(charge_commands.keys())} | Vals: {list(charge_commands.values())}")
            
            # v11.9.741: Diagnostic - trace charge_commands before sim
            _charge_trace = [f"{h}:{p}kW" for h, p in charge_commands.items() if p > 0.05]
            man.log_to_file(f"[Strategy Buy Diagnostic] Final Commands: {', '.join(_charge_trace)}")
            
            # v11.9.740: Inject mode_overrides to ensure simulator knows we are in 'buy' mode
            m_overrides = {h: "buy" for h, p in charge_commands.items() if p > 0.05}
            for h_abs_override, m_name in m_manual_overrides.items():
                m_overrides[h_abs_override] = m_name
            
            # 3. Final Simulation to get REAL progressive SOC levels (Chronological)
            _, sim_log, _ = self.run_soc_simulation(b_soc, sim_range, now, charge_commands, allow_discharge=True, no_solar_to_bat=False, b_min_soc=min_soc, dynamic_floors=d_floors, mode_overrides=m_overrides)

            # v11.9.741: Diagnostic - trace sim result for charging hours
            for h, p in charge_commands.items():
                if p > 0.05:
                    _s_val = self._get_soc_from_log(sim_log, h, -1.0)
                    man.log_to_file(f"[Strategy Buy Diagnostic] Hour {h}: Cmd {p}kW -> Result SOC: {_s_val}%")
            
            # v11.9.742: Critical - use HH:00 keys to match sensor.py expectations perfectly.
            res["soc_simulation"] = {f"{h%24:02d}:00" + (" (Завтра)" if h >= 24 else ""): self._get_soc_from_log(sim_log, h, b_soc) for h in sim_range}
            res["charge_commands_debug"] = charge_commands
            
            # 3b. Survival-only simulation for debug (to see what 'Survival Bridge' sees)
            _, sim_log_base, _ = self.run_soc_simulation(b_soc, sim_range, now, charge_commands, allow_discharge=True, no_solar_to_bat=True, b_min_soc=min_soc, house_profile_override="consumption_base", dynamic_floors=d_floors, mode_overrides=m_overrides)
            
            # v11.9.427: Use the last hour OF CHARGING for soc_end, to match Planned Power display
            last_charge_h = max([h for h, p in charge_commands.items() if p > 0.05], default=cur_hour)
            soc_end = self._get_soc_from_log(sim_log, last_charge_h, b_soc)
            
            res["gatekeeper_floor"] = self.get_gatekeeper_floor(last_charge_h + 1, morning_h_abs)
            res["survival_floor"] = self.get_survival_floor(last_charge_h + 1, morning_h_abs)
            
            soc_morning = self._get_soc_from_log(sim_log, morning_h_abs - 1, None)
            if soc_morning is None:
                soc_morning = soc_end
                
            soc_morning_base = self._get_soc_from_log(sim_log_base, morning_h_abs - 1, soc_morning)
            # v11.9.615: Ultra-Detailed Debug Log
            def fmt_log(log_dict):
                return " | ".join([
                    f"{int(h)%24:02d}: {v['soc']:.0f}% (G:{v.get('gen_kw',0.0):.1f}|C:{v.get('cons_kw',0.0):.1f}|N:{v.get('p_bat',0.0):.1f})" 
                    for h, v in log_dict.items() if isinstance(h, int) and h < 48
                ])
            # v11.9.739: Ensure debug log uses the final simulation (sim_log) which includes charges.
            # v11.9.742: Ensure debug log also uses predictable UI keys
            res["buy_debug"]["sim_log_24h"] = {
                (f"{int(k):02d}:00" if isinstance(k, int) else f"{int(str(k).split(':')[0]):02d}:00") + 
                (" (Завтра)" if self.is_tomorrow_log_key(k) else (" (Через день)" if self.is_dafter_log_key(k) else "")): v["soc"] 
                for k, v in list(sim_log.items())[:36]
            }
            res["projected_soc"] = round_f(self._get_soc_from_log(sim_log, cur_hour, b_soc), 1)
            
            log_str_base = fmt_log(sim_log_base)
            log_str_final = fmt_log(sim_log)

            res["buy_simulation"] = {
                "projected_soc_at_start_pct": round_f(soc_at_start_plan, 1),
                "projected_soc_at_end_pct": round_f(soc_end, 1),
                "projected_soc_morning_pct": round_f(soc_morning, 1),
                "projected_soc_morning_base_pct": round_f(soc_morning_base, 1),
                "eff": 0.98,
                "b_cap": round_f(b_cap, 2),
                "needed_kwh_dc": round_f(needed_kwh_dc, 3),
                "max_p": round_f(max_p, 2),
                "p_total_planned": round_f(sum(charge_commands.values()), 3),
                "sim_log_base": log_str_base,
                "sim_log": log_str_final,
                "log": sim_log
            }
            res["charge_commands"] = charge_commands
            res["recommended_power_kw"] = charge_commands.get(cur_hour, 0.0)
            
            is_neg = bool(all_buy_prices.get(cur_hour, 1.0) <= 0.0)
            res["is_charging_now"] = bool(res["recommended_power_kw"] > 0.05 or is_neg)
            
            # v11.9.519: If we have a planned charge for NOW, state MUST be active
            if res["is_charging_now"]:
                res["state"] = "active"
            
            v_val = 52.0
            if man.battery_voltage_sensor:
                v_val = float(man.get_sensor_float(man.battery_voltage_sensor) or 52.0)
            res["recommended_amps"] = round_f((charge_commands.get(cur_hour, 0.0) * 1000.0) / v_val, 1) if v_val > 0 else 0.0
            
            strat_log = f"[Strategy Buy] SOC: {b_soc:.1f}% | Power: {res['recommended_power_kw']:.1f} kW | Reason: {res['charge_reason']} | Target: {target_soc:.1f}% | Now Charging: {res['is_charging_now']} | Windows: {res.get('active_periods','')}"
            if str(strat_log) != str(getattr(self, "_last_strat_log", "")):
                man.log_to_file(strat_log)
                self._last_strat_log = strat_log
            
            planned_results = {}
            for h, p in charge_commands.items():
                if p > 0.05:
                    h_fmt = f"{h%24:02d}:00" + (" (Завтра)" if h >= 24 else "")
                    # v11.9.746: Take SOC from the FINAL sim_log, not the search loop log.
                    h_soc = self._get_soc_from_log(sim_log, h, b_soc)
                    planned_results[h_fmt] = {
                        "power": round_f(p, 3),
                        "soc": round_f(h_soc, 1)
                    }
            res["planned_power_per_h"] = planned_results
            res["target_soc"] = round_f(target_soc, 1)
            res["active_hours"] = [int(h) for h, v in charge_commands.items() if v > 0.05 or all_buy_prices.get(int(h), 1.0) <= 0.0]
            if negative_hours:
                res["first_negative_hour"] = min(negative_hours)
                res["last_negative_hour"] = max(negative_hours)
                res["can_wait_for_negative"] = True
            
            # v11.9.421: Expanded Survival Debug
            v_hour = res.get("deadline_hour")
            v_text = f"Дедлайн в {v_hour%24:02d}:00" if v_hour is not None else "Без дедлайнов"

            if res["state"] == "active":
                res["state"] = "active"
                res["power"] = charge_commands.get(cur_hour, 0.0)

            _neg_tag = "[Отрицательная цена]" if cur_hour in negative_hours else ""
            if not _neg_tag and negative_hours:
                _neg_tag = "Ожидание отрицательных цен"
            
            future_sell = {hs: ps for hs, ps in all_sell_prices.items() if hs > cur_hour}
            _best_s = max(future_sell.values()) if future_sell else 0.0
            _gain = float(_best_s * eff - cur_p_f - deg_cost) if _best_s > 0 else 0.0
            _is_arb = bool(_gain >= threshold)
            future_buy = {hb: pb for hb, pb in all_buy_prices.items() if hb > cur_hour}
            _best_b = min(future_buy.values()) if future_buy else 0.0
            # v11.9.730: Consolidated debug output (removed diag_* duplicates)
            res["buy_debug"].update({
                "summary": f"{_neg_tag} | Цена: {cur_p_f:.2f} | Цель: {target_soc:.1f}%".strip(" | "),
                "current_price": cur_p_f,
                "target_soc": round_f(target_soc, 1),
                "morning_soc_total": round_f(soc_morning, 1),
                "morning_soc_base": round_f(soc_morning_base, 1),
                "survival_status": v_text,
                "is_arbitrage_profitable": _is_arb,
                "best_sell_later": round_f(_best_s, 2),
                "best_buy_later": round_f(_best_b, 2),
                "arbitrage_gain": round_f(_gain, 3),
                "negative_prices_upcoming": bool(negative_hours),
                "charge_reason": res.get("charge_reason", "Нет"),
                "gatekeeper_floor": round_f(res.get("gatekeeper_floor", 0.0), 1),
                "survival_floor": round_f(res.get("survival_floor", 0.0), 1),
                "target_hours": target_hours,
                "candidates": candidates,
                "commands": {f"{h}h": p for h, p in charge_commands.items() if p > 0},
            })
            
            # v11.9.729: Inject simulation samples and anchors from loop
            res["buy_debug"].update(_buy_debug)

            txt = "Ожидание окна"
            reason = res.get("charge_reason", "")
            has_plan = bool(charge_commands and any(p > 0.05 for p in charge_commands.values()))

            if res["state"] == "active":
                if reason == "Отрицательная цена": txt = "Зарядка (Отриц. цена)"
                elif reason == "Арбитраж": txt = "Зарядка (Арбитраж)"
                elif reason == "Выживание": txt = "Зарядка (Выживание)"
                else: txt = "Зарядка (Дешево)"
            elif has_plan:
                # v11.9.598: Show reason even if the window is in the future
                prefix = "Запланировано"
                if reason == "Отрицательная цена": txt = f"{prefix} (Отриц. цена)"
                elif reason == "Арбитраж": txt = f"{prefix} (Арбитраж)"
                elif reason == "Выживание": txt = f"{prefix} (Выживание)"
                else: txt = f"{prefix} (Дешево)"
                
                # Add time hint
                next_h = min([h for h, p in charge_commands.items() if p > 0.05 and h > cur_hour], default=None)
                if next_h is not None:
                    h_fmt = f"{next_h%24:02d}:00" + (" (Завтра)" if next_h >= 24 else "")
                    txt += f" в {h_fmt}"
            elif reason == "Выживание":
                txt = "Запланировано выживание"
            elif reason == "Нет":
                txt = "В покупке нет необходимости"
            res["current_mode_text"] = txt
            res["strategy_decision"] = txt
            
            # v11.9.606: Restore technical info to power_decision
            if res["state"] == "active":
                p_val = res["recommended_power_kw"]
                res["power_decision"] = f"Зарядка {p_val:.2f} кВт ({reason})"
            elif has_plan:
                next_h = min([h for h, p in charge_commands.items() if p > 0.05 and h > cur_hour], default=None)
                h_fmt = f"{next_h%24:02d}:00" + (" (Завтра)" if next_h >= 24 else "")
                res["power_decision"] = f"Запланировано {charge_commands.get(next_h, 0):.2f} кВт в {h_fmt}"
            else:
                res["power_decision"] = "Ожидание окна"
            
            res["raw_commands"] = charge_commands
            # v11.9.586: Integrate shared arbitrage logic
            # v11.9.596: Sync Arbitrage Info logic with Sell strategy
            sell_targets = [h for h, p in all_sell_prices.items() if h > cur_hour and p >= max(all_sell_prices.values()) - 0.05]
            arb_info = self._get_arbitrage_info(cur_hour, all_buy_prices, all_sell_prices, sell_targets)
            res["arbitrage_decision"] = arb_info["arbitrage_decision"]

            self._strategy_cache[cache_key] = {"time": now, "res": res, "start_soc": b_soc}
            return res
        finally:
            self._calculating_strategy = old_calc
