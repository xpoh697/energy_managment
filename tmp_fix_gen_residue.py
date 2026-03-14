import json

path = r'\\192.168.100.5\config\energy_management_backup.json'
with open(path, 'r', encoding='utf-8') as f:
    data = json.load(f)

print("Fixing fake generation data from yesterday residue...")

# 1. Reset today's generation accumulator to a realistic morning value
# (The system will continue summing from this point)
old_gen = data.get("temp_daily_gen", 0.0)
data["temp_daily_gen"] = 0.7  # Setting to 0.7 kWh as per latest hour 7 state
print(f"  temp_daily_gen: {old_gen} -> 0.7")

# 2. Reset hourly accumulators for generation as well just in case
if "hourly_accumulators" in data:
    acc = data["hourly_accumulators"]
    old_h_gen = acc.get("generation", 0.0)
    acc["generation"] = 0.1 # Real generation since 07:00
    print(f"  hourly_accumulators[generation]: {old_h_gen} -> 0.1")

# Save
with open(path, 'w', encoding='utf-8') as f:
    json.dump(data, f, indent=4)

print("Fix complete. Ready for import.")
