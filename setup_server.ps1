# 服务器一键安装依赖脚本（部署用）
$py = 'C:\Program Files\Python313\python.exe'
& $py -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -q qq-botpy==1.2.1 PyYAML Pillow aiohttp
& $py -c "import botpy, PIL, yaml, aiohttp; print('DEPS_OK')"