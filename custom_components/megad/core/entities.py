import logging
from typing import Optional

from propcache import cached_property

from homeassistant.helpers.update_coordinator import CoordinatorEntity
from .base_ports import RelayPortOut
from .megad import MegaD
from .. import MegaDCoordinator
from ..const import PORT_COMMAND

_LOGGER = logging.getLogger(__name__)


class MegaDAssumedStateEntity:
    """Миксин для сущностей с поддержкой assumed_state."""
    
    def __init__(self):
        self._assumed_state = False
        self._last_confirmed_state = None
        self._pending_command = None
        
    @property
    def assumed_state(self) -> bool:
        """Return True if the state is based on assumption."""
        return self._assumed_state
    
    def set_assumed_state(self, value: bool):
        """Установить флаг assumed_state."""
        self._assumed_state = value
        
    def get_last_confirmed_state(self):
        """Получить последнее подтвержденное состояние."""
        return self._last_confirmed_state
    
    def set_last_confirmed_state(self, state):
        """Установить последнее подтвержденное состояние."""
        self._last_confirmed_state = state
        self._assumed_state = False  # Сбрасываем флаг при подтверждении
        
    def clear_pending_command(self):
        """Очистить ожидающую команду."""
        self._pending_command = None
        
    def _clear_cached_state(self):
        """Очистить все кэшированные состояния."""
        self._last_confirmed_state = None
        self._assumed_state = False
        self._pending_command = None
    
    async def confirm_state_from_device(self):
        """Подтвердить состояние с устройства (абстрактный метод)."""
        raise NotImplementedError("Метод должен быть реализован в дочернем классе")


class BaseMegaDEntity(CoordinatorEntity):
    """Базовый класс для сущностей MegaD с индивидуальным device_info"""
    
    def __init__(self, coordinator, unique_id, name, model=None):
        super().__init__(coordinator)
        self._attr_unique_id = unique_id
        self._attr_name = name
        self._attr_has_entity_name = True
        self._attr_device_info = coordinator.entity_device_info(
            unique_id, name, model
        )
        
    def _handle_coordinator_update(self):
        """Обработчик обновления координатора."""
        # При получении свежих данных с контроллера сбрасываем флаг assumed_state
        if hasattr(self, '_assumed_state'):
            self._assumed_state = False
        super()._handle_coordinator_update()


