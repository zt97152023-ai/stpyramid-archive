"""Windows desktop interface for the STPyramid archive workflows."""
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from queue import Empty, Queue
import re
import subprocess
import sys
from threading import Thread
from time import strftime
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

ROOT = Path(__file__).resolve().parent
MODES = {"Count＋图像（极致压缩）": "integrated", "仅图像（快速融合）": "image"}
STAGES = {
    "native_image_census": "扫描原生图像层", "read_source_and_census": "扫描原生图像层",
    "verify_census": "核对图块信息", "pin_runtimes": "检查解码运行时",
    "shared_count_encode": "编码 Count 空间块", "direct_JXL9_all_tiles_including_immediate_decode_check": "编码直接 JPEG XL 图块",
    "P089_direct_dependency_package_assembly": "写入直接编码参考", "P089_correction_source_package_assembly": "组装低频校正来源",
    "L0_091_all_candidate_adapter_including_checks": "生成 L0 条件候选",
    "L0_HF_all_candidate_adapter_including_checks": "生成 L0 HF 候选",
    "L1_091_all_candidate_adapter_including_checks": "生成 L1 条件候选",
    "retained_089_091_strict_paid_record_selection": "比较候选记录大小",
    "direct_selection_and_image_package_assembly": "组装直接编码归档",
    "hf_l1_conditional_selection_and_image_package_assembly": "组装极致版图像归档",
    "collect_conditional_and_encode_direct_tiles": "汇总条件记录并编码其余图块",
    "assemble_image_package": "写入图像归档", "assemble_complete_archive": "组装 Count＋图像归档",
    "verify_counts_pixels_and_files": "无损核验 Count、图像与元数据",
    "verify_image_archive": "无损核验所有图像层", "verify_archive": "核验归档",
    "restore_archive": "恢复归档内容",
}
for _level in (0, 1):
    STAGES.update({
        f"prepare_native_L{_level}": f"准备 L{_level} 父层参考",
        f"encode_fused_L{_level}": f"编码 L{_level} 条件图块",
        f"P089_L{_level}_source_spooling": f"准备 L{_level} 父层参考",
        f"P089_L{_level}_native_all_candidates": f"生成 L{_level} 的 089 候选",
        f"P089_L{_level}_native_receipt_validation": f"核验 L{_level} 的 089 候选",
    })


@dataclass
class Job:
    label: str
    request: dict
    state: str = "等待"
    result: dict = field(default_factory=dict)
    scratch: str = ""
    error: str = ""


def file_size(size):
    if size >= 1024**3:
        return f"{size / 1024**3:.2f} GiB"
    if size >= 1024**2:
        return f"{size / 1024**2:.2f} MiB"
    return f"{size:,} B"


