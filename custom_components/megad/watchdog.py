import asyncio
import logging
import platform
import socket
import re
from datetime import datetime
from typing import Optional, Callable, Any, List

from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import async_timeout
from .const import (
    WATCHDOG_MAX_FAILURES, 
    WATCHDOG_INACTIVITY_TIMEOUT,
    WATCHDOG_CHECK_INTERVAL,
    WATCHDOG_FEEDBACK_TIMEOUT,
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
        self._last_restore_time = None
        self._restore_cooldown = 300  # 5 минут между попытками восстановления
        
        # Обратная связь (только события от контроллера!)
        self._feedback_enabled = True
        self._feedback_port = 8123
        self._feedback_listeners: List[Callable] = []
        self._feedback_last_event = None
        self._last_meaningful_feedback = None
        self._feedback_timeout = WATCHDOG_FEEDBACK_TIMEOUT
        self._feedback_check_attempts = 0
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
        self._last_restore_time = None
        
        self._feedback_last_event = datetime.now()
        self._last_meaningful_feedback = datetime.now()
        self._feedback_check_attempts = 0
        self._meaningful_event_counter = 0
        self._non_meaningful_event_counter = 0
        
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        _LOGGER.info(f"Watchdog для MegaD-{self.megad.id} запущен")
        
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
        """Вызывается при любом получении данных от контроллера."""
        self._last_incoming_data = datetime.now()
        self._failure_count = 0
    
    def mark_feedback_event(self, event_data: Any = None):
        """Вызывается только при получении событий от контроллера (обратная связь)."""
        is_real_feedback = False
        
        if isinstance(event_data, dict):
            if event_data.get('type') == 'http_callback' and event_data.get('port_id') is not None:
                is_real_feedback = True
            elif event_data.get('type') == 'port_updated' and event_data.get('success'):
                is_real_feedback = True
            elif event_data.get('type') == 'restore_after_reboot':
                is_real_feedback = True
            elif event_data.get('is_feedback'):
                is_real_feedback = True
        
        self.mark_data_received()
        
        if is_real_feedback:
            self._feedback_last_event = datetime.now()
            self._last_meaningful_feedback = datetime.now()
            self._feedback_check_attempts = 0
            self._meaningful_event_counter += 1
            _LOGGER.info(f"MegaD-{self.megad.id}: ✅ обратная связь от контроллера! (#{self._meaningful_event_counter})")
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
                
                # Проверка доступности контроллера
                is_healthy = await self._check_megad_health_basic()
                
                if not is_healthy:
                    self._failure_count = min(self._failure_count + 1, self._max_failures)
                    self._was_offline = True
                    await self._update_coordinator_state(False)
                    
                    if self._failure_count >= self._max_failures:
                        await self._recover_megad()
                    continue
                
                # Контроллер доступен
                await self._update_coordinator_state(True)
                self._failure_count = 0
                
                # Проверяем время без реальной обратной связи
                feedback_inactivity = self._get_feedback_inactivity_seconds()
                
                # Если долго нет обратной связи - пробуем восстановить
                if feedback_inactivity > self._feedback_timeout:
                    self._feedback_check_attempts += 1
                    _LOGGER.warning(f"MegaD-{self.megad.id}: нет обратной связи {feedback_inactivity} сек (шаг {self._feedback_check_attempts}/3)")
                    
                    if self._feedback_check_attempts >= 3:
                        await self._restore_feedback()
                else:
                    if self._feedback_check_attempts > 0:
                        _LOGGER.info(f"MegaD-{self.megad.id}: обратная связь восстановлена!")
                        self._feedback_check_attempts = 0
                            
            except asyncio.CancelledError:
                break
            except Exception as e:
                _LOGGER.error(f"Ошибка в цикле watchdog: {e}")
                await asyncio.sleep(30)
    
    # ========== МЕТОДЫ ДЛЯ РАБОТЫ С СЕНСОРАМИ ==========
    
    def _get_inactivity_seconds(self) -> int:
        """Возвращает количество секунд без полученных данных."""
        if not self._last_incoming_data:
            return 0
        return int((datetime.now() - self._last_incoming_data).total_seconds())
    
    def _get_feedback_inactivity_seconds(self) -> int:
        """Возвращает количество секунд без обратной связи."""
        if not self._last_meaningful_feedback:
            return 999999
        return int((datetime.now() - self._last_meaningful_feedback).total_seconds())
    
    def _get_meaningful_inactivity_seconds(self) -> int:
        """Возвращает количество секунд без значимых событий."""
        if not self._last_meaningful_feedback:
            return 999999
        return int((datetime.now() - self._last_meaningful_feedback).total_seconds())
    
    # ========== ОСНОВНЫЕ МЕТОДЫ ВОССТАНОВЛЕНИЯ ==========
    
    async def _restore_feedback(self) -> bool:
        """Одна попытка восстановления обратной связи."""
        
        # Проверяем кулдаун (не чаще 1 раза в 5 минут)
        if self._last_restore_time:
            seconds_since_last = (datetime.now() - self._last_restore_time).total_seconds()
            if seconds_since_last < self._restore_cooldown:
                _LOGGER.debug(f"MegaD-{self.megad.id}: восстановление в кулдауне ({int(seconds_since_last)}/{self._restore_cooldown} сек)")
                return False
        
        _LOGGER.warning(f"MegaD-{self.megad.id}: === ВОССТАНОВЛЕНИЕ ОБРАТНОЙ СВЯЗИ ===")
        
        # Отправляем правильные настройки на контроллер
        success = await self._send_restore_request()
        
        if success:
            self._last_restore_time = datetime.now()
            _LOGGER.warning(f"MegaD-{self.megad.id}: ✅ запрос отправлен, контроллер перезагружается")
            
            # Ждем перезагрузки контроллера
            await asyncio.sleep(15)
            
            # Проверяем, появилась ли обратная связь
            if self._get_feedback_inactivity_seconds() < 60:
                _LOGGER.info(f"MegaD-{self.megad.id}: ✅ ОБРАТНАЯ СВЯЗЬ ВОССТАНОВЛЕНА!")
                self._feedback_check_attempts = 0
                return True
            else:
                _LOGGER.error(f"MegaD-{self.megad.id}: ❌ восстановление не помогло")
                await self._create_manual_intervention_notification()
                return False
        
        return False
    
    async def _send_restore_request(self) -> bool:
        """Отправляет правильные настройки на контроллер."""
        try:
            session = async_get_clientsession(self.hass)
            
            # ========== 1. IP КОНТРОЛЛЕРА из настроек интеграции ==========
            controller_ip = str(self.megad.config.plc.ip_megad)
            
            # ========== 2. ПАРОЛЬ из настроек интеграции ==========
            password = getattr(self.megad.config.plc, 'password', 'sec')
            
            # ========== 3. АДРЕС HA (автоопределение) ==========
            ha_ip = await self._get_home_assistant_ip()
            if not ha_ip:
                _LOGGER.error(f"MegaD-{self.megad.id}: не удалось определить IP адрес HA!")
                await self._create_ip_detection_notification()
                return False
            
            ha_port = 8123
            server_address = f"{ha_ip}:{ha_port}"
            encoded_server = server_address.replace(':', '%3A')
            
            # ========== 4. ОСТАЛЬНЫЕ НАСТРОЙКИ ==========
            standard = DEFAULT_CF1_SETTINGS.copy()
            
            # Формируем базовый URL
            base_url = self.megad.url.rstrip('/')
            
            # Формируем полный URL для восстановления
            update_url = (
                f"{base_url}/sec/?cf=1"
                f"&eip={controller_ip}"
                f"&emsk={standard.get('emsk', '255.255.255.0')}"
                f"&pwd={password}"
                f"&gw={standard.get('gw', '255.255.255.255')}"
                f"&sip={encoded_server}"
                f"&srvt=0"
                f"&sct={standard.get('sct', 'megad')}"
                f"&pr={standard.get('pr', '')}"
                f"&lp={standard.get('lp', '10')}"
                f"&gsm={standard.get('gsm', '0')}"
                f"&gsmf={standard.get('gsmf', '1')}"
                f"&save=1"
            )
            
            _LOGGER.warning(f"MegaD-{self.megad.id}: === ОТПРАВКА ВОССТАНОВИТЕЛЬНОГО ЗАПРОСА ===")
            _LOGGER.info(f"  IP контроллера: {controller_ip}")
            _LOGGER.info(f"  Пароль: {password}")
            _LOGGER.info(f"  Адрес сервера HA: {server_address}")
            _LOGGER.debug(f"  Полный URL: {update_url}")
            
            async with async_timeout.timeout(5):
                async with session.get(update_url) as resp:
                    if resp.status == 200:
                        return True
                    else:
                        _LOGGER.warning(f"Ошибка HTTP: {resp.status}")
                        return False
                    
        except asyncio.TimeoutError:
            # Таймаут - контроллер начал перезагрузку
            return True
        except Exception as e:
            _LOGGER.error(f"Ошибка отправки запроса: {e}")
            return False
    
    async def _recover_megad(self):
        """Полное восстановление контроллера."""
        if self._recovering:
            return
            
        self._recovering = True
        _LOGGER.warning(f"=== ЗАПУСК ПОЛНОГО ВОССТАНОВЛЕНИЯ ДЛЯ MegaD-{self.megad.id} ===")
        
        try:
            # Отправляем восстановительный запрос
            await self._restore_feedback()
            
            # Если не помогло, пробуем просто перезагрузить
            if self._get_feedback_inactivity_seconds() > 300:
                _LOGGER.warning(f"MegaD-{self.megad.id}: перезагрузка контроллера...")
                await self._send_reboot_command()
                await asyncio.sleep(10)
                
                # Еще раз отправляем настройки
                await self._send_restore_request()
                
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
                    return resp.status == 200
        except asyncio.TimeoutError:
            return True
        except Exception:
            return False
    
    # ========== ВСПОМОГАТЕЛЬНЫЕ МЕТОДЫ ==========
    
    async def _get_home_assistant_ip(self) -> str:
        """Определяет реальный IP адрес Home Assistant."""
        
        # Способ 1: Через network интеграцию
        try:
            from homeassistant.components import network
            adapters = network.async_get_adapters(self.hass)
            for adapter in adapters:
                for ipv4 in adapter.get('ipv4', []):
                    if (not ipv4.startswith('127.') and 
                        not ipv4.startswith('0.') and
                        not ipv4.startswith('255.')):
                        _LOGGER.info(f"MegaD-{self.megad.id}: IP через network: {ipv4}")
                        return ipv4
        except Exception as e:
            _LOGGER.debug(f"network: {e}")
        
        # Способ 2: Через socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
            if local_ip and not local_ip.startswith('127.'):
                _LOGGER.info(f"MegaD-{self.megad.id}: IP через socket: {local_ip}")
                return local_ip
        except Exception as e:
            _LOGGER.debug(f"socket: {e}")
        
        # Способ 3: Из external_url
        try:
            if self.hass.config.external_url:
                from urllib.parse import urlparse
                parsed = urlparse(self.hass.config.external_url)
                host = parsed.hostname
                if host and not host.startswith('127.') and host not in ['localhost', '0.0.0.0']:
                    _LOGGER.info(f"MegaD-{self.megad.id}: IP из external_url: {host}")
                    return host
        except Exception as e:
            _LOGGER.debug(f"external_url: {e}")
        
        _LOGGER.error(f"MegaD-{self.megad.id}: не удалось определить IP адрес HA!")
        return ""
    
    async def _check_megad_health_basic(self) -> bool:
        """Базовая проверка доступности контроллера."""
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
    
    async def _ping_megad(self) -> bool:
        """Проверка доступности через ping."""
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
        """Обновление состояния координатора."""
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
    
    # ========== УВЕДОМЛЕНИЯ ==========
    
    async def _create_manual_intervention_notification(self):
        """Уведомление о необходимости ручного вмешательства."""
        try:
            from homeassistant.components import persistent_notification
            
            ha_ip = await self._get_home_assistant_ip()
            
            message = (
                f"MegaD-{self.megad.id}: ❌ АВТОМАТИЧЕСКОЕ ВОССТАНОВЛЕНИЕ НЕ УДАЛОСЬ!\n\n"
                f"Контроллер доступен, но не отправляет обратную связь.\n\n"
                f"**Что нужно сделать:**\n\n"
                f"1. Откройте веб-интерфейс MegaD:\n"
                f"   `http://{self.megad.config.plc.ip_megad}/sec/`\n\n"
                f"2. Нажмите **Config** → **CF1**\n\n"
                f"3. Установите параметры:\n"
                f"   - **SRV Type** = `HTTP`\n"
                f"   - **SRV** = `{ha_ip if ha_ip else 'IP_вашего_HA'}:8123`\n\n"
                f"4. Нажмите **Save**\n\n"
                f"После этого контроллер перезагрузится и обратная связь заработает."
            )
            
            persistent_notification.async_create(
                self.hass,
                message,
                title=f"🚨 MegaD-{self.megad.id}: требуется ручная настройка!",
                notification_id=f"megad_feedback_failure_{self.megad.id}"
            )
        except Exception as e:
            _LOGGER.error(f"Ошибка создания уведомления: {e}")
    
    async def _create_ip_detection_notification(self):
        """Уведомление о проблеме определения IP."""
        try:
            from homeassistant.components import persistent_notification
            
            message = (
                f"MegaD-{self.megad.id}: ⚠️ НЕ УДАЛОСЬ ОПРЕДЕЛИТЬ IP АДРЕС HOME ASSISTANT!\n\n"
                f"Автоматическое восстановление обратной связи невозможно.\n\n"
                f"**Что нужно сделать:**\n\n"
                f"1. Откройте веб-интерфейс MegaD:\n"
                f"   `http://{self.megad.config.plc.ip_megad}/sec/`\n\n"
                f"2. Нажмите **Config** → **CF1**\n\n"
                f"3. Установите параметры:\n"
                f"   - **SRV Type** = `HTTP`\n"
                f"   - **SRV** = `IP_вашего_HA:8123` (введите вручную)\n\n"
                f"4. Нажмите **Save**\n\n"
                f"После этого обратная связь заработает."
            )
            
            persistent_notification.async_create(
                self.hass,
                message,
                title=f"⚠️ MegaD-{self.megad.id}: требуется ручная настройка!",
                notification_id=f"megad_ip_failure_{self.megad.id}"
            )
        except Exception as e:
            _LOGGER.error(f"Ошибка создания уведомления: {e}")
    
    # ========== РАБОТА С ЭТАЛОННЫМИ НАСТРОЙКАМИ ==========
    
    async def _init_standard_settings(self):
        """Инициализирует эталонные настройки."""
        settings = DEFAULT_CF1_SETTINGS.copy()
        settings['eip'] = str(self.megad.config.plc.ip_megad)
        await self._save_standard_cf1_settings(settings)
    
    async def _save_standard_cf1_settings(self, settings: dict):
        """Сохраняет эталонные настройки."""
        try:
            if DOMAIN not in self.hass.data:
                self.hass.data[DOMAIN] = {}
            self.hass.data[DOMAIN]['standard_cf1_settings'] = settings
        except Exception as e:
            _LOGGER.debug(f"Ошибка сохранения настроек: {e}")
    
    # ========== ПУБЛИЧНЫЕ МЕТОДЫ ДЛЯ СЕНСОРОВ ==========
    
    def get_status(self) -> dict:
        """Возвращает статус watchdog."""
        return {
            "is_running": self._is_running,
            "megad_id": self.megad.id,
            "feedback_enabled": self._feedback_enabled,
            "feedback_inactivity_seconds": self._get_feedback_inactivity_seconds(),
            "meaningful_event_counter": self._meaningful_event_counter,
            "non_meaningful_event_counter": self._non_meaningful_event_counter,
        }
    
    def get_inactivity_seconds(self) -> int:
        """Возвращает время без данных в секундах."""
        return self._get_inactivity_seconds()
    
    def get_feedback_inactivity_seconds(self) -> int:
        """Возвращает время без обратной связи в секундах."""
        return self._get_feedback_inactivity_seconds()
    
    def get_meaningful_inactivity_seconds(self) -> int:
        """Возвращает время без значимых событий в секундах."""
        return self._get_meaningful_inactivity_seconds()
    
    def get_feedback_status(self) -> str:
        """Возвращает статус обратной связи."""
        meaningful_inactivity = self._get_meaningful_inactivity_seconds()
        
        if not self._feedback_enabled:
            return "inactive"
        elif meaningful_inactivity < 60:
            return "ok"
        elif meaningful_inactivity < 300:
            return "waiting"
        else:
            return "failed"
    
    def get_feedback_details(self) -> dict:
        """Возвращает детали обратной связи."""
        return {
            "feedback_enabled": self._feedback_enabled,
            "feedback_inactivity_seconds": self._get_feedback_inactivity_seconds(),
            "meaningful_inactivity_seconds": self._get_meaningful_inactivity_seconds(),
            "meaningful_event_counter": self._meaningful_event_counter,
            "non_meaningful_event_counter": self._non_meaningful_event_counter,
        }
    
    async def force_check_and_update(self):
        """Принудительная проверка."""
        return await self._send_restore_request()
    
    async def get_activity_status(self) -> dict:
        """Возвращает статус активности."""
        status = self.get_status()
        status["is_healthy"] = await self._check_megad_health_basic()
        status["feedback_status"] = self.get_feedback_status()
        return status
