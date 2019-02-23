"""
Support for nibe uplink.
"""


from datetime import timedelta
import logging
import asyncio
import json
import voluptuous as vol
from typing import (List, Iterable)
from collections import defaultdict
import homeassistant.helpers.config_validation as cv

from homeassistant.helpers import discovery
from homeassistant.util.json import load_json, save_json
from homeassistant.util.dt import parse_datetime, now
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.components import persistent_notification
from homeassistant.const import (
    CONF_PLATFORM,
    ATTR_SECONDS,
)
from .auth import NibeAuthView

_LOGGER = logging.getLogger(__name__)

DOMAIN              = 'nibe'
DATA_NIBE           = 'nibe'
INTERVAL            = timedelta(minutes=1)
THROTTLE            = 2  # seconds

REQUIREMENTS        = ['nibeuplink==0.4.3']

CONF_CLIENT_ID      = 'client_id'
CONF_CLIENT_SECRET  = 'client_secret'
CONF_REDIRECT_URI   = 'redirect_uri'
CONF_WRITEACCESS    = 'writeaccess'
CONF_CATEGORIES     = 'categories'
CONF_SENSORS        = 'sensors'
CONF_STATUSES       = 'statuses'
CONF_SYSTEMS        = 'systems'
CONF_SYSTEM         = 'system'
CONF_UNITS          = 'units'
CONF_UNIT           = 'unit'
CONF_CLIMATES       = 'climates'
CONF_PARAMETER      = 'parameter'
CONF_OBJECTID       = 'object_id'
CONF_DATA           = 'data'
CONF_CLIMATE        = 'climate'
CONF_CURRENT        = 'current'
CONF_TARGET         = 'target'
CONF_ADJUST         = 'adjust'
CONF_ACTIVE         = 'active'
CONF_SWITCHES       = 'switches'
CONF_BINARY_SENSORS = 'binary_sensors'
CONF_SKIP_SENSORS   = 'skip_sensors'
CONF_STATUS         = 'status'
CONF_THROTTLE       = 'throttle'

SIGNAL_UPDATE       = 'nibe_update'

BINARY_SENSOR_VALUES = ('off', 'on', 'yes', 'no')

UNIT_SCHEMA = vol.Schema({
    vol.Required(CONF_UNIT): cv.positive_int,
    vol.Optional(CONF_CATEGORIES): vol.All(cv.ensure_list, [cv.string]),
    vol.Optional(CONF_STATUSES): vol.All(cv.ensure_list, [cv.string]),
    vol.Optional(CONF_SENSORS): vol.All(cv.ensure_list, [cv.string]),
    vol.Optional(CONF_CLIMATES): vol.All(cv.ensure_list, [cv.string]),
    vol.Optional(CONF_SWITCHES): vol.All(cv.ensure_list, [cv.string]),
    vol.Optional(CONF_BINARY_SENSORS): vol.All(cv.ensure_list, [cv.string]),
})

SYSTEM_SCHEMA = vol.Schema({
    vol.Required(CONF_SYSTEM): cv.positive_int,
    vol.Optional(CONF_UNITS): vol.All(cv.ensure_list, [UNIT_SCHEMA]),
    vol.Optional(CONF_SKIP_SENSORS): vol.All(cv.ensure_list, [cv.string]),
    vol.Optional(CONF_STATUS, default=False): cv.boolean,
})

NIBE_SCHEMA = vol.Schema({
    vol.Required(CONF_REDIRECT_URI): cv.string,
    vol.Required(CONF_CLIENT_ID): cv.string,
    vol.Required(CONF_CLIENT_SECRET): cv.string,
    vol.Required(CONF_CLIENT_SECRET): cv.string,
    vol.Optional(CONF_WRITEACCESS, default=False): cv.boolean,
    vol.Optional(CONF_SYSTEMS, default=[]):
        vol.All(cv.ensure_list, [SYSTEM_SCHEMA]),
    vol.Optional(CONF_THROTTLE, default=THROTTLE): cv.positive_int,
})

CONFIG_SCHEMA = vol.Schema({
    DOMAIN: NIBE_SCHEMA
}, extra=vol.ALLOW_EXTRA)

SCAN_INTERVAL = timedelta(seconds=60)


async def async_setup_systems(hass, config, uplink):

    if not len(config.get(CONF_SYSTEMS)):
        systems = await uplink.get_systems()
        msg = json.dumps(systems, indent=1)
        persistent_notification.async_create(hass, 'No systems selected, please configure one system id of:<br/><br/><pre>{}</pre>'.format(msg) , 'Invalid nibe config', 'invalid_config')
        return

    systems = [
        NibeSystem(hass,
                   uplink,
                   config[CONF_SYSTEM],
                   config)
        for config in config.get(CONF_SYSTEMS)
    ]

    hass.data[DATA_NIBE] = {}
    hass.data[DATA_NIBE]['systems'] = systems
    hass.data[DATA_NIBE]['uplink'] = uplink

    tasks = [system.load() for system in systems]

    await asyncio.gather(*tasks)


