import json

path = r'\\192.168.100.5\config\energy_management_backup.json'
with open(path, 'r', encoding='utf-8') as f:
    data = json.load(f)

print("Setting boiler consumption to 1.2 kWh for today...")

if "daily_deduct_consumption" in data:
    target = "sensor.1st_power_plug_boiler_energy"
    old_val = data["daily_deduct_consumption"].get(target, 0.0)
    data["daily_deduct_consumption"][target] = 1.2
    print(f"  {target}: {old_val} -> 1.2")

# Also ensure hourly_accumulators aren't bloated if needed, but 1.2 is the main today total.
# Usually daily_deduct_consumption is what's shown as 'Already consumed today'.

with open(path, 'w', encoding='utf-8') as f:
    json.dump(data, f, indent=4)

print("Fix complete. Ready for import.")
