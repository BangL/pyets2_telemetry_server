#
# Copyright 2019 Thomas Axelsson <thomasa88@gmail.com>
#
# This file is part of pyets2_telemetry_server.
#
# pyets2_telemetry_server is free software: you can redistribute it
# and/or modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation, either version 3 of
# the License, or (at your option) any later version.
#
# pyets2_telemetry_server is distributed in the hope that it will be
# useful, but WITHOUT ANY WARRANTY; without even the implied warranty
# of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with pyets2_telemetry_server.
# If not, see <https://www.gnu.org/licenses/>.
#

# import logging
import copy
import logging
import math
import re
import threading

from inspect import isfunction
from sre_compile import isstring
from datetime import datetime, timedelta
from typing import Any, Callable, Type, TypeVar

from requests import patch

import pyets2lib.scshelpers
from pyets2lib.scsdefs import *

from . import web_server
from .version import VERSION

# From ETS2 Telemetry Server
TELEMETRY_PLUGIN_VERSION = '7'

# Start of game time
GAME_TIME_BASE = datetime(1, 1, 1)

# # Used by conversion functions when the game gives a bad value
BAD_VALUE = object()

logger_: logging.Logger
server_: web_server.SignalrHttpServer
server_thread_: threading.Thread
game_time_: datetime = GAME_TIME_BASE
delivery_time_: datetime = GAME_TIME_BASE
onJob_: bool = False
speed_mph_: float = 0.0

# NOTE: new_data indicator only works well with 1 client.
# Need to track connection/session ids to handle multiple clients
# and call notify_all().
shared_data_ = {
    'condition': threading.Condition(),
    'telemetry_data': {},
    'new_data': False
}

# Only call these functions when shared_data is locked!
def set_shared_value(json_path: list[str | int], value: Any):
    push_shared_value(json_path, lambda _: value)

def toggle_shared_bool(json_path):
    push_shared_value(json_path, lambda v: v ^ True)

def push_shared_value(json_path, callback: Callable):
    path_len = len(json_path)
    _active_dict = shared_data_['telemetry_data']
    for i, key in enumerate(json_path):
        if i == path_len - 1:
            _active_dict[key] = callback(_active_dict.get(key))
            continue
        _active_dict = _active_dict.get(key)

def get_shared_value(json_path, fallback = None):
    path_len = len(json_path)
    _active_dict = shared_data_['telemetry_data']
    for i, key in enumerate(json_path):
        if i == path_len - 1:
            return _active_dict.get(key)
        _active_dict = _active_dict.get(key)
    return fallback

def recount_trailers():
    connected = 0
    for trailer in shared_data_['telemetry_data']['trailers'].values():
        if trailer['present'] == True:
            connected += 1
    shared_data_['telemetry_data']['trailerCount'] = connected
    shared_data_['telemetry_data']['trailer'] = shared_data_['telemetry_data']['trailers'][0] # rebind first trailer, just in case

def update_slot_count():
    slotCount = 1
    if get_shared_value(['shifter', 'type'], SCS_SHIFTER_TYPE_automatic) == SCS_SHIFTER_TYPE_hshifter:
        positions = shared_data_['telemetry_data']['shifter']['_handlePositions']
        if len(positions) > 0:
            slotCount = max(enumerate(positions))
    set_shared_value(['shifter', 'slotCount'], slotCount)

def update_tyre_circumference():
    tyreCircumference = 0.0
    wheelCount = get_shared_value(['truck', 'wheelCount'], 0)
    if wheelCount > 2:
        wheel_index = 2
        if wheelCount > 4 and get_shared_value(['truck', 'wheels', 4, 'powered'], False):
            wheel_index = 4
        tyreCircumference = get_shared_value(['truck', 'wheels', wheel_index, 'radius'], 0.0) * 2 * math.pi
    set_shared_value(['shifter', 'tyreCircumference'], tyreCircumference)

def update_gear_names():
    fwd_gears = get_shared_value(['shifter', 'forwardGears'], 0)
    rev_gears = get_shared_value(['shifter', 'reverseGears'], 0)
    shifter_type = get_shared_value(['shifter', 'type'], SCS_SHIFTER_TYPE_automatic)
    is_ets2 = shared_data_['telemetry_data']['game']['gameName'] == "ETS2"

    fwd_names = ["N"]
    rev_names = ["N"]

    if shifter_type == SCS_SHIFTER_TYPE_hshifter:
        if fwd_gears == 18:
            fwd_names = ["N", "CL", "CH", "1L", "1H", "2L", "2H", "3L", "3H", "4L", "4H", "5L", "5H", "6L", "6H", "7L", "7H", "8L", "8H"]
        elif fwd_gears == 16:
            fwd_names = ["N", "1L", "1H", "2L", "2H", "3L", "3H", "4L", "4H", "5L", "5H", "6L", "6H", "7L", "7H", "8L", "8H"]
        elif fwd_gears == 14:
                fwd_names = ["N", "1L", "1H", "2L", "2H", "3L", "3H", "4L", "4H", "5L", "5H", "6L", "6H", "7L", "7H"] if is_ets2 else \
                                       ["N", "CL", "CH", "1", "2", "3", "4", "5L", "5H", "6L", "6H", "7L", "7H", "8L", "8H"]
        elif fwd_gears == 13:
            fwd_names = ["N", "L", "1", "2", "3", "4", "5L", "5H", "6L", "6H", "7L", "7H", "8L", "8H"]
        elif fwd_gears == 12:
            fwd_names = ["N", "1L", "1H", "2L", "2H", "3L", "3H", "4L", "4H", "5L", "5H", "6L", "6H"] if is_ets2 else \
                                   ["N", "1", "2", "3", "4", "5L", "5H", "6L", "6H", "7L", "7H", "8L", "8H"]

        if rev_gears == 4:
            rev_names = ["N", "R1L", "R1H", "R2L", "R2H"]
        elif rev_gears == 3:
            rev_names = ["N", "RL", "RH", "RO"]
        elif rev_gears == 2:
            rev_names = ["N", "RL", "RH"]

    if len(fwd_names) == 1:
        for i in range(1, fwd_gears):
            fwd_names.append(str(i))

    if len(rev_names) == 1:
        if rev_gears == 1:
            rev_names.append("R")
        elif rev_gears > 1:
            for i in range(1, rev_gears):
                fwd_names.append("R" + str(i))

    set_shared_value(['shifter', 'forwardGearNames'], to_int_dict(str, fwd_names))
    set_shared_value(['shifter', 'reverseGearNames'], to_int_dict(str, rev_names))

