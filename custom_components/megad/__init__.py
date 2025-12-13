import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

import async_timeout

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_registry import async_get
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator, UpdateFailed
)
from .const import (
    TIME_UPDATE, DOMAIN, MANUFACTURER, COUNTER_CONNECT, PLATFORMS, ENTRIES,
    CURRENT_ENTITY_IDS, STATUS_THERMO, TIME_SLEEP_REQUEST, OFF,
    FIRMWARE_CHECKER, TIME_OUT_UPDATE_DATA_GENERAL,
    WATCHDOG_CHECK_INTERVAL, WATCHDOG_PING_TIMEOUT, WATCHDOG_MAX_FAILURES,
    WATCHDOG_RECOVERY_DELAY
)
from .core.base_ports import OneWireSensorPort, ReaderPort, PWMPortOut
from .core.config_manager import MegaDConfigManager
from .core.enums import ModeInMegaD, TypePortMegaD
from .core.exceptions import InvalidSettingPort, FirmwareUpdateInProgress
from .core.megad import MegaD
from .core.models_megad import DeviceMegaD, PIDConfig
from .core.request_to_ablogru import FirmwareChecker
from .core.server import MegadHttpView
from .core.utils import get_action_turnoff
from .watchdog import MegaDWatchdog  # <-- ДОБАВИТЬ ЭТУ СТРОКУ

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema("megad")


