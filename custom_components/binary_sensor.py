import logging

from propcache import cached_property

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass, BinarySensorEntity
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from . import MegaDCoordinator
from .const import DOMAIN, ENTRIES, CURRENT_ENTITY_IDS
from .core.base_ports import BinaryPortIn, I2CExtraMCP230xx
from .core.enums import DeviceClassBinary
from .core.megad import MegaD
from .core.models_megad import MCP230PortInConfig

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        async_add_entities: AddEntitiesCallback
) -> None:
    entry_id = config_entry.entry_id
    coordinator = hass.data[DOMAIN][ENTRIES][entry_id]
    megad = coordinator.megad
    
    # Сохраняем entry_id в координаторе для создания уникальных ID
    coordinator.config_entry = config_entry

    binary_sensors = []
    for port in megad.ports:
        if isinstance(port, BinaryPortIn):
            # ✅ Используем метод координатора для создания уникального ID
            unique_id = coordinator.create_entity_unique_id(
                port.conf.id, "binary_sensor"
            )
            binary_sensors.append(BinarySensorMegaD(
                coordinator, port, unique_id)
            )
        if isinstance(port, I2CExtraMCP230xx):
            for config in port.extra_confs:
                if isinstance(config, MCP230PortInConfig):
                    # ✅ Используем метод координатора для создания уникального ID
                    unique_id = coordinator.create_entity_unique_id(
                        port.conf.id, "binary_sensor", extra_port_id=config.id
                    )
                    binary_sensors.append(BinarySensorExtraMegaD(
                        coordinator, port, config, unique_id
                    ))
    
    for binary_sensor in binary_sensors:
        hass.data[DOMAIN][CURRENT_ENTITY_IDS][entry_id].append(
            binary_sensor.unique_id)
    
    if binary_sensors:
        async_add_entities(binary_sensors)
        _LOGGER.info(f'Добавлено {len(binary_sensors)} бинарных сенсоров для MegaD {megad.id}')


