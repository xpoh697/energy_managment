            # 2. Elementary Allocator (v11.8.449: Back to Basics, TS 90, 102)
            # Budget is the ONLY thing that matters. Sort by price, fill until budget is gone.
            h_by_priority = sorted(target_hours, key=lambda h: all_sell_prices.get(h, 0.0), reverse=True)
            
            # Convert budget to kWh DC for calculation (avoid efficiency confusion in loop)
            remaining_budget_dc = (soc_at_start - active_safety_floor) * b_cap / 100.0
            
            for h_target in h_by_priority:
                if remaining_budget_dc <= 0.01:
                    sell_commands[h_target] = 0.0
                    continue
                
                # Max energy we can take in 1 hour (limited by inverter AC power converted to DC)
                max_energy_h_dc = (max_p / eff) * 1.0 # 1 hour
                
                # Take what we can
                take_dc = min(remaining_budget_dc, max_energy_h_dc)
                
                # Convert back to AC for the command
                sell_commands[h_target] = round_f(take_dc * eff, 3)
                remaining_budget_dc -= take_dc
                active_h.append(h_target)

            # v11.8.449: Final validation simulation (Projection only)
            sim_range_full = list(range(cur_hour, cur_hour + 48))
            _, sim_log, _ = self.run_soc_simulation(
                b_soc, sim_range_full, now, 
                commands={h: -p for h, p in sell_commands.items()}, 
                b_min_soc=min_soc_val, dynamic_floors=floors_sliding,
                ignore_blended=True, house_profile_override="consumption_base"
            )
            
            # --- Stage 4: Build Plan ---
            planned_results = {}
            sorted_h = sorted(sell_commands.keys())
            active_h = [h for h, p in sell_commands.items() if p > 0.05]
            limit_reason = "Активная продажа (Приоритет: Цена)" if active_h else "Ожидание пика"
            
            for h in sorted_h:
                p = sell_commands.get(h, 0.0)
                if p <= 0.05: continue
                
                h_sim_key = f"{h%24:02d}:59" + (" (Завтра)" if h >= 24 else "")
                sim_entry = sim_log.get(h_sim_key, {})
                real_p = float(sim_entry.get("p_bat", 0.0))
                sim_soc = float(sim_entry.get("soc", b_soc))
                
                # Diagnostics: Determine why we aren't selling at max_p
                if real_p < p - 0.1:
                    if abs(sim_soc - user_limit) < 0.2:
                        limit_reason = "Лимит пользователя"
                    elif sim_soc < b_min_soc + soc_buffer + 1.0:
                        limit_reason = "Gatekeeper"
                    else:
                        limit_reason = "Утренний лимит"
                
                planned_results[f"{h%24:02d}:00" + (" (Завтра)" if h >= 24 else "")] = {
                    "power": round_f(real_p, 3),
                    "soc": round_f(sim_soc, 1)
                }
