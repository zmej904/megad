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
    
    # ✅ Watchdog сенсоры - ПОЛНЫЙ НАБОР
    # 1. Статус watchdog
    unique_id_watchdog_status = f'{entry_id}-{megad.id}-watchdog-status'
    sensors.append(WatchdogStatusSensor(coordinator, unique_id_watchdog_status))
    
    # 2. Время без данных
    unique_id_watchdog_inactivity = f'{entry_id}-{megad.id}-watchdog-inactivity'
    sensors.append(WatchdogInactivitySensor(coordinator, unique_id_watchdog_inactivity))
    
    # 3. Статус обратной связи
    unique_id_feedback_status = f'{entry_id}-{megad.id}-feedback-status'
    sensors.append(WatchdogFeedbackStatusSensor(coordinator, unique_id_feedback_status))
    
    # 4. Время без обратной связи
    unique_id_feedback_inactivity = f'{entry_id}-{megad.id}-feedback-inactivity'
    sensors.append(WatchdogFeedbackInactivitySensor(coordinator, unique_id_feedback_inactivity))
    
    # 5. Счетчик неудач
    unique_id_failure_count = f'{entry_id}-{megad.id}-watchdog-failure-count'
    sensors.append(WatchdogFailureCountSensor(coordinator, unique_id_failure_count))
    
    # 6. Счетчик попыток восстановления
    unique_id_restore_attempts = f'{entry_id}-{megad.id}-watchdog-restore-attempts'
    sensors.append(WatchdogRestoreAttemptsSensor(coordinator, unique_id_restore_attempts))
    
    # 7. Статус соединения
    unique_id_connection_status = f'{entry_id}-{megad.id}-watchdog-connection-status'
    sensors.append(WatchdogConnectionStatusSensor(coordinator, unique_id_connection_status))
    
    # 8. Общий статус обратной связи (текстовый)
    unique_id_feedback_text = f'{entry_id}-{megad.id}-feedback-text'
    sensors.append(WatchdogFeedbackTextSensor(coordinator, unique_id_feedback_text))

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
    
    _attr_icon = 'mdi:dog'
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
    _attr_state_class = SensorStateClass.MEASUREMENT
    
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
        
        # Фиксированные опции, соответствующие методу get_feedback_status()
        self._attr_options = ["active", "inactive", "problem", "recovering"]

    @property
    def native_value(self) -> str:
        """Возвращает статус обратной связи как простую строку."""
        if not hasattr(self._coordinator, 'watchdog') or not self._coordinator.watchdog:
            return "inactive"
        
        watchdog = self._coordinator.watchdog
        status_text = watchdog.get_feedback_status()
        
        # Преобразуем текстовый статус в одно из фиксированных значений
        if watchdog._recovering:
            return "recovering"
        elif "работает" in status_text.lower():
            return "active"
        elif "проблема" in status_text.lower() or "критическая" in status_text.lower():
            return "problem"
        else:
            return "inactive"

    @property
    def extra_state_attributes(self):
        """Дополнительные атрибуты."""
        if not hasattr(self._coordinator, 'watchdog') or not self._coordinator.watchdog:
            return {}
        
        watchdog = self._coordinator.watchdog
        return {
            "text_status": watchdog.get_feedback_status(),
            "inactivity_seconds": watchdog.get_feedback_inactivity_seconds(),
            "inactivity_minutes": watchdog.get_feedback_inactivity_seconds() // 60,
            "megad_id": self._coordinator.megad.id,
            "is_running": watchdog._is_running,
            "is_recovering": watchdog._recovering
        }


class WatchdogFeedbackInactivitySensor(CoordinatorEntity, SensorEntity):
    """Сенсор времени без обратной связи."""
    
    _attr_icon = 'mdi:timer-sand'
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    
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
    
    @property
    def extra_state_attributes(self):
        """Дополнительные атрибуты."""
        if not hasattr(self._coordinator, 'watchdog') or not self._coordinator.watchdog:
            return {}
        
        inactivity_seconds = self._coordinator.watchdog.get_feedback_inactivity_seconds()
        minutes = inactivity_seconds // 60
        seconds = inactivity_seconds % 60
        
        return {
            "display_value": f"{minutes} мин {seconds} сек",
            "minutes": minutes,
            "seconds": seconds,
            "timeout_minutes": self._coordinator.watchdog._feedback_timeout // 60,
            "is_critical": inactivity_seconds > self._coordinator.watchdog._feedback_timeout
        }


