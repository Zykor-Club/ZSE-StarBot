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


async def upload_group_image(http, group_openid: str, data: bytes, filename: str = "card.png") -> str:
    """上传本地图片到群，返回 file_info"""
    file_md5 = hashlib.md5(data).hexdigest()
    file_sha1 = hashlib.sha1(data).hexdigest()
    # 文件前 10002432 字节的 MD5（小文件就是全文件 MD5）
    md5_10m = hashlib.md5(data[:10002432]).hexdigest()
    file_size = str(len(data))

    # 1. 预上传
    prepare = await http.request(
        Route("POST", "/v2/groups/{group_openid}/upload_prepare", group_openid=group_openid),
        json={
            "file_type": 1,
            "file_size": file_size,
            "file_name": filename,
            "md5": file_md5,
            "sha1": file_sha1,
            "md5_10m": md5_10m,
        },
    )
    upload_id = prepare["upload_id"]

    # 2+3. 逐片 PUT 到预签名 URL，并通知完成
    async with aiohttp.ClientSession() as session:
        for part in prepare.get("parts", []):
            part_size = int(part.get("block_size") or len(data))
            chunk = data[:part_size]
            async with session.put(part["presigned_url"], data=chunk) as resp:
                resp.raise_for_status()
            await http.request(
                Route("POST", "/v2/groups/{group_openid}/upload_part_finish", group_openid=group_openid),
                json={
                    "upload_id": upload_id,
                    "part_index": part["index"],
                    "block_size": str(part_size),
                    "md5": hashlib.md5(chunk).hexdigest(),
                },
            )

    # 4. 合并
    merged = await http.request(
        Route("POST", "/v2/groups/{group_openid}/files", group_openid=group_openid),
        json={"file_type": 1, "upload_id": upload_id},
    )
    return merged.get("file_info") or merged["file_info"]


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