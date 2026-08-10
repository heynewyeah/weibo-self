#!/usr/bin/env python3
"""
意图行为项目 - 博文分类服务主入口

使用方式:
  1. 单条测试:
     python3 main.py --mode single --mid 5250218712893321 --uid 123456 --content "博文内容"

  2. 单条图文测试:
     python3 main.py --mode single --mid 5250218712893321 --uid 123456 --content "博文内容" --pids pid1,pid2

  3. 批量处理 (从TSV文件读取):
     python3 main.py --mode batch --input data.tsv
     TSV格式: mid \t uid \t content \t [pids] \t [media_ids]

  4. API服务模式 (后续扩展):
     python3 main.py --mode server --port 8088
"""

import sys
import os
import argparse
import yaml

# 将 src 目录加入路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.classifier import BlogClassifier
from src.utils import setup_logger


def load_config(config_path: str = "config/config.yaml") -> dict:
    """加载配置文件"""
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def run_single(args, classifier):
    """单条分类"""
    image_pids = args.pids.split(",") if args.pids else None
    video_media_ids = args.media_ids.split(",") if args.media_ids else None

    result = classifier.classify(
        mid=args.mid,
        uid=args.uid,
        content=args.content or "",
        image_pids=image_pids,
        video_media_ids=video_media_ids
    )

    print("\n" + "=" * 50)
    print(f"博文ID:   {result.mid}")
    print(f"用户ID:   {result.uid}")
    print(f"分类结果: {result.layer}")
    print(f"媒体类型: {result.media_type}")
    print(f"是否成功: {'✅ 是' if result.success else '❌ 否'}")
    if result.error:
        print(f"错误信息: {result.error}")
    if args.verbose and result.model_output:
        print(f"模型输出: {result.model_output[:500]}")
    print("=" * 50)


def run_batch(args, classifier):
    """批量分类"""
    input_file = args.input

    if not os.path.exists(input_file):
        print(f"❌ 输入文件不存在: {input_file}")
        sys.exit(1)

    items = []
    with open(input_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            item = {
                "mid": parts[0] if len(parts) > 0 else "",
                "uid": parts[1] if len(parts) > 1 else "",
                "content": parts[2] if len(parts) > 2 else "",
            }
            if len(parts) > 3 and parts[3]:
                item["image_pids"] = [p.strip() for p in parts[3].split(",") if p.strip()]
            if len(parts) > 4 and parts[4]:
                item["video_media_ids"] = [m.strip() for m in parts[4].split(",") if m.strip()]
            items.append(item)

    print(f"共加载 {len(items)} 条数据")
    classifier.classify_batch(items)


def run_server(args, classifier):
    """API服务模式（预留）"""
    print(f"[预留] API服务模式，端口: {args.port}")
    print("后续可使用 Flask/FastAPI 实现，接收 (uid + mid) 的HTTP请求")


def main():
    parser = argparse.ArgumentParser(
        description="意图行为项目 - 博文分类服务",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 纯文本分类
  python3 main.py --mode single --mid 5250218712893321 --uid 123456 --content "14万级全新威兰达..."

  # 图文分类
  python3 main.py --mode single --mid 5250218712893321 --uid 123456 --content "博文文字" --pids pid1,pid2

  # 批量处理
  python3 main.py --mode batch --input data.tsv
        """
    )
    parser.add_argument("--mode", choices=["single", "batch", "server"],
                        default="single", help="运行模式")
    parser.add_argument("--config", default="config/config.yaml", help="配置文件路径")

    # single 模式参数
    parser.add_argument("--mid", default="", help="博文ID")
    parser.add_argument("--uid", default="", help="用户ID")
    parser.add_argument("--content", default="", help="博文文字内容")
    parser.add_argument("--pids", default="", help="图片pid列表，逗号分隔")
    parser.add_argument("--media_ids", default="", help="视频media_id列表，逗号分隔")
    parser.add_argument("--verbose", action="store_true", help="输出模型原始返回")

    # batch 模式参数
    parser.add_argument("--input", default="", help="批量输入TSV文件路径")

    # server 模式参数
    parser.add_argument("--port", type=int, default=8088, help="API服务端口")

    args = parser.parse_args()

    # 加载配置
    config = load_config(args.config)

    # 初始化日志
    logger = setup_logger(
        name="classifier",
        log_dir=config["logging"].get("dir", "logs"),
        level=config["logging"].get("level", "INFO")
    )
    logger.info(f"配置加载完成: {args.config}")
    logger.info(f"模型: {config['api']['model']}")
    logger.info(f"行业: {config['classification']['industry']}")
    logger.info(f"分类层级: {config['classification']['layers']}")

    # 初始化分类器
    classifier = BlogClassifier(config, logger)

    # 路由到对应模式
    if args.mode == "single":
        run_single(args, classifier)
    elif args.mode == "batch":
        run_batch(args, classifier)
    elif args.mode == "server":
        run_server(args, classifier)


if __name__ == "__main__":
    main()
