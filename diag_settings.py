import json

path = r'\\192.168.100.5\config\energy_management_backup.json'
try:
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    print("--- Detailed Settings and History ---")
    settings = data.get('settings', {})
    print("Settings:")
    for k, v in settings.items():
        print(f"  {k}: {v}")

    # Check forecast history
    fh = data.get('forecast_history', [])
    print(f"\nForecast History (last 5 records):")
    for item in fh[-5:]:
        print(f"  {item}")

    # Check losses/efficiency
    losses = data.get('losses', {})
    # calculate efficiency like in the code
    total_gen = 0
    total_loss = 0
    for h in range(24):
        recs = losses.get(str(h), [])
        for r in recs:
            if isinstance(r, dict):
                total_gen += float(str(r.get('gen', 0)).replace(',','.'))
                total_loss += float(str(r.get('v', 0)).replace(',','.'))
    
    if total_gen > 0:
        eff = (total_gen - total_loss) / total_gen
        print(f"\nEstimated Inverter Efficiency: {eff:.2%}")

except Exception as e:
    print(f"Error: {e}")
