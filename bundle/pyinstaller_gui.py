#!/usr/bin/env python3

import os
import platform
import subprocess
import sys
import sysconfig

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyInstaller.__main__ import run as run_pyinstaller

from devscripts.utils import read_version


def main():
    if sys.platform != 'darwin':
        raise SystemExit('The yt-dlp GUI bundle can only be built on macOS')

    opts = sys.argv[1:]
    target_arch = next((
        option.partition('=')[2] for option in opts
        if option.startswith('--target-architecture=') or option.startswith('--target-arch=')
    ), 'universal2')

    architectures = set(subprocess.check_output(
        ['lipo', '-archs', sys.executable], text=True).split())
    required_architectures = {'x86_64', 'arm64'} if target_arch == 'universal2' else {target_arch}
    if not required_architectures <= architectures:
        raise SystemExit(
            f'Python at {sys.executable} does not contain the requested architecture(s): '
            f'{", ".join(sorted(required_architectures - architectures))}. '
            'Use a universal2 Python.org installer or choose a supported single architecture.')

    if not any(option in opts for option in ('-D', '--onedir', '-F', '--onefile')):
        opts.append('--onedir')
    if not any(option in opts for option in ('-c', '--console', '-w', '--windowed', '--noconsole')):
        opts.append('--windowed')
    if not any(option.startswith('--target-architecture') or option.startswith('--target-arch') for option in opts):
        opts.append(f'--target-architecture={target_arch}')

    version = read_version()
    deployment_target = sysconfig.get_config_var('MACOSX_DEPLOYMENT_TARGET')
    print(
        f'Building yt-dlp GUI v{version} for macOS {deployment_target}+ '
        f'({target_arch}; Python {platform.python_version()})')

    run_pyinstaller([
        '--name=yt-dlp-gui_macos',
        '--osx-bundle-identifier=org.enggeng.ytdlp-gui',
        '--noconfirm',
        '--clean',
        '--workpath=build/pyinstaller-gui',
        '--additional-hooks-dir=yt_dlp/__pyinstaller',
        '--add-data=THIRD_PARTY_LICENSES.txt:.',
        '--exclude-module=curl_cffi',
        '--exclude-module=setuptools',
        '--exclude-module=packaging',
        '--exclude-module=pkg_resources',
        *opts,
        'bundle/yt_dlp_gui.py',
    ])
    app_path = os.path.abspath('dist/yt-dlp-gui_macos.app')
    archive_path = os.path.abspath('dist/yt-dlp-gui_macos.zip')
    subprocess.run([
        'ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', app_path, archive_path,
    ], check=True)
    print(f'Standalone GUI app: {app_path}')
    print(f'Shareable archive: {archive_path}')


if __name__ == '__main__':
    main()
