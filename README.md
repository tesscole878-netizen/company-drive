# 苏盐新云盘 0.4.2

面向自备 NAS 的公司共享云盘。应用管理账号、资料库和访问权限，网页与 WebDAV 电脑盘访问同一批文件。

## 当前开发位置

项目已迁移到 **`D:\IdeaProjects\company-drive`**，后续从这个目录开发与运行。

- `drive/`：云盘服务与网页；`client/`：Windows 连接助手源码；`tests/`：自动化测试。
- `.venv/`：在新目录重新创建的 Python 开发环境。
- `releases/windows/` 存放员工安装包、便携 ZIP 和安装说明。当前源码部署包为 `releases/company-drive-v0.4.2.zip`，内含 Linux 正式部署模板和 Windows 连接助手 0.2.6 安装包。
- 双击 `start.cmd` 或 `start-preview.cmd` 启动本地开发实例，访问地址为 <http://127.0.0.1:8765>。`state/` 和 `work/preview-drive-data/` 保留迁移前备份。
- `docs/`：试用说明与界面截图；`work/`：中间文件、测试工具和构建产物。

## 当前 Linux 正式部署

2026 年 11 月 12 日，正式入口从 82 切换为 <https://192.168.80.252:8443>。使用原 Seafile 账号或本地 `admin` 登录，原员工身份、群组权限、设备密码和部署授权随最新数据库一起保留。原 Seafile 资料通过接口按原权限只读访问。

| 位置 | 用途 |
| --- | --- |
| `/opt/suyan-cloud-drive` | 正式 Compose 配置、程序和运行目录 |
| `/opt/suyan-cloud-drive/state` | 正式 SQLite 数据库和服务器密钥 |
| `/data/suyan-cloud-drive` | 正式文件、上传暂存、回收站和版本，应用直接访问 |
| `/opt/suyan-cloud-drive/private` | 员工目录凭据、账号映射和服务环境配置 |
| `/opt/suyan-cloud-drive/tls` | 沿用试跑的 HTTPS 证书及私钥 |
| `/opt/suyan-cloud-drive/office` | 独立文档服务的持久化目录 |

在部署目录执行 `docker compose ps` 查看 `drive`、`portal`、`gateway`、`office` 四个正式容器。模板在 `deploy/linux/compose.production.yaml`，服务配置了自动重启和资源上限。gateway 的 8443 提供网页、共享链接和 WebDAV，Office 使用 HTTPS 8444，portal 的 HTTPS 9443 和应用的 8765 仅用于内部连接。原 Seafile 的 80/443 由原服务使用。

Linux 应用 UID/GID 为 19555，与正式文件原属主一致。员工目录配置 `DRIVE_IDENTITY_READ_ONLY=0`，新增、改密及停用继续作用于统一账号目录。正式切换保留 82 的最新数据库和服务器密钥，试跑副本留存归档。

Windows 连接助手安装包已预填正式地址。已有用户在助手中把服务器地址改为 `https://192.168.80.252:8443` 后重新连接。电脑需信任该入口证书，已安装试跑证书的电脑可继续使用。切换后已实测 HTTPS WebDAV 读写、复制、移动、权限隔离和 Windows 临时盘挂载，以及网页、Office 预览和编辑保存。

切换备份位于 `/opt/suyan-cloud-drive/backups/cutover-20261112-090158` 和 `D:\Services\SuyanCloudDrive\backups\linux-cutover-20261112-090158`。恢复需先停止当前写入，备份并同步最新数据库与服务器密钥，停止 82 的跳转服务后再启用旧云盘服务，保持文件和权限元数据一致。

## 原 Windows Server 部署与回退

原 HTTP 入口 <http://192.168.70.82:8765> 和 HTTPS 入口 <https://192.168.70.82/> 已由新服务 `SuyanCloudRedirect` 引导到正式地址。旧入口写入请求返回迁移提示，客户端需修改地址。跳转服务目录为 `D:\Services\SuyanCloudDrive\cutover-redirect`，配置为自动启动。

原 `SuyanCloudDrive` 和 `SuyanLegacyPortal` 服务保持停止并禁用自动启动，程序和状态目录保留用于回退。