async def async_setup(hass: HomeAssistant, config: dict):
    """Регистрируем HTTP ручку"""
    hass.http.register_view(MegadHttpView())
    
    # Регистрация сервисов для watchdog
    async def async_handle_restart_megad(call):
        """Обработчик сервиса перезагрузки MegaD."""
        entity_id = call.data.get("entity_id")
        
        # Находим координатор по entity_id
        if DOMAIN in hass.data and ENTRIES in hass.data[DOMAIN]:
            for entry_id, coordinator in hass.data[DOMAIN][ENTRIES].items():
                if coordinator and coordinator.watchdog:
                    # Проверяем, что entity_id принадлежит этому контроллеру
                    if coordinator.megad and f"{coordinator.megad.id}" in entity_id:
                        _LOGGER.info(f"Сервис перезагрузки вызван для MegaD-{coordinator.megad.id}")
                        success = await coordinator.watchdog._reboot_megad()
                        if success:
                            hass.bus.async_fire(
                                "megad_restarted",
                                {"megad_id": coordinator.megad.id, "entity_id": entity_id, "success": True}
                            )
                            # Даем время на перезагрузку и запускаем проверку
                            await asyncio.sleep(90)
                            await coordinator.watchdog._force_check_and_update()
                        else:
                            hass.bus.async_fire(
                                "megad_restarted",
                                {"megad_id": coordinator.megad.id, "entity_id": entity_id, "success": False}
                            )
                        return
        
        _LOGGER.error(f"Не найден контроллер для entity_id: {entity_id}")
    
    async def async_handle_reset_network(call):
        """Обработчик сервиса сброса сети MegaD."""
        entity_id = call.data.get("entity_id")
        
        if DOMAIN in hass.data and ENTRIES in hass.data[DOMAIN]:
            for entry_id, coordinator in hass.data[DOMAIN][ENTRIES].items():
                if coordinator and coordinator.watchdog:
                    if coordinator.megad and f"{coordinator.megad.id}" in entity_id:
                        _LOGGER.info(f"Сервис сброса сети вызван для MegaD-{coordinator.megad.id}")
                        success = await coordinator.watchdog._reset_network_interface()
                        if success:
                            hass.bus.async_fire(
                                "megad_network_reset",
                                {"megad_id": coordinator.megad.id, "entity_id": entity_id, "success": True}
                            )
                            # Даем время на восстановление сети и запускаем проверку
                            await asyncio.sleep(30)
                            await coordinator.watchdog._force_check_and_update()
                        else:
                            hass.bus.async_fire(
                                "megad_network_reset",
                                {"megad_id": coordinator.megad.id, "entity_id": entity_id, "success": False}
                            )
                        return
        
        _LOGGER.error(f"Не найден контроллер для entity_id: {entity_id}")
    
    async def async_handle_get_status(call):
        """Обработчик сервиса получения статуса watchdog."""
        entity_id = call.data.get("entity_id")
        
        if DOMAIN in hass.data and ENTRIES in hass.data[DOMAIN]:
            for entry_id, coordinator in hass.data[DOMAIN][ENTRIES].items():
                if coordinator and coordinator.watchdog:
                    if coordinator.megad and f"{coordinator.megad.id}" in entity_id:
                        status = coordinator.watchdog.get_status()
                        
                        # Создаем уведомление со статусом
                        hass.components.persistent_notification.async_create(
                            f"Статус watchdog MegaD-{coordinator.megad.id}:\n"
                            f"• Работает: {status['is_running']}\n"
                            f"• Счётчик ошибок: {status['failure_count']}/{status['max_failures']}\n"
                            f"• Последний успех: {status['last_success']}\n"
                            f"• IP адрес: {status['megad_ip']}\n"
                            f"• Доступен: {status['is_available']}\n"
                            f"• Восстанавливается: {status['is_recovering']}",
                            title=f"Статус MegaD-{coordinator.megad.id}",
                            notification_id=f"megad_watchdog_status_{coordinator.megad.id}"
                        )
                        _LOGGER.info(f"Статус watchdog получен для MegaD-{coordinator.megad.id}")
                        return
        
        _LOGGER.error(f"Не найден контроллер для entity_id: {entity_id}")
    
    async def async_handle_sync_states(call):
        """Обработчик сервиса синхронизации состояний."""
        entity_id = call.data.get("entity_id")
        
        if DOMAIN in hass.data and ENTRIES in hass.data[DOMAIN]:
            for entry_id, coordinator in hass.data[DOMAIN][ENTRIES].items():
                if coordinator and coordinator.megad:
                    # Проверяем, что entity_id принадлежит этому контроллеру
                    if coordinator.megad and f"{coordinator.megad.id}" in entity_id:
                        await coordinator.force_sync_all_entities()
                        hass.bus.async_fire(
                            "megad_states_synced",
                            {"megad_id": coordinator.megad.id, "entity_id": entity_id}
                        )
                        _LOGGER.info(f"Синхронизация состояний для MegaD-{coordinator.megad.id}")
                        return
        
        _LOGGER.error(f"Не найден контроллер для entity_id: {entity_id}")
    
    async def async_handle_force_check(call):
        """Обработчик сервиса принудительной проверки."""
        entity_id = call.data.get("entity_id")
        
        if DOMAIN in hass.data and ENTRIES in hass.data[DOMAIN]:
            for entry_id, coordinator in hass.data[DOMAIN][ENTRIES].items():
                if coordinator and coordinator.watchdog:
                    # Проверяем по любому совпадению в entity_id
                    if coordinator.megad:
                        # Если entity_id не указан, применяем ко всем контроллерам
                        if not entity_id or f"{coordinator.megad.id}" in entity_id:
                            _LOGGER.info(f"Принудительная проверка MegaD-{coordinator.megad.id}")
                            await coordinator.watchdog.force_check_and_update()
                            # Если был указан конкретный entity_id, выходим после первого совпадения
                            if entity_id:
                                return
        
        # Только если entity_id был указан и не найден, выводим ошибку
        if entity_id:
            _LOGGER.error(f"Не найден контроллер для entity_id: {entity_id}")
        else:
            _LOGGER.error("Не указан entity_id для принудительной проверки")
    
    # Регистрируем сервисы
    hass.services.async_register(DOMAIN, "restart_megad", async_handle_restart_megad)
    hass.services.async_register(DOMAIN, "reset_megad_network", async_handle_reset_network)
    hass.services.async_register(DOMAIN, "get_watchdog_status", async_handle_get_status)
    hass.services.async_register(DOMAIN, "sync_states", async_handle_sync_states)
    hass.services.async_register(DOMAIN, "force_check_megad", async_handle_force_check)
    
    return True


