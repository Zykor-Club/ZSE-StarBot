# -*- coding: utf-8 -*-
"""
QQ 群富媒体（图片）上传与发送
官方分片上传流程（本地文件，无需公网 URL）：
  1. 预上传  POST /v2/groups/{group_openid}/upload_prepare
  2. 分片 PUT 到预签名 URL（小图片通常 1 片）
  3. 分片完成 POST /v2/groups/{group_openid}/upload_part_finish
  4. 合并     POST /v2/groups/{group_openid}/files {upload_id} -> file_info
  5. 发送     POST /v2/groups/{group_openid}/messages msg_type=7
"""

import hashlib

import aiohttp

from botpy.http import Route


def _iter_part_chunks(data: bytes, parts: list, block_size: int = 0):
    """按分片顺序切出各片字节（累计偏移，不依赖 index 数值）。

    QQ upload_prepare 返回的 part index 从 1 开始，且 index 不能直接当偏移用：
    按 index 升序逐片累计偏移切片，保证各片首尾相接；
    单片（index=1）时即返回整个文件（此前误按 index*block_size 偏移，
    导致单片上传空内容、合并报"富媒体文件格式不支持"）。
    """
    offset = 0
    ordered = sorted(parts, key=lambda p: int(p.get("index") or 0))
    for part in ordered:
        size = int(part.get("block_size") or block_size or 0)
        if size <= 0:
            size = len(data) - offset
        chunk = data[offset:offset + size]
        offset += len(chunk)
        yield part, chunk


async def upload_group_image(http, group_openid: str, data: bytes, filename: str = "card.png",
                            file_type: int = 1) -> str:
    """上传本地文件到群，返回 file_info（file_type=1 图片，=4 群文件）"""
    file_md5 = hashlib.md5(data).hexdigest()
    file_sha1 = hashlib.sha1(data).hexdigest()
    # 文件前 10002432 字节的 MD5（小文件就是全文件 MD5）
    md5_10m = hashlib.md5(data[:10002432]).hexdigest()
    file_size = str(len(data))

    # 1. 预上传
    prepare = await http.request(
        Route("POST", "/v2/groups/{group_openid}/upload_prepare", group_openid=group_openid),
        json={
            "file_type": file_type,
            "file_size": file_size,
            "file_name": filename,
            "md5": file_md5,
            "sha1": file_sha1,
            "md5_10m": md5_10m,
        },
    )
    upload_id = prepare["upload_id"]

    # 2+3. 逐片 PUT 到预签名 URL，并通知完成（按 parts 顺序逐片累计偏移切片，不依赖 index 数值）
    block_size = int(prepare.get("block_size") or 0)
    async with aiohttp.ClientSession() as session:
        for part, chunk in _iter_part_chunks(data, prepare.get("parts", []), block_size):
            async with session.put(part["presigned_url"], data=chunk) as resp:
                resp.raise_for_status()
            await http.request(
                Route("POST", "/v2/groups/{group_openid}/upload_part_finish", group_openid=group_openid),
                json={
                    "upload_id": upload_id,
                    "part_index": part["index"],
                    "block_size": str(len(chunk)),
                    "md5": hashlib.md5(chunk).hexdigest(),
                },
            )

    # 4. 合并
    merged = await http.request(
        Route("POST", "/v2/groups/{group_openid}/files", group_openid=group_openid),
        json={"file_type": file_type, "upload_id": upload_id},
    )
    return merged.get("file_info") or merged["file_info"]


async def send_group_file(client, group_openid: str, file_path_or_bytes, file_name: str,
                          file_type: int = 4, msg_id: str = None):
    """上传本地文件（群文件，file_type=4）并在群内发送（msg_type=7，返回消息对象）

    file_path_or_bytes 可传文件路径(str)或 bytes；上传流程与 send_group_image 一致（分片→合并→发送）。
    传 msg_id 时按"被动回复"发送；不传则走主动消息（无主动发言权限的群会发送失败）。
    """
    from botpy.types.message import Media

    if isinstance(file_path_or_bytes, (bytes, bytearray)):
        blob = bytes(file_path_or_bytes)
    else:
        with open(file_path_or_bytes, "rb") as f:
            blob = f.read()
    file_info = await upload_group_image(client.http, group_openid, blob,
                                         filename=file_name, file_type=file_type)
    kwargs = {}
    if msg_id:
        kwargs["msg_id"] = msg_id
    return await client.api.post_group_message(
        group_openid=group_openid,
        msg_type=7,
        media=Media(file_info=file_info),
        **kwargs,
    )


async def send_group_image(client, group_openid: str, data: bytes, filename: str = "card.png",
                           msg_id: str = None):
    """上传本地图片并在群内发送（返回消息对象）

    传 msg_id 时按"被动回复"发送（5 分钟有效），不依赖群主的"机器人主动在群聊内发言"权限；
    不传则走主动消息（无该权限的群会报 40034105 发送消息失败, 无权限）。
    """
    from botpy.types.message import Media

    file_info = await upload_group_image(client.http, group_openid, data, filename=filename)
    kwargs = {}
    if msg_id:
        kwargs["msg_id"] = msg_id
    return await client.api.post_group_message(
        group_openid=group_openid,
        msg_type=7,
        media=Media(file_info=file_info),
        **kwargs,
    )


def _selftest():
    """自测：按键值切片必须完整还原原文件（防分片偏移回归）"""
    blob = bytes(range(256)) * (12 * 1024 * 1024 // 256)  # 12MB
    # QQ 返回的 part index 从 1 开始，每片 block_size 为 5MB（末片按实际截断）
    parts = [{"index": i + 1, "block_size": 5 * 1024 * 1024} for i in range(3)]
    chunks = [c for _, c in _iter_part_chunks(blob, parts, 5 * 1024 * 1024)]
    assert [len(c) for c in chunks] == [5 * 1024 * 1024, 5 * 1024 * 1024, 2 * 1024 * 1024], [len(c) for c in chunks]
    assert b"".join(chunks) == blob, "分片切片结果无法还原原文件"
    # 单片（index=1）必须返回整个文件，不能因 index 偏移而切空
    small = blob[:1000]
    one = [c for _, c in _iter_part_chunks(small, [{"index": 1, "block_size": len(small)}], 5 * 1024 * 1024)]
    assert one == [small]
    # 分片缺失 block_size 时按顶层/剩余长度兜底
    two = [c for _, c in _iter_part_chunks(small, [{"index": 1}], 0)]
    assert two == [small]
    print("upload_media 自测通过：分片累计偏移切片可完整还原文件")


if __name__ == "__main__":
    _selftest()