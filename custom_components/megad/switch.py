import logging
import asyncio

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from . import MegaDCoordinator
from .const import DOMAIN, PORT_COMMAND, ENTRIES, CURRENT_ENTITY_IDS
from .core.base_ports import (
    RelayPortOut, PWMPortOut, I2CExtraPCA9685, I2CExtraMCP230xx
)
from .core.megad import MegaD
from .core.enums import TypePortMegaD, DeviceClassControl
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
    
    # Сохраняем entry_id в координаторе для создания уникальных ID
    coordinator.config_entry = config_entry
    
    groups = {}
    switches = []

    # ✅ ДЛЯ ОТЛАДКИ
    _LOGGER.debug(f"=== СОЗДАНИЕ SWITCH СУЩНОСТЕЙ ДЛЯ MegaD-{megad.id} ===")
    
    # ✅ ЛОГИКА: Создаем Switch сущности для портов с Mode != SW и Mode != PWM
    
    for port in megad.ports:
        # 1. Релейные порты (RelayPortOut)
        if isinstance(port, RelayPortOut):
            port_mode = getattr(port.conf, 'mode', None)
            
            _LOGGER.debug(f"Порт {port.conf.id} '{port.conf.name}': mode={port_mode}")
            
            # ✅ ПРАВИЛО: Switch создаем если mode != SW и mode != PWM
            from .core.enums import ModeOutMegaD
            
            if port_mode not in [ModeOutMegaD.SW, ModeOutMegaD.PWM]:
                unique_id = coordinator.create_entity_unique_id(
                    port.conf.id, "switch"
                )
                switches.append(SwitchMegaD(coordinator, port, unique_id))
                _LOGGER.info(f"✅ СОЗДАН Switch: порт {port.conf.id} (Mode={port_mode})")
                
                # Собираем группы если есть
                if port.conf.group is not None:
                    groups.setdefault(port.conf.group, []).append(port.conf.id)
            else:
                _LOGGER.debug(f"❌ ПРОПУСК Switch: порт {port.conf.id} (Mode={port_mode} -> будет Light)")
        
        # 2. Дополнительные порты на PCA9685
        elif isinstance(port, I2CExtraPCA9685):
            for config in port.extra_confs:
                if isinstance(config, PCA9685RelayConfig):
                    port_mode = getattr(config, 'mode', None)
                    
                    from .core.enums import ModeOutMegaD
                    
                    if port_mode not in [ModeOutMegaD.SW, ModeOutMegaD.PWM]:
                        unique_id = coordinator.create_entity_unique_id(
                            port.conf.id, "switch", extra_port_id=config.id
                        )
                        switches.append(SwitchExtraMegaD(
                            coordinator, port, config, unique_id
                        ))
                        _LOGGER.info(f"✅ СОЗДАН SwitchExtra: порт {port.conf.id}e{config.id} (Mode={port_mode})")
                        
                        # Собираем группы если есть
                        if config.group is not None:
                            groups.setdefault(config.group, []).append(
                                f'{port.conf.id}e{config.id}'
                            )
        
        # 3. Дополнительные порты на MCP230xx
        elif isinstance(port, I2CExtraMCP230xx):
            for config in port.extra_confs:
                if isinstance(config, MCP230RelayConfig):
                    port_mode = getattr(config, 'mode', None)
                    
                    from .core.enums import ModeOutMegaD
                    
                    if port_mode not in [ModeOutMegaD.SW, ModeOutMegaD.PWM]:
                        unique_id = coordinator.create_entity_unique_id(
                            port.conf.id, "switch", extra_port_id=config.id
                        )
                        switches.append(SwitchExtraMegaD(
                            coordinator, port, config, unique_id
                        ))
                        _LOGGER.info(f"✅ СОЗДАН SwitchExtra MCP: порт {port.conf.id}e{config.id} (Mode={port_mode})")
                        
                        # Собираем группы если есть
                        if config.group is not None:
                            groups.setdefault(config.group, []).append(
                                f'{port.conf.id}e{config.id}'
                            )
    
    # ✅ Групповые переключатели создаются ВСЕГДА (если есть группы)
    # Но только если есть хоть один порт в группе
    if groups:
        for group, ports in groups.items():
            if ports:  # Проверяем, что группа не пустая
                unique_id = coordinator.create_group_entity_unique_id(group, "switch")
                name = f'Группа {group}'
                switches.append(SwitchGroupMegaD(
                    coordinator, group, name, ports, unique_id)
                )
                _LOGGER.info(f"✅ СОЗДАН SwitchGroup: группа {group} с портами {ports}")
    
    for switch in switches:
        hass.data[DOMAIN][CURRENT_ENTITY_IDS][entry_id].append(
            switch.unique_id)
    
    if switches:
        async_add_entities(switches)
        _LOGGER.info(f'Добавлено {len(switches)} SWITCH сущностей для MegaD {megad.id}')
    else:
        _LOGGER.debug('Не найдено SWITCH сущностей для создания')