class ArchiveApp:
    def __init__(self, root):
        self.root = root
        self.jobs = {}
        self.next_id = 1
        self.events = Queue()
        self.active = None
        self.process = None
        self.running = self.cancelling = self.closing = False
        self.root.title("STPyramid · 空间转录组归档")
        self.root.minsize(860, 650)
        self.root.geometry("1100x690")
        self.root.configure(background="#edf2f5")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._style()
        self._layout()
        self.root.after(75, self._drain_events)

    def _style(self):
        font = ("Microsoft YaHei UI", 10)
        self.root.option_add("*Font", font)
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background="#edf2f5")
        style.configure("TLabel", background="#edf2f5", foreground="#172f40", font=font)
        style.configure("Sub.TLabel", foreground="#435b6b")
        style.configure("TButton", padding=(10, 6), font=font)
        style.configure("Primary.TButton", foreground="white", background="#0b665f", borderwidth=0)
        style.map("Primary.TButton", background=[("disabled", "#89a9a6"), ("active", "#0c766c")])
        style.configure("TNotebook", background="#edf2f5", borderwidth=0)
        style.configure("TNotebook.Tab", padding=(13, 8), font=font)
        style.map("TNotebook.Tab", background=[("selected", "#edf2f5"), ("!selected", "#dbe6eb")])
        style.configure("TCheckbutton", background="#edf2f5", font=font)
        style.configure("Treeview", rowheight=30, font=font, background="white", fieldbackground="white")
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Horizontal.TProgressbar", background="#0b665f", troughcolor="#d9e3e8")

    def _layout(self):
        header = ttk.Frame(self.root, padding=(22, 16, 22, 10))
        header.pack(fill="x")
        ttk.Label(header, text="STPyramid", font=("Microsoft YaHei UI", 22, "bold")).pack(anchor="w")
        ttk.Label(header, text="空间转录组无损压缩与归档", style="Sub.TLabel").pack(anchor="w", pady=(2, 0))

        body = ttk.Frame(self.root, padding=(22, 4, 22, 18))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=4, minsize=390)
        body.columnconfigure(1, weight=5, minsize=410)
        body.rowconfigure(0, weight=1)
        tabs = ttk.Notebook(body)
        tabs.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        compression = ttk.Frame(tabs, padding=12)
        reading = ttk.Frame(tabs, padding=12)
        tabs.add(compression, text="压缩归档")
        tabs.add(reading, text="核验与恢复")
        self._compression_controls(compression)
        self._reading_controls(reading)

        panel = ttk.Frame(body)
        panel.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(1, weight=2)
        panel.rowconfigure(6, weight=1)
        ttk.Label(panel, text="任务队列", font=("Microsoft YaHei UI", 12, "bold")).grid(
            row=0, column=0, sticky="w", pady=(4, 8))
        columns = ("job", "state", "size")
        queue_frame = ttk.Frame(panel)
        queue_frame.grid(row=1, column=0, sticky="nsew")
        self.tree = ttk.Treeview(queue_frame, columns=columns, show="headings", selectmode="browse", height=6)
        for column, title, width in zip(columns, ("任务", "状态", "完整包大小"), (205, 74, 103)):
            self.tree.heading(column, text=title, anchor="w")
            self.tree.column(column, width=width, minwidth=width, stretch=column == "job")
        scroll = ttk.Scrollbar(queue_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda _: self._update_buttons())
        buttons = ttk.Frame(panel)
        buttons.grid(row=2, column=0, sticky="ew", pady=8)
        self.start_button = ttk.Button(buttons, text="开始队列", style="Primary.TButton", command=self.start_queue)
        self.cancel_button = ttk.Button(buttons, text="取消当前", command=self.cancel)
        self.remove_button = ttk.Button(buttons, text="移除任务", command=self.remove)
        self.open_button = ttk.Button(buttons, text="打开目录", command=self.open_directory)
        for button in (self.start_button, self.cancel_button, self.remove_button, self.open_button):
            button.pack(side="left", padx=(0, 5))
        self.current = tk.StringVar(value="等待添加任务")
        ttk.Label(panel, textvariable=self.current, style="Sub.TLabel", wraplength=440).grid(
            row=3, column=0, sticky="ew", pady=(6, 4))
        self.progress = ttk.Progressbar(panel, mode="determinate")
        self.progress.grid(row=4, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(panel, text="运行日志").grid(row=5, column=0, sticky="w", pady=(0, 4))
        log_frame = ttk.Frame(panel)
        log_frame.grid(row=6, column=0, sticky="nsew")
        self.log = tk.Text(log_frame, height=6, wrap="word", state="disabled", background="#ffffff",
                           foreground="#304a5a", relief="flat", padx=8, pady=6)
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=log_scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")
        self._update_buttons()

    def _path_control(self, frame, row, label, variable, browse=None):
        ttk.Label(frame, text=label).grid(row=row, column=0, columnspan=2, sticky="w", pady=(8, 4))
        entry = ttk.Entry(frame, textvariable=variable)
        entry.grid(row=row + 1, column=0, sticky="ew", ipady=4)
        if browse is None:
            entry.grid_configure(columnspan=2)
            return (entry,)
        button = ttk.Button(frame, text="浏览…", command=browse)
        button.grid(row=row + 1, column=1, padx=(7, 0))
        return entry, button

    def _compression_controls(self, frame):
        frame.columnconfigure(0, weight=1)
        self.mode = tk.StringVar(value=next(iter(MODES)))
        self.image, self.counts, self.metadata, self.output_root, self.archive_name = [tk.StringVar() for _ in range(5)]
        self.keep_work = tk.BooleanVar()
        ttk.Label(frame, text="压缩模式").grid(row=0, column=0, sticky="w", pady=(0, 4))
        modes = ttk.Combobox(frame, textvariable=self.mode, values=tuple(MODES), state="readonly")
        modes.grid(row=1, column=0, columnspan=2, sticky="ew", ipady=4)
        self._path_control(frame, 2, "整片图像（TIFF / BigTIFF）", self.image, self._select_image)
        self.count_controls = self._path_control(frame, 4, "表达与坐标（H5AD）", self.counts,
                                               lambda: self._select_file(self.counts, [("H5AD", "*.h5ad")]))
        self.count_controls += self._path_control(frame, 6, "附加元数据（JSON，可选）", self.metadata,
                                                lambda: self._select_file(self.metadata, [("JSON", "*.json")]))
        modes.bind("<<ComboboxSelected>>", self._mode_changed)
        self._path_control(frame, 8, "归档输出目录", self.output_root, lambda: self._select_directory(self.output_root))
        self._path_control(frame, 10, "归档名称", self.archive_name)
        ttk.Checkbutton(frame, text="保留成功任务的中间文件", variable=self.keep_work).grid(
            row=12, column=0, columnspan=2, sticky="w", pady=(8, 5))
        ttk.Button(frame, text="加入压缩队列", style="Primary.TButton", command=self.add_encode).grid(
            row=13, column=0, columnspan=2, sticky="ew", pady=(4, 0))

    def _reading_controls(self, frame):
        frame.columnconfigure(0, weight=1)
        self.read_source, self.restore_output, self.decode_runtime = [tk.StringVar() for _ in range(3)]
        self._path_control(frame, 0, "归档目录（含 manifest.json）", self.read_source, self._select_archive)
        self._path_control(frame, 2, "恢复输出路径", self.restore_output, self._select_restore_parent)
        self._path_control(frame, 4, "历史归档运行时目录（可选）", self.decode_runtime,
                           lambda: self._select_directory(self.decode_runtime))
        for col, action, label in ((0, "verify", "加入核验队列"), (1, "restore", "加入恢复队列")):
            ttk.Button(frame, text=label, command=lambda action=action: self.add_read(action)).grid(
                row=6, column=col, sticky="ew", padx=(0, 5), pady=(12, 0))

    def _mode_changed(self, _):
        for control in self.count_controls:
            control.state(["!disabled"] if MODES[self.mode.get()] == "integrated" else ["disabled"])

    def _select_file(self, variable, types):
        value = filedialog.askopenfilename(parent=self.root, filetypes=types + [("所有文件", "*")])
        if value:
            variable.set(value)
        return value

    def _select_directory(self, variable):
        value = filedialog.askdirectory(parent=self.root, mustexist=True)
        if value:
            variable.set(value)
        return value

    def _select_image(self):
        value = self._select_file(self.image, [("TIFF / BigTIFF", "*.tif *.tiff *.btf *.bigtiff")])
        if value:
            path = Path(value)
            self.archive_name.set(path.stem)
            if not self.output_root.get():
                self.output_root.set(str(path.parent / "STPyramid归档"))
            sibling = path.with_suffix(".h5ad")
            self.counts.set(str(sibling) if sibling.is_file() else "")
            sibling = path.with_suffix(".json")
            self.metadata.set(str(sibling) if sibling.is_file() else "")

    def _select_archive(self):
        value = self._select_directory(self.read_source)
        if value:
            archive = Path(value)
            self.restore_output.set(str(archive.with_name(f"{archive.stem}-已恢复")))

    def _select_restore_parent(self):
        value = filedialog.askdirectory(parent=self.root, mustexist=True)
        if value:
            suffix = Path(self.read_source.get()).stem or "STPyramid"
            self.restore_output.set(str(Path(value) / f"{suffix}-已恢复"))

    def add_encode(self):
        image, counts, meta, output_root, name = (field.get().strip() for field in
                                                   (self.image, self.counts, self.metadata, self.output_root, self.archive_name))
        mode = MODES[self.mode.get()]
        if not image or not Path(image).is_file():
            return self._warn("请选择有效的 TIFF 文件。")
        if mode == "integrated" and (not counts or not Path(counts).is_file() or (meta and not Path(meta).is_file())):
            return self._warn("请选择有效的 H5AD 文件和可选的 JSON 附件。")
        name = name.removesuffix(".stpyramid")
        if (not output_root or not name or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
                or name.endswith((".", " ")) or Path(f"{name}.stpyramid").is_reserved()):
            return self._warn("请填写输出目录和有效的归档名称。")
        output = Path(output_root) / f"{name}.stpyramid"
        self._add_job(f"压缩 · {name}", {"action": "encode", "mode": mode, "image": image, "counts": counts,
                    "metadata": meta, "output": str(output.absolute()), "keep_work": self.keep_work.get()})

    def add_read(self, action):
        source, output, runtime = (v.get().strip() for v in (self.read_source, self.restore_output, self.decode_runtime))
        if not source or not (Path(source) / "manifest.json").is_file():
            return self._warn("请选择含 manifest.json 的归档目录。")
        if action == "restore" and not output:
            return self._warn("请填写恢复输出路径。")
        label = "恢复" if action == "restore" else "核验"
        self._add_job(f"{label} · {Path(source).name}", {"action": action, "archive": source, "output": output, "runtime": runtime})

    def _add_job(self, label, request):
        if request["action"] != "verify":
            output = Path(request["output"]).resolve()
            if output.exists() or output.is_symlink() or any(
                    job.state in ("等待", "处理中") and job.request["action"] != "verify"
                    and Path(job.request["output"]).resolve() == output for job in self.jobs.values()):
                return self._warn("输出路径已存在或已在队列中。请选择新路径。")
        item = str(self.next_id)
        self.next_id += 1
        self.jobs[item] = Job(label, request)
        self.tree.insert("", "end", iid=item, values=(label, "等待", ""))
        self.tree.selection_set(item)
        self.current.set("可开始队列" if self.active is None else self.current.get())
        self._update_buttons()

    def start_queue(self):
        if self.active is None and not self.running:
            self.running = True
            self._start_next()

    def _start_next(self):
        item = next((item for item, job in self.jobs.items() if job.state == "等待"), None)
        if item is None or not self.running:
            self.running = False
            self.current.set("队列已结束" if item is None else "队列已暂停")
            self._update_buttons()
            return
        job = self.jobs[item]
        self.active = item
        self._set_state(item, "处理中")
        self.current.set(job.label)
        self._log(job.label)
        self.progress.configure(mode="indeterminate", value=0)
        self.progress.start(20)
        worker_python = Path(sys.executable).with_name("python.exe")
        try:
            self.process = subprocess.Popen(
                [str(worker_python), "-I", "-B", "-X", "utf8", str(ROOT / "desktop_worker.py")], cwd=ROOT,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace", creationflags=subprocess.CREATE_NO_WINDOW)
        except OSError as exc:
            self.events.put((item, {"error": str(exc)}))
            self.events.put((item, {"exit": 1}))
        else:
            Thread(target=self._read_worker, args=(item, self.process, job.request), daemon=True).start()
        self._update_buttons()

    def _read_worker(self, item, process, request):
        try:
            with process.stdin as stream:
                json.dump(request, stream)
            with process.stdout as stream:
                for line in stream:
                    try:
                        record = json.loads(line)
                        if not isinstance(record, dict):
                            record = {"message": line.strip()}
                    except ValueError:
                        record = {"message": line.strip()}
                    self.events.put((item, record))
        except OSError as exc:
            self.events.put((item, {"error": str(exc)}))
        finally:
            self.events.put((item, {"exit": process.wait()}))

    def _drain_events(self):
        for _ in range(100):
            try:
                item, event = self.events.get_nowait()
            except Empty:
                break
            if item == self.active:
                self._handle_event(item, event)
        if not self.closing or self.active is not None:
            self.root.after(75, self._drain_events)

    def _handle_event(self, item, event):
        job = self.jobs[item]
        if "begin" in event:
            stage = STAGES.get(event["begin"], event["begin"])
            self.current.set(f"{job.label} · {stage}")
            self._log(stage)
        elif "end" in event:
            stage = STAGES.get(event["end"], event["end"])
            self._log(f"{stage} · {event['seconds']:.1f} 秒")
        elif "scratch" in event:
            job.scratch = event["scratch"]
        elif "result" in event:
            job.result = event["result"]
        elif "error" in event:
            job.error = event["error"]
            self._log(job.error)
        elif "cancel_error" in event:
            self.cancelling = self.closing = False
            self.current.set("取消失败，当前任务仍在运行")
            self._log(event["cancel_error"])
            self._update_buttons()
        elif "message" in event:
            self._log(event["message"])
        elif "exit" in event:
            succeeded = event["exit"] == 0 and bool(job.result)
            state = "已完成" if succeeded else "已取消" if self.cancelling else "失败"
            self._set_state(item, state)
            self._log(f"{job.label} · {state}")
            if succeeded:
                self._log(job.result["output"])
            elif job.scratch and Path(job.scratch).is_dir():
                self._log(f"中间文件保存在 {job.scratch}")
            self.active = self.process = None
            self.cancelling = False
            self.progress.stop()
            self.progress.configure(mode="determinate", value=100 if succeeded else 0)
            if self.closing:
                self.root.destroy()
            else:
                self._start_next()

    def _set_state(self, item, state):
        job = self.jobs[item]
        job.state = state
        size = file_size(job.result["bytes"]) if "bytes" in job.result else ""
        self.tree.item(item, values=(job.label, state, size))

    def _log(self, message):
        if message:
            self.log.configure(state="normal")
            self.log.insert("end", f"[{strftime('%H:%M:%S')}] {message}\n")
            self.log.see("end")
            self.log.configure(state="disabled")

    def _warn(self, message):
        messagebox.showwarning("STPyramid", message, parent=self.root)

    def _selected(self):
        return next(iter(self.tree.selection()), None)

    def _update_buttons(self):
        item = self._selected()
        job = self.jobs.get(item)
        self.start_button.configure(state="normal" if not self.running and any(j.state == "等待" for j in self.jobs.values()) else "disabled")
        self.cancel_button.configure(state="normal" if self.active and not self.cancelling else "disabled")
        self.remove_button.configure(state="normal" if item and item != self.active else "disabled")
        self.open_button.configure(state="normal" if job and item != self.active and (job.result or job.scratch) else "disabled")

    def remove(self):
        item = self._selected()
        if item is not None and item != self.active:
            self.tree.delete(item)
            del self.jobs[item]
            self._update_buttons()

    def open_directory(self):
        job = self.jobs.get(self._selected())
        path = (job.result.get("output") or job.scratch) if job else ""
        if path:
            try:
                os.startfile(path)
            except OSError as exc:
                self._warn(str(exc))

    def cancel(self):
        if self.process is None or self.cancelling:
            return
        self.running = False
        self.cancelling = True
        self.current.set("正在取消当前任务")
        self._update_buttons()
        Thread(target=self._terminate_worker, args=(self.active, self.process), daemon=True).start()

    def _terminate_worker(self, item, process):
        try:
            taskkill = Path(os.environ["WINDIR"]) / "System32/taskkill.exe"
            status = subprocess.run([str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
            if status.returncode and process.poll() is None:
                self.events.put((item, {"cancel_error": "Windows 未能结束当前任务。"}))
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.events.put((item, {"cancel_error": str(exc)}))

    def close(self):
        if self.active is None:
            self.root.destroy()
        elif messagebox.askyesno("退出 STPyramid", "取消当前任务并退出？", parent=self.root):
            self.closing = True
            self.cancel()


def main():
    root = tk.Tk()
    ArchiveApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
