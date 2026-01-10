import logging
import asyncio
from math import floor
from typing import Optional, Any
from datetime import datetime

from homeassistant.components.light import LightEntity, ColorMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import MegaDCoordinator
from .const import DOMAIN, ENTRIES, CURRENT_ENTITY_IDS
from .core.base_ports import (
    RelayPortOut, PWMPortOut, I2CExtraPCA9685, I2CExtraMCP230xx
)
from .core.enums import TypePortMegaD
from .core.megad import MegaD
from .core.models_megad import (
    PCA9685RelayConfig, MCP230RelayConfig, PCA9685PWMConfig
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        async_add_entities: AddEntitiesCallback
) -> None:
    """Настройка платформы освещения."""
    
    if DOMAIN not in hass.data:
        return
    
    entry_id = config_entry.entry_id
    
    if (ENTRIES not in hass.data[DOMAIN] or 
        entry_id not in hass.data[DOMAIN][ENTRIES]):
        return
    
    coordinator = hass.data[DOMAIN][ENTRIES][entry_id]
    megad = coordinator.megad
    
    # ✅ ЛОГИКА: Создаем Light сущности для ВСЕХ портов с Mode=SW или Mode=PWM
    
    lights = []
    
    # ✅ ДЛЯ ОТЛАДКИ
    _LOGGER.debug(f"=== СОЗДАНИЕ LIGHT СУЩНОСТЕЙ ДЛЯ MegaD-{megad.id} ===")
    
    for port in megad.ports:
        # 1. Релейные порты (RelayPortOut) - проверяем режим SW
        if isinstance(port, RelayPortOut):
            port_type = getattr(port.conf, 'type_port', None)
            port_mode = getattr(port.conf, 'mode', None)  # SW, PWM, SW_LINK, DS2413
            
            _LOGGER.debug(f"Порт {port.conf.id} '{port.conf.name}': "
                         f"type_port={port_type}, mode={port_mode}")
            
            # ✅ ПРАВИЛО: Light создаем если mode == SW
            from .core.enums import ModeOutMegaD
            
            if port_mode == ModeOutMegaD.SW:
                unique_id = coordinator.create_entity_unique_id(
                    port.conf.id, "light"
                )
                lights.append(LightRelayMegaD(coordinator, port, unique_id))
                _LOGGER.info(f"✅ СОЗДАН LightRelay: порт {port.conf.id} (Mode=SW)")
            else:
                _LOGGER.debug(f"❌ ПРОПУСК Light: порт {port.conf.id} (Mode={port_mode})")
        
        # 2. ШИМ порты (PWMPortOut) - проверяем режим PWM
        elif isinstance(port, PWMPortOut):
            port_mode = getattr(port.conf, 'mode', None)
            
            _LOGGER.debug(f"ШИМ порт {port.conf.id} '{port.conf.name}': mode={port_mode}")
            
            # ✅ ПРАВИЛО: Light создаем если mode == PWM
            from .core.enums import ModeOutMegaD
            
            if port_mode == ModeOutMegaD.PWM:
                unique_id = coordinator.create_entity_unique_id(
                    port.conf.id, "light"
                )
                lights.append(LightPWMMegaD(coordinator, port, unique_id))
                _LOGGER.info(f"✅ СОЗДАН LightPWM: порт {port.conf.id} (Mode=PWM)")
            else:
                _LOGGER.debug(f"❌ ПРОПУСК Light: порт {port.conf.id} (Mode={port_mode})")
        
        # 3. Дополнительные релейные порты на расширителях PCA9685
        elif isinstance(port, I2CExtraPCA9685):
            for config in port.extra_confs:
                if isinstance(config, PCA9685RelayConfig):
                    port_mode = getattr(config, 'mode', None)
                    
                    from .core.enums import ModeOutMegaD
                    
                    if port_mode == ModeOutMegaD.SW:
                        unique_id = coordinator.create_entity_unique_id(
                            port.conf.id, "light", extra_port_id=config.id
                        )
                        lights.append(LightExtraMegaD(
                            coordinator, port, config, unique_id
                        ))
                        _LOGGER.info(f"✅ СОЗДАН LightExtra: порт {port.conf.id}e{config.id} (Mode=SW)")
                
                # Дополнительные ШИМ порты на PCA9685
                elif isinstance(config, PCA9685PWMConfig):
                    port_mode = getattr(config, 'mode', None)
                    
                    from .core.enums import ModeOutMegaD
                    
                    if port_mode == ModeOutMegaD.PWM:
                        unique_id = coordinator.create_entity_unique_id(
                            port.conf.id, "light", extra_port_id=config.id
                        )
                        lights.append(LightExtraPWMMegaD(
                            coordinator, port, config, unique_id
                        ))
                        _LOGGER.info(f"✅ СОЗДАН LightExtraPWM: порт {port.conf.id}e{config.id} (Mode=PWM)")
        
        # 4. Дополнительные порты на MCP расширителях
        elif isinstance(port, I2CExtraMCP230xx):
            for config in port.extra_confs:
                if isinstance(config, MCP230RelayConfig):
                    port_mode = getattr(config, 'mode', None)
                    
                    from .core.enums import ModeOutMegaD
                    
                    if port_mode == ModeOutMegaD.SW:
                        unique_id = coordinator.create_entity_unique_id(
                            port.conf.id, "light", extra_port_id=config.id
                        )
                        lights.append(LightExtraMegaD(
                            coordinator, port, config, unique_id
                        ))
                        _LOGGER.info(f"✅ СОЗДАН LightExtra MCP: порт {port.conf.id}e{config.id} (Mode=SW)")
    
    if not lights:
        _LOGGER.warning(f"Не найдено LIGHT сущностей для MegaD {megad.id}")
        return
    
    # Регистрируем ID сущностей
    if CURRENT_ENTITY_IDS not in hass.data[DOMAIN]:
        hass.data[DOMAIN][CURRENT_ENTITY_IDS] = {}
    
    if entry_id not in hass.data[DOMAIN][CURRENT_ENTITY_IDS]:
        hass.data[DOMAIN][CURRENT_ENTITY_IDS][entry_id] = []
    
    for light in lights:
        hass.data[DOMAIN][CURRENT_ENTITY_IDS][entry_id].append(light.unique_id)
    
    async_add_entities(lights)
    _LOGGER.info(f"Добавлено {len(lights)} LIGHT сущностей для MegaD {megad.id}")


