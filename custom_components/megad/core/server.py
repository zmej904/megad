import logging
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
            coordinator.watchdog.mark_data_received()
            coordinator.watchdog.mark_feedback_event({
                "type": "restore_after_reboot",
                "megad_id": coordinator.megad.id,
                "timestamp": datetime.now().isoformat(),
                "is_meaningful": True  # ✅ Явно указываем, что это значимое событие
            })
            _LOGGER.info(f"MegaD-{coordinator.megad.id}: watchdog обновлен после восстановления")

    @staticmethod
    def _is_meaningful_event(params: dict, port_id: str | None, state_megad: str | None) -> bool:
        """Определяет, является ли событие значимым для обратной связи."""
        
        # 1. Перезагрузка контроллера
        if state_megad == '1':
            return True
        
        # 2. Обновление состояния порта
        if port_id is not None:
            return True
        
        # 3. Данные от датчиков (temp, hum, CO2, press, lux)
        sensor_keys = ['temp', 'hum', 'CO2', 'press', 'lux', 'temperature', 'humidity']
        if any(key in params for key in sensor_keys):
            return True
        
        # 4. Данные от сенсоров нажатий (click)
        if 'click' in params:
            return True
        
        # 5. Состояние всех портов (cmd=all)
        if params.get('cmd') == 'all':
            return True
        
        # 6. Состояние списка портов (cmd=list)
        if params.get('cmd') == 'list':
            return True
        
        # 7. Данные от i2c устройств
        i2c_keys = ['i2c_dev', 'scl', 'i2c_par']
        if any(key in params for key in i2c_keys):
            return True
        
        # 8. Незначимые события:
        # - пустые запросы (только пароль)
        if len(params) == 0 or (len(params) == 1 and ('password' in params or 'auth' in params)):
            return False
        
        # - проверочные запросы (только md5 или mdid)
        if len(params) <= 2 and ('md5' in params or MEGAD_ID in params):
            return False
        
        # - служебные запросы
        if params.get('cmd') in ['id', 'uptime', 'ver']:
            return False
        
        # По умолчанию считаем значимым, чтобы не потерять важные события
        return True

    async def get(self, request: Request):
        """Обрабатываем GET-запрос."""
        host = request.remote
        params: dict = dict(request.query)
        _LOGGER.debug(f'MegaD request от {host}: {params}')
        hass = request.app['hass']
        
        # ✅ ДОБАВЛЯЕМ ОТЛАДОЧНОЕ СООБЩЕНИЕ ПРИ СТАРТЕ
        _LOGGER.debug(f"HTTP запрос получен на {self.url} от {host}")
        
        if hass.data.get(DOMAIN) is None:
            _LOGGER.info(f'Интеграция загружается, запрос не обработан: {params}')
            return Response(status=HTTPStatus.NOT_FOUND)
            
        entry_ids = hass.data[DOMAIN][ENTRIES]
        id_megad = params.get(MEGAD_ID)
        state_megad = params.get(MEGAD_STATE)
        ext = any(EXTRA in key for key in params)
        port_id = params.get(PORT_ID)
        coordinator = None
        
        _LOGGER.debug(f"Поиск контроллера: host={host}, id_megad={id_megad}, port_id={port_id}")
        
        # Ищем координатор по нескольким критериям
        for entry_id in entry_ids:
            coordinator_temp = hass.data[DOMAIN][ENTRIES][entry_id]
            if coordinator_temp is None:
                _LOGGER.debug(f"Координатор {entry_id} равен None, пропускаем")
                continue
                
            try:
                megad_id = coordinator_temp.megad.id if hasattr(coordinator_temp.megad, 'id') else "unknown"
                
                # 1. Пробуем по domain (основной метод)
                if hasattr(coordinator_temp.megad, 'domain') and coordinator_temp.megad.domain == host:
                    coordinator = coordinator_temp
                    _LOGGER.debug(f"Найден контроллер по domain: MegaD-{megad_id} (domain: {host})")
                    break
                
                # 2. Пробуем по IP из конфига (резервный метод)
                if hasattr(coordinator_temp.megad, 'config') and hasattr(coordinator_temp.megad.config.plc, 'ip_megad'):
                    config_ip = str(coordinator_temp.megad.config.plc.ip_megad)
                    if config_ip == host:
                        coordinator = coordinator_temp
                        _LOGGER.debug(f"Найден контроллер по IP: MegaD-{megad_id} (IP: {host})")
                        break
                
                # 3. Пробуем по MEGAD_ID из параметров запроса
                if id_megad and hasattr(coordinator_temp.megad, 'id') and str(coordinator_temp.megad.id) == id_megad:
                    coordinator = coordinator_temp
                    _LOGGER.debug(f"Найден контроллер по ID: MegaD-{megad_id} (ID из запроса: {id_megad})")
                    break
                    
            except AttributeError as e:
                _LOGGER.warning(
                    f'Ошибка при поиске координатора: {e}. '
                    f'params: {params}, host: {host}'
                )

        if coordinator is None:
            _LOGGER.warning(f'Контроллер ip={host} не найден в конфигурации HA')
            _LOGGER.debug(f"Доступные координаторы: {list(entry_ids.keys())}")
            return Response(status=HTTPStatus.NOT_FOUND)

        megad_id = coordinator.megad.id if hasattr(coordinator.megad, 'id') else "unknown"
        _LOGGER.info(f"Обработка запроса для MegaD-{megad_id}")

        # ✅ ОПРЕДЕЛЯЕМ, ЯВЛЯЕТСЯ ЛИ СОБЫТИЕ ЗНАЧИМЫМ
        is_meaningful = self._is_meaningful_event(params, port_id, state_megad)
        
        if not is_meaningful:
            _LOGGER.debug(f"MegaD-{megad_id}: пропущено НЕЗНАЧИМОЕ событие (params: {params})")

        # ✅ ОТМЕЧАЕМ ПОЛУЧЕНИЕ ДАННЫХ ОТ КОНТРОЛЛЕРА
        try:
            # Отмечаем получение данных в watchdog (всегда)
            if hasattr(coordinator, 'watchdog') and coordinator.watchdog:
                # Обновление времени получения данных (всегда)
                coordinator.watchdog.mark_data_received()
                
                # Отмечаем событие обратной связи ТОЛЬКО для значимых событий
                if is_meaningful:
                    coordinator.watchdog.mark_feedback_event({
                        "type": "http_callback",
                        "megad_id": megad_id,
                        "host": host,
                        "port_id": port_id,
                        "state_megad": state_megad,
                        "is_meaningful": True,
                        "params": {k: v for k, v in params.items() if k not in ['password', 'auth']},
                        "timestamp": datetime.now().isoformat()
                    })
                    
                    _LOGGER.debug(
                        f"MegaD-{megad_id}: отмечено ЗНАЧИМОЕ событие через callback "
                        f"(host: {host}, port: {port_id}, state: {state_megad})"
                    )
                    
                    # ✅ СБРАСЫВАЕМ СЧЕТЧИКИ ТОЛЬКО ДЛЯ ЗНАЧИМЫХ СОБЫТИЙ
                    coordinator.watchdog._failure_count = 0
                    coordinator.watchdog._was_offline = False
                    
                    if hasattr(coordinator.watchdog, '_feedback_check_attempts'):
                        coordinator.watchdog._feedback_check_attempts = 0
                    if hasattr(coordinator.watchdog, '_feedback_restore_attempts'):
                        coordinator.watchdog._feedback_restore_attempts = 0
                else:
                    _LOGGER.debug(f"MegaD-{megad_id}: пропущен сброс счетчиков (незначимое событие)")
            
            # Также обновляем время последнего получения данных в координаторе
            if hasattr(coordinator, 'last_data_received'):
                coordinator.last_data_received = datetime.now()
                
            # Используем публичный метод координатора (дополнительная гарантия)
            if hasattr(coordinator, 'mark_watchdog_data_received'):
                coordinator.mark_watchdog_data_received()
                
            # Отмечаем через метод координатора ТОЛЬКО для значимых событий
            if is_meaningful and hasattr(coordinator, 'mark_feedback_event'):
                coordinator.mark_feedback_event({
                    "type": "http_get",
                    "host": host,
                    "port_id": port_id,
                    "is_meaningful": True
                })
                
        except Exception as e:
            _LOGGER.error(f"MegaD-{megad_id}: ошибка при отметке получения данных: {e}")
            import traceback
            _LOGGER.debug(f"Трассировка ошибки: {traceback.format_exc()}")

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
            _LOGGER.info(f"MegaD-{megad_id}: обновление состояния порта {port_id}, данные: {params}")
            try:
                await coordinator.update_port_state(
                    port_id=port_id, data=params, ext=ext
                )
                
                # ✅ ДОПОЛНИТЕЛЬНАЯ ОТМЕТКА ДЛЯ WATCHDOG ПОСЛЕ ОБНОВЛЕНИЯ ПОРТА
                if hasattr(coordinator, 'watchdog') and coordinator.watchdog:
                    coordinator.watchdog.mark_feedback_event({
                        "type": "port_updated",
                        "megad_id": megad_id,
                        "port_id": port_id,
                        "ext": ext,
                        "success": True,
                        "is_meaningful": True  # ✅ Обновление порта - всегда значимое событие
                    })
                    
            except Exception as e:
                _LOGGER.error(f"MegaD-{megad_id}: ошибка при обновлении порта {port_id}: {e}")
                
                # ✅ ОТМЕЧАЕМ ОШИБКУ В WATCHDOG
                if hasattr(coordinator, 'watchdog') and coordinator.watchdog:
                    coordinator.watchdog.mark_feedback_event({
                        "type": "port_update_error",
                        "megad_id": megad_id,
                        "port_id": port_id,
                        "error": str(e),
                        "success": False,
                        "is_meaningful": True  # ✅ Ошибка - тоже значимое событие
                    })
        
        # ✅ ЛОГИРУЕМ УСПЕШНОЕ ВЫПОЛНЕНИЕ
        _LOGGER.debug(f"MegaD-{megad_id}: запрос успешно обработан")
        
        return Response(status=HTTPStatus.OK)
    
    async def post(self, request: Request):
        """Обработка POST-запросов от контроллера (альтернативный метод)."""
        try:
            host = request.remote
            data = await request.text()
            _LOGGER.debug(f"POST запрос от {host}: {data[:200]}...")  # Логируем первые 200 символов
            
            hass = request.app['hass']
            
            if hass.data.get(DOMAIN) is None:
                _LOGGER.info(f'Интеграция загружается, POST запрос не обработан')
                return Response(status=HTTPStatus.NOT_FOUND)
            
            # ✅ ПОИСК КООРДИНАТОРА ДЛЯ POST ЗАПРОСОВ
            entry_ids = hass.data[DOMAIN][ENTRIES]
            coordinator = None
            
            for entry_id in entry_ids:
                coordinator_temp = hass.data[DOMAIN][ENTRIES][entry_id]
                if coordinator_temp is None:
                    continue
                    
                try:
                    megad_id = coordinator_temp.megad.id if hasattr(coordinator_temp.megad, 'id') else "unknown"
                    
                    # Пробуем найти по host
                    if hasattr(coordinator_temp.megad, 'domain') and coordinator_temp.megad.domain == host:
                        coordinator = coordinator_temp
                        _LOGGER.debug(f"POST: найден контроллер по domain: MegaD-{megad_id}")
                        break
                        
                    # Или по IP из конфига
                    if hasattr(coordinator_temp.megad, 'config') and hasattr(coordinator_temp.megad.config.plc, 'ip_megad'):
                        config_ip = str(coordinator_temp.megad.config.plc.ip_megad)
                        if config_ip == host:
                            coordinator = coordinator_temp
                            _LOGGER.debug(f"POST: найден контроллер по IP: MegaD-{megad_id}")
                            break
                            
                except AttributeError as e:
                    _LOGGER.debug(f"POST: ошибка при поиске координатора: {e}")
            
            if coordinator is None:
                _LOGGER.warning(f"POST: контроллер {host} не найден")
                return Response(status=HTTPStatus.NOT_FOUND)
            
            megad_id = coordinator.megad.id if hasattr(coordinator.megad, 'id') else "unknown"
            
            # ✅ Для POST запросов определяем значимость по наличию данных
            is_meaningful = bool(data and data.strip() and len(data) > 10)
            
            # ✅ ОТМЕЧАЕМ ПОЛУЧЕНИЕ ДАННЫХ ЧЕРЕЗ POST
            if hasattr(coordinator, 'watchdog') and coordinator.watchdog:
                # Всегда отмечаем получение данных
                coordinator.watchdog.mark_data_received()
                
                # Отмечаем обратную связь только для значимых POST запросов
                if is_meaningful:
                    coordinator.watchdog.mark_feedback_event({
                        "type": "http_post",
                        "megad_id": megad_id,
                        "host": host,
                        "data_length": len(data),
                        "data_preview": data[:100] if data else "",
                        "is_meaningful": True,
                        "timestamp": datetime.now().isoformat()
                    })
                    
                    # Сбрасываем счетчики
                    coordinator.watchdog._failure_count = 0
                    coordinator.watchdog._was_offline = False
                    
                    _LOGGER.debug(f"POST: значимые данные получены от MegaD-{megad_id}, длина: {len(data)} байт")
                else:
                    _LOGGER.debug(f"POST: незначимые данные от MegaD-{megad_id}, пропущены")
            
            # ✅ ВОЗМОЖНОСТЬ ОБРАБОТКИ JSON ДАННЫХ
            if data and data.strip() and is_meaningful:
                try:
                    import json
                    json_data = json.loads(data)
                    _LOGGER.debug(f"POST: JSON данные от MegaD-{megad_id}: {json.dumps(json_data)[:200]}...")
                    
                    # Здесь можно добавить обработку специфичных JSON структур
                    # Например, если в данных есть информация о портах
                    if isinstance(json_data, dict) and 'ports' in json_data:
                        _LOGGER.info(f"POST: получены данные о портах от MegaD-{megad_id}")
                        
                except json.JSONDecodeError:
                    # Не JSON, просто текст
                    _LOGGER.debug(f"POST: текстовые данные от MegaD-{megad_id}: {data[:100]}...")
                except Exception as e:
                    _LOGGER.debug(f"POST: ошибка обработки данных: {e}")
            
            return Response(status=HTTPStatus.OK)
            
        except Exception as e:
            _LOGGER.error(f"Ошибка обработки POST запроса: {e}")
            import traceback
            _LOGGER.debug(f"Трассировка: {traceback.format_exc()}")
            return Response(status=HTTPStatus.INTERNAL_SERVER_ERROR)