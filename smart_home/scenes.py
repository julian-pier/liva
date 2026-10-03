from smart_home.service import turn_on, turn_off
from smart_home.config import is_govee_enabled

if is_govee_enabled():
    from smart_home.govee import turn_on as govee_on
    from smart_home.govee import turn_off as govee_off
    from smart_home.govee import set_brightness, set_color_rgb
else:
    govee_on = None
    govee_off = None
    set_brightness = None
    set_color_rgb = None


def desk_on():
    turn_on("monitor_links")
    turn_on("monitor_rechts")
    if is_govee_enabled():
        govee_on("desk_strip")
        set_brightness("desk_strip", 35)
        set_color_rgb("desk_strip", 255, 180, 120)


def desk_off():
    turn_off("monitor_links")
    turn_off("monitor_rechts")
    if is_govee_enabled():
        govee_off("desk_strip")


def shutdown_room():
    turn_off("monitor_links")
    turn_off("monitor_rechts")
    turn_off("schreibtischlampe")
    turn_off("ventilator")
    if is_govee_enabled():
        govee_off("desk_strip")


def focus_mode():
    turn_on("monitor_links")
    turn_on("monitor_rechts")
    turn_on("schreibtischlampe")
    turn_off("ventilator")
    if is_govee_enabled():
        govee_on("desk_strip")
        set_brightness("desk_strip", 45)
        set_color_rgb("desk_strip", 255, 190, 140)


def chill_mode():
    turn_off("monitor_links")
    turn_off("monitor_rechts")
    turn_on("schreibtischlampe")
    if is_govee_enabled():
        govee_on("desk_strip")
        set_brightness("desk_strip", 12)
        set_color_rgb("desk_strip", 255, 90, 40)