def update_gear_name(value: int):
    fwd_gear_names = shared_data_['telemetry_data']['shifter']['forwardGearNames']
    rev_gear_names = shared_data_['telemetry_data']['shifter']['reverseGearNames']

    gear_name = "N"
    if value != 0 and len(fwd_gear_names) > 1 and len(rev_gear_names) > 1:
        gear_names = fwd_gear_names if value > 0 else rev_gear_names
        gear_name = gear_names[abs(value)]

    set_shared_value(['shifter', 'displayedGearName'], gear_name)

def update_shifter_speeds():
    fwd_speeds: dict[int, int] = {0: 0}
    rev_speeds: dict[int, int] = {0: 0}
    fwd_rpm: dict[int, int] = {0: 0}
    rev_rpm: dict[int, int] = {0: 0}
    best_gear = 0
    best_gear_name = 'N'

    fwd_ratios = shared_data_['telemetry_data']['shifter']['forwardGearRatios']
    rev_ratios = shared_data_['telemetry_data']['shifter']['reverseGearRatios']
    fwd_gears = get_shared_value(['shifter', 'forwardGears'], 0)
    rev_gears = get_shared_value(['shifter', 'reverseGears'], 0)
    tyreCircumference = get_shared_value(['shifter', 'tyreCircumference'], 0.0)
    differentialRatio = get_shared_value(['shifter', 'differentialRatio'], 0.0)

    if len(fwd_ratios) > 0 and len(rev_ratios) > 0 and fwd_gears > 0 and rev_gears > 0 and tyreCircumference > 0.0 and differentialRatio > 0.0:

        for i in range(1, fwd_gears):
            if len(fwd_ratios) > i:
                fwd_speeds[i] = round(90 * tyreCircumference / differentialRatio * fwd_ratios[i - 1])
                fwd_rpm[i] = round(60 * abs(speed_mph_) * differentialRatio * fwd_ratios[i - 1] / tyreCircumference)

        for i in range(1, rev_gears):
            if len(rev_ratios) > i:
                rev_speeds[i] = round(90 * tyreCircumference / differentialRatio * rev_ratios[i - 1])
                rev_rpm[i] = round(60 * abs(speed_mph_) * differentialRatio * rev_ratios[i - 1] / tyreCircumference)

        gap = 1500
        check = 1300
        speeds = fwd_rpm if speed_mph_ > 0 else rev_rpm
        for i in range(1, len(speeds)):
            if speeds[i] < 0:
                pos = abs(speeds[i] + check)
                if gap > pos:
                    best_gear = -i
                    gap = pos
            else:
                neg = abs(speeds[i] - check)
                if gap < neg:
                    best_gear = i
                    gap = neg

        if best_gear < 0:
            best_gear_name = shared_data_['telemetry_data']['shifter']['reverseGearNames'][abs(best_gear)]
        else:
            best_gear_name = shared_data_['telemetry_data']['shifter']['forwardGearNames'][best_gear]

    set_shared_value(['shifter', 'forwardSpeedAt1500Rpm'], fwd_speeds)
    set_shared_value(['shifter', 'reverseSpeedAt1500Rpm'], rev_speeds)
    set_shared_value(['shifter', 'forwardRpmAtCurrentSpeed'], fwd_rpm)
    set_shared_value(['shifter', 'reverseRpmAtCurrentSpeed'], rev_rpm)
    set_shared_value(['shifter', 'bestGear'], best_gear)
    set_shared_value(['shifter', 'bestGearName'], best_gear_name)

def shared_data_notify():
    shared_data_['new_data'] = True
    shared_data_['condition'].notify()

def reset_job_data():
    set_shared_value(['job', 'income'], 0)
    set_shared_value(['job', 'deadlineTime'], json_time(GAME_TIME_BASE))
    set_shared_value(['job', 'remainingTime'], json_time(GAME_TIME_BASE))
    set_shared_value(['job', 'sourceCity'], '')
    set_shared_value(['job', 'sourceCompany'], '')
    set_shared_value(['job', 'destinationCity'], '')
    set_shared_value(['job', 'destinationCompany'], '')
    set_shared_value(['job', 'specialTransport'], False)
    set_shared_value(['job', 'jobMarket'], '')
    set_shared_value(['job', 'plannedDistance'], 0)
    set_shared_value(['cargo', 'cargoLoaded'], False)
    set_shared_value(['cargo', 'cargoId'], '')
    set_shared_value(['cargo', 'cargo'], '')
    set_shared_value(['cargo', 'mass'], 0.0)
    set_shared_value(['cargo', 'unitMass'], 0.0)
    set_shared_value(['cargo', 'unitCount'], 0.0)
    set_shared_value(['cargo', 'damage'], 0.0)

def telemetry_init(version, telemetry):
    global logger_
    logger_ = telemetry.common.logger
    if not logger_:
        exit(0)

    logger_.info("Version %s", VERSION)

    init_shared_data()
    shared_data_['telemetry_data']['game']['gameName'] = telemetry.common.game_id.upper().replace('EUT2', 'ETS2')
    shared_data_['telemetry_data']['game']['version'] = telemetry.common.game_name.split(' ')[-1]

    telemetry.register_for_event(SCS_TELEMETRY_EVENT_configuration, event_cb, None)
    telemetry.register_for_event(SCS_TELEMETRY_EVENT_gameplay, event_cb, None)
    telemetry.register_for_event(SCS_TELEMETRY_EVENT_started, event_cb, None)
    telemetry.register_for_event(SCS_TELEMETRY_EVENT_paused, event_cb, None)

    for channel in SCS_CHANNELS:
        config = CHANNEL_EVENT_MAP.get(channel.parent.name if channel.parent else channel.name)
        if not config:
            continue

        if channel.indexed:
            for i in range(0, channel.index_count):
                logger_.debug(f"registering for channel {channel.name} (index {i})")
                telemetry.register_for_channel(channel, channel_cb, i)
        else:
            logger_.debug(f"registering for channel {channel.name}")
            telemetry.register_for_channel(channel, channel_cb)

    start_server()

