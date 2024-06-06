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
from typing import Any, Callable

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
        # if _active_dict.get(key) == None:
        #     _active_dict[key] = {}
        _active_dict = _active_dict.get(key)

def recount_trailers():
    connected = 0
    for trailer in shared_data_['telemetry_data']['trailers'].values():
        if trailer['present'] == True:
            connected += 1
    shared_data_['telemetry_data']['trailerCount'] = connected
    shared_data_['telemetry_data']['trailer'] = shared_data_['telemetry_data']['trailers'][0] # rebind first trailer, just in case

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
    global game_time_

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

        channel_config_name = channel.name

        # disect indexed events
        indexed_pattern = r'^\w+\.(\d)\.[\.\w]+$'
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
            if sub and channel.indexed:
                json_path.append(index)
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
            trailer_match = re.match(r'^trailer\.(\d)$', event_id)
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

                        if name == SCS_TELEMETRY_CONFIG_ATTRIBUTE_delivery_time:
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
                            if key == SCS_TELEMETRY_CONFIG_ATTRIBUTE_id:
                                set_shared_value(json_path + ['present'], value != None and value != '')

                        json_path.append(key)

                        # indexed value
                        if sub:
                            json_path.append(index)
                            json_path.append(sub)

                        set_shared_value(json_path, value)

                if is_job and is_empty and onJob_:
                    onJob_ = False
                    toggle_shared_bool(['jobEvent', 'jobFinished'])
                elif is_job and not is_empty and not onJob_:
                    onJob_ = True

                if trailer_match:
                    recount_trailers()

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

