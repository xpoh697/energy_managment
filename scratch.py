import datetime
last_h_sell_immediate = 43
key_after = f"{last_h_sell_immediate % 24:02d}:59" + (" (Tomorrow)" if last_h_sell_immediate >= 24 else "")
print("key_after:", key_after)

sunrise_h = 7
key_morning = f"{sunrise_h-1:02d}:59 (Tomorrow)"
print("key_morning:", key_morning)

print("history_log keys:")
for h_abs in range(22, 46):
    real_h = int(h_abs % 24)
    is_tom = bool(h_abs >= 24)
    k = f"{real_h:0>2}:59" + (" (Tomorrow)" if is_tom else "")
    if k == key_after:
        print("MATCH AFTER!", k)
    if k == key_morning:
        print("MATCH MORNING!", k)