| 服务器目录 | 用途 |
| --- | --- |
| `D:\Services\SuyanCloudDrive\app` | 云盘程序与 Windows 连接助手安装包 |
| `D:\Services\SuyanCloudDrive\runtime` | 独立 Python 运行环境 |
| `D:\Services\SuyanCloudDrive\state` | SQLite 数据库、服务器密钥和暂存文件 |
| `D:\Services\SuyanCloudDrive\logs` | 服务与应用日志 |
| `D:\Services\SuyanCloudDrive\backups` | 上线前的数据库及状态备份 |
| `192.168.80.252:/data/suyan-cloud-drive` | 正式文件、上传暂存、回收站和版本 |
| `D:\CompanyDriveNAS` | 切换 Linux 存储前的文件副本 |

内网防火墙规则允许 `192.168.0.0/16` 访问 TCP 8765。升级时先停止服务，备份 `state` 和文件存储，再替换 `app` 并启动。状态备份须保留 `drive.sqlite3` 和 `server.key`；应用运行时使用 SQLite 备份接口获取一致的数据库副本。

原 Windows 部署通过 SMB3 共享 `\\192.168.80.252\SuyanCloudData` 访问文件。Linux 上 `suyan-cloud-storage` 容器只挂载 `/data/suyan-cloud-drive`，仅接受 `192.168.70.82` 的认证连接。容器配置在 `deploy/storage`，服务器上的构建目录为 `/opt/suyan-cloud-storage`。旧 Windows 凭据仍由 DPAPI 保护，保留在 `private-storage`。当前 Linux 应用直接访问文件目录。原 Seafile 和新云盘共用 `/data` 的物理容量，各自管理目录。

### 旧资料库

员工入口为 <https://192.168.80.252:8443>。在信任内网证书后，员工使用原 Seafile 账号和密码直接登录。页面显示我的资料库、共享给我的、公共资料库及所属群组。浏览和下载的权限实时取自原 Seafile。加密资料库仍在 Seafile 打开。

同一 HTTPS 地址支持本地系统管理员 `admin` 登录，进入完整的新云盘管理界面。成员、群组和授权管理使用现有应用服务。员工账号的收藏夹、文件活动、已发布资料库、已连接设备、共享管理和通知从 Seafile 读取。通知保持原来的已读状态；文件活动取决于旧服务的接口是否可用。

文件名鼠标悬停时显示下划线。点击文件可打开只读预览，支持 Office、PDF、常见图片和文本格式。Office 文件通过本机 ONLYOFFICE 转换为 PDF，预览最大 50 MB，文本最大 2 MB；其他格式和超出限制的文件可下载查看。

HTTPS 入口由 Linux 的 gateway 和 portal 提供，证书沿用已验证的试跑证书，私钥仅保留在服务器。82 原 HTTPS 证书由跳转服务继续使用。portal 重启后员工需要重新登录。

员工使用同一套 Seafile 账号，登录后直接进入新工作区。「我的资料库」「共享给我的」「公共资料库」和「群组共享」同时展示新资料库和当前账号有权访问的原资料库。原资料库可只读浏览和下载，文件仍存放在 Seafile。新增员工、改密及停用使用统一目录。新文件可按用户或原群组共享，原群组的成员关系由 Seafile 管理。加密资料库和仅允许预览的内容在 Seafile 中打开。

电脑盘根目录中的「原 Seafile」支持编辑已有文件，按该账号的原 Seafile `rw` 权限写回。服务器在接收完整内容后复查权限与版本，再用该用户令牌获取更新地址并提交至原服务。Word、Excel 保存涉及的临时备份只存于新云盘状态目录的设备隔离暂存区。直接保存或通过临时文件替换时，零字节 Office 文件返回 422，保留原文件。原 Seafile 历史版本由更新接口保留，设备读取的版本记录在 `legacy_dav_reads`，过期版本写入返回 409。`LEGACY_DAV_WRITEBACK=0` 可关闭该入口写回。新建、删除和普通改名在原 Seafile 操作。

