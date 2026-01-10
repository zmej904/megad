import logging
import asyncio
from math import floor
from typing import Optional, Any

from propcache import cached_property

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from . import MegaDCoordinator
from .const import DOMAIN, PORT_COMMAND, ENTRIES, CURRENT_ENTITY_IDS
from .core.base_ports import (
    RelayPortOut, PWMPortOut, I2CExtraPCA9685, I2CExtraMCP230xx
)
from .core.entities import PortOutEntity, PortOutExtraEntity
from .core.enums import DeviceClassControl
from .core.enums import ModeOutMegaD
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
    entry_id = config_entry.entry_id
    coordinator = hass.data[DOMAIN][ENTRIES][entry_id]
    megad = coordinator.megad

    fans = []
    
    # ✅ ДЛЯ ОТЛАДКИ
    _LOGGER.debug(f"=== СОЗДАНИЕ FAN СУЩНОСТЕЙ ДЛЯ MegaD-{megad.id} ===")
    
    # ✅ ЛОГИКА: Fan сущности создаются отдельно через device_class
    # Так как в настройках контроллера нет device_class, вероятно
    # Fan сущности создаются через другой механизм
    
    # Проверяем, есть ли в конфигурации специальные настройки для Fan
    for port in megad.ports:
        port_mode = getattr(port.conf, 'mode', None)
        port_name = getattr(port.conf, 'name', '').lower()
        
        _LOGGER.debug(f"Порт {port.conf.id} '{port.conf.name}': mode={port_mode}")
        
        # ✅ Если имя содержит "fan", "вентилятор", "vent" - создаем Fan
        # (это временное решение, так как device_class не используется)
        fan_keywords = ['fan', 'вентилятор', 'vent', 'кулер']
        is_fan_by_name = any(keyword in port_name for keyword in fan_keywords)
        
        if isinstance(port, RelayPortOut):
            # Релейные вентиляторы (Mode=SW)
            if port_mode == ModeOutMegaD.SW and is_fan_by_name:
                unique_id = f'{entry_id}-{megad.id}-{port.conf.id}-fan'
                fans.append(FanMegaD(coordinator, port, unique_id))
                _LOGGER.info(f"✅ СОЗДАН FanRelay: порт {port.conf.id} (по имени)")
        
        elif isinstance(port, PWMPortOut):
            # ШИМ вентиляторы (Mode=PWM)
            if port_mode == ModeOutMegaD.PWM and is_fan_by_name:
                unique_id = f'{entry_id}-{megad.id}-{port.conf.id}-fan'
                fans.append(FanPWMMegaD(coordinator, port, unique_id))
                _LOGGER.info(f"✅ СОЗДАН FanPWM: порт {port.conf.id} (по имени)")
    
    for fan in fans:
        hass.data[DOMAIN][CURRENT_ENTITY_IDS][entry_id].append(
            fan.unique_id)
    
    if fans:
        async_add_entities(fans)
        _LOGGER.info(f'Добавлено {len(fans)} FAN сущностей для MegaD {megad.id}')

