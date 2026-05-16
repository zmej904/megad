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
        
        # ОТМЕЧАЕМ СОБЫТИЕ ВОССТАНОВЛЕНИЯ В WATCHDOG
        if hasattr(coordinator, 'watchdog') and coordinator.watchdog:
            coordinator.watchdog.mark_data_received()
            coordinator.watchdog.mark_feedback_event({
                "type": "restore_after_reboot",
                "megad_id": coordinator.megad.id,
                "timestamp": datetime.now().isoformat(),
                "is_feedback": True,
                "is_meaningful": True
            })
            _LOGGER.info(f"MegaD-{coordinator.megad.id}: watchdog обновлен после восстановления")

    @staticmethod
    def _is_meaningful_event(params: dict, port_id: str | None, state_megad: str | None) -> bool:
        """Определяет, является ли событие СПОНТАННОЙ обратной связью от контроллера
        (не ответом на команду, отправленную из Home Assistant)."""

        # 1. Перезагрузка контроллера – всегда значима
        if state_megad == '1':
            return True

        # 2. Данные от датчиков (temp, hum, CO2, press, lux)
        sensor_keys = ['temp', 'hum', 'CO2', 'press', 'lux', 'temperature', 'humidity']
        if any(key in params for key in sensor_keys):
            return True

        # 3. События нажатий (click) – значимы
        if 'click' in params:
            return True

        # 4. Счётчик нажатий (cnt) – значим (обычно идёт вместе с click)
        if 'cnt' in params:
            return True

        # 5. Команды получения состояния (cmd=all, list) – не обратная связь
        if params.get('cmd') in ['all', 'list']:
            return False

        # 6. Если есть только pt и v (и нет признаков спонтанности) – это ответ на команду HA
        if port_id is not None and 'v' in params:
            if not any(k in params for k in ['click', 'cnt', 'temp', 'hum', 'CO2', 'press', 'lux']):
                return False

        # 7. Запросы с одним портом и более одного параметра (например, pt=..&mdid=..) – скорее ответ на команду
        if port_id is not None and len(params) <= 2:
            return False

        # 8. Всё остальное считаем значимым (на всякий случай)
        return True

    async def get(self, request: Request):
        """Обрабатываем GET-запрос."""
        host = request.remote
        params: dict = dict(request.query)
        _LOGGER.debug(f'MegaD request от {host}: {params}')
        hass = request.app['hass']

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

                # 1. По domain (основной метод)
                if hasattr(coordinator_temp.megad, 'domain') and coordinator_temp.megad.domain == host:
                    coordinator = coordinator_temp
                    _LOGGER.debug(f"Найден контроллер по domain: MegaD-{megad_id} (domain: {host})")
                    break

                # 2. По IP из конфига (резервный метод)
                if hasattr(coordinator_temp.megad, 'config') and hasattr(coordinator_temp.megad.config.plc, 'ip_megad'):
                    config_ip = str(coordinator_temp.megad.config.plc.ip_megad)
                    if config_ip == host:
                        coordinator = coordinator_temp
                        _LOGGER.debug(f"Найден контроллер по IP: MegaD-{megad_id} (IP: {host})")
                        break

                # 3. По MEGAD_ID из параметров запроса
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

        # ОПРЕДЕЛЯЕМ, ЯВЛЯЕТСЯ ЛИ СОБЫТИЕ СПОНТАННОЙ ОБРАТНОЙ СВЯЗЬЮ
        is_meaningful = self._is_meaningful_event(params, port_id, state_megad)

        if not is_meaningful:
            _LOGGER.debug(f"MegaD-{megad_id}: пропущено НЕЗНАЧИМОЕ событие (params: {params})")

        # ОТМЕЧАЕМ ПОЛУЧЕНИЕ ДАННЫХ ОТ КОНТРОЛЛЕРА
        try:
            if hasattr(coordinator, 'watchdog') and coordinator.watchdog:
                coordinator.watchdog.mark_data_received()

                if is_meaningful:
                    coordinator.watchdog.mark_feedback_event({
                        "type": "http_callback",
                        "megad_id": megad_id,
                        "host": host,
                        "port_id": port_id,
                        "state_megad": state_megad,
                        "is_feedback": True,
                        "is_meaningful": True,
                        "params": {k: v for k, v in params.items() if k not in ['password', 'auth']},
                        "timestamp": datetime.now().isoformat()
                    })
                    _LOGGER.debug(
                        f"MegaD-{megad_id}: отмечено ЗНАЧИМОЕ событие через callback "
                        f"(host: {host}, port: {port_id}, state: {state_megad})"
                    )

                    coordinator.watchdog._failure_count = 0
                    coordinator.watchdog._was_offline = False
                    if hasattr(coordinator.watchdog, '_feedback_check_attempts'):
                        coordinator.watchdog._feedback_check_attempts = 0
                    if hasattr(coordinator.watchdog, '_feedback_restore_attempts'):
                        coordinator.watchdog._feedback_restore_attempts = 0
                else:
                    _LOGGER.debug(f"MegaD-{megad_id}: пропущен сброс счетчиков (незначимое событие)")

        except Exception as e:
            _LOGGER.error(f"MegaD-{megad_id}: ошибка при отметке получения данных: {e}")

        # ПРОВЕРЯЕМ ДОСТУПНОСТЬ КОНТРОЛЛЕРА И ОБНОВЛЯЕМ ДАННЫЕ
        if not coordinator.megad.is_available:
            _LOGGER.info(f"MegaD-{megad_id}: контроллер был недоступен, запрашиваем обновление")
            hass.async_create_task(coordinator.async_request_refresh())

        if coordinator.megad.is_flashing:
            _LOGGER.warning(f'Контроллер MegaD-{megad_id} в процессе обновления прошивки.')
            return Response(status=HTTPStatus.SERVICE_UNAVAILABLE)

        # ОБРАБАТЫВАЕМ ПЕРЕЗАГРУЗКУ КОНТРОЛЛЕРА
        if state_megad == '1':
            _LOGGER.info(f'MegaD-{megad_id} был перезагружен, начинаем восстановление')
            hass.async_create_task(self.restore_after_reboot(coordinator))
            hass.async_create_task(coordinator.async_request_refresh())

        # ОБРАБАТЫВАЕМ ИЗМЕНЕНИЯ ПОРТОВ
        if port_id is not None:
            _LOGGER.info(f"MegaD-{megad_id}: обновление состояния порта {port_id}, данные: {params}")
            try:
                await coordinator.update_port_state(
                    port_id=port_id, data=params, ext=ext
                )
                # ВТОРОЙ ВЫЗОВ mark_feedback_event УДАЛЁН – не дублируем
            except Exception as e:
                _LOGGER.error(f"MegaD-{megad_id}: ошибка при обновлении порта {port_id}: {e}")

        _LOGGER.debug(f"MegaD-{megad_id}: запрос успешно обработан")
        return Response(status=HTTPStatus.OK)

    async def post(self, request: Request):
        """Обработка POST-запросов от контроллера."""
        try:
            host = request.remote
            data = await request.text()
            _LOGGER.debug(f"POST запрос от {host}: {data[:200]}...")

            hass = request.app['hass']

            if hass.data.get(DOMAIN) is None:
                _LOGGER.info(f'Интеграция загружается, POST запрос не обработан')
                return Response(status=HTTPStatus.NOT_FOUND)

            entry_ids = hass.data[DOMAIN][ENTRIES]
            coordinator = None

            for entry_id in entry_ids:
                coordinator_temp = hass.data[DOMAIN][ENTRIES][entry_id]
                if coordinator_temp is None:
                    continue
                try:
                    megad_id = coordinator_temp.megad.id if hasattr(coordinator_temp.megad, 'id') else "unknown"
                    if hasattr(coordinator_temp.megad, 'domain') and coordinator_temp.megad.domain == host:
                        coordinator = coordinator_temp
                        _LOGGER.debug(f"POST: найден контроллер по domain: MegaD-{megad_id}")
                        break
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

            # Для POST запросов определяем значимость по наличию данных (длина > 10 байт)
            is_meaningful = bool(data and data.strip() and len(data) > 10)

            if hasattr(coordinator, 'watchdog') and coordinator.watchdog:
                coordinator.watchdog.mark_data_received()
                if is_meaningful:
                    coordinator.watchdog.mark_feedback_event({
                        "type": "http_post",
                        "megad_id": megad_id,
                        "host": host,
                        "data_length": len(data),
                        "data_preview": data[:100] if data else "",
                        "is_feedback": True,
                        "is_meaningful": True,
                        "timestamp": datetime.now().isoformat()
                    })
                    coordinator.watchdog._failure_count = 0
                    coordinator.watchdog._was_offline = False
                    _LOGGER.debug(f"POST: значимые данные получены от MegaD-{megad_id}, длина: {len(data)} байт")
                else:
                    _LOGGER.debug(f"POST: незначимые данные от MegaD-{megad_id}, пропущены")

            if data and data.strip() and is_meaningful:
                try:
                    import json
                    json_data = json.loads(data)
                    _LOGGER.debug(f"POST: JSON данные от MegaD-{megad_id}: {json.dumps(json_data)[:200]}...")
                    if isinstance(json_data, dict) and 'ports' in json_data:
                        _LOGGER.info(f"POST: получены данные о портах от MegaD-{megad_id}")
                except json.JSONDecodeError:
                    _LOGGER.debug(f"POST: текстовые данные от MegaD-{megad_id}: {data[:100]}...")
                except Exception as e:
                    _LOGGER.debug(f"POST: ошибка обработки данных: {e}")

            return Response(status=HTTPStatus.OK)

        except Exception as e:
            _LOGGER.error(f"Ошибка обработки POST запроса: {e}")
            import traceback
            _LOGGER.debug(f"Трассировка: {traceback.format_exc()}")
            return Response(status=HTTPStatus.INTERNAL_SERVER_ERROR)
