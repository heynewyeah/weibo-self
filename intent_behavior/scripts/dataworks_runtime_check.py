#!/usr/bin/env python3
"""只检查运行依赖和真实视频编解码，不读取配置、不连接业务服务。"""
import importlib
import importlib.metadata
import json
import os
import platform
import sys
import tempfile


def main():
    if sys.version_info < (3, 9):
        raise RuntimeError("需要 Python >= 3.9")
    versions = {}
    for name, package in (("yaml", "PyYAML"), ("requests", "requests"), ("pymysql", "PyMySQL"),
                          ("cv2", "opencv-python-headless"), ("numpy", "numpy")):
        importlib.import_module(name)
        versions[package] = importlib.metadata.version(package)
    from zoneinfo import ZoneInfo
    import cv2
    import numpy as np
    assert str(ZoneInfo("Asia/Shanghai")) == "Asia/Shanghai"
    with tempfile.TemporaryDirectory(dir=".") as temp:
        filename = os.path.join(temp, "check.avi")
        writer = cv2.VideoWriter(filename, cv2.VideoWriter_fourcc(*"MJPG"), 1, (32, 32))
        if not writer.isOpened():
            raise RuntimeError("OpenCV 视频编码器不可用")
        try:
            writer.write(np.zeros((32, 32, 3), dtype=np.uint8))
        finally:
            writer.release()
        reader = cv2.VideoCapture(filename)
        try:
            ok, frame = reader.read()
            if not ok or frame.shape != (32, 32, 3):
                raise RuntimeError("OpenCV 视频解码失败")
        finally:
            reader.release()
    print("DATAWORKS_RUNTIME " + json.dumps({"ok": True, "python": sys.version.split()[0],
          "machine": platform.machine(), "packages": versions, "video_roundtrip": True}))


if __name__ == "__main__":
    main()