class FanMegaD(PortOutEntity, FanEntity):
    """Релейный вентилятор с поддержкой assumed_state."""

    _attr_supported_features = (FanEntityFeature.TURN_ON
                                | FanEntityFeature.TURN_OFF)

    def __init__(
            self, coordinator: MegaDCoordinator, port: RelayPortOut,
            unique_id: str
    ) -> None:
        # PortOutEntity уже содержит всю логику assumed_state
        super().__init__(coordinator, port, unique_id)
        self._megad: MegaD = coordinator.megad
        self._port: RelayPortOut = port
        self._attr_unique_id = unique_id
        self._attr_name = port.conf.name
        self._attr_assumed_state = True  # Включаем assumed_state
        
        # Индивидуальный device_info для релейного вентилятора
        self._attr_device_info = coordinator.entity_device_info(
            unique_id,
            port.conf.name,
            f"MegaD-{self._megad.id} Relay Fan"
        )
        
        self._attr_has_entity_name = False

    def __repr__(self) -> str:
        if not self.hass:
            return f"<Fan entity {self.entity_id}>"
        return super().__repr__()

    async def async_added_to_hass(self):
        """Когда сущность добавлена в HA."""
        await super().async_added_to_hass()
        # Слушаем обновления координатора
        self.async_on_remove(
            self.coordinator.async_add_listener(self._handle_coordinator_update)
        )

    @property
    def name(self) -> str:
        return self._attr_name

    @property
    def unique_id(self) -> str:
        return self._attr_unique_id

    @property
    def assumed_state(self) -> bool:
        """Return True if the state is assumed."""
        return self._attr_assumed_state

    @property
    def percentage(self) -> int | None:
        """Return the current speed percentage."""
        # Релейный вентилятор всегда 100% когда включен
        if self.is_on:
            return 100
        return 0

    @property
    def is_on(self) -> bool | None:
        """Return true if the fan is on."""
        # Используем родительскую логику из PortOutEntity
        return super().is_on

    async def async_turn_on(
            self, speed: Optional[str] = None,
            percentage: Optional[int] = None,
            preset_mode: Optional[str] = None, **kwargs: Any
    ) -> None:
        """Turn the entity on."""
        _LOGGER.debug(f"Включение релейного fan порта {self._port.conf.id}, инверсия={getattr(self._port.conf, 'inverse', False)}")
        
        try:
            # Учитываем инверсию порта
            if hasattr(self._port.conf, 'inverse') and self._port.conf.inverse:
                await self._switch_port(0)  # Инвертированный порт: 0 = включено
            else:
                await self._switch_port(1)  # Обычный порт: 1 = включено
            
            _LOGGER.info(f"Релейный fan порт {self._port.conf.id} включен")
            
        except Exception as e:
            _LOGGER.error(f"Ошибка включения релейного fan порта {self._port.conf.id}: {e}")
            raise

    async def async_turn_off(
            self, speed: Optional[str] = None,
            percentage: Optional[int] = None,
            preset_mode: Optional[str] = None, **kwargs: Any
    ) -> None:
        """Turn the entity off."""
        _LOGGER.debug(f"Выключение релейного fan порта {self._port.conf.id}, инверсия={getattr(self._port.conf, 'inverse', False)}")
        
        try:
            # Учитываем инверсию порта
            if hasattr(self._port.conf, 'inverse') and self._port.conf.inverse:
                await self._switch_port(1)  # Инвертированный порт: 1 = выключено
            else:
                await self._switch_port(0)  # Обычный порт: 0 = выключено
            
            _LOGGER.info(f"Релейный fan порт {self._port.conf.id} выключен")
            
        except Exception as e:
            _LOGGER.error(f"Ошибка выключения релейного fan порта {self._port.conf.id}: {e}")
            raise

    async def async_toggle(
            self, speed: Optional[str] = None,
            percentage: Optional[int] = None,
            preset_mode: Optional[str] = None, **kwargs: Any
    ) -> None:
        """Toggle the entity."""
        _LOGGER.debug(f"Переключение релейного fan порта {self._port.conf.id}")
        
        try:
            await self._switch_port(2)  # 2 = toggle
            
            _LOGGER.info(f"Релейный fan порт {self._port.conf.id} переключен")
            
        except Exception as e:
            _LOGGER.error(f"Ошибка переключения релейного fan порта {self._port.conf.id}: {e}")
            raise


class FanPWMBaseMegaD(CoordinatorEntity, FanEntity):
    """Базовый класс для вентиляции с ШИМ с поддержкой assumed_state"""

    _attr_supported_features = (FanEntityFeature.TURN_ON
                                | FanEntityFeature.TURN_OFF
                                | FanEntityFeature.SET_SPEED)

    def __init__(
            self, coordinator: MegaDCoordinator,
            min_speed,
            max_speed
    ) -> None:
        super().__init__(coordinator)
        self.min_speed = min_speed
        self.max_speed = max_speed
        self._attr_assumed_state = True  # Включаем assumed_state
        self._last_confirmed_speed = 0
        self._assumed_speed = 0

    def __repr__(self) -> str:
        if not self.hass:
            return f"<Fan entity {self.entity_id}>"
        return super().__repr__()

    @property
    def assumed_state(self) -> bool:
        """Return True if the state is assumed."""
        return self._attr_assumed_state

    def device_to_ha_speed(self, device_value) -> int:
        if device_value < self.min_speed or device_value == 0:
            return 0
        elif device_value == self.min_speed:
            return 1
        else:
            value = (device_value - self.min_speed) / (
                (self.max_speed - self.min_speed)) * 100
            return floor(value + 0.5)

    def ha_to_device_speed(self, ha_value) -> int:
        if ha_value == 0:
            return 0
        elif ha_value == 1:
            return self.min_speed
        else:
            value = ha_value / 100 * (
                    self.max_speed - self.min_speed) + self.min_speed
            return floor(value + 0.5)
    
    def _clear_cached_state(self):
        """Очистить все кэшированные состояния."""
        self._last_confirmed_speed = 0
        self._assumed_speed = 0
        self._attr_assumed_state = False
        
    async def confirm_state_from_device(self):
        """Подтвердить состояние с устройства."""
        _LOGGER.debug(f"ШИМ вентилятор: подтверждение состояния с устройства")
        await self.coordinator.async_request_refresh()