def channel_cb(channel: ScsChannel, index: int, value, context):
    global game_time_, speed_mph_

    with shared_data_['condition']:
        something_changed = False
        trailers_changed = False

        # Optimize this?
        if channel == SCS_TELEMETRY_CHANNEL_game_time:
            game_time_ = GAME_TIME_BASE + timedelta(minutes=value)
            if game_time_ > delivery_time_:
                # Passed the deadline
                remaining_time = timedelta(0)
            else:
                remaining_time = delivery_time_ - game_time_
            set_shared_value(['job', 'remainingTime'], json_time(GAME_TIME_BASE + remaining_time))
            something_changed = True

        elif channel == SCS_TELEMETRY_TRUCK_CHANNEL_dashboard_backlight:
            set_shared_value(['truck', 'lightsDashboardOn'], value > 0)
            something_changed = True

        elif channel == SCS_TELEMETRY_TRUCK_CHANNEL_cruise_control:
            set_shared_value(['truck', 'cruiseControlOn'], value > 0)
            something_changed = True

        elif channel == SCS_TELEMETRY_TRUCK_CHANNEL_displayed_gear:
            update_gear_name(value)
            something_changed = True

        elif channel == SCS_TELEMETRY_TRUCK_CHANNEL_speed:
            speed_mph_ = value

        channel_config_name = channel.name

        # disect indexed events
        indexed_pattern = r'^\w+\.(\d+)\.[\.\w]+$'
        indexed_match = re.match(indexed_pattern, channel.name)
        channel_index = None
        if indexed_match and channel.parent:
            channel_index = int(indexed_match.group(1)) # parse index
            channel_config_name = channel.parent.name # switch to parent channel for shared config

        channel_config = CHANNEL_EVENT_MAP.get(channel_config_name)
        if channel_config:
            sub: str | None = None
            if len(channel_config) > 2:
                p3 = channel_config[2]
                convert = None
                if isfunction(p3): # convert
                    convert = p3
                elif isstring(p3): # index sub
                    sub = str(p3)
                    if len(channel_config) > 3:
                        convert = channel_config[3]
                if convert is not None:
                    value = convert(value)
                    if value is BAD_VALUE:
                        return

            if isinstance(value, datetime):
                value = json_time(value)

            # compose json path
            json_path: list[str | int] = [channel_config[0]]
            # indexed trailers
            if indexed_match is not None and channel_index is not None:
                json_path.append(channel_index)
                trailers_changed = True
            json_path.append(channel_config[1])
            # indexed values
            if channel.indexed:
                json_path.append(index)
                if sub:
                    json_path.append(sub)
            # set value
            set_shared_value(json_path, value)
            something_changed = True

        if trailers_changed:
            recount_trailers()
        if something_changed:
            shared_data_notify()

def event_cb(event, event_info, context):
    global delivery_time_, onJob_

    if event == SCS_TELEMETRY_EVENT_configuration or event == SCS_TELEMETRY_EVENT_gameplay:
        with shared_data_['condition']:
            event_id = event_info['id']
            is_legacy_trailer = event_id == SCS_TELEMETRY_CONFIG_trailer
            if is_legacy_trailer:
                return

            is_job = event_id == SCS_TELEMETRY_CONFIG_job
            trailer_match = re.match(r'^trailer\.(\d+)$', event_id)
            if trailer_match:
                event_map = CONFIG_EVENT_MAP.get(SCS_TELEMETRY_CONFIG_trailer)
            else:
                event_map = CONFIG_EVENT_MAP.get(event_id)
            if event_map is not None:
                is_empty = True
                for name, index, value in event_info['attributes']:
                    is_empty = False
                    event_config = event_map.get(name)
                    if event_config is not None:
                        #save_value = value

                        json_path: list[str | int] = [event_config[0]]

                        key: str = event_config[1]
                        sub: str | None = None
                        if len(event_config) > 2:
                            p3 = event_config[2]
                            if isfunction(p3): # convert
                                value = p3(value)
                            elif isstring(p3): # index sub
                                sub = str(p3)

                        if name == SCS_TELEMETRY_CONFIG_ATTRIBUTE_delivery_time.name:
                            # Remaining time will change as game time
                            # progresses, so let's save delivery time
                            # and calculate remaining time when game
                            # time changes.
                            delivery_time_ = value
                        if isinstance(value, datetime):
                            value = json_time(value)

                        # indexed trailer
                        if trailer_match:
                            json_path.append(int(trailer_match.group(1)))
                            if key == SCS_TELEMETRY_CONFIG_ATTRIBUTE_id.name:
                                set_shared_value(json_path + ['present'], value != None and value != '')

                        json_path.append(key)

                        # indexed value
                        if SCS_ATTRIBUTES[name].indexed:
                            json_path.append(index)
                            if sub:
                                json_path.append(sub)

                        set_shared_value(json_path, value)

                        if (name in [SCS_TELEMETRY_CONFIG_ATTRIBUTE_slot_handle_position.name,
                                     SCS_TELEMETRY_CONFIG_ATTRIBUTE_shifter_type.name]):
                            update_slot_count()

                        if (event_id == SCS_TELEMETRY_CONFIG_truck) and name in [
                                SCS_TELEMETRY_CONFIG_ATTRIBUTE_wheel_count.name,
                                SCS_TELEMETRY_CONFIG_ATTRIBUTE_wheel_radius.name,
                                SCS_TELEMETRY_CONFIG_ATTRIBUTE_wheel_powered.name]:
                            update_tyre_circumference()

                        if (name in [SCS_TELEMETRY_CONFIG_ATTRIBUTE_forward_gear_count.name,
                                     SCS_TELEMETRY_CONFIG_ATTRIBUTE_reverse_gear_count.name,
                                     SCS_TELEMETRY_CONFIG_ATTRIBUTE_shifter_type.name]):
                            update_gear_names()

                if is_job and is_empty and onJob_:
                    onJob_ = False
                    toggle_shared_bool(['jobEvent', 'jobFinished'])
                elif is_job and not is_empty and not onJob_:
                    onJob_ = True

                if trailer_match:
                    recount_trailers()

                update_shifter_speeds()

                shared_data_notify()
    elif event == SCS_TELEMETRY_EVENT_started:
        with shared_data_['condition']:
            set_shared_value(['game', 'paused'], False)
            shared_data_notify()
    elif event == SCS_TELEMETRY_EVENT_paused:
        with shared_data_['condition']:
            set_shared_value(['game', 'paused'], True)
            shared_data_notify()
    if event == SCS_TELEMETRY_EVENT_gameplay:
        with shared_data_['condition']:
            gameplay_event_config = GAMEPLAY_EVENTS_MAP.get(event_info['id'])
            if gameplay_event_config is not None:
                toggle_shared_bool([gameplay_event_config[0], gameplay_event_config[1]])
                if gameplay_event_config[1] in ['jobCancelled', 'jobDelivered']:
                    onJob_ = False
                    toggle_shared_bool([gameplay_event_config[0], 'jobFinished'])
                if len(gameplay_event_config) > 2:
                    gameplay_event_config[2]()
                shared_data_notify()

def start_server():
    global server_, server_thread_

    server_ = web_server.SignalrHttpServer(logger_, shared_data_)

    # Using Python Threading for now. Switch to Multiprocessing if this
    # becomes a performance problem. This will affect logging and data sharing.
    server_thread_ = threading.Thread(
        target=run_and_log_exceptions(server_.serve_forever))
    server_thread_.name = "signalr server"
    server_thread_.start()

    logger_.info("Started server on port %u" % server_.PORT_NUMBER)

