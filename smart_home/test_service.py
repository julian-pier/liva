from smart_home.service import turn_on, turn_off


def desk_on():
    turn_on("monitor_links")
    turn_on("monitor_rechts")


def desk_off():
    turn_off("monitor_links")
    turn_off("monitor_rechts")


def shutdown_room():
    turn_off("monitor_links")
    turn_off("monitor_rechts")
    turn_off("schreibtischlampe")
    turn_off("ventilator")


def focus_mode():
    turn_on("monitor_links")
    turn_on("monitor_rechts")
    turn_on("schreibtischlampe")
    turn_off("ventilator")


def chill_mode():
    turn_off("monitor_links")
    turn_off("monitor_rechts")
    turn_on("schreibtischlampe")