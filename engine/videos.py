"""视频接收：大小/格式校验、流式写入、sha256。"""
from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path
from typing import Any

from .accounts import ApiError, now_iso
from .config import Config

MAX_BYTES = 500 * 1024 * 1024
ALLOWED_EXT = {".mp4", ".mov"}


def decode_upload_filename(encoded: str | None) -> str:
    """解码 x-filename（浏览器头只能放 ByteString，中文文件名需 encodeURIComponent）。"""
    from urllib.parse import unquote
    raw = unquote(str(encoded or "")).strip()
    if not raw:
        return "video.mp4"
    # 只保留文件名部分，防御路径穿越
    raw = raw.replace("\\", "/").split("/")[-1]
    return raw[:200]


def videos_dir(config: Config) -> Path:
    path = config.paths.temp / "videos"
    path.mkdir(parents=True, exist_ok=True)
    return path


def check_meta(filename: str, size: int | None = None) -> str:
    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        raise ApiError(1013, "不支持的视频格式：" + (ext or "(无扩展名)") + "；仅允许 mp4/mov", 400)
    if size is not None and size > MAX_BYTES:
        raise ApiError(1013, "视频过大：" + str(size) + " 字节；上限 500MB", 400)
    return ext


def safe_upload_path(config: Config, filename: str, ext: str) -> Path:
    """保留原始文件名（清理非法字符），重名时加短后缀；避免上传页只显示 UUID。"""
    folder = videos_dir(config)
    stem = Path(filename or "video").stem
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" .")[:100] or "video"
    candidate = folder / (stem + ext)
    if candidate.exists():
        candidate = folder / (stem + "-" + uuid.uuid4().hex[:6] + ext)
    return candidate


class UploadSink:
    def __init__(self, config: Config, filename: str, expected_size: int | None = None):
        self.config = config
        self.filename = filename or "video.mp4"
        self.ext = check_meta(self.filename, expected_size)
        self.video_id = uuid.uuid4().hex
        self.path = safe_upload_path(config, self.filename, self.ext)
        self._hash = hashlib.sha256()
        self.size = 0
        self._handle = self.path.open("wb")
        self._finished = False

    def write(self, chunk: bytes) -> None:
        if self._finished:
            return
        self.size += len(chunk)
        if self.size > MAX_BYTES:
            self.abort()
            raise ApiError(1013, "视频超过 500MB 上限，已中止", 400)
        self._hash.update(chunk)
        self._handle.write(chunk)

    def finish(self) -> dict[str, Any]:
        self._handle.close()
        self._finished = True
        if self.size <= 0:
            self.abort()
            raise ApiError(1013, "视频为空", 400)
        return {
            "video_id": self.video_id,
            "path": str(self.path),
            "sha256": self._hash.hexdigest(),
            "size_bytes": self.size,
            "filename": self.filename,
            "ext": self.ext,
            "received_at": now_iso(),
        }

    def abort(self) -> None:
        try:
            self._handle.close()
        except Exception:
            pass
        self._finished = True
        try:
            self.path.unlink()
        except OSError:
            pass


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_video(config: Config, path: str, sha256: str) -> dict[str, Any]:
    file_path = Path(path or "")
    if not file_path.is_file():
        raise ApiError(1013, "视频文件不存在", 400)
    size = file_path.stat().st_size
    if size > MAX_BYTES:
        raise ApiError(1013, "视频超过 500MB 上限", 400)
    check_meta(file_path.name)
    actual = sha256_file(file_path)
    expected = str(sha256 or "").strip().lower()
    if expected and actual != expected:
        raise ApiError(1013, "视频 sha256 校验失败（文件可能被改动）", 400)
    return {"path": str(file_path), "sha256": actual, "size_bytes": size}


def cleanup(path: str | Path) -> None:
    try:
        Path(path).unlink()
    except OSError:
        pass
