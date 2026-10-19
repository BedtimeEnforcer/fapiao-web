from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

root = Path(SPECPATH).parent
a = Analysis(
    [str(root / 'server' / 'main.py')],
    pathex=[str(root), str(root / 'server')],
    binaries=[],
    # 明确允许的资源，绝不收集 data、测试样本、报告和浏览器设置。
    datas=[
        (str(root / 'web'), 'web'),
        (str(root / 'assets'), 'assets'),
        (str(root / 'server/core/airports_cn.json'), 'core'),
        (str(root / 'server/core/airports_cn_LICENSE.txt'), 'core'),
    ],
    hiddenimports=collect_submodules('uvicorn'),
    excludes=['pytest', 'torch', 'pandas', 'scipy', 'numpy', 'matplotlib',
              'tensorflow', 'cv2', 'sklearn', 'IPython', 'jupyter'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name='报销助手',
    icon=str(root / 'web/assets/app.ico'),
    version=str(root / 'packaging/version.txt'),
    debug=False, strip=False, upx=False, console=True,
)
