import logging
import asyncio
from math import floor
from typing import Optional, Any
from datetime import datetime, timedelta

from homeassistant.components.light import LightEntity, ColorMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import MegaDCoordinator
from .const import DOMAIN, ENTRIES, CURRENT_ENTITY_IDS
from .core.base_ports import (
    ReleyPortOut, PWMPortOut, I2CExtraPCA9685, I2CExtraMCP230xx
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
    
    lights = []
    
    for port in megad.ports:
        # Релейные порты создаем как светильники
        if isinstance(port, ReleyPortOut):
            if hasattr(port.conf, 'type_port') and port.conf.type_port == TypePortMegaD.OUT:
                unique_id = f'{entry_id}-{megad.id}-{port.conf.id}-light'
                namespace = getattr(port.conf, 'namespace', None)
                lights.append(LightRelayMegaD(
                    coordinator, port, unique_id, namespace=namespace
                ))
        
        # ШИМ порты создаем как диммируемые светильники
        elif isinstance(port, PWMPortOut):
            unique_id = f'{entry_id}-{megad.id}-{port.conf.id}-light'
            namespace = getattr(port.conf, 'namespace', None)
            lights.append(LightPWMMegaD(
                coordinator, port, unique_id, namespace=namespace
            ))
        
        # Дополнительные релейные порты на расширителях PCA9685
        elif isinstance(port, I2CExtraPCA9685):
            for config in port.extra_confs:
                if (isinstance(config, PCA9685RelayConfig) and
                    hasattr(config, 'type_port') and config.type_port == TypePortMegaD.OUT):
                    unique_id = (f'{entry_id}-{megad.id}-{port.conf.id}-'
                                 f'ext{config.id}-light')
                    namespace = getattr(config, 'namespace', None)
                    lights.append(LightExtraMegaD(
                        coordinator, port, config, unique_id, namespace=namespace
                    ))
                # Дополнительные ШИМ порты на расширителях
                elif isinstance(config, PCA9685PWMConfig):
                    unique_id = (f'{entry_id}-{megad.id}-{port.conf.id}-'
                                 f'ext{config.id}-light')
                    namespace = getattr(config, 'namespace', None)
                    lights.append(LightExtraPWMMegaD(
                        coordinator, port, config, unique_id, namespace=namespace
                    ))
        
        # Дополнительные релейные порты на MCP расширителях
        elif isinstance(port, I2CExtraMCP230xx):
            for config in port.extra_confs:
                if (isinstance(config, MCP230RelayConfig) and
                    hasattr(config, 'type_port') and config.type_port == TypePortMegaD.OUT):
                    unique_id = (f'{entry_id}-{megad.id}-{port.conf.id}-'
                                 f'ext{config.id}-light')
                    namespace = getattr(config, 'namespace', None)
                    lights.append(LightExtraMegaD(
                        coordinator, port, config, unique_id, namespace=namespace
                    ))
    
    if not lights:
        _LOGGER.warning(f"Не найдено сущностей освещения для MegaD {megad.id}")
        return
    
    # Регистрируем ID сущностей
    if CURRENT_ENTITY_IDS not in hass.data[DOMAIN]:
        hass.data[DOMAIN][CURRENT_ENTITY_IDS] = {}
    
    if entry_id not in hass.data[DOMAIN][CURRENT_ENTITY_IDS]:
        hass.data[DOMAIN][CURRENT_ENTITY_IDS][entry_id] = []
    
    for light in lights:
        hass.data[DOMAIN][CURRENT_ENTITY_IDS][entry_id].append(light.unique_id)
    
    async_add_entities(lights)
    _LOGGER.info(f"Добавлено {len(lights)} сущностей освещения для MegaD {megad.id}")


class LightRelayMegaD(CoordinatorEntity, LightEntity):
    """Релейный свет."""

    _attr_supported_color_modes = {ColorMode.ONOFF}
    _attr_color_mode = ColorMode.ONOFF
    _attr_has_entity_name = True

    def __init__(
            self, 
            coordinator: MegaDCoordinator, 
            port: ReleyPortOut,
            unique_id: str,
            namespace: Optional[str] = None
    ) -> None:
        super().__init__(coordinator)
        self._coordinator: MegaDCoordinator = coordinator
        self._megad: MegaD = coordinator.megad
        self._port: ReleyPortOut = port
        self._attr_unique_id = unique_id
        self._attr_name = port.conf.name
        self._namespace = namespace
        
        # Для отслеживания команд
        self._command_timestamp = 0
        
        # Формируем entity_id с учетом пространства
        if namespace:
            self.entity_id = f'light.{namespace}_{self._megad.id}_port{port.conf.id}'
        else:
            self.entity_id = f'light.{self._megad.id}_port{port.conf.id}'

    async def async_added_to_hass(self):
        """Когда сущность добавлена в HA."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_listener(self._handle_coordinator_update)
        )
        self._handle_coordinator_update()

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
            
            # УСТАНОВЛИВАЕМ ОЖИДАЕМОЕ СОСТОЯНИЕ НЕМЕДЛЕННО
            # Используем метод координатора для обновления состояния
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
                    # Даем время на выполнение команды
                    await asyncio.sleep(0.2)
                    
                    # Получаем актуальное состояние с контроллера
                    actual_value = await self._megad.get_port(self._port.conf.id)
                    if actual_value is not None:
                        # Если состояние отличается от ожидаемого, обновляем
                        if actual_value != expected_state:
                            await self._coordinator.update_port_state(
                                self._port.conf.id, 
                                actual_value
                            )
                            _LOGGER.debug(
                                f"Порт {self._port.conf.id}: состояние обновлено с {expected_state} на {actual_value}"
                            )
                        
                        # Обновляем UI с актуальным состоянием
                        self.async_write_ha_state()
                        _LOGGER.debug(f"Порт {self._port.conf.id}: состояние подтверждено")
                
                except Exception as e:
                    _LOGGER.warning(f"Ошибка подтверждения состояния порта {self._port.conf.id}: {e}")
            
            # Запускаем задачу подтверждения
            self.hass.async_create_task(confirm_and_update())
            
        except Exception as e:
            _LOGGER.error(f'Ошибка управления портом {self._port.conf.id}: {e}')
            raise

    def _handle_coordinator_update(self):
        """Обработчик обновления координатора."""
        # Всегда обновляем UI при обновлении от координатора
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


class LightPWMBaseMegaD(CoordinatorEntity, LightEntity):
    """Базовый класс для освещения с ШИМ."""

    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_has_entity_name = True

    def __init__(
            self, 
            coordinator: MegaDCoordinator,
            min_brightness,
            max_brightness,
            namespace: Optional[str] = None
    ) -> None:
        super().__init__(coordinator)
        self._coordinator: MegaDCoordinator = coordinator
        self.min_brightness = min_brightness
        self.max_brightness = max_brightness
        self._namespace = namespace
        
        # Для отслеживания команд
        self._command_timestamp = 0

    async def async_added_to_hass(self):
        """Когда сущность добавлена в HA."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_listener(self._handle_coordinator_update)
        )
        self._handle_coordinator_update()

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


