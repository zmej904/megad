import asyncio
import logging
import platform
import socket
import re
from datetime import datetime
from typing import Optional, Callable, Any, List

from homeassistant.helpers.aiohttp_client import async_get_clientsession
from .const import (
    WATCHDOG_MAX_FAILURES, 
    WATCHDOG_INACTIVITY_TIMEOUT,
    WATCHDOG_CHECK_INTERVAL,
    DOMAIN,
    DEFAULT_CF1_SETTINGS
)

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
        self._max_failures = WATCHDOG_MAX_FAILURES
        self._recovering = False
        self._last_success = None
        self._last_incoming_data = None
        self._health_check_interval = WATCHDOG_CHECK_INTERVAL
        self._was_offline = False
        self._inactivity_timeout = WATCHDOG_INACTIVITY_TIMEOUT
        self._last_reboot_attempt = None
        
        # Обратная связь
        self._feedback_enabled = True
        self._feedback_port = 8123
        self._feedback_listeners: List[Callable] = []
        self._feedback_last_event = None
        self._last_meaningful_feedback = None
        self._feedback_timeout = 900
        self._feedback_check_attempts = 0
        self._feedback_restore_attempts = 0
        self._max_feedback_restore_attempts = 3
        self._meaningful_event_counter = 0
        self._non_meaningful_event_counter = 0
        
    async def start(self):
        """Запуск watchdog."""
        if self._is_running:
            return
            
        self._is_running = True
        self._failure_count = 0
        self._recovering = False
        self._was_offline = False
        self._last_incoming_data = datetime.now()
        self._last_success = datetime.now()
        self._last_reboot_attempt = None
        
        self._feedback_last_event = datetime.now()
        self._last_meaningful_feedback = datetime.now()
        self._feedback_check_attempts = 0
        self._feedback_restore_attempts = 0
        self._meaningful_event_counter = 0
        self._non_meaningful_event_counter = 0
        
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        _LOGGER.info(f"Watchdog для MegaD-{self.megad.id} запущен")
        
        # При первом запуске сохраняем эталонные настройки
        try:
            await self._init_standard_settings()
        except Exception as e:
            _LOGGER.warning(f"MegaD-{self.megad.id}: ошибка инициализации: {e}")
    
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
    
    def mark_feedback_event(self, event_data: Any = None):
        """Вызывается при получении события обратной связи."""
        is_meaningful = False
        
        if isinstance(event_data, dict):
            if event_data.get('is_meaningful') or event_data.get('port_id') is not None:
                is_meaningful = True
            elif event_data.get('type') in ['port_updated', 'http_callback', 'port_update']:
                is_meaningful = True
        
        self._feedback_last_event = datetime.now()
        self.mark_data_received()
        
        if is_meaningful:
            self._last_meaningful_feedback = datetime.now()
            self._feedback_check_attempts = 0
            self._feedback_restore_attempts = 0
            self._meaningful_event_counter += 1
            _LOGGER.debug(f"MegaD-{self.megad.id}: ✅ ЗНАЧИМОЕ событие (#{self._meaningful_event_counter})")
        else:
            self._non_meaningful_event_counter += 1
    
    async def _watchdog_loop(self):
        """Основной цикл watchdog."""
        _LOGGER.debug(f"Watchdog запущен с интервалом {self._health_check_interval} сек")

        while self._is_running:
            try:
                await asyncio.sleep(self._health_check_interval)
                
                if self._recovering or self.megad.is_flashing:
                    continue
                
                # Периодическая проверка настроек CF1 (раз в час)
                if int(datetime.now().timestamp()) % 3600 < self._health_check_interval:
                    await self._check_and_fix_cf1()
                
                # Проверка доступности контроллера
                is_healthy = await self._check_megad_health_basic()
                
                if not is_healthy:
                    self._failure_count = min(self._failure_count + 1, self._max_failures)
                    self._was_offline = True
                    await self._update_coordinator_state(False)
                    
                    if self._failure_count >= self._max_failures:
                        await self._execute_recovery_procedure()
                    continue
                
                # Проверка обратной связи
                feedback_ok = await self._check_feedback_connection()
                
                if not feedback_ok and self._feedback_enabled:
                    self._feedback_check_attempts += 1
                    _LOGGER.warning(f"MegaD-{self.megad.id}: нет обратной связи (попытка {self._feedback_check_attempts})")
                    
                    if self._feedback_check_attempts >= 3:
                        await self._try_restore_feedback()
                    continue
                
                # Нормальная работа
                inactivity_seconds = self._get_inactivity_seconds()
                
                if inactivity_seconds < self._inactivity_timeout:
                    if self._failure_count > 0:
                        _LOGGER.info(f"MegaD-{self.megad.id}: восстановил нормальную работу")
                        self._failure_count = 0
                        self._was_offline = False
                    await self._update_coordinator_state(True)
                    
                else:
                    # Давно нет данных - проверяем
                    can_pull_data = await self._test_megad_data_pull()
                    
                    if can_pull_data:
                        _LOGGER.info(f"MegaD-{self.megad.id}: доступен, но нет событий")
                        self._failure_count = 0
                        await self._update_coordinator_state(True)
                    else:
                        self._failure_count = min(self._failure_count + 1, self._max_failures)
                        await self._update_coordinator_state(False)
                        
                        if self._failure_count >= self._max_failures:
                            await self._execute_recovery_procedure()
                            
            except asyncio.CancelledError:
                break
            except Exception as e:
                _LOGGER.error(f"Ошибка в цикле watchdog: {e}")
                await asyncio.sleep(30)
    
    # ========== ВСПОМОГАТЕЛЬНЫЕ МЕТОДЫ ==========
    
    def _get_inactivity_seconds(self) -> int:
        if not self._last_incoming_data:
            return 0
        return int((datetime.now() - self._last_incoming_data).total_seconds())
    
    def _get_meaningful_inactivity_seconds(self) -> int:
        if not self._last_meaningful_feedback:
            return 999999
        return int((datetime.now() - self._last_meaningful_feedback).total_seconds())
    
    async def _check_feedback_connection(self) -> bool:
        if not self._feedback_enabled:
            return True
        
        meaningful_inactivity = self._get_meaningful_inactivity_seconds()
        if meaningful_inactivity < self._feedback_timeout:
            return True
        
        settings_ok = await self._check_feedback_settings()
        if not settings_ok:
            return False
        
        return await self._test_feedback_with_command()
    
    async def _check_feedback_settings(self, verbose: bool = False) -> bool:
        try:
            session = async_get_clientsession(self.hass)
            base_url = self.megad.url.rstrip('/')
            config_url = f"{base_url}/sec/?cf=1"
            
            async with async_timeout.timeout(10):
                async with session.get(config_url) as response:
                    if response.status != 200:
                        return False
                    text = await response.text()
            
            # Проверяем SRV Type (должен быть HTTP - 0)
            if 'srvt' in text and 'value=0 selected' not in text and 'srvt=0' not in text:
                if verbose:
                    _LOGGER.warning(f"MegaD-{self.megad.id}: SRV Type не HTTP")
                return False
            
            return True
        except Exception as e:
            if verbose:
                _LOGGER.warning(f"MegaD-{self.megad.id}: ошибка проверки: {e}")
            return False
    
    async def _test_feedback_with_command(self) -> bool:
        try:
            original_time = self._last_meaningful_feedback
            session = async_get_clientsession(self.hass)
            base_url = self.megad.url.rstrip('/')
            
            async with async_timeout.timeout(5):
                async with session.get(f"{base_url}/sec/?pt=255&cmd=on") as resp:
                    if resp.status == 200:
                        await asyncio.sleep(2)
                        if self._last_meaningful_feedback != original_time:
                            return True
            return False
        except Exception:
            return False
    
    async def _check_megad_health_basic(self) -> bool:
        if not await self._ping_megad():
            return False
        
        try:
            session = async_get_clientsession(self.hass)
            base_url = self.megad.url.rstrip('/')
            
            async with async_timeout.timeout(3):
                async with session.get(f"{base_url}/sec/?cmd=id") as response:
                    if response.status == 200:
                        text = await response.text()
                        return bool(text and text.strip() and 'timeout' not in text.lower())
            return False
        except Exception:
            return False
    
    async def _test_megad_data_pull(self) -> bool:
        try:
            session = async_get_clientsession(self.hass)
            base_url = self.megad.url.rstrip('/')
            
            for cmd in ["?cmd=all", "?cmd=uptime", "?cmd=ver"]:
                try:
                    async with async_timeout.timeout(5):
                        async with session.get(f"{base_url}/sec{cmd}") as response:
                            if response.status == 200:
                                text = await response.text()
                                if text and text.strip():
                                    return True
                except:
                    continue
            return False
        except Exception:
            return False
    
    async def _ping_megad(self) -> bool:
        try:
            ip_address = str(self.megad.config.plc.ip_megad)
            param = '-n' if platform.system().lower() == 'windows' else '-c'
            process = await asyncio.create_subprocess_exec(
                'ping', param, '1', '-W', '2', ip_address,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await process.communicate()
            return process.returncode == 0
        except Exception:
            return False
    
    async def _update_coordinator_state(self, is_available: bool):
        try:
            self.megad.is_available = is_available
            if hasattr(self.coordinator, 'available'):
                self.coordinator.available = is_available
            if hasattr(self.coordinator, 'async_update_listeners'):
                try:
                    self.hass.loop.call_soon_threadsafe(self.coordinator.async_update_listeners)
                except:
                    pass
        except Exception as e:
            _LOGGER.debug(f"Ошибка обновления состояния: {e}")
    
    # ========== ОСНОВНЫЕ МЕТОДЫ ВОССТАНОВЛЕНИЯ ==========
    
    async def _send_correct_cf1_with_save(self) -> bool:
        """Отправляет правильные настройки CF1 с save=1 (кнопка Save)."""
        try:
            session = async_get_clientsession(self.hass)
            base_url = self.megad.url.rstrip('/')
            
            # Определяем правильный адрес HA
            ha_address = await self._get_home_assistant_address()
            if not ha_address:
                _LOGGER.error(f"MegaD-{self.megad.id}: не удалось определить адрес HA")
                return False
            
            # Получаем эталонные настройки
            standard = await self._get_standard_cf1_settings()
            
            # Формируем URL с save=1 (кнопка Save)
            update_url = (
                f"{base_url}/sec/?cf=1"
                f"&eip={self.megad.config.plc.ip_megad}"
                f"&emsk={standard.get('emsk', '255.255.255.0')}"
                f"&pwd={standard.get('pwd', 'sec')}"
                f"&gw={standard.get('gw', '255.255.255.255')}"
                f"&sip={ha_address.replace(':', '%3A')}"
                f"&srvt=0"
                f"&sct={standard.get('sct', 'megad')}"
                f"&pr={standard.get('pr', '')}"
                f"&lp={standard.get('lp', '10')}"
                f"&gsm={standard.get('gsm', '0')}"
                f"&gsmf={standard.get('gsmf', '1')}"
                f"&save=1"
            )
            
            _LOGGER.info(f"MegaD-{self.megad.id}: отправка правильных настроек на CF1")
            _LOGGER.info(f"  Адрес сервера: {ha_address}")
            
            async with async_timeout.timeout(5):
                async with session.get(update_url) as resp:
                    if resp.status == 200:
                        _LOGGER.info(f"MegaD-{self.megad.id}: ✅ настройки применены (Save)")
                        return True
                    return False
                    
        except asyncio.TimeoutError:
            # Контроллер перезагружается - это нормально
            _LOGGER.info(f"MegaD-{self.megad.id}: контроллер перезагружается после Save")
            return True
        except Exception as e:
            _LOGGER.error(f"MegaD-{self.megad.id}: ошибка отправки настроек: {e}")
            return False
    
    async def _try_restore_feedback(self) -> bool:
        """Пытается восстановить обратную связь отправкой Save с правильными настройками."""
        if not self._feedback_enabled:
            return False
        
        if self._feedback_restore_attempts >= self._max_feedback_restore_attempts:
            _LOGGER.warning(f"MegaD-{self.megad.id}: максимум попыток восстановления")
            return False
        
        self._feedback_restore_attempts += 1
        _LOGGER.info(f"MegaD-{self.megad.id}: восстановление обратной связи (попытка {self._feedback_restore_attempts})")
        
        # Отправляем правильные настройки с save=1
        success = await self._send_correct_cf1_with_save()
        
        if success:
            # Контроллер перезагрузился, ждем восстановления
            await asyncio.sleep(5)
            if await self._check_feedback_connection():
                _LOGGER.info(f"MegaD-{self.megad.id}: ✅ обратная связь восстановлена!")
                self._feedback_restore_attempts = 0
                return True
        
        return False
    
    async def _execute_recovery_procedure(self) -> bool:
        """Полная процедура восстановления."""
        if self._recovering:
            return False
            
        self._recovering = True
        _LOGGER.warning(f"=== ЗАПУСК ВОССТАНОВЛЕНИЯ ДЛЯ MegaD-{self.megad.id} ===")
        
        try:
            # Сначала пробуем восстановить обратную связь через Save настроек
            if self._feedback_enabled:
                if await self._try_restore_feedback():
                    await self._force_data_update_after_recovery()
                    return True
            
            # Если не помогло - перезагружаем контроллер и затем отправляем Save
            _LOGGER.warning(f"MegaD-{self.megad.id}: перезагрузка контроллера...")
            reboot_success = await self._send_reboot_command()
            
            if reboot_success:
                await asyncio.sleep(3)
                
                # После перезагрузки отправляем Save с правильными настройками
                _LOGGER.info(f"MegaD-{self.megad.id}: отправка настроек после перезагрузки...")
                save_success = await self._send_correct_cf1_with_save()
                
                if save_success:
                    await asyncio.sleep(5)
                    if await self._check_megad_health_basic():
                        _LOGGER.info(f"MegaD-{self.megad.id}: ✅ восстановление успешно!")
                        await self._force_data_update_after_recovery()
                        return True
                
                await self._create_failure_notification()
                return False
            else:
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
            
            async with async_timeout.timeout(3):
                async with session.get(f"{base_url}/sec/?restart=1") as resp:
                    if resp.status == 200:
                        return True
                    return False
        except asyncio.TimeoutError:
            return True
        except Exception:
            return False
    
    async def _force_data_update_after_recovery(self):
        """Принудительное обновление данных после восстановления."""
        try:
            _LOGGER.info(f"MegaD-{self.megad.id}: принудительное обновление данных...")
            self.mark_data_received()
            self.mark_feedback_event({"type": "recovery_complete", "is_meaningful": True})
            await self._update_coordinator_state(True)
            
            if hasattr(self.coordinator, 'async_refresh'):
                await self.coordinator.async_refresh()
        except Exception as e:
            _LOGGER.error(f"Ошибка при обновлении: {e}")
    
    async def _get_home_assistant_address(self) -> str:
        """Определяет адрес Home Assistant."""
        try:
            # Внешний URL
            if self.hass.config.external_url:
                from urllib.parse import urlparse
                parsed = urlparse(self.hass.config.external_url)
                return f"{parsed.hostname}:{parsed.port or 8123}"
            
            # Внутренний URL
            if self.hass.config.internal_url:
                from urllib.parse import urlparse
                parsed = urlparse(self.hass.config.internal_url)
                return f"{parsed.hostname}:{parsed.port or 8123}"
            
            # Автоопределение IP
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
            return f"{local_ip}:8123"
        except Exception:
            return ""
    
    # ========== РАБОТА С ЭТАЛОННЫМИ НАСТРОЙКАМИ ==========
    
    async def _init_standard_settings(self):
        """Инициализирует эталонные настройки."""
        standard = await self._get_standard_cf1_settings()
        await self._save_standard_cf1_settings(standard)
    
    async def _get_correct_cf1_settings(self) -> dict:
        """Формирует правильные настройки CF1."""
        settings = DEFAULT_CF1_SETTINGS.copy()
        settings['eip'] = str(self.megad.config.plc.ip_megad)
        
        ha_address = await self._get_home_assistant_address()
        settings['sip'] = ha_address if ha_address else "0.0.0.0:8123"
        
        return settings
    
    async def _read_current_cf1_settings(self) -> dict:
        """Читает текущие настройки с контроллера."""
        try:
            session = async_get_clientsession(self.hass)
            base_url = self.megad.url.rstrip('/')
            
            async with async_timeout.timeout(10):
                async with session.get(f"{base_url}/sec/?cf=1") as response:
                    if response.status != 200:
                        return {}
                    html = await response.text()
            
            settings = {}
            
            for param in ['eip', 'emsk', 'gw', 'sip', 'sct', 'pr', 'lp']:
                match = re.search(rf'name={param} value="([^"]+)"', html)
                if match:
                    settings[param] = match.group(1)
            
            match = re.search(r'name=pwd maxlength=3 value="([^"]+)"', html)
            if match:
                settings['pwd'] = match.group(1)
            
            match = re.search(r'name=srvt.*?value="?(\d+)"?\s*(?:selected)?', html)
            if match:
                settings['srvt'] = match.group(1)
            
            match = re.search(r'name=gsm.*?value="?(\d+)"?\s*(?:selected)?', html)
            if match:
                settings['gsm'] = match.group(1)
            
            settings['gsmf'] = '1' if 'name=gsmf value=1 checked' in html else '0'
            
            return settings
        except Exception:
            return {}
    
    async def _save_standard_cf1_settings(self, settings: dict):
        """Сохраняет эталонные настройки."""
        try:
            if DOMAIN not in self.hass.data:
                self.hass.data[DOMAIN] = {}
            self.hass.data[DOMAIN]['standard_cf1_settings'] = settings
            _LOGGER.info(f"MegaD-{self.megad.id}: эталонные настройки сохранены")
        except Exception as e:
            _LOGGER.error(f"Ошибка сохранения: {e}")
    
    async def _get_standard_cf1_settings(self) -> dict:
        """Возвращает эталонные настройки."""
        try:
            stored = self.hass.data.get(DOMAIN, {}).get('standard_cf1_settings')
            if stored:
                return stored
            
            correct = await self._get_correct_cf1_settings()
            await self._save_standard_cf1_settings(correct)
            return correct
        except Exception:
            return await self._get_correct_cf1_settings()
    
    async def _check_and_fix_cf1(self) -> bool:
        """Проверяет настройки и исправляет при необходимости."""
        current = await self._read_current_cf1_settings()
        if not current:
            return False
        
        ha_address = await self._get_home_assistant_address()
        
        # Проверяем только критичные параметры
        if ha_address and current.get('sip', '') != ha_address:
            _LOGGER.info(f"MegaD-{self.megad.id}: адрес сервера отличается, исправляем...")
            return await self._send_correct_cf1_with_save()
        
        if current.get('srvt', '1') != '0':
            _LOGGER.info(f"MegaD-{self.megad.id}: тип сервера не HTTP, исправляем...")
            return await self._send_correct_cf1_with_save()
        
        return True
    
    # ========== ПУБЛИЧНЫЕ МЕТОДЫ ДЛЯ СЕРВИСОВ ==========
    
    async def force_check_and_update(self):
        """Принудительная проверка."""
        return await self._send_correct_cf1_with_save()
    
    def get_status(self) -> dict:
        return {
            "is_running": self._is_running,
            "megad_id": self.megad.id,
            "feedback_enabled": self._feedback_enabled,
            "meaningful_inactivity_seconds": self._get_meaningful_inactivity_seconds(),
            "meaningful_event_counter": self._meaningful_event_counter,
        }
    
    def get_inactivity_seconds(self) -> int:
        return self._get_inactivity_seconds()
    
    def get_meaningful_inactivity_seconds(self) -> int:
        return self._get_meaningful_inactivity_seconds()
    
    async def get_activity_status(self) -> dict:
        status = self.get_status()
        status["is_healthy"] = await self._check_megad_health_basic()
        return status
    
    async def restart_feedback_service(self) -> bool:
        """Ручной перезапуск - отправляет Save с правильными настройками."""
        return await self._send_correct_cf1_with_save()
    
    async def enable_feedback_service(self) -> bool:
        """Ручное включение - отправляет Save с правильными настройками."""
        return await self._send_correct_cf1_with_save()