def remove_entity(hass: HomeAssistant, current_entries_id: list,
                  config_entry: ConfigEntry):
    """Удаление неиспользуемых сущностей"""
    entity_registry = async_get(hass)
    remove_entities = []
    for entity_id, entity in entity_registry.entities.items():
        if entity.config_entry_id == config_entry.entry_id:
            if entity.unique_id not in current_entries_id:
                remove_entities.append(entity_id)
    for entity_id in remove_entities:
        entity_registry.async_remove(entity_id)
        _LOGGER.info(f'Удалена устаревшая сущность {entity_id}')


async def async_setup_entry(
        hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    config_entry.async_on_unload(
        config_entry.add_update_listener(update_listener)
    )
    entry_id = config_entry.entry_id
    _LOGGER.debug(f'Entry_id {entry_id}')
    file_path = config_entry.data.get('file_path')
    url = config_entry.data.get('url')
    manager_config = MegaDConfigManager(
        url, file_path, async_get_clientsession(hass)
    )
    await manager_config.read_config_file(file_path)
    megad_config = await manager_config.create_config_megad()
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault(FIRMWARE_CHECKER, {})
    hass.data[DOMAIN].setdefault(ENTRIES, {})
    hass.data[DOMAIN][ENTRIES][entry_id] = None
    if not hass.data[DOMAIN][FIRMWARE_CHECKER]:
        fw_checker = FirmwareChecker(hass)
        await fw_checker.update_page_firmwares()
        hass.data[DOMAIN][FIRMWARE_CHECKER] = fw_checker
        _LOGGER.debug(f'Добавлен firmware checker: '
                      f'{hass.data[DOMAIN][FIRMWARE_CHECKER]}')
    megad = MegaD(
        hass=hass,
        config=megad_config,
        url=url,
        config_path=file_path,
        fw_checker=hass.data[DOMAIN][FIRMWARE_CHECKER]
    )
    await megad.async_init_i2c_bus()
    await megad.check_local_software()

    coordinator = MegaDCoordinator(hass=hass, megad=megad)
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN].setdefault(CURRENT_ENTITY_IDS, {})
    hass.data[DOMAIN][CURRENT_ENTITY_IDS][entry_id] = []
    hass.data[DOMAIN][ENTRIES][entry_id] = coordinator
    
    # ЗАПУСКАЕМ WATCHDOG
    await coordinator.start_watchdog()
    
    # ОЖИДАЕМ запуск платформ с помощью await
    await hass.config_entries.async_forward_entry_setups(
        config_entry, PLATFORMS
    )
    current_entries_id = hass.data[DOMAIN][CURRENT_ENTITY_IDS][entry_id]
    remove_entity(hass, current_entries_id, config_entry)
    _LOGGER.debug(f'Unique_id актуальных сущностей контроллера {megad.id}: '
                  f'{current_entries_id}')
    _LOGGER.debug(f'Количество актуальных сущностей: '
                  f'{len(current_entries_id)}')
    return True


