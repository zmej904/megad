import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

import async_timeout

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.entity_registry import async_get
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator, UpdateFailed
)
from .const import (
    TIME_UPDATE, DOMAIN, MANUFACTURER, COUNTER_CONNECT, PLATFORMS, ENTRIES,
    CURRENT_ENTITY_IDS, STATUS_THERMO, TIME_SLEEP_REQUEST, OFF,
    FIRMWARE_CHECKER, TIME_OUT_UPDATE_DATA_GENERAL,
    WATCHDOG_CHECK_INTERVAL, WATCHDOG_MAX_FAILURES, WATCHDOG_INACTIVITY_TIMEOUT
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
from .watchdog import MegaDWatchdog

_LOGGER = logging.getLogger(__name__)

def clean_port_name(port_name: str, port_id: int, default_name: str = "Entity") -> str:
    """Очищает имя порта от префикса 'portXX_'."""
    if not port_name:
        return f"{default_name} {port_id}"
    
    import re
    
    # Убираем префикс portXX_
    if '_' in port_name:
        parts = port_name.split('_')
        if parts[0].lower().startswith('port'):
            # Убираем первый элемент (portXX) и соединяем остальные
            cleaned = ' '.join(parts[1:])
            return cleaned.title() if cleaned else f"{default_name} {port_id}"
        else:
            # Если есть подчеркивания, но не начинается с port
            return ' '.join(parts).title()
    
    # Если нет подчеркивания, но начинается с port
    if port_name.lower().startswith('port'):
        # Проверяем, есть ли цифры после "port"
        match = re.match(r'^[Pp]ort(\d+)(.*)$', port_name)
        if match:
            # "port26Light" -> "Light"
            suffix = match.group(2)
            return suffix.title() if suffix else f"{default_name} {port_id}"
    
    return port_name.replace('_', ' ').title()


def extract_area_from_name(port_name: str) -> str | None:
    """Извлекает предполагаемую область из имени порта."""
    if not port_name:
        return None
    
    area_keywords = {
        'kitchen': 'Kitchen',
        'living': 'Living Room',
        'bedroom': 'Bedroom',
        'bathroom': 'Bathroom',
        'hall': 'Hallway',
        'corridor': 'Corridor',
        'garage': 'Garage',
        'office': 'Office',
        'garden': 'Garden',
        'balcony': 'Balcony',
        'terrace': 'Terrace',
        'cellar': 'Cellar',
        'attic': 'Attic',
        'entrance': 'Entrance',
        'staircase': 'Staircase',
        'laundry': 'Laundry Room',
        'wardrobe': 'Wardrobe',
    }
    
    port_name_lower = port_name.lower()
    
    for keyword, area in area_keywords.items():
        if keyword in port_name_lower:
            return area
    
    return None

CONFIG_SCHEMA = cv.config_entry_only_config_schema("megad")


async def async_setup(hass: HomeAssistant, config: dict):
    """Регистрируем HTTP ручку"""
    hass.http.register_view(MegadHttpView())
    
    # ============ ОПТИМИЗИРОВАННЫЕ СЕРВИСЫ ============

    async def async_handle_megad_reboot(call):
        """Перезагрузка контроллера MegaD через HTTP запрос /sec/?restart=1"""
        entity_id = call.data.get("entity_id", "")
        
        if DOMAIN not in hass.data or ENTRIES not in hass.data[DOMAIN]:
            _LOGGER.error("Интеграция MegaD не найдена")
            return
        
        results = []
        
        for entry_id, coordinator in hass.data[DOMAIN][ENTRIES].items():
            if not coordinator or not coordinator.megad:
                continue
            
            megad_id = coordinator.megad.id if hasattr(coordinator.megad, 'id') else "unknown"
            
            # Фильтр по entity_id если указан
            if entity_id and str(megad_id) not in entity_id:
                continue
            
            try:
                url = coordinator.megad.url.rstrip('/')
                session = async_get_clientsession(hass)
        
                # Получаем IP контроллера
                megad_ip = str(coordinator.megad.config.plc.ip_megad)
                ha_ip = "192.168.31.100:8123"  # Нужно получить из hass.config
        
                # Формируем команду с конфигурацией
                config_params = (
                    f"?cf=1&eip={megad_ip}&emsk=255.255.255.0"
                    f"&pwd=sec&gw=255.255.255.255"
                    f"&sip={ha_ip}&srvt=0&sct=megad"
                    f"&pr=&lp=10&gsm=0&gsmf=1"
                )
                reboot_url = f"{url}/sec/{config_params}"
        
                _LOGGER.info(f"Отправка команды конфигурации на {reboot_url}")
        
                async with session.get(reboot_url, timeout=5) as response:
                    if response.status == 200:
                        result = f"MegaD-{megad_id}: ✅ команда конфигурации отправлена"
                    else:
                        result = f"MegaD-{megad_id}: ❌ ошибка HTTP {response.status}"
                
                results.append(result)
                
            except Exception as e:
                results.append(f"MegaD-{megad_id}: ❌ ошибка: {str(e)}")
        
        # Уведомление
        from homeassistant.components import persistent_notification
        
        message = "Перезагрузка контроллеров:\n\n" + "\n".join(results) if results else "Контроллеры не найдены"
        
        persistent_notification.async_create(
            hass,
            message,
            title="Перезагрузка MegaD",
            notification_id="megad_reboot_results"
        )

    async def async_handle_check_connection(call):
        """Проверка соединения с контроллером через /sec/?cmd=id"""
        entity_id = call.data.get("entity_id", "")
        
        if DOMAIN not in hass.data or ENTRIES not in hass.data[DOMAIN]:
            _LOGGER.error("Интеграция MegaD не найдена")
            return
        
        results = []
        
        for entry_id, coordinator in hass.data[DOMAIN][ENTRIES].items():
            if not coordinator or not coordinator.megad:
                continue
            
            megad_id = coordinator.megad.id if hasattr(coordinator.megad, 'id') else "unknown"
            
            # Фильтр по entity_id если указан
            if entity_id and str(megad_id) not in entity_id:
                continue
            
            try:
                url = coordinator.megad.url.rstrip('/')
                session = async_get_clientsession(hass)
                
                # Проверяем доступность как в PHP скрипте
                test_url = f"{url}/sec/?cmd=id"
                
                async with session.get(test_url, timeout=5) as response:
                    if response.status == 200:
                        text = await response.text()
                        if text and text.strip() and 'timeout' not in text.lower():
                            result = f"MegaD-{megad_id}: ✅ доступен"
                        else:
                            result = f"MegaD-{megad_id}: ⚠️ доступен, но пустой ответ"
                    else:
                        result = f"MegaD-{megad_id}: ❌ HTTP ошибка {response.status}"
                
                results.append(result)
                
            except asyncio.TimeoutError:
                results.append(f"MegaD-{megad_id}: ⏰ таймаут соединения")
            except Exception as e:
                results.append(f"MegaD-{megad_id}: ❌ ошибка: {str(e)}")
        
        # Уведомление
        from homeassistant.components import persistent_notification
        
        message = "Проверка соединения:\n\n" + "\n".join(results) if results else "Контроллеры не найдены"
        
        persistent_notification.async_create(
            hass,
            message,
            title="Проверка соединения MegaD",
            notification_id="megad_connection_check"
        )

    async def async_handle_restore_feedback_simple(call):
        """Простое восстановление обратной связи (перезапуск контроллера)"""
        entity_id = call.data.get("entity_id", "")
        
        if DOMAIN not in hass.data or ENTRIES not in hass.data[DOMAIN]:
            _LOGGER.error("Интеграция MegaD не найдена")
            return
        
        results = []
        
        for entry_id, coordinator in hass.data[DOMAIN][ENTRIES].items():
            if not coordinator or not coordinator.megad:
                continue
            
            megad_id = coordinator.megad.id if hasattr(coordinator.megad, 'id') else "unknown"
            
            # Фильтр по entity_id если указан
            if entity_id and str(megad_id) not in entity_id:
                continue
            
            try:
                url = coordinator.megad.url.rstrip('/')
                session = async_get_clientsession(hass)
                
                # Перезагрузка контроллера
                reboot_url = f"{url}/sec/?restart=1"
                
                _LOGGER.info(f"Восстановление обратной связи для MegaD-{megad_id}")
                
                async with session.get(reboot_url, timeout=5) as response:
                    if response.status == 200:
                        result = f"MegaD-{megad_id}: ✅ команда перезагрузки отправлена"
                    else:
                        result = f"MegaD-{megad_id}: ❌ ошибка отправки команды"
                
                results.append(result)
                
            except Exception as e:
                results.append(f"MegaD-{megad_id}: ❌ ошибка: {str(e)}")
        
        # Уведомление
        from homeassistant.components import persistent_notification
        
        message = "Восстановление обратной связи:\n\n" + "\n".join(results) if results else "Контроллеры не найдены"
        
        persistent_notification.async_create(
            hass,
            message,
            title="Восстановление обратной связи MegaD",
            notification_id="megad_feedback_restore"
        )

    async def async_handle_get_megad_info(call):
        """Получение информации о контроллере"""
        entity_id = call.data.get("entity_id", "")
        
        if DOMAIN not in hass.data or ENTRIES not in hass.data[DOMAIN]:
            _LOGGER.error("Интеграция MegaD не найдена")
            return
        
        results = []
        
        for entry_id, coordinator in hass.data[DOMAIN][ENTRIES].items():
            if not coordinator or not coordinator.megad:
                continue
            
            megad_id = coordinator.megad.id if hasattr(coordinator.megad, 'id') else "unknown"
            
            # Фильтр по entity_id если указан
            if entity_id and str(megad_id) not in entity_id:
                continue
            
            try:
                url = coordinator.megad.url.rstrip('/')
                session = async_get_clientsession(hass)
                
                # Получаем основную информацию
                info_url = f"{url}/sec/?cf=1"
                
                async with session.get(info_url, timeout=5) as response:
                    if response.status == 200:
                        html = await response.text()
                        
                        # Парсим информацию (упрощенно)
                        info = []
                        
                        # ID контроллера
                        info.append(f"ID: {megad_id}")
                        
                        # IP адрес
                        ip = coordinator.megad.config.plc.ip_megad
                        info.append(f"IP: {ip}")
                        
                        # Версия ПО
                        if hasattr(coordinator.megad, 'software') and coordinator.megad.software:
                            info.append(f"Версия ПО: {coordinator.megad.software}")
                        
                        # Статус watchdog
                        if coordinator.watchdog:
                            status = coordinator.watchdog.get_status()
                            inactivity = status.get('inactivity_minutes', 0)
                            info.append(f"Watchdog: {'работает' if status.get('is_running') else 'остановлен'}")
                            info.append(f"Без обратной связи: {inactivity} мин")
                        
                        result = f"MegaD-{megad_id}:\n" + "\n".join(f"  • {i}" for i in info)
                    else:
                        result = f"MegaD-{megad_id}: ❌ ошибка получения информации"
                
                results.append(result)
                
            except Exception as e:
                results.append(f"MegaD-{megad_id}: ❌ ошибка: {str(e)}")
        
        # Уведомление
        from homeassistant.components import persistent_notification
        
        message = "Информация о контроллерах:\n\n" + "\n".join(results) if results else "Контроллеры не найдены"
        
        persistent_notification.async_create(
            hass,
            message,
            title="Информация о MegaD",
            notification_id="megad_info"
        )

    async def async_handle_check_activity(call):
        """Проверка активности контроллеров MegaD."""
        hass = call.hass
        entity_id = call.data.get("entity_id", "")
        
        try:
            if DOMAIN not in hass.data or ENTRIES not in hass.data[DOMAIN]:
                _LOGGER.error("Интеграция MegaD не найдена")
                return
            
            entries = hass.data[DOMAIN][ENTRIES]
            results = []
            timestamp = datetime.now()
            
            for entry_id, coordinator in entries.items():
                if not coordinator or not hasattr(coordinator, 'megad'):
                    continue
                
                megad_id = coordinator.megad.id if hasattr(coordinator.megad, 'id') else "unknown"
                
                # Если указан entity_id, проверяем соответствие
                if entity_id and str(megad_id) not in entity_id:
                    continue
                
                ip_address = getattr(coordinator.megad.config.plc, 'ip_megad', 'unknown')
                
                # Проверяем доступность через watchdog
                is_healthy = False
                watchdog_running = False
                
                if hasattr(coordinator, 'watchdog') and coordinator.watchdog:
                    watchdog_running = coordinator.watchdog._is_running
                    try:
                        is_healthy = await coordinator.watchdog._check_megad_health()
                    except Exception as e:
                        _LOGGER.debug(f"Ошибка проверки доступности MegaD-{megad_id}: {e}")
                
                # Получаем статус обратной связи
                if hasattr(coordinator, 'watchdog') and coordinator.watchdog:
                    status = coordinator.watchdog.get_status()
                    feedback_inactivity = status.get('feedback_inactivity_seconds', 999999)
                    
                    # Определяем символ статуса
                    if not is_healthy:
                        status_symbol = "❌"
                        display_status = "НЕДОСТУПЕН"
                    elif feedback_inactivity > 300:  # 5 минут
                        status_symbol = "⚠️"
                        display_status = "ОБРАТНАЯ СВЯЗЬ НЕ РАБОТАЕТ"
                    else:
                        status_symbol = "✅"
                        display_status = "В РАБОТЕ"
                    
                    result = {
                        "id": megad_id,
                        "ip": ip_address,
                        "symbol": status_symbol,
                        "status": display_status,
                        "healthy": is_healthy,
                        "feedback_inactivity": feedback_inactivity,
                        "watchdog_running": watchdog_running,
                    }
                else:
                    # Если нет watchdog
                    result = {
                        "id": megad_id,
                        "ip": ip_address,
                        "symbol": "❌",
                        "status": "WATCHDOG НЕ ЗАПУЩЕН",
                        "healthy": False,
                        "watchdog_running": False,
                    }
                
                results.append(result)
            
            # Формируем сообщение
            message = f"Проверка активности MegaD\n\n"
            message += f"Время: {timestamp}\n"
            message += f"Всего контроллеров: {len(entries)}\n"
            message += f"Найдено по фильтру: {len(results)}\n\n"
            
            if entity_id:
                message += f"Фильтр: '{entity_id}'\n\n"
            
            if results:
                for result in results:
                    message += f"{result['symbol']} MegaD-{result['id']} ({result['ip']})\n"
                    message += f"   Статус: {result['status']}\n"
                    
                    if result.get('feedback_inactivity') and result['feedback_inactivity'] < 999999:
                        minutes = result['feedback_inactivity'] // 60
                        seconds = result['feedback_inactivity'] % 60
                        if minutes > 0 or seconds > 0:
                            message += f"   Без обратной связи: {minutes} мин {seconds} сек\n"
                    
                    message += f"   Watchdog: {'✅ запущен' if result['watchdog_running'] else '❌ не запущен'}\n"
                    message += f"   Доступность: {'✅ доступен' if result['healthy'] else '❌ недоступен'}\n\n"
            else:
                message += "⚠️ Контроллеры не найдены\n"
            
            # Создаем уведомление
            from homeassistant.components import persistent_notification
            persistent_notification.async_create(
                hass,
                message,
                title="Проверка активности MegaD",
                notification_id=f"megad_check_activity_{int(timestamp.timestamp())}"
            )
            
            _LOGGER.info(f"Проверка активности завершена: найдено {len(results)} контроллеров")
            
        except Exception as e:
            _LOGGER.error(f"Ошибка проверки активности: {e}")

    async def async_handle_sync_states(call):
        """Синхронизация состояний всех сущностей."""
        entity_id = call.data.get("entity_id", "")
        
        if DOMAIN not in hass.data or ENTRIES not in hass.data[DOMAIN]:
            _LOGGER.error("Интеграция MegaD не найдена")
            return
        
        results = []
        
        for entry_id, coordinator in hass.data[DOMAIN][ENTRIES].items():
            if not coordinator or not coordinator.megad:
                continue
            
            megad_id = coordinator.megad.id if hasattr(coordinator.megad, 'id') else "unknown"
            
            # Фильтр по entity_id если указан
            if entity_id and str(megad_id) not in entity_id:
                continue
            
            try:
                await coordinator.force_sync_all_entities()
                results.append(f"MegaD-{megad_id}: ✅ синхронизация завершена")
                
            except Exception as e:
                results.append(f"MegaD-{megad_id}: ❌ ошибка: {str(e)}")
        
        # Создаем уведомление
        from homeassistant.components import persistent_notification
        
        if results:
            message = "Синхронизация состояний:\n\n" + "\n".join(results)
        else:
            message = "Контроллеры не найдены"
        
        persistent_notification.async_create(
            hass,
            message,
            title="Синхронизация MegaD",
            notification_id="megad_sync_states"
        )

    # Регистрируем только необходимые сервисы
    hass.services.async_register(DOMAIN, "megad_reboot", async_handle_megad_reboot)
    hass.services.async_register(DOMAIN, "check_connection", async_handle_check_connection)
    hass.services.async_register(DOMAIN, "restore_feedback", async_handle_restore_feedback_simple)
    hass.services.async_register(DOMAIN, "get_megad_info", async_handle_get_megad_info)
    hass.services.async_register(DOMAIN, "check_activity", async_handle_check_activity)
    hass.services.async_register(DOMAIN, "sync_states", async_handle_sync_states)
    
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
    """Set up MegaD from a config entry."""
    
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
    
    # ✅ ВАЖНО: переменная megad должна быть определена
    megad = MegaD(
        hass=hass,
        config=megad_config,
        url=url,
        config_path=file_path,
        fw_checker=hass.data[DOMAIN][FIRMWARE_CHECKER]
    )
    
    await megad.async_init_i2c_bus()
    await megad.check_local_software()

    # ✅ ТЕПЕРЬ megad определена
    coordinator = MegaDCoordinator(hass=hass, megad=megad)
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN].setdefault(CURRENT_ENTITY_IDS, {})
    hass.data[DOMAIN][CURRENT_ENTITY_IDS][entry_id] = []
    hass.data[DOMAIN][ENTRIES][entry_id] = coordinator
    
    # Регистрируем основное устройство
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, str(megad.id))},
        manufacturer=MANUFACTURER,
        name=f"MegaD-{megad.id}",
        model=f"MegaD Controller",
        sw_version=megad.software if megad.software else "Unknown",
        configuration_url=url,
    )
    _LOGGER.info(f"Основное устройство MegaD-{megad.id} зарегистрировано в реестре устройств")
    
    # Задержка для инициализации
    _LOGGER.info(f"MegaD-{megad.id}: ожидание инициализации (2 секунды)...")
    await asyncio.sleep(2)
    
    # Запускаем watchdog
    await coordinator.start_watchdog()
    
    # Запускаем платформы (ВАЖНО: sensor должна быть в списке PLATFORMS)
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
        self.last_data_received = datetime.now()
        self._updating_watchdog = False
        
        # Сохраняем базовый уникальный ID устройства
        self._device_unique_id = f"{DOMAIN}_{megad.id}"

    def _safe_update_callback_impl(self):
        """Реализация безопасного callback для call_soon."""
        try:
            self.async_update_listeners()
        except Exception as e:
            _LOGGER.error(f"MegaD-{self.megad.id}: ошибка в callback: {e}")
    
    def safe_call_soon_update(self):
        """Безопасный вызов call_soon для обновления слушателей."""
        if (self.hass and 
            hasattr(self.hass, 'loop') and 
            self.hass.loop and 
            hasattr(self, '_safe_update_callback_impl') and 
            self._safe_update_callback_impl is not None and 
            callable(self._safe_update_callback_impl)):
            
            try:
                self.hass.loop.call_soon(self._safe_update_callback_impl)
                return True
            except Exception as e:
                _LOGGER.error(f"MegaD-{self.megad.id}: ошибка safe_call_soon_update: {e}")
                return False
        return False    

    def mark_watchdog_data(self):
        """Отметить получение данных для watchdog."""
        if self.watchdog:
            # Используем mark_data_received для общих данных
            if hasattr(self.watchdog, 'mark_data_received'):
                self.watchdog.mark_data_received()
            else:
                # Запасной вариант
                _LOGGER.warning(f"MegaD-{self.megad.id}: метод mark_data_received не найден в watchdog")
                # Обновляем напрямую
                self.watchdog._last_data_received = datetime.now()

    def device_base_info(self, suggested_area=None):
        """Базовый device_info для всего контроллера с поддержкой областей."""
        megad_id = self.megad.id
        device_info = {
            "identifiers": {(DOMAIN, str(megad_id))},
            "name": f'MegaD-{megad_id}',
            "sw_version": self.megad.software if self.megad.software else "Unknown",
            "configuration_url": self.megad.url,
            "manufacturer": MANUFACTURER,
        }
        
        # Добавляем область если указана
        if suggested_area:
            device_info["suggested_area"] = suggested_area
        
        return DeviceInfo(**device_info)
    
    def entity_device_info(self, entity_name, entity_model=None, entity_type="generic", 
                        port_id=None, extra_port_id=None, suggested_area=None):
        """Создает device_info для отдельных сущностей."""
        megad_id = self.megad.id
    
        # Создаем уникальный идентификатор для устройства сущности
        if extra_port_id:
            unique_device_id = f"{DOMAIN}_{megad_id}_port{port_id}_ext{extra_port_id}"
        elif port_id is not None:
            unique_device_id = f"{DOMAIN}_{megad_id}_port{port_id}"
        else:
            unique_device_id = f"{DOMAIN}_{megad_id}_{entity_type}"
    
        # Уникальный идентификатор для каждой сущности
        identifiers = {(DOMAIN, unique_device_id)}
    
        # Ссылка на основное устройство
        via_device = (DOMAIN, str(megad_id))
    
        # Формируем device_info
        device_info = {
            "identifiers": identifiers,
            "name": entity_name,
            "manufacturer": MANUFACTURER,
            "model": entity_model or f"MegaD-{megad_id} Controller",
            "via_device": via_device,
            "sw_version": self.megad.software if self.megad.software else "Unknown",
            "configuration_url": self.megad.url,
        }
    
        # Добавляем suggested_area если указано
        if suggested_area:
            normalized_area = ' '.join(word.capitalize() for word in str(suggested_area).split())
            device_info["suggested_area"] = normalized_area
    
        # Добавляем model по умолчанию если не указан
        if not entity_model:
            if extra_port_id:
                device_info["model"] = f"MegaD-{megad_id} Port {port_id} Extra {extra_port_id}"
            elif port_id is not None:
                device_info["model"] = f"MegaD-{megad_id} Port {port_id}"
            else:
                device_info["model"] = f"MegaD-{megad_id} {entity_type.replace('_', ' ').title()}"
    
        return DeviceInfo(**device_info)
    
    def create_entity_unique_id(self, port_id, entity_type, extra_port_id=None):
        """Создает уникальный ID для сущности."""
        # Используем entry_id из config_entry
        if hasattr(self, 'config_entry') and self.config_entry:
            entry_id = self.config_entry.entry_id
        else:
            entry_id = "unknown"
    
        megad_id = self.megad.id
    
        # ПРАВИЛЬНЫЙ ФОРМАТ: entry_megad_port_type
        if extra_port_id:
            return f"{entry_id}_{megad_id}_p{port_id}_e{extra_port_id}_{entity_type}"
        else:
            return f"{entry_id}_{megad_id}_p{port_id}_{entity_type}"
    
    def create_group_entity_unique_id(self, group_id, entity_type):
        """Создает уникальный ID для группы."""
        # Получаем entry_id из координатора
        entry_id = None
        for entry_id_key, coordinator in self.hass.data.get(DOMAIN, {}).get(ENTRIES, {}).items():
            if coordinator == self:
                entry_id = entry_id_key
                break
        
        if not entry_id:
            entry_id = "unknown"
        
        return f"{entry_id}-{self.megad.id}-group{group_id}-{entity_type}"
    
    def create_device_entity_unique_id(self, sensor_type):
        """Создает уникальный ID для сенсоров устройства."""
        # Получаем entry_id из координатора
        entry_id = None
        for entry_id_key, coordinator in self.hass.data.get(DOMAIN, {}).get(ENTRIES, {}).items():
            if coordinator == self:
                entry_id = entry_id_key
                break
        
        if not entry_id:
            entry_id = "unknown"
        
        return f"{entry_id}-{self.megad.id}-{sensor_type}"
    
    def create_clean_port_name(self, port_name: str, port_id: int, default_name: str = "Entity") -> str:
        """Очищает имя порта от префикса 'portXX_'."""
        return clean_port_name(port_name, port_id, default_name)
    
    def extract_area_from_port_name(self, port_name: str) -> str | None:
        """Извлекает предполагаемую область из имени порта."""
        return extract_area_from_name(port_name)
    
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
                
                # ✅ ПРАВИЛЬНО: Отмечаем получение общих данных при периодическом обновлении
                # Это НЕ обратная связь, но показывает, что контроллер доступен
                if self.watchdog:
                    self.watchdog.mark_data_received()
                
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
                               f'{self.megad.id}')
        except Exception as err:
            self.megad.is_available = False
            self._was_unavailable = True
            
            # ✅ ПРАВИЛЬНО: Увеличиваем счетчик ошибок в watchdog
            if self.watchdog:
                self.watchdog._failure_count = min(self.watchdog._failure_count + 1, self.watchdog._max_failures)
                _LOGGER.debug(f"MegaD-{self.megad.id}: ошибка обновления, счетчик watchdog: {self.watchdog._failure_count}")
            
            if self._count_connect < COUNTER_CONNECT:
                self._count_connect += 1
                _LOGGER.warning(
                    f'Неудачная попытка обновления данных контроллера '
                    f'id: {self.megad.id}. Ошибка: {err}.'
                    f'Осталось попыток: '
                    f'{COUNTER_CONNECT - self._count_connect + 1}'
                )
                return self.megad
            else:
                raise UpdateFailed(f'Ошибка соединения с контроллера id: '
                                   f'{self.megad.id}: {err}')
    
    async def force_sync_all_entities(self):
        """Принудительная синхронизация всех сущностей."""
        _LOGGER.info(f"Синхронизация сущностей для MegaD-{self.megad.id}")
    
        # Обновляем данные локально
        try:
            if hasattr(self.megad, 'update_data'):
                await self.megad.update_data()
                
                # ❌ УБИРАЕМ: Отмечаем получение общих данных (но НЕ обратной связи!)
                # force_sync - это внутренняя операция HA
                if self.watchdog:
                    self.watchdog.mark_data_received()  # ✅ ТОЛЬКО общие данные, не обратная связь
        except Exception as e:
            _LOGGER.error(f"Ошибка обновления данных при синхронизации: {e}")
    
        # Уведомляем все сущности об обновлении
        self.async_update_listeners()
    
        # Даем время сущностям на обновление
        await asyncio.sleep(1)
    
        # Дополнительное обновление для надежности
        self.async_update_listeners()
    
        _LOGGER.info(f"Синхронизация завершена для MegaD-{self.megad.id}")
    
    async def update_port_state(self, port_id, data, ext=False):
        """Обновление состояния конкретного порта."""
        _LOGGER.debug(f"Обновление состояния порта {port_id}: данные={data}, ext={ext}")
        
        port = self.megad.get_port(port_id)
        if port is None:
            _LOGGER.warning(f"Порт {port_id} не найден")
            return

        if port.conf.type_port in (TypePortMegaD.ADC, ):
            return

        # Обновляем состояние порта
        try:
            if ext and isinstance(data, dict):
                for key, value in data.items():
                    if key.startswith('ext'):
                        ext_id = int(key[3:])
                        ext_port_id = f"{port_id}e{ext_id}"
                        
                        if isinstance(value, dict) and 'v' in value:
                            actual_value = int(value['v'])
                        else:
                            actual_value = value
                        
                        self.megad.update_port(ext_port_id, actual_value)
                        _LOGGER.debug(f"Обновление доп. порта {ext_port_id}: {actual_value}")
            else:
                if isinstance(data, dict) and 'v' in data:
                    actual_data = int(data['v'])
                else:
                    actual_data = data
                
                self.megad.update_port(port_id, actual_data)
                _LOGGER.debug(f"Обновление основного порта {port_id}: {actual_data}")
        except Exception as e:
            _LOGGER.error(f"Ошибка обновления состояния порта {port_id}: {e}")
            return

        # ❌ УБИРАЕМ: self.watchdog.mark_data_received()
        # Это локальное обновление от сущностей HA, не данные от контроллера
        # Watchdog должен получать данные только от реальных HTTP запросов контроллера

        # Обновляем UI
        self.async_update_listeners()
        # self.hass.loop.call_soon(self.async_update_listeners())

        # Если это порт в режиме C или ReaderPort, добавляем задержку выключения
        if isinstance(port, ReaderPort) or (hasattr(port.conf, 'mode') and port.conf.mode == ModeInMegaD.C):
            await self._turn_off_state('off', 0.5, port_id, data)
    
    def mark_feedback_event(self, event_data=None):
        """Метод для отметки событий обратной связи."""
        if not self.watchdog:
            return

        source = event_data.get('source', 'unknown') if event_data else 'unknown'
        event_type = event_data.get('type', 'unknown') if event_data else 'unknown'
        port = event_data.get('pt', 'unknown') if event_data else 'unknown'
    
        # ✅ ИСПРАВЛЕНИЕ: Логируем ВСЕ события для отладки
        _LOGGER.debug(
            f"MegaD-{self.megad.id}: получено событие для mark_feedback_event - "
            f"источник: '{source}', тип: '{event_type}', порт: '{port}'"
        )
    
        # ✅ ИСПРАВЛЕНИЕ: Расширенный список разрешенных источников
        allowed_sources = [
            'http_callback', 'http_get', 'http_post',
            'server_get', 'server_post', 'restore_after_reboot',
            # ✅ ДОБАВЬТЕ ЭТИ НОВЫЕ ИСТОЧНИКИ:
            'coordinator',  # События из координатора (важно!)
            'server', 'megad_server', 'http_server',
            'callback', 'webhook', 'ha_callback'
        ]
    
        if source in allowed_sources:
            _LOGGER.info(f"MegaD-{self.megad.id}: передача события в watchdog (источник: {source})")
            self.watchdog.mark_feedback_event(event_data)
        else:
            _LOGGER.warning(
                f"MegaD-{self.megad.id}: источник '{source}' не разрешен! "
                f"Событие ИГНОРИРУЕТСЯ. Разрешенные: {allowed_sources}"
            )
    
    async def start_watchdog(self):
        """Запуск watchdog для этого контроллера."""
        if not self.watchdog:
            self.watchdog = MegaDWatchdog(self, self.hass)
        await self.watchdog.start()
        _LOGGER.info(f"Watchdog запущен для MegaD-{self.megad.id}")
        
        # ❌ УБИРАЕМ: Искусственное событие обратной связи при инициализации
        # Вместо этого просто отмечаем получение данных
        if self.watchdog:
            self.watchdog.mark_data_received()
            _LOGGER.debug(f"MegaD-{self.megad.id}: watchdog инициализирован")
        
    async def stop_watchdog(self):
        """Остановка watchdog."""
        if self.watchdog:
            await self.watchdog.stop()
            self.watchdog = None
            _LOGGER.info(f"Watchdog остановлен для MegaD-{self.megad.id}")
    
    async def set_recovery_state(self, state: bool):
        """Устанавливает состояние восстановления."""
        self._recovery_in_progress = state
        if state:
            _LOGGER.info(f"MegaD-{self.megad.id}: запущено восстановление")
        else:
            _LOGGER.info(f"MegaD-{self.megad.id}: восстановление завершено")
        if self.watchdog:
            self.watchdog._recovering = state

    async def set_flashing_state(self, state):
        """Устанавливает режим прошивки устройства."""
        self.megad.is_flashing = state
        # self.hass.loop.call_soon(self.async_update_listeners)
        self.last_update_success = not state

    async def _turn_off_state(self, state_off, delay, port_id, data):
        """Возвращает выключенное состояние порта."""
        port = self.megad.get_port(port_id)
        if port:
            if isinstance(port, PWMPortOut):
                state_off = 0
        
        self.megad.update_port(port_id, data)
        # self.hass.loop.call_soon(self.async_update_listeners)
        await asyncio.sleep(delay)
        self.megad.update_port(port_id, state_off)
        self.hass.loop.call_soon(self.async_update_listeners)

    def update_pid_state(self, pid_id: int, data: dict):
        """Обновление состояния ПИД регулятора."""
        self.megad.update_pid(pid_id, data)
        # self.hass.loop.call_soon(self.async_update_listeners)

    def update_set_temperature(self, port_id, temperature):
        """Обновление заданной температуры порта сенсора"""
        port = self.megad.get_port(port_id)
        if isinstance(port, OneWireSensorPort):
            port.conf.set_value = temperature
            _LOGGER.debug(f"Обновлена температура порта {port_id}: {temperature}")
            # self.hass.loop.call_soon(self.async_update_listeners)
        else:
            raise InvalidSettingPort(f'Проверьте настройки порта №{port_id}')

    def update_group_state(self, port_states: dict[int, str]):
        """Обновление состояний портов в группе"""
        _LOGGER.debug(f"Обновление группы портов: {port_states}")
        for port_id, state in port_states.items():
            self.megad.update_port(port_id, state)
        # self.hass.loop.call_soon(self.async_update_listeners)

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
        # self.hass.loop.call_soon(self.async_update_listeners)
        try:
            self.async_update_listeners()
        except Exception as e:
            _LOGGER.error(f"MegaD-{self.megad.id}: ошибка обновления слушателей в restore_status_ports: {e}")
