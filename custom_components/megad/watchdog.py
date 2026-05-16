import asyncio
import logging
import platform
import socket
import sys
from datetime import datetime
from typing import Optional, Callable, Any, List

from homeassistant.helpers.aiohttp_client import async_get_clientsession
from .const import (
    WATCHDOG_MAX_FAILURES, 
    WATCHDOG_INACTIVITY_TIMEOUT,
    WATCHDOG_CHECK_INTERVAL
)

_LOGGER = logging.getLogger(__name__)


class MegaDWatchdog:
    """Watchdog для мониторинга и восстановления MegaD с отслеживанием обратной связи."""
    
    def __init__(self, coordinator, hass):
        self.coordinator = coordinator
        self.hass = hass
        self.megad = coordinator.megad
        self._watchdog_task = None
        self._is_running = False
        self._failure_count = 0
        self._max_failures = WATCHDOG_MAX_FAILURES
        self._recovering = False
        self._last_success = None
        self._last_incoming_data = None  # Время последних полученных данных
        self._health_check_interval = WATCHDOG_CHECK_INTERVAL
        self._was_offline = False
        self._inactivity_timeout = WATCHDOG_INACTIVITY_TIMEOUT
        self._last_reboot_attempt = None  # Время последней попытки перезагрузки
        
        # ✅ ОБРАТНАЯ СВЯЗЬ: новые атрибуты для отслеживания обратной связи
        self._feedback_enabled = True  # Включена ли обратная связь
        self._feedback_port = 8082  # Порт для обратной связи (по умолчанию для MegaD)
        self._feedback_listeners: List[Callable] = []  # Слушатели для событий обратной связи
        self._feedback_last_event = None  # Время последнего события обратной связи
        self._last_meaningful_feedback = None  # ✅ Время последнего ЗНАЧИМОГО события обратной связи
        self._feedback_timeout = 900  # ✅ Увеличен таймаут обратной связи (15 минут)
        self._feedback_check_attempts = 0  # Счетчик попыток проверки обратной связи
        self._feedback_restore_attempts = 0  # Счетчик попыток восстановления обратной связи
        self._max_feedback_restore_attempts = 3  # Максимум попыток восстановления обратной связи
        self._meaningful_event_counter = 0  # ✅ Счетчик значимых событий для отладки
        self._non_meaningful_event_counter = 0  # ✅ Счетчик незначимых событий для отладки
        
    async def start(self):
        """Запуск watchdog."""
        if self._is_running:
            _LOGGER.debug(f"Watchdog для MegaD-{self.megad.id} уже запущен")
            return
            
        self._is_running = True
        self._failure_count = 0
        self._recovering = False
        self._was_offline = False
        self._last_incoming_data = datetime.now()  # Инициализируем время
        self._last_success = datetime.now()
        self._last_reboot_attempt = None
        
        # ✅ ОБРАТНАЯ СВЯЗЬ: инициализация
        self._feedback_last_event = datetime.now()
        self._last_meaningful_feedback = datetime.now()  # ✅ Инициализируем значимое событие
        self._feedback_check_attempts = 0
        self._feedback_restore_attempts = 0
        self._meaningful_event_counter = 0
        self._non_meaningful_event_counter = 0
        
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        _LOGGER.info(f"Watchdog для MegaD-{self.megad.id} запущен")
        
        # ✅ ОБРАТНАЯ СВЯЗЬ: проверяем настройки обратной связи при старте
        try:
            await self._check_feedback_settings(verbose=True)
            await self._enable_feedback_on_megad()
        except Exception as e:
            _LOGGER.warning(f"MegaD-{self.megad.id}: не удалось проверить настройки обратной связи при старте: {e}")
    
    async def stop(self):
        """Остановка watchdog."""
        self._is_running = False
        if self._watchdog_task:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
            self._watchdog_task = None
        _LOGGER.info(f"Watchdog для MegaD-{self.megad.id} остановлен")
    
    def mark_data_received(self):
        """Вызывается при получении данных от контроллера."""
        self._last_incoming_data = datetime.now()
        _LOGGER.debug(f"MegaD-{self.megad.id}: получены данные, сбрасываем таймер неактивности")
    
    # ✅ ОБРАТНАЯ СВЯЗЬ: метод для маркировки событий обратной связи
    def mark_feedback_event(self, event_data: Any = None):
        """Вызывается при получении события обратной связи от контроллера."""
        # Определяем, является ли событие значимым
        is_meaningful = False
        
        if isinstance(event_data, dict):
            # Событие значимое, если:
            # - это явно указано в данных
            if event_data.get('is_meaningful'):
                is_meaningful = True
            # - это обновление порта
            elif event_data.get('type') in ['port_updated', 'http_callback', 'port_update', 'restore_after_reboot']:
                is_meaningful = True
            # - есть данные о порте
            elif event_data.get('port_id') is not None:
                is_meaningful = True
            # - есть данные о состоянии контроллера
            elif event_data.get('state_megad') == '1':
                is_meaningful = True
            # - есть данные от датчиков
            elif any(key in event_data for key in ['temp', 'hum', 'CO2', 'press', 'lux', 'click']):
                is_meaningful = True
        elif event_data is None:
            # Событие без данных - считаем незначимым (служебные вызовы)
            is_meaningful = False
        else:
            # Другие типы данных считаем значимыми
            is_meaningful = True
        
        # ✅ Обновляем общий таймер всегда
        self._feedback_last_event = datetime.now()
        self.mark_data_received()
        
        # ✅ Обновляем таймер значимых событий ТОЛЬКО для важных событий
        if is_meaningful:
            self._last_meaningful_feedback = datetime.now()
            self._feedback_check_attempts = 0
            self._feedback_restore_attempts = 0
            self._meaningful_event_counter += 1
            _LOGGER.debug(f"MegaD-{self.megad.id}: ✅ ЗНАЧИМОЕ событие обратной связи (#{self._meaningful_event_counter}): {event_data}")
        else:
            self._non_meaningful_event_counter += 1
            _LOGGER.debug(f"MegaD-{self.megad.id}: ⏭️ НЕЗНАЧИМОЕ событие обратной связи (#{self._non_meaningful_event_counter})")
        
        # Уведомляем слушателей только для значимых событий
        if is_meaningful:
            for listener in self._feedback_listeners:
                try:
                    if asyncio.iscoroutinefunction(listener):
                        asyncio.create_task(listener(event_data))
                    else:
                        listener(event_data)
                except Exception as e:
                    _LOGGER.debug(f"Ошибка в слушателе обратной связи: {e}")
    
    # ✅ ОБРАТНАЯ СВЯЗЬ: добавление слушателей
    def add_feedback_listener(self, callback: Callable):
        """Добавляет слушателя событий обратной связи."""
        if callback not in self._feedback_listeners:
            self._feedback_listeners.append(callback)
            _LOGGER.debug(f"MegaD-{self.megad.id}: добавлен слушатель обратной связи, всего: {len(self._feedback_listeners)}")
    
    def remove_feedback_listener(self, callback: Callable):
        """Удаляет слушателя событий обратной связи."""
        if callback in self._feedback_listeners:
            self._feedback_listeners.remove(callback)
            _LOGGER.debug(f"MegaD-{self.megad.id}: удален слушатель обратной связи, осталось: {len(self._feedback_listeners)}")
    
    async def _watchdog_loop(self):
        """Основной цикл watchdog с поддержкой отслеживания обратной связи."""
        _LOGGER.debug(f"Watchdog запущен для MegaD-{self.megad.id} с интервалом {self._health_check_interval} сек")

        while self._is_running:
            try:
                await asyncio.sleep(self._health_check_interval)
            
                # Отладочный вывод
                inactivity_seconds = self._get_inactivity_seconds()
                feedback_inactivity = self._get_feedback_inactivity_seconds()
                meaningful_inactivity = self._get_meaningful_inactivity_seconds()  # ✅ Новый метод
                _LOGGER.debug(
                    f"Watchdog проверка MegaD-{self.megad.id} "
                    f"(ошибок: {self._failure_count}/{self._max_failures}, "
                    f"без данных: {inactivity_seconds} сек, "
                    f"без обратной связи: {feedback_inactivity} сек, "
                    f"без ЗНАЧИМЫХ событий: {meaningful_inactivity} сек)"
                )
                
                # Пропускаем проверку если идет восстановление
                if self._recovering:
                    _LOGGER.debug(f"MegaD-{self.megad.id} в процессе восстановления, пропускаем проверку")
                    continue
                
                # Проверяем, не идет ли процесс прошивки
                if self.megad.is_flashing:
                    _LOGGER.info(f"MegaD-{self.megad.id}: идет процесс прошивки, пропускаем проверку")
                    continue
                
                # ✅ 1. Проверка базовой доступности (PING + HTTP)
                is_healthy = await self._check_megad_health_basic()
                
                if not is_healthy:
                    # ❌ Проблема с базовым подключением
                    self._failure_count = min(self._failure_count + 1, self._max_failures)
                    self._was_offline = True
                    _LOGGER.warning(f"MegaD-{self.megad.id}: не отвечает (ping/HTTP), ошибок: {self._failure_count}/{self._max_failures}")
                    
                    # Обновляем состояние координатора
                    await self._update_coordinator_state(False)
                    
                    # Проверяем нужно ли запускать восстановление
                    if self._failure_count >= self._max_failures:
                        _LOGGER.warning(f"MegaD-{self.megad.id}: достигнут лимит ошибок, запуск восстановления")
                        await self._execute_recovery_procedure()
                    
                    continue
                
                # ✅ Контроллер доступен на базовом уровне
                
                # ✅ 2. Проверяем обратную связь (только по ЗНАЧИМЫМ событиям)
                feedback_ok = await self._check_feedback_connection()
                
                if not feedback_ok and self._feedback_enabled:
                    # ⚠️ Обратная связь не работает, но контроллер доступен
                    self._feedback_check_attempts += 1
                    _LOGGER.warning(
                        f"MegaD-{self.megad.id}: обратная связь не работает "
                        f"(попытка {self._feedback_check_attempts}), "
                        f"без значимых событий: {self._get_meaningful_inactivity_seconds()} сек"
                    )
                    
                    # Пытаемся восстановить обратную связь
                    if self._feedback_check_attempts >= 3:  # После 3 неудачных проверок
                        await self._try_restore_feedback()
                    
                    # Не увеличиваем общий счетчик ошибок для обратной связи,
                    # так как контроллер все еще доступен
                    continue
                
                # ✅ 3. Проверяем активность контроллера (получаем ли мы данные)
                inactivity_seconds = self._get_inactivity_seconds()
                
                if inactivity_seconds < self._inactivity_timeout:
                    # ✅ Всё в порядке: контроллер доступен и мы получаем данные
                    if self._failure_count > 0 or self._was_offline:
                        _LOGGER.info(f"MegaD-{self.megad.id}: восстановил нормальную работу")
                        self._failure_count = 0
                        self._was_offline = False
                        self._feedback_check_attempts = 0
                        self._feedback_restore_attempts = 0
                    
                    await self._update_coordinator_state(True)
                    self._last_success = datetime.now()
                    _LOGGER.debug(f"MegaD-{self.megad.id}: работает нормально ({inactivity_seconds} сек без данных)")
                    
                else:
                    # ⚠️ Долго не было данных - проверяем, почему
                    _LOGGER.info(f"MegaD-{self.megad.id}: давно не было данных ({inactivity_seconds} сек), анализируем ситуацию")
                    
                    # Проверяем, можем ли мы получить данные ОТ контроллера
                    can_pull_data = await self._test_megad_data_pull()
                    
                    if can_pull_data:
                        # ✅ Мы можем получать данные по запросу - контроллер работает нормально, просто нет событий
                        _LOGGER.info(f"MegaD-{self.megad.id}: доступен и отвечает на запросы, но нет событий - это НОРМАЛЬНО")
                        
                        # Сбрасываем счетчик ошибок
                        self._failure_count = 0
                        self._was_offline = False
                        self._feedback_check_attempts = 0
                        
                        # Контроллер доступен и мы можем получать данные - всё хорошо
                        await self._update_coordinator_state(True)
                    else:
                        # ❌ Мы НЕ можем получать данные от контроллера
                        self._failure_count = min(self._failure_count + 1, self._max_failures)
                        self._was_offline = True
                        _LOGGER.error(f"MegaD-{self.megad.id}: доступен, но не отдает данные по запросу! Ошибок: {self._failure_count}/{self._max_failures}")
                        
                        # Обновляем состояние координатора
                        await self._update_coordinator_state(False)
                        
                        # Проверяем нужно ли запускать восстановление
                        if self._failure_count >= self._max_failures:
                            _LOGGER.error(f"MegaD-{self.megad.id}: контроллер не отдает данные, запуск восстановления")
                            await self._execute_recovery_procedure()
            
            except asyncio.CancelledError:
                break
            except Exception as e:
                _LOGGER.error(f"Ошибка в watchdog цикле MegaD-{self.megad.id}: {e}")
                await asyncio.sleep(30)
    
    def _get_inactivity_seconds(self) -> int:
        """Возвращает количество секунд без полученных данных."""
        if not self._last_incoming_data:
            return 0
        return int((datetime.now() - self._last_incoming_data).total_seconds())
    
    def _get_feedback_inactivity_seconds(self) -> int:
        """Возвращает количество секунд без событий обратной связи (включая незначимые)."""
        if not self._feedback_last_event:
            return 999999
        return int((datetime.now() - self._feedback_last_event).total_seconds())
    
    # ✅ НОВЫЙ МЕТОД: получение времени без ЗНАЧИМЫХ событий обратной связи
    def _get_meaningful_inactivity_seconds(self) -> int:
        """Возвращает количество секунд без ЗНАЧИМЫХ событий обратной связи."""
        if not self._last_meaningful_feedback:
            return 999999
        return int((datetime.now() - self._last_meaningful_feedback).total_seconds())
    
    # ✅ ОБРАТНАЯ СВЯЗЬ: проверка соединения обратной связи (используем значимые события)
    async def _check_feedback_connection(self) -> bool:
        """Проверяет, работает ли обратная связь от контроллера (по значимым событиям)."""
        try:
            # Если обратная связь отключена, считаем что всё ок
            if not self._feedback_enabled:
                return True
            
            # 1. Проверяем, были ли недавно ЗНАЧИМЫЕ события обратной связи
            meaningful_inactivity = self._get_meaningful_inactivity_seconds()
            if meaningful_inactivity < self._feedback_timeout:
                return True
            
            # 2. Проверяем настройки обратной связи в контроллере
            settings_ok = await self._check_feedback_settings()
            if not settings_ok:
                _LOGGER.warning(f"MegaD-{self.megad.id}: обратная связь отключена в настройках контроллера")
                return False
            
            # 3. Тестовая проверка обратной связи
            return await self._test_feedback_with_command()
            
        except Exception as e:
            _LOGGER.error(f"MegaD-{self.megad.id}: ошибка проверки обратной связи: {e}")
            return False
    
    # ✅ ОБРАТНАЯ СВЯЗЬ: проверка настроек обратной связи на контроллере
    async def _check_feedback_settings(self, verbose: bool = False) -> bool:
        """Проверяет настройки обратной связи на контроллере (страница CF1)."""
        try:
            session = async_get_clientsession(self.hass)
            base_url = self.megad.url.rstrip('/')
        
            # Правильная страница - CF1 (не CF3!)
            config_url = f"{base_url}/sec/?cf=1"
        
            if verbose:
                _LOGGER.debug(f"MegaD-{self.megad.id}: проверка настроек обратной связи: {config_url}")
        
            try:
                async with asyncio.timeout(10):
                    async with session.get(config_url) as response:
                        if response.status != 200:
                            if verbose:
                                _LOGGER.warning(f"MegaD-{self.megad.id}: не удалось получить настройки, статус: {response.status}")
                            return False
                        text = await response.text()
            except AttributeError:
                async with async_timeout.timeout(10):
                    async with session.get(config_url) as response:
                        if response.status != 200:
                            if verbose:
                                _LOGGER.warning(f"MegaD-{self.megad.id}: не удалось получить настройки, статус: {response.status}")
                            return False
                        text = await response.text()
        
            # Проверяем SRV Type (должен быть HTTP - значение 0)
            if 'srvt' in text:
                if 'value=0 selected' in text or 'srvt=0' in text:
                    if verbose:
                        _LOGGER.info(f"MegaD-{self.megad.id}: SRV Type = HTTP (OK)")
                else:
                    if verbose:
                        _LOGGER.warning(f"MegaD-{self.megad.id}: SRV Type не HTTP")
                    return False
        
            # Проверяем наличие SIP (адрес сервера)
            if 'sip' in text:
                if verbose:
                    # Извлекаем текущий адрес для диагностики
                    import re
                    match = re.search(r'sip value="([^"]+)"', text)
                    if match:
                        _LOGGER.info(f"MegaD-{self.megad.id}: текущий SRV адрес: {match.group(1)}")
                return True
        
            if verbose:
                _LOGGER.warning(f"MegaD-{self.megad.id}: настройки обратной связи не найдены на странице")
            return False
                
        except Exception as e:
            if verbose:
                _LOGGER.warning(f"MegaD-{self.megad.id}: ошибка проверки настроек: {e}")
            return False
    
    # ✅ ОБРАТНАЯ СВЯЗЬ: тестирование обратной связи путем отправки команды
    async def _test_feedback_with_command(self) -> bool:
        """Тестирует обратную связь путем отправки тестовой команды."""
        try:
            # Сохраняем текущее время значимого события
            original_meaningful_time = self._last_meaningful_feedback
            
            # Отправляем тестовую команду (используем порт 255, который обычно виртуальный/тестовый)
            session = async_get_clientsession(self.hass)
            base_url = self.megad.url.rstrip('/')
            
            # Команда для включения/выключения виртуального порта
            test_cmd_url = f"{base_url}/sec/?pt=255&cmd=on"
            
            _LOGGER.debug(f"MegaD-{self.megad.id}: тестирование обратной связи, команда: {test_cmd_url}")
            
            # ✅ ИСПРАВЛЕНИЕ: поддержка таймаута для новых версий Python
            try:
                async with asyncio.timeout(5):
                    async with session.get(test_cmd_url) as response:
                        if response.status == 200:
                            text = await response.text()
                            _LOGGER.debug(f"MegaD-{self.megad.id}: тестовая команда отправлена, ответ: {text}")
                            
                            # Ждем немного, чтобы событие обратной связи могло прийти
                            await asyncio.sleep(3)
                            
                            # Проверяем, пришло ли ЗНАЧИМОЕ событие обратной связи
                            if self._last_meaningful_feedback != original_meaningful_time:
                                _LOGGER.info(f"MegaD-{self.megad.id}: обратная связь работает (значимое событие получено)")
                                return True
                            else:
                                _LOGGER.warning(f"MegaD-{self.megad.id}: команда отправлена, но значимое событие обратной связи не получено")
                                
                                # Попробуем альтернативный порт
                                return await self._test_feedback_alternative()
                        else:
                            _LOGGER.warning(f"MegaD-{self.megad.id}: ошибка отправки тестовой команды, статус: {response.status}")
                            return False
            except AttributeError:
                async with async_timeout.timeout(5):
                    async with session.get(test_cmd_url) as response:
                        if response.status == 200:
                            text = await response.text()
                            _LOGGER.debug(f"MegaD-{self.megad.id}: тестовая команда отправлена, ответ: {text}")
                            
                            # Ждем немного, чтобы событие обратной связи могло прийти
                            await asyncio.sleep(3)
                            
                            # Проверяем, пришло ли ЗНАЧИМОЕ событие обратной связи
                            if self._last_meaningful_feedback != original_meaningful_time:
                                _LOGGER.info(f"MegaD-{self.megad.id}: обратная связь работает (значимое событие получено)")
                                return True
                            else:
                                _LOGGER.warning(f"MegaD-{self.megad.id}: команда отправлена, но значимое событие обратной связи не получено")
                                
                                # Попробуем альтернативный порт
                                return await self._test_feedback_alternative()
                        else:
                            _LOGGER.warning(f"MegaD-{self.megad.id}: ошибка отправки тестовой команды, статус: {response.status}")
                            return False
                    
        except asyncio.TimeoutError:
            _LOGGER.warning(f"MegaD-{self.megad.id}: таймаут при тестировании обратной связи")
            return False
        except Exception as e:
            _LOGGER.warning(f"MegaD-{self.megad.id}: ошибка тестирования обратной связи: {e}")
            return False
    
    async def _test_feedback_alternative(self) -> bool:
        """Альтернативный тест обратной связи."""
        try:
            original_meaningful_time = self._last_meaningful_feedback
            
            # Пробуем другую команду
            session = async_get_clientsession(self.hass)
            base_url = self.megad.url.rstrip('/')
            
            # Альтернативная команда - получить состояние порта
            test_cmd_url = f"{base_url}/sec/?pt=0&cmd=get"
            
            _LOGGER.debug(f"MegaD-{self.megad.id}: альтернативный тест обратной связи: {test_cmd_url}")
            
            # ✅ ИСПРАВЛЕНИЕ: поддержка таймаута для новых версий Python
            try:
                async with asyncio.timeout(5):
                    async with session.get(test_cmd_url) as response:
                        if response.status == 200:
                            text = await response.text()
                            
                            # Ждем
                            await asyncio.sleep(2)
                            
                            # Проверяем, было ли ЗНАЧИМОЕ событие
                            if self._last_meaningful_feedback != original_meaningful_time:
                                return True
                            
                            # Если нет события, но контроллер ответил - возможно обратная связь просто не настроена
                            _LOGGER.warning(f"MegaD-{self.megad.id}: контроллер отвечает, но обратная связь не работает")
                            return False
                        else:
                            return False
            except AttributeError:
                async with async_timeout.timeout(5):
                    async with session.get(test_cmd_url) as response:
                        if response.status == 200:
                            text = await response.text()
                            
                            # Ждем
                            await asyncio.sleep(2)
                            
                            # Проверяем, было ли ЗНАЧИМОЕ событие
                            if self._last_meaningful_feedback != original_meaningful_time:
                                return True
                            
                            # Если нет события, но контроллер ответил - возможно обратная связь просто не настроена
                            _LOGGER.warning(f"MegaD-{self.megad.id}: контроллер отвечает, но обратная связь не работает")
                            return False
                        else:
                            return False
                    
        except Exception:
            return False
    
    # ✅ ОБРАТНАЯ СВЯЗЬ: попытка восстановления обратной связи
    async def _try_restore_feedback(self) -> bool:
        """Пытается восстановить обратную связь без полной перезагрузки контроллера."""
        if not self._feedback_enabled:
            return False
        
        if self._feedback_restore_attempts >= self._max_feedback_restore_attempts:
            _LOGGER.warning(f"MegaD-{self.megad.id}: достигнут максимум попыток восстановления обратной связи ({self._max_feedback_restore_attempts})")
            return False
        
        self._feedback_restore_attempts += 1
        _LOGGER.info(f"MegaD-{self.megad.id}: попытка восстановления обратной связи #{self._feedback_restore_attempts}")
        
        try:
            # 1. Попробуем включить обратную связь в настройках
            if await self._enable_feedback_on_megad():
                _LOGGER.info(f"MegaD-{self.megad.id}: настройки обратной связи обновлены")
                
                # Ждем и проверяем
                await asyncio.sleep(5)
                
                if await self._check_feedback_connection():
                    _LOGGER.info(f"MegaD-{self.megad.id}: обратная связь восстановлена после настройки")
                    self.mark_feedback_event({"type": "feedback_restored", "method": "settings_update", "is_meaningful": True})
                    self._feedback_restore_attempts = 0
                    return True
            
            # 2. Попробуем перезапустить службу обратной связи
            if await self._restart_feedback_service():
                _LOGGER.info(f"MegaD-{self.megad.id}: служба обратной связи перезапущена")
                
                # Ждем
                await asyncio.sleep(5)
                
                if await self._check_feedback_connection():
                    _LOGGER.info(f"MegaD-{self.megad.id}: обратная связь восстановлена после перезапуска службы")
                    self.mark_feedback_event({"type": "feedback_restored", "method": "service_restart", "is_meaningful": True})
                    self._feedback_restore_attempts = 0
                    return True
            
            # 3. Если не помогло, создаем уведомление
            _LOGGER.warning(f"MegaD-{self.megad.id}: не удалось восстановить обратную связь")
            await self._create_feedback_failure_notification()
            
            return False
            
        except Exception as e:
            _LOGGER.error(f"MegaD-{self.megad.id}: ошибка восстановления обратной связи: {e}")
            return False
    
    # ✅ ОБРАТНАЯ СВЯЗЬ: включение обратной связи на контроллере
    async def _enable_feedback_on_megad(self) -> bool:
        """Включает обратную связь на контроллере (страница CF1, параметры sip и srvt)."""
        try:
            session = async_get_clientsession(self.hass)
            base_url = self.megad.url.rstrip('/')
        
            # Определяем адрес сервера Home Assistant
            server_address = await self._get_home_assistant_address()
            if not server_address:
                _LOGGER.warning(f"MegaD-{self.megad.id}: не удалось определить адрес Home Assistant")
                return False
        
            # Правильный URL для CF1 с параметрами sip (адрес) и srvt (тип сервера, 0=HTTP)
            # Формат как на странице: sip=192.168.31.100:8123&srvt=0
            enable_url = f"{base_url}/sec/?cf=1&sip={server_address}&srvt=0&save=1"
        
            _LOGGER.debug(f"MegaD-{self.megad.id}: попытка включения обратной связи: {enable_url}")
        
            try:
                async with asyncio.timeout(5):
                    async with session.get(enable_url) as resp:
                        if resp.status == 200:
                            resp_text = await resp.text()
                            # Проверяем успешность сохранения
                            if 'saved' in resp_text.lower() or 'сохранено' in resp_text.lower() or 'Save' in resp_text:
                                _LOGGER.info(f"MegaD-{self.megad.id}: настройки обратной связи успешно обновлены")
                                return True
                            else:
                                _LOGGER.debug(f"MegaD-{self.megad.id}: ответ контроллера: {resp_text[:200]}")
                                return True  # Считаем успехом, так как ошибки нет
                        else:
                            _LOGGER.warning(f"MegaD-{self.megad.id}: ошибка HTTP: {resp.status}")
                            return False
            except AttributeError:
                async with async_timeout.timeout(5):
                    async with session.get(enable_url) as resp:
                        if resp.status == 200:
                            resp_text = await resp.text()
                            if 'saved' in resp_text.lower() or 'сохранено' in resp_text.lower() or 'Save' in resp_text:
                                _LOGGER.info(f"MegaD-{self.megad.id}: настройки обратной связи успешно обновлены")
                                return True
                            else:
                                return True
                        else:
                            return False
                
        except asyncio.TimeoutError:
            _LOGGER.warning(f"MegaD-{self.megad.id}: таймаут при включении обратной связи")
            return False
        except Exception as e:
            _LOGGER.error(f"MegaD-{self.megad.id}: ошибка включения обратной связи: {e}")
            return False
    
    async def _get_home_assistant_address(self) -> str:
        """Получает адрес Home Assistant для обратной связи (порт 8123)."""
        try:
            # 1. Из конфигурации Home Assistant
            if hasattr(self.hass.config, 'api'):
                host = self.hass.config.api.host or '0.0.0.0'
                if host != '0.0.0.0':
                    return f"{host}:8123"  # Порт 8123, не 8082!
        
            # 2. Из настроек сети
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                local_ip = s.getsockname()[0]
                s.close()
                return f"{local_ip}:8123"  # Порт 8123
            except:
                pass
        
            # 3. Запасной вариант
            _LOGGER.warning(f"MegaD-{self.megad.id}: не удалось определить адрес HA, используем заглушку")
            return "192.168.1.100:8123"
        
        except Exception as e:
            _LOGGER.warning(f"MegaD-{self.megad.id}: ошибка определения адреса HA: {e}")
            return ""
    
    # ✅ ОБРАТНАЯ СВЯЗЬ: перезапуск службы обратной связи
    async def _restart_feedback_service(self) -> bool:
        """Перезапускает службу обратной связи на контроллере (через перезагрузку)."""
        try:
            session = async_get_clientsession(self.hass)
            base_url = self.megad.url.rstrip('/')
        
            # Для CF1 просто перезагружаем контроллер (restart=1)
            restart_url = f"{base_url}/sec/?restart=1"
        
            _LOGGER.debug(f"MegaD-{self.megad.id}: перезагрузка контроллера для применения настроек: {restart_url}")
        
            try:
                async with asyncio.timeout(3):
                    async with session.get(restart_url) as response:
                        if response.status == 200:
                            _LOGGER.info(f"MegaD-{self.megad.id}: команда перезагрузки отправлена")
                            return True
                        else:
                            return False
            except AttributeError:
                async with async_timeout.timeout(3):
                    async with session.get(restart_url) as response:
                        if response.status == 200:
                            _LOGGER.info(f"MegaD-{self.megad.id}: команда перезагрузки отправлена")
                            return True
                        else:
                            return False
            except asyncio.TimeoutError:
                # Таймаут - контроллер начал перезагрузку
                _LOGGER.info(f"MegaD-{self.megad.id}: контроллер перезагружается (таймаут)")
                return True
            
        except Exception as e:
            _LOGGER.error(f"MegaD-{self.megad.id}: ошибка перезапуска: {e}")
            return False
    
    async def _check_megad_health_basic(self) -> bool:
        """Базовая проверка здоровья MegaD (ping + простой HTTP)."""
        if not await self._ping_megad():
            _LOGGER.debug(f"MegaD-{self.megad.id}: не отвечает на ping")
            return False
        
        try:
            session = async_get_clientsession(self.hass)
            base_url = self.megad.url.rstrip('/')
            test_url = f"{base_url}/sec/?cmd=id"
            _LOGGER.debug(f"MegaD-{self.megad.id}: проверка доступности: {test_url}")
            
            # ✅ ИСПРАВЛЕНИЕ: поддержка таймаута для новых версий Python
            try:
                async with asyncio.timeout(3):
                    async with session.get(test_url) as response:
                        if response.status == 200:
                            text = await response.text()
                            if text and text.strip() and 'timeout' not in text.lower():
                                _LOGGER.debug(f"MegaD-{self.megad.id}: доступен (ping и HTTP)")
                                return True
                            else:
                                _LOGGER.debug(f"MegaD-{self.megad.id}: пустой или timeout ответ")
                                return False
                        elif response.status == 401:
                            _LOGGER.error(f"MegaD-{self.megad.id}: ошибка аутентификации (401)")
                            return False
                        else:
                            _LOGGER.debug(f"MegaD-{self.megad.id}: HTTP статус {response.status}")
                            return False
            except AttributeError:
                async with async_timeout.timeout(3):
                    async with session.get(test_url) as response:
                        if response.status == 200:
                            text = await response.text()
                            if text and text.strip() and 'timeout' not in text.lower():
                                _LOGGER.debug(f"MegaD-{self.megad.id}: доступен (ping и HTTP)")
                                return True
                            else:
                                _LOGGER.debug(f"MegaD-{self.megad.id}: пустой или timeout ответ")
                                return False
                        elif response.status == 401:
                            _LOGGER.error(f"MegaD-{self.megad.id}: ошибка аутентификации (401)")
                            return False
                        else:
                            _LOGGER.debug(f"MegaD-{self.megad.id}: HTTP статус {response.status}")
                            return False
        except asyncio.TimeoutError:
            _LOGGER.debug(f"MegaD-{self.megad.id}: таймаут HTTP запроса")
            return False
        except Exception as e:
            _LOGGER.debug(f"MegaD-{self.megad.id}: ошибка HTTP запроса: {e}")
            return False
    
    async def _test_megad_data_pull(self) -> bool:
        """Проверка возможности получения данных от контроллера по запросу."""
        try:
            session = async_get_clientsession(self.hass)
            base_url = self.megad.url.rstrip('/')
            
            test_commands = [
                "?cmd=all",
                "?cmd=uptime",
                "?pt=0&cmd=get",
                "?cmd=ver",
            ]
            
            for cmd in test_commands:
                try:
                    test_url = f"{base_url}/sec/{cmd}" if not cmd.startswith('?') else f"{base_url}/sec{cmd}"
                    _LOGGER.debug(f"MegaD-{self.megad.id}: проверка данных: {test_url}")
                    
                    # ✅ ИСПРАВЛЕНИЕ: поддержка таймаута для новых версий Python
                    try:
                        async with asyncio.timeout(5):
                            async with session.get(test_url) as response:
                                if response.status == 200:
                                    text = await response.text()
                                    if text and text.strip():
                                        _LOGGER.debug(f"MegaD-{self.megad.id}: команда {cmd} вернула данные")
                                        return True
                    except AttributeError:
                        async with async_timeout.timeout(5):
                            async with session.get(test_url) as response:
                                if response.status == 200:
                                    text = await response.text()
                                    if text and text.strip():
                                        _LOGGER.debug(f"MegaD-{self.megad.id}: команда {cmd} вернула данные")
                                        return True
                except asyncio.TimeoutError:
                    _LOGGER.debug(f"MegaD-{self.megad.id}: таймаут на команде {cmd}")
                    continue
                except Exception as e:
                    _LOGGER.debug(f"MegaD-{self.megad.id}: ошибка на команде {cmd}: {e}")
                    continue
            
            _LOGGER.warning(f"MegaD-{self.megad.id}: не отвечает ни на одну команду")
            return False
            
        except Exception as e:
            _LOGGER.error(f"MegaD-{self.megad.id}: ошибка проверки получения данных: {e}")
            return False
    
    async def _test_megad_feedback(self) -> bool:
        """Совместимость со старым кодом."""
        _LOGGER.warning(f"MegaD-{self.megad.id}: использование устаревшего метода _test_megad_feedback")
        return await self._test_megad_data_pull()
    
    async def _ping_megad(self) -> bool:
        """Проверка доступности через ping."""
        try:
            ip_address = str(self.megad.config.plc.ip_megad)
            
            param = '-n' if platform.system().lower() == 'windows' else '-c'
            command = ['ping', param, '1', '-W', '2', ip_address]
            
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await process.communicate()
            
            result = process.returncode == 0
            _LOGGER.debug(f"MegaD-{self.megad.id}: ping результат: {'успех' if result else 'неудача'}")
            return result
            
        except Exception as e:
            _LOGGER.debug(f"Ошибка при ping MegaD-{self.megad.id}: {e}")
            return False
    
    async def _update_coordinator_state(self, is_available: bool):
        """Обновление состояния доступности в координаторе."""
        try:
            self.megad.is_available = is_available
            
            if is_available:
                if hasattr(self.coordinator, 'last_update_success'):
                    self.coordinator.last_update_success = datetime.now()
                if hasattr(self.coordinator, 'last_update_success_time'):
                    self.coordinator.last_update_success_time = datetime.now()
                
                if hasattr(self.coordinator, '_count_connect'):
                    self.coordinator._count_connect = 0
            
            if hasattr(self.coordinator, 'available'):
                self.coordinator.available = is_available
            
            if hasattr(self.coordinator, 'async_update_listeners'):
                try:
                    self.hass.loop.call_soon_threadsafe(self.coordinator.async_update_listeners)
                except:
                    try:
                        self.coordinator.async_update_listeners()
                    except:
                        pass
                    
        except Exception as e:
            _LOGGER.debug(f"Ошибка при обновлении состояния координатора: {e}")
    
    async def _force_data_update_after_recovery(self):
        """Принудительное обновление данных после восстановления связи."""
        try:
            _LOGGER.info(f"=== ПРИНУДИТЕЛЬНОЕ ОБНОВЛЕНИЕ ДЛЯ MegaD-{self.megad.id} ===")
        
            self.mark_data_received()
            self.mark_feedback_event({"type": "forced_update", "is_meaningful": True})
        
            self.megad.is_available = True
            await self._update_coordinator_state(True)
        
            try:
                if hasattr(self.coordinator, 'async_refresh'):
                    await self.coordinator.async_refresh()
                elif hasattr(self.coordinator, '_async_update_data'):
                    await self.coordinator._async_update_data()
            except Exception as e:
                _LOGGER.debug(f"Не удалось обновить через координатор: {e}")
                
                try:
                    if hasattr(self.megad, 'update_data'):
                        await self.megad.update_data()
                        _LOGGER.debug("Данные обновлены напрямую")
                except Exception as e2:
                    _LOGGER.debug(f"Не удалось обновить напрямую: {e2}")
        
            await asyncio.sleep(1)
        
            try:
                if hasattr(self.coordinator, 'async_update_listeners'):
                    self.hass.loop.call_soon(self.coordinator.async_update_listeners)
            except:
                pass
        
            _LOGGER.info(f"✓ Восстановление завершено для MegaD-{self.megad.id}")
        
        except Exception as e:
            _LOGGER.error(f"Ошибка при принудительном обновлении данных: {e}")
    
    async def _execute_recovery_procedure(self) -> bool:
        """Процедура восстановления."""
        if self._recovering:
            _LOGGER.debug(f"MegaD-{self.megad.id} уже в процессе восстановления")
            return False
            
        self._recovering = True
        _LOGGER.warning(f"=== ЗАПУСК ВОССТАНОВЛЕНИЯ ДЛЯ MegaD-{self.megad.id} ===")
        
        try:
            meaningful_inactivity = self._get_meaningful_inactivity_seconds()
            
            _LOGGER.critical(f"MegaD-{self.megad.id}: запуск восстановления, {meaningful_inactivity} сек без значимых событий")
            
            now = datetime.now()
            if self._last_reboot_attempt and (now - self._last_reboot_attempt).total_seconds() < 300:
                _LOGGER.warning(f"MegaD-{self.megad.id}: перезагрузка уже пыталась недавно, пропускаем")
                await self._create_manual_intervention_notification()
                return False
            
            self._last_reboot_attempt = now
            
            if self._feedback_enabled and meaningful_inactivity > 300:
                _LOGGER.info(f"MegaD-{self.megad.id}: пробуем восстановить обратную связь перед перезагрузкой")
                if await self._try_restore_feedback():
                    _LOGGER.info(f"MegaD-{self.megad.id}: обратная связь восстановлена, пропускаем перезагрузку")
                    await self._force_data_update_after_recovery()
                    self._recovering = False
                    return True
            
            _LOGGER.warning(f"MegaD-{self.megad.id}: отправка команды перезагрузки...")
            reboot_success = await self._send_reboot_command()
            
            if reboot_success:
                _LOGGER.info(f"MegaD-{self.megad.id}: команда перезагрузки отправлена")
                
                await asyncio.sleep(10)
                
                if await self._check_megad_health_basic():
                    _LOGGER.info(f"MegaD-{self.megad.id}: доступен после перезагрузки")
                    
                    if self._feedback_enabled:
                        await asyncio.sleep(5)
                        await self._enable_feedback_on_megad()
                    
                    await self._force_data_update_after_recovery()
                    return True
                else:
                    _LOGGER.error(f"MegaD-{self.megad.id}: перезагрузка не помогла")
                    await self._create_failure_notification()
                    return False
            else:
                _LOGGER.error(f"MegaD-{self.megad.id}: не удалось отправить команду перезагрузки")
                await self._create_failure_notification()
                return False
                
        finally:
            self._recovering = False
            self._failure_count = 0
    
    async def _send_reboot_command(self) -> bool:
        """Отправка команды перезагрузки."""
        try:
            session = async_get_clientsession(self.hass)
            base_url = self.megad.url.rstrip('/')
            reboot_url = f"{base_url}/sec/?restart=1"
            
            _LOGGER.info(f"MegaD-{self.megad.id}: отправка команды перезагрузки: {reboot_url}")
            
            try:
                # ✅ ИСПРАВЛЕНИЕ: поддержка таймаута для новых версий Python
                try:
                    async with asyncio.timeout(3):
                        async with session.get(reboot_url) as response:
                            if response.status == 200:
                                text = await response.text()
                                _LOGGER.info(f"MegaD-{self.megad.id}: команда перезагрузки отправлена, ответ: {text[:100]}")
                                return True
                            else:
                                _LOGGER.warning(f"MegaD-{self.megad.id}: команда перезагрузки вернула статус {response.status}")
                                return False
                except AttributeError:
                    async with async_timeout.timeout(3):
                        async with session.get(reboot_url) as response:
                            if response.status == 200:
                                text = await response.text()
                                _LOGGER.info(f"MegaD-{self.megad.id}: команда перезагрузки отправлена, ответ: {text[:100]}")
                                return True
                            else:
                                _LOGGER.warning(f"MegaD-{self.megad.id}: команда перезагрузки вернула статус {response.status}")
                                return False
            except asyncio.TimeoutError:
                _LOGGER.info(f"MegaD-{self.megad.id}: начал перезагрузку (таймаут)")
                return True
                
        except Exception as e:
            _LOGGER.error(f"MegaD-{self.megad.id}: ошибка при отправке команды перезагрузки: {e}")
            return False
    
    async def _create_feedback_failure_notification(self):
        """Создание уведомления о проблемах с обратной связью."""
        try:
            from homeassistant.components import persistent_notification
            
            meaningful_inactivity = self._get_meaningful_inactivity_seconds()
            
            message = f"MegaD-{self.megad.id}: ПРОБЛЕМА С ОБРАТНОЙ СВЯЗЬЮ!\n\n"
            message += f"IP адрес: {self.megad.config.plc.ip_megad}\n"
            message += f"Без значимых событий: {meaningful_inactivity} секунд\n"
            message += f"Попыток восстановления: {self._feedback_restore_attempts}\n\n"
            message += f"Контроллер доступен, но не отправляет события.\n\n"
            message += f"Что нужно сделать:\n"
            message += f"1. Проверить настройки 'Send To Server' в веб-интерфейсе MegaD (страница CF3)\n"
            message += f"2. Убедиться, что адрес сервера правильный: {await self._get_home_assistant_address()}\n"
            message += f"3. Проверить, что порт {self._feedback_port} открыт на роутере\n"
            message += f"4. Использовать сервис 'megad.restart_megad' для перезагрузки\n"
            
            persistent_notification.async_create(
                self.hass,
                message,
                title=f"⚠️ MegaD-{self.megad.id}: проблема с обратной связью",
                notification_id=f"megad_feedback_failure_{self.megad.id}"
            )
        except Exception as e:
            _LOGGER.error(f"Не удалось создать уведомление о проблемах с обратной связью: {e}")
    
    async def _create_failure_notification(self):
        """Создание уведомления."""
        try:
            from homeassistant.components import persistent_notification
            
            meaningful_inactivity = self._get_meaningful_inactivity_seconds()
            
            message = f"MegaD-{self.megad.id}: АВТОМАТИЧЕСКОЕ ВОССТАНОВЛЕНИЕ НЕ УДАЛОСЬ!\n\n"
            message += f"IP адрес: {self.megad.config.plc.ip_megad}\n"
            message += f"Без значимых событий: {meaningful_inactivity} секунд\n\n"
            message += f"Требуется ручное вмешательство:\n"
            message += f"1. Отключить питание контроллера на 10 секунд\n"
            message += f"2. Включить питание обратно\n"
            message += f"3. Проверить соединение Ethernet\n"
            
            persistent_notification.async_create(
                self.hass,
                message,
                title=f"🚨 MegaD-{self.megad.id}: требуется вмешательство!",
                notification_id=f"megad_failure_{self.megad.id}"
            )
        except Exception as e:
            _LOGGER.error(f"Не удалось создать уведомление: {e}")
    
    async def _create_manual_intervention_notification(self):
        """Создание уведомления о необходимости ручного вмешательства."""
        try:
            from homeassistant.components import persistent_notification
            
            meaningful_inactivity = self._get_meaningful_inactivity_seconds()
            
            persistent_notification.async_create(
                self.hass,
                f"MegaD-{self.megad.id}: ТРЕБУЕТСЯ РУЧНОЕ ВМЕШАТЕЛЬСТВО!\n\n"
                f"Контроллер доступен, но не отправляет данные.\n"
                f"Без значимых событий: {meaningful_inactivity} секунд\n"
                f"IP адрес: {self.megad.config.plc.ip_megad}\n\n"
                f"Недавно уже была попытка автоматической перезагрузки.\n\n"
                f"Что нужно сделать ВРУЧНУЮ:\n"
                f"1. Отключить питание контроллера на 10 секунд\n"
                f"2. Включить питание обратно\n"
                f"3. Проверить настройки 'Обратная связь' в веб-интерфейсе\n"
                f"4. Проверить физическое соединение Ethernet\n\n"
                f"После этого система автоматически обнаружит восстановление.",
                title=f"🚨 MegaD-{self.megad.id}: требуется ручное вмешательство!",
                notification_id=f"megad_manual_intervention_{self.megad.id}"
            )
        except Exception as e:
            _LOGGER.error(f"Не удалось создать уведомление: {e}")
    
    async def force_check_and_update(self):
        """Принудительная проверка."""
        _LOGGER.info(f"Принудительная проверка MegaD-{self.megad.id}")
        
        is_healthy = await self._check_megad_health_basic()
        
        if is_healthy:
            feedback_ok = await self._check_feedback_connection()
            
            if feedback_ok:
                inactivity_seconds = self._get_inactivity_seconds()
                
                if inactivity_seconds < self._inactivity_timeout:
                    _LOGGER.info(f"MegaD-{self.megad.id}: доступен, обратная связь работает, есть данные")
                    self._failure_count = 0
                    await self._update_coordinator_state(True)
                    return "Контроллер доступен, обратная связь работает, есть данные"
                else:
                    _LOGGER.info(f"MegaD-{self.megad.id}: доступен, обратная связь работает, но нет событий")
                    self._failure_count = 0
                    await self._update_coordinator_state(True)
                    return "Контроллер доступен, обратная связь работает, но нет событий"
            else:
                _LOGGER.warning(f"MegaD-{self.megad.id}: доступен, но обратная связь не работает")
                await self._try_restore_feedback()
                return "Контроллер доступен, но обратная связь не работает"
        else:
            _LOGGER.warning(f"MegaD-{self.megad.id}: недоступен")
            self._failure_count = self._max_failures
            await self._update_coordinator_state(False)
            await self._execute_recovery_procedure()
            return "Контроллер недоступен"
    
    def get_status(self) -> dict:
        """Получение статуса."""
        inactivity_seconds = self._get_inactivity_seconds()
        feedback_inactivity = self._get_feedback_inactivity_seconds()
        meaningful_inactivity = self._get_meaningful_inactivity_seconds()
        
        if inactivity_seconds < 300:
            activity_status = "active"
            activity_description = "Работает нормально, недавно были данные"
        else:
            activity_status = "no_recent_events"
            activity_description = "Давно не было событий переключения портов"
        
        if meaningful_inactivity < self._feedback_timeout:
            feedback_status = "active"
            feedback_description = "Обратная связь работает, недавно были значимые события"
        else:
            feedback_status = "inactive"
            feedback_description = f"Давно не было значимых событий обратной связи ({meaningful_inactivity} сек)"
        
        return {
            "is_running": self._is_running,
            "last_success": self._last_success.isoformat() if self._last_success else None,
            "last_incoming_data": self._last_incoming_data.isoformat() if self._last_incoming_data else None,
            "inactivity_seconds": inactivity_seconds,
            "inactivity_timeout": self._inactivity_timeout,
            "is_active": inactivity_seconds < 300,
            "activity_status": activity_status,
            "activity_description": activity_description,
            "megad_id": self.megad.id,
            "megad_ip": str(self.megad.config.plc.ip_megad),
            "is_available": self.megad.is_available if hasattr(self.megad, 'is_available') else None,
            "failure_count": self._failure_count,
            "max_failures": self._max_failures,
            "is_recovering": self._recovering,
            "health_check_interval": self._health_check_interval,
            "was_offline": self._was_offline,
            
            "feedback_enabled": self._feedback_enabled,
            "feedback_last_event": self._feedback_last_event.isoformat() if self._feedback_last_event else None,
            "last_meaningful_feedback": self._last_meaningful_feedback.isoformat() if self._last_meaningful_feedback else None,
            "last_data_seconds_ago": inactivity_seconds,
            "feedback_timeout": self._feedback_timeout,
            "feedback_status": feedback_status,
            "feedback_description": feedback_description,
            "feedback_check_attempts": self._feedback_check_attempts,
            "feedback_restore_attempts": self._feedback_restore_attempts,
            "max_feedback_restore_attempts": self._max_feedback_restore_attempts,
            "feedback_port": self._feedback_port,
            "feedback_listeners_count": len(self._feedback_listeners),
            "meaningful_event_counter": self._meaningful_event_counter,
            "non_meaningful_event_counter": self._non_meaningful_event_counter,
        }
    
    async def get_activity_status(self) -> dict:
        """Статус активности для отображения в UI."""
        inactivity_seconds = self._get_inactivity_seconds()
        meaningful_inactivity = self._get_meaningful_inactivity_seconds()
        is_healthy = await self._check_megad_health_basic()
        
        can_pull_data = False
        if is_healthy:
            can_pull_data = await self._test_megad_data_pull()
        
        feedback_ok = False
        if is_healthy and self._feedback_enabled:
            feedback_ok = await self._check_feedback_connection()
        
        if not is_healthy:
            display_status = "❌ НЕДОСТУПЕН"
            display_description = "Контроллер не отвечает на ping/HTTP запросы"
            display_color = "red"
            should_recover = True
            is_active = False
            show_warning = True
            
        elif not feedback_ok and self._feedback_enabled:
            display_status = "⚠️ ОБРАТНАЯ СВЯЗЬ НЕ РАБОТАЕТ"
            display_description = f"Контроллер доступен, но обратная связь не работает ({meaningful_inactivity} сек без значимых событий)"
            display_color = "orange"
            should_recover = True
            is_active = False
            show_warning = True
            
        elif inactivity_seconds < self._inactivity_timeout:
            display_status = "✅ В РАБОТЕ"
            display_description = "Контроллер доступен, обратная связь работает, недавно были данные"
            display_color = "green"
            should_recover = False
            is_active = True
            show_warning = False
            
        elif can_pull_data:
            display_status = "✅ В РАБОТЕ (нет событий)"
            display_description = f"Контроллер доступен, обратная связь работает, но нет событий ({inactivity_seconds} сек)"
            display_color = "blue"
            should_recover = False
            is_active = False
            show_warning = False
            
        else:
            display_status = "⚠️ ПРОБЛЕМА С ДАННЫМИ"
            display_description = f"Контроллер доступен, но не отдает данные по запросу ({inactivity_seconds} сек)"
            display_color = "orange"
            should_recover = True
            is_active = False
            show_warning = True
        
        status = self.get_status()
        
        status.update({
            "is_healthy": is_healthy,
            "can_pull_data": can_pull_data,
            "feedback_ok": feedback_ok,
            "is_active": is_active,
            "display_status": display_status,
            "display_description": display_description,
            "display_color": display_color,
            "should_recover": should_recover,
            "show_warning": show_warning,
            "inactivity_minutes": int(inactivity_seconds / 60),
            "meaningful_inactivity_minutes": int(meaningful_inactivity / 60),
            "watchdog_logic": "meaningful_feedback_check"
        })
        
        if not is_healthy:
            status["recommendations"] = [
                "Проверьте физическое подключение контроллера к сети",
                "Убедитесь, что контроллер включен",
                "Проверьте IP адрес в настройках"
            ]
        elif not feedback_ok and self._feedback_enabled:
            status["recommendations"] = [
                "Проверьте настройки 'Send To Server' в веб-интерфейсе MegaD (страница CF3)",
                "Убедитесь, что адрес сервера правильный",
                "Используйте сервис 'megad.restart_feedback' для перезапуска обратной связи",
                "Проверьте, что порт 8082 открыт на роутере",
                "Попробуйте переключить любой порт на контроллере"
            ]
        elif not can_pull_data and inactivity_seconds > self._inactivity_timeout:
            status["recommendations"] = [
                "Проверьте настройки 'Обратная связь' в веб-интерфейсе MegaD",
                "Убедитесь, что контроллер не в режиме прошивки",
                "Используйте сервис 'megad.restart_megad' для перезагрузки",
                "Проверьте настройки порта 8082 (обратная связь)",
                "Попробуйте переключить любой порт на контроллере"
            ]
        else:
            status["recommendations"] = [
                "Всё в порядке! Если нужны данные, переключите любой порт на контроллере"
            ]
        
        return status
    
    async def get_feedback_status_detailed(self) -> dict:
        """Детальный статус обратной связи для диагностики."""
        try:
            settings_ok = await self._check_feedback_settings(verbose=False)
            ha_address = await self._get_home_assistant_address()
            
            return {
                "enabled": self._feedback_enabled,
                "settings_configured": settings_ok,
                "home_assistant_address": ha_address,
                "feedback_port": self._feedback_port,
                "last_event": self._feedback_last_event.isoformat() if self._feedback_last_event else None,
                "last_meaningful_event": self._last_meaningful_feedback.isoformat() if self._last_meaningful_feedback else None,
                "inactivity_seconds": self._get_feedback_inactivity_seconds(),
                "meaningful_inactivity_seconds": self._get_meaningful_inactivity_seconds(),
                "timeout_seconds": self._feedback_timeout,
                "check_attempts": self._feedback_check_attempts,
                "restore_attempts": self._feedback_restore_attempts,
                "max_restore_attempts": self._max_feedback_restore_attempts,
                "listeners_count": len(self._feedback_listeners),
                "meaningful_event_counter": self._meaningful_event_counter,
                "non_meaningful_event_counter": self._non_meaningful_event_counter,
                "test_result": await self._test_feedback_with_command_detailed(),
                "timestamp": datetime.now().isoformat()
            }
        except Exception as e:
            return {
                "error": str(e),
                "timestamp": datetime.now().isoformat()
            }
    
    async def _test_feedback_with_command_detailed(self) -> dict:
        """Детальное тестирование обратной связи."""
        result = {
            "overall": False,
            "tests": {},
            "timestamp": datetime.now().isoformat()
        }
        
        try:
            session = async_get_clientsession(self.hass)
            base_url = self.megad.url.rstrip('/')
            
            original_meaningful_time = self._last_meaningful_feedback
            
            # Тест 1: Виртуальный порт 255
            try:
                test_url = f"{base_url}/sec/?pt=255&cmd=on"
                
                try:
                    async with asyncio.timeout(5):
                        async with session.get(test_url) as response:
                            status = response.status
                            text = await response.text() if status == 200 else ""
                except AttributeError:
                    async with async_timeout.timeout(5):
                        async with session.get(test_url) as response:
                            status = response.status
                            text = await response.text() if status == 200 else ""
                
                await asyncio.sleep(3)
                
                event_received = self._last_meaningful_feedback != original_meaningful_time
                
                result["tests"]["port_255_on"] = {
                    "url": test_url,
                    "status": status,
                    "response": text[:100],
                    "event_received": event_received,
                    "success": event_received
                }
                
                if event_received:
                    result["overall"] = True
                    return result
            except asyncio.TimeoutError:
                result["tests"]["port_255_on"] = {"timeout": True, "success": False}
            except Exception as e:
                result["tests"]["port_255_on"] = {"error": str(e), "success": False}
            
            # Тест 2: Получить состояние порта 0
            try:
                test_url = f"{base_url}/sec/?pt=0&cmd=get"
                
                try:
                    async with asyncio.timeout(5):
                        async with session.get(test_url) as response:
                            status = response.status
                            text = await response.text() if status == 200 else ""
                except AttributeError:
                    async with async_timeout.timeout(5):
                        async with session.get(test_url) as response:
                            status = response.status
                            text = await response.text() if status == 200 else ""
                
                result["tests"]["port_0_get"] = {
                    "url": test_url,
                    "status": status,
                    "response": text,
                    "success": status == 200 and text and text.strip() and text.strip().lower() != 'busy'
                }
            except asyncio.TimeoutError:
                result["tests"]["port_0_get"] = {"timeout": True, "success": False}
            except Exception as e:
                result["tests"]["port_0_get"] = {"error": str(e), "success": False}
            
            result["overall"] = any(test.get("success", False) for test in result["tests"].values())
            
            return result
            
        except Exception as e:
            result["error"] = str(e)
            result["overall"] = False
            return result
    
    async def restart_feedback_service(self) -> bool:
        """Ручной перезапуск службы обратной связи."""
        _LOGGER.info(f"MegaD-{self.megad.id}: ручной перезапуск службы обратной связи")
        return await self._restart_feedback_service()
    
    async def enable_feedback_service(self) -> bool:
        """Ручное включение обратной связи."""
        _LOGGER.info(f"MegaD-{self.megad.id}: ручное включение обратной связи")
        return await self._enable_feedback_on_megad()
    
    async def test_feedback_service(self) -> dict:
        """Ручное тестирование обратной связи."""
        _LOGGER.info(f"MegaD-{self.megad.id}: ручное тестирование обратной связи")
        return await self._test_feedback_with_command_detailed()
    
    async def _check_megad_health(self) -> bool:
        """Совместимость со старым кодом."""
        return await self._check_megad_health_basic()
    
    async def test_feedback_detailed(self) -> dict:
        """Детальная проверка получения данных для отладки."""
        result = {
            "overall": False,
            "tests": {},
            "timestamp": datetime.now().isoformat()
        }
        
        try:
            session = async_get_clientsession(self.hass)
            base_url = self.megad.url.rstrip('/')
            
            try:
                test_url = f"{base_url}/sec/?cmd=all"
                try:
                    async with asyncio.timeout(5):
                        async with session.get(test_url) as response:
                            status = response.status
                            text = await response.text() if status == 200 else ""
                except AttributeError:
                    async with async_timeout.timeout(5):
                        async with session.get(test_url) as response:
                            status = response.status
                            text = await response.text() if status == 200 else ""
                
                result["tests"]["cmd_all"] = {
                    "url": test_url,
                    "status": status,
                    "text": text[:200] if text else "",
                    "success": status == 200 and text and text.strip(),
                    "has_semicolon": ';' in text if text else False,
                    "semicolon_count": text.count(';') if text else 0
                }
            except asyncio.TimeoutError:
                result["tests"]["cmd_all"] = {"timeout": True, "success": False}
            except Exception as e:
                result["tests"]["cmd_all"] = {"error": str(e), "success": False}
            
            try:
                test_url = f"{base_url}/sec/?cmd=uptime"
                try:
                    async with asyncio.timeout(3):
                        async with session.get(test_url) as response:
                            status = response.status
                            text = await response.text() if status == 200 else ""
                except AttributeError:
                    async with async_timeout.timeout(3):
                        async with session.get(test_url) as response:
                            status = response.status
                            text = await response.text() if status == 200 else ""
                
                result["tests"]["cmd_uptime"] = {
                    "url": test_url,
                    "status": status,
                    "text": text,
                    "success": status == 200 and text and text.strip(),
                    "is_numeric": text.strip().isdigit() if text else False
                }
            except asyncio.TimeoutError:
                result["tests"]["cmd_uptime"] = {"timeout": True, "success": False}
            except Exception as e:
                result["tests"]["cmd_uptime"] = {"error": str(e), "success": False}
            
            try:
                test_url = f"{base_url}/sec/?pt=0&cmd=get"
                try:
                    async with asyncio.timeout(3):
                        async with session.get(test_url) as response:
                            status = response.status
                            text = await response.text() if status == 200 else ""
                except AttributeError:
                    async with async_timeout.timeout(3):
                        async with session.get(test_url) as response:
                            status = response.status
                            text = await response.text() if status == 200 else ""
                
                result["tests"]["pt_cmd_get"] = {
                    "url": test_url,
                    "status": status,
                    "text": text,
                    "success": status == 200 and text and text.strip() and text.strip().lower() != 'busy',
                    "is_busy": text.strip().lower() == 'busy' if text else False
                }
            except asyncio.TimeoutError:
                result["tests"]["pt_cmd_get"] = {"timeout": True, "success": False}
            except Exception as e:
                result["tests"]["pt_cmd_get"] = {"error": str(e), "success": False}
            
            overall_success = any(test.get("success", False) for test in result["tests"].values())
            result["overall"] = overall_success
            
            return result
            
        except Exception as e:
            result["error"] = str(e)
            result["overall"] = False
            return result
        
    def get_status_details(self) -> dict:
        """Полный статус watchdog для атрибутов сенсора."""
        return self.get_status()
    
    def get_inactivity_seconds(self) -> int:
        """Публичный метод получения времени без данных."""
        return self._get_inactivity_seconds()
    
    def get_feedback_status(self) -> str:
        """Получить статус обратной связи как строку."""
        meaningful_inactivity = self._get_meaningful_inactivity_seconds()
        
        if meaningful_inactivity == 0:
            return "inactive"
        elif meaningful_inactivity < 60:
            return "ok"
        elif meaningful_inactivity < 300:
            return "waiting"
        else:
            return "failed"
    
    def get_feedback_details(self) -> dict:
        """Получить детали обратной связи."""
        return {
            "feedback_enabled": self._feedback_enabled,
            "feedback_inactivity_seconds": self._get_feedback_inactivity_seconds(),
            "meaningful_inactivity_seconds": self._get_meaningful_inactivity_seconds(),
            "feedback_last_event": self._feedback_last_event.isoformat() if self._feedback_last_event else None,
            "last_meaningful_event": self._last_meaningful_feedback.isoformat() if self._last_meaningful_feedback else None,
            "megad_id": self.megad.id,
            "meaningful_event_counter": self._meaningful_event_counter,
            "non_meaningful_event_counter": self._non_meaningful_event_counter,
            "timestamp": datetime.now().isoformat()
        }
    
    def get_feedback_inactivity_seconds(self) -> int:
        """Публичный метод получения времени без обратной связи."""
        return self._get_feedback_inactivity_seconds()
    
    def get_meaningful_inactivity_seconds(self) -> int:
        """Публичный метод получения времени без значимых событий."""
        return self._get_meaningful_inactivity_seconds()