class FanPWMMegaD(FanPWMBaseMegaD):
    """ШИМ вентилятор с поддержкой assumed_state."""

    def __init__(
            self, coordinator: MegaDCoordinator, port: PWMPortOut,
            unique_id: str
    ) -> None:
        super().__init__(coordinator, port.conf.min_value, 255)
        self._coordinator: MegaDCoordinator = coordinator
        self._megad: MegaD = coordinator.megad
        self._port: PWMPortOut = port
        self._attr_unique_id = unique_id
        self._attr_name = port.conf.name
        
        # Индивидуальный device_info для ШИМ вентилятора
        self._attr_device_info = coordinator.entity_device_info(
            unique_id,
            port.conf.name,
            f"MegaD-{self._megad.id} PWM Fan"
        )
        
        self._attr_has_entity_name = False
        
        # Инициализируем состояние
        self._update_state_from_port()

    async def async_added_to_hass(self):
        """Когда сущность добавлена в HA."""
        await super().async_added_to_hass()
        # Слушаем обновления координатора
        self.async_on_remove(
            self.coordinator.async_add_listener(self._handle_coordinator_update)
        )

    def _update_state_from_port(self):
        """Обновить состояние из данных порта."""
        port_state = self._port.state
        if port_state is None:
            self._last_confirmed_speed = 0
        else:
            self._last_confirmed_speed = self.device_to_ha_speed(port_state)
        
        self._attr_assumed_state = False
        _LOGGER.debug(f"ШИМ вентилятор порт {self._port.conf.id}: обновлено состояние из порта: скорость={self._last_confirmed_speed}%")

    async def set_value_port(self, value):
        """Установка значения порта с поддержкой assumed_state"""
        try:
            # Устанавливаем предполагаемое состояние
            ha_speed = self.device_to_ha_speed(value)
            self._assumed_speed = ha_speed
            self._attr_assumed_state = True
            
            _LOGGER.debug(f"ШИМ вентилятор порт {self._port.conf.id}: устанавливаем предполагаемое состояние: скорость={ha_speed}%")
            
            # Немедленно обновляем UI
            self.async_write_ha_state()
            
            # Отправляем команду на контроллер
            await self._megad.set_port(self._port.conf.id, value)
            
            # Триггерим обновление координатора
            await self._coordinator.update_port_state(
                self._port.conf.id, value
            )
            
            # Запланировать проверку состояния через 2 секунды
            self.hass.async_create_task(self._verify_state_after_command(value))
            
            _LOGGER.debug(f"Установлено значение ШИМ fan порта {self._port.conf.id}: {value}")
            
        except Exception as e:
            _LOGGER.error(f'Ошибка управления ШИМ fan портом '
                          f'{self._port.conf.id}: {e}')
            self._attr_assumed_state = False
            raise

    async def _verify_state_after_command(self, value_sent: int):
        """Проверка состояния после отправки команды."""
        try:
            # Ждем выполнения команды
            await asyncio.sleep(2)
            
            # Запрашиваем обновление данных
            await self._coordinator.async_request_refresh()
            
            # После обновления данных состояние будет подтверждено
            self._attr_assumed_state = False
            self.async_write_ha_state()
            
            _LOGGER.debug(f"ШИМ вентилятор порт {self._port.conf.id}: состояние подтверждено после команды {value_sent}")
            
        except Exception as e:
            _LOGGER.debug(f"ШИМ вентилятор порт {self._port.conf.id}: не удалось подтвердить состояние: {e}")
            # Оставляем состояние как предполагаемое

    @property
    def name(self) -> str:
        return self._attr_name

    @property
    def unique_id(self) -> str:
        return self._attr_unique_id

    @property
    def percentage(self) -> int | None:
        """Return the current speed as a percentage."""
        # Если состояние предполагаемое, возвращаем предполагаемую скорость
        if self.assumed_state:
            _LOGGER.debug(f"ШИМ вентилятор порт {self._port.conf.id}: возвращаем предполагаемую скорость: {self._assumed_speed}%")
            return self._assumed_speed
        
        # Иначе получаем скорость из порта
        self._update_state_from_port()
        _LOGGER.debug(f"ШИМ вентилятор порт {self._port.conf.id}: возвращаем подтвержденную скорость: {self._last_confirmed_speed}%")
        return self._last_confirmed_speed

    @property
    def is_on(self) -> bool | None:
        """Return true if the fan is on."""
        # Определяем состояние на основе скорости
        current_speed = self.percentage or 0
        is_on = current_speed > 0
        _LOGGER.debug(f"Состояние ШИМ вентилятора порт {self._port.conf.id}: скорость={current_speed}%, включен={is_on}")
        return is_on

    def _handle_coordinator_update(self):
        """Обработчик обновления координатора."""
        # При получении свежих данных с контроллера обновляем состояние из порта
        self._update_state_from_port()
        super()._handle_coordinator_update()

    async def async_set_percentage(self, percentage: int) -> None:
        """Set the speed of the fan, as a percentage."""
        try:
            if percentage == 0:
                await self.async_turn_off()
            else:
                device_value = self.ha_to_device_speed(percentage)
                await self.set_value_port(device_value)
                
        except Exception as e:
            _LOGGER.error(f"Ошибка установки скорости fan: {e}")
            raise

    async def async_turn_on(
            self, speed: Optional[str] = None,
            percentage: Optional[int] = None,
            preset_mode: Optional[str] = None, **kwargs: Any
    ) -> None:
        """Turn the entity on."""
        try:
            if percentage is not None:
                device_value = self.ha_to_device_speed(percentage)
                await self.set_value_port(device_value)
            else:
                await self.set_value_port(self.ha_to_device_speed(100))
                
        except Exception as e:
            _LOGGER.error(f"Ошибка включения ШИМ fan: {e}")
            raise

    async def async_turn_off(
            self, speed: Optional[str] = None,
            percentage: Optional[int] = None,
            preset_mode: Optional[str] = None, **kwargs: Any
    ) -> None:
        """Turn the entity off."""
        try:
            await self.set_value_port(0)
        except Exception as e:
            _LOGGER.error(f"Ошибка выключения ШИМ fan: {e}")
            raise


