import sys
import serial
import serial.tools.list_ports
import pyqtgraph as pg
from PyQt6 import QtWidgets, QtCore
import numpy as np
import struct

class RelativeAxisItem(pg.AxisItem):
    """Custom AxisItem that shows labels relative to an active offset."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.offset = 0.0

    def setOffset(self, offset):
        if self.offset != offset:
            self.offset = offset
            self.picture = None 
            self.update()

    def tickStrings(self, values, scale, spacing):
        return [f"{v - self.offset:.1f}" for v in values]

class DraggableCurve(pg.PlotCurveItem):
    """Custom PlotCurveItem that updates a spinbox when dragged vertically and handles selection."""
    def __init__(self, ch_num, spinbox, main_window, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ch_num = ch_num
        self.spinbox = spinbox
        self.main_window = main_window
        self.setClickable(True, width=20)
        self.last_mouse_y = None

    def mouseClickEvent(self, ev):
        if ev.button() == QtCore.Qt.MouseButton.LeftButton:
            self.main_window.set_active_channel(self.ch_num)
            ev.accept()

    def mouseDoubleClickEvent(self, ev):
        if ev.button() == QtCore.Qt.MouseButton.LeftButton:
            self.spinbox.setValue(0.0)
            ev.accept()

    def mouseDragEvent(self, ev):
        if ev.button() != QtCore.Qt.MouseButton.LeftButton:
            ev.ignore()
            return
        vb = self.getViewBox()
        if not vb: return
        if ev.isStart():
            self.main_window.set_active_channel(self.ch_num)
            pos = ev.buttonDownScenePos()
            self.last_mouse_y = vb.mapSceneToView(pos).y()
            ev.accept()
        elif ev.isFinish():
            self.last_mouse_y = None
            ev.accept()
        else:
            pos = ev.scenePos()
            current_y = vb.mapSceneToView(pos).y()
            if self.last_mouse_y is not None:
                dy = current_y - self.last_mouse_y
                self.spinbox.setValue(self.spinbox.value() + dy)
                self.last_mouse_y = current_y
            ev.accept()

class ADCViewer(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()

        # --- Settings ---
        self.samples_per_ch = 1000
        self.display_points = 500
        self.adc_vref = 3.3
        self.adc_bits = 12
        self.adc_max_raw = (2**self.adc_bits) - 1
        self.pico_clk = 48000000
        self.current_clkdiv = 4800
        self.active_ch = 1
        
        self.color_ch1 = (255, 255, 0)      # Yellow
        self.color_ch2 = (255, 105, 180)    # Pink
        
        # --- UI Setup ---
        self.setWindowTitle("Pico 2 W 2nd-Gen Oscilloscope PRO v5")
        self.resize(1200, 950)

        central_widget = QtWidgets.QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QtWidgets.QHBoxLayout(central_widget)

        # Left side: Plot
        plot_container = QtWidgets.QWidget()
        plot_layout = QtWidgets.QVBoxLayout(plot_container)
        
        self.y_axis = RelativeAxisItem(orientation='left')
        self.plot_widget = pg.PlotWidget(axisItems={'left': self.y_axis})
        self.plot_widget.setBackground('k')
        self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
        self.plot_widget.setLabel('left', 'Voltage', units='V')
        self.plot_widget.setLabel('bottom', 'Time', units='s')
        
        self.plot_widget.setYRange(-1.5, 4.8)
        self.plot_widget.setMouseEnabled(x=False, y=False)
        self.plot_widget.hideButtons()
        
        plot_layout.addWidget(self.plot_widget)
        main_layout.addWidget(plot_container, stretch=4)

        # Right side: Controls (no change)
        controls_group = QtWidgets.QGroupBox("Controls")
        controls_layout = QtWidgets.QVBoxLayout(controls_group)
        self.port_combo = QtWidgets.QComboBox(); self.refresh_ports()
        controls_layout.addWidget(QtWidgets.QLabel("Serial Port:"))
        controls_layout.addWidget(self.port_combo)
        self.connect_btn = QtWidgets.QPushButton("Connect"); self.connect_btn.clicked.connect(self.toggle_connection)
        controls_layout.addWidget(self.connect_btn)
        
        controls_layout.addSpacing(10)
        self.ch1_group = QtWidgets.QGroupBox("Channel 1 (Yellow)")
        ch1_layout = QtWidgets.QVBoxLayout(self.ch1_group)
        ch1_layout.addWidget(QtWidgets.QLabel("Scale:"))
        self.ch1_scale = QtWidgets.QDoubleSpinBox(); self.ch1_scale.setRange(0.1, 10.0); self.ch1_scale.setValue(1.0)
        ch1_layout.addWidget(self.ch1_scale)
        ch1_layout.addWidget(QtWidgets.QLabel("Offset (V):"))
        self.ch1_offset = QtWidgets.QDoubleSpinBox(); self.ch1_offset.setRange(-10.0, 10.0); self.ch1_offset.setValue(0.0); self.ch1_offset.setDecimals(3)
        ch1_layout.addWidget(self.ch1_offset)
        controls_layout.addWidget(self.ch1_group)

        self.ch2_group = QtWidgets.QGroupBox("Channel 2 (Pink)")
        ch2_layout = QtWidgets.QVBoxLayout(self.ch2_group)
        ch2_layout.addWidget(QtWidgets.QLabel("Scale:"))
        self.ch2_scale = QtWidgets.QDoubleSpinBox(); self.ch2_scale.setRange(0.1, 10.0); self.ch2_scale.setValue(1.0)
        ch2_layout.addWidget(self.ch2_scale)
        ch2_layout.addWidget(QtWidgets.QLabel("Offset (V):"))
        self.ch2_offset = QtWidgets.QDoubleSpinBox(); self.ch2_offset.setRange(-10.0, 10.0); self.ch2_offset.setValue(0.0); self.ch2_offset.setDecimals(3)
        ch2_layout.addWidget(self.ch2_offset)
        controls_layout.addWidget(self.ch2_group)

        trig_group = QtWidgets.QGroupBox("Trigger Settings")
        trig_layout = QtWidgets.QVBoxLayout(trig_group)
        trig_layout.addWidget(QtWidgets.QLabel("Source:"))
        self.trig_src = QtWidgets.QComboBox(); self.trig_src.addItems(["None", "CH1", "CH2"])
        trig_layout.addWidget(self.trig_src)
        trig_layout.addWidget(QtWidgets.QLabel("Level (V):"))
        self.trig_level = QtWidgets.QDoubleSpinBox(); self.trig_level.setRange(0, 3.3); self.trig_level.setValue(1.65); self.trig_level.setSingleStep(0.1)
        trig_layout.addWidget(self.trig_level)
        trig_layout.addWidget(QtWidgets.QLabel("Edge:"))
        self.trig_edge = QtWidgets.QComboBox(); self.trig_edge.addItems(["Rising", "Falling"])
        trig_layout.addWidget(self.trig_edge)
        controls_layout.addWidget(trig_group)

        controls_layout.addWidget(QtWidgets.QLabel("Rate (per CH):"))
        self.srate_combo = QtWidgets.QComboBox()
        self.rates = {"250 kHz": 192, "100 kHz": 480, "50 kHz": 960, "10 kHz": 4800, "5 kHz": 9600, "1 kHz": 48000}
        self.srate_combo.addItems(list(self.rates.keys()))
        self.srate_combo.setCurrentText("5 kHz")
        self.srate_combo.currentIndexChanged.connect(self.send_sampling_rate)
        controls_layout.addWidget(self.srate_combo)

        controls_layout.addStretch()
        main_layout.addWidget(controls_group, stretch=1)

        # --- Markers (Ground References) ---
        self.m1 = pg.InfiniteLine(angle=0, pen=pg.mkPen(self.color_ch1, width=1, style=QtCore.Qt.PenStyle.DashLine), label="GND1", labelOpts={'position': 0.1, 'color': self.color_ch1})
        self.m2 = pg.InfiniteLine(angle=0, pen=pg.mkPen(self.color_ch2, width=1, style=QtCore.Qt.PenStyle.DashLine), label="GND2", labelOpts={'position': 0.9, 'color': self.color_ch2})
        self.plot_widget.addItem(self.m1); self.plot_widget.addItem(self.m2)

        # --- TRIGER Level Line & Pointer (Restored) ---
        # Fixed alignment and labeling
        self.trig_line = pg.InfiniteLine(angle=0, movable=False, label="◀ Trigger", 
                                          labelOpts={'position': 1.0, 'color': 'y', 'movable': False, 'fill': None})
        self.plot_widget.addItem(self.trig_line)
        self.trig_line.hide()

        # --- Curves ---
        self.curve1 = DraggableCurve(1, self.ch1_offset, self, pen=pg.mkPen(self.color_ch1, width=2), name="CH1")
        self.curve2 = DraggableCurve(2, self.ch2_offset, self, pen=pg.mkPen(self.color_ch2, width=2), name="CH2")
        self.plot_widget.addItem(self.curve1); self.plot_widget.addItem(self.curve2)

        # Connect UI changes
        for w in [self.trig_src, self.trig_level, self.trig_edge, self.ch1_scale, self.ch2_scale, self.ch1_offset, self.ch2_offset]:
            try: w.valueChanged.connect(self.update_ui_state)
            except: w.currentIndexChanged.connect(self.update_ui_state)

        self.set_active_channel(1)
        self.ser = None
        self.timer = QtCore.QTimer(); self.timer.timeout.connect(self.update_data)
        self.full_buffer = b""

    def set_active_channel(self, ch):
        self.active_ch = ch
        color = self.color_ch1 if ch == 1 else self.color_ch2
        self.y_axis.setPen(pg.mkPen(color, width=1)); self.y_axis.setTextPen(pg.mkPen(color))
        qss_inactive = "QGroupBox { border: 1px solid white; border-radius: 5px; margin-top: 10px; } QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 3px 0 3px; }"
        if ch == 1:
            self.ch1_group.setStyleSheet("QGroupBox { border: 2px solid yellow; border-radius: 5px; margin-top: 10px; font-weight: bold; } QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 3px 0 3px; color: yellow; }")
            self.ch2_group.setStyleSheet(qss_inactive)
        else:
            self.ch2_group.setStyleSheet("QGroupBox { border: 2px solid hotpink; border-radius: 5px; margin-top: 10px; font-weight: bold; } QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 3px 0 3px; color: hotpink; }")
            self.ch1_group.setStyleSheet(qss_inactive)
        self.update_ui_state()

    def update_ui_state(self):
        # Update Ground Markers
        self.m1.setValue(self.ch1_offset.value()); self.m2.setValue(self.ch2_offset.value())
        
        # Handle Trigger Line & Pointer
        src = self.trig_src.currentText()
        if src == "None":
            self.trig_line.hide()
        else:
            self.trig_line.show()
            color = self.color_ch1 if src == "CH1" else self.color_ch2
            color_q = pg.mkColor(color)
            
            # Use visible dashed pen for the line
            self.trig_line.setPen(pg.mkPen(color, width=1, style=QtCore.Qt.PenStyle.DashLine, alpha=150))
            
            # Pointer Label
            html = f"<span style='color: rgb({color_q.red()},{color_q.green()},{color_q.blue()}); font-size: 14pt; font-weight: bold;'>◀ Trigger</span>"
            self.trig_line.label.setHtml(html)
            
            # Calculate position on axis
            scale = self.ch1_scale.value() if src == "CH1" else self.ch2_scale.value()
            offset = self.ch1_offset.value() if src == "CH1" else self.ch2_offset.value()
            self.trig_line.setValue((self.trig_level.value() * scale) + offset)
            
        # Update Axis Relative Offset
        active_offset = self.ch1_offset.value() if self.active_ch == 1 else self.ch2_offset.value()
        self.y_axis.setOffset(active_offset)

    def send_sampling_rate(self):
        if self.ser and self.ser.is_open:
            rate_name = self.srate_combo.currentText()
            base_div = self.rates[rate_name] // 2 
            self.current_clkdiv = base_div
            cmd = b'S' + struct.pack("<I", int(base_div))
            self.ser.write(cmd)

    def refresh_ports(self):
        self.port_combo.clear()
        for p in serial.tools.list_ports.comports(): self.port_combo.addItem(p.device)

    def toggle_connection(self):
        if self.ser and self.ser.is_open:
            self.ser.close(); self.connect_btn.setText("Connect"); self.timer.stop()
        else:
            port = self.port_combo.currentText()
            if not port: return
            try:
                self.ser = serial.Serial(port, 115200, timeout=0.01); self.ser.set_buffer_size(rx_size=256000)
                self.connect_btn.setText("Disconnect"); self.full_buffer = b""; self.timer.start(16); self.send_sampling_rate()
            except Exception as e: QtWidgets.QMessageBox.critical(self, "Error", f"Could not connect: {e}")

    def update_data(self):
        if not self.ser or not self.ser.is_open: return
        try:
            if self.ser.in_waiting > 0: self.full_buffer += self.ser.read(self.ser.in_waiting)
            SYNC_HEADER = b'\x01\xef\xcd\xab'
            header_idx = self.full_buffer.rfind(SYNC_HEADER)
            if header_idx == -1:
                if len(self.full_buffer) > 20000: self.full_buffer = self.full_buffer[-8000:]
                return
            if len(self.full_buffer) < header_idx + 4008: return
            frame_data = self.full_buffer[header_idx + 8 : header_idx + 4008]
            self.full_buffer = self.full_buffer[header_idx + 4008:]
            raw = np.frombuffer(frame_data, dtype=np.uint16)
            if len(raw) != 2000: return
            ch1_v = (raw[0::2] / float(self.adc_max_raw)) * self.adc_vref
            ch2_v = (raw[1::2] / float(self.adc_max_raw)) * self.adc_vref
            trig_idx = 0
            src = self.trig_src.currentText()
            if src != "None":
                trig_v = ch1_v if src == "CH1" else ch2_v
                level = self.trig_level.value()
                edge = self.trig_edge.currentText()
                if edge == "Rising": indices = np.where((trig_v[:500-1] < level) & (trig_v[1:500] >= level))[0]
                else: indices = np.where((trig_v[:500-1] > level) & (trig_v[1:500] <= level))[0]
                if len(indices) > 0: trig_idx = indices[0]
            ch1_f = ch1_v[trig_idx : trig_idx + self.display_points]
            ch2_f = ch2_v[trig_idx : trig_idx + self.display_points]
            ch1_p = (ch1_f * self.ch1_scale.value()) + self.ch1_offset.value()
            ch2_p = (ch2_f * self.ch2_scale.value()) + self.ch2_offset.value()
            
            s_rate_ch = self.pico_clk / (2 * self.current_clkdiv)
            t_step = 1.0 / s_rate_ch
            
            # --- Centered Time Origin ---
            points = len(ch1_f)
            time_array = (np.arange(points) - points/2) * t_step
            self.curve1.setData(time_array, ch1_p); self.curve2.setData(time_array, ch2_p)
            
            # --- Fix X Range to match Centered Origin ---
            duration = points * t_step
            self.plot_widget.setXRange(-duration/2, duration/2, padding=0)
            
        except Exception as e: print(f"Update error: {e}")

if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    window = ADCViewer()
    window.show()
    sys.exit(app.exec())