async def async_setup(hass, config):
    """Setup nibe uplink component"""

    store = hass.config.path('nibe.json')

    def save_json_local(data):
        save_json(store, data)

    from nibeuplink import Uplink

    scope = None
    if config[DOMAIN].get(CONF_WRITEACCESS):
        scope = ['READSYSTEM', 'WRITESYSTEM']
    else:
        scope = ['READSYSTEM']

    uplink = Uplink(
        client_id=config[DOMAIN].get(CONF_CLIENT_ID),
        client_secret=config[DOMAIN].get(CONF_CLIENT_SECRET),
        redirect_uri=config[DOMAIN].get(CONF_REDIRECT_URI),
        access_data=load_json(store),
        access_data_write=save_json_local,
        scope=scope,
        throttle=timedelta(seconds=config[DOMAIN].get(CONF_THROTTLE)),
    )

    if not uplink.access_data:
        view = NibeAuthView(hass, uplink, config[DOMAIN], async_setup_systems)
        hass.http.register_view(view)
        view.async_request_config()
    else:
        hass.async_add_job(async_setup_systems(hass, config[DOMAIN], uplink))

    return True


def filter_list(data: List[dict], field: str, selected: List[str]):
    """Return a filtered array based on existance in a filter list"""
    if len(selected):
        return [x for x in data if x[field] in selected]
    else:
        return data


class NibeSystem(object):
    def __init__(self, hass, uplink, system_id, config):
        self.hass = hass
        self.parameters = {}
        self.config = config
        self.system_id = system_id
        self.system = None
        self.uplink = uplink
        self.notice = []
        self.discovered = defaultdict(set)

    def filter_discovered(self, discovery_info, platform):
        """Keep unique discovery list, to avoid duplicate loads"""
        table = self.discovered[platform]
        for entry in discovery_info:
            object_id = entry.get(CONF_OBJECTID)
            if object_id in table:
                continue
            table.add(object_id)
            yield entry

    async def load_platform(self, discovery_info, platform):
        """Load plaform avoding duplicates"""
        load_info = list(self.filter_discovered(discovery_info, platform))
        if load_info:
            await discovery.async_load_platform(
                self.hass,
                platform,
                DOMAIN,
                load_info,
                self.config)

        """Return entity id of all objects, even skipped"""
        return [
            '{}.{}'.format(platform, x[CONF_OBJECTID])
            for x in discovery_info
        ]

    async def load_parameter_group(self,
                                   name: str,
                                   object_id: str,
                                   parameters: List[dict]):

        sensors = {}
        binary_sensors = {}
        skip_sensors = self.config.get(CONF_SKIP_SENSORS) or []
        for x in parameters:
            param_id = x['parameterId']
            if str(param_id) in skip_sensors:
                continue
            if str(x['value']).lower() in BINARY_SENSOR_VALUES:
                binary_sensors[param_id] = x
            else:
                sensors[param_id] = x

        entity_ids = []
        if sensors:
            entity_ids.extend(
                await self.load_component('sensor', sensors.keys(), sensors)
            )
        if binary_sensors:
            entity_ids.extend(
                await self.load_component('binary_sensor',
                                          binary_sensors.keys(),
                                          binary_sensors)
            )

        group = self.hass.components.group
        entity = await group.Group.async_create_group(
            self.hass,
            name=name,
            control=False,
            entity_ids=entity_ids,
            object_id='{}_{}_{}'.format(DOMAIN, self.system_id, object_id))
        return entity.entity_id

    async def load_categories(self,
                              unit: int,
                              selected):
        data = await self.uplink.get_categories(self.system_id, True, unit)
        data = filter_list(data, 'categoryId', selected)
        tasks = [
            self.load_parameter_group(
                x['name'],
                '{}_{}'.format(unit, x['categoryId']),
                x['parameters'])
            for x in data
        ]
        return await asyncio.gather(*tasks)

    async def load_status(self,
                          unit: int):
        data = await self.uplink.get_unit_status(self.system_id, unit)
        tasks = [
            self.load_parameter_group(
                x['title'],
                '{}_{}'.format(unit, x['title']),
                x['parameters'])
            for x in data
        ]
        return await asyncio.gather(*tasks)

    async def load_component(self,
                             component: str,
                             ids: Iterable[str],
                             data: dict = {}):
        _LOGGER.debug("Loading {}: {}".format(component, ids))

        param = CONF_CLIMATE if component == 'climate' else CONF_PARAMETER
        discovery_info = [
            {
                CONF_PLATFORM: DOMAIN,
                CONF_SYSTEM: self.system['systemId'],
                param: x,
                CONF_OBJECTID: '{}_{}_{}'.format(DOMAIN,
                                                 self.system_id,
                                                 str(x)),
                CONF_DATA: data.get(x, None)
            }
            for x in ids
        ]
        return await self.load_platform(discovery_info, component)

    async def load_unit(self, unit):
        entities = []
        if CONF_CATEGORIES in unit:
            entities.extend(
                await self.load_categories(
                    unit.get(CONF_UNIT),
                    unit.get(CONF_CATEGORIES)))

        if CONF_STATUSES in unit:
            entities.extend(
                await self.load_status(
                    unit.get(CONF_UNIT)))

        if CONF_SENSORS in unit:
            entities.extend(
                await self.load_component(
                    'sensor',
                    unit.get(CONF_SENSORS)))

        if CONF_CLIMATES in unit:
            entities.extend(
                await self.load_component(
                    'climate',
                    unit.get(CONF_CLIMATES)))

        if CONF_SWITCHES in unit:
            entities.extend(
                await self.load_component(
                    'switch',
                    unit.get(CONF_SWITCHES)))

        if CONF_BINARY_SENSORS in unit:
            entities.extend(
                await self.load_component(
                    'binary_sensor',
                    unit.get(CONF_BINARY_SENSORS)))

        group = self.hass.components.group
        return await group.Group.async_create_group(
            self.hass,
            '{} - Unit {}'.format(self.system['productName'], unit.get(CONF_UNIT)),
            user_defined=False,
            control=False,
            view=True,
            icon='mdi:thermostat',
            object_id='{}_{}_{}'.format(DOMAIN,
                                        self.system_id,
                                        unit.get(CONF_UNIT)),
            entity_ids=entities)

    async def load(self):
        if not self.system:
            self.system = await self.uplink.get_system(self.system_id)
            if self.config.get(CONF_STATUS):
                _LOGGER.debug("Adding status sensors")
                status = await self.uplink.get_status(self.system_id)
                component = EntityComponent(_LOGGER, DOMAIN, self.hass)
                extra_entities = [
                    NibeSystemSensor(self.uplink, self.system_id, self.system),
                    NibeSystemStatusSensor(self.uplink, self.system_id, status),
                ]
                await component.async_add_entities(extra_entities)

        for unit in self.config.get(CONF_UNITS):
            await self.load_unit(unit)

        await self.update()
        async_track_time_interval(self.hass, self.update, INTERVAL)

    async def update(self, now=None):
        notice = await self.uplink.get_notifications(self.system_id)
        added = [k for k in notice if k not in self.notice]
        removed = [k for k in self.notice if k not in notice]
        self.notice = notice

        for x in added:
            persistent_notification.async_create(
                self.hass,
                x['info']['description'],
                x['info']['title'],
                'nibe:{}'.format(x['notificationId'])
            )
        for x in removed:
            persistent_notification.async_dismiss(
                'nibe:{}'.format(x['notificationId'])
            )


