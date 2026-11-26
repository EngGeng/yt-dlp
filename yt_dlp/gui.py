import csv
import os
import queue
import shutil
import sys
import tempfile
import threading
import tkinter as tk
import uuid
from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog
from tkinter import messagebox
from tkinter import scrolledtext
from tkinter import ttk
from urllib.parse import urlsplit

from . import YoutubeDL
from .postprocessor.ffmpeg import FFmpegPostProcessor
from .utils import DownloadCancelled, filesize_from_tbr, format_bytes


class _QueueLogger:
    def __init__(self, events):
        self._events = events
        self.had_error = False

    def debug(self, message):
        if not message.startswith('[debug] '):
            self.info(message)

    def info(self, message):
        self._events.put(('log', message))

    def warning(self, message):
        self._events.put(('log', f'警告：{message}'))

    def error(self, message):
        self.had_error = True
        self._events.put(('log', f'错误：{message}'))


@dataclass(frozen=True)
class _SizeEstimate:
    known_bytes: int = 0
    unknown_parts: int = 0
    approximate: bool = False


@dataclass
class _MediaItem:
    title: str
    info: dict
    selected: bool = True
    state: str = '待选择'
    detail: str = ''
    video_size: _SizeEstimate | None = None
    audio_size: _SizeEstimate | None = None
    report_record: '_DownloadRecord | None' = None
    previous_records: dict = field(default_factory=dict)


@dataclass
class _DownloadRecord:
    task_id: str
    title: str
    url: str
    download_url: str
    extractor_key: str
    entry_type: str
    output_directory: Path
    audio_only: bool
    state: str = '未开始'
    detail: str = ''


def _format_size(info):
    formats = info.get('requested_formats') or [info]
    sizes = []
    for fmt in formats:
        if fmt.get('requested_formats'):
            sizes.append(_format_size(fmt))
        elif isinstance(fmt.get('filesize'), (int, float)) and fmt['filesize'] >= 0:
            sizes.append(_SizeEstimate(int(fmt['filesize'])))
        elif isinstance(fmt.get('filesize_approx'), (int, float)) and fmt['filesize_approx'] >= 0:
            sizes.append(_SizeEstimate(int(fmt['filesize_approx']), approximate=True))
        else:
            sizes.append(_SizeEstimate(unknown_parts=1))
    return _SizeEstimate(
        sum(size.known_bytes for size in sizes),
        sum(size.unknown_parts for size in sizes),
        any(size.approximate for size in sizes))


def _csv_safe_text(value):
    value = str(value)
    if value.startswith(('\t', '\r', '\n')) or value.lstrip().startswith(('=', '+', '-', '@')):
        return f"'{value}"
    return value


def _csv_restore_text(value):
    if value.startswith("''"):
        return value[1:]
    if value.startswith("'") and _csv_safe_text(value[1:]) == value:
        return value[1:]
    return value


def _report_safe_text(value):
    value = str(value)
    return f"'{value}" if value.startswith("'") else _csv_safe_text(value)


def _export_csv(items, filename):
    def size_fields(size):
        if size is None or size.unknown_parts:
            return '未解析', '未解析'
        return size.known_bytes, '估算' if size.approximate else '已解析'

    with open(filename, 'w', encoding='utf-8-sig', newline='') as output:
        writer = csv.writer(output)
        writer.writerow(('资源名称', '资源链接', '视频大小（字节）', '视频大小说明',
                         'MP3 源音频大小（字节）', '音频大小说明'))
        for item in items:
            writer.writerow((
                _csv_safe_text(item.title),
                _csv_safe_text(item.info.get('webpage_url') or item.info.get('url') or ''),
                *size_fields(item.video_size),
                *size_fields(item.audio_size),
            ))


_REPORT_COLUMNS = (
    '任务 ID', '资源名称', '资源链接', '下载链接', '提取器', '链接类型', '下载类型', '保存位置', '状态', '说明')


