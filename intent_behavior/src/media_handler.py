"""
媒体处理模块

负责图片和视频的获取、下载、转换。
- 图片：pid → URL → 下载到本地 → base64
- 视频：
    方案A（cover）：media_id → showBatch API → 封面图URL → 下载封面图
    方案B（frame）：media_id → showBatch API → 视频URL → 下载视频 → OpenCV抽帧
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
                                tmp_dir: str = "/tmp/xuanyu11/blog_images") -> List[str]:
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
    视频处理器：media_id → showBatch API → 视频URL/封面图URL → 处理

    支持两种模式：
      - cover 模式（默认）：获取封面图URL → 下载封面图 → 多模态分类
      - frame 模式：获取视频URL → 下载视频 → OpenCV抽帧 → 多模态分类
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
        # 视频处理模式：cover（封面图）或 frame（抽帧）
        self.video_mode = config.get("video_mode", "cover")
        self.logger = logger or logging.getLogger(__name__)

    def get_video_info(self, media_id: str, customer_id: str = None) -> Dict[str, str]:
        """
        通过 showBatch API 获取视频信息（封面图URL + 视频URL）

        Args:
            media_id: 视频 fid，如 "2362904:4826598285967434"
            customer_id: 客户ID（可选，默认用配置值）

        Returns:
            dict，包含 cover_url 和 video_url（可能为空字符串）
        """
        cid = customer_id or self.default_customer_id
        result = {"cover_url": "", "video_url": ""}

        if not self.showbatch_api:
            self.logger.warning("showbatch_api 未配置")
            return result

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
                item = data["data"][0]
                # 封面图：frontUrl 或 cover
                result["cover_url"] = item.get("frontUrl") or item.get("cover") or ""
                # 视频播放地址：url 或 mp4Url
                result["video_url"] = item.get("url") or item.get("mp4Url") or ""
                self.logger.debug(
                    f"视频信息获取成功: fid={media_id} "
                    f"cover={result['cover_url'][:60]} "
                    f"video={result['video_url'][:60]}"
                )
            else:
                self.logger.warning(f"showBatch API 返回异常: fid={media_id}, resp={data}")

        except Exception as e:
            self.logger.warning(f"showBatch API 调用异常: fid={media_id} - {e}")

        return result

    def get_video_url(self, media_id: str, customer_id: str = None) -> Optional[str]:
        """
        获取视频播放地址（兼容旧接口）

        Returns:
            视频URL，失败返回 None
        """
        info = self.get_video_info(media_id, customer_id)
        return info["video_url"] or None

    def get_cover_url(self, media_id: str, customer_id: str = None) -> Optional[str]:
        """
        获取视频封面图URL

        Returns:
            封面图URL，失败返回 None
        """
        info = self.get_video_info(media_id, customer_id)
        return info["cover_url"] or None

    def download_cover(self, cover_url: str, save_path: str) -> bool:
        """
        下载视频封面图

        Args:
            cover_url: 封面图URL
            save_path: 本地保存路径

        Returns:
            成功返回 True，失败返回 False
        """
        try:
            resp = requests.get(cover_url, timeout=30, stream=True)
            resp.raise_for_status()
            with open(save_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            file_size = os.path.getsize(save_path)
            if file_size < 100:
                self.logger.warning(f"封面图文件过小({file_size}B): {cover_url}")
                return False
            self.logger.debug(f"封面图下载成功: {cover_url} → {save_path} ({file_size}B)")
            return True
        except Exception as e:
            self.logger.warning(f"封面图下载失败: {cover_url} - {e}")
            return False

    def download_video(self, url: str, save_path: str) -> bool:
        """
        下载视频到本地

        Args:
            url: 视频URL
            save_path: 本地保存路径

        Returns:
            成功返回 True，失败返回 False
        """
        try:
            resp = requests.get(url, timeout=self.download_timeout, stream=True)
            resp.raise_for_status()
            with open(save_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    f.write(chunk)
            file_size = os.path.getsize(save_path)
            if file_size < 1024:
                self.logger.warning(f"视频文件过小({file_size}B)，可能异常: {url}")
                return False
            self.logger.info(f"视频下载成功: {save_path} ({file_size // 1024}KB)")
            return True
        except requests.exceptions.Timeout:
            self.logger.warning(f"视频下载超时: {url}")
        except Exception as e:
            self.logger.warning(f"视频下载失败: {url} - {e}")
        return False

    def extract_frames(self, video_path: str, num_frames: int = None,
                       output_dir: str = "/tmp/xuanyu11/video_frames") -> List[str]:
        """
        使用 OpenCV 从视频中均匀抽取关键帧

        抽帧策略：
          - 均匀采样：将视频总帧数等分为 num_frames 段，取每段中间帧
          - 若视频总帧数 < num_frames，则取所有帧

        Args:
            video_path: 本地视频文件路径
            num_frames: 抽取帧数，默认使用配置值 extract_frames_count
            output_dir: 帧图片输出目录

        Returns:
            帧图片路径列表（按时间顺序），失败返回空列表
        """
        n = num_frames or self.extract_frames_count
        os.makedirs(output_dir, exist_ok=True)

        try:
            import cv2
        except ImportError:
            self.logger.error(
                "OpenCV 未安装，无法抽帧。请执行: pip install opencv-python-headless"
            )
            return []

        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            self.logger.error(f"无法打开视频文件: {video_path}")
            return []

        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = cap.get(cv2.CAP_PROP_FPS)
        duration_s = total_frames / fps if fps > 0 else 0

        self.logger.info(
            f"视频信息: 总帧数={total_frames} FPS={fps:.1f} 时长={duration_s:.1f}s"
        )

        if total_frames <= 0:
            self.logger.warning(f"视频总帧数为0，无法抽帧: {video_path}")
            cap.release()
            return []

        # 计算均匀采样的帧索引
        actual_n = min(n, total_frames)
        if actual_n == 1:
            frame_indices = [total_frames // 2]
        else:
            # 均匀分布：在 [0, total_frames-1] 区间内取 actual_n 个点
            step = total_frames / actual_n
            frame_indices = [int(step * i + step / 2) for i in range(actual_n)]

        frame_paths = []
        video_basename = os.path.splitext(os.path.basename(video_path))[0]

        for idx, frame_no in enumerate(frame_indices):
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_no)
            ret, frame = cap.read()
            if not ret:
                self.logger.warning(f"读取帧失败: frame_no={frame_no}")
                continue

            frame_path = os.path.join(output_dir, f"{video_basename}_frame{idx:02d}.jpg")
            success = cv2.imwrite(frame_path, frame)
            if success:
                frame_paths.append(frame_path)
                self.logger.debug(
                    f"帧保存成功: frame_no={frame_no} → {frame_path}"
                )
            else:
                self.logger.warning(f"帧保存失败: frame_no={frame_no}")

        cap.release()
        self.logger.info(
            f"抽帧完成: 目标{actual_n}帧，实际获取{len(frame_paths)}帧"
        )
        return frame_paths

    def process_video_cover(self, media_id: str, customer_id: str = None,
                            tmp_dir: str = "/tmp/xuanyu11/video_covers") -> List[str]:
        """
        方案A（cover）：获取封面图 → 下载 → 返回封面图路径列表

        Args:
            media_id: 视频 fid
            customer_id: 客户ID
            tmp_dir: 临时目录

        Returns:
            封面图路径列表（通常只有1张），失败返回空列表
        """
        os.makedirs(tmp_dir, exist_ok=True)
        cover_url = self.get_cover_url(media_id, customer_id)
        if not cover_url:
            self.logger.warning(f"未获取到封面图URL: fid={media_id}")
            return []

        safe_id = media_id.replace(":", "_")
        cover_path = os.path.join(tmp_dir, f"cover_{safe_id}.jpg")
        if self.download_cover(cover_url, cover_path):
            return [cover_path]
        return []

    def process_video_frames(self, media_id: str, customer_id: str = None,
                             tmp_dir: str = "/tmp/xuanyu11/video_frames") -> List[str]:
        """
        方案B（frame）：获取视频URL → 下载视频 → OpenCV抽帧 → 返回帧路径列表

        Args:
            media_id: 视频 fid
            customer_id: 客户ID
            tmp_dir: 临时目录

        Returns:
            帧图片路径列表，失败返回空列表
        """
        os.makedirs(tmp_dir, exist_ok=True)
        video_url = self.get_video_url(media_id, customer_id)
        if not video_url:
            self.logger.warning(f"未获取到视频URL: fid={media_id}")
            return []

        safe_id = media_id.replace(":", "_")
        video_path = os.path.join(tmp_dir, f"video_{safe_id}.mp4")

        if not self.download_video(video_url, video_path):
            return []

        frames_dir = os.path.join(tmp_dir, f"frames_{safe_id}")
        frames = self.extract_frames(video_path, self.extract_frames_count, frames_dir)

        # 清理视频文件（帧保留供后续使用）
        try:
            os.remove(video_path)
        except OSError:
            pass

        return frames

    def process_video(self, media_id: str, customer_id: str = None,
                      mode: str = None) -> List[str]:
        """
        统一视频处理入口

        根据 mode 参数（或配置中的 video_mode）选择处理方案：
          - "cover"：方案A，使用封面图
          - "frame"：方案B，下载视频并抽帧

        Args:
            media_id: 视频 fid
            customer_id: 客户ID
            mode: 处理模式，None 时使用配置值

        Returns:
            图片路径列表（封面图或帧图片），失败返回空列表
        """
        if not self.enabled:
            self.logger.info("视频处理未启用，跳过")
            return []

        effective_mode = mode or self.video_mode
        self.logger.info(f"视频处理模式: {effective_mode}, fid={media_id}")

        if effective_mode == "frame":
            return self.process_video_frames(media_id, customer_id)
        else:
            # 默认 cover 模式
            return self.process_video_cover(media_id, customer_id)