def stop_server():
    server_.shutdown()
    server_thread_.join()
    server_.server_close()
    logger_.info("Stopped server")

def run_and_log_exceptions(target):
    def runner():
        try:
            target()
        except Exception as e:
            if logger_:
                pyets2lib.scshelpers.log_exception(logger_, e)
            raise e
    return runner

def telemetry_shutdown():
    logger_.info("Shutting down")
    if server_:
        stop_server()
    logger_.info("bye")

def clone(blueprint) -> Any:
    return copy.deepcopy(blueprint)

def clone_dict(blueprint, count) -> dict[int, Any]:
    result: dict[int, Any] = {}
    for i in range(0, count):
        result[i] = (clone(blueprint))
    return result

T = TypeVar('T', bound=Any)

def make_dict(typ: Type[T]) -> dict[int, T]:
    result: dict[int, T] = {}
    return result

def to_int_dict(typ: Type[T], s: list[T]) -> dict[int, T]:
    result = make_dict(typ)
    i = 0
    for v in s:
        result[i] = v
        i += 1
    return result

def init_shared_data():
    max_trailers = SCS_TELEMETRY_trailers_count
    max_wheels = SCS_TELEMETRY_wheels_count

    # compose blueprints
    bp_vector = {
        'x': 0.0,
        'y': 0.0,
        'z': 0.0,
    }
    bp_placement = {
        'x': 0.0,
        'y': 0.0,
        'z': 0.0,
        'heading': 0.0,
        'pitch': 0.0,
        'roll': 0.0,
    }
    bp_wheel = {
        'simulated': False,
        'steerable': False,
        'radius': 0.0,
        'position': clone(bp_vector),
        'powered': False,
        'liftable': False,
        'lifted': False,
    }
    bp_trailer = {
        'trailerNumber': 0,
        'attached': False,
        'present': False,
        'id': '',
        'name': '',
        'wearWheels': 0.0,
        'wearChassis': 0.0,
        'wearBody': 0.0,
        'cargoDamage': 0.0,
        'cargoAccessoryId': '',
        'brandId': '',
        'brand': '',
        'bodyType': '',
        'cargo': '',
        'licensePlate': '',
        'licensePlateCountry': '',
        'licensePlateCountryId': '',
        'chainType': '',
        'placement': clone(bp_placement),
        'wheelCount': 0,
        'wheels': clone_dict(bp_wheel, max_wheels),
    }

    # TODO ?
    # bp_shifter_selector = {
    #     'selector': 0,
    #     'gear': 0,
    #     'gearName': '',
    # }
    # bp_shifter_slot = {
    #     'slot': 0,
    #     'selectors': clone_dict(bp_shifter_selector, 0),
    # }

    # compose data
    trailers = clone_dict(bp_trailer, max_trailers)
    shared_data_['new_data'] = True
    shared_data_['telemetry_data'] = {
        'game': {
            'connected': True,
            'gameName': '',
            'paused': True,
            'time': json_time(GAME_TIME_BASE),
            'timeScale': 19.0,
            'nextRestStopTime': json_time(GAME_TIME_BASE),
            'version': '0.0',
            'telemetryPluginVersion': TELEMETRY_PLUGIN_VERSION,
            'maxTrailerCount': max_trailers,
        },
        'truck': {
            'id': '',
            'make': '',
            'model': '',
            'speed': 0.0,
            'cruiseControlSpeed': 0.0,
            'cruiseControlOn': False,
            'odometer': 0.0,
            # 'gear': 0, # deprecated. use shifter.gear instead.
            # 'displayedGear': 0, # deprecated. use shifter.displayedGear instead.
            # 'forwardGears': 12, # deprecated. use shifter.forwardGears instead.
            # 'reverseGears': 1, # deprecated. use shifter.reverseGears instead.
            # 'shifterType': SCS_SHIFTER_TYPE_automatic, # deprecated. use shifter.type instead.
            'engineRpm': 0.0,
            'engineRpmMax': 2500.0,
            'fuel': 0.0,
            'fuelCapacity': 700.0,
            'fuelAverageConsumption': 0.1,
            'fuelWarningFactor': 0.15,
            'fuelWarningOn': False,
            'wearEngine': 0.0,
            'wearTransmission': 0.0,
            'wearCabin': 0.0,
            'wearChassis': 0.0,
            'wearWheels': 0.0,
            'userSteer': 0.0, # wheel + input steering. scale wheel *4
            'userThrottle': 0.0, # wheel + input
            'userBrake': 0.0,
            'userClutch': 0.0,
            'gameSteer': 0.0,
            'gameThrottle': 0.0,
            'gameBrake': 0.0,
            'gameClutch': 0.0,
            # 'shifterSlot': 0, # deprecated. use shifter.slot instead.
            'engineOn': False,
            'electricOn': False,
            'wipersOn': False,
            'retarderBrake': 0,
            'retarderStepCount': 3,
            'parkBrakeOn': False,
            'motorBrakeOn': False,
            'brakeTemperature': 0.0,
            'adblue': 0.0,
            'adblueCapacity': 0.0,
            #'adblueAverageConsumption': 0.0, # Removed in SDK 1.9
            'adblueWarningOn': False,
            'airPressure': 0.0,
            'airPressureWarningOn': False,
            'airPressureWarningValue': 65.0,
            'airPressureEmergencyOn': False,
            'airPressureEmergencyValue': 30.0,
            'oilTemperature': 0.0,
            'oilPressure': 0.0,
            'oilPressureWarningOn': False,
            'oilPressureWarningValue': 10.0,
            'waterTemperature': 0.0,
            'waterTemperatureWarningOn': False,
            'waterTemperatureWarningValue': 105.0,
            'batteryVoltage': 24.0,
            'batteryVoltageWarningOn': False,
            'batteryVoltageWarningValue': 22.0,
            'lightsDashboardValue': 0.0,
            'lightsDashboardOn': False,
            'blinkerLeftActive': False,
            'blinkerRightActive': False,
            'blinkerLeftOn': False,
            'blinkerRightOn': False,
            'hazardWarningOn': False,
            'lightsParkingOn': False,
            'lightsBeamLowOn': False,
            'lightsBeamHighOn': False,
            'lightsAuxFrontOn': False,
            'lightsAuxRoofOn': False,
            'lightsBeaconOn': False,
            'lightsBrakeOn': False,
            'lightsReverseOn': False,
            'placement': clone(bp_placement),
            'acceleration': clone(bp_vector),
            'head': clone(bp_vector),
            'cabin': clone(bp_vector),
            'hook': clone(bp_vector),
            'licensePlate': '',
            'licensePlateCountryId': '',
            'licensePlateCountry': '',
            'wheelCount': 0,
            'wheels': clone_dict(bp_wheel, max_wheels),
        },
        'shifter': {
            'type': SCS_SHIFTER_TYPE_automatic,
            'forwardGears': 0,
            'forwardGearNames': make_dict(str), # str
            'reverseGears': 0,
            'reverseGearNames': make_dict(str), # str
            'differentialRatio': 0.0,
            'forwardGearRatios': make_dict(float), # float
            'reverseGearRatios': make_dict(float), # float
            'tyreCircumference': 0.0,
            'forwardSpeedAt1500Rpm': make_dict(int), # int
            'reverseSpeedAt1500Rpm': make_dict(int), # int
            'forwardRpmAtCurrentSpeed': make_dict(int), # int
            'reverseRpmAtCurrentSpeed': make_dict(int), # int
            'selectorCount': 0,
            'slotCount': 0,
        #     'slots': clone_dict(bp_shifter_slot, 0), # IEts2ShifterSlot # TODO ?
            'gear': 0,
            'displayedGear': 0,
            'displayedGearName': 'N',
        #     'gearRatio': 0.0, # TODO ?
            'slot': 0,
        #     'selector': 0, # TODO ?
            'bestGear': 0,
            'bestGearName': 'N',
            '_handlePositions': make_dict(int), # int
            '_bitMasks': make_dict(int), # int
        },
        'trailerCount': 0,
        'trailers': trailers,
        'trailer': trailers[0],
        'job': {
            'income': 0,
            'deadlineTime':  json_time(GAME_TIME_BASE),
            'remainingTime': json_time(GAME_TIME_BASE),
            'sourceCity': '',
            'sourceCompany': '',
            'destinationCity': '',
            'destinationCompany': '',
            'specialTransport': False,
            'jobMarket': '',
            'plannedDistance': 0,
        },
        'cargo': {
            'cargoLoaded': False,
            'cargoId': '',
            'cargo': '',
            'mass': 0.0,
            'unitMass': 0.0,
            'unitCount': 0.0,
            'damage': 0.0,
        },
        'navigation': {
            'estimatedTime': json_time(GAME_TIME_BASE),
            'estimatedDistance': 0,
            'speedLimit': 0,
        },
        'finedEvent': {
            'fineOffense': '',
            'fineAmount': 0.0,
            'fined': False,
        },
        'jobEvent': {
            'jobFinished': False,
            'jobCancelled': False,
            'jobDelivered': False,
            'cancelPenalty': 0.0,
            'revenue': 0.0,
            'earnedXp': 0.0,
            'cargoDamage': 0.0,
            'distance': 0.0,
            'deliveryTime': json_time(GAME_TIME_BASE),
            'autoparkUsed': False,
            'autoloadUsed': False,
        },
        'tollgateEvent': {
            'tollgateUsed': False,
            'payAmount': 0.0,
        },
        'ferryEvent': {
            'ferryUsed': False,
            'sourceName': '',
            'targetName': '',
            'sourceId': '',
            'targetId': '',
            'payAmount': 0.0,
        },
        'trainEvent': {
            'trainUsed': False,
            'sourceName': '',
            'targetName': '',
            'sourceId': '',
            'targetId': '',
            'payAmount': 0.0,
        },
    }