class WatchdogFailureCountSensor(CoordinatorEntity, SensorEntity):
    """Сенсор счетчика неудач watchdog."""
    
    _attr_icon = 'mdi:alert-circle'
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    
    def __init__(self, coordinator: MegaDCoordinator, unique_id: str):
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._attr_unique_id = unique_id
        self._attr_name = f"MegaD {coordinator.megad.id} Watchdog Failures"
        
        self._attr_device_info = coordinator.entity_device_info(
            self._attr_name,
            f"MegaD-{coordinator.megad.id} Watchdog"
        )

    @property
    def native_value(self) -> int:
        """Возвращает счетчик неудач."""
        if not hasattr(self._coordinator, 'watchdog') or not self._coordinator.watchdog:
            return 0
        
        return self._coordinator.watchdog._failure_count
    
    @property
    def extra_state_attributes(self):
        """Дополнительные атрибуты."""
        if not hasattr(self._coordinator, 'watchdog') or not self._coordinator.watchdog:
            return {}
        
        watchdog = self._coordinator.watchdog
        return {
            "max_failures": watchdog._max_failures,
            "threshold_reached": watchdog._failure_count >= watchdog._max_failures,
            "remaining_attempts": max(0, watchdog._max_failures - watchdog._failure_count)
        }


class WatchdogRestoreAttemptsSensor(CoordinatorEntity, SensorEntity):
    """Сенсор счетчика попыток восстановления."""
    
    _attr_icon = 'mdi:refresh'
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    
    def __init__(self, coordinator: MegaDCoordinator, unique_id: str):
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._attr_unique_id = unique_id
        self._attr_name = f"MegaD {coordinator.megad.id} Restore Attempts"
        
        self._attr_device_info = coordinator.entity_device_info(
            self._attr_name,
            f"MegaD-{coordinator.megad.id} Watchdog"
        )

    @property
    def native_value(self) -> int:
        """Возвращает счетчик попыток восстановления."""
        if not hasattr(self._coordinator, 'watchdog') or not self._coordinator.watchdog:
            return 0
        
        return self._coordinator.watchdog._feedback_restore_attempts
    
    @property
    def extra_state_attributes(self):
        """Дополнительные атрибуты."""
        if not hasattr(self._coordinator, 'watchdog') or not self._coordinator.watchdog:
            return {}
        
        watchdog = self._coordinator.watchdog
        return {
            "max_restore_attempts": watchdog._max_feedback_restore_attempts,
            "threshold_reached": watchdog._feedback_restore_attempts >= watchdog._max_feedback_restore_attempts,
            "remaining_attempts": max(0, watchdog._max_feedback_restore_attempts - watchdog._feedback_restore_attempts)
        }


class WatchdogConnectionStatusSensor(CoordinatorEntity, SensorEntity):
    """Сенсор статуса соединения с контроллером."""
    
    _attr_icon = 'mdi:network'
    _attr_device_class = SensorDeviceClass.ENUM
    
    def __init__(self, coordinator: MegaDCoordinator, unique_id: str):
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._attr_unique_id = unique_id
        self._attr_name = f"MegaD {coordinator.megad.id} Connection Status"
        
        self._attr_device_info = coordinator.entity_device_info(
            self._attr_name,
            f"MegaD-{coordinator.megad.id} Watchdog"
        )
        
        self._attr_options = ["online", "offline", "checking"]
        
        # Кэширование для проверки соединения
        self._last_check = None
        self._cached_status = "checking"
        self._check_in_progress = False

    @property
    def native_value(self) -> str:
        """Возвращает статус соединения."""
        if not hasattr(self._coordinator, 'watchdog') or not self._coordinator.watchdog:
            return "offline"
        
        # Если проверка не запущена и прошло больше 60 секунд с последней проверки
        if not self._check_in_progress and (not self._last_check or 
                                          (datetime.now() - self._last_check).seconds > 60):
            self._schedule_health_check()
        
        return self._cached_status
    
    def _schedule_health_check(self):
        """Запланировать проверку здоровья контроллера."""
        import asyncio
        
        self._check_in_progress = True
        
        async def check_and_update():
            try:
                watchdog = self._coordinator.watchdog
                if watchdog:
                    is_healthy = await watchdog.check_megad_health()
                    self._cached_status = "online" if is_healthy else "offline"
                else:
                    self._cached_status = "offline"
            except Exception:
                self._cached_status = "offline"
            finally:
                self._last_check = datetime.now()
                self._check_in_progress = False
                self.async_write_ha_state()
        
        # Запускаем в фоне
        asyncio.create_task(check_and_update())
    
    @property
    def extra_state_attributes(self):
        """Дополнительные атрибуты."""
        if not hasattr(self._coordinator, 'watchdog') or not self._coordinator.watchdog:
            return {}
        
        watchdog = self._coordinator.watchdog
        return {
            "ip_address": str(watchdog.megad.config.plc.ip_megad) if hasattr(watchdog.megad.config.plc, 'ip_megad') else "unknown",
            "last_check": self._last_check.isoformat() if self._last_check else None,
            "check_in_progress": self._check_in_progress
        }


