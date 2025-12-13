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
    WATCHDOG_CHECK_INTERVAL, WATCHDOG_MAX_FAILURES  # Добавлены константы watchdog
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
    sensors.append(SensorMegaD(
        coordinator, port, unique_id_temp, TEMPERATURE, prefix)
    )
    unique_id_hum = f'{entry_id}-{megad.id}-{port.conf.id}-{HUMIDITY}{prefix}'
    sensors.append(SensorMegaD(
        coordinator, port, unique_id_hum, HUMIDITY, prefix)
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
            
        # УБРАНО: НЕ создаем сущности состояния для BinaryPortIn в режимах P/R - 
        # они будут созданы как BinarySensor в binary_sensor.py
        # Для портов в режимах P, R, P&R сущности создаются в binary_sensor.py
                
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
        if isinstance(port, I2CSensorSCD4x):
            prefix = port.prefix
            unique_id_co2 = (f'{entry_id}-{megad.id}-{port.conf.id}-'
                             f'{CO2.lower()}{prefix}')
            sensors.append(SensorMegaD(
                coordinator, port, unique_id_co2, CO2, prefix)
            )
            create_temp_hum(sensors, entry_id, coordinator, megad, port)
        if isinstance(port, I2CSensorMBx280):
            prefix = port.prefix
            unique_id_press = (f'{entry_id}-{megad.id}-{port.conf.id}-'
                               f'{PRESSURE}{prefix}')
            sensors.append(SensorMegaD(
                coordinator, port, unique_id_press, PRESSURE, prefix)
            )
            create_temp_hum(sensors, entry_id, coordinator, megad, port)
        if isinstance(port, I2CSensorINA226):
            prefix = port.prefix
            unique_id_current = (f'{entry_id}-{megad.id}-{port.conf.id}-'
                                 f'{CURRENT}{prefix}')
            sensors.append(SensorMegaD(
                coordinator, port, unique_id_current, CURRENT, prefix)
            )
            unique_id_voltage = (f'{entry_id}-{megad.id}-{port.conf.id}-'
                                 f'{VOLTAGE}{prefix}')
            sensors.append(SensorMegaD(
                coordinator, port, unique_id_voltage, VOLTAGE, prefix)
            )
            unique_id_raw = (f'{entry_id}-{megad.id}-{port.conf.id}-'
                             f'{RAW_VALUE}{prefix}')
            sensors.append(SensorMegaD(
                coordinator, port, unique_id_raw, RAW_VALUE, prefix)
            )
        if isinstance(port, I2CSensorILLUM):
            prefix = port.prefix
            unique_id = (f'{entry_id}-{megad.id}-{port.conf.id}-'
                         f'{LUXURY}{prefix}')
            sensors.append(SensorMegaD(
                coordinator, port, unique_id, LUXURY, prefix)
            )
        if isinstance(port, I2CSensorT67xx):
            prefix = port.prefix
            unique_id = (f'{entry_id}-{megad.id}-{port.conf.id}-'
                         f'{CO2}{prefix}')
            sensors.append(SensorMegaD(
                coordinator, port, unique_id, CO2, prefix)
            )
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
        if isinstance(port, I2CSensorPT):
            prefix = port.prefix
            unique_id = (f'{entry_id}-{megad.id}-{port.conf.id}-'
                         f'{BAR}{prefix}')
            sensors.append(SensorMegaD(
                coordinator, port, unique_id, BAR, prefix)
            )
        if isinstance(port, AnalogSensor):
            unique_id = f'{entry_id}-{megad.id}-{port.conf.id}-analog'
            sensors.append(AnalogSensorMegaD(coordinator, port, unique_id))

    sensors.append(SensorDeviceMegaD(
        coordinator, f'{entry_id}-{megad.id}-{TEMPERATURE}', TEMPERATURE)
    )
    sensors.append(SensorDeviceMegaD(
        coordinator, f'{entry_id}-{megad.id}-{UPTIME}', UPTIME)
    )
    for pid in megad.pids:
        unique_id = f'{entry_id}-{megad.id}-{pid.conf.id}-pid-value'
        sensors.append(PIDSensorMegaD(coordinator, pid, unique_id))
    
    # Добавляем сенсор статуса watchdog
    unique_id_watchdog = f'{entry_id}-{megad.id}-watchdog-status'
    sensors.append(WatchdogStatusSensor(coordinator, unique_id_watchdog))
    
    # Добавляем сенсор истории восстановлений watchdog
    unique_id_recovery_history = f'{entry_id}-{megad.id}-watchdog-recovery-history'
    sensors.append(WatchdogRecoveryHistorySensor(coordinator, unique_id_recovery_history))

    for sensor in sensors:
        hass.data[DOMAIN][CURRENT_ENTITY_IDS][entry_id].append(
            sensor.unique_id)
    if sensors:
        async_add_entities(sensors)
        _LOGGER.debug(f'Добавлены сенсоры: {sensors}')


class WatchdogStatusSensor(CoordinatorEntity, SensorEntity):
    """Сенсор статуса watchdog для MegaD."""
    
    _attr_icon = "mdi:heart-pulse"
    _attr_device_class = SensorDeviceClass.ENUM

    def __init__(self, coordinator: MegaDCoordinator, unique_id: str):
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._megad = coordinator.megad
        self._unique_id = unique_id
        self._attr_name = f"MegaD-{coordinator.megad.id} Watchdog Status"
        self._attr_device_info = coordinator.devices_info()
        self.entity_id = f'sensor.{coordinator.megad.id}_watchdog_status'
        
        # Определяем допустимые значения для enum
        self._attr_options = [
            "not_initialized",
            "stopped", 
            "online",
            "degraded",
            "recovering",
            "error",
            "unavailable"
        ]
        
        # Для отслеживания изменений статуса
        self._last_status = None

    def __repr__(self) -> str:
        if not self.hass:
            return f"<Sensor entity {self.entity_id}>"
        return super().__repr__()

    @cached_property
    def name(self) -> str:
        return self._attr_name

    @cached_property
    def unique_id(self) -> str:
        return self._unique_id

    @property
    def native_value(self) -> str:
        """Возвращает текущий статус watchdog."""
        # Если координатор не инициализирован
        if not self._coordinator:
            return "not_initialized"
        
        # Если watchdog не создан
        if not hasattr(self._coordinator, 'watchdog') or not self._coordinator.watchdog:
            return "not_initialized"
        
        try:
            # Получаем статус watchdog
            status = self._get_watchdog_status()
            
            # Определяем текстовое состояние на основе полученных данных
            if not status["is_running"]:
                return "stopped"
            elif status.get("is_recovering", False):
                return "recovering"
            elif status.get("failure_count", 0) == 0:
                return "online"
            elif status.get("failure_count", 0) < status.get("max_failures", WATCHDOG_MAX_FAILURES):
                return "degraded"
            elif status.get("failure_count", 0) >= status.get("max_failures", WATCHDOG_MAX_FAILURES):
                return "recovering"
            elif not status.get("is_available", False):
                return "unavailable"
            else:
                return "error"
                
        except Exception as e:
            _LOGGER.debug(f"Ошибка при определении статуса watchdog: {e}")
            return "error"

    @cached_property
    def extra_state_attributes(self):
        """Дополнительные атрибуты сенсора."""
        # Базовые атрибуты
        attributes = {
            "megad_id": self._megad.id,
            "watchdog_type": "enhanced",
            "check_interval_seconds": WATCHDOG_CHECK_INTERVAL,
            "max_failures": WATCHDOG_MAX_FAILURES
        }
        
        # Добавляем IP адрес если доступен
        if hasattr(self._megad, 'config') and hasattr(self._megad.config, 'plc'):
            attributes["megad_ip"] = str(self._megad.config.plc.ip_megad)
        else:
            attributes["megad_ip"] = "unknown"
        
        # Если координатор не инициализирован
        if not self._coordinator:
            attributes.update({
                "status": "not_initialized",
                "message": "Координатор не инициализирован",
                "recommended_action": "Перезагрузить интеграцию"
            })
            return attributes
        
        # Если watchdog не создан
        if not hasattr(self._coordinator, 'watchdog') or not self._coordinator.watchdog:
            attributes.update({
                "status": "not_initialized",
                "message": "Watchdog не инициализирован",
                "recommended_action": "Дождаться инициализации или перезагрузить интеграцию"
            })
            return attributes
        
        try:
            # Получаем детальный статус
            status = self._get_watchdog_status()
            
            # Добавляем все поля из статуса
            for key, value in status.items():
                if value is not None:
                    attributes[key] = value
            
            # Определяем дополнительные атрибуты в зависимости от статуса
            current_status = self.native_value
            
            if current_status == "online":
                attributes.update({
                    "message": "Стабильное соединение с контроллером",
                    "recommended_action": "Мониторинг не требуется",
                    "health_score": 100
                })
            elif current_status == "degraded":
                failure_count = status.get("failure_count", 0)
                max_failures = status.get("max_failures", WATCHDOG_MAX_FAILURES)
                attributes.update({
                    "message": f"Частичная деградация: {failure_count}/{max_failures} ошибок",
                    "recommended_action": "Мониторинг состояния, возможно временные сетевые проблемы",
                    "health_score": max(0, 100 - (failure_count * 100 // max_failures))
                })
            elif current_status == "recovering":
                attributes.update({
                    "message": "Выполняется процедура восстановления",
                    "recommended_action": "Ожидание завершения восстановления",
                    "health_score": 30,
                    "recovery_stage": "active"
                })
            elif current_status == "unavailable":
                attributes.update({
                    "message": "Контроллер недоступен",
                    "recommended_action": "Проверить питание и сетевое подключение",
                    "health_score": 0,
                    "last_success": status.get("last_success", "never")
                })
            elif current_status == "stopped":
                attributes.update({
                    "message": "Watchdog остановлен",
                    "recommended_action": "Перезапустить интеграцию или watchdog",
                    "health_score": 0
                })
            elif current_status == "error":
                attributes.update({
                    "message": "Ошибка в работе watchdog",
                    "recommended_action": "Проверить логи и перезагрузить интеграцию",
                    "health_score": 0,
                    "error": status.get("error", "unknown")
                })
            
            # Добавляем временные метки
            if status.get("last_success"):
                attributes["last_success_timestamp"] = status["last_success"]
            
            # Добавляем информацию о восстановлении
            if status.get("is_recovering"):
                attributes["recovery_started"] = datetime.now().isoformat()
                
            # Добавляем информацию о доступности MegaD
            if hasattr(self._megad, 'is_available'):
                attributes["megad_available"] = self._megad.is_available
                
            # Добавляем информацию о последнем обновлении координатора
            if hasattr(self._coordinator, 'last_update_success'):
                attributes["coordinator_last_success"] = self._coordinator.last_update_success
                
            if hasattr(self._coordinator, 'last_update_time'):
                attributes["coordinator_last_update"] = self._coordinator.last_update_time.isoformat() if self._coordinator.last_update_time else None
            
            return attributes
            
        except Exception as e:
            _LOGGER.error(f"Ошибка при получении атрибутов watchdog: {e}")
            attributes.update({
                "status": "error",
                "message": f"Ошибка получения статуса: {str(e)}",
                "recommended_action": "Проверить логи интеграции",
                "health_score": 0,
                "error": str(e)
            })
            return attributes

    def _get_watchdog_status(self) -> dict:
        """Получает статус watchdog."""
        try:
            # Проверяем доступность watchdog
            if not self._coordinator or not hasattr(self._coordinator, 'watchdog') or not self._coordinator.watchdog:
                return {
                    "is_running": False,
                    "is_recovering": False,
                    "failure_count": 0,
                    "max_failures": WATCHDOG_MAX_FAILURES,
                    "last_success": None,
                    "megad_id": self._megad.id,
                    "is_available": False,
                    "error": "watchdog_not_initialized"
                }
            
            # Используем метод get_status если он есть
            if hasattr(self._coordinator.watchdog, 'get_status'):
                status = self._coordinator.watchdog.get_status()
                
                # Обеспечиваем совместимость со старым и новым форматом
                if "is_recovering" not in status:
                    status["is_recovering"] = getattr(self._coordinator.watchdog, '_recovering', False)
                if "max_failures" not in status:
                    status["max_failures"] = getattr(self._coordinator.watchdog, '_max_failures', WATCHDOG_MAX_FAILURES)
                if "health_check_interval" not in status:
                    status["health_check_interval"] = getattr(self._coordinator.watchdog, '_health_check_interval', WATCHDOG_CHECK_INTERVAL)
                
                return status
            else:
                # Альтернативный способ получения статуса
                return {
                    "is_running": getattr(self._coordinator.watchdog, '_is_running', False),
                    "is_recovering": getattr(self._coordinator.watchdog, '_recovering', False),
                    "failure_count": getattr(self._coordinator.watchdog, '_failure_count', 0),
                    "max_failures": getattr(self._coordinator.watchdog, '_max_failures', WATCHDOG_MAX_FAILURES),
                    "last_success": getattr(self._coordinator.watchdog, '_last_success', None),
                    "megad_id": self._megad.id,
                    "is_available": getattr(self._megad, 'is_available', False),
                    "health_check_interval": getattr(self._coordinator.watchdog, '_health_check_interval', WATCHDOG_CHECK_INTERVAL)
                }
                
        except Exception as e:
            _LOGGER.error(f"Критическая ошибка при получении статуса watchdog: {e}")
            return {
                "is_running": False,
                "is_recovering": False,
                "failure_count": 0,
                "max_failures": WATCHDOG_MAX_FAILURES,
                "last_success": None,
                "megad_id": self._megad.id,
                "is_available": False,
                "error": str(e)
            }
    
    def _handle_coordinator_update(self) -> None:
        """Обработчик обновлений от координатора."""
        # Принудительно обновляем состояние сенсора
        self.async_write_ha_state()
        
        # Логируем изменение статуса для отладки
        status = self.native_value
        _LOGGER.debug(f"Watchdog статус обновлен для MegaD-{self._megad.id}: {status}")
        
        # Дополнительная логика при определенных статусах
        if status == "recovering":
            _LOGGER.warning(f"MegaD-{self._megad.id} находится в процессе восстановления")
        elif status == "unavailable":
            _LOGGER.error(f"MegaD-{self._megad.id} недоступен")
        elif status == "online" and self._last_status != "online":
            _LOGGER.info(f"MegaD-{self._megad.id} восстановил соединение")
        
        # Сохраняем последний статус для сравнения
        self._last_status = status
    
    async def async_added_to_hass(self):
        """Вызывается когда сущность добавлена в HA."""
        await super().async_added_to_hass()
        _LOGGER.info(f"Watchdog статус сенсор добавлен для MegaD-{self._megad.id}")
        
        # Инициализируем последний статус
        self._last_status = None
        
        # Регистрируем обновление при старте
        self.async_write_ha_state()
        
        # Подписываемся на обновления координатора
        self.async_on_remove(
            self.coordinator.async_add_listener(self._handle_coordinator_update)
        )
    
    async def async_will_remove_from_hass(self):
        """Вызывается когда сущность удаляется из HA."""
        await super().async_will_remove_from_hass()
        _LOGGER.info(f"Watchdog статус сенсор удален для MegaD-{self._megad.id}")


class WatchdogRecoveryHistorySensor(CoordinatorEntity, SensorEntity):
    """Сенсор истории восстановлений watchdog."""
    
    _attr_icon = "mdi:history"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    
    def __init__(self, coordinator: MegaDCoordinator, unique_id: str):
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._megad = coordinator.megad
        self._unique_id = unique_id
        self._attr_name = f"MegaD-{coordinator.megad.id} Watchdog Recovery History"
        self._attr_device_info = coordinator.devices_info()
        self.entity_id = f'sensor.{coordinator.megad.id}_watchdog_recovery_history'
        
        # История восстановлений
        self._recovery_history = []
        self._max_history_entries = 10  # Максимальное количество записей в истории
        
    @cached_property
    def name(self) -> str:
        return self._attr_name

    @cached_property
    def unique_id(self) -> str:
        return self._unique_id

    @property
    def native_value(self) -> datetime | None:
        """Возвращает время последнего восстановления."""
        if self._recovery_history:
            return self._recovery_history[-1]  # Последнее восстановление
        return None

    @cached_property
    def extra_state_attributes(self):
        """Дополнительные атрибуты с историей восстановлений."""
        attributes = {
            "megad_id": self._megad.id,
            "total_recoveries": len(self._recovery_history),
            "history_limit": self._max_history_entries
        }
        
        # Добавляем полную историю
        if self._recovery_history:
            attributes["recovery_history"] = [
                ts.isoformat() for ts in self._recovery_history[-self._max_history_entries:]
            ]
            
            # Статистика
            if len(self._recovery_history) > 1:
                # Время между восстановлениями
                intervals = []
                for i in range(1, len(self._recovery_history)):
                    interval = (self._recovery_history[i] - self._recovery_history[i-1]).total_seconds()
                    intervals.append(interval)
                
                attributes.update({
                    "average_interval_hours": sum(intervals) / len(intervals) / 3600 if intervals else 0,
                    "min_interval_hours": min(intervals) / 3600 if intervals else 0,
                    "max_interval_hours": max(intervals) / 3600 if intervals else 0,
                    "last_interval_hours": intervals[-1] / 3600 if intervals else 0
                })
        
        return attributes
    
    def add_recovery_record(self):
        """Добавляет запись о восстановлении."""
        recovery_time = datetime.now()
        self._recovery_history.append(recovery_time)
        
        # Ограничиваем размер истории
        if len(self._recovery_history) > self._max_history_entries:
            self._recovery_history = self._recovery_history[-self._max_history_entries:]
        
        # Обновляем состояние
        self.async_write_ha_state()
        _LOGGER.info(f"Запись о восстановлении добавлена для MegaD-{self._megad.id}")
    
    async def async_added_to_hass(self):
        """Вызывается когда сущность добавлена в HA."""
        await super().async_added_to_hass()
        _LOGGER.info(f"Сенсор истории восстановлений добавлен для MegaD-{self._megad.id}")


# УБРАНО: класс BinaryStateSensorMegaD больше не нужен, 
# т.к. сущности для портов P/R создаются в binary_sensor.py


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
        self._attr_device_info = coordinator.devices_info()
        # Получаем domain из конфигурации порта или используем по умолчанию
        self._domain = getattr(port.conf, 'domain', 'sensor')
        self.entity_id = f'{self._domain}.{self._megad.id}_port{port.conf.id}'

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
        self._attr_device_info = coordinator.devices_info()
        # Получаем domain из конфигурации порта или используем по умолчанию
        self._domain = getattr(port.conf, 'domain', 'sensor')
        self.entity_id = f'{self._domain}.{self._megad.id}_port{port.conf.id}_click'
        
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
        # Используем старые значения для обратной совместимости с автоматизациями
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
            unique_id: str, type_sensor: str, prefix: str = ''
    ) -> None:
        super().__init__(coordinator)
        self._megad: MegaD = coordinator.megad
        self._port: DigitalSensorBase = port
        self.type_sensor = type_sensor
        self._sensor_name: str = (f'{port.conf.name} '
                                  f'{TYPE_SENSOR_RUS[type_sensor]}{prefix}')
        self._unique_id: str = unique_id
        self._domain = getattr(port.conf, 'domain', 'sensor')
        self._attr_device_info = coordinator.devices_info()
        self.entity_id = (f'{self._domain}.{self._megad.id}_port{port.conf.id}_'
                          f'{self.type_sensor.lower()}{prefix}')
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
        self.entity_id = (f'{self._domain}.{self._megad.id}_port{port.conf.id}_'
                          f'{id_one_wire}')

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
        self._attr_device_info = coordinator.devices_info()
        self.entity_id = f'{self._domain}.megad_{self._sensor_name}'

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
        self._attr_device_info = coordinator.devices_info()
        self.entity_id = f'{self._domain}.{self._megad.id}_port{port.conf.id}_analog'

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
        self._attr_device_info = coordinator.devices_info()
        self.entity_id = f'{self._domain}.{self._megad.id}_{pid.conf.id}_pid_value'

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
