import sys
import serial
import serial.tools.list_ports
import pyqtgraph as pg
from PyQt6 import QtWidgets, QtCore
import numpy as np
import json
import os
import struct

class RelativeAxisItem(pg.AxisItem):
    """Custom AxisItem that shows labels relative to an active offset and scale."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.offset = 0.0
        self.v_scale = 1.0

    def setOffsetScale(self, offset, v_scale):
        if self.offset != offset or self.v_scale != v_scale:
            self.offset = offset
            self.v_scale = v_scale
            self.picture = None 
            self.update()

    def tickValues(self, minVal, maxVal, size):
        # Force strict 1.0 physical division spacing for oscilloscope grid
        start = np.ceil(minVal); end = np.floor(maxVal)
        major_ticks = np.arange(start, end + 1.0, 1.0)
        minor_ticks = np.arange(np.floor(minVal), np.ceil(maxVal), 0.2)
        return [(1.0, major_ticks), (0.2, minor_ticks)]

    def tickStrings(self, values, scale, spacing):
        return [f"{(v - self.offset) * self.v_scale:.2f}" for v in values]

class DraggableCurve(pg.PlotCurveItem):
    def __init__(self, ch_num, spinbox, main_window, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ch_num = ch_num
        self.spinbox = spinbox
        self.main_window = main_window
        self.setClickable(True, width=20); self.last_mouse_y = None

    def mouseClickEvent(self, ev):
        if ev.button() == QtCore.Qt.MouseButton.LeftButton:
            self.main_window.set_active_channel(self.ch_num); ev.accept()

    def mouseDoubleClickEvent(self, ev):
        if ev.button() == QtCore.Qt.MouseButton.LeftButton:
            self.spinbox.setValue(0.0); ev.accept()

    def mouseDragEvent(self, ev):
        if ev.button() != QtCore.Qt.MouseButton.LeftButton: ev.ignore(); return
        vb = self.getViewBox(); 
        if not vb: return
        if ev.isStart():
            self.main_window.set_active_channel(self.ch_num); pos = ev.buttonDownScenePos()
            self.last_mouse_y = vb.mapSceneToView(pos).y(); ev.accept()
        elif ev.isFinish(): self.last_mouse_y = None; ev.accept()
        else:
            pos = ev.scenePos(); current_y = vb.mapSceneToView(pos).y()
            if self.last_mouse_y is not None:
                dy = current_y - self.last_mouse_y; self.spinbox.setValue(self.spinbox.value() + dy)
                self.last_mouse_y = current_y
            ev.accept()

class ChannelFocusFilter(QtCore.QObject):
    def __init__(self, ch_num, main_window, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ch_num = ch_num
        self.main_window = main_window

    def eventFilter(self, obj, event):
        if event.type() in (QtCore.QEvent.Type.MouseButtonPress, QtCore.QEvent.Type.FocusIn):
            self.main_window.set_active_channel(self.ch_num)
        return super().eventFilter(obj, event)

class ADCViewer(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()

        # --- Settings ---
        self.samples_per_ch = 1000
        self.display_points = 500
        self.adc_vref = 3.3; self.adc_bits = 12; self.adc_max_raw = (2**self.adc_bits) - 1
        self.pico_clk = 48000000; self.current_clkdiv = 4800; self.active_ch = 1
        
        self.color_ch1 = (255, 255, 0); self.color_ch2 = (255, 105, 180)
        
        self.setWindowTitle("Pico 2 W Oscilloscope Pro v7.2 - Stable Sync")
        self.resize(1200, 950)

        central_widget = QtWidgets.QWidget(); self.setCentralWidget(central_widget)
        main_layout = QtWidgets.QHBoxLayout(central_widget)

        plot_container = QtWidgets.QWidget(); plot_layout = QtWidgets.QVBoxLayout(plot_container)
        self.y_axis = RelativeAxisItem(orientation='left')
        self.plot_widget = pg.PlotWidget(axisItems={'left': self.y_axis})
        self.plot_widget.setBackground('k'); self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
        self.plot_widget.getAxis('left').setTickSpacing(1.0, 0.2)
        self.plot_widget.setLabel('left', 'Voltage', units='V'); self.plot_widget.setLabel('bottom', 'Time', units='s')
        self.plot_widget.setYRange(-5.0, 5.0); self.plot_widget.setMouseEnabled(x=False, y=False); self.plot_widget.hideButtons()
        plot_layout.addWidget(self.plot_widget); main_layout.addWidget(plot_container, stretch=4)

        scroll = QtWidgets.QScrollArea(); scroll_widget = QtWidgets.QWidget(); scroll_layout = QtWidgets.QVBoxLayout(scroll_widget)
        
        # Reduce font size for all controls to ~90%
        base_font = scroll_widget.font()
        size = base_font.pointSize()
        if size > 0: base_font.setPointSize(max(6, int(size * 0.9)))
        else: base_font.setPixelSize(max(10, int(base_font.pixelSize() * 0.9)))
        scroll_widget.setFont(base_font)
        
        controls_group = QtWidgets.QGroupBox("Controls"); controls_layout = QtWidgets.QVBoxLayout(controls_group)
        self.port_combo = QtWidgets.QComboBox(); self.refresh_ports(); controls_layout.addWidget(QtWidgets.QLabel("Serial Port:")); controls_layout.addWidget(self.port_combo)
        self.connect_btn = QtWidgets.QPushButton("Connect"); self.connect_btn.clicked.connect(self.toggle_connection); controls_layout.addWidget(self.connect_btn)
        
        controls_layout.addSpacing(10)
        self.ch1_group = QtWidgets.QGroupBox("Channel 1 (Yellow)"); ch1_layout = QtWidgets.QVBoxLayout(self.ch1_group)
        self.ch1_enabled = QtWidgets.QCheckBox("Enable Channel 1"); self.ch1_enabled.setChecked(True); ch1_layout.addWidget(self.ch1_enabled)
        ch1_layout.addWidget(QtWidgets.QLabel("Scale (V/div equiv):")); self.ch1_scale = QtWidgets.QDoubleSpinBox(); self.ch1_scale.setRange(0.01, 10.0); self.ch1_scale.setValue(1.0); ch1_layout.addWidget(self.ch1_scale)
        ch1_layout.addWidget(QtWidgets.QLabel("Offset (V):")); self.ch1_offset = QtWidgets.QDoubleSpinBox(); self.ch1_offset.setRange(-10.0, 10.0); self.ch1_offset.setValue(0.0); self.ch1_offset.setDecimals(3); ch1_layout.addWidget(self.ch1_offset)
        controls_layout.addWidget(self.ch1_group); self.ch2_group = QtWidgets.QGroupBox("Channel 2 (Pink)"); ch2_layout = QtWidgets.QVBoxLayout(self.ch2_group)
        self.ch2_enabled = QtWidgets.QCheckBox("Enable Channel 2"); self.ch2_enabled.setChecked(True); ch2_layout.addWidget(self.ch2_enabled)
        ch2_layout.addWidget(QtWidgets.QLabel("Scale (V/div equiv):")); self.ch2_scale = QtWidgets.QDoubleSpinBox(); self.ch2_scale.setRange(0.01, 10.0); self.ch2_scale.setValue(1.0); ch2_layout.addWidget(self.ch2_scale)
        ch2_layout.addWidget(QtWidgets.QLabel("Offset (V):")); self.ch2_offset = QtWidgets.QDoubleSpinBox(); self.ch2_offset.setRange(-10.0, 10.0); self.ch2_offset.setValue(0.0); self.ch2_offset.setDecimals(3); ch2_layout.addWidget(self.ch2_offset)
        controls_layout.addWidget(self.ch2_group)

        trig_group = QtWidgets.QGroupBox("Trigger / Horizontal Shift"); trig_layout = QtWidgets.QVBoxLayout(trig_group)
        trig_layout.addWidget(QtWidgets.QLabel("Source:")); self.trig_src = QtWidgets.QComboBox(); self.trig_src.addItems(["None", "CH1", "CH2"]); trig_layout.addWidget(self.trig_src)
        trig_layout.addWidget(QtWidgets.QLabel("Level (0-3.3V):")); self.trig_level = QtWidgets.QDoubleSpinBox(); self.trig_level.setRange(0, 3.3); self.trig_level.setValue(1.65); self.trig_level.setSingleStep(0.1); trig_layout.addWidget(self.trig_level)
        trig_layout.addWidget(QtWidgets.QLabel("Edge:")); self.trig_edge = QtWidgets.QComboBox(); self.trig_edge.addItems(["Rising", "Falling"]); trig_layout.addWidget(self.trig_edge)
        trig_layout.addWidget(QtWidgets.QLabel("Horizontal Shift (ms):")); self.h_shift = QtWidgets.QDoubleSpinBox(); self.h_shift.setRange(-100.0, 100.0); self.h_shift.setValue(0.0); self.h_shift.setDecimals(2); trig_layout.addWidget(self.h_shift)
        controls_layout.addWidget(trig_group)
        
        gen_group = QtWidgets.QGroupBox("Generator / PWM (GP28)"); gen_layout = QtWidgets.QVBoxLayout(gen_group)
        gen_layout.addWidget(QtWidgets.QLabel("Mode:")); self.dac_type = QtWidgets.QComboBox(); self.dac_type.addItems(["Off", "Sine", "Triangle", "Square", "PWM Duty"]); gen_layout.addWidget(self.dac_type)
        gen_layout.addWidget(QtWidgets.QLabel("Freq (Hz):")); self.dac_freq = QtWidgets.QDoubleSpinBox(); self.dac_freq.setRange(1, 10000); self.dac_freq.setValue(1000); self.dac_freq.setDecimals(0); gen_layout.addWidget(self.dac_freq)
        self.dac_amp_label = QtWidgets.QLabel("Amplitude / Duty (%):"); gen_layout.addWidget(self.dac_amp_label)
        self.dac_amp = QtWidgets.QDoubleSpinBox(); self.dac_amp.setRange(0, 100); self.dac_amp.setValue(50); self.dac_amp.setDecimals(0); gen_layout.addWidget(self.dac_amp)
        for w in [self.dac_type, self.dac_freq, self.dac_amp]:
            try: w.valueChanged.connect(self.send_dac_command)
            except: w.currentIndexChanged.connect(self.send_dac_command)
        controls_layout.addWidget(gen_group)

        controls_layout.addWidget(QtWidgets.QLabel("Rate (per CH):")); self.srate_combo = QtWidgets.QComboBox()
        self.rates = {"250 kHz": 192, "100 kHz": 480, "50 kHz": 960, "10 kHz": 4800, "5 kHz": 9600, "1 kHz": 48000}
        self.srate_combo.addItems(list(self.rates.keys())); self.srate_combo.setCurrentText("5 kHz"); self.srate_combo.currentIndexChanged.connect(self.send_sampling_rate)
        controls_layout.addWidget(self.srate_combo); controls_layout.addStretch()
        scroll_layout.addWidget(controls_group); scroll.setWidget(scroll_widget); scroll.setWidgetResizable(True); main_layout.addWidget(scroll, stretch=1)

        self.m1 = pg.InfiniteLine(angle=0, pen=pg.mkPen(self.color_ch1, width=1, style=QtCore.Qt.PenStyle.DashLine), label="GND1", labelOpts={'position': 0.1, 'color': self.color_ch1})
        self.m2 = pg.InfiniteLine(angle=0, pen=pg.mkPen(self.color_ch2, width=1, style=QtCore.Qt.PenStyle.DashLine), label="GND2", labelOpts={'position': 0.9, 'color': self.color_ch2})
        self.plot_widget.addItem(self.m1); self.plot_widget.addItem(self.m2)
        self.trig_line = pg.InfiniteLine(angle=0, movable=False, label="◀ Trigger", labelOpts={'position': 1.0, 'color': 'y', 'movable': False, 'fill': None})
        self.plot_widget.addItem(self.trig_line); self.trig_line.hide()
        
        self.curve1 = DraggableCurve(1, self.ch1_offset, self, pen=pg.mkPen(self.color_ch1, width=2), name="CH1")
        self.curve2 = DraggableCurve(2, self.ch2_offset, self, pen=pg.mkPen(self.color_ch2, width=2), name="CH2")
        self.plot_widget.addItem(self.curve1); self.plot_widget.addItem(self.curve2)

        for w in [self.trig_src, self.trig_level, self.trig_edge, self.ch1_scale, self.ch2_scale, self.ch1_offset, self.ch2_offset, self.h_shift, self.ch1_enabled, self.ch2_enabled]:
            if hasattr(w, 'valueChanged'): w.valueChanged.connect(self.update_ui_state)
            elif hasattr(w, 'currentIndexChanged'): w.currentIndexChanged.connect(self.update_ui_state)
            elif hasattr(w, 'toggled'): w.toggled.connect(self.update_ui_state)

        self.ch1_filter = ChannelFocusFilter(1, self)
        for w in [self.ch1_group, self.ch1_scale, self.ch1_offset, self.ch1_enabled]: w.installEventFilter(self.ch1_filter)
        self.ch2_filter = ChannelFocusFilter(2, self)
        for w in [self.ch2_group, self.ch2_scale, self.ch2_offset, self.ch2_enabled]: w.installEventFilter(self.ch2_filter)

        self.set_active_channel(1); self.ser = None; self.timer = QtCore.QTimer(); self.timer.timeout.connect(self.update_data); self.full_buffer = b""
        self.load_settings()

    def load_settings(self):
        if not os.path.exists("settings.json"): return
        try:
            with open("settings.json", "r") as f: s = json.load(f)
            if "port" in s:
                idx = self.port_combo.findText(s["port"])
                if idx >= 0: self.port_combo.setCurrentIndex(idx)
            if "ch1_enabled" in s: self.ch1_enabled.setChecked(s["ch1_enabled"])
            if "ch2_enabled" in s: self.ch2_enabled.setChecked(s["ch2_enabled"])
            if "ch1_scale" in s: self.ch1_scale.setValue(s["ch1_scale"])
            if "ch1_offset" in s: self.ch1_offset.setValue(s["ch1_offset"])
            if "ch2_scale" in s: self.ch2_scale.setValue(s["ch2_scale"])
            if "ch2_offset" in s: self.ch2_offset.setValue(s["ch2_offset"])
            if "trig_src" in s: self.trig_src.setCurrentText(s["trig_src"])
            if "trig_level" in s: self.trig_level.setValue(s["trig_level"])
            if "trig_edge" in s: self.trig_edge.setCurrentText(s["trig_edge"])
            if "h_shift" in s: self.h_shift.setValue(s["h_shift"])
            if "dac_type" in s: self.dac_type.setCurrentText(s["dac_type"])
            if "dac_freq" in s: self.dac_freq.setValue(s["dac_freq"])
            if "dac_amp" in s: self.dac_amp.setValue(s["dac_amp"])
            if "srate_combo" in s: self.srate_combo.setCurrentText(s["srate_combo"])
        except Exception as e: print(f"Error loading settings: {e}")

    def save_settings(self):
        s = {
            "port": self.port_combo.currentText(),
            "ch1_enabled": self.ch1_enabled.isChecked(), "ch2_enabled": self.ch2_enabled.isChecked(),
            "ch1_scale": self.ch1_scale.value(), "ch1_offset": self.ch1_offset.value(),
            "ch2_scale": self.ch2_scale.value(), "ch2_offset": self.ch2_offset.value(),
            "trig_src": self.trig_src.currentText(), "trig_level": self.trig_level.value(), "trig_edge": self.trig_edge.currentText(),
            "h_shift": self.h_shift.value(),
            "dac_type": self.dac_type.currentText(), "dac_freq": self.dac_freq.value(), "dac_amp": self.dac_amp.value(),
            "srate_combo": self.srate_combo.currentText()
        }
        try:
            with open("settings.json", "w") as f: json.dump(s, f)
        except Exception as e: print(f"Error saving settings: {e}")

    def closeEvent(self, event):
        self.save_settings()
        if self.ser and self.ser.is_open: self.ser.close()
        super().closeEvent(event)

    def set_active_channel(self, ch):
        self.active_ch = ch; color = self.color_ch1 if ch == 1 else self.color_ch2
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
        self.m1.setValue(self.ch1_offset.value()); self.m2.setValue(self.ch2_offset.value())
        src = self.trig_src.currentText()
        if src == "None": self.trig_line.hide()
        else:
            self.trig_line.show(); color = self.color_ch1 if src == "CH1" else self.color_ch2; color_q = pg.mkColor(color)
            self.trig_line.setPen(pg.mkPen(color, width=1, style=QtCore.Qt.PenStyle.DashLine, alpha=150))
            self.trig_line.label.setHtml(f"<span style='color: rgb({color_q.red()},{color_q.green()},{color_q.blue()}); font-size: 14pt; font-weight: bold;'>◀ Trigger</span>")
            scale = self.ch1_scale.value() if src == "CH1" else self.ch2_scale.value(); offset = self.ch1_offset.value() if src == "CH1" else self.ch2_offset.value()
            self.trig_line.setValue((self.trig_level.value() / scale) + offset)
        active_offset = self.ch1_offset.value() if self.active_ch == 1 else self.ch2_offset.value()
        active_scale = self.ch1_scale.value() if self.active_ch == 1 else self.ch2_scale.value()
        self.y_axis.setOffsetScale(active_offset, active_scale)
        if self.dac_type.currentText() == "PWM Duty": self.dac_amp_label.setText("Duty Cycle (%):")
        else: self.dac_amp_label.setText("Amplitude (%):")

    def send_dac_command(self):
        if self.ser and self.ser.is_open:
            type_idx = self.dac_type.currentIndex(); freq = int(self.dac_freq.value()); amp = float(self.dac_amp.value() / 100.0)
            cmd = b'W' + struct.pack("<BI f", type_idx, freq, amp); self.ser.write(cmd)

    def send_sampling_rate(self):
        if self.ser and self.ser.is_open:
            base_div = self.rates[self.srate_combo.currentText()] // 2; self.current_clkdiv = base_div
            cmd = b'S' + struct.pack("<I", int(base_div)); self.ser.write(cmd)

    def refresh_ports(self):
        self.port_combo.clear()
        for p in serial.tools.list_ports.comports(): self.port_combo.addItem(p.device)

    def toggle_connection(self):
        if self.ser and self.ser.is_open: self.ser.close(); self.connect_btn.setText("Connect"); self.timer.stop()
        else:
            port = self.port_combo.currentText()
            if not port: return
            try:
                self.ser = serial.Serial(port, 115200, timeout=0.01); self.ser.set_buffer_size(rx_size=256000)
                self.connect_btn.setText("Disconnect"); self.full_buffer = b""; self.timer.start(16); self.send_sampling_rate(); self.send_dac_command()
            except Exception as e: QtWidgets.QMessageBox.critical(self, "Error", f"Could not connect: {e}")

    def update_data(self):
        if not self.ser or not self.ser.is_open: return
        try:
            if self.ser.in_waiting > 0: self.full_buffer += self.ser.read(self.ser.in_waiting)
            
            SYNC_HEADER = b'\x01\xef\xcd\xab'; SYNC_FOOTER = b'\xef\xbe\xad\xde'
            FRAME_SIZE = 4012; found_frame = False
            
            while True:
                header_idx = self.full_buffer.find(SYNC_HEADER)
                if header_idx == -1:
                    if len(self.full_buffer) > 100000: self.full_buffer = self.full_buffer[-8000:]
                    break
                
                # Discard junk before header
                if header_idx > 0: self.full_buffer = self.full_buffer[header_idx:]; header_idx = 0
                
                if len(self.full_buffer) < FRAME_SIZE: break
                
                # Verify Footer to ensure frame integrity
                if self.full_buffer[FRAME_SIZE-4:FRAME_SIZE] != SYNC_FOOTER:
                    # Corrupt frame alignment, discard the bad header and continue searching
                    self.full_buffer = self.full_buffer[4:]
                    continue
                
                # We have a full, mathematically verified frame! Extract it
                frame_data = self.full_buffer[8:FRAME_SIZE-4]
                self.full_buffer = self.full_buffer[FRAME_SIZE:]
                raw = np.frombuffer(frame_data, dtype=np.uint16)
                if len(raw) == 2000:
                    found_frame = True; latest_raw = raw
                
                # Limit catch-up to prevent blocking GUI
                if len(self.full_buffer) > 20000:
                    last_header = self.full_buffer.rfind(SYNC_HEADER)
                    if last_header != -1 and len(self.full_buffer) >= last_header + FRAME_SIZE:
                        self.full_buffer = self.full_buffer[last_header:]
            
            if not found_frame: return
            
            ch1_v = (latest_raw[0::2] / float(self.adc_max_raw)) * self.adc_vref
            ch2_v = (latest_raw[1::2] / float(self.adc_max_raw)) * self.adc_vref
            s_rate_ch = self.pico_clk / (2.0 * self.current_clkdiv); t_step = 1.0 / s_rate_ch
            
            h_shift_samples = int((self.h_shift.value() / 1000.0) / t_step)
            half_display = self.display_points // 2
            
            # Default to half_display so it fills the screen perfectly when Trigger is Off
            trig_found_idx = half_display; src = self.trig_src.currentText()
            if src != "None":
                trig_v = ch1_v if src == "CH1" else ch2_v
                level = self.trig_level.value(); edge = self.trig_edge.currentText()
                hysteresis = 0.05 # 50mV hysteresis to ignore noise spikes
                
                # Search entire buffer for trigger
                if edge == "Rising": indices = np.where((trig_v[:-1] < level - hysteresis) & (trig_v[1:] >= level))[0]
                else: indices = np.where((trig_v[:-1] > level + hysteresis) & (trig_v[1:] <= level))[0]
                
                if len(indices) > 0:
                    # Smart Pre-trigger selection: find an edge that has enough samples before it
                    # so we don't end up with a blank space on the left side of the screen.
                    required_pre_trigger = half_display + h_shift_samples
                    valid_indices = [idx for idx in indices if idx >= required_pre_trigger]
                    
                    if len(valid_indices) > 0:
                        trig_found_idx = valid_indices[0] # The first "good" edge
                    else:
                        trig_found_idx = indices[-1] if h_shift_samples > 0 else indices[0] # Fallback
            
            # The exact index in the raw array that maps to '0 ms'
            center_idx = trig_found_idx - h_shift_samples
            start_idx = center_idx - half_display
            end_idx = center_idx + half_display
            
            # Clip bounds to available raw array without moving the center mapping
            valid_start = max(0, start_idx)
            valid_end = min(self.samples_per_ch, end_idx)
            
            if valid_start < valid_end:
                ch1_f = ch1_v[valid_start : valid_end]
                ch2_f = ch2_v[valid_start : valid_end]
                
                # The time matches the indices relative to center_idx so the geometry is preserved
                time_array = (np.arange(valid_start, valid_end) - center_idx) * t_step
                
                if self.ch1_enabled.isChecked():
                    ch1_p = (ch1_f / self.ch1_scale.value()) + self.ch1_offset.value()
                    self.curve1.setData(time_array, ch1_p)
                else: self.curve1.setData([], [])
                
                if self.ch2_enabled.isChecked():
                    ch2_p = (ch2_f / self.ch2_scale.value()) + self.ch2_offset.value()
                    self.curve2.setData(time_array, ch2_p)
                else: self.curve2.setData([], [])
            else:
                self.curve1.setData([], []); self.curve2.setData([], [])
                
            # Keep X Range grid permanently fixed!
            self.plot_widget.setXRange(-half_display * t_step, (half_display - 1) * t_step, padding=0)
            
        except Exception as e: print(f"Processing error: {e}"); self.full_buffer = b""

if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    window = ADCViewer()
    window.show()
    sys.exit(app.exec())