def _export_download_report(records, filename):
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
                mode='w', encoding='utf-8-sig', newline='', dir=Path(filename).parent,
                prefix='.yt-dlp-report-', suffix='.tmp', delete=False) as output:
            temporary_path = Path(output.name)
            writer = csv.writer(output)
            writer.writerow(_REPORT_COLUMNS)
            for record in records:
                writer.writerow((
                    record.task_id, _report_safe_text(record.title), _report_safe_text(record.url),
                    _report_safe_text(record.download_url), _report_safe_text(record.extractor_key), record.entry_type,
                    'MP3 音频' if record.audio_only else '视频',
                    _report_safe_text(record.output_directory), record.state, _report_safe_text(record.detail),
                ))
        os.replace(temporary_path, filename)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _read_download_report(filename):
    records = []
    seen = set()
    with open(filename, encoding='utf-8-sig', newline='') as source:
        reader = csv.DictReader(source, strict=True)
        if reader.fieldnames != list(_REPORT_COLUMNS):
            raise ValueError('文件不是受支持的下载报告，或报告表头已被修改。')
        for line, row in enumerate(reader, 2):
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f'报告第 {line} 行列数不正确。')
            task_id = row['任务 ID']
            url = _csv_restore_text(row['下载链接'])
            directory = _csv_restore_text(row['保存位置'])
            try:
                valid_id = uuid.UUID(task_id).hex == task_id
            except ValueError:
                valid_id = False
            if (not valid_id or task_id in seen or (not url and row['状态'] != '完成') or not directory
                    or row['状态'] not in ('完成', '失败', '已停止', '未开始')
                    or row['下载类型'] not in ('视频', 'MP3 音频')
                    or row['链接类型'] not in ('url', 'url_transparent')):
                raise ValueError(f'报告第 {line} 行包含无效或重复的任务信息。')
            seen.add(task_id)
            records.append(_DownloadRecord(
                task_id, _csv_restore_text(row['资源名称']), _csv_restore_text(row['资源链接']),
                url, _csv_restore_text(row['提取器']), row['链接类型'],
                Path(directory), row['下载类型'] == 'MP3 音频', row['状态'],
                _csv_restore_text(row['说明'])))
    return records


def _find_ffmpeg_location():
    if sys.platform != 'darwin':
        return None

    programs = ('ffmpeg', 'ffprobe')
    if all(shutil.which(program) for program in programs):
        return None

    for directory in ('/opt/homebrew/bin', '/usr/local/bin', '/opt/local/bin'):
        if all(shutil.which(program, path=directory) for program in programs):
            return directory
    return None


class _ScanControl:
    def __init__(self, events):
        self._condition = threading.Condition()
        self._events = events
        self._paused = False
        self._cancelled = False

    def pause(self):
        with self._condition:
            self._paused = True

    def resume(self):
        with self._condition:
            self._paused = False
            self._condition.notify_all()

    def cancel(self):
        with self._condition:
            self._cancelled = True
            self._condition.notify_all()

    def checkpoint(self):
        with self._condition:
            if self._paused and not self._cancelled:
                self._events.put(('scan_paused',))
            while self._paused and not self._cancelled:
                self._condition.wait()
            if self._cancelled:
                raise DownloadCancelled('Scan cancelled from GUI')


class _ScanYoutubeDL(YoutubeDL):
    def __init__(self, options, checkpoint, *, auto_init=True):
        self._scan_checkpoint = checkpoint
        super().__init__(options, auto_init=auto_init)

    def urlopen(self, req):
        self._scan_checkpoint()
        return super().urlopen(req)


def _quick_sizes(ydl, info):
    unknown = _SizeEstimate(unknown_parts=1)
    if info.get('_type', 'video') == 'video' and info.get('formats'):
        selected = ydl.process_video_result(deepcopy(info), download=False)
        if selected:
            audio_formats = ydl._select_formats(
                ydl._get_formats(selected), ydl.build_format_selector('bestaudio/best'))
            return _format_size(selected), _format_size(audio_formats[-1]) if audio_formats else unknown

    duration = info.get('duration')

    def bitrate_size(key):
        bitrate = info.get(key)
        if (isinstance(bitrate, (int, float)) and bitrate > 0
                and isinstance(duration, (int, float)) and duration > 0):
            return _SizeEstimate(filesize_from_tbr(bitrate, duration), approximate=True)
        return unknown

    media_size = _format_size(info)
    if media_size.unknown_parts and not info.get('requested_formats'):
        media_size = bitrate_size('tbr')
    if info.get('vcodec') == 'none':
        return unknown, media_size
    if media_size.known_bytes or not media_size.unknown_parts:
        media_size = _SizeEstimate(media_size.known_bytes, media_size.unknown_parts, approximate=True)
        if info.get('acodec') == 'none':
            media_size = _SizeEstimate(media_size.known_bytes, max(1, media_size.unknown_parts), approximate=True)
    audio_formats = [
        fmt for fmt in info.get('requested_formats') or ()
        if fmt.get('vcodec') == 'none' and fmt.get('acodec') not in (None, 'none')
    ]
    audio_size = _format_size(audio_formats[0]) if len(audio_formats) == 1 else bitrate_size('abr')
    return media_size, audio_size


def _actual_sizes(ydl, info):
    result = ydl.process_ie_result(deepcopy(info), download=False)
    if not result or result.get('_type', 'video') != 'video':
        return None
    audio_formats = ydl._select_formats(
        ydl._get_formats(result), ydl.build_format_selector('bestaudio/best'))
    return _format_size(result), _format_size(audio_formats[-1]) if audio_formats else _SizeEstimate(unknown_parts=1)


