import sys
import os
import time
import requests
import json
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QLabel, QLineEdit, QPushButton, 
                             QTextEdit, QComboBox, QCheckBox, QFileDialog, 
                             QTableWidget, QTableWidgetItem, QHeaderView, 
                             QMessageBox, QGroupBox)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer, QUrl, QMutex, QSize
from PyQt6.QtGui import QDesktopServices, QPalette, QColor, QPixmap, QIcon

# ===========================
# 1. API 交互类
# ===========================

class SoraAPI:
    def __init__(self, base_url, api_key):
        clean_url = base_url.strip().rstrip('/')
        if clean_url.endswith('/v1') or clean_url.endswith('/v2'):
            clean_url = clean_url[:-3]
        self.base_url = clean_url
        self.api_key = api_key
        self.provider = "yunwu" if "yunwu.ai" in clean_url else "bltcy"
        
        self.headers_common = {
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

    def create_generation(self, payload_data, image_path):
        if self.provider == "yunwu":
            url = f"{self.base_url}/v1/video/create"
        else:
            url = f"{self.base_url}/v1/videos"
        print(f"⏳ [API] 提交任务... URL: {url}")
        
        try:
            if self.provider == "yunwu":
                resp = requests.post(url, headers=self.headers_common, json=payload_data, timeout=300)
            else:
                data_fields = payload_data
                files = {}
                file_obj = None
                
                if image_path and os.path.exists(image_path):
                    file_obj = open(image_path, 'rb')
                    files = {'input_reference': ('reference.jpg', file_obj, 'image/jpeg')}

                # 5分钟超时
                resp = requests.post(url, headers=self.headers_common, data=data_fields, files=files, timeout=300)
                
                if file_obj: file_obj.close()
            
            print(f"📡 [Create] Code: {resp.status_code}")
            
            if resp.status_code != 200:
                try:
                    err = resp.json()
                    msg = err.get('error', {}).get('message') or err.get('message') or resp.text
                except:
                    msg = resp.text
                raise Exception(f"API 提交失败 ({resp.status_code}): {msg}")
                
            return resp.json()
            
        except requests.exceptions.Timeout:
            raise Exception("❌ 请求超时 (已等待5分钟)！请稍后在后台查看。")
        except Exception as e:
            if 'file_obj' in locals() and file_obj: file_obj.close()
            raise Exception(f"请求异常: {e}")

    def get_task_status(self, task_id):
        if self.provider == "yunwu":
            url = f"{self.base_url}/v1/video/query"
            resp = requests.get(url, headers=self.headers_common, params={"id": task_id}, timeout=15)
        else:
            url = f"{self.base_url}/v2/videos/generations/{task_id}"
            resp = requests.get(url, headers=self.headers_common, timeout=15)
        return resp

# ===========================
# 2. 异步线程
# ===========================

class SubmitWorker(QThread):
    finished = pyqtSignal(dict) 
    error = pyqtSignal(str)

    def __init__(self, api, params, image_path, image_url):
        super().__init__()
        self.api = api
        self.params = params
        self.image_path = image_path
        self.image_url = image_url

    def run(self):
        try:
            result = self.api.create_generation(self.params, self.image_path)
            task_id = result.get("task_id") or result.get("id")
            
            if not task_id:
                if isinstance(result, str): task_id = result
                else: raise Exception(f"未找到任务ID: {result}")

            self.finished.emit({
                "task_id": str(task_id),
                "prompt": self.params.get("prompt"),
                "status": "NOT_START",
                "image_path": self.image_path,
                "image_url": self.image_url
            })
        except Exception as e:
            self.error.emit(str(e))

class DownloadWorker(QThread):
    finished = pyqtSignal(str)
    
    def __init__(self, url, save_path):
        super().__init__()
        self.url = url
        self.save_path = save_path

    def run(self):
        try:
            print(f"⬇️ [Download] {self.url}")
            headers = {"User-Agent": "Mozilla/5.0 Chrome/120.0.0.0 Safari/537.36"}
            response = requests.get(self.url, headers=headers, stream=True, timeout=(20, 120))
            if response.status_code == 200:
                with open(self.save_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk: f.write(chunk)
                self.finished.emit(self.save_path)
            else:
                print(f"❌ 下载失败，状态码: {response.status_code}")
        except Exception as e:
            print(f"❌ 下载异常: {e}")

# === 核心修改：加强版轮询线程 ===
class StatusPollingWorker(QThread):
    task_update = pyqtSignal(str, str, str, str, str)
    
    def __init__(self, api, pending_task_ids):
        super().__init__()
        self.api = api
        self.pending_task_ids = pending_task_ids

    def run(self):
        for tid in self.pending_task_ids:
            try:
                resp = self.api.get_task_status(tid)
                if resp.status_code == 200:
                    data = resp.json()
                    status = data.get('status', 'UNKNOWN')
                    progress = data.get('progress', '0%')
                    fail_reason = data.get('fail_reason', '')
                    
                    # === 🔍 核心修复：全方位寻找视频 URL ===
                    output_url = ""
                    
                    # 1. 先找 data 里面的字段 (video_url / output / url)
                    raw_data = data.get('data')
                    if isinstance(raw_data, dict):
                        output_url = raw_data.get('video_url') or raw_data.get('output') or raw_data.get('url')
                        # 如果是列表，取第一个
                        if isinstance(output_url, list) and len(output_url) > 0:
                            output_url = output_url[0]

                    # 2. 如果没找到，找根目录下的字段
                    if not output_url:
                        output_url = data.get('video_url') or data.get('output') or data.get('url')
                        # 同样处理列表情况
                        if isinstance(output_url, list) and len(output_url) > 0:
                            output_url = output_url[0]

                    # 3. 调试日志：如果成功但没找到URL，打印出来
                    if status == 'SUCCESS' and not output_url:
                        print(f"⚠️ 状态成功但未找到URL，返回数据: {json.dumps(data)}")
                    
                    # 确保是字符串
                    final_url = str(output_url) if output_url else ""

                    self.task_update.emit(tid, status, progress, final_url, fail_reason)
            except Exception as e:
                # 轮询网络波动忽略
                pass

# ===========================
# 3. 主界面
# ===========================

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Sora2 Client (Auto-DL Fixed)")
        self.resize(1100, 750)
        self.tasks = []
        self.settings_path = os.path.join(os.path.dirname(__file__), "settings.json")
        self.api_key_map = self.load_settings()
        
        self.setup_ui()
        
        self.timer = QTimer()
        self.timer.timeout.connect(self.trigger_polling)
        self.timer.start(3000)
        self.polling_worker = None

        if not os.path.exists("downloads"):
            os.makedirs("downloads")

    def setup_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)

        # 左侧
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_panel.setFixedWidth(380)
        
        api_group = QGroupBox("API 配置")
        api_layout = QVBoxLayout()
        self.provider_combo = QComboBox()
        self.provider_combo.addItems(["https://api.bltcy.ai", "https://yunwu.ai"])
        self.provider_combo.currentTextChanged.connect(self.on_provider_changed)
        self.base_url_input = QLineEdit()
        self.base_url_input.setReadOnly(True)
        self.api_key_input = QLineEdit()
        self.api_key_input.setPlaceholderText("API Key (按站点保存)")
        self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.save_key_btn = QPushButton("💾 保存 API Key")
        self.save_key_btn.clicked.connect(self.save_api_key)
        api_layout.addWidget(QLabel("Base URL:"))
        api_layout.addWidget(self.provider_combo)
        api_layout.addWidget(self.base_url_input)
        api_layout.addWidget(QLabel("API Key:"))
        api_layout.addWidget(self.api_key_input)
        api_layout.addWidget(self.save_key_btn)
        api_group.setLayout(api_layout)
        
        param_group = QGroupBox("生成参数")
        param_layout = QVBoxLayout()
        self.img_btn = QPushButton("📂 选择图片")
        self.img_btn.clicked.connect(self.select_image)
        self.img_path_label = QLabel("未选择")
        self.current_img_path = None
        self.image_url_input = QLineEdit()
        self.image_url_input.setPlaceholderText("https://... (yunwu 图片链接)")
        
        self.prompt_input = QTextEdit()
        self.prompt_input.setPlaceholderText("提示词...")
        self.prompt_input.setFixedHeight(80)
        
        self.model_combo = QComboBox()
        self.model_combo.addItems(["sora-2", "sora-2-pro"])
        self.model_combo.currentTextChanged.connect(self.update_ui_state)
        
        self.ratio_combo = QComboBox()
        self.ratio_combo.addItems(["16:9 (横屏)", "9:16 (竖屏)"])
        
        self.duration_combo = QComboBox()
        self.duration_combo.addItems(["5s", "10s", "15s", "25s"])
        
        self.hd_check = QCheckBox("HD 高清 (仅Pro)")
        self.hd_check.toggled.connect(self.update_ui_state)
        
        param_layout.addWidget(self.img_btn)
        param_layout.addWidget(self.img_path_label)
        param_layout.addWidget(QLabel("图片链接 (yunwu):"))
        param_layout.addWidget(self.image_url_input)
        param_layout.addWidget(QLabel("提示词:"))
        param_layout.addWidget(self.prompt_input)
        param_layout.addWidget(QLabel("模型:"))
        param_layout.addWidget(self.model_combo)
        param_layout.addWidget(QLabel("分辨率:"))
        param_layout.addWidget(self.ratio_combo)
        param_layout.addWidget(QLabel("时长:"))
        param_layout.addWidget(self.duration_combo)
        param_layout.addWidget(self.hd_check)
        param_group.setLayout(param_layout)
        
        self.submit_btn = QPushButton("🚀 创建视频")
        self.submit_btn.setFixedHeight(45)
        self.submit_btn.clicked.connect(self.submit_task)

        left_layout.addWidget(api_group)
        left_layout.addWidget(param_group)
        left_layout.addWidget(self.submit_btn)
        left_layout.addStretch()

        # 右侧
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(["Image", "ID", "Time", "Status", "Progress", "Prompt", "Action"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        for col in range(1, 7):
            self.table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
        self.table.setIconSize(QSize(64, 64))
        right_layout.addWidget(QLabel("任务列表"))
        right_layout.addWidget(self.table)

        main_layout.addWidget(left_panel)
        main_layout.addWidget(right_panel)
        self.on_provider_changed(self.provider_combo.currentText())
        self.update_ui_state()

    def select_image(self):
        fname, _ = QFileDialog.getOpenFileName(self, 'Select', '.', 'Images (*.jpg *.png *.jpeg)')
        if fname:
            self.current_img_path = fname
            self.img_path_label.setText(os.path.basename(fname))

    def update_ui_state(self):
        is_yunwu = "yunwu.ai" in self.base_url_input.text()
        is_pro = "pro" in self.model_combo.currentText()
        self.hd_check.setEnabled(is_pro)
        if not is_pro: self.hd_check.setChecked(False)
        self.image_url_input.setEnabled(is_yunwu)
        self.image_url_input.setPlaceholderText("https://... (yunwu 图片链接)" if is_yunwu else "仅 yunwu 需要图片链接")
        allowed_durations = ["15s", "25s"] if is_yunwu else ["5s", "10s", "15s", "25s"]
        current = self.duration_combo.currentText()
        self.duration_combo.clear()
        self.duration_combo.addItems(allowed_durations)
        if current in allowed_durations:
            self.duration_combo.setCurrentText(current)

    def get_resolution_string(self):
        is_16_9 = "16:9" in self.ratio_combo.currentText()
        is_hd = self.hd_check.isChecked()
        if is_16_9: return "1792x1024" if is_hd else "1280x720"
        else: return "1024x1792" if is_hd else "720x1280"

    def get_yunwu_size(self):
        return "large" if self.hd_check.isChecked() else "small"

    def get_orientation(self):
        return "landscape" if "16:9" in self.ratio_combo.currentText() else "portrait"

    def load_settings(self):
        if not os.path.exists(self.settings_path):
            return {}
        try:
            with open(self.settings_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def save_settings(self):
        with open(self.settings_path, "w", encoding="utf-8") as f:
            json.dump(self.api_key_map, f, ensure_ascii=False, indent=2)

    def on_provider_changed(self, provider_url):
        self.base_url_input.setText(provider_url)
        self.api_key_input.setText(self.api_key_map.get(provider_url, ""))
        self.update_ui_state()

    def save_api_key(self):
        provider_url = self.base_url_input.text().strip()
        key = self.api_key_input.text().strip()
        if not provider_url:
            return
        self.api_key_map[provider_url] = key
        self.save_settings()
        QMessageBox.information(self, "提示", "API Key 已保存")

    def submit_task(self):
        base = self.base_url_input.text()
        key = self.api_key_input.text()
        prompt = self.prompt_input.toPlainText()
        if not base or not key or not prompt: return
        is_yunwu = "yunwu.ai" in base
        image_url = self.image_url_input.text().strip()
        if is_yunwu:
            if not image_url:
                QMessageBox.warning(self, "错误", "yunwu 需要图片链接")
                return
        else:
            if not self.current_img_path:
                QMessageBox.warning(self, "错误", "请选择图片")
                return

        self.submit_btn.setEnabled(False)
        self.submit_btn.setText("提交中...")
        
        api = SoraAPI(base, key)
        if is_yunwu:
            payload = {
                "images": [image_url],
                "model": self.model_combo.currentText(),
                "orientation": self.get_orientation(),
                "prompt": prompt,
                "size": self.get_yunwu_size(),
                "duration": int(self.duration_combo.currentText().replace("s", "")),
                "watermark": False,
                "private": False
            }
        else:
            # 移除 watermark
            payload = {
                "model": self.model_combo.currentText(),
                "prompt": prompt,
                "size": self.get_resolution_string(),
                "seconds": self.duration_combo.currentText().replace("s", "")
            }
        
        self.worker = SubmitWorker(api, payload, self.current_img_path, image_url)
        self.worker.finished.connect(self.on_submit_success)
        self.worker.error.connect(lambda e: [self.submit_btn.setEnabled(True), self.submit_btn.setText("🚀 创建视频"), QMessageBox.critical(self, "Error", e)])
        self.worker.start()

    def on_submit_success(self, data):
        self.submit_btn.setEnabled(True)
        self.submit_btn.setText("🚀 创建视频")
        self.tasks.insert(0, {
            "task_id": data['task_id'], "prompt": data['prompt'],
            "submit_time": time.strftime("%H:%M:%S"), "status": "NOT_START",
            "progress": "0%", "local_path": "", "downloaded": False, "url": "",
            "image_path": data.get("image_path") or "", "image_url": data.get("image_url") or ""
        })
        self.update_table()

    def trigger_polling(self):
        if not self.tasks: return
        pending_ids = [t['task_id'] for t in self.tasks if t['status'] in ['NOT_START', 'IN_PROGRESS', 'UNKNOWN']]
        if not pending_ids: return
        if self.polling_worker and self.polling_worker.isRunning(): return 

        api = SoraAPI(self.base_url_input.text(), self.api_key_input.text())
        self.polling_worker = StatusPollingWorker(api, pending_ids)
        self.polling_worker.task_update.connect(self.on_polling_update)
        self.polling_worker.start()

    def on_polling_update(self, task_id, status, progress, output_url, fail_reason):
        task = next((t for t in self.tasks if t['task_id'] == task_id), None)
        if not task: return

        task['status'] = status
        task['progress'] = progress
        if output_url: task['url'] = output_url # 保存 URL
        
        # === 核心：自动下载触发逻辑 ===
        if status == 'SUCCESS' and not task['downloaded'] and output_url:
            self.start_download(task, output_url)
        elif status == 'FAILURE':
            print(f"❌ 任务失败: {fail_reason}")
            
        self.update_table()

    def start_download(self, task, url):
        path = os.path.join("downloads", f"{task['task_id']}.mp4")
        task['local_path'] = path
        dl = DownloadWorker(url, path)
        task['dl_ref'] = dl
        def on_dl_finish(p):
            task['downloaded'] = True
            print(f"✅ 下载完成: {p}")
            self.update_table()
        dl.finished.connect(on_dl_finish)
        dl.start()

    def update_table(self):
        self.table.setRowCount(len(self.tasks))
        for r, t in enumerate(self.tasks):
            thumb_item = QTableWidgetItem()
            image_path = t.get("image_path") or ""
            if image_path and os.path.exists(image_path):
                pixmap = QPixmap(image_path)
                if not pixmap.isNull():
                    pixmap = pixmap.scaled(64, 64, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                    thumb_item.setIcon(QIcon(pixmap))
            self.table.setItem(r, 0, thumb_item)
            self.table.setItem(r, 1, QTableWidgetItem(t['task_id']))
            self.table.setItem(r, 2, QTableWidgetItem(t['submit_time']))
            
            st = QTableWidgetItem(t['status'])
            if t['status']=='SUCCESS': st.setForeground(Qt.GlobalColor.green)
            elif t['status']=='FAILURE': st.setForeground(Qt.GlobalColor.red)
            self.table.setItem(r, 3, st)
            
            self.table.setItem(r, 4, QTableWidgetItem(t['progress']))
            self.table.setItem(r, 5, QTableWidgetItem(t['prompt'][:10]))
            
            w = QWidget()
            l = QHBoxLayout(w)
            l.setContentsMargins(0,0,0,0)
            if t['status']=='SUCCESS':
                if t['downloaded']:
                    btn = QPushButton("▶️ 打开")
                    btn.setStyleSheet("background-color:#198754; color:white; font-weight:bold;")
                    btn.clicked.connect(lambda _, p=t['local_path']: QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.abspath(p))))
                    l.addWidget(btn)
                elif t.get('url'):
                    # 即使自动下载失败，这里也会显示手动下载按钮作为兜底
                    btn = QPushButton("📥 下载")
                    btn.setStyleSheet("background-color:#0d6efd; color:white;")
                    btn.clicked.connect(lambda _, task=t: self.start_download(task, task['url']))
                    l.addWidget(btn)
                else:
                    l.addWidget(QLabel("等待链接..."))
            elif t['status']=='FAILURE':
                l.addWidget(QLabel("❌ 失败"))
            else:
                l.addWidget(QLabel("⏳ 进行中"))
            self.table.setCellWidget(r, 6, w)
            self.table.setRowHeight(r, 70)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(53, 53, 53))
    palette.setColor(QPalette.ColorRole.WindowText, Qt.GlobalColor.white)
    palette.setColor(QPalette.ColorRole.Base, QColor(25, 25, 25))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(53, 53, 53))
    palette.setColor(QPalette.ColorRole.ToolTipBase, Qt.GlobalColor.white)
    palette.setColor(QPalette.ColorRole.ToolTipText, Qt.GlobalColor.white)
    palette.setColor(QPalette.ColorRole.Text, Qt.GlobalColor.white)
    palette.setColor(QPalette.ColorRole.Button, QColor(53, 53, 53))
    palette.setColor(QPalette.ColorRole.ButtonText, Qt.GlobalColor.white)
    palette.setColor(QPalette.ColorRole.BrightText, Qt.GlobalColor.red)
    palette.setColor(QPalette.ColorRole.Link, QColor(42, 130, 218))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(42, 130, 218))
    palette.setColor(QPalette.ColorRole.HighlightedText, Qt.GlobalColor.black)
    app.setPalette(palette)

    window = MainWindow()
    window.show()
    sys.exit(app.exec())
