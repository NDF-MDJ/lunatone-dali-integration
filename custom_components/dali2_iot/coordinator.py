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

    async def _on_devices_update(self, data: dict[str, Any]) -> None:
        """Handle device update events from WebSocket."""
        _LOGGER.debug("Received device update via WebSocket")
        # Trigger coordinator refresh
        await self.async_request_refresh()

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