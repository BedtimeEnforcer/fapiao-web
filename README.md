<p align="center"><img src="web/assets/app-icon.png" width="112" alt="报销助手图标"></p>
<h1 align="center">报销助手</h1>
<p align="center">把散落的发票整理成清晰的报销资料与出差行程。</p>
<p align="center">
  <a href="https://github.com/BedtimeEnforcer/fapiao-web/releases/latest">下载 Windows 便携版</a> ·
  <a href="docs/ARCHITECTURE.md">架构与数据流</a> ·
  <a href="docs/PRIVACY.md">数据与隐私说明</a>
</p>

报销助手是一款在 Windows 本机运行的发票整理工具。它将导入、分类筛选、信息补充、抬头核对、批量重命名、PDF 合并和交通住宿行程单放在同一工作台中。后端使用 Python、FastAPI 和 SQLite，前端为原生 HTML/CSS/JavaScript，无需云端账号或在线大模型服务。

## 下载与使用

在 [Releases](https://github.com/BedtimeEnforcer/fapiao-web/releases) 下载 `报销助手.exe`，双击运行，无需安装 Python。当前便携版面向 **Windows 10/11 x64**，首次启动需要解压内部运行资源，可能稍慢。

- 浏览器会自动打开 `http://127.0.0.1:8766/`，服务仅接受本机连接。
- 控制台窗口用于维持服务；关闭窗口即退出软件。
- 首次进入“设置”后填写自己的抬头和税号，程序不内置公司抬头。
- 导入后副本进入本机发票库；生成结果先预览，再按需保存或打印。
- 当前软件未签名；请从本仓库 Release 下载，可用附带的 SHA-256 文件核验。

便携版已在当前 Windows 环境中验证启动、空白设置、导入和 PDF 预览；其他电脑的兼容性仍需实际使用验证。软件不是税务验真服务，结果需人工复核。

## 主要功能

| 功能 | 说明 |
| --- | --- |
| 统一工作台 | 一次解析后复用于核对、重命名、合并和行程；仅展示勾选的处理模块 |
| 发票库 | 留存副本、搜索和日期筛选、多票种选择；支持分批追加至工作台 |
| 分类与完整度 | 票种分布、品名中类筛选；交通和住宿缺少必要字段时提示待补充 |
| 手动补充 | 详情中补充姓名、起止地点、行程日期和住宿日期，保存后同步发票库 |
| 机场选择 | 按省份、城市、机场选择出发/到达地点，也可只选城市；包含港澳台目录 |
| 抬头核对 | 按用户填写的名称和税号核对票面；与行程信息完整度分别展示 |
| 批量重命名 | 先预览，按规则命名；支持复制输出或用户主动触发原地改名 |
| PDF 合并 | 合并所选 PDF，OFD 保留原件但不保证转换为 PDF |
| 交通住宿行程单 | 汇总火车、飞机、客运和住宿；交通连续排布，住宿插在相关行程之间 |
| 行程展示 | 时间顺序、地图和统计；补充未知行程、直接调整住宿日期并计算晚数 |
| 生成范围 | 可选全部交通住宿票，或仅当前勾选的行程票；其他票种自动排除 |
| 预览与主题 | PDF 缩略图、可折叠详情栏、深浅主题、报告预览、保存和打印 |

住宿未识别到入住/退房日期时，以开票日作为退房日期的兜底，入住日期保持未知；公司购买方不会当作入住人。补全起止日期后自动计算晚数。

## 支持范围与限制

- 主要处理可提取文字的电子 PDF 和部分 OFD。纯扫描件、特殊版式、字段缺失的票据可能需要人工补充。
- OFD 优先读取结构化字段，无法可靠解析时提示失败。仅在有同名 PDF 时提供页内预览，未实现通用 OFD 渲染。
- 地图依赖可识别的地点；没有具体起止信息的记录仍可进入行程列表。
- 原地重命名会修改用户选定原件的文件名，执行前请复核预览。
- 暂未提供自动备份与恢复，请自行备份本机数据。

## 数据与隐私

发行包通过明确的资源清单构建，不包含开发者发票、数据库、报告、浏览器配置或测试样本。**复制 exe 到另一台电脑，不会复制当前电脑的用户数据。**

| 内容 | 默认存放位置 |
| --- | --- |
| 便携版发票库与副本 | `%LOCALAPPDATA%\发票助手\data` |
| 上传文件 | `%LOCALAPPDATA%\发票助手\uploads` |
| 生成报告 | `%LOCALAPPDATA%\发票助手\reports` |
| 抬头、税号和执行偏好 | 当前浏览器的本地存储，便携版使用独立设置键 |
| 源码版发票库 | 项目 `data/`，不纳入 Git 或发行包 |

同一台电脑再次打开时，会读取这台电脑保存的数据。源码版默认 `8765`，便携版默认 `8766`，便携版不继承源码版的旧抬头和输出目录。更多说明见 [数据与隐私](docs/PRIVACY.md)。

## 从源码运行

已使用 Python 3.14 在 Windows 上验证：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe server/main.py
```

浏览器访问 `http://127.0.0.1:8765/`。前端无需 npm 安装或构建。

## 构建单文件 exe

建议从公开仓库的干净克隆构建，避免将个人票据或配置放入资源目录：

```powershell
python -m pip install -r requirements-build.txt
powershell -File packaging/build_windows.ps1 -Python python
```

输出 `dist/报销助手.exe`。PyInstaller 只收集代码、`web/`、`assets/` 和机场目录。图标包含 16–256 像素多个尺寸。构建使用 Windows x64 和 PyInstaller 6.20.0。成品通过 Releases 分发，不提交到源码历史。

不访问发票库的设置隔离回归：

```powershell
node tests/test_settings_privacy.mjs
```

## 项目结构

```text
server/
  main.py          本机服务入口、访问约束和静态资源
  routes/          HTTP 与文件入口、统一扫描池和处理流水线
  core/            解析、分类、核对、重命名、合并、行程与发票库
web/
  js/              工作台、发票库、详情和设置
  css/             主题、布局与视觉样式
  assets/          原创 SVG / PNG / ICO 图标
assets/            地图、站点目录和本地前端依赖
packaging/         Windows 单文件构建配置和版本资源
tests/             不含真实票据的设置隔离回归
docs/              架构与隐私说明
```

解析和数据规则集中在 `core/`，路由处理 HTTP 边界，前端维护界面状态。详见 [架构与数据流](docs/ARCHITECTURE.md)。内置第三方资源的许可说明保留在对应目录中。
