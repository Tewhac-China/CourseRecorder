import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
from PyQt5.QtWidgets import QApplication

sys.argv = ['test']
app = QApplication(sys.argv)

from src.core.config import Config
from src.ui.main_window import MainWindow

config = Config()
window = MainWindow(config)
toolbar = window.toolbar

print('Toolbar actions count:', len(toolbar.actions()))
for i, a in enumerate(toolbar.actions()):
    print(f'  [{i}] type={type(a).__name__} text="{a.text()}"')

print()
print('btn_refresh_camera exists:', hasattr(window, 'btn_refresh_camera'))
print('btn_camera_toggle exists:', hasattr(window, 'btn_camera_toggle'))
print('btn_refresh_camera text:', window.btn_refresh_camera.text())
print('btn_camera_toggle text:', window.btn_camera_toggle.text())