class SwitchMegaD(CoordinatorEntity, SwitchEntity):
    def __init__(self, coordinator: MegaDCoordinator, port: RelayPortOut, unique_id: str) -> None:
        super().__init__(coordinator)
        self._coordinator: MegaDCoordinator = coordinator
        self._megad: MegaD = coordinator.megad
        self._port: RelayPortOut = port
        self._attr_unique_id = unique_id
        
        # Используем метод координатора для очистки имени
        port_name = port.conf.name
        self._attr_name = coordinator.create_clean_port_name(port_name, port.conf.id, "Switch")
        self._original_name = port_name
        
        # Извлекаем область из имени порта
        suggested_area = coordinator.extract_area_from_port_name(port_name)
        
        # ✅ ИСПРАВЛЕНИЕ: Используем entity_device_info
        self._attr_device_info = coordinator.entity_device_info(
            self._attr_name,
            f"MegaD-{self._megad.id} Switch",
            entity_type="switch",
            port_id=port.conf.id,
            suggested_area=suggested_area
        )
        
        self._attr_has_entity_name = False
        self._port_id = port.conf.id

    async def async_added_to_hass(self):
        """Когда сущность добавлена в HA."""
        await super().async_added_to_hass()
        # Слушаем обновления координатора
        self.async_on_remove(
            self.coordinator.async_add_listener(self._handle_coordinator_update)
        )
        # Первоначальное обновление состояния
        self._handle_coordinator_update()
        
        # ✅ Логирование для отладки
        _LOGGER.debug(f"SwitchMegaD добавлен: {self.entity_id}, порт {self._port_id}")

    @property
    def device_info(self):
        """Return device info."""
        return self._attr_device_info

    def _handle_coordinator_update(self):
        """Обработчик обновления координатора."""
        self.async_write_ha_state()
        
        # Дополнительное логирование для отладки
        current_state = self.is_on
        _LOGGER.debug(f"Координатор обновил состояние переключателя {self.entity_id}: {current_state}")

    @property
    def name(self) -> str:
        return self._attr_name

    @property
    def unique_id(self) -> str:
        return self._attr_unique_id
    
    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self._coordinator.last_update_success
    
    @property
    def is_on(self) -> bool | None:
        """Return true if the switch is on."""
        # Получаем состояние порта
        port_state = self._port.state
        if port_state is None:
            return None
        
        # Учитываем инверсию порта
        if hasattr(self._port.conf, 'inverse') and self._port.conf.inverse:
            return not bool(port_state)
        return bool(port_state)

    async def _switch_port(self, command: int):
        """Отправка команды переключения порта"""
        _LOGGER.debug(f"Отправка команды переключателю порт {self._port_id}: команда={command}")
        try:
            await self._megad.set_port(self._port_id, command)
            
            # Обновляем состояние через координатор
            await self._coordinator.update_port_state(self._port_id, command)
            
            # Немедленно обновляем UI
            self.async_write_ha_state()
            
            _LOGGER.debug(f"Команда отправлена переключателю порт {self._port_id}: {command}")
            
        except Exception as e:
            _LOGGER.error(f'Ошибка управления портом {self._port_id}: {e}')

    async def async_turn_on(self, **kwargs):
        """Turn the entity on."""
        _LOGGER.debug(f"Включение switch порта {self._port_id}, инверсия={getattr(self._port.conf, 'inverse', False)}")
        
        try:
            # Учитываем инверсию порта
            if hasattr(self._port.conf, 'inverse') and self._port.conf.inverse:
                await self._switch_port(0)  # Инвертированный порт: 0 = включено
            else:
                await self._switch_port(1)  # Обычный порт: 1 = включено
            
            # НЕМЕДЛЕННОЕ ОБНОВЛЕНИЕ СОСТОЯНИЯ ПОСЛЕ КОМАНДЫ
            await self._coordinator.async_request_refresh()
            self.async_write_ha_state()
            
            # Дополнительная проверка через 500 мс для уверенности
            async def delayed_update():
                await asyncio.sleep(0.5)
                await self._coordinator.async_request_refresh()
                self.async_write_ha_state()
            
            self.hass.async_create_task(delayed_update())
            
            _LOGGER.info(f"Switch порт {self._port_id} включен")
            
        except Exception as e:
            _LOGGER.error(f"Ошибка включения переключателя порт {self._port_id}: {e}")
            raise

    async def async_turn_off(self, **kwargs):
        """Turn the entity off."""
        _LOGGER.debug(f"Выключение switch порта {self._port_id}, инверсия={getattr(self._port.conf, 'inverse', False)}")
        
        try:
            # Учитываем инверсию порта
            if hasattr(self._port.conf, 'inverse') and self._port.conf.inverse:
                await self._switch_port(1)  # Инвертированный порт: 1 = выключено
            else:
                await self._switch_port(0)  # Обычный порт: 0 = выключено
            
            # НЕМЕДЛЕННОЕ ОБНОВЛЕНИЕ СОСТОЯНИЯ ПОСЛЕ КОМАНДЫ
            await self._coordinator.async_request_refresh()
            self.async_write_ha_state()
            
            # Дополнительная проверка через 500 мс
            async def delayed_update():
                await asyncio.sleep(0.5)
                await self._coordinator.async_request_refresh()
                self.async_write_ha_state()
            
            self.hass.async_create_task(delayed_update())
            
            _LOGGER.info(f"Switch порт {self._port_id} выключен")
            
        except Exception as e:
            _LOGGER.error(f"Ошибка выключения переключателя порт {self._port_id}: {e}")
            raise

    async def async_toggle(self, **kwargs):
        """Toggle the entity."""
        _LOGGER.debug(f"Переключение switch порта {self._port_id}")
        
        try:
            await self._switch_port(2)  # 2 = toggle
            
            # НЕМЕДЛЕННОЕ ОБНОВЛЕНИЕ СОСТОЯНИЯ ПОСЛЕ КОМАНДЫ
            await self._coordinator.async_request_refresh()
            self.async_write_ha_state()
            
            # Дополнительная проверка через 500 мс
            async def delayed_update():
                await asyncio.sleep(0.5)
                await self._coordinator.async_request_refresh()
                self.async_write_ha_state()
            
            self.hass.async_create_task(delayed_update())
            
            _LOGGER.info(f"Switch порт {self._port_id} переключен")
            
        except Exception as e:
            _LOGGER.error(f"Ошибка переключения переключателя порт {self._port_id}: {e}")
            raise

    @property
    def extra_state_attributes(self):
        """Дополнительные атрибуты для диагностики."""
        return {
            "port_id": self._port_id,
            "megad_id": self._megad.id,
            "device_type": "switch",
            "original_name": self._original_name
        }


