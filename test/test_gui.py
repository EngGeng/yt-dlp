import csv
import queue
import tempfile
import threading
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

try:
    import tkinter  # noqa: F401
except ImportError as error:
    raise unittest.SkipTest('Tkinter is not available') from error

from yt_dlp import gui
from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.utils import ExtractorError


class TestGUIDownloadQueue(unittest.TestCase):
    def setUp(self):
        self.app = app = gui._DownloadApp.__new__(gui._DownloadApp)
        app.events = queue.Queue()
        app.pending = deque()
        app.items = {}
        app.worker = app.cancel_event = None
        app.scan_worker = app.scan_control = None
        app.scan_paused = False
        app.scan_pause_ack = False
        app.scan_phase = 'list'
        app.scan_list_failed = False
        app.scan_count = 0
        app.scan_size_count = 0
        app.current_item = None
        app.current_record = None
        app.download_records = []
        app.download_started = False
        app.report_prompt_open = False
        app.paused = app.stopping = False
        app.close_when_done = False
        app.root = Mock()
        app.url_input = Mock()
        app.output_directory = Mock()
        app.download_type = Mock()
        app.download_type.get.return_value = 'video'
        app.output_directory.get.return_value = '.'
        app.status = Mock()
        app.queue_status = Mock()
        app.scan_status = Mock()
        app.progress = MagicMock()
        app.start_button = Mock()
        app.scan_button = Mock()
        app.scan_pause_button = Mock()
        app.pause_button = Mock()
        app.cancel_button = Mock()
        app.resource_list = Mock()
        app._render_item = Mock()
        app._clear_log = Mock()
        app._append_log = Mock()
        self.real_export_report = gui._export_download_report
        self.thread = self.enterContext(patch.object(gui.threading, 'Thread'))
        self.save_dialog = self.enterContext(patch.object(gui.filedialog, 'asksaveasfilename', return_value='report.csv'))
        self.export_report = self.enterContext(patch.object(gui, '_export_download_report'))

    def add_items(self, count):
        items = [gui._MediaItem(f'Video {i}', {'_type': 'url', 'url': f'https://example.com/{i}'})
                 for i in range(count)]
        for item in items:
            self.app.items[str(id(item))] = item
        return items

    def complete(self, result):
        self.app.events.put(('done', result, result))
        self.app._poll_events()

    def test_selection_append_and_pause_between_items(self):
        items = self.add_items(3)
        items[1].selected = False
        self.app.start_download()
        self.assertEqual(self.thread.call_count, 1)
        self.assertEqual(items[0].state, '下载中')
        self.assertEqual(items[1].state, '待选择')
        self.assertEqual(items[2].state, '等待')
        self.app.toggle_pause()
        self.complete('success')
        self.assertEqual(self.thread.call_count, 1)
        self.assertEqual(items[0].state, '完成')
        self.app.toggle_pause()
        self.assertEqual(self.thread.call_count, 2)
        items[1].selected = True
        self.app.download_type.get.return_value = 'audio'
        self.app.output_directory.get.return_value = 'test'
        self.app.start_download()
        self.complete('failed')
        self.assertEqual(self.thread.call_count, 3)
        self.assertEqual(self.thread.call_args.kwargs['args'][:3], (items[1].info, Path('test').resolve(), True))
        self.complete('success')
        self.assertIsNone(self.app.worker)
        self.assertEqual([item.state for item in items], ['完成', '完成', '失败'])

    def test_stop_cancels_current_and_all_waiting(self):
        items = self.add_items(3)
        self.app.start_download()
        event = self.app.cancel_event
        self.app.cancel_download()
        self.assertTrue(event.is_set())
        self.assertEqual([item.state for item in items[1:]], ['已停止', '已停止'])
        self.complete('cancelled')
        self.assertEqual(self.thread.call_count, 1)
        self.assertEqual([item.state for item in items], ['已停止'] * 3)
        self.assertEqual([record.state for record in self.app.download_records], ['已停止', '未开始', '未开始'])

    def test_stop_while_paused_discards_waiting_items(self):
        items = self.add_items(2)
        self.app.start_download()
        self.app.toggle_pause()
        self.complete('success')
        self.app.cancel_download()
        self.assertEqual(items[1].state, '已停止')
        self.app.toggle_pause()
        self.assertEqual(self.thread.call_count, 1)

    def test_closing_stops_without_starting_next(self):
        self.add_items(2)
        self.app.start_download()
        with patch.object(gui.messagebox, 'askyesno', return_value=True):
            self.app.close()
        self.complete('cancelled')
        self.assertEqual(self.thread.call_count, 1)
        self.app.root.destroy.assert_called_once()
        self.export_report.assert_called_once()

    def test_close_waits_for_scan_to_finish(self):
        self.app.scan_worker = Mock()
        self.app.scan_control = gui._ScanControl(self.app.events)
        self.app.scan_control.pause()
        control = self.app.scan_control
        with patch.object(gui.messagebox, 'askyesno', return_value=True):
            self.app.close()
        with self.assertRaises(gui.DownloadCancelled):
            control.checkpoint()
        self.app.root.destroy.assert_not_called()
        self.app.events.put(('scan_done', [], 'cancelled', 'input'))
        self.app._poll_events()
        self.app.root.destroy.assert_called_once()
        self.save_dialog.assert_not_called()

    def test_close_waits_for_scan_and_download_before_reporting(self):
        self.add_items(2)
        self.app.start_download()
        self.app.scan_worker = Mock()
        self.app.scan_control = gui._ScanControl(self.app.events)
        with patch.object(gui.messagebox, 'askyesno', return_value=True):
            self.app.close()
        self.complete('cancelled')
        self.save_dialog.assert_not_called()
        self.app.root.destroy.assert_not_called()
        self.app.events.put(('scan_done', [], 'cancelled', 'input'))
        self.app._poll_events()
        self.assertEqual([record.state for record in self.app.download_records], ['已停止', '未开始'])
        self.export_report.assert_called_once()
        self.app.root.destroy.assert_called_once()

    def test_close_report_cancel_or_failure_keeps_window_open(self):
        self.add_items(1)
        self.app.start_download()
        self.complete('failed')
        self.save_dialog.return_value = ''
        self.app.close()
        self.app.root.destroy.assert_not_called()
        self.assertFalse(self.app.close_when_done)
        self.export_report.assert_not_called()

        self.save_dialog.return_value = 'report.csv'
        self.export_report.side_effect = OSError('磁盘错误')
        with patch.object(gui.messagebox, 'showerror') as error:
            self.app.close()
        self.assertIn('磁盘错误', error.call_args.args[1])
        self.app.root.destroy.assert_not_called()
        self.assertFalse(self.app.close_when_done)
        self.app.start_button.configure.assert_called_with(state='normal')

        self.export_report.side_effect = None
        self.app.close()
        self.app.root.destroy.assert_called_once()

    def test_retry_replaces_same_task_status_but_new_format_is_separate(self):
        item = self.add_items(1)[0]
        self.app.start_download()
        self.complete('failed')
        item.selected = True
        self.app.start_download()
        self.complete('success')
        self.assertEqual([record.state for record in self.app.download_records], ['完成'])
        item.selected = True
        self.app.download_type.get.return_value = 'audio'
        self.app.start_download()
        self.complete('cancelled')
        self.assertEqual([record.state for record in self.app.download_records], ['完成', '已停止'])
        self.assertEqual([record.audio_only for record in self.app.download_records], [False, True])
        item.selected = True
        self.app.download_type.get.return_value = 'video'
        self.app.start_download()
        self.complete('success')
        self.assertEqual([record.state for record in self.app.download_records], ['完成', '已停止'])

    def test_import_resume_preserves_settings_and_outstanding_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'report.csv'
            records = [
                gui._DownloadRecord('a' * 32, "'=title", 'https://example.com/1', 'custom:1',
                                    'Video', 'url_transparent', Path(directory), True, '失败', '=error'),
                gui._DownloadRecord('b' * 32, 'Stopped', 'https://example.com/2', 'custom:2',
                                    'Video', 'url', Path(directory), False, '已停止'),
                gui._DownloadRecord('c' * 32, 'Done', 'https://example.com/3', 'custom:3',
                                    'Video', 'url', Path(directory), False, '完成'),
            ]
            self.export_report.side_effect = self.real_export_report
            gui._export_download_report(records, path)
            self.assertEqual(gui._read_download_report(path), records)
            with (
                patch.object(gui.filedialog, 'askopenfilename', return_value=str(path)),
                patch.object(gui.messagebox, 'showinfo'),
            ):
                self.app.import_report()
                self.app.import_report()
            self.assertEqual(len(self.app.items), 2)
            self.assertEqual(len(self.app.download_records), 3)
            self.assertFalse(self.app.download_started)
            items = list(self.app.items.values())
            self.assertTrue(all(item.selected for item in items))
            self.assertEqual(items[0].video_size, gui._SizeEstimate(unknown_parts=1))
            self.assertIn('2 项未知，0 项获取中', self.app.queue_status.set.call_args.args[0])
            items[1].selected = False
            self.app.output_directory.get.return_value = '/does/not/exist'
            self.app.download_type.get.return_value = 'video'
            self.app.start_download()
            args = self.thread.call_args.kwargs['args']
            self.assertEqual(args[:3], (
                {'_type': 'url_transparent', 'url': 'custom:1', 'ie_key': 'Video'}, Path(directory), True))
            self.complete('success')
            self.save_dialog.return_value = str(path)
            self.app.close()
            saved = gui._read_download_report(path)
            self.assertEqual([record.state for record in saved], ['完成', '已停止', '完成'])
            self.app.root.destroy.assert_called_once()

    def test_import_without_starting_does_not_prompt_for_report(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'report.csv'
            self.real_export_report([
                gui._DownloadRecord('a' * 32, 'Video', 'https://example.com/1', 'https://example.com/1',
                                    '', 'url', Path(directory), False, '失败'),
            ], path)
            with (
                patch.object(gui.filedialog, 'askopenfilename', return_value=str(path)),
                patch.object(gui.messagebox, 'showinfo'),
            ):
                self.app.import_report()
            self.app.close()
            self.app.root.destroy.assert_called_once()
            self.save_dialog.assert_not_called()

    def test_invalid_report_is_rejected_without_partial_import(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'report.csv'
            path.write_text('不是下载报告\n错误内容\n', encoding='utf-8-sig')
            with (
                patch.object(gui.filedialog, 'askopenfilename', return_value=str(path)),
                patch.object(gui.messagebox, 'showerror') as error,
            ):
                self.app.import_report()
            error.assert_called_once()
            self.assertFalse(self.app.items)
            self.assertFalse(self.app.download_records)

    def test_failed_report_write_does_not_destroy_previous_report(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'report.csv'
            path.write_text('previous report', encoding='utf-8')
            with patch.object(gui.csv, 'writer', side_effect=csv.Error('写入失败')):
                with self.assertRaises(csv.Error):
                    self.real_export_report([], path)
            self.assertEqual(path.read_text(encoding='utf-8'), 'previous report')
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_invalid_line_keeps_batch_in_input(self):
        self.app.url_input.get.return_value = 'https://example.com/valid\nnot a URL\nhttps://example.com/valid'
        with patch.object(gui.messagebox, 'showerror') as error:
            self.app.scan_links()
        self.assertIn('第 2 行', error.call_args.args[1])
        self.app.url_input.delete.assert_not_called()
        self.thread.assert_not_called()

    def test_scan_results_selected_by_default_and_progress_is_visible(self):
        items = self.add_items(2)
        self.app.items.clear()
        self.app.url_input.get.return_value = 'https://example.com/channel'
        self.app._finish_scan(items, 'success', 'https://example.com/channel')
        self.assertTrue(all(item.selected for item in items))
        self.assertEqual(len(self.app.items), 2)
        self.app.select_all(False)
        self.assertFalse(any(item.selected for item in items))
        items[0].selected = True
        self.app.start_download()
        self.app._update_progress(25, '正在下载')
        self.assertEqual(items[0].detail, '下载中 25.0%')

    def test_selected_size_shows_both_types_and_unknowns(self):
        items = self.add_items(3)
        items[0].video_size = gui._SizeEstimate(1024)
        items[0].audio_size = gui._SizeEstimate(512, approximate=True)
        items[1].video_size = gui._SizeEstimate(2048, unknown_parts=1)
        items[1].audio_size = gui._SizeEstimate(256)
        self.app._update_queue_status()
        text = self.app.queue_status.set.call_args.args[0]
        self.assertIn('3.00KiB', text)
        self.assertIn('1 项未知，1 项获取中', text)
        self.assertIn('MP3 源音频：约 768.00B', text)

        self.app._toggle_item(str(id(items[1])))
        text = self.app.queue_status.set.call_args.args[0]
        self.assertIn('视频：1.00KiB', text)
        self.assertIn('MP3 源音频：约 512.00B', text)
        self.assertIn('1 项获取中', text)

        self.app.select_all(False)
        self.assertIn('0.00B', self.app.queue_status.set.call_args.args[0])

    def test_scan_item_updates_both_sizes_before_scan_finishes(self):
        item = gui._MediaItem('Video', {}, video_size=gui._SizeEstimate(2048), audio_size=gui._SizeEstimate(512))
        self.app.events.put(('scan_item', item))
        self.app._poll_events()
        text = self.app.queue_status.set.call_args.args[0]
        self.assertIn('视频：2.00KiB', text)
        self.assertIn('MP3 源音频：512.00B', text)
        self.assertIn(str(id(item)), self.app.items)
        item.selected = False
        self.app._finish_scan([item], 'success', 'input')
        self.assertFalse(item.selected)
        self.assertEqual(len(self.app.items), 1)

    def test_unknown_sizes_are_not_displayed_as_zero(self):
        item = self.add_items(1)[0]
        item.video_size = item.audio_size = gui._SizeEstimate(unknown_parts=1)
        self.app._update_queue_status()
        text = self.app.queue_status.set.call_args.args[0]
        self.assertIn('视频：未知', text)
        self.assertIn('MP3 源音频：未知', text)
        self.assertNotIn('0.00B', text)

    def test_scan_size_event_updates_selected_totals(self):
        item = self.add_items(1)[0]
        self.app.events.put(('scan_size', str(id(item)), gui._SizeEstimate(1000), gui._SizeEstimate(100)))
        self.app._poll_events()
        text = self.app.queue_status.set.call_args.args[0]
        self.assertIn('视频：1000.00B', text)
        self.assertIn('MP3 源音频：100.00B', text)

    def test_scan_phase_and_size_progress_are_visible(self):
        self.app.events.put(('scan_list_done', 2, False))
        self.app._poll_events()
        self.assertIn('清单已完成：2 项', self.app.scan_status.set.call_args.args[0])
        self.assertIn('正在分析大小：0/2 项', self.app.scan_status.set.call_args.args[0])
        self.app.events.put(('scan_size', 'unknown-row', gui._SizeEstimate(100), gui._SizeEstimate(10)))
        self.app._poll_events()
        self.assertIn('正在分析大小：1/2 项', self.app.scan_status.set.call_args.args[0])

    def test_partial_list_is_not_reported_as_complete(self):
        self.app.events.put(('scan_list_done', 2, True))
        self.app._poll_events()
        self.assertIn('清单部分失败：2 项', self.app.scan_status.set.call_args.args[0])

    def test_failed_scan_marks_unfinished_size_as_unknown(self):
        item = self.add_items(1)[0]
        self.app._finish_scan([item], 'failed', 'input')
        self.assertEqual(item.video_size, gui._SizeEstimate(unknown_parts=1))
        self.assertEqual(item.audio_size, gui._SizeEstimate(unknown_parts=1))
        self.assertIn('视频：未知', self.app.queue_status.set.call_args.args[0])

    def test_scan_pause_button_and_acknowledgment(self):
        self.app.scan_worker = Mock()
        self.app.scan_control = gui._ScanControl(self.app.events)
        self.app.toggle_scan_pause()
        self.assertTrue(self.app.scan_paused)
        self.app.events.put(('scan_paused',))
        self.app._poll_events()
        self.assertIn('已暂停', self.app.scan_status.set.call_args.args[0])
        self.app.toggle_scan_pause()
        self.assertFalse(self.app.scan_paused)


class TestGUIExport(unittest.TestCase):
    def test_csv_contains_all_items_and_distinguishes_size_states(self):
        items = [
            gui._MediaItem(
                '中文,标题\n第二行', {'url': 'https://example.com/1'},
                video_size=gui._SizeEstimate(2048), audio_size=gui._SizeEstimate(1024, approximate=True)),
            gui._MediaItem(
                '  =1+1', {'url': '=HYPERLINK("https://example.com")'}, selected=False,
                video_size=gui._SizeEstimate(500, unknown_parts=1), audio_size=None),
            gui._MediaItem('\t+1', {'url': 'https://example.com/3'},
                           video_size=gui._SizeEstimate(0), audio_size=gui._SizeEstimate(unknown_parts=1)),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '清单.csv'
            gui._export_csv(items, path)
            self.assertTrue(path.read_bytes().startswith(b'\xef\xbb\xbf'))
            with path.open(encoding='utf-8-sig', newline='') as source:
                rows = list(csv.reader(source))
        self.assertEqual(rows[0], [
            '资源名称', '资源链接', '视频大小（字节）', '视频大小说明',
            'MP3 源音频大小（字节）', '音频大小说明',
        ])
        self.assertEqual(rows[1], ['中文,标题\n第二行', 'https://example.com/1',
                                   '2048', '已解析', '1024', '估算'])
        self.assertEqual(rows[2], ["'  =1+1", '\'=HYPERLINK("https://example.com")',
                                   '未解析', '未解析', '未解析', '未解析'])
        self.assertEqual(rows[3], ["'\t+1", 'https://example.com/3',
                                   '0', '已解析', '未解析', '未解析'])

    def test_export_dialog_saves_all_items_and_reports_failure(self):
        app = gui._DownloadApp.__new__(gui._DownloadApp)
        app.root = Mock()
        app._append_log = Mock()
        items = [gui._MediaItem('已选', {'url': 'https://example.com/1'}),
                 gui._MediaItem('未选', {'url': 'https://example.com/2'}, selected=False)]
        app.items = {str(id(item)): item for item in items}
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'export.csv')
            with (
                patch.object(gui.filedialog, 'asksaveasfilename', return_value=path),
                patch.object(gui.messagebox, 'showinfo') as success,
            ):
                app.export_list()
            with open(path, encoding='utf-8-sig', newline='') as source:
                self.assertEqual([row[0] for row in list(csv.reader(source))[1:]], ['已选', '未选'])
            self.assertIn('2 项', success.call_args.args[1])

            with (
                patch.object(gui.filedialog, 'asksaveasfilename', return_value=path),
                patch.object(gui, '_export_csv', side_effect=OSError('磁盘错误')),
                patch.object(gui.messagebox, 'showerror') as error,
            ):
                app.export_list()
            self.assertIn('磁盘错误', error.call_args.args[1])

    def test_empty_list_and_cancelled_dialog_do_not_export(self):
        app = gui._DownloadApp.__new__(gui._DownloadApp)
        app.root = Mock()
        app._append_log = Mock()
        app.items = {}
        with patch.object(gui.messagebox, 'showerror') as error:
            app.export_list()
        error.assert_called_once()

        app.items = {'1': gui._MediaItem('Video', {})}
        with (
            patch.object(gui.filedialog, 'asksaveasfilename', return_value=''),
            patch.object(gui, '_export_csv') as export,
        ):
            app.export_list()
        export.assert_not_called()


class TestGUISizeEstimate(unittest.TestCase):
    def test_combined_streams_keep_known_part_and_unknowns(self):
        estimate = gui._format_size({
            'filesize_approx': 9999,
            'requested_formats': [
                {'filesize': 1000},
                {'filesize_approx': 300},
                {},
            ],
        })
        self.assertEqual(estimate, gui._SizeEstimate(1300, 1, True))

    def test_flat_metadata_uses_existing_sizes_and_bitrates_only(self):
        ydl = Mock()
        video, audio = gui._quick_sizes(ydl, {
            '_type': 'url', 'filesize': 1000, 'duration': 10, 'abr': 128,
        })
        self.assertEqual(video, gui._SizeEstimate(1000, approximate=True))
        self.assertEqual(audio, gui._SizeEstimate(160000, approximate=True))
        self.assertEqual(gui._quick_sizes(ydl, {'_type': 'url', 'duration': 10}),
                         (gui._SizeEstimate(unknown_parts=1),) * 2)
        self.assertEqual(gui._quick_sizes(ydl, {
            '_type': 'url',
            'requested_formats': [
                {'vcodec': 'h264', 'acodec': 'none', 'filesize': 900},
                {'vcodec': 'none', 'acodec': 'aac', 'filesize': 100},
            ],
        }), (gui._SizeEstimate(1000, approximate=True), gui._SizeEstimate(100)))
        ydl.process_video_result.assert_not_called()

    def test_existing_formats_are_selected_without_network(self):
        info = {
            'id': '1', 'title': 'Video', 'extractor': 'test', 'extractor_key': 'test',
            'formats': [
                {'format_id': 'av', 'url': 'https://example.com/av.mp4', 'ext': 'mp4',
                 'height': 360, 'vcodec': 'h264', 'acodec': 'aac', 'filesize': 400},
                {'format_id': 'v', 'url': 'https://example.com/v.mp4', 'ext': 'mp4',
                 'height': 1080, 'vcodec': 'h264', 'acodec': 'none', 'filesize': 900},
                {'format_id': 'a', 'url': 'https://example.com/a.m4a', 'ext': 'm4a',
                 'vcodec': 'none', 'acodec': 'aac', 'filesize': 100},
            ],
        }
        with gui.YoutubeDL({'quiet': True}, auto_init=False) as ydl:
            ydl._default_format_spec = lambda _info: 'bestvideo*+bestaudio/best'
            with patch.object(ydl, 'urlopen', side_effect=AssertionError('unexpected network request')):
                sizes = gui._quick_sizes(ydl, info)
        self.assertEqual(sizes, (gui._SizeEstimate(1000), gui._SizeEstimate(100)))

    def test_actual_size_uses_selected_video_and_audio_streams(self):
        events = queue.Queue()
        control = gui._ScanControl(events)

        class VideoIE(InfoExtractor):
            _VALID_URL = r'https://example\.com/video/(?P<id>\d+)'

            def _real_extract(self, url):
                return {
                    'id': self._match_id(url), 'title': 'Video',
                    'formats': [
                        {'format_id': 'v', 'url': 'https://example.com/v.mp4', 'ext': 'mp4',
                         'height': 1080, 'vcodec': 'h264', 'acodec': 'none', 'filesize': 900},
                        {'format_id': 'a', 'url': 'https://example.com/a.m4a', 'ext': 'm4a',
                         'vcodec': 'none', 'acodec': 'aac', 'filesize': 100},
                    ],
                }

        with gui._ScanYoutubeDL({'quiet': True}, control.checkpoint, auto_init=False) as ydl:
            ydl.add_info_extractor(VideoIE(ydl))
            ydl._default_format_spec = lambda _info: 'bestvideo*+bestaudio/best'
            sizes = gui._actual_sizes(
                ydl, {'_type': 'url', 'url': 'https://example.com/video/1', 'ie_key': 'Video'})
        self.assertEqual(sizes, (gui._SizeEstimate(1000), gui._SizeEstimate(100)))


class TestGUIScan(unittest.TestCase):
    def test_all_input_links_are_listed_before_any_size_lookup(self):
        events = queue.Queue()
        urls = ['https://example.com/channel/1', 'https://example.com/channel/2']
        with patch.object(gui, '_ScanYoutubeDL') as ydl, patch.object(gui, '_actual_sizes') as lookup:
            downloader = ydl.return_value.__enter__.return_value
            downloader.extract_info.side_effect = [
                {'_type': 'playlist', 'entries': [
                    {'_type': 'url', 'url': f'https://example.com/video/{i}', 'title': f'Video {i}'},
                ]} for i in (1, 2)
            ]

            def measure(_ydl, info):
                events.put(('size_lookup', info['url']))
                return gui._SizeEstimate(100), gui._SizeEstimate(10)

            lookup.side_effect = measure
            gui._scan(urls, gui._ScanControl(events), events, 'input')
        kinds = [event[0] for event in events.queue]
        self.assertEqual(kinds.count('scan_item'), 2)
        self.assertLess(max(i for i, kind in enumerate(kinds) if kind == 'scan_item'), kinds.index('scan_list_done'))
        self.assertLess(kinds.index('scan_list_done'), kinds.index('size_lookup'))
        self.assertEqual(lookup.call_count, 2)

    def test_scans_nested_playlists_and_preserves_extractor_for_download(self):
        events = queue.Queue()
        real_ydl = gui._ScanYoutubeDL
        extracted = []

        class ChannelIE(InfoExtractor):
            _VALID_URL = r'https://example\.com/channel'
            _RETURN_TYPE = 'playlist'

            def _real_extract(self, url):
                return self.playlist_result([
                    self.url_result('https://example.com/video/1', VideoIE, video_title='First'),
                    self.url_result('https://example.com/playlist', PlaylistIE),
                ], 'channel')

        class PlaylistIE(InfoExtractor):
            _VALID_URL = r'https://example\.com/playlist'
            _RETURN_TYPE = 'playlist'

            def _real_extract(self, url):
                return self.playlist_result([
                    self.url_result('https://example.com/video/2', VideoIE, video_title='Second'),
                ], 'playlist')

        class VideoIE(InfoExtractor):
            _VALID_URL = r'https://example\.com/video/(?P<id>\d+)'
            _RETURN_TYPE = 'video'

            def _real_extract(self, url):
                video_id = self._match_id(url)
                extracted.append(video_id)
                events.put(('video_extract', video_id))
                return {
                    'id': video_id, 'title': f'Video {video_id}',
                    'url': 'https://example.com/media.mp4', 'ext': 'mp4',
                    'vcodec': 'h264', 'acodec': 'aac', 'filesize': 1000,
                }

        def make_ydl(options, checkpoint):
            ydl = real_ydl(options, checkpoint, auto_init=False)
            for ie in (ChannelIE, PlaylistIE, VideoIE):
                ydl.add_info_extractor(ie(ydl))
            return ydl

        with patch.object(gui, '_ScanYoutubeDL', side_effect=make_ydl):
            gui._scan(['https://example.com/channel'], gui._ScanControl(events), events, 'input')
        result = list(events.queue)[-1]
        self.assertEqual((result[0], result[2]), ('scan_done', 'success'))
        self.assertEqual([item.title for item in result[1]], ['First', 'Second'])
        self.assertTrue(all(item.selected for item in result[1]))
        self.assertEqual([item.info['ie_key'] for item in result[1]], ['Video', 'Video'])
        self.assertEqual(extracted, ['1', '2'])
        event_kinds = [event[0] for event in events.queue]
        self.assertLess(max(i for i, kind in enumerate(event_kinds) if kind == 'scan_item'),
                        event_kinds.index('scan_list_done'))
        self.assertLess(event_kinds.index('scan_list_done'), event_kinds.index('video_extract'))
        sizes = [event for event in events.queue if event[0] == 'scan_size']
        self.assertEqual([event[2:] for event in sizes], [
            (gui._SizeEstimate(1000), gui._SizeEstimate(1000)),
        ] * 2)

    def test_scan_error_is_visible_and_does_not_claim_success(self):
        events = queue.Queue()
        with patch.object(gui, '_ScanYoutubeDL') as ydl:
            downloader = ydl.return_value.__enter__.return_value
            downloader.extract_info.side_effect = ValueError('bad channel')
            gui._scan(['https://example.com/channel'], gui._ScanControl(events), events, 'input')
        results = list(events.queue)
        self.assertTrue(any(event[0] == 'log' and 'bad channel' in event[1] for event in results))
        self.assertEqual(results[-1][2], 'failed')

    def test_failed_size_lookup_does_not_stop_remaining_entries(self):
        events = queue.Queue()
        real_ydl = gui._ScanYoutubeDL

        class ChannelIE(InfoExtractor):
            _VALID_URL = r'https://example\.com/channel'

            def _real_extract(self, url):
                return self.playlist_result([
                    self.url_result(f'https://example.com/video/{i}', VideoIE, video_title=f'Video {i}')
                    for i in (1, 2, 3)
                ], 'channel')

        class VideoIE(InfoExtractor):
            _VALID_URL = r'https://example\.com/video/(?P<id>\d+)'
            _RETURN_TYPE = 'video'

            def _real_extract(self, url):
                video_id = self._match_id(url)
                if video_id == '2':
                    raise ExtractorError('Unavailable video', expected=True)
                return {
                    'id': video_id, 'title': f'Video {video_id}',
                    'url': 'https://example.com/media.mp4', 'ext': 'mp4', 'filesize': 1000,
                }

        def make_ydl(options, checkpoint):
            ydl = real_ydl(options, checkpoint, auto_init=False)
            for ie in (ChannelIE, VideoIE):
                ydl.add_info_extractor(ie(ydl))
            return ydl

        with patch.object(gui, '_ScanYoutubeDL', side_effect=make_ydl):
            gui._scan(['https://example.com/channel'], gui._ScanControl(events), events, 'input')
        results = list(events.queue)
        sizes = [event for event in results if event[0] == 'scan_size']
        self.assertEqual(len(sizes), 3)
        self.assertEqual(sizes[1][2:], (gui._SizeEstimate(unknown_parts=1),) * 2)
        self.assertEqual(sizes[2][2], gui._SizeEstimate(1000))
        self.assertTrue(any(event[0] == 'log' and 'Unavailable video' in event[1] for event in results))
        self.assertEqual(results[-1][2], 'failed')

    def test_single_redirect_keeps_original_url_for_download(self):
        events = queue.Queue()
        original_url = 'https://example.com/share/1'
        with patch.object(gui, '_ScanYoutubeDL') as ydl:
            downloader = ydl.return_value.__enter__.return_value
            downloader.extract_info.side_effect = [
                {'_type': 'url_transparent', 'url': 'https://example.com/video/1', 'ie_key': 'Video'},
                {'id': '1', 'title': 'Video', 'url': 'https://example.com/media.mp4', 'filesize': 500},
            ]
            downloader.process_ie_result.return_value = None
            gui._scan([original_url], gui._ScanControl(events), events, original_url)
        item = list(events.queue)[-1][1][0]
        self.assertEqual(item.info['url'], original_url)
        sizes = next(event for event in events.queue if event[0] == 'scan_size')
        self.assertEqual(sizes[2], gui._SizeEstimate(500, approximate=True))

    def test_pause_resume_and_cancel_before_next_playlist_entry(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                second_requested = threading.Event()

                class Events(queue.Queue):
                    paused_once = False

                    def put(self, item, *args, **kwargs):
                        super().put(item, *args, **kwargs)
                        if item[0] == 'scan_item' and not self.paused_once:
                            self.paused_once = True
                            control.pause()

                events = Events()
                control = gui._ScanControl(events)
                real_ydl = gui._ScanYoutubeDL

                class ChannelIE(InfoExtractor):
                    _VALID_URL = r'https://example\.com/channel'

                    def _real_extract(self, url):
                        def entries():
                            yield self.url_result('https://example.com/video/1', VideoIE, video_title='First')
                            second_requested.set()
                            yield self.url_result('https://example.com/video/2', VideoIE, video_title='Second')
                        return self.playlist_result(entries(), 'channel')

                class VideoIE(InfoExtractor):
                    _VALID_URL = r'https://example\.com/video/\d+'
                    _RETURN_TYPE = 'video'

                    def _real_extract(self, url):
                        return {
                            'id': url.rsplit('/', 1)[-1], 'title': 'Video',
                            'url': 'https://example.com/media.mp4', 'ext': 'mp4', 'filesize': 1000,
                        }

                def make_ydl(options, checkpoint):
                    ydl = real_ydl(options, checkpoint, auto_init=False)
                    for ie in (ChannelIE, VideoIE):
                        ydl.add_info_extractor(ie(ydl))
                    return ydl

                with patch.object(gui, '_ScanYoutubeDL', side_effect=make_ydl):
                    worker = threading.Thread(target=gui._scan, args=(
                        ['https://example.com/channel'], control, events, 'input'))
                    worker.start()
                    try:
                        seen = []
                        while not any(event[0] == 'scan_paused' for event in seen):
                            seen.append(events.get(timeout=2))
                        self.assertFalse(second_requested.is_set())
                        if cancel:
                            control.cancel()
                        else:
                            control.resume()
                            self.assertTrue(second_requested.wait(2))
                        worker.join(2)
                        self.assertFalse(worker.is_alive())
                        self.assertEqual(
                            list(events.queue)[-1][0:3:2], ('scan_done', 'cancelled' if cancel else 'success'))
                    finally:
                        control.cancel()
                        worker.join(2)

    def test_pause_blocks_next_network_request(self):
        events = queue.Queue()
        control = gui._ScanControl(events)
        control.pause()
        with gui._ScanYoutubeDL({'quiet': True}, control.checkpoint, auto_init=False) as ydl:
            with patch.object(gui.YoutubeDL, 'urlopen', return_value='response') as urlopen:
                worker = threading.Thread(target=ydl.urlopen, args=('https://example.com',))
                worker.start()
                try:
                    self.assertEqual(events.get(timeout=2), ('scan_paused',))
                    urlopen.assert_not_called()
                    control.resume()
                    worker.join(2)
                    self.assertFalse(worker.is_alive())
                    urlopen.assert_called_once_with('https://example.com')
                finally:
                    control.cancel()
                    worker.join(2)

    def test_pause_at_size_phase_prevents_size_lookup_until_resumed(self):
        class Events(queue.Queue):
            def put(self, item, *args, **kwargs):
                super().put(item, *args, **kwargs)
                if item[0] == 'scan_list_done':
                    control.pause()

        events = Events()
        control = gui._ScanControl(events)
        with patch.object(gui, '_ScanYoutubeDL') as ydl, patch.object(gui, '_actual_sizes') as lookup:
            downloader = ydl.return_value.__enter__.return_value
            downloader.extract_info.return_value = {
                '_type': 'playlist',
                'entries': [{'_type': 'url', 'url': 'https://example.com/video/1', 'title': 'Video'}],
            }
            lookup.return_value = gui._SizeEstimate(100), gui._SizeEstimate(10)
            worker = threading.Thread(target=gui._scan, args=(
                ['https://example.com/channel'], control, events, 'input'))
            worker.start()
            try:
                seen = []
                while not any(event[0] == 'scan_paused' for event in seen):
                    seen.append(events.get(timeout=2))
                self.assertIn('scan_list_done', [event[0] for event in seen])
                lookup.assert_not_called()
                control.resume()
                worker.join(2)
                self.assertFalse(worker.is_alive())
                lookup.assert_called_once()
                self.assertEqual(list(events.queue)[-1][2], 'success')
            finally:
                control.cancel()
                worker.join(2)


class TestGUIFFmpeg(unittest.TestCase):
    def test_ffmpeg_location_on_macos(self):
        installs = {
            None: {'ffmpeg', 'ffprobe'},
            '/opt/homebrew/bin': {'ffmpeg'},
            '/usr/local/bin': {'ffmpeg', 'ffprobe'},
        }

        def which(program, *, path=None):
            return f'/mock/{program}' if program in installs.get(path, ()) else None

        with patch.object(gui.sys, 'platform', 'darwin'), patch.object(gui.shutil, 'which', side_effect=which):
            self.assertIsNone(gui._find_ffmpeg_location())
            installs.pop(None)
            self.assertEqual(gui._find_ffmpeg_location(), '/usr/local/bin')
            installs.pop('/usr/local/bin')
            self.assertIsNone(gui._find_ffmpeg_location())

    def test_download_uses_fallback_location(self):
        with (
            patch.object(gui, '_find_ffmpeg_location', return_value='/opt/homebrew/bin'),
            patch.object(gui, 'YoutubeDL') as ydl,
            patch.object(gui, 'FFmpegPostProcessor') as ffmpeg,
        ):
            ffmpeg.return_value.available = True
            downloader = ydl.return_value.__enter__.return_value
            downloader.download.return_value = 0
            events = queue.Queue()
            gui._download('https://example.com/audio', Path('.'), True, threading.Event(), events)
            self.assertEqual(ydl.call_args.args[0]['ffmpeg_location'], '/opt/homebrew/bin')
            downloader.download.assert_called_once_with(['https://example.com/audio'])
            self.assertEqual(events.get_nowait()[:2], ('done', 'success'))

    def test_missing_ffmpeg_blocks_mp3_but_not_video(self):
        with (
            patch.object(gui, '_find_ffmpeg_location', return_value=None),
            patch.object(gui, 'YoutubeDL') as ydl,
            patch.object(gui, 'FFmpegPostProcessor') as ffmpeg,
        ):
            ffmpeg.return_value.available = False
            downloader = ydl.return_value.__enter__.return_value
            downloader.download.return_value = 0
            events = queue.Queue()
            gui._download('https://example.com/media', Path('.'), True, threading.Event(), events)
            downloader.download.assert_not_called()
            self.assertIn('FFmpeg', events.get_nowait()[1])
            self.assertEqual(events.get_nowait()[:2], ('done', 'failed'))

            gui._download('https://example.com/media', Path('.'), False, threading.Event(), events)
            downloader.download.assert_called_once_with(['https://example.com/media'])
            self.assertEqual(events.get_nowait()[:2], ('done', 'success'))


class TestGUIChannelDownload(unittest.TestCase):
    def test_selected_entry_uses_its_extractor_and_reports_errors(self):
        events = queue.Queue()
        downloaded = []
        real_ydl = gui.YoutubeDL

        class VideoIE(InfoExtractor):
            _VALID_URL = r'custom:(?P<id>\d+)'
            _RETURN_TYPE = 'video'

            def _real_extract(self, url):
                video_id = self._match_id(url)
                if video_id == '2':
                    raise ExtractorError('Unavailable video', expected=True)
                return {'id': video_id, 'title': 'Video', 'url': 'https://example.com/a.mp4', 'ext': 'mp4'}

        def make_ydl(options):
            ydl = real_ydl(options, auto_init=False)
            ydl.add_info_extractor(VideoIE(ydl))
            ydl.process_info = lambda info: downloaded.append(info['id'])
            return ydl

        with patch.object(gui, 'YoutubeDL', side_effect=make_ydl):
            gui._download({'_type': 'url', 'url': 'custom:1', 'ie_key': 'Video'},
                          Path('.'), False, threading.Event(), events)
            self.assertEqual(list(events.queue)[-1][:2], ('done', 'success'))
            gui._download({'_type': 'url', 'url': 'custom:2', 'ie_key': 'Video'},
                          Path('.'), False, threading.Event(), events)
        self.assertEqual(downloaded, ['1'])
        self.assertEqual(list(events.queue)[-1][:2], ('done', 'failed'))

    def run_channel(self, audio_only, cancel_after_first=False):
        steps = []
        events = queue.Queue()
        cancel_event = threading.Event()
        real_ydl = gui.YoutubeDL

        class ChannelIE(InfoExtractor):
            _VALID_URL = r'https://example\.com/channel'

            def _real_extract(self, url):
                def entries():
                    for video_id in ('1', '2', '3'):
                        steps.append(f'list {video_id}')
                        yield self.url_result(f'https://example.com/video/{video_id}', VideoIE)
                return self.playlist_result(entries(), 'channel')

        class VideoIE(InfoExtractor):
            _VALID_URL = r'https://example\.com/video/(?P<id>\d+)'
            _RETURN_TYPE = 'video'

            def _real_extract(self, url):
                video_id = self._match_id(url)
                steps.append(f'extract {video_id}')
                if video_id == '2':
                    raise ExtractorError('Unavailable video', expected=True)
                return {
                    'id': video_id,
                    'title': f'Video {video_id}',
                    'url': 'https://example.com/media.mp4',
                    'ext': 'mp4',
                }

        def make_ydl(options):
            ydl = real_ydl(options, auto_init=False)
            ydl.add_info_extractor(ChannelIE(ydl))
            ydl.add_info_extractor(VideoIE(ydl))

            def process_info(info):
                steps.append(f'download {info["id"]}')
                if cancel_after_first:
                    cancel_event.set()

            ydl.process_info = process_info
            return ydl

        with (
            patch.object(gui, 'YoutubeDL', side_effect=make_ydl),
            patch.object(gui, '_find_ffmpeg_location', return_value=None),
            patch.object(gui, 'FFmpegPostProcessor') as ffmpeg,
        ):
            ffmpeg.return_value.available = True
            gui._download('https://example.com/channel', Path('.'), audio_only, cancel_event, events)
        return steps, list(events.queue)

    def test_channel_downloads_sequentially_and_continues_after_failure(self):
        for audio_only in (False, True):
            with self.subTest(audio_only=audio_only):
                steps, events = self.run_channel(audio_only)
                self.assertEqual(steps, [
                    'list 1', 'extract 1', 'download 1',
                    'list 2', 'extract 2',
                    'list 3', 'extract 3', 'download 3',
                ])
                self.assertTrue(any(event[0] == 'log' and 'Unavailable video' in event[1] for event in events))
                self.assertEqual(events[-1][:2], ('done', 'failed'))

    def test_cancelling_stops_channel(self):
        steps, events = self.run_channel(False, cancel_after_first=True)
        self.assertIn('download 1', steps)
        self.assertNotIn('extract 2', steps)
        self.assertEqual(events[-1][:2], ('done', 'cancelled'))