def _scan(urls, control, events, input_text):
    logger = _QueueLogger(events)
    items = []
    pending_sizes = []

    def collect(ydl, info, source_url=None, ancestors=frozenset()):
        control.checkpoint()
        if not info:
            logger.error('无法获取资源信息。')
            return
        result_type = info.get('_type', 'video')
        if result_type in ('playlist', 'multi_video'):
            entries = iter(info.get('entries') or ())
            while True:
                control.checkpoint()
                try:
                    entry = next(entries)
                except StopIteration:
                    break
                collect(ydl, entry, ancestors=ancestors)
            return
        if result_type in ('url', 'url_transparent'):
            url, ie_key = info.get('url'), info.get('ie_key')
            if not url:
                logger.error('清单中存在没有链接的资源。')
                return
            if source_url or (ie_key and ydl.get_info_extractor(ie_key).is_single_video(url) is False):
                key = (url, ie_key)
                if key in ancestors:
                    logger.error(f'清单包含循环链接：{url}')
                    return
                collect(ydl, ydl.extract_info(url, download=False, ie_key=ie_key, process=False),
                        source_url=source_url or url, ancestors=ancestors | {key})
                return
            download_info = info.copy()
        else:
            url = source_url or info.get('webpage_url')
            download_info = {'_type': 'url', 'url': url} if url else info.copy()
        title = info.get('title') or info.get('id') or url
        if not title:
            logger.error('清单中存在没有名称或链接的资源。')
            return
        item = _MediaItem(str(title), download_info)
        items.append(item)
        pending_sizes.append((item, info))
        events.put(('scan_item', item))
        events.put(('scan_count', len(items)))

    try:
        with _ScanYoutubeDL({
            'quiet': True,
            'logger': logger,
            'extract_flat': 'in_playlist',
            'ignoreerrors': True,
            'ffmpeg_location': _find_ffmpeg_location(),
        }, control.checkpoint) as ydl:
            for url in urls:
                control.checkpoint()
                collect(ydl, ydl.extract_info(url, download=False, process=False), source_url=url)
            control.checkpoint()
            events.put(('scan_list_done', len(items), logger.had_error))
            for item, info in pending_sizes:
                control.checkpoint()
                try:
                    video_size, audio_size = _quick_sizes(ydl, info)
                except DownloadCancelled:
                    raise
                except Exception as error:
                    logger.warning(f'无法估算资源大小：{item.title}: {type(error).__name__}: {error}')
                    video_size = audio_size = _SizeEstimate(unknown_parts=1)
                try:
                    if info.get('_type', 'video') == 'video' and info.get('formats'):
                        actual_sizes = video_size, audio_size
                    else:
                        actual_sizes = _actual_sizes(ydl, item.info)
                    if actual_sizes is None:
                        logger.warning(f'无法获取实际格式大小，使用已有信息：{item.title}')
                        actual_sizes = video_size, audio_size
                except DownloadCancelled:
                    raise
                except Exception as error:
                    logger.warning(f'无法获取实际格式大小，使用已有信息：{item.title}: {type(error).__name__}: {error}')
                    actual_sizes = video_size, audio_size
                control.checkpoint()
                events.put(('scan_size', str(id(item)), *actual_sizes))
    except DownloadCancelled:
        events.put(('scan_done', [], 'cancelled', input_text))
    except Exception as error:
        logger.error(f'{type(error).__name__}: {error}')
        events.put(('scan_done', items, 'failed', input_text))
    else:
        events.put(('scan_done', items, 'failed' if logger.had_error else 'success', input_text))


def _download(url, output_directory, audio_only, cancel_event, events):
    def check_cancelled(*_args, **_kwargs):
        if cancel_event.is_set():
            raise DownloadCancelled('Download cancelled from GUI')

    def progress_hook(data):
        check_cancelled()
        status = data.get('status')
        if status == 'finished':
            events.put(('progress', 100, f'下载完成，正在处理：{Path(data["filename"]).name}'))
            return
        if status != 'downloading':
            return

        total = data.get('total_bytes') or data.get('total_bytes_estimate')
        downloaded = data.get('downloaded_bytes') or 0
        percent = min(downloaded / total * 100, 100) if total else None
        filename = Path(data.get('filename', '')).name
        message = f'正在下载：{filename}' if filename else '正在下载...'
        if percent is not None:
            message += f'（{percent:.1f}%）'
        if eta := data.get('eta'):
            message += f'，剩余 {int(eta)} 秒'
        events.put(('progress', percent, message))

    def postprocessor_hook(data):
        check_cancelled()
        if data.get('status') == 'started':
            events.put(('status', f'正在处理：{data.get("postprocessor", "媒体文件")}'))

    logger = _QueueLogger(events)
    options = {
        'outtmpl': str(output_directory / '%(title)s [%(id)s].%(ext)s'),
        'quiet': True,
        'logger': logger,
        'progress_hooks': [progress_hook],
        'postprocessor_hooks': [postprocessor_hook],
        'match_filter': check_cancelled,
        'lazy_playlist': True,
        'ignoreerrors': True,
    }
    if ffmpeg_location := _find_ffmpeg_location():
        options['ffmpeg_location'] = ffmpeg_location
    if audio_only:
        options.update({
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
            }],
        })

    try:
        check_cancelled()
        with YoutubeDL(options) as ydl:
            if audio_only and not FFmpegPostProcessor(ydl).available:
                hint = ('可在终端运行 brew install ffmpeg，然后重试。' if sys.platform == 'darwin'
                        else '请安装 FFmpeg，并将其所在目录加入 PATH 后重试。')
                events.put(('log', f'错误：无法运行 FFmpeg。{hint}'))
                events.put(('done', 'failed', '无法转换为 MP3，请检查 FFmpeg 安装。'))
                return
            check_cancelled()
            if isinstance(url, dict):
                result = ydl.process_ie_result(url.copy(), download=True)
                error_code = int(logger.had_error or result is None)
            else:
                error_code = ydl.download([url])
    except DownloadCancelled:
        events.put(('done', 'cancelled', '已取消下载。'))
    except Exception as error:
        events.put(('log', f'错误：{type(error).__name__}: {error}'))
        events.put(('done', 'failed', '下载失败，请查看日志。'))
    else:
        if cancel_event.is_set():
            events.put(('done', 'cancelled', '已取消下载。'))
        elif error_code:
            events.put(('done', 'failed', f'下载失败（错误码 {error_code}），请查看日志。'))
        else:
            events.put(('done', 'success', '下载完成。'))