class LightRelayMegaD(CoordinatorEntity, LightEntity):
    def __init__(self, coordinator: MegaDCoordinator, port: RelayPortOut, unique_id: str) -> None:
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._megad = coordinator.megad
        self._port = port
        self._attr_unique_id = unique_id
        
        # ✅ ДОБАВЛЕНО: Определение цветовых режимов
        self._attr_supported_color_modes = {ColorMode.ONOFF}
        self._attr_color_mode = ColorMode.ONOFF
        
        # Очищаем имя порта
        port_name = port.conf.name
        self._attr_name = coordinator.create_clean_port_name(port_name, port.conf.id, "Light")
        self._original_name = port_name
        
        # Извлекаем область из имени
        suggested_area = coordinator.extract_area_from_port_name(port_name)
        
        # ✅ ИСПРАВЛЕНИЕ: Используем entity_device_info вместо device_base_info
        # Это создаст уникальное устройство для каждой сущности
        self._attr_device_info = coordinator.entity_device_info(
            self._attr_name,  # Имя сущности станет именем устройства
            f"MegaD-{self._megad.id} Relay Light",
            entity_type="relay_light",
            port_id=port.conf.id,
            suggested_area=suggested_area
        )
        
        self._attr_has_entity_name = False
        self._port_id = port.conf.id

    async def async_added_to_hass(self):
        """Когда сущность добавлена в HA."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_listener(self._handle_coordinator_update)
        )
        self._handle_coordinator_update()
        
        # ✅ Логирование для отладки
        _LOGGER.debug(f"LightRelayMegaD добавлен: {self.entity_id}, порт {self._port_id}, "
                     f"name: {self._attr_name}, original: {self._original_name}")

    @property
    def device_info(self):
        """Return device info."""
        return self._attr_device_info

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self._coordinator.last_update_success
    
    @property
    def should_poll(self) -> bool:
        """Return False as we get updates via coordinator."""
        return False

    @property
    def is_on(self) -> bool | None:
        """Return true if the light is on."""
        port_state = self._port.state
        if port_state is None:
            return None
        
        # Учитываем инверсию порта
        inverse = getattr(self._port.conf, 'inverse', False)
        is_on = not bool(port_state) if inverse else bool(port_state)
        
        return is_on

    async def _switch_port(self, command: int):
        """Отправка команды переключения порта."""
        try:
            # Фиксируем время команды
            self._command_timestamp = datetime.now().timestamp()
            
            # Отправляем команду на контроллер
            await self._megad.set_port(self._port.conf.id, command)
            
            # ✅ ОТМЕЧАЕМ ПОЛУЧЕНИЕ ДАННЫХ ДЛЯ WATCHDOG
            if hasattr(self._coordinator, 'watchdog') and self._coordinator.watchdog:
                self._coordinator.watchdog.mark_data_received()
                self._coordinator.watchdog.mark_feedback_event({
                    "type": "light_switch",
                    "entity_id": self.entity_id,
                    "port_id": self._port.conf.id,
                    "command": command,
                    "light_type": "relay"
                })
            
            # Устанавливаем ожидаемое состояние немедленно
            expected_state = command
            inverse = getattr(self._port.conf, 'inverse', False)
            if inverse:
                expected_state = 1 if command == 0 else 0
            
            # Обновляем состояние через координатор
            await self._coordinator.update_port_state(
                self._port.conf.id, 
                expected_state
            )
            
            # Немедленно обновляем UI с ожидаемым состоянием
            self.async_write_ha_state()
            
            # Запрашиваем подтверждение состояния
            async def confirm_and_update():
                try:
                    await asyncio.sleep(0.2)
                    
                    # Получаем актуальное состояние с контроллера
                    actual_value = await self._megad.get_port(self._port.conf.id)
                    if actual_value is not None and actual_value != expected_state:
                        await self._coordinator.update_port_state(
                            self._port.conf.id, 
                            actual_value
                        )
                        _LOGGER.debug(
                            f"Порт {self._port.conf.id}: состояние обновлено с {expected_state} на {actual_value}"
                        )
                    
                    self.async_write_ha_state()
                    _LOGGER.debug(f"Порт {self._port.conf.id}: состояние подтверждено")
                
                except Exception as e:
                    _LOGGER.debug(f"Порт {self._port.conf.id}: не удалось подтвердить состояние: {e}")
            
            self.hass.async_create_task(confirm_and_update())
            
        except Exception as e:
            _LOGGER.error(f'Ошибка управления портом {self._port.conf.id}: {e}')
            raise

    def _handle_coordinator_update(self):
        """Обработчик обновления координатора."""
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs):
        """Turn the light on."""
        inverse = getattr(self._port.conf, 'inverse', False)
        command = 0 if inverse else 1
        await self._switch_port(command)

    async def async_turn_off(self, **kwargs):
        """Turn the light off."""
        inverse = getattr(self._port.conf, 'inverse', False)
        command = 1 if inverse else 0
        await self._switch_port(command)

    async def async_toggle(self, **kwargs):
        """Toggle the light."""
        await self._switch_port(2)

    @property
    def extra_state_attributes(self):
        """Дополнительные атрибуты для диагностики."""
        return {
            "port_id": self._port.conf.id,
            "megad_id": self._megad.id,
            "device_type": "relay_light",
            "original_name": self._original_name
        }


class LightPWMBaseMegaD(CoordinatorEntity, LightEntity):
    """Базовый класс для освещения с ШИМ."""

    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_has_entity_name = False

    def __init__(
            self, 
            coordinator: MegaDCoordinator,
            min_brightness,
            max_brightness,
            name: str,
            unique_id: str,
            port_id: int = None,
            extra_port_id: int = None
    ) -> None:
        super().__init__(coordinator)
        self._coordinator: MegaDCoordinator = coordinator
        self.min_brightness = min_brightness
        self.max_brightness = max_brightness
        self._attr_name = name
        self._attr_unique_id = unique_id
        self._port_id = port_id
        self._extra_port_id = extra_port_id
        
        # Для отслеживания команд
        self._command_timestamp = 0

    async def async_added_to_hass(self):
        """Когда сущность добавлена в HA."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_listener(self._handle_coordinator_update)
        )
        self._handle_coordinator_update()
        
        # ✅ Логирование для отладки
        _LOGGER.debug(f"LightPWMBaseMegaD добавлен: {self.entity_id}, порт {self._port_id}, "
                     f"extra_port {self._extra_port_id}")

    @property
    def device_info(self):
        """Return device info."""
        return self._attr_device_info

    def device_to_ha_brightness(self, device_value) -> int:
        """Конвертация значения устройства в яркость HA (0-255)."""
        if device_value <= self.min_brightness:
            return 0
        elif device_value >= self.max_brightness:
            return 255
        
        value = (device_value - self.min_brightness) / (
            self.max_brightness - self.min_brightness) * 255
        return floor(value + 0.5)

    def ha_to_device_brightness(self, ha_value) -> int:
        """Конвертация яркости HA (0-255) в значение устройства."""
        if ha_value == 0:
            return 0
        elif ha_value == 255:
            return self.max_brightness
        
        value = ha_value / 255 * (
            self.max_brightness - self.min_brightness
        ) + self.min_brightness
        return floor(value + 0.5)
    
    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self._coordinator.last_update_success
    
    @property
    def should_poll(self) -> bool:
        """Return False as we get updates via coordinator."""
        return False

    def _handle_coordinator_update(self):
        """Обработчик обновления координатора."""
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self):
        """Дополнительные атрибуты для диагностики."""
        attributes = {
            "megad_id": self._coordinator.megad.id,
            "device_type": "pwm_light",
            "min_brightness": self.min_brightness,
            "max_brightness": self.max_brightness
        }
        
        if self._port_id is not None:
            attributes["port_id"] = self._port_id
        
        if self._extra_port_id is not None:
            attributes["extra_port_id"] = self._extra_port_id
        
        return attributes


