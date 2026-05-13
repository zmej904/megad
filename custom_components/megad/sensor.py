import logging
from datetime import datetime
from propcache import cached_property

from homeassistant.components.sensor import (
    SensorEntity, 
    SensorStateClass,
    SensorDeviceClass
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from . import MegaDCoordinator
from .const import (
    DOMAIN, STATE_BUTTON, SENSOR_UNIT, SENSOR_CLASS, TEMPERATURE, UPTIME,
    HUMIDITY, ENTRIES, CURRENT_ENTITY_IDS, CO2, TYPE_SENSOR_RUS, PRESSURE,
    TYPE_SENSOR, TEMPERATURE_CONDITION, DEVIATION_TEMPERATURE,
    ALLOWED_TEMP_JUMP, ALLOWED_HUM_JUMP, CURRENT, VOLTAGE, RAW_VALUE, LUXURY,
    BAR, SINGLE_CLICK, DOUBLE_CLICK, LONG_CLICK, CLICK_TYPES, CLICK_STATES,
    CLICK_STATE_SINGLE, CLICK_STATE_DOUBLE, CLICK_STATE_LONG, CLICK_STATE_NONE,
    WATCHDOG_CHECK_INTERVAL, WATCHDOG_MAX_FAILURES, WATCHDOG_INACTIVITY_TIMEOUT
)
from .core.base_pids import PIDControl
from .core.base_ports import (
    BinaryPortClick, BinaryPortCount, BinaryPortIn, OneWireSensorPort,
    DigitalSensorBase, DHTSensorPort, OneWireBusSensorPort, I2CSensorSCD4x,
    I2CSensorSTH31, I2CSensorHTUxxD, I2CSensorMBx280, ReaderPort,
    I2CSensorINA226, I2CSensorBH1750, I2CSensorT67xx, I2CSensorBMP180,
    I2CSensorPT, I2CSensorILLUM, AnalogSensor
)
from .core.megad import MegaD

_LOGGER = logging.getLogger(__name__)


def create_temp_hum(sensors, entry_id, coordinator, megad, port):
    """Создаём сенсоры температуры и влажности."""
    prefix = port.prefix
    unique_id_temp = (f'{entry_id}-{megad.id}-{port.conf.id}-'
                      f'{TEMPERATURE}{prefix}')
    
    # ✅ ДЛЯ I2C ДАТЧИКОВ: СОЗДАЕМ УНИКАЛЬНЫЙ DEVICE_INFO ДЛЯ КАЖДОЙ СУЩНОСТИ
    if isinstance(port, (DHTSensorPort, I2CSensorSTH31, I2CSensorHTUxxD, 
                         I2CSensorSCD4x, I2CSensorMBx280, I2CSensorBMP180)):
        # Для I2C датчиков каждая физическая величина получает отдельное устройство
        device_suffix = f"{TEMPERATURE}{prefix}"
    else:
        device_suffix = None
    
    sensors.append(SensorMegaD(
        coordinator, port, unique_id_temp, TEMPERATURE, prefix, device_suffix=device_suffix)
    )
    
    unique_id_hum = f'{entry_id}-{megad.id}-{port.conf.id}-{HUMIDITY}{prefix}'
    
    if isinstance(port, (DHTSensorPort, I2CSensorSTH31, I2CSensorHTUxxD,
                         I2CSensorSCD4x, I2CSensorMBx280)):
        # Для влажности на том же I2C датчике
        device_suffix = f"{HUMIDITY}{prefix}"
    
    sensors.append(SensorMegaD(
        coordinator, port, unique_id_hum, HUMIDITY, prefix, device_suffix=device_suffix)
    )


def get_port_mode(port):
    """Определяет режим работы порта на основе его типа"""
    # BinaryPortClick всегда в режиме C (нажатия)
    if isinstance(port, BinaryPortClick):
        return 'C'
    
    # BinaryPortIn может быть в режимах P, R, P&R
    if isinstance(port, BinaryPortIn):
        # Пытаемся определить режим из названия порта
        port_name = port.conf.name.lower()
        
        if ' p&r' in port_name or ' p_r' in port_name or ' p и r' in port_name:
            return 'P&R'
        elif ' p ' in port_name or ' режим p' in port_name:
            return 'P'
        elif ' r ' in port_name or ' режим r' in port_name:
            return 'R'
        else:
            # По умолчанию для BinaryPortIn считаем P&R
            return 'P&R'
    
    return None


async def async_setup_entry(
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        async_add_entities: AddEntitiesCallback
) -> None:
    entry_id = config_entry.entry_id
    coordinator = hass.data[DOMAIN][ENTRIES][entry_id]
    megad = coordinator.megad

    sensors = []
    for port in megad.ports:
        # Определяем режим работы порта
        port_mode = get_port_mode(port)
        _LOGGER.debug(f'Port {port.conf.id} ({port.conf.name}): type={type(port).__name__}, mode={port_mode}, current_state={port.state}')
        
        # Для портов в режиме C создаем сущность нажатий
        if port_mode == 'C':
            unique_id = f'{entry_id}-{megad.id}-{port.conf.id}-click'
            click_sensor = ClickSensorMegaD(coordinator, port, unique_id)
            sensors.append(click_sensor)
            _LOGGER.info(f'Создана сущность нажатий для порта {port.conf.id} (режим C), текущее состояние: {port.state}')
        
        if isinstance(port, ReaderPort):
            unique_id = f'{entry_id}-{megad.id}-{port.conf.id}-reader'
            sensors.append(ReaderSensorMegaD(
                coordinator, port, unique_id)
            )
        if isinstance(port, OneWireSensorPort):
            unique_id = f'{entry_id}-{megad.id}-{port.conf.id}-1wire'
            sensors.append(SensorMegaD(
                coordinator, port, unique_id, TEMPERATURE)
            )
        if isinstance(port, (DHTSensorPort, I2CSensorSTH31, I2CSensorHTUxxD)):
            create_temp_hum(sensors, entry_id, coordinator, megad, port)
        if isinstance(port, OneWireBusSensorPort):
            for id_one_wire in port.state:
                unique_id = (f'{entry_id}-{megad.id}-{port.conf.id}-'
                             f'{id_one_wire}')
                sensors.append(SensorBusMegaD(
                    coordinator, port, unique_id, TEMPERATURE, id_one_wire)
                )
        
        # ✅ I2C датчики будут создаваться правильно
        if isinstance(port, I2CSensorSCD4x):
            prefix = port.prefix
            unique_id_co2 = (f'{entry_id}-{megad.id}-{port.conf.id}-'
                             f'{CO2.lower()}{prefix}')
            
            # ✅ ДЛЯ I2C ДАТЧИКОВ: КАЖДАЯ ВЕЛИЧИНА ПОЛУЧАЕТ УНИКАЛЬНЫЙ DEVICE_INFO
            # Для CO2 сенсора
            sensors.append(SensorMegaD(
                coordinator, port, unique_id_co2, CO2, prefix, 
                device_suffix=f"{CO2}{prefix}"  # ✅ УКАЗЫВАЕМ УНИКАЛЬНЫЙ СУФФИКС
            ))
            
            # Для температуры и влажности на том же датчике
            create_temp_hum(sensors, entry_id, coordinator, megad, port)
            _LOGGER.info(f'Созданы сенсоры для I2C SCD4x на порту {port.conf.id}')
            
        if isinstance(port, I2CSensorMBx280):
            prefix = port.prefix
            unique_id_press = (f'{entry_id}-{megad.id}-{port.conf.id}-'
                               f'{PRESSURE}{prefix}')
            
            # ✅ ДЛЯ ДАТЧИКА ДАВЛЕНИЯ: УНИКАЛЬНЫЙ DEVICE_INFO
            sensors.append(SensorMegaD(
                coordinator, port, unique_id_press, PRESSURE, prefix,
                device_suffix=f"{PRESSURE}{prefix}"  # ✅ УНИКАЛЬНЫЙ СУФФИКС
            ))
            
            # Для температуры и влажности
            create_temp_hum(sensors, entry_id, coordinator, megad, port)
            _LOGGER.info(f'Созданы сенсоры для I2C MBx280 на порту {port.conf.id}')
            
        if isinstance(port, I2CSensorILLUM):
            prefix = port.prefix
            unique_id = (f'{entry_id}-{megad.id}-{port.conf.id}-'
                         f'{LUXURY}{prefix}')
            sensors.append(SensorMegaD(
                coordinator, port, unique_id, LUXURY, prefix)
            )
            _LOGGER.info(f'Создан сенсор освещенности для I2C на порту {port.conf.id}')
            
        if isinstance(port, I2CSensorT67xx):
            prefix = port.prefix
            unique_id = (f'{entry_id}-{megad.id}-{port.conf.id}-'
                         f'{CO2}{prefix}')
            sensors.append(SensorMegaD(
                coordinator, port, unique_id, CO2, prefix)
            )
            _LOGGER.info(f'Создан сенсор CO2 T67xx для I2C на порту {port.conf.id}')
            
        if isinstance(port, I2CSensorBMP180):
            prefix = port.prefix
            unique_id_temp = (f'{entry_id}-{megad.id}-{port.conf.id}-'
                              f'{TEMPERATURE}{prefix}')
            sensors.append(SensorMegaD(
                coordinator, port, unique_id_temp, TEMPERATURE, prefix)
            )
            unique_id_press = (f'{entry_id}-{megad.id}-{port.conf.id}-'
                               f'{PRESSURE}{prefix}')
            sensors.append(SensorMegaD(
                coordinator, port, unique_id_press, PRESSURE, prefix)
            )
            _LOGGER.info(f'Созданы сенсоры для I2C BMP180 на порту {port.conf.id}')
            
        if isinstance(port, I2CSensorPT):
            prefix = port.prefix
            unique_id = (f'{entry_id}-{megad.id}-{port.conf.id}-'
                         f'{BAR}{prefix}')
            sensors.append(SensorMegaD(
                coordinator, port, unique_id, BAR, prefix)
            )
            _LOGGER.info(f'Создан сенсор давления PT для I2C на порту {port.conf.id}')
            
        if isinstance(port, I2CSensorBH1750):
            prefix = port.prefix
            unique_id = (f'{entry_id}-{megad.id}-{port.conf.id}-'
                         f'{LUXURY}{prefix}')
            sensors.append(SensorMegaD(
                coordinator, port, unique_id, LUXURY, prefix)
            )
            _LOGGER.info(f'Создан сенсор освещенности BH1750 для I2C на порту {port.conf.id}')
            
        if isinstance(port, AnalogSensor):
            unique_id = f'{entry_id}-{megad.id}-{port.conf.id}-analog'
            sensors.append(AnalogSensorMegaD(coordinator, port, unique_id))

    # Сенсоры устройства
    sensors.append(SensorDeviceMegaD(
        coordinator, f'{entry_id}-{megad.id}-{TEMPERATURE}', TEMPERATURE)
    )
    sensors.append(SensorDeviceMegaD(
        coordinator, f'{entry_id}-{megad.id}-{UPTIME}', UPTIME)
    )
    
    # PID сенсоры
    for pid in megad.pids:
        unique_id = f'{entry_id}-{megad.id}-{pid.conf.id}-pid-value'
        sensors.append(PIDSensorMegaD(coordinator, pid, unique_id))
    
    # ✅ Watchdog сенсоры
    # 1. Сенсор статуса watchdog
    unique_id_watchdog_status = f'{entry_id}-{megad.id}-watchdog-status'
    sensors.append(WatchdogStatusSensor(coordinator, unique_id_watchdog_status))
    
    # 2. Сенсор времени без данных
    unique_id_watchdog_inactivity = f'{entry_id}-{megad.id}-watchdog-inactivity'
    sensors.append(WatchdogInactivitySensor(coordinator, unique_id_watchdog_inactivity))
    
    # 3. Сенсор статуса обратной связи
    unique_id_feedback_status = f'{entry_id}-{megad.id}-feedback-status'
    sensors.append(WatchdogFeedbackStatusSensor(coordinator, unique_id_feedback_status))
    
    # 4. Сенсор времени без обратной связи
    unique_id_feedback_inactivity = f'{entry_id}-{megad.id}-feedback-inactivity'
    sensors.append(WatchdogFeedbackInactivitySensor(coordinator, unique_id_feedback_inactivity))

    # Регистрируем сущности
    for sensor in sensors:
        hass.data[DOMAIN][CURRENT_ENTITY_IDS][entry_id].append(
            sensor.unique_id)
    
    if sensors:
        async_add_entities(sensors)
        _LOGGER.info(f'Добавлено {len(sensors)} сенсоров для MegaD {megad.id}')
        _LOGGER.debug(f'Добавлены сенсоры: {sensors}')

# СУЩЕСТВУЮЩИЕ КЛАССЫ (без изменений)

class StringSensorMegaD(CoordinatorEntity, SensorEntity):
    """Класс для сенсоров с текстовым значением"""

    def __init__(
            self, coordinator: MegaDCoordinator, port,
            unique_id: str
    ) -> None:
        super().__init__(coordinator)
        self._megad: MegaD = coordinator.megad
        self._port = port
        self._sensor_name: str = port.conf.name
        self._unique_id: str = unique_id
        
        # ✅ Правильный device_info - ссылается на контроллер
        self._attr_device_info = coordinator.entity_device_info(
            port.conf.name,
            f"MegaD-{self._megad.id} Sensor"
        )
        
        # Получаем domain из конфигурации порта или используем по умолчанию
        self._domain = getattr(port.conf, 'domain', 'sensor')
        
        # ❌ НЕ создаем entity_id вручную - HA сделает это автоматически

    def __repr__(self) -> str:
        if not self.hass:
            return f"<Sensor entity {self.entity_id}>"
        return super().__repr__()

    @cached_property
    def name(self) -> str:
        return self._sensor_name

    @cached_property
    def unique_id(self) -> str:
        return self._unique_id

    @property
    def native_value(self) -> str:
        """Возвращает состояние сенсора"""
        return self._port.state

    @cached_property
    def domain(self) -> str:
        """Возвращает пространство имен сущности"""
        return self._domain


class ReaderSensorMegaD(StringSensorMegaD):

    _attr_icon = 'mdi:lock-smart'
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["detected", "idle"]


class ClickSensorMegaD(CoordinatorEntity, SensorEntity):
    """Сенсор кнопки с поддержкой всех типов нажатий"""
    
    _attr_icon = 'mdi:gesture-tap-button'
    _attr_device_class = SensorDeviceClass.ENUM

    def __init__(
            self, 
            coordinator: MegaDCoordinator, 
            port: BinaryPortClick,
            unique_id: str
    ) -> None:
        super().__init__(coordinator)
        self._megad: MegaD = coordinator.megad
        self._port = port
        self._unique_id: str = unique_id
        
        # ✅ ДЛЯ PORT CLICK: СОЗДАЕМ УНИКАЛЬНОЕ DEVICE_INFO
        # Каждая кнопка с режимом C получает собственное устройство
        entity_name = f'{port.conf.name} Click'
        entity_model = f"MegaD-{self._megad.id} Click Sensor"
        
        # Уникальный идентификатор для device на основе порта
        device_unique_id = f"{self._megad.id}-port{port.conf.id}-click"
        
        self._attr_device_info = coordinator.entity_device_info(
            entity_name,
            entity_model,
            entity_type="click_sensor",
            port_id=port.conf.id,
            extra_port_id=device_unique_id  # ✅ УНИКАЛЬНЫЙ ДЛЯ КАЖДОЙ КНОПКИ
        )
        
        # Получаем domain из конфигурации порта или используем по умолчанию
        self._domain = getattr(port.conf, 'domain', 'sensor')
        
        # ❌ НЕ создаем entity_id вручную - HA сделает это автоматически
        
        # Текущее состояние нажатия
        self._current_click_state = CLICK_STATE_NONE
        
        # Определяем допустимые значения для enum
        self._attr_options = ['off', 'single', 'double', 'long']
    def __repr__(self) -> str:
        if not self.hass:
            return f"<Sensor entity {self.entity_id}>"
        return super().__repr__()

    @cached_property
    def name(self) -> str:
        return f'{self._port.conf.name} Click'

    @cached_property
    def unique_id(self) -> str:
        return self._unique_id

    @cached_property
    def capability_attributes(self):
        return {
            "options": self._attr_options
        }

    @property
    def native_value(self) -> str:
        """Возвращает текущее состояние нажатия"""
        # Получаем текущее состояние порта
        port_state = self._port.state
        
        # Определяем тип нажатия на основе состояния порта
        if port_state == STATE_BUTTON.SINGLE:
            return 'single'
        elif port_state == STATE_BUTTON.DOUBLE:
            return 'double'
        elif port_state == STATE_BUTTON.LONG:
            return 'long'
        else:
            return 'off'

    @cached_property
    def extra_state_attributes(self):
        """Дополнительные атрибуты с детальной информацией о нажатиях"""
        port_state = self._port.state
        
        # Определяем активные типы нажатий
        single_active = port_state == STATE_BUTTON.SINGLE
        double_active = port_state == STATE_BUTTON.DOUBLE
        long_active = port_state == STATE_BUTTON.LONG
        
        return {
            'port_id': self._port.conf.id,
            'device_id': self._megad.id,
            'supported_click_types': self._attr_options,
            'domain': self._domain,
            # Детальная информация о каждом типе нажатия
            'single_click_active': single_active,
            'double_click_active': double_active,
            'long_click_active': long_active,
            # Счетчики нажатий (если доступны)
            'single_click_count': getattr(self._port, 'single_click_count', 0),
            'double_click_count': getattr(self._port, 'double_click_count', 0),
            'long_click_count': getattr(self._port, 'long_click_count', 0),
            # Общий счетчик
            'total_clicks': getattr(self._port, 'count', 0),
            # Текущее сырое состояние порта
            'raw_state': port_state,
        }

    @cached_property
    def domain(self) -> str:
        """Возвращает пространство имен сущности"""
        return self._domain


class SensorMegaD(CoordinatorEntity, SensorEntity):
    def __init__(
            self, coordinator: MegaDCoordinator, port: DigitalSensorBase,
            unique_id: str, type_sensor: str, prefix: str = '',
            device_suffix: str = None  # ✅ НОВЫЙ ПАРАМЕТР
    ) -> None:
        super().__init__(coordinator)
        self._megad: MegaD = coordinator.megad
        self._port: DigitalSensorBase = port
        self.type_sensor = type_sensor
        self._sensor_name: str = (f'{port.conf.name} '
                                  f'{TYPE_SENSOR_RUS[type_sensor]}{prefix}')
        self._unique_id: str = unique_id
        self._domain = getattr(port.conf, 'domain', 'sensor')
        
        # ✅ КРИТИЧЕСКОЕ ИСПРАВЛЕНИЕ ДЛЯ I2C ДАТЧИКОВ:
        # Каждая физическая величина I2C датчика получает уникальное device_info
        if device_suffix and isinstance(port, (DHTSensorPort, I2CSensorSTH31, 
                                               I2CSensorHTUxxD, I2CSensorSCD4x,
                                               I2CSensorMBx280, I2CSensorBMP180,
                                               I2CSensorINA226, I2CSensorBH1750,
                                               I2CSensorT67xx, I2CSensorPT)):
            # Для I2C датчиков создаем отдельные устройства для каждой величины
            entity_model = f"MegaD-{self._megad.id} {type_sensor} Sensor"
            if prefix:
                entity_model = f"{entity_model} ({prefix})"
            
            # Уникальный идентификатор устройства на основе порта и типа сенсора
            device_unique_id = f"{coordinator.megad.id}-port{port.conf.id}-{type_sensor}"
            if prefix:
                device_unique_id = f"{device_unique_id}-{prefix}"
                
            self._attr_device_info = coordinator.entity_device_info(
                self._sensor_name,
                entity_model,
                entity_type=f"i2c_{type_sensor.lower()}",
                port_id=port.conf.id,
                extra_port_id=device_unique_id  # ✅ УНИКАЛЬНЫЙ ДЛЯ КАЖДОЙ ВЕЛИЧИНЫ
            )
        else:
            # Для других сенсоров стандартный подход
            self._attr_device_info = coordinator.entity_device_info(
                self._sensor_name,
                f"MegaD-{self._megad.id} Sensor",
                entity_type="sensor",
                port_id=port.conf.id
            )
        
        # ❌ НЕ создаем entity_id вручную - HA сделает это автоматически
        
        self.last_value: None | int | float = None
        self.info_filter()

    def __repr__(self) -> str:
        if not self.hass:
            return f"<Sensor entity {self.entity_id}>"
        return super().__repr__()

    def info_filter(self):
        """Вывод информации в лог при включенной фильтрации сенсора."""
        if self._port.conf.filter:
            _LOGGER.info(f'Включена фильтрация значения у сенсора '
                         f'{self.entity_id}')

    def general_filter(self, min_value, max_value, value, value_jump):
        """Общий фильтр значений сенсоров."""
        if value < min_value: value = min_value
        elif value > max_value: value = max_value
        if self.last_value is not None:
            if value in (min_value, max_value) and (
                    abs(value - self.last_value) > value_jump):
                return self.last_value
            elif value == 0 and abs(self.last_value) > value_jump:
                return self.last_value
        self.last_value = value
        return value

    def filter_temperature(self, value):
        """Фильтр для значений температуры."""
        min_value, max_value = TEMPERATURE_CONDITION[
            self._port.conf.device_class
        ]
        min_value -= DEVIATION_TEMPERATURE
        max_value += DEVIATION_TEMPERATURE
        return self.general_filter(
            min_value, max_value, value, ALLOWED_TEMP_JUMP
        )

    def filter_humidity(self, value):
        """Фильтр для значений влажности."""
        min_value, max_value = 0, 100
        return self.general_filter(
            min_value, max_value, value, ALLOWED_HUM_JUMP
        )

    def filter_bad_value(self, value):
        """Фильтрация неадекватных значений сенсоров."""
        if self._port.conf.filter:
            try:
                value = float(value)
            except (ValueError, TypeError):
                pass
            if not isinstance(value, float):
                return self.last_value
            match self.type_sensor:
                case TYPE_SENSOR.TEMPERATURE:
                    return self.filter_temperature(value)
                case TYPE_SENSOR.HUMIDITY:
                    return self.filter_humidity(value)
                case _:
                    self.last_value = value
                    return value
        else:
            return value

    @cached_property
    def name(self) -> str:
        return self._sensor_name

    @cached_property
    def unique_id(self) -> str:
        return self._unique_id

    @cached_property
    def state_class(self) -> SensorStateClass | str | None:
        """Return the state class of this entity, if any."""
        return SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> float | str:
        """Возвращает состояние сенсора"""
        value = self._port.state.get(self.type_sensor)
        return self.filter_bad_value(value)

    @cached_property
    def native_unit_of_measurement(self) -> str | None:
        """Возвращает единицу измерения сенсора"""
        return SENSOR_UNIT.get(self.type_sensor)

    @cached_property
    def device_class(self) -> str | None:
        return SENSOR_CLASS.get(self.type_sensor)

    @cached_property
    def domain(self) -> str:
        """Возвращает пространство имен сущности"""
        return self._domain


class SensorBusMegaD(SensorMegaD):

    def __init__(
            self, coordinator: MegaDCoordinator, port: DigitalSensorBase,
            unique_id: str, type_sensor: str, id_one_wire: str
    ) -> None:
        super().__init__(coordinator, port, unique_id, type_sensor)
        self.id_one_wire = id_one_wire
        self._sensor_name: str = f'{port.conf.name}_{id_one_wire}'
        self._unique_id: str = unique_id

    @property
    def native_value(self) -> float | str:
        """Возвращает состояние сенсора"""
        value = self._port.state.get(self.id_one_wire)
        return self.filter_bad_value(value)


class SensorDeviceMegaD(CoordinatorEntity, SensorEntity):

    def __init__(
            self, coordinator: MegaDCoordinator, unique_id: str,
            type_sensor: str
    ) -> None:
        super().__init__(coordinator)
        self._megad: MegaD = coordinator.megad
        self.type_sensor = type_sensor
        self._sensor_name: str = f'megad_{self._megad.id}_{type_sensor}'
        self._unique_id: str = unique_id
        self._domain = 'sensor'  # Для device сенсоров используем sensor по умолчанию
        
        # ✅ Правильный device_info - ссылается на контроллер
        self._attr_device_info = coordinator.entity_device_info(
            self._sensor_name,
            f"MegaD-{self._megad.id} Device Sensor"
        )
        
        # ❌ НЕ создаем entity_id вручную - HA сделает это автоматически

    def __repr__(self) -> str:
        if not self.hass:
            return f"<Sensor entity {self.entity_id}>"
        return super().__repr__()

    @cached_property
    def name(self) -> str:
        return self._sensor_name

    @cached_property
    def unique_id(self) -> str:
        return self._unique_id

    @cached_property
    def state_class(self) -> SensorStateClass | str | None:
        """Return the state class of this entity, if any."""
        return SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> float | str:
        """Возвращает состояние сенсора"""
        if self.type_sensor == TEMPERATURE:
            return self._megad.temperature
        elif self.type_sensor == UPTIME:
            return self._megad.uptime

    @cached_property
    def native_unit_of_measurement(self) -> str | None:
        """Возвращает единицу измерения сенсора"""
        return SENSOR_UNIT.get(self.type_sensor)

    @cached_property
    def device_class(self) -> str | None:
        return SENSOR_CLASS.get(self.type_sensor)

    @cached_property
    def domain(self) -> str:
        """Возвращает пространство имен сущности"""
        return self._domain


class AnalogSensorMegaD(CoordinatorEntity, SensorEntity):

    _attr_icon = 'mdi:alpha-a-circle-outline'

    def __init__(
            self, coordinator: MegaDCoordinator, port: AnalogSensor,
            unique_id: str, type_sensor: str | None = None
    ) -> None:
        super().__init__(coordinator)
        self._megad: MegaD = coordinator.megad
        self._port: AnalogSensor = port
        self.type_sensor = type_sensor
        self._sensor_name: str = port.conf.name
        self._unique_id: str = unique_id
        self._domain = getattr(port.conf, 'domain', 'sensor')
        
        # ✅ Правильный device_info - ссылается на контроллер
        self._attr_device_info = coordinator.entity_device_info(
            port.conf.name,
            f"MegaD-{self._megad.id} Analog Sensor"
        )
        
        # ❌ НЕ создаем entity_id вручную - HA сделает это автоматически

    def __repr__(self) -> str:
        if not self.hass:
            return f"<Sensor entity {self.entity_id}>"
        return super().__repr__()

    @cached_property
    def name(self) -> str:
        return self._sensor_name

    @cached_property
    def unique_id(self) -> str:
        return self._unique_id

    @cached_property
    def state_class(self) -> SensorStateClass | str | None:
        """Return the state class of this entity, if any."""
        return SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> float | str:
        """Возвращает состояние сенсора"""
        return self._port.state

    @cached_property
    def domain(self) -> str:
        """Возвращает пространство имен сущности"""
        return self._domain


class PIDSensorMegaD(CoordinatorEntity, SensorEntity):

    _attr_icon = 'mdi:information-outline'

    def __init__(
            self, coordinator: MegaDCoordinator, pid: PIDControl,
            unique_id: str, type_sensor: str | None = None
    ) -> None:
        super().__init__(coordinator)
        self._megad: MegaD = coordinator.megad
        self._pid: PIDControl = pid
        self.type_sensor = type_sensor
        self._domain = getattr(pid.conf, 'domain', 'sensor')
        self._attr_name = f'{self._megad.id}_{pid.conf.id}_pid_value'
        self._attr_unique_id = unique_id
        
        # ✅ Правильный device_info - ссылается на контроллер
        self._attr_device_info = coordinator.entity_device_info(
            self._attr_name,
            f"MegaD-{self._megad.id} PID Sensor"
        )
        
        # ❌ НЕ создаем entity_id вручную - HA сделает это автоматически

    def __repr__(self) -> str:
        if not self.hass:
            return f"<Sensor entity {self.entity_id}>"
        return super().__repr__()

    @cached_property
    def state_class(self) -> SensorStateClass | str | None:
        """Return the state class of this entity, if any."""
        return SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> int:
        """Возвращает состояние сенсора"""
        if self._pid.value is not None:
            return int(self._pid.value)
        else:
            return 0

    @cached_property
    def extra_state_attributes(self):
        """Дополнительные атрибуты сенсора."""
        return {
            'min_value': -32767,
            'max_value': 32767,
            'domain': self._domain
        }

    @cached_property
    def domain(self) -> str:
        """Возвращает пространство имен сущности"""
        return self._domain
    
class WatchdogStatusSensor(CoordinatorEntity, SensorEntity):
    """Сенсор статуса watchdog."""
    
    _attr_icon = 'mdi:heart-pulse'
    _attr_device_class = SensorDeviceClass.ENUM
    
    def __init__(self, coordinator: MegaDCoordinator, unique_id: str):
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._attr_unique_id = unique_id
        self._attr_name = f"MegaD {coordinator.megad.id} Watchdog Status"
        
        self._attr_device_info = coordinator.entity_device_info(
            self._attr_name,
            f"MegaD-{coordinator.megad.id} Watchdog"
        )
        
        self._attr_options = ["ok", "warning", "error", "inactive", "recovering"]

    @property
    def native_value(self) -> str:
        """Возвращает статус watchdog как простую строку."""
        if not hasattr(self._coordinator, 'watchdog') or not self._coordinator.watchdog:
            return "inactive"
        
        watchdog = self._coordinator.watchdog
        status = watchdog.get_status()
        
        if not watchdog._is_running:
            return "inactive"
        elif watchdog._recovering:
            return "recovering"
        elif not getattr(self._coordinator.megad, 'is_available', True):
            return "error"
        elif watchdog._failure_count > 0:
            return "warning"
        else:
            return "ok"

    @property
    def extra_state_attributes(self):
        """Дополнительные атрибуты - полная информация в JSON."""
        if not hasattr(self._coordinator, 'watchdog') or not self._coordinator.watchdog:
            return {}
        
        return self._coordinator.watchdog.get_status()

class WatchdogInactivitySensor(CoordinatorEntity, SensorEntity):
    """Сенсор времени без данных."""
    
    _attr_icon = 'mdi:timer-outline'
    _attr_device_class = SensorDeviceClass.DURATION
    
    def __init__(self, coordinator: MegaDCoordinator, unique_id: str):
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._attr_unique_id = unique_id
        self._attr_name = f"MegaD {coordinator.megad.id} Watchdog Inactivity"
        
        self._attr_device_info = coordinator.entity_device_info(
            self._attr_name,
            f"MegaD-{coordinator.megad.id} Watchdog"
        )

    @property
    def native_value(self) -> int:
        """Возвращает время без данных в секундах."""
        if not hasattr(self._coordinator, 'watchdog') or not self._coordinator.watchdog:
            return 0
        
        return self._coordinator.watchdog.get_inactivity_seconds()

    @property
    def native_unit_of_measurement(self) -> str:
        return "s"


class WatchdogFeedbackStatusSensor(CoordinatorEntity, SensorEntity):
    """Сенсор статуса обратной связи."""
    
    _attr_icon = 'mdi:comment-check-outline'
    _attr_device_class = SensorDeviceClass.ENUM
    
    def __init__(self, coordinator: MegaDCoordinator, unique_id: str):
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._attr_unique_id = unique_id
        self._attr_name = f"MegaD {coordinator.megad.id} Feedback Status"
        
        self._attr_device_info = coordinator.entity_device_info(
            self._attr_name,
            f"MegaD-{coordinator.megad.id} Watchdog"
        )
        
        self._attr_options = ["ok", "waiting", "failed", "inactive"]

    @property
    def native_value(self) -> str:
        """Возвращает статус обратной связи как простую строку."""
        if not hasattr(self._coordinator, 'watchdog') or not self._coordinator.watchdog:
            return "inactive"
        
        return self._coordinator.watchdog.get_feedback_status()

    @property
    def extra_state_attributes(self):
        """Дополнительные атрибуты."""
        if not hasattr(self._coordinator, 'watchdog') or not self._coordinator.watchdog:
            return {}
        
        return self._coordinator.watchdog.get_feedback_details()


class WatchdogFeedbackInactivitySensor(CoordinatorEntity, SensorEntity):
    """Сенсор времени без обратной связи."""
    
    _attr_icon = 'mdi:timer-sand'
    _attr_device_class = SensorDeviceClass.DURATION
    
    def __init__(self, coordinator: MegaDCoordinator, unique_id: str):
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._attr_unique_id = unique_id
        self._attr_name = f"MegaD {coordinator.megad.id} Feedback Inactivity"
        
        self._attr_device_info = coordinator.entity_device_info(
            self._attr_name,
            f"MegaD-{coordinator.megad.id} Watchdog"
        )

    @property
    def native_value(self) -> int:
        """Возвращает время без обратной связи в секундах."""
        if not hasattr(self._coordinator, 'watchdog') or not self._coordinator.watchdog:
            return 0
        
        return self._coordinator.watchdog.get_feedback_inactivity_seconds()

    @property
    def native_unit_of_measurement(self) -> str:
        return "s"

class MegaDWatchdogSensor(CoordinatorEntity, SensorEntity):
    """Сенсор статуса watchdog."""
    
    def __init__(self, coordinator, megad_id):
        super().__init__(coordinator)
        self._megad_id = megad_id
        self._attr_name = f"MegaD-{megad_id} Status"
        self._attr_unique_id = f"megad_{megad_id}_watchdog_status"
        self._attr_device_class = SensorDeviceClass.ENUM
        self._attr_options = ["online", "offline", "warning"]
        
    @property
    def device_info(self):
        return self.coordinator.device_base_info()
    
    @property
    def state(self):
        if not self.coordinator.megad.is_available:
            return "offline"
        
        if self.coordinator.watchdog:
            status = self.coordinator.watchdog.get_status()
            if status.get('is_active', False):
                return "online"
            elif status.get('show_warning', False):
                return "warning"
        
        return "offline"