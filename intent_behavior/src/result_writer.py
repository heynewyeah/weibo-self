#!/usr/bin/env python3
"""
分类结果回写客户端
==================
将分类结果（level）通过 HTTP POST 回写到王燕威提供的接口：

    POST /api/v1/super-mid/update-level
    Body: {
        "customer_id": 2608812381,
        "task_id": 1296890022120652801,
        "mid": 5239425780940868,
        "level": 1
    }

替代原来的 MySQL UPDATE 方式。
"""

from __future__ import annotations

import time
import logging
from datetime import datetime
from typing import Any, Dict, Optional

import requests


logger = logging.getLogger(__name__)


LEVEL_MAPPING = {
    "认知层": 1,
    "兴趣层": 2,
    "考虑层": 3,
    "未识别": 0,
}


class LevelUpdateClient:
    """分类结果 level 回写客户端"""

    def __init__(
        self,
        url: str,
        timeout: int = 30,
        max_retry: int = 3,
        retry_backoff_base: float = 2.0,
        logger_: Optional[logging.Logger] = None,
    ):
        """
        Args:
            url: 完整接口地址，如 http://10.133.6.162:8058/api/v1/super-mid/update-level
            timeout: 单次请求超时（秒）
            max_retry: 最大重试次数
            retry_backoff_base: 重试间隔底数
        """
        self.url = url
        self.timeout = timeout
        self.max_retry = max_retry
        self.retry_backoff_base = retry_backoff_base
        self.logger = logger_ or logging.getLogger(__name__)

    def update_level(
        self,
        customer_id: int,
        task_id: int,
        mid: str,
        level: int,
        update_time: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        回写单条分类结果。

        Args:
            customer_id: 客户ID
            task_id: 任务ID（对应 super_task_id）
            mid: 博文 mid
            level: 层级数值 0/1/2/3
            update_time: 更新时间 ISO 格式（可选，默认当前时间）

        Returns:
            接口响应 JSON

        Raises:
            RuntimeError: 接口调用失败
        """
        payload: Dict[str, Any] = {
            "customer_id": int(customer_id),
            "task_id": int(task_id),
            "mid": mid,
            "level": int(level),
        }
        if update_time:
            payload["update_time"] = update_time

        last_error = ""
        for attempt in range(1, self.max_retry + 1):
            try:
                self.logger.info(
                    f"[LevelUpdate] 回写 level mid={mid} customer_id={customer_id} "
                    f"task_id={task_id} level={level} (attempt {attempt})"
                )
                resp = requests.post(
                    self.url,
                    json=payload,
                    timeout=self.timeout,
                    headers={"Content-Type": "application/json"},
                )
                resp.raise_for_status()
                data = resp.json()
                self.logger.info(f"[LevelUpdate] 回写成功 mid={mid} resp={data}")
                return data

            except requests.exceptions.Timeout:
                last_error = f"回写接口超时 (attempt {attempt})"
                self.logger.warning(f"[LevelUpdate] {last_error}: mid={mid}")
            except requests.exceptions.RequestException as e:
                last_error = f"回写接口请求异常: {e}"
                self.logger.warning(f"[LevelUpdate] {last_error}: mid={mid}")
            except Exception as e:
                last_error = f"回写接口未知异常: {e}"
                self.logger.warning(f"[LevelUpdate] {last_error}: mid={mid}")

            if attempt < self.max_retry:
                sleep_sec = self.retry_backoff_base ** attempt
                time.sleep(sleep_sec)

        raise RuntimeError(f"[LevelUpdate] 回写失败 mid={mid}: {last_error}")

    def update_level_from_result(
        self,
        customer_id: int,
        task_id: int,
        mid: str,
        layer: str,
        update_time: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        从层级名称（如"认知层"）转换为数值后回写。
        """
        level_value = LEVEL_MAPPING.get(layer, 0)
        return self.update_level(
            customer_id=customer_id,
            task_id=task_id,
            mid=mid,
            level=level_value,
            update_time=update_time,
        )