async def update_listener(hass, entry):
    """Вызывается при изменении настроек интеграции."""
    _LOGGER.info(f'Перезапуск интеграции для entry_id: {entry.entry_id})')
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload a config entry."""
    _LOGGER.info(f'Выгрузка интеграции: {entry.entry_id}')
    _LOGGER.info(f'data: {entry.data}')
    try:
        entry_id = entry.entry_id
        coordinator = hass.data[DOMAIN][ENTRIES].get(entry_id)
        
        # Останавливаем watchdog перед выгрузкой
        if coordinator:
            await coordinator.stop_watchdog()
        
        unload_ok = await hass.config_entries.async_unload_platforms(
            entry, PLATFORMS
        )
        hass.data[DOMAIN][ENTRIES].pop(entry_id)

        return unload_ok
    except Exception as e:
        _LOGGER.error(f'Ошибка при выгрузке: {e}')
        return False


class MegaDCoordinator(DataUpdateCoordinator):
    """Координатор для общего обновления данных"""

    _count_connect: int = 0
    _was_unavailable: bool = False
    _recovery_in_progress: bool = False

    def __init__(self, hass, megad):
        super().__init__(
            hass,
            _LOGGER,
            name=f'MegaD Coordinator id: {megad.id}',
            update_interval=timedelta(seconds=TIME_UPDATE),
        )
        self.megad: MegaD = megad
        self.watchdog: Optional[MegaDWatchdog] = None

    def devices_info(self):
        megad_id = self.megad.config.plc.megad_id
        device_info = DeviceInfo(**{
            "identifiers": {(DOMAIN, megad_id)},
            "name": f'MegaD-{megad_id}',
            "sw_version": self.megad.software,
            "configuration_url": self.megad.url,
            "manufacturer": MANUFACTURER,
        })
        return device_info
    
    def entity_device_info(self, entity_unique_id, entity_name, entity_model=None):
        """Создает device_info для отдельных сущностей"""
        megad_id = self.megad.config.plc.megad_id
        return DeviceInfo(**{
            "identifiers": {(DOMAIN, entity_unique_id)},
            "name": entity_name,
            "manufacturer": MANUFACTURER,
            "model": entity_model or f"MegaD-{megad_id} Entity",
            "via_device": (DOMAIN, megad_id),  # Связь с основным устройством
            "sw_version": self.megad.software,
            "configuration_url": self.megad.url,
        })

    async def async_request_refresh(self):
        """Запрос обновления данных (совместимость с HA)."""
        _LOGGER.debug(f"Запрос принудительного обновления данных для MegaD-{self.megad.id}")
        await self.async_refresh()
        # Немедленно обновляем слушателей
        self.async_update_listeners()
    
    async def _async_update_data(self):
        """Обновление всех данных megad"""
        try:
            if self.megad.is_flashing:
                raise FirmwareUpdateInProgress
            
            # Если идет восстановление, пропускаем обычное обновление
            if self._recovery_in_progress:
                _LOGGER.debug(f"MegaD-{self.megad.id}: восстановление в процессе, пропускаем обновление")
                return self.megad
            
            async with async_timeout.timeout(TIME_OUT_UPDATE_DATA_GENERAL):
                await self.megad.update_data()
                self._count_connect = 0
                self.megad.is_available = True
                
                # Сбрасываем счетчик ошибок watchdog при успешном обновлении
                if self.watchdog:
                    self.watchdog._failure_count = 0
                    self.watchdog._last_success = datetime.now()
                
                # Если было восстановление после ошибок, синхронизируем состояния
                if self._was_unavailable:
                    _LOGGER.info(f"MegaD-{self.megad.id}: соединение восстановлено, запускаем синхронизацию")
                    await self.force_sync_all_entities()
                    self._was_unavailable = False
                
                return self.megad
        except FirmwareUpdateInProgress:
            _LOGGER.warning(f'Обновление данных недоступно, контроллер '
                            f'id-{self.megad.id} обновляется.')
            raise UpdateFailed(f'Идёт процесс обновления ПО MegaD id: '
                               f'{self.megad.config.plc.megad_id}')
        except Exception as err:
            self.megad.is_available = False
            self._was_unavailable = True
            
            if self._count_connect < COUNTER_CONNECT:
                self._count_connect += 1
                _LOGGER.warning(
                    f'Неудачная попытка обновления данных контроллера '
                    f'id: {self.megad.config.plc.megad_id}. Ошибка: {err}.'
                    f'Осталось попыток: '
                    f'{COUNTER_CONNECT - self._count_connect + 1}'
                )
                return self.megad
            else:
                raise UpdateFailed(f'Ошибка соединения с контроллера id: '
                                   f'{self.megad.config.plc.megad_id}: {err}')
            
    async def force_refresh(self):
        """Принудительное обновление всех данные."""
        try:
            await self.async_refresh()
            self.async_update_listeners()
            _LOGGER.info(f"Принудительное обновление данных для MegaD-{self.megad.id}")
            return True
        except Exception as e:
            _LOGGER.error(f"Ошибка принудительного обновления данных MegaD-{self.megad.id}: {e}")
            return False

    async def force_sync_all_entities(self):
        """Принудительная синхронизация всех сущностей после восстановления соединения."""
        _LOGGER.info(f"Принудительная синхронизация сущностей для MegaD-{self.megad.id}")
        
        # Сначала обновляем данные с контроллера
        try:
            await self.async_refresh()
        except Exception as e:
            _LOGGER.error(f"Ошибка обновления данных при синхронизации: {e}")
        
        # Уведомляем все сущности об обновлении
        self.async_update_listeners()
        
        # Даем время сущностям на обновление
        await asyncio.sleep(2)
        
        # Дополнительное обновление для надежности
        self.async_update_listeners()
        
        _LOGGER.info(f"Синхронизация сущностей для MegaD-{self.megad.id} завершена")

    async def start_watchdog(self):
        """Запуск watchdog для этого контроллера."""
        if not self.watchdog:
            # УДАЛИТЬ ЭТУ СТРОКУ: from .watchdog import MegaDWatchdog
            self.watchdog = MegaDWatchdog(self, self.hass)
            await self.watchdog.start()

    async def stop_watchdog(self):
        """Остановка watchdog."""
        if self.watchdog:
            await self.watchdog.stop()
            self.watchdog = None
    
    async def set_recovery_state(self, state: bool):
        """Устанавливает состояние восстановления."""
        self._recovery_in_progress = state
        if state:
            _LOGGER.info(f"MegaD-{self.megad.id}: запущено восстановление")
        else:
            _LOGGER.info(f"MegaD-{self.megad.id}: восстановление завершено")

    async def set_flashing_state(self, state):
        """Устанавливает режим прошивки устройства."""
        self.megad.is_flashing = state
        self.hass.loop.call_soon(self.async_update_listeners)
        self.last_update_success = not state

    async def _turn_off_state(self, state_off, delay, port_id, data):
        """Возвращает выключенное состояние порта."""
        # Определяем правильное значение выключения в зависимости от типа порта
        port = self.megad.get_port(port_id)
        if port:
            if isinstance(port, PWMPortOut):
                # Для ШИМ портов выключение - это 0
                state_off = 0
        
        self.megad.update_port(port_id, data)
        self.hass.loop.call_soon(self.async_update_listeners)
        await asyncio.sleep(delay)
        self.megad.update_port(port_id, state_off)
        self.hass.loop.call_soon(self.async_update_listeners)

    def update_pid_state(self, pid_id: int, data: dict):
        """Обновление состояния ПИД регулятора."""
        self.megad.update_pid(pid_id, data)
        self.hass.loop.call_soon(self.async_update_listeners)

    async def update_port_state(self, port_id, data, ext=False):
        """Обновление состояния конкретного порта с немедленной обратной связью."""
        _LOGGER.debug(f"Обновление состояния порта {port_id}: данные={data}, ext={ext}")
        
        port = self.megad.get_port(port_id)
        if port is None:
            _LOGGER.warning(f"Порт {port_id} не найден")
            return

        if port.conf.type_port in (TypePortMegaD.ADC, ):
            return

        # Для ШИМ портов проверяем значение
        if isinstance(port, PWMPortOut):
            _LOGGER.debug(f"Обновление ШИМ порта {port_id}: значение={data}")
            # Для ШИМ портов значение может быть от 0 до 255
            if not isinstance(data, (int, float)):
                try:
                    data = int(data)
                except (ValueError, TypeError):
                    _LOGGER.error(f"Некорректное значение для ШИМ порта {port_id}: {data}")
                    return
            
            # ВАЖНО: Обновляем состояние порта вручную для немедленного отображения
            # Это ключевое изменение - напрямую обновляем состояние порта
            try:
                # Используем внутренний метод для обновления состояния
                port._state = data
                _LOGGER.debug(f"ШИМ порт {port_id}: состояние обновлено вручную: {data}")
            except AttributeError:
                # Если нет _state, пробуем другой способ
                _LOGGER.warning(f"Не удалось обновить состояние ШИМ порта {port_id} напрямую")

        # Обновляем состояние порта через метод update_port
        try:
            # Для дополнительных портов
            if ext and isinstance(data, dict):
                for key, value in data.items():
                    if key.startswith('ext'):
                        # Формируем правильный идентификатор доп. порта
                        ext_id = int(key[3:])
                        ext_port_id = f"{port_id}e{ext_id}"
                        # Обновляем состояние доп. порта
                        self.megad.update_port(ext_port_id, value)
                        _LOGGER.debug(f"Обновление доп. порта {ext_port_id}: {value}")
                        
                        # Обновляем состояние для дополнительных ШИМ портов
                        if hasattr(port, '_state') and isinstance(port._state, dict):
                            port._state[ext_id] = value
            else:
                # Для основных портов
                self.megad.update_port(port_id, data)
                _LOGGER.debug(f"Обновление основного порта {port_id}: {data}")
                
                # Для ШИМ портов дополнительно обновляем через общий метод
                if isinstance(port, PWMPortOut):
                    # Вызываем update_port для мегада
                    self.megad.update_port(port_id, data)
        except Exception as e:
            _LOGGER.error(f"Ошибка обновления состояния порта {port_id}: {e}")
            return

        # ВАЖНО: Многократно обновляем слушателей для немедленного отображения
        # 1. Немедленно обновляем UI
        self.async_update_listeners()
        
        # 2. Через event loop для гарантии
        self.hass.loop.call_soon(self.async_update_listeners)
        
        # 3. Небольшая задержка и еще одно обновление
        async def delayed_update():
            await asyncio.sleep(0.1)
            self.async_update_listeners()
            _LOGGER.debug(f"Отложенное обновление UI для порта {port_id}")
        
        # Запускаем отложенное обновление
        self.hass.async_create_task(delayed_update())
        
        # 4. Обновляем через refresh для синхронизации с контроллером
        async def refresh_and_update():
            await asyncio.sleep(0.5)
            try:
                await self.async_refresh()
                self.async_update_listeners()
                _LOGGER.debug(f"Обновление данных с контроллера для порта {port_id}")
            except Exception as e:
                _LOGGER.error(f"Ошибка при обновлении данных с контроллера: {e}")
        
        self.hass.async_create_task(refresh_and_update())
        
        _LOGGER.debug(f"Состояние порта {port_id} обновлено, UI уведомлен")

        # Если это порт в режиме C или ReaderPort, добавляем задержку выключения
        if isinstance(port, ReaderPort) or (hasattr(port.conf, 'mode') and port.conf.mode == ModeInMegaD.C):
            await self._turn_off_state('off', 0.5, port_id, data)

    def update_set_temperature(self, port_id, temperature):
        """Обновление заданной температуры порта сенсора"""
        port = self.megad.get_port(port_id)
        if isinstance(port, OneWireSensorPort):
            port.conf.set_value = temperature
            _LOGGER.debug(f"Обновлена температура порта {port_id}: {temperature}")
            self.hass.loop.call_soon(self.async_update_listeners)
        else:
            raise InvalidSettingPort(f'Проверьте настройки порта №{port_id}')

    def update_group_state(self, port_states: dict[int, str]):
        """Обновление состояний портов в группе"""
        _LOGGER.debug(f"Обновление группы портов: {port_states}")
        for port_id, state in port_states.items():
            self.megad.update_port(port_id, state)
        self.hass.loop.call_soon(self.async_update_listeners)

    async def restore_thermo(self, port):
        """Восстановление состояния терморегулятора после перезагрузки плк"""
        await self.megad.set_temperature(
            port.conf.id, port.conf.set_value
        )
        if not port.state[STATUS_THERMO]:
            await asyncio.sleep(TIME_SLEEP_REQUEST)
            await self.megad.set_port(port.conf.id, OFF)
            await self.megad.send_command(get_action_turnoff(port.conf.action))

    async def restore_status_ports(self):
        """Восстановление состояния портов после перезагрузки контроллера"""
        for port in self.megad.ports:
            if port.conf.type_port == TypePortMegaD.OUT:
                state = not port.state if port.conf.inverse else port.state
                if state:
                    await self.megad.set_port(port.conf.id, int(state))
            if self.megad.check_port_is_thermostat(port):
                await self.restore_thermo(port)
        await asyncio.sleep(1)
        await self.megad.update_data()
        self.hass.loop.call_soon(self.async_update_listeners)