class PortOutEntity(BaseMegaDEntity, MegaDAssumedStateEntity):
    """Базовый класс для сущностей выходных портов с поддержкой assumed_state"""
    
    def __init__(
            self, coordinator: MegaDCoordinator, port: RelayPortOut,
            unique_id: str
    ) -> None:
        BaseMegaDEntity.__init__(self, coordinator, unique_id, port.conf.name)
        MegaDAssumedStateEntity.__init__(self)
        self._coordinator: MegaDCoordinator = coordinator
        self._megad: MegaD = coordinator.megad
        self._port: RelayPortOut = port
        self._attr_assumed_state = True  # Включаем assumed_state по умолчанию
        
        # Инициализируем состояние
        self._update_state_from_port()
    
    def _update_state_from_port(self):
        """Обновить состояние из данных порта."""
        port_state = bool(self._port.state)
        
        # Учитываем инверсию
        if hasattr(self._port.conf, 'inverse') and self._port.conf.inverse:
            confirmed_state = not port_state
        else:
            confirmed_state = port_state
        
        # Устанавливаем как подтвержденное состояние
        self.set_last_confirmed_state(confirmed_state)
        _LOGGER.debug(f"Порт {self._port.conf.id}: обновлено состояние из порта: {confirmed_state}")

    @property
    def is_on(self) -> bool | None:
        """Return true if the switch is on."""
        # Если состояние предполагаемое, возвращаем последнее известное
        if self.assumed_state and self._last_confirmed_state is not None:
            _LOGGER.debug(f"Порт {self._port.conf.id}: возвращаем предполагаемое состояние: {self._last_confirmed_state}")
            return self._last_confirmed_state
        
        # Иначе получаем состояние из порта
        self._update_state_from_port()
        _LOGGER.debug(f"Порт {self._port.conf.id}: возвращаем подтвержденное состояние: {self._last_confirmed_state}")
        return self._last_confirmed_state

    async def _switch_port(self, command: int):
        """Переключение состояния порта с поддержкой assumed_state"""
        try:
            # Устанавливаем предполагаемое состояние
            target_state = None
            if command == 0:
                target_state = False
            elif command == 1:
                target_state = True
            elif command == 2:
                target_state = not (self._last_confirmed_state if self._last_confirmed_state is not None else False)
            
            # Учитываем инверсию для сохранения в правильном формате
            if target_state is not None:
                self.set_last_confirmed_state(target_state)
                self.set_assumed_state(True)
            
            _LOGGER.debug(f"Порт {self._port.conf.id}: устанавливаем предполагаемое состояние: {target_state}")
            
            # Немедленно обновляем UI
            self.async_write_ha_state()
            
            # Отправляем команду на MegaD
            await self._megad.set_port(self._port.conf.id, command)
            
            # Через короткое время проверяем реальное состояние
            self.hass.async_create_task(self._verify_state_after_command(command))
            
        except Exception as e:
            _LOGGER.error(f'Ошибка переключения порта {self._port.conf.id}: {e}')
            self.set_assumed_state(False)  # Сбрасываем флаг при ошибке
            raise
    
    async def _verify_state_after_command(self, command_sent: int):
        """Проверка состояния после отправки команды."""
        try:
            # Ждем выполнения команды (короткая задержка)
            await asyncio.sleep(2)
            
            # Запрашиваем обновление данных
            await self._coordinator.async_request_refresh()
            
            # После обновления данных состояние будет подтверждено
            self.set_assumed_state(False)
            self.async_write_ha_state()
            
            _LOGGER.debug(f"Порт {self._port.conf.id}: состояние подтверждено после команды {command_sent}")
            
        except Exception as e:
            _LOGGER.debug(f"Порт {self._port.conf.id}: не удалось подтвердить состояние: {e}")
            # Оставляем состояние как предполагаемое
    
    async def async_turn_on(self, **kwargs):
        """Turn the entity on."""
        _LOGGER.debug(f"Включение порта {self._port.conf.id}")
        # Учитываем инверсию при отправке команды
        if hasattr(self._port.conf, 'inverse') and self._port.conf.inverse:
            await self._switch_port(0)  # Инвертированный порт: 0 = включено
        else:
            await self._switch_port(1)  # Обычный порт: 1 = включено

    async def async_turn_off(self, **kwargs):
        """Turn the entity off."""
        _LOGGER.debug(f"Выключение порта {self._port.conf.id}")
        # Учитываем инверсию при отправке команды
        if hasattr(self._port.conf, 'inverse') and self._port.conf.inverse:
            await self._switch_port(1)  # Инвертированный порт: 1 = выключено
        else:
            await self._switch_port(0)  # Обычный порт: 0 = выключено

    async def async_toggle(self, **kwargs):
        """Toggle the entity."""
        _LOGGER.debug(f"Переключение порта {self._port.conf.id}")
        await self._switch_port(2)  # 2 = toggle
    
    def _handle_coordinator_update(self):
        """Обработчик обновления координатора."""
        # При получении свежих данных с контроллера обновляем состояние из порта
        self._update_state_from_port()
        super()._handle_coordinator_update()
    
    async def confirm_state_from_device(self):
        """Подтвердить состояние с устройства."""
        _LOGGER.debug(f"Порт {self._port.conf.id}: подтверждение состояния с устройства")
        await self._coordinator.async_request_refresh()


