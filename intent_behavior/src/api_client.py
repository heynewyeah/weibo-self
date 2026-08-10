"""
vLLM API 客户端模块

负责与 vLLM 服务交互，支持纯文本和多模态（图片）请求。
视频请求接口已预留，待后续实现。
"""

import time
import base64
import logging
import requests
from typing import Optional, List, Dict, Any


class VLLMClient:
    """vLLM API 客户端"""

    def __init__(self, config: Dict[str, Any], logger: logging.Logger):
        """
        Args:
            config: api 配置字典，从 config.yaml 的 api 段加载
            logger: 日志器实例
        """
        self.url = config["url"]
        self.model = config["model"]
        self.max_tokens = config.get("max_tokens", 512)
        self.temperature = config.get("temperature", 0.0)
        self.enable_thinking = config.get("enable_thinking", False)
        self.timeout = config.get("timeout", 60)
        self.max_retry = config.get("max_retry", 3)
        self.retry_backoff_base = config.get("retry_backoff_base", 2)
        self.logger = logger

    def _build_payload(self, system_prompt: str, user_content) -> Dict[str, Any]:
        """
        构建请求体

        Args:
            system_prompt: 系统提示词
            user_content: 用户消息内容（字符串或content数组）

        Returns:
            请求体字典
        """
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if self.enable_thinking is not None:
            payload["chat_template_kwargs"] = {"enable_thinking": self.enable_thinking}
        return payload

    def _call_api(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        调用API，带重试机制

        Args:
            payload: 请求体

        Returns:
            API响应字典，失败返回 None
        """
        for attempt in range(1, self.max_retry + 1):
            try:
                resp = requests.post(
                    self.url,
                    json=payload,
                    timeout=self.timeout,
                    headers={"Content-Type": "application/json"}
                )
                resp.raise_for_status()
                data = resp.json()

                if "choices" in data and len(data["choices"]) > 0:
                    return data

                self.logger.warning(f"API返回结构异常，第{attempt}次重试")

            except requests.exceptions.Timeout:
                self.logger.warning(f"API请求超时，第{attempt}/{self.max_retry}次重试...")
            except requests.exceptions.RequestException as e:
                self.logger.warning(f"API请求异常: {e}，第{attempt}/{self.max_retry}次重试...")
            except Exception as e:
                self.logger.warning(f"未知异常: {e}，第{attempt}/{self.max_retry}次重试...")

            if attempt < self.max_retry:
                time.sleep(self.retry_backoff_base ** attempt)

        self.logger.error(f"API调用失败，已达最大重试次数({self.max_retry})")
        return None

    def classify_text(self, system_prompt: str, user_prompt: str) -> Optional[str]:
        """
        纯文本分类

        Args:
            system_prompt: 系统提示词
            user_prompt: 用户提示词（含博文内容）

        Returns:
            模型输出文本，失败返回 None
        """
        payload = self._build_payload(system_prompt, user_prompt)
        data = self._call_api(payload)
        if data is None:
            return None
        return data["choices"][0]["message"]["content"]

    def classify_with_images(self, system_prompt: str, user_prompt: str,
                             image_paths: List[str]) -> Optional[str]:
        """
        图文分类：将图片和文字一起送给模型

        Args:
            system_prompt: 系统提示词
            user_prompt: 用户提示词（含博文文字内容）
            image_paths: 图片本地路径列表

        Returns:
            模型输出文本，失败返回 None
        """
        # 构建 content 数组：先文字，后图片
        content = [{"type": "text", "text": user_prompt}]

        for img_path in image_paths:
            try:
                with open(img_path, "rb") as f:
                    img_b64 = base64.b64encode(f.read()).decode("utf-8")
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}
                })
            except Exception as e:
                self.logger.warning(f"图片读取失败 {img_path}: {e}")

        if len(content) == 1:
            # 没有图片成功加载，退化为纯文本
            self.logger.warning("所有图片加载失败，退化为纯文本分类")
            return self.classify_text(system_prompt, user_prompt)

        payload = self._build_payload(system_prompt, content)
        data = self._call_api(payload)
        if data is None:
            return None
        return data["choices"][0]["message"]["content"]

    def classify_with_video_frames(self, system_prompt: str, user_prompt: str,
                                   frame_paths: List[str]) -> Optional[str]:
        """
        视频分类：将视频抽帧图片和文字一起送给模型

        当前为预留接口，实现方式与 classify_with_images 相同
        （视频帧本质就是图片）

        Args:
            system_prompt: 系统提示词
            user_prompt: 用户提示词（含博文文字内容）
            frame_paths: 视频帧图片本地路径列表

        Returns:
            模型输出文本，失败返回 None
        """
        # 视频帧的处理方式与图片一致
        return self.classify_with_images(system_prompt, user_prompt, frame_paths)
