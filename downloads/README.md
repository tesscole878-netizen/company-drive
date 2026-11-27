# 员工客户端安装包

将构建好的 `CompanyDriveSetup.exe` 放在本目录，登录用户即可在网页“连接电脑”中下载。Docker 构建时会复制此目录。

也可通过环境变量 `DRIVE_CLIENT_INSTALLER` 指向服务器上的另一个固定安装包路径。不要放账号配置、数据库或设备密码到这个目录。
