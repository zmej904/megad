import logging
import ipaddress
from datetime import datetime
from http import HTTPStatus

from aiohttp.web_request import Request
from aiohttp.web_response import Response

from homeassistant.components.http import HomeAssistantView
from .const_parse import EXTRA
from ..const import DOMAIN, ENTRIES, MEGAD_ID, MEGAD_STATE, PORT_ID

_LOGGER = logging.getLogger(__name__)


class MegadHttpView(HomeAssistantView):
    """Класс представления HTTP для обработки запросов."""

    url = '/megad'
    name = 'megad'
    requires_auth = False

    @staticmethod
    async def restore_after_reboot(coordinator):
        """Восстановление состояния контроллера после перезагрузки"""
        _LOGGER.info(f"MegaD-{coordinator.megad.id}: восстановление после перезагрузки")
        await coordinator.restore_status_ports()
        await coordinator.megad.set_current_time()
        
        # ✅ ОТМЕЧАЕМ СОБЫТИЕ ВОССТАНОВЛЕНИЯ В WATCHDOG
        if hasattr(coordinator, 'watchdog') and coordinator.watchdog:
            coordinator.watchdog.mark_feedback_event({
                "type": "restore_after_reboot",
                "megad_id": coordinator.megad.id,
                "timestamp": datetime.now().isoformat()
            })
            _LOGGER.debug(f"MegaD-{coordinator.megad.id}: watchdog обновлен после восстановления")

    def _find_coordinator(self, host, id_megad, hass):
        """Найти координатор по host или ID."""
        entry_ids = hass.data[DOMAIN][ENTRIES]
        
        for entry_id in entry_ids:
            coordinator_temp = hass.data[DOMAIN][ENTRIES][entry_id]
            if coordinator_temp is None:
                continue
                
            try:
                megad_id = coordinator_temp.megad.id if hasattr(coordinator_temp.megad, 'id') else "unknown"
                
                # 1. По ID из запроса (самый надежный способ)
                if id_megad and str(coordinator_temp.megad.id) == id_megad:
                    _LOGGER.info(f"Найден контроллер по ID: MegaD-{megad_id} (ID из запроса: {id_megad})")
                    return coordinator_temp
                
                # 2. По домену или URL из конфигурации
                if hasattr(coordinator_temp.megad, 'url'):
                    url = coordinator_temp.megad.url
                    # Извлекаем хост из URL
                    from urllib.parse import urlparse
                    parsed = urlparse(url)
                    config_host = parsed.hostname
                    
                    if config_host and self._hosts_match(config_host, host):
                        _LOGGER.info(f"Найден контроллер по URL: MegaD-{megad_id} (URL: {url}, host: {host})")
                        return coordinator_temp
                
                # 3. По IP из конфига MegaD
                if hasattr(coordinator_temp.megad, 'config') and hasattr(coordinator_temp.megad.config.plc, 'ip_megad'):
                    config_ip = str(coordinator_temp.megad.config.plc.ip_megad)
                    if self._hosts_match(config_ip, host):
                        _LOGGER.info(f"Найден контроллер по IP: MegaD-{megad_id} (IP: {config_ip}, host: {host})")
                        return coordinator_temp
                
                # 4. По домену из конфигурации (старый способ)
                if hasattr(coordinator_temp.megad, 'domain') and self._hosts_match(coordinator_temp.megad.domain, host):
                    _LOGGER.info(f"Найден контроллер по domain: MegaD-{megad_id} (domain: {coordinator_temp.megad.domain})")
                    return coordinator_temp
                    
            except AttributeError as e:
                _LOGGER.warning(f'Ошибка при поиске координатора: {e}')
        
        return None
    
    def _hosts_match(self, host1, host2):
        """Сравнить два хоста с учетом возможных преобразований."""
        if not host1 or not host2:
            return False
        
        # Привести к нижнему регистру
        host1 = str(host1).lower().strip()
        host2 = str(host2).lower().strip()
        
        # Если это IP адреса, сравнить напрямую
        try:
            ip1 = ipaddress.ip_address(host1)
            ip2 = ipaddress.ip_address(host2)
            return ip1 == ip2
        except ValueError:
            pass
        
        # Убрать порт если есть
        host1 = host1.split(':')[0]
        host2 = host2.split(':')[0]
        
        # Проверить локальные адреса
        local_aliases = ['127.0.0.1', 'localhost', '0.0.0.0']
        if host1 in local_aliases and host2 in local_aliases:
            return True
        
        return host1 == host2

    async def get(self, request: Request):
        """Обрабатываем GET-запрос."""
        host = request.remote
        params: dict = dict(request.query)
        
        # ✅ ЛОГИРОВАНИЕ ВСЕХ ПАРАМЕТРОВ ДЛЯ ДИАГНОСТИКИ
        _LOGGER.info(f"📨 HTTP GET запрос от {host}: {params}")
        
        hass = request.app['hass']
        
        if hass.data.get(DOMAIN) is None:
            _LOGGER.warning(f'Интеграция MegaD не загружена')
            return Response(status=HTTPStatus.NOT_FOUND)
            
        id_megad = params.get(MEGAD_ID)
        state_megad = params.get(MEGAD_STATE)
        ext = any(EXTRA in key for key in params)
        port_id = params.get(PORT_ID)
        
        _LOGGER.debug(f"Поиск контроллера: host={host}, id_megad={id_megad}, port_id={port_id}")
        
        # Ищем координатор
        coordinator = self._find_coordinator(host, id_megad, hass)

        if coordinator is None:
            # ✅ ЛОГИРУЕМ ВСЕ ДОСТУПНЫЕ КОНТРОЛЛЕРЫ ДЛЯ ДИАГНОСТИКИ
            entry_ids = hass.data[DOMAIN][ENTRIES]
            _LOGGER.warning(f'Контроллер {host} не найден! Доступные контроллеры:')
            for entry_id in entry_ids:
                coordinator_temp = hass.data[DOMAIN][ENTRIES][entry_id]
                if coordinator_temp:
                    try:
                        megad_id = coordinator_temp.megad.id
                        url = getattr(coordinator_temp.megad, 'url', 'unknown')
                        domain = getattr(coordinator_temp.megad, 'domain', 'unknown')
                        ip_megad = getattr(coordinator_temp.megad.config.plc, 'ip_megad', 'unknown') if hasattr(coordinator_temp.megad, 'config') else 'unknown'
                        
                        _LOGGER.warning(f"  - MegaD-{megad_id}: url={url}, domain={domain}, ip={ip_megad}")
                    except Exception as e:
                        _LOGGER.warning(f"  - Ошибка получения информации: {e}")
            
            return Response(status=HTTPStatus.NOT_FOUND)

        megad_id = coordinator.megad.id if hasattr(coordinator.megad, 'id') else "unknown"
        _LOGGER.info(f"✅ Найден контроллер: MegaD-{megad_id}")

        # ✅ КЛЮЧЕВОЙ МОМЕНТ: ОТМЕЧАЕМ ОБРАТНУЮ СВЯЗЬ В WATCHDOG
        try:
            # Проверяем, есть ли watchdog
            if hasattr(coordinator, 'watchdog') and coordinator.watchdog:
                # ✅ ВАЖНО: Всегда вызываем mark_feedback_event для HTTP запросов
                # Это ОБРАТНАЯ СВЯЗЬ от контроллера
                coordinator.watchdog.mark_feedback_event({
                    "type": "http_callback",
                    "megad_id": megad_id,
                    "host": host,
                    "port_id": port_id,
                    "state_megad": state_megad,
                    "params": {k: v for k, v in params.items() if k not in ['password', 'auth']},
                    "timestamp": datetime.now().isoformat(),
                    "source": "server_get",
                    "message": "Контроллер отправил обратную связь"
                })
                
                _LOGGER.info(
                    f"MegaD-{megad_id}: ✅ ОБРАТНАЯ СВЯЗЬ ОТМЕЧЕНА! "
                    f"(host: {host}, port: {port_id}, source: server_get)"
                )
           
            # Используем публичный метод координатора
            if hasattr(coordinator, 'mark_feedback_event'):
                coordinator.mark_feedback_event({
                    "type": "http_get",
                    "host": host,
                    "port_id": port_id,
                    "source": "coordinator"
                })
                
        except Exception as e:
            _LOGGER.error(f"MegaD-{megad_id}: ОШИБКА при отметке обратной связи: {e}")
            import traceback
            _LOGGER.error(f"Трассировка ошибки: {traceback.format_exc()}")

        # ✅ ПРОВЕРЯЕМ ДОСТУПНОСТЬ КОНТРОЛЛЕРА И ОБНОВЛЯЕМ ДАННЫЕ
        if not coordinator.megad.is_available:
            _LOGGER.info(f"MegaD-{megad_id}: контроллер был недоступен, запрашиваем обновление")
            hass.async_create_task(coordinator.async_request_refresh())

        if coordinator.megad.is_flashing:
            _LOGGER.warning(f'Контроллер MegaD-{megad_id} в процессе обновления прошивки.')
            return Response(status=HTTPStatus.SERVICE_UNAVAILABLE)

        # ✅ ОБРАБАТЫВАЕМ ПЕРЕЗАГРУЗКУ КОНТРОЛЛЕРА
        if state_megad == '1':
            _LOGGER.info(f'MegaD-{megad_id} был перезагружен, начинаем восстановление')
            hass.async_create_task(self.restore_after_reboot(coordinator))
            
            # ✅ СРАЗУ ОБНОВЛЯЕМ ДАННЫЕ ПОСЛЕ ПЕРЕЗАГРУЗКИ
            hass.async_create_task(coordinator.async_request_refresh())

        # ✅ ОБРАБАТЫВАЕМ ИЗМЕНЕНИЯ ПОРТОВ
        if port_id is not None:
            _LOGGER.info(f"MegaD-{megad_id}: обновление состояния порта {port_id}")
            try:
                await coordinator.update_port_state(
                    port_id=port_id, data=params, ext=ext
                )
            
            except Exception as e:
                _LOGGER.error(f"MegaD-{megad_id}: ошибка при обновлении порта {port_id}: {e}")
    
        # ✅ ЛОГИРУЕМ УСПЕШНОЕ ВЫПОЛНЕНИЕ
        _LOGGER.info(f"MegaD-{megad_id}: запрос успешно обработан, обратная связь зарегистрирована")
    
        return Response(status=HTTPStatus.OK)
    
    async def post(self, request: Request):
        """Обработка POST-запросов от контроллера."""
        try:
            host = request.remote
            data = await request.text()
            _LOGGER.info(f"📨 HTTP POST запрос от {host}, длина: {len(data)} байт")
            
            hass = request.app['hass']
            
            if hass.data.get(DOMAIN) is None:
                _LOGGER.warning('Интеграция MegaD не загружена')
                return Response(status=HTTPStatus.NOT_FOUND)
            
            # Пытаемся распарсить параметры из данных
            params = {}
            try:
                import urllib.parse
                params = dict(urllib.parse.parse_qsl(data))
                id_megad = params.get(MEGAD_ID)
            except:
                id_megad = None
            
            # Ищем координатор
            coordinator = self._find_coordinator(host, id_megad, hass)
            
            if coordinator is None:
                _LOGGER.warning(f"POST: контроллер {host} не найден")
                return Response(status=HTTPStatus.NOT_FOUND)
            
            megad_id = coordinator.megad.id if hasattr(coordinator.megad, 'id') else "unknown"
            
            # ✅ ОТМЕЧАЕМ ОБРАТНУЮ СВЯЗЬ ДЛЯ POST ЗАПРОСОВ
            if hasattr(coordinator, 'watchdog') and coordinator.watchdog:
                if hasattr(coordinator.watchdog, 'mark_feedback_event'):
                    coordinator.watchdog.mark_feedback_event({
                        "type": "http_post",
                        "megad_id": megad_id,
                        "host": host,
                        "data_length": len(data),
                        "data_preview": data[:100] if data else "",
                        "timestamp": datetime.now().isoformat(),
                        "source": "server_post"
                    })
                    
                    _LOGGER.info(
                        f"MegaD-{megad_id}: ✅ ОБРАТНАЯ СВЯЗЬ ОТМЕЧЕНА (POST)! "
                        f"длина: {len(data)} байт"
                    )
            
            return Response(status=HTTPStatus.OK)
            
        except Exception as e:
            _LOGGER.error(f"Ошибка обработки POST запроса: {e}")
            import traceback
            _LOGGER.error(f"Трассировка: {traceback.format_exc()}")
            return Response(status=HTTPStatus.INTERNAL_SERVER_ERROR)