网页「我的资料库」中的原 Seafile 库支持由库主重命名、删除整库、向员工或原群组共享和撤销共享。原库的未加密文件夹也支持重命名和管理内部共享。每次操作重新检查原库所有者和所需权限，并使用该员工的原 Seafile 身份。删除整库前会核对确认框内的库名与版本。原文件及子文件夹删除沿用原系统入口，原库文件条目不提供可验证的个人归属。

网页上传、电脑盘保存与文件替换、在线文档保存结果如果收到 0 字节 Office 内容，会拦截对现有文档的覆盖。服务器在状态数据库的 `blocked_office_writes` 中记录拦截时间、账号、已授权电脑名称、资料库、路径和请求入口。电脑名称用于核对请求来源，无法识别电脑上发起文件操作的程序。空文本文件仍可正常保存。

系统管理员 `admin` 使用新云盘原密码。员工统一账号通过 HTTPS 登录，原有本地员工首次使用当前密码登录时完成迁移。

服务配置 `LEGACY_SEAFILE_URL` 使用旧服务的 HTTPS 地址，`LEGACY_SEAFILE_CERT_SHA256` 使用经验证的旧服务证书 SHA-256 指纹。旧证书更换后更新该值并重启新云盘服务。

## Linux 试跑归档

原试跑地址 <https://192.168.80.252:8443> 已由正式部署接管。下列试跑目录保留归档，试跑容器保持停止，文件副本独立保留。

| 位置 | 用途 |
| --- | --- |
| `/opt/suyan-cloud-drive-trial/app` | Linux 容器的程序构建目录 |
| `/opt/suyan-cloud-drive-trial/state` | 独立 SQLite 数据库和保留的服务器密钥 |
| `/data/suyan-cloud-drive-trial` | 新云盘文件的独立试跑副本 |
| `/opt/suyan-cloud-drive-trial/private` | 员工目录凭据、账号映射和服务环境配置 |
| `/opt/suyan-cloud-drive-trial/tls` | 试跑证书和 TLS 私钥 |
| `/opt/suyan-cloud-drive-trial/office` | 独立文档服务的缓存、数据库和日志 |

Compose 模板在 `deploy/linux/compose.trial.yaml`。归档项目与正式项目使用相同的 8443/8444 端口，恢复试跑时需先调整映射或停止占用端口的正式服务。

数据库通过 82 的 SQLite 在线备份生成，文件复制自 `/data/suyan-cloud-drive`。员工原资料通过现有 Seafile 接口按各自权限只读访问。试跑配置 `DRIVE_IDENTITY_READ_ONLY=1`，成员新增、改密和停用在此入口受限。

Linux 员工目录凭据通过 `DRIVE_IDENTITY_CREDENTIAL_FILE` 指向只读挂载的 JSON 文件，属主需为服务用户或 root，禁止组和其他用户访问。归档试跑 UID 为 10001，正式服务 UID 为 19555，凭据权限均为 0400。Windows 留存凭据使用 DPAPI 格式。凭据、环境密钥、数据库和 TLS 私钥均独立保管。

独立 ONLYOFFICE 使用服务器已有的 8.2.1 镜像创建新容器，缓存与数据库使用试跑专属目录。云盘和文档服务共享该试跑容器的网络，内部地址使用回环接口。`LEGACY_PREVIEW_LOOPBACK_CONVERTER=1` 为旧版文档服务启用 URL 转换，源文件仅在转换期间通过回环接口提供。原 Windows 的文件上传转换方式继续适用于 Docs 10。

## 已实现

