import json
import os

path = r'\\192.168.100.5\config\.storage\energy_management_01KKCHQC1H76XNA33EYXJKFB4T'
if not os.path.exists(path):
    print(f"Path does not exist: {path}")
    # Let's search .storage for matching files
    files = os.listdir(r'\\192.168.100.5\config\.storage')
    print("Files in .storage:", files)
else:
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    # Let's check what keys exist in the data
    print("Keys in JSON:", list(data.keys()))
    
    # If there is a key with global plan or slots
    # In Home Assistant, Store data is typically inside "data" key
    payload = data.get("data", {})
    print("Keys in data payload:", list(payload.keys()))
    
    # Let's look for slots or global_plan
    plan = payload.get("global_plan", [])
    print(f"Found {len(plan)} slots in global_plan.")
    for i, slot in enumerate(plan):
        hour = slot.get("dt_iso", "").split("T")[1][:5]
        mode = slot.get("mode")
        soc_start = slot.get("soc_start")
        soc_end = slot.get("soc_end")
        reason = slot.get("reason", "")
        # Print only the first 28 hours (which includes tomorrow/today evening)
        if i < 28:
            print(f"Slot {i:02d} | {hour} | Mode: {mode:<15} | SOC: {soc_start:.1f}% -> {soc_end:.1f}% | Reason: {reason}")
