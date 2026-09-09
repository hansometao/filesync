#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键打包脚本：同步版本号 → 运行测试 → PyInstaller → 可选 Inno Setup

用法（在项目根目录执行）：
    python build.py               # 测试 + PyInstaller 打包（onefile，产物 dist/folder_sync(.exe)）
    python build.py --installer   # 打包后再用 Inno Setup 生成安装包（需安装 iscc 并加入 PATH）
    python build.py --clean       # 先清理 build/ 与 dist/ 再打包
    python build.py --skip-tests  # 跳过无头自测（仅调试打包流程时使用）

版本号唯一来源：core/meta.py 的 APP_VERSION。每次构建前自动据此重写
version_info.txt，保证 exe 版本资源与窗口标题/帮助文案一致，无需手工维护。
无头自测未通过时打包中止，确保发布版本质量。

PyInstaller 不能跨平台交叉编译：在哪个系统上运行本脚本，就生成哪个系统
的可执行文件。要得到 Windows .exe，需在 Windows 上运行本脚本。
Windows 7 目标机请使用 Python 3.8，并安装 PyInstaller 5.x：
    pip install "pyinstaller==5.13.2"
"""
import argparse
import os
import shutil
import subprocess
import sys
from typing import Optional

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from core.meta import APP_NAME, APP_VERSION  # noqa: E402  版本号唯一来源

SPEC_FILE = os.path.join(ROOT, 'folder_sync.spec')
TEST_FILE = os.path.join(ROOT, 'test_sync.py')
DIST_EXE = 'folder_sync.exe' if sys.platform == 'win32' else 'folder_sync'
DIST_DIR = os.path.join(ROOT, 'dist')

# 打包依赖（PyInstaller）仅安装在 py38 正式环境；用错解释器时自动切换
DEFAULT_BUILD_PYTHON = os.path.join(os.path.expanduser('~'), 'envs', 'py38', 'bin', 'python')


def _has_module(interp, mod):
    # type: (str, str) -> bool
    """检查指定解释器能否导入模块（隐藏输出，仅看退出码）"""
    return subprocess.call([interp, '-c', 'import %s' % mod],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL) == 0


def ensure_build_interpreter():
    # type: () -> None
    """启动自检：当前解释器缺 PyInstaller 时，切换到 py38 环境重新执行本脚本。

    用系统 python3（缺 PyInstaller）启动打包时打包步骤必然失败；
    此处统一收敛到正式环境，避免依赖装错地方。
    可用环境变量 FILESYNC_BUILD_PYTHON 指定其他打包解释器。
    """
    if _has_module(sys.executable, 'PyInstaller'):
        return
    candidates = []
    env = os.environ.get('FILESYNC_BUILD_PYTHON')
    if env:
        candidates.append(env)
    candidates.append(DEFAULT_BUILD_PYTHON)
    for cand in candidates:
        if os.path.isfile(cand) and _has_module(cand, 'PyInstaller'):
            print('[0/4] 当前解释器缺少打包依赖：%s' % sys.executable)
            print('      切换到 %s 重新执行…' % cand)
            sys.stdout.flush()
            os.execv(cand, [cand, os.path.abspath(__file__)] + sys.argv[1:])
    sys.exit('当前解释器缺少 PyInstaller：%s\n'
             '且未找到可用的打包环境（尝试：%s）。\n'
             '请改用 ~/envs/py38/bin/python build.py，或设置 FILESYNC_BUILD_PYTHON 指定解释器。'
             % (sys.executable, '、'.join(candidates)))


def ver_tuple(version):
    # type: (str) -> tuple
    """'1.1.0' -> (1, 1, 0, 0)，补齐 4 段供 Windows 版本资源使用"""
    parts = [int(p) for p in version.split('.')]
    parts += [0] * (4 - len(parts))
    return tuple(parts[:4])


def gen_version_info(path):
    # type: (str) -> None
    """依据 meta.APP_VERSION 重写 PyInstaller 版本资源文件（中文语言 0804）"""
    vt = ver_tuple(APP_VERSION)
    ver4 = '.'.join(str(x) for x in vt)
    content = """\
