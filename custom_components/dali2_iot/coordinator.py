"""Coordinator for DALI2 IoT integration."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .device import Dali2IotDevice

_LOGGER = logging.getLogger(__name__)

class Dali2IotCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the DALI2 IoT device."""

    def __init__(
        self,
        hass: HomeAssistant,
        device: Dali2IotDevice,
        update_interval: int = 30,
    ) -> None:
        """Initialize the coordinator."""
        # Create a custom logger with higher log level to reduce debug spam
        coordinator_logger = logging.getLogger(f"{__name__}.quiet")
        coordinator_logger.setLevel(logging.INFO)

        super().__init__(
            hass,
            coordinator_logger,
            name="DALI2 IoT",
            update_interval=timedelta(seconds=update_interval),
        )
        self.device = device
        self._devices: list[dict[str, Any]] = []
        self._groups: dict[int, dict[str, Any]] = {}

        # Register WebSocket event callbacks
        self.device.register_event_callback("devices", self._on_devices_update)
        self.device.register_event_callback("devicesDeleted", self._on_devices_deleted)
        self.device.register_event_callback("zones", self._on_zones_update)
        self.device.register_event_callback("scanProgress", self._on_scan_progress)
        self.device.register_event_callback("daliMonitor", self._on_dali_monitor)
        self.device.register_event_callback("daliStatus", self._on_dali_status)

        # Track last DALI monitor activity to debounce refresh requests
        self._last_dali_activity: float = 0

    async def _on_devices_update(self, data: dict[str, Any]) -> None:
        """Handle device update events from WebSocket.

        WebSocket device events contain only changed properties, not full device data.
        We need to merge these partial updates with our cached device list.
        """
        devices_data = data.get("devices", [])
        _LOGGER.debug("Received device update via WebSocket: %s device(s)", len(devices_data))

        # Check if this is a full device update (new devices with all properties)
        # or a partial update (only changed properties)
        needs_full_refresh = False

        for device_update in devices_data:
            device_id = device_update.get("id")
            if not device_id:
                continue

            # Find the existing device in our cache
            existing_device = None
            for idx, dev in enumerate(self._devices):
                if dev.get("id") == device_id:
                    existing_device = self._devices[idx]
                    break

            # If this device has a "name" field, it's a full update (new device)
            # If it only has "id" and "features", it's a partial update (state change)
            if "name" in device_update or "address" in device_update:
                # Full device data - this is a new device or complete update
                if existing_device:
                    # Update existing device with new full data
                    self._devices[idx] = device_update
                else:
                    # New device - add it
                    self._devices.append(device_update)
                _LOGGER.debug("Full device update for device %s", device_id)
            else:
                # Partial update - merge with existing device
                if existing_device:
                    # Merge the partial update into existing device
                    if "features" in device_update:
                        # Update features
                        existing_features = existing_device.get("features", {})
                        for feature_name, feature_data in device_update["features"].items():
                            existing_features[feature_name] = feature_data
                        existing_device["features"] = existing_features
                    if "groups" in device_update:
                        # Update groups
                        existing_device["groups"] = device_update["groups"]
                    _LOGGER.debug("Partial device update for device %s (cached)", device_id)
                else:
                    # We don't have this device cached - need full refresh
                    _LOGGER.warning(
                        "Received partial update for unknown device %s - requesting full refresh",
                        device_id
                    )
                    needs_full_refresh = True

        if needs_full_refresh:
            # We received an update for a device we don't know about
            # Request a full refresh to get complete device data
            await self.async_request_refresh()
        else:
            # Update groups based on new device data
            self._groups = self._extract_groups_from_devices(self._devices)

            # Notify listeners that data has changed (without doing a full refresh)
            self.async_set_updated_data({"devices": self._devices, "groups": self._groups})

    async def _on_devices_deleted(self, data: dict[str, Any]) -> None:
        """Handle device deletion events from WebSocket."""
        _LOGGER.debug("Received device deletion via WebSocket")
        # Trigger coordinator refresh
        await self.async_request_refresh()

    async def _on_zones_update(self, data: dict[str, Any]) -> None:
        """Handle zone update events from WebSocket."""
        _LOGGER.debug("Received zone update via WebSocket")
        # Trigger coordinator refresh
        await self.async_request_refresh()

    async def _on_scan_progress(self, data: dict[str, Any]) -> None:
        """Handle scan progress events from WebSocket."""
        status = data.get("status")
        progress = data.get("progress", 0)
        _LOGGER.info("DALI scan progress: %s%% - %s", progress, status)

    async def _on_dali_status(self, data: dict[str, Any]) -> None:
        """Handle DALI bus status events from WebSocket.

        Status code 2 means DALI bus power is ON. When we see this,
        devices may have powered up and we should refresh device states.
        """
        status_code = data.get("status")
        line = data.get("line", 0)

        _LOGGER.debug("DALI bus status change: status=%s, line=%s", status_code, line)

        # Status code 2 = DALI bus powered ON
        if status_code == 2:
            _LOGGER.info("DALI bus powered ON - probing all known devices")
            # Actively query all devices to see if any have powered up
            await self._probe_all_devices()
            # Also request a full refresh to get updated states
            await self.async_request_refresh()

    async def _on_dali_monitor(self, data: dict[str, Any]) -> None:
        """Handle DALI bus monitor events from WebSocket.

        When we see device responses (8-bit answers), especially from external
        sources (like physical switches), we should refresh device states.
        """
        import time

        bits = data.get("bits")
        dali_data = data.get("data", [])
        framing_error = data.get("framingError", False)
        external_source = data.get("externalSource", False)

        # We're interested in:
        # 1. 8-bit answers (device responses) - means a device answered
        # 2. External 16-bit commands - means someone else is controlling devices
        is_device_answer = bits == 8 and not framing_error
        is_external_command = bits == 16 and external_source

        if not (is_device_answer or is_external_command):
            return

        # Debounce: only refresh if we haven't seen activity in the last 5 seconds
        # This prevents spam when there's lots of bus activity
        current_time = time.time()
        if current_time - self._last_dali_activity < 5.0:
            return

        self._last_dali_activity = current_time

        if is_external_command:
            _LOGGER.info(
                "External DALI command detected (data: %s) - scheduling refresh",
                dali_data
            )
        else:
            _LOGGER.debug(
                "DALI device activity detected (answer: %s) - scheduling refresh",
                dali_data
            )

        # Schedule a refresh to check for state changes
        # Use async_request_refresh which will debounce multiple rapid requests
        await self.async_request_refresh()

    async def _probe_all_devices(self) -> None:
        """Actively probe all known devices via DALI query commands.

        This sends QUERY STATUS commands to all known device addresses
        to check if they're present and responsive. Useful when we suspect
        devices may have powered up.
        """
        if not self._devices:
            _LOGGER.debug("No known devices to probe")
            return

        # Extract DALI addresses from known devices
        addresses = []
        for device in self._devices:
            address = device.get("address")
            if address is not None and 0 <= address <= 63:
                addresses.append(address)

        if addresses:
            _LOGGER.info("Probing %d device addresses: %s", len(addresses), addresses)
            await self.device.async_query_all_devices(addresses)

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from the DALI2 IoT device.

        With WebSocket connection, this mostly returns cached data that's
        automatically updated via events. The update_interval serves as a
        fallback health check.
        """
        devices = await self.device.async_get_devices()
        self._devices = devices

        # Extract groups from device data
        self._groups = self._extract_groups_from_devices(devices)

        return {"devices": devices, "groups": self._groups}

    def get_device(self, device_id: int) -> dict[str, Any] | None:
        """Get device by ID."""
        for device in self._devices:
            if device.get("id") == device_id:
                return device
        return None
    
    def _extract_groups_from_devices(self, devices: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
        """Extract group information from device data."""
        groups = {}
        
        # Collect all group IDs and their member devices
        for device in devices:
            device_groups = device.get("groups", [])
            device_id = device.get("id")
            device_features = device.get("features", {})
            
            for group_id in device_groups:
                if group_id not in groups:
                    groups[group_id] = {
                        "id": group_id,
                        "name": f"DALI Group {group_id}",
                        "members": [],
                        "features": {},
                    }
                
                # Add device to group
                device_address = device.get("address", device_id)  # Use DALI address, fallback to ID
                groups[group_id]["members"].append({
                    "id": device_address,  # Use DALI address for group member ID
                    "name": device.get("name", f"Device {device_address}"),
                    "features": device_features,
                })
                
                # Aggregate features from all devices in group
                # A group supports a feature if any member supports it
                for feature_name, feature_data in device_features.items():
                    if feature_name not in groups[group_id]["features"]:
                        groups[group_id]["features"][feature_name] = feature_data
        
        return groups
    
    def get_group(self, group_id: int) -> dict[str, Any] | None:
        """Get group by ID."""
        return self._groups.get(group_id)
    
    def get_all_groups(self) -> dict[int, dict[str, Any]]:
        """Get all groups."""
        return self._groups 