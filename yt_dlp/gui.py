import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog
from tkinter import messagebox
from tkinter import scrolledtext
from tkinter import ttk
from urllib.parse import urlsplit

from . import YoutubeDL
from .utils import DownloadCancelled


class _QueueLogger:
    def __init__(self, events):
        self._events = events

    def debug(self, message):
        if not message.startswith('[debug] '):
            self.info(message)

    def info(self, message):
        self._events.put(('log', message))

    def warning(self, message):
        self._events.put(('log', f'警告：{message}'))

    def error(self, message):
        self._events.put(('log', f'错误：{message}'))


def _download(url, output_directory, audio_only, cancel_event, events):
    def check_cancelled(*_args, **_kwargs):
        if cancel_event.is_set():
            raise DownloadCancelled('Download cancelled from GUI')

    def progress_hook(data):
        check_cancelled()
        status = data.get('status')
        if status == 'finished':
            events.put(('progress', 100, f'已下载：{Path(data["filename"]).name}'))
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

    options = {
        'outtmpl': str(output_directory / '%(title)s [%(id)s].%(ext)s'),
        'quiet': True,
        'logger': _QueueLogger(events),
        'progress_hooks': [progress_hook],
        'postprocessor_hooks': [postprocessor_hook],
        'match_filter': check_cancelled,
    }
    if audio_only:
        options.update({
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
            }],
        })

    try:
        with YoutubeDL(options) as ydl:
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
        self.cancel_event = None
        self.worker = None
        self.close_when_done = False

        self.url = tk.StringVar()
        self.output_directory = tk.StringVar(value=str(Path.home() / 'Downloads'))
        self.download_type = tk.StringVar(value='video')
        self.status = tk.StringVar(value='就绪')

        root.title('yt-dlp')
        root.geometry('720x580')
        root.minsize(620, 480)
        root.protocol('WM_DELETE_WINDOW', self.close)
        self._build_ui()
        root.after(100, self._poll_events)

    def _build_ui(self):
        frame = ttk.Frame(self.root, padding=18)
        frame.pack(fill='both', expand=True)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(8, weight=1)

        ttk.Label(frame, text='yt-dlp', font=('TkDefaultFont', 20, 'bold')).grid(
            row=0, column=0, columnspan=3, sticky='w', pady=(0, 14))

        ttk.Label(frame, text='媒体链接').grid(row=1, column=0, sticky='w', padx=(0, 10), pady=5)
        self.url_entry = ttk.Entry(frame, textvariable=self.url)
        self.url_entry.grid(row=1, column=1, columnspan=2, sticky='ew', pady=5)
        self.url_entry.bind('<Return>', lambda _event: self.start_download())

        ttk.Label(frame, text='保存位置').grid(row=2, column=0, sticky='w', padx=(0, 10), pady=5)
        self.directory_entry = ttk.Entry(frame, textvariable=self.output_directory)
        self.directory_entry.grid(row=2, column=1, sticky='ew', pady=5)
        self.browse_button = ttk.Button(frame, text='选择...', command=self.choose_directory)
        self.browse_button.grid(row=2, column=2, padx=(8, 0), pady=5)

        type_frame = ttk.LabelFrame(frame, text='下载类型', padding=(10, 5))
        type_frame.grid(row=3, column=0, columnspan=3, sticky='ew', pady=(8, 12))
        self.video_button = ttk.Radiobutton(
            type_frame, text='视频', variable=self.download_type, value='video')
        self.video_button.pack(side='left', padx=(0, 18))
        self.audio_button = ttk.Radiobutton(
            type_frame, text='MP3 音频（需要 FFmpeg）', variable=self.download_type, value='audio')
        self.audio_button.pack(side='left')

        self.progress = ttk.Progressbar(frame, maximum=100, mode='determinate')
        self.progress.grid(row=4, column=0, columnspan=3, sticky='ew', pady=(0, 6))
        ttk.Label(frame, textvariable=self.status).grid(row=5, column=0, columnspan=3, sticky='w')

        button_frame = ttk.Frame(frame)
        button_frame.grid(row=6, column=0, columnspan=3, sticky='ew', pady=10)
        self.start_button = ttk.Button(
            button_frame, text='开始下载', command=self.start_download, default='active')
        self.start_button.pack(side='left')
        self.cancel_button = ttk.Button(
            button_frame, text='取消', command=self.cancel_download, state='disabled')
        self.cancel_button.pack(side='left', padx=(8, 0))

        ttk.Label(frame, text='日志').grid(row=7, column=0, columnspan=3, sticky='w', pady=(4, 5))
        self.log = scrolledtext.ScrolledText(frame, wrap='word', height=10, state='disabled')
        self.log.grid(row=8, column=0, columnspan=3, sticky='nsew')

    def choose_directory(self):
        directory = filedialog.askdirectory(
            parent=self.root,
            title='选择下载文件夹',
            initialdir=self.output_directory.get() or str(Path.home()))
        if directory:
            self.output_directory.set(directory)

    def start_download(self):
        if self.worker and self.worker.is_alive():
            return

        url = self.url.get().strip()
        try:
            parsed_url = urlsplit(url)
        except ValueError:
            parsed_url = None
        if (not parsed_url
                or parsed_url.scheme.lower() not in ('http', 'https')
                or not parsed_url.netloc):
            messagebox.showerror('链接无效', '请输入有效的 HTTP 或 HTTPS 媒体链接。', parent=self.root)
            return

        directory_text = self.output_directory.get().strip()
        output_directory = Path(directory_text).expanduser() if directory_text else None
        if output_directory is None or not output_directory.is_dir():
            messagebox.showerror('保存位置无效', '请选择一个已存在的文件夹。', parent=self.root)
            return

        self._clear_log()
        self.progress.configure(mode='determinate')
        self.progress['value'] = 0
        self.status.set('正在启动...')
        self.cancel_event = threading.Event()
        self._set_running(True)
        self.worker = threading.Thread(
            target=_download,
            args=(url, output_directory, self.download_type.get() == 'audio',
                  self.cancel_event, self.events),
            daemon=True,
        )
        self.worker.start()

    def cancel_download(self):
        if self.cancel_event and self.worker and self.worker.is_alive():
            self.cancel_event.set()
            self.cancel_button.configure(state='disabled')
            self.status.set('正在取消...')

    def close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(
                    '下载仍在进行', '取消下载并关闭窗口？', parent=self.root):
                return
            self.close_when_done = True
            self.cancel_download()
            return
        self.root.destroy()

    def _poll_events(self):
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break

            kind, *values = event
            if kind == 'log':
                self._append_log(values[0])
            elif kind == 'status':
                self.status.set(values[0])
            elif kind == 'progress':
                self._update_progress(*values)
            elif kind == 'done':
                self._finish(*values)
                if self.close_when_done:
                    self.root.destroy()
                    return

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

    def _finish(self, result, message):
        self.progress.stop()
        self.progress.configure(mode='determinate')
        if result == 'success':
            self.progress['value'] = 100
        self.status.set(message)
        self._set_running(False)
        self.worker = None

    def _set_running(self, running):
        state = 'disabled' if running else 'normal'
        for widget in (
                self.url_entry, self.directory_entry, self.browse_button,
                self.video_button, self.audio_button, self.start_button):
            widget.configure(state=state)
        self.cancel_button.configure(state='normal' if running else 'disabled')

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
