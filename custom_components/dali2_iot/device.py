"""Device class for DALI2 IoT integration."""
from __future__ import annotations

import aiohttp
import asyncio
import async_timeout
import logging
import json
from typing import Any, Final, Callable
from collections.abc import Awaitable

from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)
_WS_LOGGER = logging.getLogger(f"{__name__}.websocket")

API_TIMEOUT: Final = 10.0
BASE_URL: Final = "http://{host}"
WS_URL: Final = "ws://{host}"
WS_RECONNECT_DELAY: Final = 5.0
WS_PING_INTERVAL: Final = 30.0

class Dali2IotConnectionError(Exception):
    """Error to indicate we cannot connect to the device."""

class Dali2IotDevice:
    """Class to handle a DALI2 IoT device."""

    def __init__(
        self,
        host: str,
        name: str,
        session: aiohttp.ClientSession,
    ) -> None:
        """Initialize the device."""
        self._host = host
        self._name = name
        self._session = session
        self._base_url = BASE_URL.format(host=host)
        self._ws_url = WS_URL.format(host=host)
        self._device_info: DeviceInfo | None = None
        self._devices: list[dict[str, Any]] = []

        # WebSocket state
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ws_connected: bool = False
        self._ws_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._should_reconnect: bool = True

        # Event callbacks
        self._event_callbacks: dict[str, list[Callable[[dict[str, Any]], Awaitable[None]]]] = {}

        # Command response handling
        self._pending_commands: dict[str, asyncio.Future] = {}
        self._command_counter: int = 0

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        if not self._device_info:
            self._device_info = DeviceInfo(
                identifiers={("dali2_iot", self._host)},
                name=self._name,
                manufacturer="Lunatone",
                model="DALI2 IoT",
            )
        return self._device_info

    @property
    def is_connected(self) -> bool:
        """Return if WebSocket is connected."""
        return self._ws_connected

    def register_event_callback(
        self, event_type: str, callback: Callable[[dict[str, Any]], Awaitable[None]]
    ) -> None:
        """Register a callback for a specific event type."""
        if event_type not in self._event_callbacks:
            self._event_callbacks[event_type] = []
        self._event_callbacks[event_type].append(callback)

    def unregister_event_callback(
        self, event_type: str, callback: Callable[[dict[str, Any]], Awaitable[None]]
    ) -> None:
        """Unregister a callback for a specific event type."""
        if event_type in self._event_callbacks:
            self._event_callbacks[event_type].remove(callback)

    async def async_connect(self) -> None:
        """Connect to the WebSocket."""
        if self._ws_task and not self._ws_task.done():
            _LOGGER.debug("WebSocket connection already in progress")
            return

        self._should_reconnect = True
        self._ws_task = asyncio.create_task(self._ws_connection_handler())

    async def async_disconnect(self) -> None:
        """Disconnect from the WebSocket."""
        self._should_reconnect = False

        if self._reconnect_task:
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass
            self._reconnect_task = None

        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
            self._ws_task = None

        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None
        self._ws_connected = False

    async def _ws_connection_handler(self) -> None:
        """Handle WebSocket connection with automatic reconnection."""
        while self._should_reconnect:
            try:
                _WS_LOGGER.info("Connecting to WebSocket at %s", self._ws_url)
                async with self._session.ws_connect(self._ws_url) as ws:
                    self._ws = ws
                    self._ws_connected = True
                    _WS_LOGGER.info("✅ WebSocket connected to %s", self._host)

                    # Process incoming messages
                    await self._ws_message_handler()

            except aiohttp.ClientError as err:
                _WS_LOGGER.error("❌ WebSocket connection error: %s", err)
            except Exception as err:
                _WS_LOGGER.exception("❌ Unexpected error in WebSocket connection: %s", err)
            finally:
                self._ws_connected = False
                self._ws = None

            # Reconnect after delay if we should reconnect
            if self._should_reconnect:
                _WS_LOGGER.info("🔄 Reconnecting WebSocket in %s seconds", WS_RECONNECT_DELAY)
                await asyncio.sleep(WS_RECONNECT_DELAY)

    async def _ws_message_handler(self) -> None:
        """Handle incoming WebSocket messages."""
        async for msg in self._ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    _WS_LOGGER.debug("📥 Raw WebSocket message: %s", msg.data[:200])  # First 200 chars
                    await self._handle_ws_event(data)
                except json.JSONDecodeError as err:
                    _WS_LOGGER.error("❌ Failed to decode WebSocket message: %s", err)
            elif msg.type == aiohttp.WSMsgType.ERROR:
                _WS_LOGGER.error("❌ WebSocket error: %s", self._ws.exception())
                break
            elif msg.type == aiohttp.WSMsgType.CLOSED:
                _WS_LOGGER.warning("⚠️  WebSocket connection closed")
                break

    async def _handle_ws_event(self, data: dict[str, Any]) -> None:
        """Handle a WebSocket event."""
        event_type = data.get("type")
        event_data = data.get("data", {})
        time_signature = data.get("timeSignature", {})

        # Log the event with details
        _WS_LOGGER.info(
            "📨 Event: %s | Timestamp: %s | Counter: %s",
            event_type,
            time_signature.get("timestamp"),
            time_signature.get("counter")
        )

        # Log event data (truncated for readability)
        if event_type == "devices":
            devices = event_data.get("devices", [])
            _WS_LOGGER.info("   └─ Devices updated: %d device(s)", len(devices))
            for device in devices:
                _WS_LOGGER.debug(
                    "      └─ Device %s (addr=%s): %s",
                    device.get("id"),
                    device.get("address"),
                    {k: v for k, v in device.items() if k in ["name", "features", "groups"]}
                )
        elif event_type == "devicesDeleted":
            deleted = event_data.get("deleted", [])
            _WS_LOGGER.info("   └─ Devices deleted: %s", deleted)
        elif event_type == "zones":
            zones = event_data.get("zones", [])
            _WS_LOGGER.info("   └─ Zones updated: %d zone(s)", len(zones))
        elif event_type == "scanProgress":
            _WS_LOGGER.info(
                "   └─ Scan: %s%% - %s (found: %s)",
                event_data.get("progress", 0),
                event_data.get("status"),
                event_data.get("found", 0)
            )
        elif event_type == "info":
            _WS_LOGGER.info(
                "   └─ Device: %s | Version: %s | Tier: %s",
                event_data.get("name"),
                event_data.get("version"),
                event_data.get("tier")
            )
        elif event_type == "daliStatus":
            status_code = event_data.get("status")
            line = event_data.get("line", 0)
            status_messages = {
                0: "DALI bus power OFF",
                1: "System failure",
                2: "DALI bus power ON",
                3: "Send buffer full",
                4: "Send buffer empty",
                5: "DALI bus power low"
            }
            _WS_LOGGER.info(
                "   └─ 📡 DALI Status (line %s): %s",
                line,
                status_messages.get(status_code, f"Unknown status {status_code}")
            )
        elif event_type == "daliMonitor":
            bits = event_data.get("bits")
            dali_data = event_data.get("data", [])
            line = event_data.get("line", 0)
            _WS_LOGGER.debug(
                "   └─ 📡 DALI Monitor (line %s): %d bits, data=%s",
                line,
                bits,
                dali_data
            )
        else:
            # Log other event types with full data at debug level
            _WS_LOGGER.debug("   └─ Data: %s", event_data)

        # Call registered callbacks for this event type
        if event_type in self._event_callbacks:
            for callback in self._event_callbacks[event_type]:
                try:
                    await callback(event_data)
                except Exception as err:
                    _WS_LOGGER.exception("❌ Error in event callback for %s: %s", event_type, err)

        # Handle special event types
        if event_type == "info":
            await self._handle_info_event(event_data)
        elif event_type == "devices":
            await self._handle_devices_event(event_data)
        elif event_type == "devicesDeleted":
            await self._handle_devices_deleted_event(event_data)
        elif event_type == "daliAnswer":
            await self._handle_dali_answer(data)
        elif event_type == "daliFrame":
            await self._handle_dali_frame(data)

    async def _handle_info_event(self, data: dict[str, Any]) -> None:
        """Handle info event (greeting message)."""
        _WS_LOGGER.info("✅ Received device info greeting")
        # Update device info
        if data:
            self._device_info = DeviceInfo(
                identifiers={("dali2_iot", self._host)},
                name=data.get("name", self._name),
                manufacturer="Lunatone",
                model="DALI2 IoT",
                sw_version=data.get("version"),
            )

    async def _handle_devices_event(self, data: dict[str, Any]) -> None:
        """Handle devices event."""
        devices = data.get("devices", [])

        # Update or add devices
        for new_device in devices:
            device_id = new_device.get("id")
            # Find existing device
            existing_device = next((d for d in self._devices if d.get("id") == device_id), None)

            if existing_device:
                # Update existing device with new data
                _WS_LOGGER.debug("🔄 Updated device %s", device_id)
                existing_device.update(new_device)
            else:
                # Add new device
                _WS_LOGGER.debug("➕ Added new device %s", device_id)
                self._devices.append(new_device)

    async def _handle_devices_deleted_event(self, data: dict[str, Any]) -> None:
        """Handle devices deleted event."""
        deleted_ids = data.get("deleted", [])
        _WS_LOGGER.info("🗑️  Removed %d device(s)", len(deleted_ids))

        # Remove deleted devices
        self._devices = [d for d in self._devices if d.get("id") not in deleted_ids]

    async def _handle_dali_answer(self, data: dict[str, Any]) -> None:
        """Handle DALI answer from the bus."""
        # Match with pending command if any
        # This is for future command/response correlation
        _WS_LOGGER.debug("📡 DALI answer: %s", data)

    async def _handle_dali_frame(self, data: dict[str, Any]) -> None:
        """Handle DALI frame confirmation."""
        # Match with pending command if any
        _WS_LOGGER.debug("📡 DALI frame confirmation: %s", data)

    async def async_send_ws_message(self, message: dict[str, Any]) -> None:
        """Send a message via WebSocket."""
        if not self._ws or not self._ws_connected:
            raise Dali2IotConnectionError("WebSocket not connected")

        try:
            _WS_LOGGER.debug("📤 Sending: %s", message)
            await self._ws.send_json(message)
            _WS_LOGGER.debug("✅ Message sent successfully")
        except Exception as err:
            _WS_LOGGER.error("❌ Failed to send WebSocket message: %s", err)
            raise Dali2IotConnectionError(f"Failed to send message: {err}") from err

    async def async_set_event_filter(self, filters: dict[str, bool]) -> None:
        """Set WebSocket event filters."""
        _WS_LOGGER.info("🔧 Setting event filters: %s", filters)
        message = {
            "type": "filtering",
            "data": filters
        }
        await self.async_send_ws_message(message)

    async def async_get_info(self) -> dict[str, Any]:
        """Get device information."""
        try:
            async with async_timeout.timeout(API_TIMEOUT):
                async with self._session.get(f"{self._base_url}/info") as response:
                    if response.status == 200:
                        return await response.json()
                    _LOGGER.error("Failed to get device info from %s: %s", self._host, response.status)
                    raise Dali2IotConnectionError(f"Invalid response from device at {self._host}: {response.status}")
        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.error("Error getting device info from %s: %s", self._host, err)
            raise Dali2IotConnectionError(f"Connection failed to {self._host}: {err}") from err

    async def async_get_devices(self) -> list[dict[str, Any]]:
        """Get list of devices from the DALI2 IoT controller.

        Returns cached device list which is automatically updated via WebSocket events.
        If WebSocket is not connected, falls back to REST API.
        """
        if self._ws_connected:
            # Return cached devices from WebSocket events
            return self._devices
        else:
            # Fallback to REST API if WebSocket not connected
            _LOGGER.warning("WebSocket not connected, falling back to REST API for device list")
            try:
                async with async_timeout.timeout(API_TIMEOUT):
                    async with self._session.get(f"{self._base_url}/devices") as response:
                        if response.status == 200:
                            data = await response.json()
                            self._devices = data.get("devices", [])
                            return self._devices
                        _LOGGER.error("Failed to get devices from %s: %s", self._host, response.status)
                        raise Dali2IotConnectionError(f"Invalid response from device at {self._host}: {response.status}")
            except (asyncio.TimeoutError, aiohttp.ClientError) as err:
                _LOGGER.error("Error getting devices from %s: %s", self._host, err)
                raise Dali2IotConnectionError(f"Connection failed to {self._host}: {err}") from err

    async def async_control_device(
        self, device_id: int, data: dict[str, Any]
    ) -> bool:
        """Control a device."""
        try:
            async with async_timeout.timeout(API_TIMEOUT):
                async with self._session.post(
                    f"{self._base_url}/device/{device_id}/control",
                    json=data,
                ) as response:
                    if response.status == 204:
                        return True
                    _LOGGER.error("Failed to control device at %s: %s", self._host, response.status)
                    raise Dali2IotConnectionError(f"Invalid response from device at {self._host}: {response.status}")
        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.error("Error controlling device at %s: %s", self._host, err)
            raise Dali2IotConnectionError(f"Connection failed to {self._host}: {err}") from err

    async def async_start_scan(self, new_installation: bool = False) -> dict[str, Any]:
        """Start a DALI device scan."""
        try:
            async with async_timeout.timeout(API_TIMEOUT):
                async with self._session.post(
                    f"{self._base_url}/dali/scan",
                    json={"newInstallation": new_installation},
                ) as response:
                    if response.status == 200:
                        return await response.json()
                    _LOGGER.error("Failed to start scan on %s: %s", self._host, response.status)
                    raise Dali2IotConnectionError(f"Failed to start scan on {self._host}: {response.status}")
        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.error("Error starting scan on %s: %s", self._host, err)
            raise Dali2IotConnectionError(f"Connection failed to {self._host}: {err}") from err

    async def async_get_scan_status(self) -> dict[str, Any]:
        """Get the current scan status."""
        try:
            async with async_timeout.timeout(API_TIMEOUT):
                async with self._session.get(f"{self._base_url}/dali/scan") as response:
                    if response.status == 200:
                        return await response.json()
                    _LOGGER.error("Failed to get scan status from %s: %s", self._host, response.status)
                    raise Dali2IotConnectionError(f"Failed to get scan status from {self._host}: {response.status}")
        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.error("Error getting scan status from %s: %s", self._host, err)
            raise Dali2IotConnectionError(f"Connection failed to {self._host}: {err}") from err

    async def async_update_device_groups(
        self, device_id: int, groups: list[int]
    ) -> bool:
        """Update device group membership."""
        try:
            data = {"groups": groups}
            async with async_timeout.timeout(API_TIMEOUT):
                async with self._session.put(
                    f"{self._base_url}/device/{device_id}",
                    json=data,
                ) as response:
                    if response.status == 200:
                        return True
                    _LOGGER.error("Failed to update device groups at %s: %s", self._host, response.status)
                    raise Dali2IotConnectionError(f"Failed to update device groups at {self._host}: {response.status}")
        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.error("Error updating device groups at %s: %s", self._host, err)
            raise Dali2IotConnectionError(f"Connection failed to {self._host}: {err}") from err

    async def async_add_device_to_group(
        self, device_id: int, group_id: int
    ) -> bool:
        """Add device to a DALI group."""
        # Get current device data to retrieve existing groups
        devices = await self.async_get_devices()
        device = next((d for d in devices if d["id"] == device_id), None)
        
        if not device:
            _LOGGER.error("Device %s not found", device_id)
            raise Dali2IotConnectionError(f"Device {device_id} not found")
        
        current_groups = device.get("groups", [])
        
        # Add group if not already present
        if group_id not in current_groups:
            new_groups = current_groups + [group_id]
            return await self.async_update_device_groups(device_id, new_groups)
        
        return True  # Already in group

    async def async_remove_device_from_group(
        self, device_id: int, group_id: int
    ) -> bool:
        """Remove device from a DALI group."""
        # Get current device data to retrieve existing groups
        devices = await self.async_get_devices()
        device = next((d for d in devices if d["id"] == device_id), None)
        
        if not device:
            _LOGGER.error("Device %s not found", device_id)
            raise Dali2IotConnectionError(f"Device {device_id} not found")
        
        current_groups = device.get("groups", [])
        
        # Remove group if present
        if group_id in current_groups:
            new_groups = [g for g in current_groups if g != group_id]
            return await self.async_update_device_groups(device_id, new_groups)
        
        return True  # Not in group anyway

    async def async_control_group(
        self, group_id: int, data: dict[str, Any], line: int | None = None
    ) -> bool:
        """Control a DALI group."""
        try:
            url = f"{self._base_url}/group/{group_id}/control"
            params = {}
            if line is not None:
                params["_line"] = line
            
            async with async_timeout.timeout(API_TIMEOUT):
                async with self._session.post(
                    url,
                    json=data,
                    params=params if params else None,
                ) as response:
                    if response.status == 204:
                        return True
                    _LOGGER.error("Failed to control group %s at %s: %s", group_id, self._host, response.status)
                    raise Dali2IotConnectionError(f"Failed to control group {group_id} at {self._host}: {response.status}")
        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.error("Error controlling group %s at %s: %s", group_id, self._host, err)
            raise Dali2IotConnectionError(f"Connection failed to {self._host}: {err}") from err

    async def async_set_fade_time(
        self, device_id: int, fade_time: float
    ) -> bool:
        """Set the fade time for a device (in seconds)."""
        try:
            data = {"fadeTime": fade_time}
            async with async_timeout.timeout(API_TIMEOUT):
                async with self._session.post(
                    f"{self._base_url}/device/{device_id}/control",
                    json=data,
                ) as response:
                    if response.status == 204:
                        return True
                    _LOGGER.error("Failed to set fade time for device %s at %s: %s", device_id, self._host, response.status)
                    raise Dali2IotConnectionError(f"Failed to set fade time for device {device_id} at {self._host}: {response.status}")
        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.error("Error setting fade time for device %s at %s: %s", device_id, self._host, err)
            raise Dali2IotConnectionError(f"Connection failed to {self._host}: {err}") from err

    async def async_set_group_fade_time(
        self, group_id: int, fade_time: float, line: int | None = None
    ) -> bool:
        """Set the fade time for a group (in seconds)."""
        try:
            data = {"fadeTime": fade_time}
            url = f"{self._base_url}/group/{group_id}/control"
            params = {}
            if line is not None:
                params["_line"] = line
            
            async with async_timeout.timeout(API_TIMEOUT):
                async with self._session.post(
                    url,
                    json=data,
                    params=params if params else None,
                ) as response:
                    if response.status == 204:
                        return True
                    _LOGGER.error("Failed to set fade time for group %s at %s: %s", group_id, self._host, response.status)
                    raise Dali2IotConnectionError(f"Failed to set fade time for group {group_id} at {self._host}: {response.status}")
        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.error("Error setting fade time for group %s at %s: %s", group_id, self._host, err)
            raise Dali2IotConnectionError(f"Connection failed to {self._host}: {err}") from err

    async def async_control_device_with_fade(
        self, device_id: int, data: dict[str, Any], fade_time: float | None = None
    ) -> bool:
        """Control device with optional fade time."""
        if fade_time is None:
            return await self.async_control_device(device_id, data)

        # Convert standard commands to fade variants according to Lunatone API
        fade_data = {}
        
        # Handle switchable separately as it doesn't have a fade variant
        if "switchable" in data:
            fade_data["switchable"] = data["switchable"]
        
        # Convert dimmable to dimmableWithFade
        if "dimmable" in data:
            fade_data["dimmableWithFade"] = {
                "dimValue": data["dimmable"],
                "fadeTime": fade_time
            }
        
        # Convert colorRGB to colorRGBWithFade
        if "colorRGB" in data:
            fade_data["colorRGBWithFade"] = {
                "color": data["colorRGB"],
                "fadeTime": fade_time
            }
        
        # Convert colorKelvin to colorKelvinWithFade
        if "colorKelvin" in data:
            fade_data["colorKelvinWithFade"] = {
                "color": data["colorKelvin"],
                "fadeTime": fade_time
            }
        
        # Copy any other attributes (scene, etc.) that don't have fade variants
        for key, value in data.items():
            if key not in ["dimmable", "colorRGB", "colorKelvin", "switchable"]:
                fade_data[key] = value
        
        # If no fade-compatible commands were found, fall back to regular control
        if not any(key.endswith("WithFade") for key in fade_data.keys()):
            return await self.async_control_device(device_id, data)

        return await self.async_control_device(device_id, fade_data)

    async def async_control_group_with_fade(
        self, group_id: int, data: dict[str, Any], fade_time: float | None = None, line: int | None = None
    ) -> bool:
        """Control group with optional fade time."""
        if fade_time is None:
            return await self.async_control_group(group_id, data, line)

        # Convert standard commands to fade variants according to Lunatone API
        fade_data = {}
        
        # Handle switchable separately as it doesn't have a fade variant
        if "switchable" in data:
            fade_data["switchable"] = data["switchable"]
        
        # Convert dimmable to dimmableWithFade
        if "dimmable" in data:
            fade_data["dimmableWithFade"] = {
                "dimValue": data["dimmable"],
                "fadeTime": fade_time
            }
        
        # Convert colorRGB to colorRGBWithFade
        if "colorRGB" in data:
            fade_data["colorRGBWithFade"] = {
                "color": data["colorRGB"],
                "fadeTime": fade_time
            }
        
        # Convert colorKelvin to colorKelvinWithFade
        if "colorKelvin" in data:
            fade_data["colorKelvinWithFade"] = {
                "color": data["colorKelvin"],
                "fadeTime": fade_time
            }
        
        # Copy any other attributes (scene, etc.) that don't have fade variants
        for key, value in data.items():
            if key not in ["dimmable", "colorRGB", "colorKelvin", "switchable"]:
                fade_data[key] = value
        
        # If no fade-compatible commands were found, fall back to regular control
        if not any(key.endswith("WithFade") for key in fade_data.keys()):
            return await self.async_control_group(group_id, data, line)

        return await self.async_control_group(group_id, fade_data, line)

    async def async_query_device_status(self, dali_address: int, line: int = 0) -> bool:
        """Query a DALI device status via WebSocket to check if it's present.

        Sends a QUERY STATUS command (opcode 144) to the specified DALI address.
        This is useful to probe if a device has powered up.

        Args:
            dali_address: DALI short address (0-63)
            line: DALI line number (default 0)

        Returns:
            True if device responded, False otherwise
        """
        if not self._ws_connected:
            _LOGGER.warning("WebSocket not connected, cannot query device %s", dali_address)
            return False

        # DALI QUERY STATUS command
        # Address byte: (address << 1) | 1  (for query commands, LSB = 1)
        # Opcode: 144 (0x90) = QUERY STATUS
        address_byte = (dali_address << 1) | 1
        opcode = 144  # QUERY STATUS

        message = {
            "type": "daliFrame",
            "data": {
                "line": line,
                "numberOfBits": 16,
                "mode": {
                    "sendTwice": False,
                    "waitForAnswer": True,
                    "priority": 3
                },
                "daliData": [address_byte, opcode]
            }
        }

        try:
            _WS_LOGGER.debug(
                "📤 Querying device status: address=%s, line=%s",
                dali_address,
                line
            )
            await self._ws.send_json(message)
            # Note: The answer will come back via daliAnswer event
            # We don't wait for it here, the coordinator will pick it up
            return True
        except Exception as err:
            _LOGGER.exception("Failed to send DALI query: %s", err)
            return False

    async def async_query_all_devices(self, known_addresses: list[int]) -> None:
        """Query all known device addresses to refresh their states.

        This is useful when we suspect devices may have powered up.

        Args:
            known_addresses: List of DALI addresses to query
        """
        if not self._ws_connected:
            _LOGGER.warning("WebSocket not connected, cannot query devices")
            return

        _LOGGER.info("Querying %d devices for status updates", len(known_addresses))

        for address in known_addresses:
            if 0 <= address <= 63:  # Valid DALI short address range
                await self.async_query_device_status(address)
                # Small delay between queries to avoid overwhelming the bus
                await asyncio.sleep(0.05)  # 50ms between queries 