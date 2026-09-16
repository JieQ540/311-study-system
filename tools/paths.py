# -*- coding: utf-8 -*-
"""工具脚本共用的路径定义。

为什么要集中：根目录那 6 个大文件（约 150 MB）曾散落在项目根、且被 8 个工具脚本
各自硬编码了一遍绝对路径 —— 一移动就全断。集中到这里后，移动文件只需改一处。

用法：
    from paths import ROOT, RAW, OUTLINE_DOCX, OUTLINE_PDF, SOURCE
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]      # 项目根（本文件在 tools/ 下）
SOURCE = ROOT / "source"
RAW = SOURCE / "原始资料"                        # 大件原始资料（考纲/框架/导图）
DATA = ROOT / "data"

# 311 考纲（docx 是 WPS 的 OCR+版面重建产物，PDF 文字层不可靠，详见 extract_outline.py 说明）
OUTLINE_DOCX = RAW / "01 311考纲（扫描，仅作教学使用）_可搜索(1).docx"
OUTLINE_PDF = RAW / "01 311考纲（扫描，仅作教学使用）_可搜索(1).pdf"

# 其他参考大件（目前未参与数据管线，仅按需取用）
FRAMEWORK_PDF = RAW / "01.311教育学基础考研框架背背加（中外教）.pdf"
FRAMEWORK_BLANK_1 = RAW / "02 311教育学基础考研框架背背加（上册）（挖空版.pdf"
FRAMEWORK_BLANK_2 = RAW / "03 311教育学基础考研框架背背加（下册）（挖空版.pdf"
MINDMAP_PDF = RAW / "26版教育学思维导图.pdf"


def check_exists(*paths):
    """给工具脚本一个友好的前置检查（缺文件时说清期望路径）。"""
    missing = [p for p in paths if not p.exists()]
    if missing:
        raise FileNotFoundError(
            "找不到文件：\n  " + "\n  ".join(str(p) for p in missing)
            + f"\n（原始大件应放在 {RAW}）")
