# -*- coding: utf-8 -*-
"""Extension plugin and action registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Iterable, List, Optional

from src.extensions.actions import ActionDefinition


class PluginStatus(str, Enum):
    NOT_INSTALLED = "not_installed"
    DISABLED = "disabled"
    UNAVAILABLE = "unavailable"
    AVAILABLE = "available"
    ENABLED = "enabled"
    DEGRADED = "degraded"


@dataclass
class ContributionDefinition:
    contribution_id: str
    kind: str
    target: str
    metadata: Dict[str, object] = field(default_factory=dict)


@dataclass
class PluginDefinition:
    plugin_id: str
    name: str
    description: str
    status: PluginStatus = PluginStatus.DISABLED
    version: Optional[str] = None
    contributions: List[ContributionDefinition] = field(default_factory=list)


class ExtensionRegistry:
    def __init__(self):
        self._plugins: Dict[str, PluginDefinition] = {}
        self._actions: Dict[str, ActionDefinition] = {}

    def register_plugin(self, plugin: PluginDefinition) -> None:
        self._plugins[plugin.plugin_id] = plugin

    def get_plugin(self, plugin_id: str) -> Optional[PluginDefinition]:
        return self._plugins.get(plugin_id)

    def list_plugins(self) -> List[PluginDefinition]:
        return list(self._plugins.values())

    def register_action(self, action: ActionDefinition) -> None:
        if action.plugin_id not in self._plugins:
            self.register_plugin(
                PluginDefinition(
                    plugin_id=action.plugin_id,
                    name=action.plugin_id,
                    description="Implicit built-in plugin.",
                    status=PluginStatus.ENABLED,
                )
            )
        self._actions[action.action_id] = action

    def register_actions(self, actions: Iterable[ActionDefinition]) -> None:
        for action in actions:
            self.register_action(action)

    def get_action(self, action_id: str) -> Optional[ActionDefinition]:
        return self._actions.get(action_id)

    def list_actions(self, plugin_id: Optional[str] = None) -> List[ActionDefinition]:
        actions = list(self._actions.values())
        return [action for action in actions if action.plugin_id == plugin_id] if plugin_id else actions
