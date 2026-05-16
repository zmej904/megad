import asyncio
import logging
import platform
import socket
import re
from datetime import datetime
from typing import Optional, Callable, Any, List

import async_timeout

from homeassistant.helpers.aiohttp_client import async_get_clientsession
from .const import (
    WATCHDOG_MAX_FAILURES,
    WATCHDOG_INACTIVITY_TIMEOUT,
    WATCHDOG_CHECK_INTERVAL,
    WATCHDOG_FEEDBACK_TIMEOUT,
    DOMAIN,
    DEFAULT_CF1_SETTINGS,
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
        self._last_incoming_data = None  # любые данные (включая команды HA)
        self._health_check_interval = WATCHDOG_CHECK_INTERVAL
        self._was_offline = False
        self._inactivity_timeout = WATCHDOG_INACTIVITY_TIMEOUT
        self._last_reboot_attempt = None
        self._last_restore_time = None
        self._restore_cooldown = 300          # 5 минут между попытками
        self._restore_verification_task = None
        self._restore_wait_seconds = 330      # 5 минут 30 секунд (чуть дольше интервала порта 9)

        # Обратная связь (только события от контроллера)
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
        self._is_running = False
        if self._restore_verification_task:
            self._restore_verification_task.cancel()
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
        """Вызывается ТОЛЬКО при получении реальных событий от контроллера."""
        is_real_feedback = False
        if isinstance(event_data, dict):
            if event_data.get("type") == "http_callback" and event_data.get("port_id") is not None:
                is_real_feedback = True
            elif event_data.get("type") == "port_updated" and event_data.get("success"):
                is_real_feedback = True
            elif event_data.get("type") == "restore_after_reboot":
                is_real_feedback = True
            elif event_data.get("is_feedback"):
                is_real_feedback = True

        self.mark_data_received()

        if is_real_feedback:
            self._feedback_last_event = datetime.now()
            self._last_meaningful_feedback = datetime.now()
            self._feedback_check_attempts = 0
            self._meaningful_event_counter += 1

            # Если мы ожидаем подтверждения восстановления, отменяем таймер
            if self._restore_verification_task and not self._restore_verification_task.done():
                self._restore_verification_task.cancel()
                _LOGGER.info(f"MegaD-{self.megad.id}: ✅ обратная связь подтверждена через {(datetime.now() - self._last_restore_time).total_seconds():.0f} сек")
            _LOGGER.info(f"MegaD-{self.megad.id}: ✅ обратная связь от контроллера! (#{self._meaningful_event_counter})")
        else:
            self._non_meaningful_event_counter += 1

    async def _watchdog_loop(self):
        _LOGGER.debug(f"Watchdog запущен с интервалом {self._health_check_interval} сек")
        while self._is_running:
            try:
                await asyncio.sleep(self._health_check_interval)

                if self._recovering or self.megad.is_flashing:
                    continue

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

                feedback_inactivity = self._get_feedback_inactivity_seconds()
                if feedback_inactivity > self._feedback_timeout:
                    self._feedback_check_attempts += 1
                    _LOGGER.warning(
                        f"MegaD-{self.megad.id}: нет обратной связи {feedback_inactivity} сек "
                        f"(шаг {self._feedback_check_attempts}/3)"
                    )
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

    # ---------- Вспомогательные методы ----------
    def _get_inactivity_seconds(self) -> int:
        if not self._last_incoming_data:
            return 0
        return int((datetime.now() - self._last_incoming_data).total_seconds())

    def _get_feedback_inactivity_seconds(self) -> int:
        if not self._last_meaningful_feedback:
            return 999999
        return int((datetime.now() - self._last_meaningful_feedback).total_seconds())

    def _get_meaningful_inactivity_seconds(self) -> int:
        if not self._last_meaningful_feedback:
            return 999999
        return int((datetime.now() - self._last_meaningful_feedback).total_seconds())

    async def _check_megad_health_basic(self) -> bool:
        if not await self._ping_megad():
            return False
        try:
            session = async_get_clientsession(self.hass)
            base_url = self.megad.url.rstrip('/')
            async with async_timeout.timeout(3):
                async with session.get(f"{base_url}/sec/?cmd=id") as resp:
                    if resp.status == 200:
                        text = await resp.text()
                        return bool(text and text.strip() and "timeout" not in text.lower())
            return False
        except Exception:
            return False

    async def _ping_megad(self) -> bool:
        try:
            ip = str(self.megad.config.plc.ip_megad)
            param = "-n" if platform.system().lower() == "windows" else "-c"
            proc = await asyncio.create_subprocess_exec(
                "ping", param, "1", "-W", "2", ip,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            return proc.returncode == 0
        except Exception:
            return False

    async def _update_coordinator_state(self, is_available: bool):
        try:
            self.megad.is_available = is_available
            if hasattr(self.coordinator, "available"):
                self.coordinator.available = is_available
            if hasattr(self.coordinator, "async_update_listeners"):
                try:
                    self.hass.loop.call_soon_threadsafe(self.coordinator.async_update_listeners)
                except Exception:
                    pass
        except Exception as e:
            _LOGGER.debug(f"Ошибка обновления состояния: {e}")

    # ---------- Восстановление обратной связи ----------
    async def _restore_feedback(self) -> bool:
        """Одна попытка восстановления обратной связи (отправка Save)."""
        if self._last_restore_time:
            if (datetime.now() - self._last_restore_time).total_seconds() < self._restore_cooldown:
                return False

        _LOGGER.warning(f"MegaD-{self.megad.id}: === ВОССТАНОВЛЕНИЕ ОБРАТНОЙ СВЯЗИ ===")

        success = await self._send_restore_request()
        if not success:
            await self._create_manual_intervention_notification()
            return False

        self._last_restore_time = datetime.now()
        # Сбрасываем таймер обратной связи, чтобы статус временно стал "ok"/"waiting"
        self._last_meaningful_feedback = datetime.now()
        self._feedback_check_attempts = 0

        # Запускаем фоновую проверку: если за _restore_wait_seconds не придёт ни одного события – тревога
        if self._restore_verification_task:
            self._restore_verification_task.cancel()
        self._restore_verification_task = asyncio.create_task(self._wait_for_feedback_after_restore())
        _LOGGER.warning(f"MegaD-{self.megad.id}: ✅ запрос отправлен, контроллер перезагружается. Ожидаем событий в течение {self._restore_wait_seconds} сек.")
        return True

    async def _wait_for_feedback_after_restore(self):
        """Ожидает до _restore_wait_seconds реального события обратной связи."""
        try:
            await asyncio.sleep(self._restore_wait_seconds)
        except asyncio.CancelledError:
            # Задача отменена – значит событие пришло, всё хорошо
            return

        # Если мы дошли сюда, то за отведённое время ни одного события не было
        _LOGGER.error(f"MegaD-{self.megad.id}: через {self._restore_wait_seconds} сек событие от контроллера так и не пришло. Восстановление не удалось.")
        await self._create_manual_intervention_notification()

    async def _send_restore_request(self) -> bool:
        """Формирует и отправляет правильный GET-запрос с параметрами CF1 и save=1."""
        try:
            session = async_get_clientsession(self.hass)
            controller_ip = str(self.megad.config.plc.ip_megad)
            password = getattr(self.megad.config.plc, "password", "sec")
            ha_ip = await self._get_home_assistant_ip()
            if not ha_ip:
                _LOGGER.error(f"MegaD-{self.megad.id}: не удалось определить IP Home Assistant")
                await self._create_ip_detection_notification()
                return False

            server_address = f"{ha_ip}:8123"
            encoded_server = server_address.replace(":", "%3A")
            standard = DEFAULT_CF1_SETTINGS.copy()
            base_url = self.megad.url.rstrip("/")

            # Формируем URL
            url = (
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

            _LOGGER.info(f"MegaD-{self.megad.id}: отправка восстановительного запроса")
            _LOGGER.info(f"  IP контроллера: {controller_ip}")
            _LOGGER.info(f"  Адрес сервера HA: {server_address}")

            async with async_timeout.timeout(5):
                async with session.get(url) as resp:
                    if resp.status == 200:
                        return True
                    _LOGGER.warning(f"HTTP статус {resp.status}")
                    return False

        except asyncio.TimeoutError:
            # Таймаут – контроллер начал перезагрузку, считаем успехом
            return True
        except Exception as e:
            _LOGGER.error(f"Ошибка отправки запроса: {e}")
            return False

    async def _recover_megad(self):
        """Полная процедура восстановления (вызывается при потере доступности)."""
        if self._recovering:
            return
        self._recovering = True
        _LOGGER.warning(f"=== ПОЛНОЕ ВОССТАНОВЛЕНИЕ ДЛЯ MegaD-{self.megad.id} ===")
        try:
            await self._restore_feedback()
            if self._get_feedback_inactivity_seconds() > 300:
                _LOGGER.warning(f"MegaD-{self.megad.id}: перезагрузка контроллера...")
                await self._send_reboot_command()
                await asyncio.sleep(10)
                await self._send_restore_request()
        finally:
            self._recovering = False
            self._failure_count = 0

    async def _send_reboot_command(self) -> bool:
        try:
            session = async_get_clientsession(self.hass)
            base_url = self.megad.url.rstrip("/")
            async with async_timeout.timeout(3):
                async with session.get(f"{base_url}/sec/?restart=1") as resp:
                    return resp.status == 200
        except asyncio.TimeoutError:
            return True
        except Exception:
            return False

    # ---------- Определение IP Home Assistant ----------
    async def _get_home_assistant_ip(self) -> str:
        """Автоматически определяет реальный IP Home Assistant."""
        try:
            from homeassistant.components import network
            adapters = network.async_get_adapters(self.hass)
            for adapter in adapters:
                for ipv4 in adapter.get("ipv4", []):
                    if not (ipv4.startswith("127.") or ipv4.startswith("0.") or ipv4.startswith("255.")):
                        return ipv4
        except Exception:
            pass

        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            if ip and not ip.startswith("127."):
                return ip
        except Exception:
            pass

        try:
            if self.hass.config.external_url:
                from urllib.parse import urlparse
                parsed = urlparse(self.hass.config.external_url)
                host = parsed.hostname
                if host and not host.startswith("127.") and host not in ("localhost", "0.0.0.0"):
                    return host
        except Exception:
            pass

        _LOGGER.error(f"MegaD-{self.megad.id}: не удалось определить IP HA")
        return ""

    # ---------- Уведомления ----------
    async def _create_manual_intervention_notification(self):
        try:
            from homeassistant.components import persistent_notification
            ha_ip = await self._get_home_assistant_ip()
            message = (
                f"MegaD-{self.megad.id}: ❌ АВТОМАТИЧЕСКОЕ ВОССТАНОВЛЕНИЕ НЕ УДАЛОСЬ!\n\n"
                f"Контроллер доступен, но не отправляет обратную связь.\n\n"
                f"**Что нужно сделать:**\n"
                f"1. Откройте веб-интерфейс MegaD: http://{self.megad.config.plc.ip_megad}/sec/\n"
                f"2. Нажмите **Config** → **CF1**\n"
                f"3. Установите:\n"
                f"   - **SRV Type** = `HTTP`\n"
                f"   - **SRV** = `{ha_ip if ha_ip else 'IP_вашего_HA'}:8123`\n"
                f"4. Нажмите **Save**\n\n"
                f"После этого контроллер перезагрузится и обратная связь заработает."
            )
            persistent_notification.async_create(
                self.hass,
                message,
                title=f"🚨 MegaD-{self.megad.id}: требуется ручная настройка!",
                notification_id=f"megad_feedback_failure_{self.megad.id}",
            )
        except Exception as e:
            _LOGGER.error(f"Ошибка создания уведомления: {e}")

    async def _create_ip_detection_notification(self):
        try:
            from homeassistant.components import persistent_notification
            message = (
                f"MegaD-{self.megad.id}: ⚠️ НЕ УДАЛОСЬ ОПРЕДЕЛИТЬ IP HOME ASSISTANT!\n\n"
                f"Автоматическое восстановление обратной связи невозможно.\n\n"
                f"Настройте SRV вручную на странице CF1:\n"
                f"- SRV Type = HTTP\n"
                f"- SRV = IP_вашего_HA:8123\n"
                f"и нажмите Save."
            )
            persistent_notification.async_create(
                self.hass,
                message,
                title=f"⚠️ MegaD-{self.megad.id}: требуется ручная настройка!",
                notification_id=f"megad_ip_failure_{self.megad.id}",
            )
        except Exception:
            pass

    # ---------- Эталонные настройки ----------
    async def _init_standard_settings(self):
        settings = DEFAULT_CF1_SETTINGS.copy()
        settings["eip"] = str(self.megad.config.plc.ip_megad)
        await self._save_standard_cf1_settings(settings)

    async def _save_standard_cf1_settings(self, settings: dict):
        try:
            if DOMAIN not in self.hass.data:
                self.hass.data[DOMAIN] = {}
            self.hass.data[DOMAIN]["standard_cf1_settings"] = settings
        except Exception:
            pass

    # ---------- Публичные методы для сенсоров ----------
    def get_status(self) -> dict:
        return {
            "is_running": self._is_running,
            "megad_id": self.megad.id,
            "feedback_enabled": self._feedback_enabled,
            "feedback_inactivity_seconds": self._get_feedback_inactivity_seconds(),
            "meaningful_event_counter": self._meaningful_event_counter,
            "non_meaningful_event_counter": self._non_meaningful_event_counter,
        }

    def get_inactivity_seconds(self) -> int:
        return self._get_inactivity_seconds()

    def get_feedback_inactivity_seconds(self) -> int:
        return self._get_feedback_inactivity_seconds()

    def get_meaningful_inactivity_seconds(self) -> int:
        return self._get_meaningful_inactivity_seconds()

    def get_feedback_status(self) -> str:
        meaningful = self._get_meaningful_inactivity_seconds()
        if not self._feedback_enabled:
            return "inactive"
        if meaningful < 60:
            return "ok"
        if meaningful < 300:
            return "waiting"
        return "failed"

    def get_feedback_details(self) -> dict:
        return {
            "feedback_enabled": self._feedback_enabled,
            "feedback_inactivity_seconds": self._get_feedback_inactivity_seconds(),
            "meaningful_inactivity_seconds": self._get_meaningful_inactivity_seconds(),
            "meaningful_event_counter": self._meaningful_event_counter,
            "non_meaningful_event_counter": self._non_meaningful_event_counter,
        }

    async def force_check_and_update(self):
        return await self._send_restore_request()

    async def get_activity_status(self) -> dict:
        status = self.get_status()
        status["is_healthy"] = await self._check_megad_health_basic()
        status["feedback_status"] = self.get_feedback_status()
        return status