def json_time(dt):
    return dt.isoformat(timespec='seconds')+'Z'

# Value conversion functions
def mps_to_kph(mps):
    return round(3.6 * mps)

def non_zero(value):
    return value != 0

def flatten_placement(value):
    # API can give Infinity or Nan for pitch (and others?) which is
    # not part of JSON and makes the client parser fail. Note: I have
    # confirmed in the C++ loader that the values from the SDK are
    # indeed Inf and Nan.
    return check_bad_float( { **(value['position']), **(value['orientation']) } )

FLOAT_INF_POS = float('inf')
FLOAT_INF_NEG = float('-inf')
def check_bad_float(d):
    # Cannot test equality for NaN, as NaN is always non-equal to any
    # float by definition
    for v in d.values():
        if v == FLOAT_INF_POS or v == FLOAT_INF_NEG or math.isnan(v):
            return BAD_VALUE
    return d

# JSON mapping

# optional Callable converts the value
CHANNEL_EVENT_MAP: dict[str, tuple[str, str] | tuple[str, str, str] | tuple[str, str, Callable] | tuple[str, str, str, Callable]] = {
    SCS_TELEMETRY_CHANNEL_game_time.name: ('game', 'time', lambda v: game_time_),
    SCS_TELEMETRY_CHANNEL_local_scale.name: ('game', 'timeScale'),
    # SCS_TELEMETRY_CHANNEL_multiplayer_time_offset.name,
    SCS_TELEMETRY_CHANNEL_next_rest_stop.name: ('game', 'nextRestStopTime', lambda v: GAME_TIME_BASE + timedelta(minutes=v)),

    SCS_TELEMETRY_JOB_CHANNEL_cargo_damage.name: ('cargo', 'damage'),

    SCS_TELEMETRY_TRAILER_CHANNEL_cargo_damage.name: ('trailers', 'cargoDamage'),
    SCS_TELEMETRY_TRAILER_CHANNEL_connected.name: ('trailers', 'attached'),
    # SCS_TELEMETRY_TRAILER_CHANNEL_local_angular_acceleration.name,
    # SCS_TELEMETRY_TRAILER_CHANNEL_local_angular_velocity.name,
    # SCS_TELEMETRY_TRAILER_CHANNEL_local_linear_acceleration.name,
    # SCS_TELEMETRY_TRAILER_CHANNEL_local_linear_velocity.name,
    SCS_TELEMETRY_TRAILER_CHANNEL_wear_body.name: ('trailers', 'wearBody'),
    SCS_TELEMETRY_TRAILER_CHANNEL_wear_chassis.name: ('trailers', 'wearChassis'),
    SCS_TELEMETRY_TRAILER_CHANNEL_wear_wheels.name: ('trailers', 'wearWheels'),
    # SCS_TELEMETRY_TRAILER_CHANNEL_wheel_lift_offset.name, # indexed
    SCS_TELEMETRY_TRAILER_CHANNEL_wheel_lift.name: ('trailers', 'wheels', 'lifted', lambda v: v > 0.0), # indexed
    # SCS_TELEMETRY_TRAILER_CHANNEL_wheel_on_ground.name, # indexed
    # SCS_TELEMETRY_TRAILER_CHANNEL_wheel_rotation.name, # indexed
    # SCS_TELEMETRY_TRAILER_CHANNEL_wheel_steering.name, # indexed
    # SCS_TELEMETRY_TRAILER_CHANNEL_wheel_substance.name, # indexed
    # SCS_TELEMETRY_TRAILER_CHANNEL_wheel_susp_deflection.name, # indexed
    # SCS_TELEMETRY_TRAILER_CHANNEL_wheel_velocity.name, # indexed
    SCS_TELEMETRY_TRAILER_CHANNEL_world_placement.name: ('trailers', 'placement', flatten_placement),

    # SCS_TELEMETRY_TRUCK_CHANNEL_adblue_average_consumption.name: ('truck', 'adblueAverageConsumption') // Removed in SDK 1.9,
    SCS_TELEMETRY_TRUCK_CHANNEL_adblue.name: ('truck', 'adblue'),
    SCS_TELEMETRY_TRUCK_CHANNEL_adblue_warning.name: ('truck', 'adblueWarningOn'),
    SCS_TELEMETRY_TRUCK_CHANNEL_battery_voltage.name: ('truck', 'batteryVoltage'),
    SCS_TELEMETRY_TRUCK_CHANNEL_battery_voltage_warning.name: ('truck', 'batteryVoltageWarningOn'),
    SCS_TELEMETRY_TRUCK_CHANNEL_brake_air_pressure_emergency.name: ('truck', 'airPressureEmergencyOn'),
    SCS_TELEMETRY_TRUCK_CHANNEL_brake_air_pressure.name: ('truck', 'airPressure'),
    SCS_TELEMETRY_TRUCK_CHANNEL_brake_air_pressure_warning.name: ('truck', 'airPressureWarningOn'),
    SCS_TELEMETRY_TRUCK_CHANNEL_brake_temperature.name: ('truck', 'brakeTemperature'),
    # SCS_TELEMETRY_TRUCK_CHANNEL_cabin_angular_acceleration.name,
    # SCS_TELEMETRY_TRUCK_CHANNEL_cabin_angular_velocity.name,
    # SCS_TELEMETRY_TRUCK_CHANNEL_cabin_offset.name,
    SCS_TELEMETRY_TRUCK_CHANNEL_cruise_control.name: ('truck', 'cruiseControlSpeed', mps_to_kph),
    SCS_TELEMETRY_TRUCK_CHANNEL_dashboard_backlight.name: ('truck', 'lightsDashboardValue'),
    # SCS_TELEMETRY_TRUCK_CHANNEL_differential_lock.name,
    # SCS_TELEMETRY_TRUCK_CHANNEL_lift_axle.name,
    # SCS_TELEMETRY_TRUCK_CHANNEL_lift_axle_indicator.name,
    # SCS_TELEMETRY_TRUCK_CHANNEL_trailer_lift_axle.name,
    # SCS_TELEMETRY_TRUCK_CHANNEL_trailer_lift_axle_indicator.name,
    SCS_TELEMETRY_TRUCK_CHANNEL_displayed_gear.name: ('shifter', 'displayedGear'),
    SCS_TELEMETRY_TRUCK_CHANNEL_effective_brake.name: ('truck', 'gameBrake'),
    SCS_TELEMETRY_TRUCK_CHANNEL_effective_clutch.name: ('truck', 'gameClutch'),
    SCS_TELEMETRY_TRUCK_CHANNEL_effective_steering.name: ('truck', 'gameSteer'),
    SCS_TELEMETRY_TRUCK_CHANNEL_effective_throttle.name: ('truck', 'gameThrottle'),
    SCS_TELEMETRY_TRUCK_CHANNEL_electric_enabled.name: ('truck', 'electricOn'),
    SCS_TELEMETRY_TRUCK_CHANNEL_engine_enabled.name: ('truck', 'engineOn'),
    SCS_TELEMETRY_TRUCK_CHANNEL_engine_gear.name: ('shifter', 'gear'),
    SCS_TELEMETRY_TRUCK_CHANNEL_engine_rpm.name: ('truck', 'engineRpm'),
    SCS_TELEMETRY_TRUCK_CHANNEL_fuel_average_consumption.name: ('truck', 'fuelAverageConsumption'),
    SCS_TELEMETRY_TRUCK_CHANNEL_fuel.name: ('truck', 'fuel'),
    SCS_TELEMETRY_TRUCK_CHANNEL_fuel_warning.name: ('truck', 'fuelWarningOn'),
    # SCS_TELEMETRY_TRUCK_CHANNEL_head_offset.name,
    # SCS_TELEMETRY_TRUCK_CHANNEL_hshifter_selector.name, # indexed
    SCS_TELEMETRY_TRUCK_CHANNEL_hshifter_slot.name: ('shifter', 'slot', lambda v: 0 if shared_data_['telemetry_data']['shifter']['type'] != SCS_SHIFTER_TYPE_hshifter else v),
    # SCS_TELEMETRY_TRUCK_CHANNEL_input_brake.name,
    # SCS_TELEMETRY_TRUCK_CHANNEL_input_clutch.name,
    # SCS_TELEMETRY_TRUCK_CHANNEL_input_steering.name,
    # SCS_TELEMETRY_TRUCK_CHANNEL_input_throttle.name,
    SCS_TELEMETRY_TRUCK_CHANNEL_lblinker.name: ('truck', 'blinkerLeftOn'),
    SCS_TELEMETRY_TRUCK_CHANNEL_light_aux_front.name: ('truck', 'lightsAuxFrontOn', non_zero),
    SCS_TELEMETRY_TRUCK_CHANNEL_light_aux_roof.name: ('truck', 'lightsAuxRoofOn', non_zero),
    SCS_TELEMETRY_TRUCK_CHANNEL_light_beacon.name: ('truck', 'lightsBeaconOn'),
    SCS_TELEMETRY_TRUCK_CHANNEL_light_brake.name: ('truck', 'lightsBrakeOn'),
    SCS_TELEMETRY_TRUCK_CHANNEL_light_high_beam.name: ('truck', 'lightsBeamHighOn'),
    SCS_TELEMETRY_TRUCK_CHANNEL_light_lblinker.name: ('truck', 'blinkerLeftActive'),
    SCS_TELEMETRY_TRUCK_CHANNEL_light_low_beam.name: ('truck', 'lightsBeamLowOn'),
    SCS_TELEMETRY_TRUCK_CHANNEL_light_parking.name: ('truck', 'lightsParkingOn'),
    SCS_TELEMETRY_TRUCK_CHANNEL_light_rblinker.name: ('truck', 'blinkerRightActive'),
    SCS_TELEMETRY_TRUCK_CHANNEL_light_reverse.name: ('truck', 'lightsReverseOn'),
    # SCS_TELEMETRY_TRUCK_CHANNEL_local_angular_acceleration.name,
    # SCS_TELEMETRY_TRUCK_CHANNEL_local_angular_velocity.name,
    SCS_TELEMETRY_TRUCK_CHANNEL_local_linear_acceleration.name: ('truck', 'acceleration'),
    # SCS_TELEMETRY_TRUCK_CHANNEL_local_linear_velocity.name,
    SCS_TELEMETRY_TRUCK_CHANNEL_motor_brake.name: ('truck', 'motorBrakeOn'),
    SCS_TELEMETRY_TRUCK_CHANNEL_navigation_distance.name: ('navigation', 'estimatedDistance', round),
    SCS_TELEMETRY_TRUCK_CHANNEL_navigation_speed_limit.name: ('navigation', 'speedLimit', mps_to_kph),
    SCS_TELEMETRY_TRUCK_CHANNEL_navigation_time.name: ('navigation', 'estimatedTime', lambda v: GAME_TIME_BASE + timedelta(seconds=v)),
    SCS_TELEMETRY_TRUCK_CHANNEL_odometer.name: ('truck', 'odometer'),
    SCS_TELEMETRY_TRUCK_CHANNEL_oil_pressure.name: ('truck', 'oilPressure'),
    SCS_TELEMETRY_TRUCK_CHANNEL_oil_pressure_warning.name: ('truck', 'oilPressureWarningOn'),
    SCS_TELEMETRY_TRUCK_CHANNEL_oil_temperature.name: ('truck', 'oilTemperature'),
    SCS_TELEMETRY_TRUCK_CHANNEL_parking_brake.name: ('truck', 'parkBrakeOn'),
    SCS_TELEMETRY_TRUCK_CHANNEL_rblinker.name: ('truck', 'blinkerRightOn'),
    SCS_TELEMETRY_TRUCK_CHANNEL_hazard_warning.name: ('truck', 'hazardWarningOn'),
    SCS_TELEMETRY_TRUCK_CHANNEL_retarder_level.name: ('truck', 'retarderBrake'),
    SCS_TELEMETRY_TRUCK_CHANNEL_speed.name: ('truck', 'speed', mps_to_kph),
    SCS_TELEMETRY_TRUCK_CHANNEL_water_temperature.name: ('truck', 'waterTemperature'),
    SCS_TELEMETRY_TRUCK_CHANNEL_water_temperature_warning.name: ('truck', 'waterTemperatureWarningOn'),
    SCS_TELEMETRY_TRUCK_CHANNEL_wear_cabin.name: ('truck', 'wearCabin'),
    SCS_TELEMETRY_TRUCK_CHANNEL_wear_chassis.name: ('truck', 'wearChassis'),
    SCS_TELEMETRY_TRUCK_CHANNEL_wear_engine.name: ('truck', 'wearEngine'),
    SCS_TELEMETRY_TRUCK_CHANNEL_wear_transmission.name: ('truck', 'wearTransmission'),
    SCS_TELEMETRY_TRUCK_CHANNEL_wear_wheels.name: ('truck', 'wearWheels'),
    # SCS_TELEMETRY_TRUCK_CHANNEL_wheel_lift_offset.name, # indexed
    SCS_TELEMETRY_TRUCK_CHANNEL_wheel_lift.name: ('truck', 'wheels', 'lifted', lambda v: v > 0.0), # indexed
    # SCS_TELEMETRY_TRUCK_CHANNEL_wheel_on_ground.name, # indexed
    # SCS_TELEMETRY_TRUCK_CHANNEL_wheel_rotation.name, # indexed
    # SCS_TELEMETRY_TRUCK_CHANNEL_wheel_steering.name, # indexed
    # SCS_TELEMETRY_TRUCK_CHANNEL_wheel_substance.name, # indexed
    # SCS_TELEMETRY_TRUCK_CHANNEL_wheel_susp_deflection.name, # indexed
    # SCS_TELEMETRY_TRUCK_CHANNEL_wheel_velocity.name, # indexed
    SCS_TELEMETRY_TRUCK_CHANNEL_wipers.name: ('truck', 'wipersOn'),
    SCS_TELEMETRY_TRUCK_CHANNEL_world_placement.name: ('truck', 'placement', flatten_placement),
}

