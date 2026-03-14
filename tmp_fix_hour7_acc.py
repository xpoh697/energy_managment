import json

path = r'\\192.168.100.5\config\energy_management_backup.json'
with open(path, 'r', encoding='utf-8') as f:
    data = json.load(f)

print("Fixing current hourly accumulator (Hour 7)...")

if "hourly_accumulators" in data:
    acc = data["hourly_accumulators"]
    old_total = acc.get("consumption_total", 0.0)
    # 3.69 - 3.5 (what we think it was at top of hour) = ~0.19. 
    # Let's set it to 0.15 as a fair estimate for 25 minutes of house idle power.
    acc["consumption_total"] = 0.15
    acc["hourly_deduct"] = 0.0
    print(f"  consumption_total: {old_total} -> {acc['consumption_total']}")
else:
    print("  hourly_accumulators not found!")

# Also ensure history for 7 is set to 0.2 (though it won't be visible in UI until next hour)
for key in ["consumption_total", "consumption_base"]:
    if "7" in data[key]:
        for entry in data[key]["7"]:
            if entry.get("wd") == 5:
                entry["v"] = 0.2
                print(f"  Fixed historical record for {key} hour 7 to 0.2")

# Save
with open(path, 'w', encoding='utf-8') as f:
    json.dump(data, f, indent=4)

print("Done. Accumulator reset.")