class _DownloadApp:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.pending = deque()
        self.items = {}
        self.cancel_event = None
        self.worker = None
        self.scan_worker = None
        self.scan_control = None
        self.scan_paused = False
        self.scan_pause_ack = False
        self.scan_phase = 'list'
        self.scan_list_failed = False
        self.scan_count = 0
        self.scan_size_count = 0
        self.current_item = None
        self.current_record = None
        self.download_records = []
        self.download_started = False
        self.report_prompt_open = False
        self.paused = False
        self.stopping = False
        self.close_when_done = False

        self.output_directory = tk.StringVar(value=str(Path.home() / 'Downloads'))
        self.download_type = tk.StringVar(value='video')
        self.status = tk.StringVar(value='就绪')
        self.queue_status = tk.StringVar(value='资源：0 项，已选择：0 项')
        self.scan_status = tk.StringVar(value='请先解析链接')

        root.title('yt-dlp')
        root.geometry('820x800')
        root.minsize(680, 620)
        root.protocol('WM_DELETE_WINDOW', self.close)
        self._build_ui()
        root.after(100, self._poll_events)

    def _build_ui(self):
        frame = ttk.Frame(self.root, padding=18)
        frame.pack(fill='both', expand=True)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(5, weight=2)
        frame.rowconfigure(10, weight=1)

        ttk.Label(frame, text='yt-dlp', font=('TkDefaultFont', 20, 'bold')).grid(
            row=0, column=0, columnspan=3, sticky='w', pady=(0, 14))

        ttk.Label(frame, text='媒体/频道链接\n每行一个').grid(
            row=1, column=0, sticky='nw', padx=(0, 10), pady=5)
        self.url_input = scrolledtext.ScrolledText(frame, wrap='word', height=4, width=1)
        self.url_input.grid(row=1, column=1, columnspan=2, sticky='ew', pady=5)

        ttk.Label(frame, text='保存位置').grid(row=2, column=0, sticky='w', padx=(0, 10), pady=5)
        self.directory_entry = ttk.Entry(frame, textvariable=self.output_directory)
        self.directory_entry.grid(row=2, column=1, sticky='ew', pady=5)
        self.browse_button = ttk.Button(frame, text='选择...', command=self.choose_directory)
        self.browse_button.grid(row=2, column=2, padx=(8, 0), pady=5)

        type_frame = ttk.LabelFrame(frame, text='下载类型', padding=(10, 5))
        type_frame.grid(row=3, column=0, columnspan=3, sticky='ew', pady=(8, 12))
        self.video_button = ttk.Radiobutton(
            type_frame, text='视频', variable=self.download_type, value='video', command=self._update_queue_status)
        self.video_button.pack(side='left', padx=(0, 18))
        self.audio_button = ttk.Radiobutton(
            type_frame, text='MP3 音频（需要 FFmpeg）', variable=self.download_type, value='audio',
            command=self._update_queue_status)
        self.audio_button.pack(side='left')

        scan_frame = ttk.Frame(frame)
        scan_frame.grid(row=4, column=0, columnspan=3, sticky='ew')
        self.scan_button = ttk.Button(scan_frame, text='解析链接', command=self.scan_links)
        self.scan_button.pack(side='left')
        self.scan_pause_button = ttk.Button(
            scan_frame, text='暂停解析', command=self.toggle_scan_pause, state='disabled')
        self.scan_pause_button.pack(side='left', padx=(8, 0))
        ttk.Button(scan_frame, text='全选', command=lambda: self.select_all(True)).pack(side='left', padx=(8, 0))
        ttk.Button(scan_frame, text='全不选', command=lambda: self.select_all(False)).pack(side='left', padx=(8, 0))
        ttk.Label(scan_frame, textvariable=self.scan_status).pack(side='left', padx=(12, 0))

        list_frame = ttk.Frame(frame)
        list_frame.grid(row=5, column=0, columnspan=3, sticky='nsew', pady=(8, 4))
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        self.resource_list = ttk.Treeview(
            list_frame, columns=('selected', 'title', 'state'), show='headings', selectmode='browse')
        for column, heading, width in (
                ('selected', '选择', 55), ('title', '资源名称', 450), ('state', '状态', 160)):
            self.resource_list.heading(column, text=heading)
            self.resource_list.column(column, width=width, stretch=column == 'title')
        self.resource_list.grid(row=0, column=0, sticky='nsew')
        scrollbar = ttk.Scrollbar(list_frame, orient='vertical', command=self.resource_list.yview)
        scrollbar.grid(row=0, column=1, sticky='ns')
        self.resource_list.configure(yscrollcommand=scrollbar.set)
        self.resource_list.bind('<Button-1>', self._toggle_row)
        self.resource_list.bind('<space>', self._toggle_selected_row)
        ttk.Label(frame, textvariable=self.queue_status).grid(row=6, column=0, columnspan=3, sticky='w')

        self.progress = ttk.Progressbar(frame, maximum=100, mode='determinate')
        self.progress.grid(row=7, column=0, columnspan=3, sticky='ew', pady=(8, 4))
        ttk.Label(frame, textvariable=self.status).grid(row=8, column=0, columnspan=3, sticky='w')

        button_frame = ttk.Frame(frame)
        button_frame.grid(row=9, column=0, columnspan=3, sticky='ew', pady=10)
        self.start_button = ttk.Button(
            button_frame, text='下载所选', command=self.start_download, default='active')
        self.start_button.pack(side='left')
        self.pause_button = ttk.Button(
            button_frame, text='暂停', command=self.toggle_pause, state='disabled')
        self.pause_button.pack(side='left', padx=(8, 0))
        self.cancel_button = ttk.Button(
            button_frame, text='停止全部', command=self.cancel_download, state='disabled')
        self.cancel_button.pack(side='left', padx=(8, 0))
        ttk.Button(button_frame, text='导出清单（CSV）', command=self.export_list).pack(side='left', padx=(8, 0))
        ttk.Button(button_frame, text='导入下载报告', command=self.import_report).pack(side='left', padx=(8, 0))

        self.log = scrolledtext.ScrolledText(frame, wrap='word', height=6, state='disabled')
        self.log.grid(row=10, column=0, columnspan=3, sticky='nsew')

    def choose_directory(self):
        directory = filedialog.askdirectory(
            parent=self.root,
            title='选择下载文件夹',
            initialdir=self.output_directory.get() or str(Path.home()))
        if directory:
            self.output_directory.set(directory)

    def export_list(self):
        if not self.items:
            messagebox.showerror('没有资源', '请先解析链接，再导出清单。', parent=self.root)
            return
        filename = filedialog.asksaveasfilename(
            parent=self.root, title='导出 Excel 清单',
            initialfile='资源清单.csv', defaultextension='.csv',
            filetypes=[('Excel 可打开的 CSV 文件', '*.csv')])
        if not filename:
            return
        try:
            _export_csv(self.items.values(), filename)
        except (OSError, UnicodeError, csv.Error) as error:
            self._append_log(f'导出失败：{error}')
            messagebox.showerror('导出失败', f'无法保存清单：{error}', parent=self.root)
            return
        self._append_log(f'已导出 {len(self.items)} 项：{filename}')
        messagebox.showinfo('导出完成', f'已导出 {len(self.items)} 项资源。', parent=self.root)

    def import_report(self):
        if self.close_when_done:
            return
        filename = filedialog.askopenfilename(
            parent=self.root, title='导入下载报告', filetypes=[('下载报告（CSV）', '*.csv')])
        if not filename:
            return
        try:
            records = _read_download_report(filename)
        except (OSError, UnicodeError, csv.Error, ValueError) as error:
            self._append_log(f'导入下载报告失败：{error}')
            messagebox.showerror('导入失败', f'无法读取下载报告：{error}', parent=self.root)
            return
        existing_ids = {record.task_id for record in self.download_records}
        imported = 0
        skipped = 0
        for record in records:
            if record.task_id in existing_ids:
                skipped += 1
                continue
            existing_ids.add(record.task_id)
            self.download_records.append(record)
            if record.state == '完成':
                continue
            info = {'_type': record.entry_type, 'url': record.download_url}
            if record.extractor_key:
                info['ie_key'] = record.extractor_key
            item = _MediaItem(
                record.title, info,
                detail=f'待恢复（{record.state}）：{"MP3" if record.audio_only else "视频"}，{record.output_directory}',
                video_size=_SizeEstimate(unknown_parts=1), audio_size=_SizeEstimate(unknown_parts=1),
                report_record=record)
            self._add_scan_item(item)
            imported += 1
        self._update_queue_status()
        message = f'已导入 {imported} 项待恢复任务，请确认选择后点击下载所选。'
        if skipped:
            message += f'跳过 {skipped} 项已导入任务。'
        self._append_log(message)
        messagebox.showinfo('导入完成', message, parent=self.root)

    def scan_links(self):
        if self.close_when_done or self.scan_worker is not None:
            return

        input_text = self.url_input.get('1.0', 'end-1c')
        urls = []
        for line_number, line in enumerate(input_text.splitlines(), 1):
            url = line.strip()
            if not url:
                continue
            try:
                parsed_url = urlsplit(url)
                valid = (parsed_url.scheme in ('http', 'https') and parsed_url.hostname
                         and not any(character.isspace() for character in url))
            except ValueError:
                valid = False
            if not valid:
                messagebox.showerror(
                    '链接无效', f'第 {line_number} 行不是有效的 HTTP 或 HTTPS 链接。请每行填写一个链接。',
                    parent=self.root)
                return
            urls.append(url)
        if not urls:
            messagebox.showerror('没有链接', '请输入至少一个媒体或频道链接。', parent=self.root)
            return

        self.scan_control = _ScanControl(self.events)
        self.scan_paused = False
        self.scan_pause_ack = False
        self.scan_phase = 'list'
        self.scan_list_failed = False
        self.scan_count = 0
        self.scan_size_count = 0
        self._update_scan_status()
        self.scan_button.configure(state='disabled')
        self.scan_pause_button.configure(state='normal', text='暂停解析')
        self.scan_worker = threading.Thread(
            target=_scan, args=(urls, self.scan_control, self.events, input_text), daemon=True)
        self.scan_worker.start()

    def toggle_scan_pause(self):
        if self.scan_worker is None or self.close_when_done:
            return
        self.scan_paused = not self.scan_paused
        self.scan_pause_ack = False
        if self.scan_paused:
            self.scan_control.pause()
            self.scan_pause_button.configure(text='继续解析')
        else:
            self.scan_control.resume()
            self.scan_pause_button.configure(text='暂停解析')
        self._update_scan_status()

    def _update_scan_status(self):
        if self.scan_phase == 'size':
            state = ('大小分析已暂停' if self.scan_pause_ack else '正在暂停大小分析') if self.scan_paused else '正在分析大小'
            list_state = '清单部分失败' if self.scan_list_failed else '清单已完成'
            self.scan_status.set(f'{list_state}：{self.scan_count} 项，{state}：{self.scan_size_count}/{self.scan_count} 项')
        else:
            state = ('解析已暂停' if self.scan_pause_ack else '正在暂停解析') if self.scan_paused else '正在解析清单'
            self.scan_status.set(f'{state}，已发现：{self.scan_count} 项')

    def _render_item(self, item):
        row = str(id(item))
        values = ('☑' if item.selected else '☐', item.title, item.detail or item.state)
        if self.resource_list.exists(row):
            self.resource_list.item(row, values=values)
        else:
            self.resource_list.insert('', 'end', iid=row, values=values)

    def _toggle_row(self, event):
        if self.resource_list.identify_column(event.x) != '#1':
            return
        self._toggle_item(self.resource_list.identify_row(event.y))
        return 'break'

    def _toggle_selected_row(self, _event):
        for row in self.resource_list.selection():
            self._toggle_item(row)
        return 'break'

    def _toggle_item(self, row):
        item = self.items.get(row)
        if item and item.state not in ('等待', '下载中', '处理中'):
            item.selected = not item.selected
            self._render_item(item)
            self._update_queue_status()

    def select_all(self, selected):
        for item in self.items.values():
            if item.state not in ('等待', '下载中', '处理中'):
                item.selected = selected
                self._render_item(item)
        self._update_queue_status()

    def start_download(self):
        if self.close_when_done or self.stopping:
            return
        selected = [item for item in self.items.values()
                    if item.selected and item.state not in ('等待', '下载中', '处理中')]
        if not selected:
            messagebox.showerror('没有选择', '请先解析链接并选择要下载的资源。', parent=self.root)
            return
        directory_text = self.output_directory.get().strip()
        default_directory = Path(directory_text).expanduser().resolve() if directory_text else None
        default_audio = self.download_type.get() == 'audio'
        for item in selected:
            directory = item.report_record.output_directory if item.report_record else default_directory
            if directory is None or not directory.is_dir():
                messagebox.showerror('保存位置无效', f'保存文件夹不存在：{directory or "未选择"}', parent=self.root)
                return

        for item in selected:
            record = item.report_record
            if record is None:
                record = item.previous_records.get((default_directory, default_audio))
            if record is None:
                entry_type = item.info.get('_type')
                download_url = (item.info.get('url') if entry_type in ('url', 'url_transparent')
                                else item.info.get('webpage_url') or item.info.get('url'))
                record = _DownloadRecord(
                    uuid.uuid4().hex, item.title, item.info.get('webpage_url') or download_url or '',
                    download_url or '', item.info.get('ie_key') or '',
                    entry_type if entry_type in ('url', 'url_transparent') else 'url',
                    default_directory, default_audio)
                self.download_records.append(record)
            if not item.report_record:
                item.previous_records[(default_directory, default_audio)] = record
            record.state = '未开始'
            record.detail = ''
            item.selected = False
            item.state = '等待'
            item.detail = ''
            self._render_item(item)
            self.pending.append((item, record))
        self._append_log(f'已加入队列：{len(selected)} 项。')
        self._update_queue_status()
        self._start_next_download()

    def _update_queue_status(self):
        items = self.items.values()
        selected = [item for item in items if item.selected]

        def summarize(label, sizes):
            known = sum(size.known_bytes for size in sizes if size is not None)
            unknown = sum(bool(size.unknown_parts) for size in sizes if size is not None)
            pending = sum(size is None for size in sizes)
            approximate = any(size.approximate for size in sizes if size is not None)
            has_known = any(size is not None and (size.known_bytes or not size.unknown_parts) for size in sizes)
            if (unknown or pending) and not has_known:
                text = f'{label}：未知'
            else:
                text = f'{label}：{"约 " if approximate else ""}{format_bytes(known)}'
            if unknown or pending:
                text += f'（{"已知部分；" if has_known else ""}{unknown} 项未知，{pending} 项获取中）'
            return text

        video = summarize('视频', [item.video_size for item in selected])
        audio = summarize('MP3 源音频', [item.audio_size for item in selected])
        self.queue_status.set(
            f'资源：{len(self.items)} 项，已选择：{len(selected)} 项，'
            f'等待：{len(self.pending)} 项，完成：{sum(item.state == "完成" for item in items)} 项，'
            f'失败：{sum(item.state == "失败" for item in items)} 项\n'
            f'预计下载：{video}  |  {audio}')

    def _start_next_download(self):
        if self.worker is not None or not self.pending or self.close_when_done or self.paused or self.stopping:
            return

        item, record = self.pending.popleft()
        self.current_item = item
        self.current_record = record
        record.state = '下载中'
        item.state = '下载中'
        item.detail = ''
        self._render_item(item)
        self._update_queue_status()
        self._append_log(f'开始下载：{item.title}')
        self.progress.configure(mode='determinate')
        self.progress['value'] = 0
        self.status.set('正在启动...')
        self.cancel_event = threading.Event()
        self.cancel_button.configure(state='normal')
        self.pause_button.configure(state='normal')
        self.worker = threading.Thread(
            target=_download,
            args=(item.info, record.output_directory, record.audio_only, self.cancel_event, self.events),
            daemon=True,
        )
        self.worker.start()
        self.download_started = True

    def toggle_pause(self):
        if self.stopping or (self.worker is None and not self.pending):
            return
        self.paused = not self.paused
        self.pause_button.configure(text='继续' if self.paused else '暂停')
        if self.paused:
            self.status.set('当前资源完成后暂停。' if self.worker else '已暂停。')
        else:
            self.status.set('继续下载。')
            self._start_next_download()

    def cancel_download(self):
        while self.pending:
            item, record = self.pending.popleft()
            item.state = '已停止'
            record.detail = '停止前未开始下载。'
            self._render_item(item)
        self._update_queue_status()
        self.paused = False
        self.pause_button.configure(state='disabled', text='暂停')
        self.cancel_button.configure(state='disabled')
        if self.cancel_event and self.worker:
            self.stopping = True
            self.cancel_event.set()
            self.status.set('正在停止...')
        else:
            self.status.set('已停止。')

    def close(self):
        if self.close_when_done:
            return
        if self.worker is not None or self.scan_worker is not None or self.pending:
            if not messagebox.askyesno(
                    '任务仍在进行', '停止下载和解析，放弃等待中的任务并关闭窗口？', parent=self.root):
                return
            self.close_when_done = True
            self.start_button.configure(state='disabled')
            self.scan_button.configure(state='disabled')
            self.scan_pause_button.configure(state='disabled')
            if self.scan_control:
                self.scan_control.cancel()
            self.cancel_download()
            self._maybe_close()
            return
        self.close_when_done = True
        self._maybe_close()

    def _maybe_close(self):
        if (not self.close_when_done or self.worker is not None or self.scan_worker is not None
                or self.report_prompt_open):
            return False
        if self.download_started:
            self.report_prompt_open = True
            try:
                filename = filedialog.asksaveasfilename(
                    parent=self.root, title='保存下载报告', initialfile='下载报告.csv',
                    defaultextension='.csv', filetypes=[('Excel 可打开的 CSV 文件', '*.csv')])
                if not filename:
                    self.close_when_done = False
                    self.start_button.configure(state='normal')
                    self.scan_button.configure(state='normal')
                    return False
                try:
                    _export_download_report(self.download_records, filename)
                except (OSError, UnicodeError, csv.Error) as error:
                    self._append_log(f'下载报告保存失败：{error}')
                    messagebox.showerror('保存失败', f'无法保存下载报告：{error}', parent=self.root)
                    self.close_when_done = False
                    self.start_button.configure(state='normal')
                    self.scan_button.configure(state='normal')
                    return False
            finally:
                self.report_prompt_open = False
        self.root.destroy()
        return True

    def _poll_events(self):
        sizes_changed = False
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break

            kind, *values = event
            if kind == 'log':
                self._append_log(values[0])
            elif kind == 'scan_item':
                self._add_scan_item(values[0])
                sizes_changed = True
            elif kind == 'scan_size':
                row, video_size, audio_size = values
                if item := self.items.get(row):
                    item.video_size, item.audio_size = video_size, audio_size
                    sizes_changed = True
                self.scan_size_count += 1
                self._update_scan_status()
            elif kind == 'scan_count':
                self.scan_count = values[0]
                self._update_scan_status()
            elif kind == 'scan_list_done':
                self.scan_phase = 'size'
                self.scan_count = values[0]
                self.scan_list_failed = values[1]
                self.scan_size_count = 0
                self._update_scan_status()
            elif kind == 'scan_paused':
                if self.scan_paused:
                    self.scan_pause_ack = True
                    self._update_scan_status()
            elif kind == 'scan_done':
                self._finish_scan(*values)
            elif kind == 'status':
                self.status.set(values[0])
                if self.current_item:
                    self.current_item.state = '处理中'
                    self.current_item.detail = values[0]
                    self._render_item(self.current_item)
            elif kind == 'progress':
                self._update_progress(*values)
            elif kind == 'done':
                self._finish(*values)
            if self._maybe_close():
                return

        if sizes_changed:
            self._update_queue_status()
        self.root.after(100, self._poll_events)

    def _update_progress(self, percentage, message):
        if percentage is None:
            if self.progress.cget('mode') != 'indeterminate':
                self.progress.configure(mode='indeterminate')
                self.progress.start(12)
        else:
            if self.progress.cget('mode') == 'indeterminate':
                self.progress.stop()
                self.progress.configure(mode='determinate')
            self.progress['value'] = percentage
        self.status.set(message)
        if self.current_item:
            self.current_item.state = '下载中'
            self.current_item.detail = f'下载中 {percentage:.1f}%' if percentage is not None else '下载中'
            self._render_item(self.current_item)

    def _finish_scan(self, items, result, input_text):
        self.scan_worker = None
        self.scan_control = None
        self.scan_paused = False
        self.scan_pause_button.configure(state='disabled', text='暂停解析')
        if not self.close_when_done:
            self.scan_button.configure(state='normal')
        for item in items:
            if result != 'success':
                if item.video_size is None:
                    item.video_size = _SizeEstimate(unknown_parts=1)
                if item.audio_size is None:
                    item.audio_size = _SizeEstimate(unknown_parts=1)
            self._add_scan_item(item)
        self._update_queue_status()
        if result == 'success':
            self.scan_status.set(f'清单及大小分析完成：{len(items)} 项')
            if self.url_input.get('1.0', 'end-1c') == input_text:
                self.url_input.delete('1.0', 'end')
        elif result == 'failed':
            self.scan_status.set(f'清单或大小分析未完全成功，已发现：{len(items)} 项，请查看日志')
        else:
            self.scan_status.set('解析已取消。')

    def _add_scan_item(self, item):
        row = str(id(item))
        if row not in self.items:
            self.items[row] = item
            self._render_item(item)

    def _finish(self, result, message):
        self.progress.stop()
        self.progress.configure(mode='determinate')
        if result == 'success':
            self.progress['value'] = 100
        self.status.set(message)
        self._append_log(message)
        if self.current_item:
            self.current_item.state = {'success': '完成', 'failed': '失败', 'cancelled': '已停止'}[result]
            self.current_item.detail = ''
            self._render_item(self.current_item)
        if self.current_record:
            self.current_record.state = {'success': '完成', 'failed': '失败', 'cancelled': '已停止'}[result]
            self.current_record.detail = message
        self.current_item = None
        self.current_record = None
        self.cancel_event = None
        self.worker = None
        self.stopping = False
        self._update_queue_status()
        if not self.pending:
            self.paused = False
            self.cancel_button.configure(state='disabled')
            self.pause_button.configure(state='disabled', text='暂停')
        elif self.paused:
            self.status.set('已暂停。')
        self._start_next_download()

    def _append_log(self, message):
        self.log.configure(state='normal')
        self.log.insert('end', f'{message}\n')
        line_count = int(self.log.index('end-1c').split('.')[0])
        if line_count > 1200:
            self.log.delete('1.0', '201.0')
        self.log.see('end')
        self.log.configure(state='disabled')

    def _clear_log(self):
        self.log.configure(state='normal')
        self.log.delete('1.0', 'end')
        self.log.configure(state='disabled')


def main():
    root = tk.Tk()
    _DownloadApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
