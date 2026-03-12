import json
import os

# Find the config entry the user is using
# We can just look at the backup file again but this time check the 'settings' or current state if possible
# Since I don't have direct access to Hass, I'll check the backup's internal data

with open(r'\\192.168.100.5\config\energy_management_backup.json', 'r', encoding='utf-8') as f:
    data = json.load(f)

print("Sensors tracked in backup:")
print(f"Update time: {data.get('last_update')}")
print(f"Daily Gen: {data.get('temp_daily_gen')}")
print(f"Sensor Last Values: {data.get('sensor_last_values')}")

# Check if there are profiles for generation
gen_profile = data.get("generation", {})
total_gen_hist = 0
for h in gen_profile:
    total_gen_hist += sum(float(x.get('v', 0) if isinstance(x, dict) else x) for x in gen_profile[h])
print(f"Total historical generation data points: {total_gen_hist}")