def clone_array(blueprint, count) -> dict[int, Any]:
    result: dict[int, Any] = {}
    for i in range(0, count):
        result[i] = (clone(blueprint))
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
        'wheels': clone_array(bp_wheel, max_wheels),
    }
    # bp_shifter_selector = {
    #     'selector': 0,
    #     'gear': 0,
    #     'gearName': '',
    # }
    # bp_shifter_slot = {
    #     'slot': 0,
    #     'selectors': clone_array(bp_shifter_selector, 0),
    # }

    # compose data
    trailers = clone_array(bp_trailer, max_trailers)
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
            'gear': 0,
            'displayedGear': 0,
            'forwardGears': 12,
            'reverseGears': 1,
            'shifterType': SCS_SHIFTER_TYPE_automatic,
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
            'shifterSlot': 0,
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
            'wheels': clone_array(bp_wheel, max_wheels),
        },
        # 'shifter': {
        #     'type': '',
        #     'forwardGears': 0,
        #     'forwardGearNames': [], # string
        #     'reverseGears': 0,
        #     'reverseGearNames': [], # string
        #     'differentialRatio': 0.0,
        #     'forwardGearRatios': [], # float
        #     'reverseGearRatios': [], # float
        #     'tyreCircumference': 0.0,
        #     'forwardSpeedAt1500Rpm': [], # int
        #     'reverseSpeedAt1500Rpm': [], # int
        #     'forwardRpmAtCurrentSpeed': [], # int
        #     'reverseRpmAtCurrentSpeed': [], # int
        #     'selectorCount': 0,
        #     'slotCount': 0,
        #     'slots': clone_array(bp_shifter_slot, 0), # IEts2ShifterSlot
        #     'gear': 0,
        #     'displayedGear': 0,
        #     'displayedGearName': '',
        #     'gearRatio': 0.0,
        #     'slot': 0,
        #     'selector': 0,
        #     'bestGear': 0,
        #     'bestGearName': '',
        # },
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
    SCS_TELEMETRY_TRUCK_CHANNEL_displayed_gear.name: ('truck', 'displayedGear'),
    SCS_TELEMETRY_TRUCK_CHANNEL_effective_brake.name: ('truck', 'gameBrake'),
    SCS_TELEMETRY_TRUCK_CHANNEL_effective_clutch.name: ('truck', 'gameClutch'),
    SCS_TELEMETRY_TRUCK_CHANNEL_effective_steering.name: ('truck', 'gameSteer'),
    SCS_TELEMETRY_TRUCK_CHANNEL_effective_throttle.name: ('truck', 'gameThrottle'),
    SCS_TELEMETRY_TRUCK_CHANNEL_electric_enabled.name: ('truck', 'electricOn'),
    SCS_TELEMETRY_TRUCK_CHANNEL_engine_enabled.name: ('truck', 'engineOn'),
    SCS_TELEMETRY_TRUCK_CHANNEL_engine_gear.name: ('truck', 'gear'),
    SCS_TELEMETRY_TRUCK_CHANNEL_engine_rpm.name: ('truck', 'engineRpm'),
    SCS_TELEMETRY_TRUCK_CHANNEL_fuel_average_consumption.name: ('truck', 'fuelAverageConsumption'),
    SCS_TELEMETRY_TRUCK_CHANNEL_fuel.name: ('truck', 'fuel'),
    SCS_TELEMETRY_TRUCK_CHANNEL_fuel_warning.name: ('truck', 'fuelWarningOn'),
    # SCS_TELEMETRY_TRUCK_CHANNEL_head_offset.name,
    # SCS_TELEMETRY_TRUCK_CHANNEL_hshifter_selector.name, # indexed
    SCS_TELEMETRY_TRUCK_CHANNEL_hshifter_slot.name: ('truck', 'shifterSlot'),
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
    #     SCS_TELEMETRY_CONFIG_ATTRIBUTE_id,
    # },

    SCS_TELEMETRY_CONFIG_controls: {
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_shifter_type: ('truck', 'shifterType'),
    },

    #SCS_TELEMETRY_CONFIG_hshifter: {
    #    SCS_TELEMETRY_CONFIG_ATTRIBUTE_selector_count: ('shifter', 'selectorCount'),
    #    SCS_TELEMETRY_CONFIG_ATTRIBUTE_slot_gear:, # indexed
    #    SCS_TELEMETRY_CONFIG_ATTRIBUTE_slot_handle_position, # indexed
    #    SCS_TELEMETRY_CONFIG_ATTRIBUTE_slot_selectors, # indexed
    #},

    SCS_TELEMETRY_CONFIG_truck: {
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_brand_id: ('truck', 'id'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_brand: ('truck', 'make'),
        # SCS_TELEMETRY_CONFIG_ATTRIBUTE_id,
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_name: ('truck', 'model'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_fuel_capacity: ('truck', 'fuelCapacity'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_fuel_warning_factor: ('truck', 'fuelWarningFactor'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_adblue_capacity: ('truck', 'adblueCapacity'),
        # SCS_TELEMETRY_CONFIG_ATTRIBUTE_adblue_warning_factor,
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_air_pressure_warning: ('truck', 'airPressureWarningValue'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_air_pressure_emergency: ('truck', 'airPressureEmergencyValue'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_oil_pressure_warning: ('truck', 'oilPressureWarningValue'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_water_temperature_warning: ('truck', 'waterTemperatureWarningValue'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_battery_voltage_warning: ('truck', 'batteryVoltageWarningValue'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_rpm_limit: ('truck', 'engineRpmMax'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_forward_gear_count: ('truck', 'forwardGears'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_reverse_gear_count: ('truck', 'reverseGears'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_retarder_step_count: ('truck', 'retarderStepCount'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_cabin_position: ('truck', 'cabin'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_head_position: ('truck', 'head'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_hook_position: ('truck', 'hook'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_wheel_count: ('truck', 'wheelCount'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_wheel_position: ('truck', 'wheels', 'position'), # indexed
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_wheel_steerable: ('truck', 'wheels', 'steerable'), # indexed
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_wheel_simulated: ('truck', 'wheels', 'simulated'), # indexed
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_wheel_radius: ('truck', 'wheels', 'radius'), # indexed
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_wheel_powered: ('truck', 'wheels', 'powered'), # indexed
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_wheel_liftable: ('truck', 'wheels', 'liftable'), # indexed
        # SCS_TELEMETRY_CONFIG_ATTRIBUTE_differential_ratio: ('shifter', 'differentialRatio'),
        # SCS_TELEMETRY_CONFIG_ATTRIBUTE_forward_ratio, # indexed
        # SCS_TELEMETRY_CONFIG_ATTRIBUTE_reverse_ratio, # indexed
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_license_plate: ('truck', 'licensePlate'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_license_plate_country: ('truck', 'licensePlateCountry'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_license_plate_country_id: ('truck', 'licensePlateCountryId'),
    },

    SCS_TELEMETRY_CONFIG_trailer: {
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_id: ('trailers', 'id'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_cargo_accessory_id: ('trailers', 'cargoAccessoryId'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_hook_position: ('trailers', 'hook'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_wheel_count: ('trailers', 'wheelCount'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_wheel_position: ('trailers', 'wheels', 'position'), # indexed
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_wheel_steerable: ('trailers', 'wheels', 'steerable'), # indexed
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_wheel_simulated: ('trailers', 'wheels', 'simulated'), # indexed
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_wheel_radius: ('trailers', 'wheels', 'radius'), # indexed
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_wheel_powered: ('trailers', 'wheels', 'powered'), # indexed
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_wheel_liftable: ('trailers', 'wheels', 'liftable'), # indexed
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_body_type: ('trailers', 'bodyType'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_brand_id: ('trailers', 'brandId'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_brand: ('trailers', 'brand'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_name: ('trailers', 'name'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_chain_type: ('trailers', 'chainType'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_license_plate: ('trailers', 'licensePlate'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_license_plate_country: ('trailers', 'licensePlateCountry'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_license_plate_country_id: ('trailers', 'licensePlateCountryId'),
    },

    SCS_TELEMETRY_CONFIG_job: {
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_cargo_id: ('cargo', 'cargoId'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_cargo: ('cargo', 'cargo'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_cargo_mass: ('cargo', 'mass'),
        # SCS_TELEMETRY_CONFIG_ATTRIBUTE_destination_city_id: ('job', 'destinationCityId'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_destination_city: ('job', 'destinationCity'),
        # SCS_TELEMETRY_CONFIG_ATTRIBUTE_source_city_id: ('job', 'sourceCityId'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_source_city: ('job', 'sourceCity'),
        # SCS_TELEMETRY_CONFIG_ATTRIBUTE_destination_company_id: ('job', 'destinationCompanyId'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_destination_company: ('job', 'destinationCompany'),
        # SCS_TELEMETRY_CONFIG_ATTRIBUTE_source_company_id: ('job', 'sourceCompanyId'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_source_company: ('job', 'sourceCompany'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_income: ('job', 'income'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_delivery_time: ('job', 'deadlineTime', lambda v: GAME_TIME_BASE + timedelta(minutes=v)),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_is_cargo_loaded: ('cargo', 'cargoLoaded'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_job_market: ('job', 'jobMarket'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_special_job: ('job', 'specialTransport'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_cargo_unit_count: ('cargo', 'unitCount'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_cargo_unit_mass: ('cargo', 'unitMass'),
        SCS_TELEMETRY_CONFIG_ATTRIBUTE_planned_distance_km: ('job', 'plannedDistance'),
    },

    SCS_TELEMETRY_GAMEPLAY_EVENT_job_cancelled: {
        SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_cancel_penalty: ('jobEvent', 'cancelPenalty'),
    },

    SCS_TELEMETRY_GAMEPLAY_EVENT_job_delivered: {
        SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_revenue: ('jobEvent', 'revenue'),
        SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_earned_xp: ('jobEvent', 'earnedXp'),
        SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_cargo_damage: ('jobEvent', 'cargoDamage'),
        SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_distance_km: ('jobEvent', 'distance'),
        SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_delivery_time: ('jobEvent', 'deliveryTime', lambda v: GAME_TIME_BASE + timedelta(minutes=v)),
        SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_auto_park_used: ('jobEvent', 'autoparkUsed'),
        SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_auto_load_used: ('jobEvent', 'autoloadUsed'),
    },

    SCS_TELEMETRY_GAMEPLAY_EVENT_player_fined: {
        SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_fine_offence: ('finedEvent', 'fineOffense'),
        SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_fine_amount: ('finedEvent', 'fineAmount'),
    },

    SCS_TELEMETRY_GAMEPLAY_EVENT_player_tollgate_paid: {
        SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_pay_amount: ('tollgateEvent', 'payAmount'),
    },

    SCS_TELEMETRY_GAMEPLAY_EVENT_player_use_ferry: {
        SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_pay_amount: ('ferryEvent', 'payAmount'),
        SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_source_name: ('ferryEvent', 'sourceName'),
        SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_target_name: ('ferryEvent', 'targetName'),
        SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_source_id: ('ferryEvent', 'sourceId'),
        SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_target_id: ('ferryEvent', 'targetId'),
    },

    SCS_TELEMETRY_GAMEPLAY_EVENT_player_use_train: {
        SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_pay_amount: ('trainEvent', 'payAmount'),
        SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_source_name: ('trainEvent', 'sourceName'),
        SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_target_name: ('trainEvent', 'targetName'),
        SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_source_id: ('trainEvent', 'sourceId'),
        SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_target_id: ('trainEvent', 'targetId'),
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