class SwitchGroupMegaD(CoordinatorEntity, SwitchEntity):
    """Класс группы переключателей."""

    def __init__(
            self, coordinator: MegaDCoordinator, group: int, name: str,
            ports: list, unique_id: str
    ) -> None:
        super().__init__(coordinator)
        self._coordinator: MegaDCoordinator = coordinator
        self._megad: MegaD = coordinator.megad
        self._ports: list = ports
        self._group: int = group
        self._attr_unique_id = unique_id
        self._attr_name = name
        
        # ✅ ИСПРАВЛЕНИЕ: Для групп используем ТОТ ЖЕ device_info
        # Все сущности должны использовать один device_info для группировки
        self._attr_device_info = coordinator.entity_device_info(
            self._attr_name,
            f"MegaD-{self._megad.id} Switch Group",
            entity_type="switch_group",
            port_id=None,  # Группа не привязана к конкретному порту
            extra_port_id=f"group{group}"  # Уникальный идентификатор группы
        )
        
        self._attr_has_entity_name = False

    def __repr__(self) -> str:
        if not self.hass:
            return f"<Switch entity {self.entity_id}>"
        return super().__repr__()

    async def async_added_to_hass(self):
        """Когда сущность добавлена в HA."""
        await super().async_added_to_hass()
        # Слушаем обновления координатора
        self.async_on_remove(
            self.coordinator.async_add_listener(self._handle_coordinator_update)
        )
        # Первоначальное обновление состояния
        self._handle_coordinator_update()
        
        # ✅ Логирование для отладки
        _LOGGER.debug(f"SwitchGroupMegaD добавлен: {self.entity_id}, группа {self._group}")

    @property
    def device_info(self):
        """Return device info."""
        return self._attr_device_info

    def _handle_coordinator_update(self):
        """Обработчик обновления координатора."""
        self.async_write_ha_state()
        
        # Дополнительное логирование для отладки
        current_state = self.is_on
        _LOGGER.debug(f"Координатор обновил состояние группы переключателей {self.entity_id}: {current_state}")

    @property
    def name(self) -> str:
        return self._attr_name

    @property
    def unique_id(self) -> str:
        return self._attr_unique_id

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self._coordinator.last_update_success

    @property
    def is_on(self) -> bool | None:
        """Return true if the switch is on."""
        # Для группы определяем состояние по первому порту
        if not self._ports:
            return False
        
        first_port_id = self._ports[0]
        ext_id = None
        
        if isinstance(first_port_id, str):
            port_id, ext_id = first_port_id.split('e')
            port_id = int(port_id)
            ext_id = int(ext_id)
        else:
            port_id = first_port_id
        
        port = self._megad.get_port(port_id)
        if not port:
            return False
        
        if ext_id is not None:
            port_state = port.state.get(ext_id)
            if port_state is None:
                return False
            if hasattr(port.conf, 'inverse') and port.conf.inverse:
                return not bool(port_state)
            return bool(port_state)
        else:
            if hasattr(port.conf, 'inverse') and port.conf.inverse:
                return not bool(port.state)
            return bool(port.state)

    @staticmethod
    def _check_command(port, command: PORT_COMMAND, ext_id=None) -> str:
        """
        Проверка порта на возможность диммирования и корректировка команды.
        """
        if ext_id is not None:
            conf_ext = port.extra_confs[ext_id]
            if isinstance(conf_ext, PCA9685PWMConfig):
                max_value = conf_ext.max_value
                return max_value if command == PORT_COMMAND.ON else command
        if isinstance(port, PWMPortOut):
            return '255' if command == PORT_COMMAND.ON else command
        else:
            return command

    async def _switch_group(self, command):
        """Переключение состояния группы выходов"""
        port_states = {}
        _LOGGER.debug(f"Управление группой {self._group}: команда={command}")
        
        try:
            await self._megad.set_port(f'g{self._group}', command)
            
            if command == PORT_COMMAND.TOGGLE:
                for port_id in self._ports:
                    ext_id = None
                    if isinstance(port_id, str):
                        port_id, ext_id = port_id.split('e')
                        port_id = int(port_id)
                        ext_id = int(ext_id)
                    port = self._megad.get_port(port_id)
                    
                    if ext_id is not None:
                        # Дополнительный порт
                        current_state = port.state.get(ext_id)
                        if current_state:
                            # Если включен - выключаем
                            port_states.setdefault(port_id, {})[
                                f'ext{ext_id}'] = (
                                PORT_COMMAND.ON
                                if port.conf.inverse else
                                PORT_COMMAND.OFF
                            )
                        else:
                            # Если выключен - включаем
                            port_states.setdefault(port_id, {})[
                                f'ext{ext_id}'] = (
                                PORT_COMMAND.OFF
                                if port.conf.inverse else
                                self._check_command(
                                    port, PORT_COMMAND.ON, ext_id
                                )
                            )
                    else:
                        # Основной порт
                        if port.state:
                            # Если включен - выключаем
                            port_states[port_id] = (
                                PORT_COMMAND.ON
                                if port.conf.inverse else
                                PORT_COMMAND.OFF
                            )
                        else:
                            # Если выключен - включаем
                            port_states[port_id] = (
                                PORT_COMMAND.OFF
                                if port.conf.inverse else
                                self._check_command(port, PORT_COMMAND.ON)
                            )
            else:
                for port_id in self._ports:
                    if isinstance(port_id, str):
                        port_id, ext_id = port_id.split('e')
                        port_id = int(port_id)
                        ext_id = int(ext_id)
                        port = self._megad.get_port(port_id)
                        port_states.setdefault(port_id, {})[
                            f'ext{ext_id}'] = self._check_command(
                            port, command, ext_id
                        )
                    else:
                        port_states[port_id] = self._check_command(
                            self._megad.get_port(port_id), command
                        )
            
            # Обновляем состояние через координатор
            self._coordinator.update_group_state(port_states)
            
            # Немедленно обновляем UI
            self.async_write_ha_state()
            
            # НЕМЕДЛЕННОЕ ОБНОВЛЕНИЕ СОСТОЯНИЯ ПОСЛЕ КОМАНДЫ
            await self._coordinator.async_request_refresh()
            
            # Дополнительная проверка через 500 мс
            async def delayed_update():
                await asyncio.sleep(0.5)
                await self._coordinator.async_request_refresh()
                self.async_write_ha_state()
            
            self.hass.async_create_task(delayed_update())
            
            _LOGGER.info(f"Группа {self._group} переключена, команда={command}")
            
        except Exception as e:
            _LOGGER.error(f'Ошибка управления группой портов '
                          f'{self._group}: {e}')
            self.async_write_ha_state()

    async def async_turn_on(self, **kwargs):
        """Turn the entity on."""
        await self._switch_group(PORT_COMMAND.ON)

    async def async_turn_off(self, **kwargs):
        """Turn the entity off."""
        await self._switch_group(PORT_COMMAND.OFF)

    async def async_toggle(self, **kwargs):
        """Toggle the entity."""
        await self._switch_group(PORT_COMMAND.TOGGLE)

    @property
    def extra_state_attributes(self):
        """Дополнительные атрибуты для диагностики."""
        return {
            "group_id": self._group,
            "megad_id": self._megad.id,
            "device_type": "switch_group",
            "ports_count": len(self._ports)
        }


