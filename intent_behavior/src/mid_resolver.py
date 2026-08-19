#!/usr/bin/env python3
"""
博文 mid 反解客户端
===================
调用微博内部接口 `http://terra.biz.weibo.com/mid/media`，
通过博文 mid 反解出正文、图片 pid 列表、视频 fid/cover/url 等真实内容。

为什么需要：
- MySQL 分表（nature_ad_super_mid_x）里的 mid_pids / mid_fids 字段有时和实际请求 API 所需不匹配。
- 通过 mid 反解可以拿到当前最新、最准确的博文内容。

接口文档：
  intent_behavior/博文mid反解-接口wiki.docx

典型返回结构：
{
  "code": 0,
  "result": {
    "mid": "4876329553760906",
    "data": {
      "images": ["pid1", "pid2"],
      "video": {
        "source": 1,
        "cover": "http://...",
        "url": null,
        "fid": "2362904:4828110227701817"
      },
      "content": {"actual_content": "..."}
    },
    "is_retweeted": false
  },
  "message": "SUCCESS"
}
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

from .models import BlogItem


logger = logging.getLogger(__name__)


DEFAULT_RESOLVER_URL = "http://terra.biz.weibo.com/mid/media"


@dataclass
class ResolvedBlog:
    """mid 反解后的博文信息"""

    mid: str
    uid: Optional[str] = None
    content: str = ""
    pic_ids: List[str] = field(default_factory=list)
    video_fid: str = ""
    video_cover_url: str = ""
    video_url: Optional[str] = None
    is_retweeted: bool = False
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    def has_image(self) -> bool:
        return len(self.pic_ids) > 0

    def has_video(self) -> bool:
        return bool(self.video_fid)

    def to_blog_item(self) -> BlogItem:
        """转换为内部 BlogItem，供分类器使用"""
        return BlogItem(
            mid=str(self.mid),
            uid=str(self.uid or ""),
            content=self.content or "",
            pic_ids=list(self.pic_ids),
            media_ids=[self.video_fid] if self.video_fid else [],
            extra={
                "source": "mid_resolver",
                "is_retweeted": self.is_retweeted,
                "video_cover_url": self.video_cover_url,
                "video_url": self.video_url,
                "raw": self.raw,
            },
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mid": self.mid,
            "uid": self.uid,
            "content": self.content,
            "pic_ids": self.pic_ids,
            "video_fid": self.video_fid,
            "video_cover_url": self.video_cover_url,
            "video_url": self.video_url,
            "is_retweeted": self.is_retweeted,
        }


class MidResolverClient:
    """博文 mid 反解客户端"""

    def __init__(
        self,
        url: str = DEFAULT_RESOLVER_URL,
        timeout: int = 30,
        max_retry: int = 3,
        logger_: Optional[logging.Logger] = None,
    ):
        self.url = url
        self.timeout = timeout
        self.max_retry = max_retry
        self.logger = logger_ or logging.getLogger(__name__)

    def resolve(
        self,
        mid: str,
        uid: Optional[str] = None,
        parse_component: int = 1,
        obj_type: Optional[int] = None,
    ) -> ResolvedBlog:
        """
        通过 mid 反解博文内容。

        Args:
            mid: 博文 mid
            uid: 博文作者 uid（可选，建议传入以提高命中率）
            parse_component: 是否解析组件，1=解析，0=不解析
            obj_type: 是否解析标的信息，1=解析，0=不解析

        Returns:
            ResolvedBlog 对象

        Raises:
            RuntimeError: 接口返回非 0 或请求失败
        """
        params: Dict[str, Any] = {
            "mid": mid,
            "parse_component": parse_component,
        }
        if uid:
            params["uid"] = uid
        if obj_type is not None:
            params["obj_type"] = obj_type

        last_error = ""
        for attempt in range(1, self.max_retry + 1):
            try:
                self.logger.info(
                    f"[MidResolver] 请求反解 mid={mid} uid={uid or ''} (attempt {attempt})"
                )
                resp = requests.get(
                    self.url,
                    params=params,
                    timeout=self.timeout,
                    headers={"Accept": "application/json"},
                )
                resp.raise_for_status()
                data = resp.json()
                return self._parse_response(mid, uid, data)

            except requests.exceptions.Timeout:
                last_error = f"反解接口超时 (attempt {attempt})"
                self.logger.warning(f"[MidResolver] {last_error}: mid={mid}")
            except requests.exceptions.RequestException as e:
                last_error = f"反解接口请求异常: {e}"
                self.logger.warning(f"[MidResolver] {last_error}: mid={mid}")
            except Exception as e:
                last_error = f"反解接口未知异常: {e}"
                self.logger.warning(f"[MidResolver] {last_error}: mid={mid}")

        raise RuntimeError(f"[MidResolver] 反解失败 mid={mid}: {last_error}")

    def _parse_response(self, mid: str, uid: Optional[str], data: Dict[str, Any]) -> ResolvedBlog:
        """解析反解接口响应"""
        code = data.get("code")
        if code != 0:
            msg = data.get("message", "未知错误")
            raise RuntimeError(f"[MidResolver] 反解接口返回错误 mid={mid}: code={code}, message={msg}")

        result = data.get("result", {}) or {}
        result_mid = result.get("mid", mid)
        result_data = result.get("data", {}) or {}

        # 正文
        content = ""
        content_obj = result_data.get("content")
        if isinstance(content_obj, dict):
            content = content_obj.get("actual_content", "") or ""
        elif isinstance(content_obj, str):
            content = content_obj

        # 图片 pid 列表
        pic_ids: List[str] = []
        images = result_data.get("images")
        if isinstance(images, list):
            pic_ids = [str(p).strip() for p in images if str(p).strip()]

        # 视频信息
        video_fid = ""
        video_cover_url = ""
        video_url = None
        video_obj = result_data.get("video")
        if isinstance(video_obj, dict):
            video_fid = str(video_obj.get("fid", "")).strip()
            video_cover_url = str(video_obj.get("cover", "")).strip()
            video_url = video_obj.get("url")

        is_retweeted = bool(result.get("is_retweeted", False))

        return ResolvedBlog(
            mid=str(result_mid),
            uid=str(uid) if uid else None,
            content=content,
            pic_ids=pic_ids,
            video_fid=video_fid,
            video_cover_url=video_cover_url,
            video_url=video_url,
            is_retweeted=is_retweeted,
            raw=data,
        )
