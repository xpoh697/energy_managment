with open('custom_components/energy_management/strategy.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Fix 1: last_h_sell_immediate
old1 = '''                        if future_active_sell:
                            # v11.3.16: Anchor 'after sale' projection to the end of the WHOLE continuous block
                            last_h_sell_immediate = future_active_sell[-1]
                            # Search for the first break in the sequence to define the immediate block
                            for i in range(1, len(future_active_sell)):
                                if future_active_sell[i] != future_active_sell[i-1] + 1:
                                    last_h_sell_immediate = future_active_sell[i-1]
                                    break'''

new1 = '''                        if future_active_sell:
                            # v11.6.40: Anchor 'after sale' projection to the end of the FIRST ENERGY POOL.
                            # Since hours in the same pool share the same energy with no solar recharge,
                            # the "after sale" SOC must reflect ALL sales within this pool.
                            last_h_sell_immediate = max(epochs[0]) if 'epochs' in locals() and epochs else future_active_sell[-1]
                            res["first_pool_hours"] = epochs[0] if 'epochs' in locals() and epochs else future_active_sell'''

# Fix 2: power_decision text
old2 = '''                            # v11.6.39: Partial sale status in power_decision
                            sold_now = sell_commands.get(int(cur_hour), 0.0) if 'sell_commands' in locals() else 0.0
                            future_sells = {h: p for h, p in sell_commands.items() if h > cur_hour and p > 0.01} if 'sell_commands' in locals() else {}
                            if sold_now > 0.01 and future_sells:
                                total_planned = sum(sell_commands.values())
                                next_h = min(future_sells.keys())
                                res["power_decision"] = f"{sell_diagnosis} | Часть {sold_now:.1f} из {total_planned:.1f} кВтч (Остаток в {self._format_h(next_h)})"
                            else:
                                res["power_decision"] = sell_diagnosis'''

new2 = '''                            # v11.6.40: Smart Pool Splitting Status
                            future_sells = {h: p for h, p in sell_commands.items() if h >= cur_hour and p > 0.01} if 'sell_commands' in locals() else {}
                            if future_sells:
                                h_list = sorted(future_sells.keys())
                                has_gap = any(h_list[i] > h_list[i-1] + 1 for i in range(1, len(h_list)))
                                if has_gap:
                                    first_h = h_list[0]
                                    last_h = h_list[-1]
                                    first_p = future_sells[first_h]
                                    last_p = future_sells[last_h]
                                    res["power_decision"] = f"{sell_diagnosis} | Пул разбит: {first_p:.1f}кВтч в {self._format_h(first_h)}, допродажа в {self._format_h(last_h)}"
                                else:
                                    res["power_decision"] = sell_diagnosis
                            else:
                                res["power_decision"] = sell_diagnosis'''

# Fix 3: planned_power (p_distribution)
old3 = '''                # v11.6.37: Show only nearest (first) continuous window in planned_power
                _first_window_active = [actual_active[0]]
                for i in range(1, len(actual_active)):
                    if actual_active[i] == actual_active[i-1] + 1:
                        _first_window_active.append(actual_active[i])
                    else:
                        break
                        
                for h in _first_window_active:'''

new3 = '''                # v11.6.40: Show ALL hours of the first Energy Pool in planned_power
                _first_window_active = res.get("first_pool_hours", actual_active)
                for h in sorted(_first_window_active):'''

if old1 in content:
    content = content.replace(old1, new1, 1)
    print("Replaced block 1")
else:
    print("Block 1 not found")

if old2 in content:
    content = content.replace(old2, new2, 1)
    print("Replaced block 2")
else:
    print("Block 2 not found")

if old3 in content:
    content = content.replace(old3, new3, 1)
    print("Replaced block 3")
else:
    print("Block 3 not found")

with open('custom_components/energy_management/strategy.py', 'w', encoding='utf-8') as f:
    f.write(content)