class NibeSystemSensor(Entity):
    """Representation of system attributes."""

    SYSTEM_ATTRS = ['name', 'productName', 'securityLevel', 'serialNumber', 'lastActivityDate', 'connectionStatus', 'hasAlarmed']
    STATE_ATTR = "lastActivityDate"

    def __init__(self, uplink, system_id, data):
        self._uplink = uplink
        self._system_id = system_id
        self._name = "{}_{}".format(system_id, "system")
        self._data = {}
        self._state = None
        self.parse_data(data)

    @property
    def name(self):
        """Return the name of the device."""
        return self._name

    @property
    def state(self):
        """Return the state of the device."""
        return self._state

    @property
    def unique_id(self):
        """Return a unique identifier for a this parameter"""
        return "{}_{}".format(self._system_id, "system")

    @property
    def device_state_attributes(self):
        """Return the state attributes."""
        return self._data

    @property
    def available(self):
        """Return True if entity is available."""
        return bool(self._data)

    @property
    def unit_of_measurement(self):
        return ATTR_SECONDS

    def parse_data(self, data):
        """Parse dat to update internal variables"""
        if data:
            self._data = {a: data[a] for a in self.SYSTEM_ATTRS if a in data}
            date = parse_datetime(data.get(self.STATE_ATTR))
            delta = now() - date if date else timedelta(seconds=-1)
            self._state = int(delta.total_seconds())
        else:
            self._state = None
            self._data = {}

    async def async_update(self):
        """Fetch new state data for the sensor."""
        try:
            data = await self._uplink.get_system(self._system_id)
            self.parse_data(data)
        except BaseException:
            self.parse_data(None)
            raise

class NibeSystemStatusSensor(Entity):
    """Representation of system attributes."""

    STATE_ATTR = "title"

    def __init__(self, uplink, system_id, data):
        self._uplink = uplink
        self._system_id = system_id
        self._name = "{}_{}".format(system_id, "system_status")
        self._data = {}
        self._state = None
        self.parse_data(data)

    @property
    def name(self):
        """Return the name of the device."""
        return self._name

    @property
    def state(self):
        """Return the state of the device."""
        return self._state

    @property
    def unique_id(self):
        """Return a unique identifier for a this parameter"""
        return "{}_{}".format(self._system_id, "system_status")

    @property
    def device_state_attributes(self):
        """Return the state attributes."""
        return self._data

    def parse_data(self, data):
        """Parse data to update internal variables"""
        if data:
            self._state = data[0][self.STATE_ATTR]
            self._data = {s[self.STATE_ATTR]: "on" for s in data}
        else:
            self._state = None
            self._data = None

    async def async_update(self):
        """Fetch new state data for the sensor."""
        try:
            data = await self._uplink.get_status(self._system_id)
            self.parse_data(data)
        except BaseException:
            self.parse_data(None)
            raise