- 授权管理。首次部署试用 90 天，到期后保留查看和下载，暂停上传及修改。管理员通过软件提供方签发的离线激活码续期。
- 网页和 Windows 客户端采用深蓝灰配色，使用苏盐标志原图。网页标签图标、客户端与安装包图标均使用同一标志。授权详情和激活操作仅管理员可访问。
- 成员在右上角头像菜单中修改自己的密码。管理员在“成员管理”中重置其他成员密码，改密后需要重新登录并重新连接电脑盘。
- 登录账号可用中文姓名，支持 2–40 位中文、字母、数字及 `._-`，登录密码至少 6 位。弹窗在点击外部或按 Esc 时保留输入内容，右上角叉号用于关闭。
- 我的资料库、共享给我的、公共资料库和群组共享。个人可创建资料库。管理员可创建公共资料库，供已登录员工浏览。旧分组自动成为对应群组的共享资料库。
- 文件夹和资料库可共享给用户或群组。共享链接、上传收件链接和资料库发布支持撤销，普通外链支持密码与过期时间。共享管理显示授权、链接状态和访问次数。
- 列表、格点、分栏、属性、按名称或标签搜索、收藏、评论、公告、通知和设备活动记录。
- 支持目录上传、文件覆盖留存旧版本、批量复制与移动、ZIP 下载、资料库快照、恢复已删除资料库。资料库属性按需统计。
- 支持新建 Markdown、文本、Word、Excel 和 PPT。文本可直接编辑，Office 在线编辑通过可选的 ONLYOFFICE Docs 接入。
- 加密资料库使用独立密码解锁，已提交文件和历史版本的内容使用 AES-GCM 加密。解锁状态按账号保留一小时，重启后需重新解锁。文件名称和目录信息可见。待提交文件在服务端状态目录暂存，配置该目录时可选用加密卷。加密资料库只支持授权成员访问。
- 首次初始化管理员，无内置默认密码；员工账号创建、停用和密码修改。
- 分组共享：一个成员加入多个分组；每组可分配“只读”或“可编辑”。管理员可访问所有组。
- 网页文件夹、目录浏览、当前文件夹搜索、重命名、下载与多文件上传。
- 4MB 分片上传、失败重试、重新选择原文件后续传；完成后服务端计算 SHA-256，并在最终提交前隐藏未完成文件。
- 网页和 WebDAV 删除均进入回收站；有编辑权限的组员可以恢复。
- WebDAV：目录枚举、读写、范围下载、MKCOL、MOVE、COPY、DELETE、LOCK/UNLOCK。写入先暂存，再原子替换现有文件。
- 每台电脑使用单独的连接密码，可撤销；分组访问权限每次操作重新检查。
- 文件操作记录（上海时区）；存储卷可用空间展示。
- 中文桌面与移动网页；无需外部字体、CDN 或第三方云服务。

## 本地 Windows 启动

需要 Python 3.12 或更新版本，首次安装依赖需要联网。

1. 双击 `start.cmd`，或在项目目录运行它。保持启动窗口打开。
2. 打开 <http://127.0.0.1:8765>。
3. 首次打开时，从 `data/state/setup.key` 复制初始化密钥，设置自己的管理员账号及长密码。
4. 新建分组；在“成员管理”创建账号，再在分组“管理成员”中添加用户及权限。
5. 上传文件，或打开“连接电脑”设置挂载。

默认只在本机监听，不会自动修改防火墙、开放公网或接入任何真实 NAS。

`start.cmd` 默认使用本地 `data/`，`start-preview.cmd` 默认使用本地 `work/dev-drive-data/`。可用 `DRIVE_DATA_DIR` 和 `DRIVE_STATE_DIR` 显式指定独立的测试存储，也可通过当前机器的 `storage.local.json` 配置。源码发布包会排除该配置。开发实例应使用独立于正式服务的文件和数据库。

Linux/macOS 可运行：

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python run.py
```

## NAS / Docker 部署

支持能运行 Python/Docker 的服务器；已有 NAS 也可以作为存储后端挂载给独立的应用服务器。硬件型号和 NAS 系统尚未指定，因此这里提供通用部署包。

1. 将项目复制到服务器；将 `.env.example` 复制为 `.env`。
2. `NAS_DATA_PATH` 指向用于此应用的专用数据目录；请勿指向包含其他业务数据的整个 NAS 根目录。
3. `LOCAL_STATE_PATH` 放到应用服务器本地持久化磁盘（SQLite WAL 数据库不要放在 SMB/NFS 共享上）。
4. 挂载目录需要允许容器内 UID/GID `10001:10001` 读写。Linux 下先由管理员创建目录并分配相应所有权。
5. 在项目目录运行 `docker compose up -d --build`。
6. 从 `LOCAL_STATE_PATH/setup.key` 取得初始化密钥，通过页面创建管理员。

已有部署升级时，先停止旧服务，备份 NAS 文件和本地状态目录，再启动 0.4.0。新程序启动会自动增加资料库、共享和授权等表。原分组 ID 和文件路径继续使用，Windows 连接助手仍可使用已有账号和连接。

```text
浏览器 ──────────┐
                ├─ 云盘服务（统一账号和分组） ─ NAS 专用数据目录
