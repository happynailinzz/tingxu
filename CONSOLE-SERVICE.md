# 听序 macOS 登录常驻

先停止手动运行的管理台，再在项目目录执行：

```bash
python scripts/console-service.py install
python scripts/console-service.py status
python scripts/console-service.py restart
```

安装器根据当前项目路径和 Python 环境生成配置。`install` 写入当前用户的 LaunchAgents 并启用服务；重定位项目后需重新生成配置。

```bash
python scripts/console-service.py stop
python scripts/console-service.py uninstall
```

`stop` 停止当前登录会话的服务；`uninstall` 同时移除下次登录启动配置。日志位于项目根目录 `console-service.log` 和 `console-service-error.log`。

插卡监听每 2.5 秒轮询，拔卡后页面显示未挂载，后台保留以检测下一次插入。拔插间隔至少留 3 秒。睡眠期间不检测，唤醒后恢复。浏览器页面不会随拔卡自动关闭。