# -*- coding: utf-8 -*-
# Windows 版本资源（PyInstaller 的 version= 参数）。
# 注意：此文件由 build.py 依据 core/meta.py 的 APP_VERSION 自动重写，
# 请勿手改——发版只需更新 core/meta.py，再运行 python build.py。
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=(%s),
    prodvers=(%s),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [
        StringTable(
          '080404B0',
          [
            StringStruct('CompanyName', '开发团队'),
            StringStruct('FileDescription', '%s'),
            StringStruct('FileVersion', '%s'),
            StringStruct('InternalName', '%s'),
            StringStruct('OriginalFilename', '%s.exe'),
            StringStruct('ProductName', '%s'),
            StringStruct('ProductVersion', '%s')
          ]
        )
      ]
    ),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
""" % (', '.join(str(x) for x in vt), ', '.join(str(x) for x in vt),
       APP_NAME, ver4, 'folder_sync', 'folder_sync', APP_NAME, ver4)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    print('[2/4] 已同步版本资源 %s（APP_VERSION=%s）' % (os.path.relpath(path, ROOT), APP_VERSION))


def _ensure_icon():
    # type: () -> str
    """确保 app.ico 存在：缺则用 make_icon 生成（需 Pillow），缺失时给出指引而非崩溃"""
    ico = os.path.join(ROOT, 'app.ico')
    if os.path.exists(ico):
        return ico
    try:
        import make_icon
        make_icon.make_icon(ico)
    except ImportError:
        # 缺 Pillow 时 make_icon 惰性导入抛 ImportError，给出安装指引而非裸崩溃
        print('缺少 app.ico 且未安装 Pillow（生成占位图标需要）:')
        print('  方案一: pip install pillow  再重试本脚本')
        print('  方案二: 自行放置 app.ico 到项目根目录后重试')
        print('  （app.ico 用于 exe 图标与 Windows 托盘图标，缺失不影响打包逻辑）')
        sys.exit(1)
    except OSError as e:
        # make_icon 的 I/O 失败（如目标目录不可写）：同样给出指引而非裸崩溃
        print('生成 app.ico 失败: %s' % e)
        print('  请检查项目目录是否可写，或自行放置 app.ico 到项目根目录后重试')
        sys.exit(1)
    return ico


def _backup_old_artifact():
    # type: () -> Optional[str]
    """打包前备份旧产物（dist/folder_sync(.exe)），打包失败时恢复。

    PyInstaller 失败时 dist/ 可能残留半成品/覆盖可用版本：先改名备份，
    成功则删除备份，失败则改回，保证用户手里始终有一个可用的 exe。
    """
    out = os.path.join(DIST_DIR, DIST_EXE)
    if not os.path.exists(out):
        return None
    bak = out + '.bak'
    try:
        if os.path.exists(bak):
            os.remove(bak)
        os.rename(out, bak)
        return bak
    except OSError as e:
        print('警告: 旧产物备份失败（%s），打包失败时可能无可用版本' % e)
        return None


def run_tests():
    # type: () -> None
    """打包前先跑无头自测，测试失败则中止（保证发布版本质量）。

    filesync 的权威测试入口是 test_sync.py（自定义 check()，非 pytest）：
    每个编号节封装为独立 test_N() 函数，失败定位到节，exit 1 表示存在失败。
    """
    print('[1/4] 运行无头自测…')
    code = subprocess.call([sys.executable, TEST_FILE], cwd=ROOT)
    if code != 0:
        sys.exit('无头自测未通过（退出码 %d），打包中止。'
                 '请先修复测试再发布。' % code)
    print('      测试全部通过')


def run_pyinstaller():
    # type: () -> Optional[str]
    """执行 PyInstaller 打包（onefile + windowed + 图标 + 版本资源）"""
    _ensure_icon()
    print('[3/4] 执行 PyInstaller（spec=%s）…' % os.path.relpath(SPEC_FILE, ROOT))
    bak = _backup_old_artifact()
    cmd = [sys.executable, '-m', 'PyInstaller', '--noconfirm', SPEC_FILE]
    print('执行: %s' % ' '.join(cmd))
    # 安全说明：cmd 为本地固定拼接的列表（sys.executable + PyInstaller 子命令 +
    # spec 文件名），不含用户输入，不存在命令注入风险。subprocess.call 使用
    # 列表形式（非 shell=True），参数不经 shell 解释。
    rc = subprocess.call(cmd, cwd=ROOT)
    if rc != 0:
        print('\n打包失败（退出码 %d）' % rc)
        # 失败回滚：恢复旧产物，避免用户拿到半成品/无可用版本
        if bak is not None:
            out = os.path.join(DIST_DIR, DIST_EXE)
            try:
                if os.path.exists(out):
                    os.remove(out)  # 删除半成品
                os.rename(bak, out)
                print('已恢复旧产物: %s' % out)
            except OSError as e:
                print('警告: 旧产物恢复失败（%s），可在 %s 手动恢复' % (e, bak))
        sys.exit(rc)
    if bak is not None:
        try:
            os.remove(bak)  # 打包成功：清理备份
        except OSError:
            pass
    out = os.path.join(DIST_DIR, DIST_EXE)
    print('      产物：%s' % os.path.relpath(out, ROOT))
    return out


def run_installer():
    # type: () -> None
    """用 Inno Setup 生成安装包（需 iscc 在 PATH 中，仅 Windows 有意义）"""
    iss = os.path.join(ROOT, 'installer.iss')
    if not os.path.exists(iss):
        sys.exit('缺少 installer.iss，无法生成安装包')
    iscc = shutil.which('iscc')
    if not iscc:
        sys.exit('未找到 iscc（Inno Setup 编译器）。请安装 Inno Setup 6 并加入 PATH，'
                 '或去掉 --installer 仅生成单文件 exe。')
    print('[4/4] 执行 Inno Setup（%s）…' % iscc)
    code = subprocess.call([iscc, '/DMyAppVersion=%s' % APP_VERSION, iss], cwd=ROOT)
    if code != 0:
        sys.exit('Inno Setup 失败（退出码 %d）' % code)


def main():
    # type: () -> None
    ap = argparse.ArgumentParser(description='filesync 一键打包')
    ap.add_argument('--installer', action='store_true', help='打包后再用 Inno Setup 生成安装包')
    ap.add_argument('--clean', action='store_true', help='先清理 build/ 与 dist/ 再打包')
    ap.add_argument('--skip-tests', action='store_true',
                    help='跳过无头自测（仅调试打包流程时使用）')
    args = ap.parse_args()
    ensure_build_interpreter()

    if args.clean:
        for d in ('build', 'dist'):
            p = os.path.join(ROOT, d)
            if os.path.isdir(p):
                shutil.rmtree(p)
                print('已清理 %s/' % d)

    gen_version_info(os.path.join(ROOT, 'version_info.txt'))
    if not args.skip_tests:
        run_tests()
    else:
        print('[1/4] 已跳过无头自测（--skip-tests）')
    out = run_pyinstaller()
    if args.installer:
        run_installer()
    else:
        print('[4/4] 完成。单文件版位于 dist/%s。' % DIST_EXE)
    print('提示：')
    print('  - 当前产物为 %s 平台可执行文件；要得到 Windows exe 请在 Windows 上运行本脚本。' % sys.platform)
    print('  - config/ 与 logs/ 会生成在可执行文件同目录。')
    if sys.platform != 'win32':
        print('  - Win7 目标机请用 Python 3.8 + PyInstaller 5.x（见文件头注释）。')


if __name__ == '__main__':
    main()
