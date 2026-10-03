import time
from smart_home.config import is_govee_enabled
from smart_home.govee import turn_on, turn_off, set_brightness, set_color_rgb

if not is_govee_enabled():
    raise SystemExit("Govee disabled via LIVA_GOVEE_ENABLED=false")

print("turn_on")
print(turn_on("desk_strip"))
time.sleep(2)

print("brightness 20")
print(set_brightness("desk_strip", 20))
time.sleep(2)

print("warm orange")
print(set_color_rgb("desk_strip", 255, 120, 40))
time.sleep(2)

print("blue")
print(set_color_rgb("desk_strip", 80, 140, 255))
time.sleep(2)

print("turn_off")
print(turn_off("desk_strip"))