电脑盘客户端 ────┘                  └─ 本地持久化数据库
```

配置项：

| 变量 | 默认值 | 含义 |
|---|---|---|
| `DRIVE_DATA_DIR` | `data` | Python 直接启动时的文件数据根目录 |
| `DRIVE_STATE_DIR` | 数据目录下 `state` | 数据库与首次初始化密钥目录 |
| `DRIVE_HOST` | `127.0.0.1` | Python 直接启动监听地址 |
| `DRIVE_PORT` | `8765` | 应用端口 |
| `DRIVE_MAX_FILE_GB` | `100` | 单文件上限，按 GiB 计算 |
| `DRIVE_SECURE_COOKIE` | `0` | 正式 HTTPS 部署设为 `1` |
| `DRIVE_TRUST_PROXY` | `0` | 仅可信代理后、直连入口受限时启用 |
| `DRIVE_BIND` | `127.0.0.1` | Compose 宿主机绑定地址 |

### 公司内外访问

电脑盘使用 HTTPS WebDAV。对外访问需要实际域名、证书、可达的网络入口或公司 VPN。

- 本地 HTTP 仅用于回环试用。正式外网入口使用 HTTPS；开启 `DRIVE_SECURE_COOKIE=1`。
- `Caddyfile.example` 提供独立反向代理的示例；如使用 NAS 自带代理，应将同一个域名的全部路径原样转发，包括 `/api`、`/assets`、`/dav`。
- 代理需要放行 WebDAV 方法，并保留 `Authorization`、`Destination`、`If`、`Lock-Token`、`Range` 请求头。`/dav` 前缀不能被去掉。
- WebDAV 的 Basic 凭证由 HTTPS 保护。不要公开 HTTP WebDAV、NAS 管理端或 SMB 端口。
- 对超大文件，应核对代理超时、临时空间、NAS 网络带宽和磁盘空间。

## Windows 映射为 Z: 盘

推荐员工使用已打包的 **Windows 连接助手**：登录网页 → “连接电脑” → 下载并安装助手 → 输入云盘地址、账号和网页登录密码 → “登录并连接”。设备连接密码由助手自动管理，员工不用执行命令。

首次使用按助手提示安装 WinFsp，Windows 会要求管理员确认。公司 IT 可提前统一安装 WinFsp。连接助手与 rclone 已一起打包，员工无需安装 Python。客户端源码与管理员预填公司地址的方法见 `client/README.md`。

网页安装包放在 `downloads/CompanyDriveSetup.exe`，默认所有登录用户均可下载；Docker 镜像构建时会复制该目录。可用环境变量 `DRIVE_CLIENT_INSTALLER` 指定另一个固定文件路径。源代码包自带的客户端是未签名测试版，正式分发可由公司 IT 签名和试装。

### 高级：手动挂载

第一版复用 [rclone](https://rclone.org/downloads/) 与 [WinFsp](https://winfsp.dev/)，不用开发内核驱动。

1. 安装 rclone 和 WinFsp，让 `rclone` 可从终端运行。
2. 登录网页，点击“连接电脑”，填设备名称，生成连接密码。密码只显示一次。
3. 运行 `rclone config`，选择新建 remote，名称设为 `company`，存储类型选择 `webdav`。
4. URL 填 `https://你的域名/dav/`，vendor 选择 `other`，user 填云盘账号，pass 在交互密码输入处填网页生成的连接密码。其他选项按默认值即可。
5. 运行：

```sh
rclone mount company: Z: --vfs-cache-mode full --network-mode --dir-cache-time 30s --vfs-cache-max-size 20G --vfs-write-back 3s
```

也可以运行项目里的 `scripts/mount-drive.ps1`，它会检查 remote 和盘符是否可用。默认 remote 为 `company`，盘符为 `Z:`。