class LightPWMMegaD(LightPWMBaseMegaD):
    """ШИМ свет."""

    def __init__(
            self, 
            coordinator: MegaDCoordinator, 
            port: PWMPortOut,
            unique_id: str
    ) -> None:
        # ✅ ИСПРАВЛЕНИЕ: Используем метод координатора для очистки имени
        port_name = port.conf.name
        cleaned_name = coordinator.create_clean_port_name(port_name, port.conf.id, "Light")
        
        super().__init__(
            coordinator, 
            port.conf.min_value, 
            255,
            cleaned_name,  # ✅ Передаем очищенное имя
            unique_id,
            port_id=port.conf.id
        )
        self._megad: MegaD = coordinator.megad
        self._port: PWMPortOut = port
        self._original_name = port_name
        
        # ✅ ИСПРАВЛЕНИЕ: Используем device_base_info с областью
        suggested_area = coordinator.extract_area_from_port_name(port_name) if hasattr(coordinator, 'extract_area_from_port_name') else None
        self._attr_device_info = coordinator.entity_device_info(
            self._attr_name,
            f"MegaD-{self._megad.id} PWM Light",
            entity_type="pwm_light",
            port_id=port.conf.id,
            suggested_area=suggested_area
        )
        self._attr_has_entity_name = False

    @property
    def device_info(self):
        """Return device info."""
        return self._attr_device_info

    @property
    def brightness(self) -> int | None:
        """Return the brightness of this light between 0..255."""
        port_state = self._port.state
        if port_state is None:
            return None
        
        return self.device_to_ha_brightness(port_state)

    @property
    def is_on(self) -> bool | None:
        """Return true if the light is on."""
        brightness = self.brightness
        if brightness is None:
            return None
        
        return brightness > 0

    async def async_turn_on(self, brightness: int = 255, **kwargs):
        """Turn the entity on."""
        try:
            # Фиксируем время команды
            self._command_timestamp = datetime.now().timestamp()
            
            # Определяем яркость для установки
            target_ha_brightness = brightness if brightness is not None else 255
            
            # Конвертируем в значение устройства
            device_value = self.ha_to_device_brightness(target_ha_brightness)
            
            # Отправляем команду на контроллер
            await self._megad.set_port(self._port.conf.id, device_value)
            
            # ✅ ОТМЕЧАЕМ ПОЛУЧЕНИЕ ДАННЫХ ДЛЯ WATCHDOG
            if hasattr(self._coordinator, 'watchdog') and self._coordinator.watchdog:
                self._coordinator.watchdog.mark_data_received()
                self._coordinator.watchdog.mark_feedback_event({
                    "type": "pwm_light_switch",
                    "entity_id": self.entity_id,
                    "port_id": self._port.conf.id,
                    "brightness": target_ha_brightness,
                    "device_value": device_value,
                    "light_type": "pwm"
                })
            
            # Устанавливаем ожидаемое состояние немедленно
            await self._coordinator.update_port_state(
                self._port.conf.id, 
                device_value
            )
            
            # Немедленно обновляем UI с ожидаемым состоянием
            self.async_write_ha_state()
            
            # Запрашиваем подтверждение состояния
            async def confirm_and_update():
                try:
                    await asyncio.sleep(0.2)
                    
                    # Получаем актуальное состояние с контроллера
                    actual_value = await self._megad.get_port(self._port.conf.id)
                    if actual_value is not None and actual_value != device_value:
                        await self._coordinator.update_port_state(
                            self._port.conf.id, 
                            actual_value
                        )
                        _LOGGER.debug(
                            f"Порт {self._port.conf.id}: яркость обновлена с {device_value} на {actual_value}"
                        )
                    
                    self.async_write_ha_state()
                    _LOGGER.debug(f"Порт {self._port.conf.id}: яркость подтверждена")
                
                except Exception as e:
                    _LOGGER.debug(f"Порт {self._port.conf.id}: не удалось подтвердить яркость: {e}")
            
            self.hass.async_create_task(confirm_and_update())
            
        except Exception as e:
            _LOGGER.error(f"Ошибка включения ШИМ света: {e}")
            raise

    async def async_turn_off(self, **kwargs):
        """Turn the entity off."""
        await self.async_turn_on(brightness=0)

    @property
    def extra_state_attributes(self):
        """Дополнительные атрибуты для диагностики."""
        attributes = super().extra_state_attributes
        attributes.update({
            "original_name": self._original_name,
            "port_type": "pwm"
        })
        return attributes


