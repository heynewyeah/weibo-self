#!/usr/bin/env python3
"""
分类结果回写客户端
==================
将分类结果（level）通过 HTTP POST 回写到王燕威提供的接口。
"""

from __future__ import annotations

import time
import logging
from typing import Any, Dict, Optional

import requests


logger = logging.getLogger(__name__)


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