此时在资源管理器中浏览分组，拖入文件上传，使用本地软件打开并保存。**保存先进入本地缓存，再上传；等待上传完成后再退出客户端。** 缓存配置中的 20G 是软目标，打开文件或尚未上传的文件可能暂时超出。

这是一种网络挂载盘。它与带云朵状态图标、离线固定、自动冲突合并的原生按需同步客户端不同。未缓存文件需要联网读取。本地程序是否适合通过网络盘工作，还需用公司的真实文件验证。

电脑盘支持 DAV 锁和条件请求。通用客户端可能省略这些请求，协同修改同一个 Office 文档时使用在线编辑器。

成员移出分组、停用或撤销设备密码后，会阻止后续服务端访问；已下载的本地副本无法通过服务端收回。

## 文件与备份

```text
data/
  files/<分组ID>/普通文件与目录
  uploads/<上传任务ID>      # 未完成分片；DAV 暂存文件也在此
  trash/<回收站记录ID>     # 软删除内容
  versions/<版本ID>       # 历史文件和快照引用的文件
state/
  drive.sqlite3           # 账号、权限、回收站索引、日志
  server.key              # 密码外链访问凭证签名密钥
  staging/                # 加密资料库待提交文件和导出暂存
  drive.sqlite3-wal       # 运行中可能存在，不能随意删除
  drive.sqlite3-shm
```

配置分离时数据库在 `DRIVE_STATE_DIR`，默认本地启动时在 `data/state/`。备份时保留数据目录和状态目录。快照恢复会先保存当前状态，并撤销路径级授权和外链，避免历史内容被新共享对象看到。通过云盘网页或电脑盘修改资料，确保权限和版本记录同步。

可靠的简单备份方式：安排停机窗口，等待电脑上传完成，停止应用，备份整个数据目录、状态目录和部署配置到另一台设备或备份系统，再启动应用。回收站不是独立备份。100TB 数据应采用适合 NAS 的增量备份和快照工具，备份位置与恢复目标需要另行配置。

正式新云盘通过 `suyan-cloud-drive-backup.timer` 每天北京时间 03:00 备份。归档存放在 `/opt/suyan-cloud-drive/backups/daily`，保留 14 天，包含新云盘文件、历史版本、回收站、SQLite、服务器密钥和部署配置。每次解包后校验数据库及历史记录引用的文件。恢复时应先在隔离目录解包并验证，再按同一份归档恢复文件和状态；恢复后的客户端需要重新发起未完成的上传。该归档存放在同一台服务器的另一块磁盘，原 Seafile 和异地备份由各自备份方案负责。

可以用以下命令导出一致的 SQLite 数据库快照，但它**不包含文件内容**，也不能单独保证与正在变化的文件处在同一时间点：

```sh
python scripts/manage.py export-database --output backups/drive-snapshot.sqlite3
```

恢复验证：在隔离目录还原文件、回收站和数据库，使用新的测试端口启动，检查账号、分组、下载内容及回收站恢复。不要先覆盖唯一一份现有资料。

未完成的网页上传保留供续传，可在停机后运行 `python scripts/manage.py cleanup-uploads` 清理超过 7 天未更新的任务。回收站不自动清空。

忘记密码时，在服务器项目目录运行 `python scripts/manage.py reset-password --username 账号`，密码通过隐藏输入获取，不出现在命令参数中。重置会撤销该账号的登录会话和电脑连接密码。

## 测试和接口

```sh
pip install -r requirements-dev.txt
python -m pytest tests -q
```

测试覆盖：文件完整下载、续传、过早完成拒绝、同名冲突、文件夹/重命名/回收站、路径穿越、CSRF、管理员限制、只读与组间隔离、DAV 目录/读写/范围下载/移动/复制/锁定、成员移出、账号停用、设备撤销、密码更新、重启持久化与一次性初始化。

- API 文档：`/api/docs`；OpenAPI：`/api/openapi.json`。
- 网页 API 使用 HttpOnly 会话 Cookie；修改请求还需要从登录或 `/api/me` 获取的 `csrf`，放在 `X-Drive-CSRF` 请求头。
- DAV 使用账号 + 可撤销设备密码，通过 HTTP Basic 认证；网页登录密码不能用于 DAV。
- 默认单进程运行。DAV 锁和临时属性在内存中，重启会失效。不要启动多个应用 worker 或同时让两台应用写同一数据目录。

