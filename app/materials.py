"""冻结资料目录的单一读取入口；不写库，不执行资料中的指令。"""
import hashlib
import json
from pathlib import Path


def digest(data):
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def load_bundle(directory, bundle_name, manifest_name="delivery_manifest.json"):
    root = Path(directory).resolve()
    manifest = json.loads((root / manifest_name).read_text(encoding="utf-8"))
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise ValueError("资料交付清单为空。")
    verified = set()
    for item in entries:
        name = item.get("path")
        if not isinstance(name, str) or not name or name.replace("\\", "/") in verified:
            raise ValueError("交付文件路径为空或重复。")
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("交付路径越界或文件缺失。")
        if hashlib.sha256(path.read_bytes()).hexdigest() != item.get("sha256"):
            raise ValueError("交付文件已变化，需重新审校版本。")
        verified.add(name.replace("\\", "/"))
    if bundle_name.replace("\\", "/") not in verified:
        raise ValueError("冻结题包不在校验清单。")
    bundle = json.loads((root / bundle_name).read_text(encoding="utf-8"))
    if not isinstance(bundle, dict):
        raise ValueError("冻结资料格式不正确。")
    original = bundle.get("freeze_sha256")
    canonical = {k: v for k, v in bundle.items() if k != "freeze_sha256"}
    if not original or digest(canonical) != original or manifest.get("content_freeze_sha256") != original:
        raise ValueError("资料内容指纹不一致。")
    return bundle, manifest, root
