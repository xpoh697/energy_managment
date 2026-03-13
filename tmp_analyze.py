
import json
import datetime

PATH = r'\\192.168.100.5\config\energy_management_backup.json'

try:
    with open(PATH, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # Manual override for analysis if local time is different from backup time
    print("--- Profile analysis for 12:00 - 17:00 ---")
    
    prof_gen = data.get('generation', {})
    prof_cons = data.get('consumption_total', {})
    
    for h in range(12, 18):
        sh = str(h)
        gen_vals = [x.get('v', 0) for x in prof_gen.get(sh, [])]
        cons_vals = [x.get('v', 0) for x in prof_cons.get(sh, [])]
        
        avg_gen = sum(gen_vals) / len(gen_vals) if gen_vals else 0
        avg_cons = sum(cons_vals) / len(cons_vals) if cons_vals else 0
        
        print(f"Hour {h:02d}:00 | Avg Gen: {avg_gen:.2f} kWh | Avg Cons: {avg_cons:.2f} kWh | Net: {avg_gen - avg_cons:.2f} kWh")

except Exception as e:
    print(f"Error accessing backup: {e}")