class LightExtraMegaD(CoordinatorEntity, LightEntity):
    """Дополнительный релейный свет."""

    _attr_supported_color_modes = {ColorMode.ONOFF}
    _attr_color_mode = ColorMode.ONOFF
    # ✅ ИЗМЕНЕНО: Устанавливаем False, так как используем полное имя
    _attr_has_entity_name = False  # Было True

    def __init__(
            self, 
            coordinator: MegaDCoordinator,
            port: I2CExtraPCA9685 | I2CExtraMCP230xx,
            config_extra_port: PCA9685RelayConfig | MCP230RelayConfig,
            unique_id: str
    ) -> None:
        super().__init__(coordinator)
        self._coordinator: MegaDCoordinator = coordinator
        self._megad: MegaD = coordinator.megad
        self._port = port
        self._config_extra_port = config_extra_port
        self._attr_unique_id = unique_id
        
        # ✅ ИСПРАВЛЕНИЕ: Используем метод координатора для очистки имени
        port_name = config_extra_port.name
        self._attr_name = coordinator.create_clean_port_name(port_name, config_extra_port.id, "Light")
        self._original_name = port_name
        
        # ✅ КРИТИЧЕСКОЕ ИСПРАВЛЕНИЕ: Для I2C сенсоров используем ТОТ ЖЕ device_info
        # Все сущности должны использовать один device_info для группировки
        suggested_area = coordinator.extract_area_from_port_name(port_name) if hasattr(coordinator, 'extract_area_from_port_name') else None
        self._attr_device_info = coordinator.entity_device_info(
            self._attr_name,
            f"MegaD-{self._megad.id} Extra Relay Light",
            entity_type="extra_relay_light",
            port_id=port.conf.id,
            extra_port_id=config_extra_port.id,
            suggested_area=suggested_area
        )
        
        # Для отслеживания команд
        self._command_timestamp = 0
        
        # ✅ Сохраняем ID портов для диагностики
        self._base_port_id = port.conf.id
        self._extra_port_id = config_extra_port.id

    async def async_added_to_hass(self):
        """Когда сущность добавлена в HA."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_listener(self._handle_coordinator_update)
        )
        self._handle_coordinator_update()
        
        # ✅ Логирование для отладки
        _LOGGER.debug(f"LightExtraMegaD добавлен: {self.entity_id}, "
                     f"base_port {self._base_port_id}, extra_port {self._extra_port_id}")
    
    @property
    def device_info(self):
        """Return device info."""
        return self._attr_device_info

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self._coordinator.last_update_success
    
    @property
    def should_poll(self) -> bool:
        """Return False as we get updates via coordinator."""
        return False
    
    @property
    def is_on(self) -> bool | None:
        """Return true if the light is on."""
        port_state = self._port.state.get(self._config_extra_port.id)
        if port_state is None:
            return None
        
        inverse = getattr(self._config_extra_port, 'inverse', False)
        is_on = not bool(port_state) if inverse else bool(port_state)
        
        return is_on

    async def _switch_port(self, command: int):
        """Отправка команды переключения дополнительного порта."""
        try:
            # Фиксируем время команды
            self._command_timestamp = datetime.now().timestamp()
            
            # Формируем ID порта для отправки команды
            ext_port_id = f'{self._port.conf.id}e{self._config_extra_port.id}'
            
            # Отправляем команду на контроллер
            await self._megad.set_port(ext_port_id, command)
            
            # ✅ ОТМЕЧАЕМ ПОЛУЧЕНИЕ ДАННЫХ ДЛЯ WATCHDOG
            if hasattr(self._coordinator, 'watchdog') and self._coordinator.watchdog:
                self._coordinator.watchdog.mark_data_received()
                self._coordinator.watchdog.mark_feedback_event({
                    "type": "extra_light_switch",
                    "entity_id": self.entity_id,
                    "base_port_id": self._port.conf.id,
                    "extra_port_id": self._config_extra_port.id,
                    "command": command,
                    "light_type": "extra_relay"
                })
            
            # Устанавливаем ожидаемое состояние немедленно
            expected_state = command
            inverse = getattr(self._config_extra_port, 'inverse', False)
            if inverse:
                expected_state = 1 if command == 0 else 0
            
            # Обновляем состояние через координатор
            update_data = {f'ext{self._config_extra_port.id}': expected_state}
            await self._coordinator.update_port_state(
                self._port.conf.id,
                update_data
            )
            
            # Немедленно обновляем UI с ожидаемым состоянием
            self.async_write_ha_state()
            
            # Запрашиваем подтверждение состояния
            async def confirm_and_update():
                try:
                    await asyncio.sleep(0.3)
                    
                    # Запрашиваем обновление состояния через координатор
                    await self._coordinator.async_request_refresh()
                    
                    # Обновляем UI с актуальным состоянием
                    self.async_write_ha_state()
                    _LOGGER.debug(f"Доп. порт {self._config_extra_port.id}: состояние подтверждено")
                
                except Exception as e:
                    _LOGGER.debug(f"Доп. порт {self._config_extra_port.id}: не удалось подтвердить состояние: {e}")
            
            self.hass.async_create_task(confirm_and_update())
            
        except Exception as e:
            _LOGGER.error(f'Ошибка управления доп. портом {self._config_extra_port.id}: {e}')
            raise

    def _handle_coordinator_update(self):
        """Обработчик обновления координатора."""
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs):
        """Turn the light on."""
        inverse = getattr(self._config_extra_port, 'inverse', False)
        command = 0 if inverse else 1
        await self._switch_port(command)

    async def async_turn_off(self, **kwargs):
        """Turn the light off."""
        inverse = getattr(self._config_extra_port, 'inverse', False)
        command = 1 if inverse else 0
        await self._switch_port(command)

    @property
    def extra_state_attributes(self):
        """Дополнительные атрибуты для диагностики."""
        return {
            "base_port_id": self._base_port_id,
            "extra_port_id": self._extra_port_id,
            "megad_id": self._megad.id,
            "device_type": "extra_relay_light",
            "original_name": self._original_name
        }


class LightExtraPWMMegaD(LightPWMBaseMegaD):
    """Дополнительный ШИМ свет."""

    def __init__(
            self, 
            coordinator: MegaDCoordinator,
            port: I2CExtraPCA9685,
            config_extra_port: PCA9685PWMConfig,
            unique_id: str
    ) -> None:
        # ✅ ИСПРАВЛЕНИЕ: Используем метод координатора для очистки имени
        port_name = config_extra_port.name
        cleaned_name = coordinator.create_clean_port_name(port_name, config_extra_port.id, "Extra Light")
        
        super().__init__(
            coordinator,
            config_extra_port.min_value,
            config_extra_port.max_value,
            cleaned_name,  # ✅ Передаем очищенное имя
            unique_id,
            port_id=port.conf.id,
            extra_port_id=config_extra_port.id
        )
        self._megad: MegaD = coordinator.megad
        self._port: I2CExtraPCA9685 = port
        self._config_extra_port = config_extra_port
        self._original_name = port_name
        
        # ✅ ИСПРАВЛЕНИЕ: Используем device_base_info с областью
        suggested_area = coordinator.extract_area_from_port_name(port_name) if hasattr(coordinator, 'extract_area_from_port_name') else None
        self._attr_device_info = coordinator.entity_device_info(
            self._attr_name,
            f"MegaD-{self._megad.id} Extra PWM Light",
            entity_type="extra_pwm_light",
            port_id=port.conf.id,
            extra_port_id=config_extra_port.id,
            suggested_area=suggested_area
        )
        # ✅ ИЗМЕНЕНО: Устанавливаем False, так как используем полное имя
        self._attr_has_entity_name = False  # Было в базовом классе True

    @property
    def device_info(self):
        """Return device info."""
        return self._attr_device_info

    @property
    def brightness(self) -> int | None:
        """Return the brightness of this light between 0..255."""
        port_state = self._port.state.get(self._config_extra_port.id)
        if port_state is None:
            return None
        
        return self.device_to_ha_brightness(port_state)

    @property
    def is_on(self) -> bool | None:
        """Return true if the light is on."""
        brightness = self.brightness
        if brightness is None:
            return None
        
        return brightness > 0

    async def async_turn_on(self, brightness: int = 255, **kwargs):
        """Turn the entity on."""
        try:
            # Фиксируем время команды
            self._command_timestamp = datetime.now().timestamp()
            
            # Определяем яркость для установки
            target_ha_brightness = brightness if brightness is not None else 255
            
            # Конвертируем в значение устройства
            device_value = self.ha_to_device_brightness(target_ha_brightness)
            
            # Формируем ID порта для отправки команды
            ext_port_id = f'{self._port.conf.id}e{self._config_extra_port.id}'
            
            # Отправляем команду на контроллер
            await self._megad.set_port(ext_port_id, device_value)
            
            # ✅ ОТМЕЧАЕМ ПОЛУЧЕНИЕ ДАННЫХ ДЛЯ WATCHDOG
            if hasattr(self._coordinator, 'watchdog') and self._coordinator.watchdog:
                self._coordinator.watchdog.mark_data_received()
                self._coordinator.watchdog.mark_feedback_event({
                    "type": "extra_pwm_light_switch",
                    "entity_id": self.entity_id,
                    "base_port_id": self._port.conf.id,
                    "extra_port_id": self._config_extra_port.id,
                    "brightness": target_ha_brightness,
                    "device_value": device_value,
                    "light_type": "extra_pwm"
                })
            
            # Устанавливаем ожидаемое состояние немедленно
            update_data = {f'ext{self._config_extra_port.id}': device_value}
            await self._coordinator.update_port_state(
                self._port.conf.id,
                update_data
            )
            
            # Немедленно обновляем UI с ожидаемым состоянием
            self.async_write_ha_state()
            
            # Запрашиваем подтверждение состояния
            async def confirm_and_update():
                try:
                    await asyncio.sleep(0.3)
                    
                    # Запрашиваем обновление состояния через координатор
                    await self._coordinator.async_request_refresh()
                    
                    # Обновляем UI с актуальным состоянием
                    self.async_write_ha_state()
                    _LOGGER.debug(f"Доп. ШИМ порт {self._config_extra_port.id}: яркость подтверждена")
                
                except Exception as e:
                    _LOGGER.debug(f"Доп. ШИМ порт {self._config_extra_port.id}: не удалось подтвердить яркость: {e}")
            
            self.hass.async_create_task(confirm_and_update())
            
        except Exception as e:
            _LOGGER.error(f"Ошибка включения доп. ШИМ света: {e}")
            raise

    async def async_turn_off(self, **kwargs):
        """Turn the entity off."""
        await self.async_turn_on(brightness=0)

    @property
    def extra_state_attributes(self):
        """Дополнительные атрибуты для диагностики."""
        attributes = super().extra_state_attributes
        attributes.update({
            "original_name": self._original_name,
            "port_type": "extra_pwm",
            "base_port_id": self._port.conf.id,
            "extra_port_id": self._config_extra_port.id
        })
        return attributes
    
    def _handle_coordinator_update(self):
        """Обработчик обновления координатора."""
        self.async_write_ha_state()