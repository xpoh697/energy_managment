import json

path = r'\\192.168.100.5\config\energy_management_backup.json'
try:
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # Search for blended coeff in saved data or logs
    # It's not usually saved in data keys directly, but let's check profile_averaged results
    print("Checking for adaptation coefficient indicators...")
    
    # Check if we have any recorded waste that implies high potential
    waste = data.get('temp_daily_waste', 0)
    print(f"Daily waste recorded so far: {waste:.3f} kWh")

    # The manager has self.last_blended_coeff but it's volatile.
    # We can try to infer it from history.
    # Generation today so far (hour 0 to 10)
    gen_today_so_far = data.get('hourly_accumulators', {}).get('generation', 0)
    print(f"Generation in current hour: {gen_today_so_far} kWh")
    
    # Total gen today from temp_daily_gen
    total_gen_today = data.get('temp_daily_gen', 0)
    print(f"Total generation today (reported): {total_gen_today} kWh")

except Exception as e:
    print(f"Error: {e}")
