import asyncio
import logging
import sys
from datetime import datetime, timedelta
from typing import Optional

import async_timeout

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, device_registry as dr
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
    WATCHDOG_RECOVERY_DELAY, WATCHDOG_INACTIVITY_TIMEOUT
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
    
    # Словарь соответствий для автоматического определения области
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
    
    # Регистрация сервисов для watchdog
    async def async_handle_restart_megad(call):
        """Обработчик сервиса перезагрузки MegaD."""
        entity_id = call.data.get("entity_id")
        
        if not entity_id:
            _LOGGER.error("Сервис restart_megad требует параметр entity_id")
            from homeassistant.components import persistent_notification
            persistent_notification.async_create(
                hass,
                "Сервис перезагрузки MegaD требует указания entity_id.",
                title="Ошибка вызова сервиса MegaD",
                notification_id="megad_service_error_no_entity_id"
            )
            return
        
        # Находим координатор по entity_id
        if DOMAIN in hass.data and ENTRIES in hass.data[DOMAIN]:
            for entry_id, coordinator in hass.data[DOMAIN][ENTRIES].items():
                if coordinator and coordinator.watchdog:
                    if coordinator.megad and f"{coordinator.megad.id}" in entity_id:
                        _LOGGER.info(f"Сервис перезагрузки вызван для MegaD-{coordinator.megad.id}")
                        try:
                            # ИСПРАВЛЕНИЕ: используем правильный формат параметров (словарь)
                            reboot_response = await coordinator.megad.request_to_megad({"restart": 1})
                            if reboot_response and reboot_response.status == 200:
                                _LOGGER.info(f"MegaD-{coordinator.megad.id}: команда перезагрузки отправлена")
                                hass.bus.async_fire(
                                    "megad_restarted",
                                    {"megad_id": coordinator.megad.id, "entity_id": entity_id, "success": True}
                                )
                                await asyncio.sleep(90)
                                if coordinator.watchdog:
                                    await coordinator.watchdog.force_check_and_update()
                            else:
                                _LOGGER.error(f"MegaD-{coordinator.megad.id}: не удалось отправить команду перезагрузки")
                                hass.bus.async_fire(
                                    "megad_restarted",
                                    {"megad_id": coordinator.megad.id, "entity_id": entity_id, "success": False}
                                )
                        except Exception as e:
                            _LOGGER.error(f"MegaD-{coordinator.megad.id}: ошибка при перезагрузке: {e}")
                            hass.bus.async_fire(
                                "megad_restarted",
                                {"megad_id": coordinator.megad.id, "entity_id": entity_id, "success": False}
                            )
                        return
        
        _LOGGER.error(f"Не найден контроллер для entity_id: {entity_id}")
    
    async def async_handle_get_status(call):
        """Обработчик сервиса получения статуса watchdog."""
        entity_id = call.data.get("entity_id")
        
        if not entity_id:
            _LOGGER.error("Сервис get_watchdog_status требует параметр entity_id")
            from homeassistant.components import persistent_notification
            persistent_notification.async_create(
                hass,
                "Сервис получения статуса watchdog требует указания entity_id.",
                title="Ошибка вызова сервиса MegaD",
                notification_id="megad_service_error_no_entity_id_status"
            )
            return
        
        if DOMAIN in hass.data and ENTRIES in hass.data[DOMAIN]:
            for entry_id, coordinator in hass.data[DOMAIN][ENTRIES].items():
                if coordinator and coordinator.watchdog:
                    if coordinator.megad and f"{coordinator.megad.id}" in entity_id:
                        status = coordinator.watchdog.get_status()
                        message = f"Статус watchdog MegaD-{coordinator.megad.id}:\n\n"
                        message += f"• Работает: {status.get('is_running', False)}\n"
                        message += f"• Счётчик ошибок: {status.get('failure_count', 0)}/{status.get('max_failures', 3)}\n"
                        message += f"• IP адрес: {status.get('megad_ip', 'unknown')}\n"
                        message += f"• Без данных: {status.get('inactivity_seconds', 0)} сек\n"
                        
                        from homeassistant.components import persistent_notification
                        persistent_notification.async_create(
                            hass,
                            message,
                            title=f"Статус watchdog MegaD-{coordinator.megad.id}",
                            notification_id=f"megad_watchdog_status_{coordinator.megad.id}"
                        )
                        return
        
        _LOGGER.error(f"Не найден контроллер для entity_id: {entity_id}")
    
    async def async_handle_sync_states(call):
        """Обработчик сервиса синхронизации состояний."""
        entity_id = call.data.get("entity_id")
        
        if not entity_id:
            _LOGGER.error("Сервис sync_states требует параметр entity_id")
            return
        
        if DOMAIN in hass.data and ENTRIES in hass.data[DOMAIN]:
            for entry_id, coordinator in hass.data[DOMAIN][ENTRIES].items():
                if coordinator and coordinator.megad:
                    if coordinator.megad and f"{coordinator.megad.id}" in entity_id:
                        await coordinator.force_sync_all_entities()
                        return
        
        _LOGGER.error(f"Не найден контроллер для entity_id: {entity_id}")
    
    async def async_handle_force_check(call):
        """Обработчик сервиса принудительной проверки."""
        entity_id = call.data.get("entity_id")
        
        found_any = False
        
        if DOMAIN in hass.data and ENTRIES in hass.data[DOMAIN]:
            for entry_id, coordinator in hass.data[DOMAIN][ENTRIES].items():
                if coordinator and coordinator.watchdog:
                    if coordinator.megad:
                        if not entity_id or f"{coordinator.megad.id}" in entity_id:
                            found_any = True
                            _LOGGER.info(f"Принудительная проверка MegaD-{coordinator.megad.id}")
                            await coordinator.watchdog.force_check_and_update()
                            if entity_id:
                                return
        
        if entity_id and not found_any:
            _LOGGER.error(f"Не найден контроллер для entity_id: {entity_id}")
        elif not entity_id and not found_any:
            _LOGGER.warning("Нет активных контроллеров MegaD для принудительной проверки")
    
    async def async_handle_check_activity(call):
        """Проверка активности контроллеров MegaD."""
        hass = call.hass
        entity_id = call.data.get("entity_id", "")
        
        try:
            if DOMAIN not in hass.data:
                _LOGGER.error("Интеграция MegaD не найдена")
                from homeassistant.components import persistent_notification
                persistent_notification.async_create(
                    hass,
                    "Интеграция MegaD не найдена в системе.",
                    title="Ошибка проверки активности",
                    notification_id="megad_check_activity_error_no_domain"
                )
                return
                
            if ENTRIES not in hass.data[DOMAIN]:
                _LOGGER.error("Нет активных контроллеров MegaD")
                from homeassistant.components import persistent_notification
                persistent_notification.async_create(
                    hass,
                    "Нет активных контроллеров MegaD в системе.",
                    title="Ошибка проверки активности",
                    notification_id="megad_check_activity_error_no_entries"
                )
                return
                
            entries = hass.data[DOMAIN][ENTRIES]
            results = []
            timestamp = datetime.now()
            
            for entry_id, coordinator in entries.items():
                if not coordinator or not hasattr(coordinator, 'megad'):
                    continue
                    
                try:
                    megad_id = coordinator.megad.id if hasattr(coordinator.megad, 'id') else "unknown"
                    
                    # Если указан entity_id, проверяем соответствие
                    if entity_id:
                        # Проверяем все возможные форматы ID
                        possible_ids = [
                            f"{megad_id}",
                            f"MegaD-{megad_id}",
                            f"megad-{megad_id}",
                            f"megad_{megad_id}",
                            f"mega_d_{megad_id}",
                        ]
                        
                        if not any(id_format in entity_id for id_format in possible_ids):
                            continue
                    
                    ip_address = getattr(coordinator.megad.config.plc, 'ip_megad', 'unknown')
                    
                    # Проверяем доступность через watchdog
                    is_available = False
                    is_healthy = False
                    watchdog_running = False
                    
                    if hasattr(coordinator, 'watchdog') and coordinator.watchdog:
                        watchdog_running = coordinator.watchdog._is_running
                        try:
                            is_healthy = await coordinator.watchdog._check_megad_health_basic()
                            is_available = is_healthy
                        except Exception as e:
                            _LOGGER.debug(f"Ошибка проверки доступности MegaD-{megad_id}: {e}")
                    
                    # Получаем детальный статус
                    if hasattr(coordinator, 'watchdog') and coordinator.watchdog:
                        try:
                            activity_status = await coordinator.watchdog.get_activity_status()
                            
                            display_status = activity_status.get('display_status', 'НЕИЗВЕСТНО')
                            display_description = activity_status.get('display_description', '')
                            is_active = activity_status.get('is_active', False)
                            show_warning = activity_status.get('show_warning', False)
                            inactivity_seconds = activity_status.get('inactivity_seconds', 0)
                            feedback_ok = activity_status.get('feedback_ok', False)
                            
                            # Определяем символ статуса
                            if not is_healthy:
                                status_symbol = "❌"
                            elif show_warning:
                                status_symbol = "⚠️"
                            elif is_active:
                                status_symbol = "✅"
                            else:
                                status_symbol = "⏸️"
                            
                            result = {
                                "id": megad_id,
                                "ip": ip_address,
                                "symbol": status_symbol,
                                "status": display_status,
                                "description": display_description,
                                "healthy": is_healthy,
                                "active": is_active,
                                "warning": show_warning,
                                "inactivity_seconds": inactivity_seconds,
                                "feedback_ok": feedback_ok,
                                "watchdog_running": watchdog_running,
                            }
                            
                        except Exception as e:
                            _LOGGER.error(f"Ошибка получения детального статуса для MegaD-{megad_id}: {e}")
                            result = {
                                "id": megad_id,
                                "ip": ip_address,
                                "symbol": "✅" if is_healthy else "❌",
                                "status": "✅ В РАБОТЕ" if is_healthy else "❌ НЕДОСТУПЕН",
                                "healthy": is_healthy,
                                "watchdog_running": watchdog_running,
                            }
                    else:
                        # Если нет watchdog, показываем простой статус
                        result = {
                            "id": megad_id,
                            "ip": ip_address,
                            "symbol": "✅" if is_healthy else "❌",
                            "status": "✅ В РАБОТЕ" if is_healthy else "❌ НЕДОСТУПЕН",
                            "healthy": is_healthy,
                            "watchdog_running": watchdog_running,
                        }
                    
                    results.append(result)
                    
                except Exception as e:
                    _LOGGER.error(f"Ошибка обработки контроллера {entry_id}: {e}")
                    continue
            
            # Формируем сообщение
            message = f"🔄 Проверка активности MegaD\n\n"
            message += f"Время: {timestamp}\n"
            message += f"Всего контроллеров в системе: {len(entries)}\n"
            message += f"Найдено по фильтру: {len(results)}\n\n"
            
            if entity_id:
                message += f"Фильтр: entity_id содержит '{entity_id}'\n\n"
            
            if results:
                for result in results:
                    message += f"{result['symbol']} MegaD-{result['id']} ({result['ip']})\n"
                    message += f"   Статус: {result['status']}\n"
                    
                    if result.get('description'):
                        message += f"   {result['description']}\n"
                    
                    if 'inactivity_seconds' in result:
                        minutes = int(result['inactivity_seconds'] / 60)
                        message += f"   Без данных: {minutes} минут\n"
                    
                    if 'feedback_ok' in result:
                        message += f"   Обратная связь: {'✅ работает' if result['feedback_ok'] else '❌ не работает'}\n"
                    
                    message += f"   Watchdog: {'✅ запущен' if result['watchdog_running'] else '❌ не запущен'}\n"
                    message += f"   Доступность: {'✅ доступен' if result['healthy'] else '❌ недоступен'}\n"
                    
                    message += "\n"
            else:
                message += "⚠️ Контроллеры не найдены\n"
                
                # Показываем все доступные контроллеры
                if entity_id:
                    message += f"\nДоступные контроллеры:\n"
                    for entry_id, coordinator in entries.items():
                        if coordinator and hasattr(coordinator, 'megad'):
                            megad_id = coordinator.megad.id if hasattr(coordinator.megad, 'id') else "unknown"
                            ip_address = getattr(coordinator.megad.config.plc, 'ip_megad', 'unknown')
                            message += f"• MegaD-{megad_id} ({ip_address})\n"
                    
                    message += f"\nПопробуйте вызвать сервис без entity_id или с одним из этих значений:\n"
                    message += f"• megad.check_activity (без параметров)\n"
                    message += f"• megad.check_activity с entity_id: 'megad'\n"
                    message += f"• megad.check_activity с entity_id: 'MegaD-megad'\n"
            
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
            from homeassistant.components import persistent_notification
            persistent_notification.async_create(
                hass,
                f"Ошибка при проверке активности MegaD:\n\n{str(e)}",
                title="Ошибка проверки активности MegaD",
                notification_id=f"megad_check_activity_error_{int(datetime.now().timestamp())}"
            )
    
    async def async_handle_check_all_megad(call):
        """Упрощенная проверка всех контроллеров."""
        hass = call.hass
        
        try:
            if DOMAIN not in hass.data or ENTRIES not in hass.data[DOMAIN]:
                _LOGGER.error("Интеграция MegaD не найдена")
                return
            
            entries = hass.data[DOMAIN][ENTRIES]
            message = f"Проверка всех контроллеров MegaD:\n\n"
            message += f"Всего контроллеров в системе: {len(entries)}\n\n"
            
            for entry_id, coordinator in entries.items():
                if coordinator and hasattr(coordinator, 'megad'):
                    megad_id = coordinator.megad.id if hasattr(coordinator.megad, 'id') else "unknown"
                    ip_address = getattr(coordinator.megad.config.plc, 'ip_megad', 'unknown')
                    watchdog_running = hasattr(coordinator, 'watchdog') and coordinator.watchdog and coordinator.watchdog._is_running
                    
                    message += f"• MegaD-{megad_id} ({ip_address}): "
                    message += f"watchdog={'✅ работает' if watchdog_running else '❌ не работает'}\n"
            
            from homeassistant.components import persistent_notification
            persistent_notification.async_create(
                hass,
                message,
                title="Проверка всех контроллеров MegaD",
                notification_id=f"megad_all_check_{int(datetime.now().timestamp())}"
            )
            
        except Exception as e:
            _LOGGER.error(f"Ошибка в сервисе check_all_megad: {e}")
    
    async def async_handle_diagnose_megad(call):
        """Диагностика интеграции MegaD."""
        hass = call.hass
        message = "Диагностика интеграции MegaD:\n\n"
        
        # Проверяем наличие DOMAIN
        if DOMAIN not in hass.data:
            message += "❌ Интеграция MegaD не найдена в hass.data\n"
        else:
            message += "✅ Интеграция MegaD найдена\n"
            
            domain_data = hass.data[DOMAIN]
            message += f"Ключи в hass.data['{DOMAIN}']:\n"
            for key in domain_data.keys():
                message += f"• {key}\n"
            message += "\n"
            
            # Проверка ENTRIES
            if ENTRIES in domain_data:
                entries = domain_data[ENTRIES]
                message += f"✅ ENTRIES найдены: {len(entries)} записей\n\n"
                
                for entry_id, coordinator in entries.items():
                    message += f"Entry ID: {entry_id}\n"
                    message += f"• Тип: {type(coordinator)}\n"
                    
                    if coordinator is None:
                        message += "• ❌ Координатор равен None\n"
                    else:
                        message += "• ✅ Координатор не None\n"
                        
                        if hasattr(coordinator, 'megad'):
                            message += "• ✅ Есть атрибут 'megad'\n"
                            if hasattr(coordinator.megad, 'id'):
                                message += f"• ID контроллера: {coordinator.megad.id}\n"
                        else:
                            message += "• ❌ Нет атрибута 'megad'\n"
                            
                        if hasattr(coordinator, 'watchdog'):
                            message += "• ✅ Есть атрибут 'watchdog'\n"
                        else:
                            message += "• ❌ Нет атрибута 'watchdog'\n"
                    
                    message += "\n"
            else:
                message += "❌ ENTRIES не найдены\n"
        
        from homeassistant.components import persistent_notification
        persistent_notification.async_create(
            hass,
            message,
            title="Диагностика интеграции MegaD",
            notification_id=f"megad_diagnose_{int(datetime.now().timestamp())}"
        )
    
    async def async_perform_regular_activity_check(hass: HomeAssistant):
        """Выполняет регулярную проверку активности всех контроллеров MegaD."""
        try:
            _LOGGER.info("Запуск автоматической проверки активности MegaD")
            
            # Создаем простое уведомление о начале проверки
            from homeassistant.components import persistent_notification
            timestamp = datetime.now().strftime("%H:%M:%S")
            
            message = f"🔄 Автоматическая проверка активности MegaD\n\n"
            message += f"Время начала: {timestamp}\n"
            
            if DOMAIN not in hass.data or ENTRIES not in hass.data[DOMAIN]:
                message += "❌ Интеграция MegaD не найдена\n"
                persistent_notification.async_create(
                    hass,
                    message,
                    title="Автопроверка MegaD",
                    notification_id=f"megad_auto_check_error_{int(datetime.now().timestamp())}"
                )
                return
            
            entries = hass.data[DOMAIN][ENTRIES]
            message += f"Контроллеров в системе: {len(entries)}\n\n"
            
            if not entries:
                message += "⚠️ Нет активных контроллеров MegaD\n"
                persistent_notification.async_create(
                    hass,
                    message,
                    title="Автопроверка MegaD",
                    notification_id=f"megad_auto_check_empty_{int(datetime.now().timestamp())}"
                )
                return
            
            healthy_count = 0
            warning_count = 0
            error_count = 0
            
            for entry_id, coordinator in entries.items():
                if not coordinator or not hasattr(coordinator, 'megad'):
                    continue
                    
                try:
                    megad_id = coordinator.megad.id if hasattr(coordinator.megad, 'id') else "unknown"
                    ip_address = getattr(coordinator.megad.config.plc, 'ip_megad', 'unknown')
                    
                    # Проверяем доступность через watchdog если он существует
                    if hasattr(coordinator, 'watchdog') and coordinator.watchdog:
                        try:
                            # Получаем детальный статус
                            activity_status = await coordinator.watchdog.get_activity_status()
                            
                            is_healthy = activity_status.get('is_healthy', False)
                            show_warning = activity_status.get('show_warning', False)
                            display_status = activity_status.get('display_status', 'НЕИЗВЕСТНО')
                            
                            if is_healthy and not show_warning:
                                healthy_count += 1
                                status_symbol = "✅"
                            elif is_healthy and show_warning:
                                warning_count += 1
                                status_symbol = "⚠️"
                            else:
                                error_count += 1
                                status_symbol = "❌"
                            
                            message += f"{status_symbol} MegaD-{megad_id}: {display_status}\n"
                            
                        except Exception as e:
                            _LOGGER.error(f"Ошибка получения статуса для {megad_id}: {e}")
                            error_count += 1
                            message += f"❌ MegaD-{megad_id}: Ошибка получения статуса\n"
                    else:
                        # Если нет watchdog, просто проверяем ping
                        if hasattr(coordinator, 'watchdog') and coordinator.watchdog:
                            is_healthy = await coordinator.watchdog._check_megad_health_basic()
                        else:
                            is_healthy = False
                        
                        if is_healthy:
                            healthy_count += 1
                            message += f"✅ MegaD-{megad_id}: Доступен (ping)\n"
                        else:
                            error_count += 1
                            message += f"❌ MegaD-{megad_id}: Недоступен\n"
                            
                except Exception as e:
                    _LOGGER.error(f"Ошибка проверки контроллера {entry_id}: {e}")
                    error_count += 1
                    message += f"❌ Entry {entry_id}: Ошибка проверки\n"
            
            # Итоговая статистика
            message += f"\n📊 ИТОГ:\n"
            message += f"✅ Работают нормально: {healthy_count}\n"
            message += f"⚠️ Требуют внимания: {warning_count}\n"
            message += f"❌ Проблемы: {error_count}\n"
            
            # Время окончания
            end_time = datetime.now().strftime("%H:%M:%S")
            message += f"\nВремя окончания: {end_time}\n"
            
            # Создаем уведомление
            persistent_notification.async_create(
                hass,
                message,
                title="Автопроверка активности MegaD",
                notification_id=f"megad_auto_check_result_{int(datetime.now().timestamp())}"
            )
            
            _LOGGER.info(f"Автоматическая проверка завершена: {healthy_count}✅ {warning_count}⚠️ {error_count}❌")
            
        except Exception as e:
            _LOGGER.error(f"Ошибка при автоматической проверке активности: {e}")
    
    async def async_handle_auto_check_megad(call):
        """Обработчик для автоматической проверки всех контроллеров."""
        hass = call.hass
        await async_perform_regular_activity_check(hass)
    
    # Регистрируем только работающие сервисы
    hass.services.async_register(DOMAIN, "restart_megad", async_handle_restart_megad)
    hass.services.async_register(DOMAIN, "get_watchdog_status", async_handle_get_status)
    hass.services.async_register(DOMAIN, "force_check_megad", async_handle_force_check)
    hass.services.async_register(DOMAIN, "check_activity", async_handle_check_activity)
    hass.services.async_register(DOMAIN, "check_all_megad", async_handle_check_all_megad)
    hass.services.async_register(DOMAIN, "diagnose_megad", async_handle_diagnose_megad)
    hass.services.async_register(DOMAIN, "auto_check_megad", async_handle_auto_check_megad)
    
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
    
    # ✅ Регистрируем основное устройство
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
    
    # Запускаем платформы
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
        
        # Сохраняем базовый уникальный ID устройства
        self._device_unique_id = f"{DOMAIN}_{megad.id}"

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
        
        # ✅ Добавляем область если указана
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
    
        # ✅ Ссылка на основное устройство (должно быть уже зарегистрировано)
        via_device = (DOMAIN, str(megad_id))
    
        # Формируем device_info
        device_info = {
            "identifiers": identifiers,
            "name": entity_name,
            "manufacturer": MANUFACTURER,
            "model": entity_model or f"MegaD-{megad_id} Controller",
            "via_device": via_device,  # ✅ Связь с основным контроллером
            "sw_version": self.megad.software if self.megad.software else "Unknown",
            "configuration_url": self.megad.url,
        }
    
        # ✅ ДОБАВЛЯЕМ suggested_area если указано
        # if suggested_area:
            # Нормализуем область (убираем лишние пробелы, делаем заглавными первые буквы)
            # normalized_area = ' '.join(word.capitalize() for word in str(suggested_area).split())
            # device_info["suggested_area"] = normalized_area
    
        # ✅ ДОБАВЛЯЕМ model по умолчанию если не указан
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
    
        # ✅ ПРАВИЛЬНЫЙ ФОРМАТ: entry_megad_port_type
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
    
    
    async def update_megad_references(self, new_megad_id: str = None):
        """Обновляет все ссылки на контроллер при изменении его ID."""
        _LOGGER.info(f"MegaD-{self.megad.id}: обновление ссылки на контроллер")
        
        # Обновляем ID контроллера если указан
        if new_megad_id:
            old_id = self.megad.id
            self.megad.id = new_megad_id
            _LOGGER.info(f"ID контроллера изменен с {old_id} на {new_megad_id}")
        
        # Обновляем все слушатели
        self.async_update_listeners()
        
        # Принудительно обновляем UI
        self.hass.loop.call_soon(self.async_update_listeners)
        
        _LOGGER.info(f"Ссылки на контроллер обновлены")
    
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

            # ИСПРАВЛЕНИЕ: поддержка новых версий Python (3.11+) и старых
            if sys.version_info >= (3, 11):
                # Для Python 3.11+ используем встроенный asyncio.timeout
                async with asyncio.timeout(TIME_OUT_UPDATE_DATA_GENERAL):
                    await self.megad.update_data()
            else:
                # Fallback для старых версий Python
                async with async_timeout.timeout(TIME_OUT_UPDATE_DATA_GENERAL):
                    await self.megad.update_data()

            self._count_connect = 0
            self.megad.is_available = True

            # ✅ ВАЖНОЕ ИСПРАВЛЕНИЕ: ОТМЕЧАЕМ ПОЛУЧЕНИЕ ДАННЫХ ДЛЯ WATCHDOG
            if self.watchdog:
                self.watchdog.mark_data_received()
                # ✅ ТАКЖЕ ОТМЕЧАЕМ КАК СОБЫТИЕ ОБРАТНОЙ СВЯЗИ
                self.watchdog.mark_feedback_event({"type": "periodic_update", "source": "coordinator"})
                self.watchdog._failure_count = 0
                self.watchdog._last_success = datetime.now()
                self.watchdog._was_offline = False
                _LOGGER.debug(f"MegaD-{self.megad.id}: данные обновлены, watchdog сброшен")

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

            # ✅ УВЕДОМЛЯЕМ WATCHDOG О ПРОБЛЕМЕ
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
        _LOGGER.info(f"Синхронизация сущностей для MegaD-{self.megad.id}")
    
        # НЕ вызываем async_refresh() - это вызывает конфликт!
        # Вместо этого просто обновляем UI
    
        # 1. Обновляем данные локально (если нужно)
        try:
            # Можно обновить только локальные данные без вызова async_refresh
            if hasattr(self.megad, 'update_data'):
                await self.megad.update_data()
                
                # ✅ ОТМЕЧАЕМ ПОЛУЧЕНИЕ ДАННЫХ ДЛЯ WATCHDOG
                if self.watchdog:
                    self.watchdog.mark_data_received()
                    self.watchdog.mark_feedback_event({"type": "sync_update", "source": "force_sync"})
        except Exception as e:
            _LOGGER.error(f"Ошибка обновления данных при синхронизации: {e}")
    
        # 2. Уведомляем все сущности об обновлении
        self.async_update_listeners()
    
        # 3. Даем время сущностям на обновление
        await asyncio.sleep(1)
    
        # 4. Дополнительное обновление для надежности
        self.async_update_listeners()
    
        _LOGGER.info(f"Синхронизация завершена для MegaD-{self.megad.id}")

    async def start_watchdog(self):
        """Запуск watchdog для этого контроллера."""
        if not self.watchdog:
            self.watchdog = MegaDWatchdog(self, self.hass)
        await self.watchdog.start()
        _LOGGER.info(f"Watchdog запущен для MegaD-{self.megad.id}")
        
        # ДОБАВЬТЕ ЭТОТ ОТЛАДОЧНЫЙ ВЫВОД:
        _LOGGER.info(f"Watchdog состояние: is_running={self.watchdog._is_running}, "
                     f"last_incoming_data={self.watchdog._last_incoming_data}, "
                     f"inactivity_timeout={self.watchdog._inactivity_timeout}, "
                     f"feedback_enabled={self.watchdog._feedback_enabled}")
        
        # ✅ ИНИЦИАЛИЗИРУЕМ ПОСЛЕДНЕЕ СОБЫТИЕ ОБРАТНОЙ СВЯЗИ
        self.watchdog.mark_feedback_event({"type": "initialization", "source": "start_watchdog"})
        
    async def stop_watchdog(self):
        """Остановка watchdog."""
        if self.watchdog:
            await self.watchdog.stop()
            self.watchdog = None
            _LOGGER.info(f"Watchdog остановлен для MegaD-{self.megad.id}")
    
    def mark_watchdog_data_received(self):
        """Публичный метод для отметки получения данных в watchdog."""
        self.last_data_received = datetime.now()
        if self.watchdog:
            self.watchdog.mark_data_received()
            # ✅ ТАКЖЕ ОТМЕЧАЕМ КАК СОБЫТИЕ ОБРАТНОЙ СВЯЗИ
            self.watchdog.mark_feedback_event({"type": "manual_mark", "source": "mark_watchdog_data_received"})
            _LOGGER.debug(f"MegaD-{self.megad.id}: данные получены, watchdog обновлен")
    
    def mark_feedback_event(self, event_data=None):
        """Метод для отметки событий обратной связи."""
        if self.watchdog:
            self.watchdog.mark_feedback_event(event_data)
            _LOGGER.debug(f"MegaD-{self.megad.id}: отмечено событие обратной связи")
    
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
                    # ЕСЛИ data - это словарь, извлекаем значение 'v'
                    if isinstance(data, dict) and 'v' in data:
                        data = int(data['v'])
                        _LOGGER.debug(f"Извлечено значение из JSON для порта {port_id}: {data}")
                    else:
                        data = int(data)
                except (ValueError, TypeError) as e:
                    _LOGGER.error(f"Некорректное значение для ШИМ порта {port_id}: {data}, ошибка: {e}")
                    return
            
            # ВАЖНО: Обновляем состояние порта вручную для немедленного отображения
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
                        
                        # ИСПРАВЛЕНИЕ: Если value - это словарь, извлекаем значение 'v'
                        if isinstance(value, dict) and 'v' in value:
                            actual_value = int(value['v'])
                            _LOGGER.debug(f"Извлечение значения из JSON для доп. порта {ext_port_id}: {value} -> {actual_value}")
                        else:
                            actual_value = value
                        
                        # Обновляем состояние доп. порта
                        self.megad.update_port(ext_port_id, actual_value)
                        _LOGGER.debug(f"Обновление доп. порта {ext_port_id}: {actual_value}")
                        
                        # Обновляем состояние для дополнительных ШИМ портов
                        if hasattr(port, '_state') and isinstance(port._state, dict):
                            port._state[ext_id] = actual_value
            else:
                # Для основных портов
                # ИСПРАВЛЕНИЕ: Если data - это словарь для основного порта
                if isinstance(data, dict) and 'v' in data:
                    actual_data = int(data['v'])
                    _LOGGER.debug(f"Извлечение значения из JSON для основного порта {port_id}: {data} -> {actual_data}")
                else:
                    actual_data = data
                
                self.megad.update_port(port_id, actual_data)
                _LOGGER.debug(f"Обновление основного порта {port_id}: {actual_data}")
                
                # Для ШИМ портов дополнительно обновляем через общий метод
                if isinstance(port, PWMPortOut):
                    # Вызываем update_port для мегада
                    self.megad.update_port(port_id, actual_data)
        except Exception as e:
            _LOGGER.error(f"Ошибка обновления состояния порта {port_id}: {e}")
            return

        # ✅ ОТМЕЧАЕМ СОБЫТИЕ ОБРАТНОЙ СВЯЗИ ПРИ ИЗМЕНЕНИИ СОСТОЯНИЯ ПОРТА
        if self.watchdog:
            self.watchdog.mark_feedback_event({
                "type": "port_update", 
                "port_id": port_id, 
                "data": data,
                "ext": ext
            })
            _LOGGER.debug(f"MegaD-{self.megad.id}: отмечено событие обратной связи для порта {port_id}")

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
            await asyncio.sleep(2)
            try:
                # await self.async_refresh()
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