## Office 在线编辑

正式云盘已在同一台 Windows 服务器部署 ONLYOFFICE Docs Community 10.0.0，目录为 `D:\Services\OnlyOfficeDocs`，内网端口为 8080。员工从云盘文件预览页点「打开在线编辑器」。Word 双人协作已验证实时同步、关闭后写回和保留旧版本。Excel 和 PPT 已验证编辑器可以打开。

维护时在 Windows 服务中查看 `DsDocServiceSvc`、`DsConverterSvc` 和 `DsProxySvc`，用 `/healthcheck` 检查 8080 端口。Windows Server 2016 的 `certutil` 可能生成带空格的资源编号，当前安装的 `documentserver-flush-cache.bat` 已处理这种格式。升级后如编辑器资源路径出现空格，可用该脚本的 `-h` 参数传入 32 位十六进制编号刷新资源缓存。

在可被员工浏览器和云盘服务访问的位置部署 ONLYOFFICE Docs。云盘和文档服务需要配置同一个 JWT 密钥。应用收到签名保存回调后，将编辑结果写回原文件并保存旧版本。

| 配置 | 值 |
| --- | --- |
| `OFFICE_PUBLIC_URL` | 员工浏览器访问文档编辑器的地址，仅填写协议、域名和端口 |
| `OFFICE_DRIVE_URL` | 文档服务访问云盘文件和保存回调的地址 |
| `OFFICE_RESULT_URL` | 云盘下载保存结果时使用的文档服务地址，默认与 `OFFICE_PUBLIC_URL` 相同 |
| `OFFICE_JWT_SECRET` | 两端相同的至少 32 字节随机密钥 |
| `OFFICE_JWT_HEADER` | 文档服务发送 JWT 的请求头，默认 `Authorization` |

配置后，普通资料库中的 DOCX、XLSX 和 PPTX 会显示“打开在线编辑器”，单文档限 100 MB。公司外访问时，员工浏览器需要能通过 HTTPS 访问文档服务。反向代理须保留 `/office/` 路径和 JWT 请求头。在线编辑验证覆盖签名下载、保存回调、上游失败和版本保留。真实编辑器的多人协作需在部署后验证。[ONLYOFFICE 回调文档](https://api.onlyoffice.com/docs/docs-api/usage-api/callback-handler)

## 授权激活

首次部署启动 90 天试用。在网页“授权管理”查看到期时间和部署编号。续期时由软件提供方签发激活码，管理员粘贴后立即生效。到期后保留查看、下载和导出，暂停上传、文件修改、删除、共享配置修改及 Office 保存。

授权码按部署编号签发，可指定有效天数或上海时区的到期日期。部署时保留本机状态数据库，正常重启、升级和数据库恢复沿用原授权。软件提供方的签发私钥单独保管，部署包只包含 Ed25519 验签公钥。离线部署的本机时间和程序文件受服务器管理员控制，存在被修改或恢复旧状态而绕过的限制。

## 当前验证范围

已在本地验证网页和 WebDAV。截图使用示例资料。真实 NAS、100TB 容量、500 人并发、Windows 挂载驱动和外网访问仍需现场验证。

跨库搜索按文件名和标签匹配。文件名搜索读取当前目录内容，每次最多检查 20,000 个条目，网页每页最多显示 100 条结果，可继续搜索后面的条目。标签搜索使用数据库索引。资料库属性单次最多统计 100,000 个文件。大量文件集中在单个目录时，应使用真实数据评估列表速度。Windows 客户端通过 WebDAV 挂载，Office 在线协作需要独立的文档服务。

网页批量下载在后台打包，打包期间显示进度并支持取消。完成的 ZIP 保留 15 分钟，服务重启后需要重新打包。单任务默认最多 10 GB、10,000 个文件，服务同时处理最多两个打包任务。

依赖的许可证见各上游项目；自研源码在交付目录内，可继续修改与部署。