class FanExtraMegaD(PortOutExtraEntity, FanEntity):
    """Дополнительный релейный вентилятор с поддержкой assumed_state."""

    _attr_supported_features = (FanEntityFeature.TURN_ON
                                | FanEntityFeature.TURN_OFF)

    def __init__(
            self, coordinator: MegaDCoordinator,
            port: I2CExtraPCA9685 | I2CExtraMCP230xx,
            config_extra_port: PCA9685RelayConfig | MCP230RelayConfig,
            unique_id: str
    ) -> None:
        # PortOutExtraEntity уже содержит всю логику assumed_state
        super().__init__(coordinator, port, config_extra_port, unique_id)
        self._megad: MegaD = coordinator.megad
        self._port = port
        self._config_extra_port = config_extra_port
        self._attr_unique_id = unique_id
        self._attr_name = config_extra_port.name
        self._attr_assumed_state = True  # Включаем assumed_state
        
        # Индивидуальный device_info для дополнительного релейного вентилятора
        self._attr_device_info = coordinator.entity_device_info(
            unique_id,
            config_extra_port.name,
            f"MegaD-{self._megad.id} Extra Relay Fan"
        )
        
        self._attr_has_entity_name = False

    def __repr__(self) -> str:
        if not self.hass:
            return f"<Fan entity {self.entity_id}>"
        return super().__repr__()

    async def async_added_to_hass(self):
        """Когда сущность добавлена в HA."""
        await super().async_added_to_hass()
        # Слушаем обновления координатора
        self.async_on_remove(
            self.coordinator.async_add_listener(self._handle_coordinator_update)
        )

    @property
    def name(self) -> str:
        return self._attr_name

    @property
    def unique_id(self) -> str:
        return self._attr_unique_id

    @property
    def assumed_state(self) -> bool:
        """Return True if the state is assumed."""
        return self._attr_assumed_state

    @property
    def percentage(self) -> int | None:
        """Return the current speed percentage."""
        # Релейный вентилятор всегда 100% когда включен
        if self.is_on:
            return 100
        return 0

    @property
    def is_on(self) -> bool | None:
        """Return true if the fan is on."""
        # Используем родительскую логику из PortOutExtraEntity
        return super().is_on

    async def async_turn_on(
            self, speed: Optional[str] = None,
            percentage: Optional[int] = None,
            preset_mode: Optional[str] = None, **kwargs: Any
    ) -> None:
        """Turn the entity on."""
        _LOGGER.debug(f"Включение доп. релейного fan порта {self._config_extra_port.id}, инверсия={getattr(self._config_extra_port, 'inverse', False)}")
        
        try:
            # Учитываем инверсию порта
            if hasattr(self._config_extra_port, 'inverse') and self._config_extra_port.inverse:
                await self._switch_port(0)  # Инвертированный порт: 0 = включено
            else:
                await self._switch_port(1)  # Обычный порт: 1 = включено
            
            _LOGGER.info(f"Доп. релейный fan порт {self._config_extra_port.id} включен")
            
        except Exception as e:
            _LOGGER.error(f"Ошибка включения доп. релейного fan порта {self._config_extra_port.id}: {e}")
            raise

    async def async_turn_off(
            self, speed: Optional[str] = None,
            percentage: Optional[int] = None,
            preset_mode: Optional[str] = None, **kwargs: Any
    ) -> None:
        """Turn the entity off."""
        _LOGGER.debug(f"Выключение доп. релейного fan порта {self._config_extra_port.id}, инверсия={getattr(self._config_extra_port, 'inverse', False)}")
        
        try:
            # Учитываем инверсию порта
            if hasattr(self._config_extra_port, 'inverse') and self._config_extra_port.inverse:
                await self._switch_port(1)  # Инвертированный порт: 1 = выключено
            else:
                await self._switch_port(0)  # Обычный порт: 0 = выключено
            
            _LOGGER.info(f"Доп. релейный fan порт {self._config_extra_port.id} выключен")
            
        except Exception as e:
            _LOGGER.error(f"Ошибка выключения доп. релейного fan порта {self._config_extra_port.id}: {e}")
            raise

    async def async_toggle(
            self, speed: Optional[str] = None,
            percentage: Optional[int] = None,
            preset_mode: Optional[str] = None, **kwargs: Any
    ) -> None:
        """Toggle the entity."""
        _LOGGER.debug(f"Переключение доп. релейного fan порта {self._config_extra_port.id}")
        
        try:
            await self._switch_port(2)  # 2 = toggle
            
            _LOGGER.info(f"Доп. релейный fan порт {self._config_extra_port.id} переключен")
            
        except Exception as e:
            _LOGGER.error(f"Ошибка переключения доп. релейного fan порта {self._config_extra_port.id}: {e}")
            raise