class BinarySensorMegaD(CoordinatorEntity, BinarySensorEntity):
    def __init__(self, coordinator: MegaDCoordinator, port: BinaryPortIn, unique_id: str) -> None:
        super().__init__(coordinator)
        self._megad: MegaD = coordinator.megad
        self._port: BinaryPortIn = port
        self._attr_unique_id = unique_id
        
        # Используем метод координатора для очистки имени
        port_name = port.conf.name
        self._attr_name = coordinator.create_clean_port_name(port_name, port.conf.id, "Sensor")
        self._original_name = port_name
        
        # Извлекаем область из имени порта
        suggested_area = coordinator.extract_area_from_port_name(port_name)
        
        # ✅ ИСПРАВЛЕНИЕ: Используем entity_device_info
        self._attr_device_info = coordinator.entity_device_info(
            self._attr_name,
            f"MegaD-{self._megad.id} Binary Sensor",
            entity_type="binary_sensor",
            port_id=port.conf.id,
            suggested_area=suggested_area
        )
        
        self._attr_has_entity_name = False
        self._last_state = None
        
    @property
    def name(self) -> str:
        return self._attr_name

    @property
    def unique_id(self) -> str:
        return self._attr_unique_id

    @property
    def device_info(self):
        """Return device info."""
        return self._attr_device_info

    @property
    def is_on(self) -> bool | None:
        """Return true if the binary sensor is on."""
        return self._port.state

    @cached_property
    def device_class(self) -> BinarySensorDeviceClass | None:
        """Return the class of this entity."""
        if not hasattr(self._port.conf, 'device_class'):
            return None
            
        # Используем прямое сравнение без .value
        device_class = self._port.conf.device_class
        
        if device_class == DeviceClassBinary.SMOKE:
            return BinarySensorDeviceClass.SMOKE
        elif device_class == DeviceClassBinary.DOOR:
            return BinarySensorDeviceClass.DOOR
        elif device_class == DeviceClassBinary.MOTION:
            return BinarySensorDeviceClass.MOTION
        elif device_class == DeviceClassBinary.GARAGE_DOOR:
            return BinarySensorDeviceClass.GARAGE_DOOR
        elif device_class == DeviceClassBinary.LOCK:
            return BinarySensorDeviceClass.LOCK
        elif device_class == DeviceClassBinary.MOISTURE:
            return BinarySensorDeviceClass.MOISTURE
        elif device_class == DeviceClassBinary.WINDOW:
            return BinarySensorDeviceClass.WINDOW
        else:
            return None
    
    async def async_added_to_hass(self):
        """Вызывается когда сущность добавлена в HA."""
        await super().async_added_to_hass()
        
        # Слушаем обновления координатора
        self.async_on_remove(
            self.coordinator.async_add_listener(self._handle_coordinator_update)
        )
        
        # Инициализируем последнее состояние
        self._last_state = self.is_on
        
        # Первоначальное обновление состояния
        self._handle_coordinator_update()
        
        # Логируем создание сущности
        _LOGGER.debug(f"Binary sensor добавлен: {self.entity_id}, порт {self._port.conf.id}, "
                     f"initial state: {self._last_state}")
        
        # ❌ УБИРАЕМ: Не отмечаем watchdog при создании сущности
        # Это внутреннее событие HA, а не обратная связь от контроллера
    
    def _handle_coordinator_update(self) -> None:
        """Обработчик обновлений от координатора."""
        current_state = self.is_on
        
        # Логируем изменение состояния
        if self._last_state is not None and self._last_state != current_state:
            _LOGGER.debug(f"Binary sensor {self.entity_id} changed: {self._last_state} -> {current_state}")
            
            # ❌ ВАЖНО: НЕ ОТМЕЧАЕМ ДЛЯ WATCHDOG!
            # Обновления координатора НЕ являются обратной связью от контроллера
            # Обратная связь - только HTTP запросы от контроллера через server.py
            
            _LOGGER.debug(f"Binary sensor {self.entity_id} обновлен (внутреннее обновление координатора)")
        
        # Сохраняем текущее состояние для сравнения
        self._last_state = current_state
        
        # Вызываем родительский метод для обновления UI
        super()._handle_coordinator_update()
    
    # ✅ ДОБАВЛЕНО: Дополнительные атрибуты для диагностики
    @cached_property
    def extra_state_attributes(self):
        """Дополнительные атрибуты для диагностики."""
        attributes = {
            "port_id": self._port.conf.id,
            "megad_id": self._megad.id,
            "device_type": "binary_sensor",
            "original_name": self._original_name,
            "last_state_change": self._last_state,
            "sensor_type": "binary_input",
            "supports_watchdog": True
        }
        
        # Добавляем информацию о порте если доступна
        if hasattr(self._port.conf, 'mode'):
            attributes["port_mode"] = str(self._port.conf.mode)
        
        # ❌ УБИРАЕМ: Не добавляем информацию о watchdog в атрибуты
        # Watchdog - отдельная система мониторинга
        
        return attributes


