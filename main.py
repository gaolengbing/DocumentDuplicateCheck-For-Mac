import os
import hashlib
import sys
import subprocess
import traceback
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QPushButton, QLabel, QProgressBar, QTreeWidget, QTreeWidgetItem,
                             QFileDialog, QMessageBox, QHeaderView, QAbstractItemView, QMenu,
                             QCheckBox, QGroupBox, QFormLayout, QLineEdit, QFrame, QGridLayout,
                             QSplitter, QStatusBar, QScrollArea, QComboBox)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QObject, QSize, QMutex, QWaitCondition, QSemaphore, QTimer, \
    QMetaObject, Q_ARG
from PyQt5.QtGui import QFont, QColor, QBrush, QPen, QPainter, QIcon

# 尝试导入快速哈希库，如果没有则使用标准库
try:
    import xxhash

    HAS_XXHASH = True
except ImportError:
    HAS_XXHASH = False


# 全局异常处理函数
def handle_exception(exc_type, exc_value, exc_traceback):
    """处理未捕获的全局异常"""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return

    # 记录异常信息到日志文件
    log_message = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 未捕获异常: {exc_type.__name__}: {exc_value}\n"
    log_message += "".join(traceback.format_tb(exc_traceback)) + "\n"
    save_error_log(log_message)

    # 显示错误消息给用户
    QMessageBox.critical(
        None,
        "程序错误",
        f"程序发生错误，已记录到日志文件。\n错误信息: {str(exc_value)}\n请查看 error_log.txt 获取详细信息。"
    )


# 设置全局异常钩子
sys.excepthook = handle_exception


def save_error_log(message):
    """保存错误日志到文件"""
    try:
        log_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
        log_path = os.path.join(log_dir, "error_log.txt")

        with open(log_path, "a", encoding="utf-8") as f:
            f.write(message)
    except Exception as e:
        print(f"保存错误日志失败: {e}")
        print("错误详情:", message)


def calculate_hash(file_path, hash_algorithm='md5', chunk_size=8192, partial=False):
    """
    计算文件的哈希值，支持多种算法和部分哈希

    参数:
        file_path: 文件路径
        hash_algorithm: 哈希算法，支持'md5', 'sha1', 'sha256', 'xxhash'
        chunk_size: 读取块大小
        partial: 是否只计算部分内容的哈希（前1MB和后1MB）

    返回:
        哈希值字符串，计算失败返回None
    """
    try:
        # 根据算法类型初始化哈希对象
        if hash_algorithm == 'md5':
            hash_obj = hashlib.md5()
        elif hash_algorithm == 'sha1':
            hash_obj = hashlib.sha1()
        elif hash_algorithm == 'sha256':
            hash_obj = hashlib.sha256()
        elif hash_algorithm == 'xxhash' and HAS_XXHASH:
            hash_obj = xxhash.xxh64()
        else:
            # 默认使用md5
            hash_obj = hashlib.md5()
            hash_algorithm = 'md5'

        with open(file_path, "rb") as f:
            if partial:
                # 部分哈希：读取前1MB和后1MB
                file_size = os.path.getsize(file_path)

                # 读取前1MB
                bytes_read = f.read(min(1024 * 1024, file_size))
                hash_obj.update(bytes_read)

                # 如果文件大于2MB，再读取最后1MB
                if file_size > 2 * 1024 * 1024:
                    f.seek(max(0, file_size - 1024 * 1024))
                    bytes_read = f.read()
                    hash_obj.update(bytes_read)
            else:
                # 完整哈希：读取整个文件
                while True:
                    bytes_read = f.read(chunk_size)
                    if not bytes_read:
                        break
                    hash_obj.update(bytes_read)

        if hash_algorithm == 'xxhash' and HAS_XXHASH:
            return hash_obj.hexdigest()
        else:
            return hash_obj.hexdigest()

    except Exception as e:
        print(f"计算哈希值时出错 {file_path}: {str(e)}")
        return None


