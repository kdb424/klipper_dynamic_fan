# Support fans that are temperature controlled
#
# Copyright (C) 2016-2020  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from . import fan

KELVIN_TO_CELSIUS = -273.15
MAX_FAN_TIME = 5.0
AMBIENT_TEMP = 25.0
PID_PARAM_BASE = 255.0


class DynamicTemperatureFan:
    def __init__(self, config):
        self.name = config.get_name().split()[1]
        self.printer = config.get_printer()
        self.fan = fan.Fan(config, default_shutdown_speed=1.0)
        self.min_temp = config.getfloat("min_temp", minval=KELVIN_TO_CELSIUS)
        self.max_temp = config.getfloat("max_temp", above=self.min_temp)
        pheaters = self.printer.load_object(config, "heaters")
        self.sensor = pheaters.setup_sensor(config)
        self.sensor.setup_minmax(self.min_temp, self.max_temp)
        self.sensor.setup_callback(self.temperature_callback)
        pheaters.register_sensor(config, self)
        self.speed_delay = self.sensor.get_report_time_delta()
        self.max_speed_conf = config.getfloat("max_speed", 1.0, above=0.0, maxval=1.0)
        self.max_speed = self.max_speed_conf
        self.min_speed_conf = config.getfloat("min_speed", 0.3, minval=0.0, maxval=1.0)
        self.min_speed = self.min_speed_conf
        self.ramp_down_conf = config.getboolean("ramp_down", False)
        self.ramp_down = self.ramp_down_conf
        self.enable = False
        self.last_temp = 0.0
        self.last_temp_time = 0.0
        self.target_temp_conf = config.getfloat(
            "target_temp",
            40.0 if self.max_temp > 40.0 else self.max_temp,
            minval=self.min_temp,
            maxval=self.max_temp,
        )
        self.target_temp = self.target_temp_conf
        algos = {"watermark": ControlBangBang}
        algo = config.getchoice("control", algos)
        self.control = algo(self, config)
        self.next_speed_time = 0.0
        self.last_speed_value = 0.0
        gcode = self.printer.lookup_object("gcode")
        gcode.register_mux_command(
            "SET_DYNAMIC_FAN_TARGET",
            "DYNAMIC_FAN",
            self.name,
            self.cmd_SET_DYNAMIC_FAN_TARGET,
            desc=self.cmd_SET_DYNAMIC_FAN_TARGET_help,
        )
        gcode.register_command(
            "SET_DYNAMIC_FAN_TEMPERATURE_LIMITS",
            self.cmd_SET_DYNAMIC_TEMPERATURE_LIMITS,
        )
        gcode.register_command(
            "SET_DYNAMIC_FAN_ENABLE", self.cmd_SET_DYNAMIC_FAN_ENABLE
        )

    def set_speed(self, read_time, value):
        if value <= 0.0:
            value = 0.0
        elif value < self.min_speed:
            value = self.min_speed
        if self.target_temp <= 0.0:
            value = 0.0
        if (read_time < self.next_speed_time or not self.last_speed_value) and abs(
            value - self.last_speed_value
        ) < 0.05:
            # No significant change in value - can suppress update
            return
        speed_time = read_time + self.speed_delay
        self.next_speed_time = speed_time + 0.75 * MAX_FAN_TIME
        self.last_speed_value = value
        self.fan.set_speed(speed_time, value)

    def temperature_callback(self, read_time, temp):
        self.last_temp = temp
        self.control.temperature_callback(read_time, temp)

    def get_temp(self, eventtime):
        return self.last_temp, self.target_temp

    def get_min_speed(self):
        return self.min_speed

    def get_max_speed(self):
        return self.max_speed

    def get_status(self, eventtime):
        status = self.fan.get_status(eventtime)
        status["temperature"] = round(self.last_temp, 2)
        status["target"] = self.target_temp
        return status

    def calculate_fan_speed(self):
        # Calculations simplified for visibility
        if self.ramp_down:
            # Ramp down
            # abs(((input - min)/(max - min)))
            target_speed = abs(
                (
                    (self.temperature_last_temp - self.min_temp)
                    / (self.target_temp - self.min_temp)
                )
                - 1
            )
        else:
            # Ramp up
            # abs((input - min)/(max - min) -1)
            target_speed = abs(
                (self.temperature_last_temp - self.min_temp)
                / (self.target_temp - self.min_temp)
            )
        if target_speed <= self.min_speed:
            target_speed = self.min_speed
        elif target_speed >= self.max_speed:
            target_speed = self.max_speed
        self.target_fan.set_speed(read_time, target_speed)

    cmd_SET_DYNAMIC_FAN_TARGET_help = "Sets a temperature fan speed limits"

    def cmd_SET_DYNAMIC_FAN_TARGET(self, gcmd):
        temp = gcmd.get_float("TARGET", self.target_temp_conf)
        self.set_temp(temp)
        min_speed = gcmd.get_float("MIN_SPEED", self.min_speed)
        max_speed = gcmd.get_float("MAX_SPEED", self.max_speed)
        if min_speed > max_speed:
            raise self.printer.command_error(
                "Requested min speed (%.1f) is greater than max speed (%.1f)"
                % (min_speed, max_speed)
            )
        self.set_min_speed(min_speed)
        self.set_max_speed(max_speed)

    cmd_SET_DYNAMIC_TEMPERATURE_LIMITS_help = "Sets a temperature target"

    def cmd_SET_DYNAMIC_TEMPERATURE_LIMITS(self, gcmd):
        temp = gcmd.get_float("TARGET", self.target_temp_conf)
        self.set_temp(temp)
        min_temp = gcmd.get_float("MIN_TEMP", self.min_temp)
        max_temp = gcmd.get_float("MAX_TEMP", self.max_temp)
        if min_temp > max_temp:
            raise self.printer.command_error(
                "Requested min temp (%.1f) is greater than max temp (%.1f)"
                % (min_temp, max_temp)
            )
        self.set_min_temp(min_temp)
        self.set_max_temp(max_temp)

    cmd_SET_DYNAMIC_FAN_ENABLE_help = (
        "Enables and disables the temperature fan being able to turn on."
    )

    def cmd_SET_DYNAMIC_FAN_ENABLE(self, gcmd):
        enable = gcmd.get_int("ENABLE", None, minval=0, maxval=1)
        self.set_enable(enable)

    def set_temp(self, degrees):
        if degrees and (degrees > self.max_temp):
            raise self.printer.command_error(
                "Requested temperature (%.1f) out of range (%.1f:%.1f)"
                % (degrees, self.min_temp, self.max_temp)
            )
        self.target_temp = degrees

    def set_min_speed(self, speed):
        if speed and (speed < 0.0 or speed > 1.0):
            raise self.printer.command_error(
                "Requested min speed (%.1f) out of range (0.0 : 1.0)" % (speed)
            )
        self.min_speed = speed

    def set_max_speed(self, speed):
        if speed and (speed < 0.0 or speed > 1.0):
            raise self.printer.command_error(
                "Requested max speed (%.1f) out of range (0.0 : 1.0)" % (speed)
            )
        self.max_speed = speed

    def set_enable(self, enable):
        if enable and (enable < 0.0 or enable > 1.0):
            raise self.printer.command_error(
                "Requested enable (%.1f) out of range (0.0 : 1.0)" % (speed)
            )
        if enable >= 0:
            self.enable = True
        else:
            self.enable = False
            self.temperature_fan.set_speed(read_time, 0.0)


######################################################################
# Bang-bang control algo
######################################################################


class ControlBangBang:
    def __init__(self, temperature_fan, config):
        self.temperature_fan = temperature_fan
        self.max_delta = config.getfloat("max_delta", 2.0, above=0.0)
        self.heating = True

    def temperature_callback(self, read_time, temp):
        current_temp, target_temp = self.temperature_fan.get_temp(read_time)
        if self.enable:
            if self.heating and temp >= target_temp + self.max_delta:
                self.heating = False
            elif not self.heating and temp <= target_temp - self.max_delta:
                self.heating = True
            if self.heating:
                self.temperature_fan.set_speed(read_time, self.calculate_fan_speed())
            else:
                self.temperature_fan.set_speed(read_time, 0.0)


def load_config_prefix(config):
    return DynamicTemperatureFan(config)
