import json
from datetime import datetime, timedelta

# Mocking HA dt_util.now()
now = datetime.now() # We'll assume the time in the backup is roughly now
cur_hour = now.hour

with open(r'\\192.168.100.5\config\energy_management_backup.json', 'r', encoding='utf-8') as f:
    data = json.load(f)

def get_average_profile(profile_type, days, day_type="all"):
    profile = {}
    profile_data = data.get(profile_type, {})
    for h in range(24):
        sh = str(h)
        history = profile_data.get(sh, [])
        relevant = history[-days:] if days > 0 else history
        valid_vals = []
        for item in relevant:
            v = item.get('v', 0.0)
            wd = item.get('wd')
            if wd is not None:
                if day_type == "weekday" and wd >= 5: continue
                if day_type == "weekend" and wd < 5: continue
            valid_vals.append(v)
        if valid_vals:
            profile[str(h)] = sum(valid_vals) / len(valid_vals)
        else:
            profile[str(h)] = 0.0
    return profile

def simulate_budget():
    forecast_val = data.get('temp_max_forecast', 0.0)
    
    # Simple coeff calculation
    history = data.get("forecast_history", [])
    hist_coeff = 1.0
    if history:
        tot_actual = sum(h["actual"] for h in history)
        tot_expected = sum(h["forecast"] for h in history)
        if tot_expected > 0.1:
            hist_coeff = tot_actual / tot_expected
    
    prof_gen_today = get_average_profile("generation", 14, "all")
    total_hist_gen = sum(prof_gen_today.values())
    
    fraction_so_far = 0.0
    if total_hist_gen > 0.1:
        hist_gen_so_far = sum(prof_gen_today.get(str(h), 0.0) for h in range(cur_hour))
        fraction_so_far = hist_gen_so_far / total_hist_gen
    
    actual_today = data.get("temp_daily_gen", 0.0)
    expected_today_total = data.get("temp_max_forecast", 0.0)
    expected_today_so_far = expected_today_total * fraction_so_far
    
    today_coeff = hist_coeff
    if expected_today_so_far > 0.1:
        today_coeff = actual_today / expected_today_so_far
    
    blended_coeff = (today_coeff * fraction_so_far) + (hist_coeff * (1.0 - fraction_so_far))
    forecast_val_adjusted = forecast_val * blended_coeff
    
    # Battery state - assume some values if not in backup settings (since they are live sensors)
    # The user said -3000, which is extremely large.
    # What if batt_energy_val is huge or expected_consumption is huge?
    
    batt_energy_val = 0.0 # We don't have live SOC in backup JSON usually, let's assume 0 for now to see consumption impact
    
    today_type = "weekend" if now.weekday() >= 5 else "weekday"
    tom_type = "weekend" if (now + timedelta(days=1)).weekday() >= 5 else "weekday"
    
    prof_today = get_average_profile("consumption_base", 14, today_type)
    prof_tom = get_average_profile("consumption_base", 14, tom_type)
    
    expected_consumption = 0.0
    for h in range(cur_hour, 24):
        expected_consumption += prof_today.get(str(h), 0.0)
    for h in range(0, 8):
        expected_consumption += prof_tom.get(str(h), 0.0)
        
    print(f"Current Hour: {cur_hour}")
    print(f"Forecast Adjusted: {forecast_val_adjusted}")
    print(f"Expected Consumption (Sum of hours): {expected_consumption}")
    
    budget = (forecast_val_adjusted + batt_energy_val) - expected_consumption
    print(f"Resulting Budget (w/o battery): {budget}")

simulate_budget()
