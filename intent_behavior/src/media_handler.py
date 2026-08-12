"""
媒体处理模块

负责图片和视频的获取、下载、转换。
- 图片：pid → URL → 下载到本地 → base64
- 视频：media_id → hive表 → fid → showBatch API → 视频URL → 下载 → 抽帧（预留）
"""

import os
import re
import logging
import requests
from typing import Optional, List, Dict, Any


logger = logging.getLogger(__name__)


class ImageHandler:
    """图片处理器：pid → URL → 下载 → base64"""

    def __init__(self, config: Dict[str, Any], logger: Optional[logging.Logger] = None):
        """
        Args:
            config: media.image 配置段
            logger: 日志器
        """
        self.url_pattern = config.get("url_pattern", "https://wx2.sinaimg.cn/mw690/{pid}.jpg")
        self.download_timeout = config.get("download_timeout", 30)
        self.max_images = config.get("max_images_per_request", 3)
        self.logger = logger or logging.getLogger(__name__)

    def pid_to_url(self, pid: str) -> str:
        """
        pid 转图片URL

        Args:
            pid: 图片pid，如 "006mX07Rly8ifv3xs5535j30ud0plk1m"

        Returns:
            图片URL字符串
        """
        return self.url_pattern.replace("{pid}", pid)

    def extract_pids(self, content: str) -> List[str]:
        """
        从博文内容中提取图片pid（实例方法，供分类器调用）
        """
        return self._extract_pids_from_text(content, self.max_images)

    @staticmethod
    def _extract_pids_from_text(content: str, max_images: int = 3) -> List[str]:
        """
        从博文内容中提取图片pid（静态方法，供数据提取层无实例时调用）

        微博博文中的图片pid通常以JSON格式嵌入，如：
        {"pid":"006mX07Rly8ifv3xs5535j30ud0plk1m"}
        也可能直接出现pid字符串
        """
        if not content:
            return []

        pids = []

        # 策略1：匹配 {"pid":"xxx"} 格式
        pid_matches = re.findall(r'"pid"\s*:\s*"([^"]+)"', content)
        pids.extend(pid_matches)

        # 策略2：匹配 pid:xxx 格式（无引号）
        if not pids:
            pid_matches = re.findall(r'pid["\']?\s*[:=]\s*["\']?([a-zA-Z0-9]{20,})', content)
            pids.extend(pid_matches)

        # 去重，限制数量
        seen = set()
        unique_pids = []
        for p in pids:
            if p not in seen:
                seen.add(p)
                unique_pids.append(p)
                if len(unique_pids) >= max_images:
                    break

        return unique_pids

    def download_image(self, url: str, save_path: str) -> bool:
        """
        下载图片到本地

        Args:
            url: 图片URL
            save_path: 本地保存路径

        Returns:
            成功返回 True，失败返回 False
        """
        try:
            resp = requests.get(url, timeout=self.download_timeout, stream=True)
            resp.raise_for_status()

            with open(save_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)

            file_size = os.path.getsize(save_path)
            if file_size < 100:
                self.logger.warning(f"图片文件过小({file_size}B)，可能异常: {url}")
                return False

            self.logger.debug(f"图片下载成功: {url} → {save_path} ({file_size}B)")
            return True

        except requests.exceptions.Timeout:
            self.logger.warning(f"图片下载超时: {url}")
        except requests.exceptions.RequestException as e:
            self.logger.warning(f"图片下载失败: {url} - {e}")
        except Exception as e:
            self.logger.warning(f"图片下载异常: {url} - {e}")

        return False

    def download_images_by_pids(self, pids: List[str],
                                tmp_dir: str = "/tmp/blog_images") -> List[str]:
        """
        批量下载图片

        Args:
            pids: pid 列表
            tmp_dir: 临时目录

        Returns:
            成功下载的图片本地路径列表
        """
        os.makedirs(tmp_dir, exist_ok=True)
        downloaded = []

        for i, pid in enumerate(pids):
            url = self.pid_to_url(pid)
            save_path = os.path.join(tmp_dir, f"{pid}.jpg")

            if self.download_image(url, save_path):
                downloaded.append(save_path)
            else:
                self.logger.warning(f"图片下载失败 pid={pid}")

        return downloaded


class VideoHandler:
    """
    视频处理器：media_id → hive表 → fid → showBatch API → 视频URL → 下载 → 抽帧

    当前为预留实现，待视频功能启用后完善。
    """

    def __init__(self, config: Dict[str, Any], logger: Optional[logging.Logger] = None):
        """
        Args:
            config: media.video 配置段
            logger: 日志器
        """
        self.hive_table = config.get("hive_table", "ods_ad_sfst_media_info")
        self.showbatch_api = config.get("showbatch_api", "")
        self.default_customer_id = config.get("default_customer_id", "")
        self.extract_frames_count = config.get("extract_frames_count", 3)
        self.download_timeout = config.get("download_timeout", 120)
        self.enabled = config.get("enabled", False)
        self.logger = logger or logging.getLogger(__name__)

    def get_video_url(self, media_id: str, customer_id: str = None) -> Optional[str]:
        """
        通过 media_id 获取视频播放地址

        步骤：
        1. 查 hive 表 ods_ad_sfst_media_info 获取 fid（预留）
        2. 调 showBatch API 获取视频URL

        Args:
            media_id: 媒体ID
            customer_id: 客户ID（可选，默认用配置值）

        Returns:
            视频URL，失败返回 None

        TODO: 实现hive查询逻辑
        """
        if not self.enabled:
            self.logger.info("视频处理未启用，跳过")
            return None

        cid = customer_id or self.default_customer_id

        # TODO: 实现hive查询
        # 当前使用showBatch API直接查询
        try:
            resp = requests.get(
                self.showbatch_api,
                params={"customer_id": cid, "fids": media_id},
                headers={
                    "User-Agent": "BlogClassifier/1.0",
                    "Accept": "*/*",
                    "Connection": "keep-alive"
                },
                timeout=30
            )
            resp.raise_for_status()
            data = resp.json()

            if data.get("status") == 200 and data.get("data"):
                video_url = data["data"][0].get("url")
                if video_url:
                    self.logger.debug(f"视频URL获取成功: {media_id}")
                    return video_url

            self.logger.warning(f"视频URL获取失败: {media_id}, resp={data}")
            return None

        except Exception as e:
            self.logger.warning(f"视频URL获取异常: {media_id} - {e}")
            return None

    def download_video(self, url: str, save_path: str) -> bool:
        """
        下载视频到本地

        Returns:
            成功返回 True，失败返回 False

        TODO: 实现下载逻辑
        """
        self.logger.info(f"[预留] 视频下载: {url} → {save_path}")
        return False

    def extract_frames(self, video_path: str, num_frames: int = None,
                       output_dir: str = "/tmp/video_frames") -> List[str]:
        """
        从视频中抽取关键帧

        Returns:
            帧图片路径列表

        TODO: 实现抽帧逻辑（使用cv2或imageio）
        """
        self.logger.info(f"[预留] 视频抽帧: {video_path}")
        return []

    def process_video(self, media_id: str, customer_id: str = None) -> List[str]:
        """
        完整视频处理流程：获取URL → 下载 → 抽帧

        Returns:
            视频帧图片路径列表
        """
        if not self.enabled:
            self.logger.info("视频处理未启用，跳过")
            return []

        video_url = self.get_video_url(media_id, customer_id)
        if not video_url:
            return []

        video_path = f"/tmp/video_{media_id}.mp4"
        if not self.download_video(video_url, video_path):
            return []

        frames = self.extract_frames(video_path, self.extract_frames_count)
        return frames
