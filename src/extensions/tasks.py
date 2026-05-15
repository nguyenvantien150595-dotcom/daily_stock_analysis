# -*- coding: utf-8 -*-
"""Task queue bridge for asynchronous Extension Runtime actions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Callable, Dict

from src.extensions.actions import ActionContext, ActionDefinition
from src.services.task_queue import TaskInfo, get_task_queue


class ActionTaskRunner:
    def __init__(self, queue_factory: Callable[[], object] = get_task_queue):
        self.queue_factory = queue_factory

    def submit(
        self,
        *,
        action: ActionDefinition,
        payload: Mapping[str, Any],
        context: ActionContext,
        run_id: str,
        run_callable: Callable[[], Dict[str, Any]],
    ) -> TaskInfo:
        subject = str(payload.get(action.subject_key) or action.action_id) if action.subject_key else action.action_id
        task = self.queue_factory().submit_background_task(
            run_callable,
            stock_code=subject,
            stock_name=action.description,
            report_type="plugin",
            message="插件 Action 已加入队列",
            task_type="plugin",
            action_id=action.action_id,
            subject=subject,
            run_id=run_id,
            caller=context.caller,
        )
        return task
