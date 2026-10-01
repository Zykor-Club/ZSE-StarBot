# -*- coding: utf-8 -*-
r"""部署配置读取：从同目录 deploy_config.json 加载（该文件不入库），示例见 deploy_config.example.json

字段说明：
  host / port / user / key : SSH 连接信息（必填）
  plugin_out_dir           : 本机插件编译输出目录（含 starZSEbot.dll），留空则跳过插件上传
  cai_assets_dir           : CaiBotLite 素材目录（查背包素材打包用）
  bg_dir                   : 背景图目录（查背包素材打包用）
"""
import json
import os

CFG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deploy_config.json")


def load_deploy_config() -> dict:
    if not os.path.exists(CFG_PATH):
        raise SystemExit(
            f"未找到部署配置：{CFG_PATH}\n"
            "请复制 deploy_config.example.json 为 deploy_config.json，并填写 SSH 连接信息后重试"
        )
    with open(CFG_PATH, "r", encoding="utf-8") as fp:
        return json.load(fp)