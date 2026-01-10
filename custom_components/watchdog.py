import asyncio
import logging
import platform
from datetime import datetime
from typing import Optional, Callable, Any, List

from homeassistant.helpers.aiohttp_client import async_get_clientsession
from .const import (
    WATCHDOG_MAX_FAILURES, 
    WATCHDOG_INACTIVITY_TIMEOUT,
    WATCHDOG_CHECK_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


class MegaDWatchdog:
    """Watchdog для мониторинга обратной связи MegaD."""
    
    def __init__(self, coordinator, hass):
        self.coordinator = coordinator
        self.hass = hass
        self.megad = coordinator.megad
        self._watchdog_task = None
        self._is_running = False
        self._failure_count = 0
        self._max_failures = WATCHDOG_MAX_FAILURES
        self._recovering = False
        
        # ✅ ЗАЩИТА ОТ РЕКУРСИИ
        self._updating_feedback = False
        self._updating_data = False
        self._last_update_time = datetime.now()
        self._min_update_interval = 1.0  # Минимальный интервал между обновлениями (сек)
        
        # ОБРАТНАЯ СВЯЗЬ
        self._feedback_last_event = datetime.now()
        self._feedback_timeout = 600  # 10 минут без обратной связи = проблема
        self._feedback_check_interval = 150  # Проверка каждую 2,5 минуты
        
        # Счетчики для восстановления
        self._feedback_restore_attempts = 0
        self._max_feedback_restore_attempts = 2
        
        # Для общего времени без данных
        self._last_data_received = datetime.now()
        
        # Слушатели событий
        self._feedback_listeners: List[Callable] = []
        
        _LOGGER.info(f"Watchdog инициализирован для MegaD-{self.megad.id}")
    
    async def start(self):
        """Запуск watchdog для отслеживания обратной связи."""
        if self._is_running:
            _LOGGER.debug(f"Watchdog для MegaD-{self.megad.id} уже запущен")
            return
            
        self._is_running = True
        self._feedback_last_event = datetime.now()
        self._last_data_received = datetime.now()
        self._failure_count = 0
        self._recovering = False
        
        self._watchdog_task = asyncio.create_task(self._feedback_monitor_loop())
        _LOGGER.info(f"🚀 Watchdog запущен для MegaD-{self.megad.id}")
    
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
        _LOGGER.info(f"Watchdog остановлен для MegaD-{self.megad.id}")
    
    def mark_feedback_event(self, event_data: Any = None):
        """✅ КЛЮЧЕВОЙ МЕТОД: Отметка события обратной связи."""
        # ✅ ЗАЩИТА ОТ РЕКУРСИИ
        if self._updating_feedback:
            _LOGGER.debug(f"MegaD-{self.megad.id}: mark_feedback_event - защита от рекурсии")
            return

        # ✅ СТРОГИЙ КОНТРОЛЬ ИСТОЧНИКА
        source = event_data.get('source', 'unknown') if event_data else 'unknown'
    
        # ✅ СТРОГИЙ СПИСОК: Только реальные HTTP события от контроллера
        feedback_sources = [
            'http_callback', 'http_get', 'http_post', 
            'server_get', 'server_post', 'restore_after_reboot'
        ]
    
        if source not in feedback_sources:
            _LOGGER.debug(f"MegaD-{self.megad.id}: источник '{source}' не является обратной связью, ИГНОРИРУЕМ")
            # ❌ НЕ ВЫЗЫВАЕМ mark_data_received()!
            return

        # ✅ ТОЛЬКО ТЕПЕРЬ - РЕАЛЬНАЯ ОБРАТНАЯ СВЯЗЬ ОТ КОНТРОЛЛЕРА
        self._updating_feedback = True
        try:
            old_time = self._feedback_last_event
            time_since_last = (datetime.now() - old_time).total_seconds()
    
            _LOGGER.info(
                f"MegaD-{self.megad.id}: 🔄 ПОЛУЧЕНА РЕАЛЬНАЯ ОБРАТНАЯ СВЯЗЬ ОТ КОНТРОЛЛЕРА! "
                f"Источник: {source}, прошло: {time_since_last:.1f} сек"
            )
    
            self._feedback_last_event = datetime.now()
    
            # ✅ ОБНОВЛЯЕМ И ОБЩИЕ ДАННЫЕ (это реальные данные от контроллера)
            self.mark_data_received()
    
            # Сбрасываем счетчики проблем
            self._failure_count = 0
            self._feedback_restore_attempts = 0
    
            _LOGGER.info(f"MegaD-{self.megad.id}: ✅ Обратная связь подтверждена, счетчики сброшены")
    
            # ✅ УМНОЕ ОБНОВЛЕНИЕ
            self._safe_update_listeners()
    
        finally:
            self._updating_feedback = False
    
    def _safe_update_listeners(self):
        """Безопасное обновление слушателей с защитой от рекурсии и flood."""
        now = datetime.now()
        time_since_last_update = (now - self._last_update_time).total_seconds()
    
        # ✅ Ограничиваем частоту обновлений
        if time_since_last_update < self._min_update_interval:
            _LOGGER.debug(f"MegaD-{self.megad.id}: слишком частое обновление ({time_since_last_update:.1f} сек)")
            return
    
        # ✅ Проверяем, не вызываем ли мы сами себя через рекурсию
        if hasattr(self.coordinator, '_updating_watchdog') and self.coordinator._updating_watchdog:
            _LOGGER.debug(f"MegaD-{self.megad.id}: предотвращена рекурсия в _safe_update_listeners")
            return
    
        try:
            self.coordinator._updating_watchdog = True
            _LOGGER.debug(f"MegaD-{self.megad.id}: обновление слушателей")
            self.coordinator.async_update_listeners()
            self._last_update_time = now
        except Exception as e:
            _LOGGER.error(f"MegaD-{self.megad.id}: ошибка при обновлении слушателей: {e}")
        finally:
            self.coordinator._updating_watchdog = False
    
    async def _feedback_monitor_loop(self):
        """Основной цикл мониторинга обратной связи."""
        _LOGGER.info(f"🔄 Запущен монитор обратной связи для MegaD-{self.megad.id}")
        
        while self._is_running:
            try:
                await asyncio.sleep(self._feedback_check_interval)
                
                # Проверяем время с последнего события
                feedback_inactivity = self._get_feedback_inactivity_seconds()
                
                # Логируем статус
                if feedback_inactivity < 60:
                    _LOGGER.debug(f"MegaD-{self.megad.id}: обратная связь активна ({feedback_inactivity} сек)")
                elif feedback_inactivity < 300:
                    _LOGGER.info(f"MegaD-{self.megad.id}: обратная связь работает ({feedback_inactivity//60} мин)")
                else:
                    _LOGGER.warning(f"MegaD-{self.megad.id}: ⚠️ {feedback_inactivity//60} мин без обратной связи")
                
                # ✅ УМНОЕ ОБНОВЛЕНИЕ
                self._safe_update_listeners()
                
                # Если больше 5 минут без обратной связи
                if feedback_inactivity > self._feedback_timeout:
                    await self._handle_feedback_timeout()
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                _LOGGER.error(f"Ошибка в мониторе обратной связи MegaD-{self.megad.id}: {e}")
                await asyncio.sleep(60)
    
    def _get_feedback_inactivity_seconds(self) -> int:
        """Время без событий обратной связи."""
        if not self._feedback_last_event:
            return 999999
        return int((datetime.now() - self._feedback_last_event).total_seconds())
    
    def _get_inactivity_seconds(self) -> int:
        """Общее время без получения любых данных."""
        if not self._last_data_received:
            return 999999
        return int((datetime.now() - self._last_data_received).total_seconds())
    
    async def _handle_feedback_timeout(self):
        """Обработка таймаута обратной связи."""
        feedback_inactivity = self._get_feedback_inactivity_seconds()
        minutes = feedback_inactivity // 60
        
        _LOGGER.warning(f"MegaD-{self.megad.id}: 🚨 {minutes} минут без обратной связи!")
        
        # Проверяем доступность контроллера
        is_healthy = await self._check_megad_health()
        
        if not is_healthy:
            # Контроллер недоступен
            _LOGGER.error(f"MegaD-{self.megad.id}: контроллер недоступен!")
            self._failure_count += 1
            
            if self._failure_count >= self._max_failures and not self._recovering:
                await self._execute_recovery_procedure()
        else:
            # Контроллер доступен, но обратная связь не работает
            _LOGGER.error(f"MegaD-{self.megad.id}: доступен, но обратная связь не работает!")
            self._feedback_restore_attempts += 1
            
            if self._feedback_restore_attempts >= self._max_feedback_restore_attempts:
                await self._create_feedback_failure_notification()
            else:
                await self._try_restore_feedback()
        
        # ✅ УМНОЕ ОБНОВЛЕНИЕ
        self._safe_update_listeners()
    
    # ... остальные методы остаются без изменений ...
    
    async def _check_megad_health(self) -> bool:
        """Проверка доступности контроллера."""
        try:
            # 1. Проверяем ping
            if not await self._ping_megad():
                return False
            
            # 2. Проверяем HTTP соединение
            session = async_get_clientsession(self.hass)
            base_url = self.megad.url.rstrip('/')
            test_url = f"{base_url}/sec/?cmd=id"
            
            async with session.get(test_url, timeout=5) as response:
                if response.status == 200:
                    text = await response.text()
                    return bool(text and text.strip() and 'timeout' not in text.lower())
                return False
                
        except Exception:
            return False
    
    async def _ping_megad(self) -> bool:
        """Проверка через ping."""
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
            return process.returncode == 0
            
        except Exception:
            return False
    
    async def _execute_recovery_procedure(self) -> bool:
        """Процедура восстановления при недоступности."""
        if self._recovering:
            return False
        
        self._recovering = True
        _LOGGER.warning(f"=== ЗАПУСК ВОССТАНОВЛЕНИЯ ДЛЯ MegaD-{self.megad.id} ===")
    
        try:
            # Отправляем команду перезагрузки
            reboot_success = await self._send_reboot_command()
        
            if not reboot_success:
                _LOGGER.error(f"MegaD-{self.megad.id}: не удалось отправить команду перезагрузки")
                return False
        
            # Ждем перезагрузки
            _LOGGER.info(f"MegaD-{self.megad.id}: ожидание перезагрузки (60 сек)...")
            await asyncio.sleep(60)
        
            # Проверяем восстановление
            for attempt in range(3):
                wait_time = 20 * (attempt + 1)
                await asyncio.sleep(wait_time)
            
                if await self._check_megad_health():
                    _LOGGER.info(f"MegaD-{self.megad.id}: ✅ доступен после перезагрузки")
                
                    # Сбрасываем счетчики
                    self._failure_count = 0
                    self._feedback_restore_attempts = 0
                
                    # ✅ ИСПРАВЛЕНИЕ: Не вызываем mark_feedback_event для искусственных событий
                    # Вместо этого просто обновляем данные
                    self.mark_data_received()
                    _LOGGER.info(f"MegaD-{self.megad.id}: данные обновлены после восстановления")
                
                    return True
        
            return False
        
        finally:
            self._recovering = False
    
    async def _try_restore_feedback(self) -> bool:
        """Попытка восстановления обратной связи."""
        _LOGGER.info(f"MegaD-{self.megad.id}: попытка восстановления обратной связи #{self._feedback_restore_attempts}")
        
        try:
            # Пробуем перезагрузить контроллер
            reboot_success = await self._send_reboot_command()
            
            if not reboot_success:
                return False
            
            # Ждем
            await asyncio.sleep(60)
            
            # Проверяем
            if await self._check_megad_health():
                _LOGGER.info(f"MegaD-{self.megad.id}: обратная связь восстановлена")
                return True
            
            return False
            
        except Exception as e:
            _LOGGER.error(f"Ошибка восстановления обратной связи: {e}")
            return False
    
    async def _send_reboot_command(self) -> bool:
        """Отправка команды перезагрузки."""
        try:
            session = async_get_clientsession(self.hass)
            base_url = self.megad.url.rstrip('/')
        
            # Получаем текущие настройки контроллера
            megad_ip = str(self.megad.config.plc.ip_megad)
            ha_ip = "192.168.31.100:8123"  # или извлечь из конфигурации
        
            # Формируем URL с параметрами конфигурации
            config_params = (
                f"?cf=1&eip={megad_ip}&emsk=255.255.255.0"
                f"&pwd=sec&gw=255.255.255.255"
                f"&sip={ha_ip}&srvt=0&sct=megad"
                f"&pr=&lp=10&gsm=0&gsmf=1"
            )
            reboot_url = f"{base_url}/sec/{config_params}"
        
            async with session.get(reboot_url, timeout=5) as response:
                return response.status == 200
        except Exception:
            return False
    
    async def _create_feedback_failure_notification(self):
        """Создание уведомления о проблеме с обратной связью."""
        try:
            from homeassistant.components import persistent_notification
            
            feedback_inactivity = self._get_feedback_inactivity_seconds()
            minutes = feedback_inactivity // 60
            
            message = f"MegaD-{self.megad.id}: КРИТИЧЕСКАЯ ПРОБЛЕМА С ОБРАТНОЙ СВЯЗЬЮ!\n\n"
            message += f"Без обратной связи: {minutes} минут\n"
            message += f"Попыток восстановления: {self._feedback_restore_attempts}\n\n"
            message += f"Контроллер доступен, но не отправляет события.\n\n"
            message += f"РЕКОМЕНДАЦИИ:\n"
            message += f"1. Проверьте настройки SRV в MegaD\n"
            message += f"2. Проверьте настройки роутера\n"
            message += f"3. Перезагрузите контроллер вручную\n"
            
            persistent_notification.async_create(
                self.hass,
                message,
                title=f"⚠️ MegaD-{self.megad.id}: потеря обратной связи",
                notification_id=f"megad_feedback_critical_{self.megad.id}"
            )
        except Exception:
            pass
    
    # ✅ ПУБЛИЧНЫЕ МЕТОДЫ ДЛЯ ВНЕШНЕГО ИСПОЛЬЗОВАНИЯ
    
    async def check_megad_health(self) -> bool:
        """Публичный метод проверки доступности контроллера."""
        return await self._check_megad_health()
    
    def get_inactivity_seconds(self) -> int:
        """Публичный метод получения общего времени без данных."""
        return self._get_inactivity_seconds()
    
    def get_feedback_inactivity_seconds(self) -> int:
        """Публичный метод получения времени без обратной связи."""
        return self._get_feedback_inactivity_seconds()
    
    def get_feedback_status(self) -> str:
        """Получение статуса обратной связи в виде строки (для сенсоров)."""
        feedback_inactivity = self._get_feedback_inactivity_seconds()
        
        if not self._is_running:
            return "неактивен"
        elif self._recovering:
            return "восстановление"
        elif feedback_inactivity > self._feedback_timeout:
            minutes = feedback_inactivity // 60
            seconds = feedback_inactivity % 60
            return f"проблема ({minutes}м {seconds}с)"
        elif feedback_inactivity < 60:  # меньше минуты
            return f"работает ({feedback_inactivity}с)"
        elif feedback_inactivity < 300:  # меньше 5 минут
            minutes = feedback_inactivity // 60
            return f"работает ({minutes}м)"
        else:
            minutes = feedback_inactivity // 60
            return f"работает ({minutes}м)"
    
    def get_status(self) -> dict:
        """Получить статус watchdog."""
        feedback_inactivity = self._get_feedback_inactivity_seconds()
        general_inactivity = self._get_inactivity_seconds()
        
        status_text = "✅ работает"
        if feedback_inactivity > self._feedback_timeout:
            status_text = "⚠️ проблема"
        elif feedback_inactivity > 600:
            status_text = "❌ критическая"
        
        return {
            "megad_id": self.megad.id,
            "status": status_text,
            "last_feedback": self._feedback_last_event.isoformat() if self._feedback_last_event else None,
            "last_data": self._last_data_received.isoformat() if self._last_data_received else None,
            "inactivity_seconds": general_inactivity,
            "feedback_inactivity_seconds": feedback_inactivity,
            "inactivity_minutes": general_inactivity // 60,
            "feedback_inactivity_minutes": feedback_inactivity // 60,
            "is_running": self._is_running,
            "is_recovering": self._recovering,
            "restore_attempts": self._feedback_restore_attempts,
            "failure_count": self._failure_count,
            "megad_ip": str(self.megad.config.plc.ip_megad) if hasattr(self.megad.config.plc, 'ip_megad') else 'unknown',
            "is_active": self._is_running and not self._recovering and feedback_inactivity < self._feedback_timeout,
            "show_warning": feedback_inactivity > self._feedback_timeout * 0.5,  # Предупреждение при 50% таймаута
        }
    
    # ✅ МЕТОДЫ ДЛЯ СЕНСОРОВ - УЖЕ ИСПРАВЛЕНЫ ВЫШЕ
    
    # ✅ ОБРАТНАЯ СОВМЕСТИМОСТЬ - старые методы, которые могут вызываться
    
    async def _reboot_megad(self) -> bool:
        """Старый метод для обратной совместимости."""
        _LOGGER.warning(f"MegaD-{self.megad.id}: использование deprecated метода _reboot_megad")
        return await self._send_reboot_command()
    
    async def _restore_feedback(self) -> bool:
        """Старый метод для обратной совместимости."""
        _LOGGER.warning(f"MegaD-{self.megad.id}: использование deprecated метода _restore_feedback")
        return await self._try_restore_feedback()
    
    async def force_check(self) -> str:
        """Старый метод для обратной совместимости."""
        _LOGGER.warning(f"MegaD-{self.megad.id}: использование deprecated метода force_check")
        feedback_inactivity = self._get_feedback_inactivity_seconds()
        
        if feedback_inactivity > self._feedback_timeout:
            return f"MegaD-{self.megad.id}: проблема с обратной связью ({feedback_inactivity//60} мин)"
        else:
            return f"MegaD-{self.megad.id}: работает ({feedback_inactivity//60} мин)"
    
    async def restore_feedback(self) -> bool:
        """Старый метод для обратной совместимости."""
        _LOGGER.warning(f"MegaD-{self.megad.id}: использование deprecated метода restore_feedback")
        return await self._try_restore_feedback()
    
    def mark_data_received(self):
        """Отметка получения любых данных (не только обратной связи)."""
        # ✅ ЗАЩИТА ОТ РЕКУРСИИ
        if self._updating_data:
            _LOGGER.debug(f"MegaD-{self.megad.id}: mark_data_received - защита от рекурсии")
            return
    
        self._updating_data = True
        try:
            old_time = self._last_data_received
            self._last_data_received = datetime.now()
        
            time_diff = (self._last_data_received - old_time).total_seconds()
            _LOGGER.debug(f"MegaD-{self.megad.id}: данные получены (прошло: {time_diff:.1f} сек)")
        
            # ✅ УМНОЕ ОБНОВЛЕНИЕ: только если прошло достаточно времени
            self._safe_update_listeners()
        
        finally:
            self._updating_data = False