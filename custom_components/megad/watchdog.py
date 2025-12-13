import asyncio
import logging
import platform
import subprocess
from datetime import datetime, timedelta
from typing import Optional

from homeassistant.helpers.aiohttp_client import async_get_clientsession
from .const import WATCHDOG_MAX_FAILURES  # <-- ДОБАВИТЬ ИМПОРТ

_LOGGER = logging.getLogger(__name__)


class MegaDWatchdog:
    """Watchdog для мониторинга и восстановления MegaD."""
    
    def __init__(self, coordinator, hass):
        self.coordinator = coordinator
        self.hass = hass
        self.megad = coordinator.megad
        self._watchdog_task = None
        self._is_running = False
        self._failure_count = 0
        self._max_failures = WATCHDOG_MAX_FAILURES  # Используем константу
        self._recovering = False
        self._last_success = None
        self._health_check_interval = 60  # 1 минута для быстрой реакции
        self._was_offline = False  # Флаг что устройство было оффлайн
        
    async def start(self):
        """Запуск watchdog."""
        if self._is_running:
            _LOGGER.debug(f"Watchdog для MegaD-{self.megad.id} уже запущен")
            return
            
        self._is_running = True
        self._failure_count = 0
        self._recovering = False
        self._was_offline = False
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        _LOGGER.info(f"Watchdog для MegaD-{self.megad.id} запущен")
    
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
    
    async def _watchdog_loop(self):
        """Основной цикл watchdog."""
        _LOGGER.debug(f"Watchdog запущен для MegaD-{self.megad.id} с интервалом {self._health_check_interval} сек")
        
        while self._is_running:
            try:
                await asyncio.sleep(self._health_check_interval)
                
                # Отладочный вывод
                _LOGGER.debug(f"Watchdog проверка MegaD-{self.megad.id} (ошибок: {self._failure_count}/{self._max_failures})")
                
                # Пропускаем проверку если идет восстановление
                if self._recovering:
                    _LOGGER.debug(f"MegaD-{self.megad.id} в процессе восстановления, пропускаем проверку")
                    continue
                
                # Выполняем проверку здоровья
                is_healthy = await self._check_megad_health()
                
                if is_healthy:
                    # Устройство здорово
                    if self._failure_count > 0 or self._was_offline:
                        _LOGGER.info(f"MegaD-{self.megad.id} восстановил связь после {self._failure_count} ошибок")
                        self._failure_count = 0
                        
                        # Если устройство было оффлайн, запускаем принудительное обновление
                        if self._was_offline:
                            _LOGGER.info(f"MegaD-{self.megad.id} был оффлайн, запускаем принудительное обновление данных")
                            await self._force_data_update_after_recovery()
                            self._was_offline = False
                        else:
                            # Обновляем состояние координатора
                            await self._update_coordinator_state(True)
                    
                    self._last_success = datetime.now()
                    _LOGGER.debug(f"MegaD-{self.megad.id} отвечает нормально")
                else:
                    # Устройство не отвечает
                    self._failure_count += 1
                    self._was_offline = True
                    
                    _LOGGER.warning(
                        f"MegaD-{self.megad.id} не отвечает. "
                        f"Счётчик ошибок: {self._failure_count}/{self._max_failures}"
                    )
                    
                    # Обновляем состояние координатора
                    await self._update_coordinator_state(False)
                    
                    # Если достигли максимума ошибок - запускаем восстановление
                    if self._failure_count >= self._max_failures:
                        await self._execute_recovery_procedure()
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                _LOGGER.error(f"Ошибка в watchdog цикле MegaD-{self.megad.id}: {e}")
                await asyncio.sleep(30)
    
    async def _update_coordinator_state(self, is_available: bool):
        """Обновление состояния доступности в координаторе."""
        try:
            # Обновляем флаг доступности в мегаде
            self.megad.is_available = is_available
            
            # Если координатор имеет слушателей, уведомляем их
            if hasattr(self.coordinator, 'async_update_listeners'):
                # Вызываем обновление в основном потоке HA
                self.hass.loop.call_soon(self.coordinator.async_update_listeners)
                
        except Exception as e:
            _LOGGER.debug(f"Ошибка при обновлении состояния координатора: {e}")
    
    async def _force_data_update_after_recovery(self):
        """Принудительное обновление данных после восстановления связи."""
        try:
            _LOGGER.info(f"=== ЗАПУСК ПРИНУДИТЕЛЬНОГО ОБНОВЛЕНИЯ ДАННЫХ ДЛЯ MegaD-{self.megad.id} ===")
            
            # 1. Сбрасываем счетчик ошибок в координаторе
            if hasattr(self.coordinator, '_count_connect'):
                self.coordinator._count_connect = 0
            
            # 2. Сбрасываем флаг недоступности
            if hasattr(self.coordinator, '_was_unavailable'):
                self.coordinator._was_unavailable = True
            
            # 3. Обновляем состояние доступности
            self.megad.is_available = True
            
            # 4. Устанавливаем флаг восстановления
            if hasattr(self.coordinator, '_recovery_in_progress'):
                self.coordinator._recovery_in_progress = True
            
            # 5. Принудительно запрашиваем обновление данных
            _LOGGER.info(f"Принудительный запрос обновления данных для MegaD-{self.megad.id}")
            try:
                # Пробуем разные методы обновления
                if hasattr(self.coordinator, 'async_refresh'):
                    await self.coordinator.async_refresh()
                elif hasattr(self.coordinator, 'force_refresh'):
                    await self.coordinator.force_refresh()
                else:
                    await self.coordinator.async_request_refresh()
            except Exception as e:
                _LOGGER.warning(f"Первая попытка обновления не удалась: {e}")
            
            # 6. Ждем немного для обработки
            await asyncio.sleep(2)
            
            # 7. Если обновление не удалось, пробуем еще раз
            if hasattr(self.coordinator, 'last_update_success') and not self.coordinator.last_update_success:
                _LOGGER.warning(f"Первое обновление не удалось, пробуем еще раз...")
                await asyncio.sleep(3)
                if hasattr(self.coordinator, 'async_refresh'):
                    await self.coordinator.async_refresh()
            
            # 8. Отключаем флаг восстановления
            if hasattr(self.coordinator, '_recovery_in_progress'):
                self.coordinator._recovery_in_progress = False
            
            # 9. Синхронизируем состояния сущностей
            if hasattr(self.coordinator, 'force_sync_all_entities'):
                _LOGGER.info(f"Синхронизация состояний сущностей для MegaD-{self.megad.id}")
                await self.coordinator.force_sync_all_entities()
            
            # 10. Обновляем слушателей
            if hasattr(self.coordinator, 'async_update_listeners'):
                self.coordinator.async_update_listeners()
            
            # 11. Записываем восстановление в историю
            await self._record_recovery_in_history()
            
            _LOGGER.info(f"✓ Принудительное обновление данных завершено для MegaD-{self.megad.id}")
            
        except Exception as e:
            _LOGGER.error(f"Ошибка при принудительном обновлении данных: {e}")
    
    async def _check_megad_health(self) -> bool:
        """Проверка здоровья MegaD."""
        # Сначала проверяем ping
        if not await self._ping_megad():
            _LOGGER.debug(f"MegaD-{self.megad.id} не отвечает на ping")
            return False
        
        # Затем проверяем HTTP
        return await self._http_check_megad()
    
    async def _ping_megad(self) -> bool:
        """Проверка доступности через ping."""
        try:
            ip_address = str(self.megad.config.plc.ip_megad)
            
            # Определяем параметры ping для текущей ОС
            param = '-n' if platform.system().lower() == 'windows' else '-c'
            command = ['ping', param, '1', '-W', '2', ip_address]
            
            # Запускаем ping
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await process.communicate()
            
            return process.returncode == 0
            
        except Exception as e:
            _LOGGER.debug(f"Ошибка при ping MegaD-{self.megad.id}: {e}")
            return False
    
    async def _http_check_megad(self) -> bool:
        """Проверка здоровья MegaD через HTTP."""
        try:
            session = async_get_clientsession(self.hass)
            
            # Пробуем несколько легких запросов с коротким таймаутом
            urls_to_try = [
                f"{self.megad.url}?cmd=uptime",   # Самый легкий запрос
                f"{self.megad.url}?cmd=id",       # Получение ID устройства
                f"{self.megad.url}?pt=0&cmd=get"  # Статус порта 0
            ]
            
            for url in urls_to_try:
                try:
                    async with session.get(url, timeout=3) as response:
                        if response.status == 200:
                            # Читаем ответ для проверки
                            text = await response.text()
                            if text and len(text) > 0:
                                _LOGGER.debug(f"MegaD-{self.megad.id} ответил на {url}")
                                return True
                except asyncio.TimeoutError:
                    continue
                except Exception:
                    continue
                    
        except Exception as e:
            _LOGGER.debug(f"MegaD-{self.megad.id}: ошибка при HTTP проверке: {e}")
        
        return False
    
    async def _execute_recovery_procedure(self) -> bool:
        """Процедура восстановления соединения."""
        if self._recovering:
            _LOGGER.debug(f"MegaD-{self.megad.id} уже в процессе восстановления")
            return False
            
        self._recovering = True
        _LOGGER.warning(f"=== ЗАПУСК ВОССТАНОВЛЕНИЯ ДЛЯ MegaD-{self.megad.id} ===")
        
        try:
            # 1. Пробуем легкий сетевой сброс
            _LOGGER.info(f"Попытка сброса сети MegaD-{self.megad.id}")
            network_reset_success = await self._reset_network_interface()
            
            if network_reset_success:
                _LOGGER.info(f"Команда сброса сети отправлена, ждем 30 секунд...")
                await asyncio.sleep(30)
                
                # Проверяем после сброса сети
                if await self._check_megad_health():
                    _LOGGER.info(f"✓ MegaD-{self.megad.id} восстановлен после сброса сети")
                    await self._force_data_update_after_recovery()
                    return True
                else:
                    _LOGGER.warning(f"Сброс сети не помог, MegaD-{self.megad.id} все еще недоступен")
            else:
                _LOGGER.warning(f"Не удалось отправить команду сброса сети")
            
            # 2. Если не помогло, пробуем перезагрузку
            _LOGGER.warning(f"Пробуем перезагрузку MegaD-{self.megad.id}")
            reboot_success = await self._reboot_megad()
            
            if reboot_success:
                _LOGGER.info(f"Команда перезагрузки отправлена, ждем 10 секунд...")
                await asyncio.sleep(10)
                
                # Проверяем после перезагрузки
                if await self._check_megad_health():
                    _LOGGER.info(f"✓ MegaD-{self.megad.id} восстановлен после перезагрузки")
                    await self._force_data_update_after_recovery()
                    return True
                else:
                    _LOGGER.error(f"Перезагрузка не помогла, MegaD-{self.megad.id} все еще недоступен")
            else:
                _LOGGER.error(f"Не удалось отправить команду перезагрузки")
            
            # 3. Если ничего не помогло
            _LOGGER.error(f"✗ Не удалось восстановить MegaD-{self.megad.id} автоматически")
            await self._create_failure_notification()
            
            return False
            
        finally:
            self._recovering = False
            # Сбрасываем счетчик ошибок после попытки восстановления
            self._failure_count = 0
    
    async def _reset_network_interface(self) -> bool:
        """Отправка команды сброса сети."""
        try:
            session = async_get_clientsession(self.hass)
            
            # Пробуем разные варианты команд сброса сети
            commands = [
                f"{self.megad.url}?cmd=netreset",
                f"{self.megad.url}?pt=255&cmd=netreset",
                f"{self.megad.url}?cmd=ifdown&&cmd=ifup"
            ]
            
            for url in commands:
                try:
                    async with session.get(url, timeout=5) as response:
                        if response.status == 200:
                            _LOGGER.debug(f"Команда сброса сети отправлена: {url}")
                            return True
                except asyncio.TimeoutError:
                    # Таймаут тоже может означать успешное выполнение команды
                    _LOGGER.debug(f"Таймаут при отправке команды сброса сети: {url}")
                    return True
                except Exception:
                    continue
                    
        except Exception as e:
            _LOGGER.error(f"Ошибка при сбросе сети MegaD-{self.megad.id}: {e}")
        
        return False
    
    async def _reboot_megad(self) -> bool:
        """Отправка команды перезагрузки."""
        try:
            session = async_get_clientsession(self.hass)
            
            # Команда перезагрузки
            reboot_url = f"{self.megad.url}?cmd=reboot"
            
            # Отправляем команду с коротким таймаутом
            async with session.get(reboot_url, timeout=3) as response:
                _LOGGER.debug(f"Команда перезагрузки отправлена MegaD-{self.megad.id}")
                return True
                
        except asyncio.TimeoutError:
            # Таймаут ожидаем, так как устройство перезагружается
            _LOGGER.debug(f"MegaD-{self.megad.id} начал перезагрузку (таймаут)")
            return True
        except Exception as e:
            _LOGGER.error(f"Ошибка при отправке команды перезагрузки MegaD-{self.megad.id}: {e}")
        
        return False
    
    async def _record_recovery_in_history(self):
        """Записывает восстановление в историю."""
        try:
            # Ищем сенсор истории восстановлений
            entity_id = f"sensor.{self.megad.id}_watchdog_recovery_history"
            
            # Получаем регистр сущностей
            from homeassistant.helpers import entity_registry
            er = entity_registry.async_get(self.hass)
            
            entity_entry = er.async_get(entity_id)
            if entity_entry:
                # Получаем сущность
                entity = self.hass.data.get("entity_components", {}).get("sensor")
                if entity:
                    # Получаем сущность по entity_id
                    for comp_entity in entity.entities.values():
                        if hasattr(comp_entity, 'entity_id') and comp_entity.entity_id == entity_id:
                            if hasattr(comp_entity, 'add_recovery_record'):
                                comp_entity.add_recovery_record()
                                _LOGGER.debug(f"Запись о восстановлении добавлена в историю для MegaD-{self.megad.id}")
                                break
        except Exception as e:
            _LOGGER.debug(f"Не удалось записать восстановление в историю: {e}")
    
    async def _create_failure_notification(self):
        """Создание уведомления о неудачном восстановлении."""
        try:
            from homeassistant.components import persistent_notification
            
            persistent_notification.async_create(
                self.hass,
                f"MegaD-{self.megad.id} недоступен и автоматическое восстановление не удалось.\n\n"
                f"IP адрес: {self.megad.config.plc.ip_megad}\n"
                f"Последняя проверка: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"Что можно сделать:\n"
                f"1. Проверить физическое подключение и питание\n"
                f"2. Перезагрузить контроллер вручную\n"
                f"3. Использовать сервис 'megad.restart_megad'",
                title=f"⚠️ MegaD-{self.megad.id} не отвечает",
                notification_id=f"megad_failure_{self.megad.id}"
            )
        except Exception as e:
            _LOGGER.error(f"Не удалось создать уведомление: {e}")
    
    async def force_check_and_update(self):
        """Принудительная проверка и обновление состояния."""
        _LOGGER.info(f"Принудительная проверка MegaD-{self.megad.id}")
        
        # Выполняем проверку
        is_healthy = await self._check_megad_health()
        
        if is_healthy:
            _LOGGER.info(f"MegaD-{self.megad.id} доступен")
            self._failure_count = 0
            
            # Если был оффлайн, запускаем принудительное обновление
            if self._was_offline:
                await self._force_data_update_after_recovery()
            else:
                await self._update_coordinator_state(True)
        else:
            _LOGGER.warning(f"MegaD-{self.megad.id} недоступен")
            self._failure_count += 1
            self._was_offline = True
            await self._update_coordinator_state(False)
            
            if self._failure_count >= self._max_failures:
                await self._execute_recovery_procedure()
    
    def get_status(self) -> dict:
        """Получение статуса watchdog."""
        return {
            "is_running": self._is_running,
            "last_success": self._last_success.isoformat() if self._last_success else None,
            "megad_id": self.megad.id,
            "megad_ip": str(self.megad.config.plc.ip_megad),
            "is_available": self.megad.is_available if hasattr(self.megad, 'is_available') else None,
            "failure_count": self._failure_count,
            "max_failures": self._max_failures,
            "is_recovering": self._recovering,
            "health_check_interval": self._health_check_interval,
            "was_offline": self._was_offline
        }