class LightPWMMegaD(LightPWMBaseMegaD):
    """ШИМ свет."""

    def __init__(
            self, 
            coordinator: MegaDCoordinator, 
            port: PWMPortOut,
            unique_id: str,
            namespace: Optional[str] = None
    ) -> None:
        super().__init__(coordinator, port.conf.min_value, 255, namespace=namespace)
        self._megad: MegaD = coordinator.megad
        self._port: PWMPortOut = port
        self._attr_unique_id = unique_id
        self._attr_name = port.conf.name
        
        # Формируем entity_id с учетом пространства
        if namespace:
            self.entity_id = f'light.{namespace}_{self._megad.id}_port{port.conf.id}'
        else:
            self.entity_id = f'light.{self._megad.id}_port{port.conf.id}'

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
            
            # УСТАНОВЛИВАЕМ ОЖИДАЕМОЕ СОСТОЯНИЕ НЕМЕДЛЕННО
            # Используем метод координатора для обновления состояния
            await self._coordinator.update_port_state(
                self._port.conf.id, 
                device_value
            )
            
            # Немедленно обновляем UI с ожидаемым состоянием
            self.async_write_ha_state()
            
            # Запрашиваем подтверждение состояния
            async def confirm_and_update():
                try:
                    # Даем время на выполнение команды
                    await asyncio.sleep(0.2)
                    
                    # Получаем актуальное состояние с контроллера
                    actual_value = await self._megad.get_port(self._port.conf.id)
                    if actual_value is not None:
                        # Если состояние отличается от ожидаемого, обновляем
                        if actual_value != device_value:
                            await self._coordinator.update_port_state(
                                self._port.conf.id, 
                                actual_value
                            )
                            _LOGGER.debug(
                                f"Порт {self._port.conf.id}: яркость обновлена с {device_value} на {actual_value}"
                            )
                        
                        # Обновляем UI с актуальным состоянием
                        self.async_write_ha_state()
                        _LOGGER.debug(f"Порт {self._port.conf.id}: яркость подтверждена")
                
                except Exception as e:
                    _LOGGER.warning(f"Ошибка подтверждения яркости порта {self._port.conf.id}: {e}")
            
            # Запускаем задачу подтверждения
            self.hass.async_create_task(confirm_and_update())
            
        except Exception as e:
            _LOGGER.error(f"Ошибка включения ШИМ света: {e}")
            raise

    async def async_turn_off(self, **kwargs):
        """Turn the entity off."""
        await self.async_turn_on(brightness=0)


