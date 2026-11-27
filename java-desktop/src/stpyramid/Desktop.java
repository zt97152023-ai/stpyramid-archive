package stpyramid;

import java.awt.*;
import java.awt.event.WindowAdapter;
import java.awt.event.WindowEvent;
import java.util.ArrayList;
import java.util.Arrays;
import javax.swing.*;

final class Desktop {
    private static final String[] MODES = {
        "HEST Count＋TIFF 压缩", "HEST 批量压缩", "HEST 原文件归档",
        "厂商原生目录归档", "TIFF 快速压缩", "核验归档", "恢复归档"
    };

    static void show() {
        var frame = new JFrame("STPyramid Java Desktop");
        frame.setDefaultCloseOperation(JFrame.DO_NOTHING_ON_CLOSE);
        var panel = new JPanel(new BorderLayout(0, 14));
        panel.setBorder(BorderFactory.createEmptyBorder(20, 22, 20, 22));
        frame.setContentPane(panel);

        var header = new JLabel("空间转录组压缩与归档");
        header.setFont(header.getFont().deriveFont(Font.BOLD, 22));
        var form = new JPanel(new GridLayout(0, 1, 0, 8));
        form.add(header);
        var capability = new JLabel(PlatformSupport.nativeCompressionStatus(NativeEngine.installedRuntime()));
        capability.setForeground(new Color(90, 90, 90));
        form.add(capability);
        var mode = new JComboBox<>(MODES);
        form.add(mode);
        var profile = new JComboBox<>(new String[]{"完整包 · Fast 091 + Shared_2", "完整包 · 常规融合", "完整包 · 极致压缩"});
        form.add(profile);
        var input = new JTextField();
        var output = new JTextField();
        var sample = new JTextField();
        var installedRuntime = NativeEngine.installedRuntime();
        var runtime = new JTextField(installedRuntime == null ? "" : installedRuntime.toString());
        var inputRow = pathRow(frame, "输入路径", input, JFileChooser.FILES_AND_DIRECTORIES, false);
        var outputRow = pathRow(frame, "输出路径", output, JFileChooser.FILES_AND_DIRECTORIES, true);
        var sampleRow = new JPanel(new BorderLayout(8, 0));
        sampleRow.add(new JLabel("样本 ID（批量可用空格分隔）"), BorderLayout.WEST);
        sampleRow.add(sample, BorderLayout.CENTER);
        var runtimeRow = pathRow(frame, "STPyramid 运行时", runtime, JFileChooser.DIRECTORIES_ONLY, false);
        form.add(inputRow); form.add(sampleRow); form.add(outputRow); form.add(runtimeRow);
        panel.add(form, BorderLayout.NORTH);

        var console = new JTextArea(15, 70);
        console.setEditable(false);
        console.setLineWrap(true);
        console.setWrapStyleWord(true);
        panel.add(new JScrollPane(console), BorderLayout.CENTER);
        var action = new JButton("开始");
        var status = new JLabel("等待任务");
        var footer = new JPanel(new BorderLayout());
        footer.add(status, BorderLayout.WEST);
        footer.add(action, BorderLayout.EAST);
        panel.add(footer, BorderLayout.SOUTH);

        Runnable update = () -> {
            int i = mode.getSelectedIndex();
            sampleRow.setVisible(i <= 2);
            profile.setVisible(i <= 1);
            outputRow.setVisible(i != 5);
            runtimeRow.setVisible(i != 2 && i != 3);
            panel.revalidate();
        };
        mode.addActionListener(e -> update.run());
        update.run();

        frame.addWindowListener(new WindowAdapter() {
            @Override public void windowClosing(WindowEvent e) {
                if (action.isEnabled()) frame.dispose();
                else status.setText("任务正在执行，请等当前操作结束");
            }
        });
        action.addActionListener(e -> {
            String in = input.getText().strip(), out = output.getText().strip();
            String id = sample.getText().strip(), installed = runtime.getText().strip();
            int operation = mode.getSelectedIndex();
            if (in.isEmpty() || (operation != 5 && out.isEmpty())) {
                status.setText("请填写所需路径"); return;
            }
            var params = new ArrayList<String>();
            String suffix = switch (profile.getSelectedIndex()) {
                case 0 -> "-fast";
                case 1 -> "-fused";
                default -> "";
            };
            switch (operation) {
                case 0 -> params.addAll(java.util.List.of("hest" + suffix, in, id, out, installed));
                case 1 -> {
                    params.addAll(java.util.List.of("hest-batch" + suffix, in, out, installed));
                    if (!id.isEmpty()) params.addAll(Arrays.asList(id.split("\\s+")));
                }
                case 2 -> params.addAll(java.util.List.of("hest-source", in, id, out));
                case 3 -> params.addAll(java.util.List.of("source", in, out));
                case 4 -> params.addAll(java.util.List.of("image", in, out, installed));
                case 5, 6 -> {
                    boolean restore = operation == 6;
                    params.addAll(java.util.List.of(restore ? "restore" : "verify", in));
                    if (restore) params.add(out);
                    if (!installed.isEmpty()) params.add(installed);
                }
            }
            action.setEnabled(false);
            console.setText("");
            status.setText("正在执行");
            new SwingWorker<Void, String>() {
                @Override protected Void doInBackground() throws Exception {
                    Main.execute(params.toArray(String[]::new), this::publish);
                    return null;
                }
                @Override protected void process(java.util.List<String> messages) {
                    for (String message : messages) console.append(message + "\n");
                    int cut = console.getDocument().getLength() - 160_000;
                    if (cut > 0) console.replaceRange("", 0, cut);
                    console.setCaretPosition(console.getDocument().getLength());
                }
                @Override protected void done() {
                    try {
                        get();
                        status.setText(operation <= 1 ? "已出包 · 可独立核验" : operation == 5 ? "核验通过" : "完成");
                    }
                    catch (Exception ex) {
                        console.append(String.valueOf(ex.getCause() == null ? ex.getMessage() : ex.getCause().getMessage()) + "\n");
                        status.setText("请查看任务日志");
                    } finally { action.setEnabled(true); }
                }
            }.execute();
        });
        frame.pack();
        frame.setMinimumSize(new Dimension(760, 550));
        frame.setLocationRelativeTo(null);
        frame.setVisible(true);
    }

    private static JPanel pathRow(JFrame frame, String title, JTextField text, int mode, boolean save) {
        var row = new JPanel(new BorderLayout(8, 0));
        row.add(new JLabel(title), BorderLayout.WEST);
        row.add(text, BorderLayout.CENTER);
        var pick = new JButton("浏览…");
        pick.addActionListener(e -> {
            var chooser = new JFileChooser();
            chooser.setFileSelectionMode(mode);
            int result = save ? chooser.showSaveDialog(frame) : chooser.showOpenDialog(frame);
            if (result == JFileChooser.APPROVE_OPTION) text.setText(chooser.getSelectedFile().getAbsolutePath());
        });
        row.add(pick, BorderLayout.EAST);
        return row;
    }
}
