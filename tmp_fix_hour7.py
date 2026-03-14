import json

path = r'\\192.168.100.5\config\energy_management_backup.json'
with open(path, 'r', encoding='utf-8') as f:
    data = json.load(f)

print("Adding/Fixing Hour 7 data for Saturday (wd: 5)...")

for key in ["consumption_total", "consumption_base"]:
    print(f"Processing {key}...")
    
    # Ensure the hour "7" key exists in history
    if "7" not in data[key]:
        data[key]["7"] = []
        
    sh = "7"
    # Find existing wd:5 entry for hour 7 or create new one
    found = False
    for entry in data[key][sh]:
        if entry.get("wd") == 5:
            print(f"  Found existing entry: {entry['v']} -> 0.2")
            entry["v"] = 0.2
            found = True
            break
    
    if not found:
        print(f"  Creating new entry for Hour 7, wd: 5 with value 0.2")
        # Find occupancy from previous or common value
        occ = 3 # default based on night hours
        data[key][sh].append({"v": 0.2, "wd": 5, "occ": occ})

# Save the fixed data
with open(path, 'w', encoding='utf-8') as f:
    json.dump(data, f, indent=4)

print("Done. Hour 7 fixed to 0.2.")