class FanPWMExtraMegaD(FanPWMBaseMegaD):
    """Дополнительный ШИМ вентилятор с поддержкой assumed_state."""

    def __init__(
            self, coordinator: MegaDCoordinator,
            port: I2CExtraPCA9685,
            config_extra_port: PCA9685PWMConfig,
            unique_id: str
    ) -> None:
        super().__init__(
            coordinator,
            config_extra_port.min_value,
            config_extra_port.max_value
        )
        self._coordinator: MegaDCoordinator = coordinator
        self._megad: MegaD = coordinator.megad
        self._port: I2CExtraPCA9685 = port
        self._config_extra_port = config_extra_port
        self._attr_unique_id = unique_id
        self._attr_name = config_extra_port.name
        self.ext_id = f'{port.conf.id}e{config_extra_port.id}'
        
        # Индивидуальный device_info для дополнительного ШИМ вентилятора
        self._attr_device_info = coordinator.entity_device_info(
            unique_id,
            config_extra_port.name,
            f"MegaD-{self._megad.id} Extra PWM Fan"
        )
        
        self._attr_has_entity_name = False
        
        # Инициализируем состояние
        self._update_state_from_port()

    async def async_added_to_hass(self):
        """Когда сущность добавлена в HA."""
        await super().async_added_to_hass()
        # Слушаем обновления координатора
        self.async_on_remove(
            self.coordinator.async_add_listener(self._handle_coordinator_update)
        )

    def _update_state_from_port(self):
        """Обновить состояние из данных порта."""
        port_state = self._port.state.get(self._config_extra_port.id)
        if port_state is None:
            self._last_confirmed_speed = 0
        else:
            self._last_confirmed_speed = self.device_to_ha_speed(port_state)
        
        self._attr_assumed_state = False
        _LOGGER.debug(f"Доп. ШИМ вентилятор порт {self.ext_id}: обновлено состояние из порта: скорость={self._last_confirmed_speed}%")

    async def set_value_port(self, value):
        """Установка значения порта с поддержкой assumed_state"""
        try:
            # Устанавливаем предполагаемое состояние
            ha_speed = self.device_to_ha_speed(value)
            self._assumed_speed = ha_speed
            self._attr_assumed_state = True
            
            _LOGGER.debug(f"Доп. ШИМ вентилятор порт {self.ext_id}: устанавливаем предполагаемое состояние: скорость={ha_speed}%")
            
            # Немедленно обновляем UI
            self.async_write_ha_state()
            
            # Отправляем команду на контроллер
            await self._megad.set_port(self.ext_id, value)
            
            # Триггерим обновление координатора
            await self._coordinator.update_port_state(
                self._port.conf.id,
                {f'ext{self._config_extra_port.id}': value}
            )
            
            # Запланировать проверку состояния через 2 секунды
            self.hass.async_create_task(self._verify_state_after_command(value))
            
            _LOGGER.debug(f"Установлено значение доп. ШИМ fan порта {self.ext_id}: {value}")
            
        except Exception as e:
            _LOGGER.error(f'Ошибка управления доп. ШИМ fan портом '
                          f'{self.ext_id}: {e}')
            self._attr_assumed_state = False
            raise

    async def _verify_state_after_command(self, value_sent: int):
        """Проверка состояния после отправки команды."""
        try:
            # Ждем выполнения команды
            await asyncio.sleep(2)
            
            # Запрашиваем обновление данных
            await self._coordinator.async_request_refresh()
            
            # После обновления данных состояние будет подтверждено
            self._attr_assumed_state = False
            self.async_write_ha_state()
            
            _LOGGER.debug(f"Доп. ШИМ вентилятор порт {self.ext_id}: состояние подтверждено после команды {value_sent}")
            
        except Exception as e:
            _LOGGER.debug(f"Доп. ШИМ вентилятор порт {self.ext_id}: не удалось подтвердить состояние: {e}")
            # Оставляем состояние как предполагаемое

    @property
    def name(self) -> str:
        return self._attr_name

    @property
    def unique_id(self) -> str:
        return self._attr_unique_id

    @property
    def percentage(self) -> int | None:
        """Return the current speed as a percentage."""
        # Если состояние предполагаемое, возвращаем предполагаемую скорость
        if self.assumed_state:
            _LOGGER.debug(f"Доп. ШИМ вентилятор порт {self.ext_id}: возвращаем предполагаемую скорость: {self._assumed_speed}%")
            return self._assumed_speed
        
        # Иначе получаем скорость из порта
        self._update_state_from_port()
        _LOGGER.debug(f"Доп. ШИМ вентилятор порт {self.ext_id}: возвращаем подтвержденную скорость: {self._last_confirmed_speed}%")
        return self._last_confirmed_speed

    @property
    def is_on(self) -> bool | None:
        """Return true if the fan is on."""
        # Определяем состояние на основе скорости
        current_speed = self.percentage or 0
        is_on = current_speed > 0
        _LOGGER.debug(f"Состояние доп. ШИМ вентилятора порт {self.ext_id}: скорость={current_speed}%, включен={is_on}")
        return is_on

    def _handle_coordinator_update(self):
        """Обработчик обновления координатора."""
        # При получении свежих данных с контроллера обновляем состояние из порта
        self._update_state_from_port()
        super()._handle_coordinator_update()

    async def async_set_percentage(self, percentage: int) -> None:
        """Set the speed of the fan, as a percentage."""
        try:
            if percentage == 0:
                await self.async_turn_off()
            else:
                device_value = self.ha_to_device_speed(percentage)
                await self.set_value_port(device_value)
                
        except Exception as e:
            _LOGGER.error(f"Ошибка установки скорости fan: {e}")
            raise

    async def async_turn_on(
            self, speed: Optional[str] = None,
            percentage: Optional[int] = None,
            preset_mode: Optional[str] = None, **kwargs: Any
    ) -> None:
        """Turn the entity on."""
        try:
            if percentage is not None:
                device_value = self.ha_to_device_speed(percentage)
                await self.set_value_port(device_value)
            else:
                await self.set_value_port(self.ha_to_device_speed(100))
                
        except Exception as e:
            _LOGGER.error(f"Ошибка включения ШИМ fan: {e}")
            raise

    async def async_turn_off(
            self, speed: Optional[str] = None,
            percentage: Optional[int] = None,
            preset_mode: Optional[str] = None, **kwargs: Any
    ) -> None:
        """Turn the entity off."""
        try:
            await self.set_value_port(0)
        except Exception as e:
            _LOGGER.error(f"Ошибка выключения ШИМ fan: {e}")
            raise