class BinarySensorExtraMegaD(CoordinatorEntity, BinarySensorEntity):
    """Дополнительный бинарный сенсор (I2C расширитель)."""

    def __init__(
            self, 
            coordinator: MegaDCoordinator, 
            port: I2CExtraMCP230xx,
            config_extra_port: MCP230PortInConfig,
            unique_id: str
    ) -> None:
        super().__init__(coordinator)
        self._megad: MegaD = coordinator.megad
        self._port = port
        self._config_extra_port = config_extra_port
        self._attr_unique_id = unique_id
        
        # ✅ ИСПРАВЛЕНИЕ: Используем метод координатора для очистки имени
        port_name = config_extra_port.name
        self._attr_name = coordinator.create_clean_port_name(port_name, config_extra_port.id, "Sensor")
        self._original_name = port_name
        
        # ✅ КРИТИЧЕСКОЕ ИСПРАВЛЕНИЕ: Для I2C сенсоров используем ТОТ ЖЕ device_info
        # Все сущности должны использовать один device_info для группировки
        suggested_area = coordinator.extract_area_from_port_name(port_name) if hasattr(coordinator, 'extract_area_from_port_name') else None
        self._attr_device_info = coordinator.entity_device_info(
            self._attr_name,
            f"MegaD-{self._megad.id} Extra Binary Sensor",
            entity_type="extra_binary_sensor",
            port_id=port.conf.id,
            extra_port_id=config_extra_port.id,
            suggested_area=suggested_area
        )
        
        self._attr_has_entity_name = False
        self._last_state = None

    @property
    def device_info(self):
        """Return device info."""
        return self._attr_device_info

    @property
    def is_on(self) -> bool | None:
        """Return true if the binary sensor is on."""
        if self._port.state:
            return bool(self._port.state.get(self._config_extra_port.id))
        return False

    @cached_property
    def device_class(self) -> BinarySensorDeviceClass | None:
        """Return the class of this entity."""
        if not hasattr(self._config_extra_port, 'device_class'):
            return None
            
        # Используем прямое сравнение без .value
        device_class = self._config_extra_port.device_class
        
        if device_class == DeviceClassBinary.SMOKE:
            return BinarySensorDeviceClass.SMOKE
        elif device_class == DeviceClassBinary.DOOR:
            return BinarySensorDeviceClass.DOOR
        elif device_class == DeviceClassBinary.MOTION:
            return BinarySensorDeviceClass.MOTION
        elif device_class == DeviceClassBinary.GARAGE_DOOR:
            return BinarySensorDeviceClass.GARAGE_DOOR
        elif device_class == DeviceClassBinary.LOCK:
            return BinarySensorDeviceClass.LOCK
        elif device_class == DeviceClassBinary.MOISTURE:
            return BinarySensorDeviceClass.MOISTURE
        elif device_class == DeviceClassBinary.WINDOW:
            return BinarySensorDeviceClass.WINDOW
        else:
            return None
    
    async def async_added_to_hass(self):
        """Вызывается когда сущность добавлена в HA."""
        await super().async_added_to_hass()
        
        # Слушаем обновления координатора
        self.async_on_remove(
            self.coordinator.async_add_listener(self._handle_coordinator_update)
        )
        
        # Инициализируем последнее состояние
        self._last_state = self.is_on
        
        # Первоначальное обновление состояния
        self._handle_coordinator_update()
        
        # Логируем создание сущности
        _LOGGER.debug(f"Extra binary sensor добавлен: {self.entity_id}, "
                     f"base_port {self._port.conf.id}, extra_port {self._config_extra_port.id}, "
                     f"initial state: {self._last_state}")
        
        # ❌ УБИРАЕМ: Не отмечаем watchdog при создании сущности
    
    def _handle_coordinator_update(self) -> None:
        """Обработчик обновлений от координатора."""
        current_state = self.is_on
        
        # Логируем изменение состояния
        if self._last_state is not None and self._last_state != current_state:
            _LOGGER.debug(f"Extra binary sensor {self.entity_id} changed: {self._last_state} -> {current_state}")
            
            # ❌ ВАЖНО: НЕ ОТМЕЧАЕМ ДЛЯ WATCHDOG!
            # Это внутреннее обновление координатора, не обратная связь
            
            _LOGGER.debug(f"Extra binary sensor {self.entity_id} обновлен (внутреннее обновление координатора)")
        
        # Сохраняем текущее состояние для сравнения
        self._last_state = current_state
        
        # Вызываем родительский метод для обновления UI
        super()._handle_coordinator_update()
    
    # ✅ ДОБАВЛЕНО: Дополнительные атрибуты для диагностики
    @cached_property
    def extra_state_attributes(self):
        """Дополнительные атрибуты для диагностики."""
        attributes = {
            "base_port_id": self._port.conf.id,
            "extra_port_id": self._config_extra_port.id,
            "megad_id": self._megad.id,
            "device_type": "extra_binary_sensor",
            "original_name": self._original_name,
            "last_state_change": self._last_state,
            "sensor_type": "extra_binary_input",
            "supports_watchdog": True
        }
        
        # ❌ УБИРАЕМ: Не добавляем информацию о watchdog в атрибуты
        
        return attributes