# 报销助手

Windows 上本地跑的发票整理工具，起因是自己报销时发票攒了一堆，手动重命名、合并、拼行程单实在烦。

干的事就这些：导入电子发票（PDF / 部分 OFD），解析完之后核对抬头、按规则批量重命名、合并 PDF，再把火车/飞机/住宿这些凑成一张交通住宿行程单。所有东西都在本机处理，后端是 Python + FastAPI + SQLite，前端是原生 HTML/CSS/JS，没接云端，也没接什么大模型服务。

更细的说明在 `docs/ARCHITECTURE.md`（结构和数据流）和 `docs/PRIVACY.md`（数据放哪）。

## 直接用（便携版）

去 [Releases](https://github.com/BedtimeEnforcer/fapiao-web/releases) 下 `BaoXiaoAssistant-v0.4.1-windows-x64.zip`，解压双击 `报销助手.exe` 就行，不用装 Python。也有单文件的 `BaoXiaoAssistant.exe`，功能一样，ZIP 只是为了绕开 GitHub 对全中文附件名的过滤。只支持 Windows 10/11 x64。第一次启动会解压内置资源，会慢一点，正常。

- 启动后浏览器自动打开 `http://127.0.0.1:8766/`，服务只听本机，外面连不进来。控制台窗口别关，关了就退出了。
- 先去「设置」填自己的公司抬头和税号。程序里不内置任何抬头，填错了核对结果就别信。
- 导入的发票副本会存进本机发票库；重命名、合并这些生成结果都是先预览，确认了再保存或打印。
- 软件没签名，Windows 可能弹安全提示。从本仓库 Release 下的可以用附带的 SHA-256 文件对一下。
- 便携版只在我的 Windows 环境里实际跑过启动、导入和 PDF 预览，其他机器没条件逐个验。出问题先看是不是票本身的问题。
- 它不是验真工具，解析结果要人工复核一遍再交财务。

常用的几块功能：发票库里能搜索、按日期筛、攒着分批加进工作台；抬头按名称+税号核对，没识别出入住/退房日期的住宿票会拿开票日当退房日兜底、入住日留空，补上日期后自动算晚数；行程单按时间排，住宿插在对应交通段之间，也能画到地图上看看。批量重命名可以选复制输出，也可以原地改名——原地改会动原件文件名，点之前务必看预览。

局限也说一下：主要吃能提取出文字的电子 PDF 和一部分 OFD，纯扫描件、版式太怪、字段缺失的票经常要手工补；OFD 没有同名 PDF 时没法在页面里预览，也没做通用渲染；没有自动备份，数据丢了找不回来，自己记得备份。

数据默认放在 `%LOCALAPPDATA%\发票助手\` 下面（`data` 是发票库和副本，`uploads` 是上传文件，`reports` 是生成的报告），抬头税号这类偏好存在浏览器本地存储里。把 exe 拷到另一台电脑不会把数据带过去，这个放心。

## 跑源码

在 Windows + Python 3.14 上验证过：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe server/main.py
```

打开 `http://127.0.0.1:8765/`。注意源码版是 8765，便携版是 8766，两边的抬头等设置不互通。前端不用 npm，不用构建。

打包成单文件 exe（建议在干净克隆里打，别把个人票据混进资源目录）：

```powershell
python -m pip install -r requirements-build.txt
powershell -File packaging/build_windows.ps1 -Python python
```

出 `dist/报销助手.exe`。环境是 Windows x64 + PyInstaller 6.20.0。成品只发 Release，不进 git 历史。

有个不碰发票库的小回归测试：

```powershell
node tests/test_settings_privacy.mjs
```

## 目录

```
server/     后端，main.py 是入口，routes/ 管 HTTP，core/ 放解析/核对/重命名/合并/行程的逻辑
web/        前端页面，js / css / assets
assets/     地图、机场站点目录、本地前端依赖
packaging/  Windows 打包配置
tests/      回归测试，不含真实票据
docs/       架构和隐私说明
```
