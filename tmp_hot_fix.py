import json

path = r'\\192.168.100.5\config\energy_management_backup.json'
with open(path, 'r', encoding='utf-8') as f:
    data = json.load(f)

print("Fixing HOT exported data...")

# 1. Reset current hourly accumulator
if "hourly_accumulators" in data:
    acc = data["hourly_accumulators"]
    # 3.7 -> 0.15 range estimate
    acc["consumption_total"] = 0.2
    acc["hourly_deduct"] = 0.0
    print(f"  Reset accumulator consumption_total to 0.2")

# 2. Ensure history for Hour 7 is clean 0.2
for key in ["consumption_total", "consumption_base"]:
    if "7" in data[key]:
        for entry in data[key]["7"]:
            if entry.get("wd") == 5:
                entry["v"] = 0.2
                print(f"  Force set historical hour 7 ({key}) to 0.2")

# Save
with open(path, 'w', encoding='utf-8') as f:
    json.dump(data, f, indent=4)

print("Batch fix complete.")
