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
sys.path.insert(0, str(ROOT))
from desktop_i18n import ACTIONS, LANGUAGES, MODES, stage_name, translate


@dataclass
class Job:
    name: str
    request: dict
    state: str = "Queued"
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
        self.language = "en"
        self._labels = []
        self._status = "Waiting for jobs"
        self._stage = None
        self.jobs = {}
        self.next_id = 1
        self.events = Queue()
        self.active = None
        self.process = None
        self.running = self.cancelling = self.closing = False
        self._label(self.root.title, "STPyramid · Spatial transcriptomics archive")
        self.root.minsize(965, 650)
        self.root.geometry("1100x690")
        self.root.configure(background="#edf2f5")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._style()
        self._layout()
        self.root.after(75, self._drain_events)

    def _text(self, text, **values):
        return translate(self.language, text, **values)

    def _label(self, setter, text):
        setter(self._text(text))
        self._labels.append((setter, text))

    def _label_widget(self, widget, text):
        self._label(lambda value: widget.configure(text=value), text)
        return widget

    def _change_language(self, _):
        mode = self.mode_picker.current()
        self.language = LANGUAGES[self.language_picker.get()]
        for setter, text in self._labels:
            setter(self._text(text))
        self.mode_picker.configure(values=[self._text(text) for text in MODES.values()])
        self.mode_picker.current(mode)
        for item, job in self.jobs.items():
            self._set_state(item, job.state)
        self._set_status(self._status, self._stage)

    def _job_title(self, job):
        return f"{self._text(ACTIONS[job.request['action']])} · {job.name}"

    def _set_status(self, text, stage=None):
        self._status, self._stage = text, stage
        if text is not None:
            self.current.set(self._text(text))
        elif self.active is not None:
            title = self._job_title(self.jobs[self.active])
            self.current.set(f"{title} · {stage_name(self.language, stage)}" if stage else title)

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
        self.language_picker = ttk.Combobox(header, values=tuple(LANGUAGES), state="readonly", width=12)
        self.language_picker.current(0)
        self.language_picker.pack(side="right", anchor="n")
        self.language_picker.bind("<<ComboboxSelected>>", self._change_language)
        ttk.Label(header, text="STPyramid", font=("Microsoft YaHei UI", 22, "bold")).pack(anchor="w")
        self._label_widget(ttk.Label(header, style="Sub.TLabel"),
                           "Lossless spatial transcriptomics compression and archiving").pack(anchor="w", pady=(2, 0))

        body = ttk.Frame(self.root, padding=(22, 4, 22, 18))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=4, minsize=390)
        body.columnconfigure(1, weight=5, minsize=460)
        body.rowconfigure(0, weight=1)
        tabs = ttk.Notebook(body)
        tabs.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        compression = ttk.Frame(tabs, padding=12)
        reading = ttk.Frame(tabs, padding=12)
        tabs.add(compression)
        tabs.add(reading)
        self._label(lambda value: tabs.tab(compression, text=value), "Compress & archive")
        self._label(lambda value: tabs.tab(reading, text=value), "Verify & restore")
        self._compression_controls(compression)
        self._reading_controls(reading)

        panel = ttk.Frame(body)
        panel.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(1, weight=2)
        panel.rowconfigure(6, weight=1)
        self._label_widget(ttk.Label(panel, font=("Microsoft YaHei UI", 12, "bold")), "Job queue").grid(
            row=0, column=0, sticky="w", pady=(4, 8))
        columns = ("job", "state", "size")
        queue_frame = ttk.Frame(panel)
        queue_frame.grid(row=1, column=0, sticky="nsew")
        self.tree = ttk.Treeview(queue_frame, columns=columns, show="headings", selectmode="browse", height=6)
        for column, title, width in zip(columns, ("Job", "Status", "Archive size"), (205, 89, 103)):
            self._label(lambda value, col=column: self.tree.heading(col, text=value, anchor="w"), title)
            self.tree.column(column, width=width, minwidth=width, stretch=column == "job")
        scroll = ttk.Scrollbar(queue_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda _: self._update_buttons())
        buttons = ttk.Frame(panel)
        buttons.grid(row=2, column=0, sticky="ew", pady=8)
        self.start_button = self._label_widget(ttk.Button(buttons, style="Primary.TButton", command=self.start_queue), "Start queue")
        self.cancel_button = self._label_widget(ttk.Button(buttons, command=self.cancel), "Cancel current")
        self.remove_button = self._label_widget(ttk.Button(buttons, command=self.remove), "Remove job")
        self.open_button = self._label_widget(ttk.Button(buttons, command=self.open_directory), "Open folder")
        for button in (self.start_button, self.cancel_button, self.remove_button, self.open_button):
            button.pack(side="left", padx=(0, 5))
        self.current = tk.StringVar(value=self._text(self._status))
        ttk.Label(panel, textvariable=self.current, style="Sub.TLabel", wraplength=440).grid(
            row=3, column=0, sticky="ew", pady=(6, 4))
        self.progress = ttk.Progressbar(panel, mode="determinate")
        self.progress.grid(row=4, column=0, sticky="ew", pady=(0, 10))
        self._label_widget(ttk.Label(panel), "Activity log").grid(row=5, column=0, sticky="w", pady=(0, 4))
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
        self._label_widget(ttk.Label(frame), label).grid(row=row, column=0, columnspan=2, sticky="w", pady=(8, 4))
        entry = ttk.Entry(frame, textvariable=variable)
        entry.grid(row=row + 1, column=0, sticky="ew", ipady=4)
        if browse is None:
            entry.grid_configure(columnspan=2)
            return (entry,)
        button = self._label_widget(ttk.Button(frame, command=browse), "Browse…")
        button.grid(row=row + 1, column=1, padx=(7, 0))
        return entry, button

    def _compression_controls(self, frame):
        frame.columnconfigure(0, weight=1)
        self.image, self.counts, self.metadata, self.output_root, self.archive_name = [tk.StringVar() for _ in range(5)]
        self.keep_work = tk.BooleanVar()
        self._label_widget(ttk.Label(frame), "Compression mode").grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.mode_picker = ttk.Combobox(frame, values=tuple(MODES.values()), state="readonly")
        self.mode_picker.current(0)
        self.mode_picker.grid(row=1, column=0, columnspan=2, sticky="ew", ipady=4)
        self._path_control(frame, 2, 'Whole-slide image (TIFF / BigTIFF)', self.image, self._select_image)
        self.count_controls = self._path_control(frame, 4, 'Expression and coordinates (H5AD)', self.counts,
                                               lambda: self._select_file(self.counts, [("H5AD", "*.h5ad")]))
        self.count_controls += self._path_control(frame, 6, 'Metadata attachment (optional JSON)', self.metadata,
                                                lambda: self._select_file(self.metadata, [("JSON", "*.json")]))
        self.mode_picker.bind("<<ComboboxSelected>>", self._mode_changed)
        self._path_control(frame, 8, 'Archive output folder', self.output_root, lambda: self._select_directory(self.output_root))
        self._path_control(frame, 10, 'Archive name', self.archive_name)
        self._label_widget(ttk.Checkbutton(frame, variable=self.keep_work), "Keep intermediate files after success").grid(
            row=12, column=0, columnspan=2, sticky="w", pady=(8, 5))
        self._label_widget(ttk.Button(frame, style="Primary.TButton", command=self.add_encode), "Add compression job").grid(
            row=13, column=0, columnspan=2, sticky="ew", pady=(4, 0))

    def _reading_controls(self, frame):
        frame.columnconfigure(0, weight=1)
        self.read_source, self.restore_output, self.decode_runtime = [tk.StringVar() for _ in range(3)]
        self._path_control(frame, 0, 'Archive folder (contains manifest.json)', self.read_source, self._select_archive)
        self._path_control(frame, 2, 'Restore output path', self.restore_output, self._select_restore_parent)
        self._path_control(frame, 4, 'Decoder runtime folder (optional)', self.decode_runtime,
                           lambda: self._select_directory(self.decode_runtime))
        for col, action, label in ((0, "verify", 'Add verification job'), (1, "restore", 'Add restoration job')):
            self._label_widget(ttk.Button(frame, command=lambda action=action: self.add_read(action)), label).grid(
                row=6, column=col, sticky="ew", padx=(0, 5), pady=(12, 0))

    def _mode_changed(self, _):
        for control in self.count_controls:
            control.state(["disabled"] if self._selected_mode() == "image" else ["!disabled"])

    def _selected_mode(self):
        return tuple(MODES)[self.mode_picker.current()]

    def _select_file(self, variable, types):
        value = filedialog.askopenfilename(parent=self.root, filetypes=types + [(self._text("All files"), "*")])
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
                self.output_root.set(str(path.parent / "STPyramid-archives"))
            sibling = path.with_suffix(".h5ad")
            self.counts.set(str(sibling) if sibling.is_file() else "")
            sibling = path.with_suffix(".json")
            self.metadata.set(str(sibling) if sibling.is_file() else "")

    def _select_archive(self):
        value = self._select_directory(self.read_source)
        if value:
            archive = Path(value)
            self.restore_output.set(str(archive.with_name(f"{archive.stem}-restored")))

    def _select_restore_parent(self):
        value = filedialog.askdirectory(parent=self.root, mustexist=True)
        if value:
            suffix = Path(self.read_source.get()).stem or "STPyramid"
            self.restore_output.set(str(Path(value) / f"{suffix}-restored"))

    def add_encode(self):
        image, counts, meta, output_root, name = (field.get().strip() for field in
                                                   (self.image, self.counts, self.metadata, self.output_root, self.archive_name))
        mode = self._selected_mode()
        if not image or not Path(image).is_file():
            return self._warn('Select a valid TIFF file.')
        if mode != "image" and (not counts or not Path(counts).is_file() or (meta and not Path(meta).is_file())):
            return self._warn('Select a valid H5AD file and, optionally, a JSON attachment.')
        name = name.removesuffix(".stpyramid")
        if (not output_root or not name or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
                or name.endswith((".", " ")) or Path(f"{name}.stpyramid").is_reserved()):
            return self._warn('Enter an output folder and a valid archive name.')
        output = Path(output_root) / f"{name}.stpyramid"
        self._add_job(name, {"action": "encode", "mode": mode, "image": image, "counts": counts,
                    "metadata": meta, "output": str(output.absolute()), "keep_work": self.keep_work.get()})

    def add_read(self, action):
        source, output, runtime = (v.get().strip() for v in (self.read_source, self.restore_output, self.decode_runtime))
        if not source or not (Path(source) / "manifest.json").is_file():
            return self._warn('Select an archive folder containing manifest.json.')
        if action == "restore" and not output:
            return self._warn('Enter a restore output path.')
        self._add_job(Path(source).name, {"action": action, "archive": source, "output": output, "runtime": runtime})

    def _add_job(self, name, request):
        if request["action"] != "verify":
            output = Path(request["output"]).resolve()
            if output.exists() or output.is_symlink() or any(
                    job.state in ('Queued', 'Running') and job.request["action"] != "verify"
                    and Path(job.request["output"]).resolve() == output for job in self.jobs.values()):
                return self._warn('The output path already exists or is queued. Choose a new path.')
        item = str(self.next_id)
        self.next_id += 1
        self.jobs[item] = Job(name, request)
        self.tree.insert("", "end", iid=item)
        self._set_state(item, "Queued")
        self.tree.selection_set(item)
        if self.active is None:
            self._set_status("Ready to start")
        self._update_buttons()

    def start_queue(self):
        if self.active is None and not self.running:
            self.running = True
            self._start_next()

    def _start_next(self):
        item = next((item for item, job in self.jobs.items() if job.state == 'Queued'), None)
        if item is None or not self.running:
            self.running = False
            self._set_status("Queue finished" if item is None else "Queue paused")
            self._update_buttons()
            return
        job = self.jobs[item]
        self.active = item
        self._set_state(item, 'Running')
        self._set_status(None)
        self._log(self._job_title(job))
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
            request = dict(job.request, language=self.language)
            Thread(target=self._read_worker, args=(item, self.process, request), daemon=True).start()
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
            self._set_status(None, event["begin"])
            self._log(stage_name(self.language, event["begin"]))
        elif "end" in event:
            stage = stage_name(self.language, event["end"])
            self._log(f"{stage} · {event['seconds']:.1f} s")
        elif "scratch" in event:
            job.scratch = event["scratch"]
        elif "result" in event:
            job.result = event["result"]
        elif "error" in event:
            job.error = event["error"]
            self._log(job.error)
        elif "cancel_error" in event:
            self.cancelling = self.closing = False
            self._set_status("The job is still running because cancellation failed.")
            self._log(self._text(event["cancel_error"]))
            self._update_buttons()
        elif "message" in event:
            self._log(event["message"])
        elif "exit" in event:
            succeeded = event["exit"] == 0 and bool(job.result)
            state = 'Completed' if succeeded else 'Cancelled' if self.cancelling else 'Failed'
            self._set_state(item, state)
            self._log(f"{self._job_title(job)} · {self._text(state)}")
            if succeeded:
                self._log(job.result["output"])
                if job.result.get("verification") == "not_run":
                    self._log(self._text("Archive published. Run Verify to check lossless recovery."))
                elif job.request["action"] == "verify":
                    self._log(self._text("Lossless verification passed."))
            elif job.scratch and Path(job.scratch).is_dir():
                self._log(self._text("Intermediate files retained at {path}", path=job.scratch))
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
        self.tree.item(item, values=(self._job_title(job), self._text(state), size))

    def _log(self, message):
        if message:
            self.log.configure(state="normal")
            self.log.insert("end", f"[{strftime('%H:%M:%S')}] {message}\n")
            self.log.see("end")
            self.log.configure(state="disabled")

    def _warn(self, message):
        messagebox.showwarning("STPyramid", self._text(message), parent=self.root)

    def _selected(self):
        return next(iter(self.tree.selection()), None)

    def _update_buttons(self):
        item = self._selected()
        job = self.jobs.get(item)
        self.start_button.configure(state="normal" if not self.running and any(j.state == 'Queued' for j in self.jobs.values()) else "disabled")
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
        self._set_status("Cancelling the current job")
        self._update_buttons()
        Thread(target=self._terminate_worker, args=(self.active, self.process), daemon=True).start()

    def _terminate_worker(self, item, process):
        try:
            taskkill = Path(os.environ["WINDIR"]) / "System32/taskkill.exe"
            status = subprocess.run([str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
            if status.returncode and process.poll() is None:
                self.events.put((item, {"cancel_error": 'Windows could not terminate the current job.'}))
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.events.put((item, {"cancel_error": str(exc)}))

    def close(self):
        if self.active is None:
            self.root.destroy()
        elif messagebox.askyesno(self._text("Exit STPyramid"), self._text("Cancel the current job and exit?"), parent=self.root):
            self.closing = True
            self.cancel()


def main():
    root = tk.Tk()
    ArchiveApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