class LightExtraMegaD(CoordinatorEntity, LightEntity):
    """Дополнительный релейный свет."""

    _attr_supported_color_modes = {ColorMode.ONOFF}
    _attr_color_mode = ColorMode.ONOFF
    _attr_has_entity_name = True

    def __init__(
            self, 
            coordinator: MegaDCoordinator,
            port: I2CExtraPCA9685 | I2CExtraMCP230xx,
            config_extra_port: PCA9685RelayConfig | MCP230RelayConfig,
            unique_id: str,
            namespace: Optional[str] = None
    ) -> None:
        super().__init__(coordinator)
        self._coordinator: MegaDCoordinator = coordinator
        self._megad: MegaD = coordinator.megad
        self._port = port
        self._config_extra_port = config_extra_port
        self._attr_unique_id = unique_id
        self._attr_name = config_extra_port.name
        self._namespace = namespace
        
        # Для отслеживания команд
        self._command_timestamp = 0
        
        # Формируем entity_id с учетом пространства
        base_entity_id = f'{self._megad.id}_port{port.conf.id}_ext{config_extra_port.id}'
        if namespace:
            self.entity_id = f'light.{namespace}_{base_entity_id}'
        else:
            self.entity_id = f'light.{base_entity_id}'

    async def async_added_to_hass(self):
        """Когда сущность добавлена в HA."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_listener(self._handle_coordinator_update)
        )
        self._handle_coordinator_update()
    
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
            
            # УСТАНОВЛИВАЕМ ОЖИДАЕМОЕ СОСТОЯНИЕ НЕМЕДЛЕННО
            expected_state = command
            inverse = getattr(self._config_extra_port, 'inverse', False)
            if inverse:
                expected_state = 1 if command == 0 else 0
            
            # Обновляем состояние через координатор
            # Формируем данные для обновления состояния дополнительного порта
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
                    # Даем время на выполнение команды
                    await asyncio.sleep(0.3)
                    
                    # Запрашиваем обновление состояния через координатор
                    await self._coordinator.async_request_refresh()
                    
                    # Обновляем UI с актуальным состоянием
                    self.async_write_ha_state()
                    _LOGGER.debug(f"Доп. порт {self._config_extra_port.id}: состояние подтверждено")
                
                except Exception as e:
                    _LOGGER.warning(
                        f"Ошибка подтверждения состояния доп. порта "
                        f"{self._config_extra_port.id}: {e}"
                    )
            
            # Запускаем задачу подтверждения
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


class LightExtraPWMMegaD(LightPWMBaseMegaD):
    """Дополнительный ШИМ свет."""

    def __init__(
            self, 
            coordinator: MegaDCoordinator,
            port: I2CExtraPCA9685,
            config_extra_port: PCA9685PWMConfig,
            unique_id: str,
            namespace: Optional[str] = None
    ) -> None:
        super().__init__(
            coordinator,
            config_extra_port.min_value,
            config_extra_port.max_value,
            namespace=namespace
        )
        self._megad: MegaD = coordinator.megad
        self._port: I2CExtraPCA9685 = port
        self._config_extra_port = config_extra_port
        self._attr_unique_id = unique_id
        self._attr_name = config_extra_port.name
        
        # Формируем entity_id с учетом пространства
        base_entity_id = f'{self._megad.id}_port{port.conf.id}_ext{config_extra_port.id}'
        if namespace:
            self.entity_id = f'light.{namespace}_{base_entity_id}'
        else:
            self.entity_id = f'light.{base_entity_id}'

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
            
            # УСТАНОВЛИВАЕМ ОЖИДАЕМОЕ СОСТОЯНИЕ НЕМЕДЛЕННО
            # Обновляем состояние через координатор
            # Формируем данные для обновления состояния дополнительного порта
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
                    # Даем время на выполнение команды
                    await asyncio.sleep(0.3)
                    
                    # Запрашиваем обновление состояния через координатор
                    await self._coordinator.async_request_refresh()
                    
                    # Обновляем UI с актуальным состоянием
                    self.async_write_ha_state()
                    _LOGGER.debug(f"Доп. ШИМ порт {self._config_extra_port.id}: яркость подтверждена")
                
                except Exception as e:
                    _LOGGER.warning(
                        f"Ошибка подтверждения яркости доп. порта "
                        f"{self._config_extra_port.id}: {e}"
                    )
            
            # Запускаем задачу подтверждения
            self.hass.async_create_task(confirm_and_update())
            
        except Exception as e:
            _LOGGER.error(f"Ошибка включения доп. ШИМ света: {e}")
            raise

    async def async_turn_off(self, **kwargs):
        """Turn the entity off."""
        await self.async_turn_on(brightness=0)