# optional Callable converts the value
CONFIG_EVENT_MAP: dict[str, dict[str, tuple[str, str] | tuple[str, str, str] | tuple[str, str, Callable]]] = {

    # SCS_TELEMETRY_CONFIG_substances: {
    #     SCS_TELEMETRY_CONFIG_ATTRIBUTE_id.name,
    # },

    SCS_TELEMETRY_CONFIG_controls: {
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_shifter_type.name: ('shifter', 'type'),
    },

    SCS_TELEMETRY_CONFIG_hshifter: {
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_selector_count.name: ('shifter', 'selectorCount', lambda v: 1 if shared_data_['telemetry_data']['shifter']['type'] != SCS_SHIFTER_TYPE_hshifter else math.pow(2, v)),
        # SCS_TELEMETRY_CONFIG_ATTRIBUTE_slot_gear.name:, # indexed
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_slot_handle_position.name: ('shifter', '_handlePositions'), # indexed
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_slot_selectors.name: ('shifter', '_bitMasks'), # indexed
    },

    SCS_TELEMETRY_CONFIG_truck: {
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_brand_id.name: ('truck', 'id'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_brand.name: ('truck', 'make'),
        # SCS_TELEMETRY_CONFIG_ATTRIBUTE_id.name,
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_name.name: ('truck', 'model'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_fuel_capacity.name: ('truck', 'fuelCapacity'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_fuel_warning_factor.name: ('truck', 'fuelWarningFactor'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_adblue_capacity.name: ('truck', 'adblueCapacity'),
        # SCS_TELEMETRY_CONFIG_ATTRIBUTE_adblue_warning_factor.name,
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_air_pressure_warning.name: ('truck', 'airPressureWarningValue'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_air_pressure_emergency.name: ('truck', 'airPressureEmergencyValue'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_oil_pressure_warning.name: ('truck', 'oilPressureWarningValue'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_water_temperature_warning.name: ('truck', 'waterTemperatureWarningValue'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_battery_voltage_warning.name: ('truck', 'batteryVoltageWarningValue'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_rpm_limit.name: ('truck', 'engineRpmMax'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_forward_gear_count.name: ('shifter', 'forwardGears'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_reverse_gear_count.name: ('shifter', 'reverseGears'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_retarder_step_count.name: ('truck', 'retarderStepCount'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_cabin_position.name: ('truck', 'cabin'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_head_position.name: ('truck', 'head'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_hook_position.name: ('truck', 'hook'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_wheel_count.name: ('truck', 'wheelCount'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_wheel_position.name: ('truck', 'wheels', 'position'), # indexed
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_wheel_steerable.name: ('truck', 'wheels', 'steerable'), # indexed
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_wheel_simulated.name: ('truck', 'wheels', 'simulated'), # indexed
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_wheel_radius.name: ('truck', 'wheels', 'radius'), # indexed
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_wheel_powered.name: ('truck', 'wheels', 'powered'), # indexed
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_wheel_liftable.name: ('truck', 'wheels', 'liftable'), # indexed
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_differential_ratio.name: ('shifter', 'differentialRatio'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_forward_ratio.name: ('shifter', 'forwardGearRatios'), # indexed
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_reverse_ratio.name: ('shifter', 'reverseGearRatios'), # indexed
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_license_plate.name: ('truck', 'licensePlate'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_license_plate_country.name: ('truck', 'licensePlateCountry'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_license_plate_country_id.name: ('truck', 'licensePlateCountryId'),
    },

    SCS_TELEMETRY_CONFIG_trailer: {
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_id.name: ('trailers', 'id'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_cargo_accessory_id.name: ('trailers', 'cargoAccessoryId'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_hook_position.name: ('trailers', 'hook'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_wheel_count.name: ('trailers', 'wheelCount'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_wheel_position.name: ('trailers', 'wheels', 'position'), # indexed
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_wheel_steerable.name: ('trailers', 'wheels', 'steerable'), # indexed
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_wheel_simulated.name: ('trailers', 'wheels', 'simulated'), # indexed
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_wheel_radius.name: ('trailers', 'wheels', 'radius'), # indexed
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_wheel_powered.name: ('trailers', 'wheels', 'powered'), # indexed
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_wheel_liftable.name: ('trailers', 'wheels', 'liftable'), # indexed
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_body_type.name: ('trailers', 'bodyType'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_brand_id.name: ('trailers', 'brandId'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_brand.name: ('trailers', 'brand'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_name.name: ('trailers', 'name'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_chain_type.name: ('trailers', 'chainType'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_license_plate.name: ('trailers', 'licensePlate'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_license_plate_country.name: ('trailers', 'licensePlateCountry'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_license_plate_country_id.name: ('trailers', 'licensePlateCountryId'),
    },

    SCS_TELEMETRY_CONFIG_job: {
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_cargo_id.name: ('cargo', 'cargoId'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_cargo.name: ('cargo', 'cargo'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_cargo_mass.name: ('cargo', 'mass'),
        # SCS_TELEMETRY_CONFIG_ATTRIBUTE_destination_city_id.name: ('job', 'destinationCityId'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_destination_city.name: ('job', 'destinationCity'),
        # SCS_TELEMETRY_CONFIG_ATTRIBUTE_source_city_id.name: ('job', 'sourceCityId'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_source_city.name: ('job', 'sourceCity'),
        # SCS_TELEMETRY_CONFIG_ATTRIBUTE_destination_company_id.name: ('job', 'destinationCompanyId'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_destination_company.name: ('job', 'destinationCompany'),
        # SCS_TELEMETRY_CONFIG_ATTRIBUTE_source_company_id.name: ('job', 'sourceCompanyId'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_source_company.name: ('job', 'sourceCompany'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_income.name: ('job', 'income'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_delivery_time.name: ('job', 'deadlineTime', lambda v: GAME_TIME_BASE + timedelta(minutes=v)),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_is_cargo_loaded.name: ('cargo', 'cargoLoaded'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_job_market.name: ('job', 'jobMarket'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_special_job.name: ('job', 'specialTransport'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_cargo_unit_count.name: ('cargo', 'unitCount'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_cargo_unit_mass.name: ('cargo', 'unitMass'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_planned_distance_km.name: ('job', 'plannedDistance'),
    },

    SCS_TELEMETRY_GAMEPLAY_EVENT_job_cancelled: {
        SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_cancel_penalty.name: ('jobEvent', 'cancelPenalty'),
    },

    SCS_TELEMETRY_GAMEPLAY_EVENT_job_delivered: {
        SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_revenue.name: ('jobEvent', 'revenue'),
        SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_earned_xp.name: ('jobEvent', 'earnedXp'),
        SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_cargo_damage.name: ('jobEvent', 'cargoDamage'),
        SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_distance_km.name: ('jobEvent', 'distance'),
        SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_delivery_time.name: ('jobEvent', 'deliveryTime', lambda v: GAME_TIME_BASE + timedelta(minutes=v)),
        SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_auto_park_used.name: ('jobEvent', 'autoparkUsed'),
        SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_auto_load_used.name: ('jobEvent', 'autoloadUsed'),
    },

    SCS_TELEMETRY_GAMEPLAY_EVENT_player_fined: {
        SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_fine_offence.name: ('finedEvent', 'fineOffense'),
        SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_fine_amount.name: ('finedEvent', 'fineAmount'),
    },

    SCS_TELEMETRY_GAMEPLAY_EVENT_player_tollgate_paid: {
        SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_pay_amount.name: ('tollgateEvent', 'payAmount'),
    },

    SCS_TELEMETRY_GAMEPLAY_EVENT_player_use_ferry: {
        SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_pay_amount.name: ('ferryEvent', 'payAmount'),
        SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_source_name.name: ('ferryEvent', 'sourceName'),
        SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_target_name.name: ('ferryEvent', 'targetName'),
        SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_source_id.name: ('ferryEvent', 'sourceId'),
        SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_target_id.name: ('ferryEvent', 'targetId'),
    },

    SCS_TELEMETRY_GAMEPLAY_EVENT_player_use_train: {
        SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_pay_amount.name: ('trainEvent', 'payAmount'),
        SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_source_name.name: ('trainEvent', 'sourceName'),
        SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_target_name.name: ('trainEvent', 'targetName'),
        SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_source_id.name: ('trainEvent', 'sourceId'),
        SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_target_id.name: ('trainEvent', 'targetId'),
    },

}

# optional Callable converts nothing, it is just called
GAMEPLAY_EVENTS_MAP: dict[str, tuple[str, str] | tuple[str, str, Callable]] = {
    SCS_TELEMETRY_GAMEPLAY_EVENT_job_cancelled: ('jobEvent', 'jobCancelled', reset_job_data),
    SCS_TELEMETRY_GAMEPLAY_EVENT_job_delivered: ('jobEvent', 'jobDelivered', reset_job_data),
    SCS_TELEMETRY_GAMEPLAY_EVENT_player_fined: ('finedEvent', 'fined'),
    SCS_TELEMETRY_GAMEPLAY_EVENT_player_tollgate_paid: ('tollgateEvent', 'tollgateUsed'),
    SCS_TELEMETRY_GAMEPLAY_EVENT_player_use_ferry: ('ferryEvent', 'ferryUsed'),
    SCS_TELEMETRY_GAMEPLAY_EVENT_player_use_train: ('trainEvent', 'trainUsed'),
}