class SwitchExtraMegaD(CoordinatorEntity, SwitchEntity):
    def __init__(self, coordinator: MegaDCoordinator,
                 port: I2CExtraPCA9685 | I2CExtraMCP230xx,
                 config_extra_port: PCA9685RelayConfig | MCP230RelayConfig,
                 unique_id: str) -> None:
        super().__init__(coordinator)
        self._coordinator: MegaDCoordinator = coordinator
        self._megad: MegaD = coordinator.megad
        self._port = port
        self._config_extra_port = config_extra_port
        self._attr_unique_id = unique_id
        
        # Используем метод координатора для очистки имени
        port_name = config_extra_port.name
        self._attr_name = coordinator.create_clean_port_name(port_name, config_extra_port.id, "Switch")
        self._original_name = port_name
        
        # Извлекаем область из имени порта
        suggested_area = coordinator.extract_area_from_port_name(port_name)
        
        # ✅ ИСПРАВЛЕНИЕ: Для дополнительных портов указываем extra_port_id
        self._attr_device_info = coordinator.entity_device_info(
            self._attr_name,
            f"MegaD-{self._megad.id} Extra Switch",
            entity_type="extra_switch",
            port_id=port.conf.id,
            extra_port_id=config_extra_port.id,  # ✅ Это важно!
            suggested_area=suggested_area
        )
        
        self._attr_has_entity_name = False
        self._base_port_id = port.conf.id
        self._extra_port_id = config_extra_port.id
        
    async def async_added_to_hass(self):
        """Когда сущность добавлена в HA."""
        await super().async_added_to_hass()
        # Слушаем обновления координатора
        self.async_on_remove(
            self.coordinator.async_add_listener(self._handle_coordinator_update)
        )
        # Первоначальное обновление состояния
        self._handle_coordinator_update()
        
        # ✅ Логирование для отладки
        _LOGGER.debug(f"SwitchExtraMegaD добавлен: {self.entity_id}, "
                     f"base_port {self._base_port_id}, extra_port {self._extra_port_id}")

    @property
    def device_info(self):
        """Return device info."""
        return self._attr_device_info

    def _handle_coordinator_update(self):
        """Обработчик обновления координатора."""
        self.async_write_ha_state()
        
        # Дополнительное логирование для отладки
        current_state = self.is_on
        _LOGGER.debug(f"Координатор обновил состояние доп. переключателя {self.entity_id}: {current_state}")

    @property
    def name(self) -> str:
        return self._attr_name

    @property
    def unique_id(self) -> str:
        return self._attr_unique_id
    
    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self._coordinator.last_update_success
    
    @property
    def is_on(self) -> bool | None:
        """Return true if the switch is on."""
        # Получаем состояние дополнительного порта
        port_state = self._port.state.get(self._extra_port_id)
        if port_state is None:
            return None
        
        # Учитываем инверсию порта
        if hasattr(self._config_extra_port, 'inverse') and self._config_extra_port.inverse:
            return not bool(port_state)
        return bool(port_state)

    async def _switch_port(self, command: int):
        """Отправка команды переключения дополнительного порта"""
        _LOGGER.debug(f"Отправка команды доп. переключателю порт {self._extra_port_id}: команда={command}")
        try:
            # Отправляем команду для дополнительного порта
            await self._megad.set_port(
                self._base_port_id, 
                command, 
                extra_port=self._extra_port_id
            )
            
            # Обновляем состояние через координатор
            await self._coordinator.update_port_state(
                self._base_port_id, 
                {f'ext{self._extra_port_id}': command}, 
                ext=True
            )
            
            # Немедленно обновляем UI
            self.async_write_ha_state()
            
            _LOGGER.debug(f"Команда отправлена доп. переключателю порт {self._extra_port_id}: {command}")
            
        except Exception as e:
            _LOGGER.error(f'Ошибка управления доп. портом {self._extra_port_id}: {e}')

    async def async_turn_on(self, **kwargs):
        """Turn the entity on."""
        _LOGGER.debug(f"Включение доп. switch порта {self._extra_port_id}, инверсия={getattr(self._config_extra_port, 'inverse', False)}")
        
        try:
            # Учитываем инверсию порта
            if hasattr(self._config_extra_port, 'inverse') and self._config_extra_port.inverse:
                await self._switch_port(0)  # Инвертированный порт: 0 = включено
            else:
                await self._switch_port(1)  # Обычный порт: 1 = включено
            
            # НЕМЕДЛЕННОЕ ОБНОВЛЕНИЕ СОСТОЯНИЯ ПОСЛЕ КОМАНДЫ
            await self._coordinator.async_request_refresh()
            self.async_write_ha_state()
            
            # Дополнительная проверка через 500 мс
            async def delayed_update():
                await asyncio.sleep(0.5)
                await self._coordinator.async_request_refresh()
                self.async_write_ha_state()
            
            self.hass.async_create_task(delayed_update())
            
            _LOGGER.info(f"Доп. switch порт {self._extra_port_id} включен")
            
        except Exception as e:
            _LOGGER.error(f"Ошибка включения доп. переключателя порт {self._extra_port_id}: {e}")
            raise

    async def async_turn_off(self, **kwargs):
        """Turn the entity off."""
        _LOGGER.debug(f"Выключение доп. switch порта {self._extra_port_id}, инверсия={getattr(self._config_extra_port, 'inverse', False)}")
        
        try:
            # Учитываем инверсию порта
            if hasattr(self._config_extra_port, 'inverse') and self._config_extra_port.inverse:
                await self._switch_port(1)  # Инвертированный порт: 1 = выключено
            else:
                await self._switch_port(0)  # Обычный порт: 0 = выключено
            
            # НЕМЕДЛЕННОЕ ОБНОВЛЕНИЕ СОСТОЯНИЯ ПОСЛЕ КОМАНДЫ
            await self._coordinator.async_request_refresh()
            self.async_write_ha_state()
            
            # Дополнительная проверка через 500 мс
            async def delayed_update():
                await asyncio.sleep(0.5)
                await self._coordinator.async_request_refresh()
                self.async_write_ha_state()
            
            self.hass.async_create_task(delayed_update())
            
            _LOGGER.info(f"Доп. switch порт {self._extra_port_id} выключен")
            
        except Exception as e:
            _LOGGER.error(f"Ошибка выключения доп. переключателя порт {self._extra_port_id}: {e}")
            raise

    async def async_toggle(self, **kwargs):
        """Toggle the entity."""
        _LOGGER.debug(f"Переключение доп. switch порта {self._extra_port_id}")
        
        try:
            await self._switch_port(2)  # 2 = toggle
            
            # НЕМЕДЛЕННОЕ ОБНОВЛЕНИЕ СОСТОЯНИЯ ПОСЛЕ КОМАНДЫ
            await self._coordinator.async_request_refresh()
            self.async_write_ha_state()
            
            # Дополнительная проверка через 500 мс
            async def delayed_update():
                await asyncio.sleep(0.5)
                await self._coordinator.async_request_refresh()
                self.async_write_ha_state()
            
            self.hass.async_create_task(delayed_update())
            
            _LOGGER.info(f"Доп. switch порт {self._extra_port_id} переключен")
            
        except Exception as e:
            _LOGGER.error(f"Ошибка переключения доп. переключателя порт {self._extra_port_id}: {e}")
            raise

    @property
    def extra_state_attributes(self):
        """Дополнительные атрибуты для диагностики."""
        return {
            "base_port_id": self._base_port_id,
            "extra_port_id": self._extra_port_id,
            "megad_id": self._megad.id,
            "device_type": "extra_switch",
            "original_name": self._original_name
        }