class PortOutExtraEntity(BaseMegaDEntity, MegaDAssumedStateEntity):
    """Базовый класс для дополнительных выходных портов с поддержкой assumed_state"""

    def __init__(
            self, coordinator: MegaDCoordinator, port,
            config_extra_port, unique_id: str
    ) -> None:
        BaseMegaDEntity.__init__(self, coordinator, unique_id, config_extra_port.name)
        MegaDAssumedStateEntity.__init__(self)
        self._coordinator: MegaDCoordinator = coordinator
        self._megad: MegaD = coordinator.megad
        self._config_extra_port = config_extra_port
        self._port = port
        self.ext_id = f'{self._port.conf.id}e{self._config_extra_port.id}'
        self._attr_assumed_state = True  # Включаем assumed_state по умолчанию
        
        # Инициализируем состояние
        self._update_state_from_port()
    
    def _update_state_from_port(self):
        """Обновить состояние из данных порта."""
        port_state = self._port.state.get(self._config_extra_port.id)
        if port_state is None:
            confirmed_state = False
        else:
            confirmed_state = bool(port_state)
        
        # Учитываем инверсию
        if hasattr(self._config_extra_port, 'inverse') and self._config_extra_port.inverse:
            confirmed_state = not confirmed_state
        
        # Устанавливаем как подтвержденное состояние
        self.set_last_confirmed_state(confirmed_state)
        _LOGGER.debug(f"Доп. порт {self.ext_id}: обновлено состояние из порта: {confirmed_state}")

    @property
    def is_on(self) -> bool | None:
        """Return true if the binary sensor is on."""
        # Если состояние предполагаемое, возвращаем последнее известное
        if self.assumed_state and self._last_confirmed_state is not None:
            _LOGGER.debug(f"Доп. порт {self.ext_id}: возвращаем предполагаемое состояние: {self._last_confirmed_state}")
            return self._last_confirmed_state
        
        # Иначе получаем состояние из порта
        self._update_state_from_port()
        _LOGGER.debug(f"Доп. порт {self.ext_id}: возвращаем подтвержденное состояние: {self._last_confirmed_state}")
        return self._last_confirmed_state

    async def _switch_port(self, command):
        """Переключение состояния порта с поддержкой assumed_state"""
        _LOGGER.debug(f"Переключение доп. порта {self.ext_id}: команда={command}")
        
        try:
            # Определяем целевое состояние
            target_state = None
            if command == PORT_COMMAND.ON:
                target_state = True
            elif command == PORT_COMMAND.OFF:
                target_state = False
            elif command == PORT_COMMAND.TOGGLE:
                current_state = self._last_confirmed_state if self._last_confirmed_state is not None else False
                target_state = not current_state
            
            # Учитываем инверсию для сохранения в правильном формате
            if target_state is not None:
                # Для сохранения в памяти мы всегда храним состояние без учета инверсии
                # Инверсия учитывается только при отображении в is_on
                self.set_last_confirmed_state(target_state)
                self.set_assumed_state(True)
            
            _LOGGER.debug(f"Доп. порт {self.ext_id}: устанавливаем предполагаемое состояние: {target_state}")
            
            # Немедленно обновляем UI
            self.async_write_ha_state()
            
            # Отправляем команду с учетом инверсии
            send_command = command
            if hasattr(self._config_extra_port, 'inverse') and self._config_extra_port.inverse:
                if command == PORT_COMMAND.ON:
                    send_command = PORT_COMMAND.OFF
                elif command == PORT_COMMAND.OFF:
                    send_command = PORT_COMMAND.ON
            
            await self._megad.set_port(self.ext_id, send_command)
            
            # Через короткое время проверяем реальное состояние
            self.hass.async_create_task(self._verify_state_after_command(command))
            
            _LOGGER.info(f"Доп. порт {self.ext_id} переключен: команда={command}")
            
        except Exception as e:
            _LOGGER.error(f'Ошибка управления портом {self.ext_id}: {e}')
            self.set_assumed_state(False)  # Сбрасываем флаг при ошибке
            raise
    
    async def _verify_state_after_command(self, command_sent: str):
        """Проверка состояния после отправки команды."""
        try:
            # Ждем выполнения команды
            await asyncio.sleep(2)
            
            # Запрашиваем обновление данных
            await self._coordinator.async_request_refresh()
            
            # После обновления данных состояние будет подтверждено
            self.set_assumed_state(False)
            self.async_write_ha_state()
            
            _LOGGER.debug(f"Доп. порт {self.ext_id}: состояние подтверждено после команды {command_sent}")
            
        except Exception as e:
            _LOGGER.debug(f"Доп. порт {self.ext_id}: не удалось подтвердить состояние: {e}")
            # Оставляем состояние как предполагаемое

    async def async_turn_on(self, **kwargs):
        """Turn the entity on."""
        await self._switch_port(PORT_COMMAND.ON)

    async def async_turn_off(self, **kwargs):
        """Turn the entity off."""
        await self._switch_port(PORT_COMMAND.OFF)

    async def async_toggle(self, **kwargs):
        """Toggle the entity."""
        await self._switch_port(PORT_COMMAND.TOGGLE)
    
    def _handle_coordinator_update(self):
        """Обработчик обновления координатора."""
        # При получении свежих данных с контроллера обновляем состояние из порта
        self._update_state_from_port()
        super()._handle_coordinator_update()
    
    async def confirm_state_from_device(self):
        """Подтвердить состояние с устройства."""
        _LOGGER.debug(f"Доп. порт {self.ext_id}: подтверждение состояния с устройства")
        await self._coordinator.async_request_refresh()
