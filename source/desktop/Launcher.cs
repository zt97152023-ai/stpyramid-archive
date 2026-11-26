using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

internal static class Launcher
{
    [STAThread]
    private static void Main()
    {
        string directory = AppDomain.CurrentDomain.BaseDirectory;
        string script = Path.Combine(directory, "app", "desktop_app.py");
        try
        {
            var start = new ProcessStartInfo
            {
                FileName = Path.Combine(directory, "python", "pythonw.exe"),
                Arguments = "-I -B -X utf8 \"" + script + "\"",
                WorkingDirectory = Path.GetDirectoryName(script),
                UseShellExecute = false
            };
            Process.Start(start);
        }
        catch (Exception error)
        {
            MessageBox.Show(error.Message, "STPyramid", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }
}