class ScanWorker(QObject):
    """后台扫描线程，负责文件扫描和重复检测"""
    progress_updated = pyqtSignal(int)
    status_updated = pyqtSignal(str)
    scan_finished = pyqtSignal(list)
    scan_stopped = pyqtSignal()
    finished = pyqtSignal()  # 完成信号
    error_occurred = pyqtSignal(str)  # 错误发生信号

    # 用于限制并发文件句柄数量的信号量
    _file_semaphore = None
    # 用于保护共享数据的互斥锁
    _mutex = QMutex()

    def __init__(self, folder_path, compare_name=True, compare_size=True, compare_hash=False,
                 hash_algorithm='md5', scan_by_name=False, scan_by_size=False, scan_by_hash=False,
                 fast_scan_mode=False):
        super().__init__()
        self.folder_path = folder_path
        self.stop_scan = False
        self.compare_name = compare_name
        self.compare_size = compare_size
        self.compare_hash = compare_hash  # 替代原有的compare_md5
        self.hash_algorithm = hash_algorithm  # 哈希算法选择
        self.scan_by_name = scan_by_name
        self.scan_by_size = scan_by_size
        self.scan_by_hash = scan_by_hash  # 替代原有的scan_by_md5
        self.fast_scan_mode = fast_scan_mode

        # 自动调整线程池大小
        self._adjust_worker_count()

        # 初始化文件句柄信号量
        max_open_files = min(1024, self.max_workers * 2)
        ScanWorker._file_semaphore = QSemaphore(max_open_files)

        self.total_files = 0
        self.processed_files = 0
        self.file_groups = {}  # 用于存储文件组的共享数据

        # 预分配缓冲区
        self._buffer = bytearray(1024 * 1024)  # 1MB缓冲区

    def _adjust_worker_count(self):
        """根据系统配置自动调整线程池大小"""
        try:
            # 获取系统内存（GB）
            if sys.platform.startswith('linux'):
                with open('/proc/meminfo', 'r') as f:
                    mem_info = f.read()
                    mem_total = int(mem_info.split('MemTotal:')[1].split()[0]) / (1024 * 1024)  # GB
            elif sys.platform == 'win32':
                import ctypes
                class MEMORYSTATUSEX(ctypes.Structure):
                    _fields_ = [
                        ("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                    ]

                stat = MEMORYSTATUSEX()
                stat.dwLength = ctypes.sizeof(stat)
                ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
                mem_total = stat.ullTotalPhys / (1024 ** 3)  # GB
            else:  # macOS及其他系统
                mem_total = 8  # 默认值

            # 基于内存和CPU核心数计算最佳线程数
            cpu_count = os.cpu_count() or 4
            if mem_total < 4:
                self.max_workers = max(2, cpu_count)
            elif mem_total < 8:
                self.max_workers = max(4, cpu_count * 2)
            else:
                self.max_workers = max(8, cpu_count * 4)

            # 限制最大线程数
            self.max_workers = min(self.max_workers, 64)

        except:
            # 失败时使用默认值
            self.max_workers = max(4, (os.cpu_count() or 4) * 2)

    def run(self):
        try:
            # 第一阶段：收集文件信息
            self.status_updated.emit("正在收集文件信息...")
            file_list = self.collect_file_metadata()

            if self.stop_scan:
                self.scan_stopped.emit()
                self.finished.emit()
                return

            self.total_files = len(file_list)
            self.processed_files = 0
            self.status_updated.emit(f"发现 {self.total_files} 个文件，开始分析...")

            # 第二阶段：处理文件 - 采用分阶段哈希比较策略提高效率
            self.process_files_in_phases(file_list)

            if self.stop_scan:
                self.scan_stopped.emit()
                self.finished.emit()
                return

            # 筛选出重复文件（至少2个文件）
            duplicates = [group for group in self.file_groups.values() if len(group) >= 2]
            self.scan_finished.emit(duplicates)

        except Exception as e:
            error_msg = f"扫描过程出错: {str(e)}"
            self.status_updated.emit(error_msg)
            self.error_occurred.emit(error_msg)
            # 记录错误日志
            log_msg = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {error_msg}\n"
            log_msg += "".join(traceback.format_exc()) + "\n"
            save_error_log(log_msg)
        finally:
            self.finished.emit()  # 最终发射finished信号

    def collect_file_metadata(self):
        """收集文件元数据（路径、大小）"""
        file_list = []
        try:
            with os.scandir(self.folder_path) as entries:
                for entry in entries:
                    if self.stop_scan:
                        return []

                    try:
                        if entry.is_dir(follow_symlinks=False):
                            # 递归处理子目录
                            subdir_files = self.collect_file_metadata_from_dir(entry.path)
                            file_list.extend(subdir_files)
                        elif entry.is_file(follow_symlinks=False):
                            # 获取文件大小（精确到字节）
                            file_size = entry.stat().st_size
                            if file_size > 0:  # 忽略空文件
                                file_list.append({
                                    'path': entry.path,
                                    'name': entry.name,
                                    'size': file_size,
                                    'partial_hash': None,  # 部分内容哈希
                                    'full_hash': None  # 完整内容哈希
                                })
                    except Exception as e:
                        error_msg = f"访问 {entry.name} 时出错: {str(e)}"
                        print(error_msg)
                        log_msg = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {error_msg}\n"
                        log_msg += "".join(traceback.format_exc()) + "\n"
                        save_error_log(log_msg)
            return file_list
        except Exception as e:
            error_msg = f"收集文件信息时出错: {str(e)}"
            self.error_occurred.emit(error_msg)
            log_msg = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {error_msg}\n"
            log_msg += "".join(traceback.format_exc()) + "\n"
            save_error_log(log_msg)
            return []

    def collect_file_metadata_from_dir(self, dir_path):
        """从目录收集文件元数据"""
        file_list = []
        try:
            with os.scandir(dir_path) as entries:
                for entry in entries:
                    if self.stop_scan:
                        return []

                    try:
                        if entry.is_dir(follow_symlinks=False):
                            # 递归处理子目录
                            subdir_files = self.collect_file_metadata_from_dir(entry.path)
                            file_list.extend(subdir_files)
                        elif entry.is_file(follow_symlinks=False):
                            # 获取文件大小（精确到字节）
                            file_size = entry.stat().st_size
                            if file_size > 0:  # 忽略空文件
                                file_list.append({
                                    'path': entry.path,
                                    'name': entry.name,
                                    'size': file_size,
                                    'partial_hash': None,  # 部分内容哈希
                                    'full_hash': None  # 完整内容哈希
                                })
                    except Exception as e:
                        # 忽略无法访问的文件/目录
                        continue
            return file_list
        except Exception as e:
            # 忽略无法访问的目录
            return []

    def process_files_in_phases(self, file_list):
        """分阶段处理文件：大小 -> 部分哈希 -> 完整哈希"""
        # 阶段1: 按快速可获取的属性初步分组（大小、名称）
        phase1_groups = self.group_files_by_fast_properties(file_list)

        # 阶段2: 对每组文件进行更详细的比较
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = []

            # 提交每组文件的处理任务
            for group_key, files in phase1_groups.items():
                if self.stop_scan:
                    break

                # 只有组内文件数 > 1时才需要进一步处理
                if len(files) > 1:
                    future = executor.submit(
                        self.process_file_group,
                        group_key,
                        files
                    )
                    futures.append(future)

            # 处理完成的任务
            total_groups = len(futures)
            completed_groups = 0

            for future in as_completed(futures):
                if self.stop_scan:
                    # 取消所有未完成的任务
                    for f in futures:
                        if not f.done():
                            f.cancel()
                    break

                try:
                    future.result()  # 处理可能的异常
                except Exception as e:
                    error_msg = f"处理文件组时出错: {str(e)}"
                    print(error_msg)
                    log_msg = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {error_msg}\n"
                    log_msg += "".join(traceback.format_exc()) + "\n"
                    save_error_log(log_msg)
                    self.error_occurred.emit(f"处理文件组时出错: {str(e)}")

                # 更新进度
                completed_groups += 1
                progress = (completed_groups / total_groups) * 100
                self.progress_updated.emit(int(progress))

                if completed_groups % 10 == 0:
                    self.status_updated.emit(f"正在分析: 已完成 {completed_groups}/{total_groups} 组文件")

    def group_files_by_fast_properties(self, file_list):
        """按快速可获取的属性对文件进行初步分组"""
        groups = {}

        for file_info in file_list:
            if self.stop_scan:
                return {}

            key_parts = []

            # 根据选择的扫描方式生成初步分组键
            if self.scan_by_name:
                key_parts.append(file_info['name'].lower())
            elif self.scan_by_size:
                key_parts.append(str(file_info['size']))  # 精确到字节
            elif self.scan_by_hash:
                # 哈希扫描也先按大小分组（精确到字节）
                key_parts.append(str(file_info['size']))
            else:
                if self.compare_name:
                    key_parts.append(file_info['name'].lower())
                if self.compare_size:
                    key_parts.append(str(file_info['size']))  # 精确到字节

            group_key = "|".join(key_parts)

            if group_key not in groups:
                groups[group_key] = []
            groups[group_key].append(file_info)

        return groups

    def process_file_group(self, group_key, files):
        """处理单个文件组，采用分阶段哈希比较策略"""
        if self.stop_scan:
            return

        try:
            # 确定是否需要计算哈希
            need_hash = self.scan_by_hash or self.compare_hash

            # 阶段1: 计算部分哈希（前1MB和后1MB）- 快速排除非重复文件
            if need_hash:
                for file_info in files:
                    if self.stop_scan:
                        return

                    # 计算部分哈希
                    file_info['partial_hash'] = calculate_hash(
                        file_info['path'],
                        self.hash_algorithm,
                        partial=True
                    )

            # 阶段2: 使用部分哈希进行再次分组
            partial_groups = {}
            for file_info in files:
                if self.stop_scan:
                    return

                partial_key = f"{file_info['partial_hash'] or 'unknown'}"
                if partial_key not in partial_groups:
                    partial_groups[partial_key] = []
                partial_groups[partial_key].append(file_info)

            # 阶段3: 只对部分哈希相同的组计算完整哈希
            final_groups = {}
            for partial_key, partial_group in partial_groups.items():
                # 如果组内只有一个文件，无需进一步处理
                if len(partial_group) <= 1:
                    final_key = f"{group_key}|{partial_key}"
                    final_groups[final_key] = partial_group
                    continue

                # 计算完整哈希
                for file_info in partial_group:
                    if self.stop_scan:
                        return

                    # 计算完整哈希
                    file_info['full_hash'] = calculate_hash(
                        file_info['path'],
                        self.hash_algorithm,
                        partial=False
                    )

                # 使用完整哈希进行最终分组
                for file_info in partial_group:
                    final_key_parts = [group_key, partial_key]
                    final_key_parts.append(file_info['full_hash'] or 'unknown')
                    final_key = "|".join(final_key_parts)

                    if final_key not in final_groups:
                        final_groups[final_key] = []
                    final_groups[final_key].append(file_info)

            # 将结果合并到共享的文件组中
            ScanWorker._mutex.lock()
            try:
                for key, group in final_groups.items():
                    # 确保每个组都有唯一标识
                    if key not in self.file_groups:
                        self.file_groups[key] = []
                    self.file_groups[key].extend(group)
            finally:
                ScanWorker._mutex.unlock()

        except Exception as e:
            raise Exception(f"处理文件组时出错: {str(e)}")

    def stop(self):
        """停止扫描"""
        self.stop_scan = True


class GroupSeparatorTreeWidget(QTreeWidget):
    """带组分隔线的自定义TreeWidget"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.group_items = {}  # 存储每组的所有项
        self.setAlternatingRowColors(True)
        self.setStyleSheet("""
            QTreeWidget {
                border: 1px solid #e0e0e0;
                border-radius: 4px;
                padding: 5px;
                background-color: #ffffff;
                color: #333333;
                font-size: 13px;
            }
            QTreeWidget::item {
                height: 25px;
                border-radius: 2px;
            }
            QTreeWidget::item:selected {
                background-color: #e8f4fd;
                color: #0d6efd;
            }
            QTreeWidget::item:hover {
                background-color: #f0f7ff;
            }
            QHeaderView::section {
                background-color: #f8f9fa;
                color: #495057;
                padding: 8px;
                border: 1px solid #e0e0e0;
                font-weight: bold;
                font-size: 12px;
            }
        """)

    def paintEvent(self, event):
        """重绘事件，绘制组之间的分隔线"""
        try:
            super().paintEvent(event)

            if self.topLevelItemCount() == 0:
                return

            painter = QPainter(self.viewport())
            pen = QPen(QColor(220, 220, 220), 1)
            painter.setPen(pen)

            last_items = {}
            for i in range(self.topLevelItemCount()):
                item = self.topLevelItem(i)
                group_id = item.text(1)
                last_items[group_id] = i

            for group_id, last_idx in last_items.items():
                if last_idx < self.topLevelItemCount() - 1:
                    item = self.topLevelItem(last_idx)
                    rect = self.visualItemRect(item)
                    painter.drawLine(
                        rect.left(),
                        rect.bottom(),
                        rect.right(),
                        rect.bottom()
                    )
        except Exception as e:
            error_msg = f"绘制界面时出错: {str(e)}"
            print(error_msg)
            log_msg = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {error_msg}\n"
            log_msg += "".join(traceback.format_exc()) + "\n"
            save_error_log(log_msg)
            QMessageBox.warning(self.parent(), "界面错误", error_msg)


def format_file_size(size_bytes):
    """将字节大小转换为合适的单位显示"""
    try:
        if size_bytes < 1024:
            return f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.2f} KB"
        elif size_bytes < 1024 * 1024 * 1024:
            return f"{size_bytes / (1024 * 1024):.2f} MB"
        else:
            return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"
    except Exception as e:
        error_msg = f"格式化文件大小时出错: {str(e)}"
        print(error_msg)
        log_msg = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {error_msg}\n"
        log_msg += "".join(traceback.format_exc()) + "\n"
        save_error_log(log_msg)
        return f"{size_bytes} B"


def parse_file_size(size_str):
    """将带单位的大小字符串转换为字节数"""
    try:
        if not size_str:
            return None

        size_str = size_str.strip().upper()
        units = {
            'B': 1,
            'KB': 1024,
            'MB': 1024 * 1024,
            'GB': 1024 * 1024 * 1024
        }

        # 处理可能包含的范围分隔符
        separators = ['至', '-', '~']
        for sep in separators:
            if sep in size_str:
                # 只取第一个值作为解析结果
                size_str = size_str.split(sep)[0].strip()
                break

        for unit, multiplier in units.items():
            if size_str.endswith(unit):
                try:
                    value = float(size_str[:-len(unit)])
                    return int(value * multiplier)
                except ValueError:
                    return None
        return None
    except Exception as e:
        error_msg = f"解析文件大小时出错: {str(e)}"
        print(error_msg)
        log_msg = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {error_msg}\n"
        log_msg += "".join(traceback.format_exc()) + "\n"
        save_error_log(log_msg)
        return None


class FileDuplicateChecker(QMainWindow):
    """主窗口类，支持多种扫描方式和自适应布局"""

    def __init__(self):
        try:
            super().__init__()
            self.folder_path = ""
            self.scan_thread = None
            self.scan_worker = None
            self.duplicates = []  # 存储所有重复文件组
            self.filtered_duplicates = []  # 存储过滤后的重复文件组
            self.group_colors = []  # 用于存储每组的颜色

            # 确保中文显示正常
            font = QFont()
            if sys.platform.startswith('darwin'):  # macOS
                font.setFamily("Heiti TC")  # macOS 黑体
            else:  # Windows
                font.setFamily("SimHei")  # Windows 黑体
            self.setFont(font)

            # 设置全局样式
            self.setStyleSheet("""
                QMainWindow {
                    background-color: #f8f9fa;
                }
                QLabel {
                    color: #495057;
                    font-size: 13px;
                }
                QPushButton {
                    background-color: #0d6efd;
                    color: white;
                    border: none;
                    padding: 6px 12px;
                    border-radius: 4px;
                    font-size: 13px;
                    min-width: 80px;
                }
                QPushButton:hover {
                    background-color: #0b5ed7;
                }
                QPushButton:pressed {
                    background-color: #0a58ca;
                }
                QPushButton:disabled {
                    background-color: #6c757d;
                    color: #ffffff;
                    opacity: 0.65;
                }
                QGroupBox {
                    border: 1px solid #e0e0e0;
                    border-radius: 4px;
                    margin-top: 10px;
                    padding: 10px;
                    color: #495057;
                    font-weight: bold;
                    font-size: 14px;
                }
                QGroupBox::title {
                    subcontrol-origin: margin;
                    left: 10px;
                    padding: 0 3px 0 3px;
                }
                QLineEdit {
                    padding: 5px;
                    border: 1px solid #ced4da;
                    border-radius: 4px;
                    font-size: 13px;
                    background-color: white;
                }
                QLineEdit:focus {
                    border-color: #80bdff;
                    outline: 0;
                }
                QCheckBox {
                    color: #495057;
                    font-size: 13px;
                    spacing: 5px;
                }
                QProgressBar {
                    border: 1px solid #e0e0e0;
                    border-radius: 4px;
                    text-align: center;
                    height: 24px;
                    background-color: #ffffff;
                }
                QProgressBar::chunk {
                    background-color: #0d6efd;
                    border-radius: 3px;
                }
                QMessageBox {
                    background-color: #ffffff;
                    font-size: 13px;
                }
                QMessageBox QLabel {
                    font-size: 13px;
                }
                QMenu {
                    background-color: white;
                    border: 1px solid #e0e0e0;
                    border-radius: 4px;
                    padding: 5px 0;
                }
                QMenu::item {
                    padding: 5px 20px;
                    color: #333333;
                    font-size: 13px;
                }
                QMenu::item:selected {
                    background-color: #e8f4fd;
                    color: #0d6efd;
                }
                .info-panel {
                    background-color: #e9f7fe;
                    border-left: 4px solid #0d90e8;
                    padding: 8px 12px;
                    border-radius: 0 4px 4px 0;
                }
                .info-label {
                    color: #0a6aa1;
                    font-size: 12px;
                    line-height: 1.5;
                }
                QSplitter::handle {
                    background-color: #e0e0e0;
                    border: 1px solid #ccc;
                }
                QSplitter::handle:hover {
                    background-color: #ccc;
                }
                QScrollArea {
                    border: none;
                }
                QScrollArea::viewport {
                    background-color: transparent;
                }
                QComboBox {
                    padding: 5px;
                    border: 1px solid #ced4da;
                    border-radius: 4px;
                    font-size: 13px;
                    background-color: white;
                    min-width: 150px;
                }
            """)

            self.init_ui()
        except Exception as e:
            error_msg = f"初始化程序时出错: {str(e)}"
            print(error_msg)
            log_msg = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {error_msg}\n"
            log_msg += "".join(traceback.format_exc()) + "\n"
            save_error_log(log_msg)
            QMessageBox.critical(None, "初始化错误", error_msg)
            sys.exit(1)

    def init_ui(self):
        """初始化用户界面"""
        try:
            self.setWindowTitle("文件查重工具V1.0 - 增强版")
            self.setGeometry(100, 100, 1400, 900)  # 初始窗口大小

            # 创建主部件和布局
            central_widget = QWidget()
            self.setCentralWidget(central_widget)
            main_layout = QVBoxLayout(central_widget)
            main_layout.setContentsMargins(15, 15, 15, 15)
            main_layout.setSpacing(12)

            # 顶部标题区域
            title_frame = QFrame()
            title_frame.setStyleSheet("background-color: #0d6efd; border-radius: 6px;")
            title_frame.setMinimumHeight(60)
            title_layout = QHBoxLayout(title_frame)
            title_layout.setContentsMargins(20, 0, 20, 0)

            title_label = QLabel("文件查重工具V1.0 - 增强版")
            title_label.setStyleSheet("color: white; font-size: 20px; font-weight: bold;")
            title_layout.addWidget(title_label)

            subtitle_label = QLabel("高效查找并管理重复文件，支持多种哈希算法")
            subtitle_label.setStyleSheet("color: rgba(255, 255, 255, 0.9); font-size: 13px; margin-left: 15px;")
            title_layout.addWidget(subtitle_label)

            title_layout.addStretch()
            main_layout.addWidget(title_frame)

            # 创建水平分割器
            main_splitter = QSplitter(Qt.Horizontal)
            main_splitter.setHandleWidth(6)

            # 左侧：主控制区
            left_panel = QWidget()
            left_layout = QVBoxLayout(left_panel)
            left_layout.setContentsMargins(0, 0, 0, 0)
            left_layout.setSpacing(12)

            # 左侧主控制区容器
            left_scroll_area = QScrollArea()
            left_scroll_area.setWidgetResizable(True)

            main_control_frame = QFrame()
            main_control_frame.setStyleSheet("background-color: white; border-radius: 6px; border: 1px solid #e0e0e0;")
            main_control_layout = QVBoxLayout(main_control_frame)
            main_control_layout.setContentsMargins(15, 15, 15, 15)
            main_control_layout.setSpacing(15)

            # 1. 文件夹选择区域
            folder_section = QWidget()
            folder_layout = QVBoxLayout(folder_section)
            folder_layout.setSpacing(8)

            folder_header = QLabel("选择要扫描的文件夹")
            folder_header.setStyleSheet("font-weight: bold; color: #343a40; font-size: 14px;")
            folder_layout.addWidget(folder_header)

            folder_select = QWidget()
            folder_select_layout = QHBoxLayout(folder_select)

            self.folder_label = QLabel("未选择文件夹")
            self.folder_label.setMinimumHeight(34)
            self.folder_label.setStyleSheet(
                "color: #6c757d; border: 1px solid #e9ecef; padding: 6px 10px; border-radius: 4px; background-color: #f8f9fa;")
            self.folder_label.setWordWrap(True)
            folder_select_layout.addWidget(self.folder_label, 1)

            browse_btn = QPushButton("选择文件夹")
            browse_btn.setStyleSheet("background-color: #28a745;")
            browse_btn.setIcon(self.style().standardIcon(self.style().SP_DialogOpenButton))
            browse_btn.setMinimumHeight(34)
            browse_btn.clicked.connect(self.browse_folder)
            folder_select_layout.addWidget(browse_btn)

            folder_layout.addWidget(folder_select)

            # 添加文件夹选择说明
            folder_help = QLabel("提示：选择一个包含可能有重复文件的文件夹，程序会扫描所有子文件夹")
            folder_help.setStyleSheet("color: #6c757d; font-size: 12px;")
            folder_layout.addWidget(folder_help)

            main_control_layout.addWidget(folder_section)

            # 添加分隔线
            line1 = QFrame()
            line1.setFrameShape(QFrame.HLine)
            line1.setFrameShadow(QFrame.Sunken)
            line1.setStyleSheet("color: #e9ecef;")
            main_control_layout.addWidget(line1)

            # 2. 扫描控制区域
            scan_control_section = QWidget()
            scan_control_layout = QVBoxLayout(scan_control_section)
            scan_control_layout.setSpacing(8)

            scan_header = QLabel("扫描控制")
            scan_header.setStyleSheet("font-weight: bold; color: #343a40; font-size: 14px;")
            scan_control_layout.addWidget(scan_header)

            # 扫描方式选择（单选）
            scan_method_group = QGroupBox("扫描方式（单选）")
            scan_method_group.setFlat(True)
            scan_method_group.setStyleSheet("font-weight: bold; color: #495057;")
            scan_method_layout = QVBoxLayout(scan_method_group)

            method_options = QWidget()
            method_options_layout = QVBoxLayout(method_options)
            method_options_layout.setSpacing(5)

            self.scan_by_name = QCheckBox("仅按文件名扫描 (最快)")
            self.scan_by_name.clicked.connect(self.on_scan_method_changed)
            method_options_layout.addWidget(self.scan_by_name)

            self.scan_by_size = QCheckBox("仅按文件大小扫描 (快速)")
            self.scan_by_size.clicked.connect(self.on_scan_method_changed)
            self.scan_by_size.setChecked(True)  # 默认选中按文件大小扫描
            method_options_layout.addWidget(self.scan_by_size)

            self.scan_by_hash = QCheckBox("仅按文件内容(哈希)扫描 (精确)")
            self.scan_by_hash.clicked.connect(self.on_scan_method_changed)
            method_options_layout.addWidget(self.scan_by_hash)

            self.scan_by_combo = QCheckBox("组合条件扫描（可多选）")
            self.scan_by_combo.clicked.connect(self.on_scan_method_changed)
            method_options_layout.addWidget(self.scan_by_combo)

            scan_method_layout.addWidget(method_options)
            scan_control_layout.addWidget(scan_method_group)

            # 组合条件选项
            self.combo_options_group = QGroupBox("组合条件")
            self.combo_options_group.setFlat(True)
            self.combo_options_group.setStyleSheet("font-weight: bold; color: #495057;")
            combo_options_layout = QVBoxLayout(self.combo_options_group)

            # 禁用组合条件选项，因为默认选中的是按大小扫描
            self.combo_options_group.setEnabled(False)

            # 文件名和大小选项
            basic_options = QWidget()
            basic_options_layout = QHBoxLayout(basic_options)
            basic_options_layout.setSpacing(15)

            self.compare_name = QCheckBox("文件名")
            self.compare_name.setChecked(True)
            self.compare_name.stateChanged.connect(self.on_compare_option_changed)
            basic_options_layout.addWidget(self.compare_name)

            self.compare_size = QCheckBox("文件大小 (精确到字节)")
            self.compare_size.setChecked(True)
            self.compare_size.stateChanged.connect(self.on_compare_option_changed)
            basic_options_layout.addWidget(self.compare_size)

            basic_options_layout.addStretch()
            combo_options_layout.addWidget(basic_options)

            # 哈希选项
            hash_options = QWidget()
            hash_options_layout = QHBoxLayout(hash_options)
            hash_options_layout.setSpacing(15)

            self.compare_hash = QCheckBox("内容(哈希)")
            self.compare_hash.setChecked(False)
            self.compare_hash.stateChanged.connect(self.on_compare_option_changed)
            hash_options_layout.addWidget(self.compare_hash)

            # 哈希算法选择
            hash_options_layout.addWidget(QLabel("算法:"))
            self.hash_algorithm = QComboBox()
            self.hash_algorithm.addItem("MD5", "md5")
            self.hash_algorithm.addItem("SHA-1", "sha1")
            self.hash_algorithm.addItem("SHA-256", "sha256")

            # 如果安装了xxhash，添加这个更快的选项
            if HAS_XXHASH:
                self.hash_algorithm.addItem("XXHash (最快)", "xxhash")
                self.hash_algorithm.setCurrentIndex(3)  # 默认使用最快的
            else:
                self.hash_algorithm.setCurrentIndex(0)  # 默认使用MD5

            hash_options_layout.addWidget(self.hash_algorithm)
            hash_options_layout.addStretch()
            combo_options_layout.addWidget(hash_options)

            scan_control_layout.addWidget(self.combo_options_group)

            # 扫描优化选项
            optimization_group = QGroupBox("扫描优化")
            optimization_group.setFlat(True)
            optimization_group.setStyleSheet("font-weight: bold; color: #495057;")
            optimization_layout = QVBoxLayout(optimization_group)

            self.fast_scan_mode = QCheckBox("快速扫描模式 (优先速度，适合大量文件)")
            self.fast_scan_mode.setChecked(True)  # 默认启用快速模式
            optimization_layout.addWidget(self.fast_scan_mode)

            self.skip_large_files = QCheckBox("跳过超大文件 (>1GB)")
            self.skip_large_files.setChecked(False)
            optimization_layout.addWidget(self.skip_large_files)

            scan_control_layout.addWidget(optimization_group)

            # 扫描按钮组
            scan_buttons = QWidget()
            scan_buttons_layout = QHBoxLayout(scan_buttons)
            scan_buttons_layout.setSpacing(10)

            self.scan_btn = QPushButton("开始扫描")
            self.scan_btn.setIcon(self.style().standardIcon(self.style().SP_MediaPlay))
            self.scan_btn.setMinimumHeight(34)
            self.scan_btn.clicked.connect(self.start_scan)
            scan_buttons_layout.addWidget(self.scan_btn)

            self.stop_btn = QPushButton("停止扫描")
            self.stop_btn.setIcon(self.style().standardIcon(self.style().SP_MediaStop))
            self.stop_btn.setStyleSheet("background-color: #dc3545;")
            self.stop_btn.setMinimumHeight(34)
            self.stop_btn.clicked.connect(self.stop_scanning)
            self.stop_btn.setEnabled(False)
            scan_buttons_layout.addWidget(self.stop_btn)

            self.clear_btn = QPushButton("清除结果")
            self.clear_btn.setIcon(self.style().standardIcon(self.style().SP_DialogResetButton))
            self.clear_btn.setStyleSheet("background-color: #6c757d;")
            self.clear_btn.setMinimumHeight(34)
            self.clear_btn.clicked.connect(self.clear_results)
            self.clear_btn.setEnabled(False)
            scan_buttons_layout.addWidget(self.clear_btn)

            scan_control_layout.addWidget(scan_buttons)

            # 进度条和状态
            progress_status = QWidget()
            progress_status_layout = QVBoxLayout(progress_status)
            progress_status_layout.setSpacing(8)

            progress_label = QLabel("扫描进度")
            progress_label.setStyleSheet("color: #6c757d; font-size: 12px;")
            progress_status_layout.addWidget(progress_label)

            self.progress_bar = QProgressBar()
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(0)
            self.progress_bar.setTextVisible(True)
            progress_status_layout.addWidget(self.progress_bar)

            self.status_label = QLabel("就绪")
            self.status_label.setStyleSheet("color: #28a745; font-weight: bold;")
            progress_status_layout.addWidget(self.status_label)

            scan_control_layout.addWidget(progress_status)

            main_control_layout.addWidget(scan_control_section)

            # 添加分隔线
            line2 = QFrame()
            line2.setFrameShape(QFrame.HLine)
            line2.setFrameShadow(QFrame.Sunken)
            line2.setStyleSheet("color: #e9ecef;")
            main_control_layout.addWidget(line2)

            # 3. 文件操作区域
            file_ops_section = QWidget()
            file_ops_layout = QVBoxLayout(file_ops_section)
            file_ops_layout.setSpacing(8)

            ops_header = QLabel("文件操作")
            ops_header.setStyleSheet("font-weight: bold; color: #343a40; font-size: 14px;")
            file_ops_layout.addWidget(ops_header)

            # 删除按钮和说明
            delete_box = QWidget()
            delete_box_layout = QVBoxLayout(delete_box)
            delete_box_layout.setSpacing(5)

            # 添加一键选择按钮
            select_buttons = QWidget()
            select_buttons_layout = QHBoxLayout(select_buttons)
            select_buttons_layout.setSpacing(10)

            self.select_duplicates_btn = QPushButton("一键选择重复文件")
            self.select_duplicates_btn.setIcon(self.style().standardIcon(self.style().SP_DialogYesButton))
            self.select_duplicates_btn.setStyleSheet("background-color: #ffc107; color: #212529;")
            self.select_duplicates_btn.setMinimumHeight(34)
            self.select_duplicates_btn.clicked.connect(self.select_duplicates_automatically)
            self.select_duplicates_btn.setEnabled(False)
            select_buttons_layout.addWidget(self.select_duplicates_btn)

            self.deselect_all_btn = QPushButton("取消全选")
            self.deselect_all_btn.setIcon(self.style().standardIcon(self.style().SP_DialogNoButton))
            self.deselect_all_btn.setStyleSheet("background-color: #6c757d;")
            self.deselect_all_btn.setMinimumHeight(34)
            self.deselect_all_btn.clicked.connect(self.deselect_all_files)
            self.deselect_all_btn.setEnabled(False)
            select_buttons_layout.addWidget(self.deselect_all_btn)

            delete_box_layout.addWidget(select_buttons)

            self.delete_selected_btn = QPushButton("删除选中文件")
            self.delete_selected_btn.setIcon(self.style().standardIcon(self.style().SP_TrashIcon))
            self.delete_selected_btn.setStyleSheet("background-color: #dc3545;")
            self.delete_selected_btn.setMinimumHeight(34)
            self.delete_selected_btn.clicked.connect(self.delete_selected_files)
            self.delete_selected_btn.setEnabled(False)
            delete_box_layout.addWidget(self.delete_selected_btn)

            delete_help = QLabel("提示：勾选要删除的文件，点击此按钮可批量删除，操作不可恢复")
            delete_help.setStyleSheet("color: #6c757d; font-size: 12px;")
            delete_box_layout.addWidget(delete_help)

            file_ops_layout.addWidget(delete_box)

            # 信息提示面板
            info_panel = QFrame()
            info_panel.setObjectName("info-panel")
            info_layout = QHBoxLayout(info_panel)

            info_icon = QLabel()
            info_icon.setPixmap(self.style().standardIcon(self.style().SP_MessageBoxInformation).pixmap(16, 16))
            info_layout.addWidget(info_icon)

            info_text = QLabel("已添加一键选择功能，可自动勾选每组重复文件中除一个之外的所有文件。")
            info_text.setObjectName("info-label")
            info_text.setWordWrap(True)
            info_layout.addWidget(info_text, 1)

            file_ops_layout.addWidget(info_panel)

            main_control_layout.addWidget(file_ops_section)

            # 添加足够的底部空间
            main_control_layout.addStretch(1)

            left_scroll_area.setWidget(main_control_frame)
            left_layout.addWidget(left_scroll_area)
            left_panel.setMinimumWidth(400)  # 左侧控制区宽度
            main_splitter.addWidget(left_panel)

            # 右侧：创建垂直分割器
            right_splitter = QSplitter(Qt.Vertical)
            right_splitter.setHandleWidth(6)

            # 右上：过滤选项区
            filter_scroll_area = QScrollArea()
            filter_scroll_area.setWidgetResizable(True)
            filter_scroll_area.setMinimumHeight(300)

            filter_frame = QFrame()
            filter_frame.setStyleSheet("background-color: white; border-radius: 6px; border: 1px solid #e0e0e0;")
            filter_layout = QVBoxLayout(filter_frame)
            filter_layout.setContentsMargins(15, 15, 15, 15)
            filter_layout.setSpacing(12)

            filter_header = QLabel("过滤选项")
            filter_header.setStyleSheet("font-weight: bold; color: #343a40; font-size: 14px;")
            filter_layout.addWidget(filter_header)

            # 文件大小过滤
            size_filter_group = QGroupBox("文件大小过滤")
            size_filter_group.setFlat(True)
            size_filter_group.setStyleSheet("font-weight: bold; color: #495057;")
            size_filter_layout = QVBoxLayout(size_filter_group)

            size_range = QWidget()
            size_range_layout = QHBoxLayout(size_range)
            size_range_layout.setSpacing(10)

            self.size_filter_min = QLineEdit()
            self.size_filter_min.setPlaceholderText("最小大小 (如: 100KB)")
            self.size_filter_min.setMinimumHeight(30)
            size_range_layout.addWidget(self.size_filter_min)

            size_range_layout.addWidget(QLabel("至"))

            self.size_filter_max = QLineEdit()
            self.size_filter_max.setPlaceholderText("最大大小 (如: 10MB)")
            self.size_filter_max.setMinimumHeight(30)
            size_range_layout.addWidget(self.size_filter_max)

            size_range_layout.addStretch()
            size_filter_layout.addWidget(size_range)
            filter_layout.addWidget(size_filter_group)

            # 文件名过滤
            name_filter_group = QGroupBox("文件名过滤")
            name_filter_group.setFlat(True)
            name_filter_group.setStyleSheet("font-weight: bold; color: #495057;")
            name_filter_layout = QVBoxLayout(name_filter_group)

            self.name_contains = QLineEdit()
            self.name_contains.setPlaceholderText("包含文字")
            self.name_contains.setMinimumHeight(30)
            self.name_contains.textChanged.connect(self.apply_filters)
            name_filter_layout.addWidget(self.name_contains)
            filter_layout.addWidget(name_filter_group)

            # 文件格式过滤
            ext_filter_group = QGroupBox("文件格式过滤")
            ext_filter_group.setFlat(True)
            ext_filter_group.setStyleSheet("font-weight: bold; color: #495057;")
            ext_filter_layout = QVBoxLayout(ext_filter_group)

            self.file_extension = QLineEdit()
            self.file_extension.setPlaceholderText("如: txt, pdf (多个用逗号分隔)")
            self.file_extension.setMinimumHeight(30)
            self.file_extension.textChanged.connect(self.apply_filters)
            ext_filter_layout.addWidget(self.file_extension)
            filter_layout.addWidget(ext_filter_group)

            # 应用过滤按钮
            apply_filter_btn = QPushButton("应用过滤")
            apply_filter_btn.setIcon(self.style().standardIcon(self.style().SP_DialogApplyButton))
            apply_filter_btn.setStyleSheet("background-color: #28a745;")
            apply_filter_btn.setMinimumHeight(34)
            apply_filter_btn.clicked.connect(self.apply_filters)
            filter_layout.addWidget(apply_filter_btn)

            # 添加足够的底部空间
            filter_layout.addStretch(1)

            filter_scroll_area.setWidget(filter_frame)
            right_splitter.addWidget(filter_scroll_area)

            # 右下：统计信息区
            stats_scroll_area = QScrollArea()
            stats_scroll_area.setWidgetResizable(True)
            stats_scroll_area.setMinimumHeight(200)

            stats_frame = QFrame()
            stats_frame.setStyleSheet("background-color: white; border-radius: 6px; border: 1px solid #e0e0e0;")
            stats_layout = QVBoxLayout(stats_frame)
            stats_layout.setContentsMargins(15, 15, 15, 15)
            stats_layout.setSpacing(12)

            stats_header = QLabel("扫描统计")
            stats_header.setStyleSheet("font-weight: bold; color: #343a40; font-size: 14px;")
            stats_layout.addWidget(stats_header)

            # 统计信息将在扫描完成后更新
            self.stats_total_files = QLabel("总文件数: --")
            self.stats_duplicate_groups = QLabel("重复文件组: --")
            self.stats_duplicate_files = QLabel("重复文件总数: --")
            self.stats_saved_space = QLabel("可释放空间: --")
            self.stats_scan_time = QLabel("扫描时间: --")
            self.stats_hash_algorithm = QLabel("使用的哈希算法: --")

            for label in [self.stats_total_files, self.stats_duplicate_groups,
                          self.stats_duplicate_files, self.stats_saved_space,
                          self.stats_scan_time, self.stats_hash_algorithm]:
                label.setStyleSheet("padding: 5px; border-bottom: 1px solid #f1f1f1;")
                stats_layout.addWidget(label)

            # 操作说明
            help_text = QLabel("""
            <p style="font-size: 12px; color: #6c757d;">
            <strong>哈希算法说明:</strong><br>
            - XXHash: 最快，适合快速校验<br>
            - MD5: 平衡速度和兼容性<br>
            - SHA-1: 中等速度，较高安全性<br>
            - SHA-256: 最安全，速度较慢
            </p>
            """)
            help_text.setTextInteractionFlags(Qt.TextSelectableByMouse)
            stats_layout.addWidget(help_text)

            # 添加足够的底部空间
            stats_layout.addStretch(1)

            stats_scroll_area.setWidget(stats_frame)
            right_splitter.addWidget(stats_scroll_area)

            # 设置右侧上下区域的初始大小比例
            right_splitter.setSizes([300, 200])

            # 将右侧分割器添加到主分割器
            main_splitter.addWidget(right_splitter)

            # 设置左右区域的初始大小比例
            main_splitter.setSizes([400, 1000])  # 左侧宽度比例

            # 添加主分割器到主布局
            main_layout.addWidget(main_splitter)

            # 创建垂直分割器
            bottom_splitter = QSplitter(Qt.Vertical)
            bottom_splitter.setHandleWidth(6)

            # 结果展示区域标题
            results_header = QWidget()
            results_header_layout = QHBoxLayout(results_header)
            results_header_layout.setContentsMargins(0, 0, 0, 0)

            results_label = QLabel("扫描结果")
            results_label.setStyleSheet("font-size: 15px; font-weight: bold; color: #495057;")
            results_header_layout.addWidget(results_label)

            self.results_count_label = QLabel("")
            self.results_count_label.setStyleSheet("color: #6c757d;")
            results_header_layout.addWidget(self.results_count_label)

            results_header_layout.addStretch()
            bottom_splitter.addWidget(results_header)

            # 结果展示区
            results_frame = QFrame()
            results_frame.setStyleSheet("background-color: white; border-radius: 6px; border: 1px solid #e0e0e0;")
            results_layout = QVBoxLayout(results_frame)
            results_layout.setContentsMargins(10, 10, 10, 10)

            self.result_tree = GroupSeparatorTreeWidget()
            self.result_tree.setHeaderLabels(["选择", "组号", "文件名", "大小", "路径", "哈希值", "操作"])
            self.result_tree.setSelectionMode(QAbstractItemView.SingleSelection)
            self.result_tree.setEditTriggers(QAbstractItemView.NoEditTriggers)

            # 添加双击事件支持
            self.result_tree.itemDoubleClicked.connect(self.on_item_double_clicked)

            # 设置列宽策略
            header = self.result_tree.header()
            for i in range(7):
                header.setSectionResizeMode(i, QHeaderView.Interactive)

            # 初始列宽度
            header.resizeSection(0, 60)  # 选择框
            header.resizeSection(1, 60)  # 组号
            header.resizeSection(2, 180)  # 文件名
            header.resizeSection(3, 100)  # 大小
            header.resizeSection(4, 380)  # 路径
            header.resizeSection(5, 300)  # 哈希值
            header.resizeSection(6, 160)  # 操作

            # 连接信号和槽
            self.result_tree.setContextMenuPolicy(Qt.CustomContextMenu)
            self.result_tree.customContextMenuRequested.connect(self.show_context_menu)

            results_layout.addWidget(self.result_tree)
            results_frame.setMinimumHeight(400)
            bottom_splitter.addWidget(results_frame)

            # 设置结果标题区和结果区的初始大小比例
            bottom_splitter.setSizes([40, 600])

            # 添加底部分割器到主布局
            main_layout.addWidget(bottom_splitter, 3)

            # 状态栏
            self.setStatusBar(QStatusBar())
            self.statusBar().showMessage("就绪")
            self.statusBar().setStyleSheet("font-size: 12px; color: #6c757d;")

            # 窗口大小变化时调整布局
            self.resizeEvent = self.on_resize

        except Exception as e:
            error_msg = f"创建用户界面时出错: {str(e)}"
            print(error_msg)
            log_msg = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {error_msg}\n"
            log_msg += "".join(traceback.format_exc()) + "\n"
            save_error_log(log_msg)
            QMessageBox.critical(self, "界面错误", error_msg)

    def on_item_double_clicked(self, item, column):
        """双击文件列时打开文件"""
        try:
            # 只有双击文件名或路径列时才打开文件
            if column in [2, 4]:  # 2是文件名列，4是路径列
                file_path = item.data(0, Qt.UserRole)
                if file_path:
                    self.open_file(file_path)
        except Exception as e:
            error_msg = f"双击打开文件时出错: {str(e)}"
            print(error_msg)
            log_msg = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {error_msg}\n"
            log_msg += "".join(traceback.format_exc()) + "\n"
            save_error_log(log_msg)
            QMessageBox.warning(self, "打开文件错误", error_msg)

    def on_resize(self, event):
        """窗口大小变化时的处理"""
        try:
            # 调用父类的resizeEvent
            super().resizeEvent(event)

            # 调整列宽以适应窗口宽度
            if hasattr(self, 'result_tree') and self.result_tree:
                header = self.result_tree.header()
                total_width = self.result_tree.width()

                # 根据总宽度按比例分配列宽
                if total_width > 1000:
                    header.resizeSection(0, int(total_width * 0.04))  # 选择框 4%
                    header.resizeSection(1, int(total_width * 0.04))  # 组号 4%
                    header.resizeSection(2, int(total_width * 0.15))  # 文件名 15%
                    header.resizeSection(3, int(total_width * 0.08))  # 大小 8%
                    header.resizeSection(4, int(total_width * 0.30))  # 路径 30%
                    header.resizeSection(5, int(total_width * 0.25))  # 哈希值 25%
                    header.resizeSection(6, int(total_width * 0.14))  # 操作 14%
        except Exception as e:
            error_msg = f"窗口大小调整时出错: {str(e)}"
            print(error_msg)
            log_msg = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {error_msg}\n"
            log_msg += "".join(traceback.format_exc()) + "\n"
            save_error_log(log_msg)

    def browse_folder(self):
        """选择文件夹"""
        try:
            folder = QFileDialog.getExistingDirectory(self, "选择文件夹", os.path.expanduser("~"))
            if folder:
                if not os.access(folder, os.R_OK):
                    raise Exception("没有访问该文件夹的权限")

                self.folder_path = folder
                self.folder_label.setText(folder)
                self.status_label.setText(f"已选择文件夹: {os.path.basename(folder)}")
                self.statusBar().showMessage(f"已选择文件夹: {folder}")
        except Exception as e:
            error_msg = f"选择文件夹时出错: {str(e)}"
            print(error_msg)
            log_msg = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {error_msg}\n"
            log_msg += "".join(traceback.format_exc()) + "\n"
            save_error_log(log_msg)
            QMessageBox.warning(self, "选择文件夹错误", error_msg)

    def on_scan_method_changed(self):
        """扫描方式改变时的处理"""
        try:
            sender = self.sender()
            if sender == self.scan_by_name:
                self.scan_by_size.setChecked(False)
                self.scan_by_hash.setChecked(False)
                self.scan_by_combo.setChecked(False)
                self.combo_options_group.setEnabled(False)
            elif sender == self.scan_by_size:
                self.scan_by_name.setChecked(False)
                self.scan_by_hash.setChecked(False)
                self.scan_by_combo.setChecked(False)
                self.combo_options_group.setEnabled(False)
            elif sender == self.scan_by_hash:
                self.scan_by_name.setChecked(False)
                self.scan_by_size.setChecked(False)
                self.scan_by_combo.setChecked(False)
                self.combo_options_group.setEnabled(False)
            elif sender == self.scan_by_combo:
                self.scan_by_name.setChecked(False)
                self.scan_by_size.setChecked(False)
                self.scan_by_hash.setChecked(False)
                self.combo_options_group.setEnabled(True)

                # 确保至少有一个组合选项被选中
                if not (
                        self.compare_name.isChecked() or self.compare_size.isChecked() or self.compare_hash.isChecked()):
                    self.compare_name.setChecked(True)
                    self.compare_size.setChecked(True)
        except Exception as e:
            error_msg = f"更改扫描方式时出错: {str(e)}"
            print(error_msg)
            log_msg = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {error_msg}\n"
            log_msg += "".join(traceback.format_exc()) + "\n"
            save_error_log(log_msg)
            QMessageBox.warning(self, "扫描方式错误", error_msg)

    def on_compare_option_changed(self):
        """组合比较选项改变时的处理"""
        try:
            if not (self.compare_name.isChecked() or self.compare_size.isChecked() or self.compare_hash.isChecked()):
                QMessageBox.warning(self, "警告", "至少需要选择一个比较选项")
                sender = self.sender()
                if sender == self.compare_name:
                    self.compare_name.setChecked(True)
                elif sender == self.compare_size:
                    self.compare_size.setChecked(True)
                else:
                    self.compare_hash.setChecked(True)
        except Exception as e:
            error_msg = f"更改比较选项时出错: {str(e)}"
            print(error_msg)
            log_msg = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {error_msg}\n"
            log_msg += "".join(traceback.format_exc()) + "\n"
            save_error_log(log_msg)
            QMessageBox.warning(self, "选项错误", error_msg)

    def start_scan(self):
        """开始扫描文件"""
        try:
            if not self.folder_path:
                QMessageBox.warning(self, "警告", "请先选择文件夹")
                return

            # 确保之前的线程已正确销毁
            self.cleanup_thread()

            if self.scan_thread is not None:
                QMessageBox.information(self, "提示", "正在扫描中，请等待完成或停止当前扫描")
                return

            # 记录扫描开始时间
            self.scan_start_time = datetime.now()

            # 获取当前选择的扫描方式
            scan_by_name = self.scan_by_name.isChecked()
            scan_by_size = self.scan_by_size.isChecked()
            scan_by_hash = self.scan_by_hash.isChecked()
            scan_by_combo = self.scan_by_combo.isChecked()
            fast_scan_mode = self.fast_scan_mode.isChecked()
            hash_algorithm = self.hash_algorithm.currentData()

            # 确保有选择扫描方式
            if not (scan_by_name or scan_by_size or scan_by_hash or scan_by_combo):
                QMessageBox.warning(self, "警告", "请选择一种扫描方式")
                return

            # 重置状态
            self.result_tree.clear()
            self.duplicates = []
            self.filtered_duplicates = []
            self.group_colors = []
            self.progress_bar.setValue(0)
            self.status_label.setText("开始扫描文件...")
            self.status_label.setStyleSheet("color: #fd7e14; font-weight: bold;")
            self.statusBar().showMessage("开始扫描文件...")
            self.scan_btn.setEnabled(False)
            self.stop_btn.setEnabled(True)
            self.delete_selected_btn.setEnabled(False)
            self.select_duplicates_btn.setEnabled(False)
            self.deselect_all_btn.setEnabled(False)
            self.clear_btn.setEnabled(True)
            self.results_count_label.setText("")

            # 重置统计信息
            self.stats_total_files.setText("总文件数: 扫描中...")
            self.stats_duplicate_groups.setText("重复文件组: --")
            self.stats_duplicate_files.setText("重复文件总数: --")
            self.stats_saved_space.setText("可释放空间: --")
            self.stats_scan_time.setText("扫描时间: 计算中...")
            self.stats_hash_algorithm.setText(f"使用的哈希算法: {self.hash_algorithm.currentText()}")

            # 创建并启动扫描线程
            self.scan_thread = QThread()
            self.scan_worker = ScanWorker(
                self.folder_path,
                self.compare_name.isChecked(),
                self.compare_size.isChecked(),
                self.compare_hash.isChecked(),
                hash_algorithm,
                scan_by_name,
                scan_by_size,
                scan_by_hash,
                fast_scan_mode
            )

            # 将 worker 移动到线程
            self.scan_worker.moveToThread(self.scan_thread)

            # 连接信号和槽，使用Qt.QueuedConnection确保线程安全
            self.scan_thread.started.connect(self.scan_worker.run)
            self.scan_worker.progress_updated.connect(self.update_progress, Qt.QueuedConnection)
            self.scan_worker.status_updated.connect(self.update_status, Qt.QueuedConnection)
            self.scan_worker.scan_finished.connect(self.on_scan_finished, Qt.QueuedConnection)
            self.scan_worker.scan_stopped.connect(self.on_scan_stopped, Qt.QueuedConnection)
            self.scan_worker.error_occurred.connect(self.show_error_message, Qt.QueuedConnection)

            # 线程完成后清理
            self.scan_worker.finished.connect(self.scan_thread.quit, Qt.QueuedConnection)
            self.scan_worker.finished.connect(self.scan_worker.deleteLater, Qt.QueuedConnection)
            self.scan_thread.finished.connect(self.scan_thread.deleteLater, Qt.QueuedConnection)

            # 跟踪线程和worker的销毁
            self.scan_thread.destroyed.connect(self.on_thread_destroyed)
            self.scan_worker.destroyed.connect(self.on_worker_destroyed)

            # 启动线程
            self.scan_thread.start()

        except Exception as e:
            error_msg = f"启动扫描时出错: {str(e)}"
            print(error_msg)
            log_msg = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {error_msg}\n"
            log_msg += "".join(traceback.format_exc()) + "\n"
            save_error_log(log_msg)
            QMessageBox.warning(self, "扫描错误", error_msg)
            self.scan_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            self.status_label.setStyleSheet("color: #28a745; font-weight: bold;")

    def on_thread_destroyed(self):
        """线程销毁时的回调，确保引用被正确清除"""
        self.scan_thread = None

    def on_worker_destroyed(self):
        """Worker销毁时的回调，确保引用被正确清除"""
        self.scan_worker = None

    def cleanup_thread(self):
        """安全清理线程资源"""
        try:
            # 先检查线程是否存在且正在运行
            if self.scan_thread and not self.scan_thread.isFinished():
                # 请求worker停止
                if self.scan_worker:
                    self.scan_worker.stop()

                # 等待线程结束，最多等待2秒
                if not self.scan_thread.wait(2000):
                    # 强制终止线程
                    self.scan_thread.terminate()
                    self.scan_thread.wait()

            # 显式清除引用
            self.scan_worker = None
            self.scan_thread = None

        except Exception as e:
            error_msg = f"清理线程资源时出错: {str(e)}"
            print(error_msg)
            log_msg = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {error_msg}\n"
            log_msg += "".join(traceback.format_exc()) + "\n"
            save_error_log(log_msg)

    def update_progress(self, value):
        """更新进度条"""
        if QThread.currentThread() == self.thread():
            self.progress_bar.setValue(value)
        else:
            QMetaObject.invokeMethod(
                self.progress_bar,
                "setValue",
                Qt.QueuedConnection,
                Q_ARG(int, value)
            )

    def update_status(self, message):
        """更新状态文本"""
        if QThread.currentThread() == self.thread():
            self.status_label.setText(message)
            self.statusBar().showMessage(message)
        else:
            QMetaObject.invokeMethod(
                self,
                "_update_status_impl",
                Qt.QueuedConnection,
                Q_ARG(str, message)
            )

    def _update_status_impl(self, message):
        """状态更新的实际实现"""
        self.status_label.setText(message)
        self.statusBar().showMessage(message)

    def stop_scanning(self):
        """停止扫描"""
        try:
            if self.scan_worker and self.scan_thread and self.scan_thread.isRunning():
                self.status_label.setText("正在停止扫描...")
                self.statusBar().showMessage("正在停止扫描...")
                self.status_label.setStyleSheet("color: #fd7e14; font-weight: bold;")

                # 请求停止扫描
                self.scan_worker.stop()
        except Exception as e:
            error_msg = f"停止扫描时出错: {str(e)}"
            print(error_msg)
            log_msg = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {error_msg}\n"
            log_msg += "".join(traceback.format_exc()) + "\n"
            save_error_log(log_msg)
            QMessageBox.warning(self, "停止错误", error_msg)
            self.status_label.setStyleSheet("color: #28a745; font-weight: bold;")

    def on_scan_finished(self, duplicates):
        """扫描完成处理"""
        if QThread.currentThread() != self.thread():
            QMetaObject.invokeMethod(
                self,
                "_on_scan_finished_impl",
                Qt.QueuedConnection,
                Q_ARG(list, duplicates)
            )
            return

        self._on_scan_finished_impl(duplicates)

    def _on_scan_finished_impl(self, duplicates):
        """扫描完成处理的实际实现"""
        try:
            # 计算扫描时间
            scan_time = datetime.now() - self.scan_start_time
            scan_time_str = f"{scan_time.seconds // 60}分{scan_time.seconds % 60}秒"

            self.duplicates = duplicates
            self.filtered_duplicates = duplicates.copy()
            self.generate_group_colors(len(duplicates))
            self.display_results()

            # 更新统计信息
            total_files = self.scan_worker.total_files if hasattr(self.scan_worker, 'total_files') else 0
            total_duplicates = sum(len(group) for group in duplicates)
            duplicate_groups = len(duplicates)

            # 计算可释放空间
            saved_space = 0
            for group in duplicates:
                if group:  # 确保组不为空
                    base_size = group[0]['size']
                    for file_info in group[1:]:
                        saved_space += file_info['size']

            self.stats_total_files.setText(f"总文件数: {total_files}")
            self.stats_duplicate_groups.setText(f"重复文件组: {duplicate_groups}")
            self.stats_duplicate_files.setText(f"重复文件总数: {total_duplicates}")
            self.stats_saved_space.setText(f"可释放空间: {format_file_size(saved_space)}")
            self.stats_scan_time.setText(f"扫描时间: {scan_time_str}")
            self.stats_hash_algorithm.setText(f"使用的哈希算法: {self.hash_algorithm.currentText()}")

            # 更新结果计数标签
            self.results_count_label.setText(
                f"共发现 {duplicate_groups} 组重复文件，总计 {total_duplicates} 个重复文件 (耗时 {scan_time_str})")

            self.status_label.setText(f"扫描完成。找到 {duplicate_groups} 组重复文件 (耗时 {scan_time_str})")
            self.statusBar().showMessage(
                f"扫描完成。找到 {duplicate_groups} 组重复文件，总计 {total_duplicates} 个重复文件 (耗时 {scan_time_str})")
            self.status_label.setStyleSheet("color: #28a745; font-weight: bold;")
            self.progress_bar.setValue(100)
            self.scan_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)

            # 启用一键选择按钮
            self.select_duplicates_btn.setEnabled(duplicate_groups > 0)
            self.deselect_all_btn.setEnabled(duplicate_groups > 0)

        except Exception as e:
            error_msg = f"扫描完成处理时出错: {str(e)}"
            print(error_msg)
            log_msg = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {error_msg}\n"
            log_msg += "".join(traceback.format_exc()) + "\n"
            save_error_log(log_msg)
            QMessageBox.warning(self, "处理错误", error_msg)
            self.scan_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            self.status_label.setStyleSheet("color: #28a745; font-weight: bold;")

    def clear_results(self):
        """清除当前结果"""
        try:
            reply = QMessageBox.question(
                self, "确认清除",
                "确定要清除当前结果吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No
            )

            if reply == QMessageBox.Yes:
                self.result_tree.clear()
                self.duplicates = []
                self.filtered_duplicates = []
                self.group_colors = []
                self.progress_bar.setValue(0)
                self.status_label.setText("结果已清除")
                self.statusBar().showMessage("结果已清除")
                self.status_label.setStyleSheet("color: #28a745; font-weight: bold;")
                self.delete_selected_btn.setEnabled(False)
                self.select_duplicates_btn.setEnabled(False)
                self.deselect_all_btn.setEnabled(False)
                self.clear_btn.setEnabled(False)
                self.results_count_label.setText("")

                # 重置统计信息
                self.stats_total_files.setText("总文件数: --")
                self.stats_duplicate_groups.setText("重复文件组: --")
                self.stats_duplicate_files.setText("重复文件总数: --")
                self.stats_saved_space.setText("可释放空间: --")
                self.stats_scan_time.setText("扫描时间: --")
                self.stats_hash_algorithm.setText("使用的哈希算法: --")
        except Exception as e:
            error_msg = f"清除结果时出错: {str(e)}"
            print(error_msg)
            log_msg = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {error_msg}\n"
            log_msg += "".join(traceback.format_exc()) + "\n"
            save_error_log(log_msg)
            QMessageBox.warning(self, "清除错误", error_msg)

    def apply_filters(self):
        """应用过滤条件"""
        try:
            if not self.duplicates:
                return

            # 获取过滤条件
            min_size_str = self.size_filter_min.text().strip()
            max_size_str = self.size_filter_max.text().strip()
            name_contains = self.name_contains.text().strip().lower()
            extensions = [ext.strip().lower() for ext in self.file_extension.text().strip().split(',') if ext.strip()]

            # 解析大小过滤条件
            min_size = parse_file_size(min_size_str) if min_size_str else None
            max_size = parse_file_size(max_size_str) if max_size_str else None

            # 验证大小格式
            if (min_size_str and min_size is None) or (max_size_str and max_size is None):
                QMessageBox.warning(self, "格式错误", "文件大小格式不正确，请使用如 '100KB' 或 '2MB' 的格式")
                return

            # 应用过滤
            self.filtered_duplicates = []
            for group in self.duplicates:
                filtered_group = []
                for file_info in group:
                    # 大小过滤
                    if min_size is not None and file_info["size"] < min_size:
                        continue
                    if max_size is not None and file_info["size"] > max_size:
                        continue

                    # 文件名包含文字过滤
                    if name_contains and name_contains not in file_info["name"].lower():
                        continue

                    # 文件格式过滤
                    if extensions:
                        file_ext = os.path.splitext(file_info["name"])[1].lower().lstrip('.')
                        if file_ext not in extensions:
                            continue

                    filtered_group.append(file_info)

                # 如果过滤后组内仍有重复文件，保留该组
                if len(filtered_group) >= 2:
                    self.filtered_duplicates.append(filtered_group)

            # 重新生成组颜色并显示结果
            self.generate_group_colors(len(self.filtered_duplicates))
            self.display_results()

            # 更新结果计数标签
            total_duplicates = sum(len(group) for group in self.filtered_duplicates)
            self.results_count_label.setText(
                f"共显示 {len(self.filtered_duplicates)} 组重复文件，总计 {total_duplicates} 个重复文件")

            self.status_label.setText(f"过滤完成。显示 {len(self.filtered_duplicates)} 组重复文件")
            self.statusBar().showMessage(f"过滤完成。显示 {len(self.filtered_duplicates)} 组重复文件")

            # 更新一键选择按钮状态
            has_results = len(self.filtered_duplicates) > 0
            self.select_duplicates_btn.setEnabled(has_results)
            self.deselect_all_btn.setEnabled(has_results)

        except Exception as e:
            error_msg = f"应用过滤条件时出错: {str(e)}"
            print(error_msg)
            log_msg = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {error_msg}\n"
            log_msg += "".join(traceback.format_exc()) + "\n"
            save_error_log(log_msg)
            QMessageBox.warning(self, "过滤错误", error_msg)

    def generate_group_colors(self, num_groups):
        """生成用于区分不同组的背景颜色"""
        try:
            self.group_colors = []
            colors = [
                QColor(220, 231, 255, 80),  # 柔和蓝色
                QColor(255, 243, 224, 80),  # 柔和橙色
                QColor(220, 255, 220, 80),  # 柔和绿色
                QColor(255, 224, 230, 80),  # 柔和粉色
                QColor(230, 224, 255, 80),  # 柔和紫色
                QColor(224, 251, 255, 80),  # 柔和青色
                QColor(255, 236, 204, 80),  # 柔和黄色
                QColor(255, 228, 225, 80)  # 柔和红色
            ]

            for i in range(num_groups):
                self.group_colors.append(colors[i % len(colors)])
        except Exception as e:
            error_msg = f"生成组颜色时出错: {str(e)}"
            print(error_msg)
            log_msg = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {error_msg}\n"
            log_msg += "".join(traceback.format_exc()) + "\n"
            save_error_log(log_msg)

    def on_scan_stopped(self):
        """扫描停止处理"""
        if QThread.currentThread() != self.thread():
            QMetaObject.invokeMethod(
                self,
                "_on_scan_stopped_impl",
                Qt.QueuedConnection
            )
            return

        self._on_scan_stopped_impl()

    def _on_scan_stopped_impl(self):
        """扫描停止处理的实际实现"""
        try:
            # 计算已用扫描时间
            if hasattr(self, 'scan_start_time'):
                scan_time = datetime.now() - self.scan_start_time
                scan_time_str = f"{scan_time.seconds // 60}分{scan_time.seconds % 60}秒"
                self.stats_scan_time.setText(f"扫描时间: {scan_time_str} (已中断)")

            self.status_label.setText("扫描已停止")
            self.statusBar().showMessage("扫描已停止")
            self.status_label.setStyleSheet("color: #dc3545; font-weight: bold;")
            self.scan_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            self.select_duplicates_btn.setEnabled(False)
            self.deselect_all_btn.setEnabled(False)
        except Exception as e:
            error_msg = f"处理扫描停止时出错: {str(e)}"
            print(error_msg)
            log_msg = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {error_msg}\n"
            log_msg += "".join(traceback.format_exc()) + "\n"
            save_error_log(log_msg)
            QMessageBox.warning(self, "停止错误", error_msg)
            self.status_label.setStyleSheet("color: #28a745; font-weight: bold;")

    def display_results(self):
        """显示扫描结果"""
        try:
            self.result_tree.clear()
            group_items = {}

            for group_idx, group in enumerate(self.filtered_duplicates, 1):
                group_id = str(group_idx)
                group_items[group_id] = []

                group_color = self.group_colors[group_idx - 1] if group_idx - 1 < len(self.group_colors) else QColor(
                    240, 240, 240, 50)
                brush = QBrush(group_color)

                for file_info in group:
                    # 确保哈希字段存在
                    hash_value = file_info.get('full_hash') or file_info.get('partial_hash') or "未知"

                    file_path = file_info["path"]
                    file_name = file_info["name"]
                    file_size = file_info["size"]

                    relative_path = os.path.relpath(file_path, self.folder_path)

                    item = QTreeWidgetItem([
                        "",
                        group_id,
                        file_name,
                        format_file_size(file_size),
                        relative_path,
                        hash_value
                    ])

                    for col in range(7):
                        item.setBackground(col, brush)

                    item.setData(0, Qt.UserRole, file_path)
                    item.setData(1, Qt.UserRole, file_size)

                    item.setToolTip(2, file_path)
                    item.setToolTip(4, file_path)

                    self.result_tree.addTopLevelItem(item)
                    group_items[group_id].append(item)

                    # 添加复选框
                    checkbox = QCheckBox()
                    checkbox.setStyleSheet("margin-left: 20px;")
                    checkbox.stateChanged.connect(self.update_delete_button_state)
                    self.result_tree.setItemWidget(item, 0, checkbox)

                    # 创建操作按钮
                    action_widget = QWidget()
                    action_layout = QHBoxLayout(action_widget)
                    action_layout.setContentsMargins(2, 2, 2, 2)
                    action_layout.setSpacing(5)

                    open_file_btn = QPushButton("打开文件")
                    open_file_btn.setStyleSheet(
                        "background-color: #28a745; color: white; padding: 3px 8px; font-size: 12px;")
                    open_file_btn.setFixedWidth(70)
                    open_file_btn.clicked.connect(lambda checked, path=file_path: self.open_file(path))
                    action_layout.addWidget(open_file_btn)

                    open_path_btn = QPushButton("打开路径")
                    open_path_btn.setStyleSheet(
                        "background-color: #0d6efd; color: white; padding: 3px 8px; font-size: 12px;")
                    open_path_btn.setFixedWidth(70)
                    open_path_btn.clicked.connect(lambda checked, path=file_path: self.open_file_path(path))
                    action_layout.addWidget(open_path_btn)

                    self.result_tree.setItemWidget(item, 6, action_widget)

            self.result_tree.group_items = group_items
            self.update_delete_button_state()
        except Exception as e:
            error_msg = f"显示扫描结果时出错: {str(e)}"
            print(error_msg)
            log_msg = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {error_msg}\n"
            log_msg += "".join(traceback.format_exc()) + "\n"
            save_error_log(log_msg)
            QMessageBox.warning(self, "显示错误", error_msg)

    def update_delete_button_state(self):
        """更新删除按钮状态"""
        try:
            has_selected = any(
                self.result_tree.itemWidget(self.result_tree.topLevelItem(i), 0).isChecked()
                for i in range(self.result_tree.topLevelItemCount())
            )
            self.delete_selected_btn.setEnabled(has_selected)
            if has_selected:
                self.delete_selected_btn.setStyleSheet("background-color: #bb2d3b; color: white;")
            else:
                self.delete_selected_btn.setStyleSheet("background-color: #dc3545; color: white; opacity: 0.7;")
        except Exception as e:
            error_msg = f"更新删除按钮状态时出错: {str(e)}"
            print(error_msg)
            log_msg = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {error_msg}\n"
            log_msg += "".join(traceback.format_exc()) + "\n"
            save_error_log(log_msg)

    def select_duplicates_automatically(self):
        """一键选择重复文件，每组保留一个不勾选"""
        try:
            if not self.result_tree.group_items:
                QMessageBox.information(self, "提示", "没有可选择的重复文件组")
                return

            # 遍历所有组
            for group_id, items in self.result_tree.group_items.items():
                if len(items) >= 2:
                    # 保留第一个文件不勾选，勾选其余所有文件
                    for i, item in enumerate(items):
                        checkbox = self.result_tree.itemWidget(item, 0)
                        if checkbox:
                            # 第一个不勾选，其余勾选
                            checkbox.setChecked(i > 0)

            # 更新删除按钮状态
            self.update_delete_button_state()

            # 计算选中的文件数量
            selected_count = sum(
                1 for i in range(self.result_tree.topLevelItemCount())
                if self.result_tree.itemWidget(self.result_tree.topLevelItem(i), 0).isChecked()
            )

            self.status_label.setText(f"已自动选择 {selected_count} 个重复文件")
            self.statusBar().showMessage(f"已自动选择 {selected_count} 个重复文件，每组保留一个文件")

        except Exception as e:
            error_msg = f"自动选择重复文件时出错: {str(e)}"
            print(error_msg)
            log_msg = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {error_msg}\n"
            log_msg += "".join(traceback.format_exc()) + "\n"
            save_error_log(log_msg)
            QMessageBox.warning(self, "选择错误", error_msg)

    def deselect_all_files(self):
        """取消所有选中的文件"""
        try:
            # 遍历所有文件，取消勾选
            for i in range(self.result_tree.topLevelItemCount()):
                item = self.result_tree.topLevelItem(i)
                checkbox = self.result_tree.itemWidget(item, 0)
                if checkbox and checkbox.isChecked():
                    checkbox.setChecked(False)

            # 更新删除按钮状态
            self.update_delete_button_state()

            self.status_label.setText("已取消所有选择")
            self.statusBar().showMessage("已取消所有选择")

        except Exception as e:
            error_msg = f"取消选择时出错: {str(e)}"
            print(error_msg)
            log_msg = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {error_msg}\n"
            log_msg += "".join(traceback.format_exc()) + "\n"
            save_error_log(log_msg)
            QMessageBox.warning(self, "取消选择错误", error_msg)

    def delete_selected_files(self):
        """删除所有选中的文件"""
        try:
            selected_items = []
            for i in range(self.result_tree.topLevelItemCount()):
                item = self.result_tree.topLevelItem(i)
                checkbox = self.result_tree.itemWidget(item, 0)
                if checkbox.isChecked():
                    file_path = item.data(0, Qt.UserRole)
                    selected_items.append((item, file_path))

            if not selected_items:
                QMessageBox.information(self, "提示", "没有选中任何文件")
                return

            count = len(selected_items)
            reply = QMessageBox.question(
                self, "确认删除",
                f"确定要删除选中的 {count} 个文件吗？\n此操作不可恢复！",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No
            )

            if reply == QMessageBox.Yes:
                deleted_count = 0
                failed_count = 0
                failed_files = []

                for item, file_path in reversed(selected_items):
                    try:
                        if not os.path.exists(file_path):
                            raise Exception("文件不存在")
                        if not os.access(file_path, os.W_OK):
                            raise Exception("没有删除权限")

                        os.remove(file_path)
                        index = self.result_tree.indexOfTopLevelItem(item)
                        self.result_tree.takeTopLevelItem(index)
                        deleted_count += 1
                    except Exception as e:
                        failed_count += 1
                        failed_files.append(f"{file_path}: {str(e)}")
                        log_msg = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 删除文件失败: {file_path}: {str(e)}\n"
                        log_msg += "".join(traceback.format_exc()) + "\n"
                        save_error_log(log_msg)

                self.result_tree.viewport().update()
                self.update_duplicates_after_deletion()

                message = f"成功删除 {deleted_count} 个文件"
                if failed_count > 0:
                    message += f"\n删除失败 {failed_count} 个文件:\n" + "\n".join(failed_files[:5])
                    if len(failed_files) > 5:
                        message += f"\n...及其他 {len(failed_files) - 5} 个文件"

                QMessageBox.information(self, "删除结果", message)
                self.update_delete_button_state()
                self.statusBar().showMessage(f"删除完成: 成功删除 {deleted_count} 个文件，失败 {failed_count} 个文件")
        except Exception as e:
            error_msg = f"删除文件时出错: {str(e)}"
            print(error_msg)
            log_msg = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {error_msg}\n"
            log_msg += "".join(traceback.format_exc()) + "\n"
            save_error_log(log_msg)
            QMessageBox.warning(self, "删除错误", error_msg)

    def update_duplicates_after_deletion(self):
        """删除文件后更新重复组列表"""
        try:
            remaining_paths = set()
            for i in range(self.result_tree.topLevelItemCount()):
                item = self.result_tree.topLevelItem(i)
                file_path = item.data(0, Qt.UserRole)
                remaining_paths.add(file_path)

            new_duplicates = []
            for group in self.duplicates:
                new_group = [file_info for file_info in group if file_info["path"] in remaining_paths]
                if len(new_group) >= 2:
                    new_duplicates.append(new_group)

            self.duplicates = new_duplicates
            self.apply_filters()

            # 更新统计信息
            total_files = self.scan_worker.total_files if hasattr(self.scan_worker, 'total_files') else 0
            total_duplicates = sum(len(group) for group in self.duplicates)
            duplicate_groups = len(self.duplicates)

            saved_space = 0
            for group in self.duplicates:
                if group:
                    base_size = group[0]['size']
                    for file_info in group[1:]:
                        saved_space += file_info['size']

            self.stats_total_files.setText(f"总文件数: {total_files}")
            self.stats_duplicate_groups.setText(f"重复文件组: {duplicate_groups}")
            self.stats_duplicate_files.setText(f"重复文件总数: {total_duplicates}")
            self.stats_saved_space.setText(f"可释放空间: {format_file_size(saved_space)}")
        except Exception as e:
            error_msg = f"更新重复组列表时出错: {str(e)}"
            print(error_msg)
            log_msg = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {error_msg}\n"
            log_msg += "".join(traceback.format_exc()) + "\n"
            save_error_log(log_msg)
            QMessageBox.warning(self, "更新错误", error_msg)

    def open_file(self, file_path):
        """打开指定文件"""
        try:
            if not file_path or not os.path.exists(file_path):
                QMessageBox.warning(self, "文件不存在", f"文件不存在或已被删除：{file_path}")
                return

            if not os.access(file_path, os.R_OK):
                QMessageBox.warning(self, "权限不足", f"没有访问文件的权限：{file_path}")
                return

            try:
                # 跨平台打开文件
                if sys.platform.startswith('darwin'):  # macOS
                    subprocess.Popen(['open', file_path], start_new_session=True)
                elif os.name == 'nt':  # Windows
                    os.startfile(file_path)
                else:  # Linux
                    subprocess.Popen(['xdg-open', file_path], start_new_session=True)

                self.status_label.setText(f"已打开文件：{os.path.basename(file_path)}")
                self.statusBar().showMessage(f"已打开文件：{file_path}")
                self.status_label.setStyleSheet("color: #28a745; font-weight: bold;")
            except Exception as e:
                error_msg = f"无法打开文件: {str(e)}"
                self.status_label.setText(error_msg)
                self.statusBar().showMessage(error_msg)
                self.status_label.setStyleSheet("color: #dc3545; font-weight: bold;")
                QMessageBox.warning(self, "打开文件失败", error_msg)
        except Exception as e:
            error_msg = f"打开文件时出错: {str(e)}"
            print(error_msg)
            log_msg = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {error_msg}\n"
            log_msg += "".join(traceback.format_exc()) + "\n"
            save_error_log(log_msg)
            QMessageBox.warning(self, "打开文件错误", error_msg)
            self.status_label.setStyleSheet("color: #28a745; font-weight: bold;")

    def open_file_path(self, file_path):
        """打开文件所在的文件夹"""
        try:
            if file_path and os.path.exists(file_path):
                try:
                    dir_path = os.path.dirname(file_path)

                    if not os.access(dir_path, os.R_OK):
                        raise Exception("没有访问文件夹的权限")

                    # 跨平台打开文件夹
                    if sys.platform.startswith('darwin'):  # macOS
                        subprocess.Popen(['open', dir_path], start_new_session=True)
                    elif os.name == 'nt':  # Windows
                        os.startfile(dir_path)
                    else:  # Linux
                        subprocess.Popen(['xdg-open', dir_path], start_new_session=True)

                    self.status_label.setText(f"已打开路径：{os.path.basename(dir_path)}")
                    self.statusBar().showMessage(f"已打开路径：{dir_path}")
                    self.status_label.setStyleSheet("color: #28a745; font-weight: bold;")
                except Exception as e:
                    raise Exception(f"无法打开文件夹: {str(e)}")
            else:
                raise Exception("文件不存在")
        except Exception as e:
            error_msg = f"打开文件夹时出错: {str(e)}"
            print(error_msg)
            log_msg = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {error_msg}\n"
            log_msg += "".join(traceback.format_exc()) + "\n"
            save_error_log(log_msg)
            QMessageBox.warning(self, "打开路径错误", error_msg)
            self.status_label.setStyleSheet("color: #28a745; font-weight: bold;")

    def show_context_menu(self, position):
        """显示右键菜单"""
        try:
            item = self.result_tree.itemAt(position)
            if item:
                index = self.result_tree.indexAt(position)
                column = index.column()

                if column in [2, 4]:  # 文件名和路径列
                    menu = QMenu()
                    copy_action = menu.addAction("复制完整文件路径")
                    copy_action.triggered.connect(lambda: self.copy_file_path(item))
                    menu.exec_(self.result_tree.viewport().mapToGlobal(position))
        except Exception as e:
            error_msg = f"显示右键菜单时出错: {str(e)}"
            print(error_msg)
            log_msg = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {error_msg}\n"
            log_msg += "".join(traceback.format_exc()) + "\n"
            save_error_log(log_msg)

    def copy_file_path(self, item):
        """复制文件完整路径到剪贴板"""
        try:
            file_path = item.data(0, Qt.UserRole)
            if file_path:
                clipboard = QApplication.clipboard()
                clipboard.setText(file_path)
                self.statusBar().showMessage(f"已复制文件路径: {file_path}")
        except Exception as e:
            error_msg = f"复制文件路径时出错: {str(e)}"
            print(error_msg)
            log_msg = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {error_msg}\n"
            log_msg += "".join(traceback.format_exc()) + "\n"
            save_error_log(log_msg)
            QMessageBox.warning(self, "复制错误", error_msg)

    def show_error_message(self, message):
        """显示错误消息给用户"""
        if QThread.currentThread() != self.thread():
            QMetaObject.invokeMethod(
                self,
                "_show_error_message_impl",
                Qt.QueuedConnection,
                Q_ARG(str, message)
            )
            return

        self._show_error_message_impl(message)

    def _show_error_message_impl(self, message):
        """错误消息显示的实际实现"""
        try:
            self.status_label.setText(f"错误: {message}")
            self.statusBar().showMessage(f"错误: {message}")
            self.status_label.setStyleSheet("color: #dc3545; font-weight: bold;")
            QMessageBox.warning(self, "操作错误", message)
        except Exception as e:
            error_msg = f"显示错误消息时出错: {str(e)} 原始错误: {message}"
            print(error_msg)
            log_msg = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {error_msg}\n"
            log_msg += "".join(traceback.format_exc()) + "\n"
            save_error_log(log_msg)

    def closeEvent(self, event):
        """窗口关闭事件处理，确保线程正确终止"""
        try:
            self.cleanup_thread()
            event.accept()
        except Exception as e:
            error_msg = f"关闭窗口时出错: {str(e)}"
            print(error_msg)
            log_msg = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {error_msg}\n"
            log_msg += "".join(traceback.format_exc()) + "\n"
            save_error_log(log_msg)
            event.accept()


if __name__ == "__main__":
    try:
        app = QApplication(sys.argv)

        # 确保中文显示正常
        font = QFont()
        if sys.platform.startswith('darwin'):  # macOS
            font.setFamily("Heiti TC")  # macOS 黑体
        else:  # Windows
            font.setFamily("SimHei")  # Windows 黑体
        app.setFont(font)

        window = FileDuplicateChecker()
        window.show()
        sys.exit(app.exec_())
    except Exception as e:
        error_msg = f"程序启动时出错: {str(e)}"
        print(error_msg)
        log_msg = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {error_msg}\n"
        log_msg += "".join(traceback.format_exc()) + "\n"
        save_error_log(log_msg)
        QMessageBox.critical(None, "启动错误", error_msg)
        sys.exit(1)