class WatchdogFeedbackTextSensor(CoordinatorEntity, SensorEntity):
    """Текстовый сенсор статуса обратной связи (человекочитаемый)."""
    
    _attr_icon = 'mdi:message-text'
    
    def __init__(self, coordinator: MegaDCoordinator, unique_id: str):
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._attr_unique_id = unique_id
        self._attr_name = f"MegaD {coordinator.megad.id} Feedback"
        
        self._attr_device_info = coordinator.entity_device_info(
            self._attr_name,
            f"MegaD-{coordinator.megad.id} Watchdog"
        )

    @property
    def native_value(self) -> str:
        """Возвращает текстовый статус обратной связи."""
        if not hasattr(self._coordinator, 'watchdog') or not self._coordinator.watchdog:
            return "Watchdog не запущен"
        
        return self._coordinator.watchdog.get_feedback_status()
    
    @property
    def extra_state_attributes(self):
        """Дополнительные атрибуты."""
        if not hasattr(self._coordinator, 'watchdog') or not self._coordinator.watchdog:
            return {}
        
        watchdog = self._coordinator.watchdog
        status = watchdog.get_status()
        
        return {
            "last_feedback": status.get('last_feedback'),
            "last_data": status.get('last_data'),
            "megad_ip": status.get('megad_ip'),
            "is_active": status.get('is_active'),
            "show_warning": status.get('show_warning')
        }


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
    
class WatchdogDiagnosticSensor(CoordinatorEntity, SensorEntity):
    """Диагностический сенсор для watchdog."""
    
    _attr_icon = 'mdi:bug'
    
    def __init__(self, coordinator: MegaDCoordinator, unique_id: str):
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._attr_unique_id = unique_id
        self._attr_name = f"MegaD {coordinator.megad.id} Watchdog Diagnostic"
        
        self._attr_device_info = coordinator.entity_device_info(
            self._attr_name,
            f"MegaD-{coordinator.megad.id} Watchdog"
        )

    @property
    def native_value(self) -> str:
        """Возвращает диагностическую информацию."""
        if not hasattr(self._coordinator, 'watchdog') or not self._coordinator.watchdog:
            return "watchdog_not_found"
        
        watchdog = self._coordinator.watchdog
        feedback_inactivity = watchdog.get_feedback_inactivity_seconds()
        general_inactivity = watchdog.get_inactivity_seconds()
        
        return f"feedback:{feedback_inactivity}s, general:{general_inactivity}s"
    
    @property
    def extra_state_attributes(self):
        """Детальная диагностика."""
        if not hasattr(self._coordinator, 'watchdog') or not self._coordinator.watchdog:
            return {"error": "watchdog_not_found"}
        
        watchdog = self._coordinator.watchdog
        return {
            "feedback_inactivity_seconds": watchdog.get_feedback_inactivity_seconds(),
            "general_inactivity_seconds": watchdog.get_inactivity_seconds(),
            "feedback_last_event": watchdog._feedback_last_event.isoformat() if watchdog._feedback_last_event else None,
            "last_data_received": watchdog._last_data_received.isoformat() if watchdog._last_data_received else None,
            "is_running": watchdog._is_running,
            "failure_count": watchdog._failure_count,
            "feedback_restore_attempts": watchdog._feedback_restore_attempts,
            "recovering": watchdog._recovering,
            "feedback_timeout": watchdog._feedback_timeout,
            "feedback_check_interval": watchdog._feedback_check_interval,
            "megad_ip": str(watchdog.megad.config.plc.ip_megad) if hasattr(watchdog.megad.config.plc, 'ip_megad') else 'unknown',
            "megad_url": getattr(watchdog.megad, 'url', 'unknown'),
            "megad_domain": getattr(watchdog.megad, 'domain', 'unknown'),
        }    