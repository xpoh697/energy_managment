import json

path = r'\\192.168.100.5\config\energy_management_backup.json'
with open(path, 'r', encoding='utf-8') as f:
    data = json.load(f)

print("Performing deep fix of Saturday 0:0 and other hours...")

for key in ["consumption_total", "consumption_base"]:
    print(f"Processing {key}...")
    
    # 1. Get Friday 23:00 value (wd: 4)
    # This was the residue that didn't reset.
    friday_23_val = 0.0
    for entry in data[key].get("23", []):
        if entry.get("wd") == 4:
            friday_23_val = entry["v"]
            break
    
    print(f"  Friday 23:00 residue detected: {friday_23_val}")
    
    # 2. Fix Saturday 0:0 (wd: 5)
    # Saturday 0:0 currently has 1.3 (cumulative)
    sat_0_entry = None
    for entry in data[key].get("0", []):
        if entry.get("wd") == 5:
            sat_0_entry = entry
            break
            
    if sat_0_entry:
        old_val = sat_0_entry["v"]
        if old_val >= friday_23_val and old_val > 0.5: # 0.5 is a safety threshold to avoid double-fixing
            new_val = old_val - friday_23_val
            print(f"  Fixing Sat 0:0: {old_val} -> {new_val}")
            sat_0_entry["v"] = round(new_val, 3)
        else:
            print(f"  Sat 0:0 ({old_val}) seems ok or already fixed relative to Friday.")

    # 3. Ensure Sat 1-6 are consistent diffs (in case the user's manual update changed something)
    # We already fixed them in the previous script, but let's re-run a simple diff check
    # just for the Saturday sequence itself.
    
    # We need the original cumulative values for Sat to fix the whole day.
    # From previous check: Sat 0: 1.3, 1: 0.4, 2: 0.4... 
    # WAIT! If the user "updated" the file with my PREVIOUS fix, then Sat 1 is ALREADY 0.4 (diff).
    # If I subtract Sat 0 from Sat 1 now, I'll get -0.9.
    # So I must only fix "cumulative" jumps.
    
    # Let's look at the current Sat 0-6 values in the file:
    # 0: [1.3], 1: [0.4], 2: [0.4], 3: [0.4], 4: [0.3], 5: [0.3], 6: [0.4]
    # These look like they are ALREADY diffs for 1-6, but 0 is still cumulative.
    
# Save
with open(path, 'w', encoding='utf-8') as f:
    json.dump(data, f, indent=4)

print("Done. Deep fix applied.")
